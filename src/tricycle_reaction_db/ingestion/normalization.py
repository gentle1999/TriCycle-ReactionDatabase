"""Canonicalize parsed molecular frames into immutable business records."""

import json
from collections import Counter
from hashlib import sha256
from typing import Any

import numpy as np
import numpy.typing as npt
from molgr.utils.converter import (
    METAL_UNPAIRED_ELECTRONS_PROP,
    get_atom_unpaired_electrons,
)
from rdkit import Chem
from rdkit.Geometry import Point3D

from tricycle_reaction_db.application.dtos.chemistry import (
    GeometryRecord,
    MolecularFormulaRecord,
    MolecularTopologyDerivationRecord,
    MolecularTopologyRecord,
    NormalizedMoleculeRecord,
    NormalizedTopologyRecord,
)
from tricycle_reaction_db.domain.enums import StereoStatus, TopologySanitizationStatus
from tricycle_reaction_db.domain.formulas import element_count_vector_from_composition
from tricycle_reaction_db.domain.internal_coordinates import (
    canonical_cartesian_coordinates,
    cartesian_from_internal_coordinates,
    internal_coordinate_hash,
    internal_coordinates_from_cartesian,
    proper_rigid_alignment,
)

FORMULA_COMPOSITION_VERSION = "formula-composition-v1"
TOPOLOGY_IDENTITY_VERSION = "topology-identity-v1"
TOPOLOGY_DERIVATION_VERSION = "topology-derivation-v1"
GEOMETRY_CANONICALIZATION_VERSION = "geometry-internal-coordinates-v1"
_INTERNAL_COORDINATE_ROUNDTRIP_TOLERANCE_ANGSTROM = 1e-7


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


_STEREOCHEMISTRY_PROPERTIES = frozenset(
    {
        "__computedProps",
        "_CIPCode",
        "_CIPRank",
        "_ChiralityPossible",
        "_StereochemDone",
        "_MolFileBondStereo",
        "_MolFileBondCfg",
    }
)
_SMILES_OUTPUT_ORDER_PROPERTIES = ("_smilesAtomOutputOrder", "_smilesBondOutputOrder")


def _is_stereochemistry_property(prop_name: str) -> bool:
    """Return whether an RDKit property is part of stereo assignment state."""

    return prop_name in _STEREOCHEMISTRY_PROPERTIES or prop_name.startswith("_CIP")


def _clear_rdkit_properties(
    mol: Chem.Mol,
    *,
    preserve_stereochemistry: bool = False,
) -> None:
    """Drop parser metadata while optionally retaining MolGR stereo caches.

    MolGR has already restored stereochemistry from the QM coordinates before
    this boundary.  Clearing RDKit's computed CIP properties forces an
    expensive and potentially different second assignment later, so trusted
    MolGR graphs retain those properties verbatim.
    """

    for prop_name in list(mol.GetPropNames(includePrivate=True, includeComputed=True)):
        if preserve_stereochemistry and _is_stereochemistry_property(prop_name):
            continue
        mol.ClearProp(prop_name)
    for atom in mol.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
        for prop_name in list(atom.GetPropNames(includePrivate=True, includeComputed=True)):
            if preserve_stereochemistry and _is_stereochemistry_property(prop_name):
                continue
            atom.ClearProp(prop_name)
    for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
        for prop_name in list(bond.GetPropNames(includePrivate=True, includeComputed=True)):
            if preserve_stereochemistry and _is_stereochemistry_property(prop_name):
                continue
            bond.ClearProp(prop_name)


def _copy_rdkit_property(source: Any, target: Any, prop_name: str) -> None:
    """Copy one typed RDKit property without rebuilding chemical state."""

    value = source.GetPropsAsDict(includePrivate=True, includeComputed=True).get(prop_name)
    if isinstance(value, bool):
        target.SetBoolProp(prop_name, value)
    elif isinstance(value, int):
        target.SetIntProp(prop_name, value)
    elif isinstance(value, float):
        target.SetDoubleProp(prop_name, value)
    else:
        target.SetProp(prop_name, source.GetProp(prop_name))


def _renumber_atoms_preserving_stereochemistry(
    mol: Chem.Mol,
    order: list[int],
) -> Chem.Mol:
    """Apply a deterministic projection while retaining MolGR stereo caches."""

    renumbered = Chem.RenumberAtoms(mol, order)
    for prop_name in mol.GetPropNames(includePrivate=True, includeComputed=True):
        if prop_name == "__computedProps" or not _is_stereochemistry_property(prop_name):
            continue
        _copy_rdkit_property(mol, renumbered, prop_name)
    return renumbered


def _canonical_smiles_atom_order(mol: Chem.Mol) -> list[int] | None:
    """Read the atom traversal emitted by the preceding canonical SMILES write."""

    value = mol.GetPropsAsDict(includePrivate=True, includeComputed=True).get(
        "_smilesAtomOutputOrder"
    )
    if value is None:
        return None
    order = [int(index) for index in value]
    if sorted(order) != list(range(mol.GetNumAtoms())):
        return None
    return order


def _clear_smiles_output_order(mol: Chem.Mol) -> None:
    for prop_name in _SMILES_OUTPUT_ORDER_PROPERTIES:
        if mol.HasProp(prop_name):
            mol.ClearProp(prop_name)


