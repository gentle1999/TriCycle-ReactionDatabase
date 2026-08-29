"""Manifest declarations and mapped-reaction semantics."""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from molalchemy.rdkit.index import RdkitIndex
from molalchemy.rdkit.types import RdkitBitFingerprint, RdkitReaction
from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INTEGER, JSONB, SMALLINT
from sqlmodel import Field, Relationship, SQLModel

from tricycle_reaction_db.db.models.base import created_at_field, uuid_primary_key_field
from tricycle_reaction_db.domain.enums import (
    ArtifactResolutionStatus,
    LogicalReactionParticipantRole,
    LogicalReactionParticipantSide,
    ManifestArtifactRole,
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
    ReactionClass,
    WorkflowManifestStatus,
    string_enum,
)
from tricycle_reaction_db.domain.fingerprints import (
    REACTION_STRUCTURAL_BFP_RADIUS,
    REACTION_STRUCTURAL_BFP_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from tricycle_reaction_db.db.models.artifacts import ArtifactFile
    from tricycle_reaction_db.db.models.chemistry import Geometry, MolecularTopology

_HASH_PATTERN = "^[0-9a-f]{64}$"


class WorkflowManifest(SQLModel, table=True):
    """One immutable revision of a reaction workflow declaration."""

    __tablename__ = "workflow_manifest"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "manifest_key",
            "revision",
            name="uq_workflow_manifest_key_revision",
        ),
        UniqueConstraint(
            "manifest_key",
            "id",
            name="uq_workflow_manifest_key_id",
        ),
        ForeignKeyConstraint(
            ("manifest_key", "supersedes_id"),
            ("workflow_manifest.manifest_key", "workflow_manifest.id"),
            ondelete="RESTRICT",
            name="fk_workflow_manifest_supersedes_same_series",
        ),
        CheckConstraint("revision >= 1", name="ck_workflow_manifest_revision_positive"),
        CheckConstraint(
            f"payload_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_workflow_manifest_payload_hash_hex",
        ),
        CheckConstraint(
            "supersedes_id IS NULL OR supersedes_id <> id",
            name="ck_workflow_manifest_not_self_superseding",
        ),
        CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="ck_workflow_manifest_published_timestamp",
        ),
        CheckConstraint(
            "status NOT IN ('received', 'validated', 'rejected') OR published_at IS NULL",
            name="ck_workflow_manifest_unpublished_timestamp",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    artifact_file_id: UUID = Field(
        foreign_key="artifact_file.id",
        ondelete="RESTRICT",
        unique=True,
        nullable=False,
    )
    manifest_key: str = Field(sa_type=Text, nullable=False)
    revision: int = Field(nullable=False)
    schema_version: str = Field(max_length=64, nullable=False)
    payload_sha256: str = Field(max_length=64, nullable=False)
    qc_policy_version: str = Field(max_length=64, nullable=False)
    status: WorkflowManifestStatus = Field(
        default=WorkflowManifestStatus.RECEIVED,
        sa_column=Column(
            string_enum(WorkflowManifestStatus, name="workflow_manifest_status"),
            nullable=False,
            server_default=WorkflowManifestStatus.RECEIVED.value,
            index=True,
        ),
    )
    supersedes_id: UUID | None = Field(default=None, index=True)
    validation_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    published_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    artifact_file: "ArtifactFile" = Relationship(back_populates="workflow_manifest")
    artifact_bindings: list["ManifestArtifactBinding"] = Relationship(
        back_populates="workflow_manifest",
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={
            "overlaps": "source_geometry_binding,dependent_bindings",
        },
    )
    supersedes: Optional["WorkflowManifest"] = Relationship(
        back_populates="superseded_by",
        sa_relationship_kwargs={
            "foreign_keys": "[WorkflowManifest.manifest_key, WorkflowManifest.supersedes_id]",
            "remote_side": "[WorkflowManifest.manifest_key, WorkflowManifest.id]",
        },
    )
    superseded_by: list["WorkflowManifest"] = Relationship(
        back_populates="supersedes",
        passive_deletes="all",
        sa_relationship_kwargs={
            "foreign_keys": "[WorkflowManifest.manifest_key, WorkflowManifest.supersedes_id]",
        },
    )


class ManifestArtifactBinding(SQLModel, table=True):
    """A manifest's expected or resolved calculation artifact."""

    __tablename__ = "manifest_artifact_binding"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "workflow_manifest_id",
            "artifact_key",
            name="uq_manifest_artifact_binding_manifest_key",
        ),
        ForeignKeyConstraint(
            ("workflow_manifest_id", "source_geometry_artifact_key"),
            (
                "manifest_artifact_binding.workflow_manifest_id",
                "manifest_artifact_binding.artifact_key",
            ),
            ondelete="RESTRICT",
            name="fk_manifest_artifact_binding_source_same_manifest",
        ),
        CheckConstraint(
            f"expected_content_sha256 IS NULL OR expected_content_sha256 ~ '{_HASH_PATTERN}'",
            name="ck_manifest_artifact_binding_expected_hash_hex",
        ),
        CheckConstraint(
            "num_nonnulls(segment_index, frame_index) IN (0, 2)",
            name="ck_manifest_artifact_binding_selector_complete",
        ),
        CheckConstraint(
            "segment_index IS NULL OR segment_index >= 0",
            name="ck_manifest_artifact_binding_segment_nonnegative",
        ),
        CheckConstraint(
            "frame_index IS NULL OR frame_index >= 0",
            name="ck_manifest_artifact_binding_frame_nonnegative",
        ),
        CheckConstraint(
            "source_geometry_artifact_key IS NULL OR source_geometry_artifact_key <> artifact_key",
            name="ck_manifest_artifact_binding_not_self_sourcing",
        ),
        CheckConstraint(
            "resolution_status <> 'resolved' OR "
            "(artifact_file_id IS NOT NULL AND expected_content_sha256 IS NOT NULL AND "
            "segment_index IS NOT NULL AND frame_index IS NOT NULL)",
            name="ck_manifest_artifact_binding_resolved_payload",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    workflow_manifest_id: UUID = Field(
        foreign_key="workflow_manifest.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    artifact_key: str = Field(sa_type=Text, nullable=False)
    artifact_file_id: UUID | None = Field(
        default=None,
        foreign_key="artifact_file.id",
        ondelete="RESTRICT",
        index=True,
    )
    expected_content_sha256: str | None = Field(default=None, max_length=64)
    artifact_role: ManifestArtifactRole = Field(
        sa_column=Column(
            string_enum(ManifestArtifactRole, name="manifest_artifact_binding_role"),
            nullable=False,
            index=True,
        )
    )
    reaction_key: str = Field(sa_type=Text, nullable=False)
    path_key: str = Field(sa_type=Text, nullable=False)
    node_key: str = Field(sa_type=Text, nullable=False)
    segment_index: int | None = Field(default=None)
    frame_index: int | None = Field(default=None)
    source_geometry_artifact_key: str | None = Field(default=None, sa_type=Text)
    resolution_status: ArtifactResolutionStatus = Field(
        default=ArtifactResolutionStatus.DECLARED,
        sa_column=Column(
            string_enum(
                ArtifactResolutionStatus,
                name="manifest_artifact_binding_resolution_status",
            ),
            nullable=False,
            server_default=ArtifactResolutionStatus.DECLARED.value,
            index=True,
        ),
    )
    workflow_manifest: WorkflowManifest = Relationship(
        back_populates="artifact_bindings",
        sa_relationship_kwargs={
            "overlaps": "source_geometry_binding,dependent_bindings",
        },
    )
    artifact_file: Optional["ArtifactFile"] = Relationship(
        back_populates="manifest_artifact_bindings"
    )
    source_geometry_binding: Optional["ManifestArtifactBinding"] = Relationship(
        back_populates="dependent_bindings",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[ManifestArtifactBinding.workflow_manifest_id, "
                "ManifestArtifactBinding.source_geometry_artifact_key]"
            ),
            "remote_side": (
                "[ManifestArtifactBinding.workflow_manifest_id, "
                "ManifestArtifactBinding.artifact_key]"
            ),
            "overlaps": "workflow_manifest,artifact_bindings,dependent_bindings",
        },
    )
    dependent_bindings: list["ManifestArtifactBinding"] = Relationship(
        back_populates="source_geometry_binding",
        passive_deletes="all",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[ManifestArtifactBinding.workflow_manifest_id, "
                "ManifestArtifactBinding.source_geometry_artifact_key]"
            ),
            "overlaps": "workflow_manifest,artifact_bindings,source_geometry_binding",
        },
    )


