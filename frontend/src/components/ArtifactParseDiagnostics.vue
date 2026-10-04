<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";
import { api } from "@/api";
import { collectParseDiagnostics, diagnosticStage } from "@/parseDiagnostics";
import type { ArtifactSummary, CalculationFrameSummary, ParseRevisionSummary, TransitionStateInferenceSummary } from "@/types";

const props = defineProps<{
  artifact: ArtifactSummary;
  revision: ParseRevisionSummary | null;
  frames: CalculationFrameSummary[];
  loading: boolean;
  framesLoading: boolean;
  framesError: string;
  error: string;
}>();
const emit = defineEmits<{ openFrame: [id: string]; retry: [] }>();
const processing = computed(() => ["pending", "processing"].includes(props.artifact.ingestion_status ?? ""));
const inferenceQuery = useQuery({
  queryKey: computed(() => ["artifact-diagnostics-inferences", props.artifact.project_id, props.revision?.id]),
  enabled: computed(() => Boolean(props.revision) && !processing.value),
  queryFn: async ({ signal }) => {
    const items: TransitionStateInferenceSummary[] = [];
    for (let offset = 0; ; offset += 200) {
      const page = await api.transitionStateInferences({ projectId: props.artifact.project_id,
        parseRevisionId: props.revision!.id, status: "failed", limit: 200, offset }, signal);
      items.push(...page.items);
      if (!page.items.length || offset + page.items.length >= page.page.total) return items;
    }
  },
  staleTime: 30_000,
});
const diagnostics = computed(() => collectParseDiagnostics(props.artifact, props.revision, inferenceQuery.data.value ?? [], props.frames));
const queryError = computed(() => props.error || props.framesError || (inferenceQuery.error.value instanceof Error ? inferenceQuery.error.value.message : ""));
const search = ref("");
const page = ref(1);
watch([search, () => props.revision?.id, () => props.artifact.id], () => { page.value = 1; });
const filtered = computed(() => diagnostics.value.items.filter((item) =>
  [item.message, item.code, diagnosticStage(item.stage), item.frameIndex === null ? "文件级" : `第 ${item.frameIndex + 1} 帧`]
    .join(" ").toLowerCase().includes(search.value.trim().toLowerCase()),
));
const pageCount = computed(() => Math.max(1, Math.ceil(filtered.value.length / 20)));
const currentPage = computed(() => Math.min(page.value, pageCount.value));
const visible = computed(() => filtered.value.slice((currentPage.value - 1) * 20, currentPage.value * 20));
const affectedFrames = computed(() => new Set(diagnostics.value.items.flatMap((item) => item.frameIndex === null ? [] : [item.frameIndex])).size);
const loading = computed(() => props.loading || inferenceQuery.isFetching.value);
function retry() { emit("retry"); if (props.revision) void inferenceQuery.refetch(); }
</script>

