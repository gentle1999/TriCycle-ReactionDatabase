"""Clear derived chemistry and requeue available calculation files atomically.

Stop API and all write workers before --apply. Raw objects, ArtifactFile,
identity records and upload manifests are preserved. Default is plan only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.rebuild_mapped_reactions import _configure_backend_access, _save  # noqa: E402
from scripts.reset_derived_data import DERIVED_TABLES  # noqa: E402
from tricycle_reaction_db.application.services.artifact_uploads import MOLOP_VERSION  # noqa: E402
from tricycle_reaction_db.core.config import get_settings  # noqa: E402

TABLES = sorted(
    (
        set(DERIVED_TABLES)
        - {
            "upload_batch",
            "storage_garbage_collection_run",
            "storage_garbage_collection_state",
        }
    )
    | {
        "mapped_reaction_thermodynamic_profile_refresh_job",
        "mapped_reaction_thermodynamic_profile_source",
        "units_ts_dataset_export_job",
    }
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError("use a new report path; preserve previous reset evidence")
    _configure_backend_access()
    engine = create_engine(
        get_settings().database_url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c lock_timeout=10000 -c statement_timeout=600000",
        },
    )
    report = {"started_at": datetime.now(UTC).isoformat(), "apply": args.apply}
    try:
        with engine.begin() as c:
            if (
                c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                != "0061_reaction_normal_form"
            ):
                raise ValueError("requires schema 0061_reaction_normal_form")
            if not c.execute(text("SELECT pg_try_advisory_xact_lock(1414677315,61)")).scalar_one():
                raise ValueError("reaction rebuild is still running")
            actual = set(
                c.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                ).scalars()
            )
            if set(TABLES) - actual:
                raise ValueError("expected derived tables are missing")
            refs = c.execute(
                text(
                    "SELECT conrelid::regclass::text,confrelid::regclass::text "
                    "FROM pg_constraint WHERE contype='f' "
                    "AND connamespace='public'::regnamespace"
                )
            ).all()
            if any(parent in TABLES and child not in TABLES for child, parent in refs):
                raise ValueError("an unlisted table references derived data; refusing truncate")
            immutable = actual - set(TABLES) - {"upload_batch"}

            def fingerprint(table: str) -> list[object]:
                return list(
                    c.execute(
                        text(
                            f"SELECT count(*),md5(string_agg(h,'' ORDER BY h)) FROM "
                            f'(SELECT md5(row_to_json(t)::text) h FROM public."{table}" t) s'
                        )
                    ).one()
                )

            if args.apply:
                c.execute(
                    text(
                        "LOCK TABLE "
                        + ",".join(f'public."{t}"' for t in sorted(actual - {"alembic_version"}))
                        + " IN ACCESS EXCLUSIVE MODE"
                    )
                )
            before = {t: fingerprint(t) for t in sorted(immutable)}
            counts = {
                t: c.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar_one()
                for t in TABLES
            }
            eligible = c.execute(
                text(
                    "SELECT count(*) FROM artifact_file "
                    "WHERE artifact_kind='calculation_output' AND storage_status='available'"
                )
            ).scalar_one()
            report.update(
                derived_before=counts, immutable_before=before, available_calculation_files=eligible
            )
            _save(args.report, report)
            if args.apply:
                c.execute(
                    text(
                        "CREATE TEMP TABLE saved_upload_items ON COMMIT DROP "
                        "AS TABLE upload_batch_item"
                    )
                )
                # All FK dependants are listed explicitly. Never use CASCADE.
                c.execute(text("TRUNCATE TABLE " + ",".join(f'public."{t}"' for t in TABLES)))
                c.execute(text("UPDATE saved_upload_items SET parse_revision_id=NULL"))
                c.execute(
                    text("""UPDATE saved_upload_items i SET status='staged',
                    parse_status='not_started', materialization_status='not_started',
                    selection_status='selected', processing_attempt_count=0,
                    worker_lease_id=NULL, worker_lease_expires_at=NULL,
                    error_code=NULL,error_message=NULL
                    FROM artifact_file a WHERE i.artifact_file_id=a.id
                    AND a.artifact_kind='calculation_output' AND a.storage_status='available'""")
                )
                c.execute(text("INSERT INTO upload_batch_item SELECT * FROM saved_upload_items"))
                if (
                    c.execute(text("SELECT count(*) FROM upload_batch_item")).scalar_one()
                    != counts["upload_batch_item"]
                ):
                    raise ValueError("upload item preservation failed")
                c.execute(
                    text("""INSERT INTO artifact_ingestion
                    (artifact_file_id,status,parser_name,parser_version,processing_attempt_count,parser_metadata)
                    SELECT id,'pending','molop',:version,0,
                    jsonb_build_object('reparse_queued',true,'reason','global-derived-reset','queued_at',now())
                    FROM artifact_file WHERE artifact_kind='calculation_output'
                    AND storage_status='available'"""),
                    {"version": MOLOP_VERSION},
                )
                c.execute(
                    text("""UPDATE upload_batch b SET
                    status=CASE WHEN x.staged>0 THEN 'active' ELSE b.status END,
                    succeeded_count=x.succeeded,failed_count=x.failed,cancelled_count=x.cancelled,
                    uploading_count=x.uploading,staged_count=x.staged,processing_count=x.processing
                    FROM (SELECT batch_id,
                    count(*) FILTER(WHERE status='succeeded') succeeded,
                    count(*) FILTER(WHERE status='failed') failed,
                    count(*) FILTER(WHERE status='cancelled') cancelled,
                    count(*) FILTER(WHERE status='uploading') uploading,
                    count(*) FILTER(WHERE status='staged') staged,
                    count(*) FILTER(WHERE status='processing') processing
                    FROM upload_batch_item GROUP BY batch_id) x WHERE b.id=x.batch_id""")
                )
                after = {t: fingerprint(t) for t in sorted(immutable)}
                if before != after:
                    raise ValueError("a preserved table changed; rolling back")
                remaining = {
                    t: c.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar_one()
                    for t in TABLES
                    if t not in ("artifact_ingestion", "upload_batch_item")
                }
                if any(remaining.values()):
                    raise ValueError("derived rows remain; rolling back")
                queued = c.execute(
                    text("SELECT count(*) FROM artifact_ingestion WHERE status='pending'")
                ).scalar_one()
                if queued != eligible:
                    raise ValueError("queue count differs from available files")
                report.update(
                    remaining_derived=remaining, queued_files=queued, immutable_after=after
                )
        report["committed"] = args.apply
        report["finished_at"] = datetime.now(UTC).isoformat()
        _save(args.report, report)
        print(
            json.dumps(
                {"committed": args.apply, "available_files": eligible, "report": str(args.report)}
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
