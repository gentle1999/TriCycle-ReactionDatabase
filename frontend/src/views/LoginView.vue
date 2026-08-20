<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { api } from "@/api";
import { frontendAppName } from "@/branding";

const route = useRoute();
const returnTo = computed(() => typeof route.query.redirect === "string" ? route.query.redirect : "/reactions");
const error = computed(() => typeof route.query.error === "string" ? route.query.error : null);
</script>

<template>
  <main class="auth-page" aria-labelledby="login-title">
    <section class="state-page auth-card">
      <span class="eyebrow">Authentication</span>
      <h1 id="login-title">登录{{ frontendAppName }}</h1>
      <p>使用组织身份提供方登录或创建账户。首次登录后可创建组织和项目，也可以接受已有项目的邀请。</p>
      <p v-if="error" class="error-text" role="alert">登录失败：{{ error }}</p>
      <a class="command-button" :href="api.loginUrl(returnTo)">继续登录</a>
      <RouterLink class="text-link" :to="{ name: 'reactions' }">返回公开文件浏览</RouterLink>
    </section>
  </main>
</template>
