<script setup lang="ts">
import { Check, Clipboard, Download, X } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { artifactDownloadUrl } from "@/api";
import { formatBytes, shortId } from "@/format";
import type { ArtifactPreview } from "@/types";

const props = defineProps<{
  open: boolean;
  loading: boolean;
  error: string;
  preview: ArtifactPreview | null;
}>();

const emit = defineEmits<{ close: [] }>();
const copied = ref(false);
const downloadUrl = computed(() =>
  props.preview ? artifactDownloadUrl(props.preview.id) : "#",
);

async function copyPreview(): Promise<void> {
  if (!props.preview) return;
  await navigator.clipboard.writeText(props.preview.preview_text);
  copied.value = true;
  window.setTimeout(() => {
    copied.value = false;
  }, 1600);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && props.open) emit("close");
}

watch(
  () => props.open,
  (open) => {
    document.body.classList.toggle("drawer-open", open);
    if (!open) copied.value = false;
  },
);

onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.classList.remove("drawer-open");
});
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <button
        v-if="open"
        type="button"
        class="drawer-backdrop"
        aria-label="关闭文件预览"
        @click="emit('close')"
      ></button>
    </Transition>
    <Transition name="drawer-panel">
      <aside v-if="open" class="detail-drawer artifact-preview-drawer" aria-labelledby="artifact-preview-title">
        <header class="drawer-header">
          <div>
            <span class="eyebrow">RustFS Artifact</span>
            <h2 id="artifact-preview-title">文件预览</h2>
          </div>
          <div class="drawer-actions">
            <button
              class="icon-button"
              type="button"
              title="复制预览内容"
              aria-label="复制预览内容"
              :disabled="!preview"
              @click="copyPreview"
            >
              <Check v-if="copied" :size="17" aria-hidden="true" />
              <Clipboard v-else :size="17" aria-hidden="true" />
            </button>
            <a
              v-if="preview"
              class="icon-button"
              :href="downloadUrl"
              :download="preview.original_filename"
              title="下载原文件"
              aria-label="下载原文件"
            >
              <Download :size="17" aria-hidden="true" />
            </a>
            <button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="emit('close')">
              <X :size="18" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div v-if="loading" class="drawer-loading">
          <div class="loading-block"></div>
          <div class="loading-block is-wide"></div>
        </div>
        <div v-else-if="error" class="drawer-error">{{ error }}</div>
        <div v-else-if="preview" class="artifact-preview-content">
          <header class="artifact-preview-meta">
            <div>
              <strong>{{ preview.original_filename }}</strong>
              <span>{{ preview.media_type }} · {{ formatBytes(preview.size_bytes) }}</span>
            </div>
            <code :title="preview.content_sha256">SHA-256 {{ shortId(preview.content_sha256) }}</code>
          </header>
          <p v-if="preview.truncated" class="preview-truncated">
            当前显示前 {{ formatBytes(preview.preview_bytes) }}，下载可查看完整文件。
          </p>
          <pre class="artifact-preview-text"><code>{{ preview.preview_text }}</code></pre>
        </div>
      </aside>
    </Transition>
  </Teleport>
</template>
