<script setup lang="ts">
import { ArrowUpRight, X } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { withoutAccessState } from "@/routeAccessState";
import type { CalculationFrameDetail } from "@/types";

import FrameDetailContent from "./FrameDetailContent.vue";

const props = defineProps<{
  open: boolean;
  loading: boolean;
  error: string;
  frame: CalculationFrameDetail | null;
  projectId?: string;
}>();

const emit = defineEmits<{ close: [] }>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && props.open) emit("close");
}

watch(
  () => props.open,
  (open) => document.body.classList.toggle("drawer-open", open),
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
      <button v-if="open" type="button" class="drawer-backdrop" aria-label="关闭帧详情" @click="emit('close')"></button>
    </Transition>
    <Transition name="drawer-panel">
      <aside v-if="open" class="detail-drawer" aria-labelledby="drawer-title">
        <header class="drawer-header">
          <div><span class="eyebrow">CalculationFrame</span><h2 id="drawer-title">帧详情</h2></div>
          <div class="drawer-actions">
            <RouterLink
              v-if="frame"
              class="icon-button"
              :to="{ name: 'calculation-detail', params: { frameId: frame.id }, query: navigationQuery }"
              title="在独立页面打开"
              aria-label="在独立页面打开计算帧"
              @click="emit('close')"
            ><ArrowUpRight :size="18" aria-hidden="true" /></RouterLink>
            <button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="emit('close')"><X :size="18" aria-hidden="true" /></button>
          </div>
        </header>
        <div v-if="loading" class="drawer-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></div>
        <div v-else-if="error" class="drawer-error">{{ error }}</div>
        <div v-else-if="!frame" class="drawer-error">计算帧不存在或当前项目不可见</div>
        <FrameDetailContent v-else class="drawer-content" :frame="frame" :project-id="projectId" />
      </aside>
    </Transition>
  </Teleport>
</template>
