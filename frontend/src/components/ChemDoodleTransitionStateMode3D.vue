<script setup lang="ts">
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Pause, Play } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { getTransitionStateAnchorSdf } from "@/api";
import { loadChemRenderer } from "@/chem/useChemRenderer";

type Anchor = "negative" | "center" | "positive";

interface MovieCanvas extends ChemDoodleViewer {
  frames: Array<{ mols: unknown[]; shapes: unknown[] }>;
  frameNumber: number;
  playMode: number;
  timeout: number;
  nextFrame(elapsed: number): void;
  addFrame(molecules: unknown[], shapes: unknown[]): void;
  startAnimation(): void;
  stopAnimation(): void;
  isRunning(): boolean;
}

interface MovieApi extends ChemDoodleApi {
  MovieCanvas3D: {
    new (id: string, width: number, height: number): MovieCanvas;
    PLAY_LOOP: number;
  };
}

const props = withDefaults(defineProps<{
  frameId: string;
  negativeDisplacementRatio?: number;
  positiveDisplacementRatio?: number;
  height?: number;
}>(), {
  negativeDisplacementRatio: 1,
  positiveDisplacementRatio: 1,
  height: 350,
});

const host = ref<HTMLElement | null>(null);
const canvasId = `chemdoodle-ts-mode-${useId()}`;
const canvasGeneration = ref(0);
const loading = ref(true);
const error = ref("");
const recovering = ref(false);
const recoveryCount = ref(0);
const currentFrame = ref(0);
const movieFrameCount = ref(0);
const playing = ref(false);
const canvasElementId = computed(() => `${canvasId}-${canvasGeneration.value}`);
const webglState = computed(() => (
  error.value ? "error" : recovering.value ? "recovering" : loading.value ? "loading" : "ready"
));
const panelStyle = computed(() => ({ height: `${props.height}px` }));
const signedModeRatio = computed(() => {
  const fraction = (currentFrame.value - 10) / 10;
  return fraction < 0
    ? fraction * props.negativeDisplacementRatio
    : fraction * props.positiveDisplacementRatio;
});

let movie: MovieCanvas | null = null;
let cachedMolecules: unknown[] = [];
let contextCanvas: HTMLCanvasElement | null = null;
let resizeObserver: ResizeObserver | null = null;
let requestController: AbortController | null = null;
let resizeFrame = 0;
let recoveryTimer = 0;
let recoveryAttempt = 0;
let creatingMovie = false;
let resumeAfterRecovery = true;
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

function discardMovie(replaceCanvas: boolean): void {
  const canvas = contextCanvas ?? currentCanvas();
  movie?.stopAnimation();
  detachContextListener();
  movie = null;
  loseContext(canvas);
  if (replaceCanvas && !unmounted) canvasGeneration.value += 1;
}

function scheduleRecovery(): void {
  if (unmounted || recoveryTimer || !cachedMolecules.length) return;
  recovering.value = true;
  loading.value = true;
  const delay = Math.min(200 * (2 ** recoveryAttempt), 4000);
  recoveryAttempt += 1;
  recoveryTimer = window.setTimeout(() => {
    recoveryTimer = 0;
    void rebuildMovie(currentFrame.value, resumeAfterRecovery);
  }, delay);
}

function handleContextLost(event: Event): void {
  event.preventDefault();
  if (event.currentTarget !== contextCanvas || unmounted) return;
  resumeAfterRecovery = playing.value || Boolean(movie?.isRunning());
  movie?.stopAnimation();
  detachContextListener();
  movie = null;
  playing.value = false;
  recoveryCount.value += 1;
  canvasGeneration.value += 1;
  scheduleRecovery();
}

function configureMovie(target: MovieCanvas): void {
  target.styles.backgroundColor = "#fbfcfa";
  target.styles.projectionPerspective_3D = true;
  target.styles.atoms_display = true;
  target.styles.atoms_useJMOLColors = true;
  target.styles.atoms_sphereDiameter_3D = 0.42;
  target.styles.bonds_display = true;
  target.styles.bonds_renderAsLines_3D = false;
  target.styles.bonds_cylinderDiameter_3D = 0.09;
  target.styles.bonds_pillDiameter_3D = 0.09;
  target.styles.bonds_showBondOrders_3D = true;
  target.timeout = 140;
  const nextFrame = target.nextFrame.bind(target);
  target.nextFrame = (elapsed) => {
    const renderedFrame = target.frameNumber;
    nextFrame(elapsed);
    if (movieFrameCount.value) currentFrame.value = Math.min(renderedFrame, movieFrameCount.value - 1);
  };
}

function resizeMovie(): void {
  if (!host.value || !movie) return;
  movie.resize(Math.max(240, Math.floor(host.value.clientWidth)), Math.max(210, Math.floor(host.value.clientHeight)));
  movie.repaint();
}

