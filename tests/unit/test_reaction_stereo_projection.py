from rdkit import Chem

from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    inversion_labile_atom_indices,
    inversion_labile_atom_map_numbers,
    stereo_features_to_clear_for_atom_maps,
)
from tricycle_reaction_db.application.services.topology_abstraction import (
    StereoFeature,
    assigned_stereo_features,
    clear_stereo_features,
)
from tricycle_reaction_db.core.chemistry_config import InversionLabileRule
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
    nitrogen_rule = InversionLabileRule(
        rule_id="test-neutral-trivalent-nitrogen",
        atom_smarts="[N;X3;v3;+0]",
    )
    rules = (nitrogen_rule,)
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
        atom_map
        for _rule_id, atom_map in inversion_labile_atom_map_numbers(
            product,
            product_maps,
            rules=rules,
        )
    } == {9, 10, 13, 14}
    assert {
        atom_map
        for _rule_id, atom_map in inversion_labile_atom_map_numbers(
            diene,
            diene_maps,
            rules=rules,
        )
    } == {10, 14}

    diene_features = assigned_stereo_features(diene.mol)
    assert len(diene_features) == 2
    endpoint_data = ((product, product_maps), (diene, diene_maps))
    for ordered_endpoints in (endpoint_data, tuple(reversed(endpoint_data))):
        labile_maps = {
            atom_map
            for endpoint, endpoint_maps in ordered_endpoints
            for _rule_id, atom_map in inversion_labile_atom_map_numbers(
                endpoint,
                endpoint_maps,
                rules=rules,
            )
        }
        assert labile_maps == {9, 10, 13, 14}
        selected = stereo_features_to_clear_for_atom_maps(diene, diene_maps, labile_maps)
        assert {feature.key for feature in selected} == {feature.key for feature in diene_features}
        assert all(feature.kind == "bond" for feature in selected)


def test_configured_inversion_labile_rules_match_requested_atom_types() -> None:
    cases = (
        (
            "[O+:1]([CH3:2])([CH3:3])[CH3:4]",
            "trivalent-chalcogen-cation",
        ),
        (
            "[N:1]([CH3:2])([CH3:3])[CH3:4]",
            "neutral-trivalent-nitrogen-phosphorus-arsenic",
        ),
        (
            "[P:1]([CH3:2])([CH3:3])[CH3:4]",
            "neutral-trivalent-nitrogen-phosphorus-arsenic",
        ),
        (
            "[As:1]([CH3:2])([CH3:3])[CH3:4]",
            "neutral-trivalent-nitrogen-phosphorus-arsenic",
        ),
        (
            "[Cl+2:1]([CH3:2])([CH3:3])[CH3:4]",
            "trivalent-halogen-dication",
        ),
    )

    for smiles, rule_id in cases:
        molecule = Chem.MolFromSmiles(smiles)
        assert molecule is not None
        assert inversion_labile_atom_indices(molecule) == ((rule_id, 0),)

    boron = Chem.MolFromSmiles("[B:1]([CH3:2])([CH3:3])[CH3:4]")
    assert boron is not None
    assert inversion_labile_atom_indices(boron) == ()


def test_aromatic_trivalent_sulfur_matches_chalcogen_rule() -> None:
    molecule = Chem.MolFromSmiles("[s+:1]1(C)cccc1")
    assert molecule is not None

    sulfur = molecule.GetAtomWithIdx(0)
    assert sulfur.GetAtomicNum() == 16
    assert sulfur.GetIsAromatic()
    assert sulfur.GetDegree() == 3
    assert sulfur.GetTotalValence() == 3
    assert sulfur.GetFormalCharge() == 1
    assert inversion_labile_atom_indices(molecule) == (("trivalent-chalcogen-cation", 0),)


def test_sulfur_chirality_is_cleared_for_charge_separated_oxygen() -> None:
    topology, topology_maps = _normalized_topology_with_source_maps(
        "[CH3:1][S@+:2]([O-:3])[CH2:4][Cl:5]"
    )

    sulfur_index = next(
        atom.GetIdx() for atom in topology.mol.GetAtoms() if atom.GetAtomicNum() == 16
    )
    assert inversion_labile_atom_indices(topology.mol) == (
        ("trivalent-chalcogen-cation", sulfur_index),
    )
    assert StereoFeature("atom", sulfur_index) in assigned_stereo_features(topology.mol)

    labile_maps = {
        atom_map
        for _rule_id, atom_map in inversion_labile_atom_map_numbers(
            topology,
            topology_maps,
        )
    }
    selected = stereo_features_to_clear_for_atom_maps(
        topology,
        topology_maps,
        labile_maps,
    )

    assert selected == (StereoFeature("atom", sulfur_index),)
    assert assigned_stereo_features(clear_stereo_features(topology.mol, selected)) == ()
