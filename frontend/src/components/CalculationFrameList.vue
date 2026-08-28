<script setup lang="ts">
import { ArrowUpRight, Eye, LoaderCircle } from "@lucide/vue";
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { UiStatusBadge } from "@/components/ui";
import { formatEnergy, labelFor } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { CalculationFrameSummary } from "@/types";

defineProps<{
  frames: CalculationFrameSummary[];
  loading?: boolean;
  error?: string;
  compact?: boolean;
}>();

const emit = defineEmits<{ open: [id: string] }>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));
</script>

<template>
  <div class="frame-list" :class="{ 'is-compact': compact }">
    <div v-if="loading" class="frame-list-state"><LoaderCircle class="is-spinning" :size="16" />正在加载计算帧</div>
    <div v-else-if="error" class="frame-list-state is-error">{{ error }}</div>
    <div v-else-if="!frames.length" class="frame-list-state">该文件还没有可见的计算帧</div>
    <div
      v-for="frame in frames"
      v-else
      :key="frame.id"
      class="frame-list-item"
    >
      <button class="frame-list-row" type="button" @click="emit('open', frame.id)">
        <span class="frame-list-index">{{ String(frame.file_frame_index + 1).padStart(2, "0") }}</span>
        <span class="frame-list-main"><strong>{{ labelFor(frame.frame_role) }}</strong><small>segment {{ frame.segment_index + 1 }} · frame {{ frame.frame_index + 1 }}</small></span>
        <span class="frame-list-energy">{{ formatEnergy(frame.selected_energy_hartree) }}</span>
        <UiStatusBadge :status="frame.optimization_status" />
        <Eye :size="15" aria-hidden="true" />
      </button>
      <RouterLink class="frame-list-direct-link" :to="{ name: 'calculation-detail', params: { frameId: frame.id }, query: navigationQuery }" title="在独立页面打开" :aria-label="`在独立页面打开计算帧 ${frame.id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink>
    </div>
  </div>
</template>
