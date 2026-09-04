export interface PageInfo {
  total: number;
  limit: number;
  offset: number;
  next_cursor?: string | null;
}

export interface Page<T> {
  items: T[];
  page: PageInfo;
}

export interface MolecularTopologyDetail {
  id: string;
  formula_id: string;
  hill_formula: string;
  formula_composition_hash: string;
  canonical_isomeric_smiles: string | null;
  graph_hash: string;
  atom_count: number;
  heavy_atom_count: number;
  formal_charge: number;
  radical_electron_count: number;
  fragment_count: number;
  stereo_status: string;
  sanitization_status: string;
  sanitization_error: string | null;
  substructure_match_count: number | null;
  morgan_bfp_schema_version: string;
  morgan_bfp_available: boolean;
  similarity_score: number | null;
  molecular_weight: number | null;
  logp: number | null;
  tpsa: number | null;
  hba_count: number | null;
  hbd_count: number | null;
  ring_count: number | null;
  scaffold_smiles: string | null;
  geometry_count: number;
  logical_reaction_count: number;
  derivation_count: number;
}

export interface LogicalReactionSummary {
  id: string;
  reaction_key: string;
  label: string | null;
  reaction_class: string | null;
  cycloaddition_pattern: string | null;
  reaction_hash: string;
  mapped_reaction_count: number;
  similarity_score: number | null;
  reactant_product_changed: boolean | null;
  created_at: string | null;
  reactant_topology_ids: string[];
  product_topology_ids: string[];
  transition_state_geometry_id: string | null;
  minimum_activation_gibbs_free_energy_kcal_mol: number | null;
  maximum_activation_gibbs_free_energy_kcal_mol: number | null;
  minimum_reaction_gibbs_free_energy_kcal_mol: number | null;
  maximum_reaction_gibbs_free_energy_kcal_mol: number | null;
}

export interface LogicalReactionParticipant {
  id: string;
  side: "reactant" | "product" | string;
  participant_index: number;
  role: string | null;
  topology_id: string;
  canonical_isomeric_smiles: string | null;
  stoichiometric_coefficient: number;
}

export interface MappedReactionSummary {
  id: string;
  logical_reaction_id: string;
  mapped_reaction_key: string;
  label: string | null;
  mapped_reaction_kind: string;
  mapped_reaction_smiles: string;
  mapping_hash: string;
  reactant_product_changed: boolean | null;
  created_at: string | null;
  minimum_activation_gibbs_free_energy_kcal_mol: number | null;
  maximum_activation_gibbs_free_energy_kcal_mol: number | null;
  minimum_reaction_gibbs_free_energy_kcal_mol: number | null;
  maximum_reaction_gibbs_free_energy_kcal_mol: number | null;
  similarity_score: number | null;
}

export interface LogicalReactionDetail extends LogicalReactionSummary {
  participants: LogicalReactionParticipant[];
  mapped_reactions: MappedReactionSummary[];
}

export interface CalculationFrameSummary {
  id: string;
  artifact_file_id: string;
  original_filename: string;
  parse_revision_id: string;
  segment_id: string;
  segment_index: number;
  frame_index: number;
  file_frame_index: number;
  frame_role: string;
  geometry_id: string;
  topology_id: string;
  topology_derivation_id: string;
  canonical_isomeric_smiles: string | null;
  charge: number;
  multiplicity: number;
  coordinate_decimal_places: number | null;
  scf_status: string;
  optimization_status: string;
  selected_energy_hartree: number | null;
  selected_energy_kind: string | null;
  frequency_count: number | null;
  negative_frequency_count: number | null;
  running_time_seconds: number | null;
  protocol_id?: string | null;
}

export interface GeometrySummary {
  id: string;
  topology_id: string;
  canonical_isomeric_smiles: string | null;
  atom_count: number;
  geometry_hash: string;
  internal_coordinate_hash: string;
  canonicalization_version: string;
  charge: number;
  multiplicity: number;
  calculation_count: number;
  reaction_binding_count: number;
  is_transition_state: boolean;
  imaginary_frequency_status: "present" | "absent" | "unavailable";
  similarity_score: number | null;
}

export interface GeometryDetail extends GeometrySummary {
  frames: CalculationFrameSummary[];
  energy_view: GeometryEnergyView;
  coordinates: GeometryAtomCoordinate[];
}

