"""Canonicalize parsed molecular frames into immutable business records."""

import ast
import json
import re
from collections import Counter
from contextlib import suppress
from functools import lru_cache
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
from tricycle_reaction_db.core.chemistry_config import (
    FORMULA_COMPOSITION_VERSION,
    GEOMETRY_CANONICALIZATION_VERSION,
    INVERSION_LABILE_RULES,
    TOPOLOGY_DERIVATION_VERSION,
    TOPOLOGY_IDENTITY_VERSION,
    TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION,
    TOPOLOGY_STEREO_AGNOSTIC_GRAPH_HASH_VERSION,
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
from tricycle_reaction_db.domain.mol_properties import (
    RADICAL_ELECTRONS_PROP,
    annotate_electronic_state,
    metal_spin_identity,
)

_INTERNAL_COORDINATE_ROUNDTRIP_TOLERANCE_ANGSTROM = 1e-7


class StereoProjectionError(ValueError):
    """A trusted stereo assignment cannot be represented losslessly in SMILES."""

    error_code = "stereo_projection_failed"

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        self._evidence = dict(evidence or {})
        super().__init__(message)

    def evidence(self) -> dict[str, Any]:
        return dict(self._evidence)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


_STEREOCHEMISTRY_PROPERTIES = frozenset(
    {
        "__computedProps",
        # Internal marker used to keep the MolGR -> RDKit stereo boundary
        # idempotent while the same trusted graph is projected into Formula,
        # Topology, Geometry, and TS endpoint records.
        "_tricycle_molgr_stereo_normalized",
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
            if prop_name == RADICAL_ELECTRONS_PROP:
                continue
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


def _clear_bond_directions(mol: Chem.Mol) -> None:
    """Remove SMILES-writer directions from a stereo-authoritative graph.

    ``BondDir`` is projection metadata.  MolGR's coordinate pass assigns the
    actual E/Z value to ``BondStereo`` and RDKit may choose a different set of
    neighboring directional bonds for another atom traversal.  Retaining
    those directions across a canonical projection lets a later SMILES write
    override the source atom identities, which is especially dangerous for
    symmetric conjugated systems.
    """

    for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
        bond.SetBondDir(Chem.BondDir.NONE)


@lru_cache(maxsize=32)
def _inversion_labile_rule_query(atom_smarts: str) -> Chem.Mol:
    """Compile one configured inversion-labile atom query once per process."""

    query = Chem.MolFromSmarts(atom_smarts)
    if query is None:
        raise ValueError(f"inversion-labile SMARTS could not be parsed: {atom_smarts}")
    if query.GetNumAtoms() != 1:
        raise ValueError(f"inversion-labile SMARTS must match one atom: {atom_smarts}")
    return query


def clear_inversion_labile_atom_chirality(mol: Chem.Mol) -> Chem.Mol:
    """Clear atom chirality at the configured inversion-labile centres.

    The returned molecule is a clone.  This operation is deliberately applied
    only at the MolGR -> ingestion boundary, after coordinate-based stereo
    inference has completed.  It therefore removes transient ``@``/``@@``
    assignments from labile atoms without changing the source RDKit molecule
    or unrelated atom and double-bond stereochemistry.
    """

    cleaned = Chem.Mol(mol)
    # MolGR's fallback graph can intentionally be unsanitized.  SMARTS atom
    # queries need RDKit's local valence/implicit-H cache, but updating that
    # cache with ``strict=False`` does not perform graph sanitization or valence
    # repair and therefore preserves the trusted MolGR graph contract.
    cleaned.UpdatePropertyCache(strict=False)
    labile_atom_indices: set[int] = set()
    for rule in INVERSION_LABILE_RULES:
        query = _inversion_labile_rule_query(rule.atom_smarts)
        query_atom = query.GetAtomWithIdx(0)
        # These rules are explicitly constrained to one SMARTS atom. Running
        # a whole-molecule substructure search here needlessly launched an
        # isolated RDKit process for every rule and every large calculation
        # frame. Atom.Match applies the same compiled SMARTS predicate in
        # linear time without constructing graph matchings.
        labile_atom_indices.update(
            atom.GetIdx()
            for atom in cleaned.GetAtoms()  # type: ignore[no-untyped-call]
            if query_atom.Match(atom)
        )

    for atom_index in labile_atom_indices:
        atom = cleaned.GetAtomWithIdx(atom_index)
        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
        # Remove cached atom stereo values as well as the tag.  Otherwise a
        # downstream consumer can observe a stale CIP assignment even though
        # the molecule no longer contains the corresponding chiral centre.
        for property_name in (
            "_CIPCode",
            "_CIPRank",
            "_ChiralityPossible",
            "_StereochemDone",
        ):
            if atom.HasProp(property_name):
                atom.ClearProp(property_name)
    return cleaned


def _copy_bond_directions(source: Chem.Mol, target: Chem.Mol) -> None:
    """Copy round-trip stereo and directions between graphs in the same order.

    The normalized MOL must carry the same control atoms as its serialized
    representation, not just the writer's slash flags over stale BondStereo.
    """

    if source.GetNumAtoms() != target.GetNumAtoms():
        raise ValueError("direction source and target atom counts differ")
    _clear_bond_directions(target)
    for source_bond in source.GetBonds():  # type: ignore[no-untyped-call]
        target_bond = target.GetBondBetweenAtoms(
            source_bond.GetBeginAtomIdx(),
            source_bond.GetEndAtomIdx(),
        )
        if target_bond is None:
            raise ValueError("direction source and target graphs differ")
        stereo_atoms = tuple(int(index) for index in source_bond.GetStereoAtoms())
        if len(stereo_atoms) == 2:
            if target_bond.GetBeginAtomIdx() != source_bond.GetBeginAtomIdx():
                stereo_atoms = tuple(reversed(stereo_atoms))
            target_bond.SetStereoAtoms(*stereo_atoms)
        target_bond.SetStereo(source_bond.GetStereo())
        target_bond.SetBondDir(source_bond.GetBondDir())


def _canonical_smiles_atom_order(mol: Chem.Mol) -> list[int] | None:
    """Return the canonical writer traversal without mutating its source graph."""

    candidate = Chem.Mol(mol)
    _clear_smiles_output_order(candidate)
    try:
        Chem.MolToSmiles(
            candidate,
            canonical=True,
            isomericSmiles=True,
            allHsExplicit=True,
        )
    except (RuntimeError, ValueError):
        return None
    value = candidate.GetPropsAsDict(includePrivate=True, includeComputed=True).get(
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


def _remove_atom_maps(mol: Chem.Mol) -> Chem.Mol:
    """Return a map-free copy before any topology identity operation.

    Reaction atom maps belong to the mapping/provenance layer, not to
    reusable molecular-topology identity.  RDKit can use atom maps as a
    canonical traversal tie-breaker and can retain a stale SMILES output
    order after they are changed, so topology normalization must start from a
    clean copy rather than remove maps after the first stereo/canonical pass.
    """

    map_free = Chem.Mol(mol)
    for atom in map_free.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    _clear_smiles_output_order(map_free)
    return map_free


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


def stereo_agnostic_graph_hash(mol: Chem.Mol) -> str:
    """Hash canonical connectivity while deliberately ignoring all stereo.

    The projection retains the graph facts needed to identify a DAG node:
    element and isotope identity, formal charge, explicit hydrogen count,
    radical state, aromaticity, bond connectivity, and bond order. Atom maps,
    atom chirality, bond stereo, and directional writer flags are excluded.
    """

    projected = Chem.Mol(mol)
    for atom in projected.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    Chem.RemoveStereochemistry(projected)
    try:
        smiles = Chem.MolToSmiles(
            projected,
            canonical=True,
            isomericSmiles=True,
            allHsExplicit=True,
        )
    except Exception:
        # Unsanitized trusted MolGR fallbacks may not be serializable by the
        # canonical SMILES writer. Canonical ranks still remove source atom
        # ordering when RDKit can compute them; source ordering is the final
        # deterministic fallback for a graph that RDKit cannot rank.
        try:
            ranks = tuple(int(rank) for rank in Chem.CanonicalRankAtoms(projected, breakTies=True))
        except Exception:
            ranks = tuple(range(projected.GetNumAtoms()))
        atoms = [
            {
                "rank": ranks[index],
                "atomic_number": atom.GetAtomicNum(),
                "isotope": atom.GetIsotope(),
                "formal_charge": atom.GetFormalCharge(),
                "radical_electrons": atom.GetNumRadicalElectrons(),
                "aromatic": atom.GetIsAromatic(),
                "no_implicit": atom.GetNoImplicit(),
                "explicit_hydrogens": atom.GetNumExplicitHs(),
            }
            for index, atom in enumerate(projected.GetAtoms())  # type: ignore[no-untyped-call]
        ]
        bonds = [
            {
                "begin_rank": min(ranks[bond.GetBeginAtomIdx()], ranks[bond.GetEndAtomIdx()]),
                "end_rank": max(ranks[bond.GetBeginAtomIdx()], ranks[bond.GetEndAtomIdx()]),
                "bond_type": str(bond.GetBondType()),
                "aromatic": bond.GetIsAromatic(),
            }
            for bond in projected.GetBonds()  # type: ignore[no-untyped-call]
        ]
        return _digest(
            {
                "schema_version": TOPOLOGY_STEREO_AGNOSTIC_GRAPH_HASH_VERSION,
                **metal_spin_identity(mol),
                "graph": {
                    "atoms": sorted(atoms, key=lambda atom: int(atom["rank"])),
                    "bonds": sorted(
                        bonds,
                        key=lambda bond: (
                            int(bond["begin_rank"]),
                            int(bond["end_rank"]),
                            str(bond["bond_type"]),
                        ),
                    ),
                },
            }
        )
    return _digest(
        {
            "schema_version": TOPOLOGY_STEREO_AGNOSTIC_GRAPH_HASH_VERSION,
            **metal_spin_identity(mol),
            "explicit_graph_smiles": smiles,
        }
    )


def _source_order_graph_signature(
    mol: Chem.Mol,
    *,
    include_stereo_metadata: bool = False,
) -> dict[str, object]:
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
                *([list(bond.GetStereoAtoms())] if include_stereo_metadata else []),
            ]
            for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
        ],
    }


def _stereo_projection_evidence(
    mol: Chem.Mol,
    *,
    reason: str,
    expected: dict[frozenset[int], Chem.BondStereo],
    serialized: dict[frozenset[int], Chem.BondStereo],
) -> dict[str, Any]:
    """Capture enough graph state to explain a rejected stereo projection."""

    def _bond_state(bond: Chem.Bond) -> dict[str, Any]:
        return {
            "bond_index": int(bond.GetIdx()),
            "atom_indices": [int(bond.GetBeginAtomIdx()), int(bond.GetEndAtomIdx())],
            "stereo": str(bond.GetStereo()),
            "stereo_atoms": [int(index) for index in bond.GetStereoAtoms()],
            "bond_direction": str(bond.GetBondDir()),
        }

    return {
        "reason": reason,
        "atom_count": int(mol.GetNumAtoms()),
        "bond_count": int(mol.GetNumBonds()),
        "conformer_count": int(mol.GetNumConformers()),
        "expected_e_z_bonds": [
            {
                "atom_indices": sorted(int(index) for index in key),
                "stereo": str(stereo),
            }
            for key, stereo in sorted(
                expected.items(),
                key=lambda item: tuple(sorted(item[0])),
            )
        ],
        "serialized_e_z_bonds": [
            {
                "atom_indices": sorted(int(index) for index in key),
                "stereo": str(stereo),
            }
            for key, stereo in sorted(
                serialized.items(),
                key=lambda item: tuple(sorted(item[0])),
            )
        ],
        "stereo_bonds": [
            _bond_state(bond)
            for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
            if bond.GetStereo() in _DOUBLE_BOND_E_Z_STEREO
        ],
    }


def _stereo_projection_failure(
    mol: Chem.Mol,
    message: str,
    *,
    reason: str,
    expected: dict[frozenset[int], Chem.BondStereo],
    serialized: dict[frozenset[int], Chem.BondStereo] | None = None,
) -> StereoProjectionError:
    return StereoProjectionError(
        message,
        evidence=_stereo_projection_evidence(
            mol,
            reason=reason,
            expected=expected,
            serialized=serialized or {},
        ),
    )


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
_LEGACY_DOUBLE_BOND_STEREO_TO_E_Z = {
    Chem.BondStereo.STEREOCIS: Chem.BondStereo.STEREOZ,
    Chem.BondStereo.STEREOTRANS: Chem.BondStereo.STEREOE,
}
_SERIALIZED_DOUBLE_BOND_STEREO = frozenset(
    {
        Chem.BondStereo.STEREOCIS,
        Chem.BondStereo.STEREOTRANS,
        Chem.BondStereo.STEREOE,
        Chem.BondStereo.STEREOZ,
    }
)
_MAX_STEREO_SMILES_ROOT_CANDIDATES = 32


def _has_single_3d_conformer(mol: Chem.Mol) -> bool:
    return mol.GetNumConformers() == 1 and mol.GetConformer().Is3D()


def _normalize_aromatic_bond_flags(mol: Chem.Mol) -> None:
    """Explicit bond orders must not carry stale aromatic bond flags.

    MolGR can retain an aromatic cache flag from the precursor ring. Keep
    atoms and connectivity unchanged, while making the final bond flags agree
    with the explicit bond orders that RDKit serializes and reads back.
    """
    for bond in mol.GetBonds():
        if bond.GetBondType() != Chem.BondType.AROMATIC and bond.GetIsAromatic():
            bond.SetIsAromatic(False)


def _clear_nonstereogenic_double_bond_tags(mol: Chem.Mol) -> None:
    """Remove assignments on bonds with no independent geometric isomerism."""
    probe = Chem.Mol(mol)
    for atom in probe.GetAtoms():
        atom.SetAtomMapNum(0)
    potential = {
        int(info.centeredOn)
        for info in Chem.FindPotentialStereo(probe, cleanIt=True)
        if info.type == Chem.StereoType.Bond_Double
    }
    for bond in mol.GetBonds():
        if bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO and bond.GetIdx() not in potential:
            bond.SetStereo(Chem.BondStereo.STEREONONE)


def _normalize_coordination_ring_stereo(mol: Chem.Mol) -> None:
    """Drop only cis markers already implicit in a small coordination ring.

    RDKit's writer treats dative closures as rings and omits slash markers on
    rings smaller than eight atoms, although default ring perception excludes
    dative bonds. Check the actual ring control atoms, not the E/Z enum. A
    trans assignment is never discarded here.
    """
    for bond in mol.GetBonds():
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo(), bond.GetStereo())
        if stereo not in _DOUBLE_BOND_E_Z_STEREO:
            continue
        begin, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        refs = tuple(bond.GetStereoAtoms())
        if len(refs) != 2:
            continue
        paths = [(begin,)]
        visited = {begin}
        found = None
        for path in paths:
            if len(path) >= 7:
                continue
            for neighbor in mol.GetAtomWithIdx(path[-1]).GetNeighbors():
                index = neighbor.GetIdx()
                if len(path) == 1 and index == end:
                    continue
                if index == end:
                    found = (*path, end)
                    break
                if index not in visited:
                    visited.add(index)
                    paths.append((*path, index))
            if found is not None:
                break
        if found is None or not any(
            mol.GetBondBetweenAtoms(a, b).GetBondType() == Chem.BondType.DATIVE
            for a, b in zip(found, found[1:], strict=False)
        ):
            continue
        flipped = (refs[0] != found[1]) ^ (refs[1] != found[-2])
        ring_stereo = _flip_e_z_stereo(stereo) if flipped else stereo
        if ring_stereo == Chem.BondStereo.STEREOZ:
            bond.SetStereo(Chem.BondStereo.STEREONONE)


