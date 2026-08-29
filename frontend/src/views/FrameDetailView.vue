<script setup lang="ts">
import { ArrowLeft, FileText } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import FrameDetailContent from "@/components/FrameDetailContent.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { withoutAccessState } from "@/routeAccessState";

const route = useRoute();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const frameId = computed(() => typeof route.params.frameId === "string" ? route.params.frameId : null);
const navigationQuery = computed(() => withoutAccessState(route.query));

const frameQuery = useQuery({
  queryKey: computed(() => ["calculation-frame-detail", { id: frameId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.frame(frameId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => frameId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
  refetchOnMount: "always",
});

const error = computed(() => frameQuery.error.value instanceof Error ? frameQuery.error.value.message : "");
</script>

<template>
  <main class="entity-detail-page" aria-labelledby="frame-detail-title">
    <header class="entity-detail-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'artifacts', query: navigationQuery }">
          <ArrowLeft :size="15" aria-hidden="true" />原始文件
        </RouterLink>
        <span class="eyebrow">CalculationFrame</span>
        <h1 id="frame-detail-title">计算帧详情</h1>
        <p>计算结果、结构、热化学、振动分析和科学数组。</p>
      </div>
      <RouterLink v-if="frameQuery.data.value" class="command-button is-quiet" :to="{ name: 'artifact-detail', params: { artifactId: frameQuery.data.value.artifact_file_id }, query: navigationQuery }">
        <FileText :size="15" aria-hidden="true" />查看原始文件
      </RouterLink>
    </header>

    <section v-if="frameQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="error" class="entity-detail-state is-error" role="alert"><strong>计算帧无法读取</strong><p>{{ error }}</p></section>
    <section v-else-if="!frameQuery.data.value" class="entity-detail-state"><strong>计算帧不存在或当前项目不可见</strong></section>
    <FrameDetailContent
      v-else
      class="frame-detail-page-content"
      :frame="frameQuery.data.value"
      :project-id="currentProjectId ?? undefined"
    />
  </main>
</template>
