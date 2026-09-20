from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from rdkit import Chem
from sqlalchemy.dialects import postgresql

from tricycle_reaction_db.application.services import topology_abstraction
from tricycle_reaction_db.application.services.topology_abstraction import (
    STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
    STEREO_ABSTRACTION_POLICY_VERSION,
    StereoFeature,
    assigned_stereo_features,
    clear_stereo_features,
    find_stereo_abstraction_match,
    stereo_abstraction_projection,
    topology_abstraction_mapping_witness,
)
from tricycle_reaction_db.ingestion.normalization import (
    normalize_topology,
    normalize_topology_with_mapping,
)


def _two_center_molecule() -> Chem.Mol:
    molecule = Chem.MolFromSmiles("F[C@H](Cl)[C@H](Br)I")
    assert molecule is not None
    return molecule


def _fail_if_graph_match_runs(*_: object, **__: object) -> None:
    raise AssertionError("known normalization mapping must not trigger full graph matching")


def test_stereo_projection_clears_only_requested_features() -> None:
    molecule = _two_center_molecule()
    features = assigned_stereo_features(molecule)

    assert features == (StereoFeature("atom", 1), StereoFeature("atom", 3))
    projection = stereo_abstraction_projection(molecule, (features[0],))

    assert projection.cleared_features == (features[0],)
    assert assigned_stereo_features(projection.molecule) == (features[1],)


def test_two_center_specialization_is_a_dag_diamond() -> None:
    molecule = _two_center_molecule()
    features = assigned_stereo_features(molecule)
    one_center_a = clear_stereo_features(molecule, (features[0],))
    one_center_b = clear_stereo_features(molecule, (features[1],))
    zero_center = clear_stereo_features(molecule, features)

    full_to_a = find_stereo_abstraction_match(molecule, one_center_a)
    full_to_b = find_stereo_abstraction_match(molecule, one_center_b)
    a_to_zero = find_stereo_abstraction_match(one_center_a, zero_center)
    b_to_zero = find_stereo_abstraction_match(one_center_b, zero_center)

    assert full_to_a is not None
    assert full_to_b is not None
    assert a_to_zero is not None
    assert b_to_zero is not None
    assert full_to_a.abstracted_feature_count == 1
    assert full_to_b.abstracted_feature_count == 1
    assert a_to_zero.abstracted_feature_count == 1
    assert b_to_zero.abstracted_feature_count == 1
    assert find_stereo_abstraction_match(one_center_a, one_center_b) is None


def test_clear_stereo_features_removes_ez_and_directional_bonds() -> None:
    molecule = Chem.MolFromSmiles("C/C=C/C")
    assert molecule is not None
    features = assigned_stereo_features(molecule)

    assert features == (StereoFeature("bond", 1),)
    projected = clear_stereo_features(molecule, features)
    double_bond = projected.GetBondWithIdx(1)

    assert double_bond.GetStereo() == Chem.BondStereo.STEREONONE
    assert all(bond.GetBondDir() == Chem.BondDir.NONE for bond in projected.GetBonds())
    match = find_stereo_abstraction_match(molecule, projected)
    assert match is not None
    assert match.abstracted_bond_indices == (1,)


def test_stereo_projection_does_not_reinfer_cleared_ez_from_stale_directions() -> None:
    molecule = Chem.MolFromSmiles("CC/C(C)=C(C)/CC")
    assert molecule is not None
    molecule = Chem.AddHs(molecule)
    source = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/source-topology",
        reconstruction_version="1",
    )
    feature = assigned_stereo_features(molecule)[0]
    # Simulate stale RDKit writer directions on the alternate substituents.
    # These do not agree with the control atoms stored on the E/Z bond and can
    # otherwise recreate a (potentially different) E/Z assignment on parse.
    molecule.GetBondWithIdx(2).SetBondDir(Chem.BondDir.ENDUPRIGHT)
    molecule.GetBondWithIdx(4).SetBondDir(Chem.BondDir.ENDUPRIGHT)

    projection = stereo_abstraction_projection(molecule, (feature,))
    assert all(bond.GetBondDir() == Chem.BondDir.NONE for bond in projection.molecule.GetBonds())
    normalized, _source_to_topology = normalize_topology_with_mapping(
        projection.molecule,
        add_hydrogens=False,
        reconstruction_method="topology/stereo-abstraction",
        reconstruction_version="1",
        reconstruction_metadata={
            "topology_source_trusted": True,
            "stereo_abstraction": True,
            "is_stereo_abstraction_upstream": True,
        },
    )

    assert assigned_stereo_features(normalized.topology.mol) == ()
    assert normalized.topology.graph_hash != source.topology.graph_hash


