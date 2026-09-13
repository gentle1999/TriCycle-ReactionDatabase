from rdkit import Chem

from tricycle_reaction_db.application.services.topology_compatibility import (
    source_geometry_compatible_topology,
)


def test_endpoint_source_compatibility_accepts_one_endpoint_only_bond() -> None:
    endpoint = Chem.MolFromSmiles(
        "[O:1]=[C:2]1[C@:3]2([O-:4])[C@@:5]3([H:10])"
        "[C@:6]2([H:11])[C+:7]([H:12])[C@:8]1([H:13])"
        "[C:9]3([H:14])[H:15]"
    )
    source_geometry = Chem.MolFromSmiles(
        "[O:1]=[C:2]1[C:3](=[O:4])[C@@:5]2([H:10])"
        "[C:6]([H:11])=[C:7]([H:12])[C@:8]1([H:13])"
        "[C:9]2([H:14])[H:15]"
    )

    assert endpoint is not None
    assert source_geometry is not None
    assert source_geometry_compatible_topology(endpoint, source_geometry)
    assert not source_geometry_compatible_topology(source_geometry, endpoint)