function copyWithCoordinates(
  ChemDoodle: ChemDoodleApi,
  sdf: string,
  center: ChemDoodleMoleculeModel,
  endpoint: ChemDoodleMoleculeModel,
  fraction: number,
): ChemDoodleMoleculeModel {
  const molecule = ChemDoodle.readMOL(sdf, 1);
  if (!molecule || molecule.atoms.length !== center.atoms.length || molecule.atoms.length !== endpoint.atoms.length) {
    throw new Error("TS mode anchors have incompatible atom order");
  }
  molecule.atoms.forEach((atom, index) => {
    const centerAtom = center.atoms[index];
    const endpointAtom = endpoint.atoms[index];
    atom.x = centerAtom.x + fraction * (endpointAtom.x - centerAtom.x);
    atom.y = centerAtom.y + fraction * (endpointAtom.y - centerAtom.y);
    atom.z = centerAtom.z + fraction * (endpointAtom.z - centerAtom.z);
  });
  return molecule;
}

function buildInterpolatedFrames(
  ChemDoodle: ChemDoodleApi,
  sdfs: Record<Anchor, string>,
): unknown[] {
  const center = ChemDoodle.readMOL(sdfs.center, 1);
  const negative = ChemDoodle.readMOL(sdfs.negative, 1);
  const positive = ChemDoodle.readMOL(sdfs.positive, 1);
  if (!center || !negative || !positive) throw new Error("TS mode anchor SDF 无法解析");
  const frames: unknown[] = [];
  for (let step = 10; step >= 1; step -= 1) {
    frames.push(copyWithCoordinates(ChemDoodle, sdfs.negative, center, negative, step / 10));
  }
  frames.push(center);
  for (let step = 1; step <= 10; step += 1) {
    frames.push(copyWithCoordinates(ChemDoodle, sdfs.positive, center, positive, step / 10));
  }
  return frames;
}

async function rebuildMovie(frameIndex: number, shouldPlay: boolean): Promise<void> {
  if (creatingMovie || unmounted || !cachedMolecules.length) return;
  creatingMovie = true;
  try {
    const ChemDoodle = await loadChemRenderer() as MovieApi;
    if (!ChemDoodle?.MovieCanvas3D) throw new Error("ChemDoodle MovieCanvas3D 未加载");
    await nextTick();
    if (unmounted || !host.value) return;
    if (!movie) {
      ChemDoodle._Canvas3D.PRESERVE_DRAWING_BUFFER = true;
      movie = new ChemDoodle.MovieCanvas3D(
        canvasElementId.value,
        Math.max(240, Math.floor(host.value.clientWidth)),
        Math.max(210, Math.floor(host.value.clientHeight)),
      );
      const canvas = currentCanvas();
      if (!canvas) throw new Error("WebGL canvas 创建失败");
      const gl = canvas.getContext("webgl")
        ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
      if (!gl || gl.isContextLost()) throw new Error("WebGL context 不可用");
      contextCanvas = canvas;
      contextCanvas.addEventListener("webglcontextlost", handleContextLost);
      configureMovie(movie);
    } else {
      movie.stopAnimation();
      movie.frames = [];
      movie.frameNumber = 0;
    }
    movie.playMode = ChemDoodle.MovieCanvas3D.PLAY_LOOP;
    for (const molecule of cachedMolecules) movie.addFrame([molecule], []);
    movieFrameCount.value = movie.frames.length;
    const bounded = Math.max(0, Math.min(frameIndex, movieFrameCount.value - 1));
    movie.loadMolecule(movie.frames[bounded].mols[0]);
    movie.frameNumber = bounded;
    currentFrame.value = bounded;
    resizeMovie();
    if (shouldPlay) movie.startAnimation();
    else movie.repaint();
    playing.value = shouldPlay;
    resumeAfterRecovery = shouldPlay;
    recoveryAttempt = 0;
    recovering.value = false;
    loading.value = false;
    error.value = "";
  } catch (caught) {
    discardMovie(true);
    if (caught instanceof Error && caught.message.includes("未加载")) {
      loading.value = false;
      recovering.value = false;
      error.value = caught.message;
    } else {
      scheduleRecovery();
    }
  } finally {
    creatingMovie = false;
  }
}

