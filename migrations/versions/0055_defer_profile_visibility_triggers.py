"""Defer profile source visibility triggers during one bulk persistence transaction."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0055_defer_profile_visibility"
down_revision: str | None = "0054_profile_refresh_dirty"
branch_labels: str | None = None
depends_on: str | None = None


_DEFERRED_SETTING = "tricycle.defer_profile_source_visibility"


def _source_trigger_function(*, guarded: bool) -> str:
    guard = (
        f"""
                IF current_setting('{_DEFERRED_SETTING}', true) = 'on' THEN
                    IF TG_OP = 'DELETE' THEN
                        RETURN OLD;
                    END IF;
                    RETURN NEW;
                END IF;
"""
        if guarded
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION
            refresh_thermodynamic_profile_source_visibility_on_source_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_profile_ids uuid[] := ARRAY[]::uuid[];
        BEGIN
{guard}
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                affected_profile_ids := array_append(affected_profile_ids, NEW.profile_id);
            END IF;
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                affected_profile_ids := array_append(affected_profile_ids, OLD.profile_id);
            END IF;
            PERFORM refresh_thermodynamic_profile_source_visibility(
                affected_profile_ids
            );
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$;
    """


def _profile_trigger_function(*, guarded: bool) -> str:
    guard = (
        f"""
            IF current_setting('{_DEFERRED_SETTING}', true) = 'on' THEN
                RETURN NEW;
            END IF;
"""
        if guarded
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION
            refresh_thermodynamic_profile_source_visibility_on_profile_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
{guard}
            PERFORM refresh_thermodynamic_profile_source_visibility(ARRAY[NEW.id]);
            RETURN NEW;
        END;
        $$;
    """


def _frame_trigger_function(*, guarded: bool) -> str:
    guard = (
        f"""
                IF current_setting('{_DEFERRED_SETTING}', true) = 'on' THEN
                    RETURN NEW;
                END IF;
"""
        if guarded
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION
            refresh_thermodynamic_profile_source_visibility_on_frame_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_frame_ids uuid[] := ARRAY[]::uuid[];
        BEGIN
{guard}
            IF TG_TABLE_NAME = 'calculation_frame' THEN
                affected_frame_ids := ARRAY[NEW.id];
            ELSIF TG_TABLE_NAME = 'parse_revision' THEN
                SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                INTO affected_frame_ids
                FROM calculation_frame AS cf
                WHERE cf.parse_revision_id IN (NEW.id, OLD.id);
            ELSIF TG_TABLE_NAME = 'artifact_file' THEN
                SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                INTO affected_frame_ids
                FROM calculation_frame AS cf
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                WHERE pr.artifact_file_id = NEW.id;
            ELSIF TG_TABLE_NAME = 'artifact_ingestion' THEN
                SELECT COALESCE(array_agg(cf.id), ARRAY[]::uuid[])
                INTO affected_frame_ids
                FROM calculation_frame AS cf
                JOIN parse_revision AS pr ON pr.id = cf.parse_revision_id
                WHERE pr.artifact_file_id = NEW.artifact_file_id;
            END IF;

            PERFORM refresh_thermodynamic_profile_source_visibility_for_frames(
                affected_frame_ids
            );
            RETURN NEW;
        END;
        $$;
    """


def upgrade() -> None:
    op.execute(sa.text(_source_trigger_function(guarded=True)))
    op.execute(sa.text(_profile_trigger_function(guarded=True)))
    op.execute(sa.text(_frame_trigger_function(guarded=True)))


def downgrade() -> None:
    op.execute(sa.text(_source_trigger_function(guarded=False)))
    op.execute(sa.text(_profile_trigger_function(guarded=False)))
    op.execute(sa.text(_frame_trigger_function(guarded=False)))
