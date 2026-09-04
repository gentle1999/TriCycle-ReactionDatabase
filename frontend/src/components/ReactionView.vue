<script setup lang="ts">
import { CircleHelp, FlaskConical, ListFilter, LoaderCircle, RotateCcw, Search, X } from "@lucide/vue";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { api } from "@/api";
import QueryValidationIndicator from "@/components/QueryValidationIndicator.vue";
import type { LogicalReactionSummary, PageInfo } from "@/types";
import type { ReactionQueryFilters, ReactionSort, ReactionSortBy } from "@/reactionQuery";

import ReactionAdvancedQueryModal from "./ReactionAdvancedQueryModal.vue";
import ReactionPathCard from "./ReactionPathCard.vue";
import PaginationControls from "./PaginationControls.vue";

const props = defineProps<{
  reactions: LogicalReactionSummary[];
  loading: boolean;
  querying: boolean;
  projectId: string | null;
  total: number;
  page: PageInfo;
  queryFilters: ReactionQueryFilters;
  sort: ReactionSort;
}>();

const emit = defineEmits<{
  previousPage: [];
  nextPage: [];
  jumpPage: [offset: number];
  applyFilters: [filters: ReactionQueryFilters];
  updateSort: [sort: ReactionSort];
}>();

const quickReactionInput = ref(props.queryFilters.similarityReactionSmiles ?? props.queryFilters.reactionSmarts ?? "");
const hasActivationGibbsFreeEnergy = ref(props.queryFilters.hasActivationGibbsFreeEnergy ?? false);
const hasReactionGibbsFreeEnergy = ref(props.queryFilters.hasReactionGibbsFreeEnergy ?? false);
const reactantProductChanged = ref<boolean | null>(props.queryFilters.reactantProductChanged ?? null);
const minimumMappedReactionCount = ref<number | string | null>(props.queryFilters.minimumMappedReactionCount ?? null);
const maximumMappedReactionCount = ref<number | string | null>(props.queryFilters.maximumMappedReactionCount ?? null);
const validationError = ref("");
type QueryValidationStatus = "idle" | "pending" | "valid" | "invalid";
const quickValidation = ref<{ status: QueryValidationStatus; message: string }>({ status: "idle", message: "" });
const advancedQueryOpen = ref(false);
let quickValidationTimer: number | null = null;
let quickValidationController: AbortController | null = null;
let quickValidationGeneration = 0;
const advancedConditionCount = computed(() => props.queryFilters.filterExpression?.conditions.length ?? 0);
const resultTitle = computed(() => {
  if (advancedConditionCount.value) return "高级查询结果";
  if (props.queryFilters.similarityReactionSmiles) return "映射反应相似度查询结果";
  if (props.queryFilters.reactionSmarts) return "映射反应结构查询结果";
  return "反应路径";
});

watch(() => props.queryFilters.similarityReactionSmiles ?? props.queryFilters.reactionSmarts, (value) => {
  if (value !== quickReactionInput.value) quickReactionInput.value = value ?? "";
});
watch(() => props.queryFilters.hasActivationGibbsFreeEnergy, (value) => {
  hasActivationGibbsFreeEnergy.value = value ?? false;
});
watch(() => props.queryFilters.hasReactionGibbsFreeEnergy, (value) => {
  hasReactionGibbsFreeEnergy.value = value ?? false;
});
watch(() => props.queryFilters.reactantProductChanged, (value) => {
  reactantProductChanged.value = value ?? null;
});
watch(() => props.queryFilters.minimumMappedReactionCount, (value) => {
  minimumMappedReactionCount.value = value ?? null;
});
watch(() => props.queryFilters.maximumMappedReactionCount, (value) => {
  maximumMappedReactionCount.value = value ?? null;
});

