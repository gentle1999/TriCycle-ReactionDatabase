import { computed } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { api } from "@/api";
import type { CurrentUser } from "@/types";

const SESSION_KEY = ["session"] as const;

export function useSession() {
  const query = useQuery<CurrentUser | null>({
    queryKey: SESSION_KEY,
    queryFn: ({ signal }) => api.currentUser(signal),
    staleTime: 60_000,
    retry: (failureCount, error) => {
      // Authentication failures are a valid anonymous state, not a retryable outage.
      if (error instanceof Error && /^401\b/.test(error.message)) return false;
      return failureCount < 2;
    },
    refetchInterval: (activeQuery) => activeQuery.state.status === "error" ? 2_000 : false,
  });

  const user = computed(() => query.data.value ?? null);
  const isAuthenticated = computed(() => user.value !== null);

  function projectAccess(projectId: string) {
    return user.value?.projects.find((project) => project.project_id === projectId) ?? null;
  }

  function can(projectId: string | null, permission: string): boolean {
    if (!projectId) return false;
    return projectAccess(projectId)?.permissions.includes(permission) ?? false;
  }

  return {
    user,
    identity: computed(() => user.value?.identity ?? null),
    isAuthenticated,
    isLoading: query.isLoading,
    error: query.error,
    refresh: query.refetch,
    projectAccess,
    can,
  };
}

export { SESSION_KEY };