export interface GeometryAtomCoordinate {
  atom_index: number;
  element: string;
  x_angstrom: number;
  y_angstrom: number;
  z_angstrom: number;
}

export interface GeometryEnergyView {
  geometry_id: string;
  policy_version: string;
  electronic_selection_status: "selected" | "missing" | "ambiguous";
  electronic_candidate_frame_ids: string[];
  electronic_energy_hartree: number | null;
  electronic_energy_source_frame_id: string | null;
  electronic_energy_protocol_id: string | null;
  charge: number | null;
  multiplicity: number | null;
  electronic_state_kind: string | null;
  electronic_state_index: number | null;
  thermochemistry_selection_status: "selected" | "missing" | "ambiguous";
  thermochemistry_candidate_frame_ids: string[];
  thermochemistry_source_frame_id: string | null;
  thermochemistry_protocol_id: string | null;
  temperature_kelvin: number | null;
  pressure_atm: number | null;
  zpe_correction_hartree: number | null;
  thermal_energy_correction_hartree: number | null;
  thermal_enthalpy_correction_hartree: number | null;
  thermal_gibbs_correction_hartree: number | null;
  zero_point_energy_hartree: number | null;
  thermal_internal_energy_hartree: number | null;
  enthalpy_hartree: number | null;
  gibbs_free_energy_hartree: number | null;
  entropy_cal_mol_k: number | null;
}

export interface ThermodynamicDifference {
  enthalpy_kcal_mol: number;
  gibbs_free_energy_kcal_mol: number;
  entropy_cal_mol_k: number;
}

export interface MappedReactionThermodynamicsProfile {
  mapped_reaction_id: string;
  policy_version: string;
  electronic_level: (string | null)[];
  thermochemistry_level: (string | null)[];
  level_of_theory: string;
  temperature_kelvin: number;
  pressure_atm: number;
  reactants: { enthalpy_hartree: number; gibbs_free_energy_hartree: number; entropy_cal_mol_k: number };
  transition_state: { enthalpy_hartree: number; gibbs_free_energy_hartree: number; entropy_cal_mol_k: number } | null;
  products: { enthalpy_hartree: number; gibbs_free_energy_hartree: number; entropy_cal_mol_k: number } | null;
  activation: ThermodynamicDifference | null;
  reaction: ThermodynamicDifference | null;
  reactants_running_time_seconds: number | null;
  transition_state_running_time_seconds: number | null;
  products_running_time_seconds: number | null;
  total_running_time_seconds: number | null;
}

export interface MappedReactionThermodynamics {
  mapped_reaction_id: string;
  profiles: MappedReactionThermodynamicsProfile[];
}

export interface ThermodynamicDistributionBin {
  lower: number;
  upper: number;
  count: number;
}

export interface ThermodynamicDistributionCategory {
  label: string;
  count: number;
}

export interface ThermodynamicScatterPoint {
  mapped_reaction_id: string;
  mapped_reaction_smiles: string;
  activation_gibbs_free_energy_kcal_mol: number;
  reaction_gibbs_free_energy_kcal_mol: number;
}

export interface MappedReactionThermodynamicStatistics {
  mapped_reaction_count: number;
  profile_count: number;
  activation_profile_count: number;
  reaction_profile_count: number;
  complete_profile_count: number;
  activation_gibbs_free_energy_kcal_mol: ThermodynamicDistributionBin[];
  reaction_gibbs_free_energy_kcal_mol: ThermodynamicDistributionBin[];
  level_of_theory: ThermodynamicDistributionCategory[];
  temperature_kelvin: ThermodynamicDistributionCategory[];
  scatter: ThermodynamicScatterPoint[];
}

export interface NodeAdditiveProperties {
  component_count: number;
  policy_version: string;
  source_levels_compatible: boolean;
  electronic_energy_hartree: number | null;
  temperature_kelvin: number | null;
  pressure_atm: number | null;
  zero_point_energy_hartree: number | null;
  thermal_internal_energy_hartree: number | null;
  enthalpy_hartree: number | null;
  gibbs_free_energy_hartree: number | null;
}

