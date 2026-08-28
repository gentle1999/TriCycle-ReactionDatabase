<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import { frontendAppName } from "@/branding";
import { useI18n } from "vue-i18n";

const route = useRoute();
const { t } = useI18n();
const returnTo = computed(() => typeof route.query.redirect === "string" ? route.query.redirect : "/reactions");
const error = computed(() => typeof route.query.error === "string" ? route.query.error : null);
</script>

<template>
  <main class="auth-page" aria-labelledby="login-title">
    <section class="state-page auth-card">
      <span class="eyebrow">{{ t("auth.eyebrow") }}</span>
      <h1 id="login-title">{{ t("auth.title", { app: frontendAppName }) }}</h1>
      <p>{{ t("auth.description") }}</p>
      <p v-if="error" class="error-text" role="alert">{{ t("auth.loginFailed", { error }) }}</p>
      <a class="command-button" :href="api.loginUrl(returnTo)">{{ t("auth.continue") }}</a>
      <RouterLink class="text-link" :to="{ name: 'reactions' }">{{ t("auth.publicBrowse") }}</RouterLink>
    </section>
  </main>
</template>
