export type GeometryQueryField =
  | "topology_id"
  | "geometry_hash"
  | "internal_coordinate_hash"
  | "canonicalization_version"
  | "topology_derivation_id"
  | "reaction_node_role"
  | "topology_smiles"
  | "topology_mol_block"
  | "topology_smarts"
  | "thermodynamic_only"
  | "imaginary_frequency_status"
  | "minimum_atom_count"
  | "maximum_atom_count";

export type GeometryImaginaryFrequencyStatus = "present" | "absent" | "unavailable";

export type GeometrySortBy =
  | "default"
  | "created_at"
  | "atom_count"
  | "calculation_count";

export interface GeometrySort {
  sortBy: GeometrySortBy;
  sortDirection: "asc" | "desc";
}

export interface GeometryQueryFilters {
  projectId?: string;
  topologyId?: string;
  geometryHash?: string;
  internalCoordinateHash?: string;
  canonicalizationVersion?: string;
  topologyDerivationId?: string;
  reactionNodeRole?: string;
  topologySmiles?: string;
  topologyMolBlock?: string;
  topologySmarts?: string;
  thermodynamicOnly?: boolean;
  imaginaryFrequencyStatus?: GeometryImaginaryFrequencyStatus;
  minimumAtomCount?: number;
  maximumAtomCount?: number;
  filterExpression?: GeometryQueryExpression;
}

export interface GeometryQueryCondition {
  id: number;
  field: GeometryQueryField;
  value: string;
  molfile: string;
  negated: boolean;
}

export type GeometryQueryLogicalOperator = "and" | "or";

export interface GeometryQueryExpressionCondition {
  field: GeometryQueryField;
  value: string | number | true;
  negated?: boolean;
}

export interface GeometryQueryExpression {
  operator: GeometryQueryLogicalOperator;
  conditions: GeometryQueryExpressionCondition[];
}

export interface GeometryQueryFieldOption {
  value: GeometryQueryField;
  label: string;
  kind: "text" | "integer" | "boolean" | "status" | "smiles" | "mol_block" | "smarts";
}

export const geometryQueryFieldOptions: GeometryQueryFieldOption[] = [
  { value: "topology_id", label: "拓扑 ID", kind: "text" },
  { value: "geometry_hash", label: "Geometry hash", kind: "text" },
  { value: "internal_coordinate_hash", label: "内部坐标 hash", kind: "text" },
  { value: "canonicalization_version", label: "规范化版本", kind: "text" },
  { value: "topology_derivation_id", label: "拓扑派生 ID", kind: "text" },
  { value: "reaction_node_role", label: "反应节点角色", kind: "text" },
  { value: "topology_smiles", label: "拓扑 SMILES", kind: "smiles" },
  { value: "topology_mol_block", label: "拓扑 MOL Block", kind: "mol_block" },
  { value: "topology_smarts", label: "拓扑 SMARTS", kind: "smarts" },
  { value: "thermodynamic_only", label: "热力学属性", kind: "boolean" },
  { value: "imaginary_frequency_status", label: "虚频状态", kind: "status" },
  { value: "minimum_atom_count", label: "最小原子数", kind: "integer" },
  { value: "maximum_atom_count", label: "最大原子数", kind: "integer" },
];

export function geometryQueryFieldOption(field: GeometryQueryField): GeometryQueryFieldOption {
  return geometryQueryFieldOptions.find((option) => option.value === field) ?? geometryQueryFieldOptions[0];
}
