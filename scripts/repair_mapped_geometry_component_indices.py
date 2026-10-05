"""Audit or repair endpoint Geometry component slots against participant indices."""

from __future__ import annotations

import argparse
import json
import os
import socket
from contextlib import suppress
from typing import Any, cast
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

from tricycle_reaction_db.application.services.reactions import (
    reindex_mapped_reaction_endpoint_geometry_components,
)
from tricycle_reaction_db.core.config import get_settings


def _configure_database_proxy_bypass() -> None:
    url = make_url(get_settings().database_url)
    hosts: set[str] = set()
    if url.host:
        hosts.add(str(url.host))
        with suppress(OSError):
            hosts.update(
                cast(str, address[4][0])
                for address in socket.getaddrinfo(
                    url.host,
                    url.port or 5432,
                    type=socket.SOCK_STREAM,
                )
            )
    for key in ("NO_PROXY", "no_proxy"):
        existing = [item for item in os.getenv(key, "").split(",") if item]
        os.environ[key] = ",".join(dict.fromkeys([*existing, *sorted(hosts)]))


def _scope_clause(project_id: UUID | None) -> tuple[str, dict[str, str]]:
    if project_id is None:
        return "", {}
    return " AND mr.project_id = :project_id", {"project_id": str(project_id)}


def _mismatches(session: Session, project_id: UUID | None) -> list[dict[str, Any]]:
    scope, params = _scope_clause(project_id)
    rows = session.execute(
        text(
            """
            SELECT n.mapped_reaction_id, mr.project_id, count(*) AS binding_count
            FROM mapped_reaction_node_geometry b
            JOIN mapped_reaction_node n ON n.id = b.mapped_reaction_node_id
            JOIN mapped_reaction mr ON mr.id = n.mapped_reaction_id
            JOIN mapped_reaction_participant p ON p.id = b.mapped_reaction_participant_id
            WHERE n.role IN ('reactant', 'product')
              AND p.mapped_reaction_id = n.mapped_reaction_id
              AND ((n.role = 'reactant' AND p.side = 'reactant')
                OR (n.role = 'product' AND p.side = 'product'))
              AND (b.component_key <> p.side::text || ':' || p.template_index::text
                OR b.component_index <> p.template_index)
            """
            + scope
            + " GROUP BY n.mapped_reaction_id, mr.project_id "
            "ORDER BY mr.project_id, n.mapped_reaction_id"
        ),
        params,
    ).mappings()
    return [dict(row) for row in rows]


def _invalid_endpoint_binding_count(session: Session, project_id: UUID | None) -> int:
    scope, params = _scope_clause(project_id)
    return int(
        session.execute(
            text(
                """
                SELECT count(*)
                FROM mapped_reaction_node_geometry b
                JOIN mapped_reaction_node n ON n.id = b.mapped_reaction_node_id
                JOIN mapped_reaction mr ON mr.id = n.mapped_reaction_id
                LEFT JOIN mapped_reaction_participant p
                  ON p.id = b.mapped_reaction_participant_id
                WHERE n.role IN ('reactant', 'product')
                  AND (p.id IS NULL OR p.mapped_reaction_id <> n.mapped_reaction_id
                    OR (n.role = 'reactant' AND p.side <> 'reactant')
                    OR (n.role = 'product' AND p.side <> 'product'))
                """
                + scope
            ),
            params,
        ).scalar_one()
    )


def run(*, project_id: UUID | None, apply: bool) -> int:
    _configure_database_proxy_bypass()
    engine = create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 10,
            "options": "-c lock_timeout=5000 -c statement_timeout=300000",
        },
    )
    try:
        with Session(engine) as session:
            if apply:
                # Keep ingestion from inserting a new slot while a component
                # permutation is being repaired. This lock lasts only for the
                # bounded index rewrite transaction.
                session.execute(
                    text("LOCK TABLE mapped_reaction_node_geometry IN SHARE ROW EXCLUSIVE MODE")
                )
            if _invalid_endpoint_binding_count(session, project_id):
                raise RuntimeError(
                    "scope contains endpoint Geometry bindings that cannot be safely reindexed"
                )
            planned = _mismatches(session, project_id)
            if not apply:
                session.rollback()
                print(
                    json.dumps(
                        {
                            "apply": False,
                            "affected_projects": len({row["project_id"] for row in planned}),
                            "affected_reactions": len(planned),
                            "affected_bindings": sum(int(row["binding_count"]) for row in planned),
                        }
                    )
                )
                return 0

            repaired_bindings = 0
            for row in planned:
                repaired_bindings += reindex_mapped_reaction_endpoint_geometry_components(
                    session,
                    row["mapped_reaction_id"],
                )
            remaining = _mismatches(session, project_id)
            if remaining:
                raise RuntimeError("endpoint Geometry component reindex left mismatched bindings")
            session.commit()
            print(
                json.dumps(
                    {
                        "apply": True,
                        "affected_projects": len({row["project_id"] for row in planned}),
                        "affected_reactions": len(planned),
                        "affected_bindings": sum(int(row["binding_count"]) for row in planned),
                        "repaired_bindings": repaired_bindings,
                        "remaining_mismatched_reactions": 0,
                    }
                )
            )
            return 0
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project-id", type=UUID)
    scope.add_argument("--all-projects", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the reindex; without this option the command only reports the plan",
    )
    args = parser.parse_args()
    raise SystemExit(run(project_id=args.project_id, apply=args.apply))


if __name__ == "__main__":
    main()
