<script setup lang="ts">
import { LoaderCircle } from "@lucide/vue";

import { labelFor, statusTone } from "@/format";
import type { ArtifactSummary } from "@/types";

defineProps<{
  status: ArtifactSummary["ingestion_status"];
  errorMessage?: string | null;
}>();
</script>

<template>
  <span
    v-if="status"
    class="status-dot artifact-ingestion-status"
    :class="[statusTone(status), { 'is-processing': status === 'pending' }]"
    :title="errorMessage ?? undefined"
    role="status"
  >
    <LoaderCircle v-if="status === 'pending'" :size="12" aria-hidden="true" />
    {{ status === "pending" ? labelFor("parsing") : labelFor(status) }}
  </span>
  <span v-else>—</span>
</template>
