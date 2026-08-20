<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { CircleHelp, Plus, Search, Trash2, X } from "@lucide/vue";
import { RouterLink } from "vue-router";

import { api, type ChemistryValidationKind } from "@/api";
import ChemDoodleReactionEditor from "@/components/ChemDoodleReactionEditor.vue";
import ChemDoodleTopologyEditor from "@/components/ChemDoodleTopologyEditor.vue";
import QueryValidationIndicator from "@/components/QueryValidationIndicator.vue";
import {
  reactionQueryFieldOption,
  reactionQueryFieldOptions,
  type ReactionQueryCondition,
  type ReactionQueryExpressionCondition,
  type ReactionQueryField,
  type ReactionQueryFilters,
  type ReactionQueryLogicalOperator,
} from "@/reactionQuery";

const props = defineProps<{
  open: boolean;
  projectId: string | null;
}>();

const emit = defineEmits<{
  close: [];
  apply: [filters: ReactionQueryFilters];
}>();

const dialog = ref<HTMLElement | null>(null);
const conditions = ref<ReactionQueryCondition[]>([]);
const nextConditionId = ref(1);
const validationError = ref("");
const logicalOperator = ref<ReactionQueryLogicalOperator>("and");
type QueryValidationStatus = "idle" | "pending" | "valid" | "invalid";
interface ConditionValidationState {
  status: QueryValidationStatus;
  message: string;
}

const validationStates = ref<Record<number, ConditionValidationState>>({});
const validationTimers = new Map<number, number>();
const validationControllers = new Map<number, AbortController>();

function clearConditionValidation(id: number): void {
  const timer = validationTimers.get(id);
  if (timer !== undefined) window.clearTimeout(timer);
  validationTimers.delete(id);
  validationControllers.get(id)?.abort();
  validationControllers.delete(id);
  const next = { ...validationStates.value };
  delete next[id];
  validationStates.value = next;
}

function validationInput(condition: ReactionQueryCondition): { kind: ChemistryValidationKind; value: string } | null {
  const kind = fieldKind(condition);
  if (kind === "reaction") return { kind: "rxn_smarts", value: condition.value.trim() };
  if (kind === "smarts") return { kind: "smarts", value: condition.value.trim() };
  if (kind === "mol_block") return { kind: "mol_block", value: condition.molfile.trim() };
  return null;
}

async function validateCondition(condition: ReactionQueryCondition): Promise<boolean> {
  const input = validationInput(condition);
  if (!input || !input.value) {
    validationStates.value = { ...validationStates.value, [condition.id]: { status: "idle", message: "" } };
    return true;
  }
  validationControllers.get(condition.id)?.abort();
  const controller = new AbortController();
  validationControllers.set(condition.id, controller);
  validationStates.value = { ...validationStates.value, [condition.id]: { status: "pending", message: "正在校验格式…" } };
  try {
    const result = await api.validateChemistryRepresentation(input, controller.signal);
    if (validationControllers.get(condition.id) !== controller) return false;
    validationStates.value = {
      ...validationStates.value,
      [condition.id]: result.valid
        ? { status: "valid", message: "格式有效" }
        : { status: "invalid", message: result.error ?? "格式无法解析" },
    };
    return result.valid;
  } catch {
    if (controller.signal.aborted) return false;
    validationStates.value = { ...validationStates.value, [condition.id]: { status: "invalid", message: "校验服务暂时不可用" } };
    return false;
  } finally {
    if (validationControllers.get(condition.id) === controller) validationControllers.delete(condition.id);
  }
}

function scheduleConditionValidation(condition: ReactionQueryCondition): void {
  const input = validationInput(condition);
  const timer = validationTimers.get(condition.id);
  if (timer !== undefined) window.clearTimeout(timer);
  validationControllers.get(condition.id)?.abort();
  validationControllers.delete(condition.id);
  if (!input || !input.value) {
    validationStates.value = { ...validationStates.value, [condition.id]: { status: "idle", message: "" } };
    return;
  }
  validationStates.value = { ...validationStates.value, [condition.id]: { status: "pending", message: "等待校验…" } };
  validationTimers.set(condition.id, window.setTimeout(() => {
    validationTimers.delete(condition.id);
    void validateCondition(condition);
  }, 320));
}

function newCondition(field: ReactionQueryField = "rxn_smarts"): ReactionQueryCondition {
  return {
    id: nextConditionId.value++,
    field,
    value: field === "reaction_class" ? "cycloaddition" : "",
    reactantSmiles: "",
    productSmiles: "",
    molfile: "",
    negated: false,
  };
}

function reset(): void {
  for (const condition of conditions.value) clearConditionValidation(condition.id);
  logicalOperator.value = "and";
  conditions.value = [newCondition()];
  validationError.value = "";
}