def _apply_topology_projection(
    topology: Chem.Mol,
    source_to_topology: list[int],
    order: list[int],
) -> tuple[Chem.Mol, list[int]]:
    """Compose source mapping with a new-index-to-old-index atom projection."""

    atom_count = topology.GetNumAtoms()
    if sorted(source_to_topology) != list(range(atom_count)):
        raise ValueError("source-to-topology atom indices must be a full permutation")
    if sorted(order) != list(range(atom_count)):
        raise ValueError("topology projection must be a full atom permutation")
    old_to_new = [0] * atom_count
    for new_index, old_index in enumerate(order):
        old_to_new[old_index] = new_index
    projected_mapping = [old_to_new[index] for index in source_to_topology]
    projected = (
        topology
        if order == list(range(atom_count))
        else _renumber_atoms_preserving_stereochemistry(topology, order)
    )
    _clear_smiles_output_order(projected)
    projected.RemoveAllConformers()
    _initialize_ring_info(projected)
    return projected, projected_mapping


def _capture_unpaired_electron_state(mol: Chem.Mol) -> list[tuple[int, bool]]:
    """Capture MolGR electron assignments before clearing transient properties."""

    return [
        (
            get_atom_unpaired_electrons(atom),
            atom.HasProp(METAL_UNPAIRED_ELECTRONS_PROP),
        )
        for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
    ]


def _restore_unpaired_electron_state(
    mol: Chem.Mol,
    state: list[tuple[int, bool]],
) -> None:
    """Restore MolGR assignments after RDKit sanitization/normalization."""

    if len(state) != mol.GetNumAtoms():
        raise ValueError("unpaired-electron state does not match atom count")
    for atom, (count, is_metal_assignment) in zip(
        mol.GetAtoms(),  # type: ignore[no-untyped-call]
        state,
        strict=True,
    ):
        if is_metal_assignment:
            atom.SetNumRadicalElectrons(0)
            atom.SetIntProp(METAL_UNPAIRED_ELECTRONS_PROP, int(count))
        else:
            atom.SetNumRadicalElectrons(int(count))


def _initialize_ring_info(mol: Chem.Mol) -> None:
    """Populate graph ring metadata without requiring chemical sanitization."""

    Chem.FastFindRings(mol)


def _canonical_topology(
    mol: Chem.Mol,
    *,
    sanitize: bool = True,
    preserve_source_order: bool = False,
    preserve_stereochemistry: bool = False,
) -> tuple[Chem.Mol, list[int], TopologySanitizationStatus, str | None]:
    source_topology = Chem.Mol(mol)
    source_topology.RemoveAllConformers()
    unpaired_electron_state = _capture_unpaired_electron_state(source_topology)
    _clear_rdkit_properties(
        source_topology,
        preserve_stereochemistry=preserve_stereochemistry,
    )
    atom_count = source_topology.GetNumAtoms()
    if atom_count == 0:
        raise ValueError("molecular topology must contain at least one atom")

    topology = Chem.Mol(source_topology)
    if preserve_source_order:
        # MolGR's OpenBabel fallback is explicitly untrusted.  In particular,
        # do not probe it with RDKit sanitization: valence repair can mutate the
        # graph and hides the charge/spin provenance we need to persist.
        _initialize_ring_info(topology)
        canonical_order = list(range(atom_count))
        sanitization_status = TopologySanitizationStatus.FAILED
        sanitization_error = (
            "MolGR suspicious_fallback topology; RDKit sanitization was intentionally skipped "
            "(potential AtomValenceException)"
        )
    elif not sanitize:
        # MolGR owns this graph and its source atom order. Do not run any
        # RDKit chemical repair or stereochemistry assignment here. A later
        # canonical SMILES write supplies its own deterministic atom traversal,
        # which is reused as the persisted topology projection.
        _initialize_ring_info(topology)
        canonical_order = list(range(atom_count))
        sanitization_status = TopologySanitizationStatus.SANITIZED
        sanitization_error = None
    else:
        try:
            Chem.SanitizeMol(topology)
            _initialize_ring_info(topology)
            # RDKit may infer radicals from incomplete metal/charge valence during
            # sanitization. Restore MolGR's assignments before serialization.
            _restore_unpaired_electron_state(topology, unpaired_electron_state)
            Chem.AssignStereochemistry(topology, cleanIt=True, force=True)
            canonical_order = list(range(atom_count))
            sanitization_status = TopologySanitizationStatus.SANITIZED
            sanitization_error = None
        except Exception as error:
            # Keep the source-order connectivity graph. PostgreSQL RDKit can store
            # and substructure-search this binary Mol even when chemical
            # sanitization, descriptors, and Morgan fingerprints are unavailable.
            topology = source_topology
            _initialize_ring_info(topology)
            canonical_order = list(range(atom_count))
            sanitization_status = TopologySanitizationStatus.FAILED
            sanitization_error = f"{type(error).__name__}: {error}"
        else:
            if sanitize and any(
                atom.GetNumImplicitHs()
                for atom in topology.GetAtoms()  # type: ignore[no-untyped-call]
            ):
                raise ValueError(
                    "QM topology must contain every coordinate-bearing hydrogen explicitly"
                )

    _restore_unpaired_electron_state(topology, unpaired_electron_state)

    source_to_topology = [0] * atom_count
    for topology_index, source_index in enumerate(canonical_order):
        source_to_topology[source_index] = topology_index

    # Avoid identity RenumberAtoms: RDKit drops molecule-level computed stereo
    # state even though atom-level CIP properties survive.
    if canonical_order == list(range(atom_count)):
        canonical = topology
    else:
        canonical = _renumber_atoms_preserving_stereochemistry(topology, canonical_order)
    canonical.RemoveAllConformers()
    _initialize_ring_info(canonical)
    return canonical, source_to_topology, sanitization_status, sanitization_error