export interface NodeGeometryMapping {
  id: string;
  geometry_atom_map_numbers: number[];
  mapped_smiles: string;
  mapping_method: string;
  mapping_version: string;
  verified: boolean;
}

export interface MappedReactionNodeGeometry {
  id: string;
  component_key: string;
  component_index: number;
  coordinate_index: number;
  is_primary: boolean;
  participant_role: string | null;
  geometry_id: string;
  topology_id: string;
  canonical_isomeric_smiles: string | null;
  mappings: NodeGeometryMapping[];
  calculations: CalculationFrameSummary[];
  energy_view: GeometryEnergyView;
}

export interface MappedReactionNode {
  id: string;
  node_key: string;
  node_index: number;
  role: string;
  geometries: MappedReactionNodeGeometry[];
  additive_properties: NodeAdditiveProperties | null;
}

export interface MappedReactionDetail extends MappedReactionSummary {
  reaction_key: string;
  participants: Array<{
    id: string;
    logical_reaction_participant_id: string;
    side: string;
    template_index: number;
    topology_id: string;
    logical_topology_id: string;
    atom_map_numbers: number[];
    mapped_smiles: string;
  }>;
  nodes: MappedReactionNode[];
  edges: Array<{
    id: string;
    edge_key: string;
    source_node_id: string;
    target_node_id: string;
    transition_state_node_id: string | null;
    edge_kind: string;
  }>;
}

export interface ReactionEnergyPoint {
  node_id: string;
  node_key: string;
  node_index: number;
  role: string;
  energy_kind: string;
  energy_hartree: number | null;
  relative_energy_kcal_mol: number | null;
}

export interface ReactionEnergyEdge {
  edge_id: string;
  edge_key: string;
  source_node_id: string;
  target_node_id: string;
  transition_state_node_id: string | null;
  reaction_energy_kcal_mol: number | null;
  forward_barrier_kcal_mol: number | null;
  reverse_barrier_kcal_mol: number | null;
}

export interface ReactionEnergyProfile {
  mapped_reaction_id: string;
  energy_kind: string;
  reference_node_id: string | null;
  points: ReactionEnergyPoint[];
  edges: ReactionEnergyEdge[];
}

export interface SourceSpan {
  start_byte: number | null;
  end_byte: number | null;
  start_char: number | null;
  end_char: number | null;
  start_line: number | null;
  end_line: number | null;
  block_sha256: string | null;
}

export interface CalculationFrameDetail extends CalculationFrameSummary {
  source_span: SourceSpan | null;
  parse_completeness: string;
  geometry_assignment_kind: string;
  observed_coordinate_hash: string;
  observed_to_geometry_atom_indices: number[];
  observed_to_geometry_transform: number[];
  geometry_assignment_rmsd_angstrom: number;
  geometry_assignment_max_abs_angstrom: number;
  geometry_assignment_policy_version: string;
  electronic_state_kind: string;
  electronic_state_index: number;
  topology_derivation: {
    id: string;
    reconstruction_method: string;
    reconstruction_version: string;
    reconstruction_metadata_json: string;
    provenance_schema_version: string;
    provenance_hash: string;
  };
  protocol: Record<string, unknown> | null;
  energy: Record<string, number | null> | null;
  energy_observations: Array<Record<string, unknown>>;
  optimization: GeometryOptimization | null;
  vibration: {
    mode_count: number;
    imaginary_mode_count: number;
    lowest_frequency_cm1: number | null;
    mode_indices: number[];
  } | null;
  transition_state_endpoints: Array<{
    direction: "negative" | "positive";
    topology_id: string;
    charge: number;
    multiplicity: number;
    atom_count: number;
    displacement_ratio: number;
    source_coordinate_hash: string;
    source_to_topology_atom_indices: number[];
  }>;
  thermochemistry: Record<string, number | null> | null;
  calculation_status: Record<string, boolean | null> | null;
  scientific_arrays: ScientificArraySummary[];
}

