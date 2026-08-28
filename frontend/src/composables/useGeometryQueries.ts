import { computed, type Ref } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { api } from "@/api";
import type { GeometryQueryFilters, GeometrySort } from "@/geometryQuery";

import { usePaginatedQuery } from "./usePaginatedQuery";

const GEOMETRY_PAGE_SIZE = 50;

function geometryPageQueryKey(filters: GeometryQueryFilters, sort: GeometrySort, offset: number) {
  return ["geometries", { filters, sort, offset, limit: GEOMETRY_PAGE_SIZE }] as const;
}

function fetchGeometryPage(
  filters: GeometryQueryFilters,
  sort: GeometrySort,
  offset: number,
  signal?: AbortSignal,
) {
  return api.geometries(
    {
      ...filters,
      ...sort,
      limit: GEOMETRY_PAGE_SIZE,
      offset,
    },
    signal,
  );
}

export function useGeometryQueries(
  projectId: Ref<string | null>,
  geometryId: Ref<string | null>,
  offset: Ref<number>,
  sort: Ref<GeometrySort>,
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
  const list = usePaginatedQuery({
    queryKey: computed(() => geometryPageQueryKey(activeFilters.value, sort.value, offset.value)),
    enabled: computed(() => projectId.value !== null),
    offset,
    fetchPage: (pageOffset, signal) =>
      fetchGeometryPage(activeFilters.value, sort.value, pageOffset, signal),
    queryKeyForOffset: (pageOffset) => geometryPageQueryKey(activeFilters.value, sort.value, pageOffset),
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
