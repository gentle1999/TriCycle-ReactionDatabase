"""Validated internal DTOs for normalized chemical records."""

from hashlib import sha256
from typing import Any
from uuid import UUID

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rdkit import Chem

from tricycle_reaction_db.domain.enums import (
    SimilarityMetric,
    StereoStatus,
    TopologySanitizationStatus,
)
from tricycle_reaction_db.domain.formulas import ELEMENT_COUNT_VECTOR_SIZE
from tricycle_reaction_db.domain.internal_coordinates import internal_coordinate_hash


class MolecularFormulaRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    hill_formula: str
    composition: list[dict[str, int]]
    composition_schema_version: str
    atom_count: int = Field(gt=0)
    composition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    element_count_vector: list[int] = Field(
        min_length=ELEMENT_COUNT_VECTOR_SIZE,
        max_length=ELEMENT_COUNT_VECTOR_SIZE,
    )

    @model_validator(mode="after")
    def validate_element_count_vector(self) -> "MolecularFormulaRecord":
        if any(count < 0 for count in self.element_count_vector):
            raise ValueError("element_count_vector must contain non-negative counts")
        if sum(self.element_count_vector) != self.atom_count:
            raise ValueError("element_count_vector counts must sum to atom_count")
        return self


class MolecularFormulaRangeQuery(BaseModel):
    """Per-element inclusive count bounds; null leaves one element unconstrained."""

    model_config = ConfigDict(frozen=True)

    minimum_counts: list[int | None] = Field(
        min_length=ELEMENT_COUNT_VECTOR_SIZE,
        max_length=ELEMENT_COUNT_VECTOR_SIZE,
    )
    maximum_counts: list[int | None] = Field(
        min_length=ELEMENT_COUNT_VECTOR_SIZE,
        max_length=ELEMENT_COUNT_VECTOR_SIZE,
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> "MolecularFormulaRangeQuery":
        constrained = False
        for minimum, maximum in zip(self.minimum_counts, self.maximum_counts, strict=True):
            if minimum is not None:
                constrained = True
                if minimum < 0:
                    raise ValueError("minimum_counts must be non-negative or null")
            if maximum is not None:
                constrained = True
                if maximum < 0:
                    raise ValueError("maximum_counts must be non-negative or null")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("minimum_counts cannot exceed maximum_counts")
        if not constrained:
            raise ValueError("formula search requires at least one constrained element")
        return self


class MolecularTopologySearchQuery(BaseModel):
    """Canonical identity and indexed RDKit topology predicates.

    Formula selectors are intentionally separate from the structure predicate so the
    query service can apply the indexed Formula -> Topology join before cartridge
    matching. ``exact_smiles`` is normalized to the persisted display-SMILES identity,
    while ``smarts`` uses the RDKit cartridge. At least one selector is required to
    prevent unbounded topology scans.
    """

    model_config = ConfigDict(frozen=True)

    topology_id: UUID | None = None
    formula_id: UUID | None = None
    formula_composition_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    formula_hill_formula: str | None = Field(default=None, min_length=1, max_length=512)
    exact_smiles: str | None = Field(default=None, min_length=1, max_length=16_384)
    mol_block: str | None = Field(default=None, min_length=1, max_length=65_536)
    similarity_smiles: str | None = Field(default=None, min_length=1, max_length=16_384)
    similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto
    minimum_similarity: float | None = Field(default=None, gt=0, le=1)
    smarts: str | None = Field(default=None, min_length=1, max_length=16_384)
    match_chirality: bool = False
    minimum_substructure_matches: int | None = Field(default=None, ge=1)
    unique_substructure_matches: bool = True
    formal_charge: int | None = None
    atom_count: int | None = Field(default=None, ge=1)
    heavy_atom_count: int | None = Field(default=None, ge=0)
    stereo_status: StereoStatus | None = None
    sanitization_status: TopologySanitizationStatus | None = None
    minimum_molecular_weight: float | None = Field(default=None, ge=0)
    maximum_molecular_weight: float | None = Field(default=None, ge=0)
    minimum_logp: float | None = None
    maximum_logp: float | None = None
    minimum_tpsa: float | None = Field(default=None, ge=0)
    maximum_tpsa: float | None = Field(default=None, ge=0)
    minimum_hba_count: int | None = Field(default=None, ge=0)
    maximum_hba_count: int | None = Field(default=None, ge=0)
    minimum_hbd_count: int | None = Field(default=None, ge=0)
    maximum_hbd_count: int | None = Field(default=None, ge=0)
    minimum_ring_count: int | None = Field(default=None, ge=0)
    maximum_ring_count: int | None = Field(default=None, ge=0)
    scaffold_smiles: str | None = Field(default=None, min_length=1, max_length=16_384)

    @field_validator("exact_smiles")
    @classmethod
    def normalize_exact_smiles(cls, value: str | None) -> str | None:
        """Match the canonical display identity used by topology normalization."""

        if value is None:
            return None
        molecule = Chem.MolFromSmiles(value)
        if molecule is None:
            raise ValueError("exact_smiles must be a valid SMILES string")
        return Chem.MolToSmiles(
            Chem.RemoveHs(Chem.Mol(molecule)),
            canonical=True,
            isomericSmiles=True,
        )

    @field_validator("mol_block")
    @classmethod
    def validate_mol_block(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if Chem.MolFromMolBlock(value, sanitize=True, removeHs=True, strictParsing=False) is None:
            raise ValueError("mol_block must be a valid MDL mol block")
        return value

    @field_validator("similarity_smiles")
    @classmethod
    def normalize_similarity_smiles(cls, value: str | None) -> str | None:
        """Use the same display identity when generating the query fingerprint."""

        if value is None:
            return None
        molecule = Chem.MolFromSmiles(value)
        if molecule is None:
            raise ValueError("similarity_smiles must be a valid SMILES string")
        return Chem.MolToSmiles(
            Chem.RemoveHs(Chem.Mol(molecule)),
            canonical=True,
            isomericSmiles=True,
        )

    @field_validator("scaffold_smiles")
    @classmethod
    def normalize_scaffold_smiles(cls, value: str | None) -> str | None:
        if value is None:
            return None
        molecule = Chem.MolFromSmiles(value)
        if molecule is None:
            raise ValueError("scaffold_smiles must be a valid SMILES string")
        return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)

    @model_validator(mode="after")
    def validate_search(self) -> "MolecularTopologySearchQuery":
        formula_selector_count = sum(
            selector is not None
            for selector in (
                self.formula_id,
                self.formula_composition_hash,
                self.formula_hill_formula,
            )
        )
        if formula_selector_count > 1:
            raise ValueError(
                "formula_id, formula_composition_hash, and formula_hill_formula conflict"
            )
        if self.exact_smiles is not None and self.mol_block is not None:
            raise ValueError("exact_smiles and mol_block conflict")
        if self.smarts is not None and Chem.MolFromSmarts(self.smarts) is None:
            raise ValueError("smarts must be a valid SMARTS pattern")
        if self.match_chirality and self.smarts is None:
            raise ValueError("match_chirality requires a SMARTS pattern")
        if self.minimum_substructure_matches is not None and self.smarts is None:
            raise ValueError("minimum_substructure_matches requires a SMARTS pattern")
        if self.minimum_similarity is not None and self.similarity_smiles is None:
            raise ValueError("minimum_similarity requires similarity_smiles")
        if (
            self.similarity_metric is not SimilarityMetric.tanimoto
            and self.similarity_smiles is None
        ):
            raise ValueError("similarity_metric requires similarity_smiles")
        for minimum, maximum, label in (
            (self.minimum_molecular_weight, self.maximum_molecular_weight, "molecular_weight"),
            (self.minimum_logp, self.maximum_logp, "logp"),
            (self.minimum_tpsa, self.maximum_tpsa, "tpsa"),
            (self.minimum_hba_count, self.maximum_hba_count, "hba_count"),
            (self.minimum_hbd_count, self.maximum_hbd_count, "hbd_count"),
            (self.minimum_ring_count, self.maximum_ring_count, "ring_count"),
        ):
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"minimum_{label} cannot exceed maximum_{label}")
        if not any(
            value is not None
            for value in (
                self.topology_id,
                self.formula_id,
                self.formula_composition_hash,
                self.formula_hill_formula,
                self.exact_smiles,
                self.mol_block,
                self.similarity_smiles,
                self.smarts,
                self.formal_charge,
                self.atom_count,
                self.heavy_atom_count,
                self.stereo_status,
                self.sanitization_status,
                self.minimum_molecular_weight,
                self.maximum_molecular_weight,
                self.minimum_logp,
                self.maximum_logp,
                self.minimum_tpsa,
                self.maximum_tpsa,
                self.minimum_hba_count,
                self.maximum_hba_count,
                self.minimum_hbd_count,
                self.maximum_hbd_count,
                self.minimum_ring_count,
                self.maximum_ring_count,
                self.scaffold_smiles,
            )
        ):
            raise ValueError("topology search requires at least one predicate")
        return self


class MolecularTopologyRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    mol: Chem.Mol
    canonical_isomeric_smiles: str | None = None
    graph_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_schema_version: str
    atom_count: int = Field(gt=0)
    heavy_atom_count: int = Field(ge=0)
    formal_charge: int
    radical_electron_count: int = Field(ge=0)
    fragment_count: int = Field(gt=0)
    stereo_status: StereoStatus
    sanitization_status: TopologySanitizationStatus = TopologySanitizationStatus.SANITIZED
    sanitization_error: str | None = None

    @model_validator(mode="after")
    def validate_graph_only_mol(self) -> "MolecularTopologyRecord":
        if self.mol.GetNumConformers() != 0:
            raise ValueError("MolecularTopology.mol must not contain conformers")
        if self.mol.GetNumAtoms() != self.atom_count:
            raise ValueError("MolecularTopology.mol atom count does not match atom_count")
        if any(
            atom.GetAtomMapNum() != 0
            for atom in self.mol.GetAtoms()  # type: ignore[no-untyped-call]
        ):
            raise ValueError("MolecularTopology.mol must not contain atom maps")
        if self.sanitization_status is TopologySanitizationStatus.SANITIZED:
            if self.sanitization_error is not None:
                raise ValueError("sanitized topology must not carry a sanitization error")
        elif not self.sanitization_error:
            raise ValueError("failed topology must carry a sanitization error")
        return self


