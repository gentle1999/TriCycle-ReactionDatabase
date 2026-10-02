"""Reactant traversal defines reaction maps; source labels are provenance only."""

from dataclasses import dataclass

from rdkit import Chem, rdBase

from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    ReactionComponents,
    _canonical_smiles,
    _combine_components,
    _map_free,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.ingestion.normalization import serialize_molecule_smiles

REACTION_INDEX_POLICY = "canonical-reactant-joint-correspondence-v1"


@dataclass(frozen=True)
class CanonicalReactionIdentity:
    smiles: str
    source_map_to_canonical: dict[int, int]

    def normalization_metadata(self) -> dict[str, str]:
        """Record the selected output, not a claim of RDKit fixed-point convergence."""
        return {
            "policy": REACTION_INDEX_POLICY,
            "rdkit_version": rdBase.rdkitVersion,
            "selected_mapped_reaction_smiles": self.smiles,
        }


def serialize_reaction_components(components: ReactionComponents) -> str:
    """Serialize both complete labelled graphs without changing any map number."""
    rendered = []
    for side in (Side.REACTANT, Side.PRODUCT):
        combined = _combine_components(components.get(side, ()))
        if combined is None:
            raise ValueError("reaction side requires unique positive atom maps")
        molecule, maps = combined
        for atom, number in zip(molecule.GetAtoms(), maps, strict=True):  # type: ignore[no-untyped-call]
            atom.SetAtomMapNum(number)
        rendered.append(
            serialize_molecule_smiles(molecule, preserve_atom_maps=True, all_hs_explicit=True)
        )
    return ">>".join(rendered)


def _joint_order(reactant: Chem.Mol, product: Chem.Mol) -> list[int]:
    """Resolve precursor symmetries with the complete atom-correspondence graph.

    Two coloured layers retain both endpoint graphs and correspondence edges.
    Ranks only select among otherwise equivalent precursor traversals; they
    never become reaction map numbers themselves.
    """
    graph = Chem.RWMol()
    colours: list[tuple[object, ...]] = []
    for side, molecule in enumerate((reactant, product)):
        molecule.UpdatePropertyCache(strict=False)
        Chem.FastFindRings(molecule)
        endpoint_classes = list(
            Chem.CanonicalRankAtoms(
                molecule, breakTies=False, includeChirality=True, includeIsotopes=True
            )
        )
        for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
            graph.AddAtom(Chem.Atom(0))
            colours.append(
                (
                    0,
                    side,
                    atom.GetAtomicNum(),
                    atom.GetIsotope(),
                    atom.GetFormalCharge(),
                    atom.GetNumRadicalElectrons(),
                    atom.GetIsAromatic(),
                    endpoint_classes[atom.GetIdx()],
                )
            )
    count = reactant.GetNumAtoms()
    for side, molecule in enumerate((reactant, product)):
        for bond in molecule.GetBonds():  # type: ignore[no-untyped-call]
            index = graph.AddAtom(Chem.Atom(0))
            colours.append((1, side, str(bond.GetBondType()), str(bond.GetStereo())))
            graph.AddBond(side * count + bond.GetBeginAtomIdx(), index, Chem.BondType.SINGLE)
            if bond.GetBondType() == Chem.BondType.DATIVE:
                # Coordination is directed: donor and acceptor must not be
                # interchangeable in the joint correspondence graph.
                acceptor = graph.AddAtom(Chem.Atom(0))
                colours.append((2, side, "dative-acceptor"))
                graph.AddBond(index, acceptor, Chem.BondType.SINGLE)
                index = acceptor
            graph.AddBond(index, side * count + bond.GetEndAtomIdx(), Chem.BondType.SINGLE)
    for index in range(count):
        graph.AddBond(index, count + index, Chem.BondType.SINGLE)
    palette = {colour: index + 1 for index, colour in enumerate(sorted(set(colours)))}
    for atom, colour in zip(graph.GetAtoms(), colours, strict=True):  # type: ignore[no-untyped-call]
        atom.SetIsotope(palette[colour])
        atom.SetNoImplicit(True)
    graph.UpdatePropertyCache(strict=False)
    ranks = list(Chem.CanonicalRankAtoms(graph, breakTies=True))
    return sorted(range(count), key=ranks.__getitem__)


def canonical_reaction_identity(components: ReactionComponents) -> CanonicalReactionIdentity:
    """Standardize a complete mapped reaction, retaining the original bijection."""
    sides = [
        _combine_components(components.get(side, ())) for side in (Side.REACTANT, Side.PRODUCT)
    ]
    if any(side is None for side in sides):
        raise ValueError("reaction requires complete unique maps on both endpoints")
    reactants, products = sides
    assert reactants is not None and products is not None
    reactant, reactant_maps = reactants
    product, product_maps = products
    if set(reactant_maps) != set(product_maps):
        raise ValueError("reaction must conserve every atom including hydrogen")
    product_indices = {number: index for index, number in enumerate(product_maps)}
    product = Chem.RenumberAtoms(product, [product_indices[number] for number in reactant_maps])
    if [(a.GetAtomicNum(), a.GetIsotope()) for a in reactant.GetAtoms()] != [  # type: ignore[no-untyped-call]
        (a.GetAtomicNum(), a.GetIsotope())
        for a in product.GetAtoms()  # type: ignore[no-untyped-call]
    ]:
        raise ValueError("reaction correspondence changes element or isotope")

    joint_order = _joint_order(reactant, product)
    precursor = Chem.RenumberAtoms(_map_free(reactant), joint_order)
    canonical = _canonical_smiles(precursor, include_stereochemistry=True)
    if canonical is None:
        raise ValueError("reactant cannot establish a lossless canonical atom traversal")
    source_order = [joint_order[index] for index in canonical[1]]
    source_to_map = {reactant_maps[index]: number for number, index in enumerate(source_order, 1)}
    rendered: list[str] = []
    for molecule in (reactant, product):
        mapped = _map_free(molecule)
        for atom, source_map in zip(mapped.GetAtoms(), reactant_maps, strict=True):  # type: ignore[no-untyped-call]
            atom.SetAtomMapNum(source_to_map[source_map])
        rendered.append(
            serialize_molecule_smiles(mapped, preserve_atom_maps=True, all_hs_explicit=True)
        )
    return CanonicalReactionIdentity(
        smiles=f"{rendered[0]}>>{rendered[1]}", source_map_to_canonical=source_to_map
    )
