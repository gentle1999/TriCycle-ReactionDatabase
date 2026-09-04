<script setup lang="ts">
import { Check, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

import { UiButton, UiIconButton } from "@/components/ui";
import type { PageInfo } from "@/types";

const props = defineProps<{
  page: PageInfo;
  label: string;
}>();

const emit = defineEmits<{
  previous: [];
  next: [];
  jump: [offset: number];
}>();
const { t } = useI18n();

const cursorMode = computed(() => props.page.total < 0);
const hasPrevious = computed(() => props.page.offset > 0);
const hasNext = computed(() => cursorMode.value
  ? Boolean(props.page.next_cursor)
  : props.page.offset + props.page.limit < props.page.total);
const currentPage = computed(() => Math.floor(props.page.offset / props.page.limit) + 1);
const totalPages = computed(() => Math.ceil(props.page.total / props.page.limit));
const pageInput = ref<string | number>(currentPage.value);
const requestedPage = computed(() => {
  const value = Number(pageInput.value);
  return Number.isInteger(value) ? value : null;
});
const canJump = computed(() =>
  requestedPage.value !== null
  && requestedPage.value >= 1
  && requestedPage.value <= totalPages.value,
);
const range = computed(() =>
  cursorMode.value
    ? t("pagination.cursorPage", { page: currentPage.value })
    : props.page.total
    ? t("pagination.range", {
      start: props.page.offset + 1,
      end: Math.min(props.page.offset + props.page.limit, props.page.total),
      total: props.page.total,
    })
    : t("pagination.empty"),
);
const visible = computed(() => cursorMode.value
  ? hasPrevious.value || hasNext.value
  : props.page.total > props.page.limit);

watch(currentPage, (value) => { pageInput.value = value; });

function submitJump(): void {
  if (!canJump.value || requestedPage.value === null) return;
  emit("jump", (requestedPage.value - 1) * props.page.limit);
}
</script>

<template>
  <nav v-if="visible" class="catalog-pagination" :aria-label="label">
    <UiIconButton v-if="!cursorMode" :label="t('common.first')" :disabled="currentPage <= 1" @click="emit('jump', 0)">
      <ChevronsLeft :size="16" aria-hidden="true" />
    </UiIconButton>
    <UiIconButton :label="t('common.previous')" :disabled="!hasPrevious" @click="emit('previous')">
      <ChevronLeft :size="16" aria-hidden="true" />
    </UiIconButton>
    <span>{{ range }}</span>
    <form v-if="!cursorMode" class="catalog-page-jump" @submit.prevent="submitJump">
      <label><span>{{ t("pagination.pageInput") }}</span><input v-model="pageInput" :aria-label="t('pagination.pageInput')" type="number" min="1" :max="totalPages" step="1"></label>
      <span aria-hidden="true">/ {{ totalPages }}</span>
      <UiButton class="pagination-jump-button" type="submit" :disabled="!canJump"><Check :size="14" aria-hidden="true" />{{ t("pagination.jump") }}</UiButton>
    </form>
    <UiIconButton :label="t('common.next')" :disabled="!hasNext" @click="emit('next')">
      <ChevronRight :size="16" aria-hidden="true" />
    </UiIconButton>
    <UiIconButton v-if="!cursorMode" :label="t('common.last')" :disabled="currentPage >= totalPages" @click="emit('jump', (totalPages - 1) * props.page.limit)">
      <ChevronsRight :size="16" aria-hidden="true" />
    </UiIconButton>
  </nav>
</template>
