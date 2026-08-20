import { computed, type Ref } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { api } from "@/api";
import type { GeometryQueryFilters } from "@/geometryQuery";

export function useGeometryQueries(
  projectId: Ref<string | null>,
  geometryId: Ref<string | null>,
  offset: Ref<number>,
  topologySmiles: Ref<string> = computed(() => ""),
  advancedFilters: Ref<GeometryQueryFilters | null> = computed(() => null),
  thermodynamicOnly: Ref<boolean> = computed(() => true),
) {
  const activeFilters = computed<GeometryQueryFilters>(() => {
    if (advancedFilters.value) {
      return { projectId: projectId.value ?? undefined, ...advancedFilters.value };
    }
    const smiles = topologySmiles.value.trim();
    return {
      projectId: projectId.value ?? undefined,
      topologySmiles: smiles || undefined,
      thermodynamicOnly: thermodynamicOnly.value,
    };
  });
  const list = useQuery({
    queryKey: computed(() => ["geometries", { filters: activeFilters.value, offset: offset.value, limit: 50 }]),
    queryFn: ({ signal }) =>
      api.geometries(
        {
          ...activeFilters.value,
          limit: 50,
          offset: offset.value,
        },
        signal,
      ),
    enabled: computed(() => projectId.value !== null),
    staleTime: 30_000,
  });
  const detail = useQuery({
    queryKey: computed(() => ["geometry", { projectId: projectId.value, geometryId: geometryId.value }]),
    queryFn: ({ signal }) =>
      api.geometry(
        geometryId.value ?? "",
        { projectId: projectId.value ?? undefined },
        signal,
      ),
    enabled: computed(() => projectId.value !== null && geometryId.value !== null),
    staleTime: 60_000,
  });

  return { list, detail };
}
