import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from httpx import ASGITransport, AsyncClient, Timeout
from httpx import Auth as HttpxAuth
from mcp.types import TextContent
from rdkit import Chem
from sqlalchemy import delete, text
from sqlmodel import col

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.mcp import mcp_dedicated_app
from tricycle_reaction_db.application.services import (
    ArtifactQueryService,
    CalculationProtocolQueryService,
    CalculationQueryService,
    CalculationResultQueryService,
    GeometryQueryService,
    LogicalReactionQueryService,
    MappedReactionQueryService,
    MolecularTopologyDerivationQueryService,
    ScientificArrayContentService,
    ScientificArrayNotFoundError,
    ScientificArrayQueryService,
    WorkflowManifestQueryService,
    get_geometry_dof_depiction,
    get_geometry_sdf,
    get_geometry_xyz,
)
from tricycle_reaction_db.application.services.authentication import (
    AuthenticatedPrincipal,
    AuthenticationService,
    reset_current_principal,
    reset_request_context_active,
    set_current_principal,
    set_request_context_active,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalReaction,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    Organization,
    ParseRevision,
    Project,
    ProjectMembership,
    ScientificArray,
    ThermochemistryResult,
    TransitionStateEndpoint,
    UserAccount,
    WorkflowManifest,
)
from tricycle_reaction_db.db.session import dispose_engine, session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactResolutionStatus,
    ArtifactVisibility,
    FrameRole,
    GeometryAssignmentKind,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    MappedReactionKind,
    MappedReactionNodeRole,
    ProjectRole,
    QMSoftware,
    ReactionClass,
    ScientificArrayKind,
    SourceFormat,
    StorageStatus,
    TransitionStateEndpointDirection,
    WorkflowManifestStatus,
)
from tricycle_reaction_db.domain.formulas import ELEMENT_COUNT_VECTOR_SIZE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@dataclass(frozen=True, slots=True)
class AuthorizationSample:
    principal: AuthenticatedPrincipal
    project_a_id: UUID
    project_b_id: UUID
    artifact_a_id: UUID
    artifact_b_id: UUID
    artifact_private_id: UUID
    public_artifact_id: UUID
    frame_a_id: UUID
    frame_b_id: UUID
    frame_private_id: UUID
    shared_geometry_id: UUID
    private_geometry_id: UUID
    shared_mapped_reaction_id: UUID
    shared_logical_reaction_id: UUID
    private_mapped_reaction_id: UUID
    private_logical_reaction_id: UUID
    scientific_array_a_id: UUID
    scientific_array_b_id: UUID
    protocol_a_id: UUID | None
    derivation_a_id: UUID
    manifest_id: UUID
    binding_id: UUID
    source_less_mapped_reaction_id: UUID
    source_less_logical_reaction_id: UUID
    transition_state_frame_id: UUID


@dataclass(frozen=True, slots=True)
class CalculationSource:
    artifact_id: UUID
    parse_revision_id: UUID
    frame_id: UUID
    scientific_array_id: UUID
    protocol_id: UUID
    derivation_id: UUID
    geometry_id: UUID


def _fixture_hash(value: str) -> str:
    return sha256(value.encode()).hexdigest()


