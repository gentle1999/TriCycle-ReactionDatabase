import type { LocationQuery, LocationQueryRaw } from "vue-router";

const ACCESS_STATE_QUERY_KEYS = new Set([
  "forbidden",
  "login",
  "redirect",
  "unavailable",
  "preview_geometry",
  "preview_mapped",
  "preview_reaction",
]);

export function withoutAccessState(query: LocationQuery): LocationQueryRaw {
  const cleaned: LocationQueryRaw = {};
  for (const [key, value] of Object.entries(query)) {
    if (!ACCESS_STATE_QUERY_KEYS.has(key)) cleaned[key] = value;
  }
  return cleaned;
}

export function internalRedirect(value: unknown): string | null {
  const candidate = Array.isArray(value) ? value[0] : value;
  return typeof candidate === "string" && candidate.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : null;
}
