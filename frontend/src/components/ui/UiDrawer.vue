<script setup lang="ts">
import { X } from "@lucide/vue";
import { computed, onBeforeUnmount, onMounted, watch } from "vue";
import { useI18n } from "vue-i18n";

import UiIconButton from "./UiIconButton.vue";

const props = withDefaults(defineProps<{
  open: boolean;
  title: string;
  eyebrow?: string;
  titleId?: string;
  closeLabel?: string;
  widthClass?: string;
}>(), {
  eyebrow: "Details",
  titleId: "ui-drawer-title",
  closeLabel: undefined,
  widthClass: "",
});

const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const resolvedCloseLabel = computed(() => props.closeLabel || t("common.close"));

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && props.open) emit("close");
}

watch(
  () => props.open,
  (open) => document.body.classList.toggle("drawer-open", open),
);

onMounted(() => window.addEventListener("keydown", onKeydown));
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.classList.remove("drawer-open");
});
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <button v-if="open" class="drawer-backdrop" type="button" :aria-label="resolvedCloseLabel" @click="emit('close')"></button>
    </Transition>
    <Transition name="drawer-panel">
      <aside v-if="open" class="detail-drawer" :class="widthClass" role="dialog" aria-modal="true" :aria-labelledby="titleId">
        <header class="drawer-header">
          <div><span class="eyebrow">{{ eyebrow }}</span><h2 :id="titleId">{{ title }}</h2></div>
          <div class="drawer-actions">
            <slot name="actions" />
            <UiIconButton :label="resolvedCloseLabel" @click="emit('close')">
              <slot name="close-icon"><X :size="18" aria-hidden="true" /></slot>
            </UiIconButton>
          </div>
        </header>
        <slot />
      </aside>
    </Transition>
  </Teleport>
</template>