def test_only_explicit_abstraction_metadata_marks_an_upstream() -> None:
    molecule = _two_center_molecule()
    ordinary = normalize_topology(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/ordinary-topology",
        reconstruction_version="1",
    )
    marked = normalize_topology(
        clear_stereo_features(molecule, (assigned_stereo_features(molecule)[0],)),
        add_hydrogens=False,
        reconstruction_method="topology/stereo-abstraction",
        reconstruction_version="1",
        reconstruction_metadata={"is_stereo_abstraction_upstream": True},
    )

    assert ordinary.topology.is_stereo_abstraction_upstream is False
    assert marked.topology.is_stereo_abstraction_upstream is True


@pytest.mark.parametrize(
    ("smiles", "feature_index"),
    [
        ("F[C@H](Cl)[C@H](Br)I", 0),
        ("C/C=C/C", 0),
        ("C" * 35 + "/C=C/" + "C" * 35, 0),
    ],
)
def test_known_projection_mapping_verifies_stereo_delta_without_graph_match(
    monkeypatch: pytest.MonkeyPatch,
    smiles: str,
    feature_index: int,
) -> None:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    feature = assigned_stereo_features(molecule)[feature_index]
    projection = stereo_abstraction_projection(molecule, (feature,))
    normalized, source_to_general = normalize_topology_with_mapping(
        projection.molecule,
        add_hydrogens=False,
        reconstruction_method="topology/stereo-abstraction",
        reconstruction_version="1",
        reconstruction_metadata={"topology_source_trusted": True},
    )
    general_to_specific = [0] * len(source_to_general)
    for specific_index, general_index in enumerate(source_to_general):
        general_to_specific[general_index] = specific_index

    expected: object | None = None
    if molecule.GetNumAtoms() < 20:
        expected = find_stereo_abstraction_match(molecule, normalized.topology.mol)
        assert expected is not None

    monkeypatch.setattr(topology_abstraction, "find_topology_matches", _fail_if_graph_match_runs)
    match = topology_abstraction._stereo_abstraction_match_for_known_atom_mapping(
        molecule,
        normalized.topology.mol,
        tuple(general_to_specific),
    )

    if expected is not None:
        assert match == expected
    else:
        assert match is not None
        assert match.abstracted_bond_indices


def test_projection_persistence_reuses_normalization_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specific = SimpleNamespace(
        id=UUID(int=301),
        project_id=PROJECT_ID,
        mol=_two_center_molecule(),
    )
    context = SimpleNamespace(
        project_id=PROJECT_ID,
        topologies_by_identity={},
        topology_upstreams_by_key={},
    )
    persisted_calls: list[dict[str, object]] = []
    abstraction_calls: list[dict[str, object]] = []
    edge = object()

    def persist_topology(_session, record, **kwargs):
        persisted_calls.append(kwargs)
        return SimpleNamespace(
            topology=SimpleNamespace(
                id=UUID(int=302),
                project_id=PROJECT_ID,
                mol=record.topology.mol,
                is_stereo_abstraction_upstream=True,
            )
        )

    def persist_abstraction(_session, _specific, _general, **kwargs):
        abstraction_calls.append(kwargs)
        return edge

    monkeypatch.setattr(topology_abstraction, "persist_molecular_topology", persist_topology)
    monkeypatch.setattr(topology_abstraction, "persist_stereo_abstraction", persist_abstraction)
    monkeypatch.setattr(
        topology_abstraction, "backfill_stereo_abstraction_downstreams", lambda *_, **__: ()
    )
    monkeypatch.setattr(
        topology_abstraction, "find_stereo_abstraction_match", _fail_if_graph_match_runs
    )
    monkeypatch.setattr(topology_abstraction, "find_topology_matches", _fail_if_graph_match_runs)

    specific_features = assigned_stereo_features(specific.mol)
    projected, persisted_edge = topology_abstraction.persist_stereo_abstraction_projection(
        cast(Any, object()),
        cast(Any, specific),
        (specific_features[0],),
        context=context,
    )

    assert projected.id == UUID(int=302)
    assert persisted_edge is edge
    assert persisted_calls == [{"context": context, "register_upstream": False}]
    assert abstraction_calls[0]["known_match"] is not None


