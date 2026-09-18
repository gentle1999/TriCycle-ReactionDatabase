<script setup lang="ts">
import { ArrowLeft, Download, FileText, LoaderCircle, RotateCcw, Save, Trash2 } from "@lucide/vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { api, artifactDownloadUrl } from "@/api";
import ArtifactIngestionStatus from "@/components/ArtifactIngestionStatus.vue";
import CalculationFrameList from "@/components/CalculationFrameList.vue";
import ChemDoodleFrameMovie3D from "@/components/ChemDoodleFrameMovie3D.vue";
import FrameDrawer from "@/components/FrameDrawer.vue";
import { useProjectContext } from "@/composables/useProjectContext";
import { formatBytes, formatDurationSeconds, labelFor, shortId, statusTone } from "@/format";
import { queryClient } from "@/queryClient";
import { withoutAccessState } from "@/routeAccessState";
import type { ArtifactSummary, CalculationFrameSummary, Page, ParseRevisionSummary } from "@/types";

const route = useRoute();
const router = useRouter();
const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const artifactId = computed(() => typeof route.params.artifactId === "string" ? route.params.artifactId : null);
const navigationQuery = computed(() => withoutAccessState(route.query));
const selectedFrameId = ref<string | null>(null);

const artifactQuery = useQuery({
  queryKey: computed(() => ["artifact-detail", { id: artifactId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.artifact(artifactId.value ?? "", { projectId: currentProjectId.value ?? undefined }, signal),
  enabled: computed(() => artifactId.value !== null && currentProjectId.value !== null),
  staleTime: 60_000,
  refetchInterval: 5_000,
});

const artifact = computed(() => artifactQuery.data.value ?? null);
const parseRevisionsQuery = useQuery({
  queryKey: computed(() => ["artifact-detail-parse-revisions", { artifactId: artifactId.value, projectId: currentProjectId.value }]),
  queryFn: ({ signal }) => api.parseRevisions({
    artifactFileId: artifactId.value ?? "",
    projectId: currentProjectId.value ?? undefined,
    limit: 50,
    offset: 0,
  }, signal),
  enabled: computed(() => artifact.value !== null && currentProjectId.value !== null),
  staleTime: 30_000,
});

const latestParseRevision = computed<ParseRevisionSummary | null>(() => {
  const revisions = parseRevisionsQuery.data.value?.items ?? [];
  return revisions.reduce<ParseRevisionSummary | null>(
    (latest, revision) => latest === null || revision.revision_number > latest.revision_number ? revision : latest,
    null,
  );
});
const fileComments = computed(() => latestParseRevision.value?.comments?.items ?? []);
const canManageNotes = computed(() => projectContext.can("artifact:manage"));
const canDownloadArtifact = computed(() => Boolean(
  artifact.value
  && artifact.value.storage_status === "available"
  && (artifact.value.visibility === "public" || projectContext.can("artifact:download")),
));
const canReparseArtifact = computed(() => Boolean(
  artifact.value
  && artifact.value.artifact_kind === "calculation_output"
  && artifact.value.storage_status === "available"
  && !["pending", "processing"].includes(artifact.value.ingestion_status ?? "")
  && projectContext.can("artifact:upload"),
));
const canDeleteArtifact = computed(() => projectContext.can("artifact:delete"));
type ArtifactOperation = "delete" | "reparse";
const artifactOperation = ref<ArtifactOperation | null>(null);
const operationError = ref("");
const operationResult = ref("");
const operationBusy = computed(() => artifactOperation.value !== null);
const noteDraft = ref("");
const noteSaving = ref(false);
const noteError = ref("");
const noteSaved = ref("");

watch(
  () => artifact.value?.id,
  () => {
    noteDraft.value = artifact.value?.notes ?? "";
    noteError.value = "";
    noteSaved.value = "";
  },
  { immediate: true },
);

async function saveArtifactNotes(): Promise<void> {
  if (!artifact.value || noteSaving.value) return;
  noteSaving.value = true;
  noteError.value = "";
  noteSaved.value = "";
  try {
    const updated = await api.updateArtifactNotes(
      artifact.value.id,
      noteDraft.value.trim() || null,
    );
    noteDraft.value = updated.notes ?? "";
    queryClient.setQueryData(
      ["artifact-detail", { id: updated.id, projectId: currentProjectId.value }],
      updated,
    );
    queryClient.setQueriesData<Page<ArtifactSummary>>(
      { queryKey: ["catalog", "artifacts"] },
      (page) => {
        if (!page) return page;
        const items = page.items.map((item) => item.id === updated.id ? updated : item);
        return items.some((item, index) => item !== page.items[index])
          ? { ...page, items }
          : page;
      },
    );
    noteSaved.value = "备注已保存";
  } catch (error) {
    noteError.value = error instanceof Error ? error.message : "备注保存失败";
  } finally {
    noteSaving.value = false;
  }
}

function operationErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function applyAcceptedReparse(item: { status?: string | null; ingestion_status?: string | null }): void {
  if (!artifact.value) return;
  const ingestionStatus: ArtifactSummary["ingestion_status"] =
    item.status === "processing" || item.ingestion_status === "processing"
      ? "processing"
      : "pending";
  const updated: ArtifactSummary = {
    ...artifact.value,
    ingestion_status: ingestionStatus,
    source_frame_count: null,
    transition_state_frame_count: null,
    running_time_seconds: null,
    ingestion_error_code: null,
    ingestion_error_message: null,
  };
  queryClient.setQueryData(
    ["artifact-detail", { id: updated.id, projectId: currentProjectId.value }],
    updated,
  );
  queryClient.setQueriesData<Page<ArtifactSummary>>(
    { queryKey: ["catalog", "artifacts"] },
    (page) => {
      if (!page) return page;
      const items = page.items.map((item) => item.id === updated.id ? updated : item);
      return items.some((item, index) => item !== page.items[index]) ? { ...page, items } : page;
    },
  );
  queryClient.invalidateQueries({
    queryKey: ["artifact-detail-parse-revisions", { artifactId: updated.id, projectId: currentProjectId.value }],
  });
  queryClient.invalidateQueries({
    queryKey: ["artifact-detail-frames", { artifactId: updated.id, projectId: updated.project_id }],
  });
}

async function reparseArtifact(): Promise<void> {
  if (!artifact.value || !canReparseArtifact.value || operationBusy.value) return;
  const currentArtifact = artifact.value;
  const confirmed = window.confirm(
    `确认重新解析“${currentArtifact.original_filename}”？\n\n系统会使用当前解析器生成新的解析结果，原文件内容和文件备注不会改变。`,
  );
  if (!confirmed) return;

  artifactOperation.value = "reparse";
  operationError.value = "";
  operationResult.value = "";
  try {
    const accepted = await api.reparseArtifact(currentArtifact.id);
    applyAcceptedReparse(accepted.item);
    operationResult.value = `已提交重解析：${currentArtifact.original_filename}`;
  } catch (error) {
    operationError.value = operationErrorMessage(error);
  } finally {
    artifactOperation.value = null;
  }
}

async function deleteArtifact(): Promise<void> {
  if (!artifact.value || !canDeleteArtifact.value || operationBusy.value) return;
  const currentArtifact = artifact.value;
  const confirmed = window.confirm(
    `确认删除“${currentArtifact.original_filename}”？\n\n文件将从列表中移除，RustFS 中的原始对象也会被删除。此操作无法撤销。`,
  );
  if (!confirmed) return;

  artifactOperation.value = "delete";
  operationError.value = "";
  operationResult.value = "";
  try {
    await api.deleteArtifact(currentArtifact.id);
    queryClient.removeQueries({ queryKey: ["artifact-detail", { id: currentArtifact.id }] });
    queryClient.removeQueries({ queryKey: ["artifact-detail-preview", { id: currentArtifact.id }] });
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["catalog", "artifacts"] }),
      queryClient.invalidateQueries({ queryKey: ["catalog", "totals"] }),
      queryClient.invalidateQueries({ queryKey: ["catalog", "artifact-frames"] }),
    ]);
    await router.push({ name: "artifacts", query: navigationQuery.value });
  } catch (error) {
    operationError.value = operationErrorMessage(error);
  } finally {
    artifactOperation.value = null;
  }
}

