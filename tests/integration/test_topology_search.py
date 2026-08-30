import asyncio
import hashlib
import os
from collections.abc import Iterator
from uuid import uuid4

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from rdkit import Chem
from sqlalchemy import create_engine
from sqlmodel import Session

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.apps import paginated_graphql_app, use_case_rest_app
from tricycle_reaction_db.application.dtos import MolecularTopologySearchQuery
from tricycle_reaction_db.application.query_cost import QueryBudgetExceeded
from tricycle_reaction_db.application.services import queries as query_services
from tricycle_reaction_db.application.services.additional_queries import (
    MolecularTopologyDetailQueryService,
)
from tricycle_reaction_db.application.services.queries import MolecularTopologyQueryService
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    CalculationSegment,
    Geometry,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    ParseRevision,
)
from tricycle_reaction_db.db.session import dispose_engine
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactVisibility,
    FrameRole,
    GeometryAssignmentKind,
    SourceFormat,
    StereoStatus,
    StorageStatus,
    TopologySanitizationStatus,
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


def _formula(
    hill_formula: str,
    counts: dict[int, int],
    suffix: str,
) -> MolecularFormula:
    vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
    for atomic_number, count in counts.items():
        vector[atomic_number - 1] = count
    return MolecularFormula(
        hill_formula=hill_formula,
        composition=[
            {"atomic_number": atomic_number, "isotope": 0, "count": count}
            for atomic_number, count in sorted(counts.items())
        ],
        composition_schema_version="formula-composition-v1",
        atom_count=sum(counts.values()),
        composition_hash=hashlib.sha256(
            f"topology-search-formula-{suffix}-{uuid4()}".encode()
        ).hexdigest(),
        element_count_vector=vector,
    )


def _explicit_h_smiles(smiles: str) -> str:
    parsed = Chem.MolFromSmiles(smiles)
    assert parsed is not None
    return Chem.MolToSmiles(
        Chem.AddHs(parsed),
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )


def _topology(formula: MolecularFormula, smiles: str, suffix: str) -> MolecularTopology:
    parsed = Chem.MolFromSmiles(smiles)
    assert parsed is not None
    molecule = Chem.AddHs(parsed)
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    display_molecule = Chem.RemoveHs(Chem.Mol(molecule))
    return MolecularTopology(
        formula=formula,
        mol=molecule,
        canonical_isomeric_smiles=_explicit_h_smiles(smiles),
        graph_hash=hashlib.sha256(f"topology-search-{suffix}-{uuid4()}".encode()).hexdigest(),
        identity_schema_version="topology-search-test-v1",
        atom_count=molecule.GetNumAtoms(),
        heavy_atom_count=display_molecule.GetNumAtoms(),
        formal_charge=Chem.GetFormalCharge(molecule),
        radical_electron_count=sum(atom.GetNumRadicalElectrons() for atom in molecule.GetAtoms()),
        fragment_count=len(Chem.GetMolFrags(molecule)),
        stereo_status=StereoStatus.ASSIGNED,
    )


