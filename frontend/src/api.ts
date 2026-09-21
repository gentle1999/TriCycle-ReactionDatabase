import type {
  ArtifactPreview,
  ArtifactBatchUploadAccepted,
  ArtifactSummary,
  CalculationFrameDetail,
  CalculationFrameSummary,
  CurrentUser,
  OrganizationAccessView,
  GeometryDetail,
  GeometrySummary,
  HealthStatus,
  McpAccessTokenCreateResult,
  McpAccessTokenView,
  LogicalReactionDetail,
  LogicalReactionSummary,
  MappedReactionDetail,
  MappedReactionSummary,
  MappedReactionThermodynamicStatistics,
  MolecularTopologyDetail,
  Page,
  ArtifactUploadResult,
  ArtifactUploadAccepted,
  AuditEventView,
  ReactionEnergyProfile,
  MappedReactionThermodynamics,
  ParseRevisionPage,
  ProjectInvitationCreateResult,
  ProjectInvitationView,
  ProjectMemberView,
  ProjectView,
  ScientificArrayPreview,
  TransitionStateInferenceSummary,
  SessionView,
  UserPage,
  UploadBatch,
  UploadBatchCreate,
  UploadBatchItem,
  UploadBatchItemPage,
  UploadBatchPage,
  UploadBatchStatus,
} from "./types";
import type { GeometryQueryFilters, GeometrySort } from "./geometryQuery";
import {
  reactionFilterExpression,
  type ReactionQueryFilters,
  type ReactionSort,
} from "./reactionQuery";
import type { ArtifactSort } from "./artifactQuery";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const csrfCookieName = import.meta.env.VITE_CSRF_COOKIE_NAME?.trim() || "example_csrf";
const csrfHeaderName = import.meta.env.VITE_CSRF_HEADER_NAME?.trim() || "x-csrf-token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(`${status} ${message}`);
    this.name = "ApiError";
  }
}

function retryAfterSeconds(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

export function apiUrl(path: string): string {
  return `${apiBaseUrl}${path}`;
}

function csrfHeaders(): Record<string, string> {
  const cookiePrefix = `${csrfCookieName}=`;
  const token = document.cookie
    .split("; ")
    .find((item) => item.startsWith(cookiePrefix))
    ?.slice(cookiePrefix.length);
  return token ? { [csrfHeaderName]: decodeURIComponent(token) } : {};
}

export type ArtifactBatchDownloadPhase = "preparing" | "downloading" | "complete";

export interface ArtifactBatchDownloadProgress {
  phase: ArtifactBatchDownloadPhase;
  receivedBytes: number;
  totalBytes: number | null;
}

function archiveFilename(response: Response): string {
  const disposition = response.headers.get("content-disposition") ?? "";
  const encodedFilename = disposition.match(/filename\*=(?:UTF-8'')?([^;]+)/i)?.[1];
  if (encodedFilename) {
    try {
      return decodeURIComponent(encodedFilename.trim().replace(/^"|"$/g, ""));
    } catch {
      // Fall through to the plain filename when the header is malformed.
    }
  }
  const plainFilename = disposition.match(/filename="([^"]+)"|filename=([^;]+)/i);
  return plainFilename?.[1] ?? plainFilename?.[2]?.trim() ?? "artifacts.zip";
}

function triggerBlobDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function submitArtifactBatchDownload(
  ids: string[],
  projectId: string,
  onProgress?: (progress: ArtifactBatchDownloadProgress) => void,
): Promise<void> {
  onProgress?.({ phase: "preparing", receivedBytes: 0, totalBytes: null });

  const body = new URLSearchParams();
  for (const id of ids) body.append("artifact_ids", id);
  const response = await fetch(
    apiUrl(
      `/api/artifacts/batch-download/form?project_id=${encodeURIComponent(projectId)}`,
    ),
    {
      method: "POST",
      headers: {
        accept: "application/zip",
        "content-type": "application/x-www-form-urlencoded",
        ...csrfHeaders(),
      },
      body,
      credentials: "include",
    },
  );

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // Preserve the HTTP status when the server did not return JSON.
    }
    throw new ApiError(response.status, detail);
  }

  const contentLength = response.headers.get("content-length");
  const parsedTotalBytes = contentLength === null ? Number.NaN : Number(contentLength);
  const totalBytes = Number.isFinite(parsedTotalBytes) && parsedTotalBytes >= 0
    ? parsedTotalBytes
    : null;
  onProgress?.({ phase: "downloading", receivedBytes: 0, totalBytes });

  const chunks: BlobPart[] = [];
  let receivedBytes = 0;
  if (response.body) {
    const reader = response.body.getReader();
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      if (!result.value) continue;
      chunks.push(result.value);
      receivedBytes += result.value.byteLength;
      onProgress?.({ phase: "downloading", receivedBytes, totalBytes });
    }
  } else {
    const blob = await response.blob();
    chunks.push(blob);
    receivedBytes = blob.size;
    onProgress?.({ phase: "downloading", receivedBytes, totalBytes: totalBytes ?? blob.size });
  }

  triggerBlobDownload(
    new Blob(chunks, { type: response.headers.get("content-type") ?? "application/zip" }),
    archiveFilename(response),
  );
  onProgress?.({
    phase: "complete",
    receivedBytes,
    totalBytes: totalBytes ?? receivedBytes,
  });
}