async function loadMovie(): Promise<void> {
  window.clearTimeout(recoveryTimer);
  recoveryTimer = 0;
  recoveryAttempt = 0;
  requestController?.abort();
  requestController = new AbortController();
  const controller = requestController;
  loading.value = true;
  recovering.value = false;
  error.value = "";
  playing.value = false;
  cachedMolecules = [];
  try {
    const ChemDoodle = await loadChemRenderer() as MovieApi;
    if (!ChemDoodle?.MovieCanvas3D) throw new Error("ChemDoodle MovieCanvas3D 未加载");
    const [negative, center, positive] = await Promise.all([
      getTransitionStateAnchorSdf(props.frameId, "negative", undefined, controller.signal),
      getTransitionStateAnchorSdf(props.frameId, "center", undefined, controller.signal),
      getTransitionStateAnchorSdf(props.frameId, "positive", undefined, controller.signal),
    ]);
    if (controller.signal.aborted) return;
    cachedMolecules = buildInterpolatedFrames(ChemDoodle, { negative, center, positive });
    await rebuildMovie(10, true);
  } catch (caught) {
    if (controller.signal.aborted) return;
    loading.value = false;
    recovering.value = false;
    error.value = caught instanceof Error ? caught.message : "虚频模式加载失败";
  }
}

function showFrame(index: number): void {
  if (!movie || loading.value || error.value || !movieFrameCount.value) return;
  const bounded = Math.max(0, Math.min(index, movieFrameCount.value - 1));
  movie.stopAnimation();
  movie.frameNumber = bounded;
  movie.loadMolecule(movie.frames[bounded].mols[0]);
  movie.repaint();
  currentFrame.value = bounded;
  resumeAfterRecovery = false;
  playing.value = false;
}

function seekFrame(event: Event): void {
  showFrame(Number((event.target as HTMLInputElement).value));
}

function togglePlayback(): void {
  if (!movie || loading.value || error.value) return;
  if (movie.isRunning()) {
    movie.stopAnimation();
    playing.value = false;
    resumeAfterRecovery = false;
  } else {
    movie.startAnimation();
    playing.value = true;
    resumeAfterRecovery = true;
  }
}

function releaseMovie(): void {
  unmounted = true;
  window.clearTimeout(recoveryTimer);
  requestController?.abort();
  cachedMolecules = [];
  discardMovie(false);
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(resizeMovie);
  });
  if (host.value) resizeObserver.observe(host.value);
  void loadMovie();
});

watch(() => props.frameId, () => void loadMovie());

onBeforeUnmount(() => {
  releaseMovie();
  cancelAnimationFrame(resizeFrame);
  resizeObserver?.disconnect();
});
</script>

<template>
  <section
    class="frame-movie-pane"
    data-renderer="chemdoodle-ts-mode-3d"
    :data-frame-count="movieFrameCount"
    :data-webgl-state="webglState"
    :data-webgl-recovery-count="recoveryCount"
    :style="panelStyle"
  >
    <header>
      <div><span class="eyebrow">Imaginary mode</span><strong>虚频模式插值</strong></div>
      <span class="frame-movie-position" aria-live="polite">{{ movieFrameCount ? `mode ${signedModeRatio.toFixed(3)}` : "—" }}</span>
    </header>
    <div ref="host" class="frame-movie-canvas molecule-canvas geometry-canvas-3d">
      <canvas :id="canvasElementId" :key="canvasElementId" aria-label="虚频模式插值动画" role="img"></canvas>
      <div v-if="loading" class="molecule-state">{{ recovering ? "正在恢复虚频模式" : "正在生成虚频模式锚点" }}</div>
      <div v-else-if="error" class="molecule-state is-error">{{ error }}</div>
    </div>
    <div class="frame-movie-controls" role="group" aria-label="虚频模式控制">
      <button class="icon-button" type="button" title="负方向起点" aria-label="负方向起点" :disabled="loading || Boolean(error)" @click="showFrame(0)"><ChevronsLeft :size="15" aria-hidden="true" /></button>
      <button class="icon-button" type="button" title="上一帧" aria-label="上一帧" :disabled="loading || Boolean(error) || currentFrame === 0" @click="showFrame(currentFrame - 1)"><ChevronLeft :size="15" aria-hidden="true" /></button>
      <button class="icon-button frame-movie-play" type="button" :title="playing ? '暂停动画' : '播放动画'" :aria-label="playing ? '暂停动画' : '播放动画'" :disabled="loading || Boolean(error)" @click="togglePlayback">
        <Pause v-if="playing" :size="15" aria-hidden="true" /><Play v-else :size="15" aria-hidden="true" />
      </button>
      <button class="icon-button" type="button" title="下一帧" aria-label="下一帧" :disabled="loading || Boolean(error) || currentFrame >= movieFrameCount - 1" @click="showFrame(currentFrame + 1)"><ChevronRight :size="15" aria-hidden="true" /></button>
      <button class="icon-button" type="button" title="正方向终点" aria-label="正方向终点" :disabled="loading || Boolean(error)" @click="showFrame(movieFrameCount - 1)"><ChevronsRight :size="15" aria-hidden="true" /></button>
      <label class="frame-movie-slider"><span class="sr-only">虚频模式位置</span><input type="range" min="0" :max="Math.max(0, movieFrameCount - 1)" :value="currentFrame" :disabled="loading || Boolean(error)" @input="seekFrame"></label>
    </div>
  </section>
</template>
