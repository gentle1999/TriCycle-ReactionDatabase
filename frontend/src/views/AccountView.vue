<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { useQuery, useQueryClient } from "@tanstack/vue-query";

import { ApiError, api } from "@/api";
import { useSession } from "@/composables/useSession";
import type { AuditEventView } from "@/types";

const session = useSession();
const queryClient = useQueryClient();
const user = session.user;
const projects = computed(() => user.value?.projects ?? []);
const profile = ref({ display_name: "", primary_email: "" });
const profileMessage = ref<string | null>(null);
const profileError = ref<string | null>(null);
const busy = ref(false);
watch(user, (value) => {
  if (value) profile.value = { display_name: value.display_name, primary_email: value.primary_email ?? "" };
}, { immediate: true });

const organizationQuery = useQuery({
  queryKey: ["organizations"],
  queryFn: ({ signal }) => api.organizations(signal),
  enabled: computed(() => user.value !== null),
  staleTime: 30_000,
});

const organizations = computed(() => {
  const groups = new Map<string, { id: string; name: string; role: string | null; projects: typeof projects.value }>();
  for (const organization of organizationQuery.data.value ?? []) {
    groups.set(organization.id, {
      id: organization.id,
      name: organization.name,
      role: organization.role,
      projects: [],
    });
  }
  for (const project of projects.value) {
    const group = groups.get(project.organization_id) ?? {
      id: project.organization_id,
      name: project.organization_name,
      role: project.organization_role,
      projects: [],
    };
    group.projects.push(project);
    groups.set(project.organization_id, group);
  }
  return [...groups.values()];
});
const sessions = useQuery({ queryKey: ["account-sessions"], queryFn: ({ signal }) => api.sessions(signal), enabled: computed(() => user.value !== null), staleTime: 15_000 });
const mcpTokens = useQuery({ queryKey: ["account-mcp-tokens"], queryFn: ({ signal }) => api.mcpTokens(signal), enabled: computed(() => user.value !== null), staleTime: 15_000 });
const audit = useQuery({ queryKey: ["account-audit"], queryFn: ({ signal }) => api.accountAudit({}, signal), enabled: computed(() => user.value !== null), staleTime: 15_000 });
const mcpTokenName = ref("MCP client");
const createdMcpToken = ref<string | null>(null);
const mcpTokenMessage = ref<string | null>(null);
const mcpTokenError = ref<string | null>(null);
const mcpTokenBusy = ref(false);

async function saveProfile(): Promise<void> {
  busy.value = true;
  profileError.value = null;
  profileMessage.value = null;
  try {
    const updated = await api.updateProfile({ display_name: profile.value.display_name.trim() });
    if (updated) queryClient.setQueryData(["session"], updated);
    profileMessage.value = "账户资料已更新。";
  } catch (error) {
    profileError.value = error instanceof ApiError ? error.message : "账户资料更新失败。";
  } finally {
    busy.value = false;
  }
}

async function revoke(id: string): Promise<void> {
  await api.revokeSession(id);
  await sessions.refetch();
}

async function revokeAll(): Promise<void> {
  if (!window.confirm("撤销其他设备的登录会话？当前会话会保留。")) return;
  await api.revokeAllSessions();
  await sessions.refetch();
}

async function copyText(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard unavailable");
}

async function createMcpToken(): Promise<void> {
  mcpTokenBusy.value = true;
  mcpTokenMessage.value = null;
  mcpTokenError.value = null;
  try {
    const result = await api.createMcpToken({ name: mcpTokenName.value.trim() || "MCP client" });
    if (!result) throw new Error("MCP token response was empty");
    createdMcpToken.value = result.access_token;
    await mcpTokens.refetch();
    mcpTokenMessage.value = "Token 只会在这里显示一次，请立即完成客户端配置。";
  } catch (error) {
    mcpTokenError.value = error instanceof ApiError ? error.message : "MCP token 创建失败。";
  } finally {
    mcpTokenBusy.value = false;
  }
}

async function revokeMcpToken(id: string): Promise<void> {
  if (!window.confirm("撤销这个 MCP Token？已配置的客户端将立即无法访问。")) return;
  await api.revokeMcpToken(id);
  await mcpTokens.refetch();
  createdMcpToken.value = null;
}

async function copyCreatedMcpToken(): Promise<void> {
  if (!createdMcpToken.value) return;
  try {
    await copyText(createdMcpToken.value);
    mcpTokenMessage.value = "Token 已复制。";
  } catch {
    mcpTokenError.value = "浏览器不允许自动复制，请手动选择 token。";
  }
}

const auditLabels: Record<string, string> = {
  "auth.login": "登录账户",
  "auth.logout": "退出账户",
  "auth.session.revoked": "撤销登录会话",
  "auth.sessions.revoked_all": "撤销其他会话",
  "auth.mcp_token.created": "创建 MCP Token",
  "auth.mcp_token.revoked": "撤销 MCP Token",
  "account.profile_updated": "更新账户资料",
  "organization.created": "创建组织",
};

function auditLabel(action: string): string {
  return auditLabels[action] ?? action;
}

function auditDetail(event: AuditEventView): string {
  const metadata = event.metadata_json;
  const values = [metadata.display_name, metadata.name, metadata.slug]
    .filter((value): value is string => typeof value === "string" && value.length > 0);
  return values.join(" · ") || event.entity_type;
}
</script>

