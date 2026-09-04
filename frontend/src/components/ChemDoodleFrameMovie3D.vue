<script setup lang="ts">
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Pause, Play } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { getGeometrySdf } from "@/api";
import { loadChemRenderer } from "@/chem/useChemRenderer";
import type { CalculationFrameSummary } from "@/types";

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

interface MovieCanvasConstructor {
  new (id: string, width: number, height: number): MovieCanvas;
  PLAY_LOOP: number;
}

interface MovieApi extends ChemDoodleApi {
  MovieCanvas3D: MovieCanvasConstructor;
}

const props = withDefaults(defineProps<{
  frames: CalculationFrameSummary[];
  projectId?: string;
  height?: number;
  title?: string;
  canvasLabel?: string;
}>(), { height: 360 });

const host = ref<HTMLElement | null>(null);
const canvasId = `chemdoodle-frame-movie-${useId()}`;
const canvasGeneration = ref(0);
const loading = ref(true);
const error = ref("");
const recovering = ref(false);
const recoveryCount = ref(0);
const loadedFrameCount = ref(0);
const movieFrameCount = ref(0);
const currentFrame = ref(0);
const playing = ref(false);
const panelStyle = computed(() => ({ height: `${props.height}px` }));
const canvasElementId = computed(() => `${canvasId}-${canvasGeneration.value}`);
const webglState = computed(() => (
  error.value ? "error" : recovering.value ? "recovering" : loading.value ? "loading" : "ready"
));
const orderedFrames = computed(() => [...props.frames].sort(
  (left, right) => left.file_frame_index - right.file_frame_index,
));
const uniqueGeometryCount = computed(() => new Set(
  orderedFrames.value.map((frame) => frame.geometry_id),
).size);

let movie: MovieCanvas | null = null;
let cachedMolecules: unknown[] = [];
let resizeObserver: ResizeObserver | null = null;
let requestController: AbortController | null = null;
let contextCanvas: HTMLCanvasElement | null = null;
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
  const hadMovie = Boolean(movie || contextCanvas);
  const canvas = contextCanvas ?? currentCanvas();
  movie?.stopAnimation();
  detachContextListener();
  movie = null;
  if (hadMovie) loseContext(canvas);
  if (replaceCanvas && !unmounted) canvasGeneration.value += 1;
}