async def _create_topology_and_geometry(
    session: object,
    *,
    suffix: str,
) -> tuple[MolecularFormula, MolecularTopology, MolecularTopologyDerivation, Geometry]:
    molecule = Chem.MolFromSmiles("[H][H]")
    assert molecule is not None
    element_count_vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
    element_count_vector[0] = 2
    formula = MolecularFormula(
        id=uuid4(),
        hill_formula="H2",
        composition=[{"atomic_number": 1, "isotope": 0, "count": 2}],
        atom_count=2,
        composition_hash=_fixture_hash(f"authorization-formula:{suffix}"),
        element_count_vector=element_count_vector,
    )
    assert formula.id is not None
    topology = MolecularTopology(
        id=uuid4(),
        formula_id=formula.id,
        formula=formula,
        mol=molecule,
        canonical_isomeric_smiles="[H][H]",
        graph_hash=_fixture_hash(f"authorization-topology:{suffix}"),
        identity_schema_version="authorization-test-v1",
        atom_count=2,
        heavy_atom_count=0,
        formal_charge=0,
        radical_electron_count=0,
        fragment_count=1,
    )
    assert topology.id is not None
    coordinates = np.array([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64)
    derivation = MolecularTopologyDerivation(
        id=uuid4(),
        topology_id=topology.id,
        topology=topology,
        reconstruction_method="test/authorization",
        reconstruction_version="1",
        reconstruction_metadata={"fixture": suffix},
        provenance_hash=_fixture_hash(f"authorization-derivation:{suffix}"),
    )
    geometry_molecule = Chem.Mol(molecule)
    conformer = Chem.Conformer(2)
    for atom_index, (x, y, z) in enumerate(coordinates):
        conformer.SetAtomPosition(atom_index, (float(x), float(y), float(z)))
    conformer.Set3D(True)
    geometry_molecule.AddConformer(conformer, assignId=True)
    geometry = Geometry(
        id=uuid4(),
        topology_id=topology.id,
        topology=topology,
        mol=geometry_molecule,
        internal_coordinates=coordinates,
        internal_coordinate_distances_angstrom=[0.74],
        internal_coordinate_angles_degrees=[0.0],
        internal_coordinate_dihedrals_degrees=[0.0],
        internal_coordinate_hash=_fixture_hash(f"authorization-internal:{suffix}"),
        geometry_hash=_fixture_hash(f"authorization-geometry:{suffix}"),
        canonicalization_version="authorization-test-v1",
    )
    session.add_all([formula, topology, derivation, geometry])  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    return formula, topology, derivation, geometry


async def _create_calculation_source(
    session: object,
    *,
    project_id: UUID,
    user_id: UUID,
    suffix: str,
    geometry: Geometry,
    derivation: MolecularTopologyDerivation,
) -> CalculationSource:
    content_hash = _fixture_hash(f"authorization-artifact:{suffix}")
    artifact = ArtifactFile(
        id=uuid4(),
        project_id=project_id,
        created_by_user_id=user_id,
        visibility=ArtifactVisibility.PROJECT,
        bucket="authorization-integration",
        object_key=f"authorization/{suffix}.log",
        content_sha256=content_hash,
        size_bytes=1,
        original_filename=f"authorization-{suffix}.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    )
    assert artifact.id is not None
    revision = ParseRevision(
        id=uuid4(),
        artifact_file_id=artifact.id,
        artifact_file=artifact,
        export_schema_version="authorization-test-v1",
        parser_version="test-v1",
        parser_id="tests.authorization",
        molop_version="test-v1",
        rdkit_version="test-v1",
        parser_provenance={"fixture": "authorization"},
        parser_provenance_hash=_fixture_hash(f"authorization-provenance:{suffix}"),
        parser_config_hash=_fixture_hash(f"authorization-parser-config:{suffix}"),
        reconstruction_config_hash=_fixture_hash(f"authorization-reconstruction:{suffix}"),
        source_format=SourceFormat.GAUSSIAN_LOG,
        source_encoding="utf-8",
    )
    protocol = CalculationProtocol(
        protocol_hash=_fixture_hash(f"authorization-protocol:{suffix}"),
        qm_software=QMSoftware.OTHER,
        qm_software_version="test-v1",
        method_family="test",
        task_requests=[],
        normalized_spec={"fixture": suffix},
    )
    session.add_all([artifact, revision, protocol])  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    assert artifact.id is not None and revision.id is not None and protocol.id is not None
    segment = CalculationSegment(
        parse_revision_id=revision.id,
        protocol_id=protocol.id,
        segment_index=0,
        source_start_byte=0,
        source_end_byte=1,
        source_start_line=1,
        source_end_line=2,
        source_block_sha256=content_hash,
    )
    session.add(segment)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    assert segment.id is not None and geometry.id is not None and derivation.id is not None
    coordinates = np.array([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64)
    frame = CalculationFrame(
        parse_revision_id=revision.id,
        segment_id=segment.id,
        frame_index=0,
        file_frame_index=0,
        frame_role=FrameRole.SINGLE_POINT,
        source_start_byte=0,
        source_end_byte=1,
        source_start_line=1,
        source_end_line=2,
        source_block_sha256=content_hash,
        geometry_id=geometry.id,
        topology_derivation_id=derivation.id,
        charge=0,
        multiplicity=1,
        geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
        observed_coordinates=coordinates,
        observed_coordinate_hash=_fixture_hash(f"authorization-observed:{suffix}"),
        observed_to_geometry_atom_indices=[0, 1],
        observed_to_geometry_transform=np.eye(4, dtype=np.float64).reshape(-1).tolist(),
        geometry_assignment_rmsd_angstrom=0.0,
        geometry_assignment_max_abs_angstrom=0.0,
        geometry_assignment_policy_version="authorization-test-v1",
    )
    session.add(frame)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    assert frame.id is not None
    session.add(  # type: ignore[attr-defined]
        ThermochemistryResult(
            frame_id=frame.id,
            temperature_kelvin=298.15,
            pressure_atm=1.0,
            gibbs_free_energy_hartree=-1.0,
            source_schema_version="authorization-test-v1",
        )
    )
    values = np.zeros((2, 3), dtype=np.float64)
    array = ScientificArray(
        frame_id=frame.id,
        kind=ScientificArrayKind.FORCES,
        ordinal=0,
        unit="hartree/bohr",
        dtype=str(values.dtype),
        shape=list(values.shape),
        array_nbytes=values.nbytes,
        payload_sha256=_fixture_hash(f"authorization-array:{suffix}"),
        data=values,
    )
    session.add(array)  # type: ignore[attr-defined]
    await session.flush()  # type: ignore[attr-defined]
    assert array.id is not None
    return CalculationSource(
        artifact_id=artifact.id,
        parse_revision_id=revision.id,
        frame_id=frame.id,
        scientific_array_id=array.id,
        protocol_id=protocol.id,
        derivation_id=derivation.id,
        geometry_id=geometry.id,
    )


@asynccontextmanager
async def _request_as(principal: AuthenticatedPrincipal) -> AsyncIterator[None]:
    request_token = set_request_context_active()
    principal_token = set_current_principal(principal)
    try:
        yield
    finally:
        reset_current_principal(principal_token)
        reset_request_context_active(request_token)


@pytest_asyncio.fixture
async def authorization_sample() -> AsyncIterator[AuthorizationSample]:
    suffix = uuid4().hex
    user_id = uuid4()
    organization_id = uuid4()
    project_a_id = uuid4()
    project_b_id = uuid4()
    membership_id = uuid4()
    manifest_artifact_id: UUID | None = None
    manifest_id: UUID | None = None
    binding_id: UUID | None = None
    public_artifact_id: UUID | None = None
    source_less_logical_reaction_id: UUID | None = None
    created_artifact_ids: set[UUID] = set()
    created_protocol_ids: set[UUID] = set()
    created_derivation_ids: set[UUID] = set()
    created_geometry_ids: set[UUID] = set()
    created_topology_ids: set[UUID] = set()
    created_formula_ids: set[UUID] = set()
    created_logical_reaction_ids: set[UUID] = set()

    async with session_factory() as session:
        user = UserAccount(id=user_id, display_name=f"Authorization {suffix}")
        organization = Organization(
            id=organization_id,
            slug=f"authorization-{suffix}",
            name=f"Authorization {suffix}",
        )
        project_a = Project(
            id=project_a_id,
            organization_id=organization_id,
            slug="project-a",
            name="Project A",
        )
        project_b = Project(
            id=project_b_id,
            organization_id=organization_id,
            slug="project-b",
            name="Project B",
        )
        membership = ProjectMembership(
            id=membership_id,
            project_id=project_a_id,
            user_id=user_id,
            role=ProjectRole.VIEWER,
        )
        session.add_all([user, organization, project_a, project_b, membership])
        await session.flush()

        (
            shared_formula,
            shared_topology,
            shared_derivation,
            shared_geometry,
        ) = await _create_topology_and_geometry(session, suffix=f"{suffix}-shared")
        (
            private_formula,
            private_topology,
            private_derivation,
            private_geometry,
        ) = await _create_topology_and_geometry(session, suffix=f"{suffix}-private")
        source_a = await _create_calculation_source(
            session,
            project_id=project_a_id,
            user_id=user_id,
            suffix=f"{suffix}-a",
            geometry=shared_geometry,
            derivation=shared_derivation,
        )
        source_b = await _create_calculation_source(
            session,
            project_id=project_b_id,
            user_id=user_id,
            suffix=f"{suffix}-b",
            geometry=shared_geometry,
            derivation=shared_derivation,
        )
        private_source = await _create_calculation_source(
            session,
            project_id=project_b_id,
            user_id=user_id,
            suffix=f"{suffix}-private",
            geometry=private_geometry,
            derivation=private_derivation,
        )

        shared_logical_reaction = LogicalReaction(
            id=uuid4(),
            reaction_key=f"authorization-shared:{suffix}",
            label="Authorization shared reaction",
            reaction_hash=_fixture_hash(f"authorization-shared-logical:{suffix}"),
        )
        assert shared_logical_reaction.id is not None
        shared_mapped_reaction = MappedReaction(
            logical_reaction_id=shared_logical_reaction.id,
            logical_reaction=shared_logical_reaction,
            mapped_reaction_key="shared-path",
            label="Authorization shared mapped reaction",
            mapped_reaction_kind=MappedReactionKind.OTHER,
            mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
            mapping_hash=_fixture_hash(f"authorization-shared-mapped:{suffix}"),
        )
        private_logical_reaction = LogicalReaction(
            id=uuid4(),
            reaction_key=f"authorization-private:{suffix}",
            label="Authorization private reaction",
            reaction_hash=_fixture_hash(f"authorization-private-logical:{suffix}"),
        )
        assert private_logical_reaction.id is not None
        private_mapped_reaction = MappedReaction(
            logical_reaction_id=private_logical_reaction.id,
            logical_reaction=private_logical_reaction,
            mapped_reaction_key="private-path",
            label="Authorization private mapped reaction",
            mapped_reaction_kind=MappedReactionKind.OTHER,
            mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
            mapping_hash=_fixture_hash(f"authorization-private-mapped:{suffix}"),
        )
        session.add_all([shared_mapped_reaction, private_mapped_reaction])
        await session.flush()
        assert shared_mapped_reaction.id is not None
        assert shared_logical_reaction.id is not None
        assert private_mapped_reaction.id is not None
        assert private_logical_reaction.id is not None
        shared_node = MappedReactionNode(
            mapped_reaction_id=shared_mapped_reaction.id,
            node_key="shared-node",
            node_index=0,
            role=MappedReactionNodeRole.TRANSITION_STATE,
        )
        private_node = MappedReactionNode(
            mapped_reaction_id=private_mapped_reaction.id,
            node_key="private-node",
            node_index=0,
            role=MappedReactionNodeRole.TRANSITION_STATE,
        )
        session.add_all([shared_node, private_node])
        await session.flush()
        assert shared_node.id is not None and private_node.id is not None
        assert shared_geometry.id is not None and private_geometry.id is not None
        session.add_all(
            [
                MappedReactionNodeGeometry(
                    mapped_reaction_node_id=shared_node.id,
                    geometry_id=shared_geometry.id,
                    component_key="shared",
                    component_index=0,
                ),
                MappedReactionNodeGeometry(
                    mapped_reaction_node_id=private_node.id,
                    geometry_id=private_geometry.id,
                    component_key="private",
                    component_index=0,
                ),
            ]
        )

        assert shared_topology.id is not None
        ts_coordinates = np.array([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64)
        for direction, displacement in (
            (TransitionStateEndpointDirection.NEGATIVE, -0.05),
            (TransitionStateEndpointDirection.POSITIVE, 0.05),
        ):
            endpoint_coordinates = ts_coordinates.copy()
            endpoint_coordinates[1, 0] += displacement
            session.add(
                TransitionStateEndpoint(
                    calculation_frame_id=source_a.frame_id,
                    topology_id=shared_topology.id,
                    direction=direction,
                    atom_count=2,
                    displacement_ratio=0.1,
                    source_coordinates=endpoint_coordinates,
                    source_coordinate_hash=_fixture_hash(
                        f"authorization-endpoint:{suffix}:{direction.value}"
                    ),
                    source_to_topology_atom_indices=[0, 1],
                    provenance={"fixture": "authorization"},
                )
            )

        artifact_a_id = source_a.artifact_id
        artifact_b_id = source_b.artifact_id
        artifact_private_id = private_source.artifact_id
        frame_a_id = source_a.frame_id
        frame_b_id = source_b.frame_id
        frame_private_id = private_source.frame_id
        array_a_id = source_a.scientific_array_id
        array_b_id = source_b.scientific_array_id
        shared_geometry_id = source_a.geometry_id
        private_geometry_id = private_source.geometry_id
        shared_mapped_reaction_id = shared_mapped_reaction.id
        shared_logical_reaction_id = shared_logical_reaction.id
        private_mapped_reaction_id = private_mapped_reaction.id
        private_logical_reaction_id = private_logical_reaction.id
        created_artifact_ids.update({artifact_a_id, artifact_b_id, artifact_private_id})
        created_protocol_ids.update(
            {source_a.protocol_id, source_b.protocol_id, private_source.protocol_id}
        )
        created_derivation_ids.update({source_a.derivation_id, private_source.derivation_id})
        created_geometry_ids.update({shared_geometry_id, private_geometry_id})
        assert private_topology.id is not None
        created_topology_ids.update({shared_topology.id, private_topology.id})
        assert shared_formula.id is not None and private_formula.id is not None
        created_formula_ids.update({shared_formula.id, private_formula.id})
        created_logical_reaction_ids.update(
            {shared_logical_reaction_id, private_logical_reaction_id}
        )

        public_artifact = ArtifactFile(
            id=uuid4(),
            project_id=project_b_id,
            created_by_user_id=user_id,
            visibility=ArtifactVisibility.PUBLIC,
            bucket=f"authorization-{suffix}",
            object_key=f"authorization/{suffix}/public.log",
            content_sha256=sha256(f"authorization-public:{suffix}".encode()).hexdigest(),
            size_bytes=0,
            original_filename="authorization-public.log",
            media_type="text/plain",
            artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
            storage_status=StorageStatus.PENDING,
        )
        source_less_logical_reaction_id = uuid4()
        source_less_logical_reaction = LogicalReaction(
            id=source_less_logical_reaction_id,
            reaction_key=f"authorization-source-less:{suffix}",
            label="Authorization source-less reaction",
            reaction_class=ReactionClass.CYCLOADDITION,
            reaction_hash=sha256(f"authorization-logical:{suffix}".encode()).hexdigest(),
        )
        source_less_mapped_reaction = MappedReaction(
            logical_reaction_id=source_less_logical_reaction_id,
            logical_reaction=source_less_logical_reaction,
            mapped_reaction_key="source-less-path",
            label="Authorization source-less mapped reaction",
            mapped_reaction_kind=MappedReactionKind.OTHER,
            mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
            mapping_hash=sha256(f"authorization-mapped:{suffix}".encode()).hexdigest(),
        )
        session.add_all([public_artifact, source_less_mapped_reaction])
        await session.flush()
        assert public_artifact.id is not None
        assert source_less_logical_reaction.id is not None
        assert source_less_mapped_reaction.id is not None
        public_artifact_id = public_artifact.id
        source_less_logical_reaction_id = source_less_logical_reaction.id
        source_less_mapped_reaction_id = source_less_mapped_reaction.id

        manifest_artifact = ArtifactFile(
            id=uuid4(),
            project_id=project_a_id,
            created_by_user_id=user_id,
            visibility=ArtifactVisibility.PROJECT,
            bucket=f"authorization-{suffix}",
            object_key=f"authorization/{suffix}/manifest.json",
            content_sha256=sha256(f"authorization:{suffix}".encode()).hexdigest(),
            size_bytes=2,
            original_filename="manifest.json",
            media_type="application/json",
            artifact_kind=ArtifactKind.WORKFLOW_MANIFEST,
            storage_status=StorageStatus.AVAILABLE,
        )
        session.add(manifest_artifact)
        await session.flush()
        assert manifest_artifact.id is not None
        manifest_artifact_id = manifest_artifact.id
        manifest = WorkflowManifest(
            artifact_file_id=manifest_artifact_id,
            manifest_key=f"authorization:{suffix}",
            revision=1,
            schema_version="authorization-v1",
            payload_sha256=manifest_artifact.content_sha256,
            qc_policy_version="authorization-v1",
            status=WorkflowManifestStatus.VALIDATED,
        )
        session.add(manifest)
        await session.flush()
        assert manifest.id is not None
        manifest_id = manifest.id
        target = await session.get(ArtifactFile, artifact_b_id)
        assert target is not None
        binding = ManifestArtifactBinding(
            workflow_manifest_id=manifest_id,
            artifact_key="private-target",
            artifact_file_id=artifact_b_id,
            expected_content_sha256=target.content_sha256,
            artifact_role=ManifestArtifactRole.SUPPORTING,
            reaction_key="authorization",
            path_key="path",
            node_key="node",
            segment_index=0,
            frame_index=0,
            resolution_status=ArtifactResolutionStatus.RESOLVED,
        )
        session.add(binding)
        await session.flush()
        assert binding.id is not None
        binding_id = binding.id
        assert public_artifact_id is not None and manifest_artifact_id is not None
        assert source_less_logical_reaction_id is not None
        created_artifact_ids.update({public_artifact_id, manifest_artifact_id})
        created_logical_reaction_ids.add(source_less_logical_reaction_id)
        await session.commit()

    principal = AuthenticatedPrincipal(
        user_id=user_id,
        display_name=f"Authorization {suffix}",
        primary_email=None,
        is_service_account=False,
        issuer="urn:test:authorization",
        subject=suffix,
    )
    try:
        assert manifest_id is not None and binding_id is not None
        yield AuthorizationSample(
            principal=principal,
            project_a_id=project_a_id,
            project_b_id=project_b_id,
            artifact_a_id=artifact_a_id,
            artifact_b_id=artifact_b_id,
            artifact_private_id=artifact_private_id,
            public_artifact_id=public_artifact_id,
            frame_a_id=frame_a_id,
            frame_b_id=frame_b_id,
            frame_private_id=frame_private_id,
            shared_geometry_id=shared_geometry_id,
            private_geometry_id=private_geometry_id,
            shared_mapped_reaction_id=shared_mapped_reaction_id,
            shared_logical_reaction_id=shared_logical_reaction_id,
            private_mapped_reaction_id=private_mapped_reaction_id,
            private_logical_reaction_id=private_logical_reaction_id,
            scientific_array_a_id=array_a_id,
            scientific_array_b_id=array_b_id,
            protocol_a_id=source_a.protocol_id,
            derivation_a_id=source_a.derivation_id,
            manifest_id=manifest_id,
            binding_id=binding_id,
            source_less_mapped_reaction_id=source_less_mapped_reaction_id,
            source_less_logical_reaction_id=source_less_logical_reaction_id,
            transition_state_frame_id=source_a.frame_id,
        )
    finally:
        async with session_factory() as session:
            if manifest_id is not None:
                await session.execute(
                    delete(WorkflowManifest).where(col(WorkflowManifest.id) == manifest_id)
                )
            if created_logical_reaction_ids:
                await session.execute(
                    delete(LogicalReaction).where(
                        col(LogicalReaction.id).in_(created_logical_reaction_ids)
                    )
                )
            if created_artifact_ids:
                await session.execute(
                    delete(ParseRevision).where(
                        col(ParseRevision.artifact_file_id).in_(created_artifact_ids)
                    )
                )
                await session.execute(
                    delete(ArtifactFile).where(col(ArtifactFile.id).in_(created_artifact_ids))
                )
            if created_protocol_ids:
                await session.execute(
                    delete(CalculationProtocol).where(
                        col(CalculationProtocol.id).in_(created_protocol_ids)
                    )
                )
            if created_geometry_ids:
                await session.execute(
                    delete(Geometry).where(col(Geometry.id).in_(created_geometry_ids))
                )
            if created_derivation_ids:
                await session.execute(
                    delete(MolecularTopologyDerivation).where(
                        col(MolecularTopologyDerivation.id).in_(created_derivation_ids)
                    )
                )
            if created_topology_ids:
                await session.execute(
                    delete(MolecularTopology).where(
                        col(MolecularTopology.id).in_(created_topology_ids)
                    )
                )
            if created_formula_ids:
                await session.execute(
                    delete(MolecularFormula).where(
                        col(MolecularFormula.id).in_(created_formula_ids)
                    )
                )
            await session.execute(
                delete(ProjectMembership).where(col(ProjectMembership.id) == membership_id)
            )
            await session.execute(
                delete(Project).where(col(Project.id).in_({project_a_id, project_b_id}))
            )
            await session.execute(
                delete(Organization).where(col(Organization.id) == organization_id)
            )
            await session.execute(delete(UserAccount).where(col(UserAccount.id) == user_id))
            await session.commit()
        await dispose_engine()


@asynccontextmanager
async def _participant_topology_source(
    *,
    logical_reaction_id: UUID,
    mapped_reaction_id: UUID,
) -> AsyncIterator[UUID]:
    participant_id = uuid4()
    mapped_participant_id = uuid4()
    formula_id: UUID | None = None
    topology_id: UUID | None = None
    async with session_factory() as session:
        formula, topology, derivation, geometry = await _create_topology_and_geometry(
            session,
            suffix=f"participant-{uuid4().hex}",
        )
        assert formula.id is not None and topology.id is not None
        formula_id = formula.id
        topology_id = topology.id
        await session.delete(geometry)
        await session.delete(derivation)
        await session.flush()
        participant_index = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(participant_index), -1) + 1
                        FROM logical_reaction_participant
                        WHERE logical_reaction_id = :logical_reaction_id
                        """
                    ).bindparams(logical_reaction_id=logical_reaction_id)
                )
            ).scalar_one()
        )
        template_index = int(
            (
                await session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(template_index), -1) + 1
                        FROM mapped_reaction_participant
                        WHERE mapped_reaction_id = :mapped_reaction_id
                        """
                    ).bindparams(mapped_reaction_id=mapped_reaction_id)
                )
            ).scalar_one()
        )
        session.add_all(
            [
                LogicalReactionParticipant(
                    id=participant_id,
                    logical_reaction_id=logical_reaction_id,
                    topology_id=topology_id,
                    side=LogicalReactionParticipantSide.REACTANT,
                    participant_index=participant_index,
                    stoichiometric_coefficient=1,
                ),
                MappedReactionParticipant(
                    id=mapped_participant_id,
                    mapped_reaction_id=mapped_reaction_id,
                    logical_reaction_participant_id=participant_id,
                    side=LogicalReactionParticipantSide.REACTANT,
                    template_index=template_index,
                    atom_map_numbers=[1],
                    mapped_smiles="[H:1]",
                ),
            ]
        )
        await session.commit()

    try:
        assert topology_id is not None
        yield topology_id
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(MappedReactionParticipant).where(
                    col(MappedReactionParticipant.id) == mapped_participant_id
                )
            )
            await session.execute(
                delete(LogicalReactionParticipant).where(
                    col(LogicalReactionParticipant.id) == participant_id
                )
            )
            if topology_id is not None:
                await session.execute(
                    delete(MolecularTopology).where(col(MolecularTopology.id) == topology_id)
                )
            if formula_id is not None:
                await session.execute(
                    delete(MolecularFormula).where(col(MolecularFormula.id) == formula_id)
                )
            await session.commit()


