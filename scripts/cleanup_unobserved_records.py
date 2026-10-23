#!/usr/bin/env python3
"""Archive and remove unobserved ordinary records, excluding all TS displacement lineage.

Dry run by default. Source frames, raw artifacts, endpoint records and their
lineage are never mutated. Referenced topologies and abstraction roots remain.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MappedReaction

PROTECTED = """select t.id from molecular_topology t
where t.identity_schema_version like 'topology-source-map-order%'
or exists(select 1 from transition_state_endpoint e where e.topology_id=t.id)
or exists(select 1 from molecular_topology_derivation d where d.topology_id=t.id
 and (d.reconstruction_method ilike '%possible_pre_post_ts%'
      or d.reconstruction_method ilike '%endpoint%'))"""


def run(session: Session, backup: Path, apply: bool) -> dict:
    session.execute(text("SET LOCAL lock_timeout='5s'"))
    session.execute(text("SET LOCAL statement_timeout='180s'"))
    session.execute(text("CREATE TEMP TABLE protected_topology ON COMMIT DROP AS " + PROTECTED))
    session.execute(text("CREATE UNIQUE INDEX ON protected_topology(id)"))
    session.execute(
        text("""CREATE TEMP TABLE orphan_geometry ON COMMIT DROP AS
      select g.id from geometry g where not exists(select 1 from calculation_frame f where
f.geometry_id=g.id)
      and not exists(select 1 from protected_topology p where p.id=g.topology_id)""")
    )
    # Lock candidates before the final frame/protection checks. A new FK reference
    # cannot race a successful delete; RESTRICT aborts the entire transaction.
    session.execute(
        text(
            "select g.id from geometry g join orphan_geometry o using(id) order by g.id"
            " for update of g"
        )
    )
    session.execute(
        text(
            """delete from orphan_geometry o where exists(select 1 from calculation_frame f where
f.geometry_id=o.id)"""
        )
    )
    archive = {}
    scopes = {
        "geometry": "id in (select id from orphan_geometry)",
        "mapped_reaction_node_geometry": "geometry_id in (select id from orphan_geometry)",
        "mapped_reaction_node_geometry_mapping": (
            "mapped_reaction_node_geometry_id in (select id from "
            "mapped_reaction_node_geometry where geometry_id in (select id from "
            "orphan_geometry))"
        ),
    }
    for table, condition in scopes.items():
        archive[table] = (
            session.execute(text(f"select to_jsonb(t) from {table} t where {condition}"))
            .scalars()
            .all()
        )
    reaction_ids = (
        session.execute(
            text("""select distinct n.mapped_reaction_id from mapped_reaction_node n
      join mapped_reaction_node_geometry ng on ng.mapped_reaction_node_id=n.id
      join orphan_geometry o on o.id=ng.geometry_id""")
        )
        .scalars()
        .all()
    )
    archive["geometry_mol_binary"] = [
        dict(r)
        for r in session.execute(
            text(
                "select g.id,encode(mol_to_pkl(g.mol),'base64') mol from geometry g join "
                "orphan_geometry o using(id)"
            )
        ).mappings()
    ]
    session.execute(
        text(
            "delete from mapped_reaction_node_geometry where geometry_id in (select id "
            "from orphan_geometry)"
        )
    )
    replacement_primaries = (
        session.execute(
            text("""select distinct on (ng.mapped_reaction_node_id,ng.component_key) ng.id
      from mapped_reaction_node_geometry ng
      where ng.mapped_reaction_node_id=any(cast(:ids as uuid[]))
      and not exists(select 1 from mapped_reaction_node_geometry p
       where p.mapped_reaction_node_id=ng.mapped_reaction_node_id and
p.component_key=ng.component_key and p.is_primary)
      order by ng.mapped_reaction_node_id,ng.component_key,ng.coordinate_index,ng.id"""),
            {
                "ids": list(
                    {
                        row["mapped_reaction_node_id"]
                        for row in archive["mapped_reaction_node_geometry"]
                    }
                )
            },
        )
        .scalars()
        .all()
    )
    archive["primary_bindings_before"] = (
        session.execute(
            text(
                "select to_jsonb(t) from mapped_reaction_node_geometry t where "
                "id=any(cast(:ids as uuid[]))"
            ),
            {"ids": replacement_primaries},
        )
        .scalars()
        .all()
    )
    session.execute(
        text(
            "update mapped_reaction_node_geometry set is_primary=true where "
            "id=any(cast(:ids as uuid[]))"
        ),
        {"ids": replacement_primaries},
    )
    session.execute(text("delete from geometry where id in (select id from orphan_geometry)"))
    # Delete only genuinely unreferenced topologies. Abstract reaction templates,
    # concrete memberships and their abstraction edges are valid without frames.
    session.execute(
        text("""CREATE TEMP TABLE orphan_topology ON COMMIT DROP AS
      select t.id from molecular_topology t
      where not exists(select 1 from protected_topology p where p.id=t.id)
      and not t.is_stereo_abstraction_upstream
      and not exists(select 1 from geometry g where g.topology_id=t.id)
      and not exists(select 1 from logical_reaction_participant p where
p.topology_id=t.id)
      and not exists(select 1 from mapped_reaction_participant p where
p.concrete_topology_id=t.id)
      and not exists(select 1 from logical_participant_concrete_topology p where
p.concrete_topology_id=t.id)
      and not exists(select 1 from molecular_topology_abstraction a where
a.specific_topology_id=t.id or a.general_topology_id=t.id)
      and not exists(select 1 from molecular_topology_derivation d join calculation_frame
f on f.topology_derivation_id=d.id where d.topology_id=t.id)""")
    )
    session.execute(
        text(
            "select t.id from molecular_topology t join orphan_topology o using(id) "
            "order by t.id for update of t"
        )
    )
    for table, condition in {
        "molecular_topology": "id in (select id from orphan_topology)",
        "molecular_topology_derivation": "topology_id in (select id from orphan_topology)",
    }.items():
        archive[table] = (
            session.execute(text(f"select to_jsonb(t) from {table} t where {condition}"))
            .scalars()
            .all()
        )
    # PostgreSQL RDKit textual output is not a lossless coordinate/MOL backup.
    for table in ("molecular_topology",):
        scope = "orphan_geometry" if table == "geometry" else "orphan_topology"
        archive[table + "_mol_binary"] = [
            dict(r)
            for r in session.execute(
                text(
                    f"select t.id,encode(mol_to_pkl(t.mol),'base64') mol "
                    f"from {table} t join {scope} o using(id)"
                )
            ).mappings()
        ]
    session.execute(
        text(
            "delete from molecular_topology_derivation where topology_id in (select id "
            "from orphan_topology)"
        )
    )
    session.execute(
        text("delete from molecular_topology where id in (select id from orphan_topology)")
    )
    if reaction_ids:
        reactions = session.exec(
            select(MappedReaction).where(col(MappedReaction.id).in_(sorted(reaction_ids, key=str)))
        ).all()
        mark_mapped_reactions_thermodynamics_dirty(session, reactions)
    session.flush()
    report = {
        "apply": apply,
        "removed": {
            k: len(v)
            for k, v in archive.items()
            if not k.endswith("_mol_binary") and k != "primary_bindings_before"
        },
        "affected_reactions": len(reaction_ids),
        "replacement_primaries": len(replacement_primaries),
        "protected_topologies": session.execute(
            text("select count(*) from protected_topology")
        ).scalar_one(),
    }
    if apply:
        with backup.open("x") as output:
            json.dump({"report": report, "rows": archive}, output, default=str)
            output.flush()
            os.fsync(output.fileno())
        session.commit()
    else:
        session.rollback()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            filter(
                None,
                [
                    os.environ.get(key, ""),
                    urlsplit(settings.database_url).hostname,
                    urlsplit(settings.rustfs_endpoint_url).hostname,
                ],
            )
        )
    with Session(create_engine(settings.database_url)) as session:
        print(json.dumps(run(session, args.backup, args.apply)), flush=True)


if __name__ == "__main__":
    main()
