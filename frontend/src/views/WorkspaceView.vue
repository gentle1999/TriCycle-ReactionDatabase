<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { type LocationQueryRaw, useRoute, useRouter } from "vue-router";

import ArtifactPreviewDrawer from "@/components/ArtifactPreviewDrawer.vue";
import ArtifactsView from "@/components/ArtifactsView.vue";
import FrameDrawer from "@/components/FrameDrawer.vue";
import ReactionView from "@/components/ReactionView.vue";
import type { ArtifactFilterValues, ArtifactSort } from "@/artifactQuery";
import { useCatalogQueries, type CatalogView } from "@/composables/useCatalogQueries";
import { useProjectContext } from "@/composables/useProjectContext";
import { useSession } from "@/composables/useSession";
import { queryClient } from "@/queryClient";
import { withoutAccessState } from "@/routeAccessState";
import type { ReactionQueryFilters, ReactionSort } from "@/reactionQuery";
import type { PageInfo } from "@/types";

const route = useRoute();
const router = useRouter();
const session = useSession();
const projectContext = useProjectContext();
const currentUser = session.user;
const currentProjectId = projectContext.currentProjectId;

const activeView = computed<CatalogView>(() =>
  route.name === "reactions" || route.name === "reaction-detail" || route.name === "mapped-reaction-detail"
    ? "reactions"
    : "artifacts",
);
const routeReactionId = computed(() => route.name === "reactions"
  ? (typeof route.query.preview_reaction === "string" ? route.query.preview_reaction : null)
  : (typeof route.params.logicalReactionId === "string" ? route.params.logicalReactionId : null));
const routeMappedReactionId = computed(() => route.name === "reactions"
  ? (typeof route.query.preview_mapped === "string" ? route.query.preview_mapped : null)
  : (typeof route.params.mappedReactionId === "string" ? route.params.mappedReactionId : null));
function artifactRouteQueryValue(name: string): string | null {
  const value = route.query[name];
  return typeof value === "string" ? value : null;
}
const artifactFilterId = computed(() =>
  route.name === "artifacts"
    ? artifactRouteQueryValue("artifact_id")
    : null,
);
const artifactKindFilter = computed(() => route.name === "artifacts" ? artifactRouteQueryValue("artifact_kind") : null);
const artifactContentShaFilter = computed(() => route.name === "artifacts" ? artifactRouteQueryValue("content_sha256") : null);
const artifactFilenameFilter = computed(() => route.name === "artifacts" ? artifactRouteQueryValue("original_filename_contains") : null);
const artifactStorageStatusFilter = computed(() => route.name === "artifacts" ? artifactRouteQueryValue("storage_status") : null);
const artifactIngestionStatusFilter = computed(() => route.name === "artifacts" ? artifactRouteQueryValue("ingestion_status") : null);
const artifactFilterText = computed(() =>
  artifactFilterId.value
  ?? artifactContentShaFilter.value
  ?? artifactFilenameFilter.value
  ?? "",
);
const artifactQueryFilters = computed<ArtifactFilterValues>(() => ({
  artifactId: artifactFilterId.value,
  contentSha256: artifactContentShaFilter.value,
  originalFilenameContains: artifactFilenameFilter.value,
  artifactKind: artifactKindFilter.value,
  storageStatus: artifactStorageStatusFilter.value,
  ingestionStatus: artifactIngestionStatusFilter.value,
}));
const selectedMappedId = ref<string | null>(routeMappedReactionId.value);
const selectedFrameId = ref<string | null>(null);
const selectedArtifactId = ref<string | null>(null);
const expandedArtifactId = ref<string | null>(null);
const reactionOffset = ref(0);
const reactionFilters = ref<ReactionQueryFilters>({});
const reactionSort = ref<ReactionSort>({ sortBy: "default", sortDirection: "asc" });
const artifactOffset = ref(0);
const artifactSort = ref<ArtifactSort>({ sortBy: "created_at", sortDirection: "desc" });

