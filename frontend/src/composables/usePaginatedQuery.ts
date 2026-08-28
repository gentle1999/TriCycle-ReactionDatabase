import { keepPreviousData, useQuery, useQueryClient, type QueryKey } from "@tanstack/vue-query";
import { watch, type ComputedRef, type Ref } from "vue";

import type { Page } from "@/types";

const DEFAULT_PAGE_PREFETCH_RADIUS = 3;

interface PaginatedQueryOptions<T> {
  queryKey: ComputedRef<QueryKey>;
  enabled: ComputedRef<boolean>;
  offset: Ref<number>;
  fetchPage: (offset: number, signal: AbortSignal) => Promise<Page<T>>;
  queryKeyForOffset: (offset: number) => QueryKey;
  staleTime: number;
  prefetchRadius?: number;
}

/** Keep the active page visible and warm a bounded window on either side. */
export function usePaginatedQuery<T>(options: PaginatedQueryOptions<T>) {
  const queryClient = useQueryClient();
  const query = useQuery<Page<T>>({
    queryKey: options.queryKey,
    queryFn: ({ signal }) => options.fetchPage(options.offset.value, signal),
    enabled: options.enabled,
    staleTime: options.staleTime,
    placeholderData: keepPreviousData,
  });

  watch(
    [() => query.data.value, () => query.isPlaceholderData.value],
    ([result, isPlaceholderData]) => {
      if (!result || isPlaceholderData || result.page.offset !== options.offset.value) return;
      const prefetchRadius = options.prefetchRadius ?? DEFAULT_PAGE_PREFETCH_RADIUS;
      const nearbyOffsets = Array.from(
        { length: prefetchRadius * 2 + 1 },
        (_, index) => result.page.offset + (index - prefetchRadius) * result.page.limit,
      ).filter((candidate) => candidate >= 0 && candidate < result.page.total && candidate !== result.page.offset);
      for (const nearbyOffset of nearbyOffsets) {
        void queryClient.prefetchQuery({
          queryKey: options.queryKeyForOffset(nearbyOffset),
          queryFn: ({ signal }) => options.fetchPage(nearbyOffset, signal),
          staleTime: options.staleTime,
        });
      }
    },
  );

  return query;
}
