"""Align the calculation-protocol schema default with protocol-v3."""

from alembic import op

revision: str = "0042_protocol_schema_default_v3"
down_revision: str | None = "0041_normalize_protocol_case"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "calculation_protocol",
        "spec_schema_version",
        server_default="calculation-protocol-v3",
    )


def downgrade() -> None:
    op.alter_column(
        "calculation_protocol",
        "spec_schema_version",
        server_default="calculation-protocol-v2",
    )
