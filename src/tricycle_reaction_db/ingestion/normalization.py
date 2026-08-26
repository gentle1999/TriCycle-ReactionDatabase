"""Canonicalize parsed molecular frames into immutable business records."""

import json
from collections import Counter
from hashlib import sha256
from typing import Any

import numpy as np
import numpy.typing as npt
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


def _clear_rdkit_properties(mol: Chem.Mol) -> None:
    for prop_name in list(mol.GetPropNames(includePrivate=True, includeComputed=True)):
        mol.ClearProp(prop_name)
    for atom in mol.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
        for prop_name in list(atom.GetPropNames(includePrivate=True, includeComputed=True)):
            atom.ClearProp(prop_name)
    for bond in mol.GetBonds():  # type: ignore[no-untyped-call]
        for prop_name in list(bond.GetPropNames(includePrivate=True, includeComputed=True)):
            bond.ClearProp(prop_name)


def _initialize_ring_info(mol: Chem.Mol) -> None:
    """Populate graph ring metadata without requiring chemical sanitization."""

    Chem.FastFindRings(mol)


def _canonical_topology(
    mol: Chem.Mol,
    *,
    skip_sanitization: bool = False,
) -> tuple[Chem.Mol, list[int], TopologySanitizationStatus, str | None]:
    source_topology = Chem.Mol(mol)
    source_topology.RemoveAllConformers()
    _clear_rdkit_properties(source_topology)
    atom_count = source_topology.GetNumAtoms()
    if atom_count == 0:
        raise ValueError("molecular topology must contain at least one atom")

    topology = Chem.Mol(source_topology)
    if skip_sanitization:
        # MolGR's OpenBabel fallback is explicitly untrusted.  In particular,
        # do not probe it with RDKit sanitization: valence repair can mutate the
        # graph and hides the charge/spin provenance we need to persist.
        canonical_order = list(range(atom_count))
        sanitization_status = TopologySanitizationStatus.FAILED
        sanitization_error = (
            "MolGR suspicious_fallback topology; RDKit sanitization was intentionally skipped "
            "(potential AtomValenceException)"
        )
    else:
        try:
            Chem.SanitizeMol(topology)
            Chem.AssignStereochemistry(topology, cleanIt=True, force=True)
            ranks = list(
                Chem.CanonicalRankAtoms(
                    topology,
                    breakTies=True,
                    includeChirality=True,
                    includeIsotopes=True,
                    includeAtomMaps=False,
                    includeChiralPresence=True,
                )
            )
            canonical_order = sorted(range(atom_count), key=ranks.__getitem__)
            sanitization_status = TopologySanitizationStatus.SANITIZED
            sanitization_error = None
        except Exception as error:
            # Keep the source-order connectivity graph. PostgreSQL RDKit can store
            # and substructure-search this binary Mol even when chemical
            # sanitization, descriptors, and Morgan fingerprints are unavailable.
            topology = source_topology
            canonical_order = list(range(atom_count))
            sanitization_status = TopologySanitizationStatus.FAILED
            sanitization_error = f"{type(error).__name__}: {error}"
        else:
            if any(
                atom.GetNumImplicitHs()
                for atom in topology.GetAtoms()  # type: ignore[no-untyped-call]
            ):
                raise ValueError(
                    "QM topology must contain every coordinate-bearing hydrogen explicitly"
                )

    source_to_topology = [0] * atom_count
    for topology_index, source_index in enumerate(canonical_order):
        source_to_topology[source_index] = topology_index

    canonical = Chem.RenumberAtoms(topology, canonical_order)
    canonical.RemoveAllConformers()
    if sanitization_status is TopologySanitizationStatus.FAILED:
        # OpenBabel fallback graphs can fail valence sanitization while still
        # carrying usable connectivity. PostgreSQL RDKit requires initialized
        # RingInfo even for those deliberately unsanitized molecules.
        _initialize_ring_info(canonical)
    return canonical, source_to_topology, sanitization_status, sanitization_error


def _graph_smiles(mol: Chem.Mol, *, all_hydrogens_explicit: bool) -> str | None:
    try:
        return Chem.MolToSmiles(
            mol,
            canonical=True,
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
    (
        topology_mol,
        source_to_topology,
        sanitization_status,
        sanitization_error,
    ) = _canonical_topology(mol, skip_sanitization=suspicious_fallback)
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

    explicit_graph_smiles = (
        None if suspicious_fallback else _graph_smiles(topology_mol, all_hydrogens_explicit=True)
    )
    if sanitization_status is TopologySanitizationStatus.SANITIZED:
        display_mol = Chem.RemoveHs(Chem.Mol(topology_mol))
        canonical_isomeric_smiles = _graph_smiles(
            display_mol,
            all_hydrogens_explicit=False,
        )
    else:
        canonical_isomeric_smiles = None
    identity_schema_version = (
        TOPOLOGY_IDENTITY_VERSION
        if explicit_graph_smiles is not None
        else "topology-source-order-identity-v1"
    )
    graph_hash = _digest(
        {
            "schema_version": identity_schema_version,
            **(
                {"explicit_graph_smiles": explicit_graph_smiles}
                if explicit_graph_smiles is not None
                else {"source_order_graph": _source_order_graph_signature(topology_mol)}
            ),
        }
    )
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
        radical_electron_count=sum(
            atom.GetNumRadicalElectrons()
            for atom in topology_mol.GetAtoms()  # type: ignore[no-untyped-call]
        ),
        fragment_count=len(Chem.GetMolFrags(topology_mol)),
        stereo_status=(
            _stereo_status(topology_mol)
            if sanitization_status is TopologySanitizationStatus.SANITIZED
            else StereoStatus.UNKNOWN
        ),
        sanitization_status=sanitization_status,
        sanitization_error=sanitization_error,
    )
    derivation_metadata = {
        **(reconstruction_metadata or {}),
        "topology_sanitization_status": sanitization_status.value,
        "topology_sanitization_error": sanitization_error,
    }
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

    source = Chem.Mol(mol)
    suspicious_fallback = (reconstruction_metadata or {}).get(
        "molgr_status"
    ) == "suspicious_fallback"
    if not suspicious_fallback:
        Chem.SanitizeMol(source)
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
) -> Chem.Mol:
    geometry_mol = Chem.Mol(mol)
    geometry_mol.RemoveAllConformers()
    _clear_rdkit_properties(geometry_mol)
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
            "distance_unit": "angstrom",
            "angular_unit": "degree",
        }
    )
    geometry = GeometryRecord(
        mol=_ordered_geometry_mol(
            topology.mol,
            geometry_coordinates,
        ),
        internal_coordinates=internal_coordinates,
        internal_coordinate_hash=internal_hash,
        geometry_hash=geometry_hash,
        canonicalization_version=GEOMETRY_CANONICALIZATION_VERSION,
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
    "normalize_molecule",
    "normalize_topology",
    "normalize_topology_with_mapping",
]