@pytest_asyncio.fixture
async def source_backed_participant_topology_id(
    authorization_sample: AuthorizationSample,
) -> AsyncIterator[UUID]:
    sample = authorization_sample
    async with _participant_topology_source(
        logical_reaction_id=sample.shared_logical_reaction_id,
        mapped_reaction_id=sample.shared_mapped_reaction_id,
    ) as topology_id:
        yield topology_id


@pytest_asyncio.fixture
async def private_participant_topology_id(
    authorization_sample: AuthorizationSample,
) -> AsyncIterator[UUID]:
    sample = authorization_sample
    async with _participant_topology_source(
        logical_reaction_id=sample.private_logical_reaction_id,
        mapped_reaction_id=sample.private_mapped_reaction_id,
    ) as topology_id:
        yield topology_id


def _reaction_frame_ids(detail: object) -> set[UUID]:
    return {
        calculation.id
        for node in detail.nodes  # type: ignore[attr-defined]
        for geometry in node.geometries
        for calculation in geometry.calculations
    }


@pytest.mark.asyncio
async def test_artifact_frame_geometry_and_array_visibility(
    authorization_sample: AuthorizationSample,
) -> None:
    sample = authorization_sample
    async with _request_as(sample.principal):
        artifacts = await ArtifactQueryService.list_artifacts(limit=200, offset=0)
        frames = await CalculationQueryService.list_calculation_frames(limit=200, offset=0)
        geometries = await GeometryQueryService.list_geometries(limit=200, offset=0)
        arrays_a = await ScientificArrayQueryService.list_scientific_arrays(
            frame_id=sample.frame_a_id, limit=200, offset=0
        )
        arrays_b = await ScientificArrayQueryService.list_scientific_arrays(
            frame_id=sample.frame_b_id, limit=200, offset=0
        )
        shared_geometry = await GeometryQueryService.get_geometry(
            geometry_id=sample.shared_geometry_id
        )

        assert sample.artifact_a_id in {item.id for item in artifacts.items}
        assert sample.artifact_b_id not in {item.id for item in artifacts.items}
        assert sample.frame_a_id in {item.id for item in frames.items}
        assert sample.frame_b_id not in {item.id for item in frames.items}
        assert sample.shared_geometry_id in {item.id for item in geometries.items}
        assert sample.private_geometry_id not in {item.id for item in geometries.items}
        assert sample.scientific_array_a_id in {item.id for item in arrays_a.items}
        assert arrays_b.page.total == 0
        assert sample.scientific_array_b_id not in {item.id for item in arrays_b.items}
        assert shared_geometry is not None
        assert sample.frame_a_id in {frame.id for frame in shared_geometry.frames}
        assert sample.frame_b_id not in {frame.id for frame in shared_geometry.frames}
        assert (
            await GeometryQueryService.get_geometry(geometry_id=sample.private_geometry_id) is None
        )
        assert await get_geometry_sdf(sample.shared_geometry_id) is not None
        assert await get_geometry_sdf(sample.private_geometry_id) is None
        assert await get_geometry_xyz(sample.shared_geometry_id) is not None
        assert await get_geometry_xyz(sample.private_geometry_id) is None
        assert await get_geometry_dof_depiction(sample.shared_geometry_id) is not None
        assert await get_geometry_dof_depiction(sample.private_geometry_id) is None
        with pytest.raises(ScientificArrayNotFoundError):
            await ScientificArrayContentService.load_npy(
                sample.scientific_array_b_id,
                max_bytes=32 * 1024 * 1024,
            )


