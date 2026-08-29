import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
import pytest
from rdkit import Chem
from sqlalchemy import create_engine, func, select, text
from sqlmodel import Session, col

from tricycle_reaction_db.application.services import (
    CalculationProtocolQueryService,
    CalculationQueryService,
    GeometryQueryService,
    LogicalReactionQueryService,
    MappedReactionQueryService,
    ScientificArrayQueryService,
    TransitionStateInferenceQueryService,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_thermodynamic_property_predicate,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    ParseRevision,
    ScientificArray,
    ThermochemistryResult,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import dispose_engine
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    ArtifactVisibility,
    FrameRole,
    GeometryAssignmentKind,
    LogicalReactionParticipantSide,
    MappedReactionKind,
    MappedReactionNodeRole,
    QMSoftware,
    ScientificArrayKind,
    SelectedEnergyKind,
    SourceFormat,
    StorageStatus,
    TransitionStateInferenceStatus,
)
from tricycle_reaction_db.domain.formulas import ELEMENT_COUNT_VECTOR_SIZE
from tricycle_reaction_db.domain.identity import DEVELOPMENT_USER_ID, SYSTEM_PROJECT_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _fixture_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _create_domain_sample(session: Session) -> tuple[Any, ...]:
    suffix = uuid4().hex
    molecule = Chem.MolFromSmiles("[H][H]")
    assert molecule is not None
    vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
    vector[0] = 2
    formula = MolecularFormula(
        id=uuid4(),
        hill_formula="H2",
        composition=[{"atomic_number": 1, "isotope": 0, "count": 2}],
        atom_count=2,
        composition_hash=_fixture_hash(f"domain-formula:{suffix}"),
        element_count_vector=vector,
    )
    assert formula.id is not None
    topology = MolecularTopology(
        id=uuid4(),
        formula_id=formula.id,
        formula=formula,
        mol=molecule,
        canonical_isomeric_smiles="[H][H]",
        graph_hash=_fixture_hash(f"domain-topology:{suffix}"),
        identity_schema_version="domain-filter-test-v1",
        atom_count=2,
        heavy_atom_count=0,
        formal_charge=0,
        radical_electron_count=0,
        fragment_count=1,
    )
    assert topology.id is not None
    coordinates = np.array([[0.0, 0.0, 0.0], [0.74, 0.0, 0.0]], dtype=np.float64)
    geometry_molecule = Chem.Mol(molecule)
    conformer = Chem.Conformer(2)
    for atom_index, (x, y, z) in enumerate(coordinates):
        conformer.SetAtomPosition(atom_index, (float(x), float(y), float(z)))
    conformer.Set3D(True)
    geometry_molecule.AddConformer(conformer, assignId=True)
    derivation = MolecularTopologyDerivation(
        id=uuid4(),
        topology_id=topology.id,
        topology=topology,
        reconstruction_method="test/domain-filter",
        reconstruction_version="1",
        reconstruction_metadata={"fixture": suffix},
        provenance_hash=_fixture_hash(f"domain-derivation:{suffix}"),
    )
    geometry = Geometry(
        id=uuid4(),
        topology_id=topology.id,
        topology=topology,
        mol=geometry_molecule,
        internal_coordinates=coordinates,
        internal_coordinate_distances_angstrom=[0.74],
        internal_coordinate_angles_degrees=[0.0],
        internal_coordinate_dihedrals_degrees=[0.0],
        internal_coordinate_hash=_fixture_hash(f"domain-internal:{suffix}"),
        geometry_hash=_fixture_hash(f"domain-geometry:{suffix}"),
        canonicalization_version="domain-filter-test-v1",
    )
    artifact = ArtifactFile(
        id=uuid4(),
        project_id=SYSTEM_PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        visibility=ArtifactVisibility.PROJECT,
        bucket="domain-filter-integration",
        object_key=f"domain-filter/{suffix}.log",
        content_sha256=_fixture_hash(f"domain-artifact:{suffix}"),
        size_bytes=1,
        original_filename=f"domain-filter-{suffix}.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    )
    assert artifact.id is not None
    revision = ParseRevision(
        id=uuid4(),
        artifact_file_id=artifact.id,
        artifact_file=artifact,
        export_schema_version="domain-filter-test-v1",
        parser_version="test-v1",
        parser_id="tests.domain-filter",
        molop_version="test-v1",
        rdkit_version="test-v1",
        parser_provenance={"fixture": "domain-filter"},
        parser_provenance_hash=_fixture_hash(f"domain-provenance:{suffix}"),
        parser_config_hash=_fixture_hash(f"domain-parser:{suffix}"),
        reconstruction_config_hash=_fixture_hash(f"domain-reconstruction:{suffix}"),
        source_format=SourceFormat.GAUSSIAN_LOG,
        source_encoding="utf-8",
    )
    protocol = CalculationProtocol(
        id=uuid4(),
        protocol_hash=_fixture_hash(f"domain-protocol:{suffix}"),
        qm_software=QMSoftware.OTHER,
        qm_software_version=f"domain-{suffix}",
        method_family="DFT",
        solvation_model=None,
        task_requests=["frequency"],
        normalized_spec={"fixture": suffix},
    )
    session.add_all([formula, topology, derivation, geometry, artifact, revision, protocol])
    session.flush()
    assert revision.id is not None and protocol.id is not None
    segment = CalculationSegment(
        id=uuid4(),
        parse_revision_id=revision.id,
        protocol_id=protocol.id,
        segment_index=0,
        source_start_byte=0,
        source_end_byte=1,
        source_start_line=1,
        source_end_line=2,
        source_block_sha256=artifact.content_sha256,
    )
    session.add(segment)
    session.flush()
    assert segment.id is not None and geometry.id is not None and derivation.id is not None
    frame = CalculationFrame(
        id=uuid4(),
        parse_revision_id=revision.id,
        segment_id=segment.id,
        frame_index=0,
        file_frame_index=0,
        frame_role=FrameRole.SINGLE_POINT,
        source_start_byte=0,
        source_end_byte=1,
        source_start_line=1,
        source_end_line=2,
        source_block_sha256=artifact.content_sha256,
        geometry_id=geometry.id,
        topology_derivation_id=derivation.id,
        charge=0,
        multiplicity=1,
        geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
        observed_coordinates=coordinates,
        observed_coordinate_hash=_fixture_hash(f"domain-observed:{suffix}"),
        observed_to_geometry_atom_indices=[0, 1],
        observed_to_geometry_transform=np.eye(4, dtype=np.float64).reshape(-1).tolist(),
        geometry_assignment_rmsd_angstrom=0.0,
        geometry_assignment_max_abs_angstrom=0.0,
        geometry_assignment_policy_version="domain-filter-test-v1",
        electronic_total_energy_hartree=-1.0,
        selected_energy_hartree=-1.0,
        selected_energy_kind=SelectedEnergyKind.ELECTRONIC_TOTAL,
        energy_selection_policy_version="domain-filter-test-v1",
        frequency_count=1,
        negative_frequency_count=1,
        lowest_frequency_cm1=-100.0,
    )
    session.add(frame)
    session.flush()
    assert frame.id is not None
    values = np.array([-100.0], dtype=np.float64)
    array = ScientificArray(
        id=uuid4(),
        frame_id=frame.id,
        kind=ScientificArrayKind.VIBRATIONAL_FREQUENCIES,
        ordinal=0,
        unit="cm^-1",
        dtype=str(values.dtype),
        shape=list(values.shape),
        array_nbytes=values.nbytes,
        payload_sha256=_fixture_hash(f"domain-array:{suffix}"),
        data=values,
    )
    thermochemistry = ThermochemistryResult(
        frame_id=frame.id,
        temperature_kelvin=298.15,
        pressure_atm=1.0,
        gibbs_free_energy_hartree=-0.9,
        source_schema_version="domain-filter-test-v1",
    )
    logical_reaction = LogicalReaction(
        id=uuid4(),
        reaction_key=f"domain-filter:{suffix}",
        label=f"Domain filter {suffix}",
        reaction_hash=_fixture_hash(f"domain-logical:{suffix}"),
    )
    assert logical_reaction.id is not None
    mapped_reaction = MappedReaction(
        id=uuid4(),
        logical_reaction_id=logical_reaction.id,
        logical_reaction=logical_reaction,
        mapped_reaction_key="domain-filter-path",
        label=f"Domain filter path {suffix}",
        mapped_reaction_kind=MappedReactionKind.OTHER,
        mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
        mapping_hash=_fixture_hash(f"domain-mapped:{suffix}"),
        minimum_reaction_gibbs_free_energy_kcal_mol=1.0,
        maximum_reaction_gibbs_free_energy_kcal_mol=1.0,
    )
    participants = [
        LogicalReactionParticipant(
            logical_reaction_id=logical_reaction.id,
            topology_id=topology.id,
            side=LogicalReactionParticipantSide.REACTANT,
            participant_index=0,
            stoichiometric_coefficient=1,
        ),
        LogicalReactionParticipant(
            logical_reaction_id=logical_reaction.id,
            topology_id=topology.id,
            side=LogicalReactionParticipantSide.PRODUCT,
            participant_index=0,
            stoichiometric_coefficient=1,
        ),
    ]
    session.add_all([array, thermochemistry, mapped_reaction, *participants])
    session.flush()
    assert mapped_reaction.id is not None
    node = MappedReactionNode(
        id=uuid4(),
        mapped_reaction_id=mapped_reaction.id,
        node_key="transition-state",
        node_index=0,
        role=MappedReactionNodeRole.TRANSITION_STATE,
    )
    session.add(node)
    session.flush()
    assert node.id is not None
    session.add(
        MappedReactionNodeGeometry(
            mapped_reaction_node_id=node.id,
            geometry_id=geometry.id,
            component_key="transition-state",
            component_index=0,
        )
    )
    now = datetime.now(UTC)
    ingestion = ArtifactIngestion(
        id=uuid4(),
        artifact_file_id=artifact.id,
        status=ArtifactIngestionStatus.SUCCEEDED,
        parser_version="test-v1",
        source_frame_count=1,
        transition_state_frame_count=1,
        started_at=now,
        completed_at=now,
    )
    session.add(ingestion)
    session.flush()
    assert ingestion.id is not None
    inference = TransitionStateInference(
        id=uuid4(),
        artifact_ingestion_id=ingestion.id,
        parse_revision_id=revision.id,
        file_frame_index=0,
        imaginary_mode_index=0,
        imaginary_frequency_cm1=-100.0,
        status=TransitionStateInferenceStatus.SUCCEEDED,
        logical_reaction_id=logical_reaction.id,
        mapped_reaction_id=mapped_reaction.id,
        calculation_frame_id=frame.id,
    )
    session.add(inference)
    session.commit()
    return (
        inference,
        frame,
        segment,
        protocol,
        geometry,
        topology,
        mapped_reaction,
        logical_reaction,
        array,
        ingestion,
        artifact,
        revision,
        derivation,
        formula,
    )


def _delete_domain_sample(session: Session, sample: tuple[Any, ...]) -> None:
    (
        _inference,
        _frame,
        _segment,
        protocol,
        geometry,
        topology,
        _mapped_reaction,
        logical_reaction,
        _array,
        ingestion,
        artifact,
        revision,
        derivation,
        formula,
    ) = sample
    for entity in (
        ingestion,
        logical_reaction,
        revision,
        artifact,
        protocol,
        geometry,
        derivation,
        topology,
        formula,
    ):
        stored = session.get(type(entity), entity.id)
        if stored is not None:
            session.delete(stored)
            session.flush()
    session.commit()


def _reaction_geometry_counts(session: Session, mapped_reaction_id: UUID) -> tuple[int, int]:
    total = int(
        session.execute(
            select(func.count())
            .select_from(MappedReactionNodeGeometry)
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNode.mapped_reaction_id) == mapped_reaction_id,
                geometry_has_thermodynamic_property_predicate(
                    col(MappedReactionNodeGeometry.geometry_id)
                ),
            )
        ).scalar_one()
    )
    transition_states = int(
        session.execute(
            select(func.count())
            .select_from(MappedReactionNodeGeometry)
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNode.mapped_reaction_id) == mapped_reaction_id,
                col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                geometry_has_thermodynamic_property_predicate(
                    col(MappedReactionNodeGeometry.geometry_id)
                ),
            )
        ).scalar_one()
    )
    return total, transition_states


