<script setup lang="ts">
import { ArrowUpRight } from "@lucide/vue";
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { GeometrySummary } from "@/types";

import GeometryDofPreview from "./GeometryDofPreview.vue";

const props = defineProps<{
  geometry: GeometrySummary;
  projectId: string | null;
  active: boolean;
}>();
const emit = defineEmits<{ open: [id: string] }>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));
const imaginaryFrequencyLabel = computed(() => ({
  present: "含虚频",
  absent: "无虚频",
  unavailable: "未提供频率",
})[props.geometry.imaginary_frequency_status]);
</script>

<template>
  <article class="geometry-card-shell">
    <button class="geometry-card" :class="{ 'is-active': active }" :data-imaginary-frequency-status="geometry.imaginary_frequency_status" :data-transition-state="geometry.is_transition_state" type="button" @click="emit('open', geometry.id)">
      <GeometryDofPreview :geometry-id="geometry.id" :project-id="projectId ?? undefined" :label="geometry.canonical_isomeric_smiles ?? undefined" :height="210" />
      <span class="geometry-card-title" :title="geometry.canonical_isomeric_smiles ?? undefined">{{ geometry.canonical_isomeric_smiles ?? "SMILES 不可用" }}</span>
      <span class="geometry-card-facts"><span>{{ geometry.atom_count }} atoms</span><span>{{ geometry.calculation_count }} frames</span><span>{{ geometry.reaction_binding_count }} reactions</span><span v-if="geometry.is_transition_state" class="geometry-transition-state-status">过渡态</span><span class="geometry-frequency-status" :class="`is-${geometry.imaginary_frequency_status}`">{{ imaginaryFrequencyLabel }}</span></span>
      <code>{{ shortId(geometry.id) }}</code>
    </button>
    <RouterLink class="geometry-card-direct-link" :to="{ name: 'geometry-detail', params: { geometryId: geometry.id }, query: navigationQuery }" title="在独立页面打开" :aria-label="`在独立页面打开几何构象 ${geometry.id}`"><ArrowUpRight :size="16" aria-hidden="true" /></RouterLink>
  </article>
</template>
