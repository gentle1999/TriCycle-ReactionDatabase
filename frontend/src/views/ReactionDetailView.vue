<script setup lang="ts">
import { ArrowLeft, ArrowUpRight } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import ChemDoodleMolecule from "@/components/ChemDoodleMolecule.vue";
import FrameDrawer from "@/components/FrameDrawer.vue";
import MappedReactionExpansion from "@/components/MappedReactionExpansion.vue";
import MappedReactionSummaryCard from "@/components/MappedReactionSummaryCard.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { labelFor, shortId } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { LogicalReactionParticipant, MappedReactionSummary } from "@/types";

const route = useRoute();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const logicalReactionId = computed(() =>
  typeof route.params.logicalReactionId === "string" ? route.params.logicalReactionId : null,
);
const mappedReactionId = computed(() =>
  typeof route.params.mappedReactionId === "string" ? route.params.mappedReactionId : null,
);
const isMappedReactionPage = computed(() => mappedReactionId.value !== null);
const navigationQuery = computed(() => withoutAccessState(route.query));
const selectedFrameId = ref<string | null>(null);

const mappedQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-mapped", { id: mappedReactionId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.mappedReaction(
    mappedReactionId.value ?? "",
    { projectId: currentProjectId.value ?? undefined },
    signal,
  ),
  enabled: computed(() => mappedReactionId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const reactionId = computed(() =>
  logicalReactionId.value ?? mappedQuery.data.value?.logical_reaction_id ?? null,
);
const reactionQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-page", { id: reactionId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.reaction(
    reactionId.value ?? "",
    { projectId: currentProjectId.value ?? undefined },
    signal,
  ),
  enabled: computed(() => reactionId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const error = computed(() => {
  const source = mappedQuery.error.value ?? reactionQuery.error.value;
  return source instanceof Error ? source.message : "";
});
const frameQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-frame", { id: selectedFrameId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.frame(
    selectedFrameId.value ?? "",
    { projectId: currentProjectId.value ?? undefined },
    signal,
  ),
  enabled: computed(() => selectedFrameId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});
const frameError = computed(() => frameQuery.error.value instanceof Error ? frameQuery.error.value.message : "");
const reaction = computed(() => reactionQuery.data.value ?? null);
const mappedReaction = computed(() => mappedQuery.data.value ?? null);
const logicalReactants = computed(() =>
  [...(reaction.value?.participants ?? [])]
    .filter((participant) => participant.side === "reactant")
    .sort((left, right) => left.participant_index - right.participant_index),
);
const logicalProducts = computed(() =>
  [...(reaction.value?.participants ?? [])]
    .filter((participant) => participant.side === "product")
    .sort((left, right) => left.participant_index - right.participant_index),
);
const mappedSummaries = computed<MappedReactionSummary[]>(() => reaction.value?.mapped_reactions ?? []);
const mappedCount = computed(() => reaction.value?.mapped_reaction_count ?? mappedSummaries.value.length);

function participantRole(participant: LogicalReactionParticipant): string {
  return participant.role ? labelFor(participant.role) : labelFor(participant.side);
}
</script>

