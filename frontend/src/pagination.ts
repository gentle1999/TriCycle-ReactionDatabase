export const MIN_PAGE_SIZE = 1;
export const REACTION_PAGE_SIZE_MAX = 12;
export const CATALOG_PAGE_SIZE_MAX = 500;
export const DEFAULT_REACTION_PAGE_SIZE = 12;
export const DEFAULT_CATALOG_PAGE_SIZE = 50;

export const REACTION_PAGE_SIZE_STORAGE_KEY = "tricycle.pagination.reactions";
export const GEOMETRY_PAGE_SIZE_STORAGE_KEY = "tricycle.pagination.geometries";
export const ARTIFACT_PAGE_SIZE_STORAGE_KEY = "tricycle.pagination.artifacts";

function boundedFallback(fallback: number, max: number): number {
  if (!Number.isFinite(fallback)) return Math.min(MIN_PAGE_SIZE, max);
  return Math.min(max, Math.max(MIN_PAGE_SIZE, Math.trunc(fallback)));
}

/** Normalize a user-controlled page size without allowing an invalid request. */
export function normalizePageSize(value: unknown, fallback: number, max: number): number {
  const safeMax = Number.isFinite(max) ? Math.max(MIN_PAGE_SIZE, Math.trunc(max)) : MIN_PAGE_SIZE;
  const safeFallback = boundedFallback(fallback, safeMax);
  if (value === null || value === undefined || (typeof value === "string" && !value.trim())) {
    return safeFallback;
  }
  const candidate = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(candidate)) return safeFallback;
  return Math.min(safeMax, Math.max(MIN_PAGE_SIZE, Math.trunc(candidate)));
}

export function loadPageSize(storageKey: string, fallback: number, max: number): number {
  if (typeof window === "undefined") return normalizePageSize(fallback, fallback, max);
  try {
    return normalizePageSize(window.localStorage.getItem(storageKey), fallback, max);
  } catch {
    return normalizePageSize(fallback, fallback, max);
  }
}

export function savePageSize(storageKey: string, value: number, max: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, String(normalizePageSize(value, value, max)));
  } catch {
    // A blocked or unavailable localStorage must not prevent pagination.
  }
}
