<script setup lang="ts">
import { ArchiveRestore, Building2, FolderPlus, Search, X } from "@lucide/vue";
import { computed, ref } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/vue-query";

import { ApiError, api } from "@/api";
import type { OrganizationAccessView, ProjectView } from "@/types";

const queryClient = useQueryClient();
const route = useRoute();
const includeArchived = ref(false);
const searchQuery = ref("");
const selectedOrganizationId = ref(typeof route.query.organization_id === "string" ? route.query.organization_id : "all");
const showCreate = ref(route.query.create === "true");
const formError = ref<string | null>(null);
const createForm = ref({ organization_id: selectedOrganizationId.value === "all" ? "" : selectedOrganizationId.value, slug: "", name: "" });
const editingId = ref<string | null>(null);
const editForm = ref({ slug: "", name: "" });

const query = useQuery({
  queryKey: computed(() => ["projects", includeArchived.value]),
  queryFn: ({ signal }) => api.projects(includeArchived.value, signal),
  staleTime: 15_000,
});
const organizationQuery = useQuery({
  queryKey: ["organizations"],
  queryFn: ({ signal }) => api.organizations(signal),
  staleTime: 30_000,
});
const projects = computed(() => query.data.value ?? []);
const allOrganizations = computed(() => organizationQuery.data.value ?? []);
const organizations = computed(() => allOrganizations.value.filter((organization) => organization.can_create_projects));
const canCreate = computed(() => organizations.value.length > 0);
const normalizedSearch = computed(() => searchQuery.value.trim().toLocaleLowerCase());
const filteredProjects = computed(() => projects.value.filter((project) => {
  if (selectedOrganizationId.value !== "all" && project.organization_id !== selectedOrganizationId.value) return false;
  if (!normalizedSearch.value) return true;
  return [project.name, project.slug, project.organization_name]
    .some((value) => value.toLocaleLowerCase().includes(normalizedSearch.value));
}));
type OrganizationGroup = OrganizationAccessView & { projects: ProjectView[]; activeCount: number; archivedCount: number };
const organizationGroups = computed<OrganizationGroup[]>(() => {
  const groups = new Map<string, OrganizationGroup>();
  for (const organization of allOrganizations.value) {
    groups.set(organization.id, { ...organization, projects: [], activeCount: 0, archivedCount: 0 });
  }
  for (const project of filteredProjects.value) {
    const group = groups.get(project.organization_id) ?? {
      id: project.organization_id,
      slug: project.organization_slug,
      name: project.organization_name,
      status: "active",
      role: project.organization_role,
      can_create_projects: false,
      projects: [],
      activeCount: 0,
      archivedCount: 0,
    };
    group.projects.push(project);
    if (project.status === "archived") group.archivedCount += 1;
    else group.activeCount += 1;
    groups.set(project.organization_id, group);
  }
  return [...groups.values()]
    .filter((group) => selectedOrganizationId.value === "all" || group.id === selectedOrganizationId.value)
    .sort((a, b) => a.name.localeCompare(b.name));
});
const activeProjectCount = computed(() => projects.value.filter((project) => project.status !== "archived").length);
const archivedProjectCount = computed(() => projects.value.filter((project) => project.status === "archived").length);
const hasFilters = computed(() => Boolean(normalizedSearch.value) || selectedOrganizationId.value !== "all" || includeArchived.value);
const accessStatus = computed<401 | 403 | null>(() => {
  if (route.query.forbidden) return 403;
  const errors = [query.error.value, organizationQuery.error.value];
  const error = errors.find((item): item is ApiError => item instanceof ApiError);
  return error?.status === 401 ? 401 : error?.status === 403 ? 403 : null;
});

function invalidate(): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["projects"] }),
    queryClient.invalidateQueries({ queryKey: ["organizations"] }),
    queryClient.invalidateQueries({ queryKey: ["session"] }),
  ]).then(() => undefined);
}

const createMutation = useMutation({
  mutationFn: () => api.createProject(createForm.value),
  onSuccess: async () => {
    showCreate.value = false;
    createForm.value = { organization_id: "", slug: "", name: "" };
    formError.value = null;
    await invalidate();
  },
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "项目创建失败。"; },
});

const updateMutation = useMutation({
  mutationFn: ({ id, body }: { id: string; body: { slug?: string; name?: string; status?: string } }) => api.updateProject(id, body),
  onSuccess: async () => { editingId.value = null; formError.value = null; await invalidate(); },
  onError: (error) => { formError.value = error instanceof ApiError ? error.message : "项目更新失败。"; },
});

