<script setup lang="ts">
import { computed } from "vue";

type ButtonVariant = "primary" | "secondary" | "muted" | "quiet";

const props = withDefaults(defineProps<{
  variant?: ButtonVariant;
  type?: "button" | "submit" | "reset";
  disabled?: boolean;
  busy?: boolean;
}>(), {
  variant: "primary",
  type: "button",
  disabled: false,
  busy: false,
});

const variantClass = computed(() => props.variant === "primary" ? undefined : {
  "command-button-secondary": props.variant === "secondary",
  "command-button-muted": props.variant === "muted",
  "is-quiet": props.variant === "quiet",
});
</script>

<template>
  <button
    class="command-button"
    :class="variantClass"
    :type="type"
    :disabled="disabled || busy"
    :aria-busy="busy || undefined"
  >
    <slot />
  </button>
</template>

