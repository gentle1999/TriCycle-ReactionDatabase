"""Electronic annotations which plain SMILES and the RDKit cartridge omit."""

from molgr.utils.converter import METAL_UNPAIRED_ELECTRONS_PROP
from rdkit import Chem

RADICAL_ELECTRONS_PROP = "_tricycle_source_radical_electrons"


def annotate_electronic_state(molecule: Chem.Mol) -> None:
    """Retain explicit source evidence, including a known zero on open valences."""
    for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
        if not atom.HasProp(METAL_UNPAIRED_ELECTRONS_PROP):
            atom.SetIntProp(RADICAL_ELECTRONS_PROP, atom.GetNumRadicalElectrons())


def mol_atom_properties(molecule: Chem.Mol) -> dict[str, int]:
    """Capture metal spin and ordinary radical evidence in persisted atom order.

    Numeric keys retain the v1 metal format; ``radical:N`` stores ordinary
    atom N's assignment, including known zero-electron open-valence states.
    """

    properties = {
        str(atom.GetIdx()): atom.GetIntProp(METAL_UNPAIRED_ELECTRONS_PROP)
        for atom in molecule.GetAtoms()  # type: ignore[no-untyped-call]
        if atom.HasProp(METAL_UNPAIRED_ELECTRONS_PROP)
    }
    properties.update(
        {
            f"radical:{atom.GetIdx()}": atom.GetIntProp(RADICAL_ELECTRONS_PROP)
            for atom in molecule.GetAtoms()  # type: ignore[no-untyped-call]
            if atom.HasProp(RADICAL_ELECTRONS_PROP)
        }
    )
    return properties


def restore_mol_atom_properties(molecule: Chem.Mol, properties: dict[str, int]) -> Chem.Mol:
    """Restore sidecar evidence without deriving spin from bracket valence."""

    for index, count in properties.items():
        if index.startswith("radical:"):
            atom = molecule.GetAtomWithIdx(int(index.split(":", 1)[1]))
            atom.SetNumRadicalElectrons(count)
            atom.SetIntProp(RADICAL_ELECTRONS_PROP, count)
            continue
        atom = molecule.GetAtomWithIdx(int(index))
        atom.SetIntProp(METAL_UNPAIRED_ELECTRONS_PROP, count)
        atom.SetNumRadicalElectrons(0)
    return molecule


def metal_spin_identity(molecule: Chem.Mol) -> dict[str, str]:
    """Distinguish electronic states without relying on source atom order.

    Maps are temporary vertex labels for canonicalization, not reaction maps.
    Odd labels encode MolGR metal spin, even labels ordinary radical counts.
    Ordinary annotation presence is deliberately irrelevant to identity.
    """

    graph = Chem.Mol(molecule)
    Chem.RemoveStereochemistry(graph)
    for atom in graph.GetAtoms():  # type: ignore[no-untyped-call]
        # Disjoint labels preserve metal evidence and survive atom reordering.
        label = 2 * atom.GetNumRadicalElectrons() + 2
        if atom.HasProp(METAL_UNPAIRED_ELECTRONS_PROP):
            label = 2 * atom.GetIntProp(METAL_UNPAIRED_ELECTRONS_PROP) + 1
        atom.SetAtomMapNum(label)
    return {
        "source_electronic_identity_v2": Chem.MolToSmiles(
            graph, canonical=True, isomericSmiles=True, allHsExplicit=True
        )
    }
