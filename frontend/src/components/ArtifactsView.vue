<script setup lang="ts">
import { ArrowDown, ArrowDownUp, ArrowUp, ArrowUpRight, ChevronDown, CircleHelp, Download, Eye, Globe2, ListFilter, LoaderCircle, LockKeyhole, RotateCcw, Search, Trash2, UploadCloud, X } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, artifactDownloadUrl } from "@/api";
import { emptyArtifactFilters, type ArtifactFilterValues, type ArtifactSort, type ArtifactSortBy } from "@/artifactQuery";
import { formatBytes, formatDurationSeconds, labelFor, shortId, statusTone } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { ArtifactSummary, CalculationFrameSummary, CurrentUser, PageInfo } from "@/types";
import ArtifactIngestionStatus from "./ArtifactIngestionStatus.vue";
import CalculationFrameList from "./CalculationFrameList.vue";
import ArtifactAdvancedQueryModal from "./ArtifactAdvancedQueryModal.vue";
import ChemDoodleFrameMovie3D from "./ChemDoodleFrameMovie3D.vue";
import PaginationControls from "./PaginationControls.vue";
import QueryValidationIndicator from "./QueryValidationIndicator.vue";

const props = defineProps<{
  artifacts: ArtifactSummary[];
  queryFilters: ArtifactFilterValues;
  filterText: string;
  loading: boolean;
  querying: boolean;
  currentUser: CurrentUser | null;
  selectedProjectId: string | null;
  expandedArtifactId: string | null;
  expandedFrames: CalculationFrameSummary[];
  framesLoading: boolean;
  framesError: string;
  total: number;
  page: PageInfo;
  sort: ArtifactSort;
}>();

const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));

const emit = defineEmits<{
  preview: [id: string];
  deleted: [ids: string[]];
  reparsed: [ids: string[]];
  toggleFrames: [id: string];
  openFrame: [id: string];
  previousPage: [];
  nextPage: [];
  jumpPage: [offset: number];
  applyFilters: [filters: ArtifactFilterValues];
  updateSort: [sort: ArtifactSort];
}>();

const filterText = ref(props.filterText);
const advancedQueryOpen = ref(false);
type QueryValidationStatus = "idle" | "valid" | "invalid";
const quickValidation = computed<{ status: QueryValidationStatus; message: string }>(() => {
  const value = filterText.value.trim();
  if (!value) return { status: "idle", message: "" };
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    return { status: "valid", message: "文件 ID 格式有效" };
  }
  if (/^[0-9a-f]{64}$/i.test(value)) return { status: "valid", message: "SHA-256 格式有效" };
  if (/^[0-9a-f-]{20,}$/i.test(value) && value.includes("-")) {
    return { status: "invalid", message: "文件 ID 必须是 UUID 格式" };
  }
  if (/^[0-9a-f]{32,}$/i.test(value)) {
    return { status: "invalid", message: "SHA-256 必须是 64 位十六进制字符串" };
  }
  return { status: "idle", message: "普通文本将按文件名包含匹配" };
});
const advancedConditionCount = computed(() => Object.values(props.queryFilters).filter(Boolean).length);
type ArtifactOperation = "delete" | "reparse";
const selectedArtifactIds = ref<Set<string>>(new Set());
const operationArtifactId = ref<string | null>(null);
const operationKind = ref<ArtifactOperation | null>(null);
const batchOperation = ref<ArtifactOperation | null>(null);
const batchProgress = ref({ completed: 0, total: 0 });
const operationError = ref("");
const operationResult = ref("");

const uploadProjects = computed(() =>
  (props.currentUser?.projects ?? []).filter((project) => project.permissions.includes("artifact:upload")),
);
const canUpload = computed(() => Boolean(
  props.selectedProjectId && uploadProjects.value.some((project) => project.project_id === props.selectedProjectId),
));
const deletableProjectIds = computed(() => new Set(
  (props.currentUser?.projects ?? [])
    .filter((project) => project.permissions.includes("artifact:delete"))
    .map((project) => project.project_id),
));
const reparseableProjectIds = computed(() => new Set(
  (props.currentUser?.projects ?? [])
    .filter((project) => project.permissions.includes("artifact:upload"))
    .map((project) => project.project_id),
));