class _EdgeQueryResult:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows

    def all(self) -> tuple[object, ...]:
        return self.rows


class _EdgeQuerySession:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self.rows = rows
        self.statement: object | None = None
        self.exec_calls = 0
        self.new: list[object] = []
        self.info: dict[str, object] = {}

    def exec(self, statement: object) -> _EdgeQueryResult:
        self.statement = statement
        self.exec_calls += 1
        return _EdgeQueryResult(self.rows)

    def get_transaction(self) -> None:
        return None

    def get_nested_transaction(self) -> None:
        return None


def _mapping_witness(
    session: object,
    specific: object,
    general: object,
    **kwargs: Any,
) -> tuple[int, ...] | None:
    return topology_abstraction_mapping_witness(
        cast(Any, session),
        cast(Any, specific),
        cast(Any, general),
        **kwargs,
    )


def _abstraction_edge(
    specific_id: UUID,
    general_id: UUID,
    mapping: list[int],
    *,
    projection_provenance: bool,
) -> SimpleNamespace:
    caller_metadata = (
        {"atom_mapping_provenance": "normalized_projection_atom_order_v1"}
        if projection_provenance
        else {}
    )
    return SimpleNamespace(
        id=UUID(int=len(mapping) + specific_id.int % 1000),
        project_id=PROJECT_ID,
        abstraction_policy_version=STEREO_ABSTRACTION_POLICY_VERSION,
        specific_topology_id=specific_id,
        general_topology_id=general_id,
        abstraction_metadata={
            "match_schema_version": STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
            "general_to_specific_atom_indices": mapping,
            "caller_metadata": caller_metadata,
        },
    )


PROJECT_ID = UUID("00000000-0000-7000-8000-000000000001")


def test_dag_mapping_witness_composes_edges_and_requires_trusted_provenance() -> None:
    general_id, middle_id, specific_id = (UUID(int=value) for value in (101, 102, 103))
    general = SimpleNamespace(id=general_id, project_id=PROJECT_ID, atom_count=4)
    specific = SimpleNamespace(id=specific_id, project_id=PROJECT_ID, atom_count=4)
    session = _EdgeQuerySession(
        (
            _abstraction_edge(
                middle_id,
                general_id,
                [1, 0, 2, 3],
                projection_provenance=True,
            ),
            _abstraction_edge(
                specific_id,
                middle_id,
                [0, 2, 1, 3],
                projection_provenance=True,
            ),
        )
    )

    assert _mapping_witness(session, specific, general) == (2, 0, 1, 3)
    assert session.statement is not None
    statement_sql = str(cast(Any, session.statement).compile(dialect=postgresql.dialect()))
    assert "WITH RECURSIVE STEREO_ABSTRACTION_MAPPING_NODES" in statement_sql.upper()
    assert _mapping_witness(
        session,
        specific,
        general,
        require_projection_provenance=True,
        require_unique=True,
    ) == (2, 0, 1, 3)
    assert session.exec_calls == 1


def test_dag_atom_map_witness_rejects_untrusted_or_ambiguous_paths() -> None:
    general_id, specific_id, other_specific_id = (UUID(int=value) for value in (201, 202, 203))
    general = SimpleNamespace(id=general_id, project_id=PROJECT_ID, atom_count=3)
    specific = SimpleNamespace(id=specific_id, project_id=PROJECT_ID, atom_count=3)
    untrusted_session = _EdgeQuerySession(
        (
            _abstraction_edge(
                specific_id,
                general_id,
                [0, 1, 2],
                projection_provenance=False,
            ),
        )
    )
    assert _mapping_witness(untrusted_session, specific, general) == (
        0,
        1,
        2,
    )
    assert (
        _mapping_witness(
            untrusted_session,
            specific,
            general,
            require_projection_provenance=True,
            require_unique=True,
        )
        is None
    )

    ambiguous_session = _EdgeQuerySession(
        (
            _abstraction_edge(
                specific_id,
                general_id,
                [0, 1, 2],
                projection_provenance=True,
            ),
            _abstraction_edge(
                other_specific_id,
                general_id,
                [1, 0, 2],
                projection_provenance=True,
            ),
            _abstraction_edge(
                specific_id,
                other_specific_id,
                [0, 1, 2],
                projection_provenance=True,
            ),
        )
    )
    assert (
        _mapping_witness(
            ambiguous_session,
            specific,
            general,
            require_projection_provenance=True,
            require_unique=True,
        )
        is None
    )
