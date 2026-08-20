<script setup lang="ts">
import { RotateCcw } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { api } from "../api";
import { ChemDoodleEditorBridge } from "../chem/ChemDoodleEditorBridge";

const props = withDefaults(
  defineProps<{ modelValue: string; molfile?: string; height?: number; oneMolecule?: boolean }>(),
  { molfile: "", height: 260, oneMolecule: true },
);
const emit = defineEmits<{ "update:modelValue": [value: string]; "update:molfile": [value: string] }>();
const iframe = ref<HTMLIFrameElement | null>(null);
const editorId = `chemdoodle-editor-${useId()}`;
const ready = ref(false);
const error = ref("");
const editorGeneration = ref(0);
const editorHeight = ref(props.height + 122);
const localSmiles = ref(props.modelValue);
const editorSource = computed(() =>
  `/editor/chemdoodle-editor.html?oneMolecule=${props.oneMolecule ? "true" : "false"}&generation=${editorGeneration.value}`,
);
let bridge: ChemDoodleEditorBridge | null = null;
let resizeObserver: ResizeObserver | null = null;
let readyTimer = 0;
let latestEditorMolfile = "";
let awaitingSmilesLoad = false;
let conversionController: AbortController | null = null;
let conversionGeneration = 0;

function cancelConversion(): void {
  conversionGeneration += 1;
  conversionController?.abort();
  conversionController = null;
}

async function updateFromEditor(molfile: string, editorSmiles: string): Promise<void> {
  latestEditorMolfile = molfile;
  if (awaitingSmilesLoad) {
    awaitingSmilesLoad = false;
    return;
  }
  emit("update:molfile", molfile);
  cancelConversion();
  if (!molfile) {
    localSmiles.value = "";
    emit("update:modelValue", "");
    return;
  }
  const generation = conversionGeneration;
  const controller = new AbortController();
  conversionController = controller;
  try {
    const converted = await api.convertChemistryRepresentation({ molfile }, controller.signal);
    if (generation !== conversionGeneration) return;
    localSmiles.value = converted.smiles;
    emit("update:modelValue", converted.smiles);
  } catch {
    if (controller.signal.aborted || generation !== conversionGeneration) return;
    // Keep the editor usable when the API is temporarily unavailable.
    if (editorSmiles) {
      localSmiles.value = editorSmiles;
      emit("update:modelValue", editorSmiles);
    }
  } finally {
    if (conversionController === controller) conversionController = null;
  }
}

async function loadSmilesIntoEditor(smiles: string): Promise<void> {
  cancelConversion();
  if (!smiles) {
    latestEditorMolfile = "";
    bridge?.clear();
    emit("update:molfile", "");
    return;
  }
  const generation = conversionGeneration;
  const controller = new AbortController();
  conversionController = controller;
  try {
    const converted = await api.convertChemistryRepresentation({ smiles }, controller.signal);
    if (generation !== conversionGeneration || !bridge) return;
    localSmiles.value = converted.smiles;
    emit("update:modelValue", converted.smiles);
    latestEditorMolfile = converted.molfile;
    awaitingSmilesLoad = true;
    bridge.loadMolfile(converted.molfile);
    emit("update:molfile", converted.molfile);
  } catch {
    // Invalid or incomplete direct input stays visible below the editor.
  } finally {
    if (conversionController === controller) conversionController = null;
  }
}

function resize(): void {
  const frame = iframe.value;
  if (!frame || !bridge) return;
  bridge.resize(frame.clientWidth, props.height);
}

