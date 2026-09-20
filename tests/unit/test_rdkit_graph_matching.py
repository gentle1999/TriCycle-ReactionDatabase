import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import rdkit_graph_matching
from tricycle_reaction_db.application.services.rdkit_graph_matching import (
    MolecularGraphMatchTimeoutError,
    get_substruct_matches,
)


def test_large_full_graph_match_uses_isolated_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    target = Chem.MolFromSmiles("C" * 49)
    query = Chem.MolFromSmiles("C" * 49)
    assert target is not None
    assert query is not None
    called: dict[str, object] = {}

    def fake_isolated(*args: object, **kwargs: object) -> tuple[tuple[int, ...], ...]:
        called["args"] = args
        called.update(kwargs)
        return ((0,),)

    monkeypatch.setattr(rdkit_graph_matching, "_run_isolated_substructure_matches", fake_isolated)

    assert get_substruct_matches(
        target,
        query,
        hard_timeout_for_large_molecules=True,
    ) == ((0,),)
    assert called["timeout_seconds"] == 5.0
    assert called["target_atom_count"] == 49
    assert called["query_atom_count"] == 49


def test_large_full_graph_match_can_be_hard_stopped() -> None:
    target = Chem.MolFromSmiles("C" * 49)
    query = Chem.MolFromSmiles("C" * 49)
    assert target is not None
    assert query is not None

    with pytest.raises(MolecularGraphMatchTimeoutError):
        get_substruct_matches(
            target,
            query,
            hard_timeout_for_large_molecules=True,
            timeout_seconds=0.000001,
        )


def test_large_graph_timeout_is_a_conservative_compatibility_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = Chem.MolFromSmiles("C" * 49)
    source = Chem.MolFromSmiles("C" * 49)
    assert endpoint is not None
    assert source is not None

    def timeout(*_: object, **__: object) -> tuple[tuple[int, ...], ...]:
        raise MolecularGraphMatchTimeoutError(
            target_atom_count=49,
            query_atom_count=49,
            timeout_seconds=0.01,
        )

    monkeypatch.setattr(
        "tricycle_reaction_db.application.services.topology_compatibility.get_substruct_matches",
        timeout,
    )
    from tricycle_reaction_db.application.services.topology_compatibility import (
        source_geometry_compatible_topology,
    )

    assert not source_geometry_compatible_topology(endpoint, source)


def test_match_result_count_is_explicitly_bounded() -> None:
    target = Chem.MolFromSmiles("c1ccccc1")
    query = Chem.MolFromSmiles("c1ccccc1")
    assert target is not None
    assert query is not None

    matches = get_substruct_matches(target, query, max_matches=1)

    assert len(matches) == 1