def _unsanitized_topology(
    formula: MolecularFormula,
    suffix: str,
) -> MolecularTopology:
    molecule = Chem.RWMol()
    carbon_index = molecule.AddAtom(Chem.Atom(6))
    for _ in range(5):
        hydrogen_index = molecule.AddAtom(Chem.Atom(1))
        molecule.AddBond(carbon_index, hydrogen_index, Chem.BondType.SINGLE)
    graph = molecule.GetMol()
    error = "AtomValenceException: explicit valence 5 is greater than permitted"
    return MolecularTopology(
        formula=formula,
        mol=graph,
        canonical_isomeric_smiles=Chem.MolToSmiles(graph, canonical=True),
        graph_hash=hashlib.sha256(f"topology-search-{suffix}-{uuid4()}".encode()).hexdigest(),
        identity_schema_version="topology-search-test-v1",
        atom_count=graph.GetNumAtoms(),
        heavy_atom_count=1,
        formal_charge=0,
        radical_electron_count=0,
        fragment_count=1,
        stereo_status=StereoStatus.UNKNOWN,
        sanitization_status=TopologySanitizationStatus.FAILED,
        sanitization_error=error,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _add_visible_calculation_frames(
    session: Session,
    topologies: list[MolecularTopology],
    *,
    suffix: str,
) -> tuple[ArtifactFile, ParseRevision, list[Geometry], list[MolecularTopologyDerivation]]:
    source_hash = _sha256(f"topology-search-source-{suffix}")
    artifact = ArtifactFile(
        project_id=SYSTEM_PROJECT_ID,
        created_by_user_id=DEVELOPMENT_USER_ID,
        visibility=ArtifactVisibility.PUBLIC,
        bucket="integration-test",
        object_key=f"topology-search/{suffix}.log",
        content_sha256=source_hash,
        size_bytes=1,
        original_filename=f"topology-search-{suffix}.log",
        media_type="text/plain",
        artifact_kind=ArtifactKind.CALCULATION_OUTPUT,
        storage_status=StorageStatus.AVAILABLE,
    )
    revision = ParseRevision(
        artifact_file=artifact,
        export_schema_version="topology-search-test-v1",
        parser_version="test-v1",
        parser_id="tests.topology-search",
        molop_version="test-v1",
        rdkit_version="test-v1",
        parser_provenance={"fixture": "topology-search"},
        parser_provenance_hash=_sha256(f"topology-search-provenance-{suffix}"),
        parser_config_hash=_sha256(f"topology-search-parser-config-{suffix}"),
        reconstruction_config_hash=_sha256(f"topology-search-reconstruction-{suffix}"),
        source_format=SourceFormat.GAUSSIAN_LOG,
        source_encoding="utf-8",
    )
    segment = CalculationSegment(
        parse_revision=revision,
        segment_index=0,
        source_start_byte=0,
        source_end_byte=1,
        source_start_line=1,
        source_end_line=2,
        source_block_sha256=source_hash,
    )
    session.add(segment)
    session.flush()
    assert revision.id is not None

    geometries: list[Geometry] = []
    derivations: list[MolecularTopologyDerivation] = []
    for index, topology in enumerate(topologies):
        coordinates = np.zeros((topology.atom_count, 3), dtype=np.float64)
        coordinates[:, 0] = np.arange(topology.atom_count, dtype=np.float64)
        coordinate_hash = _sha256(f"topology-search-coordinates-{suffix}-{index}")
        derivation = MolecularTopologyDerivation(
            topology=topology,
            reconstruction_method="test/topology-search",
            reconstruction_version="1",
            reconstruction_metadata={"fixture_index": index},
            provenance_hash=_sha256(f"topology-search-derivation-{suffix}-{index}"),
        )
        geometry = Geometry(
            topology=topology,
            mol=Chem.Mol(topology.mol),
            internal_coordinates=coordinates,
            internal_coordinate_distances_angstrom=[1.0],
            internal_coordinate_angles_degrees=[0.0],
            internal_coordinate_dihedrals_degrees=[0.0],
            internal_coordinate_hash=coordinate_hash,
            geometry_hash=_sha256(f"topology-search-geometry-{suffix}-{index}"),
            canonicalization_version="topology-search-test-v1",
        )
        session.add_all((derivation, geometry))
        session.flush()
        assert segment.id is not None
        assert geometry.id is not None
        assert derivation.id is not None
        session.add(
            CalculationFrame(
                parse_revision_id=revision.id,
                segment_id=segment.id,
                frame_index=index,
                file_frame_index=index,
                frame_role=FrameRole.SINGLE_POINT,
                source_start_byte=0,
                source_end_byte=1,
                source_start_line=1,
                source_end_line=2,
                source_block_sha256=source_hash,
                geometry_id=geometry.id,
                topology_derivation_id=derivation.id,
                charge=topology.formal_charge,
                multiplicity=1,
                geometry_assignment_kind=GeometryAssignmentKind.PARSED_EXACT,
                observed_coordinates=coordinates,
                observed_coordinate_hash=coordinate_hash,
                observed_to_geometry_atom_indices=list(range(topology.atom_count)),
                observed_to_geometry_transform=np.eye(4, dtype=np.float64).reshape(-1).tolist(),
                geometry_assignment_rmsd_angstrom=0.0,
                geometry_assignment_max_abs_angstrom=0.0,
                geometry_assignment_policy_version="topology-search-test-v1",
            )
        )
        geometries.append(geometry)
        derivations.append(derivation)
    session.commit()
    return artifact, revision, geometries, derivations


@pytest.fixture
def inserted_topologies() -> Iterator[dict[str, object]]:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    suffix = uuid4().hex
    ethanol_formula = _formula("C2H6O", {1: 6, 6: 2, 8: 1}, "ethanol")
    benzene_formula = _formula("C6H6", {1: 6, 6: 6}, "benzene")
    chiral_formula = _formula("C2H5FO", {1: 5, 6: 2, 8: 1, 9: 1}, "chiral")
    rare_formula = _formula(
        "C2H6ClFSi",
        {1: 6, 6: 2, 9: 1, 14: 1, 17: 1},
        "rare-isotope",
    )
    unsanitized_formula = _formula("CH5", {1: 5, 6: 1}, "unsanitized")
    topologies = {
        "ethanol": _topology(ethanol_formula, "CCO", "ethanol"),
        "ether": _topology(ethanol_formula, "COC", "ether"),
        "benzene": _topology(benzene_formula, "c1ccccc1", "benzene"),
        "right": _topology(chiral_formula, "C[C@H](O)F", "right"),
        "left": _topology(chiral_formula, "C[C@@H](O)F", "left"),
        "rare": _topology(rare_formula, "[13CH3][Si](C)(F)Cl", "rare-isotope"),
        "unsanitized": _unsanitized_topology(unsanitized_formula, "unsanitized"),
    }
    formulas = [
        ethanol_formula,
        benzene_formula,
        chiral_formula,
        rare_formula,
        unsanitized_formula,
    ]
    artifact: ArtifactFile | None = None
    revision: ParseRevision | None = None
    geometries: list[Geometry] = []
    derivations: list[MolecularTopologyDerivation] = []
    try:
        with Session(engine, expire_on_commit=False) as session:
            session.add_all(formulas)
            session.flush()
            session.add_all(topologies.values())
            session.flush()
            artifact, revision, geometries, derivations = _add_visible_calculation_frames(
                session,
                list(topologies.values()),
                suffix=suffix,
            )
        yield {"formulas": formulas, "topologies": topologies}
    finally:
        with Session(engine) as session:
            if revision is not None and revision.id is not None:
                persisted_revision = session.get(ParseRevision, revision.id)
                if persisted_revision is not None:
                    session.delete(persisted_revision)
                    session.flush()
            if artifact is not None and artifact.id is not None:
                persisted_artifact = session.get(ArtifactFile, artifact.id)
                if persisted_artifact is not None:
                    session.delete(persisted_artifact)
                    session.flush()
            for geometry in geometries:
                if geometry.id is not None:
                    persisted = session.get(Geometry, geometry.id)
                    if persisted is not None:
                        session.delete(persisted)
            session.flush()
            for derivation in derivations:
                if derivation.id is not None:
                    persisted = session.get(MolecularTopologyDerivation, derivation.id)
                    if persisted is not None:
                        session.delete(persisted)
            session.flush()
            for topology in topologies.values():
                if topology.id is not None:
                    persisted = session.get(MolecularTopology, topology.id)
                    if persisted is not None:
                        session.delete(persisted)
            session.flush()
            for formula in formulas:
                if formula.id is not None:
                    persisted = session.get(MolecularFormula, formula.id)
                    if persisted is not None:
                        session.delete(persisted)
            session.commit()
        engine.dispose()


def test_formula_prefilter_exact_smiles_and_substructure_counts(
    inserted_topologies: dict[str, object],
) -> None:
    formulas = inserted_topologies["formulas"]
    assert isinstance(formulas, list)
    ethanol_formula = formulas[0]
    assert isinstance(ethanol_formula, MolecularFormula)

    formula_prefiltered = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_id=ethanol_formula.id,
                smarts="CO",
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    assert {item.canonical_isomeric_smiles for item in formula_prefiltered.items} == {
        _explicit_h_smiles("CCO"),
        _explicit_h_smiles("COC"),
    }
    assert all(item.hill_formula == "C2H6O" for item in formula_prefiltered.items)

    exact = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(exact_smiles="CCO").model_dump(),
            limit=10,
            offset=0,
        )
    )
    assert [item.canonical_isomeric_smiles for item in exact.items] == [_explicit_h_smiles("CCO")]
    assert exact.items[0].molecular_weight == pytest.approx(46.069)
    assert exact.items[0].hba_count == 1
    assert exact.items[0].hbd_count == 1

    descriptor_filtered = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C6H6",
                minimum_ring_count=1,
                maximum_ring_count=1,
                scaffold_smiles="c1ccccc1",
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    assert [item.canonical_isomeric_smiles for item in descriptor_filtered.items] == [
        _explicit_h_smiles("c1ccccc1")
    ]
    assert descriptor_filtered.items[0].ring_count == 1
    assert descriptor_filtered.items[0].scaffold_smiles == "c1ccccc1"

    nearest = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C2H6O",
                similarity_smiles="OCC",
            ).model_dump(),
            limit=2,
            offset=0,
        )
    )
    assert nearest.page.total == 2
    assert [item.canonical_isomeric_smiles for item in nearest.items] == [
        _explicit_h_smiles("CCO"),
        _explicit_h_smiles("COC"),
    ]
    assert nearest.items[0].similarity_score == pytest.approx(1.0)
    assert all(item.morgan_bfp_schema_version == "morgan-bfp-r2-v1" for item in nearest.items)

    tanimoto_threshold = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C2H6O",
                similarity_smiles="CCO",
                minimum_similarity=0.99,
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    dice_threshold = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C2H6O",
                similarity_smiles="CCO",
                similarity_metric="dice",
                minimum_similarity=0.99,
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    assert [item.canonical_isomeric_smiles for item in tanimoto_threshold.items] == [
        _explicit_h_smiles("CCO")
    ]
    assert [item.canonical_isomeric_smiles for item in dice_threshold.items] == [
        _explicit_h_smiles("CCO")
    ]
    assert tanimoto_threshold.items[0].similarity_score == pytest.approx(1.0)
    assert dice_threshold.items[0].similarity_score == pytest.approx(1.0)

    counted = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C6H6",
                smarts="[c]",
                minimum_substructure_matches=6,
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    assert [item.canonical_isomeric_smiles for item in counted.items] == [
        _explicit_h_smiles("c1ccccc1")
    ]
    assert counted.items[0].substructure_match_count == 6


