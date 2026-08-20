from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.orm import Mapper
from sqlmodel import SQLModel

from tricycle_reaction_db.db.models import (
    ArtifactFile,
    AtomicPopulationSeries,
    BondOrderResult,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    ChargeSpinPopulationResult,
    ElectronicConfiguration,
    ElectronicState,
    ElectronicStateSet,
    ExternalIdentity,
    Geometry,
    ImplicitSolvationResult,
    LogicalReaction,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularFormula,
    MolecularOrbitalResult,
    MolecularTopology,
    MultireferenceResult,
    NMRResult,
    NMRShieldingTensor,
    Organization,
    OrganizationMembership,
    ParseRevision,
    PolarizabilityResult,
    Project,
    ProjectMembership,
    ScientificArray,
    ScientificArrayAssignment,
    SinglePointPropertyResult,
    ThermochemistryResult,
    TotalSpinResult,
    UserAccount,
    WorkflowManifest,
)


def _mapper(model: type[SQLModel]) -> Mapper[Any]:
    return cast(Mapper[Any], inspect(model))


def _pairs(model: type[SQLModel], name: str) -> set[tuple[str, str]]:
    pairs = _mapper(model).relationships[name].local_remote_pairs
    assert pairs is not None
    return {(local.name, remote.name) for local, remote in pairs}


def test_all_sqlmodel_relationships_are_bidirectional() -> None:
    models = (
        MolecularFormula,
        MolecularTopology,
        Geometry,
        ArtifactFile,
        CalculationProtocol,
        ParseRevision,
        CalculationSegment,
        CalculationFrame,
        ScientificArray,
        ScientificArrayAssignment,
        ThermochemistryResult,
        MolecularOrbitalResult,
        ChargeSpinPopulationResult,
        AtomicPopulationSeries,
        PolarizabilityResult,
        NMRResult,
        NMRShieldingTensor,
        BondOrderResult,
        TotalSpinResult,
        SinglePointPropertyResult,
        ElectronicStateSet,
        ElectronicState,
        ElectronicConfiguration,
        MultireferenceResult,
        ImplicitSolvationResult,
        WorkflowManifest,
        ManifestArtifactBinding,
        LogicalReaction,
        LogicalReactionParticipant,
        MappedReaction,
        MappedReactionParticipant,
        MappedReactionNode,
        MappedReactionNodeGeometry,
        MappedReactionNodeGeometryMapping,
        MappedReactionEdge,
        UserAccount,
        ExternalIdentity,
        Organization,
        OrganizationMembership,
        Project,
        ProjectMembership,
    )
    for model in models:
        for relationship in _mapper(model).relationships:
            assert relationship.back_populates is not None
            reverse = relationship.mapper.relationships[relationship.back_populates]
            assert reverse.back_populates == relationship.key


def test_reaction_axis_has_explicit_topology_and_mapping_foreign_keys() -> None:
    assert _pairs(LogicalReaction, "participants") == {("id", "logical_reaction_id")}
    assert _pairs(LogicalReactionParticipant, "topology") == {("topology_id", "id")}
    assert _pairs(MolecularTopology, "logical_reaction_participants") == {("id", "topology_id")}
    assert _pairs(MappedReaction, "logical_reaction") == {("logical_reaction_id", "id")}
    assert _pairs(MappedReactionParticipant, "logical_reaction_participant") == {
        ("logical_reaction_participant_id", "id")
    }
    assert _pairs(MappedReactionNodeGeometry, "mapped_reaction_participant") == {
        ("mapped_reaction_participant_id", "id")
    }
    assert _pairs(MappedReactionNodeGeometry, "geometry") == {("geometry_id", "id")}


def test_node_geometry_references_geometry_without_reaction_owned_calculations() -> None:
    assert _pairs(MappedReactionNodeGeometry, "geometry") == {("geometry_id", "id")}
    assert "calculation_bindings" not in _mapper(MappedReactionNodeGeometry).relationships
    assert "mapped_reaction_node_calculations" not in _mapper(CalculationFrame).relationships


def test_mapped_reaction_edges_enforce_same_parent() -> None:
    for name, column in {
        "source_node": "source_node_id",
        "target_node": "target_node_id",
        "transition_state_node": "transition_state_node_id",
    }.items():
        assert _pairs(MappedReactionEdge, name) == {
            ("mapped_reaction_id", "mapped_reaction_id"),
            (column, "id"),
        }


def test_scientific_array_assignments_have_real_result_foreign_keys() -> None:
    assert _pairs(ScientificArray, "assignment") == {("id", "scientific_array_id")}
    assert _pairs(AtomicPopulationSeries, "array_assignments") == {
        ("id", "atomic_population_series_id")
    }
    assert _pairs(NMRShieldingTensor, "array_assignments") == {("id", "nmr_shielding_tensor_id")}
    assert _pairs(ElectronicState, "array_assignments") == {("id", "electronic_state_id")}


def test_identity_and_project_access_relationships_are_explicit() -> None:
    assert _pairs(UserAccount, "identities") == {("id", "user_id")}
    assert _pairs(OrganizationMembership, "organization") == {("organization_id", "id")}
    assert _pairs(OrganizationMembership, "user") == {("user_id", "id")}
    assert _pairs(Project, "organization") == {("organization_id", "id")}
    assert _pairs(ProjectMembership, "project") == {("project_id", "id")}
    assert _pairs(ProjectMembership, "user") == {("user_id", "id")}
    assert _pairs(ArtifactFile, "project") == {("project_id", "id")}
    assert _pairs(ArtifactFile, "created_by_user") == {("created_by_user_id", "id")}
