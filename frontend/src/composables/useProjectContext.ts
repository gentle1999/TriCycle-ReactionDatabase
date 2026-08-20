import { computed, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useSession } from "./useSession";
import { withoutAccessState } from "@/routeAccessState";

const PROJECT_STORAGE_KEY = "tricycle.activeProjectId";

export function useProjectContext() {
  const route = useRoute();
  const router = useRouter();
  const session = useSession();
  const projects = computed(() => session.user.value?.projects ?? []);
  const routeProjectId = computed(() => {
    const value = route.query.project_id;
    return typeof value === "string" && value.length > 0 ? value : null;
  });
  const storedProjectId = typeof window === "undefined" ? null : window.localStorage.getItem(PROJECT_STORAGE_KEY);
  const currentProjectId = computed(() => {
    // An explicit URL scope must reach the API unchanged so unauthorized IDs produce 403.
    if (routeProjectId.value) return routeProjectId.value;
    if (storedProjectId && projects.value.some((project) => project.project_id === storedProjectId)) {
      return storedProjectId;
    }
    return projects.value[0]?.project_id ?? null;
  });
  const currentProject = computed(() =>
    projects.value.find((project) => project.project_id === currentProjectId.value) ?? null,
  );

  async function selectProject(projectId: string): Promise<void> {
    if (!projects.value.some((project) => project.project_id === projectId)) return;
    if (typeof window !== "undefined") window.localStorage.setItem(PROJECT_STORAGE_KEY, projectId);
    await router.push({
      name: route.name ?? "reactions",
      params: route.params,
      query: { ...withoutAccessState(route.query), project_id: projectId },
    });
  }

  watch(
    [projects, routeProjectId, () => route.name],
    ([available, requested]) => {
      if (!available.length || requested || !currentProjectId.value) return;
      void router.isReady().then(() => {
        const currentRoute = router.currentRoute.value;
        if (
          currentRoute.query.project_id
          || currentRoute.query.forbidden
          || currentRoute.query.login
          || currentRoute.query.unavailable
          || typeof currentRoute.name !== "string"
        ) return;
        void router.replace({
          name: currentRoute.name,
          params: currentRoute.params,
          query: { ...currentRoute.query, project_id: currentProjectId.value },
        });
      });
    },
    { immediate: true },
  );

  return {
    projects,
    currentProjectId,
    currentProject,
    selectProject,
    can: (permission: string) => session.can(currentProjectId.value, permission),
  };
}