const mappedReactionId = computed(() => routeMappedReactionId.value ?? selectedMappedId.value);
const queries = useCatalogQueries({
  projectId: currentProjectId,
  activeView,
  user: currentUser,
  reactionOffset,
  reactionFilters,
  reactionSort,
  artifactOffset,
  artifactSort,
  artifactFilterId,
  artifactKindFilter,
  artifactContentShaFilter,
  artifactFilenameFilter,
  artifactStorageStatusFilter,
  artifactIngestionStatusFilter,
  reactionId: routeReactionId,
  mappedReactionId,
  frameId: selectedFrameId,
  artifactId: selectedArtifactId,
  expandedArtifactId,
});

const reactions = computed(() => queries.reactions.data.value?.items ?? []);
const artifacts = computed(() => queries.artifacts.data.value?.items ?? []);
const reaction = computed(() => queries.reaction.data.value ?? null);
const mappedReaction = computed(() => queries.mappedReaction.data.value ?? null);
const activeReactionId = computed(() => routeReactionId.value ?? reaction.value?.id ?? null);
const selectedFrame = computed(() => queries.frame.data.value ?? null);
const artifactPreview = computed(() => queries.artifactPreview.data.value ?? null);
const reactionPage = computed<PageInfo>(() => queries.reactions.data.value?.page ?? { total: 0, limit: 12, offset: reactionOffset.value });
const artifactPage = computed<PageInfo>(() => queries.artifacts.data.value?.page ?? { total: 0, limit: 50, offset: artifactOffset.value });
const loading = computed(() => activeView.value === "reactions" ? queries.reactions.isLoading.value : queries.artifacts.isLoading.value);
const querying = computed(() => {
  const query = activeView.value === "reactions" ? queries.reactions : queries.artifacts;
  return query.isFetching.value && (query.isLoading.value || query.isPlaceholderData.value);
});
const globalError = computed(() => {
  const errors = activeView.value === "reactions"
    ? [queries.reactions.error.value, queries.reaction.error.value, queries.mappedReaction.error.value]
    : [queries.artifacts.error.value];
  const error = errors.find((item): item is Error => item instanceof Error);
  return error?.message ?? "";
});
const drawerError = computed(() => queries.frame.error.value instanceof Error ? queries.frame.error.value.message : "");
const artifactPreviewError = computed(() => queries.artifactPreview.error.value instanceof Error ? queries.artifactPreview.error.value.message : "");
const forbiddenNotice = computed(() => Boolean(route.query.forbidden) || route.query.unavailable === "forbidden");

watch(
  [routeReactionId, routeMappedReactionId, () => reaction.value?.mapped_reactions],
  ([reactionId, mappedId, available]) => {
    selectedMappedId.value = reactionId || mappedId ? (mappedId ?? available?.[0]?.id ?? null) : null;
  },
  { immediate: true },
);

watch(currentProjectId, (next, previous) => {
  if (next === previous) return;
  reactionOffset.value = 0;
  resetArtifactPagination();
  selectedMappedId.value = null;
  selectedFrameId.value = null;
  selectedArtifactId.value = null;
  expandedArtifactId.value = null;
  if (previous) {
    queryClient.removeQueries({ predicate: (query) => query.queryKey.some((part) => part === previous) });
  }
});
watch(
  [
    artifactFilterId,
    artifactKindFilter,
    artifactContentShaFilter,
    artifactFilenameFilter,
    artifactStorageStatusFilter,
    artifactIngestionStatusFilter,
  ],
  () => {
    resetArtifactPagination();
    if (expandedArtifactId.value !== artifactFilterId.value) {
      expandedArtifactId.value = null;
    }
  },
);

