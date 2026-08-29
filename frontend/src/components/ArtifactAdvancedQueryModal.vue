<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { CircleHelp, Plus, Search, Trash2, X } from "@lucide/vue";
import { RouterLink } from "vue-router";

import QueryValidationIndicator from "@/components/QueryValidationIndicator.vue";
import {
  artifactKindOptions,
  artifactQueryFieldOption,
  artifactQueryFieldOptions,
  emptyArtifactFilters,
  ingestionStatusOptions,
  storageStatusOptions,
  type ArtifactFilterValues,
  type ArtifactQueryCondition,
  type ArtifactQueryField,
} from "@/artifactQuery";

const props = defineProps<{
  open: boolean;
  initialFilters: ArtifactFilterValues;
}>();

const emit = defineEmits<{
  close: [];
  apply: [filters: ArtifactFilterValues];
}>();

type QueryValidationStatus = "idle" | "pending" | "valid" | "invalid";
interface ConditionValidationState {
  status: QueryValidationStatus;
  message: string;
}

const dialog = ref<HTMLElement | null>(null);
const conditions = ref<ArtifactQueryCondition[]>([]);
const nextConditionId = ref(1);
const validationError = ref("");
const validationStates = ref<Record<number, ConditionValidationState>>({});
const validationTimers = new Map<number, number>();

function clearConditionValidation(id: number): void {
  const timer = validationTimers.get(id);
  if (timer !== undefined) window.clearTimeout(timer);
  validationTimers.delete(id);
  const next = { ...validationStates.value };
  delete next[id];
  validationStates.value = next;
}

function newCondition(field: ArtifactQueryField = "artifact_id", value = ""): ArtifactQueryCondition {
  return { id: nextConditionId.value++, field, value };
}

function reset(): void {
  for (const condition of conditions.value) clearConditionValidation(condition.id);
  nextConditionId.value = 1;
  const initial = props.initialFilters;
  const values: Array<[ArtifactQueryField, string | null]> = [
    ["artifact_id", initial.artifactId],
    ["content_sha256", initial.contentSha256],
    ["original_filename_contains", initial.originalFilenameContains],
    ["artifact_kind", initial.artifactKind],
    ["storage_status", initial.storageStatus],
    ["ingestion_status", initial.ingestionStatus],
  ];
  conditions.value = values
    .filter(([, value]) => Boolean(value))
    .map(([field, value]) => newCondition(field, value ?? ""));
  if (!conditions.value.length) conditions.value = [newCondition()];
  validationError.value = "";
}

function addCondition(): void {
  const used = new Set(conditions.value.map((condition) => condition.field));
  const next = artifactQueryFieldOptions.find((option) => !used.has(option.value));
  if (!next) return;
  conditions.value.push(newCondition(next.value));
  validationError.value = "";
}

function removeCondition(id: number): void {
  clearConditionValidation(id);
  conditions.value = conditions.value.filter((condition) => condition.id !== id);
  if (!conditions.value.length) conditions.value = [newCondition()];
  validationError.value = "";
}

function changeField(condition: ArtifactQueryCondition): void {
  clearConditionValidation(condition.id);
  condition.value = "";
  validationError.value = "";
}

function fieldKind(condition: ArtifactQueryCondition): string {
  return artifactQueryFieldOption(condition.field).kind;
}

function validationFor(condition: ArtifactQueryCondition): ConditionValidationState {
  const value = condition.value.trim();
  if (!value || fieldKind(condition) !== "identifier") return { status: "idle", message: "" };
  if (condition.field === "artifact_id") {
    const valid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
    return valid
      ? { status: "valid", message: "文件 ID 格式有效" }
      : { status: "invalid", message: "文件 ID 必须是 UUID 格式" };
  }
  const valid = /^[0-9a-f]{64}$/i.test(value);
  return valid
    ? { status: "valid", message: "SHA-256 格式有效" }
    : { status: "invalid", message: "SHA-256 必须是 64 位十六进制字符串" };
}

function scheduleValidation(condition: ArtifactQueryCondition): void {
  const timer = validationTimers.get(condition.id);
  if (timer !== undefined) window.clearTimeout(timer);
  validationTimers.delete(condition.id);
  const immediate = validationFor(condition);
  if (immediate.status === "idle") {
    const next = { ...validationStates.value, [condition.id]: immediate };
    validationStates.value = next;
    return;
  }
  validationStates.value = { ...validationStates.value, [condition.id]: { status: "pending", message: "等待校验…" } };
  validationTimers.set(condition.id, window.setTimeout(() => {
    validationTimers.delete(condition.id);
    validationStates.value = { ...validationStates.value, [condition.id]: validationFor(condition) };
  }, 180));
}

function buildFilters(): ArtifactFilterValues | null {
  const filters = emptyArtifactFilters();
  for (const condition of conditions.value) {
    const value = condition.value.trim();
    if (!value) {
      validationError.value = `请填写“${artifactQueryFieldOption(condition.field).label}”`;
      return null;
    }
    const validation = validationFor(condition);
    validationStates.value = { ...validationStates.value, [condition.id]: validation };
    if (validation.status === "invalid") {
      validationError.value = validation.message;
      return null;
    }
    if (condition.field === "artifact_id") filters.artifactId = value;
    if (condition.field === "content_sha256") filters.contentSha256 = value;
    if (condition.field === "original_filename_contains") filters.originalFilenameContains = value;
    if (condition.field === "artifact_kind") filters.artifactKind = value;
    if (condition.field === "storage_status") filters.storageStatus = value;
    if (condition.field === "ingestion_status") filters.ingestionStatus = value;
  }
  validationError.value = "";
  return filters;
}