const operationBusy = computed(() => operationArtifactId.value !== null || batchOperation.value !== null);

function canReparseArtifact(artifact: ArtifactSummary): boolean {
  return artifact.artifact_kind === "calculation_output"
    && artifact.storage_status === "available"
    && artifact.ingestion_status !== "pending"
    && reparseableProjectIds.value.has(artifact.project_id);
}

function canSelectArtifact(artifact: ArtifactSummary): boolean {
  return canDeleteArtifact(artifact) || canReparseArtifact(artifact);
}

const selectableArtifacts = computed(() => props.artifacts.filter(canSelectArtifact));
const selectedArtifacts = computed(() => props.artifacts.filter((artifact) => selectedArtifactIds.value.has(artifact.id)));
const selectedDeletableArtifacts = computed(() => selectedArtifacts.value.filter(canDeleteArtifact));
const selectedReparseableArtifacts = computed(() => selectedArtifacts.value.filter(canReparseArtifact));
const allSelectableSelected = computed(() =>
  selectableArtifacts.value.length > 0
  && selectableArtifacts.value.every((artifact) => selectedArtifactIds.value.has(artifact.id)),
);
const someSelectableSelected = computed(() =>
  selectableArtifacts.value.some((artifact) => selectedArtifactIds.value.has(artifact.id)),
);

function clearSelection(): void {
  selectedArtifactIds.value = new Set();
}

function toggleArtifactSelection(artifact: ArtifactSummary): void {
  if (operationBusy.value || !canSelectArtifact(artifact)) return;
  const next = new Set(selectedArtifactIds.value);
  if (next.has(artifact.id)) next.delete(artifact.id);
  else next.add(artifact.id);
  selectedArtifactIds.value = next;
}

function toggleAllCurrentPage(event: Event): void {
  if (operationBusy.value) return;
  const checked = (event.currentTarget as HTMLInputElement).checked;
  const next = new Set(selectedArtifactIds.value);
  if (checked) selectableArtifacts.value.forEach((artifact) => next.add(artifact.id));
  else selectableArtifacts.value.forEach((artifact) => next.delete(artifact.id));
  selectedArtifactIds.value = next;
}

function applyFilters(): void {
  if (quickValidation.value.status === "invalid") return;
  const filters = emptyArtifactFilters();
  const value = filterText.value.trim();
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    filters.artifactId = value;
  } else if (/^[0-9a-f]{64}$/i.test(value)) {
    filters.contentSha256 = value;
  } else if (value) {
    filters.originalFilenameContains = value;
  }
  emit("applyFilters", filters);
}

function clearFilters(): void {
  filterText.value = "";
  advancedQueryOpen.value = false;
  emit("applyFilters", emptyArtifactFilters());
}

function applyAdvancedFilters(filters: ArtifactFilterValues): void {
  advancedQueryOpen.value = false;
  filterText.value = filters.artifactId ?? filters.contentSha256 ?? filters.originalFilenameContains ?? "";
  emit("applyFilters", filters);
}

function updateSortBy(event: Event): void {
  const sortBy = (event.currentTarget as HTMLButtonElement).dataset.sortBy as ArtifactSortBy;
  const sortDirection = props.sort.sortBy === sortBy
    ? props.sort.sortDirection === "asc" ? "desc" : "asc"
    : "asc";
  emit("updateSort", { sortBy, sortDirection });
}

function sortAriaValue(sortBy: ArtifactSortBy): "ascending" | "descending" | undefined {
  if (props.sort.sortBy !== sortBy) return undefined;
  return props.sort.sortDirection === "asc" ? "ascending" : "descending";
}

function sortButtonLabel(sortBy: ArtifactSortBy, label: string): string {
  if (props.sort.sortBy !== sortBy) return `按${label}排序`;
  return `${label}，当前${props.sort.sortDirection === "asc" ? "升序" : "降序"}，点击切换`;
}

