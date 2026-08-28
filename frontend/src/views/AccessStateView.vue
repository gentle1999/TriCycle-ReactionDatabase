<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { useSession } from "@/composables/useSession";
import { useI18n } from "vue-i18n";

const route = useRoute();
const session = useSession();
const { t } = useI18n();
const isAuthenticated = session.isAuthenticated;
const title = computed(() => String(route.meta.title ?? t("state.page")));
const isNotFound = computed(() => route.name === "not-found");
</script>

<template>
  <section class="state-page" aria-labelledby="state-page-title">
    <span class="eyebrow">{{ isAuthenticated ? t("state.workspace") : t("state.authentication") }}</span>
    <h1 id="state-page-title">{{ isNotFound ? t("state.notFound") : title }}</h1>
    <p v-if="!isAuthenticated && route.meta.requiresAuth">{{ t("state.authRequired") }}</p>
    <p v-else>{{ t("state.pending") }}</p>
    <RouterLink v-if="!isAuthenticated && route.meta.requiresAuth" class="command-button" :to="{ name: 'login', query: { redirect: route.fullPath } }">{{ t("state.login") }}</RouterLink>
    <RouterLink v-else class="command-button" :to="{ name: 'reactions' }">{{ t("state.backToWorkspace") }}</RouterLink>
  </section>
</template>
