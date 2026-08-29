<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useQuery } from "@tanstack/vue-query";
import { RouterLink, useRoute, useRouter } from "vue-router";
import { ArrowUpRight, CircleHelp, ListFilter, LoaderCircle, RotateCcw, Search } from "@lucide/vue";

import GeometryCatalogCard from "@/components/GeometryCatalogCard.vue";
import GeometryDetailContent from "@/components/GeometryDetailContent.vue";
import GeometryAdvancedQueryModal from "@/components/GeometryAdvancedQueryModal.vue";
import FrameDrawer from "@/components/FrameDrawer.vue";
import PaginationControls from "@/components/PaginationControls.vue";
import QueryValidationIndicator from "@/components/QueryValidationIndicator.vue";
import { UiDrawer } from "@/components/ui";
import { api } from "@/api";
import { useGeometryQueries } from "@/composables/useGeometryQueries";
import { useProjectContext } from "@/composables/useProjectContext";
import { withoutAccessState } from "@/routeAccessState";
import type { GeometryQueryFilters, GeometrySort } from "@/geometryQuery";
import type { CalculationFrameDetail, GeometryDetail } from "@/types";

const route = useRoute();
const router = useRouter();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const offset = ref(0);
const geometrySort = ref<GeometrySort>({ sortBy: "default", sortDirection: "asc" });
const initialTopologySmiles = typeof route.query.topology === "string" ? route.query.topology : "";
const quickSmilesInput = ref(initialTopologySmiles);
const topologySmiles = ref(initialTopologySmiles);
const selectedFrameId = ref<string | null>(null);
const frame = ref<CalculationFrameDetail | null>(null);
const frameLoading = ref(false);
const frameError = ref("");
const advancedQueryOpen = ref(false);
const advancedFilters = ref<GeometryQueryFilters | null>(null);
const thermodynamicOnly = ref(true);
type QueryValidationStatus = "idle" | "pending" | "valid" | "invalid";
const quickValidation = ref<{ status: QueryValidationStatus; message: string }>({ status: "idle", message: "" });
let quickValidationTimer: number | null = null;
let quickValidationController: AbortController | null = null;
let quickValidationGeneration = 0;

const selectedGeometryId = computed(() => typeof route.query.preview_geometry === "string" ? route.query.preview_geometry : null);
const queries = useGeometryQueries(currentProjectId, selectedGeometryId, offset, geometrySort, topologySmiles, advancedFilters, thermodynamicOnly);
const databaseTotals = useQuery({
  queryKey: computed(() => ["catalog", "geometry-totals", { projectId: currentProjectId.value }]),
  queryFn: async ({ signal }) => {
    const projectId = currentProjectId.value ?? undefined;
    const [reactions, mappedReactions, geometries, artifacts, frames] = await Promise.all([
      api.reactions({ projectId, limit: 1, offset: 0 }, signal),
      api.mappedReactions({ projectId, limit: 1, offset: 0 }, signal),
      api.geometries({ projectId, thermodynamicOnly: false, limit: 1, offset: 0 }, signal),
      api.artifacts({ projectId, limit: 1, offset: 0 }, signal),
      api.frames({ projectId, limit: 1, offset: 0 }, signal),
    ]);
    return {
      reactions: reactions.page.total,
      mappedReactions: mappedReactions.page.total,
      geometries: geometries.page.total,
      artifacts: artifacts.page.total,
      frames: frames.page.total,
    };
  },
  enabled: computed(() => currentProjectId.value !== null),
  staleTime: 30_000,
});
const selectedGeometry = computed<GeometryDetail | null>(() => queries.detail.data.value ?? null);
const geometries = computed(() => queries.list.data.value?.items ?? []);
const page = computed(() => queries.list.data.value?.page ?? { total: 0, limit: 50, offset: offset.value });
const listError = computed(() => {
  const error = queries.list.error.value;
  return error instanceof Error ? error.message : "";
});
const catalogLoading = computed(() => queries.list.isLoading.value);
const catalogQuerying = computed(() =>
  queries.list.isFetching.value
  && (queries.list.isLoading.value || queries.list.isPlaceholderData.value),
);
const detailError = computed(() => queries.detail.error.value instanceof Error ? queries.detail.error.value.message : "");
const advancedConditionCount = computed(() => advancedFilters.value?.filterExpression?.conditions.length ?? 0);