@pytest.mark.asyncio
async def test_reaction_visibility_filters_mixed_calculation_sources(
    authorization_sample: AuthorizationSample,
) -> None:
    sample = authorization_sample
    async with _request_as(sample.principal):
        mapped = await MappedReactionQueryService.list_mapped_reactions(limit=200, offset=0)
        logical = await LogicalReactionQueryService.list_logical_reactions(limit=200, offset=0)
        detail = await MappedReactionQueryService.get_mapped_reaction(
            mapped_reaction_id=sample.shared_mapped_reaction_id
        )

        assert sample.shared_mapped_reaction_id in {item.id for item in mapped.items}
        assert sample.private_mapped_reaction_id not in {item.id for item in mapped.items}
        assert sample.shared_logical_reaction_id in {item.id for item in logical.items}
        assert sample.private_logical_reaction_id not in {item.id for item in logical.items}
        assert sample.source_less_mapped_reaction_id in {item.id for item in mapped.items}
        assert sample.source_less_logical_reaction_id in {item.id for item in logical.items}
        assert detail is not None
        assert sample.frame_a_id in _reaction_frame_ids(detail)
        assert sample.frame_b_id not in _reaction_frame_ids(detail)
        assert (
            await MappedReactionQueryService.get_mapped_reaction(
                mapped_reaction_id=sample.private_mapped_reaction_id
            )
            is None
        )
        assert (
            await MappedReactionQueryService.get_mapped_reaction(
                mapped_reaction_id=sample.source_less_mapped_reaction_id
            )
            is not None
        )
        assert (
            await LogicalReactionQueryService.get_logical_reaction(
                logical_reaction_id=sample.source_less_logical_reaction_id
            )
            is not None
        )