function canDeleteArtifact(artifact: ArtifactSummary): boolean {
  return deletableProjectIds.value.has(artifact.project_id);
}

function operationErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

async function removeArtifact(artifact: ArtifactSummary): Promise<void> {
  if (!canDeleteArtifact(artifact) || operationBusy.value) return;
  const confirmed = window.confirm(
    `确认删除“${artifact.original_filename}”？\n\n文件将从列表中移除，RustFS 中的原始对象也会被删除。此操作无法撤销。`,
  );
  if (!confirmed) return;

  operationArtifactId.value = artifact.id;
  operationKind.value = "delete";
  operationError.value = "";
  operationResult.value = "";
  try {
    await api.deleteArtifact(artifact.id);
    selectedArtifactIds.value = new Set(
      [...selectedArtifactIds.value].filter((id) => id !== artifact.id),
    );
    operationResult.value = `已删除：${artifact.original_filename}`;
    emit("deleted", [artifact.id]);
  } catch (error) {
    operationError.value = operationErrorMessage(error);
  } finally {
    operationArtifactId.value = null;
    operationKind.value = null;
  }
}

async function reparseArtifact(artifact: ArtifactSummary): Promise<void> {
  if (!canReparseArtifact(artifact) || operationBusy.value) return;
  const confirmed = window.confirm(
    `确认重新解析“${artifact.original_filename}”？\n\n系统会使用当前解析器生成新的解析结果，原文件内容不会改变。`,
  );
  if (!confirmed) return;

  operationArtifactId.value = artifact.id;
  operationKind.value = "reparse";
  operationError.value = "";
  operationResult.value = "";
  try {
    await api.reparseArtifact(artifact.id);
    selectedArtifactIds.value = new Set(
      [...selectedArtifactIds.value].filter((id) => id !== artifact.id),
    );
    operationResult.value = `已提交重解析：${artifact.original_filename}`;
    emit("reparsed", [artifact.id]);
  } catch (error) {
    operationError.value = operationErrorMessage(error);
  } finally {
    operationArtifactId.value = null;
    operationKind.value = null;
  }
}

interface ArtifactBatchFailure {
  artifact: ArtifactSummary;
  message: string;
}

async function executeBatchOperation(
  artifacts: ArtifactSummary[],
  operation: ArtifactOperation,
): Promise<{ succeeded: ArtifactSummary[]; failed: ArtifactBatchFailure[] }> {
  const succeeded: ArtifactSummary[] = [];
  const failed: ArtifactBatchFailure[] = [];
  let nextIndex = 0;
  let completed = 0;

  async function worker(): Promise<void> {
    while (true) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= artifacts.length) return;
      const artifact = artifacts[index];
      try {
        if (operation === "delete") await api.deleteArtifact(artifact.id);
        else await api.reparseArtifact(artifact.id);
        succeeded.push(artifact);
      } catch (error) {
        failed.push({ artifact, message: operationErrorMessage(error) });
      } finally {
        completed += 1;
        batchProgress.value = { completed, total: artifacts.length };
      }
    }
  }

  const workerCount = Math.min(4, artifacts.length);
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  return { succeeded, failed };
}

