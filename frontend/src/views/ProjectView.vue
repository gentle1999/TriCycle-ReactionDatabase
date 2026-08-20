<script setup lang="ts">
import { Check, Copy, MailPlus, RefreshCw, Search, UserPlus, X } from "@lucide/vue";
import { computed, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";

import { ApiError, api } from "@/api";
import { useSession } from "@/composables/useSession";
import type { AuditEventView } from "@/types";

const route = useRoute();
const session = useSession();
const queryClient = useQueryClient();
const projectId = computed(() => typeof route.params.projectId === "string" ? route.params.projectId : null);
const project = computed(() => session.projectAccess(projectId.value ?? ""));
const enabled = computed(() => projectId.value !== null && project.value !== null);
const isMembersRoute = computed(() => route.name === "project-members");
const canManage = computed(() => project.value?.permissions.includes("project:manage") ?? false);
const formError = ref<string | null>(null);
const inviteForm = ref({ email: "", role: "viewer", expires_in_days: 7 });
const createdInviteUrl = ref<string | null>(null);
const copyMessage = ref<string | null>(null);
const memberRole = ref<Record<string, string>>({});
const showAddMember = ref(false);
const memberSearch = ref("");
const addMemberForm = ref({ user_id: "", role: "viewer" });
const invitationFilter = ref("all");

const reactions = useQuery({
  queryKey: computed(() => ["project-overview", projectId.value, "reactions"]),
  queryFn: ({ signal }) => api.reactions({ projectId: projectId.value ?? undefined, limit: 1, offset: 0 }, signal),
  enabled,
  staleTime: 30_000,
});
const frames = useQuery({
  queryKey: computed(() => ["project-overview", projectId.value, "frames"]),
  queryFn: ({ signal }) => api.frames({ projectId: projectId.value ?? undefined, limit: 1, offset: 0 }, signal),
  enabled,
  staleTime: 30_000,
});
const artifacts = useQuery({
  queryKey: computed(() => ["project-overview", projectId.value, "artifacts"]),
  queryFn: ({ signal }) => api.artifacts({ projectId: projectId.value ?? undefined, limit: 1, offset: 0 }, signal),
  enabled,
  staleTime: 30_000,
});
const members = useQuery({
  queryKey: computed(() => ["project-members", projectId.value]),
  queryFn: ({ signal }) => api.projectMembers(projectId.value ?? "", signal),
  enabled: computed(() => enabled.value && isMembersRoute.value && canManage.value),
  staleTime: 15_000,
});
const userDirectory = useQuery({
  queryKey: computed(() => ["project-user-directory", projectId.value, memberSearch.value.trim()]),
  queryFn: ({ signal }) => api.users({ projectId: projectId.value ?? undefined, query: memberSearch.value.trim(), limit: 50 }, signal),
  enabled: computed(() => enabled.value && isMembersRoute.value && canManage.value && showAddMember.value),
  staleTime: 15_000,
});
const invitations = useQuery({
  queryKey: computed(() => ["project-invitations", projectId.value]),
  queryFn: ({ signal }) => api.projectInvitations(projectId.value ?? "", signal),
  enabled: computed(() => enabled.value && isMembersRoute.value && canManage.value),
  staleTime: 15_000,
});
const audit = useQuery({
  queryKey: computed(() => ["project-audit", projectId.value]),
  queryFn: ({ signal }) => api.projectAudit(projectId.value ?? "", {}, signal),
  enabled: computed(() => enabled.value && isMembersRoute.value && canManage.value),
  staleTime: 15_000,
});

function invalidateMembers(): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["project-members", projectId.value] }),
    queryClient.invalidateQueries({ queryKey: ["project-invitations", projectId.value] }),
    queryClient.invalidateQueries({ queryKey: ["project-audit", projectId.value] }),
    queryClient.invalidateQueries({ queryKey: ["session"] }),
  ]).then(() => undefined);
}