def test_unsanitized_topology_remains_available_to_smarts_search(
    inserted_topologies: dict[str, object],
) -> None:
    result = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="CH5",
                smarts="[#6]-[#1]",
                sanitization_status=TopologySanitizationStatus.FAILED,
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )

    assert len(result.items) == 1
    topology = result.items[0]
    assert topology.sanitization_status == "failed"
    assert topology.sanitization_error is not None
    assert topology.substructure_match_count == 5
    assert topology.morgan_bfp_available is False
    assert topology.similarity_score is None
    assert topology.molecular_weight is None
    assert topology.logp is None
    assert topology.tpsa is None

    detail = asyncio.run(MolecularTopologyDetailQueryService.get_topology(topology_id=topology.id))
    assert detail is not None
    assert detail.id == topology.id
    assert detail.sanitization_status == "failed"


def test_chiral_smarts_search_can_distinguish_enantiomers(
    inserted_topologies: dict[str, object],
) -> None:
    topologies = inserted_topologies["topologies"]
    assert isinstance(topologies, dict)
    right = topologies["right"]
    assert isinstance(right, MolecularTopology)
    non_chiral = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C2H5FO",
                smarts="C[C@H](O)F",
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )
    chiral = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                formula_hill_formula="C2H5FO",
                smarts="C[C@H](O)F",
                match_chirality=True,
            ).model_dump(),
            limit=10,
            offset=0,
        )
    )

    assert len(non_chiral.items) == 2
    assert [item.id for item in chiral.items] == [right.id]