export interface GeometryOptimization {
  geometry_optimized: boolean | null;
  convergence_multiplier: number;
  energy_change_hartree: number | null;
  energy_change_threshold_hartree: number | null;
  energy_change_converged: boolean | null;
  rms_force_hartree_per_bohr: number | null;
  rms_force_threshold_hartree_per_bohr: number | null;
  rms_force_converged: boolean | null;
  max_force_hartree_per_bohr: number | null;
  max_force_threshold_hartree_per_bohr: number | null;
  max_force_converged: boolean | null;
  rms_displacement_bohr: number | null;
  rms_displacement_threshold_bohr: number | null;
  rms_displacement_converged: boolean | null;
  max_displacement_bohr: number | null;
  max_displacement_threshold_bohr: number | null;
  max_displacement_converged: boolean | null;
}

export interface ScientificArrayPreview {
  id: string;
  kind: string;
  unit: string;
  dtype: string;
  shape: number[];
  total_elements: number;
  values: Array<unknown>;
  truncated: boolean;
}

export interface ScientificArraySummary {
  id: string;
  kind: string;
  ordinal: number;
  unit: string;
  dtype: string;
  shape: number[];
  array_nbytes: number;
  payload_sha256: string;
  owner_kind?: string | null;
  owner_id?: string | null;
  slot?: string | null;
  slot_ordinal?: number | null;
  source_field?: string | null;
  source_unit?: string | null;
  population_name?: string | null;
  population_scheme?: string | null;
  population_quantity?: string | null;
  population_spin_channel?: string | null;
  population_source_label?: string | null;
}

export interface ArtifactSummary {
  id: string;
  project_id: string;
  created_by_user_id: string;
  created_at?: string | null;
  visibility: "public" | "project";
  original_filename: string;
  content_sha256: string;
  size_bytes: number;
  media_type: string;
  artifact_kind: string;
  storage_status: string;
  storage_verified_at: string | null;
  preview_available: boolean;
  ingestion_status: "pending" | "succeeded" | "partial" | "filtered" | "failed" | null;
  source_frame_count: number | null;
  transition_state_frame_count: number | null;
  running_time_seconds: number | null;
  ingestion_error_code: string | null;
  ingestion_error_message: string | null;
}