async function operateSelected(operation: ArtifactOperation): Promise<void> {
  if (operationBusy.value) return;
  const candidates = operation === "delete"
    ? selectedDeletableArtifacts.value
    : selectedReparseableArtifacts.value;
  if (!candidates.length) return;

  const actionLabel = operation === "delete" ? "删除" : "重解析";
  const skippedCount = selectedArtifacts.value.length - candidates.length;
  const skippedNotice = skippedCount
    ? `\n\n另有 ${skippedCount} 个已选文件不满足该操作条件，将被跳过。`
    : "";
  const confirmed = window.confirm(
    `确认${actionLabel}选中的 ${candidates.length} 个文件？${operation === "delete" ? "\n\n删除会同时清理 RustFS 中的原始对象，且无法撤销。" : "\n\n系统会为每个文件使用当前解析器生成新的解析结果，原文件内容不会改变。"}${skippedNotice}`,
  );
  if (!confirmed) return;

  batchOperation.value = operation;
  batchProgress.value = { completed: 0, total: candidates.length };
  operationError.value = "";
  operationResult.value = "";
  try {
    const result = await executeBatchOperation(candidates, operation);
    const succeededIds = new Set(result.succeeded.map((artifact) => artifact.id));
    selectedArtifactIds.value = new Set(
      [...selectedArtifactIds.value].filter((id) => !succeededIds.has(id)),
    );

    if (result.succeeded.length) {
      operationResult.value = `${actionLabel}完成：成功 ${result.succeeded.length} 个${result.failed.length ? `，失败 ${result.failed.length} 个` : ""}`;
      if (operation === "delete") emit("deleted", result.succeeded.map((artifact) => artifact.id));
      else emit("reparsed", result.succeeded.map((artifact) => artifact.id));
    }
    if (result.failed.length) {
      const details = result.failed
        .slice(0, 3)
        .map(({ artifact, message }) => `${artifact.original_filename}（${message}）`)
        .join("；");
      operationError.value = `${actionLabel}失败：${details}${result.failed.length > 3 ? "；……" : ""}`;
    }
  } catch (error) {
    operationError.value = `${actionLabel}失败：${operationErrorMessage(error)}`;
  } finally {
    batchOperation.value = null;
  }
}

watch(
  () => props.filterText,
  (nextText) => { filterText.value = nextText ?? ""; },
);

watch(
  () => `${props.page.offset}:${props.artifacts.map((artifact) => artifact.id).join(",")}`,
  clearSelection,
);
</script>