def test_candidate_budget_limits_only_the_actual_topology_scan(
    inserted_topologies: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topologies = inserted_topologies["topologies"]
    assert isinstance(topologies, dict)
    rare = topologies["rare"]
    assert isinstance(rare, MolecularTopology)
    settings = get_settings().model_copy(update={"structure_candidate_limit": 1})
    monkeypatch.setattr(query_services, "get_settings", lambda: settings)

    for unindexed_filter in (
        {"minimum_molecular_weight": 0.0},
        {"scaffold_smiles": "c1ccccc1"},
    ):
        with pytest.raises(QueryBudgetExceeded, match="candidate set exceeds the 1-row limit"):
            asyncio.run(
                MolecularTopologyQueryService.search_topologies(
                    **MolecularTopologySearchQuery(**unindexed_filter).model_dump(),
                    limit=1,
                    offset=0,
                )
            )

    # The indexed SMARTS predicate is part of the candidate relation. The database
    # contains more than one topology, but this exact isotope query has one match.
    indexed_smarts = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(smarts="[13CH3][Si]([CH3])([F])[Cl]").model_dump(),
            limit=1,
            offset=0,
        )
    )
    assert [item.id for item in indexed_smarts.items] == [rare.id]

    indexed_threshold = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                similarity_smiles=rare.canonical_isomeric_smiles,
                minimum_similarity=1.0,
            ).model_dump(),
            limit=1,
            offset=0,
        )
    )
    assert indexed_threshold.items
    assert indexed_threshold.items[0].id == rare.id

    # A pure nearest-neighbour query is bounded by PageLimit and the GiST KNN plan;
    # it must not be rejected merely because the table has more than one row.
    nearest = asyncio.run(
        MolecularTopologyQueryService.search_topologies(
            **MolecularTopologySearchQuery(
                similarity_smiles=rare.canonical_isomeric_smiles,
            ).model_dump(),
            limit=1,
            offset=0,
        )
    )
    assert [item.id for item in nearest.items] == [rare.id]


