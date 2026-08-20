<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import { ApiError, api } from "@/api";
import { useSession } from "@/composables/useSession";

const route = useRoute();
const router = useRouter();
const session = useSession();
const isAuthenticated = session.isAuthenticated;
const token = computed(() => typeof route.params.token === "string" ? route.params.token : "");
const busy = ref(false);
const error = ref<string | null>(null);
const accepted = ref(false);

async function accept(): Promise<void> {
  if (!token.value) return;
  busy.value = true;
  error.value = null;
  try {
    await api.acceptInvitation(token.value);
    accepted.value = true;
    await session.refresh();
    await router.push({ name: "projects" });
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "邀请接受失败，请稍后重试。";
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <main class="auth-page" aria-labelledby="invitation-title">
    <section class="state-page auth-card">
      <span class="eyebrow">Project Invitation</span>
      <h1 id="invitation-title">接受项目邀请</h1>
      <p v-if="!isAuthenticated">请先登录与邀请邮箱匹配的组织账户。</p>
      <p v-else-if="accepted">邀请已接受，项目访问已刷新。</p>
      <p v-else>接受后，你将获得邀请中指定的项目角色。</p>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <a v-if="!isAuthenticated" class="command-button" :href="api.loginUrl(route.fullPath)">先登录</a>
      <button v-else class="command-button" type="button" :disabled="busy" @click="accept">{{ busy ? "正在接受…" : "接受邀请" }}</button>
      <RouterLink class="text-link" :to="{ name: 'projects' }">返回项目</RouterLink>
    </section>
  </main>
</template>