@pytest.mark.asyncio
async def test_anonymous_public_artifact_list_and_detail_are_consistent(
    authorization_sample: AuthorizationSample,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample

    async def authenticate_optional(_: str | None) -> None:
        return None

    monkeypatch.setattr(AuthenticationService, "authenticate_optional", authenticate_optional)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        artifacts = await client.get("/api/artifacts", params={"limit": 200})
        public_detail = await client.get(f"/api/artifacts/{sample.public_artifact_id}")
        private_detail = await client.get(f"/api/artifacts/{sample.artifact_b_id}")

    assert artifacts.status_code == 200
    assert sample.public_artifact_id in {UUID(item["id"]) for item in artifacts.json()["items"]}
    assert sample.artifact_b_id not in {UUID(item["id"]) for item in artifacts.json()["items"]}
    assert public_detail.status_code == 200
    assert UUID(public_detail.json()["id"]) == sample.public_artifact_id
    assert private_detail.status_code == 404
    assert private_detail.json() == {"detail": "artifact not found"}


@pytest.mark.asyncio
async def test_advanced_protocol_derivation_and_manifest_visibility(
    authorization_sample: AuthorizationSample,
) -> None:
    sample = authorization_sample
    async with _request_as(sample.principal):
        results = await CalculationResultQueryService.list_calculation_results(
            artifact_file_id=sample.artifact_b_id,
            limit=200,
            offset=0,
        )
        derivation = await MolecularTopologyDerivationQueryService.get_topology_derivation(
            derivation_id=sample.derivation_a_id
        )
        manifests = await WorkflowManifestQueryService.list_workflow_manifests(
            artifact_file_id=None,
            limit=200,
            offset=0,
        )
        manifest = await WorkflowManifestQueryService.get_workflow_manifest(
            workflow_manifest_id=sample.manifest_id
        )
        binding = await WorkflowManifestQueryService.get_manifest_artifact_binding(
            binding_id=sample.binding_id
        )
        reverse = await WorkflowManifestQueryService.list_manifest_artifact_bindings(
            artifact_file_id=sample.artifact_b_id,
            limit=200,
            offset=0,
        )

        assert results.page.total == 0
        assert derivation is not None
        assert derivation.calculation_frame_count >= 1
        if sample.protocol_a_id is not None:
            assert (
                await CalculationProtocolQueryService.get_calculation_protocol(
                    protocol_id=sample.protocol_a_id
                )
                is not None
            )
        assert sample.manifest_id in {item.id for item in manifests.items}
        assert manifest is not None
        assert manifest.artifact_bindings[0].artifact_file_id is None
        assert binding is not None and binding.artifact_file_id is None
        assert reverse.page.total == 0


@pytest.mark.asyncio
async def test_private_and_missing_detail_ids_have_same_service_semantics(
    authorization_sample: AuthorizationSample,
) -> None:
    sample = authorization_sample
    missing = uuid4()
    async with _request_as(sample.principal):
        assert await ArtifactQueryService.get_artifact(artifact_id=sample.artifact_b_id) is None
        assert await ArtifactQueryService.get_artifact(artifact_id=missing) is None
        assert (
            await CalculationQueryService.get_calculation_frame(frame_id=sample.frame_b_id) is None
        )
        assert await CalculationQueryService.get_calculation_frame(frame_id=missing) is None
        assert (
            await ScientificArrayQueryService.get_scientific_array(
                array_id=sample.scientific_array_b_id
            )
            is None
        )
        assert await ScientificArrayQueryService.get_scientific_array(array_id=missing) is None


@pytest.mark.asyncio
async def test_rest_graphql_and_mcp_share_authorized_visibility(
    authorization_sample: AuthorizationSample,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample

    async def authenticate(_: str | None) -> AuthenticatedPrincipal:
        return sample.principal

    monkeypatch.setattr(AuthenticationService, "authenticate", authenticate)
    headers = {"Authorization": "Bearer authorization-test"}
    application = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
        headers=headers,
    ) as client:
        rest = await client.post(
            "/api/mapped_reaction_query_service/list_mapped_reactions",
            json={"limit": 200, "offset": 0},
        )
        graphql = await client.post(
            "/graphql",
            json={
                "query": (
                    "{ MappedReactionQueryService { "
                    "list_mapped_reactions(limit: 200, offset: 0) { items { id } } } }"
                )
            },
        )
        private_rest = await client.post(
            "/api/mapped_reaction_query_service/get_mapped_reaction",
            json={"mapped_reaction_id": str(sample.private_mapped_reaction_id)},
        )

    assert rest.status_code == 200
    assert graphql.status_code == 200
    rest_ids = {UUID(item["id"]) for item in rest.json()["items"]}
    graphql_ids = {
        UUID(item["id"])
        for item in graphql.json()["data"]["MappedReactionQueryService"]["list_mapped_reactions"][
            "items"
        ]
    }
    assert rest_ids == graphql_ids
    assert sample.shared_mapped_reaction_id in rest_ids
    assert sample.private_mapped_reaction_id not in rest_ids
    assert sample.source_less_mapped_reaction_id in rest_ids
    assert private_rest.status_code == 200
    assert private_rest.json() is None

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: Timeout | None = None,
        auth: HttpxAuth | None = None,
        **kwargs: Any,
    ) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=mcp_dedicated_app),
            base_url="http://test",
            headers=headers,
            timeout=timeout,
            auth=auth,
            **kwargs,
        )

    transport = StreamableHttpTransport(
        "http://test/mcp",
        headers=headers,
        httpx_client_factory=cast(Any, client_factory),
    )
    async with (
        mcp_dedicated_app.lifespan(mcp_dedicated_app),
        Client(transport) as mcp_client,
    ):
        result = await mcp_client.call_tool(
            "compose_query",
            {
                "app_name": "example-chemistry-database",
                "query": (
                    "{ MappedReactionQueryService { "
                    "list_mapped_reactions(limit: 200, offset: 0) { items { id } } } }"
                ),
            },
        )
    content = result.content[0]
    assert isinstance(content, TextContent)
    payload = json.loads(content.text)
    mcp_ids = {
        UUID(item["id"])
        for item in payload["data"]["MappedReactionQueryService"]["list_mapped_reactions"]["items"]
    }
    assert mcp_ids == rest_ids


