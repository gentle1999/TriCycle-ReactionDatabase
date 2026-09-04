"""Allow strict concrete mappings that share one mapped reaction string."""

import sqlalchemy as sa
from alembic import op

revision: str = "0027_mapped_concrete_identity"
down_revision: str | None = "0026_concrete_reaction_topology"
branch_labels: str | None = None
depends_on: str | None = None

_CONCRETE_HASH_CHECK = "ck_concrete_mapping_hash_hex"
_CONCRETE_UNIQUE = "uq_mapped_reaction_concrete_hash"
_LEGACY_UNIQUE_INDEX = "uq_mapped_reaction_legacy_hash"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("mapped_reaction")}
    if "concrete_mapping_hash" not in columns:
        op.add_column(
            "mapped_reaction",
            sa.Column("concrete_mapping_hash", sa.Text(), nullable=True),
        )

    constraints = {
        constraint.get("name") for constraint in inspector.get_unique_constraints("mapped_reaction")
    }
    if "uq_mapped_reaction_hash" in constraints:
        op.drop_constraint(
            "uq_mapped_reaction_hash",
            "mapped_reaction",
            type_="unique",
        )
    if _CONCRETE_UNIQUE not in constraints:
        op.create_unique_constraint(
            _CONCRETE_UNIQUE,
            "mapped_reaction",
            ["logical_reaction_id", "mapping_hash", "concrete_mapping_hash"],
        )

    indexes = {index.get("name") for index in sa.inspect(bind).get_indexes("mapped_reaction")}
    if _LEGACY_UNIQUE_INDEX not in indexes:
        op.create_index(
            _LEGACY_UNIQUE_INDEX,
            "mapped_reaction",
            ["logical_reaction_id", "mapping_hash"],
            unique=True,
            postgresql_where=sa.text("concrete_mapping_hash IS NULL"),
        )
    if "ix_mapped_reaction_concrete_mapping_hash" not in indexes:
        op.create_index(
            "ix_mapped_reaction_concrete_mapping_hash",
            "mapped_reaction",
            ["concrete_mapping_hash"],
        )

    check_constraints = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_check_constraints("mapped_reaction")
    }
    if _CONCRETE_HASH_CHECK not in check_constraints:
        op.create_check_constraint(
            _CONCRETE_HASH_CHECK,
            "mapped_reaction",
            "concrete_mapping_hash IS NULL OR concrete_mapping_hash ~ '^[0-9a-f]{64}$'",
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM mapped_reaction
            GROUP BY logical_reaction_id, mapping_hash
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade mapped-reaction concrete identity while duplicate "
            "strict mappings share one mapping_hash"
        )

    inspector = sa.inspect(bind)
    check_constraints = {
        constraint.get("name") for constraint in inspector.get_check_constraints("mapped_reaction")
    }
    if _CONCRETE_HASH_CHECK in check_constraints:
        op.drop_constraint(
            _CONCRETE_HASH_CHECK,
            "mapped_reaction",
            type_="check",
        )
    indexes = {index.get("name") for index in sa.inspect(bind).get_indexes("mapped_reaction")}
    if "ix_mapped_reaction_concrete_mapping_hash" in indexes:
        op.drop_index(
            "ix_mapped_reaction_concrete_mapping_hash",
            table_name="mapped_reaction",
        )
    if _LEGACY_UNIQUE_INDEX in indexes:
        op.drop_index(_LEGACY_UNIQUE_INDEX, table_name="mapped_reaction")
    unique_constraints = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_unique_constraints("mapped_reaction")
    }
    if _CONCRETE_UNIQUE in unique_constraints:
        op.drop_constraint(_CONCRETE_UNIQUE, "mapped_reaction", type_="unique")
    if "uq_mapped_reaction_hash" not in unique_constraints:
        op.create_unique_constraint(
            "uq_mapped_reaction_hash",
            "mapped_reaction",
            ["logical_reaction_id", "mapping_hash"],
        )
    if "concrete_mapping_hash" in {
        column["name"] for column in sa.inspect(bind).get_columns("mapped_reaction")
    }:
        op.drop_column("mapped_reaction", "concrete_mapping_hash")