<template>
  <main class="entity-detail-page reaction-detail-page" :aria-labelledby="isMappedReactionPage ? 'mapped-reaction-detail-title' : 'reaction-detail-title'">
    <template v-if="isMappedReactionPage">
      <header class="entity-detail-header">
        <div>
          <RouterLink
            v-if="reaction"
            class="entity-back-link"
            :to="{ name: 'reaction-detail', params: { logicalReactionId: reaction.id }, query: navigationQuery }"
          ><ArrowLeft :size="15" aria-hidden="true" />返回逻辑反应</RouterLink>
          <RouterLink v-else class="entity-back-link" :to="{ name: 'reactions', query: navigationQuery }"><ArrowLeft :size="15" aria-hidden="true" />反应路径目录</RouterLink>
          <span class="eyebrow">MappedReaction · 映射反应</span>
          <h1 id="mapped-reaction-detail-title">{{ mappedReaction?.label || mappedReaction?.mapped_reaction_key || "映射反应详情" }}</h1>
          <p>严格的原子映射、具体拓扑、节点几何和计算帧。</p>
        </div>
      </header>
    </template>
    <template v-else>
      <header class="entity-detail-header">
        <div>
          <RouterLink class="entity-back-link" :to="{ name: 'reactions', query: navigationQuery }"><ArrowLeft :size="15" aria-hidden="true" />反应路径目录</RouterLink>
          <span class="eyebrow">LogicalReaction · 逻辑反应</span>
          <h1 id="reaction-detail-title">{{ reaction?.label || reaction?.reaction_key || "逻辑反应详情" }}</h1>
          <p>逻辑反应拓扑及其对应的多个严格映射反应。</p>
        </div>
      </header>
    </template>

    <section v-if="reactionQuery.isLoading.value || mappedQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="error" class="entity-detail-state is-error" role="alert"><strong>{{ isMappedReactionPage ? "映射反应无法读取" : "逻辑反应无法读取" }}</strong><p>{{ error }}</p></section>
    <section v-else-if="!reaction" class="entity-detail-state"><strong>{{ isMappedReactionPage ? "映射反应不存在或当前项目不可见" : "逻辑反应不存在或当前项目不可见" }}</strong></section>
    <section v-else-if="isMappedReactionPage" class="reaction-detail-body mapped-reaction-page-body">
      <div class="reaction-detail-summary"><span>MappedReaction</span><code :title="mappedReaction?.id">{{ shortId(mappedReaction?.id ?? mappedReactionId) }}</code><span>所属逻辑反应：{{ reaction.label || reaction.reaction_key }}</span></div>
      <MappedReactionExpansion
        :reaction="reaction"
        :mapped-reaction="mappedReaction"
        :mapped-loading="mappedQuery.isLoading.value"
        :project-id="currentProjectId"
        @open-frame="selectedFrameId = $event"
      />
    </section>
    <section v-else class="reaction-detail-body logical-reaction-page-body">
      <div class="reaction-detail-summary"><span>LogicalReaction</span><code :title="reaction.id">{{ shortId(reaction.id) }}</code><span>{{ reaction.participants.length }} 个逻辑参与物 · {{ mappedCount }} 个映射反应</span></div>

      <section class="logical-reaction-overview" aria-labelledby="logical-reaction-overview-title">
        <header class="section-heading"><div><span class="eyebrow">LogicalReaction</span><h2 id="logical-reaction-overview-title">逻辑反应参与物</h2></div><span>逻辑拓扑，不含具体原子映射</span></header>
        <div class="logical-equation">
          <div class="logical-equation-side">
            <article v-for="participant in logicalReactants" :key="participant.id" class="logical-participant-card">
              <header><strong>{{ participantRole(participant) }}</strong><RouterLink :to="{ name: 'topology-detail', params: { topologyId: participant.topology_id }, query: navigationQuery }" :aria-label="`查看逻辑拓扑 ${participant.topology_id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></header>
              <ChemDoodleMolecule
                :topology-id="participant.topology_id"
                :height="180"
                :label="participant.canonical_isomeric_smiles ?? `逻辑拓扑 ${participant.topology_id}`"
              />
              <code>{{ participant.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code>
              <small>逻辑拓扑 {{ shortId(participant.topology_id) }}</small>
            </article>
          </div>
          <span class="logical-equation-arrow" aria-hidden="true">→</span>
          <div class="logical-equation-side is-product">
            <article v-for="participant in logicalProducts" :key="participant.id" class="logical-participant-card">
              <header><strong>{{ participantRole(participant) }}</strong><RouterLink :to="{ name: 'topology-detail', params: { topologyId: participant.topology_id }, query: navigationQuery }" :aria-label="`查看逻辑拓扑 ${participant.topology_id}`"><ArrowUpRight :size="15" aria-hidden="true" /></RouterLink></header>
              <ChemDoodleMolecule
                :topology-id="participant.topology_id"
                :height="180"
                :label="participant.canonical_isomeric_smiles ?? `逻辑拓扑 ${participant.topology_id}`"
              />
              <code>{{ participant.canonical_isomeric_smiles ?? "SMILES 不可用" }}</code>
              <small>逻辑拓扑 {{ shortId(participant.topology_id) }}</small>
            </article>
          </div>
        </div>
      </section>

      <section class="logical-mapped-reactions" aria-labelledby="logical-mapped-reactions-title">
        <header class="section-heading"><div><span class="eyebrow">MappedReaction</span><h2 id="logical-mapped-reactions-title">严格映射反应</h2></div><span>{{ mappedCount }} 个，默认折叠</span></header>
        <div v-if="!mappedSummaries.length" class="compact-empty">当前逻辑反应没有映射反应</div>
        <div v-else class="mapped-summary-list">
          <MappedReactionSummaryCard
            v-for="(mapped, index) in mappedSummaries"
            :key="mapped.id"
            :mapped="mapped"
            :index="index"
            :project-id="currentProjectId"
          />
        </div>
      </section>
    </section>

    <FrameDrawer :open="selectedFrameId !== null" :loading="frameQuery.isLoading.value" :error="frameError" :frame="frameQuery.data.value ?? null" :project-id="currentProjectId ?? undefined" @close="selectedFrameId = null" />
  </main>
</template>
