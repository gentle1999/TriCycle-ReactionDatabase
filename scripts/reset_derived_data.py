"""Clear project-derived rows while preserving identity and raw artifacts.

This is an explicit recovery operation for the historical cross-project data
repair.  It intentionally uses a fixed table allow-list: ``TRUNCATE ...
CASCADE`` is followed by checks that the preserved identity and ArtifactFile
rows are unchanged.  The command refuses to run without an explicit
confirmation flag and without the project-owned schema head.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import text

from tricycle_reaction_db.db.session import session_factory

PRESERVED_TABLES = (
    "artifact_file",
    "user_account",
    "external_identity",
    "organization",
    "organization_membership",
    "project",
    "project_membership",
    "project_invitation",
    "audit_event",
)

# These tables contain parse facts, chemistry identities, reaction relations,
# calculations, queues, projections, or recovery state.  They are all
# reproducible from the preserved ArtifactFile object bytes.
DERIVED_TABLES = (
    "artifact_ingestion",
    "upload_batch_item",
    "upload_batch",
    "parse_revision",
    "calculation_protocol",
    "calculation_segment",
    "calculation_frame",
    "frame_energy_result",
    "energy_observation",
    "geometry_optimization_result",
    "vibration_result",
    "calculation_status_result",
    "scientific_array_assignment",
    "scientific_array",
    "thermochemistry_result",
    "molecular_orbital_result",
    "charge_spin_population_result",
    "atomic_population_series",
    "polarizability_result",
    "nmr_result",
    "nmr_shielding_tensor",
    "bond_order_result",
    "total_spin_result",
    "single_point_property_result",
    "electronic_state_set",
    "electronic_state",
    "electronic_configuration",
    "multireference_result",
    "implicit_solvation_result",
    "molecular_formula",
    "molecular_topology",
    "molecular_topology_abstraction",
    "molecular_topology_derivation",
    "geometry",
    "logical_participant_concrete_topology",
    "logical_reaction_participant",
    "logical_reaction",
    "mapped_reaction_thermodynamic_profile",
    "mapped_reaction_edge",
    "mapped_reaction_node_geometry_mapping",
    "mapped_reaction_node_geometry",
    "mapped_reaction_node",
    "mapped_reaction_participant",
    "mapped_reaction",
    "transition_state_endpoint",
    "transition_state_inference",
    "workflow_manifest",
    "manifest_artifact_binding",
    "project_geometry_catalog_count",
    "project_geometry_catalog",
    "derived_data_isolation_quarantine",
    "storage_garbage_collection_run",
    "storage_garbage_collection_state",
)

REQUIRED_PROJECT_SLUGS = ("autode", "ground-truth")
EXPECTED_SCHEMA_HEAD = "0038_geometry_match_index"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "clear all reproducible project-derived data while preserving "
            "users, projects, audit records, and ArtifactFile rows"
        )
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement that the allow-listed derived rows will be deleted",
    )
    parser.add_argument(
        "--project-slug",
        action="append",
        dest="project_slugs",
        help="project slug that must exist before clearing; repeat to override the defaults",
    )
    return parser


async def _count(session: Any, table_name: str) -> int:
    statement = text(f'SELECT count(*) FROM "{table_name}"')
    return int((await session.execute(statement)).scalar_one())


async def _run(args: argparse.Namespace) -> int:
    if not args.confirm:
        raise ValueError("refusing to clear derived data without --confirm")

    expected_slugs = tuple(args.project_slugs or REQUIRED_PROJECT_SLUGS)
    async with session_factory() as session, session.begin():
        revision = str(
            (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        )
        if revision != EXPECTED_SCHEMA_HEAD:
            raise RuntimeError(f"database must be at {EXPECTED_SCHEMA_HEAD}, found {revision}")

        projects = (
            await session.execute(text("SELECT slug, id, name FROM project ORDER BY slug"))
        ).all()
        project_by_slug = {
            str(slug): (str(project_id), str(name)) for slug, project_id, name in projects
        }
        missing = [slug for slug in expected_slugs if slug not in project_by_slug]
        if missing:
            raise RuntimeError(f"required project(s) are missing: {', '.join(missing)}")

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
            {"lock_name": "tricycle-reset-derived-data"},
        )

        preserved_before = {
            table_name: await _count(session, table_name) for table_name in PRESERVED_TABLES
        }
        derived_before = {
            table_name: await _count(session, table_name) for table_name in DERIVED_TABLES
        }
        quoted_tables = ", ".join(f'"{table_name}"' for table_name in DERIVED_TABLES)
        await session.execute(text(f"TRUNCATE TABLE {quoted_tables} RESTART IDENTITY CASCADE"))
        preserved_after = {
            table_name: await _count(session, table_name) for table_name in PRESERVED_TABLES
        }
        if preserved_after != preserved_before:
            raise RuntimeError(
                "the derived-table truncate changed a preserved table; transaction rolled back"
            )
        derived_after = {
            table_name: await _count(session, table_name) for table_name in DERIVED_TABLES
        }
        non_empty = {name: count for name, count in derived_after.items() if count}
        if non_empty:
            raise RuntimeError(f"derived rows remain after reset: {non_empty}")

    print(
        json.dumps(
            {
                "status": "reset",
                "schema_head": EXPECTED_SCHEMA_HEAD,
                "projects": {
                    slug: {"id": project_by_slug[slug][0], "name": project_by_slug[slug][1]}
                    for slug in expected_slugs
                },
                "preserved_counts": preserved_before,
                "cleared_counts": {name: count for name, count in derived_before.items() if count},
                "remaining_derived_rows": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run(_parser().parse_args())))
    except (RuntimeError, ValueError, OSError) as error:
        print(f"derived-data reset failed: {error}", flush=True)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