class LogicalReaction(SQLModel, table=True):
    """A globally reusable net transformation whose identity is its topologies."""

    __tablename__ = "logical_reaction"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint("reaction_hash", name="uq_logical_reaction_hash"),
        Index("ix_logical_reaction_created_id", "created_at", "id"),
        Index("ix_logical_reaction_reaction_key", "reaction_key"),
        Index(
            "ix_logical_reaction_reactant_sort_created_id",
            "reactant_sort_key",
            "created_at",
            "id",
        ),
        CheckConstraint(f"reaction_hash ~ '{_HASH_PATTERN}'", name="ck_reaction_hash_hex"),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    reaction_key: str = Field(sa_type=Text, nullable=False)
    label: str | None = Field(default=None, sa_type=Text)
    reaction_class: ReactionClass | None = Field(
        default=None,
        sa_column=Column(
            string_enum(ReactionClass, name="reaction_class"),
            nullable=True,
            server_default=None,
            index=True,
        ),
    )
    cycloaddition_pattern: str | None = Field(default=None, max_length=32, index=True)
    reaction_hash: str = Field(max_length=64, index=True, nullable=False)
    # Persisted in participant order so the default catalogue sort does not
    # aggregate every reactant on every request.
    reactant_sort_key: list[str] | None = Field(
        default=None,
        sa_column=Column(ARRAY(Text), nullable=True),
    )
    participants: list["LogicalReactionParticipant"] = Relationship(
        back_populates="logical_reaction", cascade_delete=True, passive_deletes=True
    )
    mapped_reactions: list["MappedReaction"] = Relationship(
        back_populates="logical_reaction", cascade_delete=True, passive_deletes=True
    )


