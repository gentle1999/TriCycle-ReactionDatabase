<script setup lang="ts">
import { Activity, Building2, Code2, Database, ExternalLink, FolderKanban, LogIn, LogOut, RefreshCw, UserRound } from "@lucide/vue";
import { computed, onMounted, ref, watch } from "vue";
import { RouterLink, RouterView, useRoute, useRouter } from "vue-router";

import { api, apiUrl } from "@/api";
import { frontendAppName, frontendBrandName, frontendTagline } from "@/branding";
import { useProjectContext } from "@/composables/useProjectContext";
import { useSession } from "@/composables/useSession";
import { queryClient } from "@/queryClient";
import { internalRedirect, withoutAccessState } from "@/routeAccessState";
import type { HealthStatus } from "@/types";

type ViewName = "reactions" | "artifacts" | "geometry" | "statistics";

const route = useRoute();
const router = useRouter();
const session = useSession();
const projectContext = useProjectContext();
const user = session.user;
const projects = projectContext.projects;
const currentProjectId = projectContext.currentProjectId;
const projectGroups = computed(() => {
  const groups = new Map<string, { organizationId: string; organizationName: string; projects: typeof projects.value }>();
  for (const project of projects.value) {
    const group = groups.get(project.organization_id) ?? { organizationId: project.organization_id, organizationName: project.organization_name, projects: [] };
    group.projects.push(project);
    groups.set(project.organization_id, group);
  }
  return [...groups.values()].sort((a, b) => a.organizationName.localeCompare(b.organizationName));
});
const health = ref<HealthStatus | null>(null);
const healthError = ref(false);
const refreshing = ref(false);
const loggingOut = ref(false);
let recoveringAccessState = false;
const apiDocsUrl = apiUrl("/docs");
const navigationQuery = computed(() => withoutAccessState(route.query));

const activeView = computed<ViewName>(() => {
  if (route.name === "artifacts" || route.name === "artifact-detail" || route.name === "uploads") return "artifacts";
  if (route.name === "geometries" || route.name === "geometry-detail") return "geometry";
  if (route.name === "topology-detail" || route.name === "calculation-detail") return "geometry";
  if (route.name === "statistics") return "statistics";
  return "reactions";
});

const tabs = computed(() => [
  { id: "reactions" as const, label: "反应路径", route: "reactions" },
  { id: "geometry" as const, label: "几何构象", route: "geometries" },
  { id: "artifacts" as const, label: "原始文件", route: "artifacts" },
  { id: "statistics" as const, label: "分布统计", route: "statistics" },
]);

async function refreshHealth(): Promise<void> {
  try {
    health.value = await api.health();
    healthError.value = false;
  } catch {
    health.value = null;
    healthError.value = true;
  }
}

async function selectTab(view: ViewName): Promise<void> {
  const target = tabs.value.find((tab) => tab.id === view)?.route ?? "reactions";
  await router.push({ name: target, query: navigationQuery.value });
}

async function recoverRouteAfterSession(): Promise<void> {
  const unavailable = route.query.unavailable;
  const recoverable = route.name === "login" || Boolean(route.query.login) || (Boolean(unavailable) && unavailable !== "forbidden");
  if (!user.value || !recoverable || route.query.forbidden || recoveringAccessState) return;

  recoveringAccessState = true;
  try {
    const redirect = internalRedirect(route.query.redirect);
    const target = redirect ? router.resolve(redirect) : router.currentRoute.value;
    await router.replace({
      path: target.path,
      query: withoutAccessState(target.query),
      hash: target.hash,
    });
  } finally {
    recoveringAccessState = false;
  }
}

async function refreshApplication(): Promise<void> {
  refreshing.value = true;
  try {
    await Promise.all([refreshHealth(), session.refresh()]);
    const projectId = currentProjectId.value;
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["catalog", "artifacts", { projectId }] }),
      queryClient.invalidateQueries({ queryKey: ["catalog", "frames", { projectId }] }),
      queryClient.invalidateQueries({ queryKey: ["catalog", "reactions", { projectId }] }),
      queryClient.invalidateQueries({ queryKey: ["geometries"] }),
      queryClient.invalidateQueries({ queryKey: ["thermodynamic-statistics", { projectId }] }),
      ...(projectId ? [queryClient.invalidateQueries({ queryKey: ["project-overview", projectId] })] : []),
    ]);
  } finally {
    refreshing.value = false;
  }
}

