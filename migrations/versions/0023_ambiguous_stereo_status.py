"""Allow topology rows whose trusted stereo projection is inconclusive."""

import sqlalchemy as sa
from alembic import op

revision: str = "0023_ambiguous_stereo_status"
down_revision: str | None = "0022_profile_runtime"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "molecular_topology_stereo_status"
_VALUES = "'assigned', 'unassigned', 'unknown', 'conflict', 'ambiguous'"


def upgrade() -> None:
    bind = op.get_bind()
    constraints = {
        constraint.get("name")
        for constraint in sa.inspect(bind).get_check_constraints("molecular_topology")
    }
    if _CONSTRAINT in constraints:
        op.drop_constraint(_CONSTRAINT, "molecular_topology", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "molecular_topology",
        f"stereo_status IN ({_VALUES})",
    )


def downgrade() -> None:
    bind = op.get_bind()
    ambiguous_count = bind.execute(
        sa.text("SELECT count(*) FROM molecular_topology WHERE stereo_status = 'ambiguous'")
    ).scalar_one()
    if ambiguous_count:
        raise RuntimeError("cannot remove ambiguous stereo status while rows use it")
    op.drop_constraint(_CONSTRAINT, "molecular_topology", type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        "molecular_topology",
        "stereo_status IN ('assigned', 'unassigned', 'unknown', 'conflict')",
    )
