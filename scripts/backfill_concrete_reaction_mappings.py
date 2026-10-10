"""Materialize missing concrete reactions from existing project-owned evidence.

Default: run the normal expansion and reconciliation services, then roll back
each logical reaction. --apply commits each reaction separately. Raw artifacts,
parse revisions, source geometries and source atom-map vectors are not changed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from time import monotonic
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import create_engine
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
)
from tricycle_reaction_db.application.services.molecular_geometry import GeometryPersistenceContext
from tricycle_reaction_db.application.services.molop_artifact_ingestion import (
    reconcile_molop_geometry_context,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
)
from tricycle_reaction_db.application.services.reaction_mapping_resolution import (
    ensure_mapped_reactions_for_logical_reaction,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MolecularTopology,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project-id", type=UUID)
    scope.add_argument("--all-projects", action="store_true")
    parser.add_argument("--logical-reaction-id", type=UUID)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    host = urlsplit(settings.database_url).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            dict.fromkeys(filter(None, [*os.getenv(key, "").split(","), host]))
        )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        statement = (
            select(col(LogicalReaction.id), col(LogicalReaction.project_id))
            .join(LogicalReactionParticipant)
            .join(MolecularTopology)
            .where(
                col(MolecularTopology.is_stereo_abstraction_upstream).is_(True),
                col(MolecularTopology.project_id) == col(LogicalReaction.project_id),
                select(col(MappedReaction.id))
                .where(col(MappedReaction.logical_reaction_id) == col(LogicalReaction.id))
                .exists(),
            )
            .distinct()
            .order_by(col(LogicalReaction.project_id), col(LogicalReaction.id))
        )
        if args.project_id is not None:
            statement = statement.where(col(LogicalReaction.project_id) == args.project_id)
        if args.logical_reaction_id is not None:
            statement = statement.where(col(LogicalReaction.id) == args.logical_reaction_id)
        if args.limit is not None:
            if args.limit <= 0:
                parser.error("--limit must be positive")
            statement = statement.limit(args.limit)
        with Session(engine) as session:
            candidates = session.exec(statement).all()
        print(json.dumps({"candidates": len(candidates), "apply": args.apply}), flush=True)
        created_by_project: Counter[str] = Counter()
        failed = 0
        for index, (reaction_id, project_id) in enumerate(candidates, start=1):
            started = monotonic()
            try:
                with Session(engine) as session:
                    reaction = session.get(LogicalReaction, reaction_id)
                    if reaction is None or reaction.project_id != project_id:
                        raise ValueError("logical reaction disappeared or changed project")
                    context = GeometryPersistenceContext(project_id=project_id)
                    cache = ReconciliationBatchCache()
                    context.reconciliation_cache = cache
                    created = ensure_mapped_reactions_for_logical_reaction(
                        session,
                        reaction,
                        topology_context=context,
                        reconciliation_cache=cache,
                        refresh_thermodynamics=False,
                    )
                    reconcile_molop_geometry_context(session, context, refresh_thermodynamics=False)
                    mark_mapped_reactions_thermodynamics_dirty(session, created)
                    created_ids = [str(mapping.id) for mapping in created]
                    if args.apply:
                        session.commit()
                    else:
                        session.rollback()
                created_by_project[str(project_id)] += len(created_ids)
                print(
                    json.dumps(
                        {
                            "index": index,
                            "logical_reaction_id": str(reaction_id),
                            "project_id": str(project_id),
                            "created": len(created_ids),
                            "mapped_reaction_ids": created_ids,
                            "seconds": round(monotonic() - started, 2),
                        }
                    ),
                    flush=True,
                )
            except Exception as error:
                failed += 1
                print(
                    json.dumps(
                        {
                            "logical_reaction_id": str(reaction_id),
                            "error_type": type(error).__name__,
                            "error": str(error),
                        }
                    ),
                    flush=True,
                )
        print(
            json.dumps(
                {
                    "created": sum(created_by_project.values()),
                    "created_by_project": dict(created_by_project),
                    "failed": failed,
                    "apply": args.apply,
                    "source_files_requeued": 0,
                }
            ),
            flush=True,
        )
        if failed:
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
