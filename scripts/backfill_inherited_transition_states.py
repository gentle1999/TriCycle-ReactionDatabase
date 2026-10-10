"""Restore verified TS evidence on concrete mappings of the same logical reaction.

The default plan validates atom correspondences without writing data. --apply
uses the ingestion evidence-sharing service in one transaction per reaction pair
and queues thermodynamic refreshes. Existing TS bindings and raw data are retained.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import create_engine, func
from sqlalchemy.orm import aliased
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    _validated_target_ts_geometry_atom_maps,
    share_mapped_reaction_evidence,
)
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
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


def candidates(project_id: UUID | None, logical_reaction_id: UUID | None = None) -> Any:
    target = aliased(MappedReaction, name="target_reaction")
    target_node = aliased(MappedReactionNode, name="target_node")
    target_binding = aliased(MappedReactionNodeGeometry, name="target_binding")
    node, binding, mapping = (
        MappedReactionNode,
        MappedReactionNodeGeometry,
        MappedReactionNodeGeometryMapping,
    )
    target_has_geometry = (
        select(col(target_binding.id))
        .join(target_node, col(target_node.id) == col(target_binding.mapped_reaction_node_id))
        .where(
            col(target_node.mapped_reaction_id) == col(target.id),
            col(target_node.role) == "transition_state",
            col(target_binding.geometry_id) == col(binding.geometry_id),
        )
        .correlate(target, binding)
        .exists()
    )
    statement: Any = (
        select(col(MappedReaction.id).label("source_id"), col(target.id).label("target_id"))
        .select_from(MappedReaction)
        .join(node, col(node.mapped_reaction_id) == col(MappedReaction.id))
        .join(binding, col(binding.mapped_reaction_node_id) == col(node.id))
        .join(mapping, col(mapping.mapped_reaction_node_geometry_id) == col(binding.id))
        .join(
            target,
            col(target.logical_reaction_id) == col(MappedReaction.logical_reaction_id),
        )
        .where(
            col(MappedReaction.project_id).is_not(None),
            col(target.project_id) == col(MappedReaction.project_id),
            col(target.id) != col(MappedReaction.id),
            col(target.mapped_reaction_key).startswith("mapping:"),
            col(node.role) == "transition_state",
            col(mapping.verified).is_(True),
            col(mapping.mapping_method) == REACTION_TS_GEOMETRY_LINK_METHOD,
            col(mapping.mapping_version) == REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
            ~target_has_geometry,
        )
        .distinct()
    )
    if project_id is not None:
        statement = statement.where(col(MappedReaction.project_id) == project_id)
    if logical_reaction_id is not None:
        statement = statement.where(col(MappedReaction.logical_reaction_id) == logical_reaction_id)
    return statement


def _ts_geometry_ids(session: Session, reaction_id: UUID) -> set[UUID]:
    return set(
        session.exec(
            select(col(MappedReactionNodeGeometry.geometry_id))
            .join(MappedReactionNode)
            .where(
                col(MappedReactionNode.mapped_reaction_id) == reaction_id,
                col(MappedReactionNode.role) == "transition_state",
            )
        ).all()
    )


def _valid_missing_geometries(
    session: Session, source: MappedReaction, target: MappedReaction
) -> set[UUID]:
    if not isinstance(target.id, UUID):
        raise ValueError("target reaction must have a persisted UUID")
    source_participants = session.exec(
        select(MappedReactionParticipant).where(
            col(MappedReactionParticipant.mapped_reaction_id) == source.id
        )
    ).all()
    target_participants = session.exec(
        select(MappedReactionParticipant).where(
            col(MappedReactionParticipant.mapped_reaction_id) == target.id
        )
    ).all()
    existing = _ts_geometry_ids(session, target.id)
    rows = session.exec(
        select(Geometry, MappedReactionNodeGeometryMapping)
        .join(
            MappedReactionNodeGeometry,
            col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
        )
        .join(
            MappedReactionNode,
            col(MappedReactionNode.id) == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
        )
        .join(
            MappedReactionNodeGeometryMapping,
            col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
            == col(MappedReactionNodeGeometry.id),
        )
        .where(
            col(MappedReactionNode.mapped_reaction_id) == source.id,
            col(MappedReactionNode.role) == "transition_state",
            col(Geometry.project_id) == source.project_id,
        )
    ).all()
    return {
        geometry.id
        for geometry, mapping in rows
        if isinstance(geometry.id, UUID)
        and geometry.id not in existing
        and _validated_target_ts_geometry_atom_maps(
            session,
            source_mapped_reaction=source,
            target_mapped_reaction=target,
            source_participants=source_participants,
            target_participants=target_participants,
            source_mapping=mapping,
            geometry=geometry,
        )
        is not None
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project-id", type=UUID)
    scope.add_argument("--all-projects", action="store_true")
    parser.add_argument("--logical-reaction-id", type=UUID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    host = urlsplit(settings.database_url).hostname
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            dict.fromkeys(filter(None, [*os.getenv(key, "").split(","), host]))
        )
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    planned: set[tuple[UUID, UUID]] = set()
    affected: set[UUID] = set()
    affected_projects: set[UUID] = set()
    skipped = 0
    try:
        with Session(engine) as session:
            pairs = session.exec(candidates(args.project_id, args.logical_reaction_id)).all()
        print(
            json.dumps(
                {
                    "candidate_pairs": len(pairs),
                    "apply": args.apply,
                    "all_projects": args.all_projects,
                }
            ),
            flush=True,
        )
        for source_id, target_id in pairs:
            with Session(engine) as session:
                source, target = (
                    session.get(MappedReaction, source_id),
                    session.get(MappedReaction, target_id),
                )
                if source is None or target is None:
                    raise ValueError("reaction disappeared during the backfill")
                if not isinstance(target.project_id, UUID):
                    raise ValueError("target reaction must belong to a project")
                before = _ts_geometry_ids(session, target_id)
                missing = _valid_missing_geometries(session, source, target)
                if not missing:
                    if _ts_geometry_ids(session, source_id) - before:
                        skipped += 1
                    continue
                planned.update((target_id, geometry_id) for geometry_id in missing)
                if args.apply:
                    share_mapped_reaction_evidence(
                        session,
                        source_mapped_reaction=source,
                        target_mapped_reaction=target,
                    )
                    if not missing.issubset(_ts_geometry_ids(session, target_id)):
                        raise ValueError(
                            "evidence sharing did not restore all validated TS bindings"
                        )
                    mark_mapped_reactions_thermodynamics_dirty(session, (target,))
                    session.commit()
                    affected.add(target_id)
                    affected_projects.add(target.project_id)
                    print(
                        json.dumps(
                            {
                                "target_id": str(target_id),
                                "project_id": str(target.project_id),
                                "inherited_ts_count": len(missing),
                            }
                        ),
                        flush=True,
                    )
        with Session(engine) as session:
            remaining = session.exec(
                select(func.count()).select_from(
                    candidates(args.project_id, args.logical_reaction_id).subquery()
                )
            ).one()
        print(
            json.dumps(
                {
                    "apply": args.apply,
                    "candidate_pairs": len(pairs),
                    "validated_missing_bindings": len(planned),
                    "updated_reactions": len(affected),
                    "updated_projects": len(affected_projects),
                    "skipped_pairs": skipped,
                    "remaining_pairs": remaining,
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()
    if args.apply and remaining:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
