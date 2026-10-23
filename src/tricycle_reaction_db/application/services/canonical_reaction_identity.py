"""Reactant traversal defines reaction maps; source labels are provenance only."""

from collections.abc import Mapping, Sequence
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

REACTION_INDEX_POLICY = "canonical-reactant-joint-correspondence-v2"


def derived_reaction_variant_key(
    components: Mapping[tuple[Side, int], tuple[Chem.Mol, Sequence[int]]],
) -> tuple[tuple[tuple[str, int, str], ...], str]:
    """Identify a generated variant, never an independently observed reaction.

    Expansion selects concrete stereo topologies for existing logical slots.
    Do not manufacture another atom-to-stereocentre assignment for a combination
    already represented. Preserve complete connectivity correspondence, so equal
    endpoint graphs alone cannot collapse distinct atom-transfer mechanisms.
    Direct TS ingestion still uses the strict stereo reaction identity below.
    """
    endpoint_keys = []
    connectivity = {Side.REACTANT: [], Side.PRODUCT: []}
    for (side, index), (molecule, maps) in components.items():
        projection = _canonical_smiles(
            molecule, include_stereochemistry=True, preserve_assigned_stereo=True
        )
        if projection is None:
            raise ValueError("derived reaction requires a lossless concrete stereo topology")
        endpoint_keys.append((side.value, index, projection[0]))
        molecule = _map_free(molecule)
        Chem.RemoveStereochemistry(molecule)
        connectivity[side].append((molecule, maps))
    return tuple(sorted(endpoint_keys)), canonical_reaction_identity(connectivity).smiles


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


def _ranking_projection(molecule: Chem.Mol) -> Chem.Mol:
    """Use one stereo representation for ranking, retaining source atom indices.

    Stored MOLs can encode the same double bond with different control atoms
    (and therefore opposite E/Z enum values). RDKit ranks and joint bond colours
    consume those values directly. A lossless labelled SMILES round trip gives
    the ranking graph consistent controls without changing the source molecule.
    Parsing is unsanitized so trusted metal valence and electron assignments are
    not replaced by RDKit's valence inference.
    """
    labelled = _map_free(molecule)
    for atom in labelled.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    smiles = serialize_molecule_smiles(labelled, preserve_atom_maps=True, all_hs_explicit=True)
    parser = Chem.SmilesParserParams()
    parser.removeHs = False
    parser.sanitize = False
    parsed = Chem.MolFromSmiles(smiles, parser)
    if parsed is None:
        raise ValueError("reaction ranking requires a lossless stereo projection")
    indices = {
        atom.GetAtomMapNum(): atom.GetIdx()
        for atom in parsed.GetAtoms()  # type: ignore[no-untyped-call]
    }
    if set(indices) != set(range(1, molecule.GetNumAtoms() + 1)):
        raise ValueError("reaction ranking projection changed atom identities")
    projected = Chem.RenumberAtoms(
        parsed, [indices[index + 1] for index in range(molecule.GetNumAtoms())]
    )
    for atom, source in zip(projected.GetAtoms(), molecule.GetAtoms(), strict=True):  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
        atom.SetNumRadicalElectrons(source.GetNumRadicalElectrons())
    projected.UpdatePropertyCache(strict=False)
    Chem.FastFindRings(projected)
    Chem.AssignStereochemistry(projected, cleanIt=True, force=True)
    return projected


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

    ranking_reactant = _ranking_projection(reactant)
    ranking_product = _ranking_projection(product)
    joint_order = _joint_order(ranking_reactant, ranking_product)
    precursor = Chem.RenumberAtoms(ranking_reactant, joint_order)
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