async function initialize(): Promise<void> {
  await nextTick();
  if (!iframe.value) return;
  window.clearTimeout(readyTimer);
  bridge?.destroy();
  ready.value = false;
  error.value = "";
  bridge = new ChemDoodleEditorBridge(iframe.value);
  bridge.onChange(({ molfile, smiles }) => void updateFromEditor(molfile, smiles));
  bridge.onLayout(({ height }) => {
    const nextHeight = Math.max(props.height + 2, Math.ceil(height) + 2);
    if (Math.abs(nextHeight - editorHeight.value) > 1) editorHeight.value = nextHeight;
  });
  bridge.onReady(() => {
    window.clearTimeout(readyTimer);
    ready.value = true;
    error.value = "";
    if (props.molfile) {
      latestEditorMolfile = props.molfile;
      bridge?.loadMolfile(props.molfile);
    } else if (localSmiles.value) {
      void loadSmilesIntoEditor(localSmiles.value);
    }
    resize();
  });
  bridge.onError((message) => {
    window.clearTimeout(readyTimer);
    ready.value = false;
    error.value = message;
  });
  readyTimer = window.setTimeout(() => {
    if (!ready.value) error.value = "ChemDoodle editor did not become ready";
  }, 8_000);
}

async function reload(): Promise<void> {
  window.clearTimeout(readyTimer);
  cancelConversion();
  bridge?.destroy();
  bridge = null;
  latestEditorMolfile = "";
  awaitingSmilesLoad = false;
  editorHeight.value = props.height + 122;
  ready.value = false;
  error.value = "";
  editorGeneration.value += 1;
  await initialize();
}

function clear(): void {
  cancelConversion();
  latestEditorMolfile = "";
  awaitingSmilesLoad = false;
  localSmiles.value = "";
  bridge?.clear();
  emit("update:modelValue", "");
  emit("update:molfile", "");
}

function getMolfile(): Promise<string> {
  return bridge?.getMolfile() ?? Promise.resolve(props.molfile);
}

function getSmiles(): Promise<string> {
  return Promise.resolve(localSmiles.value);
}

function onSmilesInput(event: Event): void {
  const value = (event.target as HTMLInputElement).value;
  localSmiles.value = value;
  emit("update:modelValue", value);
  if (ready.value) void loadSmilesIntoEditor(value);
}

onMounted(() => {
  resizeObserver = new ResizeObserver(resize);
  if (iframe.value) resizeObserver.observe(iframe.value);
  void initialize();
});
onBeforeUnmount(() => {
  window.clearTimeout(readyTimer);
  cancelConversion();
  resizeObserver?.disconnect();
  bridge?.destroy();
  bridge = null;
});
watch(() => props.height, (value) => {
  editorHeight.value = value + 122;
  resize();
});
watch(() => props.molfile, (value) => {
  if (ready.value && value !== latestEditorMolfile) {
    latestEditorMolfile = value;
    bridge?.loadMolfile(value);
  }
});
watch(() => props.modelValue, (value) => {
  if (value === localSmiles.value) return;
  localSmiles.value = value;
  if (ready.value) void loadSmilesIntoEditor(value);
});
defineExpose({ clear, getMolfile, getSmiles });
</script>

<template>
  <div
    class="topology-editor-field"
    data-renderer="chemdoodle-sketcher"
    :style="{ '--topology-editor-height': `${editorHeight}px` }"
    :data-editor-id="editorId"
  >
    <div class="topology-editor">
      <iframe
        ref="iframe"
        :key="editorGeneration"
        title="ChemDoodle structure editor"
        :src="editorSource"
        sandbox="allow-scripts"
        class="topology-editor-frame"
      ></iframe>
      <div v-if="!ready" class="topology-editor-state" :class="{ 'is-error': error }">
        <div>
          <span>{{ error || "正在加载结构编辑器" }}</span>
          <button v-if="error" class="icon-button" type="button" title="重新加载编辑器" aria-label="重新加载编辑器" @click="reload">
            <RotateCcw :size="14" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
    <label class="topology-smiles-input">
      <span>SMILES</span>
      <input
        :value="localSmiles"
        type="text"
        placeholder="也可直接输入"
        @input="onSmilesInput"
      >
      <slot name="validation" />
    </label>
  </div>
</template>
