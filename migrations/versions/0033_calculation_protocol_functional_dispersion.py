"""Version calculation-protocol identity after functional normalization."""

from alembic import op

revision: str = "0033_calc_protocol_dispersion"
down_revision: str | None = "0032_upload_item_update_index"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "calculation_protocol",
        "spec_schema_version",
        server_default="calculation-protocol-v2",
    )


def downgrade() -> None:
    op.alter_column(
        "calculation_protocol",
        "spec_schema_version",
        server_default="calculation-protocol-v1",
    )
