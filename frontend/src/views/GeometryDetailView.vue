<script setup lang="ts">
import { ArrowLeft, ArrowUpRight } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import FrameDrawer from "@/components/FrameDrawer.vue";
import GeometryDetailContent from "@/components/GeometryDetailContent.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { withoutAccessState } from "@/routeAccessState";

const route = useRoute();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const geometryId = computed(() => typeof route.params.geometryId === "string" ? route.params.geometryId : null);
const navigationQuery = computed(() => withoutAccessState(route.query));
const selectedFrameId = ref<string | null>(null);

const geometryQuery = useQuery({
  queryKey: computed(() => ["geometry-detail-page", { geometryId: geometryId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.geometry(geometryId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => geometryId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const frameQuery = useQuery({
  queryKey: computed(() => ["geometry-detail-frame", { frameId: selectedFrameId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.frame(selectedFrameId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => selectedFrameId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
});

const detailError = computed(() => geometryQuery.error.value instanceof Error ? geometryQuery.error.value.message : "");
const frameError = computed(() => frameQuery.error.value instanceof Error ? frameQuery.error.value.message : "");
</script>

<template>
  <main class="entity-detail-page" aria-labelledby="geometry-page-title">
    <header class="entity-detail-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'geometries', query: navigationQuery }"><ArrowLeft :size="15" aria-hidden="true" />几何构象目录</RouterLink>
        <span class="eyebrow">Geometry</span><h1 id="geometry-page-title">几何构象详情</h1><p>三维结构、统一能量视图以及生成该构象的计算帧。</p>
      </div>
      <RouterLink v-if="geometryQuery.data.value" class="command-button is-quiet" :to="{ name: 'topology-detail', params: { topologyId: geometryQuery.data.value.topology_id }, query: navigationQuery }"><ArrowUpRight :size="15" aria-hidden="true" />分子拓扑</RouterLink>
    </header>
    <section v-if="geometryQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="detailError" class="entity-detail-state is-error" role="alert"><strong>几何构象无法读取</strong><p>{{ detailError }}</p></section>
    <section v-else-if="!geometryQuery.data.value" class="entity-detail-state"><strong>几何构象不存在或当前项目不可见</strong></section>
    <GeometryDetailContent v-else class="geometry-detail-page-content" :geometry="geometryQuery.data.value" :project-id="currentProjectId ?? undefined" @open-frame="selectedFrameId = $event" />
    <FrameDrawer :open="selectedFrameId !== null" :loading="frameQuery.isLoading.value" :error="frameError" :frame="frameQuery.data.value ?? null" :project-id="currentProjectId ?? undefined" @close="selectedFrameId = null" />
  </main>
</template>