<template>
  <section class="artifact-browser" aria-labelledby="artifact-view-title">
    <aside class="artifact-filter-sidebar">
      <header class="panel-heading">
        <span class="eyebrow">Immutable Artifact</span>
        <h1 id="artifact-view-title">原始文件</h1>
        <p>输入文件 ID、SHA-256 或名称快速查询，其他条件使用高级筛选。</p>
      </header>

      <form class="artifact-filter-form" aria-label="原始文件筛选" @submit.prevent="applyFilters">
        <label class="search-field" :class="`is-validation-${quickValidation.status}`">
          <Search :size="15" aria-hidden="true" />
          <span class="sr-only">按文件 ID、SHA-256 或名称筛选</span>
          <input v-model="filterText" type="search" placeholder="文件 ID、SHA-256 或名称" aria-label="按文件 ID、SHA-256 或名称筛选" :aria-invalid="quickValidation.status === 'invalid'">
          <QueryValidationIndicator :status="quickValidation.status" :message="quickValidation.message" />
        </label>
        <div class="filter-actions">
          <button class="command-button" type="submit" :disabled="querying"><LoaderCircle v-if="querying" class="is-spinning" :size="15" aria-hidden="true" /><Search v-else :size="15" aria-hidden="true" />{{ querying ? "正在查询" : "查询" }}</button>
          <button class="command-button command-button-muted" type="button" @click="advancedQueryOpen = true"><ListFilter :size="15" aria-hidden="true" />高级筛选</button>
          <RouterLink class="icon-button" :to="{ name: 'artifact-query-help' }" title="原始文件查询帮助" aria-label="原始文件查询帮助"><CircleHelp :size="16" aria-hidden="true" /></RouterLink>
          <button class="icon-button" type="button" title="清空文件筛选" aria-label="清空文件筛选" @click="clearFilters"><RotateCcw :size="15" aria-hidden="true" /></button>
        </div>
      </form>
      <div v-if="advancedConditionCount" class="advanced-query-active">
        <span>当前筛选</span><strong>{{ advancedConditionCount }} 个条件</strong>
        <button class="icon-button" type="button" title="清除文件筛选" aria-label="清除文件筛选" @click="clearFilters"><X :size="14" aria-hidden="true" /></button>
      </div>
      <div class="filter-result-count"><strong>{{ total >= 0 ? total : artifacts.length }}</strong><span>{{ total >= 0 ? "个匹配文件" : "个本页文件" }}</span></div>
      <div class="artifact-upload-toolbar">
        <RouterLink v-if="canUpload" class="command-button" :to="{ name: 'uploads', query: { ...navigationQuery, project_id: selectedProjectId } }">
          <UploadCloud :size="16" aria-hidden="true" />批量上传
        </RouterLink>
      </div>
    </aside>

    <section class="artifact-results" aria-labelledby="artifact-results-title" :aria-busy="querying">
      <header class="artifact-results-header">
        <div>
          <span class="eyebrow">Artifact catalog</span>
          <h2 id="artifact-results-title">文件目录</h2>
        </div>
        <div class="catalog-header-actions">
          <PaginationControls :page="page" label="原始文件分页（顶部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
        </div>
      </header>
      <div class="catalog-query-status-slot" aria-live="polite">
        <div v-if="querying" class="catalog-query-status" role="status"><LoaderCircle class="is-spinning" :size="16" aria-hidden="true" /><span>{{ artifacts.length ? "正在查询，当前显示上次结果" : "正在查询筛选结果" }}</span></div>
      </div>

      <div v-if="selectedArtifacts.length" class="artifact-bulk-toolbar" aria-live="polite">
        <div class="artifact-bulk-summary">
          <strong>已选 {{ selectedArtifacts.length }} 个当前页文件</strong>
          <span v-if="batchOperation">{{ batchOperation === "delete" ? "正在批量删除" : "正在批量重解析" }} {{ batchProgress.completed }} / {{ batchProgress.total }}</span>
          <span v-else>删除 {{ selectedDeletableArtifacts.length }} 个 · 重解析 {{ selectedReparseableArtifacts.length }} 个</span>
        </div>
        <div class="artifact-bulk-actions">
          <button
            v-if="selectedReparseableArtifacts.length"
            class="command-button"
            type="button"
            :disabled="operationBusy"
            @click="operateSelected('reparse')"
          >
            <LoaderCircle v-if="batchOperation === 'reparse'" class="is-spinning" :size="15" aria-hidden="true" />
            <RotateCcw v-else :size="15" aria-hidden="true" />
            重解析 {{ selectedReparseableArtifacts.length }} 个
          </button>
          <button
            v-if="selectedDeletableArtifacts.length"
            class="command-button command-button-danger"
            type="button"
            :disabled="operationBusy"
            @click="operateSelected('delete')"
          >
            <LoaderCircle v-if="batchOperation === 'delete'" class="is-spinning" :size="15" aria-hidden="true" />
            <Trash2 v-else :size="15" aria-hidden="true" />
            删除 {{ selectedDeletableArtifacts.length }} 个
          </button>
          <button class="command-button command-button-muted" type="button" :disabled="operationBusy" @click="clearSelection">取消选择</button>
        </div>
      </div>

    <p v-if="operationResult" class="upload-result" role="status">{{ operationResult }}</p>
    <p v-if="operationError" class="inline-error" role="alert">{{ operationError }}</p>

    <div class="data-table-wrap">
      <table class="data-table artifacts-table">
        <thead>
          <tr>
            <th class="selection-header">
              <input
                type="checkbox"
                :checked="allSelectableSelected"
                :indeterminate="someSelectableSelected && !allSelectableSelected"
                :disabled="!selectableArtifacts.length || operationBusy"
                aria-label="选择当前页可操作文件"
                @change="toggleAllCurrentPage"
              >
            </th>
            <th scope="col" :aria-sort="sortAriaValue('original_filename')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'original_filename'" :aria-label="sortButtonLabel('original_filename', '文件名')" @click="updateSortBy">
                <span>文件</span>
                <ArrowUp v-if="sort.sortBy === 'original_filename' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'original_filename'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th scope="col" :aria-sort="sortAriaValue('artifact_kind')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'artifact_kind'" :aria-label="sortButtonLabel('artifact_kind', '文件类型')" @click="updateSortBy">
                <span>类型</span>
                <ArrowUp v-if="sort.sortBy === 'artifact_kind' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'artifact_kind'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th>可见性</th>
            <th scope="col" :aria-sort="sortAriaValue('size_bytes')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'size_bytes'" :aria-label="sortButtonLabel('size_bytes', '文件大小')" @click="updateSortBy">
                <span>大小</span>
                <ArrowUp v-if="sort.sortBy === 'size_bytes' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'size_bytes'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th scope="col" :aria-sort="sortAriaValue('running_time_seconds')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'running_time_seconds'" :aria-label="sortButtonLabel('running_time_seconds', '文件总耗时')" @click="updateSortBy">
                <span>文件总耗时</span>
                <ArrowUp v-if="sort.sortBy === 'running_time_seconds' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'running_time_seconds'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th scope="col" :aria-sort="sortAriaValue('storage_status')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'storage_status'" :aria-label="sortButtonLabel('storage_status', '存储状态')" @click="updateSortBy">
                <span>存储状态</span>
                <ArrowUp v-if="sort.sortBy === 'storage_status' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'storage_status'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th>解析状态</th>
            <th>SHA-256</th>
            <th scope="col" :aria-sort="sortAriaValue('created_at')">
              <button class="data-table-sort-button" type="button" :data-sort-by="'created_at'" :aria-label="sortButtonLabel('created_at', '创建时间')" @click="updateSortBy">
                <span>创建时间</span>
                <ArrowUp v-if="sort.sortBy === 'created_at' && sort.sortDirection === 'asc'" :size="13" aria-hidden="true" />
                <ArrowDown v-else-if="sort.sortBy === 'created_at'" :size="13" aria-hidden="true" />
                <ArrowDownUp v-else :size="13" aria-hidden="true" />
              </button>
            </th>
            <th>验证时间</th>
            <th><span class="sr-only">文件操作</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !artifacts.length">
            <td colspan="12"><div class="table-loading">正在加载原始文件</div></td>
          </tr>
          <tr v-else-if="!artifacts.length">
            <td colspan="12"><div class="compact-empty">没有匹配的原始文件</div></td>
          </tr>
          <template v-for="artifact in artifacts" v-else :key="artifact.id">
          <tr class="artifact-row" :class="{ 'is-expanded': artifact.id === expandedArtifactId }" @click="emit('toggleFrames', artifact.id)">
            <td class="selection-cell">
              <input
                type="checkbox"
                class="artifact-select-checkbox"
                :checked="selectedArtifactIds.has(artifact.id)"
                :disabled="!canSelectArtifact(artifact) || operationBusy"
                :title="canSelectArtifact(artifact) ? '选择文件' : '当前账户没有可用的文件操作权限'"
                :aria-label="`选择文件 ${artifact.original_filename}`"
                @click.stop
                @change="toggleArtifactSelection(artifact)"
              >
            </td>
            <td>
              <button class="artifact-name-button" type="button" @click.stop="emit('toggleFrames', artifact.id)">
                <ChevronDown :size="16" :class="{ 'is-rotated': artifact.id === expandedArtifactId }" aria-hidden="true" />
                <span>
                <strong>{{ artifact.original_filename }}</strong>
                <span>{{ artifact.media_type }}</span>
                </span>
              </button>
            </td>
            <td>{{ labelFor(artifact.artifact_kind) }}</td>
            <td>
              <span class="visibility-label">
                <Globe2 v-if="artifact.visibility === 'public'" :size="14" aria-hidden="true" />
                <LockKeyhole v-else :size="14" aria-hidden="true" />
                {{ artifact.visibility === "public" ? "公开" : "项目内" }}
              </span>
            </td>
            <td class="number-cell">{{ formatBytes(artifact.size_bytes) }}</td>
            <td class="number-cell">{{ formatDurationSeconds(artifact.running_time_seconds) }}</td>
            <td><span class="status-dot" :class="statusTone(artifact.storage_status)">{{ labelFor(artifact.storage_status) }}</span></td>
            <td>
              <ArtifactIngestionStatus :status="artifact.ingestion_status" :error-message="artifact.ingestion_error_message" />
            </td>
            <td><code :title="artifact.content_sha256">{{ shortId(artifact.content_sha256) }}</code></td>
            <td>{{ artifact.created_at ? new Date(artifact.created_at).toLocaleString("zh-CN") : "—" }}</td>
            <td>{{ artifact.storage_verified_at ? new Date(artifact.storage_verified_at).toLocaleString("zh-CN") : "—" }}</td>
            <td>
              <div class="table-actions">
                <RouterLink
                  class="table-action"
                  :to="{ name: 'artifact-detail', params: { artifactId: artifact.id }, query: navigationQuery }"
                  title="在独立页面打开原始文件"
                  :aria-label="`在独立页面打开原始文件 ${artifact.original_filename}`"
                  @click.stop
                >
                  <ArrowUpRight :size="15" aria-hidden="true" />
                </RouterLink>
                <button class="table-action" type="button" title="预览文件" aria-label="预览文件" :disabled="artifact.storage_status !== 'available'" @click.stop="emit('preview', artifact.id)">
                  <Eye :size="15" aria-hidden="true" />
                </button>
                <a class="table-action" :class="{ 'is-disabled': artifact.storage_status !== 'available' }" :href="artifactDownloadUrl(artifact.id)" :download="artifact.original_filename" title="下载文件" aria-label="下载文件" @click.stop>
                  <Download :size="15" aria-hidden="true" />
                </a>
                <button
                  v-if="canReparseArtifact(artifact)"
                  class="table-action"
                  :class="{ 'is-spinning': operationArtifactId === artifact.id && operationKind === 'reparse' }"
                  type="button"
                  title="重新解析文件"
                  :aria-label="`重新解析文件 ${artifact.original_filename}`"
                  :disabled="operationBusy"
                  @click.stop="reparseArtifact(artifact)"
                >
                  <LoaderCircle v-if="operationArtifactId === artifact.id && operationKind === 'reparse'" :size="15" aria-hidden="true" />
                  <RotateCcw v-else :size="15" aria-hidden="true" />
                </button>
                <button
                  v-if="canDeleteArtifact(artifact)"
                  class="table-action is-danger"
                  :class="{ 'is-spinning': operationArtifactId === artifact.id && operationKind === 'delete' }"
                  type="button"
                  title="删除文件"
                  :aria-label="`删除文件 ${artifact.original_filename}`"
                  :disabled="operationBusy"
                  @click.stop="removeArtifact(artifact)"
                >
                  <LoaderCircle v-if="operationArtifactId === artifact.id && operationKind === 'delete'" :size="15" aria-hidden="true" />
                  <Trash2 v-else :size="15" aria-hidden="true" />
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="artifact.id === expandedArtifactId" class="artifact-frames-row">
            <td colspan="12">
              <div class="artifact-frames-panel">
                <div class="artifact-frames-content">
                  <section class="artifact-frame-list-pane">
                    <header>
                      <div><span class="eyebrow">CalculationFrame</span><strong>文件中的全部计算帧</strong></div>
                      <span>{{ expandedFrames.length }} 帧</span>
                    </header>
                    <div class="artifact-frame-list-scroll">
                      <CalculationFrameList :frames="expandedFrames" :loading="framesLoading" :error="framesError" @open="emit('openFrame', $event)" />
                    </div>
                  </section>
                  <ChemDoodleFrameMovie3D :frames="expandedFrames" :project-id="selectedProjectId ?? undefined" />
                </div>
              </div>
            </td>
          </tr>
          </template>
        </tbody>
      </table>
    </div>
    <PaginationControls :page="page" label="原始文件分页（底部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
    <p class="table-summary">{{ total >= 0 ? `显示 ${artifacts.length} / ${total} 个文件` : `本页显示 ${artifacts.length} 个文件` }}</p>
    </section>
    <ArtifactAdvancedQueryModal :open="advancedQueryOpen" :initial-filters="queryFilters" @close="advancedQueryOpen = false" @apply="applyAdvancedFilters" />
  </section>
</template>