function addCondition(): void {
  conditions.value.push(newCondition("reaction_key"));
  validationError.value = "";
}

function removeCondition(id: number): void {
  clearConditionValidation(id);
  conditions.value = conditions.value.filter((condition) => condition.id !== id);
  validationError.value = "";
}

function changeField(condition: ReactionQueryCondition): void {
  clearConditionValidation(condition.id);
  condition.value = condition.field === "reaction_class" ? "cycloaddition" : "";
  condition.reactantSmiles = "";
  condition.productSmiles = "";
  condition.molfile = "";
  condition.negated = false;
  validationError.value = "";
}

function fieldKind(condition: ReactionQueryCondition): string {
  return reactionQueryFieldOption(condition.field).kind;
}

function parseNumber(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseDatetime(value: string): string | null {
  if (!value.trim()) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? null : parsed.toISOString();
}

function buildFilters(): ReactionQueryFilters | null {
  const expressionConditions: ReactionQueryExpressionCondition[] = [];
  for (const condition of conditions.value) {
    let expressionValue: string | number;
    const structureInput = validationInput(condition);
    const structureValidation = validationStates.value[condition.id];
    if (fieldKind(condition) === "reaction") {
      expressionValue = condition.value.trim();
      if (!expressionValue) {
        validationError.value = "请绘制反应或填写反应 SMILES";
        return null;
      }
    } else if (fieldKind(condition) === "mol_block") {
      expressionValue = condition.molfile.trim();
      if (!expressionValue) {
        validationError.value = `请绘制“${reactionQueryFieldOption(condition.field).label}”结构`;
        return null;
      }
    } else if (fieldKind(condition) === "number") {
      const parsed = parseNumber(condition.value);
      if (parsed === null) {
        validationError.value = `“${reactionQueryFieldOption(condition.field).label}”必须是有限数字`;
        return null;
      }
      expressionValue = parsed;
    } else if (fieldKind(condition) === "datetime") {
      const parsed = parseDatetime(condition.value);
      if (parsed === null) {
        validationError.value = `“${reactionQueryFieldOption(condition.field).label}”必须是有效时间`;
        return null;
      }
      expressionValue = parsed;
    } else {
      expressionValue = condition.value.trim();
      if (!expressionValue) {
        validationError.value = `请填写“${reactionQueryFieldOption(condition.field).label}”`;
        return null;
      }
    }
    if (structureInput && structureValidation?.status === "invalid") {
      validationError.value = structureValidation.message;
      return null;
    }
    if (structureInput && structureValidation?.status === "pending") {
      validationError.value = "请等待结构格式校验完成";
      return null;
    }
    expressionConditions.push({
      field: condition.field,
      value: expressionValue,
      ...(condition.negated ? { negated: true } : {}),
    });
  }
  validationError.value = "";
  return {
    filterExpression: {
      operator: logicalOperator.value,
      conditions: expressionConditions,
    },
  };
}

async function apply(): Promise<void> {
  const validationResults = await Promise.all(conditions.value.map((condition) => validateCondition(condition)));
  if (!validationResults.every(Boolean)) {
    validationError.value = conditions.value
      .map((condition) => validationStates.value[condition.id]?.message)
      .find((message) => Boolean(message) && message !== "格式有效") ?? "请修正无效的结构条件";
    return;
  }
  const filters = buildFilters();
  if (filters) emit("apply", filters);
}

function close(): void {
  emit("close");
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && props.open) close();
}

watch(
  () => props.open,
  (open) => {
    document.body.classList.toggle("drawer-open", open);
    if (open) {
      reset();
      void nextTick(() => dialog.value?.querySelector<HTMLSelectElement>("select")?.focus());
    }
  },
);

onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.classList.remove("drawer-open");
  for (const condition of conditions.value) clearConditionValidation(condition.id);
});

watch(
  conditions,
  (nextConditions) => {
    for (const condition of nextConditions) scheduleConditionValidation(condition);
  },
  { deep: true },
);
</script>