function selectReaction(id: string): void {
  if (activeReactionId.value === id) {
    selectedMappedId.value = null;
    const query = { ...withoutAccessState(route.query) };
    delete query.preview_reaction;
    delete query.preview_mapped;
    void router.push({ name: "reactions", query });
    return;
  }
  void router.push({ name: "reactions", query: { ...withoutAccessState(route.query), preview_reaction: id, preview_mapped: undefined } });
}

function selectMapped(id: string): void {
  selectedMappedId.value = id;
  void router.push({ name: "reactions", query: { ...withoutAccessState(route.query), preview_reaction: reaction.value?.id ?? routeReactionId.value ?? undefined, preview_mapped: id } });
}

function openFrame(id: string): void {
  selectedFrameId.value = id;
}

function applyReactionFilters(filters: ReactionQueryFilters): void {
  reactionFilters.value = filters;
  reactionOffset.value = 0;
}

function updateReactionSort(sort: ReactionSort): void {
  reactionSort.value = sort;
  reactionOffset.value = 0;
}

function closeFrame(): void {
  selectedFrameId.value = null;
}

function toggleArtifactFrames(id: string): void {
  expandedArtifactId.value = expandedArtifactId.value === id ? null : id;
}

function openArtifactPreview(id: string): void {
  selectedArtifactId.value = id;
}

const artifactFilterQueryKeys = [
  "artifact_id",
  "artifact_kind",
  "content_sha256",
  "original_filename_contains",
  "storage_status",
  "ingestion_status",
] as const;

function artifactFilterQuery(filters: ArtifactFilterValues): LocationQueryRaw {
  const query = { ...withoutAccessState(route.query) };
  for (const key of artifactFilterQueryKeys) delete query[key];
  if (filters.artifactId) query.artifact_id = filters.artifactId;
  if (filters.contentSha256) query.content_sha256 = filters.contentSha256;
  if (filters.originalFilenameContains) query.original_filename_contains = filters.originalFilenameContains;
  if (filters.artifactKind) query.artifact_kind = filters.artifactKind;
  if (filters.storageStatus) query.storage_status = filters.storageStatus;
  if (filters.ingestionStatus) query.ingestion_status = filters.ingestionStatus;
  return query;
}

function applyArtifactFilters(filters: ArtifactFilterValues): void {
  resetArtifactPagination();
  expandedArtifactId.value = null;
  void router.push({ name: "artifacts", query: artifactFilterQuery(filters) });
}

function updateArtifactSort(sort: ArtifactSort): void {
  artifactSort.value = sort;
  resetArtifactPagination();
}

function closeArtifactPreview(): void {
  selectedArtifactId.value = null;
}

async function refreshAfterDelete(artifactId: string): Promise<void> {
  if (selectedArtifactId.value === artifactId) closeArtifactPreview();
  if (expandedArtifactId.value === artifactId) expandedArtifactId.value = null;
  queryClient.removeQueries({ queryKey: ["catalog", "artifact-preview", { id: artifactId }] });
  const projectId = currentProjectId.value;
  if (!projectId) return;
  resetArtifactPagination();
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: ["catalog", "artifacts", { projectId }] }),
    queryClient.invalidateQueries({ queryKey: ["catalog", "totals", { projectId }] }),
  ]);
}

function previousReactionPage(): void { reactionOffset.value = Math.max(0, reactionOffset.value - reactionPage.value.limit); }
function nextReactionPage(): void {
  if (reactionOffset.value + reactionPage.value.limit < reactionPage.value.total) reactionOffset.value += reactionPage.value.limit;
}
function jumpReactionPage(offset: number): void { reactionOffset.value = offset; }
function resetArtifactPagination(): void {
  artifactOffset.value = 0;
}
function previousArtifactPage(): void {
  artifactOffset.value = Math.max(0, artifactOffset.value - artifactPage.value.limit);
}
function nextArtifactPage(): void {
  if (artifactOffset.value + artifactPage.value.limit < artifactPage.value.total) {
    artifactOffset.value += artifactPage.value.limit;
  }
}
function jumpArtifactPage(offset: number): void {
  artifactOffset.value = offset;
}
</script>