class MolecularTopologyDerivationRecord(BaseModel):
    """Versioned evidence describing how one topology was obtained."""

    model_config = ConfigDict(frozen=True)

    reconstruction_method: str
    reconstruction_version: str
    reconstruction_metadata: dict[str, Any]
    provenance_schema_version: str
    provenance_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class NormalizedTopologyRecord(BaseModel):
    """Formula and graph identity that may exist without a calculated geometry."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    formula: MolecularFormulaRecord
    topology: MolecularTopologyRecord
    topology_derivation: MolecularTopologyDerivationRecord

    @model_validator(mode="after")
    def validate_atom_counts(self) -> "NormalizedTopologyRecord":
        if self.formula.atom_count != self.topology.atom_count:
            raise ValueError("Formula and Topology atom counts must match")
        return self


class GeometryRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    mol: Chem.Mol
    internal_coordinates: npt.NDArray[np.float64]
    internal_coordinate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonicalization_version: str

    @property
    def atom_count(self) -> int:
        return self.mol.GetNumAtoms()

    @model_validator(mode="after")
    def validate_invariant_geometry(self) -> "GeometryRecord":
        atom_count = self.mol.GetNumAtoms()
        if atom_count == 0:
            raise ValueError("Geometry.mol must contain at least one atom")
        if self.mol.GetNumConformers() != 1:
            raise ValueError("Geometry.mol must contain exactly one conformer")
        if not self.mol.GetConformer().Is3D():
            raise ValueError("Geometry.mol conformer must be three-dimensional")
        internal = np.asarray(self.internal_coordinates)
        if internal.shape != (atom_count, 3):
            raise ValueError("Geometry.internal_coordinates must have shape (atom_count, 3)")
        if internal.dtype != np.dtype("<f8") or not internal.flags.c_contiguous:
            raise ValueError(
                "Geometry.internal_coordinates must be C-contiguous little-endian float64"
            )
        if not np.isfinite(internal).all():
            raise ValueError("Geometry.internal_coordinates must contain only finite values")
        if internal_coordinate_hash(internal) != self.internal_coordinate_hash:
            raise ValueError(
                "Geometry.internal_coordinate_hash does not match internal_coordinates"
            )
        if any(
            atom.GetAtomMapNum() != 0
            for atom in self.mol.GetAtoms()  # type: ignore[no-untyped-call]
        ):
            raise ValueError("Geometry.mol must not contain reaction atom maps")
        return self


class NormalizedMoleculeRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    formula: MolecularFormulaRecord
    topology: MolecularTopologyRecord
    topology_derivation: MolecularTopologyDerivationRecord
    geometry: GeometryRecord
    observed_coordinates: npt.NDArray[np.float64]
    observed_coordinate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_atomic_numbers: list[int]
    observed_to_geometry_atom_indices: list[int]
    observed_to_geometry_transform: list[float] = Field(min_length=16, max_length=16)
    geometry_assignment_rmsd_angstrom: float = Field(ge=0, allow_inf_nan=False)
    geometry_assignment_max_abs_angstrom: float = Field(ge=0, allow_inf_nan=False)
    charge: int
    multiplicity: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_chemical_axes(self) -> "NormalizedMoleculeRecord":
        topology_is_untrusted = (
            self.topology.sanitization_status is TopologySanitizationStatus.FAILED
        )
        atom_count = self.geometry.mol.GetNumAtoms()
        if not (self.formula.atom_count == self.topology.atom_count == atom_count):
            raise ValueError("Formula, Topology, and Geometry atom counts must match")
        observed = np.asarray(self.observed_coordinates)
        if observed.shape != (atom_count, 3):
            raise ValueError("observed_coordinates must have shape (atom_count, 3)")
        if observed.dtype != np.dtype("<f8") or not observed.flags.c_contiguous:
            raise ValueError("observed_coordinates must be C-contiguous little-endian float64")
        if not np.isfinite(observed).all():
            raise ValueError("observed_coordinates must contain only finite values")
        if sha256(observed.tobytes(order="C")).hexdigest() != self.observed_coordinate_hash:
            raise ValueError("observed_coordinate_hash does not match observed_coordinates")
        if len(self.observed_atomic_numbers) != atom_count:
            raise ValueError("observed_atomic_numbers length must match atom count")
        if sorted(self.observed_to_geometry_atom_indices) != list(range(atom_count)):
            raise ValueError(
                "observed_to_geometry_atom_indices must be a source-to-Geometry permutation"
            )
        for source_index, topology_index in enumerate(self.observed_to_geometry_atom_indices):
            topology_atomic_number = self.topology.mol.GetAtomWithIdx(topology_index).GetAtomicNum()
            source_atomic_number = self.observed_atomic_numbers[source_index]
            if topology_atomic_number != source_atomic_number:
                raise ValueError(
                    "observed_to_geometry_atom_indices does not map source atoms onto Topology"
                )
        topology_graph = Chem.Mol(self.topology.mol)
        geometry_graph = Chem.Mol(self.geometry.mol)
        topology_graph.RemoveAllConformers()
        geometry_graph.RemoveAllConformers()
        for topology_index, atom in enumerate(
            topology_graph.GetAtoms()  # type: ignore[no-untyped-call]
        ):
            atom.SetAtomMapNum(topology_index + 1)
        for topology_index, atom in enumerate(
            geometry_graph.GetAtoms()  # type: ignore[no-untyped-call]
        ):
            atom.SetAtomMapNum(topology_index + 1)
        if not topology_is_untrusted:
            Chem.AssignStereochemistry(topology_graph, cleanIt=True, force=True)
            Chem.AssignStereochemistry(geometry_graph, cleanIt=True, force=True)
            if Chem.MolToCXSmiles(
                topology_graph,
                canonical=True,
                isomericSmiles=True,
            ) != Chem.MolToCXSmiles(
                geometry_graph,
                canonical=True,
                isomericSmiles=True,
            ):
                raise ValueError("Geometry.mol does not match Topology in canonical atom order")
            if self.charge != self.topology.formal_charge:
                raise ValueError("calculation charge does not match Topology formal charge")
            electron_count = (
                sum(
                    atom.GetAtomicNum()
                    for atom in self.geometry.mol.GetAtoms()  # type: ignore[no-untyped-call]
                )
                - self.charge
            )
            if (electron_count + self.multiplicity) % 2 != 1:
                raise ValueError("electron count and multiplicity have inconsistent parity")
        transform = np.asarray(self.observed_to_geometry_transform, dtype=np.float64).reshape(4, 4)
        rotation = transform[:3, :3]
        if not np.isfinite(transform).all():
            raise ValueError("observed-to-Geometry transform must contain only finite values")
        if not np.allclose(transform[3], [0.0, 0.0, 0.0, 1.0], rtol=0.0, atol=1e-12):
            raise ValueError("observed-to-Geometry transform must be homogeneous")
        if not np.allclose(rotation @ rotation.T, np.eye(3), rtol=0.0, atol=1e-10):
            raise ValueError("observed-to-Geometry rotation must be orthonormal")
        if not np.isclose(np.linalg.det(rotation), 1.0, rtol=0.0, atol=1e-10):
            raise ValueError("observed-to-Geometry transform must use a proper rotation")
        if self.geometry_assignment_max_abs_angstrom < self.geometry_assignment_rmsd_angstrom:
            raise ValueError("geometry assignment maximum deviation cannot be smaller than RMSD")
        return self


__all__ = [
    "GeometryRecord",
    "MolecularFormulaRecord",
    "MolecularFormulaRangeQuery",
    "MolecularTopologySearchQuery",
    "MolecularTopologyRecord",
    "MolecularTopologyDerivationRecord",
    "NormalizedMoleculeRecord",
    "NormalizedTopologyRecord",
]
