<script setup lang="ts">
import { CircleHelp, FlaskConical, ListFilter, RotateCcw, Search, X } from "@lucide/vue";
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { api } from "@/api";
import QueryValidationIndicator from "@/components/QueryValidationIndicator.vue";
import type { LogicalReactionDetail, LogicalReactionSummary, MappedReactionDetail, PageInfo } from "@/types";
import type { ReactionQueryFilters } from "@/reactionQuery";

import MappedReactionExpansion from "./MappedReactionExpansion.vue";
import ReactionAdvancedQueryModal from "./ReactionAdvancedQueryModal.vue";
import ReactionPathCard from "./ReactionPathCard.vue";
import PaginationControls from "./PaginationControls.vue";

const props = defineProps<{
  reactions: LogicalReactionSummary[];
  selectedReactionId: string | null;
  reaction: LogicalReactionDetail | null;
  mappedReaction: MappedReactionDetail | null;
  selectedMappedId: string | null;
  loading: boolean;
  mappedLoading: boolean;
  projectId: string | null;
  total: number;
  page: PageInfo;
  queryFilters: ReactionQueryFilters;
}>();

const emit = defineEmits<{
  selectReaction: [id: string];
  selectMapped: [id: string];
  openFrame: [id: string];
  previousPage: [];
  nextPage: [];
  jumpPage: [offset: number];
  applyFilters: [filters: ReactionQueryFilters];
}>();

const quickReactionInput = ref(props.queryFilters.reactionSmarts ?? "");
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
  if (props.queryFilters.reactionSmarts) return "反应结构查询结果";
  return "反应路径";
});

watch(() => props.queryFilters.reactionSmarts, (value) => {
  if (value !== quickReactionInput.value) quickReactionInput.value = value ?? "";
});

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

function chooseReaction(id: string): void {
  emit("selectReaction", id);
}

async function applyQuickQuery(): Promise<void> {
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
  emit("applyFilters", reactionSmarts ? { reactionSmarts } : {});
}

function clearFilters(): void {
  if (quickValidationTimer !== null) window.clearTimeout(quickValidationTimer);
  quickValidationTimer = null;
  quickValidationController?.abort();
  quickValidationController = null;
  quickValidation.value = { status: "idle", message: "" };
  quickReactionInput.value = "";
  validationError.value = "";
  advancedQueryOpen.value = false;
  emit("applyFilters", {});
}

function applyAdvancedQuery(filters: ReactionQueryFilters): void {
  quickReactionInput.value = "";
  validationError.value = "";
  advancedQueryOpen.value = false;
  emit("applyFilters", filters);
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
        <p>输入反应 SMILES 快速查询，复杂条件使用高级查询。</p>
      </div>
      <form class="reaction-filter-form" @submit.prevent="applyQuickQuery">
        <label class="search-field" :class="`is-validation-${quickValidation.status}`">
          <Search :size="15" aria-hidden="true" />
          <span class="sr-only">反应 SMILES 快速查询</span>
          <input v-model="quickReactionInput" type="search" placeholder="反应物&gt;&gt;产物，例如 C=C&gt;&gt;CC" aria-label="反应 SMILES 快速查询" :aria-invalid="quickValidation.status === 'invalid'">
          <QueryValidationIndicator :status="quickValidation.status" :message="quickValidation.message" />
        </label>
        <div class="filter-actions">
          <button class="command-button" type="submit"><Search :size="15" aria-hidden="true" />查询</button>
          <button class="command-button command-button-muted" type="button" @click="advancedQueryOpen = true"><ListFilter :size="15" aria-hidden="true" />高级查询</button>
          <RouterLink class="icon-button" :to="{ name: 'reaction-query-help' }" title="反应查询帮助" aria-label="反应查询帮助">
            <CircleHelp :size="16" aria-hidden="true" />
          </RouterLink>
          <button class="icon-button" type="button" title="清空查询" aria-label="清空查询" @click="clearFilters"><RotateCcw :size="15" aria-hidden="true" /></button>
        </div>
      </form>
      <p v-if="validationError" class="filter-error" role="alert">{{ validationError }}</p>
      <div v-if="advancedConditionCount" class="advanced-query-active">
        <span>高级查询</span><strong>{{ advancedConditionCount }} 个条件</strong>
        <button class="icon-button" type="button" title="清除高级查询" aria-label="清除高级查询" @click="clearFilters"><X :size="14" aria-hidden="true" /></button>
      </div>
      <div class="filter-summary"><strong>{{ total }}</strong><span>个匹配反应</span></div>
    </aside>

    <section class="reaction-results" aria-labelledby="reaction-results-title">
      <header class="reaction-results-header">
        <div>
          <span class="eyebrow">Reaction catalog</span>
          <h2 id="reaction-results-title">{{ resultTitle }}</h2>
        </div>
        <PaginationControls :page="page" label="反应分页（顶部）" @previous="emit('previousPage')" @next="emit('nextPage')" @jump="emit('jumpPage', $event)" />
      </header>
      <div v-if="loading && !reactions.length" class="workspace-loading"><div class="loading-block is-wide"></div><div class="loading-block is-wide"></div></div>
      <div v-else-if="!reactions.length" class="workspace-empty"><FlaskConical :size="32" /><strong>没有匹配的逻辑反应</strong><p>请调整反应 SMILES，或使用高级查询组合其他条件。</p></div>
      <div v-else class="reaction-card-list">
        <div
          v-for="item in reactions"
          :key="item.id"
          class="reaction-card-group"
          :class="{ 'is-expanded': item.id === selectedReactionId && reaction?.id === item.id }"
        >
          <ReactionPathCard
            :reaction="item"
            :project-id="projectId"
            :active="item.id === selectedReactionId"
            @select="chooseReaction"
          />
          <MappedReactionExpansion
            v-if="reaction && reaction.id === item.id && item.id === selectedReactionId"
            :reaction="reaction"
            :mapped-reaction="mappedReaction"
            :selected-mapped-id="selectedMappedId"
            :mapped-loading="mappedLoading"
            :project-id="projectId"
            @select-mapped="emit('selectMapped', $event)"
            @open-frame="emit('openFrame', $event)"
          />
        </div>
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
