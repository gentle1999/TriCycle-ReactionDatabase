import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import reaction_geometry_reconciliation as module
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side


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