<template>
  <div v-if="route.query.login || forbiddenNotice || route.query.unavailable" class="notice" role="status">
    <span v-if="route.query.login">请登录后访问受保护资源；当前仍可浏览公开 Artifact。</span>
    <span v-else-if="forbiddenNotice">当前账户没有访问该资源的权限。</span>
    <span v-else>服务暂时不可用；当前仍可浏览公开 Artifact。</span>
  </div>
  <div v-if="globalError" class="notice" role="alert">{{ globalError }}</div>
  <main class="workspace-main">
    <section class="metrics-band" aria-label="当前项目数据库概览">
      <div><span>逻辑反应总数</span><strong>{{ queries.databaseTotals.data.value?.reactions ?? "—" }}</strong></div>
      <div><span>映射方案总数</span><strong>{{ queries.databaseTotals.data.value?.mappedReactions ?? "—" }}</strong></div>
      <div><span>几何构象总数</span><strong>{{ queries.databaseTotals.data.value?.geometries ?? "—" }}</strong></div>
      <div><span>原始文件总数</span><strong>{{ queries.databaseTotals.data.value?.artifacts ?? "—" }}</strong></div>
      <div><span>计算帧总数</span><strong>{{ queries.databaseTotals.data.value?.frames ?? "—" }}</strong></div>
    </section>

    <ReactionView
      v-if="activeView === 'reactions'"
      :reactions="reactions"
      :selected-reaction-id="activeReactionId"
      :reaction="reaction"
      :mapped-reaction="mappedReaction"
      :selected-mapped-id="selectedMappedId"
      :loading="loading"
      :querying="querying"
      :mapped-loading="queries.mappedReaction.isLoading.value"
      :project-id="currentProjectId"
      :total="reactionPage.total"
      :page="reactionPage"
      :query-filters="reactionFilters"
      :sort="reactionSort"
      @select-reaction="selectReaction"
      @select-mapped="selectMapped"
      @open-frame="openFrame"
      @previous-page="previousReactionPage"
      @next-page="nextReactionPage"
      @jump-page="jumpReactionPage"
      @apply-filters="applyReactionFilters"
      @update-sort="updateReactionSort"
    />
    <ArtifactsView
      v-if="activeView === 'artifacts'"
      :artifacts="artifacts"
      :query-filters="artifactQueryFilters"
      :filter-text="artifactFilterText"
      :loading="loading"
      :querying="querying"
      :current-user="currentUser"
      :selected-project-id="currentProjectId"
      :expanded-artifact-id="expandedArtifactId"
      :expanded-frames="queries.artifactFrames.data.value?.items ?? []"
      :frames-loading="queries.artifactFrames.isLoading.value"
      :frames-error="queries.artifactFrames.error.value instanceof Error ? queries.artifactFrames.error.value.message : ''"
      :total="artifactPage.total"
      :page="artifactPage"
      :sort="artifactSort"
      @apply-filters="applyArtifactFilters"
      @update-sort="updateArtifactSort"
      @preview="openArtifactPreview"
      @deleted="refreshAfterDelete"
      @toggle-frames="toggleArtifactFrames"
      @open-frame="openFrame"
      @previous-page="previousArtifactPage"
      @next-page="nextArtifactPage"
      @jump-page="jumpArtifactPage"
    />

    <FrameDrawer :open="selectedFrameId !== null" :loading="queries.frame.isLoading.value" :error="drawerError" :frame="selectedFrame" :project-id="currentProjectId ?? undefined" @close="closeFrame" />
    <ArtifactPreviewDrawer :open="selectedArtifactId !== null" :loading="queries.artifactPreview.isLoading.value" :error="artifactPreviewError" :preview="artifactPreview" @close="closeArtifactPreview" />
  </main>
</template>
