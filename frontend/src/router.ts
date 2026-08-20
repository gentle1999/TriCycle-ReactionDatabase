import { createRouter, createWebHistory } from "vue-router";

import { queryClient } from "./queryClient";
import { SESSION_KEY } from "./composables/useSession";
import { ApiError, api } from "./api";
import WorkspaceView from "./views/WorkspaceView.vue";
import AccessStateView from "./views/AccessStateView.vue";
import GeometryView from "./views/GeometryView.vue";
import AccountView from "./views/AccountView.vue";
import ProjectsView from "./views/ProjectsView.vue";
import OrganizationsView from "./views/OrganizationsView.vue";
import NexusXView from "./views/NexusXView.vue";
import ProjectView from "./views/ProjectView.vue";
import LoginView from "./views/LoginView.vue";
import InvitationView from "./views/InvitationView.vue";

const StatisticsView = () => import("./views/StatisticsView.vue");
const TopologyDetailView = () => import("./views/TopologyDetailView.vue");
const FrameDetailView = () => import("./views/FrameDetailView.vue");
const GeometryDetailView = () => import("./views/GeometryDetailView.vue");
const ArtifactDetailView = () => import("./views/ArtifactDetailView.vue");
const ReactionDetailView = () => import("./views/ReactionDetailView.vue");
const ReactionQueryHelpView = () => import("./views/ReactionQueryHelpView.vue");
const GeometryQueryHelpView = () => import("./views/GeometryQueryHelpView.vue");
const ArtifactQueryHelpView = () => import("./views/ArtifactQueryHelpView.vue");
const UploadView = () => import("./views/UploadView.vue");

const protectedNames = new Set(["uploads", "account", "organizations", "projects", "project", "statistics", "nexusx"]);

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: { name: "reactions" } },
    { path: "/login", name: "login", component: LoginView, meta: { title: "登录" } },
    { path: "/reactions", name: "reactions", component: WorkspaceView, meta: { requiresAuth: true } },
    { path: "/help/reaction-query", name: "reaction-query-help", component: ReactionQueryHelpView, meta: { title: "反应查询帮助" } },
    { path: "/help/geometry-query", name: "geometry-query-help", component: GeometryQueryHelpView, meta: { title: "几何构象查询帮助" } },
    { path: "/help/artifact-query", name: "artifact-query-help", component: ArtifactQueryHelpView, meta: { title: "原始文件查询帮助" } },
    { path: "/reactions/:logicalReactionId", name: "reaction-detail", component: ReactionDetailView, meta: { requiresAuth: true, title: "反应路径" } },
    { path: "/mapped-reactions/:mappedReactionId", name: "mapped-reaction-detail", component: ReactionDetailView, meta: { requiresAuth: true, title: "映射反应" } },
    { path: "/geometries", name: "geometries", component: GeometryView, meta: { requiresAuth: true } },
    { path: "/geometries/:geometryId", name: "geometry-detail", component: GeometryDetailView, meta: { requiresAuth: true, title: "几何构象" } },
    { path: "/topologies/:topologyId", name: "topology-detail", component: TopologyDetailView, meta: { requiresAuth: true, title: "分子拓扑" } },
    { path: "/statistics", name: "statistics", component: StatisticsView, meta: { requiresAuth: true, title: "分布统计" } },
    // Keep the catalog consolidated, while individual frames have stable detail URLs.
    { path: "/calculations", redirect: { name: "artifacts" } },
    { path: "/calculations/:frameId", name: "calculation-detail", component: FrameDetailView, meta: { requiresAuth: true, title: "计算帧" } },
    { path: "/artifacts", name: "artifacts", component: WorkspaceView },
    { path: "/artifacts/:artifactId", name: "artifact-detail", component: ArtifactDetailView, meta: { title: "原始文件" } },
    { path: "/uploads", name: "uploads", component: UploadView, meta: { requiresAuth: true, title: "批量文件上传" } },
    // Keep prior links usable while consolidating exploration into resource-specific filters.
    { path: "/search", redirect: (to) => ({ name: "geometries", query: to.query }) },
    { path: "/account", name: "account", component: AccountView, meta: { requiresAuth: true, title: "账户" } },
    { path: "/organizations", name: "organizations", component: OrganizationsView, meta: { requiresAuth: true, title: "组织" } },
    { path: "/projects", name: "projects", component: ProjectsView, meta: { requiresAuth: true, title: "项目" } },
    { path: "/nexusx", name: "nexusx", component: NexusXView, meta: { requiresAuth: true, title: "增强接口" } },
    { path: "/projects/:projectId", name: "project", component: ProjectView, meta: { requiresAuth: true, title: "项目概览" } },
    { path: "/projects/:projectId/members", name: "project-members", component: ProjectView, meta: { requiresAuth: true, title: "项目成员" } },
    { path: "/invitations/:token", name: "invitation", component: InvitationView, meta: { requiresAuth: true, title: "项目邀请" } },
    { path: "/:pathMatch(.*)*", name: "not-found", component: AccessStateView, meta: { title: "页面不存在" } },
  ],
});

router.beforeEach(async (to) => {
  if (!protectedNames.has(String(to.name)) && !to.meta.requiresAuth) return true;
  let user: Awaited<ReturnType<typeof api.currentUser>>;
  try {
    user = await queryClient.ensureQueryData({
      queryKey: SESSION_KEY,
      queryFn: ({ signal }) => api.currentUser(signal),
      staleTime: 60_000,
    });
  } catch (error) {
    const forbidden = error instanceof ApiError && error.status === 403;
    return {
      name: "artifacts",
      query: {
        ...(forbidden ? { forbidden: "true" } : { unavailable: "true" }),
        redirect: to.fullPath,
      },
    };
  }
  if (user) {
    if (to.name === "project" || to.name === "project-members") {
      const projectId = typeof to.params.projectId === "string" ? to.params.projectId : null;
      if (!projectId || !user.projects.some((project) => project.project_id === projectId)) {
        return { name: "projects", query: { forbidden: "true", redirect: to.fullPath } };
      }
    }
    return true;
  }
  if (to.name === "reactions") {
    return { name: "artifacts", query: { login: "required", redirect: to.fullPath } };
  }
  return { name: "login", query: { redirect: to.fullPath } };
});
