import type { ArtifactSummary, CalculationFrameSummary, ParseRevisionSummary, TransitionStateInferenceSummary } from "./types";

export interface ParseDiagnostic {
  code: string;
  message: string;
  stage: string;
  severity: "error" | "warning" | "info";
  frameIndex: number | null;
  segmentIndex: number | null;
  sourceLine: number | null;
  frameId: string | null;
  evidence: Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function index(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}
function decode(value: string | null | undefined, fallback: unknown): unknown {
  if (!value) return fallback;
  try { return JSON.parse(value); } catch { return undefined; }
}

export function collectParseDiagnostics(
  artifact: Pick<ArtifactSummary, "ingestion_status" | "ingestion_error_code" | "ingestion_error_message">,
  revision: ParseRevisionSummary | null,
  inferences: TransitionStateInferenceSummary[],
  frames: CalculationFrameSummary[],
): { items: ParseDiagnostic[]; unreadable: boolean } {
  const decoded = decode(revision?.parse_diagnostics_json, []);
  const unreadable = !Array.isArray(decoded) || decoded.some((item) => !record(item));
  const records: Record<string, unknown>[] = Array.isArray(decoded) ? decoded.flatMap((item) => record(item) ? [record(item)!] : []) : [];
  if (revision?.error_code || revision?.error_message) {
    records.push({ code: revision.error_code, message: revision.error_message, stage: "parsing", severity: "error",
      metadata: decode(revision.error_metadata_json, {}) });
  }
  for (const inference of inferences) {
    if (inference.status !== "failed" || inference.parse_revision_id !== revision?.id) continue;
    records.push({ code: inference.error_code || "ts_inference_failed", message: inference.error_message || "过渡态推导失败",
      stage: "inference", severity: "error", file_frame_index: inference.file_frame_index,
      calculation_frame_id: inference.calculation_frame_id, metadata: decode(inference.error_metadata_json, {}) });
  }
  if (artifact.ingestion_error_message && !records.some((item) => item.message === artifact.ingestion_error_message)) {
    records.unshift({ code: artifact.ingestion_error_code, message: artifact.ingestion_error_message, stage: "file",
      severity: artifact.ingestion_status === "failed" ? "error" : "warning" });
  }
  const seen = new Set<string>();
  const items: ParseDiagnostic[] = [];
  for (const item of records) {
    const metadata = record(item.metadata) ?? {};
    const frameIndex = index(item.file_frame_index) ?? index(metadata.file_frame_index);
    const code = text(item.code) || text(item.error_type) || "parse_diagnostic";
    const message = text(item.message) || text(item.reason) || "解析器未提供说明，请展开原始诊断。";
    const key = JSON.stringify([code, message, frameIndex, index(item.segment_index)]);
    if (seen.has(key)) continue;
    seen.add(key);
    const frame = frames.find((frame) => frame.parse_revision_id === revision?.id && frame.file_frame_index === frameIndex);
    items.push({ code, message, stage: text(item.stage) || "parsing",
      severity: item.severity === "info" ? "info" : item.severity === "warning" ? "warning"
        : item.severity === "error" || code.toLowerCase().includes("failed") || item.error_type ? "error" : "warning",
      frameIndex, segmentIndex: index(item.segment_index) ?? index(metadata.segment_index),
      sourceLine: index(item.source_line) ?? index(metadata.source_line),
      frameId: text(item.calculation_frame_id) || frame?.id || null, evidence: item });
  }
  return { items, unreadable };
}

export function diagnosticStage(stage: string): string {
  return ({ file: "文件处理", parsing: "文件解析", conversion: "帧转换", topology_normalization: "拓扑规范化",
    inference: "TS 推导 / 入库", persistence: "帧入库", source: "源文件" } as Record<string, string>)[stage] ?? stage;
}