const previewQuery = useQuery({
  queryKey: computed(() => ["artifact-detail-preview", { id: artifactId.value, projectId: artifact.value?.project_id }]),
  queryFn: ({ signal }) => api.artifactPreview(artifactId.value ?? "", { projectId: artifact.value?.project_id }, signal),
  enabled: computed(() => artifact.value?.storage_status === "available"),
  staleTime: 60_000,
});
const effectiveMediaType = computed(
  () => previewQuery.data.value?.media_type ?? artifact.value?.media_type ?? "application/octet-stream",
);

async function loadArtifactFrames(signal: AbortSignal): Promise<Page<CalculationFrameSummary>> {
  const firstPage = await api.frames({
    artifactFileId: artifactId.value ?? "",
    projectId: artifact.value?.project_id,
    limit: 200,
    offset: 0,
  }, signal);
  const remainingOffsets = Array.from(
    { length: Math.max(0, Math.ceil(firstPage.page.total / 200) - 1) },
    (_, index) => (index + 1) * 200,
  );
  const remainingPages = await Promise.all(remainingOffsets.map((offset) => api.frames({
    artifactFileId: artifactId.value ?? "",
    projectId: artifact.value?.project_id,
    limit: 200,
    offset,
  }, signal)));
  const items = [firstPage, ...remainingPages]
    .flatMap((page) => page.items)
    .sort((left, right) => left.file_frame_index - right.file_frame_index);
  return { items, page: { total: firstPage.page.total, limit: items.length, offset: 0 } };
}