def _graph_smiles(
    mol: Chem.Mol,
    *,
    all_hydrogens_explicit: bool,
    canonical: bool = True,
) -> str | None:
    try:
        return Chem.MolToSmiles(
            mol,
            canonical=canonical,
            isomericSmiles=True,
            allHsExplicit=all_hydrogens_explicit,
        )
    except Exception:
        return None


def _source_order_graph_signature(mol: Chem.Mol) -> dict[str, object]:
    """Return a serialization fallback when canonical SMILES is unavailable."""

    return {
        "atoms": [
            [
                atom.GetAtomicNum(),
                atom.GetIsotope(),
                atom.GetFormalCharge(),
                atom.GetNumRadicalElectrons(),
                int(atom.GetChiralTag()),
                atom.GetIsAromatic(),
                atom.GetNoImplicit(),
                atom.GetNumExplicitHs(),
            ]
            for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
        ],
        "bonds": [
            [
                min(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                max(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                str(bond.GetBondType()),
                bond.GetIsAromatic(),
                int(bond.GetStereo()),
            ]
            for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
        ],
    }


def _formula_components(mol: Chem.Mol) -> tuple[list[dict[str, int]], str]:
    counts = Counter(
        (atom.GetAtomicNum(), atom.GetIsotope())
        for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
    )
    composition = [
        {"atomic_number": atomic_number, "isotope": isotope, "count": count}
        for (atomic_number, isotope), count in sorted(counts.items())
    ]

    periodic_table = Chem.GetPeriodicTable()

    def hill_key(item: tuple[tuple[int, int], int]) -> tuple[int, str, int]:
        (atomic_number, isotope), _ = item
        symbol = periodic_table.GetElementSymbol(atomic_number)
        group = 0 if symbol == "C" else 1 if symbol == "H" else 2
        return group, symbol, isotope

    tokens: list[str] = []
    for (atomic_number, isotope), count in sorted(counts.items(), key=hill_key):
        symbol = periodic_table.GetElementSymbol(atomic_number)
        token = symbol if isotope == 0 else f"[{isotope}{symbol}]"
        tokens.append(token if count == 1 else f"{token}{count}")
    return composition, "".join(tokens)


def _stereo_status(mol: Chem.Mol) -> StereoStatus:
    stereo = list(Chem.FindPotentialStereo(mol))
    if not stereo:
        return StereoStatus.UNKNOWN
    if any(info.specified == Chem.StereoSpecified.Unknown for info in stereo):
        return StereoStatus.CONFLICT
    if any(info.specified == Chem.StereoSpecified.Unspecified for info in stereo):
        return StereoStatus.UNASSIGNED
    return StereoStatus.ASSIGNED


_DOUBLE_BOND_E_Z_STEREO = frozenset({Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ})
_SERIALIZED_DOUBLE_BOND_STEREO = frozenset(
    {
        Chem.BondStereo.STEREOCIS,
        Chem.BondStereo.STEREOTRANS,
        Chem.BondStereo.STEREOE,
        Chem.BondStereo.STEREOZ,
    }
)


def _has_serialized_e_z_marker(smiles: str | None) -> bool:
    return smiles is not None and ("/" in smiles or "\\" in smiles)


def _serialized_e_z_stereo(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
) -> dict[frozenset[int], Chem.BondStereo]:
    """Return SMILES-round-tripped E/Z assignments keyed by atom identities."""

    projected = Chem.Mol(mol)
    existing_maps = [atom.GetAtomMapNum() for atom in projected.GetAtoms()]
    if not (
        preserve_atom_maps
        and all(number > 0 for number in existing_maps)
        and len(set(existing_maps)) == len(existing_maps)
    ):
        for atom_index, atom in enumerate(
            projected.GetAtoms(),
            start=1,
        ):  # type: ignore[no-untyped-call]
            # Temporary labels let us compare the same graph edges after
            # canonical SMILES changes the atom and bond ordering.
            atom.SetAtomMapNum(atom_index)
    smiles = _graph_smiles(projected, all_hydrogens_explicit=True)
    if not _has_serialized_e_z_marker(smiles):
        return {}
    try:
        # Let the SMILES parser assign the double-bond stereo from the emitted
        # directions.  ``SetBondStereoFromDirections`` alone reports the
        # legacy CIS/TRANS spelling against a temporary stereo-atom order and
        # can therefore appear to flip a valid E/Z value on branched graphs.
        serialized = Chem.MolFromSmiles(smiles, sanitize=True)
        if serialized is None:
            return {}
        result: dict[frozenset[int], Chem.BondStereo] = {}
        for bond in serialized.GetBonds():  # type: ignore[no-untyped-call]
            stereo = bond.GetStereo()
            if stereo not in _SERIALIZED_DOUBLE_BOND_STEREO:
                continue
            # ``SetBondStereoFromDirections`` reports the legacy CIS/TRANS
            # enum for a direction-only parse when sanitization is disabled.
            # Normalize that spelling to the E/Z value stored by MolGR before
            # comparing the assigned state with the serialized projection.
            stereo = {
                Chem.BondStereo.STEREOCIS: Chem.BondStereo.STEREOZ,
                Chem.BondStereo.STEREOTRANS: Chem.BondStereo.STEREOE,
            }.get(stereo, stereo)
            result[
                frozenset(
                    (
                        serialized.GetAtomWithIdx(bond.GetBeginAtomIdx()).GetAtomMapNum(),
                        serialized.GetAtomWithIdx(bond.GetEndAtomIdx()).GetAtomMapNum(),
                    )
                )
            ] = stereo
        return result
    except Exception:
        return {}


def _serialized_double_bond_keys_match(
    serialized: dict[frozenset[int], Chem.BondStereo],
    expected: dict[frozenset[int], Chem.BondStereo],
) -> bool:
    """Check that every assigned double bond retains its E/Z designation.

    The atom-map key identifies the double bond independently of canonical
    traversal. The value must also match: a stale or contradictory BondDir can
    otherwise make the SMILES writer emit the opposite E/Z state while the
    source molecule still reports its original BondStereo value.
    """

    return serialized == expected


def _restore_double_bond_directions_from_smiles_reference(
    mol: Chem.Mol,
    reference_smiles: str,
    stereo_bonds: list[Chem.Bond],
) -> bool:
    """Copy SMILES slash directions onto a graph with the same atom order."""

    reference = Chem.MolFromSmiles(reference_smiles, sanitize=False)
    if reference is None or reference.GetNumAtoms() != mol.GetNumAtoms():
        return False
    try:
        # The stored canonical projection has the same graph but may use a
        # different atom order.  Ignore stereo while finding the graph
        # correspondence; the directions are precisely what we are restoring.
        source_to_reference = reference.GetSubstructMatch(mol, useChirality=False)
    except (RuntimeError, ValueError):
        return False
    if len(source_to_reference) != mol.GetNumAtoms():
        return False

    # The canonical reference may choose a different equivalent substituent
    # as the SMILES stereo-atom pair than the source graph.  Copy every
    # directional single bond, rather than only the pair currently stored on
    # each source double bond, so branched conjugated systems are covered too.
    reference_to_source = {
        reference_index: source_index
        for source_index, reference_index in enumerate(source_to_reference)
    }
    for bond in mol.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)
    for reference_bond in reference.GetBonds():
        direction = reference_bond.GetBondDir()
        if direction == Chem.BondDir.NONE:
            continue
        source_bond = mol.GetBondBetweenAtoms(
            reference_to_source[reference_bond.GetBeginAtomIdx()],
            reference_to_source[reference_bond.GetEndAtomIdx()],
        )
        if source_bond is None:
            return False
        source_bond.SetBondDir(direction)
    return True


def _solve_double_bond_direction_constraints(
    mol: Chem.Mol,
    stereo_bonds: list[Chem.Bond],
    *,
    preserve_atom_maps: bool = False,
) -> bool:
    """Set writer directions while respecting bonds shared by two E/Z bonds."""

    # ``SetDoubleBondNeighborDirections`` may have assigned directions to more
    # than the selected stereo-atom pair on a substituted alkene. Clear those
    # provisional flags before applying the solved constraint set; otherwise
    # RDKit can suppress the SMILES stereo marker or emit a contradictory one.
    for bond in mol.GetBonds():
        bond.SetBondDir(Chem.BondDir.NONE)

    constraints: list[tuple[int, int, int]] = []
    for bond in stereo_bonds:
        stereo_atoms = list(bond.GetStereoAtoms())
        if len(stereo_atoms) != 2:
            return False
        first_bond = mol.GetBondBetweenAtoms(bond.GetBeginAtomIdx(), stereo_atoms[0])
        second_bond = mol.GetBondBetweenAtoms(bond.GetEndAtomIdx(), stereo_atoms[1])
        if first_bond is None or second_bond is None:
            return False
        candidate = Chem.Mol(mol)
        for candidate_bond in candidate.GetBonds():
            candidate_bond.SetBondDir(Chem.BondDir.NONE)
        candidate.GetBondWithIdx(first_bond.GetIdx()).SetBondDir(Chem.BondDir.ENDUPRIGHT)
        candidate.GetBondWithIdx(second_bond.GetIdx()).SetBondDir(Chem.BondDir.ENDUPRIGHT)
        serialized = _serialized_e_z_stereo(
            candidate,
            preserve_atom_maps=preserve_atom_maps,
        )
        key = (
            frozenset(
                (
                    mol.GetAtomWithIdx(bond.GetBeginAtomIdx()).GetAtomMapNum(),
                    mol.GetAtomWithIdx(bond.GetEndAtomIdx()).GetAtomMapNum(),
                )
            )
            if preserve_atom_maps
            else frozenset((bond.GetBeginAtomIdx() + 1, bond.GetEndAtomIdx() + 1))
        )
        base_stereo = serialized.get(key)
        if base_stereo not in _DOUBLE_BOND_E_Z_STEREO:
            return False
        parity = 0 if base_stereo == bond.GetStereo() else 1
        constraints.append((first_bond.GetIdx(), second_bond.GetIdx(), parity))

    directions: dict[int, int] = {}
    for first_bond_index, second_bond_index, parity in constraints:
        if first_bond_index not in directions:
            directions[first_bond_index] = (
                directions[second_bond_index] ^ parity if second_bond_index in directions else 0
            )
        expected_second = directions[first_bond_index] ^ parity
        if second_bond_index in directions and directions[second_bond_index] != expected_second:
            return False
        directions[second_bond_index] = expected_second

    for bond in mol.GetBonds():
        if bond.GetIdx() in directions:
            bond.SetBondDir(
                Chem.BondDir.ENDUPRIGHT
                if directions[bond.GetIdx()] == 0
                else Chem.BondDir.ENDDOWNRIGHT
            )
    return True


def ensure_serializable_double_bond_stereochemistry(
    mol: Chem.Mol,
    *,
    reference_smiles: str | None = None,
    preserve_atom_maps: bool = False,
) -> Chem.Mol:
    """Preserve assigned stereo state in an explicit-H SMILES projection.

    MolGR 0.1.8 can assign ``STEREOE``/``STEREOZ`` and its stereo-atom pair
    without the adjacent RDKit bond-direction flags required by the SMILES
    writer.  This function uses the same Cartesian conformer only to recover
    those missing writer flags. Atom-centered chirality (tetrahedral and
    RDKit's supported non-tetrahedral tags), isotopes, radical state, stereo
    groups, and every already-assigned bond stereo value are copied unchanged
    by the RDKit molecule clone and are emitted by ``isomericSmiles=True``.
    This function never sanitizes or reassigns any stereochemical designation.
    """

    repaired = Chem.Mol(mol)
    stereo_bonds = [
        bond
        for bond in repaired.GetBonds()  # type: ignore[no-untyped-call]
        if bond.GetStereo() in _DOUBLE_BOND_E_Z_STEREO
    ]
    if preserve_atom_maps:
        atom_maps = [atom.GetAtomMapNum() for atom in repaired.GetAtoms()]
        if any(number <= 0 for number in atom_maps) or len(set(atom_maps)) != len(atom_maps):
            raise ValueError("preserve_atom_maps requires unique positive atom maps")
        expected_stereo = {
            frozenset(
                (
                    atom_maps[bond.GetBeginAtomIdx()],
                    atom_maps[bond.GetEndAtomIdx()],
                )
            ): bond.GetStereo()
            for bond in stereo_bonds
        }
    else:
        expected_stereo = {
            frozenset((bond.GetBeginAtomIdx() + 1, bond.GetEndAtomIdx() + 1)): bond.GetStereo()
            for bond in stereo_bonds
        }
    serialized_stereo = _serialized_e_z_stereo(
        repaired,
        preserve_atom_maps=preserve_atom_maps,
    )
    if not stereo_bonds or _serialized_double_bond_keys_match(
        serialized_stereo,
        expected_stereo,
    ):
        return repaired
    # RDKit's molecule pickle preserves the MolGR E/Z designation and stereo
    # atom pair, but can drop the neighboring single-bond directions used by
    # the SMILES writer. Re-project those directions directly from the trusted
    # stereo state before consulting coordinates; this does not discover or
    # reassign stereochemistry.
    Chem.SetDoubleBondNeighborDirections(repaired)
    serialized_stereo = _serialized_e_z_stereo(
        repaired,
        preserve_atom_maps=preserve_atom_maps,
    )
    if _serialized_double_bond_keys_match(serialized_stereo, expected_stereo):
        return repaired
    if _solve_double_bond_direction_constraints(
        repaired,
        stereo_bonds,
        preserve_atom_maps=preserve_atom_maps,
    ):
        serialized_stereo = _serialized_e_z_stereo(
            repaired,
            preserve_atom_maps=preserve_atom_maps,
        )
        if _serialized_double_bond_keys_match(serialized_stereo, expected_stereo):
            return repaired
    if reference_smiles is not None and _restore_double_bond_directions_from_smiles_reference(
        repaired,
        reference_smiles,
        stereo_bonds,
    ):
        serialized_stereo = _serialized_e_z_stereo(
            repaired,
            preserve_atom_maps=preserve_atom_maps,
        )
        if _serialized_double_bond_keys_match(serialized_stereo, expected_stereo):
            return repaired
    if repaired.GetNumConformers() != 1:
        raise ValueError(
            "MolGR assigned E/Z stereochemistry without a serializable SMILES projection"
        )
    reference = Chem.MolFromMolBlock(
        Chem.MolToMolBlock(repaired),
        sanitize=False,
        removeHs=False,
        strictParsing=True,
    )
    if reference is None or reference.GetNumAtoms() != repaired.GetNumAtoms():
        raise ValueError("could not recover MolGR E/Z SMILES direction metadata")
    for bond in stereo_bonds:
        stereo_atoms = list(bond.GetStereoAtoms())
        if len(stereo_atoms) != 2:
            raise ValueError("MolGR assigned E/Z stereochemistry without two stereo atoms")
        for atom_index, stereo_atom_index in zip(
            (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
            stereo_atoms,
            strict=True,
        ):
            source_bond = repaired.GetBondBetweenAtoms(atom_index, stereo_atom_index)
            if source_bond is None:
                raise ValueError("MolGR E/Z stereo atoms are not adjacent to their double bond")
            reference_bond = reference.GetBondWithIdx(source_bond.GetIdx())
            direction = reference_bond.GetBondDir()
            if direction == Chem.BondDir.NONE:
                raise ValueError("could not recover MolGR E/Z SMILES direction metadata")
            source_bond.SetBondDir(direction)
    serialized_stereo = _serialized_e_z_stereo(repaired)
    if not _serialized_double_bond_keys_match(serialized_stereo, expected_stereo):
        raise ValueError(
            "MolGR E/Z stereochemistry is not fully representable in explicit-H SMILES "
            f"({len(serialized_stereo)}/{len(stereo_bonds)} double bonds serialized)"
        )
    return repaired


def _preserved_stereo_status(mol: Chem.Mol) -> StereoStatus:
    """Report only stereochemistry already assigned by a trusted graph source."""

    if any(
        atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
        for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
    ) or any(
        bond.GetStereo() != Chem.BondStereo.STEREONONE
        for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
    ):
        return StereoStatus.ASSIGNED
    return StereoStatus.UNKNOWN


def _normalized_topology_records(
    mol: Chem.Mol,
    *,
    reconstruction_method: str,
    reconstruction_version: str,
    reconstruction_metadata: dict[str, Any] | None,
) -> tuple[NormalizedTopologyRecord, list[int]]:
    suspicious_fallback = (reconstruction_metadata or {}).get(
        "molgr_status"
    ) == "suspicious_fallback"
    preserve_stereochemistry = (
        reconstruction_method.startswith("molgr/")
        or (reconstruction_metadata or {}).get("topology_source_trusted") is True
    )
    trusted_molgr_graph = (preserve_stereochemistry) and not suspicious_fallback
    # Every MolGR result, including a suspicious fallback, must retain a
    # lossless E/Z SMILES projection.  ``suspicious_fallback`` only changes the
    # sanitization policy below; it must not create a stereo-specific escape
    # hatch that silently drops MolGR's assigned double-bond state.
    source = (
        ensure_serializable_double_bond_stereochemistry(mol)
        if preserve_stereochemistry
        else Chem.Mol(mol)
    )
    (
        topology_mol,
        source_to_topology,
        sanitization_status,
        sanitization_error,
    ) = _canonical_topology(
        source,
        sanitize=not trusted_molgr_graph,
        preserve_source_order=suspicious_fallback,
        preserve_stereochemistry=preserve_stereochemistry,
    )
    if preserve_stereochemistry:
        # RDKit copies can retain the E/Z designation while dropping the
        # neighboring writer directions. Restore the lossless projection on
        # the complete topology before any endpoint fragment is extracted.
        topology_mol = ensure_serializable_double_bond_stereochemistry(topology_mol)
    composition, hill_formula = _formula_components(topology_mol)
    composition_hash = _digest(
        {"schema_version": FORMULA_COMPOSITION_VERSION, "composition": composition}
    )
    formula = MolecularFormulaRecord(
        hill_formula=hill_formula,
        composition=composition,
        composition_schema_version=FORMULA_COMPOSITION_VERSION,
        atom_count=topology_mol.GetNumAtoms(),
        composition_hash=composition_hash,
        element_count_vector=element_count_vector_from_composition(composition),
    )

    # The persisted topology string is the explicit-H graph projection.  Do
    # not switch to an implicit-H "skeleton" projection: coordinate-bearing
    # hydrogen atoms and MolGR's radical annotations are part of the trusted
    # molecular graph identity.
    explicit_graph_smiles = (
        None if suspicious_fallback else _graph_smiles(topology_mol, all_hydrogens_explicit=True)
    )
    stable_topology_projection = False
    if explicit_graph_smiles is not None:
        smiles_atom_order = _canonical_smiles_atom_order(topology_mol)
        if smiles_atom_order is not None:
            topology_mol, source_to_topology = _apply_topology_projection(
                topology_mol,
                source_to_topology,
                smiles_atom_order,
            )
            if preserve_stereochemistry:
                topology_mol = ensure_serializable_double_bond_stereochemistry(topology_mol)
                explicit_graph_smiles = _graph_smiles(
                    topology_mol,
                    all_hydrogens_explicit=True,
                )
                if explicit_graph_smiles is None:
                    raise ValueError(
                        "projected MolGR topology lost its explicit-H SMILES serialization"
                    )
            stable_topology_projection = True
        else:
            _clear_smiles_output_order(topology_mol)
    if explicit_graph_smiles is None and trusted_molgr_graph:
        # Canonical ranking can fail for unusual but trusted MolGR valence
        # states.  A source-order explicit-H SMILES still preserves the graph
        # and its electronic annotations without attempting chemical repair.
        explicit_graph_smiles = _graph_smiles(
            topology_mol,
            all_hydrogens_explicit=True,
            canonical=False,
        )
        _clear_smiles_output_order(topology_mol)
    # ``canonical_isomeric_smiles`` is retained as the public field name for
    # compatibility, but its value is now always the explicit-H projection.
    # This makes topology strings lossless with respect to explicit hydrogen
    # atoms and MolGR-provided radical state.
    canonical_isomeric_smiles = explicit_graph_smiles
    identity_schema_version = (
        TOPOLOGY_IDENTITY_VERSION
        if explicit_graph_smiles is not None and stable_topology_projection
        else "topology-source-order-identity-v1"
    )
    graph_hash = _digest(
        {
            "schema_version": identity_schema_version,
            **(
                {"explicit_graph_smiles": explicit_graph_smiles}
                if explicit_graph_smiles is not None and stable_topology_projection
                else {"source_order_graph": _source_order_graph_signature(topology_mol)}
            ),
        }
    )
    radical_electron_count = 0
    for atom in topology_mol.GetAtoms():  # type: ignore[no-untyped-call]
        radical_electron_count += int(get_atom_unpaired_electrons(atom))
    topology = MolecularTopologyRecord(
        mol=topology_mol,
        canonical_isomeric_smiles=canonical_isomeric_smiles,
        graph_hash=graph_hash,
        identity_schema_version=identity_schema_version,
        atom_count=topology_mol.GetNumAtoms(),
        heavy_atom_count=sum(
            atom.GetAtomicNum() > 1
            for atom in topology_mol.GetAtoms()  # type: ignore[no-untyped-call]
        ),
        formal_charge=sum(
            atom.GetFormalCharge()
            for atom in topology_mol.GetAtoms()  # type: ignore[no-untyped-call]
        ),
        radical_electron_count=radical_electron_count,
        fragment_count=len(Chem.GetMolFrags(topology_mol)),
        stereo_status=(
            _preserved_stereo_status(topology_mol)
            if trusted_molgr_graph
            else (
                _stereo_status(topology_mol)
                if sanitization_status is TopologySanitizationStatus.SANITIZED
                else StereoStatus.UNKNOWN
            )
        ),
        sanitization_status=sanitization_status,
        sanitization_error=sanitization_error,
    )
    derivation_metadata = {
        **(reconstruction_metadata or {}),
        "topology_sanitization_status": sanitization_status.value,
        "topology_sanitization_error": sanitization_error,
    }
    source_atom_map_numbers = derivation_metadata.get("source_atom_map_numbers")
    if isinstance(source_atom_map_numbers, list):
        topology_atom_count = topology_mol.GetNumAtoms()
        if len(source_atom_map_numbers) != topology_atom_count:
            raise ValueError("source atom-map numbers do not match topology atom count")
        topology_atom_map_numbers = [0] * topology_atom_count
        for source_index, topology_index in enumerate(source_to_topology):
            topology_atom_map_numbers[topology_index] = int(source_atom_map_numbers[source_index])
        derivation_metadata["topology_atom_map_numbers"] = topology_atom_map_numbers
    topology_derivation = MolecularTopologyDerivationRecord(
        reconstruction_method=reconstruction_method,
        reconstruction_version=reconstruction_version,
        reconstruction_metadata=derivation_metadata,
        provenance_schema_version=TOPOLOGY_DERIVATION_VERSION,
        provenance_hash=_digest(
            {
                "schema_version": TOPOLOGY_DERIVATION_VERSION,
                "reconstruction_method": reconstruction_method,
                "reconstruction_version": reconstruction_version,
                "reconstruction_metadata": derivation_metadata,
            }
        ),
    )
    return (
        NormalizedTopologyRecord(
            formula=formula,
            topology=topology,
            topology_derivation=topology_derivation,
        ),
        source_to_topology,
    )


def normalize_topology(
    mol: Chem.Mol,
    *,
    add_hydrogens: bool,
    reconstruction_method: str,
    reconstruction_version: str,
    reconstruction_metadata: dict[str, Any] | None = None,
) -> NormalizedTopologyRecord:
    """Build graph identity without inventing a Geometry."""

    record, _ = normalize_topology_with_mapping(
        mol,
        add_hydrogens=add_hydrogens,
        reconstruction_method=reconstruction_method,
        reconstruction_version=reconstruction_version,
        reconstruction_metadata=reconstruction_metadata,
    )
    return record


def normalize_topology_with_mapping(
    mol: Chem.Mol,
    *,
    add_hydrogens: bool,
    reconstruction_method: str,
    reconstruction_version: str,
    reconstruction_metadata: dict[str, Any] | None = None,
) -> tuple[NormalizedTopologyRecord, list[int]]:
    """Build graph identity and source-to-canonical atom mapping.

    This helper deliberately does not inspect or rebuild Cartesian coordinates;
    it is used by TS mode anchors whose coordinates must stay in one shared
    MolOP source frame.
    """

    suspicious_fallback = (reconstruction_metadata or {}).get(
        "molgr_status"
    ) == "suspicious_fallback"
    trusted_source_graph = (
        reconstruction_method.startswith("molgr/")
        or (reconstruction_metadata or {}).get("topology_source_trusted") is True
    )
    source = Chem.Mol(mol)
    unpaired_electron_state = _capture_unpaired_electron_state(source)
    if not suspicious_fallback and not trusted_source_graph:
        Chem.SanitizeMol(source)
    _restore_unpaired_electron_state(source, unpaired_electron_state)
    if add_hydrogens:
        source = Chem.AddHs(source)
    return _normalized_topology_records(
        source,
        reconstruction_method=reconstruction_method,
        reconstruction_version=reconstruction_version,
        reconstruction_metadata=reconstruction_metadata,
    )


def _ordered_geometry_mol(
    mol: Chem.Mol,
    coordinates: npt.NDArray[np.float64],
    *,
    preserve_stereochemistry: bool = False,
) -> Chem.Mol:
    geometry_mol = Chem.Mol(mol)
    geometry_mol.RemoveAllConformers()
    unpaired_electron_state = _capture_unpaired_electron_state(geometry_mol)
    _clear_rdkit_properties(
        geometry_mol,
        preserve_stereochemistry=preserve_stereochemistry,
    )
    _restore_unpaired_electron_state(geometry_mol, unpaired_electron_state)
    conformer = Chem.Conformer(geometry_mol.GetNumAtoms())
    conformer.SetId(0)
    conformer.Set3D(True)
    for atom_index, (x, y, z) in enumerate(coordinates):
        conformer.SetAtomPosition(atom_index, Point3D(float(x), float(y), float(z)))
    geometry_mol.AddConformer(conformer, assignId=False)
    _initialize_ring_info(geometry_mol)
    return geometry_mol


def normalize_molecule(
    mol: Chem.Mol,
    coordinates: object,
    *,
    charge: int,
    multiplicity: int,
    reconstruction_method: str,
    reconstruction_version: str,
    reconstruction_metadata: dict[str, Any] | None = None,
) -> NormalizedMoleculeRecord:
    """Build deterministic Formula, Topology, and Geometry records."""

    if multiplicity <= 0:
        raise ValueError("multiplicity must be positive")
    source_atom_count = mol.GetNumAtoms()
    observed_coordinates = canonical_cartesian_coordinates(
        coordinates,
        atom_count=source_atom_count,
    )
    observed_atomic_numbers = [
        atom.GetAtomicNum()
        for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    normalized_topology, topology_atom_indices = _normalized_topology_records(
        mol,
        reconstruction_method=reconstruction_method,
        reconstruction_version=reconstruction_version,
        reconstruction_metadata=reconstruction_metadata,
    )
    formula = normalized_topology.formula
    topology = normalized_topology.topology
    graph_hash = topology.graph_hash

    topology_coordinates = np.empty_like(observed_coordinates)
    for source_index, topology_index in enumerate(topology_atom_indices):
        topology_coordinates[topology_index] = observed_coordinates[source_index]
    symbols = [
        atom.GetSymbol()
        for atom in topology.mol.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    internal_coordinates = internal_coordinates_from_cartesian(
        symbols,
        topology_coordinates,
    )
    internal_hash = internal_coordinate_hash(internal_coordinates)
    geometry_coordinates = cartesian_from_internal_coordinates(
        symbols,
        internal_coordinates,
    )
    assignment_rmsd, assignment_max_abs, assignment_transform = proper_rigid_alignment(
        topology_coordinates,
        geometry_coordinates,
    )
    if assignment_max_abs > _INTERNAL_COORDINATE_ROUNDTRIP_TOLERANCE_ANGSTROM:
        raise ValueError(
            "MolOP InternalCoords cannot represent this ordered geometry without loss "
            f"(maximum reconstruction error {assignment_max_abs:.6g} angstrom)"
        )

    observed_coordinate_hash = sha256(observed_coordinates.tobytes(order="C")).hexdigest()
    geometry_hash = _digest(
        {
            "schema_version": GEOMETRY_CANONICALIZATION_VERSION,
            "graph_hash": graph_hash,
            "internal_coordinate_hash": internal_hash,
            "charge": charge,
            "multiplicity": multiplicity,
            "distance_unit": "angstrom",
            "angular_unit": "degree",
        }
    )
    geometry = GeometryRecord(
        mol=_ordered_geometry_mol(
            topology.mol,
            geometry_coordinates,
            preserve_stereochemistry=(
                reconstruction_method.startswith("molgr/")
                or (reconstruction_metadata or {}).get("topology_source_trusted") is True
            ),
        ),
        internal_coordinates=internal_coordinates,
        internal_coordinate_hash=internal_hash,
        geometry_hash=geometry_hash,
        canonicalization_version=GEOMETRY_CANONICALIZATION_VERSION,
        charge=charge,
        multiplicity=multiplicity,
    )
    return NormalizedMoleculeRecord(
        formula=formula,
        topology=topology,
        topology_derivation=normalized_topology.topology_derivation,
        geometry=geometry,
        observed_coordinates=observed_coordinates,
        observed_coordinate_hash=observed_coordinate_hash,
        observed_atomic_numbers=observed_atomic_numbers,
        observed_to_geometry_atom_indices=topology_atom_indices,
        observed_to_geometry_transform=list(assignment_transform),
        geometry_assignment_rmsd_angstrom=assignment_rmsd,
        geometry_assignment_max_abs_angstrom=assignment_max_abs,
        charge=charge,
        multiplicity=multiplicity,
    )


__all__ = [
    "FORMULA_COMPOSITION_VERSION",
    "GEOMETRY_CANONICALIZATION_VERSION",
    "TOPOLOGY_IDENTITY_VERSION",
    "TOPOLOGY_DERIVATION_VERSION",
    "ensure_serializable_double_bond_stereochemistry",
    "normalize_molecule",
    "normalize_topology",
    "normalize_topology_with_mapping",
]