<template>
  <Teleport to="body">
    <Transition name="advanced-query-fade">
      <button
        v-if="open"
        class="advanced-query-backdrop"
        type="button"
        aria-label="关闭高级查询"
        @click="close"
      ></button>
    </Transition>
    <Transition name="advanced-query-panel">
      <section
        v-if="open"
        ref="dialog"
        class="advanced-query-dialog reaction-advanced-query-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reaction-advanced-query-title"
      >
        <header class="advanced-query-header">
          <div>
            <span class="eyebrow">LogicalReaction query builder</span>
            <h2 id="reaction-advanced-query-title">高级查询</h2>
          </div>
          <div class="advanced-query-header-actions">
            <RouterLink class="icon-button" :to="{ name: 'reaction-query-help' }" title="查看反应查询帮助" aria-label="查看反应查询帮助">
              <CircleHelp :size="17" aria-hidden="true" />
            </RouterLink>
            <button class="icon-button" type="button" title="关闭高级查询" aria-label="关闭高级查询" @click="close">
              <X :size="18" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div class="advanced-query-content">
          <div class="advanced-query-toolbar">
            <label class="advanced-query-logic">
              <span>条件组合</span>
              <select v-model="logicalOperator" aria-label="反应高级查询逻辑运算">
                <option value="and">全部满足（AND）</option>
                <option value="or">任一满足（OR）</option>
              </select>
            </label>
            <button class="command-button command-button-muted" type="button" @click="addCondition">
              <Plus :size="15" aria-hidden="true" />添加条件
            </button>
          </div>

          <div class="advanced-query-conditions">
            <div v-for="condition in conditions" :key="condition.id" class="advanced-query-row">
              <div class="advanced-query-row-header">
                <span class="advanced-query-index">{{ String(condition.id).padStart(2, "0") }}</span>
                <label :for="`reaction-advanced-query-field-${condition.id}`">字段</label>
                <select
                  :id="`reaction-advanced-query-field-${condition.id}`"
                  v-model="condition.field"
                  @change="changeField(condition)"
                >
                  <option v-for="option in reactionQueryFieldOptions" :key="option.value" :value="option.value">
                    {{ option.label }}
                  </option>
                </select>
                <label class="advanced-query-negation">
                  <input v-model="condition.negated" type="checkbox">
                  <span>排除（NOT）</span>
                </label>
                <button
                  class="icon-button advanced-query-remove"
                  type="button"
                  title="删除条件"
                  aria-label="删除条件"
                  :disabled="conditions.length === 1"
                  @click="removeCondition(condition.id)"
                >
                  <Trash2 :size="15" aria-hidden="true" />
                </button>
              </div>

              <div
                class="advanced-query-value"
                :data-query-field="condition.field"
                :data-validation-status="validationStates[condition.id]?.status ?? 'idle'"
              >
                <ChemDoodleReactionEditor v-if="fieldKind(condition) === 'reaction'" v-model="condition.value" :height="250">
                  <template #validation>
                    <QueryValidationIndicator
                      :status="validationStates[condition.id]?.status ?? 'idle'"
                      :message="validationStates[condition.id]?.message ?? ''"
                    />
                  </template>
                </ChemDoodleReactionEditor>
                <ChemDoodleTopologyEditor
                  v-else-if="fieldKind(condition) === 'mol_block'"
                  v-model="condition.value"
                  v-model:molfile="condition.molfile"
                  :height="210"
                >
                  <template #validation>
                    <QueryValidationIndicator
                      :status="validationStates[condition.id]?.status ?? 'idle'"
                      :message="validationStates[condition.id]?.message ?? ''"
                    />
                  </template>
                </ChemDoodleTopologyEditor>
                <select v-else-if="fieldKind(condition) === 'class'" v-model="condition.value" aria-label="反应类型条件">
                  <option value="cycloaddition">环加成</option>
                </select>
                <div v-else class="query-input-with-validation">
                  <input
                    v-model="condition.value"
                    :type="fieldKind(condition) === 'number' ? 'number' : fieldKind(condition) === 'datetime' ? 'datetime-local' : 'text'"
                    :step="fieldKind(condition) === 'number' ? '0.1' : undefined"
                    :placeholder="fieldKind(condition) === 'smarts' ? '[C;H2]=[C;H2]' : fieldKind(condition) === 'text' ? '输入条件值' : undefined"
                    :aria-label="`${reactionQueryFieldOption(condition.field).label}条件值`"
                    :aria-invalid="validationStates[condition.id]?.status === 'invalid'"
                  >
                  <QueryValidationIndicator
                    v-if="fieldKind(condition) === 'smarts'"
                    :status="validationStates[condition.id]?.status ?? 'idle'"
                    :message="validationStates[condition.id]?.message ?? ''"
                  />
                </div>
              </div>
            </div>
          </div>

          <p v-if="validationError" class="advanced-query-error" role="alert">{{ validationError }}</p>
          <p v-if="projectId" class="advanced-query-context">当前项目：<code>{{ projectId }}</code></p>
        </div>

        <footer class="advanced-query-footer">
          <button class="command-button command-button-muted" type="button" @click="close">取消</button>
          <button class="command-button" type="button" @click="apply">
            <Search :size="15" aria-hidden="true" />应用高级查询
          </button>
        </footer>
      </section>
    </Transition>
  </Teleport>
</template>