const framesQuery = useQuery({
  queryKey: computed(() => ["artifact-detail-frames", { artifactId: artifactId.value, projectId: artifact.value?.project_id }]),
  queryFn: ({ signal }) => loadArtifactFrames(signal),
  enabled: computed(() => artifact.value !== null),
  staleTime: 30_000,
});

watch(
  () => artifact.value?.ingestion_status,
  (status, previousStatus) => {
    if (["pending", "processing"].includes(previousStatus ?? "")
      && !["pending", "processing"].includes(status ?? "")) {
      void framesQuery.refetch();
      void parseRevisionsQuery.refetch();
    }
  },
);

const frameQuery = useQuery({
  queryKey: computed(() => ["artifact-detail-frame", { frameId: selectedFrameId.value, projectId: artifact.value?.project_id }]),
  queryFn: ({ signal }) => api.frame(selectedFrameId.value ?? "", { projectId: artifact.value?.project_id }, signal),
  enabled: computed(() => selectedFrameId.value !== null && artifact.value !== null),
  staleTime: 60_000,
});

const detailError = computed(() => artifactQuery.error.value instanceof Error ? artifactQuery.error.value.message : "");
const previewError = computed(() => previewQuery.error.value instanceof Error ? previewQuery.error.value.message : "");
const framesError = computed(() => framesQuery.error.value instanceof Error ? framesQuery.error.value.message : "");
const frameError = computed(() => frameQuery.error.value instanceof Error ? frameQuery.error.value.message : "");
</script>

