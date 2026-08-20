export type ArtifactQueryField =
  | "artifact_id"
  | "content_sha256"
  | "original_filename_contains"
  | "artifact_kind"
  | "storage_status";

export interface ArtifactFilterValues {
  artifactId: string | null;
  contentSha256: string | null;
  originalFilenameContains: string | null;
  artifactKind: string | null;
  storageStatus: string | null;
}

export interface ArtifactQueryCondition {
  id: number;
  field: ArtifactQueryField;
  value: string;
}

export interface ArtifactQueryFieldOption {
  value: ArtifactQueryField;
  label: string;
  kind: "identifier" | "text" | "enum";
}

export const artifactQueryFieldOptions: ArtifactQueryFieldOption[] = [
  { value: "artifact_id", label: "文件 ID", kind: "identifier" },
  { value: "content_sha256", label: "SHA-256", kind: "identifier" },
  { value: "original_filename_contains", label: "文件名包含", kind: "text" },
  { value: "artifact_kind", label: "文件类型", kind: "enum" },
  { value: "storage_status", label: "存储状态", kind: "enum" },
];

export function artifactQueryFieldOption(field: ArtifactQueryField): ArtifactQueryFieldOption {
  return artifactQueryFieldOptions.find((option) => option.value === field) ?? artifactQueryFieldOptions[0];
}

export const artifactKindOptions = [
  { value: "calculation_output", label: "计算输出" },
  { value: "input", label: "计算输入" },
  { value: "workflow_manifest", label: "Workflow manifest" },
  { value: "auxiliary", label: "辅助文件" },
] as const;

export const storageStatusOptions = [
  { value: "available", label: "可用" },
  { value: "pending", label: "待处理" },
  { value: "missing", label: "缺失" },
  { value: "corrupt", label: "损坏" },
] as const;

export function emptyArtifactFilters(): ArtifactFilterValues {
  return {
    artifactId: null,
    contentSha256: null,
    originalFilenameContains: null,
    artifactKind: null,
    storageStatus: null,
  };
}
