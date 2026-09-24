<script setup lang="ts">
import { Download, RefreshCw } from "@lucide/vue";
import { computed, ref } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { api, apiUrl } from "@/api";
import { useProjectContext } from "@/composables/useProjectContext";
import type { UnitsTsDatasetExportCreated, UnitsTsDatasetExportStatus } from "@/types";

const projectContext = useProjectContext();
const currentProjectId = projectContext.currentProjectId;
const currentProject = projectContext.currentProject;
const canDownloadProjectData = computed(() => projectContext.can("artifact:download"));
const geometryJsonlUrl = computed(() => {
  const projectId = currentProjectId.value;
  if (!projectId) return "";
  const query = new URLSearchParams({ project_id: projectId });
  return apiUrl(`/api/mapped-reactions/transition-state-geometries/export.jsonl?${query}`);
});
const unitsJsonlUrl = computed(() => {
  const projectId = currentProjectId.value;
  if (!projectId) return "";
  const query = new URLSearchParams({ project_id: projectId });
  return apiUrl(`/api/units-ts-datasets/export.jsonl?${query}`);
});
const csvDownloading = ref(false);
const csvError = ref("");
const reactionSmarts = ref("");
const requireActivationEnergy = ref(true);
const requireReactionEnergy = ref(true);
const unitsStartingProjectIds = ref<Set<string>>(new Set());
const unitsCreateErrorsByProject = ref<Record<string, string>>({});
const unitsJobsByProject = ref<Record<string, UnitsTsDatasetExportCreated>>({});
const unitsJob = computed(() => {
  const projectId = currentProjectId.value;
  return projectId ? unitsJobsByProject.value[projectId] ?? null : null;
});
const unitsStarting = computed(() => (
  currentProjectId.value !== null
  && unitsStartingProjectIds.value.has(currentProjectId.value)
));
const unitsCreateError = computed(() => (
  currentProjectId.value
    ? unitsCreateErrorsByProject.value[currentProjectId.value] ?? ""
    : ""
));
const unitsJobProjectName = computed(() => (
  projectContext.projects.value.find((project) => project.project_id === unitsJob.value?.project_id)?.project_name
  ?? unitsJob.value?.project_id
  ?? "—"
));

const unitsStatusQuery = useQuery<UnitsTsDatasetExportStatus>({
  queryKey: computed(() => [
    "units-ts-dataset-export",
    currentProjectId.value,
    unitsJob.value?.job_id ?? null,
  ]),
  queryFn: ({ signal }) => {
    const jobId = unitsJob.value?.job_id;
    if (!jobId) throw new Error("没有活动的 UniTS 导出任务。");
    return api.unitsTsDatasetExportStatus(jobId, signal);
  },
  enabled: computed(() => unitsJob.value !== null),
  refetchInterval: (query) => {
    const status = query.state.data?.status ?? unitsJob.value?.status;
    return status === "pending" || status === "processing" ? 2_000 : false;
  },
});

const unitsStatus = computed(() => {
  const job = unitsJob.value;
  if (!job) return null;
  const status = unitsStatusQuery.data.value;
  return status?.job_id === job.job_id ? status : job;
});
const unitsStatusError = computed(() => (
  unitsJob.value ? unitsStatusQuery.error.value : null
));
const unitsStatusLabel = computed(() => {
  switch (unitsStatus.value?.status) {
    case "pending": return "排队中";
    case "processing": return "生成中";
    case "completed": return "已完成";
    case "failed": return "失败";
    case "expired": return "已过期";
    default: return "尚未开始";
  }
});
const unitsStatusClass = computed(() => `is-${unitsStatus.value?.status ?? "idle"}`);
const skipReasons = computed(() => Object.entries(unitsStatus.value?.skip_reasons ?? {}));
const unitsCanStart = computed(() => (
  currentProjectId.value !== null
  && canDownloadProjectData.value
  && !unitsStarting.value
  && unitsStatus.value?.status !== "pending"
  && unitsStatus.value?.status !== "processing"
));

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

async function downloadEnergyTimeCsv(): Promise<void> {
  const projectId = currentProjectId.value;
  if (!projectId || csvDownloading.value) return;
  const projectSlug = currentProject.value?.project_slug ?? projectId.slice(0, 8);
  csvDownloading.value = true;
  csvError.value = "";
  try {
    const blob = await api.mappedReactionEnergyTimeExport({
      projectId,
      reactionSmarts: reactionSmarts.value,
      hasActivationGibbsFreeEnergy: requireActivationEnergy.value,
      hasReactionGibbsFreeEnergy: requireReactionEnergy.value,
    });
    saveBlob(blob, `mapped-reaction-energy-time-${projectSlug}.csv`);
  } catch (error) {
    csvError.value = error instanceof Error ? error.message : "CSV 导出失败。";
  } finally {
    csvDownloading.value = false;
  }
}

