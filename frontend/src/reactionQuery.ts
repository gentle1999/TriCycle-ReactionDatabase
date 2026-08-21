export type ReactionQueryField =
  | "topology_id"
  | "reaction_key"
  | "label"
  | "reaction_hash"
  | "reaction_class"
  | "smarts"
  | "reactant_smarts"
  | "product_smarts"
  | "reaction_smarts"
  | "rxn_smarts"
  | "reactant_mol_block"
  | "product_mol_block"
  | "minimum_activation_gibbs_free_energy_kcal_mol"
  | "maximum_activation_gibbs_free_energy_kcal_mol"
  | "minimum_reaction_gibbs_free_energy_kcal_mol"
  | "maximum_reaction_gibbs_free_energy_kcal_mol"
  | "created_after"
  | "created_before";

export type ReactionQueryLogicalOperator = "and" | "or";

export interface ReactionQueryFilters {
  projectId?: string;
  topologyId?: string;
  reactionKey?: string;
  label?: string;
  reactionHash?: string;
  reactionClass?: string;
  reactionSmarts?: string;
  reactantMolBlock?: string;
  productMolBlock?: string;
  minimumActivationGibbsFreeEnergyKcalMol?: number;
  maximumActivationGibbsFreeEnergyKcalMol?: number;
  minimumReactionGibbsFreeEnergyKcalMol?: number;
  maximumReactionGibbsFreeEnergyKcalMol?: number;
  hasActivationGibbsFreeEnergy?: boolean;
  hasReactionGibbsFreeEnergy?: boolean;
  createdAfter?: string;
  createdBefore?: string;
  filterExpression?: ReactionQueryExpression;
}

export interface ReactionQueryExpressionCondition {
  field: ReactionQueryField;
  value: string | number;
  negated?: boolean;
}

export interface ReactionQueryExpression {
  operator: ReactionQueryLogicalOperator;
  conditions: ReactionQueryExpressionCondition[];
}

export interface ReactionQueryCondition {
  id: number;
  field: ReactionQueryField;
  value: string;
  reactantSmiles: string;
  productSmiles: string;
  molfile: string;
  negated: boolean;
}

export interface ReactionQueryFieldOption {
  value: ReactionQueryField;
  label: string;
  kind: "text" | "smarts" | "number" | "datetime" | "class" | "reaction" | "mol_block";
}

export const reactionQueryFieldOptions: ReactionQueryFieldOption[] = [
  { value: "topology_id", label: "拓扑 ID", kind: "text" },
  { value: "reaction_key", label: "反应键", kind: "text" },
  { value: "label", label: "反应名称", kind: "text" },
  { value: "reaction_hash", label: "反应 hash", kind: "text" },
  { value: "reaction_class", label: "反应类型", kind: "class" },
  { value: "reactant_smarts", label: "前体 SMARTS", kind: "smarts" },
  { value: "product_smarts", label: "后体 SMARTS", kind: "smarts" },
  { value: "rxn_smarts", label: "RXN SMILES / SMARTS", kind: "reaction" },
  { value: "reactant_mol_block", label: "前体结构", kind: "mol_block" },
  { value: "product_mol_block", label: "后体结构", kind: "mol_block" },
  { value: "minimum_activation_gibbs_free_energy_kcal_mol", label: "最低活化自由能（kcal/mol）", kind: "number" },
  { value: "maximum_activation_gibbs_free_energy_kcal_mol", label: "最高活化自由能（kcal/mol）", kind: "number" },
  { value: "minimum_reaction_gibbs_free_energy_kcal_mol", label: "最低反应自由能（kcal/mol）", kind: "number" },
  { value: "maximum_reaction_gibbs_free_energy_kcal_mol", label: "最高反应自由能（kcal/mol）", kind: "number" },
  { value: "created_after", label: "创建时间不早于", kind: "datetime" },
  { value: "created_before", label: "创建时间不晚于", kind: "datetime" },
];

export function reactionQueryFieldOption(field: ReactionQueryField): ReactionQueryFieldOption {
  return reactionQueryFieldOptions.find((option) => option.value === field) ?? reactionQueryFieldOptions[0];
}