def infer_molgr_stereochemistry_from_3d(mol: Chem.Mol) -> Chem.Mol:
    """Create one coordinate-authoritative stereo snapshot of a MolGR graph.

    This is the only function in the ingestion layer that is allowed to infer
    endpoint stereochemistry from Cartesian coordinates. The returned molecule
    is a clone; callers may use it as the frozen source graph while all later
    SMILES projection helpers operate on further clones.
    """

    if not _has_single_3d_conformer(mol):
        raise ValueError("stereochemistry inference requires one 3D conformer")

    inferred = Chem.Mol(mol)
    annotate_electronic_state(inferred)
    _normalize_aromatic_bond_flags(inferred)
    if inferred.HasProp("_tricycle_molgr_stereo_normalized"):
        inferred.ClearProp("_tricycle_molgr_stereo_normalized")
    # Existing BondStereo and BondDir values may have come from MolGR's graph
    # reconstruction or from an earlier SMILES traversal. Neither is evidence
    # for a displaced endpoint, so remove both before the coordinate pass.
    # Clear computed CIP ranks as well as tags: ranks cached on a graph before
    # coordination was finalized must not select the new stereo control atoms.
    Chem.RemoveStereochemistry(inferred)
    inferred.UpdatePropertyCache(strict=False)
    Chem.AssignStereochemistryFrom3D(
        inferred,
        confId=-1,
        replaceExistingTags=True,
    )
    for bond in inferred.GetBonds():  # type: ignore[no-untyped-call]
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo())
        if stereo is not None:
            bond.SetStereo(stereo)
    # ``AssignStereochemistryFrom3D`` also leaves a traversal-specific set of
    # BondDir flags.  They are not an additional coordinate measurement and
    # become misleading as soon as atoms are reordered or reaction maps are
    # attached.  Freeze only BondStereo as the source-of-truth state.
    _clear_bond_directions(inferred)
    # MolGR may assign a transient tetrahedral tag to an atom whose geometry
    # can invert on the reaction timescale.  Do this after the coordinate pass;
    # clearing it before AssignStereochemistryFrom3D would let the pass add it
    # back immediately.
    inferred = clear_inversion_labile_atom_chirality(inferred)
    _clear_nonstereogenic_double_bond_tags(inferred)
    _normalize_coordination_ring_stereo(inferred)
    inferred.SetBoolProp("_tricycle_molgr_stereo_normalized", True)
    return inferred


