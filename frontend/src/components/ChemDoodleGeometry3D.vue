<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { getGeometrySdf } from "@/api";
import { loadChemRenderer } from "@/chem/useChemRenderer";

const props = withDefaults(
  defineProps<{
    geometryId: string;
    projectId?: string;
    label?: string;
    height?: number;
  }>(),
  {
    label: "三维分子构象",
    height: 240,
  },
);

const host = ref<HTMLElement | null>(null);
const canvasId = `chemdoodle-geometry-${useId()}`;
const canvasGeneration = ref(0);
const loading = ref(true);
const error = ref("");
const recovering = ref(false);
const recoveryCount = ref(0);
const style = computed(() => ({ height: `${props.height}px` }));
const canvasElementId = computed(() => `${canvasId}-${canvasGeneration.value}`);
const webglState = computed(() => (
  error.value ? "error" : recovering.value ? "recovering" : loading.value ? "loading" : "ready"
));

let viewer: ChemDoodleViewer | null = null;
let currentMolecule: unknown = null;
let resizeObserver: ResizeObserver | null = null;
let requestController: AbortController | null = null;
let contextCanvas: HTMLCanvasElement | null = null;
let resizeFrame = 0;
let recoveryTimer = 0;
let recoveryAttempt = 0;
let creatingViewer = false;
let unmounted = false;

function currentCanvas(): HTMLCanvasElement | null {
  return document.getElementById(canvasElementId.value) as HTMLCanvasElement | null;
}

function detachContextListener(): void {
  contextCanvas?.removeEventListener("webglcontextlost", handleContextLost);
  contextCanvas = null;
}

function loseContext(canvas: HTMLCanvasElement | null): void {
  const gl = canvas?.getContext("webgl")
    ?? canvas?.getContext("experimental-webgl") as WebGLRenderingContext | null;
  gl?.getExtension("WEBGL_lose_context")?.loseContext();
}

function discardViewer(replaceCanvas: boolean): void {
  const hadViewer = Boolean(viewer || contextCanvas);
  const canvas = contextCanvas ?? currentCanvas();
  detachContextListener();
  viewer = null;
  if (hadViewer) loseContext(canvas);
  if (replaceCanvas && !unmounted) canvasGeneration.value += 1;
}

function scheduleRecovery(): void {
  if (unmounted || recoveryTimer) return;
  recovering.value = true;
  loading.value = true;
  error.value = "";
  const delay = Math.min(200 * (2 ** recoveryAttempt), 4000);
  recoveryAttempt += 1;
  recoveryTimer = window.setTimeout(() => {
    recoveryTimer = 0;
    void ensureViewer();
  }, delay);
}

function handleContextLost(event: Event): void {
  event.preventDefault();
  if (event.currentTarget !== contextCanvas || unmounted) return;
  detachContextListener();
  viewer = null;
  recoveryCount.value += 1;
  canvasGeneration.value += 1;
  scheduleRecovery();
}

function configureViewer(target: ChemDoodleViewer): void {
  const bondDiameter = 0.1;
  target.styles.backgroundColor = "#fbfcfa";
  target.styles.projectionPerspective_3D = true;
  target.styles.atoms_display = true;
  target.styles.atoms_useJMOLColors = true;
  target.styles.atoms_sphereDiameter_3D = 0.45;
  target.styles.bonds_display = true;
  // Thin cylinders keep bond orders legible without relying on WebGL line widths.
  target.styles.bonds_renderAsLines_3D = false;
  target.styles.bonds_cylinderDiameter_3D = bondDiameter;
  target.styles.bonds_pillDiameter_3D = bondDiameter;
  target.styles.bonds_showBondOrders_3D = true;
}

function resizeViewer(): void {
  if (!host.value || !viewer) return;
  const width = Math.max(180, Math.floor(host.value.clientWidth));
  viewer.resize(width, props.height);
  if (currentMolecule) viewer.loadMolecule(currentMolecule);
  else viewer.repaint();
}

async function ensureViewer(): Promise<void> {
  if (viewer || creatingViewer || unmounted || !currentMolecule) return;
  creatingViewer = true;
  try {
    const ChemDoodle = await loadChemRenderer();
    await nextTick();
    if (unmounted || !host.value || !currentMolecule) return;

    ChemDoodle._Canvas3D.PRESERVE_DRAWING_BUFFER = true;
    const nextViewer = new ChemDoodle.TransformCanvas3D(
      canvasElementId.value,
      Math.max(180, Math.floor(host.value.clientWidth)),
      props.height,
    );
    viewer = nextViewer;
    const canvas = currentCanvas();
    if (!canvas) throw new Error("WebGL canvas 创建失败");
    const gl = canvas.getContext("webgl")
      ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
    if (!gl || gl.isContextLost()) throw new Error("WebGL context 不可用");

    contextCanvas = canvas;
    contextCanvas.addEventListener("webglcontextlost", handleContextLost);
    configureViewer(nextViewer);
    resizeViewer();
    recoveryAttempt = 0;
    recovering.value = false;
    loading.value = false;
    error.value = "";
  } catch (caught) {
    discardViewer(true);
    if (caught instanceof Error && caught.message === "ChemDoodle 未加载") {
      recovering.value = false;
      loading.value = false;
      error.value = caught.message;
    } else {
      scheduleRecovery();
    }
  } finally {
    creatingViewer = false;
  }
}

async function renderGeometry(): Promise<void> {
  window.clearTimeout(recoveryTimer);
  recoveryTimer = 0;
  recoveryAttempt = 0;
  requestController?.abort();
  requestController = new AbortController();
  currentMolecule = null;
  loading.value = true;
  recovering.value = false;
  error.value = "";

  try {
    const ChemDoodle = await loadChemRenderer();

    const sdf = await getGeometrySdf(props.geometryId, props.projectId, requestController.signal);
    // ChemDoodle otherwise applies its 20 px 2D bond-length multiplier to MOL coordinates.
    const molecule = ChemDoodle.readMOL(sdf, 1);
    if (!molecule) throw new Error("SDF 无法解析");
    currentMolecule = molecule;
    if (viewer) {
      resizeViewer();
      loading.value = false;
    } else {
      await ensureViewer();
    }
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") return;
    recovering.value = false;
    loading.value = false;
    error.value = caught instanceof Error ? caught.message : "三维构象绘制失败";
  }
}

function releaseViewer(): void {
  unmounted = true;
  window.clearTimeout(recoveryTimer);
  recoveryTimer = 0;
  requestController?.abort();
  requestController = null;
  currentMolecule = null;
  discardViewer(false);
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => resizeViewer());
  });
  if (host.value) resizeObserver.observe(host.value);
  void renderGeometry();
});

watch(
  () => [props.geometryId, props.projectId],
  () => void renderGeometry(),
);

onBeforeUnmount(() => {
  releaseViewer();
  cancelAnimationFrame(resizeFrame);
  resizeObserver?.disconnect();
});
</script>

<template>
  <div
    ref="host"
    class="molecule-canvas geometry-canvas-3d"
    data-renderer="chemdoodle-transform-3d"
    data-representation="enhanced-wireframe"
    :data-webgl-state="webglState"
    :data-webgl-recovery-count="recoveryCount"
    :style="style"
  >
    <canvas :id="canvasElementId" :key="canvasElementId" :aria-label="label" role="img"></canvas>
    <div v-if="loading" class="molecule-state">{{ recovering ? "正在恢复三维构象" : "正在绘制三维构象" }}</div>
    <div v-else-if="error" class="molecule-state is-error">{{ error }}</div>
  </div>
</template>