@pytest.mark.asyncio
async def test_core_content_routes_do_not_reveal_private_ids(
    authorization_sample: AuthorizationSample,
    source_backed_participant_topology_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample

    async def authenticate(_: str | None) -> AuthenticatedPrincipal:
        return sample.principal

    monkeypatch.setattr(AuthenticationService, "authenticate", authenticate)
    application = create_app()
    missing = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
        headers={"Authorization": "Bearer authorization-test"},
    ) as client:
        private_artifact = await client.get(f"/api/artifacts/{sample.artifact_b_id}/preview")
        missing_artifact = await client.get(f"/api/artifacts/{missing}/preview")
        private_array = await client.get(
            f"/api/scientific-arrays/{sample.scientific_array_b_id}.npy"
        )
        missing_array = await client.get(f"/api/scientific-arrays/{missing}.npy")
        private_array_preview = await client.get(
            f"/api/scientific-arrays/{sample.scientific_array_b_id}/preview"
        )
        missing_array_preview = await client.get(f"/api/scientific-arrays/{missing}/preview")
        private_geometry = await client.get(
            f"/api/depictions/geometry/{sample.private_geometry_id}.sdf"
        )
        missing_geometry = await client.get(f"/api/depictions/geometry/{missing}.sdf")
        private_geometry_xyz = await client.get(
            f"/api/depictions/geometry/{sample.private_geometry_id}.xyz"
        )
        missing_geometry_xyz = await client.get(f"/api/depictions/geometry/{missing}.xyz")
        private_geometry_svg = await client.get(
            f"/api/depictions/geometry/{sample.private_geometry_id}.svg"
        )
        missing_geometry_svg = await client.get(f"/api/depictions/geometry/{missing}.svg")
        participant_topology = await client.get(
            f"/api/depictions/topology/{source_backed_participant_topology_id}.mol"
        )
        missing_topology = await client.get(f"/api/depictions/topology/{missing}.mol")

    assert private_artifact.status_code == missing_artifact.status_code == 404
    assert private_artifact.json() == missing_artifact.json() == {"detail": "artifact not found"}
    assert private_array.status_code == missing_array.status_code == 404
    assert private_array.json() == missing_array.json() == {"detail": "scientific array not found"}
    assert private_array_preview.status_code == missing_array_preview.status_code == 404
    assert (
        private_array_preview.json()
        == missing_array_preview.json()
        == {"detail": "scientific array not found"}
    )
    assert private_geometry.status_code == missing_geometry.status_code == 404
    assert private_geometry.json() == missing_geometry.json()
    assert private_geometry_xyz.status_code == missing_geometry_xyz.status_code == 404
    assert private_geometry_xyz.json() == missing_geometry_xyz.json()
    assert private_geometry_svg.status_code == missing_geometry_svg.status_code == 404
    assert private_geometry_svg.json() == missing_geometry_svg.json()
    assert participant_topology.status_code == 200
    assert participant_topology.headers["cache-control"] == "private, no-store"
    assert "V2000" in participant_topology.text
    assert missing_topology.status_code == 404


