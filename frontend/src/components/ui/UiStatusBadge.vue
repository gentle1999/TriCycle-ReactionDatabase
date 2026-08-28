<script setup lang="ts">
import { labelFor, statusTone } from "@/format";

const props = defineProps<{
  status: string | null | undefined;
  label?: string;
}>();
</script>

<template>
  <span class="status-dot" :class="statusTone(props.status)">{{ props.label ?? labelFor(props.status) }}</span>
</template>

