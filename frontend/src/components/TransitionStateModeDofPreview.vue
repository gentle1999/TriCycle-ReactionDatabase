<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { transitionStateModeDofAnimationUrl } from "@/api";

const props = withDefaults(
  defineProps<{
    frameId: string;
    projectId?: string;
    height?: number;
  }>(),
  { height: 340 },
);

const loading = ref(true);
const error = ref(false);
const style = computed(() => ({ height: `${props.height}px` }));
const sourceUrl = computed(() => transitionStateModeDofAnimationUrl(props.frameId, props.projectId));

watch(sourceUrl, () => {
  loading.value = true;
  error.value = false;
});
</script>

<template>
  <section class="transition-state-mode-dof-preview" data-renderer="rdkit-dof-ts-mode" data-animation="smil" :style="style">
    <header><div><span class="eyebrow">RDKit-DOF</span><strong>虚频模式拓扑</strong></div></header>
    <div class="transition-state-mode-dof-image">
      <img
        :src="sourceUrl"
        alt="虚频模式插值的分子拓扑动画"
        decoding="async"
        @load="loading = false"
        @error="loading = false; error = true"
      >
      <div v-if="loading" class="molecule-state">正在生成虚频模式拓扑</div>
      <div v-else-if="error" class="molecule-state is-error">虚频模式拓扑预览失败</div>
    </div>
  </section>
</template>