@pytest.mark.asyncio
async def test_core_topology_list_uses_the_shared_visibility_scope(
    authorization_sample: AuthorizationSample,
    source_backed_participant_topology_id: UUID,
    private_participant_topology_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample

    async def authenticate(_: str | None) -> AuthenticatedPrincipal:
        return sample.principal

    async with session_factory() as session:
        shared_geometry = await session.get(Geometry, sample.shared_geometry_id)
        private_geometry = await session.get(Geometry, sample.private_geometry_id)
        assert shared_geometry is not None and private_geometry is not None
        shared_topology_id = shared_geometry.topology_id
        private_topology_id = private_geometry.topology_id

    monkeypatch.setattr(AuthenticationService, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers={"Authorization": "Bearer authorization-test"},
    ) as client:
        response = await client.get("/api/topologies", params={"limit": 200})

    assert response.status_code == 200
    topology_ids = {UUID(item["id"]) for item in response.json()}
    assert shared_topology_id in topology_ids
    assert source_backed_participant_topology_id in topology_ids
    assert private_topology_id not in topology_ids
    assert private_participant_topology_id not in topology_ids


@pytest.mark.asyncio
async def test_same_depiction_url_is_not_reused_across_authorization_contexts(
    authorization_sample: AuthorizationSample,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample
    outsider = AuthenticatedPrincipal(
        user_id=uuid4(),
        display_name="Authorization outsider",
        primary_email=None,
        is_service_account=False,
        issuer="urn:test:authorization",
        subject="outsider",
    )

    async def authenticate_optional(
        authorization: str | None,
        _session_token: str | None = None,
    ) -> AuthenticatedPrincipal | None:
        if authorization == "Bearer allowed":
            return sample.principal
        if authorization == "Bearer outsider":
            return outsider
        return None

    monkeypatch.setattr(AuthenticationService, "authenticate_optional", authenticate_optional)
    url = f"/api/depictions/geometry/{sample.shared_geometry_id}.sdf"
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
    ) as client:
        allowed = await client.get(url, headers={"Authorization": "Bearer allowed"})
        denied = await client.get(url, headers={"Authorization": "Bearer outsider"})
        anonymous = await client.get(url)

    assert allowed.status_code == 200
    assert allowed.headers["cache-control"] == "private, no-store"
    assert allowed.headers["content-disposition"] == (
        f'attachment; filename="geometry-{sample.shared_geometry_id}.sdf"'
    )
    assert denied.status_code == anonymous.status_code == 404
    assert denied.json() == anonymous.json() == {"detail": "molecular geometry not found"}


@pytest.mark.asyncio
async def test_depiction_authorization_matrix_covers_all_roles_and_formats(
    authorization_sample: AuthorizationSample,
    source_backed_participant_topology_id: UUID,
    private_participant_topology_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = authorization_sample
    role_users = {
        "viewer": (uuid4(), ProjectRole.VIEWER),
        "contributor": (uuid4(), ProjectRole.CONTRIBUTOR),
        "manager": (uuid4(), ProjectRole.MANAGER),
    }
    outsider_user_id = uuid4()
    created_user_ids = {user_id for user_id, _ in role_users.values()} | {outsider_user_id}
    ts_frame_id = sample.transition_state_frame_id
    async with session_factory() as session:
        session.add_all(
            [
                UserAccount(
                    id=user_id,
                    display_name=f"Depiction {role_name}",
                    primary_email=f"depiction-{role_name}-{user_id}@example.test",
                )
                for role_name, (user_id, _) in role_users.items()
            ]
            + [
                UserAccount(
                    id=outsider_user_id,
                    display_name="Depiction outsider",
                    primary_email=f"depiction-outsider-{outsider_user_id}@example.test",
                )
            ]
        )
        await session.flush()
        session.add_all(
            [
                ProjectMembership(
                    project_id=sample.project_a_id,
                    user_id=user_id,
                    role=role,
                )
                for user_id, role in role_users.values()
            ]
        )
        await session.commit()

    principals = {
        role_name: AuthenticatedPrincipal(
            user_id=user_id,
            display_name=f"Depiction {role_name}",
            primary_email=None,
            is_service_account=False,
            issuer="urn:test:depiction-matrix",
            subject=role_name,
        )
        for role_name, (user_id, _) in role_users.items()
    }
    principals["outsider"] = AuthenticatedPrincipal(
        user_id=outsider_user_id,
        display_name="Depiction outsider",
        primary_email=None,
        is_service_account=False,
        issuer="urn:test:depiction-matrix",
        subject="outsider",
    )

    async def authenticate_optional(
        authorization: str | None,
        _session_token: str | None = None,
    ) -> AuthenticatedPrincipal | None:
        if authorization is None:
            return None
        return principals.get(authorization.removeprefix("Bearer "))

    monkeypatch.setattr(AuthenticationService, "authenticate_optional", authenticate_optional)
    visible_urls = [
        f"/api/depictions/geometry/{sample.shared_geometry_id}.svg",
        f"/api/depictions/geometry/{sample.shared_geometry_id}.sdf",
        f"/api/depictions/geometry/{sample.shared_geometry_id}.xyz",
        f"/api/depictions/topology/{source_backed_participant_topology_id}.svg",
        f"/api/depictions/topology/{source_backed_participant_topology_id}.mol",
        f"/api/depictions/calculation-frame/{ts_frame_id}/transition-state/negative.sdf",
        f"/api/depictions/calculation-frame/{ts_frame_id}/transition-state/center.sdf",
        f"/api/depictions/calculation-frame/{ts_frame_id}/transition-state/positive.sdf",
    ]
    denied_urls = [
        f"/api/depictions/geometry/{sample.private_geometry_id}.svg",
        f"/api/depictions/geometry/{sample.private_geometry_id}.sdf",
        f"/api/depictions/geometry/{sample.private_geometry_id}.xyz",
        f"/api/depictions/topology/{private_participant_topology_id}.svg",
        f"/api/depictions/topology/{private_participant_topology_id}.mol",
    ]
    missing = uuid4()
    missing_urls = [
        f"/api/depictions/geometry/{missing}.svg",
        f"/api/depictions/geometry/{missing}.sdf",
        f"/api/depictions/geometry/{missing}.xyz",
        f"/api/depictions/topology/{missing}.svg",
        f"/api/depictions/topology/{missing}.mol",
        f"/api/depictions/calculation-frame/{missing}/transition-state/center.sdf",
    ]

    try:
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            for role_name in ("viewer", "contributor", "manager"):
                headers = {"Authorization": f"Bearer {role_name}"}
                for url in visible_urls:
                    response = await client.get(url, headers=headers)
                    assert response.status_code == 200, (role_name, url, response.text)
                    assert response.headers["cache-control"] == "private, no-store"
                for denied_url, missing_url in zip(denied_urls, missing_urls, strict=False):
                    denied = await client.get(denied_url, headers=headers)
                    absent = await client.get(missing_url, headers=headers)
                    assert denied.status_code == absent.status_code == 404, (
                        role_name,
                        denied_url,
                        denied.text,
                    )

            for role_name in ("outsider", None):
                headers = (
                    {"Authorization": f"Bearer {role_name}"} if role_name is not None else None
                )
                for visible_url, missing_url in zip(visible_urls, missing_urls, strict=False):
                    denied = await client.get(visible_url, headers=headers)
                    absent = await client.get(missing_url, headers=headers)
                    assert denied.status_code == absent.status_code == 404, (
                        role_name,
                        visible_url,
                        denied.text,
                    )
    finally:
        async with session_factory() as session:
            await session.execute(
                delete(ProjectMembership).where(
                    col(ProjectMembership.user_id).in_(created_user_ids)
                )
            )
            await session.execute(
                delete(UserAccount).where(col(UserAccount.id).in_(created_user_ids))
            )
            await session.commit()
