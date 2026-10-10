"""Backfill mapping-wide search costs without changing energy/profile selection.

Defaults to a dry run. --apply requires a new backup JSONL path and commits
one locked mapping batch at a time. Source artifacts and frames are untouched.
"""

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import create_engine, text, update
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_runtime import (
    RUNTIME_COLUMNS,
    load_mapped_reaction_runtimes,
)
from tricycle_reaction_db.core.chemistry_config import MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MappedReaction, MappedReactionThermodynamicProfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all-projects", action="store_true")
    scope.add_argument("--project-id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size <= 0 or (args.apply and args.backup is None):
        parser.error("--batch-size must be positive; --apply requires --backup")
    settings = get_settings()
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            filter(None, [os.getenv(key, ""), urlsplit(settings.database_url).hostname])
        )
    engine = create_engine(settings.database_url)
    backup = args.backup.open("x") if args.apply else None
    try:
        with Session(engine) as session:
            statement = (
                select(col(MappedReaction.id))
                .join(MappedReactionThermodynamicProfile)
                .where(
                    MappedReactionThermodynamicProfile.policy_version
                    == MAPPED_REACTION_THERMODYNAMICS_POLICY_VERSION
                )
                .distinct()
                .order_by(col(MappedReaction.id))
            )
            if args.project_id is not None:
                statement = statement.where(MappedReaction.project_id == args.project_id)
            ids = [mid for mid in session.exec(statement).all() if isinstance(mid, UUID)]
        counts: Counter[str] = Counter()
        fingerprint = (
            "md5((to_jsonb(p)"
            + "".join(f" - '{column}'" for column in RUNTIME_COLUMNS)
            + ")::text)"
        )
        query = text(
            f"SELECT p.id,p.mapped_reaction_id,{','.join('p.' + c for c in RUNTIME_COLUMNS)}, "
            f"{fingerprint} unchanged_fields_hash FROM mapped_reaction_thermodynamic_profile p "
            "WHERE mapped_reaction_id=ANY(:ids)"
        )
        print(json.dumps({"mappings": len(ids), "apply": args.apply}), flush=True)
        for offset in range(0, len(ids), args.batch_size):
            batch = ids[offset : offset + args.batch_size]
            with Session(engine) as session:
                mappings = session.exec(
                    select(MappedReaction)
                    .where(col(MappedReaction.id).in_(batch))
                    .order_by(col(MappedReaction.id))
                    .with_for_update()
                ).all()
                times = load_mapped_reaction_runtimes(session, batch)
                counts["known_total_mappings"] += sum(
                    t["total_running_time_seconds"] is not None for t in times.values()
                )
                before = [
                    dict(row)
                    for row in session.connection().execute(query, {"ids": batch}).mappings()
                ]
                changed = {
                    row["mapped_reaction_id"]
                    for row in before
                    if any(
                        row[column] != times[row["mapped_reaction_id"]][column]
                        for column in RUNTIME_COLUMNS
                    )
                }
                if backup is not None:
                    backup.write(
                        json.dumps({"batch": offset, "profiles": before}, default=str) + "\n"
                    )
                    backup.flush()
                    os.fsync(backup.fileno())
                    session.execute(
                        text("SET LOCAL tricycle.defer_profile_source_visibility = 'on'")
                    )
                    for mapping in mappings:
                        assert isinstance(mapping.id, UUID)
                        session.execute(
                            update(MappedReactionThermodynamicProfile)
                            .where(
                                col(MappedReactionThermodynamicProfile.mapped_reaction_id)
                                == mapping.id
                            )
                            .values(**times[mapping.id])
                        )
                    after = [
                        dict(row)
                        for row in session.connection().execute(query, {"ids": batch}).mappings()
                    ]
                    assert {r["id"]: r["unchanged_fields_hash"] for r in before} == {
                        r["id"]: r["unchanged_fields_hash"] for r in after
                    }, "energy/profile fields changed"
                    assert all(
                        all(r[c] == times[r["mapped_reaction_id"]][c] for c in RUNTIME_COLUMNS)
                        for r in after
                    )
                    session.commit()
                else:
                    session.rollback()
                counts["profiles"] += len(before)
                counts["changed_mappings"] += len(changed)
            print(
                json.dumps({"processed": offset + len(batch), "changed": len(changed)}), flush=True
            )
        print(
            json.dumps(
                {
                    "apply": args.apply,
                    **counts,
                    "energy_fields_changed": False,
                    "source_files_requeued": 0,
                }
            ),
            flush=True,
        )
    finally:
        if backup is not None:
            backup.close()
        engine.dispose()


if __name__ == "__main__":
    main()