async function startUnitsExport(): Promise<void> {
  const projectId = currentProjectId.value;
  if (
    !projectId
    || !canDownloadProjectData.value
    || unitsStartingProjectIds.value.has(projectId)
  ) return;
  unitsStartingProjectIds.value = new Set(unitsStartingProjectIds.value).add(projectId);
  unitsCreateErrorsByProject.value = {
    ...unitsCreateErrorsByProject.value,
    [projectId]: "",
  };
  try {
    const job = await api.createUnitsTsDatasetExport(projectId);
    unitsJobsByProject.value = {
      ...unitsJobsByProject.value,
      [projectId]: job,
    };
  } catch (error) {
    unitsCreateErrorsByProject.value = {
      ...unitsCreateErrorsByProject.value,
      [projectId]: error instanceof Error ? error.message : "UniTS 数据集任务创建失败。",
    };
  } finally {
    const starting = new Set(unitsStartingProjectIds.value);
    starting.delete(projectId);
    unitsStartingProjectIds.value = starting;
  }
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
</script>

<template>
  <main class="exports-page" aria-labelledby="exports-page-title">
    <header class="page-heading exports-heading">
      <span class="eyebrow">DATA EXPORT</span>
      <h1 id="exports-page-title">数据导出</h1>
      <p>按当前项目和所选数据库条件导出映射反应数据，支持 CSV、UniTS NPY 和逐条流式 JSONL。</p>
    </header>

    <section class="export-card-grid" aria-label="可用数据导出">
      <article class="export-card" aria-labelledby="energy-export-title">
        <header class="export-card-heading">
          <div>
            <span class="eyebrow">MAPPED REACTION THERMODYNAMICS</span>
            <h2 id="energy-export-title">映射反应能量与耗时</h2>
          </div>
          <span class="export-format">CSV</span>
        </header>
        <p class="export-card-description">从当前项目导出数据库中的映射反应自由能、活化自由能和各计算阶段耗时。项目和筛选条件均可按需调整。</p>

        <dl class="export-spec-list">
          <div><dt>当前项目</dt><dd>{{ currentProject?.project_name ?? "未选择项目" }}<code v-if="currentProjectId">{{ currentProjectId }}</code></dd></div>
          <div><dt>数据条件</dt><dd>由数据库筛选清洗成功的记录</dd></div>
          <div><dt>单位</dt><dd>能量 kcal/mol · 耗时秒</dd></div>
        </dl>

        <label class="export-input-label">
          <span>reaction_smarts（可选）</span>
          <textarea v-model="reactionSmarts" rows="3" placeholder="留空时不按 reaction_smarts 筛选，由数据库返回当前项目中符合能量条件的记录。" />
        </label>
        <p class="export-input-help">填写后按 reaction_smarts 数据库筛选语法传入；页面不会按 reaction_class 重新分类数据。</p>
        <div class="export-option-list" aria-label="能量字段条件">
          <label><input v-model="requireActivationEnergy" type="checkbox" />仅包含有活化 Gibbs 自由能的记录</label>
          <label><input v-model="requireReactionEnergy" type="checkbox" />仅包含有反应 Gibbs 自由能的记录</label>
        </div>

        <p v-if="!currentProjectId" class="export-message is-muted">请先在页面顶部选择项目。</p>
        <div v-if="csvError" class="export-message is-error" role="alert">{{ csvError }}</div>
        <button class="command-button export-action" type="button" :disabled="csvDownloading || !currentProjectId" @click="downloadEnergyTimeCsv">
          <Download :size="15" aria-hidden="true" />
          {{ csvDownloading ? "正在导出…" : "下载 CSV" }}
        </button>
      </article>

      <article class="export-card" aria-labelledby="units-export-title">
        <header class="export-card-heading">
          <div>
            <span class="eyebrow">UNITS · TRANSITION STATE</span>
            <h2 id="units-export-title">过渡态几何数据集</h2>
          </div>
          <span class="export-format">NPY</span>
        </header>
        <p class="export-card-description">后台生成 UniTS MultiDatasetV2 六段记录格式的 NPY 文件。下载后需在 UniTS 环境运行仓库内的适配脚本，将图特征数组转为 PyTorch 张量后交给加载器读取。</p>

        <dl class="export-spec-list">
          <div><dt>当前项目</dt><dd>{{ currentProject?.project_name ?? "未选择项目" }}<code v-if="currentProjectId">{{ currentProjectId }}</code></dd></div>
          <div><dt>访问要求</dt><dd>当前项目需要 artifact:download 权限</dd></div>
          <div><dt>文件格式</dt><dd>UniTS 特征顺序的 NumPy NPY 数据集</dd></div>
        </dl>

        <p v-if="!currentProjectId" class="export-message is-muted">请先在页面顶部选择项目。</p>
        <p v-else-if="!canDownloadProjectData" class="export-message is-muted">你没有当前项目的数据下载权限。</p>
        <div v-if="unitsCreateError" class="export-message is-error" role="alert">{{ unitsCreateError }}</div>
        <div v-if="unitsStatusError" class="export-message is-error" role="alert">
          状态查询失败：{{ unitsStatusError instanceof Error ? unitsStatusError.message : "请求失败" }}
          <button class="text-button" type="button" @click="unitsStatusQuery.refetch()">重试查询</button>
        </div>

        <div v-if="unitsStatus" class="export-job-state" aria-live="polite">
          <div class="export-job-heading">
            <span>任务状态</span>
            <span class="export-status" :class="unitsStatusClass">{{ unitsStatusLabel }}</span>
          </div>
          <dl class="export-job-metrics">
            <div><dt>任务项目</dt><dd>{{ unitsJobProjectName }}</dd></div>
            <div><dt>样本数</dt><dd>{{ unitsStatus.sample_count }}</dd></div>
            <div><dt>跳过数</dt><dd>{{ unitsStatus.skipped_count }}</dd></div>
            <div><dt>提交时间</dt><dd>{{ formatDate(unitsStatus.requested_at) }}</dd></div>
          </dl>
          <p v-if="unitsStatus.status === 'failed'" class="export-message is-error">{{ unitsStatus.error_message || "导出任务失败。" }}</p>
          <div v-if="skipReasons.length" class="export-skip-reasons">
            <strong>跳过原因</strong>
            <ul><li v-for="[reason, count] in skipReasons" :key="reason"><code>{{ reason }}</code><span>{{ count }}</span></li></ul>
          </div>
          <a
            v-if="unitsStatus.status === 'completed' && unitsJob?.download_url"
            class="command-button export-action"
            :href="unitsJob.download_url"
          >
            <Download :size="15" aria-hidden="true" />下载 NPY
          </a>
          <p v-if="unitsStatus.status === 'completed'" class="export-expiry">下载链接有效至 {{ formatDate(unitsStatus.expires_at) }}。</p>
        </div>

        <a
          v-if="currentProjectId && canDownloadProjectData"
          class="command-button export-action"
          :href="unitsJsonlUrl"
        >
          <Download :size="15" aria-hidden="true" />逐条下载 UniTS JSONL
        </a>
        <p class="export-input-help">JSONL 会按行输出与 NPY 相同的 UniTS 特征；可边下载边处理。NPY 保留给要求 NumPy object array 的现有流程。</p>

        <button class="command-button export-action" type="button" :disabled="!unitsCanStart" @click="startUnitsExport">
          <RefreshCw v-if="unitsStarting || unitsStatus?.status === 'pending' || unitsStatus?.status === 'processing'" class="is-spinning" :size="15" aria-hidden="true" />
          <Download v-else :size="15" aria-hidden="true" />
          {{ unitsStarting ? "正在提交…" : unitsStatus?.status === "pending" || unitsStatus?.status === "processing" ? "后台生成中…" : "生成 NPY 数据集" }}
        </button>
      </article>

      <article class="export-card" aria-labelledby="geometry-jsonl-export-title">
        <header class="export-card-heading">
          <div>
            <span class="eyebrow">MAPPED REACTION · TRANSITION STATE</span>
            <h2 id="geometry-jsonl-export-title">映射反应与 TS 几何</h2>
          </div>
          <span class="export-format">JSONL</span>
        </header>
        <p class="export-card-description">按数据库分页读取并逐条流式输出，不会先生成完整数据集文件。每行以 mapped reaction SMILES 为 key，包含坐标几何和可由 RDKit 读取的 Mol block。</p>

        <dl class="export-spec-list">
          <div><dt>当前项目</dt><dd>{{ currentProject?.project_name ?? "未选择项目" }}<code v-if="currentProjectId">{{ currentProjectId }}</code></dd></div>
          <div><dt>数据条件</dt><dd>仅包含已验证原子映射的过渡态几何</dd></div>
          <div><dt>坐标单位</dt><dd>angstrom · 按几何原子顺序输出</dd></div>
        </dl>

        <p v-if="!currentProjectId" class="export-message is-muted">请先在页面顶部选择项目。</p>
        <p v-else-if="!canDownloadProjectData" class="export-message is-muted">你没有当前项目的数据下载权限。</p>
        <a
          v-else
          class="command-button export-action"
          :href="geometryJsonlUrl"
        >
          <Download :size="15" aria-hidden="true" />下载 JSONL 流
        </a>
      </article>
    </section>
  </main>
</template>
