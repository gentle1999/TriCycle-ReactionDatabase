"""Refresh profile source visibility when source rows are inserted."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051_profile_source_insert_vis"
down_revision: str | None = "0050_profile_source_visibility"
branch_labels: str | None = None
depends_on: str | None = None


_PROFILE = "mapped_reaction_thermodynamic_profile"
_SOURCE = "mapped_reaction_thermodynamic_profile_source"
_TRIGGER = "trg_thermodynamic_profile_source_change"
_FUNCTION = "refresh_thermodynamic_profile_source_visibility_on_source_change"


def _create_source_trigger(*, include_insert: bool) -> None:
    events = "INSERT OR UPDATE OR DELETE" if include_insert else "UPDATE OR DELETE"
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER}
            AFTER {events} ON {_SOURCE}
            FOR EACH ROW
            EXECUTE FUNCTION {_FUNCTION}()
            """
        )
    )


def upgrade() -> None:
    """Repair rows written by 0050 and cover the normal profile write order."""

    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_SOURCE}"))
    _create_source_trigger(include_insert=True)
    # 0050 can leave profiles hidden when their source rows are inserted after
    # the profile row. Recompute all rows once so already-applied databases
    # receive the same state as a fresh database.
    op.execute(
        sa.text(
            f"""
            SELECT refresh_thermodynamic_profile_source_visibility(
                COALESCE(array_agg(id), ARRAY[]::uuid[])
            )
            FROM {_PROFILE}
            """
        )
    )


def downgrade() -> None:
    """Restore the 0050 trigger definition while rolling back this repair."""

    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON {_SOURCE}"))
    _create_source_trigger(include_insert=False)
