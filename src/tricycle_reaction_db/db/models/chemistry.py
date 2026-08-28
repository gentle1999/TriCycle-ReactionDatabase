"""Chemical identity and immutable geometry entities."""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import numpy as np
import numpy.typing as npt
from molalchemy.rdkit.index import RdkitIndex
from molalchemy.rdkit.types import RdkitBitFingerprint, RdkitMol
from pydantic import ConfigDict
from rdkit import Chem
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    Float,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, INTEGER, JSONB, SMALLINT
from sqlalchemy.orm import deferred
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.db.types import NumpyArray
from tricycle_reaction_db.domain.enums import (
    StereoStatus,
    TopologySanitizationStatus,
    string_enum,
)
from tricycle_reaction_db.domain.fingerprints import (
    MORGAN_BFP_RADIUS,
    MORGAN_BFP_SCHEMA_VERSION,
)
from tricycle_reaction_db.domain.formulas import (
    ELEMENT_COUNT_VECTOR_SCHEMA_VERSION,
    ELEMENT_COUNT_VECTOR_SIZE,
)

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.calculations import CalculationFrame
    from tricycle_reaction_db.db.models.reactions import (
        LogicalReactionParticipant,
        MappedReactionNodeGeometry,
    )
    from tricycle_reaction_db.db.models.uploads import TransitionStateEndpoint

_HASH_PATTERN = "^[0-9a-f]{64}$"
_ELEMENT_COUNT_TOKENS_SQL = (
    "ARRAY["
    + ",".join(
        f"'{atomic_number}:' || element_count_vector[{atomic_number}]::text"
        for atomic_number in range(1, ELEMENT_COUNT_VECTOR_SIZE + 1)
    )
    + "]"
)


class MolecularFormula(SQLModel, table=True):
    """Element and isotope composition independent of charge and bonding."""

    __tablename__ = "molecular_formula"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        CheckConstraint("atom_count > 0", name="ck_molecular_formula_atom_count_positive"),
        CheckConstraint(
            f"composition_hash ~ '{_HASH_PATTERN}'",
            name="ck_molecular_formula_composition_hash_hex",
        ),
        CheckConstraint(
            f"cardinality(element_count_vector) = {ELEMENT_COUNT_VECTOR_SIZE}",
            name="ck_molecular_formula_element_count_vector_length",
        ),
        CheckConstraint(
            "0 <= ALL(element_count_vector)",
            name="ck_molecular_formula_element_count_vector_nonnegative",
        ),
        Index(
            "ix_molecular_formula_element_count_tokens_gin",
            "element_count_tokens",
            postgresql_using="gin",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    hill_formula: str = Field(sa_type=Text, index=True, nullable=False)
    composition: list[dict[str, int]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False),
    )
    composition_schema_version: str = Field(
        default="formula-composition-v1",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default="formula-composition-v1",
        ),
    )
    atom_count: int = Field(sa_type=INTEGER, nullable=False)
    composition_hash: str = Field(max_length=64, unique=True, nullable=False)
    element_count_vector: list[int] = Field(
        sa_column=Column(
            ARRAY(INTEGER, dimensions=1),
            nullable=False,
        )
    )
    element_count_vector_schema_version: str = Field(
        default=ELEMENT_COUNT_VECTOR_SCHEMA_VERSION,
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=ELEMENT_COUNT_VECTOR_SCHEMA_VERSION,
        ),
    )
    element_count_tokens: list[str] | None = Field(
        default=None,
        sa_column=Column(
            ARRAY(Text, dimensions=1),
            Computed(_ELEMENT_COUNT_TOKENS_SQL, persisted=True),
            nullable=False,
        ),
    )
    topologies: list["MolecularTopology"] = Relationship(
        back_populates="formula",
        passive_deletes="all",
    )