def test_domain_filters_compose_and_preserve_pagination_totals(
    development_query_principal: object,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    sample: tuple[Any, ...] | None = None
    try:
        with Session(engine, expire_on_commit=False) as session:
            sample = _create_domain_sample(session)
            (
                inference,
                frame,
                segment,
                protocol,
                geometry,
                topology,
                mapped_reaction,
                logical_reaction,
                array,
            ) = sample[:9]
            assert mapped_reaction.id is not None
            geometry_count, ts_geometry_count = _reaction_geometry_counts(
                session, mapped_reaction.id
            )

        inference_page = asyncio.run(
            TransitionStateInferenceQueryService.list_transition_state_inferences(
                logical_reaction_id=logical_reaction.id,
                mapped_reaction_id=mapped_reaction.id,
                calculation_frame_id=frame.id,
                minimum_imaginary_frequency_cm1=inference.imaginary_frequency_cm1,
                maximum_imaginary_frequency_cm1=inference.imaginary_frequency_cm1,
                limit=1,
                offset=0,
            )
        )
        assert inference_page.page.total == 1
        assert [item.id for item in inference_page.items] == [inference.id]
        assert inference_page.items[0].reactant_product_changed is False

        inference_unchanged_page = asyncio.run(
            TransitionStateInferenceQueryService.list_transition_state_inferences(
                logical_reaction_id=logical_reaction.id,
                reactant_product_changed=False,
                limit=1,
                offset=0,
            )
        )
        assert inference_unchanged_page.page.total == 1
        inference_changed_page = asyncio.run(
            TransitionStateInferenceQueryService.list_transition_state_inferences(
                logical_reaction_id=logical_reaction.id,
                reactant_product_changed=True,
                limit=1,
                offset=0,
            )
        )
        assert inference_changed_page.page.total == 0

        frame_page = asyncio.run(
            CalculationQueryService.list_calculation_frames(
                geometry_id=geometry.id,
                protocol_id=protocol.id,
                segment_index=segment.segment_index,
                frame_index=frame.frame_index,
                file_frame_index=frame.file_frame_index,
                charge=frame.charge,
                multiplicity=frame.multiplicity,
                minimum_frequency_count=frame.frequency_count,
                maximum_frequency_count=frame.frequency_count,
                minimum_negative_frequency_count=frame.negative_frequency_count,
                maximum_negative_frequency_count=frame.negative_frequency_count,
                minimum_lowest_frequency_cm1=frame.lowest_frequency_cm1,
                maximum_lowest_frequency_cm1=frame.lowest_frequency_cm1,
                minimum_energy_hartree=float(frame.selected_energy_hartree),
                maximum_energy_hartree=float(frame.selected_energy_hartree),
                limit=1,
                offset=0,
            )
        )
        assert frame_page.page.total == 1
        assert [item.id for item in frame_page.items] == [frame.id]

        array_page = asyncio.run(
            ScientificArrayQueryService.list_scientific_arrays(
                frame_id=frame.id,
                kind=str(array.kind),
                dtype=array.dtype,
                shape=list(array.shape),
                payload_sha256=array.payload_sha256,
                limit=1,
                offset=0,
            )
        )
        assert array_page.page.total == 1
        assert [item.id for item in array_page.items] == [array.id]

        geometry_page = asyncio.run(
            GeometryQueryService.list_geometries(
                topology_id=topology.id,
                geometry_hash=geometry.geometry_hash,
                topology_derivation_id=frame.topology_derivation_id,
                reaction_node_role=MappedReactionNodeRole.TRANSITION_STATE.value,
                imaginary_frequency_status="present",
                minimum_atom_count=topology.atom_count,
                maximum_atom_count=topology.atom_count,
                limit=1,
                offset=0,
            )
        )
        assert geometry_page.page.total == 1
        assert [item.id for item in geometry_page.items] == [geometry.id]
        assert geometry_page.items[0].is_transition_state is True
        assert geometry_page.items[0].imaginary_frequency_status == "present"

        geometry_detail = asyncio.run(GeometryQueryService.get_geometry(geometry_id=geometry.id))
        assert geometry_detail is not None
        assert geometry_detail.is_transition_state is True
        assert geometry_detail.imaginary_frequency_status == "present"

        geometry_and_page = asyncio.run(
            GeometryQueryService.list_geometries(
                filter_expression=json.dumps(
                    {
                        "operator": "and",
                        "conditions": [
                            {"field": "topology_id", "value": str(topology.id)},
                            {"field": "geometry_hash", "value": geometry.geometry_hash},
                            {
                                "field": "imaginary_frequency_status",
                                "value": "present",
                            },
                            {"field": "minimum_atom_count", "value": topology.atom_count},
                        ],
                    }
                ),
                limit=10,
                offset=0,
            )
        )
        assert geometry_and_page.page.total == 1
        assert [item.id for item in geometry_and_page.items] == [geometry.id]

        geometry_or_page = asyncio.run(
            GeometryQueryService.list_geometries(
                filter_expression=json.dumps(
                    {
                        "operator": "or",
                        "conditions": [
                            {"field": "geometry_hash", "value": geometry.geometry_hash},
                            {"field": "geometry_hash", "value": "not-a-real-hash"},
                        ],
                    }
                ),
                limit=10,
                offset=0,
            )
        )
        assert geometry_or_page.page.total == 1
        assert [item.id for item in geometry_or_page.items] == [geometry.id]

        geometry_not_page = asyncio.run(
            GeometryQueryService.list_geometries(
                filter_expression=json.dumps(
                    {
                        "operator": "and",
                        "conditions": [
                            {"field": "geometry_hash", "value": geometry.geometry_hash},
                            {
                                "field": "geometry_hash",
                                "value": geometry.geometry_hash,
                                "negated": True,
                            },
                        ],
                    }
                ),
                limit=10,
                offset=0,
            )
        )
        assert geometry_not_page.page.total == 0
        assert geometry_not_page.items == []

        mapped_page = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                logical_reaction_id=logical_reaction.id,
                mapping_hash=mapped_reaction.mapping_hash,
                label=mapped_reaction.label,
                node_role=MappedReactionNodeRole.TRANSITION_STATE.value,
                minimum_transition_state_geometry_count=ts_geometry_count,
                maximum_transition_state_geometry_count=ts_geometry_count,
                minimum_geometry_count=geometry_count,
                maximum_geometry_count=geometry_count,
                created_after=mapped_reaction.created_at,
                created_before=mapped_reaction.created_at,
                limit=1,
                offset=0,
            )
        )
        assert mapped_page.page.total == 1
        assert [item.id for item in mapped_page.items] == [mapped_reaction.id]
        assert mapped_page.items[0].reactant_product_changed is False
        mapped_changed_page = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                logical_reaction_id=logical_reaction.id,
                reactant_product_changed=True,
                limit=1,
                offset=0,
            )
        )
        assert mapped_changed_page.page.total == 0
        assert mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol is not None
        reaction_gibbs = mapped_reaction.minimum_reaction_gibbs_free_energy_kcal_mol
        mapped_reaction_gibbs_page = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                minimum_reaction_gibbs_free_energy_kcal_mol=reaction_gibbs - 0.01,
                maximum_reaction_gibbs_free_energy_kcal_mol=reaction_gibbs + 0.01,
                limit=1,
                offset=0,
            )
        )
        assert mapped_reaction_gibbs_page.page.total == 1
        assert [item.id for item in mapped_reaction_gibbs_page.items] == [mapped_reaction.id]

        mapped_second_page = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                mapping_hash=mapped_reaction.mapping_hash,
                node_role=MappedReactionNodeRole.TRANSITION_STATE.value,
                limit=1,
                offset=1,
            )
        )
        assert mapped_second_page.page.total == 1
        assert mapped_second_page.items == []

        # A negative frequency alone must not classify a geometry as a transition state.
        with Session(engine) as session:
            node = (
                session.execute(
                    select(MappedReactionNode).where(
                        col(MappedReactionNode.mapped_reaction_id) == mapped_reaction.id
                    )
                )
                .scalars()
                .one()
            )
            node.role = MappedReactionNodeRole.INTERMEDIATE
            session.add(node)
            session.commit()
        non_transition_state_detail = asyncio.run(
            GeometryQueryService.get_geometry(geometry_id=geometry.id)
        )
        assert non_transition_state_detail is not None
        assert non_transition_state_detail.is_transition_state is False
        assert non_transition_state_detail.imaginary_frequency_status == "present"

        logical_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_reaction.reaction_hash,
                label=logical_reaction.label,
                created_after=logical_reaction.created_at,
                created_before=logical_reaction.created_at,
                limit=1,
                offset=0,
            )
        )
        assert logical_page.page.total == 1
        assert [item.id for item in logical_page.items] == [logical_reaction.id]
        assert logical_page.items[0].reactant_product_changed is False
        logical_changed_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_reaction.reaction_hash,
                reactant_product_changed=True,
                limit=1,
                offset=0,
            )
        )
        assert logical_changed_page.page.total == 0
        with Session(engine) as session:
            product_participant = (
                session.execute(
                    select(LogicalReactionParticipant).where(
                        col(LogicalReactionParticipant.logical_reaction_id) == logical_reaction.id,
                        col(LogicalReactionParticipant.side)
                        == LogicalReactionParticipantSide.PRODUCT,
                    )
                )
                .scalars()
                .one()
            )
            product_participant.stoichiometric_coefficient = 2
            session.add(product_participant)
            session.commit()
        logical_stoichiometry_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_reaction.reaction_hash,
                reactant_product_changed=True,
                limit=1,
                offset=0,
            )
        )
        assert logical_stoichiometry_page.page.total == 1
        logical_expression_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                filter_expression=json.dumps(
                    {
                        "operator": "and",
                        "conditions": [
                            {"field": "reaction_hash", "value": logical_reaction.reaction_hash},
                            {"field": "reactant_product_changed", "value": True},
                        ],
                    }
                ),
                limit=1,
                offset=0,
            )
        )
        assert logical_expression_page.page.total == 1
        logical_reaction_gibbs_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                minimum_reaction_gibbs_free_energy_kcal_mol=reaction_gibbs - 0.01,
                maximum_reaction_gibbs_free_energy_kcal_mol=reaction_gibbs + 0.01,
                limit=1,
                offset=0,
            )
        )
        assert logical_reaction_gibbs_page.page.total == 1
        assert [item.id for item in logical_reaction_gibbs_page.items] == [logical_reaction.id]
        logical_has_reaction_gibbs_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_reaction.reaction_hash,
                has_reaction_gibbs_free_energy=True,
                limit=1,
                offset=0,
            )
        )
        assert logical_has_reaction_gibbs_page.page.total == 1
        assert [item.id for item in logical_has_reaction_gibbs_page.items] == [logical_reaction.id]
        logical_has_activation_gibbs_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_reaction.reaction_hash,
                has_activation_gibbs_free_energy=True,
                limit=1,
                offset=0,
            )
        )
        assert logical_has_activation_gibbs_page.page.total == 0
    finally:
        if sample is not None:
            with Session(engine) as session:
                _delete_domain_sample(session, sample)
        engine.dispose()
        asyncio.run(dispose_engine())