<template>
  <main class="account-page" aria-labelledby="account-page-title">
    <header class="page-heading"><span class="eyebrow">Account</span><h1 id="account-page-title">账户与访问</h1><p>个人资料、登录会话和访问审计。</p><RouterLink class="text-link" :to="{ name: 'organizations' }">管理组织与项目</RouterLink></header>
    <section v-if="user" class="account-grid">
      <article class="state-page account-card"><span class="eyebrow">Profile</span><h2>{{ user.display_name }}</h2><form class="stack-form" @submit.prevent="saveProfile"><label>显示名称<input v-model="profile.display_name" required maxlength="512" /></label><label>邮箱（由身份提供方管理）<input v-model="profile.primary_email" type="email" readonly /></label><button class="command-button" type="submit" :disabled="busy">{{ busy ? "保存中…" : "保存资料" }}</button><p v-if="profileMessage" class="success-text">{{ profileMessage }}</p><p v-if="profileError" class="error-text" role="alert">{{ profileError }}</p></form><dl class="account-facts"><div><dt>身份提供方</dt><dd>{{ user.identity.issuer }}</dd></div><div><dt>项目访问</dt><dd>{{ projects.length }} 个项目</dd></div></dl></article>
      <article v-for="organization in organizations" :key="organization.id" class="account-card"><span class="eyebrow">Organization</span><h2>{{ organization.name }}</h2><p class="muted-text">{{ organization.role || "项目成员" }}</p><ul class="project-summary-list"><li v-for="project in organization.projects" :key="project.project_id"><RouterLink :to="{ name: 'project', params: { projectId: project.project_id }, query: { project_id: project.project_id } }"><strong>{{ project.project_name }}</strong><span>{{ project.project_role || project.organization_role || "只读访问" }}</span></RouterLink></li></ul><p v-if="!organization.projects.length" class="muted-text">暂时没有项目。</p></article>
    </section>
    <section v-else class="state-page"><h2>需要身份认证</h2><p>当前会话没有可展示的账户信息。</p></section>
    <section v-if="user" class="management-section"><header class="section-heading"><div><span class="eyebrow">Sessions</span><h2>登录会话</h2></div><button class="text-button danger" type="button" @click="revokeAll">撤销其他会话</button></header><div class="data-table"><div v-for="item in sessions.data.value ?? []" :key="item.id" class="data-row"><div><strong>{{ item.current ? "当前浏览器" : (item.user_agent || "未知设备") }}</strong><small>{{ item.ip_address || "未知 IP" }} · 最近访问 {{ new Date(item.last_seen_at).toLocaleString() }}</small></div><span v-if="item.current" class="status-text">当前</span><button v-else class="text-button danger" type="button" @click="revoke(item.id)">撤销</button></div></div></section>
    <section v-if="user" class="management-section">
      <header class="section-heading">
        <div><span class="eyebrow">MCP Access</span><h2>MCP 访问令牌</h2></div>
        <RouterLink class="text-link" :to="{ name: 'nexusx' }">打开增强接口</RouterLink>
      </header>
      <p class="muted-text">给 Claude、Cursor、Cline 等外部 MCP 客户端使用。数据库只保存哈希，原文只在创建后显示一次。</p>
      <form class="inline-actions mcp-account-create" @submit.prevent="createMcpToken">
        <label>Token 名称<input v-model="mcpTokenName" maxlength="128" /></label>
        <button class="command-button" type="submit" :disabled="mcpTokenBusy">{{ mcpTokenBusy ? "生成中…" : "生成 Token" }}</button>
      </form>
      <div v-if="createdMcpToken" class="invite-result mcp-account-token-result">
        <strong>本次生成的 Token</strong>
        <code>{{ createdMcpToken }}</code>
        <div class="inline-actions"><button class="command-button" type="button" @click="copyCreatedMcpToken">复制 Token</button><span>配置格式：<code>Authorization: Bearer {{ createdMcpToken }}</code></span></div>
      </div>
      <p v-if="mcpTokenMessage" class="success-text">{{ mcpTokenMessage }}</p>
      <p v-if="mcpTokenError" class="error-text" role="alert">{{ mcpTokenError }}</p>
      <div class="data-table mcp-account-token-list">
        <div v-for="item in mcpTokens.data.value ?? []" :key="item.id" class="data-row">
          <div><strong>{{ item.name }}</strong><small>到期 {{ new Date(item.expires_at).toLocaleString() }}<span v-if="item.last_used_at"> · 最近使用 {{ new Date(item.last_used_at).toLocaleString() }}</span></small></div>
          <span class="status-text">有效</span>
          <button class="text-button danger" type="button" @click="revokeMcpToken(item.id)">撤销</button>
        </div>
        <p v-if="!(mcpTokens.data.value ?? []).length" class="muted-text">尚未生成 MCP Token。</p>
      </div>
    </section>
    <section v-if="user" class="management-section"><header class="section-heading"><div><span class="eyebrow">Audit</span><h2>账户审计</h2></div></header><div v-if="!(audit.data.value ?? []).length" class="muted-text">暂无账户审计记录。</div><div v-else class="audit-list"><div v-for="event in audit.data.value" :key="event.id"><div><strong>{{ auditLabel(event.action) }}</strong><small>{{ auditDetail(event) }}</small></div><span>{{ event.created_at ? new Date(event.created_at).toLocaleString() : "" }}</span></div></div></section>
  </main>
</template>