class LogicalReactionParticipant(SQLModel, table=True):
    """A topology and stoichiometry on one side of a logical reaction."""

    __tablename__ = "logical_reaction_participant"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "logical_reaction_id",
            "side",
            "participant_index",
            name="uq_logical_reaction_participant_side_index",
        ),
        CheckConstraint("participant_index >= 0", name="ck_logical_participant_index"),
        CheckConstraint(
            "stoichiometric_coefficient > 0", name="ck_logical_participant_stoichiometry"
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    logical_reaction_id: UUID = Field(
        foreign_key="logical_reaction.id", ondelete="CASCADE", index=True, nullable=False
    )
    topology_id: UUID = Field(
        foreign_key="molecular_topology.id", ondelete="RESTRICT", index=True, nullable=False
    )
    side: LogicalReactionParticipantSide = Field(
        sa_column=Column(
            string_enum(LogicalReactionParticipantSide, name="logical_reaction_participant_side"),
            nullable=False,
            index=True,
        )
    )
    participant_index: int = Field(sa_type=SMALLINT, nullable=False)
    role: LogicalReactionParticipantRole | None = Field(
        default=None,
        sa_column=Column(
            string_enum(LogicalReactionParticipantRole, name="logical_reaction_participant_role"),
            nullable=True,
        ),
    )
    stoichiometric_coefficient: int = Field(default=1, sa_type=SMALLINT, nullable=False)
    logical_reaction: LogicalReaction = Relationship(back_populates="participants")
    topology: "MolecularTopology" = Relationship(back_populates="logical_reaction_participants")
    mapped_participants: list["MappedReactionParticipant"] = Relationship(
        back_populates="logical_reaction_participant", cascade_delete=True, passive_deletes=True
    )


class MappedReaction(SQLModel, table=True):
    """One explicit mapped reaction SMILES under a logical reaction."""

    __tablename__ = "mapped_reaction"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "logical_reaction_id", "mapped_reaction_key", name="uq_mapped_reaction_key"
        ),
        UniqueConstraint("logical_reaction_id", "mapping_hash", name="uq_mapped_reaction_hash"),
        CheckConstraint(f"mapping_hash ~ '{_HASH_PATTERN}'", name="ck_mapping_hash_hex"),
        CheckConstraint(
            f"reaction_structural_bfp_schema_version = '{REACTION_STRUCTURAL_BFP_SCHEMA_VERSION}'",
            name="ck_mapped_reaction_structural_bfp_schema_version",
        ),
        RdkitIndex("ix_mapped_reaction_reaction_gist", "reaction"),
        RdkitIndex("ix_mapped_reaction_structural_bfp_gist", "reaction_structural_bfp"),
        Index(
            "ix_mapped_reaction_min_activation_gibbs",
            "minimum_activation_gibbs_free_energy_kcal_mol",
        ),
        Index(
            "ix_mapped_reaction_max_activation_gibbs",
            "maximum_activation_gibbs_free_energy_kcal_mol",
        ),
        Index(
            "ix_mapped_reaction_min_reaction_gibbs",
            "minimum_reaction_gibbs_free_energy_kcal_mol",
        ),
        Index(
            "ix_mapped_reaction_max_reaction_gibbs",
            "maximum_reaction_gibbs_free_energy_kcal_mol",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    logical_reaction_id: UUID = Field(
        foreign_key="logical_reaction.id", ondelete="CASCADE", index=True, nullable=False
    )
    mapped_reaction_key: str = Field(sa_type=Text, nullable=False)
    label: str | None = Field(default=None, sa_type=Text)
    mapped_reaction_kind: MappedReactionKind = Field(
        sa_column=Column(
            string_enum(MappedReactionKind, name="mapped_reaction_kind"),
            nullable=False,
            index=True,
        )
    )
    mapped_reaction_smiles: str = Field(sa_type=Text, nullable=False)
    reaction: str | None = Field(
        default=None,
        sa_column=Column(
            RdkitReaction(return_type="smiles"),
            Computed(
                "reaction_from_smiles(mapped_reaction_smiles::cstring)",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    reaction_structural_bfp: bytes | None = Field(
        default=None,
        sa_column=Column(
            RdkitBitFingerprint(),
            Computed(
                "reaction_structural_bfp(reaction_from_smiles("
                "mapped_reaction_smiles::cstring), "
                f"{REACTION_STRUCTURAL_BFP_RADIUS})",
                persisted=True,
            ),
            nullable=False,
        ),
    )
    reaction_structural_bfp_schema_version: str = Field(
        default=REACTION_STRUCTURAL_BFP_SCHEMA_VERSION,
        sa_column=Column(
            Text,
            nullable=False,
            server_default=REACTION_STRUCTURAL_BFP_SCHEMA_VERSION,
        ),
    )
    mapping_hash: str = Field(max_length=64, index=True, nullable=False)
    thermodynamic_profile_policy_version: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = Field(
        default=None,
    )
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = Field(
        default=None,
    )
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = Field(default=None)
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = Field(default=None)
    logical_reaction: LogicalReaction = Relationship(back_populates="mapped_reactions")
    participants: list["MappedReactionParticipant"] = Relationship(
        back_populates="mapped_reaction", cascade_delete=True, passive_deletes=True
    )
    nodes: list["MappedReactionNode"] = Relationship(
        back_populates="mapped_reaction", cascade_delete=True, passive_deletes=True
    )
    edges: list["MappedReactionEdge"] = Relationship(
        back_populates="mapped_reaction",
        cascade_delete=True,
        passive_deletes=True,
        sa_relationship_kwargs={"overlaps": "source_node,target_node,transition_state_node"},
    )
    thermodynamic_profiles: list["MappedReactionThermodynamicProfile"] = Relationship(
        back_populates="mapped_reaction",
        cascade_delete=True,
        passive_deletes=True,
    )


class MappedReactionThermodynamicProfile(SQLModel, table=True):
    """One source-compatible, materialized profile for a mapped reaction."""

    __tablename__ = "mapped_reaction_thermodynamic_profile"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_id",
            "source_key_hash",
            name="uq_mapped_reaction_thermodynamic_source",
        ),
        Index(
            "ix_mapped_reaction_thermodynamic_activation_gibbs",
            "mapped_reaction_id",
            "activation_gibbs_free_energy_kcal_mol",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_id: UUID = Field(
        foreign_key="mapped_reaction.id", ondelete="CASCADE", index=True, nullable=False
    )
    policy_version: str = Field(sa_type=Text, nullable=False)
    source_key_hash: str = Field(max_length=64, nullable=False)
    electronic_level: list[str | None] = Field(sa_column=Column(JSONB, nullable=False))
    thermochemistry_level: list[str | None] = Field(sa_column=Column(JSONB, nullable=False))
    temperature_kelvin: float = Field(nullable=False)
    pressure_atm: float = Field(nullable=False)
    reactants: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    transition_state: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    products: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    reactants_enthalpy_hartree: float = Field(nullable=False)
    reactants_gibbs_free_energy_hartree: float = Field(nullable=False)
    reactants_entropy_cal_mol_k: float = Field(nullable=False)
    transition_state_enthalpy_hartree: float | None = Field(default=None)
    transition_state_gibbs_free_energy_hartree: float | None = Field(default=None)
    transition_state_entropy_cal_mol_k: float | None = Field(default=None)
    products_enthalpy_hartree: float | None = Field(default=None)
    products_gibbs_free_energy_hartree: float | None = Field(default=None)
    products_entropy_cal_mol_k: float | None = Field(default=None)
    activation_enthalpy_kcal_mol: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed(
                "(transition_state_enthalpy_hartree - reactants_enthalpy_hartree) * 627.5094740631",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    activation_gibbs_free_energy_kcal_mol: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed(
                "(transition_state_gibbs_free_energy_hartree "
                "- reactants_gibbs_free_energy_hartree) * 627.5094740631",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    activation_entropy_cal_mol_k: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed(
                "transition_state_entropy_cal_mol_k - reactants_entropy_cal_mol_k",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    reaction_enthalpy_kcal_mol: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed(
                "(products_enthalpy_hartree - reactants_enthalpy_hartree) * 627.5094740631",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    reaction_gibbs_free_energy_kcal_mol: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed(
                "(products_gibbs_free_energy_hartree "
                "- reactants_gibbs_free_energy_hartree) * 627.5094740631",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    reaction_entropy_cal_mol_k: float | None = Field(
        default=None,
        sa_column=Column(
            Float,
            Computed("products_entropy_cal_mol_k - reactants_entropy_cal_mol_k", persisted=True),
            nullable=True,
        ),
    )
    mapped_reaction: MappedReaction = Relationship(back_populates="thermodynamic_profiles")


class MappedReactionParticipant(SQLModel, table=True):
    """Mapping-specific atom maps assigned to a logical topology participant."""

    __tablename__ = "mapped_reaction_participant"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_id", "side", "template_index", name="uq_mapped_participant_template"
        ),
        UniqueConstraint(
            "mapped_reaction_id",
            "logical_reaction_participant_id",
            name="uq_mapped_participant_logical",
        ),
        CheckConstraint("template_index >= 0", name="ck_mapped_participant_template_index"),
        CheckConstraint(
            "cardinality(atom_map_numbers) > 0 AND array_position(atom_map_numbers, NULL) IS NULL "
            "AND 0 < ALL(atom_map_numbers)",
            name="ck_mapped_participant_atom_maps",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_id: UUID = Field(
        foreign_key="mapped_reaction.id", ondelete="CASCADE", index=True, nullable=False
    )
    logical_reaction_participant_id: UUID = Field(
        foreign_key="logical_reaction_participant.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    side: LogicalReactionParticipantSide = Field(
        sa_column=Column(
            string_enum(LogicalReactionParticipantSide, name="mapped_reaction_participant_side"),
            nullable=False,
        )
    )
    template_index: int = Field(sa_type=SMALLINT, nullable=False)
    atom_map_numbers: list[int] = Field(sa_column=Column(ARRAY(INTEGER), nullable=False))
    mapped_smiles: str = Field(sa_type=Text, nullable=False)
    mapped_reaction: MappedReaction = Relationship(back_populates="participants")
    logical_reaction_participant: LogicalReactionParticipant = Relationship(
        back_populates="mapped_participants"
    )
    node_geometries: list["MappedReactionNodeGeometry"] = Relationship(
        back_populates="mapped_reaction_participant", passive_deletes="all"
    )


class MappedReactionNode(SQLModel, table=True):
    """A logical state whose concrete coordinates are bound separately."""

    __tablename__ = "mapped_reaction_node"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_id",
            "node_key",
            name="uq_mapped_reaction_node_parent_key",
        ),
        UniqueConstraint(
            "mapped_reaction_id",
            "node_index",
            name="uq_mapped_reaction_node_parent_index",
        ),
        UniqueConstraint(
            "mapped_reaction_id",
            "id",
            name="uq_mapped_reaction_node_parent_id",
        ),
        CheckConstraint(
            "node_index >= 0",
            name="ck_mapped_reaction_node_index_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_id: UUID = Field(
        foreign_key="mapped_reaction.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    node_key: str = Field(sa_type=Text, nullable=False)
    node_index: int = Field(nullable=False)
    role: MappedReactionNodeRole = Field(
        sa_column=Column(
            string_enum(MappedReactionNodeRole, name="mapped_reaction_node_role"),
            nullable=False,
            index=True,
        )
    )
    mapped_reaction: MappedReaction = Relationship(back_populates="nodes")
    geometry_bindings: list["MappedReactionNodeGeometry"] = Relationship(
        back_populates="mapped_reaction_node",
        cascade_delete=True,
        passive_deletes=True,
    )
    outgoing_edges: list["MappedReactionEdge"] = Relationship(
        back_populates="source_node",
        passive_deletes="all",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, MappedReactionEdge.source_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,source_node,target_node,transition_state_node,"
                "incoming_edges,transition_state_edges"
            ),
        },
    )
    incoming_edges: list["MappedReactionEdge"] = Relationship(
        back_populates="target_node",
        passive_deletes="all",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, MappedReactionEdge.target_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,source_node,target_node,transition_state_node,"
                "outgoing_edges,transition_state_edges"
            ),
        },
    )
    transition_state_edges: list["MappedReactionEdge"] = Relationship(
        back_populates="transition_state_node",
        passive_deletes="all",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, "
                "MappedReactionEdge.transition_state_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,source_node,target_node,transition_state_node,"
                "outgoing_edges,incoming_edges"
            ),
        },
    )


class MappedReactionNodeGeometry(SQLModel, table=True):
    """One concrete coordinate member bound to a logical path node."""

    __tablename__ = "mapped_reaction_node_geometry"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_node_id",
            "component_key",
            "coordinate_index",
            name="uq_mapped_node_geometry_component_coordinate",
        ),
        UniqueConstraint(
            "mapped_reaction_node_id",
            "geometry_id",
            "mapped_reaction_participant_id",
            name="uq_mapped_node_geometry_identity",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            "id",
            "geometry_id",
            name="uq_mapped_node_geometry_id_geometry",
        ),
        Index(
            "uq_mapped_node_geometry_primary_component",
            "mapped_reaction_node_id",
            "component_key",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        CheckConstraint(
            "component_index >= 0 AND coordinate_index >= 0",
            name="ck_node_geometry_indices_nonnegative",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_node_id: UUID = Field(
        foreign_key="mapped_reaction_node.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    geometry_id: UUID = Field(
        foreign_key="geometry.id",
        ondelete="RESTRICT",
        index=True,
        nullable=False,
    )
    mapped_reaction_participant_id: UUID | None = Field(
        default=None,
        foreign_key="mapped_reaction_participant.id",
        ondelete="CASCADE",
        index=True,
    )
    component_key: str = Field(sa_type=Text, nullable=False)
    component_index: int = Field(sa_type=SMALLINT, nullable=False)
    coordinate_index: int = Field(default=0, sa_type=SMALLINT, nullable=False)
    is_primary: bool = Field(default=True, nullable=False)
    mapped_reaction_node: MappedReactionNode = Relationship(back_populates="geometry_bindings")
    geometry: "Geometry" = Relationship(back_populates="mapped_reaction_node_geometries")
    mapped_reaction_participant: MappedReactionParticipant | None = Relationship(
        back_populates="node_geometries"
    )
    mapping_bindings: list["MappedReactionNodeGeometryMapping"] = Relationship(
        back_populates="mapped_reaction_node_geometry",
        cascade_delete=True,
        passive_deletes=True,
    )


class MappedReactionNodeGeometryMapping(SQLModel, table=True):
    """Explicit conversion from one logical reaction mapping to a coordinate."""

    __tablename__ = "mapped_reaction_node_geometry_mapping"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_node_geometry_id",
            name="uq_mapped_reaction_node_geometry_mapping_geometry",
        ),
        CheckConstraint(
            "cardinality(geometry_atom_map_numbers) > 0 AND "
            "array_position(geometry_atom_map_numbers, NULL) IS NULL AND "
            "0 < ALL(geometry_atom_map_numbers)",
            name="ck_node_geometry_mapping_atom_maps_valid",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_node_geometry_id: UUID = Field(
        foreign_key="mapped_reaction_node_geometry.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    geometry_atom_map_numbers: list[int] = Field(
        sa_column=Column(ARRAY(INTEGER, dimensions=1), nullable=False)
    )
    mapped_smiles: str = Field(sa_type=Text, nullable=False)
    mapping_method: str = Field(max_length=64, nullable=False)
    mapping_version: str = Field(max_length=64, nullable=False)
    verified: bool = Field(nullable=False)
    mapped_reaction_node_geometry: MappedReactionNodeGeometry = Relationship(
        back_populates="mapping_bindings"
    )


class MappedReactionEdge(SQLModel, table=True):
    """A manifest-directed edge between two states in the same path."""

    __tablename__ = "mapped_reaction_edge"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "mapped_reaction_id",
            "edge_key",
            name="uq_mapped_reaction_edge_path_key",
        ),
        UniqueConstraint(
            "mapped_reaction_id",
            "id",
            name="uq_mapped_reaction_edge_path_id",
        ),
        ForeignKeyConstraint(
            ("mapped_reaction_id", "source_node_id"),
            ("mapped_reaction_node.mapped_reaction_id", "mapped_reaction_node.id"),
            ondelete="RESTRICT",
            name="fk_mapped_reaction_edge_source_same_path",
        ),
        ForeignKeyConstraint(
            ("mapped_reaction_id", "target_node_id"),
            ("mapped_reaction_node.mapped_reaction_id", "mapped_reaction_node.id"),
            ondelete="RESTRICT",
            name="fk_mapped_reaction_edge_target_same_path",
        ),
        ForeignKeyConstraint(
            ("mapped_reaction_id", "transition_state_node_id"),
            ("mapped_reaction_node.mapped_reaction_id", "mapped_reaction_node.id"),
            ondelete="RESTRICT",
            name="fk_mapped_reaction_edge_transition_state_same_path",
        ),
        CheckConstraint(
            "source_node_id <> target_node_id",
            name="ck_mapped_reaction_edge_distinct_endpoints",
        ),
    )

    id: UUID | None = uuid_primary_key_field()
    created_at: datetime | None = created_at_field()
    mapped_reaction_id: UUID = Field(
        foreign_key="mapped_reaction.id",
        ondelete="CASCADE",
        index=True,
        nullable=False,
    )
    edge_key: str = Field(sa_type=Text, nullable=False)
    source_node_id: UUID = Field(index=True, nullable=False)
    target_node_id: UUID = Field(index=True, nullable=False)
    transition_state_node_id: UUID | None = Field(default=None, index=True)
    edge_kind: MappedReactionEdgeKind = Field(
        sa_column=Column(
            string_enum(MappedReactionEdgeKind, name="mapped_reaction_edge_kind"),
            nullable=False,
            index=True,
        )
    )
    mapped_reaction: MappedReaction = Relationship(
        back_populates="edges",
        sa_relationship_kwargs={
            "overlaps": (
                "source_node,target_node,transition_state_node,outgoing_edges,"
                "incoming_edges,transition_state_edges"
            ),
        },
    )
    source_node: MappedReactionNode = Relationship(
        back_populates="outgoing_edges",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, MappedReactionEdge.source_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,target_node,transition_state_node,incoming_edges,"
                "transition_state_edges"
            ),
        },
    )
    target_node: MappedReactionNode = Relationship(
        back_populates="incoming_edges",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, MappedReactionEdge.target_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,source_node,transition_state_node,outgoing_edges,"
                "transition_state_edges"
            ),
        },
    )
    transition_state_node: MappedReactionNode | None = Relationship(
        back_populates="transition_state_edges",
        sa_relationship_kwargs={
            "foreign_keys": (
                "[MappedReactionEdge.mapped_reaction_id, "
                "MappedReactionEdge.transition_state_node_id]"
            ),
            "overlaps": (
                "edges,mapped_reaction,source_node,target_node,outgoing_edges,incoming_edges"
            ),
        },
    )


__all__ = [
    "ManifestArtifactBinding",
    "LogicalReaction",
    "LogicalReactionParticipant",
    "MappedReaction",
    "MappedReactionEdge",
    "MappedReactionParticipant",
    "MappedReactionNode",
    "MappedReactionNodeGeometry",
    "MappedReactionNodeGeometryMapping",
    "WorkflowManifest",
]