class MolecularTopology(SQLModel, table=True):
    """Reusable, atom-map-free molecular graph stored as a PostgreSQL RDKit mol."""

    __tablename__ = "molecular_topology"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "identity_schema_version",
            "graph_hash",
            name="uq_molecular_topology_identity_hash",
        ),
        CheckConstraint("atom_count > 0", name="ck_molecular_topology_atom_count_positive"),
        CheckConstraint("heavy_atom_count >= 0", name="ck_molecular_topology_heavy_atoms"),
        CheckConstraint(
            "radical_electron_count >= 0",
            name="ck_molecular_topology_radical_electrons",
        ),
        CheckConstraint("fragment_count > 0", name="ck_molecular_topology_fragment_count"),
        CheckConstraint(
            f"graph_hash ~ '{_HASH_PATTERN}'",
            name="ck_molecular_topology_graph_hash_hex",
        ),
        CheckConstraint(
            "(sanitization_status = 'sanitized' AND sanitization_error IS NULL "
            "AND canonical_isomeric_smiles IS NOT NULL) OR "
            "(sanitization_status = 'failed' AND sanitization_error IS NOT NULL)",
            name="ck_molecular_topology_sanitization_evidence",
        ),
        RdkitIndex("ix_molecular_topology_mol_gist", "mol"),
        RdkitIndex("ix_molecular_topology_morgan_bfp_gist", "morgan_bfp"),
    )
    model_config = ConfigDict(arbitrary_types_allowed=True)  # type: ignore[assignment]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    formula_id: UUID = Field(
        foreign_key="molecular_formula.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    mol: Chem.Mol = Field(sa_column=Column(RdkitMol(return_type="mol"), nullable=False))
    morgan_bfp: bytes | None = Field(
        default=None,
        sa_column=Column(
            RdkitBitFingerprint(),
            Computed(
                "CASE WHEN sanitization_status = 'sanitized' "
                "THEN morganbv_fp(mol_from_smiles(canonical_isomeric_smiles), "
                f"{MORGAN_BFP_RADIUS}) ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    morgan_bfp_schema_version: str = Field(
        default=MORGAN_BFP_SCHEMA_VERSION,
        sa_column=Column(
            String(64),
            nullable=False,
            server_default=MORGAN_BFP_SCHEMA_VERSION,
        ),
    )
    canonical_isomeric_smiles: str | None = Field(
        default=None,
        sa_type=Text,
        index=True,
        nullable=True,
    )
    graph_hash: str = Field(max_length=64, nullable=False)
    identity_schema_version: str = Field(
        default="topology-identity-v1",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default="topology-identity-v1",
        ),
    )
    atom_count: int = Field(sa_type=INTEGER, nullable=False)
    heavy_atom_count: int = Field(sa_type=INTEGER, nullable=False)
    formal_charge: int = Field(sa_type=SMALLINT, index=True, nullable=False)
    radical_electron_count: int = Field(sa_type=SMALLINT, nullable=False)
    fragment_count: int = Field(sa_type=SMALLINT, nullable=False)
    stereo_status: StereoStatus = Field(
        default=StereoStatus.UNKNOWN,
        sa_column=Column(
            string_enum(StereoStatus, name="molecular_topology_stereo_status"),
            nullable=False,
            server_default=StereoStatus.UNKNOWN.value,
        ),
    )
    sanitization_status: TopologySanitizationStatus = Field(
        default=TopologySanitizationStatus.SANITIZED,
        sa_column=Column(
            string_enum(
                TopologySanitizationStatus,
                name="molecular_topology_sanitization_status",
                length=16,
            ),
            nullable=False,
            server_default=TopologySanitizationStatus.SANITIZED.value,
        ),
    )
    sanitization_error: str | None = Field(default=None, sa_type=Text)
    formula: MolecularFormula = Relationship(back_populates="topologies")
    derivations: list["MolecularTopologyDerivation"] = Relationship(
        back_populates="topology",
        passive_deletes="all",
    )
    geometries: list["Geometry"] = Relationship(
        back_populates="topology",
        passive_deletes="all",
    )
    logical_reaction_participants: list["LogicalReactionParticipant"] = Relationship(
        back_populates="topology",
        passive_deletes="all",
    )
    transition_state_endpoints: list["TransitionStateEndpoint"] = Relationship(
        back_populates="topology",
        passive_deletes="all",
    )


class MolecularTopologyDerivation(SQLModel, table=True):
    """One immutable, versioned account of how a topology was obtained."""

    __tablename__ = "molecular_topology_derivation"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "topology_id",
            "provenance_schema_version",
            "provenance_hash",
            name="uq_molecular_topology_derivation_identity",
        ),
        UniqueConstraint(
            "id",
            "topology_id",
            name="uq_molecular_topology_derivation_id_topology",
        ),
        CheckConstraint(
            f"provenance_hash ~ '{_HASH_PATTERN}'",
            name="ck_molecular_topology_derivation_hash_hex",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    topology_id: UUID = Field(
        foreign_key="molecular_topology.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    reconstruction_method: str = Field(max_length=128, nullable=False)
    reconstruction_version: str = Field(max_length=128, nullable=False)
    reconstruction_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    provenance_schema_version: str = Field(
        default="topology-derivation-v1",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default="topology-derivation-v1",
        ),
    )
    provenance_hash: str = Field(max_length=64, nullable=False)
    topology: MolecularTopology = Relationship(back_populates="derivations")
    calculation_frames: list["CalculationFrame"] = Relationship(
        back_populates="topology_derivation",
        passive_deletes="all",
    )


_geometry_internal_coordinates_column: Column[npt.NDArray[np.generic]] = Column(
    "internal_coordinates", NumpyArray(), nullable=False
)


class Geometry(SQLModel, table=True):
    """E(3)-invariant internal coordinates under one molecular topology."""

    __tablename__ = "geometry"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "topology_id",
            "canonicalization_version",
            "geometry_hash",
            "charge",
            "multiplicity",
            name="uq_geometry_topology_hash",
        ),
        CheckConstraint(
            f"internal_coordinate_hash ~ '{_HASH_PATTERN}'",
            name="ck_geometry_internal_coordinate_hash_hex",
        ),
        CheckConstraint(
            f"geometry_hash ~ '{_HASH_PATTERN}'",
            name="ck_geometry_hash_hex",
        ),
        CheckConstraint(
            "cardinality(internal_coordinate_distances_angstrom) > 0 "
            "AND cardinality(internal_coordinate_distances_angstrom) "
            "= cardinality(internal_coordinate_angles_degrees) "
            "AND cardinality(internal_coordinate_distances_angstrom) "
            "= cardinality(internal_coordinate_dihedrals_degrees) "
            "AND array_position(internal_coordinate_distances_angstrom, NULL) IS NULL "
            "AND array_position(internal_coordinate_angles_degrees, NULL) IS NULL "
            "AND array_position(internal_coordinate_dihedrals_degrees, NULL) IS NULL",
            name="ck_geometry_internal_coordinate_match_projection",
        ),
        CheckConstraint(
            "minimum_coordinate_decimal_places IS NULL "
            "OR minimum_coordinate_decimal_places BETWEEN 0 AND 18",
            name="ck_geometry_minimum_coordinate_decimal_places",
        ),
    )
    __mapper_args__ = {
        "properties": {
            "internal_coordinates": deferred(
                _geometry_internal_coordinates_column,
                raiseload=True,
            ),
        }
    }
    model_config = ConfigDict(arbitrary_types_allowed=True)  # type: ignore[assignment]

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    topology_id: UUID = Field(
        foreign_key="molecular_topology.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    mol: Chem.Mol = Field(sa_column=Column(RdkitMol(return_type="mol"), nullable=False))
    internal_coordinates: npt.NDArray[np.generic] = Field(
        sa_column=_geometry_internal_coordinates_column
    )
    # These query projections retain the canonical NPY payload as the scientific
    # source of truth while allowing PostgreSQL to perform tolerance matching.
    internal_coordinate_distances_angstrom: list[float] = Field(
        sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    internal_coordinate_angles_degrees: list[float] = Field(
        sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    internal_coordinate_dihedrals_degrees: list[float] = Field(
        sa_column=Column(ARRAY(Float, dimensions=1), nullable=False)
    )
    minimum_coordinate_decimal_places: int | None = Field(default=None, sa_type=SMALLINT)
    internal_coordinate_hash: str = Field(max_length=64, nullable=False)
    geometry_hash: str = Field(max_length=64, nullable=False)
    charge: int = Field(default=0, sa_type=SMALLINT, nullable=False)
    multiplicity: int = Field(default=1, sa_type=SMALLINT, nullable=False)
    canonicalization_version: str = Field(
        default="geometry-internal-coordinates-v1",
        sa_column=Column(
            String(64),
            nullable=False,
            server_default="geometry-internal-coordinates-v1",
        ),
    )
    topology: MolecularTopology = Relationship(back_populates="geometries")
    calculation_frames: list["CalculationFrame"] = Relationship(
        back_populates="geometry",
        passive_deletes="all",
    )
    mapped_reaction_node_geometries: list["MappedReactionNodeGeometry"] = Relationship(
        back_populates="geometry",
        passive_deletes="all",
    )

    @property
    def atom_count(self) -> int:
        return self.mol.GetNumAtoms()


__all__ = [
    "Geometry",
    "MolecularFormula",
    "MolecularTopology",
    "MolecularTopologyDerivation",
]
