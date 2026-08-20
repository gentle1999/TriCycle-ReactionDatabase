<script setup lang="ts">
import { RotateCcw } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";

import { api } from "../api";
import { ChemDoodleEditorBridge } from "../chem/ChemDoodleEditorBridge";

const props = withDefaults(defineProps<{ modelValue: string; height?: number }>(), { height: 280 });
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const iframe = ref<HTMLIFrameElement | null>(null);
const editorId = `chemdoodle-reaction-editor-${useId()}`;
const ready = ref(false);
const error = ref("");
const editorGeneration = ref(0);
const editorHeight = ref(props.height + 122);
const localReactionSmiles = ref(props.modelValue);
const editorSource = computed(() => `/editor/chemdoodle-editor.html?mode=reaction&oneMolecule=false&generation=${editorGeneration.value}`);
let bridge: ChemDoodleEditorBridge | null = null;
let resizeObserver: ResizeObserver | null = null;
let readyTimer = 0;
let conversionController: AbortController | null = null;
let conversionGeneration = 0;

function cancelConversion(): void {
  conversionGeneration += 1;
  conversionController?.abort();
  conversionController = null;
}

async function updateFromEditor(rxn: string): Promise<void> {
  cancelConversion();
  if (!rxn) {
    localReactionSmiles.value = "";
    emit("update:modelValue", "");
    return;
  }
  const generation = conversionGeneration;
  const controller = new AbortController();
  conversionController = controller;
  try {
    const converted = await api.convertChemistryReactionRepresentation({ rxn }, controller.signal);
    if (generation !== conversionGeneration) return;
    localReactionSmiles.value = converted.reaction_smiles;
    emit("update:modelValue", converted.reaction_smiles);
  } catch {
    // Keep the drawing available when an incomplete reaction cannot be canonicalized yet.
  } finally {
    if (conversionController === controller) conversionController = null;
  }
}

async function loadReactionSmiles(smiles: string): Promise<void> {
  cancelConversion();
  if (!smiles) {
    bridge?.clear();
    return;
  }
  const generation = conversionGeneration;
  const controller = new AbortController();
  conversionController = controller;
  try {
    const converted = await api.convertChemistryReactionRepresentation(
      { reaction_smiles: smiles },
      controller.signal,
    );
    if (generation !== conversionGeneration || !bridge) return;
    localReactionSmiles.value = converted.reaction_smiles;
    emit("update:modelValue", converted.reaction_smiles);
    bridge.loadRxn(converted.rxn);
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
  bridge.onReactionChange(({ rxn }) => void updateFromEditor(rxn));
  bridge.onLayout(({ height }) => {
    const nextHeight = Math.max(props.height + 2, Math.ceil(height) + 2);
    if (Math.abs(nextHeight - editorHeight.value) > 1) editorHeight.value = nextHeight;
  });
  bridge.onReady(() => {
    window.clearTimeout(readyTimer);
    ready.value = true;
    error.value = "";
    if (localReactionSmiles.value) void loadReactionSmiles(localReactionSmiles.value);
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
  editorHeight.value = props.height + 122;
  ready.value = false;
  error.value = "";
  editorGeneration.value += 1;
  await initialize();
}

function clear(): void {
  cancelConversion();
  localReactionSmiles.value = "";
  bridge?.clear();
  emit("update:modelValue", "");
}

function onReactionSmilesInput(event: Event): void {
  const value = (event.target as HTMLInputElement).value;
  localReactionSmiles.value = value;
  emit("update:modelValue", value);
  if (ready.value) void loadReactionSmiles(value);
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
watch(() => props.modelValue, (value) => {
  if (value === localReactionSmiles.value) return;
  localReactionSmiles.value = value;
  if (ready.value) void loadReactionSmiles(value);
});
defineExpose({ clear, getReactionSmiles: () => Promise.resolve(localReactionSmiles.value) });
</script>

<template>
  <div
    class="topology-editor-field reaction-editor-field"
    data-renderer="chemdoodle-reaction"
    :style="{ '--topology-editor-height': `${editorHeight}px` }"
    :data-editor-id="editorId"
  >
    <div class="topology-editor">
      <iframe
        ref="iframe"
        :key="editorGeneration"
        title="ChemDoodle reaction editor"
        :src="editorSource"
        sandbox="allow-scripts"
        class="topology-editor-frame"
      ></iframe>
      <div v-if="!ready" class="topology-editor-state" :class="{ 'is-error': error }">
        <div>
          <span>{{ error || "正在加载反应编辑器" }}</span>
          <button v-if="error" class="icon-button" type="button" title="重新加载编辑器" aria-label="重新加载编辑器" @click="reload">
            <RotateCcw :size="14" aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
    <label class="topology-smiles-input">
      <span>RXN SMILES / SMARTS</span>
      <input
        :value="localReactionSmiles"
        type="text"
        placeholder="也可直接输入，例如 C=C>>CC"
        @input="onReactionSmilesInput"
      >
      <slot name="validation" />
    </label>
  </div>
</template>
