<script setup lang="ts">
import { Check, ChevronLeft, ChevronRight } from "@lucide/vue";
import { computed, ref, watch } from "vue";

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
    ? `第 ${currentPage.value} 页`
    : props.page.total
    ? `${props.page.offset + 1}-${Math.min(props.page.offset + props.page.limit, props.page.total)} / ${props.page.total}`
    : "0 / 0",
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
    <button class="icon-button" type="button" title="上一页" aria-label="上一页" :disabled="!hasPrevious" @click="emit('previous')">
      <ChevronLeft :size="16" aria-hidden="true" />
    </button>
    <span>{{ range }}</span>
    <form v-if="!cursorMode" class="catalog-page-jump" @submit.prevent="submitJump">
      <label><span>页码</span><input v-model="pageInput" aria-label="跳转页码" type="number" min="1" :max="totalPages" step="1"></label>
      <span aria-hidden="true">/ {{ totalPages }}</span>
      <button class="command-button pagination-jump-button" type="submit" :disabled="!canJump"><Check :size="14" aria-hidden="true" />跳转</button>
    </form>
    <button class="icon-button" type="button" title="下一页" aria-label="下一页" :disabled="!hasNext" @click="emit('next')">
      <ChevronRight :size="16" aria-hidden="true" />
    </button>
  </nav>
</template>
