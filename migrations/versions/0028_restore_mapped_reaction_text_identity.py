"""Restore mapped-reaction text as the sole persisted mapping identity."""

import sqlalchemy as sa
from alembic import op

revision: str = "0028_restore_mapped_text_id"
down_revision: str | None = "0027_mapped_concrete_identity"
branch_labels: str | None = None
depends_on: str | None = None

_CONCRETE_HASH_CHECK = "ck_concrete_mapping_hash_hex"
_CONCRETE_UNIQUE = "uq_mapped_reaction_concrete_hash"
_LEGACY_UNIQUE_INDEX = "uq_mapped_reaction_legacy_hash"
_CONCRETE_HASH_INDEX = "ix_mapped_reaction_concrete_mapping_hash"
_TEXT_UNIQUE = "uq_mapped_reaction_hash"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

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
            "cannot restore mapped-reaction text identity while duplicate mapping text rows exist"
        )

    check_constraints = {
        constraint.get("name") for constraint in inspector.get_check_constraints("mapped_reaction")
    }
    if _CONCRETE_HASH_CHECK in check_constraints:
        op.drop_constraint(_CONCRETE_HASH_CHECK, "mapped_reaction", type_="check")

    indexes = {index.get("name") for index in inspector.get_indexes("mapped_reaction")}
    if _CONCRETE_HASH_INDEX in indexes:
        op.drop_index(_CONCRETE_HASH_INDEX, table_name="mapped_reaction")
    if _LEGACY_UNIQUE_INDEX in indexes:
        op.drop_index(_LEGACY_UNIQUE_INDEX, table_name="mapped_reaction")

    unique_constraints = {
        constraint.get("name") for constraint in inspector.get_unique_constraints("mapped_reaction")
    }
    if _CONCRETE_UNIQUE in unique_constraints:
        op.drop_constraint(_CONCRETE_UNIQUE, "mapped_reaction", type_="unique")
    if _TEXT_UNIQUE not in unique_constraints:
        op.create_unique_constraint(
            _TEXT_UNIQUE,
            "mapped_reaction",
            ["logical_reaction_id", "mapping_hash"],
        )

    if "concrete_mapping_hash" in {
        column["name"] for column in sa.inspect(bind).get_columns("mapped_reaction")
    }:
        op.drop_column("mapped_reaction", "concrete_mapping_hash")


def downgrade() -> None:
    raise RuntimeError(
        "0028 intentionally removes the invalid concrete mapping identity; restore from a backup "
        "if that experimental schema is required"
    )