function scheduleRecovery(): void {
  if (unmounted || recoveryTimer || !cachedMolecules.length) return;
  recovering.value = true;
  loading.value = true;
  error.value = "";
  playing.value = false;
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

function configureMovie(viewer: MovieCanvas): void {
  viewer.styles.backgroundColor = "#fbfcfa";
  viewer.styles.projectionPerspective_3D = true;
  viewer.styles.atoms_display = true;
  viewer.styles.atoms_useJMOLColors = true;
  viewer.styles.atoms_sphereDiameter_3D = 0.42;
  viewer.styles.bonds_display = true;
  viewer.styles.bonds_renderAsLines_3D = false;
  viewer.styles.bonds_cylinderDiameter_3D = 0.09;
  viewer.styles.bonds_pillDiameter_3D = 0.09;
  viewer.styles.bonds_showBondOrders_3D = true;
  viewer.timeout = 140;

  const nextFrame = viewer.nextFrame.bind(viewer);
  viewer.nextFrame = (elapsed) => {
    const renderedFrame = viewer.frameNumber;
    nextFrame(elapsed);
    if (movieFrameCount.value) {
      currentFrame.value = Math.min(renderedFrame, movieFrameCount.value - 1);
    }
  };
}

function resizeMovie(): void {
  if (!host.value || !movie) return;
  movie.resize(
    Math.max(260, Math.floor(host.value.clientWidth)),
    Math.max(220, Math.floor(host.value.clientHeight)),
  );
  movie.repaint();
}

async function rebuildMovie(frameIndex: number, shouldPlay: boolean): Promise<void> {
  if (creatingMovie || unmounted || !cachedMolecules.length) return;
  creatingMovie = true;
  try {
    const ChemDoodle = await loadChemRenderer() as MovieApi;
    if (!ChemDoodle?.MovieCanvas3D) throw new Error("ChemDoodle MovieCanvas3D 未加载");
    await nextTick();
    if (unmounted || !host.value || !cachedMolecules.length) return;

    if (!movie) {
      ChemDoodle._Canvas3D.PRESERVE_DRAWING_BUFFER = true;
      const nextMovie = new ChemDoodle.MovieCanvas3D(
        canvasElementId.value,
        Math.max(260, Math.floor(host.value.clientWidth)),
        Math.max(220, Math.floor(host.value.clientHeight)),
      );
      movie = nextMovie;
      const canvas = currentCanvas();
      if (!canvas) throw new Error("WebGL canvas 创建失败");
      const gl = canvas.getContext("webgl")
        ?? canvas.getContext("experimental-webgl") as WebGLRenderingContext | null;
      if (!gl || gl.isContextLost()) throw new Error("WebGL context 不可用");
      contextCanvas = canvas;
      contextCanvas.addEventListener("webglcontextlost", handleContextLost);
      configureMovie(nextMovie);
    } else {
      movie.stopAnimation();
      movie.frames = [];
      movie.frameNumber = 0;
    }

    movie.playMode = ChemDoodle.MovieCanvas3D.PLAY_LOOP;
    for (const molecule of cachedMolecules) movie.addFrame([molecule], []);
    movieFrameCount.value = movie.frames.length;
    const boundedIndex = Math.max(0, Math.min(frameIndex, movieFrameCount.value - 1));
    const molecule = movie.frames[boundedIndex]?.mols[0];
    if (!molecule) throw new Error("文件中没有可播放的构象");
    movie.loadMolecule(molecule);
    movie.frameNumber = boundedIndex;
    currentFrame.value = boundedIndex;
    resizeMovie();
    if (shouldPlay) movie.startAnimation();
    else movie.repaint();
    resumeAfterRecovery = shouldPlay;
    playing.value = shouldPlay;
    recoveryAttempt = 0;
    recovering.value = false;
    loading.value = false;
    error.value = "";
  } catch (caught) {
    discardMovie(true);
    if (caught instanceof Error && caught.message === "ChemDoodle MovieCanvas3D 未加载") {
      recovering.value = false;
      loading.value = false;
      error.value = caught.message;
    } else {
      scheduleRecovery();
    }
  } finally {
    creatingMovie = false;
  }
}

async function loadSdfs(
  geometryIds: string[],
  controller: AbortController,
): Promise<Map<string, string>> {
  const cache = new Map<string, string>();
  let cursor = 0;
  async function worker(): Promise<void> {
    while (!controller.signal.aborted) {
      const index = cursor;
      cursor += 1;
      if (index >= geometryIds.length) return;
      const geometryId = geometryIds[index];
      const sdf = await getGeometrySdf(geometryId, props.projectId, controller.signal);
      cache.set(geometryId, sdf);
      loadedFrameCount.value = cache.size;
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(6, geometryIds.length) },
    () => worker(),
  ));
  return cache;
}

async function loadMovie(): Promise<void> {
  window.clearTimeout(recoveryTimer);
  recoveryTimer = 0;
  recoveryAttempt = 0;
  requestController?.abort();
  requestController = new AbortController();
  const controller = requestController;
  loading.value = true;
  error.value = "";
  loadedFrameCount.value = 0;
  movieFrameCount.value = 0;
  currentFrame.value = 0;
  playing.value = false;
  recovering.value = false;
  resumeAfterRecovery = true;
  cachedMolecules = [];

  try {
    const ChemDoodle = await loadChemRenderer() as MovieApi;
    if (!ChemDoodle?.MovieCanvas3D) throw new Error("ChemDoodle MovieCanvas3D 未加载");
    if (!orderedFrames.value.length) {
      discardMovie(true);
      loading.value = false;
      return;
    }

    const geometryIds = [...new Set(orderedFrames.value.map((frame) => frame.geometry_id))];
    const sdfs = await loadSdfs(geometryIds, controller);
    if (controller.signal.aborted) return;

    for (const frame of orderedFrames.value) {
      const molecule = ChemDoodle.readMOL(sdfs.get(frame.geometry_id) ?? "", 1);
      if (!molecule) throw new Error(`frame ${frame.file_frame_index + 1} 的 SDF 无法解析`);
      cachedMolecules.push(molecule);
    }
    await rebuildMovie(0, true);
  } catch (caught) {
    if (controller.signal.aborted) return;
    loading.value = false;
    error.value = caught instanceof Error ? caught.message : "全帧动画加载失败";
  }
}

function togglePlayback(): void {
  if (!movie || loading.value || error.value || !movieFrameCount.value) return;
  if (movie.isRunning()) {
    movie.stopAnimation();
    resumeAfterRecovery = false;
    playing.value = false;
  } else {
    movie.startAnimation();
    resumeAfterRecovery = true;
    playing.value = true;
  }
}

