"""Add durable, paginated queues for large artifact upload batches."""

from alembic import op

revision: str = "0002_upload_batches"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE upload_batch (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            project_id uuid NOT NULL REFERENCES project(id) ON DELETE RESTRICT,
            created_by_user_id uuid NOT NULL REFERENCES user_account(id) ON DELETE RESTRICT,
            artifact_kind varchar(32) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'active',
            shared_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            total_count integer NOT NULL,
            total_bytes bigint NOT NULL,
            succeeded_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            cancelled_count integer NOT NULL DEFAULT 0,
            uploading_count integer NOT NULL DEFAULT 0,
            CONSTRAINT upload_batch_artifact_kind CHECK (
                artifact_kind IN ('calculation_output', 'input', 'workflow_manifest', 'auxiliary')
            ),
            CONSTRAINT upload_batch_status CHECK (
                status IN ('active', 'paused', 'completed', 'cancelled')
            ),
            CONSTRAINT ck_upload_batch_total_count_positive CHECK (total_count > 0),
            CONSTRAINT ck_upload_batch_total_bytes_nonnegative CHECK (total_bytes >= 0),
            CONSTRAINT ck_upload_batch_counts_nonnegative CHECK (
                succeeded_count >= 0 AND failed_count >= 0 AND
                cancelled_count >= 0 AND uploading_count >= 0
            ),
            CONSTRAINT ck_upload_batch_counts_lte_total CHECK (
                succeeded_count + failed_count + cancelled_count + uploading_count <= total_count
            )
        );

        CREATE INDEX ix_upload_batch_project_id ON upload_batch(project_id);
        CREATE INDEX ix_upload_batch_created_by_user_id ON upload_batch(created_by_user_id);
        CREATE INDEX ix_upload_batch_status ON upload_batch(status);
        CREATE INDEX ix_upload_batch_owner_created
            ON upload_batch(created_by_user_id, created_at DESC, id DESC);

        CREATE TABLE upload_batch_item (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            batch_id uuid NOT NULL REFERENCES upload_batch(id) ON DELETE CASCADE,
            client_file_id uuid NOT NULL,
            position integer NOT NULL,
            original_filename text NOT NULL,
            relative_path text NOT NULL,
            size_bytes bigint NOT NULL,
            media_type varchar(255) NOT NULL,
            status varchar(32) NOT NULL DEFAULT 'queued',
            attempt_count integer NOT NULL DEFAULT 0,
            artifact_file_id uuid REFERENCES artifact_file(id) ON DELETE SET NULL,
            error_code varchar(128),
            error_message text,
            metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT upload_batch_item_status CHECK (
                status IN ('queued', 'uploading', 'succeeded', 'failed', 'cancelled')
            ),
            CONSTRAINT uq_upload_batch_item_client UNIQUE (batch_id, client_file_id),
            CONSTRAINT uq_upload_batch_item_position UNIQUE (batch_id, position),
            CONSTRAINT ck_upload_batch_item_position_nonnegative CHECK (position >= 0),
            CONSTRAINT ck_upload_batch_item_size_nonnegative CHECK (size_bytes >= 0),
            CONSTRAINT ck_upload_batch_item_attempts_nonnegative CHECK (attempt_count >= 0)
        );

        CREATE INDEX ix_upload_batch_item_batch_id ON upload_batch_item(batch_id);
        CREATE INDEX ix_upload_batch_item_status ON upload_batch_item(status);
        CREATE INDEX ix_upload_batch_item_artifact_file_id ON upload_batch_item(artifact_file_id);
        CREATE INDEX ix_upload_batch_item_batch_status_position
            ON upload_batch_item(batch_id, status, position);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS upload_batch_item;
        DROP TABLE IF EXISTS upload_batch;
        """
    )