const roleMutation = useMutation({
  mutationFn: ({ userId, role }: { userId: string; role: string }) => api.updateProjectMember(projectId.value ?? "", userId, role),
  onSuccess: invalidateMembers,
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "成员角色更新失败。"; },
});
const removeMutation = useMutation({
  mutationFn: (userId: string) => api.removeProjectMember(projectId.value ?? "", userId),
  onSuccess: invalidateMembers,
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "成员移除失败。"; },
});
const inviteMutation = useMutation({
  mutationFn: () => api.createProjectInvitation(projectId.value ?? "", inviteForm.value),
  onSuccess: async (result) => {
    createdInviteUrl.value = result?.accept_url ?? null;
    copyMessage.value = null;
    inviteForm.value = { email: "", role: "viewer", expires_in_days: 7 };
    formError.value = result?.delivery_status === "failed"
      ? (result.delivery_error ?? "邀请邮件发送失败，可在邀请记录中重发。")
      : null;
    await invalidateMembers();
  },
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "邀请创建失败。"; },
});
const revokeInviteMutation = useMutation({
  mutationFn: (invitationId: string) => api.revokeProjectInvitation(projectId.value ?? "", invitationId),
  onSuccess: invalidateMembers,
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "邀请撤销失败。"; },
});
const resendInviteMutation = useMutation({
  mutationFn: (invitationId: string) => api.resendProjectInvitation(projectId.value ?? "", invitationId),
  onSuccess: async (result) => {
    createdInviteUrl.value = result?.accept_url ?? null;
    copyMessage.value = null;
    formError.value = result?.delivery_status === "failed" ? (result.delivery_error ?? "邀请邮件发送失败。") : null;
    await invalidateMembers();
  },
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "邀请重发失败。"; },
});
const addMemberMutation = useMutation({
  mutationFn: () => api.addProjectMember(projectId.value ?? "", addMemberForm.value),
  onSuccess: async () => {
    addMemberForm.value = { user_id: "", role: "viewer" };
    memberSearch.value = "";
    showAddMember.value = false;
    formError.value = null;
    await invalidateMembers();
    await queryClient.invalidateQueries({ queryKey: ["project-user-directory", projectId.value] });
  },
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "添加成员失败。"; },
});

const availableUsers = computed(() => (userDirectory.data.value?.items ?? []).filter((item) => !item.project_role && !item.is_service_account));
const visibleInvitations = computed(() => (invitations.data.value ?? []).filter((invitation) => {
  if (invitationFilter.value === "all") return true;
  if (invitationFilter.value === "accepted") return Boolean(invitation.accepted_at);
  if (invitationFilter.value === "revoked") return Boolean(invitation.revoked_at);
  if (invitationFilter.value === "expired") return !invitation.accepted_at && !invitation.revoked_at && new Date(invitation.expires_at) < new Date();
  if (invitationFilter.value === "failed") return !invitation.accepted_at && !invitation.revoked_at && invitation.delivery_status === "failed";
  return !invitation.accepted_at && !invitation.revoked_at && new Date(invitation.expires_at) >= new Date();
}));

watch(() => members.data.value, (items) => {
  for (const member of items ?? []) memberRole.value[member.user_id] = member.role;
}, { immediate: true });

function updateRole(userId: string): void {
  const role = memberRole.value[userId];
  if (role) roleMutation.mutate({ userId, role });
}

function removeMember(userId: string, name: string): void {
  if (window.confirm(`确定移除成员“${name}”？`)) removeMutation.mutate(userId);
}

function toggleAddMember(): void {
  showAddMember.value = !showAddMember.value;
  memberSearch.value = "";
  addMemberForm.value = { user_id: "", role: "viewer" };
  formError.value = null;
}

function addMember(): void {
  if (addMemberForm.value.user_id) addMemberMutation.mutate();
}

function roleLabel(role: string | null | undefined): string {
  return role === "manager" ? "管理员" : role === "contributor" ? "贡献者" : "查看者";
}

function invitationStatus(invitation: { accepted_at: string | null; revoked_at: string | null; expires_at: string; delivery_status: string }): string {
  if (invitation.accepted_at) return "已接受";
  if (invitation.revoked_at) return "已撤销";
  if (new Date(invitation.expires_at) < new Date()) return "已过期";
  if (invitation.delivery_status === "failed") return "投递失败";
  if (invitation.delivery_status === "sent") return "邮件已发送";
  return "待接受";
}

function invitationClass(invitation: { accepted_at: string | null; revoked_at: string | null; expires_at: string; delivery_status: string }): string {
  if (invitation.accepted_at) return "status-pill";
  if (invitation.revoked_at || new Date(invitation.expires_at) < new Date()) return "status-pill muted";
  if (invitation.delivery_status === "failed") return "status-pill danger";
  return "status-pill";
}

async function copyInviteUrl(): Promise<void> {
  if (!createdInviteUrl.value) return;
  copyMessage.value = null;
  try {
    await navigator.clipboard.writeText(createdInviteUrl.value);
    copyMessage.value = "邀请链接已复制。";
  } catch {
    copyMessage.value = "浏览器不允许自动复制，请手动选择链接。";
  }
}

const auditLabels: Record<string, string> = {
  "project.created": "创建项目",
  "project.updated": "更新项目",
  "project.member.added": "添加成员",
  "project.member.role_changed": "修改成员角色",
  "project.member.removed": "移除成员",
  "project.invitation.created": "创建邀请",
  "project.invitation.delivery_failed": "邀请投递失败",
  "project.invitation.accepted": "接受邀请",
  "project.invitation.revoked": "撤销邀请",
};

