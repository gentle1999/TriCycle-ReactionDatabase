"""Allow reactions without a curator-supplied classification."""

from alembic import op

revision: str = "0004_optional_reaction_class"
down_revision: str | None = "0003_optional_source_evidence"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE logical_reaction DROP CONSTRAINT IF EXISTS reaction_class")
    op.execute(
        "ALTER TABLE logical_reaction ALTER COLUMN reaction_class DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE logical_reaction ALTER COLUMN reaction_class DROP NOT NULL"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM logical_reaction
                WHERE reaction_class IS NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade while unclassified reactions exist';
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE logical_reaction ALTER COLUMN reaction_class SET DEFAULT 'cycloaddition'"
    )
    op.execute(
        "ALTER TABLE logical_reaction ALTER COLUMN reaction_class SET NOT NULL"
    )
    op.execute(
        """
        ALTER TABLE logical_reaction
        ADD CONSTRAINT reaction_class CHECK (reaction_class = 'cycloaddition')
        """
    )