function beginEdit(project: ProjectView): void {
  editingId.value = project.id;
  editForm.value = { slug: project.slug, name: project.name };
  formError.value = null;
}

function canManage(project: ProjectView): boolean {
  return project.permissions.includes("project:manage");
}

async function archive(project: ProjectView): Promise<void> {
  if (!window.confirm(`确定归档项目“${project.name}”？归档后默认不会出现在工作区。`)) return;
  await updateMutation.mutateAsync({ id: project.id, body: { status: "archived" } });
}

async function restore(project: ProjectView): Promise<void> {
  await updateMutation.mutateAsync({ id: project.id, body: { status: "active" } });
}

function submitCreate(): void {
  if (!createForm.value.organization_id && organizations.value[0]) createForm.value.organization_id = organizations.value[0].id;
  createMutation.mutate();
}

function toggleProjectForm(): void {
  showCreate.value = !showCreate.value;
  if (showCreate.value) {
    if (!createForm.value.organization_id && organizations.value[0]) {
      createForm.value.organization_id = organizations.value[0].id;
    }
  }
  formError.value = null;
}

function submitEdit(id: string): void {
  updateMutation.mutate({ id, body: { slug: editForm.value.slug, name: editForm.value.name } });
}

function clearFilters(): void {
  searchQuery.value = "";
  selectedOrganizationId.value = "all";
  includeArchived.value = false;
}

function projectRoleLabel(project: ProjectView): string {
  const role = project.role || project.organization_role;
  return role === "owner" ? "组织所有者" : role === "admin" ? "组织管理员" : role === "manager" ? "项目管理员" : role === "contributor" ? "贡献者" : "查看者";
}
</script>

