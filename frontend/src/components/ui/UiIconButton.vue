<script setup lang="ts">
withDefaults(defineProps<{
  label: string;
  title?: string;
  type?: "button" | "submit" | "reset";
  disabled?: boolean;
  spinning?: boolean;
}>(), {
  title: undefined,
  type: "button",
  disabled: false,
  spinning: false,
});
</script>

<template>
  <button
    class="icon-button"
    :class="{ 'is-spinning': spinning }"
    :type="type"
    :title="title || label"
    :aria-label="label"
    :disabled="disabled"
  >
    <slot />
  </button>
</template>