watch(currentProjectId, () => { offset.value = 0; });
watch(thermodynamicOnly, () => { offset.value = 0; });
watch(geometrySort, () => { offset.value = 0; }, { deep: true });
watch(() => route.query.topology, (value) => {
  const nextValue = typeof value === "string" ? value : "";
  if (nextValue === topologySmiles.value) return;
  quickSmilesInput.value = nextValue;
  topologySmiles.value = nextValue;
  advancedFilters.value = null;
  offset.value = 0;
});

function syncRouteQuery(): void {
  void router.replace({
    query: {
      ...withoutAccessState(route.query),
      topology: topologySmiles.value || undefined,
      search: undefined,
      smarts: undefined,
      thermo: undefined,
      imaginary_frequency: undefined,
    },
  });
}
async function validateQuickQuery(value: string): Promise<boolean> {
  const smiles = value.trim();
  if (!smiles) {
    quickValidation.value = { status: "idle", message: "" };
    return true;
  }
  quickValidationController?.abort();
  const controller = new AbortController();
  quickValidationController = controller;
  const generation = ++quickValidationGeneration;
  quickValidation.value = { status: "pending", message: "正在校验 SMILES…" };
  try {
    const result = await api.validateChemistryRepresentation({ kind: "smiles", value: smiles }, controller.signal);
    if (generation !== quickValidationGeneration) return false;
    quickValidation.value = result.valid
      ? { status: "valid", message: "SMILES 格式有效" }
      : { status: "invalid", message: result.error ?? "SMILES 无法解析" };
    return result.valid;
  } catch {
    if (controller.signal.aborted) return false;
    quickValidation.value = { status: "invalid", message: "校验服务暂时不可用" };
    return false;
  } finally {
    if (quickValidationController === controller) quickValidationController = null;
  }
}
function scheduleQuickValidation(value: string): void {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationController?.abort();
  quickValidationController = null;
  const smiles = value.trim();
  if (!smiles) {
    quickValidation.value = { status: "idle", message: "" };
    return;
  }
  quickValidation.value = { status: "pending", message: "等待校验…" };
  quickValidationTimer = window.setTimeout(() => {
    quickValidationTimer = null;
    void validateQuickQuery(smiles);
  }, 320);
}
async function submitSearch(): Promise<void> {
  const smiles = quickSmilesInput.value.trim();
  if (!(await validateQuickQuery(smiles))) return;
  advancedFilters.value = null;
  topologySmiles.value = smiles;
  offset.value = 0;
  syncRouteQuery();
}
function clearFilters(): void {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationTimer = null;
  quickValidationController?.abort();
  quickValidationController = null;
  quickValidation.value = { status: "idle", message: "" };
  advancedFilters.value = null;
  thermodynamicOnly.value = true;
  quickSmilesInput.value = "";
  topologySmiles.value = "";
  offset.value = 0;
  syncRouteQuery();
}
function applyAdvancedQuery(filters: GeometryQueryFilters): void {
  advancedFilters.value = filters;
  quickSmilesInput.value = "";
  topologySmiles.value = "";
  offset.value = 0;
  advancedQueryOpen.value = false;
  syncRouteQuery();
}
function clearAdvancedQuery(): void {
  advancedFilters.value = null;
  offset.value = 0;
}
function openGeometry(id: string): void { void router.replace({ name: "geometries", query: { ...withoutAccessState(route.query), preview_geometry: id } }); }
function closeGeometry(): void { const query = { ...withoutAccessState(route.query) }; delete query.preview_geometry; void router.replace({ name: "geometries", query }); }
function nextPage(): void { if (page.value.offset + page.value.limit < page.value.total) offset.value += page.value.limit; }
function previousPage(): void { if (page.value.offset > 0) offset.value = Math.max(0, offset.value - page.value.limit); }
function jumpPage(nextOffset: number): void { offset.value = nextOffset; }