function apply(): void {
  const filters = buildFilters();
  if (filters) emit("apply", filters);
}

function close(): void { emit("close"); }

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && props.open) close();
}

watch(() => props.open, (open) => {
  document.body.classList.toggle("drawer-open", open);
  if (open) {
    reset();
    void nextTick(() => dialog.value?.querySelector<HTMLInputElement>("input, select")?.focus());
  }
});
watch(conditions, (nextConditions) => {
  for (const condition of nextConditions) scheduleValidation(condition);
}, { deep: true });
onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.classList.remove("drawer-open");
  for (const condition of conditions.value) clearConditionValidation(condition.id);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="advanced-query-fade">
      <button v-if="open" class="advanced-query-backdrop" type="button" aria-label="关闭原始文件高级筛选" @click="close"></button>
    </Transition>
    <Transition name="advanced-query-panel">
      <section
        v-if="open"
        ref="dialog"
        class="advanced-query-dialog artifact-advanced-query-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="artifact-advanced-query-title"
      >
        <header class="advanced-query-header">
          <div>
            <span class="eyebrow">Artifact query builder</span>
            <h2 id="artifact-advanced-query-title">高级筛选</h2>
          </div>
          <div class="advanced-query-header-actions">
            <RouterLink class="icon-button" :to="{ name: 'artifact-query-help' }" title="查看原始文件查询帮助" aria-label="查看原始文件查询帮助">
              <CircleHelp :size="17" aria-hidden="true" />
            </RouterLink>
            <button class="icon-button" type="button" title="关闭高级筛选" aria-label="关闭高级筛选" @click="close">
              <X :size="18" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div class="advanced-query-content">
          <div class="advanced-query-toolbar">
            <div class="advanced-query-logic"><span>条件组合</span><strong>全部满足（AND）</strong></div>
            <button class="command-button command-button-muted" type="button" :disabled="conditions.length >= artifactQueryFieldOptions.length" @click="addCondition">
              <Plus :size="15" aria-hidden="true" />添加条件
            </button>
          </div>
          <p class="advanced-query-context">多个条件会按 Artifact API 的 AND 语义同时筛选；普通文件名支持包含匹配。</p>

          <div class="advanced-query-conditions">
            <div v-for="condition in conditions" :key="condition.id" class="advanced-query-row">
              <div class="advanced-query-row-header">
                <span class="advanced-query-index">{{ String(condition.id).padStart(2, "0") }}</span>
                <label :for="`artifact-advanced-query-field-${condition.id}`">字段</label>
                <select :id="`artifact-advanced-query-field-${condition.id}`" v-model="condition.field" @change="changeField(condition)">
                  <option v-for="option in artifactQueryFieldOptions" :key="option.value" :value="option.value" :disabled="option.value !== condition.field && conditions.some((item) => item.field === option.value)">{{ option.label }}</option>
                </select>
                <button class="icon-button advanced-query-remove" type="button" title="删除条件" aria-label="删除条件" :disabled="conditions.length === 1" @click="removeCondition(condition.id)">
                  <Trash2 :size="15" aria-hidden="true" />
                </button>
              </div>
              <div class="advanced-query-value" :data-validation-status="validationStates[condition.id]?.status ?? 'idle'">
                <div v-if="fieldKind(condition) === 'identifier'" class="query-input-with-validation">
                  <input v-model="condition.value" type="text" :placeholder="condition.field === 'artifact_id' ? 'UUID，例如 00000000-0000-7000-8000-000000000001' : '64 位十六进制 SHA-256'" :aria-label="`${artifactQueryFieldOption(condition.field).label}条件值`" :aria-invalid="validationStates[condition.id]?.status === 'invalid'">
                  <QueryValidationIndicator :status="validationStates[condition.id]?.status ?? 'idle'" :message="validationStates[condition.id]?.message ?? ''" />
                </div>
                <select v-else-if="condition.field === 'artifact_kind'" v-model="condition.value" :aria-label="`${artifactQueryFieldOption(condition.field).label}条件值`">
                  <option value="">选择文件类型</option>
                  <option v-for="option in artifactKindOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
                <select v-else-if="condition.field === 'storage_status'" v-model="condition.value" :aria-label="`${artifactQueryFieldOption(condition.field).label}条件值`">
                  <option value="">选择存储状态</option>
                  <option v-for="option in storageStatusOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
                <select v-else-if="condition.field === 'ingestion_status'" v-model="condition.value" :aria-label="`${artifactQueryFieldOption(condition.field).label}条件值`">
                  <option value="">选择解析状态</option>
                  <option v-for="option in ingestionStatusOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
                <input v-else v-model="condition.value" type="text" placeholder="输入文件名片段" :aria-label="`${artifactQueryFieldOption(condition.field).label}条件值`">
              </div>
            </div>
          </div>
          <p v-if="validationError" class="advanced-query-error" role="alert">{{ validationError }}</p>
        </div>

        <footer class="advanced-query-footer">
          <button class="command-button command-button-muted" type="button" @click="close">取消</button>
          <button class="command-button" type="button" @click="apply"><Search :size="15" aria-hidden="true" />应用高级筛选</button>
        </footer>
      </section>
    </Transition>
  </Teleport>
</template>
