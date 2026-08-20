<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { geometryDepictionUrl } from "@/api";

const props = withDefaults(
  defineProps<{
    geometryId: string;
    projectId?: string;
    label?: string;
    height?: number;
  }>(),
  {
    label: "几何构象景深图",
    height: 210,
  },
);

const loading = ref(true);
const error = ref(false);
const style = computed(() => ({ height: `${props.height}px` }));
const sourceUrl = computed(() => geometryDepictionUrl(props.geometryId, props.projectId));

watch(sourceUrl, () => {
  loading.value = true;
  error.value = false;
});
</script>

<template>
  <div class="geometry-dof-preview" data-renderer="rdkit-dof" :style="style">
    <img
      :src="sourceUrl"
      :alt="label"
      loading="lazy"
      decoding="async"
      @load="loading = false"
      @error="loading = false; error = true"
    >
    <div v-if="loading" class="molecule-state">正在加载构象</div>
    <div v-else-if="error" class="molecule-state is-error">构象预览失败</div>
  </div>
</template>
