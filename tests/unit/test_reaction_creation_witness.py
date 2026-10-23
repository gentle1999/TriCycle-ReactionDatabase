from types import SimpleNamespace
from uuid import uuid4

import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reaction_geometry_reconciliation as module
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.domain.enums import MappedReactionNodeRole


def components(smiles):
    molecule = Chem.AddHs(Chem.MolFromSmiles(smiles))
    maps = list(range(1, molecule.GetNumAtoms() + 1))
    return {side: [(molecule, maps)] for side in (Side.REACTANT, Side.PRODUCT)}


def test_creation_witness_composes_geometry_order_without_recanonicalization(monkeypatch):
    source = components("F[C@H](Cl)Br")
    count = source[Side.REACTANT][0][0].GetNumAtoms()
    witness = {i: count + 1 - i for i in range(1, count + 1)}
    target = {side: [(mol, [witness[i] for i in maps])] for side, ((mol, maps),) in source.items()}

    def unexpected(*args, **kwargs):
        raise AssertionError("must reuse the selected form")

    monkeypatch.setattr(module, "canonical_reaction_identity", unexpected)
    order = list(reversed(range(count)))
    result = module._geometry_maps_from_creation_witness(source, target, order, witness)
    assert [result[i] for i in order] == list(witness.values())


@pytest.mark.parametrize("witness", [{1: 1}, dict.fromkeys(range(1, 6), 1)])
def test_creation_witness_rejects_incomplete_or_nonbijective_map(witness):
    source = components("F[C@H](Cl)Br")
    with pytest.raises(ValueError, match="complete atom permutation"):
        module._geometry_maps_from_creation_witness(source, source, list(range(5)), witness)


def test_creation_witness_cannot_attach_a_different_stereoisomer():
    source = components("F[C@H](Cl)Br")
    target = components("F[C@@H](Cl)Br")
    with pytest.raises(ValueError, match="labelled TS endpoints"):
        module._geometry_maps_from_creation_witness(
            source, target, list(range(5)), {i: i for i in range(1, 6)}
        )


@pytest.mark.parametrize("legacy", [True, False])
def test_source_rebuild_migrates_only_legacy_ts_mapping_without_a_batch_cache(legacy):
    molecule = Chem.AddHs(Chem.MolFromSmiles("C"))
    geometry = SimpleNamespace(mol=molecule, atom_count=5)
    reaction = SimpleNamespace(
        mapped_reaction_smiles="[C:1]([H:2])([H:3])([H:4])[H:5]>>[C:1]([H:2])([H:3])([H:4])[H:5]"
    )
    association = SimpleNamespace(
        id=uuid4(),
        geometry=geometry,
        mapped_reaction_node=SimpleNamespace(
            role=MappedReactionNodeRole.TRANSITION_STATE, mapped_reaction=reaction
        ),
    )
    existing = SimpleNamespace(
        geometry_atom_map_numbers=[1, 2, 3, 4, 5],
        mapped_smiles="legacy",
        mapping_method=module.REACTION_TS_GEOMETRY_LINK_METHOD,
        mapping_version=(
            "reaction-ts-geometry-link-v4"
            if legacy
            else module.REACTION_TS_GEOMETRY_LINK_POLICY_VERSION
        ),
    )
    added = []
    session = SimpleNamespace(add=added.append)
    # The caller has already verified this vector against both source endpoints.
    arguments = {
        "node_geometry": association,
        "coordinate_atom_maps": [1, 3, 2, 4, 5],
        "mapped_smiles": "unused for TS",
        "existing_mapping": existing,
    }
    if not legacy:
        with pytest.raises(ValueError, match="incompatible reaction mapping"):
            module._ensure_mapping(session, **arguments)
        assert existing.geometry_atom_map_numbers == [1, 2, 3, 4, 5]
        return
    assert module._ensure_mapping(session, **arguments) is existing
    assert existing.geometry_atom_map_numbers == [1, 3, 2, 4, 5]
    assert existing.mapping_version == module.REACTION_TS_GEOMETRY_LINK_POLICY_VERSION
    assert added == [existing]


def test_ring_stereo_writer_cache_does_not_change_labelled_graph_after_renumbering():
    import random

    from tricycle_reaction_db.ingestion.normalization import serialize_molecule_smiles

    params = Chem.SmilesParserParams()
    params.removeHs = False
    molecule = Chem.MolFromSmiles(
        "[C:1]1([H:11])([H:12])[C:2]([H:13])([H:14])[C:3]([H:15])([H:16])[C@:4]23[C@:5]([C:6]1([H:17])[H:18])([C:7]([H:19])([H:20])[C:8]([H:21])([H:22])[C:9]([H:23])([H:24])[C:10]2([H:25])[H:26])[C:28]([F:27])([F:29])[C:30]([H:35])=[C:31]([H:36])[C:32]3([F:33])[F:34]",
        params,
    )
    maps = [atom.GetAtomMapNum() for atom in molecule.GetAtoms()]
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    # Symmetric ring stereo assignment installs index-bearing writer caches.
    assert any(atom.HasProp("_ringStereoAtoms") for atom in molecule.GetAtoms())
    for atom, number in zip(molecule.GetAtoms(), maps, strict=True):
        atom.SetAtomMapNum(number)
    expected = serialize_molecule_smiles(molecule, preserve_atom_maps=True)
    generator = random.Random(19)
    for _ in range(20):
        order = list(range(molecule.GetNumAtoms()))
        generator.shuffle(order)
        changed = Chem.RenumberAtoms(molecule, order)
        assert serialize_molecule_smiles(changed, preserve_atom_maps=True) == expected