async function openFrame(id: string): Promise<void> {
  selectedFrameId.value = id;
  frame.value = null;
  frameLoading.value = true;
  frameError.value = "";
  try { frame.value = await api.frame(id, { projectId: currentProjectId.value ?? undefined }); }
  catch (caught) { frameError.value = caught instanceof Error ? caught.message : "计算帧加载失败"; }
  finally { frameLoading.value = false; }
}
function closeFrame(): void { selectedFrameId.value = null; frame.value = null; }

watch(quickSmilesInput, (value) => scheduleQuickValidation(value), { immediate: true });
onBeforeUnmount(() => {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationController?.abort();
});
</script>

<template>
  <main class="workspace-main" aria-labelledby="geometry-view-title">
    <section class="metrics-band" aria-label="当前项目数据库概览">
      <div><span>逻辑反应总数</span><strong>{{ databaseTotals.data.value?.reactions ?? "—" }}</strong></div>
      <div><span>映射方案总数</span><strong>{{ databaseTotals.data.value?.mappedReactions ?? "—" }}</strong></div>
      <div><span>几何构象总数</span><strong>{{ databaseTotals.data.value?.geometries ?? "—" }}</strong></div>
      <div><span>原始文件总数</span><strong>{{ databaseTotals.data.value?.artifacts ?? "—" }}</strong></div>
      <div><span>计算帧总数</span><strong>{{ databaseTotals.data.value?.frames ?? "—" }}</strong></div>
    </section>

    <section class="geometry-browser">
    <aside class="geometry-filter-sidebar">
      <header class="panel-heading"><span class="eyebrow">Geometry</span><h1 id="geometry-view-title">几何构象</h1><p>输入 SMILES 快速查询，复杂条件使用高级查询。</p></header>
      <form class="geometry-filter-form" @submit.prevent="submitSearch">
        <label class="search-field" :class="`is-validation-${quickValidation.status}`"><Search :size="15" aria-hidden="true" /><span class="sr-only">SMILES 快速查询</span><input v-model="quickSmilesInput" type="search" placeholder="输入 SMILES，例如 CCO" aria-label="SMILES 快速查询" :aria-invalid="quickValidation.status === 'invalid'"><QueryValidationIndicator :status="quickValidation.status" :message="quickValidation.message" /></label>
        <div class="filter-actions"><button class="command-button" type="submit" :disabled="catalogQuerying"><LoaderCircle v-if="catalogQuerying" class="is-spinning" :size="15" aria-hidden="true" /><Search v-else :size="15" aria-hidden="true" />{{ catalogQuerying ? "正在查询" : "查询" }}</button><button class="command-button command-button-muted" type="button" @click="advancedQueryOpen = true"><ListFilter :size="15" aria-hidden="true" />高级查询</button><RouterLink class="icon-button" :to="{ name: 'geometry-query-help' }" title="几何构象查询帮助" aria-label="几何构象查询帮助"><CircleHelp :size="16" aria-hidden="true" /></RouterLink><button class="icon-button" type="button" title="清空查询" aria-label="清空查询" @click="clearFilters"><RotateCcw :size="15" aria-hidden="true" /></button></div>
      </form>
      <label class="toggle-filter">
        <input v-model="thermodynamicOnly" type="checkbox" aria-label="仅显示含有热力学属性的几何" :disabled="advancedConditionCount > 0">
        <span><strong>仅显示含有热力学属性的几何</strong><small>{{ advancedConditionCount ? "高级查询已接管筛选条件。" : "默认开启，排除没有热化学结果的构象。" }}</small></span>
      </label>
      <div v-if="advancedConditionCount" class="advanced-query-active">
        <span>高级查询</span><strong>{{ advancedConditionCount }} 个条件</strong>
        <button class="icon-button" type="button" title="清除高级查询" aria-label="清除高级查询" @click="clearAdvancedQuery"><X :size="14" aria-hidden="true" /></button>
      </div>
      <div class="filter-result-count"><strong>{{ page.total }}</strong><span>个匹配构象</span></div>
    </aside>

    <section class="geometry-results" :aria-busy="catalogQuerying">
      <header class="geometry-results-header">
        <div><span class="eyebrow">Conformer catalog</span><h2>{{ advancedConditionCount ? "高级查询结果" : topologySmiles ? "SMILES 查询结果" : thermodynamicOnly ? "含热力学属性的几何构象" : "全部几何构象" }}</h2></div>
        <div class="catalog-header-actions">
          <div class="catalog-sort-controls" aria-label="几何构象排序">
            <label><span>排序</span><select v-model="geometrySort.sortBy" aria-label="几何构象排序字段"><option value="default">默认顺序</option><option value="created_at">创建时间</option><option value="atom_count">原子数</option><option value="calculation_count">计算帧数</option></select></label>
            <label><span>顺序</span><select v-model="geometrySort.sortDirection" aria-label="几何构象排序方向" :disabled="geometrySort.sortBy === 'default'"><option value="asc">升序</option><option value="desc">降序</option></select></label>
          </div>
          <PaginationControls :page="page" label="几何构象分页" @previous="previousPage" @next="nextPage" @jump="jumpPage" />
        </div>
      </header>
      <div class="catalog-query-status-slot" aria-live="polite">
        <div v-if="catalogQuerying" class="catalog-query-status" role="status"><LoaderCircle class="is-spinning" :size="16" aria-hidden="true" /><span>{{ geometries.length ? "正在查询，当前显示上次结果" : "正在查询筛选结果" }}</span></div>
      </div>
      <div v-if="listError" class="notice" role="alert">{{ listError }}</div>
      <div v-if="catalogLoading" class="geometry-card-grid"><div v-for="i in 6" :key="i" class="geometry-card geometry-card-skeleton"><div></div><span></span><span></span></div></div>
      <div v-else-if="!geometries.length" class="workspace-empty"><strong>没有匹配的 Geometry</strong><p>请调整 SMILES，或使用高级查询组合其他条件。</p></div>
      <div v-else class="geometry-card-grid">
        <GeometryCatalogCard v-for="geometry in geometries" :key="geometry.id" :geometry="geometry" :project-id="currentProjectId" :active="geometry.id === selectedGeometryId" @open="openGeometry" />
      </div>
      <PaginationControls :page="page" label="几何构象分页（底部）" @previous="previousPage" @next="nextPage" @jump="jumpPage" />
      <p class="table-summary">显示 {{ geometries.length }} / {{ page.total }} 个构象</p>
    </section>

    <UiDrawer
      :open="selectedGeometryId !== null"
      title="构象详情"
      eyebrow="Geometry detail"
      title-id="geometry-detail-title"
      close-label="关闭构象详情"
      width-class="geometry-detail-drawer"
      @close="closeGeometry"
    >
      <template #actions>
        <RouterLink
          v-if="selectedGeometryId"
          class="icon-button"
          :to="{ name: 'geometry-detail', params: { geometryId: selectedGeometryId }, query: withoutAccessState(route.query) }"
          title="在独立页面打开"
          aria-label="在独立页面打开几何构象"
        ><ArrowUpRight :size="18" aria-hidden="true" /></RouterLink>
      </template>
      <div v-if="queries.detail.isLoading.value" class="drawer-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></div>
      <div v-else-if="detailError" class="drawer-error">{{ detailError }}</div>
      <div v-else-if="!selectedGeometry" class="drawer-error">Geometry 不存在或当前项目不可见</div>
      <GeometryDetailContent v-else-if="selectedGeometry" :geometry="selectedGeometry" :project-id="currentProjectId ?? undefined" @open-frame="openFrame" />
    </UiDrawer>
    <FrameDrawer :open="selectedFrameId !== null" :loading="frameLoading" :error="frameError" :frame="frame" :project-id="currentProjectId ?? undefined" @close="closeFrame" />
    <GeometryAdvancedQueryModal :open="advancedQueryOpen" :project-id="currentProjectId" @close="advancedQueryOpen = false" @apply="applyAdvancedQuery" />
    </section>
  </main>
</template>
