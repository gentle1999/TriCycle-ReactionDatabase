import { computed, type ComputedRef, type Ref } from "vue";
import { useQuery } from "@tanstack/vue-query";

import { api } from "@/api";
import type { CalculationFrameSummary, CurrentUser, Page } from "@/types";
import type { ArtifactSort } from "@/artifactQuery";
import type { ReactionQueryFilters, ReactionSort } from "@/reactionQuery";

import { usePaginatedQuery } from "./usePaginatedQuery";

export type CatalogView = "reactions" | "frames" | "artifacts";

interface CatalogQueryOptions {
  projectId: Ref<string | null>;
  activeView: ComputedRef<CatalogView>;
  user: ComputedRef<CurrentUser | null>;
  reactionOffset: Ref<number>;
  reactionFilters: Ref<ReactionQueryFilters>;
  reactionSort: Ref<ReactionSort>;
  artifactOffset: Ref<number>;
  artifactSort: Ref<ArtifactSort>;
  artifactFilterId: ComputedRef<string | null>;
  artifactKindFilter: ComputedRef<string | null>;
  artifactContentShaFilter: ComputedRef<string | null>;
  artifactFilenameFilter: ComputedRef<string | null>;
  artifactStorageStatusFilter: ComputedRef<string | null>;
  reactionId: Ref<string | null>;
  mappedReactionId: Ref<string | null>;
  frameId: Ref<string | null>;
  artifactId: Ref<string | null>;
  expandedArtifactId: Ref<string | null>;
}

async function loadAllArtifactFrames(
  projectId: string | undefined,
  artifactFileId: string,
  signal: AbortSignal,
): Promise<Page<CalculationFrameSummary>> {
  const limit = 200;
  const firstPage = await api.frames({ projectId, artifactFileId, limit, offset: 0 }, signal);
  const remainingOffsets = Array.from(
    { length: Math.max(0, Math.ceil(firstPage.page.total / limit) - 1) },
    (_, index) => (index + 1) * limit,
  );
  const remainingPages = await Promise.all(remainingOffsets.map((offset) =>
    api.frames({ projectId, artifactFileId, limit, offset }, signal),
  ));
  const items = [firstPage, ...remainingPages]
    .flatMap((page) => page.items)
    .sort((left, right) => left.file_frame_index - right.file_frame_index);
  return { items, page: { total: firstPage.page.total, limit: items.length, offset: 0 } };
}

interface DatabaseTotals {
  reactions: number | null;
  mappedReactions: number | null;
  geometries: number | null;
  artifacts: number | null;
  frames: number | null;
}