<template>
  <main class="entity-detail-page artifact-detail-page" aria-labelledby="artifact-detail-title">
    <header class="entity-detail-header">
      <div>
        <RouterLink class="entity-back-link" :to="{ name: 'artifacts', query: navigationQuery }">
          <ArrowLeft :size="15" aria-hidden="true" />原始文件目录
        </RouterLink>
        <span class="eyebrow">Immutable Artifact</span>
        <h1 id="artifact-detail-title">原始文件详情</h1>
        <p>{{ artifact?.original_filename ?? "查看原始计算文件的存储信息、内容和关联计算帧。" }}</p>
      </div>
      <div v-if="artifact" class="artifact-detail-actions" aria-label="文件操作">
        <a
          v-if="canDownloadArtifact"
          class="command-button is-quiet"
          :href="artifactDownloadUrl(artifact.id, artifact.project_id)"
          :download="artifact.original_filename"
          title="下载原始文件"
          aria-label="下载原始文件"
        >
          <Download :size="15" aria-hidden="true" />下载文件
        </a>
        <button
          v-if="canReparseArtifact"
          class="command-button is-quiet"
          type="button"
          :disabled="operationBusy"
          :aria-label="`重新解析文件 ${artifact.original_filename}`"
          @click="reparseArtifact"
        >
          <LoaderCircle v-if="artifactOperation === 'reparse'" class="is-spinning" :size="15" aria-hidden="true" />
          <RotateCcw v-else :size="15" aria-hidden="true" />
          {{ artifactOperation === "reparse" ? "提交中" : "重新解析" }}
        </button>
        <button
          v-if="canDeleteArtifact"
          class="command-button command-button-danger"
          type="button"
          :disabled="operationBusy"
          :aria-label="`删除文件 ${artifact.original_filename}`"
          @click="deleteArtifact"
        >
          <LoaderCircle v-if="artifactOperation === 'delete'" class="is-spinning" :size="15" aria-hidden="true" />
          <Trash2 v-else :size="15" aria-hidden="true" />
          {{ artifactOperation === "delete" ? "删除中" : "删除文件" }}
        </button>
      </div>
    </header>

    <section v-if="artifactQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block"></div><div class="loading-block is-wide"></div></section>
    <section v-else-if="detailError" class="entity-detail-state is-error" role="alert"><strong>原始文件无法读取</strong><p>{{ detailError }}</p></section>
    <section v-else-if="!artifact" class="entity-detail-state"><strong>原始文件不存在或当前账户不可见</strong></section>
    <template v-else>
      <p v-if="operationResult" class="upload-result artifact-detail-operation-message" role="status">{{ operationResult }}</p>
      <p v-if="operationError" class="inline-error artifact-detail-operation-message" role="alert">{{ operationError }}</p>
      <section class="artifact-detail-overview" aria-label="原始文件元数据">
        <div class="artifact-detail-identity">
          <div class="artifact-detail-file-mark" aria-hidden="true"><FileText :size="24" /></div>
          <div>
            <strong>{{ artifact.original_filename }}</strong>
            <span>{{ effectiveMediaType }} · {{ formatBytes(artifact.size_bytes) }}</span>
            <code :title="artifact.id">{{ artifact.id }}</code>
          </div>
        </div>
        <dl class="detail-list artifact-detail-facts">
          <div><dt>文件类型</dt><dd>{{ labelFor(artifact.artifact_kind) }}</dd></div>
          <div><dt>可见性</dt><dd>{{ artifact.visibility === "public" ? "公开" : "项目内" }}</dd></div>
          <div><dt>存储状态</dt><dd><span class="status-dot" :class="statusTone(artifact.storage_status)">{{ labelFor(artifact.storage_status) }}</span></dd></div>
          <div><dt>解析状态</dt><dd><ArtifactIngestionStatus :status="artifact.ingestion_status" :error-message="artifact.ingestion_error_message" /></dd></div>
          <div><dt title="MolOP 报告的文件级计算用时">文件总耗时</dt><dd>{{ formatDurationSeconds(artifact.running_time_seconds) }}</dd></div>
          <div><dt>计算帧</dt><dd>{{ artifact.source_frame_count ?? "—" }}</dd></div>
          <div><dt>过渡态帧</dt><dd>{{ artifact.transition_state_frame_count ?? "—" }}</dd></div>
          <div><dt>SHA-256</dt><dd><code :title="artifact.content_sha256">{{ shortId(artifact.content_sha256) }}</code></dd></div>
          <div><dt>项目 ID</dt><dd><code>{{ artifact.project_id }}</code></dd></div>
          <div><dt>验证时间</dt><dd>{{ artifact.storage_verified_at ? new Date(artifact.storage_verified_at).toLocaleString("zh-CN") : "—" }}</dd></div>
        </dl>
      </section>

      <section class="artifact-detail-section artifact-notes-section" aria-labelledby="artifact-notes-title">
        <header class="artifact-detail-section-header">
          <div><span class="eyebrow">User metadata</span><h2 id="artifact-notes-title">文件备注</h2></div>
          <span>不会修改原始文件</span>
        </header>
        <p class="artifact-notes-help">记录文件内容之外的实验批次、来源说明或后续处理信息。</p>
        <textarea
          v-model="noteDraft"
          class="artifact-notes-input"
          maxlength="16384"
          rows="4"
          placeholder="填写文件备注（可留空以清除）"
          aria-label="文件备注"
          :readonly="!canManageNotes"
        ></textarea>
        <div class="artifact-notes-actions">
          <button v-if="canManageNotes" class="command-button" type="button" :disabled="noteSaving" @click="saveArtifactNotes">
            <LoaderCircle v-if="noteSaving" class="is-spinning" :size="15" aria-hidden="true" />
            <Save v-else :size="15" aria-hidden="true" />
            保存备注
          </button>
          <span v-else class="artifact-notes-permission">仅项目管理员可编辑文件备注</span>
          <span v-if="noteSaved" class="upload-result" role="status">{{ noteSaved }}</span>
          <span v-if="noteError" class="inline-error" role="alert">{{ noteError }}</span>
        </div>
      </section>

      <section v-if="fileComments.length" class="artifact-detail-section parsed-comments-section" aria-labelledby="artifact-comments-title">
        <header class="artifact-detail-section-header">
          <div><span class="eyebrow">MolOP comments</span><h2 id="artifact-comments-title">文件 comments</h2></div>
          <span>{{ fileComments.length }} 条 · revision {{ latestParseRevision?.revision_number }}</span>
        </header>
        <ol class="parsed-comments-list">
          <li v-for="(comment, index) in fileComments" :key="index" class="parsed-comment">
            <div class="parsed-comment-meta">
              <span class="role-pill">{{ comment.kind }}</span>
              <span v-if="comment.source_format">{{ comment.source_format }}</span>
              <span v-if="comment.source_line !== null">line {{ comment.source_line + 1 }}</span>
            </div>
            <p>{{ comment.text }}</p>
            <code v-if="comment.raw && comment.raw !== comment.text">{{ comment.raw }}</code>
          </li>
        </ol>
      </section>

      <section v-if="artifact.ingestion_status === 'filtered'" class="artifact-detail-section" aria-label="解析结果">
        <div class="artifact-detail-empty">文件已保存，但其中没有可识别的计算帧，因此未进入计算数据目录。</div>
      </section>
      <section v-else-if="artifact.ingestion_status === 'failed'" class="artifact-detail-section" aria-label="解析错误">
        <div class="artifact-detail-empty is-error" role="alert">{{ artifact.ingestion_error_message ?? "文件解析失败" }}</div>
      </section>

      <section class="artifact-detail-section" aria-labelledby="artifact-content-title">
        <header class="artifact-detail-section-header">
          <div><span class="eyebrow">Content</span><h2 id="artifact-content-title">文件内容</h2></div>
          <span v-if="previewQuery.data.value">显示 {{ formatBytes(previewQuery.data.value.preview_bytes) }}</span>
        </header>
        <div v-if="artifact.storage_status !== 'available'" class="artifact-detail-empty">文件当前不可用，暂时无法预览或下载。</div>
        <div v-else-if="previewQuery.isLoading.value" class="entity-detail-loading"><div class="loading-block is-wide"></div></div>
        <div v-else-if="previewError" class="artifact-detail-empty is-error" role="alert">{{ previewError }}</div>
        <template v-else-if="previewQuery.data.value">
          <p v-if="previewQuery.data.value.truncated" class="preview-truncated">当前显示前 {{ formatBytes(previewQuery.data.value.preview_bytes) }}，下载可查看完整文件。</p>
          <pre class="artifact-preview-text artifact-detail-preview"><code>{{ previewQuery.data.value.preview_text }}</code></pre>
        </template>
      </section>

      <section class="artifact-detail-section" aria-labelledby="artifact-frames-title">
        <header class="artifact-detail-section-header">
          <div><span class="eyebrow">CalculationFrame</span><h2 id="artifact-frames-title">关联计算帧</h2></div>
          <span>{{ framesQuery.data.value?.page.total ?? "—" }} 帧</span>
        </header>
        <div class="artifact-frames-content">
          <section class="artifact-frame-list-pane">
            <header>
              <div><span class="eyebrow">CalculationFrame</span><strong>文件中的全部计算帧</strong></div>
              <span>{{ framesQuery.data.value?.items.length ?? 0 }} 帧</span>
            </header>
            <div class="artifact-frame-list-scroll">
              <CalculationFrameList :frames="framesQuery.data.value?.items ?? []" :loading="framesQuery.isLoading.value" :error="framesError" @open="selectedFrameId = $event" />
            </div>
          </section>
          <ChemDoodleFrameMovie3D
            :frames="framesQuery.data.value?.items ?? []"
            :project-id="artifact.project_id"
            title="优化动画"
            canvas-label="原始文件优化动画"
          />
        </div>
      </section>
    </template>

    <FrameDrawer
      :open="selectedFrameId !== null"
      :loading="frameQuery.isLoading.value"
      :error="frameError"
      :frame="frameQuery.data.value ?? null"
      :project-id="artifact?.project_id ?? currentProjectId ?? undefined"
      @close="selectedFrameId = null"
    />
  </main>
</template>