@pytest.mark.asyncio
async def test_topology_candidate_budget_uses_stable_rest_error(
    inserted_topologies: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(update={"structure_candidate_limit": 1})
    monkeypatch.setattr(query_services, "get_settings", lambda: settings)
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/topologies/search?limit=1",
                json={"minimum_molecular_weight": 0},
            )
    finally:
        await dispose_engine()

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "query_budget_exceeded"


@pytest.mark.asyncio
async def test_topology_search_rest_endpoint_uses_formula_join(
    inserted_topologies: dict[str, object],
) -> None:
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/topologies/search?limit=10",
                json={"formula_hill_formula": "C2H6O", "smarts": "CO"},
            )
    finally:
        await dispose_engine()

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == {"total": 2, "limit": 10, "offset": 0}
    assert {item["canonical_isomeric_smiles"] for item in payload["items"]} == {
        _explicit_h_smiles("CCO"),
        _explicit_h_smiles("COC"),
    }


@pytest.mark.asyncio
async def test_topology_similarity_rest_endpoint_returns_ranked_scores(
    inserted_topologies: dict[str, object],
) -> None:
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/topologies/search?limit=10",
                json={
                    "formula_hill_formula": "C2H6O",
                    "similarity_smiles": "CCO",
                    "similarity_metric": "dice",
                    "minimum_similarity": 0.99,
                },
            )
    finally:
        await dispose_engine()

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == {"total": 1, "limit": 10, "offset": 0}
    assert payload["items"][0]["canonical_isomeric_smiles"] == _explicit_h_smiles("CCO")
    assert payload["items"][0]["similarity_score"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_nexusx_generated_topology_search_rest_and_graphql(
    inserted_topologies: dict[str, object],
) -> None:
    rest_transport = ASGITransport(app=use_case_rest_app)
    graphql_transport = ASGITransport(app=paginated_graphql_app)
    try:
        async with AsyncClient(transport=rest_transport, base_url="http://test") as client:
            rest_response = await client.post(
                "/api/molecular_topology_query_service/search_topologies",
                json={
                    "formula_hill_formula": "C2H6O",
                    "similarity_smiles": "CCO",
                    "similarity_metric": "dice",
                    "minimum_similarity": 0.99,
                    "limit": 10,
                },
            )
        async with AsyncClient(transport=graphql_transport, base_url="http://test") as client:
            graphql_response = await client.post(
                "/graphql",
                json={
                    "query": """
                    {
                      MolecularTopologyQueryService {
                        search_topologies(
                          formula_hill_formula: "C2H6O"
                          similarity_smiles: "CCO"
                          similarity_metric: dice
                          minimum_similarity: 0.99
                          limit: 10
                        ) {
                          items { canonical_isomeric_smiles similarity_score }
                          page { total limit offset }
                        }
                      }
                    }
                    """,
                },
            )
    finally:
        await dispose_engine()

    assert rest_response.status_code == 200
    rest_payload = rest_response.json()
    assert rest_payload["page"] == {"total": 1, "limit": 10, "offset": 0}
    assert rest_payload["items"][0]["canonical_isomeric_smiles"] == _explicit_h_smiles("CCO")
    assert rest_payload["items"][0]["similarity_score"] == pytest.approx(1.0)

    assert graphql_response.status_code == 200
    graphql_payload = graphql_response.json()
    assert graphql_payload["errors"] == []
    graphql_result = graphql_payload["data"]["MolecularTopologyQueryService"]["search_topologies"]
    assert graphql_result["page"] == {"total": 1, "limit": 10, "offset": 0}
    assert graphql_result["items"] == [
        {
            "canonical_isomeric_smiles": _explicit_h_smiles("CCO"),
            "similarity_score": pytest.approx(1.0),
        }
    ]