function showFrame(index: number): void {
  if (!movie || loading.value || error.value || !movieFrameCount.value) return;
  const boundedIndex = Math.max(0, Math.min(index, movieFrameCount.value - 1));
  movie.stopAnimation();
  movie.frameNumber = boundedIndex;
  const molecule = movie.frames[boundedIndex]?.mols[0];
  if (!molecule) return;
  movie.loadMolecule(molecule);
  movie.repaint();
  currentFrame.value = boundedIndex;
  resumeAfterRecovery = false;
  playing.value = false;
}

function stepFrame(delta: number): void {
  showFrame(currentFrame.value + delta);
}

function seekFrame(event: Event): void {
  showFrame(Number((event.target as HTMLInputElement).value) - 1);
}

function releaseMovie(): void {
  unmounted = true;
  window.clearTimeout(recoveryTimer);
  recoveryTimer = 0;
  requestController?.abort();
  requestController = null;
  cachedMolecules = [];
  discardMovie(false);
  currentFrame.value = 0;
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(resizeMovie);
  });
  if (host.value) resizeObserver.observe(host.value);
  void loadMovie();
});

watch(
  () => [props.projectId, ...orderedFrames.value.map((frame) => `${frame.id}:${frame.geometry_id}`)],
  () => void loadMovie(),
);

onBeforeUnmount(() => {
  releaseMovie();
  cancelAnimationFrame(resizeFrame);
  resizeObserver?.disconnect();
});
</script>

<template>
  <section
    class="frame-movie-pane"
    data-renderer="chemdoodle-movie-3d"
    :data-frame-count="movieFrameCount"
    :data-webgl-state="webglState"
    :data-webgl-recovery-count="recoveryCount"
    :style="panelStyle"
  >
    <header>
      <div><span class="eyebrow">MovieCanvas3D</span><strong>{{ title ?? "全帧结构动画" }}</strong></div>
      <span class="frame-movie-position" aria-live="polite">{{ movieFrameCount ? `第 ${currentFrame + 1} / ${movieFrameCount} 帧` : "—" }}</span>
    </header>
    <div ref="host" class="frame-movie-canvas molecule-canvas geometry-canvas-3d">
      <canvas :id="canvasElementId" :key="canvasElementId" :aria-label="canvasLabel ?? '文件全部计算帧结构动画'" role="img"></canvas>
      <div v-if="loading" class="molecule-state">{{ recovering ? "正在恢复全帧动画" : `正在加载 ${loadedFrameCount} / ${uniqueGeometryCount} 个构象` }}</div>
      <div v-else-if="error" class="molecule-state is-error">{{ error }}</div>
      <div v-else-if="!frames.length" class="molecule-state">没有可播放的计算帧</div>
    </div>
    <div class="frame-movie-controls" role="group" aria-label="动画控制">
      <button class="icon-button" type="button" title="跳到首帧" aria-label="跳到首帧" :disabled="loading || Boolean(error) || !movieFrameCount" @click="showFrame(0)">
        <ChevronsLeft :size="15" aria-hidden="true" />
      </button>
      <button class="icon-button" type="button" title="上一帧" aria-label="上一帧" :disabled="loading || Boolean(error) || !movieFrameCount || currentFrame === 0" @click="stepFrame(-1)">
        <ChevronLeft :size="15" aria-hidden="true" />
      </button>
      <button class="icon-button frame-movie-play" type="button" :title="playing ? '暂停动画' : '播放动画'" :aria-label="playing ? '暂停动画' : '播放动画'" :disabled="loading || Boolean(error) || !movieFrameCount" @click="togglePlayback">
        <Pause v-if="playing" :size="15" aria-hidden="true" />
        <Play v-else :size="15" aria-hidden="true" />
      </button>
      <button class="icon-button" type="button" title="下一帧" aria-label="下一帧" :disabled="loading || Boolean(error) || !movieFrameCount || currentFrame >= movieFrameCount - 1" @click="stepFrame(1)">
        <ChevronRight :size="15" aria-hidden="true" />
      </button>
      <button class="icon-button" type="button" title="跳到末帧" aria-label="跳到末帧" :disabled="loading || Boolean(error) || !movieFrameCount" @click="showFrame(movieFrameCount - 1)">
        <ChevronsRight :size="15" aria-hidden="true" />
      </button>
      <label class="frame-movie-slider">
        <span class="sr-only">当前帧</span>
        <input type="range" min="1" :max="Math.max(1, movieFrameCount)" :value="currentFrame + 1" :disabled="loading || Boolean(error) || !movieFrameCount" @input="seekFrame">
      </label>
    </div>
  </section>
</template>