async function logout(): Promise<void> {
  loggingOut.value = true;
  try {
    await api.logout();
  } finally {
    window.location.assign(api.logoutUrl(route.fullPath));
  }
}

onMounted(() => void refreshHealth());
watch(
  [user, () => route.name, () => route.query.login, () => route.query.unavailable, () => route.query.redirect],
  () => void recoverRouteAfterSession(),
  { immediate: true },
);
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <RouterLink class="brand" :to="{ name: 'reactions', query: navigationQuery }" :aria-label="`${frontendAppName}首页`">
        <span class="brand-mark" aria-hidden="true"><Database :size="21" /></span>
        <span>
          <strong>{{ frontendAppName }}</strong>
          <small>{{ frontendBrandName }} · {{ frontendTagline }}</small>
        </span>
      </RouterLink>
      <div class="topbar-actions">
        <nav class="utility-nav" aria-label="账户导航">
          <RouterLink class="utility-link" :to="{ name: 'organizations', query: navigationQuery }" title="组织">
            <Building2 :size="15" aria-hidden="true" /><span>组织</span>
          </RouterLink>
          <RouterLink class="utility-link" :to="{ name: 'projects', query: navigationQuery }" title="项目">
            <FolderKanban :size="15" aria-hidden="true" /><span>项目</span>
          </RouterLink>
          <RouterLink class="utility-link" :to="{ name: 'nexusx', query: navigationQuery }" title="增强接口">
            <Code2 :size="15" aria-hidden="true" /><span>NexusX</span>
          </RouterLink>
          <RouterLink class="utility-link" :to="{ name: 'account', query: navigationQuery }" title="账户">
            <UserRound :size="15" aria-hidden="true" /><span>账户</span>
          </RouterLink>
        </nav>
        <label v-if="projects.length" class="project-selector">
          <span>项目</span>
          <select
            :value="currentProjectId ?? ''"
            aria-label="当前项目"
            @change="projectContext.selectProject(($event.target as HTMLSelectElement).value)"
          >
            <optgroup v-for="group in projectGroups" :key="group.organizationId" :label="group.organizationName">
              <option v-for="project in group.projects" :key="project.project_id" :value="project.project_id">
                {{ project.project_name }}
              </option>
            </optgroup>
          </select>
        </label>
        <span class="current-user" :title="user?.primary_email || user?.identity.subject || '未登录'">
          <UserRound :size="14" aria-hidden="true" />
          <span>{{ user?.display_name || "匿名访问" }}</span>
        </span>
        <a v-if="!user" class="utility-link" :href="api.loginUrl(route.fullPath)" title="登录">
          <LogIn :size="15" aria-hidden="true" /><span>登录</span>
        </a>
        <button v-else class="icon-button" type="button" title="退出登录" aria-label="退出登录" :disabled="loggingOut" @click="logout">
          <LogOut :size="16" aria-hidden="true" />
        </button>
        <span class="health" :class="{ 'is-ready': health, 'is-error': healthError }">
          <Activity :size="14" aria-hidden="true" />
          <span>{{ health ? `PostgreSQL ${health.postgresql_version}` : healthError ? "数据库不可用" : "正在连接" }}</span>
        </span>
        <a class="api-link" :href="apiDocsUrl" target="_blank" rel="noreferrer">API 文档 <ExternalLink :size="13" aria-hidden="true" /></a>
        <button class="icon-button" :class="{ 'is-spinning': refreshing }" type="button" title="刷新数据" aria-label="刷新数据" :disabled="refreshing" @click="refreshApplication">
          <RefreshCw :size="18" aria-hidden="true" />
        </button>
      </div>
    </header>

    <nav class="view-tabs" aria-label="数据视图">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="view-tab"
        :class="{ 'is-active': activeView === tab.id }"
        type="button"
        @click="selectTab(tab.id)"
      >
        <span>{{ tab.label }}</span>
      </button>
    </nav>

    <RouterView />
  </div>
</template>