def test_logical_reactions_are_grouped_by_reactant_set_across_pages(
    development_query_principal: object,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    grouped_reaction_ids: set[UUID] = set()
    grouped_topology_id: UUID | None = None
    grouped_formula_id: UUID | None = None
    try:
        with Session(engine, expire_on_commit=False) as session:
            suffix = uuid4().hex
            molecule = Chem.MolFromSmiles("[H][H]")
            assert molecule is not None
            vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
            vector[0] = 2
            formula = MolecularFormula(
                id=uuid4(),
                hill_formula="H2",
                composition=[{"atomic_number": 1, "isotope": 0, "count": 2}],
                atom_count=2,
                composition_hash=_fixture_hash(f"grouped-formula:{suffix}"),
                element_count_vector=vector,
            )
            assert formula.id is not None
            topology = MolecularTopology(
                id=uuid4(),
                formula_id=formula.id,
                formula=formula,
                mol=molecule,
                canonical_isomeric_smiles="[H][H]",
                graph_hash=_fixture_hash(f"grouped-topology:{suffix}"),
                identity_schema_version="grouped-reaction-test-v1",
                atom_count=2,
                heavy_atom_count=0,
                formal_charge=0,
                radical_electron_count=0,
                fragment_count=1,
            )
            session.add_all([formula, topology])
            session.flush()
            assert topology.id is not None
            grouped_formula_id = formula.id
            grouped_topology_id = topology.id
            for index in range(3):
                logical = LogicalReaction(
                    id=uuid4(),
                    reaction_key=f"grouped:{suffix}:{index}",
                    label=f"Grouped reaction {index}",
                    reaction_hash=_fixture_hash(f"grouped-logical:{suffix}:{index}"),
                )
                assert logical.id is not None
                mapped = MappedReaction(
                    logical_reaction_id=logical.id,
                    logical_reaction=logical,
                    mapped_reaction_key="source-less",
                    label=f"Grouped path {index}",
                    mapped_reaction_kind=MappedReactionKind.OTHER,
                    mapped_reaction_smiles="[H:1][H:2]>>[H:1][H:2]",
                    mapping_hash=_fixture_hash(f"grouped-mapped:{suffix}:{index}"),
                )
                participant = LogicalReactionParticipant(
                    logical_reaction_id=logical.id,
                    topology_id=topology.id,
                    side=LogicalReactionParticipantSide.REACTANT,
                    participant_index=0,
                    stoichiometric_coefficient=1,
                )
                session.add_all([mapped, participant])
                grouped_reaction_ids.add(logical.id)
            session.commit()

            reaction_rows = session.execute(
                select(col(LogicalReaction.id), col(LogicalReaction.created_at))
            ).all()
            reactants_by_reaction: dict[UUID, list[tuple[str, int]]] = {}
            for logical_reaction_id, smiles, coefficient in session.execute(
                select(
                    col(LogicalReactionParticipant.logical_reaction_id),
                    col(MolecularTopology.canonical_isomeric_smiles),
                    col(LogicalReactionParticipant.stoichiometric_coefficient),
                )
                .join(
                    MolecularTopology,
                    col(LogicalReactionParticipant.topology_id) == col(MolecularTopology.id),
                )
                .where(
                    col(LogicalReactionParticipant.side) == LogicalReactionParticipantSide.REACTANT
                )
            ):
                reactants_by_reaction.setdefault(logical_reaction_id, []).append(
                    (smiles, coefficient)
                )

        reaction_created_at: dict[UUID, datetime] = {}
        for row in reaction_rows:
            reaction_created_at[cast(UUID, row[0])] = cast(datetime, row[1])
        page_size = 2
        first_page = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                limit=page_size,
                offset=0,
            )
        )
        actual_ids = [item.id for item in first_page.items]
        for page_offset in range(page_size, first_page.page.total, page_size):
            page = asyncio.run(
                LogicalReactionQueryService.list_logical_reactions(
                    limit=page_size,
                    offset=page_offset,
                )
            )
            actual_ids.extend(item.id for item in page.items)

        assert first_page.page.total == len(reaction_rows)
        assert set(actual_ids) == set(reaction_created_at)

        seen_reactant_sets: set[tuple[tuple[str, int], ...]] = set()
        current_reactant_set: tuple[tuple[str, int], ...] | None = None
        current_group_tie_breakers: list[tuple[datetime, UUID]] = []
        group_sizes: list[int] = []
        for logical_reaction_id in actual_ids:
            reactant_set = tuple(sorted(reactants_by_reaction.get(logical_reaction_id, [])))
            if reactant_set != current_reactant_set:
                if current_reactant_set is not None:
                    assert current_group_tie_breakers == sorted(current_group_tie_breakers)
                    group_sizes.append(len(current_group_tie_breakers))
                assert reactant_set not in seen_reactant_sets
                seen_reactant_sets.add(reactant_set)
                current_reactant_set = reactant_set
                current_group_tie_breakers = []
            current_group_tie_breakers.append(
                (reaction_created_at[logical_reaction_id], logical_reaction_id)
            )
        assert current_group_tie_breakers == sorted(current_group_tie_breakers)
        group_sizes.append(len(current_group_tie_breakers))
        assert any(group_size > page_size for group_size in group_sizes)
    finally:
        if (
            grouped_reaction_ids
            or grouped_topology_id is not None
            or grouped_formula_id is not None
        ):
            with Session(engine) as session:
                if grouped_reaction_ids:
                    rows = session.execute(
                        select(LogicalReaction).where(
                            col(LogicalReaction.id).in_(grouped_reaction_ids)
                        )
                    ).scalars()
                    for row in rows:
                        session.delete(row)
                    session.flush()
                if grouped_topology_id is not None:
                    topology = session.get(MolecularTopology, grouped_topology_id)
                    if topology is not None:
                        session.delete(topology)
                        session.flush()
                if grouped_formula_id is not None:
                    formula = session.get(MolecularFormula, grouped_formula_id)
                    if formula is not None:
                        session.delete(formula)
                session.commit()
        engine.dispose()
        asyncio.run(dispose_engine())


