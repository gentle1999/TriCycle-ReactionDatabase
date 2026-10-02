"""Fill missing endpoint geometry associations without reparsing source files.

Plan only by default. --apply uses bounded transactions and the same validated
binding service as ingestion; reruns select only associations still missing.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Text, cast, create_engine, func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import Select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    _bind_participant_geometry,
    _reaction_geometry_predicate,
    preload_reconciliation_context,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    Geometry,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
)


def candidates() -> Select[Any]:
    p, n, g, b, m = (
        MappedReactionParticipant,
        MappedReactionNode,
        Geometry,
        MappedReactionNodeGeometry,
        MappedReactionNodeGeometryMapping,
    )
    return (
        select(col(p.id).label("participant_id"), col(g.id).label("geometry_id"))
        .join(n, col(n.mapped_reaction_id) == col(p.mapped_reaction_id))
        .join(MappedReaction, col(MappedReaction.id) == col(p.mapped_reaction_id))
        .join(g, col(g.topology_id) == col(p.concrete_topology_id))
        .where(
            cast(col(n.role), Text) == cast(col(p.side), Text),
            col(g.project_id) == col(MappedReaction.project_id),
            _reaction_geometry_predicate(),
            ~select(b.id)
            .join(m, col(m.mapped_reaction_node_geometry_id) == col(b.id))
            .where(
                col(b.mapped_reaction_node_id) == col(n.id),
                col(b.mapped_reaction_participant_id) == col(p.id),
                col(b.geometry_id) == col(g.id),
                col(m.verified).is_(True),
            )
            .correlate(n, p, g)
            .exists(),
        )
        .distinct()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    settings = get_settings()
    host = urlsplit(settings.database_url).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            dict.fromkeys(filter(None, [*os.getenv(key, "").split(","), host]))
        )
    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        count = session.exec(select(func.count()).select_from(candidates().subquery())).one()
    print(json.dumps({"missing_pairs": count, "apply": args.apply}), flush=True)
    if not args.apply:
        return
    completed = 0
    while True:
        with Session(engine) as session:
            pairs = session.exec(
                candidates().order_by("participant_id", "geometry_id").limit(args.batch_size)
            ).all()
            if not pairs:
                break
            participants = {
                p.id: p
                for p in session.exec(
                    select(MappedReactionParticipant).where(
                        col(MappedReactionParticipant.id).in_([p for p, _ in pairs])
                    )
                ).all()
            }
            geometries = {
                g.id: g
                for g in session.exec(
                    select(Geometry).where(col(Geometry.id).in_([g for _, g in pairs]))
                ).all()
            }
            by_project = defaultdict(list)
            for participant_id, geometry_id in pairs:
                by_project[geometries[geometry_id].project_id].append((participant_id, geometry_id))
            for project_id, project_pairs in by_project.items():
                cache = ReconciliationBatchCache()
                if project_id is None:
                    raise ValueError("endpoint Geometry must belong to a project")
                reactions: dict[UUID, MappedReaction] = {}
                preload_reconciliation_context(
                    session,
                    {geometries[g].topology_id for _, g in project_pairs},
                    project_id=project_id,
                    participants_by_topology={},
                    mapped_reactions_by_id=reactions,
                    cache=cache,
                )
                affected = {}
                for participant_id, geometry_id in project_pairs:
                    participant = participants[participant_id]
                    reaction = reactions[participant.mapped_reaction_id]
                    bindings = _bind_participant_geometry(
                        session,
                        participant=participant,
                        geometry=geometries[geometry_id],
                        mapped_reaction=reaction,
                        cache=cache,
                        thermodynamic_property_verified=True,
                    )
                    if not bindings or any(
                        binding.id is None
                        or not cache.mappings_by_node_geometry_id[binding.id].verified
                        for binding in bindings
                    ):
                        raise ValueError(
                            "endpoint reconciliation did not produce verified mappings"
                        )
                    affected[reaction.id] = reaction
                mark_mapped_reactions_thermodynamics_dirty(session, tuple(affected.values()))
            session.commit()
            completed += len(pairs)
            print(json.dumps({"committed_pairs": completed}), flush=True)
    print(
        json.dumps({"complete": True, "committed_pairs": completed, "remaining_pairs": 0}),
        flush=True,
    )


if __name__ == "__main__":
    main()