export function useCatalogQueries(options: CatalogQueryOptions) {
  const databaseTotals = useQuery<DatabaseTotals>({
    queryKey: computed(() => ["catalog", "totals", { projectId: options.projectId.value }]),
    queryFn: async ({ signal }) => {
      const projectId = options.projectId.value ?? undefined;
      const pageTotals = await Promise.all([
        projectId
          ? api.reactions({ projectId, limit: 1, offset: 0 }, signal).then((page) => page.page.total).catch(() => null)
          : Promise.resolve(null),
        projectId
          ? api.mappedReactions({ projectId, limit: 1, offset: 0 }, signal).then((page) => page.page.total).catch(() => null)
          : Promise.resolve(null),
        projectId
          ? api.geometries({ projectId, thermodynamicOnly: false, limit: 1, offset: 0 }, signal).then((page) => page.page.total).catch(() => null)
          : Promise.resolve(null),
        api.artifacts({ projectId, limit: 1, offset: 0 }, signal).then((page) => page.page.total).catch(() => null),
        projectId
          ? api.frames({ projectId, limit: 1, offset: 0 }, signal).then((page) => page.page.total).catch(() => null)
          : Promise.resolve(null),
      ]);
      return {
        reactions: pageTotals[0],
        mappedReactions: pageTotals[1],
        geometries: pageTotals[2],
        artifacts: pageTotals[3],
        frames: pageTotals[4],
      };
    },
    enabled: computed(() => options.projectId.value !== null || options.user.value === null),
    staleTime: 30_000,
  });

  function reactionPageQueryKey(offset: number) {
    return ["catalog", "reactions", {
      projectId: options.projectId.value,
      reactionFilters: options.reactionFilters.value,
      sort: options.reactionSort.value,
      limit: 12,
      offset,
    }] as const;
  }

  function fetchReactionPage(offset: number, signal: AbortSignal) {
    return api.reactions({
      projectId: options.projectId.value ?? undefined,
      ...options.reactionFilters.value,
      ...options.reactionSort.value,
      limit: 12,
      offset,
    }, signal);
  }

  const reactions = usePaginatedQuery({
    queryKey: computed(() => reactionPageQueryKey(options.reactionOffset.value)),
    enabled: computed(() => options.activeView.value === "reactions" && options.projectId.value !== null),
    offset: options.reactionOffset,
    fetchPage: fetchReactionPage,
    queryKeyForOffset: reactionPageQueryKey,
    staleTime: 30_000,
  });

  const mappedReaction = useQuery({
    queryKey: computed(() => ["catalog", "mapped-reaction", { projectId: options.projectId.value, id: options.mappedReactionId.value }]),
    queryFn: ({ signal }) => api.mappedReaction(options.mappedReactionId.value ?? "", { projectId: options.projectId.value ?? undefined }, signal),
    enabled: computed(() => options.activeView.value === "reactions" && options.projectId.value !== null && options.mappedReactionId.value !== null),
    staleTime: 60_000,
  });

  const effectiveReactionId = computed(() =>
    options.reactionId.value ?? mappedReaction.data.value?.logical_reaction_id ?? null,
  );
  const reaction = useQuery({
    queryKey: computed(() => ["catalog", "reaction", { projectId: options.projectId.value, id: effectiveReactionId.value }]),
    queryFn: ({ signal }) => api.reaction(effectiveReactionId.value ?? "", { projectId: options.projectId.value ?? undefined }, signal),
    enabled: computed(() => options.activeView.value === "reactions" && options.projectId.value !== null && effectiveReactionId.value !== null),
    staleTime: 60_000,
  });

  const artifactFrames = useQuery({
    queryKey: computed(() => ["catalog", "artifact-frames", { projectId: options.projectId.value, artifactId: options.expandedArtifactId.value, all: true }]),
    queryFn: ({ signal }) => loadAllArtifactFrames(
      options.projectId.value ?? undefined,
      options.expandedArtifactId.value ?? "",
      signal,
    ),
    enabled: computed(() => options.activeView.value === "artifacts" && options.projectId.value !== null && options.expandedArtifactId.value !== null),
    staleTime: 30_000,
  });

  const frame = useQuery({
    queryKey: computed(() => ["catalog", "frame", { projectId: options.projectId.value, id: options.frameId.value }]),
    queryFn: ({ signal }) => api.frame(options.frameId.value ?? "", { projectId: options.projectId.value ?? undefined }, signal),
    enabled: computed(() => options.projectId.value !== null && options.frameId.value !== null),
    staleTime: 60_000,
  });

  function artifactPageQueryKey(offset: number) {
    return ["catalog", "artifacts", {
      artifactId: options.artifactFilterId.value,
      artifactKind: options.artifactKindFilter.value,
      contentSha256: options.artifactContentShaFilter.value,
      originalFilenameContains: options.artifactFilenameFilter.value,
      projectId: options.projectId.value,
      storageStatus: options.artifactStorageStatusFilter.value,
      sort: options.artifactSort.value,
      limit: 50,
      offset,
    }] as const;
  }

  function fetchArtifactPage(offset: number, signal: AbortSignal) {
    return api.artifacts({
      artifactId: options.artifactFilterId.value ?? undefined,
      artifactKind: options.artifactKindFilter.value ?? undefined,
      contentSha256: options.artifactContentShaFilter.value ?? undefined,
      originalFilenameContains: options.artifactFilenameFilter.value ?? undefined,
      projectId: options.projectId.value ?? undefined,
      storageStatus: options.artifactStorageStatusFilter.value ?? undefined,
      ...options.artifactSort.value,
      limit: 50,
      offset,
    }, signal);
  }

  const artifacts = usePaginatedQuery({
    queryKey: computed(() => artifactPageQueryKey(options.artifactOffset.value)),
    enabled: computed(() =>
      options.activeView.value === "artifacts" &&
      (options.user.value === null || options.projectId.value !== null),
    ),
    offset: options.artifactOffset,
    fetchPage: fetchArtifactPage,
    queryKeyForOffset: artifactPageQueryKey,
    staleTime: 30_000,
  });

  const artifactPreview = useQuery({
    queryKey: computed(() => ["catalog", "artifact-preview", { id: options.artifactId.value }]),
    queryFn: ({ signal }) => api.artifactPreview(options.artifactId.value ?? "", signal),
    enabled: computed(() => options.activeView.value === "artifacts" && options.artifactId.value !== null),
    staleTime: 60_000,
  });

  return {
    databaseTotals,
    reactions,
    reaction,
    mappedReaction,
    artifactFrames,
    frame,
    artifacts,
    artifactPreview,
  };
}
