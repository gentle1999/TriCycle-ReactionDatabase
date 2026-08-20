<script setup lang="ts">
import { CircleAlert, CircleCheck, LoaderCircle } from "@lucide/vue";

type QueryValidationStatus = "idle" | "pending" | "valid" | "invalid";

defineProps<{
  status: QueryValidationStatus;
  message: string;
}>();
</script>

<template>
  <span
    v-if="status !== 'idle'"
    class="query-validation-indicator"
    :class="`is-${status}`"
    :title="message"
    :aria-label="message"
    :role="status === 'invalid' ? 'img' : 'status'"
  >
    <LoaderCircle v-if="status === 'pending'" class="is-spinning" :size="16" aria-hidden="true" />
    <CircleCheck v-else-if="status === 'valid'" :size="16" aria-hidden="true" />
    <CircleAlert v-else :size="16" aria-hidden="true" />
  </span>
</template>
