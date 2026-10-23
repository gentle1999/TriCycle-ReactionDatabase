from types import SimpleNamespace
from uuid import uuid4

from rdkit import Chem

from tricycle_reaction_db.application.services.reaction_cpu_time_export import (
    FileCpuTimes,
    ReactionCpuTimeExport,
    substrate_pair_key,
)


def test_cpu_hours_deduplicate_shared_files_revisions_and_stage_union():
    a, b, c = uuid4(), uuid4(), uuid4()
    costs = FileCpuTimes()
    costs.add("reactants", a, 1, 3600)
    costs.add("reactants", a, 2, 7200)
    costs.add("reactants", a, 2, 7200)
    costs.add("transition_state", b, 1, 10800)
    costs.add("products", a, 2, 7200)
    costs.add("products", c, 1, 14400)
    assert costs.hours() == {
        "reactants_cpu_time_hours": 2,
        "transition_state_cpu_time_hours": 3,
        "products_cpu_time_hours": 6,
        "total_cpu_time_hours": 9,
    }
    costs.add("products", c, 2, None)
    assert costs.hours()["products_cpu_time_hours"] is None
    assert costs.hours()["total_cpu_time_hours"] is None
    assert costs.hours()["reactants_cpu_time_hours"] == 2
    assert all(value is None for value in FileCpuTimes().hours().values())


def test_substrate_multiset_preserves_multiplicity_and_project():
    project = uuid4()
    key = substrate_pair_key(project, [("A", 2), ("B", 1)])
    assert key == substrate_pair_key(project, [("B", 1), ("A", 1), ("A", 1)])
    assert key != substrate_pair_key(project, [("B", 1), ("A", 1)])
    assert key != substrate_pair_key(uuid4(), [("A", 2), ("B", 1)])


def test_canonical_substrates_ignore_maps_order_and_legacy_ids_but_preserve_stereo(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("subgraph matching is forbidden")

    for method in ("GetSubstructMatch", "GetSubstructMatches", "HasSubstructMatch"):
        monkeypatch.setattr(Chem.Mol, method, forbidden)
    first, second, opposite, missing = [uuid4() for _ in range(4)]
    molecule = Chem.MolFromSmiles("[F:1]/[CH:2]=[CH:3]/[Cl:4]")
    equivalent = Chem.RenumberAtoms(molecule, [3, 2, 1, 0])
    for atom in equivalent.GetAtoms():
        atom.SetAtomMapNum(100 + atom.GetIdx())
    water = Chem.MolFromSmiles("O")
    rows = [
        (first, 1, uuid4(), molecule),
        (first, 1, uuid4(), water),
        (second, 1, uuid4(), water),
        (second, 1, uuid4(), equivalent),
        (opposite, 1, uuid4(), Chem.MolFromSmiles("F/C=C\\Cl")),
        (opposite, 1, uuid4(), water),
        (missing, 1, uuid4(), water),
        (missing, 1, None, None),
    ]
    session = SimpleNamespace(exec=lambda statement: SimpleNamespace(all=lambda: rows))
    costs = ReactionCpuTimeExport(session, uuid4(), True)
    assert costs.group_by_mapping[first] == costs.group_by_mapping[second]
    assert costs.group_by_mapping[first] != costs.group_by_mapping[opposite]
    assert missing not in costs.group_by_mapping