export interface ChemistryRepresentation {
  smiles: string;
  molfile: string;
}

export interface ChemistryReactionRepresentation {
  reaction_smiles: string;
  rxn: string;
}

export type ChemistryValidationKind = "smiles" | "smarts" | "rxn_smiles" | "rxn_smarts" | "mol_block" | "rxn";

export interface ChemistryValidationResult {
  kind: ChemistryValidationKind;
  valid: boolean;
  normalized: string | null;
  error: string | null;
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiUrl(path), {
    headers: { accept: "application/json" },
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // A non-JSON error still carries a useful HTTP status.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

async function requestJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
      ...csrfHeaders(),
    },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // A non-JSON error still carries a useful HTTP status.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

async function requestBlob(
  path: string,
  body: unknown,
  accept: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    headers: {
      accept,
      "content-type": "application/json",
      ...csrfHeaders(),
    },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // Preserve the HTTP status when the server did not return JSON.
    }
    throw new ApiError(response.status, detail);
  }
  return response.blob();
}

async function requestBlobJson(path: string, body: unknown, signal?: AbortSignal): Promise<Blob> {
  return requestBlob(path, body, "text/csv", signal);
}

function reactionAnalyticsPayload(options: ReactionQueryFilters): Record<string, unknown> {
  const filterExpression = reactionFilterExpression(options);
  return {
    project_id: options.projectId ?? null,
    filter_expression: filterExpression ? JSON.stringify(filterExpression) : null,
    has_activation_gibbs_free_energy: options.hasActivationGibbsFreeEnergy ?? null,
    has_reaction_gibbs_free_energy: options.hasReactionGibbsFreeEnergy ?? null,
  };
}

async function requestMutation<T>(
  path: string,
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T | undefined> {
  const response = await fetch(apiUrl(path), {
    method,
    headers: body === undefined
      ? { accept: "application/json", ...csrfHeaders() }
      : {
        accept: "application/json",
        "content-type": "application/json",
        ...csrfHeaders(),
      },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // Preserve the HTTP status when the server did not return JSON.
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined;
  return (await response.json()) as T;
}

async function requestCurrentUser(signal?: AbortSignal): Promise<CurrentUser | null> {
  const response = await fetch(apiUrl("/api/auth/me"), {
    headers: { accept: "application/json" },
    credentials: "include",
    signal,
  });
  if (response.status === 401) return null;
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return (await response.json()) as CurrentUser;
}

async function uploadArtifact(
  file: File,
  projectId: string,
  artifactKind: ArtifactUploadResult["artifact_kind"],
  signal?: AbortSignal,
): Promise<ArtifactUploadAccepted> {
  const form = new FormData();
  form.set("project_id", projectId);
  form.set("file", file);
  form.set("artifact_kind", artifactKind);
  const response = await fetch(apiUrl("/api/artifacts"), {
    method: "POST",
    headers: csrfHeaders(),
    body: form,
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // Preserve the HTTP status when the server did not return JSON.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as ArtifactUploadAccepted;
}

async function uploadArtifacts(
  files: File[],
  projectId: string,
  artifactKind: ArtifactUploadResult["artifact_kind"],
  signal?: AbortSignal,
): Promise<ArtifactBatchUploadAccepted> {
  const form = new FormData();
  form.set("project_id", projectId);
  for (const file of files) form.append("files", file);
  form.set("artifact_kind", artifactKind);
  const response = await fetch(apiUrl("/api/artifacts/batch"), {
    method: "POST",
    headers: csrfHeaders(),
    body: form,
    credentials: "include",
    signal,
  });
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return (await response.json()) as ArtifactBatchUploadAccepted;
}

function uploadBatchFile(
  batchId: string,
  clientFileId: string,
  file: File,
  onProgress: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadBatchItem> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    request.open(
      "POST",
      apiUrl(`/api/upload-batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(clientFileId)}`),
    );
    request.withCredentials = true;
    request.setRequestHeader("accept", "application/json");
    for (const [name, value] of Object.entries(csrfHeaders())) request.setRequestHeader(name, value);
    request.upload.addEventListener("progress", (event) => {
      onProgress(event.loaded, event.lengthComputable ? event.total : file.size);
    });
    request.addEventListener("load", () => {
      signal?.removeEventListener("abort", abort);
      let body: { detail?: string } | UploadBatchItem | null = null;
      try {
        body = JSON.parse(request.responseText) as { detail?: string } | UploadBatchItem;
      } catch {
        // Preserve status text when an upstream proxy does not return JSON.
      }
      if (request.status < 200 || request.status >= 300) {
        const detail = body && "detail" in body ? body.detail : request.statusText;
        reject(new ApiError(
          request.status,
          detail || "upload failed",
          retryAfterSeconds(request.getResponseHeader("retry-after")),
        ));
        return;
      }
      resolve(body as UploadBatchItem);
    });
    request.addEventListener("error", () => {
      signal?.removeEventListener("abort", abort);
      reject(new ApiError(request.status || 0, request.statusText || "network error"));
    });
    request.addEventListener("abort", () => {
      signal?.removeEventListener("abort", abort);
      reject(new DOMException("Upload aborted", "AbortError"));
    });
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) {
      abort();
      return;
    }
    const form = new FormData();
    form.set("file", file, file.name);
    request.send(form);
  });
}

function uploadBatchFiles(
  batchId: string,
  inputs: Array<{ clientFileId: string; file: File }>,
  onProgress: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<UploadBatchItem[]> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    request.open("POST", apiUrl(`/api/upload-batches/${encodeURIComponent(batchId)}/files`));
    request.withCredentials = true;
    request.setRequestHeader("accept", "application/json");
    for (const [name, value] of Object.entries(csrfHeaders())) request.setRequestHeader(name, value);
    request.upload.addEventListener("progress", (event) => {
      onProgress(
        event.loaded,
        event.lengthComputable
          ? event.total
          : inputs.reduce((total, input) => total + input.file.size, 0),
      );
    });
    request.addEventListener("load", () => {
      signal?.removeEventListener("abort", abort);
      let body: { detail?: string } | UploadBatchItem[] | null = null;
      try {
        body = JSON.parse(request.responseText) as { detail?: string } | UploadBatchItem[];
      } catch {
        // Preserve status text when an upstream proxy does not return JSON.
      }
      if (request.status < 200 || request.status >= 300) {
        const detail = body && !Array.isArray(body) ? body.detail : request.statusText;
        reject(new ApiError(
          request.status,
          detail || "upload failed",
          retryAfterSeconds(request.getResponseHeader("retry-after")),
        ));
        return;
      }
      resolve(body as UploadBatchItem[]);
    });
    request.addEventListener("error", () => {
      signal?.removeEventListener("abort", abort);
      reject(new ApiError(request.status || 0, request.statusText || "network error"));
    });
    request.addEventListener("abort", () => {
      signal?.removeEventListener("abort", abort);
      reject(new DOMException("Upload aborted", "AbortError"));
    });
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) {
      abort();
      return;
    }
    const form = new FormData();
    for (const input of inputs) {
      form.append("client_file_ids", input.clientFileId);
      form.append("files", input.file, input.file.name);
    }
    request.send(form);
  });
}

