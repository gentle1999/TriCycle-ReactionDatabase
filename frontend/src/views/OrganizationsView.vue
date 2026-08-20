<script setup lang="ts">
import { ArrowRight, Building2, FolderKanban, Plus, X } from "@lucide/vue";
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";

import { ApiError, api } from "@/api";
import type { OrganizationAccessView, ProjectView } from "@/types";

const queryClient = useQueryClient();
const showCreate = ref(false);
const formError = ref<string | null>(null);
const createForm = ref({ slug: "", name: "" });

const organizationQuery = useQuery({
  queryKey: ["organizations"],
  queryFn: ({ signal }) => api.organizations(signal),
  staleTime: 30_000,
});
const projectQuery = useQuery({
  queryKey: ["projects", true],
  queryFn: ({ signal }) => api.projects(true, signal),
  staleTime: 15_000,
});
const organizations = computed(() => organizationQuery.data.value ?? []);
const projects = computed(() => projectQuery.data.value ?? []);

type OrganizationCard = OrganizationAccessView & { projects: ProjectView[]; activeCount: number; archivedCount: number };
const organizationCards = computed<OrganizationCard[]>(() => {
  const cards = new Map<string, OrganizationCard>();
  for (const organization of organizations.value) {
    cards.set(organization.id, { ...organization, projects: [], activeCount: 0, archivedCount: 0 });
  }
  for (const project of projects.value) {
    const card = cards.get(project.organization_id);
    if (!card) continue;
    card.projects.push(project);
    if (project.status === "archived") card.archivedCount += 1;
    else card.activeCount += 1;
  }
  return [...cards.values()].sort((a, b) => a.name.localeCompare(b.name));
});

function invalidate(): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["organizations"] }),
    queryClient.invalidateQueries({ queryKey: ["projects"] }),
    queryClient.invalidateQueries({ queryKey: ["session"] }),
  ]).then(() => undefined);
}

const createMutation = useMutation({
  mutationFn: () => api.createOrganization(createForm.value),
  onSuccess: async () => {
    createForm.value = { slug: "", name: "" };
    showCreate.value = false;
    formError.value = null;
    await invalidate();
  },
  onError: (error) => {
    formError.value = error instanceof ApiError ? error.message : "组织创建失败。";
  },
});

function toggleCreate(): void {
  showCreate.value = !showCreate.value;
  formError.value = null;
}

function roleLabel(role: string | null): string {
  return role === "owner" ? "所有者" : role === "admin" ? "管理员" : "成员";
}

function projectRoleLabel(project: ProjectView): string {
  const role = project.role || project.organization_role;
  return role === "owner" ? "组织所有者" : role === "admin" ? "组织管理员" : role === "manager" ? "项目管理员" : role === "contributor" ? "贡献者" : "查看者";
}
</script>

<template>
  <main class="organizations-page" aria-labelledby="organizations-page-title">
    <header class="page-heading">
      <span class="eyebrow">Organizations</span>
      <h1 id="organizations-page-title">组织</h1>
      <p>组织是项目的容器和协作边界。先选择组织，再在其中创建和管理项目。</p>
    </header>

    <section class="organization-create-callout" aria-labelledby="organization-create-title">
      <div>
        <span class="eyebrow">Workspace setup</span>
        <h2 id="organization-create-title">创建一个新组织</h2>
        <p>为课题组、团队或独立研究空间建立一个清晰的项目边界。</p>
      </div>
      <button class="command-button" type="button" @click="toggleCreate"><X v-if="showCreate" :size="15" aria-hidden="true" /><Plus v-else :size="15" aria-hidden="true" />{{ showCreate ? "取消创建" : "新建组织" }}</button>
    </section>

    <form v-if="showCreate" class="management-form organization-create-form" @submit.prevent="createMutation.mutate()">
      <div class="form-grid">
        <label>组织标识<input v-model="createForm.slug" required pattern="[a-z0-9]+(?:-[a-z0-9]+)*" placeholder="research-group" /></label>
        <label>组织名称<input v-model="createForm.name" required placeholder="课题组或团队名称" /></label>
        <button class="command-button" type="submit" :disabled="createMutation.isPending.value"><Plus :size="15" aria-hidden="true" />{{ createMutation.isPending.value ? "创建中…" : "创建组织" }}</button>
      </div>
    </form>
    <p v-if="formError" class="error-text" role="alert">{{ formError }}</p>

    <section v-if="organizationQuery.isLoading.value || projectQuery.isLoading.value" class="state-page"><p>正在加载组织…</p></section>
    <section v-else-if="organizationQuery.error.value" class="state-page" role="alert"><h2>组织列表不可用</h2><p>暂时无法读取组织访问信息，请刷新后重试。</p></section>
    <section v-else-if="projectQuery.error.value" class="state-page" role="alert"><h2>项目汇总不可用</h2><p>组织仍可访问，但项目数量暂时无法读取，请刷新后重试。</p></section>
    <section v-else-if="!organizationCards.length" class="state-page"><h2>还没有组织</h2><p>创建组织后，就可以在其中建立项目并邀请协作者。</p><button class="command-button" type="button" @click="showCreate = true"><Plus :size="15" aria-hidden="true" />创建第一个组织</button></section>
    <section v-else class="organization-card-grid">
      <article v-for="organization in organizationCards" :key="organization.id" class="organization-card">
        <header class="organization-card-heading">
          <span class="organization-icon" aria-hidden="true"><Building2 :size="19" /></span>
          <div><h2>{{ organization.name }}</h2><code>{{ organization.slug }}</code></div>
          <span class="role-pill">{{ roleLabel(organization.role) }}</span>
        </header>
        <div class="organization-facts"><div><span>活跃项目</span><strong>{{ organization.activeCount }}</strong></div><div><span>已归档</span><strong>{{ organization.archivedCount }}</strong></div><div><span>创建项目</span><strong>{{ organization.can_create_projects ? "允许" : "无" }}</strong></div></div>
        <ul v-if="organization.projects.length" class="organization-project-list">
          <li v-for="project in organization.projects.slice(0, 4)" :key="project.id"><RouterLink :to="{ name: 'project', params: { projectId: project.id }, query: { project_id: project.id } }"><span><strong>{{ project.name }}</strong><small>{{ project.status === "archived" ? "已归档" : projectRoleLabel(project) }}</small></span><ArrowRight :size="15" aria-hidden="true" /></RouterLink></li>
        </ul>
        <p v-else class="muted-text">该组织还没有项目。</p>
        <footer class="organization-card-actions"><RouterLink class="text-button" :to="{ name: 'projects', query: { organization_id: organization.id } }"><FolderKanban :size="14" aria-hidden="true" />查看项目<span v-if="organization.projects.length > 4">（{{ organization.projects.length }}）</span></RouterLink><RouterLink v-if="organization.can_create_projects" class="text-button" :to="{ name: 'projects', query: { organization_id: organization.id, create: 'true' } }"><Plus :size="14" aria-hidden="true" />新建项目</RouterLink></footer>
      </article>
    </section>
  </main>
</template>
