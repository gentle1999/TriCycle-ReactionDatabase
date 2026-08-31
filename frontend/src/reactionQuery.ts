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
  | "reactant_product_changed"
  | "created_after"
  | "created_before";

export type ReactionQueryLogicalOperator = "and" | "or";

export type ReactionSortBy =
  | "default"
  | "similarity"
  | "created_at"
  | "reaction_key"
  | "reaction_class"
  | "minimum_activation_gibbs_free_energy"
  | "minimum_reaction_gibbs_free_energy";

export interface ReactionSort {
  sortBy: ReactionSortBy;
  sortDirection: "asc" | "desc";
}

export interface ReactionQueryFilters {
  projectId?: string;
  topologyId?: string;
  reactionKey?: string;
  label?: string;
  reactionHash?: string;
  reactionClass?: string;
  reactionSmarts?: string;
  similarityReactionSmiles?: string;
  similarityMetric?: "tanimoto" | "dice";
  reactantMolBlock?: string;
  productMolBlock?: string;
  minimumActivationGibbsFreeEnergyKcalMol?: number;
  maximumActivationGibbsFreeEnergyKcalMol?: number;
  minimumReactionGibbsFreeEnergyKcalMol?: number;
  maximumReactionGibbsFreeEnergyKcalMol?: number;
  hasActivationGibbsFreeEnergy?: boolean;
  hasReactionGibbsFreeEnergy?: boolean;
  reactantProductChanged?: boolean;
  createdAfter?: string;
  createdBefore?: string;
  filterExpression?: ReactionQueryExpression;
}

export interface ReactionQueryExpressionCondition {
  field: ReactionQueryField;
  value: string | number | boolean;
  negated?: boolean;
}

export interface ReactionQueryExpression {
  operator: ReactionQueryLogicalOperator;
  conditions: Array<ReactionQueryExpressionCondition | ReactionQueryExpression>;
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
  kind: "text" | "smarts" | "number" | "datetime" | "class" | "boolean" | "reaction" | "mol_block";
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
  { value: "reactant_product_changed", label: "前后体拓扑发生变化", kind: "boolean" },
  { value: "created_after", label: "创建时间不早于", kind: "datetime" },
  { value: "created_before", label: "创建时间不晚于", kind: "datetime" },
];

export function reactionQueryFieldOption(field: ReactionQueryField): ReactionQueryFieldOption {
  return reactionQueryFieldOptions.find((option) => option.value === field) ?? reactionQueryFieldOptions[0];
}

/** Convert flat and advanced controls into one expression without changing OR-group semantics. */
export function reactionFilterExpression(filters: ReactionQueryFilters): ReactionQueryExpression | undefined {
  const conditions: Array<ReactionQueryExpressionCondition | ReactionQueryExpression> = [];
  if (filters.filterExpression) conditions.push(filters.filterExpression);

  const add = (
    field: ReactionQueryField,
    value: string | number | boolean | undefined,
  ): void => {
    if (value !== undefined && value !== "") conditions.push({ field, value });
  };
  add("topology_id", filters.topologyId);
  add("reaction_key", filters.reactionKey);
  add("label", filters.label);
  add("reaction_hash", filters.reactionHash);
  add("reaction_class", filters.reactionClass);
  add("rxn_smarts", filters.reactionSmarts);
  add("reactant_mol_block", filters.reactantMolBlock);
  add("product_mol_block", filters.productMolBlock);
  add(
    "minimum_activation_gibbs_free_energy_kcal_mol",
    filters.minimumActivationGibbsFreeEnergyKcalMol,
  );
  add(
    "maximum_activation_gibbs_free_energy_kcal_mol",
    filters.maximumActivationGibbsFreeEnergyKcalMol,
  );
  add(
    "minimum_reaction_gibbs_free_energy_kcal_mol",
    filters.minimumReactionGibbsFreeEnergyKcalMol,
  );
  add(
    "maximum_reaction_gibbs_free_energy_kcal_mol",
    filters.maximumReactionGibbsFreeEnergyKcalMol,
  );
  add("reactant_product_changed", filters.reactantProductChanged);
  add("created_after", filters.createdAfter);
  add("created_before", filters.createdBefore);

  if (!conditions.length) return undefined;
  if (conditions.length === 1 && filters.filterExpression === conditions[0]) {
    return filters.filterExpression;
  }
  return { operator: "and", conditions };
}