def normalize_molgr_stereochemistry(mol: Chem.Mol) -> Chem.Mol:
    """Normalize a MolGR graph without letting projection become authority.

    A single trusted 3D conformer delegates to
    :func:`infer_molgr_stereochemistry_from_3d`. Without a conformer, this
    function only preserves existing MolGR stereo and accepts direction-only
    graph input for compatibility with non-TS reaction representations. It is
    never a substitute for endpoint coordinate inference. In either case,
    configured inversion-labile atom chirality is removed before the graph is
    handed to topology and geometry identity creation.
    """

    mol = Chem.Mol(mol)
    annotate_electronic_state(mol)
    _normalize_aromatic_bond_flags(mol)
    if mol.HasProp("_tricycle_molgr_stereo_normalized"):
        # The marker is set only after the coordinate pass and inversion-labile
        # cleanup have completed. Returning the marked graph avoids repeating
        # the SMARTS scans when the same frame crosses the topology boundary.
        return mol
    if _has_single_3d_conformer(mol):
        return infer_molgr_stereochemistry_from_3d(mol)

    normalized = Chem.Mol(mol)
    source_bond_stereo: dict[int, tuple[Chem.BondStereo, tuple[int, ...]]] = {}
    for bond in normalized.GetBonds():  # type: ignore[no-untyped-call]
        stereo = bond.GetStereo()
        if stereo != Chem.BondStereo.STEREONONE:
            source_bond_stereo[bond.GetIdx()] = (
                stereo,
                tuple(int(index) for index in bond.GetStereoAtoms()),
            )

    # This is a graph-input compatibility operation, not endpoint inference.
    Chem.SetBondStereoFromDirections(normalized)
    potential_double_bond_indices: set[int] | None = None

    def potential_double_bonds() -> set[int]:
        nonlocal potential_double_bond_indices
        if potential_double_bond_indices is None:
            potential_double_bond_indices = {
                int(info.centeredOn)
                for info in Chem.FindPotentialStereo(normalized)
                if info.type is Chem.StereoType.Bond_Double
            }
        return potential_double_bond_indices

    # Existing MolGR assignments remain authoritative when no coordinates are
    # available. Only direction-only values on chemically potential alkenes
    # may be retained from the compatibility conversion above.
    for bond_index, (stereo, stereo_atoms) in source_bond_stereo.items():
        bond = normalized.GetBondWithIdx(bond_index)
        bond.SetStereo(stereo)
        if len(stereo_atoms) == 2:
            bond.SetStereoAtoms(*stereo_atoms)
    for bond in normalized.GetBonds():  # type: ignore[no-untyped-call]
        if bond.GetIdx() in source_bond_stereo:
            continue
        if bond.GetStereo() == Chem.BondStereo.STEREONONE:
            continue
        if bond.GetIdx() not in potential_double_bonds():
            bond.SetStereo(Chem.BondStereo.STEREONONE)
            continue
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo())
        if stereo is not None:
            bond.SetStereo(stereo)
    _clear_bond_directions(normalized)
    normalized = clear_inversion_labile_atom_chirality(normalized)
    _clear_nonstereogenic_double_bond_tags(normalized)
    _normalize_coordination_ring_stereo(normalized)
    normalized.SetBoolProp("_tricycle_molgr_stereo_normalized", True)
    return normalized


DoubleBondStereoSignature = tuple[
    Chem.BondStereo,
    tuple[int, int],
    tuple[int, int],
]


