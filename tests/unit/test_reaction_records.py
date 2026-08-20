from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tricycle_reaction_db.application.dtos import (
    CreateReactionCommand,
    LogicalReactionParticipantRecord,
    ManifestArtifactBindingRecord,
    MappedReactionNodeGeometryMappingRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services.reactions import (
    _mapped_reaction_from_smiles,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    WorkflowManifestStatus,
)


def test_manifest_record_requires_a_timezone_aware_publication_timestamp() -> None:
    common = {
        "manifest_key": "da-bench:ene_diene:00",
        "revision": 1,
        "schema_version": "da-bench-manifest-v1",
        "payload_sha256": "1" * 64,
        "qc_policy_version": "cycloaddition-qc-v1",
    }

    with pytest.raises(ValidationError, match="requires published_at"):
        WorkflowManifestRecord(**common, status=WorkflowManifestStatus.PUBLISHED)
    with pytest.raises(ValidationError, match="timezone info"):
        WorkflowManifestRecord(
            **common,
            status=WorkflowManifestStatus.PUBLISHED,
            published_at=datetime(2026, 7, 13),
        )
    with pytest.raises(ValidationError, match="cannot have published_at"):
        WorkflowManifestRecord(
            **common,
            status=WorkflowManifestStatus.VALIDATED,
            published_at=datetime.now(UTC),
        )


def test_create_reaction_command_requires_only_a_reaction_representation() -> None:
    command = CreateReactionCommand(reaction="C1CC1>>C=CC")

    assert command.reaction == "C1CC1>>C=CC"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateReactionCommand(
            reaction="C1CC1>>C=CC",
            reactants=[{"topology_id": "00000000-0000-0000-0000-000000000000"}],
        )


def test_binding_record_requires_complete_selectors_without_software_authority() -> None:
    common = {
        "artifact_key": "orca-ts-sp",
        "artifact_role": ManifestArtifactRole.ORCA_SINGLE_POINT,
        "reaction_key": "reaction-0",
        "path_key": "path-0",
        "node_key": "ts",
    }

    declared = ManifestArtifactBindingRecord(**common)
    assert declared.source_geometry_artifact_key is None
    with pytest.raises(ValidationError, match="must either both be set"):
        ManifestArtifactBindingRecord(
            **common,
            source_geometry_artifact_key="gaussian-ts",
            segment_index=1,
        )
    with pytest.raises(ValidationError, match="requires an expected hash"):
        ManifestArtifactBindingRecord(
            **common,
            source_geometry_artifact_key="gaussian-ts",
            segment_index=1,
            frame_index=18,
            resolution_status=ArtifactResolutionStatus.RESOLVED,
        )


def test_logical_participant_record_rejects_invalid_stoichiometry_and_side_roles() -> None:
    common = {
        "side": LogicalReactionParticipantSide.REACTANT,
        "participant_index": 0,
    }

    with pytest.raises(ValidationError, match="coefficient=1"):
        LogicalReactionParticipantRecord(
            **common,
            stoichiometric_coefficient=2,
        )
    with pytest.raises(ValidationError, match="product role requires"):
        LogicalReactionParticipantRecord(
            **common,
            role=LogicalReactionParticipantRole.PRODUCT,
        )


def test_coordinate_mapping_record_requires_unique_positive_geometry_maps() -> None:
    common = {
        "mapped_smiles": "[C:1][O:2]",
        "mapping_method": "manifest-explicit",
        "mapping_version": "coordinate-map-v1",
        "verified": True,
    }
    with pytest.raises(ValidationError, match="positive"):
        MappedReactionNodeGeometryMappingRecord(
            **common,
            geometry_atom_map_numbers=[0, 2],
        )
    with pytest.raises(ValidationError, match="unique"):
        MappedReactionNodeGeometryMappingRecord(
            **common,
            geometry_atom_map_numbers=[1, 1],
        )


def test_mapped_reaction_uses_rdkit_reaction_parser_with_agents() -> None:
    reaction = _mapped_reaction_from_smiles(
        "[CH3:1][OH:2]>O>[CH2:1]=[O:2]",
    )

    assert reaction.GetNumReactantTemplates() == 1
    assert reaction.GetNumAgentTemplates() == 1
    assert reaction.GetNumProductTemplates() == 1

    with pytest.raises(ValueError, match="RDKit could not parse"):
        _mapped_reaction_from_smiles("not-a-reaction")