def test_protocol_version_family_and_solvation_filters_compose(
    development_query_principal: object,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    sample: tuple[Any, ...] | None = None
    protocol_id: UUID | None = None
    segment_id: UUID | None = None
    suffix = uuid4().hex
    try:
        with Session(engine, expire_on_commit=False) as session:
            sample = _create_domain_sample(session)
            revision = cast(ParseRevision, sample[11])
            assert revision.id is not None
            parse_revision_id = revision.id
            next_segment_index = (
                int(
                    session.execute(
                        select(func.coalesce(func.max(CalculationSegment.segment_index), -1)).where(
                            col(CalculationSegment.parse_revision_id) == parse_revision_id
                        )
                    ).scalar_one()
                )
                + 1
            )
            protocol = CalculationProtocol(
                protocol_hash=hashlib.sha256(suffix.encode()).hexdigest(),
                qm_software=QMSoftware.OTHER,
                qm_software_version=f"query-test-{suffix}",
                method_family=f"method-family-{suffix}",
                solvation_model=f"solvation-model-{suffix}",
                task_requests=[],
                normalized_spec={"query_test": suffix},
            )
            session.add(protocol)
            session.flush()
            assert protocol.id is not None
            segment = CalculationSegment(
                parse_revision_id=parse_revision_id,
                protocol_id=protocol.id,
                segment_index=next_segment_index,
                source_start_byte=0,
                source_end_byte=1,
                source_start_line=1,
                source_end_line=2,
                source_block_sha256=hashlib.sha256(f"segment-{suffix}".encode()).hexdigest(),
            )
            session.add(segment)
            session.commit()
            protocol_id = protocol.id
            assert segment.id is not None
            segment_id = segment.id

        page = asyncio.run(
            CalculationProtocolQueryService.list_calculation_protocols(
                qm_software_version=protocol.qm_software_version,
                method_family=protocol.method_family,
                solvation_model=protocol.solvation_model,
                limit=1,
                offset=0,
            )
        )
        assert page.page.total == 1
        assert [item.id for item in page.items] == [protocol_id]
    finally:
        if protocol_id is not None or segment_id is not None:
            with Session(engine) as session:
                if segment_id is not None:
                    stored_segment = session.get(CalculationSegment, segment_id)
                    if stored_segment is not None:
                        session.delete(stored_segment)
                        session.flush()
                if protocol_id is not None:
                    stored_protocol = session.get(CalculationProtocol, protocol_id)
                    if stored_protocol is not None:
                        session.delete(stored_protocol)
                session.commit()
                if sample is not None:
                    _delete_domain_sample(session, sample)
        elif sample is not None:
            with Session(engine) as session:
                _delete_domain_sample(session, sample)
        engine.dispose()
        asyncio.run(dispose_engine())


def _sequential_scan_relations(plan: dict[str, Any]) -> set[str]:
    relations = {
        str(plan["Relation Name"])
        for _ in [None]
        if plan.get("Node Type") == "Seq Scan" and plan.get("Relation Name") is not None
    }
    for child in plan.get("Plans", []):
        relations.update(_sequential_scan_relations(child))
    return relations


def test_low_selectivity_metadata_scans_have_bounded_cost() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    scan_row_limits = {
        "calculation_frame": 10_000,
        "scientific_array": 25_000,
        "molecular_topology": 10_000,
        "calculation_protocol": 1_000,
    }
    statements = {
        "frame frequency metadata": """
            SELECT count(*) FROM calculation_frame
            WHERE frequency_count BETWEEN 1 AND 1000
              AND negative_frequency_count BETWEEN 1 AND 1
        """,
        "array dtype and shape": """
            SELECT count(*) FROM scientific_array
            WHERE dtype = 'float64' AND shape = ARRAY[24, 3]
        """,
        "geometry atom count": """
            SELECT count(*) FROM geometry
            WHERE topology_id IN (
                SELECT id FROM molecular_topology WHERE atom_count BETWEEN 1 AND 100
            )
        """,
        "protocol metadata": """
            SELECT count(*) FROM calculation_protocol
            WHERE qm_software_version IS NOT NULL
              AND method_family = 'DFT'
              AND solvation_model IS NULL
        """,
    }
    try:
        with engine.connect() as connection:
            relation_counts = {
                relation: int(
                    connection.execute(text(f"SELECT count(*) FROM {relation}")).scalar_one()
                )
                for relation in scan_row_limits
            }
            for label, statement in statements.items():
                explain = connection.execute(
                    text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}")
                ).scalar_one()[0]
                plan = explain["Plan"]
                execution_time_ms = float(explain["Execution Time"])
                assert execution_time_ms < 250.0, (
                    f"{label} metadata filter took {execution_time_ms:.3f} ms"
                )
                for relation in _sequential_scan_relations(plan):
                    if relation in scan_row_limits:
                        assert relation_counts[relation] <= scan_row_limits[relation], (
                            f"{label} sequentially scans {relation_counts[relation]} rows in "
                            f"{relation}; add a selective index or refresh this benchmark"
                        )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("method", "parameters", "message"),
    [
        (
            LogicalReactionQueryService.list_logical_reactions,
            {
                "created_after": datetime(2026, 1, 2, tzinfo=UTC),
                "created_before": datetime(2026, 1, 1, tzinfo=UTC),
            },
            "created_after cannot exceed created_before",
        ),
        (
            MappedReactionQueryService.list_mapped_reactions,
            {
                "minimum_transition_state_geometry_count": 2,
                "maximum_transition_state_geometry_count": 1,
            },
            "minimum_transition_state_geometry_count cannot exceed",
        ),
        (
            MappedReactionQueryService.list_mapped_reactions,
            {"minimum_geometry_count": 2, "maximum_geometry_count": 1},
            "minimum_geometry_count cannot exceed maximum_geometry_count",
        ),
        (
            LogicalReactionQueryService.list_logical_reactions,
            {
                "minimum_reaction_gibbs_free_energy_kcal_mol": 2.0,
                "maximum_reaction_gibbs_free_energy_kcal_mol": 1.0,
            },
            "minimum_reaction_gibbs_free_energy_kcal_mol cannot exceed",
        ),
        (
            MappedReactionQueryService.list_mapped_reactions,
            {
                "minimum_reaction_gibbs_free_energy_kcal_mol": 2.0,
                "maximum_reaction_gibbs_free_energy_kcal_mol": 1.0,
            },
            "minimum_reaction_gibbs_free_energy_kcal_mol cannot exceed",
        ),
        (
            MappedReactionQueryService.list_mapped_reactions,
            {
                "created_after": datetime(2026, 1, 2, tzinfo=UTC),
                "created_before": datetime(2026, 1, 1, tzinfo=UTC),
            },
            "created_after cannot exceed created_before",
        ),
        (
            GeometryQueryService.list_geometries,
            {"minimum_atom_count": 2, "maximum_atom_count": 1},
            "minimum_atom_count cannot exceed maximum_atom_count",
        ),
        (
            TransitionStateInferenceQueryService.list_transition_state_inferences,
            {
                "minimum_imaginary_frequency_cm1": -100.0,
                "maximum_imaginary_frequency_cm1": -200.0,
            },
            "minimum_imaginary_frequency_cm1 cannot exceed",
        ),
        (
            CalculationQueryService.list_calculation_frames,
            {"minimum_frequency_count": 2, "maximum_frequency_count": 1},
            "minimum_frequency_count cannot exceed maximum_frequency_count",
        ),
        (
            CalculationQueryService.list_calculation_frames,
            {"minimum_negative_frequency_count": 2, "maximum_negative_frequency_count": 1},
            "minimum_negative_frequency_count cannot exceed",
        ),
        (
            CalculationQueryService.list_calculation_frames,
            {"minimum_lowest_frequency_cm1": -100.0, "maximum_lowest_frequency_cm1": -200.0},
            "minimum_lowest_frequency_cm1 cannot exceed",
        ),
        (
            CalculationQueryService.list_calculation_frames,
            {"minimum_energy_hartree": -10.0, "maximum_energy_hartree": -20.0},
            "minimum_energy_hartree cannot exceed maximum_energy_hartree",
        ),
        (
            ScientificArrayQueryService.list_scientific_arrays,
            {"shape": []},
            "shape must contain nonnegative dimensions",
        ),
        (
            ScientificArrayQueryService.list_scientific_arrays,
            {"shape": [3, -1]},
            "shape must contain nonnegative dimensions",
        ),
    ],
)
def test_domain_filter_bounds_are_validated(
    method: Any,
    parameters: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        asyncio.run(method(**parameters))
