"""Stable read-only DTOs shared by REST, GraphQL, and MCP query surfaces."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tricycle_reaction_db.domain.precision import EnergyHartree


class QueryView(BaseModel):
    model_config = ConfigDict(frozen=True)


class PageInfo(QueryView):
    # Cursor pages intentionally do not run an exact COUNT query.
    total: int = Field(default=-1, ge=-1)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ArtifactSummary(QueryView):
    id: UUID
    project_id: UUID
    created_by_user_id: UUID
    visibility: str
    original_filename: str
    content_sha256: str
    size_bytes: int
    media_type: str
    artifact_kind: str
    storage_status: str
    storage_verified_at: datetime | None = None
    preview_available: bool
    ingestion_status: str | None = None
    source_frame_count: int | None = None
    transition_state_frame_count: int | None = None
    ingestion_error_code: str | None = None
    ingestion_error_message: str | None = None


class ArtifactPreview(QueryView):
    id: UUID
    original_filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    preview_text: str
    preview_bytes: int
    truncated: bool


class ArtifactPage(QueryView):
    items: list[ArtifactSummary]
    page: PageInfo


class MolecularFormulaSummary(QueryView):
    id: UUID
    hill_formula: str
    atom_count: int
    composition_hash: str
    element_count_vector: list[int]


class MolecularFormulaPage(QueryView):
    items: list[MolecularFormulaSummary]
    page: PageInfo


class MolecularFormulaDetail(MolecularFormulaSummary):
    topology_count: int


class MolecularTopologySearchResult(QueryView):
    id: UUID
    formula_id: UUID
    hill_formula: str
    formula_composition_hash: str
    canonical_isomeric_smiles: str | None = None
    graph_hash: str
    atom_count: int
    heavy_atom_count: int
    formal_charge: int
    radical_electron_count: int
    fragment_count: int
    stereo_status: str
    sanitization_status: str
    sanitization_error: str | None = None
    substructure_match_count: int | None = None
    morgan_bfp_schema_version: str
    morgan_bfp_available: bool
    similarity_score: float | None = None
    molecular_weight: float | None = None
    logp: float | None = None
    tpsa: float | None = None
    hba_count: int | None = None
    hbd_count: int | None = None
    ring_count: int | None = None
    scaffold_smiles: str | None = None


class MolecularTopologySearchPage(QueryView):
    items: list[MolecularTopologySearchResult]
    page: PageInfo


class MolecularTopologyDetail(MolecularTopologySearchResult):
    geometry_count: int
    logical_reaction_count: int
    derivation_count: int


class GeometrySummary(QueryView):
    id: UUID
    topology_id: UUID
    canonical_isomeric_smiles: str | None = None
    atom_count: int
    geometry_hash: str
    internal_coordinate_hash: str
    canonicalization_version: str
    charge: int
    multiplicity: int
    calculation_count: int
    reaction_binding_count: int
    is_transition_state: bool
    imaginary_frequency_status: str
    similarity_score: float | None = None


class GeometryPage(QueryView):
    items: list[GeometrySummary]
    page: PageInfo


class GeometryAtomCoordinate(QueryView):
    """One Geometry conformer atom in canonical topology atom order."""

    atom_index: int
    element: str
    x_angstrom: float
    y_angstrom: float
    z_angstrom: float


class GeometryEnergyView(QueryView):
    geometry_id: UUID
    policy_version: str
    electronic_selection_status: str
    electronic_candidate_frame_ids: list[UUID]
    electronic_energy_hartree: EnergyHartree | None = None
    electronic_energy_source_frame_id: UUID | None = None
    electronic_energy_protocol_id: UUID | None = None
    charge: int | None = None
    multiplicity: int | None = None
    electronic_state_kind: str | None = None
    electronic_state_index: int | None = None
    thermochemistry_selection_status: str
    thermochemistry_candidate_frame_ids: list[UUID]
    thermochemistry_source_frame_id: UUID | None = None
    thermochemistry_protocol_id: UUID | None = None
    temperature_kelvin: float | None = None
    pressure_atm: float | None = None
    zpe_correction_hartree: EnergyHartree | None = None
    thermal_energy_correction_hartree: EnergyHartree | None = None
    thermal_enthalpy_correction_hartree: EnergyHartree | None = None
    thermal_gibbs_correction_hartree: EnergyHartree | None = None
    zero_point_energy_hartree: EnergyHartree | None = None
    thermal_internal_energy_hartree: EnergyHartree | None = None
    enthalpy_hartree: EnergyHartree | None = None
    gibbs_free_energy_hartree: EnergyHartree | None = None
    entropy_cal_mol_k: float | None = None


class GeometryDetail(GeometrySummary):
    # Forward reference keeps the geometry DTO near its identity fields while
    # frame summaries remain the canonical calculation view below.
    frames: list["CalculationFrameSummary"]
    energy_view: GeometryEnergyView
    coordinates: list[GeometryAtomCoordinate]


class LogicalReactionSummary(QueryView):
    id: UUID
    reaction_key: str
    label: str | None = None
    reaction_class: str | None = None
    cycloaddition_pattern: str | None = None
    reaction_hash: str
    similarity_score: float | None = None
    # True when canonical reactant/product topology multisets differ.
    reactant_product_changed: bool | None = None
    created_at: datetime | None = None
    # Lightweight path-preview identities used by catalog cards. Detail
    # endpoints remain the source of the complete participant/mapping DTOs.
    reactant_topology_ids: list[UUID] = Field(default_factory=list)
    product_topology_ids: list[UUID] = Field(default_factory=list)
    transition_state_geometry_id: UUID | None = None
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None


class LogicalReactionPage(QueryView):
    items: list[LogicalReactionSummary]
    page: PageInfo


class LogicalReactionParticipantView(QueryView):
    id: UUID
    side: str
    participant_index: int
    role: str | None = None
    topology_id: UUID
    canonical_isomeric_smiles: str | None = None
    stoichiometric_coefficient: int


class MappedReactionSummary(QueryView):
    id: UUID
    logical_reaction_id: UUID
    mapped_reaction_key: str
    label: str | None = None
    mapped_reaction_kind: str
    mapped_reaction_smiles: str
    mapping_hash: str
    reaction_structural_bfp_schema_version: str
    # This mirrors the parent logical reaction's canonical topology comparison.
    reactant_product_changed: bool | None = None
    created_at: datetime | None = None
    reaction_smarts_match: bool | None = None
    similarity_score: float | None = None
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None


class MappedReactionPage(QueryView):
    items: list[MappedReactionSummary]
    page: PageInfo


class LogicalReactionDetail(LogicalReactionSummary):
    participants: list[LogicalReactionParticipantView]
    mapped_reactions: list[MappedReactionSummary]


class CalculationProtocolView(QueryView):
    id: UUID
    qm_software: str
    qm_software_version: str
    method_family: str | None = None
    method: str | None = None
    reference_method: str | None = None
    functional: str | None = None
    basis_set: str | None = None
    auxiliary_basis_set: str | None = None
    dispersion_model: str | None = None
    solvation_model: str | None = None
    solvent: str | None = None
    task_requests: list[str]


class CalculationProtocolSummary(CalculationProtocolView):
    protocol_hash: str


class CalculationProtocolPage(QueryView):
    items: list[CalculationProtocolSummary]
    page: PageInfo


class CalculationProtocolDetail(CalculationProtocolSummary):
    normalized_spec_json: str
    segment_count: int


class CalculationFrameSummary(QueryView):
    id: UUID
    artifact_file_id: UUID
    original_filename: str
    parse_revision_id: UUID
    segment_id: UUID
    segment_index: int
    frame_index: int
    file_frame_index: int
    frame_role: str
    geometry_id: UUID
    topology_id: UUID
    topology_derivation_id: UUID
    protocol_id: UUID | None = None
    canonical_isomeric_smiles: str | None = None
    charge: int
    multiplicity: int
    coordinate_decimal_places: int | None = None
    scf_status: str
    optimization_status: str
    selected_energy_hartree: EnergyHartree | None = None
    selected_energy_kind: str | None = None
    frequency_count: int | None = None
    negative_frequency_count: int | None = None
    running_time_seconds: float | None = None


class CalculationFramePage(QueryView):
    items: list[CalculationFrameSummary]
    page: PageInfo


class TransitionStateEndpointView(QueryView):
    direction: str
    topology_id: UUID
    charge: int
    multiplicity: int
    atom_count: int
    displacement_ratio: float
    source_coordinate_hash: str
    source_to_topology_atom_indices: list[int]


class ArtifactIngestionSummary(QueryView):
    id: UUID
    artifact_file_id: UUID
    status: str
    molop_version: str
    source_frame_count: int | None = None
    transition_state_frame_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ArtifactIngestionPage(QueryView):
    items: list[ArtifactIngestionSummary]
    page: PageInfo


class TransitionStateInferenceSummary(QueryView):
    id: UUID
    artifact_ingestion_id: UUID
    parse_revision_id: UUID
    file_frame_index: int
    imaginary_mode_index: int
    imaginary_frequency_cm1: float
    status: str
    logical_reaction_id: UUID | None = None
    mapped_reaction_id: UUID | None = None
    calculation_frame_id: UUID | None = None
    reactant_product_changed: bool | None = None
    error_code: str | None = None
    error_message: str | None = None


class TransitionStateInferencePage(QueryView):
    items: list[TransitionStateInferenceSummary]
    page: PageInfo


class ParseRevisionSummary(QueryView):
    id: UUID
    artifact_file_id: UUID
    revision_number: int
    reparse_of_id: UUID | None = None
    export_schema_version: str
    parser_name: str
    parser_version: str
    molop_version: str
    source_format: str
    parse_completeness: str
    status: str
    record_sha256: str | None = None
    running_time_seconds: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ParseRevisionPage(QueryView):
    items: list[ParseRevisionSummary]
    page: PageInfo


class CalculationSegmentSummary(QueryView):
    id: UUID
    parse_revision_id: UUID
    protocol_id: UUID | None = None
    segment_index: int
    segment_label: str | None = None
    source_frame_count: int | None = None
    parse_completeness: str
    termination_status: str
    scf_status: str
    source_start_line: int | None = None
    source_end_line: int | None = None


class CalculationSegmentPage(QueryView):
    items: list[CalculationSegmentSummary]
    page: PageInfo


class SourceSpanView(QueryView):
    start_byte: int | None = None
    end_byte: int | None = None
    start_char: int | None = None
    end_char: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    block_sha256: str | None = None


class MolecularTopologyDerivationView(QueryView):
    id: UUID
    reconstruction_method: str
    reconstruction_version: str
    reconstruction_metadata_json: str
    provenance_schema_version: str
    provenance_hash: str


class FrameEnergyView(QueryView):
    electronic_energy_hartree: EnergyHartree | None = None
    reference_energy_hartree: EnergyHartree | None = None
    mp2_energy_hartree: EnergyHartree | None = None
    mp3_energy_hartree: EnergyHartree | None = None
    mp4_energy_hartree: EnergyHartree | None = None
    mp5_energy_hartree: EnergyHartree | None = None
    ccsd_energy_hartree: EnergyHartree | None = None
    ccsd_t_energy_hartree: EnergyHartree | None = None


class EnergyObservationView(QueryView):
    observation_index: int
    method: str
    quantity_semantics: str
    value_hartree: EnergyHartree
    source_label: str


class GeometryOptimizationView(QueryView):
    geometry_optimized: bool | None = None
    convergence_multiplier: float
    energy_change_hartree: float | None = None
    energy_change_threshold_hartree: float | None = None
    energy_change_converged: bool | None = None
    rms_force_hartree_per_bohr: float | None = None
    rms_force_threshold_hartree_per_bohr: float | None = None
    rms_force_converged: bool | None = None
    max_force_hartree_per_bohr: float | None = None
    max_force_threshold_hartree_per_bohr: float | None = None
    max_force_converged: bool | None = None
    rms_displacement_bohr: float | None = None
    rms_displacement_threshold_bohr: float | None = None
    rms_displacement_converged: bool | None = None
    max_displacement_bohr: float | None = None
    max_displacement_threshold_bohr: float | None = None
    max_displacement_converged: bool | None = None


class VibrationView(QueryView):
    mode_count: int
    imaginary_mode_count: int
    lowest_frequency_cm1: float | None = None
    mode_indices: list[int]
    axis_order: list[str] | None = None
    atom_order: str | None = None
    normalization: str | None = None
    mass_weighting: str | None = None


class ThermochemistryView(QueryView):
    temperature_kelvin: float
    pressure_atm: float
    zpe_correction_hartree: EnergyHartree | None = None
    thermal_energy_correction_hartree: EnergyHartree | None = None
    thermal_enthalpy_correction_hartree: EnergyHartree | None = None
    thermal_gibbs_correction_hartree: EnergyHartree | None = None
    zero_point_energy_hartree: EnergyHartree | None = None
    thermal_internal_energy_hartree: EnergyHartree | None = None
    enthalpy_hartree: EnergyHartree | None = None
    gibbs_free_energy_hartree: EnergyHartree | None = None
    entropy_cal_mol_k: float | None = None
    heat_capacity_cv_cal_mol_k: float | None = None
    molecular_mass_amu: float | None = None
    rotational_symmetry_number: int | None = None


class CalculationStatusView(QueryView):
    scf_converged: bool | None = None
    normal_terminated: bool | None = None


class ScientificArraySummary(QueryView):
    id: UUID
    kind: str
    ordinal: int
    unit: str
    dtype: str
    shape: list[int]
    array_nbytes: int
    payload_sha256: str
    owner_kind: str | None = None
    owner_id: UUID | None = None
    slot: str | None = None
    slot_ordinal: int | None = None
    # Scalar provenance fields keep the NexusX schema portable (it does not
    # expose arbitrary JSON mappings) while retaining MolOP population names.
    source_field: str | None = None
    source_unit: str | None = None
    population_name: str | None = None
    population_scheme: str | None = None
    population_quantity: str | None = None
    population_spin_channel: str | None = None
    population_source_label: str | None = None


class ScientificArrayPreview(QueryView):
    id: UUID
    kind: str
    unit: str
    dtype: str
    shape: list[int]
    total_elements: int
    values: list[Any]
    truncated: bool


class ScientificArrayPage(QueryView):
    items: list[ScientificArraySummary]
    page: PageInfo


class MolecularOrbitalResultView(QueryView):
    id: UUID
    electronic_state: str | None = None
    alpha_orbital_count: int
    beta_orbital_count: int
    coefficient_count: int
    alpha_occupancies: list[float | None]
    beta_occupancies: list[float | None]
    alpha_symmetries: list[str | None]
    beta_symmetries: list[str | None]
    source_schema_version: str
    scientific_arrays: list[ScientificArraySummary]


class AtomicPopulationSeriesView(QueryView):
    id: UUID
    series_key: str
    scheme: str
    quantity: str
    unit: str = "dimensionless"
    value_count: int
    spin_channel: str | None = None
    source_label: str | None = None
    series_metadata_json: str
    scientific_arrays: list[ScientificArraySummary]


class ChargeSpinPopulationResultView(QueryView):
    id: UUID
    series_count: int
    source_schema_version: str
    series: list[AtomicPopulationSeriesView]


class PolarizabilityResultView(QueryView):
    id: UUID
    electronic_spatial_extent_bohr2: float | None = None
    isotropic_polarizability_bohr3: float | None = None
    anisotropic_polarizability_bohr3: float | None = None
    source_schema_version: str
    scientific_arrays: list[ScientificArraySummary]


class NMRShieldingTensorView(QueryView):
    id: UUID
    atom_index: int
    atom_symbol: str
    isotropic_ppm: float | None = None
    anisotropy_ppm: float | None = None
    anisotropy_convention: str | None = None
    orientation: str
    scientific_arrays: list[ScientificArraySummary]


class NMRResultView(QueryView):
    id: UUID
    gauge: str | None = None
    shielding_count: int
    coupling_atom_indices: list[int]
    source_schema_version: str
    shielding_tensors: list[NMRShieldingTensorView]
    scientific_arrays: list[ScientificArraySummary]


class BondOrderResultView(QueryView):
    id: UUID
    matrix_count: int
    source_schema_version: str
    scientific_arrays: list[ScientificArraySummary]


class TotalSpinResultView(QueryView):
    id: UUID
    spin_square: float | None = None
    spin_quantum_number: float | None = None
    source_schema_version: str


class SinglePointPropertyResultView(QueryView):
    id: UUID
    vertical_ionization_potential_ev: float | None = None
    vertical_electron_affinity_ev: float | None = None
    global_electrophilicity_index_ev: float | None = None
    source_schema_version: str
    scientific_arrays: list[ScientificArraySummary]


class ElectronicConfigurationView(QueryView):
    id: UUID
    configuration_ordinal: int
    label: str | None = None
    coefficient: float | None = None
    weight: float | None = None
    occupation: list[float]
    orbital_indices: list[int]
    raw: str


class ElectronicStateView(QueryView):
    id: UUID
    state_ordinal: int
    state_index: int | None = None
    root: int | None = None
    label: str | None = None
    multiplicity: int | None = None
    spin: float | None = None
    irrep: str | None = None
    method: str | None = None
    energy_hartree: EnergyHartree | None = None
    excitation_energy_ev: float | None = None
    oscillator_strength: float | None = None
    state_properties_json: str
    source: str | None = None
    configurations: list[ElectronicConfigurationView]
    scientific_arrays: list[ScientificArraySummary]


class ElectronicStateSetView(QueryView):
    id: UUID
    kind: str
    state_count: int
    source_schema_version: str
    states: list[ElectronicStateView]


class MultireferenceResultView(QueryView):
    id: UUID
    electronic_state_set_id: UUID | None = None
    method: str | None = None
    reference_method: str | None = None
    ci_type: str | None = None
    active_space_electrons: int | None = None
    active_space_orbitals: int | None = None
    active_space_roots: int | None = None
    active_orbitals: list[int]
    inactive_orbitals: list[int]
    frozen_orbitals: list[int]
    active_space_raw: str
    active_space_options_json: str
    corrections_json: str
    diagnostics: list[str]
    result_properties_json: str
    source_schema_version: str


class ImplicitSolvationResultView(QueryView):
    id: UUID
    solvent: str | None = None
    solvent_model: str | None = None
    atomic_radii: str | None = None
    solvent_epsilon: float | None = None
    solvent_epsilon_infinite: float | None = None
    source_schema_version: str


class CalculationResultSummary(QueryView):
    frame: CalculationFrameSummary
    result_kinds: list[str]


class CalculationResultPage(QueryView):
    items: list[CalculationResultSummary]
    page: PageInfo


class CalculationResultDetail(CalculationResultSummary):
    molecular_orbitals: MolecularOrbitalResultView | None = None
    charge_spin_populations: ChargeSpinPopulationResultView | None = None
    polarizability: PolarizabilityResultView | None = None
    nmr: NMRResultView | None = None
    bond_orders: BondOrderResultView | None = None
    total_spin: TotalSpinResultView | None = None
    single_point_properties: SinglePointPropertyResultView | None = None
    electronic_state_sets: list[ElectronicStateSetView]
    multireference: MultireferenceResultView | None = None
    implicit_solvation: ImplicitSolvationResultView | None = None


class ManifestArtifactBindingSummary(QueryView):
    id: UUID
    workflow_manifest_id: UUID
    artifact_key: str
    artifact_file_id: UUID | None = None
    expected_content_sha256: str | None = None
    artifact_role: str
    reaction_key: str
    path_key: str
    node_key: str
    segment_index: int | None = None
    frame_index: int | None = None
    source_geometry_artifact_key: str | None = None
    resolution_status: str
    created_at: datetime | None = None


class ManifestArtifactBindingPage(QueryView):
    items: list[ManifestArtifactBindingSummary]
    page: PageInfo


class ManifestArtifactBindingDetail(ManifestArtifactBindingSummary):
    source_geometry_binding_id: UUID | None = None
    dependent_binding_ids: list[UUID]


class WorkflowManifestSummary(QueryView):
    id: UUID
    artifact_file_id: UUID
    manifest_key: str
    revision: int
    schema_version: str
    payload_sha256: str
    qc_policy_version: str
    status: str
    supersedes_id: UUID | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    artifact_binding_count: int


class WorkflowManifestPage(QueryView):
    items: list[WorkflowManifestSummary]
    page: PageInfo


class WorkflowManifestDetail(WorkflowManifestSummary):
    validation_metadata_json: str
    revisions: list[WorkflowManifestSummary]
    artifact_bindings: list[ManifestArtifactBindingSummary]


class StorageGarbageCollectionRunSummary(QueryView):
    id: UUID
    state_id: UUID
    bucket: str
    root_prefix: str
    started_at: datetime
    completed_at: datetime | None = None
    scan_after: datetime
    scan_until: datetime
    status: str
    objects_seen: int
    objects_deleted: int
    objects_retained: int
    objects_failed: int
    error_message: str | None = None
    created_at: datetime | None = None


class StorageGarbageCollectionRunPage(QueryView):
    items: list[StorageGarbageCollectionRunSummary]
    page: PageInfo


class StorageGarbageCollectionStateSummary(QueryView):
    id: UUID
    bucket: str
    root_prefix: str
    watermark_at: datetime
    updated_at: datetime
    last_successful_run_id: UUID | None = None
    latest_run_id: UUID | None = None
    latest_run_status: str | None = None
    latest_failed_run_id: UUID | None = None
    run_count: int
    created_at: datetime | None = None


class StorageGarbageCollectionStatePage(QueryView):
    items: list[StorageGarbageCollectionStateSummary]
    page: PageInfo


class StorageGarbageCollectionStateDetail(StorageGarbageCollectionStateSummary):
    recent_runs: list[StorageGarbageCollectionRunSummary]


class MolecularTopologyDerivationSummary(QueryView):
    id: UUID
    topology_id: UUID
    canonical_isomeric_smiles: str | None = None
    reconstruction_method: str
    reconstruction_version: str
    provenance_schema_version: str
    provenance_hash: str
    referenced_geometry_count: int
    calculation_frame_count: int
    created_at: datetime | None = None


class MolecularTopologyDerivationPage(QueryView):
    items: list[MolecularTopologyDerivationSummary]
    page: PageInfo


class MolecularTopologyDerivationDetail(MolecularTopologyDerivationSummary):
    reconstruction_metadata_json: str


class CalculationFrameDetail(CalculationFrameSummary):
    source_span: SourceSpanView | None = None
    parse_completeness: str
    geometry_assignment_kind: str
    observed_coordinate_hash: str
    observed_to_geometry_atom_indices: list[int]
    observed_to_geometry_transform: list[float]
    geometry_assignment_rmsd_angstrom: float
    geometry_assignment_max_abs_angstrom: float
    geometry_assignment_policy_version: str
    electronic_state_kind: str
    electronic_state_index: int
    topology_derivation: MolecularTopologyDerivationView
    protocol: CalculationProtocolView | None = None
    energy: FrameEnergyView | None = None
    energy_observations: list[EnergyObservationView]
    optimization: GeometryOptimizationView | None = None
    vibration: VibrationView | None = None
    transition_state_endpoints: list[TransitionStateEndpointView]
    thermochemistry: ThermochemistryView | None = None
    calculation_status: CalculationStatusView | None = None
    scientific_arrays: list[ScientificArraySummary]


class NodeGeometryMappingView(QueryView):
    id: UUID
    geometry_atom_map_numbers: list[int]
    mapped_smiles: str
    mapping_method: str
    mapping_version: str
    verified: bool


class MappedReactionNodeGeometryView(QueryView):
    id: UUID
    component_key: str
    component_index: int
    coordinate_index: int
    is_primary: bool
    mapped_reaction_participant_id: UUID | None = None
    logical_reaction_participant_id: UUID | None = None
    participant_role: str | None = None
    geometry_id: UUID
    topology_id: UUID
    canonical_isomeric_smiles: str | None = None
    mappings: list[NodeGeometryMappingView]
    calculations: list[CalculationFrameSummary]
    energy_view: GeometryEnergyView


class NodeAdditivePropertiesView(QueryView):
    component_count: int = Field(ge=1)
    policy_version: str
    source_levels_compatible: bool
    electronic_energy_hartree: EnergyHartree | None = None
    temperature_kelvin: float | None = None
    pressure_atm: float | None = None
    zero_point_energy_hartree: EnergyHartree | None = None
    thermal_internal_energy_hartree: EnergyHartree | None = None
    enthalpy_hartree: EnergyHartree | None = None
    gibbs_free_energy_hartree: EnergyHartree | None = None
    entropy_cal_mol_k: float | None = None


class MappedReactionNodeView(QueryView):
    id: UUID
    node_key: str
    node_index: int
    role: str
    geometries: list[MappedReactionNodeGeometryView]
    additive_properties: NodeAdditivePropertiesView | None = None


class MappedReactionParticipantView(QueryView):
    id: UUID
    logical_reaction_participant_id: UUID
    side: str
    template_index: int
    topology_id: UUID
    atom_map_numbers: list[int]
    mapped_smiles: str


class MappedReactionEdgeView(QueryView):
    id: UUID
    edge_key: str
    source_node_id: UUID
    target_node_id: UUID
    transition_state_node_id: UUID | None = None
    edge_kind: str


class MappedReactionDetail(MappedReactionSummary):
    reaction_key: str
    participants: list[MappedReactionParticipantView]
    nodes: list[MappedReactionNodeView]
    edges: list[MappedReactionEdgeView]


class ReactionEnergyPoint(QueryView):
    node_id: UUID
    node_key: str
    node_index: int
    role: str
    energy_kind: str
    energy_hartree: EnergyHartree | None = None
    relative_energy_kcal_mol: float | None = None


class ReactionEnergyEdgeView(QueryView):
    edge_id: UUID
    edge_key: str
    source_node_id: UUID
    target_node_id: UUID
    transition_state_node_id: UUID | None = None
    reaction_energy_kcal_mol: float | None = None
    forward_barrier_kcal_mol: float | None = None
    reverse_barrier_kcal_mol: float | None = None


class ReactionEnergyProfile(QueryView):
    mapped_reaction_id: UUID
    energy_kind: str
    reference_node_id: UUID | None = None
    points: list[ReactionEnergyPoint]
    edges: list[ReactionEnergyEdgeView]


class ThermodynamicTopologyMinimumView(QueryView):
    """Lowest-Gibbs complete Geometry selected for one mapped component."""

    side: str
    mapped_reaction_participant_id: UUID | None = None
    topology_id: UUID
    stoichiometric_coefficient: int = Field(ge=1)
    geometry_id: UUID
    enthalpy_hartree: EnergyHartree
    gibbs_free_energy_hartree: EnergyHartree
    entropy_cal_mol_k: float


class ThermodynamicStateView(QueryView):
    topologies: list[ThermodynamicTopologyMinimumView]
    enthalpy_hartree: EnergyHartree
    gibbs_free_energy_hartree: EnergyHartree
    entropy_cal_mol_k: float


class ThermodynamicDifferenceView(QueryView):
    enthalpy_kcal_mol: float
    gibbs_free_energy_kcal_mol: float
    entropy_cal_mol_k: float


class MappedReactionThermodynamicsProfile(QueryView):
    """One source-compatible thermodynamic profile for a mapped reaction."""

    mapped_reaction_id: UUID
    policy_version: str
    electronic_level: list[str | None] = Field(default_factory=list)
    thermochemistry_level: list[str | None] = Field(default_factory=list)
    level_of_theory: str
    temperature_kelvin: float
    pressure_atm: float
    reactants: ThermodynamicStateView
    transition_state: ThermodynamicStateView | None = None
    products: ThermodynamicStateView | None = None
    activation: ThermodynamicDifferenceView | None = None
    reaction: ThermodynamicDifferenceView | None = None
    # Runtime is summed from distinct source files for each state.  It is
    # intentionally separate from the per-frame running_time_seconds field.
    reactants_running_time_seconds: float | None = None
    transition_state_running_time_seconds: float | None = None
    products_running_time_seconds: float | None = None
    total_running_time_seconds: float | None = None


class MappedReactionThermodynamics(QueryView):
    """All source-compatible profiles derived for one mapped reaction."""

    mapped_reaction_id: UUID
    profiles: list[MappedReactionThermodynamicsProfile]


class ThermodynamicDistributionBin(QueryView):
    """One numeric range used by a thermodynamic histogram."""

    lower: float
    upper: float
    count: int = Field(ge=0)


class ThermodynamicDistributionCategory(QueryView):
    """One categorical count in the thermodynamic overview."""

    label: str
    count: int = Field(ge=0)


class ThermodynamicScatterPoint(QueryView):
    """A profile with both kinetic and thermodynamic free-energy values."""

    mapped_reaction_id: UUID
    mapped_reaction_smiles: str
    activation_gibbs_free_energy_kcal_mol: float
    reaction_gibbs_free_energy_kcal_mol: float


class MappedReactionThermodynamicStatistics(QueryView):
    """Visibility-scoped aggregates for materialized reaction-path profiles."""

    mapped_reaction_count: int = Field(ge=0)
    profile_count: int = Field(ge=0)
    activation_profile_count: int = Field(ge=0)
    reaction_profile_count: int = Field(ge=0)
    complete_profile_count: int = Field(ge=0)
    activation_gibbs_free_energy_kcal_mol: list[ThermodynamicDistributionBin]
    reaction_gibbs_free_energy_kcal_mol: list[ThermodynamicDistributionBin]
    level_of_theory: list[ThermodynamicDistributionCategory]
    temperature_kelvin: list[ThermodynamicDistributionCategory]
    scatter: list[ThermodynamicScatterPoint]


__all__ = [
    "AtomicPopulationSeriesView",
    "ArtifactIngestionPage",
    "ArtifactIngestionSummary",
    "ArtifactPage",
    "ArtifactPreview",
    "ArtifactSummary",
    "CalculationFrameDetail",
    "CalculationFramePage",
    "CalculationFrameSummary",
    "TransitionStateEndpointView",
    "CalculationResultDetail",
    "CalculationResultPage",
    "CalculationResultSummary",
    "CalculationProtocolView",
    "CalculationProtocolSummary",
    "CalculationProtocolPage",
    "CalculationProtocolDetail",
    "CalculationSegmentSummary",
    "CalculationSegmentPage",
    "CalculationStatusView",
    "BondOrderResultView",
    "ChargeSpinPopulationResultView",
    "ElectronicConfigurationView",
    "ElectronicStateSetView",
    "ElectronicStateView",
    "EnergyObservationView",
    "FrameEnergyView",
    "GeometryOptimizationView",
    "ImplicitSolvationResultView",
    "GeometrySummary",
    "GeometryPage",
    "GeometryDetail",
    "GeometryEnergyView",
    "NodeGeometryMappingView",
    "NodeAdditivePropertiesView",
    "PageInfo",
    "LogicalReactionDetail",
    "LogicalReactionPage",
    "LogicalReactionParticipantView",
    "MappedReactionDetail",
    "MappedReactionEdgeView",
    "MappedReactionParticipantView",
    "MappedReactionNodeGeometryView",
    "MappedReactionNodeView",
    "MappedReactionSummary",
    "ManifestArtifactBindingPage",
    "ManifestArtifactBindingDetail",
    "ManifestArtifactBindingSummary",
    "ReactionEnergyPoint",
    "ReactionEnergyEdgeView",
    "ReactionEnergyProfile",
    "ThermodynamicTopologyMinimumView",
    "ThermodynamicStateView",
    "ThermodynamicDifferenceView",
    "MappedReactionThermodynamicsProfile",
    "MappedReactionThermodynamics",
    "MappedReactionThermodynamicStatistics",
    "ThermodynamicDistributionBin",
    "ThermodynamicDistributionCategory",
    "ThermodynamicScatterPoint",
    "MolecularTopologyDerivationView",
    "MolecularTopologyDerivationDetail",
    "MolecularTopologyDerivationPage",
    "MolecularTopologyDerivationSummary",
    "MolecularOrbitalResultView",
    "MolecularFormulaPage",
    "MolecularFormulaSummary",
    "MolecularFormulaDetail",
    "MolecularTopologySearchPage",
    "MolecularTopologySearchResult",
    "MolecularTopologyDetail",
    "LogicalReactionSummary",
    "MultireferenceResultView",
    "NMRResultView",
    "NMRShieldingTensorView",
    "PolarizabilityResultView",
    "ScientificArraySummary",
    "ScientificArrayPage",
    "ScientificArrayPreview",
    "SinglePointPropertyResultView",
    "SourceSpanView",
    "StorageGarbageCollectionRunPage",
    "StorageGarbageCollectionRunSummary",
    "StorageGarbageCollectionStateDetail",
    "StorageGarbageCollectionStatePage",
    "StorageGarbageCollectionStateSummary",
    "ThermochemistryView",
    "ParseRevisionSummary",
    "ParseRevisionPage",
    "TransitionStateInferenceSummary",
    "TransitionStateInferencePage",
    "TotalSpinResultView",
    "VibrationView",
    "WorkflowManifestDetail",
    "WorkflowManifestPage",
    "WorkflowManifestSummary",
]
