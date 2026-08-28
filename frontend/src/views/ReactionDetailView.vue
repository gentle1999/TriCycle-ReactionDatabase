<script setup lang="ts">
import { ArrowLeft, ArrowUpRight } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { api } from "@/api";
import FrameDrawer from "@/components/FrameDrawer.vue";
import MappedReactionExpansion from "@/components/MappedReactionExpansion.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { labelFor } from "@/format";
import { withoutAccessState } from "@/routeAccessState";

const route = useRoute();
const router = useRouter();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const logicalReactionId = computed(() => typeof route.params.logicalReactionId === "string" ? route.params.logicalReactionId : null);
const mappedReactionId = computed(() => typeof route.params.mappedReactionId === "string" ? route.params.mappedReactionId : null);
const navigationQuery = computed(() => withoutAccessState(route.query));
const selectedFrameId = ref<string | null>(null);

const mappedQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-mapped", { id: mappedReactionId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.mappedReaction(mappedReactionId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => mappedReactionId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const reactionId = computed(() => logicalReactionId.value ?? mappedQuery.data.value?.logical_reaction_id ?? null);
const reactionQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-page", { id: reactionId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.reaction(reactionId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => reactionId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const selectedMappedId = computed(() => mappedReactionId.value ?? reactionQuery.data.value?.mapped_reactions[0]?.id ?? null);
const selectedMappedQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-selected-mapped", { id: selectedMappedId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.mappedReaction(selectedMappedId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => selectedMappedId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const error = computed(() => {
  const source = mappedQuery.error.value ?? reactionQuery.error.value;
  return source instanceof Error ? source.message : "";
});
const frameQuery = useQuery({
  queryKey: computed(() => ["reaction-detail-frame", { id: selectedFrameId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.frame(selectedFrameId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => selectedFrameId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});
const frameError = computed(() => frameQuery.error.value instanceof Error ? frameQuery.error.value.message : "");

function selectMapped(id: string): void {
  void router.push({ name: "mapped-reaction-detail", params: { mappedReactionId: id }, query: navigationQuery.value });
}
</script>

<template>
  <main class="entity-detail-page reaction-detail-page" aria-labelledby="reaction-detail-title">
    <header class="entity-detail-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'reactions', query: navigationQuery }"><ArrowLeft :size="15" aria-hidden="true" />反应路径目录</RouterLink>
        <span class="eyebrow">LogicalReaction</span>
        <h1 id="reaction-detail-title">{{ reactionQuery.data.value?.label || reactionQuery.data.value?.reaction_key || "反应路径详情" }}</h1>
        <p>映射反应、节点构象、计算帧和热力学差值。</p>
      </div>
      <RouterLink v-if="selectedMappedId" class="command-button is-quiet" :to="{ name: 'mapped-reaction-detail', params: { mappedReactionId: selectedMappedId }, query: navigationQuery }"><ArrowUpRight :size="15" aria-hidden="true" />映射路径页</RouterLink>
    </header>
    <section v-if="reactionQuery.isLoading.value || mappedQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="error" class="entity-detail-state is-error" role="alert"><strong>反应路径无法读取</strong><p>{{ error }}</p></section>
    <section v-else-if="!reactionQuery.data.value" class="entity-detail-state"><strong>反应路径不存在或当前项目不可见</strong></section>
    <section v-else class="reaction-detail-body">
      <div class="reaction-detail-summary"><span>{{ labelFor(reactionQuery.data.value.reaction_class) }}</span><code :title="reactionQuery.data.value.id">{{ reactionQuery.data.value.id }}</code><span>{{ reactionQuery.data.value.participants.length }} 个参与物 · {{ reactionQuery.data.value.mapped_reactions.length }} 个映射方案</span></div>
      <MappedReactionExpansion :reaction="reactionQuery.data.value" :mapped-reaction="selectedMappedQuery.data.value ?? null" :selected-mapped-id="selectedMappedId" :mapped-loading="selectedMappedQuery.isLoading.value" :project-id="currentProjectId" @select-mapped="selectMapped" @open-frame="selectedFrameId = $event" />
    </section>
    <FrameDrawer :open="selectedFrameId !== null" :loading="frameQuery.isLoading.value" :error="frameError" :frame="frameQuery.data.value ?? null" :project-id="currentProjectId ?? undefined" @close="selectedFrameId = null" />
  </main>
</template>
