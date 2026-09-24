"""Build and serve UniTS-style transition-state geometry datasets."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import secrets
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import numpy.typing as npt
from botocore.exceptions import BotoCoreError, ClientError
from rdkit import Chem
from sqlalchemy import delete, update
from sqlmodel import col, select

from tricycle_reaction_db.application.services.authorization import (
    AuthorizationService,
    ProjectAccessDeniedError,
    ProjectPermission,
)
from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    molecule_in_atom_map_order,
)
from tricycle_reaction_db.db.models import (
    Geometry,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    UnitsTsDatasetExportJob,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    MappedReactionNodeRole,
    UnitsDatasetExportJobStatus,
)
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

logger = logging.getLogger(__name__)

DOWNLOAD_TTL = timedelta(days=7)
JOB_LEASE = timedelta(minutes=30)
PAGE_SIZE = 128
MAX_REACTIVE_ATOMS = 20
MAX_RC_BONDS = 25
MAX_RC_ANGLES = 25
DATASET_FORMAT = "units-multidatasetv2-adapter-npy-v2"
LEGACY_DATASET_EXPORT_PREFIX = "dataset-exports/units-ts/"


def _is_legacy_units_ts_dataset_object_key(object_key: str) -> bool:
    if not object_key.startswith(LEGACY_DATASET_EXPORT_PREFIX):
        return False
    project_id, separator, filename = object_key[len(LEGACY_DATASET_EXPORT_PREFIX) :].partition("/")
    if not separator or "/" in filename or not filename.endswith(".npy"):
        return False
    try:
        UUID(project_id)
        UUID(filename.removesuffix(".npy"))
    except ValueError:
        return False
    return True


# These categorical vocabularies and feature orders mirror UniTS units/data.py;
# its MIT attribution and license are kept in licenses/UniTS-MIT.txt.
ATOM_LST = (
    "H",
    "C",
    "N",
    "O",
    "S",
    "F",
    "Si",
    "P",
    "Cl",
    "Br",
    "Mg",
    "Na",
    "Ca",
    "Fe",
    "As",
    "Al",
    "I",
    "B",
    "V",
    "K",
    "Tl",
    "Yb",
    "Sb",
    "Sn",
    "Ag",
    "Pd",
    "Co",
    "Se",
    "Ti",
    "Zn",
    "Li",
    "Ge",
    "Cu",
    "Au",
    "Ni",
    "Cd",
    "In",
    "Mn",
    "Zr",
    "Cr",
    "Pt",
    "Hg",
    "Pb",
    "W",
    "Ru",
    "Nb",
    "Re",
    "Te",
    "Rh",
    "Ta",
    "Tc",
    "Ba",
    "Bi",
    "Hf",
    "Mo",
    "U",
    "Sm",
    "Os",
    "Ir",
    "Ce",
    "Gd",
    "Ga",
    "Cs",
    "*",
    "unk",
)
ATOM_DICT = {symbol: index for index, symbol in enumerate(ATOM_LST)}
HYBRIDIZATION_LST = (
    Chem.rdchem.HybridizationType.SP,
    Chem.rdchem.HybridizationType.SP2,
    Chem.rdchem.HybridizationType.SP3,
    Chem.rdchem.HybridizationType.SP3D,
    Chem.rdchem.HybridizationType.SP3D2,
    Chem.rdchem.HybridizationType.UNSPECIFIED,
)
CHIRAL_TAG_LST = (
    Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW,
    Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CCW,
    Chem.rdchem.ChiralType.CHI_UNSPECIFIED,
)
VALENCE_LST = (0, 1, 2, 3, 4, 5, 6)
TOTAL_H_LST = (0, 1, 3, 4, 5)
BOND_TYPE_LST = (
    Chem.rdchem.BondType.SINGLE,
    Chem.rdchem.BondType.DOUBLE,
    Chem.rdchem.BondType.TRIPLE,
    Chem.rdchem.BondType.AROMATIC,
    Chem.rdchem.BondType.DATIVE,
    Chem.rdchem.BondType.UNSPECIFIED,
)
BOND_DIRECTION_LST = (
    Chem.rdchem.BondDir.NONE,
    Chem.rdchem.BondDir.ENDUPRIGHT,
    Chem.rdchem.BondDir.ENDDOWNRIGHT,
    Chem.rdchem.BondDir.BEGINDASH,
    Chem.rdchem.BondDir.BEGINWEDGE,
    Chem.rdchem.BondDir.EITHERDOUBLE,
)
BOND_STEREO_LST = (
    Chem.rdchem.BondStereo.STEREONONE,
    Chem.rdchem.BondStereo.STEREOE,
    Chem.rdchem.BondStereo.STEREOZ,
    Chem.rdchem.BondStereo.STEREOANY,
    Chem.rdchem.BondStereo.STEREOATROPCW,
    Chem.rdchem.BondStereo.STEREOATROPCCW,
)
ATOM_FEATURE_ORDER = (
    "atom_type",
    "degree_capped_at_10",
    "reaction_charge_plus_3",
    "hybridization",
    "chiral_tag",
    "aromatic",
    "total_valence",
    "total_hydrogens",
    "cip_r_s_none",
    "multiplicity_minus_1",
)
EDGE_FEATURE_ORDER = (
    "bond_type",
    "bond_direction",
    "bond_stereo",
    "in_ring",
    "conjugated",
)


class UnitsDatasetExportError(RuntimeError):
    """Base error raised by UniTS dataset export operations."""


class UnitsDatasetExportNotFoundError(UnitsDatasetExportError):
    pass


class UnitsDatasetExportPendingError(UnitsDatasetExportError):
    pass


class UnitsDatasetExportExpiredError(UnitsDatasetExportError):
    pass


class UnitsDatasetExportUnavailableError(UnitsDatasetExportError):
    pass


@dataclass(frozen=True, slots=True)
class UnitsDatasetDownload:
    job_id: UUID
    filename: str
    size_bytes: int
    content_sha256: str
    bucket: str
    object_key: str


@dataclass(frozen=True, slots=True)
class ClaimedUnitsDatasetExportJob:
    job_id: UUID
    project_id: UUID
    lease_id: UUID


def _status_payload(job: UnitsTsDatasetExportJob) -> dict[str, Any]:
    status = (
        UnitsDatasetExportJobStatus.EXPIRED
        if job.object_key is not None and _is_legacy_units_ts_dataset_object_key(job.object_key)
        else UnitsDatasetExportJobStatus(job.status)
    )
    return {
        "job_id": str(job.id),
        "project_id": str(job.project_id),
        "status": status.value,
        "format": DATASET_FORMAT,
        "sample_count": job.sample_count,
        "skipped_count": job.skipped_count,
        "skip_reasons": dict(job.skip_reasons),
        "error_message": job.error_message,
        "size_bytes": job.size_bytes,
        "content_sha256": job.content_sha256,
        "requested_at": job.requested_at.isoformat() if job.requested_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "expires_at": job.expires_at.isoformat(),
    }


def _bond_signature(
    molecule: Chem.Mol,
) -> dict[tuple[int, int], tuple[int, bool, int, int, int, int, int]]:
    signatures: dict[tuple[int, int], tuple[int, bool, int, int, int, int, int]] = {}
    for bond in molecule.GetBonds():  # type: ignore[no-untyped-call]
        left = bond.GetBeginAtom().GetAtomMapNum()
        right = bond.GetEndAtom().GetAtomMapNum()
        if left <= 0 or right <= 0:
            continue
        pair = (min(left, right), max(left, right))
        direction = bond.GetBondDir()
        direction_value = int(direction)
        direction_anchor = 0
        if direction in {Chem.rdchem.BondDir.ENDUPRIGHT, Chem.rdchem.BondDir.ENDDOWNRIGHT}:
            if left > right:
                direction_value = int(
                    Chem.rdchem.BondDir.ENDDOWNRIGHT
                    if direction == Chem.rdchem.BondDir.ENDUPRIGHT
                    else Chem.rdchem.BondDir.ENDUPRIGHT
                )
        elif direction in {
            Chem.rdchem.BondDir.BEGINWEDGE,
            Chem.rdchem.BondDir.BEGINDASH,
        }:
            direction_anchor = left

        stereo_atoms = tuple(
            molecule.GetAtomWithIdx(int(index)).GetAtomMapNum() for index in bond.GetStereoAtoms()
        )
        if len(stereo_atoms) == 2 and left > right:
            stereo_atoms = (stereo_atoms[1], stereo_atoms[0])
        stereo_left, stereo_right = stereo_atoms if len(stereo_atoms) == 2 else (0, 0)
        signatures[pair] = (
            int(BOND_TYPE_LST.index(bond.GetBondType())),
            bond.GetIsAromatic(),
            int(bond.GetStereo()),
            direction_value,
            direction_anchor,
            stereo_left,
            stereo_right,
        )
    return signatures


def _mapped_side_signatures(
    side_smiles: str,
) -> tuple[
    dict[tuple[int, int], tuple[int, bool, int, int, int, int, int]],
    dict[int, tuple[int, int, int, int, bool, str]],
]:
    bonds: dict[tuple[int, int], tuple[int, bool, int, int, int, int, int]] = {}
    atoms: dict[int, tuple[int, int, int, int, bool, str]] = {}
    if not side_smiles:
        return bonds, atoms
    for component in side_smiles.split("."):
        molecule = Chem.MolFromSmiles(component, sanitize=False)
        if molecule is None:
            raise ValueError("reaction side cannot be parsed by RDKit")
        molecule.UpdatePropertyCache(strict=False)
        Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
        for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
            map_number = atom.GetAtomMapNum()
            if map_number <= 0:
                continue
            signature = (
                atom.GetAtomicNum(),
                atom.GetFormalCharge(),
                atom.GetNumRadicalElectrons(),
                atom.GetIsotope(),
                atom.GetIsAromatic(),
                atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else "",
            )
            if map_number in atoms:
                raise ValueError("reaction side contains duplicate atom-map numbers")
            atoms[map_number] = signature
        bonds.update(_bond_signature(molecule))
    return bonds, atoms


def reaction_center_atom_maps(mapped_reaction_smiles: str) -> frozenset[int]:
    """Infer mapped reaction-center atoms from bond and atom changes."""

    sides = mapped_reaction_smiles.split(">")
    if len(sides) == 2:
        reactant_side, product_side = sides
    elif len(sides) == 3:
        reactant_side, _agents, product_side = sides
    else:
        raise ValueError("mapped reaction SMILES must contain one or two '>' separators")
    reactant_bonds, reactant_atoms = _mapped_side_signatures(reactant_side)
    product_bonds, product_atoms = _mapped_side_signatures(product_side)
    if not reactant_atoms or not product_atoms:
        raise ValueError("mapped reactant and product atoms are required")

    changed_maps: set[int] = set()
    for pair in reactant_bonds.keys() | product_bonds.keys():
        if reactant_bonds.get(pair) != product_bonds.get(pair):
            changed_maps.update(pair)
    for map_number in reactant_atoms.keys() | product_atoms.keys():
        if reactant_atoms.get(map_number) != product_atoms.get(map_number):
            changed_maps.add(map_number)
    return frozenset(changed_maps)


def _reaction_center_indices(
    mapped_reaction_smiles: str,
    geometry_atom_map_numbers: list[int],
) -> list[int]:
    center_maps = reaction_center_atom_maps(mapped_reaction_smiles)
    map_to_index = {map_number: index for index, map_number in enumerate(geometry_atom_map_numbers)}
    if len(map_to_index) != len(geometry_atom_map_numbers):
        raise ValueError("geometry atom mapping contains duplicate atom-map numbers")
    missing = center_maps - map_to_index.keys()
    if missing:
        raise ValueError("reaction-center atom maps are absent from this TS geometry")
    indices = sorted(map_to_index[map_number] for map_number in center_maps)
    if not indices:
        raise ValueError("mapped reaction has no inferable bond or atom changes")
    if len(indices) > MAX_REACTIVE_ATOMS:
        raise ValueError(f"reaction center exceeds UniTS limit of {MAX_REACTIVE_ATOMS} atoms")
    return indices


def _empty_rc_indices() -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    return (
        np.full((MAX_RC_BONDS, 2), -1, dtype=np.int64),
        np.full((MAX_RC_ANGLES, 3), -1, dtype=np.int64),
    )


def _make_reactive_geometry_indices(
    reactive_atoms: list[int],
) -> tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    rc_bonds, rc_angles = _empty_rc_indices()
    for row, combination in enumerate(itertools.combinations(reactive_atoms, 2)):
        if row >= MAX_RC_BONDS:
            break
        rc_bonds[row] = combination
    for row, angle_combination in enumerate(itertools.combinations(reactive_atoms, 3)):
        if row >= MAX_RC_ANGLES:
            break
        rc_angles[row, 0] = angle_combination[0]
        rc_angles[row, 1] = angle_combination[1]
        rc_angles[row, 2] = angle_combination[2]
    return rc_bonds, rc_angles


def make_units_ts_sample(
    *,
    geometry: Geometry,
    mapped_reaction: MappedReaction,
    geometry_atom_map_numbers: list[int],
    binding_id: UUID,
) -> dict[str, Any]:
    """Convert one database TS Geometry to UniTS atom/bond feature arrays."""

    charge = int(geometry.charge)
    multiplicity = int(geometry.multiplicity)
    if charge not in {-3, -2, -1, 0, 1, 2, 3}:
        raise ValueError("UniTS accepts total charge values from -3 through 3")
    if multiplicity not in {1, 2, 3, 4, 5}:
        raise ValueError("UniTS accepts spin multiplicities from 1 through 5")

    molecule, ordered_atom_map_numbers = molecule_in_atom_map_order(
        geometry.mol,
        geometry_atom_map_numbers,
    )
    if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
        raise ValueError("TS geometry must contain one 3D conformer")
    molecule.UpdatePropertyCache(strict=False)
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    reactive_atoms = _reaction_center_indices(
        mapped_reaction.mapped_reaction_smiles,
        ordered_atom_map_numbers,
    )

    atomic_numbers: list[int] = []
    atom_masses: list[float] = []
    atom_features: list[list[int]] = []
    for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
        symbol = atom.GetSymbol()
        atomic_numbers.append(int(atom.GetAtomicNum()))
        atom_masses.append(float(atom.GetMass()))
        cip = atom.GetProp("_CIPCode") if atom.HasProp("_CIPCode") else "None"
        atom_features.append(
            [
                ATOM_DICT.get(symbol, ATOM_DICT["unk"]),
                min(atom.GetDegree(), 10),
                charge + 3,
                HYBRIDIZATION_LST.index(atom.GetHybridization())
                if atom.GetHybridization() in HYBRIDIZATION_LST
                else 5,
                CHIRAL_TAG_LST.index(atom.GetChiralTag())
                if atom.GetChiralTag() in CHIRAL_TAG_LST
                else 2,
                int(atom.GetIsAromatic()),
                VALENCE_LST.index(atom.GetTotalValence())
                if atom.GetTotalValence() in VALENCE_LST
                else 6,
                TOTAL_H_LST.index(atom.GetTotalNumHs())
                if atom.GetTotalNumHs() in TOTAL_H_LST
                else 4,
                {"R": 0, "S": 1}.get(cip, 2),
                multiplicity - 1,
            ]
        )

    edges: list[tuple[int, int]] = []
    edge_features: list[list[int]] = []
    for bond in molecule.GetBonds():  # type: ignore[no-untyped-call]
        bond_type = BOND_TYPE_LST.index(bond.GetBondType())
        bond_direction = BOND_DIRECTION_LST.index(bond.GetBondDir())
        bond_stereo = BOND_STEREO_LST.index(bond.GetStereo())
        feature = [
            bond_type,
            bond_direction,
            bond_stereo,
            int(bond.IsInRing()),
            int(bond.GetIsConjugated()),
        ]
        left = int(bond.GetBeginAtomIdx())
        right = int(bond.GetEndAtomIdx())
        edges.extend(((left, right), (right, left)))
        edge_features.extend((feature, feature))

    atom_feature_array = np.asarray(atom_features, dtype=np.int64).reshape((-1, 10))
    atomic_number_array = np.asarray(atomic_numbers, dtype=np.int64)
    edge_index = np.asarray(edges, dtype=np.int64).T if edges else np.empty((2, 0), dtype=np.int64)
    edge_attr = (
        np.asarray(edge_features, dtype=np.int64).reshape((-1, 5))
        if edge_features
        else np.empty((0, 5), dtype=np.int64)
    )
    coordinates = np.asarray(molecule.GetConformer().GetPositions(), dtype=np.float32)
    if coordinates.shape != (molecule.GetNumAtoms(), 3) or not np.isfinite(coordinates).all():
        raise ValueError("TS geometry coordinates are invalid")
    padded_reactive_atoms = np.full(MAX_REACTIVE_ATOMS, -1, dtype=np.int64)
    padded_reactive_atoms[: len(reactive_atoms)] = reactive_atoms
    rc_bond_indices, rc_angle_indices = _make_reactive_geometry_indices(reactive_atoms)
    rc_bond_indices_batch = rc_bond_indices[np.newaxis, ...]
    rc_angle_indices_batch = rc_angle_indices[np.newaxis, ...]
    empty_new_edge_index = np.empty((2, 0), dtype=np.int64)
    empty_new_edge_attr = np.empty((0, 5), dtype=np.int64)
    fragments = [list(fragment) for fragment in Chem.GetMolFrags(molecule)]

    return {
        "geometry_binding_id": str(binding_id),
        "geometry_id": str(geometry.id),
        "mapped_reaction_id": str(mapped_reaction.id),
        "mapped_reaction_smiles": mapped_reaction.mapped_reaction_smiles,
        "geometry_atom_map_numbers": np.asarray(ordered_atom_map_numbers, dtype=np.int64),
        "charge": charge,
        "multiplicity": multiplicity,
        "atom_symbols": [atom.GetSymbol() for atom in molecule.GetAtoms()],  # type: ignore[no-untyped-call]
        "mol_atoms": atomic_number_array,
        "atom_mass": np.asarray(atom_masses, dtype=np.float64),
        "node_attr": atom_feature_array,
        # UniTS MultiDatasetV2 appends the atomic number to mol2graphinfo's
        # 10 categorical columns when constructing Data.x.
        "x": np.column_stack((atom_feature_array, atomic_number_array)).astype(
            np.int64, copy=False
        ),
        "mol_coords": coordinates,
        "frame_batch": np.zeros(molecule.GetNumAtoms(), dtype=np.int64),
        "sub_atom_num": np.asarray([1], dtype=np.int64),
        "edge_index": edge_index,
        "edge_attr": edge_attr,
        "new_edge_index": empty_new_edge_index,
        "new_edge_attr": empty_new_edge_attr,
        "reactive_atoms": np.asarray(reactive_atoms, dtype=np.int64),
        "reactive_atoms_padded": padded_reactive_atoms,
        "reactive_atoms_batch": padded_reactive_atoms[np.newaxis, ...],
        "rc_bond_indices": rc_bond_indices,
        "rc_angle_indices": rc_angle_indices,
        "rc_bond_indices_batch": rc_bond_indices_batch,
        "rc_angle_indices_batch": rc_angle_indices_batch,
        "blk_idxs": fragments,
        "feature_schema": DATASET_FORMAT,
    }


def _make_multidatasetv2_raw_record(
    sample: dict[str, Any],
    *,
    geometry: Geometry,
    mapped_reaction: MappedReaction,
    geometry_atom_map_numbers: list[int],
    binding_id: UUID,
) -> tuple[Any, ...]:
    """Build the six-field positional record consumed by UniTS MultiDatasetV2."""

    molecule, _ = molecule_in_atom_map_order(
        geometry.mol,
        geometry_atom_map_numbers,
    )
    molecule.SetProp("_tricycle_geometry_binding_id", str(binding_id))
    molecule.SetProp("_tricycle_geometry_id", str(geometry.id))
    molecule.SetProp("_tricycle_mapped_reaction_id", str(mapped_reaction.id))
    molecule.SetProp(
        "_tricycle_mapped_reaction_smiles",
        mapped_reaction.mapped_reaction_smiles,
    )
    graph_features = (
        sample["node_attr"],
        sample["edge_index"],
        sample["edge_attr"],
        sample["atom_mass"],
        sample["new_edge_index"],
        sample["new_edge_attr"],
    )
    return (
        sample["mol_atoms"],
        sample["mol_coords"],
        graph_features,
        molecule,
        tuple(tuple(fragment) for fragment in sample["blk_idxs"]),
        sample["reactive_atoms"],
    )


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class UnitsTsDatasetExportService:
    """API-facing queue, status, and capability-link operations."""

    @staticmethod
    async def create(project_id: UUID, user_id: UUID) -> dict[str, Any]:
        await AuthorizationService.require_project_permission(
            user_id,
            project_id,
            ProjectPermission.ARTIFACT_DOWNLOAD,
        )
        token = secrets.token_urlsafe(32)
        job = UnitsTsDatasetExportJob(
            project_id=project_id,
            requested_by_user_id=user_id,
            download_token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
            expires_at=datetime.now(UTC) + DOWNLOAD_TTL,
        )
        async with session_factory() as session:
            session.add(job)
            await session.commit()
            await session.refresh(job)
        result = _status_payload(job)
        result.update(
            {
                "status_url_path": f"/api/units-ts-datasets/{job.id}",
                "download_url_path": f"/api/units-ts-datasets/download/{token}",
            }
        )
        return result

    @staticmethod
    async def status(job_id: UUID, user_id: UUID) -> dict[str, Any]:
        async with session_factory() as session:
            job = await session.get(UnitsTsDatasetExportJob, job_id)
        if job is None:
            raise UnitsDatasetExportNotFoundError("dataset export job not found")
        try:
            await AuthorizationService.require_project_permission(
                user_id,
                job.project_id,
                ProjectPermission.ARTIFACT_DOWNLOAD,
            )
        except ProjectAccessDeniedError as error:
            raise UnitsDatasetExportNotFoundError("dataset export job not found") from error
        return _status_payload(job)

    @staticmethod
    async def download(token: str) -> UnitsDatasetDownload:
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        async with session_factory() as session:
            job = (
                await session.exec(
                    select(UnitsTsDatasetExportJob).where(
                        col(UnitsTsDatasetExportJob.download_token_hash) == token_hash
                    )
                )
            ).first()
        if job is None:
            raise UnitsDatasetExportNotFoundError("dataset download link not found")
        if job.object_key is not None and _is_legacy_units_ts_dataset_object_key(job.object_key):
            raise UnitsDatasetExportExpiredError("dataset download link has been invalidated")
        job_status = UnitsDatasetExportJobStatus(job.status)
        if job.expires_at <= datetime.now(UTC) or job_status is UnitsDatasetExportJobStatus.EXPIRED:
            raise UnitsDatasetExportExpiredError("dataset download link has expired")
        if job_status in {
            UnitsDatasetExportJobStatus.PENDING,
            UnitsDatasetExportJobStatus.PROCESSING,
        }:
            raise UnitsDatasetExportPendingError("dataset is still being generated")
        if job_status is UnitsDatasetExportJobStatus.FAILED:
            raise UnitsDatasetExportUnavailableError(
                job.error_message or "dataset generation failed"
            )
        if (
            job_status is not UnitsDatasetExportJobStatus.COMPLETED
            or job.object_key is None
            or job.bucket is None
            or job.size_bytes is None
            or job.content_sha256 is None
            or job.id is None
        ):
            raise UnitsDatasetExportUnavailableError("dataset object is not available")

        download = UnitsDatasetDownload(
            job_id=job.id,
            filename=f"units_ts_dataset_{job.id}.npy",
            size_bytes=job.size_bytes,
            content_sha256=job.content_sha256,
            bucket=job.bucket,
            object_key=job.object_key,
        )

        def verify_object() -> None:
            settings = RustFSSettings().model_copy(update={"bucket": download.bucket})
            with RustFSObjectStore(settings) as store:
                metadata = store.head(download.object_key)
            if metadata.size != download.size_bytes or metadata.sha256 != download.content_sha256:
                raise UnitsDatasetExportUnavailableError(
                    "stored dataset metadata does not match the export job"
                )

        try:
            await asyncio.to_thread(verify_object)
        except (BotoCoreError, ClientError) as error:
            raise UnitsDatasetExportUnavailableError("dataset object is unavailable") from error
        return download

    @staticmethod
    def iter_download(download: UnitsDatasetDownload) -> Iterator[bytes]:
        settings = RustFSSettings().model_copy(update={"bucket": download.bucket})
        with RustFSObjectStore(settings) as store:
            yield from store.iter_bytes(download.object_key)


async def claim_units_ts_dataset_export_job() -> ClaimedUnitsDatasetExportJob | None:
    now = datetime.now(UTC)
    lease_id = uuid4()
    async with session_factory() as session:
        await session.exec(
            update(UnitsTsDatasetExportJob)
            .where(
                col(UnitsTsDatasetExportJob.status) == UnitsDatasetExportJobStatus.PROCESSING,
                (
                    col(UnitsTsDatasetExportJob.lease_expires_at).is_(None)
                    | (col(UnitsTsDatasetExportJob.lease_expires_at) <= now)
                ),
            )
            .values(
                status=UnitsDatasetExportJobStatus.PENDING,
                lease_id=None,
                lease_expires_at=None,
                available_at=now,
                updated_at=now,
            )
        )
        job = (
            await session.exec(
                select(UnitsTsDatasetExportJob)
                .where(
                    col(UnitsTsDatasetExportJob.status) == UnitsDatasetExportJobStatus.PENDING,
                    col(UnitsTsDatasetExportJob.available_at) <= now,
                    col(UnitsTsDatasetExportJob.expires_at) > now,
                )
                .order_by(
                    col(UnitsTsDatasetExportJob.requested_at),
                    col(UnitsTsDatasetExportJob.id),
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).first()
        if job is None or job.id is None:
            await session.commit()
            return None
        job.status = UnitsDatasetExportJobStatus.PROCESSING
        job.lease_id = lease_id
        job.lease_expires_at = now + JOB_LEASE
        job.attempt_count += 1
        job.updated_at = now
        session.add(job)
        await session.commit()
        return ClaimedUnitsDatasetExportJob(job.id, job.project_id, lease_id)


async def _save_job_progress(
    claimed: ClaimedUnitsDatasetExportJob,
    sample_count: int,
    skipped_count: int,
    skip_reasons: dict[str, int],
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.exec(
            update(UnitsTsDatasetExportJob)
            .where(
                col(UnitsTsDatasetExportJob.id) == claimed.job_id,
                col(UnitsTsDatasetExportJob.lease_id) == claimed.lease_id,
                col(UnitsTsDatasetExportJob.status) == UnitsDatasetExportJobStatus.PROCESSING,
            )
            .values(
                sample_count=sample_count,
                skipped_count=skipped_count,
                skip_reasons=skip_reasons,
                lease_expires_at=now + JOB_LEASE,
                updated_at=now,
            )
        )
        await session.commit()


async def _mark_job_failed(
    claimed: ClaimedUnitsDatasetExportJob,
    error: Exception,
    sample_count: int,
    skipped_count: int,
    skip_reasons: dict[str, int],
) -> None:
    async with session_factory() as session:
        await session.exec(
            update(UnitsTsDatasetExportJob)
            .where(
                col(UnitsTsDatasetExportJob.id) == claimed.job_id,
                col(UnitsTsDatasetExportJob.lease_id) == claimed.lease_id,
            )
            .values(
                status=UnitsDatasetExportJobStatus.FAILED,
                sample_count=sample_count,
                skipped_count=skipped_count,
                skip_reasons=skip_reasons,
                error_message=str(error)[:4_000],
                lease_id=None,
                lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _mark_job_completed(
    claimed: ClaimedUnitsDatasetExportJob,
    *,
    object_key: str,
    bucket: str,
    size_bytes: int,
    content_sha256: str,
    sample_count: int,
    skipped_count: int,
    skip_reasons: dict[str, int],
) -> bool:
    now = datetime.now(UTC)
    async with session_factory() as session:
        result = await session.exec(
            update(UnitsTsDatasetExportJob)
            .where(
                col(UnitsTsDatasetExportJob.id) == claimed.job_id,
                col(UnitsTsDatasetExportJob.lease_id) == claimed.lease_id,
                col(UnitsTsDatasetExportJob.status) == UnitsDatasetExportJobStatus.PROCESSING,
            )
            .values(
                status=UnitsDatasetExportJobStatus.COMPLETED,
                object_key=object_key,
                bucket=bucket,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
                sample_count=sample_count,
                skipped_count=skipped_count,
                skip_reasons=skip_reasons,
                error_message=None,
                lease_id=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) == 1


async def build_units_ts_dataset_file(
    claimed: ClaimedUnitsDatasetExportJob,
    destination: Path,
) -> tuple[int, int, dict[str, int]]:
    """Page through project TS geometry bindings and write one object-array NPY."""

    samples: list[tuple[Any, ...]] = []
    skip_reasons: Counter[str] = Counter()
    skipped_count = 0
    last_binding_id: UUID | None = None

    while True:
        statement = (
            select(
                MappedReactionNodeGeometry,
                MappedReaction,
                Geometry,
                MappedReactionNodeGeometryMapping,
            )
            .join(
                MappedReactionNode,
                col(MappedReactionNode.id)
                == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
            )
            .join(
                MappedReaction,
                col(MappedReaction.id) == col(MappedReactionNode.mapped_reaction_id),
            )
            .join(Geometry, col(Geometry.id) == col(MappedReactionNodeGeometry.geometry_id))
            .outerjoin(
                MappedReactionNodeGeometryMapping,
                col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
                == col(MappedReactionNodeGeometry.id),
            )
            .where(
                col(MappedReaction.project_id) == claimed.project_id,
                col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                col(Geometry.project_id) == claimed.project_id,
            )
            .order_by(col(MappedReactionNodeGeometry.id))
            .limit(PAGE_SIZE)
        )
        if last_binding_id is not None:
            statement = statement.where(col(MappedReactionNodeGeometry.id) > last_binding_id)
        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
        if not rows:
            break

        for binding, mapped_reaction, geometry, mapping in rows:
            if binding.id is None:
                skip_reasons["missing_geometry_binding_id"] += 1
                skipped_count += 1
                continue
            last_binding_id = binding.id
            if mapping is None:
                skip_reasons["missing_atom_mapping"] += 1
                skipped_count += 1
                continue
            if not mapping.verified:
                skip_reasons["unverified_atom_mapping"] += 1
                skipped_count += 1
                continue
            try:
                geometry_atom_map_numbers = list(mapping.geometry_atom_map_numbers)
                sample = make_units_ts_sample(
                    geometry=geometry,
                    mapped_reaction=mapped_reaction,
                    geometry_atom_map_numbers=geometry_atom_map_numbers,
                    binding_id=binding.id,
                )
                samples.append(
                    _make_multidatasetv2_raw_record(
                        sample,
                        geometry=geometry,
                        mapped_reaction=mapped_reaction,
                        geometry_atom_map_numbers=geometry_atom_map_numbers,
                        binding_id=binding.id,
                    )
                )
            except (IndexError, RuntimeError, ValueError) as error:
                reason = str(error).split(":", maxsplit=1)[0].strip() or "invalid_geometry"
                skip_reasons[reason[:160]] += 1
                skipped_count += 1

        await _save_job_progress(
            claimed,
            len(samples),
            skipped_count,
            dict(skip_reasons),
        )

    if not samples:
        if skipped_count:
            reasons = ", ".join(
                f"{reason}={count}" for reason, count in skip_reasons.most_common(5)
            )
            raise ValueError(f"no exportable TS geometries; skipped: {reasons}")
        raise ValueError("no transition-state geometry bindings found in this project")

    dataset = np.empty(len(samples), dtype=object)
    for index, record in enumerate(samples):
        dataset[index] = record
    with destination.open("wb") as output:
        np.save(output, dataset, allow_pickle=True)
    return len(samples), skipped_count, dict(skip_reasons)


def _numpy_json_value(value: object) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


async def iter_units_ts_dataset_jsonl(
    project_id: UUID,
    *,
    after_binding_id: UUID | None = None,
    max_records: int | None = None,
) -> AsyncIterator[bytes]:
    """Yield UniTS feature JSONL, optionally from a cursor with a record limit."""

    last_binding_id = after_binding_id
    emitted_records = 0
    while True:
        statement = (
            select(
                MappedReactionNodeGeometry,
                MappedReaction,
                Geometry,
                MappedReactionNodeGeometryMapping,
            )
            .join(
                MappedReactionNode,
                col(MappedReactionNode.id)
                == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
            )
            .join(
                MappedReaction,
                col(MappedReaction.id) == col(MappedReactionNode.mapped_reaction_id),
            )
            .join(Geometry, col(Geometry.id) == col(MappedReactionNodeGeometry.geometry_id))
            .join(
                MappedReactionNodeGeometryMapping,
                col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
                == col(MappedReactionNodeGeometry.id),
            )
            .where(
                col(MappedReaction.project_id) == project_id,
                col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
                col(Geometry.project_id) == project_id,
                col(MappedReactionNodeGeometryMapping.verified).is_(True),
            )
            .order_by(col(MappedReactionNodeGeometry.id))
            .limit(PAGE_SIZE)
        )
        if last_binding_id is not None:
            statement = statement.where(col(MappedReactionNodeGeometry.id) > last_binding_id)

        async with session_factory() as session:
            rows = (await session.exec(statement)).all()
        if not rows:
            break

        for binding, mapped_reaction, geometry, mapping in rows:
            if binding.id is None:
                continue
            last_binding_id = binding.id
            try:
                sample = make_units_ts_sample(
                    geometry=geometry,
                    mapped_reaction=mapped_reaction,
                    geometry_atom_map_numbers=list(mapping.geometry_atom_map_numbers),
                    binding_id=binding.id,
                )
            except (IndexError, RuntimeError, ValueError) as error:
                logger.warning(
                    "Skipping UniTS JSONL sample for geometry binding %s: %s",
                    binding.id,
                    error,
                )
                continue
            sample["feature_schema"] = "units-ts-feature-record-v2"
            line = json.dumps(sample, default=_numpy_json_value, separators=(",", ":"))
            yield (line + "\n").encode("utf-8")
            emitted_records += 1
            if max_records is not None and emitted_records >= max_records:
                return


async def process_units_ts_dataset_export_job(
    claimed: ClaimedUnitsDatasetExportJob,
) -> None:
    """Build and upload one durable job; called only by the dedicated worker."""

    sample_count = 0
    skipped_count = 0
    skip_reasons: dict[str, int] = {}
    object_key = (
        f"dataset-exports/units-ts/{claimed.project_id}/{claimed.job_id}/{claimed.lease_id}.npy"
    )
    settings = RustFSSettings()
    try:
        with tempfile.TemporaryDirectory(prefix="units-ts-export-") as directory:
            path = Path(directory) / "units_ts_dataset.npy"
            sample_count, skipped_count, skip_reasons = await build_units_ts_dataset_file(
                claimed,
                path,
            )
            content_sha256, size_bytes = await asyncio.to_thread(_sha256_file, path)
            metadata = await asyncio.to_thread(
                _put_export_file,
                settings,
                object_key,
                path,
                content_sha256,
                size_bytes,
                str(claimed.job_id),
            )
        completed = await _mark_job_completed(
            claimed,
            object_key=object_key,
            bucket=metadata.bucket,
            size_bytes=metadata.size,
            content_sha256=content_sha256,
            sample_count=sample_count,
            skipped_count=skipped_count,
            skip_reasons=skip_reasons,
        )
        if not completed:
            storage_settings = settings.model_copy(update={"bucket": metadata.bucket})
            await asyncio.to_thread(
                _delete_export_object,
                storage_settings,
                object_key,
            )
    except Exception as error:
        logger.exception("UniTS dataset export failed job_id=%s", claimed.job_id)
        await _mark_job_failed(
            claimed,
            error,
            sample_count,
            skipped_count,
            skip_reasons,
        )


def _list_legacy_units_ts_dataset_objects(
    settings: RustFSSettings,
) -> list[tuple[str, str]]:
    with RustFSObjectStore(settings) as store:
        return [
            (item.bucket, item.key)
            for item in store.iter_objects(prefix=LEGACY_DATASET_EXPORT_PREFIX)
            if _is_legacy_units_ts_dataset_object_key(item.key)
        ]


async def purge_legacy_units_ts_dataset_exports() -> int:
    """Revoke and remove exports written in the obsolete flat-key format."""

    now = datetime.now(UTC)
    settings = RustFSSettings()
    async with session_factory() as session:
        jobs = (
            await session.exec(
                select(UnitsTsDatasetExportJob).where(
                    col(UnitsTsDatasetExportJob.object_key).like(f"{LEGACY_DATASET_EXPORT_PREFIX}%")
                )
            )
        ).all()
        legacy_jobs = [
            job
            for job in jobs
            if job.object_key is not None and _is_legacy_units_ts_dataset_object_key(job.object_key)
        ]
        for job in legacy_jobs:
            job.status = UnitsDatasetExportJobStatus.EXPIRED
            job.expires_at = min(job.expires_at, now)
            job.updated_at = now
        if legacy_jobs:
            await session.commit()

    object_jobs: dict[tuple[str, str], list[UUID]] = {}
    for job in legacy_jobs:
        if job.object_key is None:
            continue
        bucket = job.bucket or settings.bucket
        if job.id is not None:
            object_jobs.setdefault((bucket, job.object_key), []).append(job.id)

    objects = set(object_jobs)
    try:
        objects.update(await asyncio.to_thread(_list_legacy_units_ts_dataset_objects, settings))
    except (BotoCoreError, ClientError, RuntimeError):
        logger.exception("failed to list legacy UniTS dataset objects")

    deleted_objects: set[tuple[str, str]] = set()
    for bucket, object_key in objects:
        bucket_settings = settings.model_copy(update={"bucket": bucket})
        try:
            await asyncio.to_thread(_delete_export_object, bucket_settings, object_key)
            deleted_objects.add((bucket, object_key))
        except (BotoCoreError, ClientError, RuntimeError):
            logger.exception(
                "failed to remove legacy UniTS dataset object bucket=%s key=%s",
                bucket,
                object_key,
            )

    deleted_job_ids = {
        job_id
        for object_reference in deleted_objects
        for job_id in object_jobs.get(object_reference, ())
    }
    if deleted_job_ids:
        async with session_factory() as session:
            await session.exec(
                delete(UnitsTsDatasetExportJob).where(
                    col(UnitsTsDatasetExportJob.id).in_(deleted_job_ids),
                    col(UnitsTsDatasetExportJob.status) == UnitsDatasetExportJobStatus.EXPIRED,
                )
            )
            await session.commit()
    return len(deleted_objects)


def _put_export_file(
    settings: RustFSSettings,
    object_key: str,
    path: Path,
    content_sha256: str,
    size_bytes: int,
    job_id: str,
) -> Any:
    with RustFSObjectStore(settings) as store:
        return store.put_file(
            key=object_key,
            path=path,
            content_sha256=content_sha256,
            size_bytes=size_bytes,
            content_type="application/x-npy",
            metadata={"export-job-id": job_id, "dataset-format": DATASET_FORMAT},
        )


async def expire_units_ts_dataset_exports() -> int:
    """Remove expired export objects; returns the number of jobs expired."""

    now = datetime.now(UTC)
    async with session_factory() as session:
        jobs = (
            await session.exec(
                select(UnitsTsDatasetExportJob)
                .where(
                    col(UnitsTsDatasetExportJob.status).in_(
                        (
                            UnitsDatasetExportJobStatus.PENDING,
                            UnitsDatasetExportJobStatus.COMPLETED,
                            UnitsDatasetExportJobStatus.FAILED,
                        )
                    ),
                    col(UnitsTsDatasetExportJob.expires_at) <= now,
                )
                .order_by(col(UnitsTsDatasetExportJob.expires_at))
                .limit(100)
            )
        ).all()
    if not jobs:
        return 0

    expired_ids: list[UUID] = []
    for job in jobs:
        if job.object_key and job.bucket:
            storage_settings = RustFSSettings().model_copy(update={"bucket": job.bucket})
            try:
                await asyncio.to_thread(_delete_export_object, storage_settings, job.object_key)
            except (BotoCoreError, ClientError):
                logger.exception("failed to remove expired UniTS dataset job_id=%s", job.id)
                continue
        if job.id is not None:
            expired_ids.append(job.id)
    if expired_ids:
        async with session_factory() as session:
            await session.exec(
                update(UnitsTsDatasetExportJob)
                .where(col(UnitsTsDatasetExportJob.id).in_(expired_ids))
                .values(
                    status=UnitsDatasetExportJobStatus.EXPIRED,
                    object_key=None,
                    bucket=None,
                    size_bytes=None,
                    content_sha256=None,
                    error_message=None,
                    updated_at=now,
                )
            )
            await session.commit()
    return len(expired_ids)


def _delete_export_object(settings: RustFSSettings, object_key: str) -> None:
    with RustFSObjectStore(settings) as store:
        store.delete(object_key)


__all__ = [
    "ClaimedUnitsDatasetExportJob",
    "UnitsDatasetDownload",
    "UnitsDatasetExportError",
    "UnitsDatasetExportExpiredError",
    "UnitsDatasetExportNotFoundError",
    "UnitsDatasetExportPendingError",
    "UnitsDatasetExportUnavailableError",
    "UnitsTsDatasetExportService",
    "build_units_ts_dataset_file",
    "claim_units_ts_dataset_export_job",
    "expire_units_ts_dataset_exports",
    "purge_legacy_units_ts_dataset_exports",
    "process_units_ts_dataset_export_job",
]