async function deleteArtifact(id: string, signal?: AbortSignal): Promise<void> {
  const response = await fetch(apiUrl(`/api/artifacts/${encodeURIComponent(id)}`), {
    method: "DELETE",
    headers: { accept: "application/json", ...csrfHeaders() },
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // Preserve the HTTP status when the server did not return JSON.
    }
    throw new ApiError(response.status, detail);
  }
}

export const api = {
  health: (signal?: AbortSignal) => request<HealthStatus>("/health/ready", signal),
  convertChemistryRepresentation: (
    body: { smiles?: string; molfile?: string },
    signal?: AbortSignal,
  ) => requestJson<ChemistryRepresentation>("/api/chemistry/representations", body, signal),
  convertChemistryReactionRepresentation: (
    body: { reaction_smiles?: string; rxn?: string },
    signal?: AbortSignal,
  ) => requestJson<ChemistryReactionRepresentation>("/api/chemistry/reactions", body, signal),
  validateChemistryRepresentation: (
    body: { kind: ChemistryValidationKind; value: string },
    signal?: AbortSignal,
  ) => requestJson<ChemistryValidationResult>("/api/chemistry/reactions/validate", body, signal),
  currentUser: (signal?: AbortSignal) => requestCurrentUser(signal),
  organizations: (signal?: AbortSignal) => request<OrganizationAccessView[]>("/api/organizations", signal),
  createOrganization: (body: { slug: string; name: string }, signal?: AbortSignal) =>
    requestMutation<OrganizationAccessView>("/api/organizations", "POST", body, signal),
  loginUrl: (returnTo = "/reactions") => apiUrl(`/api/auth/login?return_to=${encodeURIComponent(returnTo)}`),
  logoutUrl: (returnTo = "/") => apiUrl(`/api/auth/logout?return_to=${encodeURIComponent(returnTo)}`),
  logout: (signal?: AbortSignal) => requestMutation<void>("/api/auth/logout", "POST", undefined, signal),
  updateProfile: (body: { display_name: string }, signal?: AbortSignal) =>
    requestMutation<CurrentUser>("/api/auth/me", "PATCH", body, signal),
  sessions: (signal?: AbortSignal) => request<SessionView[]>("/api/auth/sessions", signal),
  mcpTokens: (signal?: AbortSignal) => request<McpAccessTokenView[]>("/api/auth/mcp-tokens", signal),
  createMcpToken: (body: { name: string }, signal?: AbortSignal) =>
    requestMutation<McpAccessTokenCreateResult>("/api/auth/mcp-tokens", "POST", body, signal),
  revokeMcpToken: (id: string, signal?: AbortSignal) =>
    requestMutation<void>(`/api/auth/mcp-tokens/${encodeURIComponent(id)}`, "DELETE", undefined, signal),
  revokeSession: (id: string, signal?: AbortSignal) =>
    requestMutation<void>(`/api/auth/sessions/${encodeURIComponent(id)}`, "DELETE", undefined, signal),
  revokeAllSessions: (signal?: AbortSignal) => requestMutation<void>("/api/auth/sessions/revoke-all", "POST", undefined, signal),
  accountAudit: (options: { limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    request<AuditEventView[]>(`/api/auth/audit?limit=${options.limit ?? 50}&offset=${options.offset ?? 0}`, signal),
  users: (options: { projectId?: string; query?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    request<UserPage>(`/api/users?${new URLSearchParams({
      ...(options.projectId ? { project_id: options.projectId } : {}),
      ...(options.query ? { q: options.query } : {}),
      limit: String(options.limit ?? 50),
      offset: String(options.offset ?? 0),
    })}`, signal),
  projects: (includeArchived = false, signal?: AbortSignal) =>
    request<ProjectView[]>(`/api/projects?include_archived=${includeArchived ? "true" : "false"}`, signal),
  createProject: (body: { organization_id: string; slug: string; name: string }, signal?: AbortSignal) =>
    requestMutation<ProjectView>("/api/projects", "POST", body, signal),
  updateProject: (id: string, body: { slug?: string; name?: string; status?: string }, signal?: AbortSignal) =>
    requestMutation<ProjectView>(`/api/projects/${encodeURIComponent(id)}`, "PATCH", body, signal),
  projectMembers: (id: string, signal?: AbortSignal) =>
    request<ProjectMemberView[]>(`/api/projects/${encodeURIComponent(id)}/members`, signal),
  addProjectMember: (id: string, body: { user_id: string; role: string }, signal?: AbortSignal) =>
    requestMutation<ProjectMemberView>(`/api/projects/${encodeURIComponent(id)}/members`, "POST", body, signal),
  updateProjectMember: (projectId: string, userId: string, role: string, signal?: AbortSignal) =>
    requestMutation<ProjectMemberView>(
      `/api/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
      "PATCH",
      { role },
      signal,
    ),
  removeProjectMember: (projectId: string, userId: string, signal?: AbortSignal) =>
    requestMutation<void>(
      `/api/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(userId)}`,
      "DELETE",
      undefined,
      signal,
    ),
  projectInvitations: (id: string, signal?: AbortSignal) =>
    request<ProjectInvitationView[]>(`/api/projects/${encodeURIComponent(id)}/invitations`, signal),
  createProjectInvitation: (id: string, body: { email: string; role: string; expires_in_days?: number }, signal?: AbortSignal) =>
    requestMutation<ProjectInvitationCreateResult>(
      `/api/projects/${encodeURIComponent(id)}/invitations`,
      "POST",
      body,
      signal,
    ),
  revokeProjectInvitation: (projectId: string, invitationId: string, signal?: AbortSignal) =>
    requestMutation<void>(
      `/api/projects/${encodeURIComponent(projectId)}/invitations/${encodeURIComponent(invitationId)}`,
      "DELETE",
      undefined,
      signal,
    ),
  resendProjectInvitation: (projectId: string, invitationId: string, signal?: AbortSignal) =>
    requestMutation<ProjectInvitationCreateResult>(
      `/api/projects/${encodeURIComponent(projectId)}/invitations/${encodeURIComponent(invitationId)}/resend`,
      "POST",
      {},
      signal,
    ),
  acceptInvitation: (token: string, signal?: AbortSignal) =>
    requestMutation<ProjectInvitationView>(`/api/auth/invitations/${encodeURIComponent(token)}/accept`, "POST", {}, signal),
  projectAudit: (id: string, options: { limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    request<AuditEventView[]>(`/api/projects/${encodeURIComponent(id)}/audit?limit=${options.limit ?? 50}&offset=${options.offset ?? 0}`, signal),
  reactions: (options: ReactionQueryFilters & Partial<ReactionSort> & { limit?: number; offset?: number } = {}, signal?: AbortSignal) => {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    if (options.filterExpression || options.hasCompatibilityEndpoints !== undefined
      || options.hasSingleEndpointFallback !== undefined || options.hasDualEndpointFallback !== undefined) {
      const filterExpression = reactionFilterExpression(options);
      return requestJson<Page<LogicalReactionSummary>>(
        "/api/logical_reaction_query_service/list_logical_reactions",
        {
          project_id: options.projectId ?? null,
          topology_id: null,
          reaction_key: null,
          label: null,
          reaction_hash: null,
          reaction_class: null,
          reaction_smarts: null,
          similarity_reaction_smiles: options.similarityReactionSmiles ?? null,
          similarity_metric: options.similarityMetric ?? "tanimoto",
          reactant_mol_block: null,
          product_mol_block: null,
          minimum_activation_gibbs_free_energy_kcal_mol: null,
          maximum_activation_gibbs_free_energy_kcal_mol: null,
          minimum_reaction_gibbs_free_energy_kcal_mol: null,
          maximum_reaction_gibbs_free_energy_kcal_mol: null,
          has_activation_gibbs_free_energy: null,
          has_reaction_gibbs_free_energy: null,
          reactant_product_changed: null,
          created_after: null,
          created_before: null,
          filter_expression: JSON.stringify(filterExpression),
          sort_by: options.sortBy ?? "default",
          sort_direction: options.sortDirection ?? "asc",
          limit,
          offset,
        },
        signal,
      );
    }
    return request<Page<LogicalReactionSummary>>(
      `/api/logical-reactions?${new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
        ...(options.projectId ? { project_id: options.projectId } : {}),
        ...(options.topologyId ? { topology_id: options.topologyId } : {}),
        ...(options.reactionClass ? { reaction_class: options.reactionClass } : {}),
        ...(options.reactionSmarts ? { reaction_smarts: options.reactionSmarts } : {}),
        ...(options.similarityReactionSmiles ? { similarity_reaction_smiles: options.similarityReactionSmiles } : {}),
        ...(options.similarityMetric ? { similarity_metric: options.similarityMetric } : {}),
        ...(options.reactantMolBlock ? { reactant_mol_block: options.reactantMolBlock } : {}),
        ...(options.productMolBlock ? { product_mol_block: options.productMolBlock } : {}),
        ...(options.minimumActivationGibbsFreeEnergyKcalMol !== undefined ? { minimum_activation_gibbs_free_energy_kcal_mol: String(options.minimumActivationGibbsFreeEnergyKcalMol) } : {}),
        ...(options.maximumActivationGibbsFreeEnergyKcalMol !== undefined ? { maximum_activation_gibbs_free_energy_kcal_mol: String(options.maximumActivationGibbsFreeEnergyKcalMol) } : {}),
        ...(options.minimumReactionGibbsFreeEnergyKcalMol !== undefined ? { minimum_reaction_gibbs_free_energy_kcal_mol: String(options.minimumReactionGibbsFreeEnergyKcalMol) } : {}),
        ...(options.maximumReactionGibbsFreeEnergyKcalMol !== undefined ? { maximum_reaction_gibbs_free_energy_kcal_mol: String(options.maximumReactionGibbsFreeEnergyKcalMol) } : {}),
        ...(options.minimumMappedReactionCount !== undefined ? { minimum_mapped_reaction_count: String(options.minimumMappedReactionCount) } : {}),
        ...(options.maximumMappedReactionCount !== undefined ? { maximum_mapped_reaction_count: String(options.maximumMappedReactionCount) } : {}),
        ...(options.hasActivationGibbsFreeEnergy ? { has_activation_gibbs_free_energy: "true" } : {}),
        ...(options.hasReactionGibbsFreeEnergy ? { has_reaction_gibbs_free_energy: "true" } : {}),
        ...(options.reactantProductChanged !== undefined ? { reactant_product_changed: String(options.reactantProductChanged) } : {}),
        ...(options.sortBy ? { sort_by: options.sortBy } : {}),
        ...(options.sortDirection ? { sort_direction: options.sortDirection } : {}),
      })}`,
      signal,
    );
  },
  mappedReactionThermodynamics: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<MappedReactionThermodynamics>(
      `/api/mapped-reactions/${encodeURIComponent(id)}/thermodynamics?${new URLSearchParams(options.projectId ? { project_id: options.projectId } : {})}`,
      signal,
    ),
  mappedReactionThermodynamicStatistics: (options: ReactionQueryFilters = {}, signal?: AbortSignal) =>
    requestJson<MappedReactionThermodynamicStatistics>(
      "/api/mapped-reactions/thermodynamics/statistics",
      reactionAnalyticsPayload(options),
      signal,
    ),
  mappedReactionThermodynamicExport: (options: ReactionQueryFilters = {}, signal?: AbortSignal) =>
    requestBlobJson(
      "/api/mapped-reactions/thermodynamics/export.csv",
      reactionAnalyticsPayload(options),
      signal,
    ),
  reaction: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<LogicalReactionDetail>(
      `/api/logical-reactions/${encodeURIComponent(id)}${options.projectId ? `?project_id=${encodeURIComponent(options.projectId)}` : ""}`,
      signal,
    ),
  mappedReaction: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<MappedReactionDetail>(
      `/api/mapped-reactions/${encodeURIComponent(id)}${options.projectId ? `?project_id=${encodeURIComponent(options.projectId)}` : ""}`,
      signal,
    ),
  mappedReactions: (options: {
    projectId?: string;
    logicalReactionId?: string;
    topologyId?: string;
    nodeRole?: string;
    minimumActivationGibbsFreeEnergyKcalMol?: number;
    maximumActivationGibbsFreeEnergyKcalMol?: number;
    minimumReactionGibbsFreeEnergyKcalMol?: number;
    maximumReactionGibbsFreeEnergyKcalMol?: number;
    reactantProductChanged?: boolean;
    limit?: number;
    offset?: number;
  } = {}, signal?: AbortSignal) =>
    request<Page<MappedReactionSummary>>(
      `/api/mapped-reactions?${new URLSearchParams({
        limit: String(options.limit ?? 50),
        offset: String(options.offset ?? 0),
        ...(options.projectId ? { project_id: options.projectId } : {}),
        ...(options.logicalReactionId ? { logical_reaction_id: options.logicalReactionId } : {}),
        ...(options.topologyId ? { topology_id: options.topologyId } : {}),
        ...(options.nodeRole ? { node_role: options.nodeRole } : {}),
        ...(options.minimumActivationGibbsFreeEnergyKcalMol !== undefined ? { minimum_activation_gibbs_free_energy_kcal_mol: String(options.minimumActivationGibbsFreeEnergyKcalMol) } : {}),
        ...(options.maximumActivationGibbsFreeEnergyKcalMol !== undefined ? { maximum_activation_gibbs_free_energy_kcal_mol: String(options.maximumActivationGibbsFreeEnergyKcalMol) } : {}),
        ...(options.minimumReactionGibbsFreeEnergyKcalMol !== undefined ? { minimum_reaction_gibbs_free_energy_kcal_mol: String(options.minimumReactionGibbsFreeEnergyKcalMol) } : {}),
        ...(options.maximumReactionGibbsFreeEnergyKcalMol !== undefined ? { maximum_reaction_gibbs_free_energy_kcal_mol: String(options.maximumReactionGibbsFreeEnergyKcalMol) } : {}),
        ...(options.reactantProductChanged !== undefined ? { reactant_product_changed: String(options.reactantProductChanged) } : {}),
      })}`,
      signal,
    ),
  transitionStateInferences: (options: {
    projectId?: string;
    artifactIngestionId?: string;
    parseRevisionId?: string;
    status?: string;
    logicalReactionId?: string;
    mappedReactionId?: string;
    calculationFrameId?: string;
    minimumImaginaryFrequencyCm1?: number;
    maximumImaginaryFrequencyCm1?: number;
    reactantProductChanged?: boolean;
    limit?: number;
    offset?: number;
  } = {}, signal?: AbortSignal) =>
    requestJson<Page<TransitionStateInferenceSummary>>(
      "/api/transition_state_inference_query_service/list_transition_state_inferences",
      {
        project_id: options.projectId ?? null,
        artifact_ingestion_id: options.artifactIngestionId ?? null,
        parse_revision_id: options.parseRevisionId ?? null,
        status: options.status ?? null,
        logical_reaction_id: options.logicalReactionId ?? null,
        mapped_reaction_id: options.mappedReactionId ?? null,
        calculation_frame_id: options.calculationFrameId ?? null,
        minimum_imaginary_frequency_cm1: options.minimumImaginaryFrequencyCm1 ?? null,
        maximum_imaginary_frequency_cm1: options.maximumImaginaryFrequencyCm1 ?? null,
        reactant_product_changed: options.reactantProductChanged ?? null,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
      signal,
    ),
  parseRevisions: (options: {
    projectId?: string;
    artifactFileId?: string;
    status?: string;
    sourceFormat?: string;
    limit?: number;
    offset?: number;
  } = {}, signal?: AbortSignal) =>
    requestJson<ParseRevisionPage>(
      "/api/parse_revision_query_service/list_parse_revisions",
      {
        project_id: options.projectId ?? null,
        artifact_file_id: options.artifactFileId ?? null,
        status: options.status ?? null,
        source_format: options.sourceFormat ?? null,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
      signal,
    ),
  reactionEnergyProfile: (
    id: string,
    options: { projectId?: string; energyKind?: string; referenceNodeId?: string } = {},
    signal?: AbortSignal,
  ) =>
    request<ReactionEnergyProfile>(
      `/api/mapped-reactions/${encodeURIComponent(id)}/energy-profile?${new URLSearchParams({
        ...(options.projectId ? { project_id: options.projectId } : {}),
        ...(options.energyKind ? { energy_kind: options.energyKind } : {}),
        ...(options.referenceNodeId ? { reference_node_id: options.referenceNodeId } : {}),
      })}`,
      signal,
    ),
  frames: (options: { projectId?: string; artifactFileId?: string; geometryId?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    request<Page<CalculationFrameSummary>>(
      `/api/calculation-frames?${new URLSearchParams({ limit: String(options.limit ?? 50), offset: String(options.offset ?? 0), ...(options.projectId ? { project_id: options.projectId } : {}), ...(options.artifactFileId ? { artifact_file_id: options.artifactFileId } : {}), ...(options.geometryId ? { geometry_id: options.geometryId } : {}) })}`,
      signal,
    ),
  frame: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<CalculationFrameDetail>(
      `/api/calculation-frames/${encodeURIComponent(id)}${options.projectId ? `?project_id=${encodeURIComponent(options.projectId)}` : ""}`,
      signal,
    ),
  scientificArrayPreview: (id: string, options: { maxElements?: number; projectId?: string } = {}, signal?: AbortSignal) =>
    request<ScientificArrayPreview>(
      `/api/scientific-arrays/${encodeURIComponent(id)}/preview?${new URLSearchParams({
        max_elements: String(options.maxElements ?? 512),
        ...(options.projectId ? { project_id: options.projectId } : {}),
      })}`,
      signal,
    ),
  artifacts: (options: Partial<ArtifactSort> & { artifactId?: string; artifactKind?: string; contentSha256?: string; originalFilenameContains?: string; projectId?: string; storageStatus?: string; ingestionStatus?: string; limit?: number; offset?: number; cursor?: string } = {}, signal?: AbortSignal) =>
    request<Page<ArtifactSummary>>(
      `/api/artifacts?${new URLSearchParams({
        limit: String(options.limit ?? 50),
        offset: String(options.offset ?? 0),
        ...(options.artifactId ? { artifact_id: options.artifactId } : {}),
        ...(options.artifactKind ? { artifact_kind: options.artifactKind } : {}),
        ...(options.contentSha256 ? { content_sha256: options.contentSha256 } : {}),
        ...(options.originalFilenameContains ? { original_filename_contains: options.originalFilenameContains } : {}),
        ...(options.projectId ? { project_id: options.projectId } : {}),
        ...(options.storageStatus ? { storage_status: options.storageStatus } : {}),
        ...(options.ingestionStatus ? { ingestion_status: options.ingestionStatus } : {}),
        ...(options.cursor !== undefined ? { cursor: options.cursor } : {}),
        ...(options.sortBy ? { sort_by: options.sortBy } : {}),
        ...(options.sortDirection ? { sort_direction: options.sortDirection } : {}),
      })}`,
      signal,
    ),
  artifact: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<ArtifactSummary>(
      `/api/artifacts/${encodeURIComponent(id)}${options.projectId ? `?project_id=${encodeURIComponent(options.projectId)}` : ""}`,
      signal,
    ),
  updateArtifactNotes: (id: string, notes: string | null, signal?: AbortSignal) =>
    requestMutation<ArtifactSummary>(
      "/api/artifacts/" + encodeURIComponent(id),
      "PATCH",
      { notes },
      signal,
    ) as Promise<ArtifactSummary>,
  geometries: (
    options: GeometryQueryFilters & Partial<GeometrySort> & { limit?: number; offset?: number } = {},
    signal?: AbortSignal,
  ) =>
    requestJson<Page<GeometrySummary>>(
      "/api/geometry_query_service/list_geometries",
      {
        project_id: options.projectId ?? null,
        topology_id: options.topologyId ?? null,
        geometry_hash: options.geometryHash ?? null,
        internal_coordinate_hash: options.internalCoordinateHash ?? null,
        canonicalization_version: options.canonicalizationVersion ?? null,
        topology_derivation_id: options.topologyDerivationId ?? null,
        reaction_node_role: options.reactionNodeRole ?? null,
        topology_smiles: options.topologySmiles ?? null,
        topology_mol_block: options.topologyMolBlock ?? null,
        topology_smarts: options.topologySmarts ?? null,
        similarity_smiles: options.similaritySmiles ?? null,
        similarity_metric: options.similarityMetric ?? "tanimoto",
        thermodynamic_only: options.thermodynamicOnly ?? false,
        imaginary_frequency_status: options.imaginaryFrequencyStatus ?? null,
        minimum_atom_count: options.minimumAtomCount ?? null,
        maximum_atom_count: options.maximumAtomCount ?? null,
        filter_expression: options.filterExpression ? JSON.stringify(options.filterExpression) : null,
        sort_by: options.sortBy ?? "default",
        sort_direction: options.sortDirection ?? "asc",
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
      signal,
    ),
  geometry: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    requestJson<GeometryDetail | null>(
      "/api/geometry_query_service/get_geometry",
      { geometry_id: id, project_id: options.projectId ?? null },
      signal,
    ),
  topology: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    requestJson<MolecularTopologyDetail | null>(
      "/api/molecular_topology_detail_query_service/get_topology",
      { topology_id: id, project_id: options.projectId ?? null },
      signal,
    ),
  artifactPreview: (id: string, options: { projectId?: string } = {}, signal?: AbortSignal) =>
    request<ArtifactPreview>(
      `/api/artifacts/${encodeURIComponent(id)}/preview?${new URLSearchParams({
        max_bytes: "131072",
        ...(options.projectId ? { project_id: options.projectId } : {}),
      })}`,
      signal,
    ),
  submitArtifactBatchDownload,
  reparseArtifact: (id: string, signal?: AbortSignal) =>
    requestMutation<ArtifactUploadAccepted>(
      `/api/artifacts/${encodeURIComponent(id)}/reparse`,
      "POST",
      undefined,
      signal,
    ) as Promise<ArtifactUploadAccepted>,
  deleteArtifact,
  uploadArtifact,
  uploadArtifacts,
  createUploadBatch: (payload: UploadBatchCreate, signal?: AbortSignal) =>
    requestJson<UploadBatch>("/api/upload-batches", payload, signal),
  uploadBatches: (options: { projectId: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    request<UploadBatchPage>(
      `/api/upload-batches?${new URLSearchParams({
        limit: String(options.limit ?? 25),
        offset: String(options.offset ?? 0),
        ...(options.projectId ? { project_id: options.projectId } : {}),
      })}`,
      signal,
    ),
  uploadBatch: (batchId: string, projectId: string, signal?: AbortSignal) =>
    request<UploadBatch>(
      `/api/upload-batches/${encodeURIComponent(batchId)}?project_id=${encodeURIComponent(projectId)}`,
      signal,
    ),
  recoverUploadBatch: (batchId: string, signal?: AbortSignal) =>
    requestMutation<UploadBatch>(
      `/api/upload-batches/${encodeURIComponent(batchId)}/recover`,
      "POST",
      undefined,
      signal,
    ) as Promise<UploadBatch>,
  uploadBatchItems: (
    batchId: string,
    options: { projectId: string; status?: string; updatedAfter?: string; limit?: number; offset?: number },
    signal?: AbortSignal,
  ) => request<UploadBatchItemPage>(
    `/api/upload-batches/${encodeURIComponent(batchId)}/items?${new URLSearchParams({
      limit: String(options.limit ?? 100),
      offset: String(options.offset ?? 0),
      project_id: options.projectId,
      ...(options.status ? { item_status: options.status } : {}),
      ...(options.updatedAfter ? { updated_after: options.updatedAfter } : {}),
    })}`,
    signal,
  ),
  updateUploadBatchStatus: (batchId: string, status: Extract<UploadBatchStatus, "active" | "paused">, signal?: AbortSignal) =>
    requestMutation<UploadBatch>(
      `/api/upload-batches/${encodeURIComponent(batchId)}`,
      "PATCH",
      { status },
      signal,
    ) as Promise<UploadBatch>,
  cancelUploadBatch: (batchId: string, signal?: AbortSignal) =>
    requestMutation<UploadBatch>(
      `/api/upload-batches/${encodeURIComponent(batchId)}`,
      "DELETE",
      undefined,
      signal,
    ) as Promise<UploadBatch>,
  retryFailedUploadBatchItems: (batchId: string, signal?: AbortSignal) =>
    requestMutation<UploadBatch>(
      `/api/upload-batches/${encodeURIComponent(batchId)}/retry-failed`,
      "POST",
      undefined,
      signal,
    ) as Promise<UploadBatch>,
  retryUploadBatchItem: (batchId: string, clientFileId: string, signal?: AbortSignal) =>
    requestMutation<UploadBatchItem>(
      `/api/upload-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(clientFileId)}/retry`,
      "POST",
      undefined,
      signal,
    ) as Promise<UploadBatchItem>,
  uploadBatchFile,
  uploadBatchFiles,
};

export function artifactDownloadUrl(id: string, projectId?: string): string {
  return apiUrl(
    `/api/artifacts/${encodeURIComponent(id)}/download${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
  );
}

export function scientificArrayDownloadUrl(id: string, projectId?: string): string {
  return apiUrl(
    `/api/scientific-arrays/${encodeURIComponent(id)}.npy${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
  );
}

export async function getTopologyMolfile(
  topologyId: string,
  projectId?: string,
  signal?: AbortSignal,
): Promise<string> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const response = await fetch(
    apiUrl(`/api/depictions/topology/${encodeURIComponent(topologyId)}.mol${query}`),
    { headers: { accept: "chemical/x-mdl-molfile" }, credentials: "include", signal },
  );
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText);
  }
  return response.text();
}

export async function getGeometrySdf(
  geometryId: string,
  projectId?: string,
  signal?: AbortSignal,
): Promise<string> {
  const response = await fetch(
    apiUrl(
      `/api/depictions/geometry/${encodeURIComponent(geometryId)}.sdf${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
    ),
    { headers: { accept: "chemical/x-mdl-sdfile" }, credentials: "include", signal },
  );
  if (!response.ok) {
    throw new ApiError(response.status, response.statusText);
  }
  return response.text();
}

export async function getTransitionStateAnchorSdf(
  frameId: string,
  anchor: "negative" | "center" | "positive",
  projectId?: string,
  signal?: AbortSignal,
): Promise<string> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const response = await fetch(
    apiUrl(
      `/api/depictions/calculation-frame/${encodeURIComponent(frameId)}/transition-state/${anchor}.sdf${query}`,
    ),
    { headers: { accept: "chemical/x-mdl-sdfile" }, credentials: "include", signal },
  );
  if (!response.ok) throw new ApiError(response.status, response.statusText);
  return response.text();
}

export function transitionStateModeDofAnimationUrl(frameId: string, projectId?: string): string {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return apiUrl(`/api/depictions/calculation-frame/${encodeURIComponent(frameId)}/transition-state.svg${query}`);
}

export function geometryDepictionUrl(geometryId: string, projectId?: string): string {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return apiUrl(`/api/depictions/geometry/${encodeURIComponent(geometryId)}.svg${query}`);
}

export function reactionDepictionUrl(reactionSmiles: string): string {
  const query = new URLSearchParams({ reaction_smiles: reactionSmiles });
  return apiUrl(`/api/depictions/reaction.svg?${query.toString()}`);
}
