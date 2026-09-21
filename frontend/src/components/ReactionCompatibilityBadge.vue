<script setup lang="ts">
defineProps<{
  reaction: {
    has_compatibility_endpoints?: boolean;
    has_single_endpoint_fallback?: boolean;
    has_dual_endpoint_fallback?: boolean;
  };
  detail?: boolean;
}>();
</script>

<template>
  <span v-if="reaction.has_compatibility_endpoints" class="compatibility-warning" :role="detail ? 'note' : undefined">
    <strong>⚠ 兼容反应 · 未经严格检验</strong>
    <span v-if="reaction.has_single_endpoint_fallback">含单端回退</span>
    <span v-if="reaction.has_dual_endpoint_fallback">含双端回退</span>
    <span v-if="detail">至少一份来源推断使用 Open Babel 重建端点（mode ratio=1）；电荷分配、金属自旋及反应合理性未经严格验证。其他严格来源不会消除此警告。</span>
  </span>
</template>

<style scoped>
.compatibility-warning {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 0.65rem;
  align-items: center;
  padding: 0.55rem 0.75rem;
  margin: 0.5rem 0;
  border: 1px solid #b45309;
  border-left: 4px solid #b45309;
  border-radius: 0.4rem;
  background: #fff7ed;
  color: #7c2d12;
  font-size: 0.85rem;
  line-height: 1.5;
}
</style>