export interface ParseRevisionSummary {
  id: string;
  artifact_file_id: string;
  revision_number: number;
  reparse_of_id: string | null;
  export_schema_version: string;
  parser_name: string;
  parser_version: string;
  molop_version: string;
  source_format: string;
  parse_completeness: string;
  status: string;
  record_sha256: string | null;
  running_time_seconds: number | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ParseRevisionPage {
  items: ParseRevisionSummary[];
  page: PageInfo;
}

export interface CurrentUser {
  id: string;
  display_name: string;
  primary_email: string | null;
  is_service_account: boolean;
  identity: {
    issuer: string;
    subject: string;
  };
  projects: Array<{
    project_id: string;
    project_slug: string;
    project_name: string;
    organization_id: string;
    organization_slug: string;
    organization_name: string;
    organization_role: string | null;
    project_role: string | null;
    permissions: string[];
  }>;
}

export interface OrganizationAccessView {
  id: string;
  slug: string;
  name: string;
  status: "active" | "suspended" | string;
  role: "owner" | "admin" | "member" | string | null;
  can_create_projects: boolean;
}

export interface ProjectView {
  id: string;
  organization_id: string;
  organization_slug: string;
  organization_name: string;
  slug: string;
  name: string;
  status: "active" | "archived" | string;
  role: string | null;
  organization_role: string | null;
  permissions: string[];
  created_at: string | null;
}

export interface UserSummaryView {
  id: string;
  display_name: string;
  primary_email: string | null;
  status: "active" | "suspended" | string;
  is_service_account: boolean;
  last_authenticated_at: string | null;
  created_at: string | null;
  project_role: "manager" | "contributor" | "viewer" | string | null;
}

export interface UserPage {
  items: UserSummaryView[];
  total: number;
  limit: number;
  offset: number;
}

export interface ProjectMemberView {
  user_id: string;
  display_name: string;
  primary_email: string | null;
  role: "manager" | "contributor" | "viewer" | string;
  created_at: string | null;
}

export interface ProjectInvitationView {
  id: string;
  project_id: string;
  email: string;
  role: "manager" | "contributor" | "viewer" | string;
  created_at: string | null;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  delivery_status: "pending" | "link_only" | "sent" | "failed" | string;
  delivery_error: string | null;
}

export interface ProjectInvitationCreateResult {
  invitation: ProjectInvitationView;
  accept_token: string;
  accept_url: string;
  delivery_status: "pending" | "link_only" | "sent" | "failed" | string;
  delivery_error: string | null;
}

export interface SessionView {
  id: string;
  created_at: string | null;
  expires_at: string;
  last_seen_at: string;
  user_agent: string | null;
  ip_address: string | null;
  current: boolean;
}

export interface McpAccessTokenView {
  id: string;
  name: string;
  created_at: string | null;
  expires_at: string;
  last_used_at: string | null;
}

export interface McpAccessTokenCreateResult {
  token: McpAccessTokenView;
  access_token: string;
}

export interface AuditEventView {
  id: string;
  created_at: string | null;
  actor_user_id: string | null;
  project_id: string | null;
  action: string;
  entity_type: string;
  entity_id: string | null;
  metadata_json: Record<string, unknown>;
}

export interface ArtifactPreview {
  id: string;
  original_filename: string;
  media_type: string;
  size_bytes: number;
  content_sha256: string;
  preview_text: string;
  preview_bytes: number;
  truncated: boolean;
}

export interface TransitionStateInferenceResult {
  id: string;
  file_frame_index: number;
  imaginary_mode_index: number;
  imaginary_frequency_cm1: number;
  status: "succeeded" | "failed";
  logical_reaction_id: string | null;
  mapped_reaction_id: string | null;
  calculation_frame_id: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface TransitionStateInferenceSummary extends TransitionStateInferenceResult {
  artifact_ingestion_id: string;
  parse_revision_id: string;
}

export interface ArtifactUploadResult {
  artifact_id: string;
  artifact_kind: "calculation_output" | "input" | "workflow_manifest" | "auxiliary";
  storage_status: string;
  ingestion_id: string | null;
  ingestion_status: "pending" | "succeeded" | "partial" | "filtered" | "failed" | null;
  source_frame_count: number | null;
  transition_state_frame_count: number | null;
  inferred_reaction_count: number;
  inferences: TransitionStateInferenceResult[];
}

export interface ArtifactBatchUploadItem {
  filename: string;
  succeeded: boolean;
  result: ArtifactUploadResult | null;
  error_code: string | null;
  error_message: string | null;
}

export interface ArtifactBatchUploadResult {
  total_count: number;
  succeeded_count: number;
  failed_count: number;
  source_frame_count: number;
  transition_state_frame_count: number;
  inferred_reaction_count: number;
  timings_ms: Record<string, number>;
  items: ArtifactBatchUploadItem[];
}

export type UploadBatchStatus = "active" | "paused" | "completed" | "cancelled";
export type UploadBatchItemStatus = "queued" | "uploading" | "succeeded" | "failed" | "cancelled";

export interface UploadBatchFileCreate {
  client_file_id: string;
  original_filename: string;
  relative_path: string;
  size_bytes: number;
  media_type: string;
}

export interface UploadBatchCreate {
  project_id: string;
  artifact_kind: ArtifactUploadResult["artifact_kind"];
  shared_metadata: Record<string, unknown>;
  files: UploadBatchFileCreate[];
}

export interface UploadBatch {
  id: string;
  created_at: string;
  updated_at: string;
  project_id: string;
  created_by_user_id: string;
  artifact_kind: ArtifactUploadResult["artifact_kind"];
  status: UploadBatchStatus;
  shared_metadata: Record<string, unknown>;
  total_count: number;
  total_bytes: number;
  succeeded_count: number;
  failed_count: number;
  cancelled_count: number;
  uploading_count: number;
}

export interface UploadBatchPage {
  items: UploadBatch[];
  total: number;
  limit: number;
  offset: number;
}

export interface UploadBatchItem {
  id: string;
  created_at: string;
  updated_at: string;
  client_file_id: string;
  position: number;
  original_filename: string;
  relative_path: string;
  size_bytes: number;
  media_type: string;
  status: UploadBatchItemStatus;
  attempt_count: number;
  artifact_file_id: string | null;
  ingestion_status: ArtifactSummary["ingestion_status"];
  ingestion_error_message: string | null;
  error_code: string | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
}

export interface UploadBatchItemPage {
  items: UploadBatchItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface HealthStatus {
  status: string;
  version: string;
  database: string;
  postgresql_version: string;
  rdkit_extension_version: string;
}
