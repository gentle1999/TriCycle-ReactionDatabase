<script setup lang="ts">
import { ArrowUpRight, ChevronDown, CircleHelp, Download, Eye, Globe2, ListFilter, LoaderCircle, LockKeyhole, RotateCcw, Search, Trash2, UploadCloud, X } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api, artifactDownloadUrl } from "@/api";
import { emptyArtifactFilters, type ArtifactFilterValues } from "@/artifactQuery";
import { artifactLabels, formatBytes, shortId, statusTone } from "@/format";
import { withoutAccessState } from "@/routeAccessState";
import type { ArtifactSummary, CalculationFrameSummary, CurrentUser, PageInfo } from "@/types";
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
  currentUser: CurrentUser | null;
  selectedProjectId: string | null;
  expandedArtifactId: string | null;
  expandedFrames: CalculationFrameSummary[];
  framesLoading: boolean;
  framesError: string;
  total: number;
  page: PageInfo;
}>();

const route = useRoute();
const navigationQuery = computed(() => withoutAccessState(route.query));

const emit = defineEmits<{
  preview: [id: string];
  deleted: [id: string];
  toggleFrames: [id: string];
  openFrame: [id: string];
  previousPage: [];
  nextPage: [];
  jumpPage: [offset: number];
  applyFilters: [filters: ArtifactFilterValues];
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
const deletingArtifactId = ref<string | null>(null);
const deleteError = ref("");
const deleteResult = ref("");

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

function canDeleteArtifact(artifact: ArtifactSummary): boolean {
  return deletableProjectIds.value.has(artifact.project_id);
}

async function removeArtifact(artifact: ArtifactSummary): Promise<void> {
  if (!canDeleteArtifact(artifact) || deletingArtifactId.value !== null) return;
  const confirmed = window.confirm(
    `确认删除“${artifact.original_filename}”？\n\n文件将从列表中移除，RustFS 中的原始对象也会被删除。此操作无法撤销。`,
  );
  if (!confirmed) return;

  deletingArtifactId.value = artifact.id;
  deleteError.value = "";
  deleteResult.value = "";
  try {
    await api.deleteArtifact(artifact.id);
    deleteResult.value = `已删除：${artifact.original_filename}`;
    emit("deleted", artifact.id);
  } catch (error) {
    deleteError.value = error instanceof Error ? error.message : "删除失败";
  } finally {
    deletingArtifactId.value = null;
  }
}

watch(
  () => props.filterText,
  (nextText) => { filterText.value = nextText ?? ""; },
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
          <button class="command-button" type="submit"><Search :size="15" aria-hidden="true" />查询</button>
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
    </aside>

    <section class="artifact-results" aria-labelledby="artifact-results-title">
      <header class="artifact-results-header">
        <div>
          <span class="eyebrow">Artifact catalog</span>
          <h2 id="artifact-results-title">文件目录</h2>
        </div>
        <PaginationControls :page="page" label="原始文件分页（顶部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
      </header>
      <div class="artifact-upload-toolbar">
        <RouterLink v-if="canUpload" class="command-button" :to="{ name: 'uploads', query: { ...navigationQuery, project_id: selectedProjectId } }">
          <UploadCloud :size="16" aria-hidden="true" />批量上传
        </RouterLink>
      </div>

    <p v-if="deleteError" class="inline-error" role="alert">{{ deleteError }}</p>
    <p v-else-if="deleteResult" class="upload-result" role="status">{{ deleteResult }}</p>

    <div class="data-table-wrap">
      <table class="data-table artifacts-table">
        <thead>
          <tr>
            <th>文件</th>
            <th>类型</th>
            <th>可见性</th>
            <th>大小</th>
            <th>存储状态</th>
            <th>SHA-256</th>
            <th>验证时间</th>
            <th><span class="sr-only">文件操作</span></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !artifacts.length">
            <td colspan="8"><div class="table-loading">正在加载原始文件</div></td>
          </tr>
          <tr v-else-if="!artifacts.length">
            <td colspan="8"><div class="compact-empty">没有匹配的原始文件</div></td>
          </tr>
          <template v-for="artifact in artifacts" v-else :key="artifact.id">
          <tr class="artifact-row" :class="{ 'is-expanded': artifact.id === expandedArtifactId }" @click="emit('toggleFrames', artifact.id)">
            <td>
              <button class="artifact-name-button" type="button" @click.stop="emit('toggleFrames', artifact.id)">
                <ChevronDown :size="16" :class="{ 'is-rotated': artifact.id === expandedArtifactId }" aria-hidden="true" />
                <span>
                <strong>{{ artifact.original_filename }}</strong>
                <span>{{ artifact.media_type }}</span>
                </span>
              </button>
            </td>
            <td>{{ artifactLabels[artifact.artifact_kind] || artifact.artifact_kind }}</td>
            <td>
              <span class="visibility-label">
                <Globe2 v-if="artifact.visibility === 'public'" :size="14" aria-hidden="true" />
                <LockKeyhole v-else :size="14" aria-hidden="true" />
                {{ artifact.visibility === "public" ? "公开" : "项目内" }}
              </span>
            </td>
            <td class="number-cell">{{ formatBytes(artifact.size_bytes) }}</td>
            <td><span class="status-dot" :class="statusTone(artifact.storage_status)">{{ artifact.storage_status }}</span></td>
            <td><code :title="artifact.content_sha256">{{ shortId(artifact.content_sha256) }}</code></td>
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
                  v-if="canDeleteArtifact(artifact)"
                  class="table-action is-danger"
                  :class="{ 'is-spinning': deletingArtifactId === artifact.id }"
                  type="button"
                  title="删除文件"
                  :aria-label="`删除文件 ${artifact.original_filename}`"
                  :disabled="deletingArtifactId !== null"
                  @click.stop="removeArtifact(artifact)"
                >
                  <LoaderCircle v-if="deletingArtifactId === artifact.id" :size="15" aria-hidden="true" />
                  <Trash2 v-else :size="15" aria-hidden="true" />
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="artifact.id === expandedArtifactId" class="artifact-frames-row">
            <td colspan="8">
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