function selectedEnergyFilters(): ReactionQueryFilters {
  const minimumMappedCount = mappedReactionCountValue(minimumMappedReactionCount.value);
  const maximumMappedCount = mappedReactionCountValue(maximumMappedReactionCount.value);
  return {
    ...(hasActivationGibbsFreeEnergy.value ? { hasActivationGibbsFreeEnergy: true } : { hasActivationGibbsFreeEnergy: undefined }),
    ...(hasReactionGibbsFreeEnergy.value ? { hasReactionGibbsFreeEnergy: true } : { hasReactionGibbsFreeEnergy: undefined }),
    ...(reactantProductChanged.value === null ? { reactantProductChanged: undefined } : { reactantProductChanged: reactantProductChanged.value }),
    ...(minimumMappedCount === undefined ? { minimumMappedReactionCount: undefined } : { minimumMappedReactionCount: minimumMappedCount }),
    ...(maximumMappedCount === undefined ? { maximumMappedReactionCount: undefined } : { maximumMappedReactionCount: maximumMappedCount }),
  };
}

function mappedReactionCountValue(value: number | string | null): number | undefined {
  if (value === null || (typeof value === "string" && !value.trim())) return undefined;
  return typeof value === "number" ? value : Number(value);
}

function validateMappedReactionCountRange(): boolean {
  for (const [value, label] of [
    [mappedReactionCountValue(minimumMappedReactionCount.value), "最少映射反应数"],
    [mappedReactionCountValue(maximumMappedReactionCount.value), "最多映射反应数"],
  ] as const) {
    if (value !== undefined && (!Number.isInteger(value) || value < 0)) {
      validationError.value = `${label}必须是非负整数`;
      return false;
    }
  }
  const minimumMappedCount = mappedReactionCountValue(minimumMappedReactionCount.value);
  const maximumMappedCount = mappedReactionCountValue(maximumMappedReactionCount.value);
  if (
    minimumMappedCount !== undefined
    && maximumMappedCount !== undefined
    && minimumMappedCount > maximumMappedCount
  ) {
    validationError.value = "最少映射反应数不能大于最多映射反应数";
    return false;
  }
  return true;
}

function applyOuterFilters(): void {
  if (!validateMappedReactionCountRange()) return;
  validationError.value = "";
  emit("applyFilters", outerReactionFilters());
}

function outerReactionFilters(): ReactionQueryFilters {
  return { ...props.queryFilters, ...selectedEnergyFilters() };
}