<template>
  <section id="parse-diagnostics" class="artifact-detail-section parse-diagnostics" aria-labelledby="parse-diagnostics-title">
    <header class="artifact-detail-section-header">
      <div><h2 id="parse-diagnostics-title">解析诊断</h2><p>查看失败阶段、具体原因及对应的源文件帧。</p></div>
      <span v-if="revision && !processing">解析版本 {{ revision.revision_number }}</span>
    </header>
    <p v-if="processing" role="status">文件正在等待解析或解析中，完成后自动更新诊断。</p>
    <template v-else>
      <p v-if="loading" role="status">正在读取解析诊断…</p>
      <div v-if="queryError" class="inline-error" role="alert">诊断未完整加载：{{ queryError }} <button type="button" class="command-button is-quiet" @click="retry">重试</button></div>
      <p v-if="diagnostics.unreadable" class="inline-error" role="alert">部分诊断数据格式无法读取，不能据此判断文件没有问题。</p>
      <template v-if="diagnostics.items.length">
        <div class="diagnostic-toolbar">
          <span>{{ diagnostics.items.length }} 条诊断 · 涉及 {{ affectedFrames }} 帧 <small>（帧号从 1 开始）</small></span>
          <input v-model="search" type="search" placeholder="搜索帧号、原因或错误码" aria-label="搜索解析诊断" />
        </div>
        <ol class="diagnostic-list">
          <li v-for="(item, index) in visible" :key="`${currentPage}-${index}`" class="diagnostic-item">
            <div class="diagnostic-meta">
              <span class="diagnostic-severity" :class="item.severity">{{ { error: "错误", warning: "警告", info: "信息" }[item.severity] }}</span>
              <strong>{{ item.frameIndex === null ? "文件级诊断（未提供帧号）" : `源文件第 ${item.frameIndex + 1} 帧` }}</strong>
              <span v-if="item.segmentIndex !== null">第 {{ item.segmentIndex + 1 }} 段</span>
              <span>{{ diagnosticStage(item.stage) }}</span>
              <span v-if="item.sourceLine !== null">源文件第 {{ item.sourceLine + 1 }} 行</span>
            </div>
            <p class="diagnostic-message">{{ item.message }}</p>
            <code class="diagnostic-code">{{ item.code }}</code>
            <div class="diagnostic-actions">
              <button v-if="item.frameId" class="command-button is-quiet" type="button" @click="emit('openFrame', item.frameId)">{{ item.frameIndex === null ? '查看对应帧' : `查看第 ${item.frameIndex + 1} 帧` }}</button>
              <span v-else-if="item.frameIndex !== null" class="diagnostic-unavailable">{{ framesLoading ? '正在查找对应计算帧…' : framesError ? '计算帧列表未能读取，请重试。' : '该源帧未生成可查看的计算记录，请根据帧号检查原文件。' }}</span>
              <a href="#artifact-content-title">查看原文件</a>
            </div>
            <details><summary>原始诊断与附加信息</summary><pre>{{ JSON.stringify(item.evidence, null, 2) }}</pre></details>
          </li>
        </ol>
        <p v-if="!filtered.length">没有匹配的诊断。</p>
        <nav v-if="pageCount > 1" class="diagnostic-pagination" aria-label="诊断分页">
          <button class="command-button is-quiet" type="button" :disabled="currentPage <= 1" @click="page = currentPage - 1">上一页</button>
          <span>{{ currentPage }} / {{ pageCount }} · {{ filtered.length }} 条</span>
          <button class="command-button is-quiet" type="button" :disabled="currentPage >= pageCount" @click="page = currentPage + 1">下一页</button>
        </nav>
      </template>
      <p v-else-if="!loading && !queryError && !diagnostics.unreadable">{{ ['failed', 'partial'].includes(artifact.ingestion_status ?? '') ? '本次解析未记录可定位到具体帧的诊断信息。' : '本次解析没有记录错误或警告。' }}</p>
    </template>
  </section>
</template>

<style scoped>
.parse-diagnostics { scroll-margin-top: 24px; }
.parse-diagnostics header p { margin: 6px 0 0; color: var(--muted, #66736c); font-size: 13px; }
.diagnostic-toolbar, .diagnostic-meta, .diagnostic-actions, .diagnostic-pagination { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.diagnostic-toolbar { justify-content: space-between; margin: 18px 0; }
.diagnostic-toolbar input { min-width: 0; width: 270px; max-width: 100%; padding: 9px 12px; border: 1px solid #ccd6ce; border-radius: 8px; background: transparent; color: inherit; }
.diagnostic-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 12px; }
.diagnostic-item { min-width: 0; padding: 16px; border: 1px solid #dce3dd; border-radius: 10px; background: #fafcf9; }
.diagnostic-meta { font-size: 13px; color: #56655c; }
.diagnostic-meta strong { color: #243e2e; }
.diagnostic-severity { border-radius: 5px; padding: 3px 7px; background: #eaf0ec; }
.diagnostic-severity.error { color: #9a3029; background: #fbe9e6; }
.diagnostic-severity.warning { color: #805615; background: #fff1d6; }
.diagnostic-message { white-space: pre-wrap; overflow-wrap: anywhere; margin: 12px 0 8px; }
.diagnostic-code { font-size: 12px; overflow-wrap: anywhere; }
.diagnostic-actions { margin: 12px 0; font-size: 13px; }
.diagnostic-unavailable { color: #69746c; }
.diagnostic-item summary { cursor: pointer; font-size: 13px; color: #526359; }
.diagnostic-item pre { max-height: 320px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.diagnostic-pagination { justify-content: flex-end; margin-top: 16px; }
@media (max-width: 600px) { .diagnostic-toolbar input { width: 100%; } .diagnostic-item { padding: 12px; } }
</style>
