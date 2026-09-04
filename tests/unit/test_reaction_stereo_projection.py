from rdkit import Chem

from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    inversion_labile_atom_map_numbers,
    stereo_features_to_clear_for_atom_maps,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    assigned_stereo_features,
)
from tricycle_reaction_db.ingestion.normalization import normalize_topology_with_mapping


def _normalized_topology_with_source_maps(
    smiles: str,
) -> tuple[object, list[int]]:
    molecule = Chem.MolFromSmiles(smiles)
    assert molecule is not None
    source_maps = [atom.GetAtomMapNum() for atom in molecule.GetAtoms()]
    record, source_to_topology = normalize_topology_with_mapping(
        molecule,
        add_hydrogens=False,
        reconstruction_method="tests/reaction-stereo-projection",
        reconstruction_version="1",
    )
    topology_maps = [0] * record.topology.atom_count
    for source_index, topology_index in enumerate(source_to_topology):
        topology_maps[topology_index] = source_maps[source_index]
    return record.topology, topology_maps


def test_inversion_labile_atom_selects_adjacent_ez_features() -> None:
    diene, diene_maps = _normalized_topology_with_source_maps(
        "[C:7]([C:8](=[N:9]\\[N:10]([H:18])[H:19])/[C:11]"
        "([C:12]([H:20])([H:21])[H:22])=[N:13]/[N:14]([H:23])[H:24])"
        "([H:15])([H:16])[H:17]"
    )
    product, product_maps = _normalized_topology_with_source_maps(
        "[C:1]1([H:3])([H:4])[C:2]([H:5])([H:6])[N:9]"
        "([N:10]([H:18])[H:19])[C:8]([C:7]([H:15])([H:16])[H:17])="
        "[C:11]([C:12]([H:20])([H:21])[H:22])[N:13]1[N:14]([H:23])[H:24]"
    )

    assert {
        atom_map for _rule_id, atom_map in inversion_labile_atom_map_numbers(product, product_maps)
    } == {9, 10, 13, 14}
    assert {
        atom_map for _rule_id, atom_map in inversion_labile_atom_map_numbers(diene, diene_maps)
    } == {10, 14}

    diene_features = assigned_stereo_features(diene.mol)
    assert len(diene_features) == 2
    endpoint_data = ((product, product_maps), (diene, diene_maps))
    for ordered_endpoints in (endpoint_data, tuple(reversed(endpoint_data))):
        labile_maps = {
            atom_map
            for endpoint, endpoint_maps in ordered_endpoints
            for _rule_id, atom_map in inversion_labile_atom_map_numbers(endpoint, endpoint_maps)
        }
        assert labile_maps == {9, 10, 13, 14}
        selected = stereo_features_to_clear_for_atom_maps(diene, diene_maps, labile_maps)
        assert {feature.key for feature in selected} == {feature.key for feature in diene_features}
        assert all(feature.kind == "bond" for feature in selected)