def _stereo_identity_numbers(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool,
) -> list[int]:
    """Return stable atom identities used by a stereo projection comparison."""

    existing_maps = [
        atom.GetAtomMapNum()
        for atom in mol.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    if preserve_atom_maps:
        if any(number <= 0 for number in existing_maps) or len(set(existing_maps)) != len(
            existing_maps
        ):
            raise ValueError("preserve_atom_maps requires unique positive atom maps")
        return existing_maps
    return list(range(1, mol.GetNumAtoms() + 1))


def _e_z_stereo_signature(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool,
) -> dict[frozenset[int], DoubleBondStereoSignature]:
    """Describe E/Z with its double-bond endpoints and control atoms.

    ``BondStereo`` alone identifies only the double-bond edge.  It does not
    identify which substituent on either endpoint is the stereo reference.
    That distinction matters for substituted conjugated systems: moving one
    slash to the other substituent changes the physical geometry while the
    edge-level E/Z enum can remain unchanged.
    """

    identities = _stereo_identity_numbers(
        mol,
        preserve_atom_maps=preserve_atom_maps,
    )
    result: dict[frozenset[int], DoubleBondStereoSignature] = {}
    for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
        stereo = {
            Chem.BondStereo.STEREOCIS: Chem.BondStereo.STEREOZ,
            Chem.BondStereo.STEREOTRANS: Chem.BondStereo.STEREOE,
        }.get(bond.GetStereo(), bond.GetStereo())
        if stereo not in _SERIALIZED_DOUBLE_BOND_STEREO:
            continue
        begin = bond.GetBeginAtomIdx()
        end = bond.GetEndAtomIdx()
        stereo_atoms = tuple(identities[index] for index in bond.GetStereoAtoms())
        if len(stereo_atoms) != 2:
            stereo_atoms = (-1, -1)
        result[frozenset((identities[begin], identities[end]))] = (
            stereo,
            stereo_atoms,
            (identities[begin], identities[end]),
        )
    return result


def _smiles_output_atom_order(mol: Chem.Mol) -> list[int] | None:
    """Return source atom indices in the exact order emitted by RDKit."""

    if not mol.HasProp("_smilesAtomOutputOrder"):
        return None
    try:
        value = ast.literal_eval(mol.GetProp("_smilesAtomOutputOrder"))
    except (SyntaxError, ValueError):
        return None
    if not isinstance(value, list) or any(not isinstance(index, int) for index in value):
        return None
    order = [int(index) for index in value]
    if sorted(order) != list(range(mol.GetNumAtoms())):
        return None
    return order


def _atom_graph_signature(
    atom: Chem.Atom,
    *,
    include_radical_electrons: bool = True,
) -> tuple[int, int, int, int, bool, int]:
    """Return the atom facts that an isomeric SMILES must preserve."""

    try:
        hydrogen_count = int(atom.GetTotalNumHs(includeNeighbors=True))
    except (RuntimeError, ValueError):
        hydrogen_count = int(atom.GetNumExplicitHs())
    return (
        int(atom.GetAtomicNum()),
        int(atom.GetIsotope()),
        int(atom.GetFormalCharge()),
        int(atom.GetNumRadicalElectrons()) if include_radical_electrons else 0,
        bool(atom.GetIsAromatic()),
        hydrogen_count,
    )


def _molecular_graph_signature(
    mol: Chem.Mol,
    atom_identities: list[int],
    *,
    include_radical_electrons: bool = True,
) -> tuple[
    tuple[tuple[int, tuple[int, int, int, int, bool, int]], ...],
    tuple[tuple[tuple[int, int], str, bool], ...],
]:
    """Describe connectivity and chemistry independently of atom ordering."""

    graph = Chem.Mol(mol)
    with suppress(RuntimeError, ValueError):
        # MolOP may provide a trusted unsanitized graph with an uninitialized
        # RDKit valence cache. Match the hydrogen count used by MolToSmiles
        # without running chemical sanitization or mutating the source graph.
        graph.UpdatePropertyCache(strict=False)
    if len(atom_identities) != graph.GetNumAtoms():
        raise ValueError("SMILES round trip changed the atom count")
    atom_signature = tuple(
        sorted(
            (
                identity,
                _atom_graph_signature(
                    atom,
                    include_radical_electrons=include_radical_electrons,
                ),
            )
            for identity, atom in zip(
                atom_identities,
                graph.GetAtoms(),  # type: ignore[no-untyped-call]
                strict=True,
            )
        )
    )
    bond_signature: list[tuple[tuple[int, int], str, bool]] = []
    for bond in graph.GetBonds():  # type: ignore[no-untyped-call]
        begin = atom_identities[bond.GetBeginAtomIdx()]
        end = atom_identities[bond.GetEndAtomIdx()]
        # Dative bonds are directed: donor and acceptor order is part of the
        # graph. All other supported bond types are undirected.
        edge = (
            (begin, end)
            if bond.GetBondType() is Chem.BondType.DATIVE
            else tuple(sorted((begin, end)))
        )
        bond_signature.append((edge, str(bond.GetBondType()), bool(bond.GetIsAromatic())))
    return atom_signature, tuple(sorted(bond_signature))


def _validate_smiles_round_trip(
    source: Chem.Mol,
    smiles: str,
    output_order: list[int] | None,
    *,
    preserve_atom_maps: bool,
    retain_atom_maps: bool,
    isomeric_smiles: bool,
) -> dict[frozenset[int], DoubleBondStereoSignature]:
    """Validate the exact output text against its source graph and E/Z state."""

    parser: Any = Chem.SmilesParserParams()
    parser.removeHs = False
    # Unsanitized MolGR graphs can be valid serialization inputs even when
    # RDKit cannot assign valence. Bond directions still encode SMILES E/Z;
    # materialize those annotations explicitly after parsing.
    parser.sanitize = False
    parsed = Chem.MolFromSmiles(smiles, parser)
    if parsed is None:
        raise ValueError("RDKit could not parse the generated SMILES")
    try:
        Chem.SetBondStereoFromDirections(parsed)
    except (RuntimeError, ValueError) as error:
        raise ValueError("RDKit could not recover E/Z from the generated SMILES") from error

    if parsed.GetNumAtoms() != source.GetNumAtoms():
        raise ValueError("SMILES round trip changed the atom count")
    if preserve_atom_maps:
        source_identities = _stereo_identity_numbers(source, preserve_atom_maps=True)
        parsed_identities = _stereo_identity_numbers(parsed, preserve_atom_maps=True)
    else:
        if output_order is None or len(output_order) != parsed.GetNumAtoms():
            raise ValueError("RDKit did not report the generated SMILES atom order")
        source_identities = list(range(1, source.GetNumAtoms() + 1))
        parsed_identities = [source_index + 1 for source_index in output_order]
        if retain_atom_maps:
            expected_maps = [
                source.GetAtomWithIdx(source_index).GetAtomMapNum() for source_index in output_order
            ]
            observed_maps = [
                atom.GetAtomMapNum()
                for atom in parsed.GetAtoms()  # type: ignore[no-untyped-call]
            ]
            if observed_maps != expected_maps:
                raise ValueError("SMILES round trip changed atom-map labels")

    if _molecular_graph_signature(
        source,
        source_identities,
        include_radical_electrons=False,
    ) != _molecular_graph_signature(
        parsed,
        parsed_identities,
        include_radical_electrons=False,
    ):
        raise ValueError("SMILES round trip changed the source molecular graph")

    # Metal spin is a MolGR atom property, not information encoded by plain
    # SMILES. RDKit can infer a different bracket-atom radical count on parse;
    # that must neither reject a valid stereo projection nor replace MolGR's
    # metal state. Ordinary atoms still require an exact radical round trip.
    metal_spin_identities = {
        identity
        for identity, atom in zip(source_identities, source.GetAtoms(), strict=True)
        if atom.HasProp(METAL_UNPAIRED_ELECTRONS_PROP) or atom.HasProp(RADICAL_ELECTRONS_PROP)
    }
    for atom in source.GetAtoms():
        if atom.HasProp(RADICAL_ELECTRONS_PROP) and (
            atom.GetIntProp(RADICAL_ELECTRONS_PROP) != atom.GetNumRadicalElectrons()
        ):
            raise ValueError("source radical annotation disagrees with the MOL")
    source_radicals = {
        identity: int(atom.GetNumRadicalElectrons())
        for identity, atom in zip(
            source_identities,
            source.GetAtoms(),  # type: ignore[no-untyped-call]
            strict=True,
        )
        if identity not in metal_spin_identities
    }
    parsed_radicals = {
        identity: int(atom.GetNumRadicalElectrons())
        for identity, atom in zip(
            parsed_identities,
            parsed.GetAtoms(),  # type: ignore[no-untyped-call]
            strict=True,
        )
        if identity not in metal_spin_identities
    }
    if source_radicals != parsed_radicals:
        # RDKit's sanitize=False parser deliberately does not infer radical
        # electrons from bracket-atom valence. Check the serialized notation
        # with its normal parser on a new molecule, leaving the trusted source
        # and the unsanitized round-trip graph untouched. Some MolGR fallback
        # graphs are not chemically sanitizable; in that case the exact graph
        # and stereo checks above remain authoritative, and the binary source
        # molecule retains MolGR's radical assignments.
        sanitized_parser: Any = Chem.SmilesParserParams()
        sanitized_parser.removeHs = False
        try:
            sanitized = Chem.MolFromSmiles(smiles, sanitized_parser)
        except (RuntimeError, ValueError):
            sanitized = None
        if sanitized is not None and sanitized.GetNumAtoms() == len(parsed_identities):
            sanitized_radicals = {
                identity: int(atom.GetNumRadicalElectrons())
                for identity, atom in zip(
                    parsed_identities,
                    sanitized.GetAtoms(),  # type: ignore[no-untyped-call]
                    strict=True,
                )
                if identity not in metal_spin_identities
            }
            if source_radicals != sanitized_radicals:
                raise ValueError("SMILES round trip changed radical-electron assignments")

    expected_stereo_mol = Chem.Mol(source)
    serialized_stereo_mol = Chem.Mol(parsed)
    for atom, identity in zip(
        expected_stereo_mol.GetAtoms(),  # type: ignore[no-untyped-call]
        source_identities,
        strict=True,
    ):
        atom.SetAtomMapNum(identity)
    for atom, identity in zip(
        serialized_stereo_mol.GetAtoms(),  # type: ignore[no-untyped-call]
        parsed_identities,
        strict=True,
    ):
        atom.SetAtomMapNum(identity)
    expected_stereo = (
        _e_z_stereo_signature(expected_stereo_mol, preserve_atom_maps=True)
        if isomeric_smiles
        else {}
    )
    serialized_stereo = (
        _e_z_stereo_signature(serialized_stereo_mol, preserve_atom_maps=True)
        if isomeric_smiles
        else {}
    )
    if not _stereo_signatures_match(serialized_stereo, expected_stereo):
        raise ValueError("SMILES round trip changed a source E/Z control-atom relationship")
    return serialized_stereo


def _stereo_root_candidates(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool,
) -> list[int]:
    """Return a bounded, deterministic set of roots near assigned E/Z bonds."""

    candidates: set[int] = set()
    for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
        if bond.GetStereo() not in _DOUBLE_BOND_E_Z_STEREO:
            continue
        candidates.update((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
        candidates.update(int(index) for index in bond.GetStereoAtoms())
    if not candidates:
        return []

    if preserve_atom_maps:

        def order_key(index: int) -> tuple[int, int]:
            return mol.GetAtomWithIdx(index).GetAtomMapNum(), index
    else:
        try:
            ranks = tuple(
                int(rank)
                for rank in Chem.CanonicalRankAtoms(
                    mol,
                    breakTies=True,
                    includeChirality=False,
                    includeIsotopes=True,
                )
            )
        except (RuntimeError, ValueError):
            ranks = tuple(range(mol.GetNumAtoms()))

        def order_key(index: int) -> tuple[int, int]:
            return ranks[index], index

    return sorted(candidates, key=order_key)[:_MAX_STEREO_SMILES_ROOT_CANDIDATES]


def _deterministic_smiles_traversal(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool,
) -> Chem.Mol:
    """Renumber a fallback traversal so re-parsing yields the same writer order."""

    atom_count = mol.GetNumAtoms()
    if preserve_atom_maps:
        _stereo_identity_numbers(mol, preserve_atom_maps=True)
        order = sorted(
            range(atom_count),
            key=lambda index: mol.GetAtomWithIdx(index).GetAtomMapNum(),
        )
    else:
        try:
            ranks = tuple(
                int(rank)
                for rank in Chem.CanonicalRankAtoms(
                    mol,
                    breakTies=True,
                    includeChirality=False,
                    includeIsotopes=True,
                )
            )
            order = sorted(range(atom_count), key=lambda index: (ranks[index], index))
        except (RuntimeError, ValueError):
            order = list(range(atom_count))
    traversal = _renumber_atoms_preserving_stereochemistry(mol, order)
    _clear_smiles_output_order(traversal)
    if any(
        bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO
        for bond in traversal.GetBonds()  # type: ignore[no-untyped-call]
    ):
        traversal = project_serializable_double_bond_stereochemistry(
            traversal,
            preserve_atom_maps=preserve_atom_maps,
        )
    return traversal


def _restore_tree_bond_directions(
    mol: Chem.Mol, smiles: str, output_order: list[int] | None
) -> str:
    """Restore redundant tree-edge slashes omitted by the RDKit writer.

    Coordination adjacent to conjugated double bonds can make the writer
    incorrectly regard a shared single-bond direction as redundant. Use the
    writer's own atom order, never graph matching or inferred stereochemistry.
    Ring-closure syntax is left untouched. The caller must validate the result.
    """
    if output_order is None:
        return smiles
    tokens = re.findall(r"\[[^\]]+\]|->|<-|%\d{2}|\d|[().=#:~/\\+-]", smiles)
    if "".join(tokens) != smiles:
        return smiles
    result: list[str] = []
    branches: list[int | None] = []
    current: int | None = None
    atom_index = 0
    for token in tokens:
        if token.startswith("["):
            if atom_index >= len(output_order):
                return smiles
            following = output_order[atom_index]
            atom_index += 1
            if current is not None:
                bond = mol.GetBondBetweenAtoms(current, following)
                if bond is not None and bond.GetBondType() == Chem.BondType.SINGLE:
                    direction = bond.GetBondDir()
                    if direction in (Chem.BondDir.ENDUPRIGHT, Chem.BondDir.ENDDOWNRIGHT):
                        forward = direction == Chem.BondDir.ENDUPRIGHT
                        if bond.GetBeginAtomIdx() != current:
                            forward = not forward
                        slash = "/" if forward else "\\"
                        if result and result[-1] in ("/", "\\", "-"):
                            result[-1] = slash
                        else:
                            result.append(slash)
            current = following
        elif token == "(":
            branches.append(current)
        elif token == ")":
            if not branches:
                return smiles
            current = branches.pop()
        elif token == ".":
            current = None
        result.append(token)
    return "".join(result) if atom_index == len(output_order) else smiles


def _serialize_molecule_smiles_once(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
    retain_atom_maps: bool = False,
    isomeric_smiles: bool = True,
    all_hs_explicit: bool = True,
) -> str:
    """Write and validate one exact SMILES candidate.

    Canonical output remains the fast path and preserves existing identities
    for ordinary molecules. If canonical traversal changes trusted E/Z, try a
    bounded set of rooted traversals around stereo bonds and their control
    atoms. A candidate is returned only after its graph and complete mapped
    E/Z control-atom signature survive parsing.
    """

    projected = Chem.Mol(mol)
    _clear_smiles_output_order(projected)
    retain_atom_maps = retain_atom_maps or preserve_atom_maps
    if preserve_atom_maps:
        _stereo_identity_numbers(projected, preserve_atom_maps=True)
    elif not retain_atom_maps:
        for atom in projected.GetAtoms():  # type: ignore[no-untyped-call]
            atom.SetAtomMapNum(0)

    if not isomeric_smiles:
        # RDKit's isomericSmiles=False suppresses isotope labels as well as
        # stereochemistry. This option controls stereo only; isotopes remain
        # part of molecular identity.
        Chem.RemoveStereochemistry(projected)
    elif any(
        bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO
        for bond in projected.GetBonds()  # type: ignore[no-untyped-call]
    ):
        projected = project_serializable_double_bond_stereochemistry(
            projected,
            preserve_atom_maps=preserve_atom_maps,
        )
    else:
        _clear_bond_directions(projected)

    def write_candidate(
        molecule: Chem.Mol,
        *,
        canonical: bool,
        root: int | None = None,
    ) -> tuple[str, list[int] | None]:
        candidate = Chem.Mol(molecule)
        _clear_smiles_output_order(candidate)
        params = Chem.SmilesWriteParams()
        params.canonical = canonical
        params.rootedAtAtom = -1 if root is None else root
        params.doIsomericSmiles = True
        params.allHsExplicit = all_hs_explicit
        # Stereo was normalized on the complete graph. The writer must not
        # run a second cleanup with different coordination/ring assumptions.
        params.cleanStereo = False
        smiles = Chem.MolToSmiles(candidate, params)
        return smiles, _smiles_output_atom_order(candidate)

    try:
        canonical_smiles, canonical_order = write_candidate(projected, canonical=True)
    except (RuntimeError, ValueError) as error:
        canonical_smiles = ""
        canonical_order = None
        canonical_error = f"{type(error).__name__}: {error}"
    else:
        canonical_error = ""
        try:
            _validate_smiles_round_trip(
                projected,
                canonical_smiles,
                canonical_order,
                preserve_atom_maps=preserve_atom_maps,
                retain_atom_maps=retain_atom_maps,
                isomeric_smiles=isomeric_smiles,
            )
            return canonical_smiles
        except (RuntimeError, ValueError) as error:
            canonical_error = f"{type(error).__name__}: {error}"

    attempted_roots: list[int] = []
    if canonical_smiles:
        repaired_smiles = _restore_tree_bond_directions(
            projected, canonical_smiles, canonical_order
        )
        if repaired_smiles != canonical_smiles:
            try:
                _validate_smiles_round_trip(
                    projected,
                    repaired_smiles,
                    canonical_order,
                    preserve_atom_maps=preserve_atom_maps,
                    retain_atom_maps=retain_atom_maps,
                    isomeric_smiles=isomeric_smiles,
                )
                return repaired_smiles
            except (RuntimeError, ValueError):
                pass
    last_serialized: dict[frozenset[int], DoubleBondStereoSignature] = {}
    traversal = _deterministic_smiles_traversal(
        projected,
        preserve_atom_maps=preserve_atom_maps,
    )
    for root in _stereo_root_candidates(
        traversal,
        preserve_atom_maps=preserve_atom_maps,
    ):
        attempted_roots.append(
            traversal.GetAtomWithIdx(root).GetAtomMapNum() if preserve_atom_maps else root
        )
        try:
            smiles, output_order = write_candidate(traversal, canonical=False, root=root)
            last_serialized = _validate_smiles_round_trip(
                traversal,
                smiles,
                output_order,
                preserve_atom_maps=preserve_atom_maps,
                retain_atom_maps=retain_atom_maps,
                isomeric_smiles=isomeric_smiles,
            )
            return smiles
        except (RuntimeError, ValueError):
            continue

    expected_stereo = _e_z_stereo_signature(
        projected,
        preserve_atom_maps=preserve_atom_maps,
    )
    projection_error = _stereo_projection_failure(
        projected,
        "no SMILES traversal preserves the molecular graph and stereochemistry; "
        f"canonical validation failed: {canonical_error}",
        reason="no_lossless_smiles_traversal",
        expected={edge: signature[0] for edge, signature in expected_stereo.items()},
        serialized={edge: signature[0] for edge, signature in last_serialized.items()},
    )
    evidence = projection_error.evidence()
    evidence["canonical_attempt_error"] = canonical_error
    evidence["attempted_root_identities"] = attempted_roots
    evidence["root_candidate_limit"] = _MAX_STEREO_SMILES_ROOT_CANDIDATES
    raise StereoProjectionError(str(projection_error), evidence=evidence)


def serialize_molecule_smiles(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
    retain_atom_maps: bool = False,
    isomeric_smiles: bool = True,
    all_hs_explicit: bool = True,
) -> str:
    """Return a round-trip-validated, stable SMILES serialization.

    A safe rooted fallback can parse into a different but physically
    equivalent ``BondStereo`` control-atom pair. RDKit may then accept its
    canonical traversal even though it rejected the original molecule's. Run
    the exact writer/round-trip check to a fixed point (bounded to four passes).
    If equivalent valid strings form a cycle, select its lexicographically
    first member so subsequent calls return the same text.
    """

    has_source_e_z = isomeric_smiles and any(
        bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO
        for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
    )
    expected_stereo = {
        edge: signature[0]
        for edge, signature in _e_z_stereo_signature(
            mol,
            preserve_atom_maps=preserve_atom_maps,
        ).items()
    }
    current = _serialize_molecule_smiles_once(
        mol,
        preserve_atom_maps=preserve_atom_maps,
        retain_atom_maps=retain_atom_maps,
        isomeric_smiles=isomeric_smiles,
        all_hs_explicit=all_hs_explicit,
    )
    if not has_source_e_z:
        return current

    path = [current]
    path_positions = {current: 0}
    for _ in range(4):
        parser: Any = Chem.SmilesParserParams()
        parser.removeHs = False
        parser.sanitize = False
        parsed = Chem.MolFromSmiles(current, parser)
        if parsed is None:
            raise _stereo_projection_failure(
                mol,
                "generated SMILES could not be parsed during stability validation",
                reason="smiles_stability_parse_failed",
                expected=expected_stereo,
            )
        try:
            Chem.SetBondStereoFromDirections(parsed)
        except (RuntimeError, ValueError) as error:
            raise _stereo_projection_failure(
                mol,
                "generated SMILES directions could not restore E/Z during stability validation",
                reason="smiles_stability_stereo_recovery_failed",
                expected=expected_stereo,
            ) from error
        if not any(
            bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO
            for bond in parsed.GetBonds()  # type: ignore[no-untyped-call]
        ):
            raise _stereo_projection_failure(
                mol,
                "generated SMILES did not recover its assigned E/Z state",
                reason="smiles_stability_lost_e_z",
                expected=expected_stereo,
            )

        rewritten = _serialize_molecule_smiles_once(
            parsed,
            preserve_atom_maps=preserve_atom_maps,
            retain_atom_maps=retain_atom_maps,
            isomeric_smiles=isomeric_smiles,
            all_hs_explicit=all_hs_explicit,
        )
        if rewritten == current:
            return current
        cycle_start = path_positions.get(rewritten)
        if cycle_start is not None:
            # Every string in this cycle has already passed the exact graph
            # and physical E/Z round-trip checks in _serialize_molecule_smiles_once.
            # Pick one deterministic representative instead of rejecting a
            # valid molecule because RDKit alternates equivalent traversals.
            return min(path[cycle_start:])
        path_positions[rewritten] = len(path)
        path.append(rewritten)
        current = rewritten

    raise _stereo_projection_failure(
        mol,
        "SMILES serialization did not reach a stable round-trip representation",
        reason="smiles_stability_pass_limit",
        expected=expected_stereo,
    )


def _flip_e_z_stereo(stereo: Chem.BondStereo) -> Chem.BondStereo:
    """Flip E/Z when exactly one endpoint's control substituent changes."""

    return Chem.BondStereo.STEREOZ if stereo is Chem.BondStereo.STEREOE else Chem.BondStereo.STEREOE


def _stereo_signatures_match(
    serialized: dict[frozenset[int], DoubleBondStereoSignature],
    expected: dict[frozenset[int], DoubleBondStereoSignature],
) -> bool:
    """Compare physical double-bond geometry, not only edge-level E/Z."""

    if set(serialized) != set(expected):
        return False
    for edge, (expected_stereo, expected_pair, expected_order) in expected.items():
        observed_stereo, observed_pair, observed_order = serialized[edge]
        if -1 in expected_pair or -1 in observed_pair:
            return False
        if observed_order == expected_order:
            source_pair_in_observed_order = expected_pair
        elif observed_order == (expected_order[1], expected_order[0]):
            source_pair_in_observed_order = (expected_pair[1], expected_pair[0])
        else:
            return False
        changes_one_side = (observed_pair[0] != source_pair_in_observed_order[0]) ^ (
            observed_pair[1] != source_pair_in_observed_order[1]
        )
        effective_stereo = (
            _flip_e_z_stereo(observed_stereo) if changes_one_side else observed_stereo
        )
        if effective_stereo is not expected_stereo:
            return False
    return True


def _canonical_isomeric_smiles_signature(smiles: str | None) -> str | None:
    """Return an atom-order-independent signature for a serialized molecule.

    A mapped atom-index comparison is useful for detecting a missing marker,
    but it is too strict for a graph with symmetric substituents.  RDKit may
    also choose a different valid stereo-atom pair after an atom-order
    projection.  Canonical isomeric SMILES lets RDKit perform that equivalence
    check instead of treating either representation as a stereo loss.
    """

    if smiles is None:
        return None
    parser: Any = Chem.SmilesParserParams()
    parser.removeHs = False
    for sanitize in (True, False):
        parser.sanitize = sanitize
        try:
            molecule = Chem.MolFromSmiles(smiles, parser)
            if molecule is None:
                continue
            for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
                # Atom maps identify source atoms, not molecular identity.
                atom.SetAtomMapNum(0)
            return serialize_molecule_smiles(molecule, all_hs_explicit=True)
        except Exception:
            continue
    return None


def _solve_double_bond_direction_constraints(
    mol: Chem.Mol,
    stereo_bonds: list[Chem.Bond],
    *,
    preserve_atom_maps: bool = False,
) -> bool:
    """Set writer directions while respecting bonds shared by two E/Z bonds.

    RDKit's SMILES writer consumes neighboring ``BondDir`` values, not only
    the source bond's ``BondStereo`` cache.  Clear stale directions before
    asking RDKit to rebuild them from the frozen stereo-atom pairs; otherwise
    the writer can keep emitting an old traversal-specific projection.
    """

    if not stereo_bonds:
        return True
    try:
        # Slash directions cannot be emitted on a dative bond. Switch such a
        # control atom to the other covalent substituent and flip the relative
        # cis/trans bit, preserving physical geometry rather than its label.
        for bond in stereo_bonds:
            controls = list(bond.GetStereoAtoms())
            if len(controls) != 2:
                continue
            flipped = False
            ends = (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
            for side, endpoint in enumerate(ends):
                control_bond = mol.GetBondBetweenAtoms(endpoint, controls[side])
                if control_bond.GetBondType() != Chem.BondType.DATIVE:
                    continue
                alternatives = [
                    neighbor.GetIdx()
                    for neighbor in mol.GetAtomWithIdx(endpoint).GetNeighbors()
                    if neighbor.GetIdx() != ends[1 - side]
                    and mol.GetBondBetweenAtoms(endpoint, neighbor.GetIdx()).GetBondType()
                    != Chem.BondType.DATIVE
                ]
                if len(alternatives) == 1:
                    controls[side] = alternatives[0]
                    flipped = not flipped
            bond.SetStereoAtoms(*controls)
            if flipped:
                bond.SetStereo(_flip_e_z_stereo(bond.GetStereo()))
        # RDKit already has the correct global solver for conjugated systems.
        # It uses each BondStereo's stored stereo-atom pair and therefore knows
        # when two adjacent E/Z bonds share a directional single bond.  The
        # previous local XOR solver compared only edge-level E/Z and could
        # select the other substituent on one side of a shared bond.
        for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
            bond.SetBondDir(Chem.BondDir.NONE)
        Chem.SetDoubleBondNeighborDirections(mol)
    except (RuntimeError, ValueError):
        return False
    return True


def project_serializable_double_bond_stereochemistry(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
) -> Chem.Mol:
    """Project frozen source stereo into SMILES-writer direction metadata.

    ``BondStereo`` and ``Bond.GetStereoAtoms()`` are the source state.  The
    returned clone is the only object this function mutates: its ``BondDir``
    values are generated once for RDKit's SMILES writer.  No conformer,
    canonical SMILES, or previously serialized text is consulted.
    """

    projected = Chem.Mol(mol)
    for bond in projected.GetBonds():  # type: ignore[no-untyped-call]
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo())
        if stereo is not None:
            bond.SetStereo(stereo)
    stereo_bonds = [
        bond
        for bond in projected.GetBonds()  # type: ignore[no-untyped-call]
        if bond.GetStereo() in _DOUBLE_BOND_E_Z_STEREO
    ]
    if not stereo_bonds:
        _clear_bond_directions(projected)
        return projected
    if not _solve_double_bond_direction_constraints(
        projected,
        stereo_bonds,
        preserve_atom_maps=preserve_atom_maps,
    ):
        expected = _e_z_stereo_signature(
            projected,
            preserve_atom_maps=preserve_atom_maps,
        )
        raise _stereo_projection_failure(
            projected,
            "MolGR assigned E/Z stereochemistry without writer directions",
            reason="stereo_direction_projection_failed",
            expected={edge: signature[0] for edge, signature in expected.items()},
        )
    return projected


def validate_serializable_double_bond_stereochemistry(
    source: Chem.Mol,
    projected: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
) -> None:
    """Check only physical E/Z preservation across the final SMILES round trip.

    The expected state is the source molecule's assigned ``BondStereo`` plus
    ``Bond.GetStereoAtoms()``—ultimately the marker recovered from its 3D
    conformer. The projection must carry that physical state, and a shared
    safe serialization must preserve the complete graph and E/Z identity in
    the exact output text. RDKit may choose another valid substituent pair or
    the opposite E/Z spelling after a traversal change, so raw E/Z enums,
    slash directions, and string/traversal identity are not compared.

    This check does not infer, canonicalize, repair, or otherwise mutate either
    molecule. It also does not validate reaction chemistry, atom-map
    completeness, or endpoint correspondence; those are separate contracts.
    """

    if source.GetNumAtoms() != projected.GetNumAtoms():
        raise ValueError("stereo projection atom counts differ")
    normalized_source = Chem.Mol(source)
    for bond in normalized_source.GetBonds():  # type: ignore[no-untyped-call]
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo())
        if stereo is not None:
            bond.SetStereo(stereo)
    expected_signature = _e_z_stereo_signature(
        normalized_source,
        preserve_atom_maps=preserve_atom_maps,
    )
    projected_signature = _e_z_stereo_signature(projected, preserve_atom_maps=preserve_atom_maps)
    if not _stereo_signatures_match(projected_signature, expected_signature):
        expected = {edge: signature[0] for edge, signature in expected_signature.items()}
        serialized = {edge: signature[0] for edge, signature in projected_signature.items()}
        failure = _stereo_projection_failure(
            source,
            "SMILES projection changed the source E/Z control-atom relationship",
            reason="serialized_stereo_does_not_match_source",
            expected=expected,
            serialized=serialized,
        )
        evidence = failure.evidence()

        def signature_evidence(
            signatures: dict[frozenset[int], DoubleBondStereoSignature],
        ) -> list[dict[str, Any]]:
            return [
                {
                    "bond_atom_identities": sorted(int(identity) for identity in edge),
                    "stereo": str(stereo),
                    "control_atom_identities": [int(identity) for identity in control_atoms],
                    "ordered_bond_atom_identities": [int(identity) for identity in bond_order],
                }
                for edge, (stereo, control_atoms, bond_order) in sorted(
                    signatures.items(),
                    key=lambda item: tuple(sorted(item[0])),
                )
            ]

        evidence["expected_stereo_signatures"] = signature_evidence(expected_signature)
        evidence["projected_stereo_signatures"] = signature_evidence(projected_signature)
        raise StereoProjectionError(str(failure), evidence=evidence) from failure

    # Validate a complete exact string through the shared safe writer. Keep a
    # serializer's own diagnostic (for example, a graph round-trip failure)
    # instead of misreporting it as an E/Z control-atom mismatch.
    try:
        serialize_molecule_smiles(
            projected,
            preserve_atom_maps=preserve_atom_maps,
            retain_atom_maps=preserve_atom_maps,
            all_hs_explicit=True,
        )
    except StereoProjectionError:
        raise
    except (RuntimeError, ValueError) as error:
        expected = {edge: signature[0] for edge, signature in expected_signature.items()}
        serialized = {edge: signature[0] for edge, signature in projected_signature.items()}
        failure = _stereo_projection_failure(
            source,
            "SMILES serialization failed while validating E/Z preservation",
            reason="smiles_serialization_failed",
            expected=expected,
            serialized=serialized,
        )
        evidence = failure.evidence()
        evidence["serialization_error"] = f"{type(error).__name__}: {error}"
        raise StereoProjectionError(str(failure), evidence=evidence) from error


def ensure_serializable_double_bond_stereochemistry(
    mol: Chem.Mol,
    *,
    preserve_atom_maps: bool = False,
) -> Chem.Mol:
    """Create a writer projection and verify its physical E/Z round trip."""

    if not any(
        bond.GetStereo() in _SERIALIZED_DOUBLE_BOND_STEREO
        for bond in mol.GetBonds()  # type: ignore[no-untyped-call]
    ):
        projected = Chem.Mol(mol)
        _clear_bond_directions(projected)
        return projected
    projected = project_serializable_double_bond_stereochemistry(
        mol,
        preserve_atom_maps=preserve_atom_maps,
    )
    validate_serializable_double_bond_stereochemistry(
        mol,
        projected,
        preserve_atom_maps=preserve_atom_maps,
    )
    # Materialize the stable representation back into the normalized MOL.
    # Temporary atom identities recover the original order without any graph
    # matching; only stereo metadata is copied back, retaining coordinates,
    # electronic state, user atom maps, and molecule-specific properties.
    mapped = Chem.Mol(projected)
    for atom in mapped.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    smiles = serialize_molecule_smiles(mapped, preserve_atom_maps=True)
    parser: Any = Chem.SmilesParserParams()
    parser.removeHs = False
    parser.sanitize = False
    parsed = Chem.MolFromSmiles(smiles, parser)
    if parsed is None:
        raise ValueError("validated stereo SMILES could not be parsed")
    Chem.SetBondStereoFromDirections(parsed)
    by_identity = {atom.GetAtomMapNum(): atom.GetIdx() for atom in parsed.GetAtoms()}
    if set(by_identity) != set(range(1, projected.GetNumAtoms() + 1)):
        raise ValueError("validated stereo SMILES changed atom identities")
    ordered = Chem.RenumberAtoms(
        parsed, [by_identity[index + 1] for index in range(projected.GetNumAtoms())]
    )
    for bond in ordered.GetBonds():  # type: ignore[no-untyped-call]
        stereo = _LEGACY_DOUBLE_BOND_STEREO_TO_E_Z.get(bond.GetStereo())
        if stereo is not None:
            bond.SetStereo(stereo)
    _copy_bond_directions(ordered, projected)
    validate_serializable_double_bond_stereochemistry(
        mol, projected, preserve_atom_maps=preserve_atom_maps
    )
    return projected


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
    # lossless E/Z SMILES projection when one is available. If the projection
    # itself is not lossless, retain the trusted source graph and downgrade the
    # stereo status below instead of discarding the calculation frame.
    # ``normalize_topology_with_mapping`` already performs this preparation for
    # TS anchors.  Keep it here as well because ``normalize_molecule`` is also
    # a public geometry-creation entry point and may receive frame.rdmol
    # directly.
    if preserve_stereochemistry:
        mol = normalize_molgr_stereochemistry(mol)
    map_free_mol = _remove_atom_maps(mol)
    stereo_projection_error: StereoProjectionError | None = None
    source = Chem.Mol(map_free_mol)
    if preserve_stereochemistry:
        try:
            # Adopt the round-trip control atoms and writer directions on the
            # normalized graph, retaining its source electronic annotations.
            source_projection = ensure_serializable_double_bond_stereochemistry(map_free_mol)
            _copy_bond_directions(source_projection, source)
        except StereoProjectionError as error:
            evidence = error.evidence()
            evidence["failure_boundary"] = "source_molecule_projection"
            stereo_projection_error = StereoProjectionError(str(error), evidence=evidence)
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
    if preserve_stereochemistry and stereo_projection_error is None:
        # RDKit copies can retain the E/Z designation while dropping the
        # neighboring writer directions. Project the complete topology once
        # from its retained source stereo cache before fragment extraction.
        try:
            topology_projection = ensure_serializable_double_bond_stereochemistry(
                topology_mol,
            )
            _copy_bond_directions(topology_projection, topology_mol)
            serialize_molecule_smiles(topology_projection, all_hs_explicit=True)
        except StereoProjectionError as error:
            evidence = error.evidence()
            evidence["failure_boundary"] = "topology_source_order_projection"
            stereo_projection_error = StereoProjectionError(str(error), evidence=evidence)
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
    explicit_graph_smiles: str | None = None
    if not suspicious_fallback:
        try:
            explicit_graph_smiles = serialize_molecule_smiles(
                topology_mol,
                isomeric_smiles=stereo_projection_error is None,
                all_hs_explicit=True,
            )
        except (RuntimeError, ValueError) as error:
            evidence = (
                error.evidence()
                if isinstance(error, StereoProjectionError)
                else {"reason": "topology_smiles_round_trip_failed"}
            )
            evidence["failure_boundary"] = "topology_serialization"
            stereo_projection_error = StereoProjectionError(str(error), evidence=evidence)
            explicit_graph_smiles = serialize_molecule_smiles(
                topology_mol,
                isomeric_smiles=False,
                all_hs_explicit=True,
            )
    stable_topology_projection = False
    source_order_topology = topology_mol
    source_order_mapping = list(source_to_topology)
    if explicit_graph_smiles is not None and stereo_projection_error is None:
        smiles_atom_order = _canonical_smiles_atom_order(topology_mol)
        if smiles_atom_order is not None:
            source_order_smiles = explicit_graph_smiles
            topology_mol, source_to_topology = _apply_topology_projection(
                topology_mol,
                source_to_topology,
                smiles_atom_order,
            )
            if preserve_stereochemistry:
                try:
                    topology_projection = ensure_serializable_double_bond_stereochemistry(
                        topology_mol,
                    )
                    _copy_bond_directions(topology_projection, topology_mol)
                    explicit_graph_smiles = serialize_molecule_smiles(
                        topology_projection,
                        all_hs_explicit=True,
                    )
                except StereoProjectionError as error:
                    # ``RenumberAtoms`` can leave a valid source graph with a
                    # stale molecule-level stereo cache. Keep the pre-projection
                    # graph/mapping so coordinates and the assigned BondStereo
                    # values remain usable, and record the projection as
                    # ambiguous rather than rejecting the whole frame.
                    evidence = error.evidence()
                    evidence["failure_boundary"] = "canonical_atom_order_projection"
                    stereo_projection_error = StereoProjectionError(
                        str(error),
                        evidence=evidence,
                    )
                    topology_mol = source_order_topology
                    source_to_topology = source_order_mapping
                    # Reuse the already validated source-order text. A new
                    # canonical write here could repeat the same stereo flip
                    # that caused the projection rollback.
                    explicit_graph_smiles = source_order_smiles
                    _clear_smiles_output_order(topology_mol)
            if stereo_projection_error is None:
                stable_topology_projection = True
        else:
            explicit_graph_smiles = serialize_molecule_smiles(
                topology_mol,
                isomeric_smiles=stereo_projection_error is None,
                all_hs_explicit=True,
            )
            _clear_smiles_output_order(topology_mol)
    elif explicit_graph_smiles is not None:
        explicit_graph_smiles = serialize_molecule_smiles(
            topology_mol,
            isomeric_smiles=stereo_projection_error is None,
            all_hs_explicit=True,
        )
        _clear_smiles_output_order(topology_mol)
    if explicit_graph_smiles is None and trusted_molgr_graph:
        # Canonical ranking can fail for unusual but trusted MolGR valence
        # states.  A source-order explicit-H SMILES still preserves the graph
        # and its electronic annotations without attempting chemical repair.
        explicit_graph_smiles = serialize_molecule_smiles(
            topology_mol,
            isomeric_smiles=stereo_projection_error is None,
            all_hs_explicit=True,
        )
        _clear_smiles_output_order(topology_mol)
    standardized_graph_smiles: str | None = None
    if stable_topology_projection and explicit_graph_smiles is not None:
        # The first canonical write can still retain a direction-bearing
        # representation whose slash orientation depends on the source atom
        # order (notably for symmetric conjugated systems).  Parse the
        # map-free projection and canonicalize it once more before using it as
        # the persisted identity.  If that round trip cannot be completed,
        # keep the source-order projection and use the explicit fallback
        # identity below.
        standardized_graph_smiles = _canonical_isomeric_smiles_signature(explicit_graph_smiles)
        if standardized_graph_smiles is None:
            stable_topology_projection = False
            topology_mol = source_order_topology
            source_to_topology = source_order_mapping
            explicit_graph_smiles = serialize_molecule_smiles(
                topology_mol,
                isomeric_smiles=stereo_projection_error is None,
                all_hs_explicit=True,
            )
            _clear_smiles_output_order(topology_mol)
    # ``canonical_isomeric_smiles`` is retained as the public field name for
    # compatibility, but its value is now always the explicit-H projection.
    # This makes topology strings lossless with respect to explicit hydrogen
    # atoms and MolGR-provided radical state.
    canonical_isomeric_smiles = (
        standardized_graph_smiles
        if stable_topology_projection and standardized_graph_smiles is not None
        else explicit_graph_smiles
    )
    stereo_agnostic_graph_hash_value = stereo_agnostic_graph_hash(topology_mol)
    identity_schema_version = (
        TOPOLOGY_IDENTITY_VERSION
        if standardized_graph_smiles is not None and stable_topology_projection
        else (
            TOPOLOGY_SOURCE_ORDER_STEREO_IDENTITY_VERSION
            if stereo_projection_error is not None
            else "topology-source-order-identity-v1"
        )
    )
    graph_hash = _digest(
        {
            "schema_version": identity_schema_version,
            **metal_spin_identity(topology_mol),
            **(
                {"explicit_graph_smiles": standardized_graph_smiles}
                if standardized_graph_smiles is not None and stable_topology_projection
                else {
                    "source_order_graph": _source_order_graph_signature(
                        topology_mol,
                        include_stereo_metadata=stereo_projection_error is not None,
                    )
                }
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
        stereo_agnostic_graph_hash=stereo_agnostic_graph_hash_value,
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
            StereoStatus.AMBIGUOUS
            if stereo_projection_error is not None
            else (
                _preserved_stereo_status(topology_mol)
                if trusted_molgr_graph
                else (
                    _stereo_status(topology_mol)
                    if sanitization_status is TopologySanitizationStatus.SANITIZED
                    else StereoStatus.UNKNOWN
                )
            )
        ),
        is_stereo_abstraction_upstream=(
            (reconstruction_metadata or {}).get("is_stereo_abstraction_upstream") is True
        ),
        sanitization_status=sanitization_status,
        sanitization_error=sanitization_error,
    )
    derivation_metadata = {
        **(reconstruction_metadata or {}),
        "topology_sanitization_status": sanitization_status.value,
        "topology_sanitization_error": sanitization_error,
    }
    if stereo_projection_error is not None:
        derivation_metadata["stereo_projection"] = {
            "status": StereoStatus.AMBIGUOUS.value,
            "error_code": stereo_projection_error.error_code,
            "error_type": type(stereo_projection_error).__name__,
            "message": str(stereo_projection_error),
            "policy": "retain_frame_with_ambiguous_stereo",
            "evidence": stereo_projection_error.evidence(),
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
    # Remove reaction atom maps before MolGR stereo repair, hydrogen addition,
    # sanitization, or canonical topology projection.  Source-to-topology
    # correspondence is index-based and is reconstructed below, so this does
    # not discard the mapping information kept in provenance.
    source = _remove_atom_maps(mol)
    if trusted_source_graph:
        source = normalize_molgr_stereochemistry(source)
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
    "StereoProjectionError",
    "TOPOLOGY_IDENTITY_VERSION",
    "TOPOLOGY_DERIVATION_VERSION",
    "clear_inversion_labile_atom_chirality",
    "ensure_serializable_double_bond_stereochemistry",
    "infer_molgr_stereochemistry_from_3d",
    "normalize_molecule",
    "normalize_topology",
    "normalize_topology_with_mapping",
    "project_serializable_double_bond_stereochemistry",
    "serialize_molecule_smiles",
    "stereo_agnostic_graph_hash",
    "validate_serializable_double_bond_stereochemistry",
]