function auditLabel(action: string): string {
  return auditLabels[action] ?? action;
}

function auditDetail(event: AuditEventView): string {
  const metadata = event.metadata_json;
  const values = [metadata.name, metadata.email, metadata.role, metadata.status]
    .filter((value): value is string => typeof value === "string" && value.length > 0);
  return values.join(" · ") || event.entity_type;
}
</script>

<template>
  <main class="project-page" aria-labelledby="project-page-title">
    <section v-if="!project" class="state-page" role="alert">
      <span class="eyebrow">Access denied</span><h1 id="project-page-title">项目不可访问</h1><p>当前会话没有该项目的访问记录，或项目已被归档。</p><RouterLink class="command-button" :to="{ name: 'projects' }">返回项目</RouterLink>
    </section>
    <template v-else>
      <header class="page-heading"><span class="eyebrow">{{ project.organization_name }}</span><h1 id="project-page-title">{{ project.project_name }}</h1><p>{{ project.project_slug }} · {{ project.project_role || project.organization_role || "只读访问" }}</p></header>
      <nav class="project-links" aria-label="项目导航"><RouterLink :to="{ name: 'project', params: { projectId: project.project_id }, query: { project_id: project.project_id } }">概览</RouterLink><RouterLink v-if="canManage" :to="{ name: 'project-members', params: { projectId: project.project_id }, query: { project_id: project.project_id } }">成员与邀请</RouterLink></nav>

      <template v-if="!isMembersRoute">
        <section class="project-facts" aria-label="项目统计"><div><span>逻辑反应</span><strong>{{ reactions.data.value?.page.total ?? "—" }}</strong></div><div><span>计算帧</span><strong>{{ frames.data.value?.page.total ?? "—" }}</strong></div><div><span>原始文件</span><strong>{{ artifacts.data.value?.page.total ?? "—" }}</strong></div></section>
        <section class="project-permissions"><header class="section-heading"><div><span class="eyebrow">Capabilities</span><h2>当前权限</h2></div></header><div class="permission-list"><span v-for="permission in project.permissions" :key="permission">{{ permission }}</span></div></section>
      </template>

      <template v-else-if="!canManage">
        <section class="state-page project-blocked"><h2>没有成员管理权限</h2><p>成员和邀请只对项目 manager 或组织 admin/owner 开放。</p></section>
      </template>
      <template v-else>
        <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>
        <section class="management-form invite-form-panel">
          <div class="form-panel-heading"><div><span class="eyebrow">Invite</span><h2>邀请新成员</h2><p>受邀者登录后可通过一次性链接加入此项目。</p></div><MailPlus :size="20" aria-hidden="true" /></div>
          <form class="form-grid" @submit.prevent="inviteMutation.mutate()">
            <label>邮箱<input v-model="inviteForm.email" type="email" required placeholder="name@example.com" /></label>
            <label>角色<select v-model="inviteForm.role"><option value="viewer">查看者</option><option value="contributor">贡献者</option><option value="manager">管理员</option></select></label>
            <label>有效期（天）<input v-model.number="inviteForm.expires_in_days" type="number" min="1" max="30" required /></label>
            <button class="command-button" type="submit" :disabled="inviteMutation.isPending.value"><MailPlus :size="15" aria-hidden="true" />{{ inviteMutation.isPending.value ? "生成中…" : "生成邀请链接" }}</button>
          </form>
          <div v-if="createdInviteUrl" class="invite-result">
            <strong>邀请链接已生成</strong>
            <div class="invite-link-row"><code>{{ createdInviteUrl }}</code><button class="icon-button" type="button" title="复制邀请链接" aria-label="复制邀请链接" @click="copyInviteUrl"><Check v-if="copyMessage === '邀请链接已复制。'" :size="15" aria-hidden="true" /><Copy v-else :size="15" aria-hidden="true" /></button></div>
            <p v-if="copyMessage" class="success-text">{{ copyMessage }}</p>
          </div>
        </section>

        <section class="management-section">
          <header class="section-heading"><div><span class="eyebrow">Members</span><h2>项目成员</h2></div><div class="section-heading-actions"><span>{{ members.data.value?.length ?? "—" }} 人</span><button class="text-button" type="button" @click="toggleAddMember"><X v-if="showAddMember" :size="14" aria-hidden="true" /><UserPlus v-else :size="14" aria-hidden="true" />{{ showAddMember ? "取消添加" : "添加已有用户" }}</button></div></header>
          <div v-if="showAddMember" class="add-member-panel">
            <div class="add-member-search"><Search :size="15" aria-hidden="true" /><label for="member-directory-search">搜索已登录用户</label><input id="member-directory-search" v-model="memberSearch" type="search" placeholder="姓名或邮箱" /></div>
            <div v-if="userDirectory.isLoading.value" class="muted-text">正在查找用户…</div>
            <div v-else-if="userDirectory.error.value" class="error-text" role="alert">用户目录暂时不可用，请稍后重试。</div>
            <div v-else-if="!availableUsers.length" class="muted-text">没有找到可添加的用户。已是成员的用户不会重复显示。</div>
            <form v-else class="add-member-controls" @submit.prevent="addMember">
              <label for="project-member-user">用户<select id="project-member-user" v-model="addMemberForm.user_id" aria-label="添加成员用户" required><option value="" disabled>选择用户</option><option v-for="item in availableUsers" :key="item.id" :value="item.id">{{ item.display_name }}{{ item.primary_email ? ` · ${item.primary_email}` : "" }}</option></select></label>
              <label>角色<select v-model="addMemberForm.role"><option value="viewer">查看者</option><option value="contributor">贡献者</option><option value="manager">管理员</option></select></label>
              <button class="command-button" type="submit" :disabled="addMemberMutation.isPending.value || !addMemberForm.user_id"><UserPlus :size="15" aria-hidden="true" />{{ addMemberMutation.isPending.value ? "添加中…" : "添加成员" }}</button>
            </form>
          </div>
          <div v-if="members.isLoading.value" class="muted-text">正在加载成员…</div>
          <div v-else-if="members.error.value" class="error-text" role="alert">成员列表暂时不可用，请刷新重试。</div>
          <div v-else-if="!(members.data.value ?? []).length" class="muted-text">暂无项目成员。</div>
          <div v-else class="data-table"><div v-for="member in members.data.value ?? []" :key="member.user_id" class="data-row"><div><strong>{{ member.display_name }}</strong><small>{{ member.primary_email || "未提供邮箱" }} · {{ roleLabel(member.role) }}</small></div><select v-model="memberRole[member.user_id]" :aria-label="`${member.display_name} 的角色`" @change="updateRole(member.user_id)"><option value="viewer">查看者</option><option value="contributor">贡献者</option><option value="manager">管理员</option></select><button class="text-button danger" type="button" :disabled="removeMutation.isPending.value" @click="removeMember(member.user_id, member.display_name)">移除</button></div></div>
        </section>

        <section class="management-section">
          <header class="section-heading"><div><span class="eyebrow">Invitations</span><h2>邀请记录</h2></div><label class="compact-filter"><span class="sr-only">邀请状态</span><select v-model="invitationFilter" aria-label="邀请状态"><option value="all">全部状态</option><option value="active">待接受</option><option value="failed">投递失败</option><option value="expired">已过期</option><option value="accepted">已接受</option><option value="revoked">已撤销</option></select></label></header>
          <div v-if="invitations.isLoading.value" class="muted-text">正在加载邀请记录…</div>
          <div v-else-if="invitations.error.value" class="error-text" role="alert">邀请记录暂时不可用，请刷新重试。</div>
          <div v-else-if="!visibleInvitations.length" class="muted-text">当前筛选下没有邀请记录。</div>
          <div v-else class="data-table"><div v-for="invitation in visibleInvitations" :key="invitation.id" class="data-row"><div><strong>{{ invitation.email }}</strong><small>{{ roleLabel(invitation.role) }} · 截止 {{ new Date(invitation.expires_at).toLocaleDateString() }} · {{ invitation.delivery_error || "" }}</small></div><span :class="invitationClass(invitation)">{{ invitationStatus(invitation) }}</span><template v-if="!invitation.accepted_at && !invitation.revoked_at"><button v-if="invitation.delivery_status === 'failed' || new Date(invitation.expires_at) < new Date()" class="text-button" type="button" :disabled="resendInviteMutation.isPending.value" @click="resendInviteMutation.mutate(invitation.id)"><RefreshCw :size="14" aria-hidden="true" />重发</button><button class="text-button danger" type="button" :disabled="revokeInviteMutation.isPending.value" @click="revokeInviteMutation.mutate(invitation.id)">撤销</button></template></div></div>
        </section>
        <section class="management-section"><header class="section-heading"><div><span class="eyebrow">Audit</span><h2>项目审计</h2></div><span>{{ audit.data.value?.length ?? "—" }} 条记录</span></header><div v-if="audit.isLoading.value" class="muted-text">正在加载审计记录…</div><div v-else-if="!(audit.data.value ?? []).length" class="muted-text">暂无项目审计记录。</div><div v-else class="audit-list"><div v-for="event in audit.data.value" :key="event.id"><div><strong>{{ auditLabel(event.action) }}</strong><small>{{ auditDetail(event) }}</small></div><span>{{ event.created_at ? new Date(event.created_at).toLocaleString() : "" }}</span></div></div></section>
      </template>
    </template>
  </main>
</template>