<template>
  <main class="projects-page" aria-labelledby="projects-page-title">
    <header class="page-heading">
      <span class="eyebrow">Projects</span>
      <h1 id="projects-page-title">项目</h1>
      <p>项目是数据访问和成员权限的边界。所有写操作都会在服务端再次校验权限。</p>
    </header>

    <section v-if="accessStatus" class="state-page access-state" role="alert">
      <span class="eyebrow">HTTP {{ accessStatus }}</span>
      <h2>{{ accessStatus === 401 ? "登录状态已失效" : "没有项目访问权限" }}</h2>
      <p>{{ accessStatus === 401 ? "请重新登录后继续访问项目管理。" : "当前账户没有访问该项目或执行该操作的权限。" }}</p>
      <RouterLink v-if="accessStatus === 401" class="command-button" :to="{ name: 'login', query: { redirect: route.fullPath } }">重新登录</RouterLink>
      <RouterLink v-else class="command-button" :to="{ name: 'projects' }">返回项目列表</RouterLink>
    </section>

    <section v-if="!accessStatus" class="management-toolbar" aria-label="项目操作">
      <div class="management-toolbar-main">
        <label class="management-search">
          <Search :size="15" aria-hidden="true" />
          <span class="sr-only">筛选项目</span>
          <input v-model="searchQuery" type="search" placeholder="搜索项目、标识或组织" />
        </label>
        <label class="management-filter">
          <Building2 :size="15" aria-hidden="true" />
          <span class="sr-only">按组织筛选</span>
          <select v-model="selectedOrganizationId" aria-label="按组织筛选">
            <option value="all">全部组织</option>
            <option v-for="organization in allOrganizations" :key="organization.id" :value="organization.id">{{ organization.name }}</option>
          </select>
        </label>
      </div>
      <div class="management-toolbar-actions">
        <label class="toggle-field"><input v-model="includeArchived" type="checkbox" /> 显示已归档</label>
        <button v-if="canCreate" class="command-button" type="button" @click="toggleProjectForm"><FolderPlus :size="15" aria-hidden="true" />{{ showCreate ? "取消创建项目" : "新建项目" }}</button>
        <span v-else-if="organizationQuery.isLoading.value" class="muted-text">正在加载组织…</span>
        <span v-else-if="allOrganizations.length" class="muted-text">只有组织 owner/admin 可以创建项目</span>
      </div>
    </section>

    <section v-if="!accessStatus" class="management-summary" aria-label="项目管理摘要">
      <div><span>组织</span><strong>{{ allOrganizations.length }}</strong></div>
      <div><span>活跃项目</span><strong>{{ activeProjectCount }}</strong></div>
      <div><span>已归档</span><strong>{{ archivedProjectCount }}</strong></div>
      <p v-if="hasFilters">当前显示 {{ filteredProjects.length }} 个项目<span v-if="searchQuery">，匹配“{{ searchQuery }}”</span></p>
      <button v-if="hasFilters" class="text-button" type="button" @click="clearFilters"><X :size="14" aria-hidden="true" />清除筛选</button>
    </section>

    <form v-if="!accessStatus && showCreate" class="management-form" @submit.prevent="submitCreate">
      <h2>新建项目</h2>
      <div class="form-grid">
        <label>组织<select v-model="createForm.organization_id" required><option value="" disabled>选择组织</option><option v-for="organization in organizations" :key="organization.id" :value="organization.id">{{ organization.name }}</option></select></label>
        <label>项目标识<input v-model="createForm.slug" required pattern="[a-z0-9]+(?:-[a-z0-9]+)*" placeholder="project-name" /></label>
        <label>项目名称<input v-model="createForm.name" required placeholder="项目名称" /></label>
      </div>
      <button class="command-button" type="submit" :disabled="createMutation.isPending.value">创建</button>
    </form>

    <p v-if="!accessStatus && formError" class="error-text" role="alert">{{ formError }}</p>
    <section v-if="query.isLoading.value" class="state-page"><p>正在加载项目…</p></section>
    <section v-else-if="query.error.value" class="state-page" role="alert"><h2>项目列表不可用</h2><p>{{ query.error.value }}</p></section>
    <section v-else-if="!projects.length" class="state-page"><h2>{{ allOrganizations.length ? "没有可访问项目" : "还没有组织" }}</h2><p>{{ allOrganizations.length ? "请让项目管理员邀请你加入，或在有管理权限的组织中创建项目。" : "创建组织后即可建立第一个项目，也可以通过邀请加入已有项目。" }}</p></section>
    <section v-else-if="!filteredProjects.length" class="state-page filtered-empty">
      <h2>没有匹配的项目</h2>
      <p>调整搜索词或组织筛选，或者清除筛选查看全部项目。</p>
      <button class="command-button" type="button" @click="clearFilters">清除筛选</button>
    </section>
    <section v-else class="organization-groups">
      <section v-for="organization in organizationGroups" :key="organization.id" class="organization-group">
        <header class="organization-heading">
          <div>
            <span class="eyebrow">Organization</span>
            <h2>{{ organization.name }}</h2>
            <code>{{ organization.slug }}</code>
          </div>
          <div class="organization-counts"><span>{{ organization.activeCount }} 个活跃项目</span><span v-if="organization.archivedCount">{{ organization.archivedCount }} 个已归档</span></div>
        </header>
        <div class="project-grid">
          <article v-for="project in organization.projects" :key="project.id" class="project-card" :class="{ 'is-archived': project.status === 'archived' }">
            <template v-if="editingId === project.id">
              <label>标识<input v-model="editForm.slug" required /></label>
              <label>名称<input v-model="editForm.name" required /></label>
              <div class="inline-actions"><button class="command-button" type="button" :disabled="updateMutation.isPending.value" @click="submitEdit(project.id)">保存</button><button class="text-button" type="button" @click="editingId = null">取消</button></div>
            </template>
            <template v-else>
              <RouterLink :to="{ name: 'project', params: { projectId: project.id }, query: { project_id: project.id } }"><strong>{{ project.name }}</strong><span>{{ project.slug }}</span></RouterLink>
              <div class="project-card-meta"><span class="role-pill">{{ projectRoleLabel(project) }}</span><span :class="project.status === 'archived' ? 'status-pill archived' : 'status-pill'">{{ project.status === "archived" ? "已归档" : "活跃" }}</span></div>
              <small>创建于 {{ project.created_at ? new Date(project.created_at).toLocaleDateString() : "—" }}</small>
              <div v-if="canManage(project)" class="inline-actions"><button class="text-button" type="button" @click="beginEdit(project)">编辑</button><button v-if="project.status !== 'archived'" class="text-button danger" type="button" @click="archive(project)">归档</button><button v-else class="text-button" type="button" :disabled="updateMutation.isPending.value" @click="restore(project)"><ArchiveRestore :size="14" aria-hidden="true" />恢复</button><RouterLink class="text-button" :to="{ name: 'project-members', params: { projectId: project.id }, query: { project_id: project.id } }">成员</RouterLink></div>
            </template>
          </article>
        </div>
      </section>
    </section>
  </main>
</template>
