<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { getTopologyMolfile } from "@/api";
import { loadChemRenderer } from "@/chem/useChemRenderer";

const props = withDefaults(
  defineProps<{
    topologyId: string;
    atomMapNumbers?: number[];
    label?: string;
    height?: number;
  }>(),
  {
    label: "分子结构",
    height: 184,
  },
);

const host = ref<HTMLElement | null>(null);
const canvasId = `chemdoodle-molecule-${useId()}`;
const loading = ref(true);
const error = ref("");
const formalCharges = ref<number[]>([]);
const style = computed(() => ({ height: `${props.height}px` }));
const atomMapStart = computed(() => props.atomMapNumbers?.length ? Math.min(...props.atomMapNumbers) : null);
const formalChargeSummary = computed(() => formalCharges.value.join(","));

let viewer: ChemDoodleViewer | null = null;
let currentMolecule: unknown = null;
let resizeObserver: ResizeObserver | null = null;
let requestController: AbortController | null = null;
let resizeFrame = 0;

function readMolCharges(molfile: string): Map<number, number> {
  const charges = new Map<number, number>();
  for (const line of molfile.split(/\r?\n/)) {
    const match = /^M\s+CHG\s+(\d+)(.*)$/.exec(line);
    if (!match) continue;
    const count = Number(match[1]);
    const tokens = match[2].trim().split(/\s+/);
    for (let index = 0; index < count; index += 1) {
      const atomIndex = Number(tokens[index * 2]) - 1;
      const charge = Number(tokens[index * 2 + 1]);
      if (Number.isInteger(atomIndex) && Number.isFinite(charge)) charges.set(atomIndex, charge);
    }
  }
  return charges;
}

function mappedAtomLabel(atom: ChemDoodleAtom, mapNumber: number): string {
  if (!atom.charge) return `${atom.label}:${mapNumber}`;
  const charge = Math.abs(atom.charge) === 1 ? "" : String(Math.abs(atom.charge));
  return `${atom.label}${charge}${atom.charge > 0 ? "+" : "-"}:${mapNumber}`;
}

function resizeViewer(): void {
  if (!host.value || !viewer) return;
  const width = Math.max(160, Math.floor(host.value.clientWidth));
  viewer.resize(width, props.height);
  if (currentMolecule) viewer.loadMolecule(currentMolecule);
  else viewer.repaint();
}

async function renderMolecule(): Promise<void> {
  requestController?.abort();
  requestController = new AbortController();
  loading.value = true;
  error.value = "";
  formalCharges.value = [];

  try {
    const ChemDoodle = await loadChemRenderer();
    await nextTick();
    if (!host.value) return;

    if (!viewer) {
      viewer = new ChemDoodle.ViewerCanvas(
        canvasId,
        Math.max(160, Math.floor(host.value.clientWidth)),
        props.height,
      );
      viewer.styles.backgroundColor = "#fbfcfa";
      viewer.styles.atoms_useJMOLColors = true;
      viewer.styles.atoms_font_size_2D = props.atomMapNumbers?.length ? 10 : 13;
      viewer.styles.bonds_width_2D = 1.35;
      viewer.styles.bonds_clearOverlaps_2D = true;
    }

    const molfile = await getTopologyMolfile(props.topologyId, requestController.signal);
    const molecule = ChemDoodle.readMOL(molfile);
    if (!molecule) throw new Error("molfile 无法解析");
    const molCharges = readMolCharges(molfile);
    for (const [atomIndex, charge] of molCharges) {
      const atom = molecule.atoms[atomIndex];
      if (!atom) continue;
      atom.charge = charge;
      // ChemDoodle ignores V2000 M CHG while inferring valence from a neutral atom.
      atom.implicitH = 0;
    }
    formalCharges.value = [...molCharges.values()];
    if (props.atomMapNumbers?.length) {
      if (molecule.atoms.length !== props.atomMapNumbers.length) {
        throw new Error("atom map 数量与拓扑原子数不一致");
      }
      molecule.atoms.forEach((atom, index) => {
        const mapNumber = props.atomMapNumbers?.[index];
        if (!mapNumber || mapNumber < 1) throw new Error("atom map 必须从 1 开始");
        atom.altLabel = mappedAtomLabel(atom, mapNumber);
      });
    }
    currentMolecule = molecule;
    resizeViewer();
    loading.value = false;
  } catch (caught) {
    if (caught instanceof DOMException && caught.name === "AbortError") return;
    loading.value = false;
    error.value = caught instanceof Error ? caught.message : "结构绘制失败";
  }
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => resizeViewer());
  });
  if (host.value) resizeObserver.observe(host.value);
  void renderMolecule();
});

watch(
  [() => props.topologyId, () => props.atomMapNumbers],
  () => void renderMolecule(),
);

onBeforeUnmount(() => {
  requestController?.abort();
  cancelAnimationFrame(resizeFrame);
  resizeObserver?.disconnect();
});
</script>

<template>
  <div
    ref="host"
    class="molecule-canvas"
    :style="style"
    :data-atom-mapped="atomMapNumbers?.length ? 'true' : 'false'"
    :data-atom-map-start="atomMapStart"
    :data-formal-charges="formalChargeSummary || undefined"
  >
    <canvas :id="canvasId" :aria-label="label" role="img"></canvas>
    <div v-if="loading" class="molecule-state">正在绘制</div>
    <div v-else-if="error" class="molecule-state is-error">{{ error }}</div>
  </div>
</template>
