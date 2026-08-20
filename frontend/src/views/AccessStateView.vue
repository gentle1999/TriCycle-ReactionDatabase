<script setup lang="ts">
import { computed } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { useSession } from "@/composables/useSession";

const route = useRoute();
const session = useSession();
const isAuthenticated = session.isAuthenticated;
const title = computed(() => String(route.meta.title ?? "页面"));
const isNotFound = computed(() => route.name === "not-found");
</script>

<template>
  <section class="state-page" aria-labelledby="state-page-title">
    <span class="eyebrow">{{ isAuthenticated ? "Workspace" : "Authentication" }}</span>
    <h1 id="state-page-title">{{ isNotFound ? "页面不存在" : title }}</h1>
    <p v-if="!isAuthenticated && route.meta.requiresAuth">请先完成身份认证后访问此页面。</p>
    <p v-else>此资源页面正在接入项目访问上下文和服务端查询。</p>
    <RouterLink v-if="!isAuthenticated && route.meta.requiresAuth" class="command-button" :to="{ name: 'login', query: { redirect: route.fullPath } }">前往登录</RouterLink>
    <RouterLink v-else class="command-button" :to="{ name: 'reactions' }">返回反应工作区</RouterLink>
  </section>
</template>
