<script setup lang="ts">
import { ArrowRight, ArrowUpRight, ChevronDown, ChevronUp, CircleAlert } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { labelFor } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { LogicalReactionSummary } from "@/types";

import ChemDoodleGeometry3D from "./ChemDoodleGeometry3D.vue";
import ChemDoodleMolecule from "./ChemDoodleMolecule.vue";

const props = defineProps<{
  reaction: LogicalReactionSummary;
  projectId: string | null;
  active: boolean;
}>();

const emit = defineEmits<{ select: [id: string] }>();
const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));
const cardRoot = ref<HTMLElement | null>(null);
const isVisible = ref(false);
let observer: IntersectionObserver | null = null;

const reactants = computed(() => props.reaction.reactant_topology_ids ?? []);
const products = computed(() => props.reaction.product_topology_ids ?? []);

onMounted(() => {
  if (!cardRoot.value || typeof IntersectionObserver === "undefined") {
    isVisible.value = true;
    return;
  }
  // The reaction filter sidebar contains two topology editors on mobile.
  // Preload the first cards below that sidebar while keeping desktop lazy
  // loading bounded to the visible result area.
  const rootMargin = window.matchMedia("(max-width: 900px)").matches ? "1800px 0px" : "320px 0px";
  observer = new IntersectionObserver((entries) => {
    const entry = entries.find((candidate) => candidate.target === cardRoot.value);
    if (entry) isVisible.value = entry.isIntersecting;
  }, { rootMargin });
  observer.observe(cardRoot.value);
});
onBeforeUnmount(() => observer?.disconnect());
</script>

<template>
  <article ref="cardRoot" class="reaction-path-card" :class="{ 'is-active': active }">
    <button class="reaction-path-card-trigger" type="button" @click="emit('select', reaction.id)">
      <header class="reaction-card-header">
        <div>
          <span class="eyebrow">{{ reaction.cycloaddition_pattern || labelFor(reaction.reaction_class) }}</span>
          <h3>{{ reaction.label || reaction.reaction_key }}</h3>
        </div>
        <span class="reaction-card-toggle" :title="active ? '收起路径' : '展开路径'" :aria-label="active ? '收起路径' : '展开路径'" role="img">
          <ChevronUp v-if="active" :size="17" aria-hidden="true" />
          <ChevronDown v-else :size="17" aria-hidden="true" />
        </span>
      </header>
      <div class="reaction-path-strip" aria-label="底物到产物的反应路径">
        <div class="path-stage">
          <div class="path-stage-label">底物</div>
          <div v-if="isVisible && reactants.length" class="path-molecules">
            <ChemDoodleMolecule
              v-for="topologyId in reactants.slice(0, 2)"
              :key="topologyId"
              :topology-id="topologyId"
              :height="112"
              :label="`底物拓扑 ${topologyId}`"
            />
          </div>
          <div v-else class="path-stage-placeholder"><CircleAlert :size="17" />{{ reactants.length ? "滚动加载结构" : "无结构" }}</div>
        </div>
        <ArrowRight class="path-arrow" :size="20" aria-hidden="true" />
        <div class="path-stage is-transition">
          <div class="path-stage-label">过渡态</div>
          <ChemDoodleGeometry3D
            v-if="isVisible && reaction.transition_state_geometry_id"
            :geometry-id="reaction.transition_state_geometry_id"
            :project-id="projectId ?? undefined"
            :height="112"
            label="过渡态"
          />
          <div v-else class="path-stage-placeholder">{{ reaction.transition_state_geometry_id ? "滚动加载结构" : "暂无 TS 构象" }}</div>
        </div>
        <ArrowRight class="path-arrow" :size="20" aria-hidden="true" />
        <div class="path-stage">
          <div class="path-stage-label">产物</div>
          <div v-if="isVisible && products.length" class="path-molecules">
            <ChemDoodleMolecule
              v-for="topologyId in products.slice(0, 2)"
              :key="topologyId"
              :topology-id="topologyId"
              :height="112"
              :label="`产物拓扑 ${topologyId}`"
            />
          </div>
          <div v-else class="path-stage-placeholder"><CircleAlert :size="17" />{{ products.length ? "滚动加载结构" : "无结构" }}</div>
        </div>
      </div>
      <footer class="reaction-card-footer">
        <span>{{ reactants.length }} 底物 · {{ products.length }} 产物</span>
        <span v-if="reaction.similarity_score !== null" class="reaction-similarity">相似度 {{ (reaction.similarity_score * 100).toFixed(1) }}%</span>
        <span v-if="reaction.minimum_activation_gibbs_free_energy_kcal_mol !== null" class="reaction-barrier">
          ΔG‡ {{ reaction.minimum_activation_gibbs_free_energy_kcal_mol.toFixed(1) }}–{{ (reaction.maximum_activation_gibbs_free_energy_kcal_mol ?? reaction.minimum_activation_gibbs_free_energy_kcal_mol).toFixed(1) }} kcal/mol
        </span>
        <span v-else class="reaction-barrier is-missing">暂无 ΔG‡</span>
        <span v-if="reaction.minimum_reaction_gibbs_free_energy_kcal_mol !== null" class="reaction-barrier">
          ΔG {{ reaction.minimum_reaction_gibbs_free_energy_kcal_mol.toFixed(1) }}–{{ (reaction.maximum_reaction_gibbs_free_energy_kcal_mol ?? reaction.minimum_reaction_gibbs_free_energy_kcal_mol).toFixed(1) }} kcal/mol
        </span>
        <span v-else class="reaction-barrier is-missing">暂无 ΔG</span>
        <span>点击查看映射方案</span>
        <code>{{ reaction.reaction_key }}</code>
      </footer>
    </button>
    <RouterLink class="reaction-card-direct-link" :to="{ name: 'reaction-detail', params: { logicalReactionId: reaction.id }, query: navigationQuery }" title="在独立页面打开" :aria-label="`在独立页面打开反应路径 ${reaction.id}`"><ArrowUpRight :size="16" aria-hidden="true" /></RouterLink>
  </article>
</template>
