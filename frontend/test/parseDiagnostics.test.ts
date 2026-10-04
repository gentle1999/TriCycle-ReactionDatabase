import { strict as assert } from "node:assert";
import { test } from "node:test";
import { collectParseDiagnostics } from "../src/parseDiagnostics.ts";
import type { ArtifactSummary, CalculationFrameSummary, ParseRevisionSummary, TransitionStateInferenceSummary } from "../src/types.ts";

const artifact = { ingestion_status: "partial", ingestion_error_code: null, ingestion_error_message: null } as ArtifactSummary;
const revision = { id: "current", parse_diagnostics_json: "[]" } as ParseRevisionSummary;

test("frame zero is retained and only the matching revision can provide a frame link", () => {
  const frames = [{ id: "old-frame", parse_revision_id: "old", file_frame_index: 0 },
    { id: "correct-frame", parse_revision_id: "current", file_frame_index: 0 }] as CalculationFrameSummary[];
  const result = collectParseDiagnostics(artifact, { ...revision, parse_diagnostics_json: JSON.stringify([
    { code: "frame_conversion_failed", message: "missing ZPVE", file_frame_index: 0, segment_index: 0 },
    { code: "frame_conversion_failed", message: "missing ZPVE", file_frame_index: 1 },
  ]) }, [], frames);
  assert.equal(result.items[0].frameIndex, 0);
  assert.equal(result.items[0].segmentIndex, 0);
  assert.equal(result.items[0].frameId, "correct-frame");
  assert.equal(result.items[1].frameId, null);
  assert.equal(result.items[0].severity, "error");
});

test("TS failures retain their exact frame and evidence, excluding old revisions", () => {
  const failures = [{ status: "failed", parse_revision_id: "current", file_frame_index: 42,
    calculation_frame_id: "ts-frame", error_code: "stereo_projection_failed", error_message: "E/Z changed",
    error_metadata_json: '{"reason":"no_lossless_smiles_traversal"}' },
    { status: "failed", parse_revision_id: "old", file_frame_index: 0 }] as TransitionStateInferenceSummary[];
  const result = collectParseDiagnostics(artifact, revision, failures, []);
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].frameIndex, 42);
  assert.equal(result.items[0].frameId, "ts-frame");
  assert.deepEqual(result.items[0].evidence.metadata, { reason: "no_lossless_smiles_traversal" });
});

test("file failure without a revision remains visible without inventing a frame", () => {
  const result = collectParseDiagnostics({ ...artifact, ingestion_status: "failed", ingestion_error_code: "parser_failed", ingestion_error_message: "unsupported source" }, null, [], []);
  assert.equal(result.items[0].message, "unsupported source");
  assert.equal(result.items[0].frameIndex, null);
});

test("malformed or mixed diagnostics cannot be displayed as a clean parse", () => {
  for (const json of ["{", "{}", '[null,{"message":"kept"}]']) {
    assert.equal(collectParseDiagnostics(artifact, { ...revision, parse_diagnostics_json: json }, [], []).unreadable, true);
  }
});

test("duplicate diagnostics collapse while distinct affected frames remain", () => {
  const rows = [{ code: "failed", message: "x", file_frame_index: 3 }, { code: "failed", message: "x", file_frame_index: 3 }, { code: "failed", message: "x", file_frame_index: 4 }];
  assert.equal(collectParseDiagnostics(artifact, { ...revision, parse_diagnostics_json: JSON.stringify(rows) }, [], []).items.length, 2);
});