async function validateQuickQuery(value: string): Promise<boolean> {
  const reaction = value.trim();
  if (!reaction) {
    quickValidation.value = { status: "idle", message: "" };
    return true;
  }
  if (!reaction.includes(">>")) {
    quickValidation.value = { status: "invalid", message: "请输入“反应物>>产物”格式" };
    return false;
  }
  quickValidationController?.abort();
  const controller = new AbortController();
  quickValidationController = controller;
  const generation = ++quickValidationGeneration;
  quickValidation.value = { status: "pending", message: "正在校验反应格式…" };
  try {
    const result = await api.validateChemistryRepresentation(
      { kind: "rxn_smiles", value: reaction },
      controller.signal,
    );
    if (generation !== quickValidationGeneration) return false;
    quickValidation.value = result.valid
      ? { status: "valid", message: "反应格式有效" }
      : { status: "invalid", message: result.error ?? "反应格式无法解析" };
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
  const reaction = value.trim();
  if (!reaction) {
    quickValidation.value = { status: "idle", message: "" };
    return;
  }
  if (!reaction.includes(">>")) {
    quickValidation.value = { status: "invalid", message: "请输入“反应物>>产物”格式" };
    return;
  }
  quickValidation.value = { status: "pending", message: "等待校验…" };
  quickValidationTimer = window.setTimeout(() => {
    quickValidationTimer = null;
    void validateQuickQuery(reaction);
  }, 320);
}

async function applyQuickQuery(): Promise<void> {
  if (!validateMappedReactionCountRange()) return;
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationTimer = null;
  const reactionSmarts = quickReactionInput.value.trim();
  if (reactionSmarts && !reactionSmarts.includes(">>")) {
    validationError.value = "请输入“反应物>>产物”格式的反应 SMILES";
    return;
  }
  if (!(await validateQuickQuery(reactionSmarts))) {
    validationError.value = quickValidation.value.message;
    return;
  }
  validationError.value = "";
  emit("applyFilters", { ...selectedEnergyFilters(), similarityReactionSmiles: reactionSmarts || undefined });
}

function clearFilters(): void {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationTimer = null;
  quickValidationController?.abort();
  quickValidationController = null;
  quickValidation.value = { status: "idle", message: "" };
  quickReactionInput.value = "";
  hasActivationGibbsFreeEnergy.value = false;
  hasReactionGibbsFreeEnergy.value = false;
  reactantProductChanged.value = null;
  minimumMappedReactionCount.value = null;
  maximumMappedReactionCount.value = null;
  validationError.value = "";
  advancedQueryOpen.value = false;
  emit("applyFilters", {});
}

function applyAdvancedQuery(filters: ReactionQueryFilters): void {
  if (!validateMappedReactionCountRange()) return;
  quickReactionInput.value = "";
  validationError.value = "";
  advancedQueryOpen.value = false;
  emit("applyFilters", {
    ...filters,
    ...selectedEnergyFilters(),
  });
}

function applyOuterEnergyFilters(): void {
  applyOuterFilters();
}

function updateSortBy(event: Event): void {
  emit("updateSort", {
    sortBy: (event.target as HTMLSelectElement).value as ReactionSortBy,
    sortDirection: props.sort.sortDirection,
  });
}

function updateSortDirection(event: Event): void {
  emit("updateSort", {
    sortBy: props.sort.sortBy,
    sortDirection: (event.target as HTMLSelectElement).value as ReactionSort["sortDirection"],
  });
}

watch(quickReactionInput, (value) => scheduleQuickValidation(value));
onBeforeUnmount(() => {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationController?.abort();
});
</script>

<template>
  <section class="reaction-browser" aria-labelledby="reaction-view-title">
    <aside class="reaction-filter-sidebar">
      <div class="panel-heading">
        <span class="eyebrow">LogicalReaction</span>
        <h1 id="reaction-view-title">反应路径</h1>
        <p>结构条件按 MappedReaction 查询，再按 LogicalReaction 归并；复杂条件使用高级查询。</p>
      </div>
      <form class="reaction-filter-form" @submit.prevent="applyQuickQuery">
        <label class="search-field" :class="`is-validation-${quickValidation.status}`">
          <Search :size="15" aria-hidden="true" />
          <span class="sr-only">映射反应 SMILES 快速查询</span>
          <input v-model="quickReactionInput" type="search" placeholder="映射反应物&gt;&gt;产物，例如 C=C&gt;&gt;CC" aria-label="映射反应 SMILES 快速查询" :aria-invalid="quickValidation.status === 'invalid'">
          <QueryValidationIndicator :status="quickValidation.status" :message="quickValidation.message" />
        </label>
        <div class="filter-actions">
          <button class="command-button" type="submit" :disabled="querying"><LoaderCircle v-if="querying" class="is-spinning" :size="15" aria-hidden="true" /><Search v-else :size="15" aria-hidden="true" />{{ querying ? "正在查询" : "查询" }}</button>
          <button class="command-button command-button-muted" type="button" @click="advancedQueryOpen = true"><ListFilter :size="15" aria-hidden="true" />高级查询</button>
          <RouterLink class="icon-button" :to="{ name: 'reaction-query-help' }" title="反应查询帮助" aria-label="反应查询帮助">
            <CircleHelp :size="16" aria-hidden="true" />
          </RouterLink>
          <button class="icon-button" type="button" title="清空查询" aria-label="清空查询" @click="clearFilters"><RotateCcw :size="15" aria-hidden="true" /></button>
        </div>
      </form>
      <p v-if="validationError" class="filter-error" role="alert">{{ validationError }}</p>
      <div class="reaction-energy-filters" aria-label="自由能筛选">
        <label class="toggle-filter">
          <input v-model="hasActivationGibbsFreeEnergy" type="checkbox" aria-label="仅显示含有活化自由能的反应路径" @change="applyOuterEnergyFilters">
          <span><strong>含有活化自由能</strong><small>仅保留有 ΔG‡ 数据的反应路径。</small></span>
        </label>
        <label class="toggle-filter">
          <input v-model="hasReactionGibbsFreeEnergy" type="checkbox" aria-label="仅显示含有反应自由能的反应路径" @change="applyOuterEnergyFilters">
          <span><strong>含有反应自由能</strong><small>仅保留有 ΔG 数据的反应路径。</small></span>
        </label>
      </div>
      <label class="filter-select-field reaction-change-filter">
        <span>前后体拓扑</span>
        <select v-model="reactantProductChanged" aria-label="前后体拓扑是否发生变化" @change="applyOuterEnergyFilters">
          <option :value="null">全部反应</option>
          <option :value="true">发生变化</option>
          <option :value="false">未发生变化</option>
        </select>
      </label>
      <div class="reaction-mapping-count-filters" aria-label="映射反应数量筛选">
        <span class="filter-field-label">映射反应数</span>
        <div class="reaction-mapping-count-range">
          <label>
            <span>至少</span>
            <input
              v-model.number="minimumMappedReactionCount"
              type="number"
              min="0"
              step="1"
              placeholder="不限"
              aria-label="最少映射反应数"
              @change="applyOuterFilters"
            >
          </label>
          <span class="reaction-mapping-count-range-separator" aria-hidden="true">–</span>
          <label>
            <span>最多</span>
            <input
              v-model.number="maximumMappedReactionCount"
              type="number"
              min="0"
              step="1"
              placeholder="不限"
              aria-label="最多映射反应数"
              @change="applyOuterFilters"
            >
          </label>
        </div>
      </div>
      <div v-if="advancedConditionCount" class="advanced-query-active">
        <span>高级查询</span><strong>{{ advancedConditionCount }} 个条件</strong>
        <button class="icon-button" type="button" title="清除高级查询" aria-label="清除高级查询" @click="clearFilters"><X :size="14" aria-hidden="true" /></button>
      </div>
      <div class="filter-summary"><strong>{{ total }}</strong><span>个匹配反应</span></div>
    </aside>

    <section class="reaction-results" aria-labelledby="reaction-results-title" :aria-busy="querying">
      <header class="reaction-results-header">
        <div>
          <span class="eyebrow">Reaction catalog</span>
          <h2 id="reaction-results-title">{{ resultTitle }}</h2>
        </div>
        <div class="catalog-header-actions">
          <div class="catalog-sort-controls" aria-label="反应排序">
            <label><span>排序</span><select :value="sort.sortBy" aria-label="反应排序字段" @change="updateSortBy"><option value="default">默认顺序</option><option v-if="queryFilters.similarityReactionSmiles" value="similarity">相似度</option><option value="created_at">创建时间</option><option value="reaction_key">反应键</option><option value="reaction_class">反应类型</option><option value="minimum_activation_gibbs_free_energy">最低 ΔG‡</option><option value="minimum_reaction_gibbs_free_energy">最低 ΔG</option></select></label>
            <label><span>顺序</span><select :value="sort.sortDirection" aria-label="反应排序方向" :disabled="sort.sortBy === 'default' || sort.sortBy === 'similarity'" @change="updateSortDirection"><option value="asc">升序</option><option value="desc">降序</option></select></label>
          </div>
          <PaginationControls :page="page" label="反应分页（顶部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
        </div>
      </header>
      <div class="catalog-query-status-slot" aria-live="polite">
        <div v-if="querying" class="catalog-query-status" role="status"><LoaderCircle class="is-spinning" :size="16" aria-hidden="true" /><span>{{ reactions.length ? "正在查询，当前显示上次结果" : "正在查询筛选结果" }}</span></div>
      </div>
      <div v-if="loading && !reactions.length" class="workspace-loading"><div class="loading-block is-wide"></div><div class="loading-block is-wide"></div></div>
      <div v-else-if="!reactions.length" class="workspace-empty"><FlaskConical :size="32" /><strong>没有匹配的逻辑反应</strong><p>请调整反应 SMILES，或使用高级查询组合其他条件。</p></div>
      <div v-else class="reaction-card-list">
        <template v-for="item in reactions" :key="item.id">
          <ReactionPathCard
            :reaction="item"
            :project-id="projectId"
          />
        </template>
      </div>
      <PaginationControls :page="page" label="反应分页（底部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
    </section>

    <ReactionAdvancedQueryModal
      :open="advancedQueryOpen"
      :project-id="projectId"
      @close="advancedQueryOpen = false"
      @apply="applyAdvancedQuery"
    />
  </section>
</template>
