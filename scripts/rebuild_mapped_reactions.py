"""Re-infer all scoped TS reactions from raw files, then retire obsolete reaction rows.

Defaults to a rollback-only preflight. --apply commits one TS at a time; old
reaction cleanup starts only after every source in the saved plan succeeds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, text
from sqlmodel import Session, col, select

# Support both `python scripts/...py` and module/test imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.backfill_transition_state_endpoints import (  # noqa: E402
    _mark_reinference_succeeded,
    _refresh_latest_ingestion_status,
)
from tricycle_reaction_db.application.services.artifact_upload_types import _SuccessfulInference
from tricycle_reaction_db.application.services.artifact_uploads import (
    infer_transition_states_from_calculation_output,
)
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    REACTION_INDEX_POLICY,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    TransitionStateInference,
)
from tricycle_reaction_db.domain.enums import MappedReactionKind, MappedReactionNodeRole
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings

STATE_VERSION = 1
CHECKPOINT_KEY = "mapped_reaction_rebuild"


class RebuildBlocked(ValueError):
    """Source evidence or a concurrent change prevents a safe update."""


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(value, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _configure_backend_access() -> None:
    hosts = {
        urlsplit(get_settings().database_url).hostname,
        urlsplit(RustFSSettings().endpoint_url).hostname,
    }
    for key in ("NO_PROXY", "no_proxy"):
        existing = [item for item in os.getenv(key, "").split(",") if item]
        os.environ[key] = ",".join(dict.fromkeys(existing + sorted(host for host in hosts if host)))


def _is_current(reaction: MappedReaction) -> bool:
    return _current_form(
        reaction.mapped_reaction_smiles, reaction.mapping_hash, reaction.normalization_metadata
    )


def _current_form(smiles: str, mapping_hash: str, metadata: dict[str, Any] | None) -> bool:
    metadata = metadata or {}
    return (
        metadata.get("policy") == REACTION_INDEX_POLICY
        and bool(metadata.get("rdkit_version"))
        and metadata.get("selected_mapped_reaction_smiles") == smiles
        and mapping_hash == sha256(smiles.encode()).hexdigest()
    )


def _snapshot(
    session: Session, project_id: UUID | None, database_key: str, *, include_evidence: bool = True
) -> dict[str, Any]:
    params = {"project": project_id}
    # Snapshot all existing reactions, including already-normalized ones. Full
    # update re-infers their TS sources too; no old reaction string is input.
    reactions = (
        session.execute(
            text("""
        SELECT id, logical_reaction_id, project_id, mapped_reaction_smiles, mapping_hash,
               normalization_metadata, mapped_reaction_key, mapped_reaction_kind
        FROM mapped_reaction
        WHERE CAST(:project AS uuid) IS NULL OR project_id=CAST(:project AS uuid)
        ORDER BY id
    """),
            params,
        )
        .mappings()
        .all()
    )
    inferences = (
        session.execute(
            text("""
        SELECT i.id, i.mapped_reaction_id, i.logical_reaction_id, i.calculation_frame_id,
               i.parse_revision_id, i.file_frame_index, a.id AS artifact_id,
               a.content_sha256, a.project_id, i.inference_settings
        FROM transition_state_inference i
        JOIN parse_revision r ON r.id=i.parse_revision_id
        JOIN artifact_file a ON a.id=r.artifact_file_id
        WHERE i.mapped_reaction_id IS NOT NULL
          AND (CAST(:project AS uuid) IS NULL OR a.project_id=CAST(:project AS uuid))
        ORDER BY a.id, i.file_frame_index, i.id
    """),
            params,
        )
        .mappings()
        .all()
    )

    def encode(rows: Any) -> list[dict[str, Any]]:
        return [
            {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}
            for row in rows
        ]

    plan = {
        "database_key": database_key,
        "project_id": str(project_id) if project_id else None,
        "policy": REACTION_INDEX_POLICY,
        "reactions": encode(reactions),
        "inferences": encode(inferences),
    }
    if include_evidence:
        associations = (
            session.execute(
                text("""
            SELECT n.mapped_reaction_id, n.node_key, n.role, g.id AS association_id,
                   g.geometry_id, g.mapped_reaction_participant_id, g.component_key,
                   m.geometry_atom_map_numbers, m.mapped_smiles, m.mapping_method,
                   m.mapping_version, m.verified
            FROM mapped_reaction_node n
            JOIN mapped_reaction r ON r.id=n.mapped_reaction_id
            JOIN mapped_reaction_node_geometry g ON g.mapped_reaction_node_id=n.id
            LEFT JOIN mapped_reaction_node_geometry_mapping m
              ON m.mapped_reaction_node_geometry_id=g.id
            WHERE CAST(:project AS uuid) IS NULL OR r.project_id=CAST(:project AS uuid)
            ORDER BY n.mapped_reaction_id, g.id
        """),
                params,
            )
            .mappings()
            .all()
        )
        plan["original_associations"] = encode(associations)
    return {
        "version": STATE_VERSION,
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "plan": plan,
        "plan_digest": _digest(plan),
        "results": {},
        "cleanup": {},
    }


def _validate_state(state: dict[str, Any], project_id: UUID | None, database_key: str) -> None:
    plan = state["plan"]
    if (
        state.get("version") != STATE_VERSION
        or state.get("plan_digest") != _digest(plan)
        or plan.get("database_key") != database_key
        or plan.get("project_id") != (str(project_id) if project_id else None)
        or plan.get("policy") != REACTION_INDEX_POLICY
    ):
        raise RebuildBlocked("state file does not match this database, project or policy")
    UUID(state["run_id"])


def _verify_binding(session: Session, inference: TransitionStateInference) -> MappedReaction:
    reaction = session.get(MappedReaction, inference.mapped_reaction_id)
    frame = session.get(CalculationFrame, inference.calculation_frame_id)
    if reaction is None or frame is None or not _is_current(reaction):
        raise RebuildBlocked("missing source frame or selected reaction normal form")
    source_maps = inference.inference_settings.get("source_atom_map_numbers")
    frame_order = list(frame.observed_to_geometry_atom_indices)
    if (
        not isinstance(source_maps, list)
        or not all(type(number) is int for number in source_maps)
        or sorted(source_maps) != list(range(1, len(frame_order) + 1))
        or sorted(frame_order) != list(range(len(frame_order)))
        or inference.inference_settings.get("canonical_mapped_reaction_smiles")
        != reaction.mapped_reaction_smiles
    ):
        raise RebuildBlocked("source permutation or selected reaction snapshot is inconsistent")
    rows = session.exec(
        select(MappedReactionNodeGeometryMapping)
        .join(MappedReactionNodeGeometry)
        .join(MappedReactionNode)
        .where(
            MappedReactionNode.mapped_reaction_id == reaction.id,
            MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
            MappedReactionNodeGeometry.geometry_id == frame.geometry_id,
            MappedReactionNodeGeometryMapping.verified == True,  # noqa: E712
        )
    ).all()
    if not any(
        len(row.geometry_atom_map_numbers) == len(frame_order)
        and [row.geometry_atom_map_numbers[index] for index in frame_order] == source_maps
        for row in rows
    ):
        raise RebuildBlocked("TS association does not reproduce the source-frame permutation")
    return reaction


def _resume_complete(
    session: Session,
    inference: TransitionStateInference,
    entry: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    marker = inference.inference_settings.get(CHECKPOINT_KEY, {})
    if marker.get("run_id") != state["run_id"]:
        return False
    if (
        marker.get("source_sha256") != entry["content_sha256"]
        or marker.get("policy") != REACTION_INDEX_POLICY
        or marker.get("old_reaction_id") != entry["mapped_reaction_id"]
        or str(inference.parse_revision_id) != entry["parse_revision_id"]
        or str(inference.calculation_frame_id) != entry["calculation_frame_id"]
        or inference.file_frame_index != entry["file_frame_index"]
    ):
        raise RebuildBlocked("checkpoint source does not match the saved plan")
    _verify_binding(session, inference)
    return True


def _rebuild_one(
    session: Session,
    inference: TransitionStateInference,
    inferred: _SuccessfulInference,
    entry: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    if str(inference.mapped_reaction_id) != entry["mapped_reaction_id"]:
        raise RebuildBlocked("source reaction changed since the plan was created")
    if (
        str(inference.parse_revision_id) != entry["parse_revision_id"]
        or str(inference.calculation_frame_id) != entry["calculation_frame_id"]
        or inference.file_frame_index != entry["file_frame_index"]
    ):
        raise RebuildBlocked("source frame changed since the plan was created")
    _mark_reinference_succeeded(
        session, inference=inference, inferred=inferred, cleanup_obsolete=False
    )
    reaction = _verify_binding(session, inference)
    result = {
        "status": "updated",
        "old_reaction_id": entry["mapped_reaction_id"],
        "new_reaction_id": str(reaction.id),
        "mapped_reaction_smiles": reaction.mapped_reaction_smiles,
    }
    inference.inference_settings = {
        **inference.inference_settings,
        CHECKPOINT_KEY: {
            "run_id": state["run_id"],
            "source_sha256": entry["content_sha256"],
            "old_reaction_id": entry["mapped_reaction_id"],
            "policy": REACTION_INDEX_POLICY,
        },
    }
    session.add(inference)
    _refresh_latest_ingestion_status(
        session,
        ingestion_id=inference.artifact_ingestion_id,
        parse_revision_id=inference.parse_revision_id,
    )
    session.flush()
    return result


def _cleanup_one(session: Session, entry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    reaction_id = UUID(entry["id"])
    reaction = session.exec(
        select(MappedReaction).where(MappedReaction.id == reaction_id).with_for_update()
    ).first()
    if reaction is None:
        return {"status": "absent"}
    if _is_current(reaction):
        return {"status": "retained_current"}
    if (
        str(reaction.project_id) != str(entry["project_id"])
        or str(reaction.logical_reaction_id) != entry["logical_reaction_id"]
        or reaction.mapped_reaction_smiles != entry["mapped_reaction_smiles"]
        or reaction.mapping_hash != entry["mapping_hash"]
    ):
        raise RebuildBlocked("old reaction changed since the cleanup plan was created")
    if (
        session.exec(
            select(TransitionStateInference.id).where(
                TransitionStateInference.mapped_reaction_id == reaction_id
            )
        ).first()
        is not None
    ):
        raise RebuildBlocked("old reaction is still referenced by a TS inference")
    # No-source reactions cannot be silently reinterpreted or removed: they
    # may be curated paths or the only remaining association of a TS geometry.
    source_entries = [
        item for item in state["plan"]["inferences"] if item["mapped_reaction_id"] == entry["id"]
    ]
    if (
        not source_entries
        and reaction.mapped_reaction_kind is MappedReactionKind.OTHER
        and reaction.mapped_reaction_key.startswith("mapping:")
    ):
        # Automatically expanded siblings have no inference of their own.
        # Retire them only when their logical family's actual TS sources were
        # rebuilt successfully; curated/untraceable paths remain explicit blocks.
        source_entries = [
            item
            for item in state["plan"]["inferences"]
            if item["logical_reaction_id"] == entry["logical_reaction_id"]
        ]
    if not source_entries:
        raise RebuildBlocked("old reaction has no source TS in the plan; retained for review")
    target_geometry_ids = set()
    for source in source_entries:
        inference = session.get(TransitionStateInference, UUID(source["id"]))
        if inference is None or not _resume_complete(session, inference, source, state):
            raise RebuildBlocked("source re-inference has no verified durable checkpoint")
        frame = session.get(CalculationFrame, inference.calculation_frame_id)
        assert frame is not None
        target_geometry_ids.add(frame.geometry_id)
    old_ts_geometries = set(
        session.exec(
            select(MappedReactionNodeGeometry.geometry_id)
            .join(MappedReactionNode)
            .where(
                MappedReactionNode.mapped_reaction_id == reaction_id,
                MappedReactionNode.role == MappedReactionNodeRole.TRANSITION_STATE,
            )
        ).all()
    )
    if not old_ts_geometries.issubset(target_geometry_ids):
        raise RebuildBlocked("old TS geometry evidence is not covered by the rebuilt sources")
    # Edges restrict their own node deletions; remove them before parent CASCADE.
    session.exec(
        delete(MappedReactionEdge).where(col(MappedReactionEdge.mapped_reaction_id) == reaction_id)
    )
    session.exec(delete(MappedReaction).where(col(MappedReaction.id) == reaction_id))
    session.flush()
    logical_id = UUID(entry["logical_reaction_id"])
    # Lock the parent before checking emptiness so a concurrent importer cannot
    # add a child between the checks and the logical reaction's CASCADE delete.
    session.exec(
        select(LogicalReaction.id).where(LogicalReaction.id == logical_id).with_for_update()
    ).first()
    if (
        session.exec(
            select(MappedReaction.id).where(MappedReaction.logical_reaction_id == logical_id)
        ).first()
        is None
        and session.exec(
            select(TransitionStateInference.id).where(
                TransitionStateInference.logical_reaction_id == logical_id
            )
        ).first()
        is None
    ):
        session.exec(delete(LogicalReaction).where(col(LogicalReaction.id) == logical_id))
    return {"status": "deleted", "old_reaction_id": entry["id"]}


def _error(error: Exception) -> dict[str, str]:
    # Avoid serializing database parameters, object-store URLs or credentials.
    return {
        "status": "blocked",
        "reason": str(error) if isinstance(error, RebuildBlocked) else type(error).__name__,
    }


def _event(path: Path, *, run_id: str, phase: str, row_id: str, result: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        os.chmod(path, 0o600)
        output.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "phase": phase,
                    "id": row_id,
                    "time": datetime.now(UTC).isoformat(),
                    **result,
                }
            )
            + "\n"
        )
        output.flush()
        os.fsync(output.fileno())


def run(args: argparse.Namespace) -> int:
    _configure_backend_access()
    engine = create_engine(
        get_settings().database_url,
        connect_args={
            "connect_timeout": 10,
            "options": "-c lock_timeout=5000 -c statement_timeout=300000",
        },
    )
    stores: dict[str, RustFSObjectStore] = {}
    state_path = args.state_file.resolve()
    report_path = args.report.resolve() if args.report else state_path.with_suffix(".report.json")
    events_path = state_path.with_suffix(".events.jsonl")
    if len({state_path, report_path, events_path}) != 3:
        raise ValueError("state, report and event paths must differ")
    if not state_path.exists() and (report_path.exists() or events_path.exists()):
        raise ValueError("report/events already exist without the matching state file")
    try:
        # One runner at a time; no database transaction is held while parsing.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
            if not lock.execute(text("SELECT pg_try_advisory_lock(1414677315, 61)")).scalar_one():
                raise RebuildBlocked("another reaction rebuild is running")
            try:
                with Session(engine) as session:
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                    version = session.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    if version != "0061_reaction_normal_form":
                        raise RebuildBlocked("schema must be upgraded to 0061_reaction_normal_form")
                    database_key = _digest(
                        list(
                            session.execute(
                                text(
                                    "SELECT current_database(), inet_server_addr()::text, "
                                    "inet_server_port(), current_schema()"
                                )
                            ).one()
                        )
                    )
                    if state_path.exists():
                        state = json.loads(state_path.read_text())
                        _validate_state(state, args.project_id, database_key)
                    else:
                        state = _snapshot(session, args.project_id, database_key)
                        _save(state_path, state)
                report: dict[str, Any] = {
                    "run_id": state["run_id"],
                    "apply": args.apply,
                    "results": {},
                    "cleanup": {},
                }
                direct_sources = {
                    item["mapped_reaction_id"] for item in state["plan"]["inferences"]
                }
                logical_sources = {
                    item["logical_reaction_id"] for item in state["plan"]["inferences"]
                }
                report["untraceable_legacy_reaction_ids"] = [
                    item["id"]
                    for item in state["plan"]["reactions"]
                    if not _current_form(
                        item["mapped_reaction_smiles"],
                        item["mapping_hash"],
                        item["normalization_metadata"],
                    )
                    and item["id"] not in direct_sources
                    and not (
                        item["mapped_reaction_kind"] == "other"
                        and item["mapped_reaction_key"].startswith("mapping:")
                        and item["logical_reaction_id"] in logical_sources
                    )
                ]
                report["planned_source_count"] = len(state["plan"]["inferences"])
                report["planned_reaction_count"] = len(state["plan"]["reactions"])
                _save(report_path, report)
                cached_artifact: str | None = None
                parsed: dict[int, Any] = {}
                for number, entry in enumerate(state["plan"]["inferences"], 1):
                    try:
                        with Session(engine) as session:
                            inference = session.get(TransitionStateInference, UUID(entry["id"]))
                            if inference is None:
                                raise RebuildBlocked("planned source inference no longer exists")
                            artifact = session.get(ArtifactFile, UUID(entry["artifact_id"]))
                            if (
                                artifact is None
                                or artifact.content_sha256 != entry["content_sha256"]
                                or str(artifact.project_id) != str(entry["project_id"])
                            ):
                                raise RebuildBlocked("source artifact changed or is missing")
                            if _resume_complete(session, inference, entry, state):
                                result = {"status": "already_updated"}
                            else:
                                bucket, object_key, filename = (
                                    artifact.bucket,
                                    artifact.object_key,
                                    artifact.original_filename,
                                )
                                session.rollback()
                                if cached_artifact != entry["artifact_id"]:
                                    print(
                                        json.dumps(
                                            {
                                                "phase": "reinfer_source",
                                                "artifact_id": entry["artifact_id"],
                                            }
                                        ),
                                        flush=True,
                                    )
                                    store = stores.get(bucket)
                                    if store is None:
                                        store = RustFSObjectStore(
                                            RustFSSettings().model_copy(update={"bucket": bucket})
                                        )
                                        stores[bucket] = store
                                    payload = store.get_bytes(object_key)
                                    if sha256(payload).hexdigest() != entry["content_sha256"]:
                                        raise RebuildBlocked(
                                            "raw artifact SHA256 verification failed"
                                        )
                                    parsed = {
                                        item.file_frame_index: item
                                        for item in infer_transition_states_from_calculation_output(
                                            payload, filename
                                        )
                                    }
                                    cached_artifact = entry["artifact_id"]
                                inferred = parsed.get(entry["file_frame_index"])
                                if not isinstance(inferred, _SuccessfulInference):
                                    raise RebuildBlocked(
                                        "original TS frame did not reproduce successful endpoints"
                                    )
                                # End the read transaction before taking the write lock.
                                session.rollback()
                                inference = session.exec(
                                    select(TransitionStateInference)
                                    .where(TransitionStateInference.id == UUID(entry["id"]))
                                    .with_for_update()
                                ).one()
                                result = _rebuild_one(session, inference, inferred, entry, state)
                                if args.apply:
                                    session.commit()
                                else:
                                    session.rollback()
                                    result["status"] = "preflight_passed"
                    except Exception as error:
                        result = _error(error)
                    report["results"][entry["id"]] = result
                    _event(
                        events_path,
                        run_id=state["run_id"],
                        phase="apply" if args.apply else "preflight",
                        row_id=entry["id"],
                        result=result,
                    )
                    if number % 50 == 0 or result["status"] == "blocked":
                        _save(report_path, report)
                    print(
                        json.dumps(
                            {
                                "inference_id": entry["id"],
                                "status": result["status"],
                                "reason": result.get("reason"),
                            }
                        ),
                        flush=True,
                    )

                failed = any(item["status"] == "blocked" for item in report["results"].values())
                if args.apply and not failed:
                    for entry in state["plan"]["reactions"]:
                        try:
                            with Session(engine) as session:
                                result = _cleanup_one(session, entry, state)
                                session.commit()
                        except Exception as error:
                            result = _error(error)
                        report["cleanup"][entry["id"]] = result
                        _event(
                            events_path,
                            run_id=state["run_id"],
                            phase="cleanup",
                            row_id=entry["id"],
                            result=result,
                        )
                        _save(report_path, report)
                report["cleanup_deferred"] = not args.apply or failed
                # Detect arrivals after the snapshot; they need a new run, not
                # deletion using a stale all-records selection.
                with Session(engine) as session:
                    fresh = _snapshot(
                        session, args.project_id, database_key, include_evidence=False
                    )
                known = {entry["id"] for entry in state["plan"]["inferences"]}
                report["new_inference_ids"] = [
                    entry["id"] for entry in fresh["plan"]["inferences"] if entry["id"] not in known
                ]
                report["remaining_legacy_reaction_ids"] = [
                    entry["id"]
                    for entry in fresh["plan"]["reactions"]
                    if not _current_form(
                        entry["mapped_reaction_smiles"],
                        entry["mapping_hash"],
                        entry["normalization_metadata"],
                    )
                ]
                report["complete"] = bool(
                    args.apply
                    and not failed
                    and not report["new_inference_ids"]
                    and not report["remaining_legacy_reaction_ids"]
                    and not any(item["status"] == "blocked" for item in report["cleanup"].values())
                )
                _save(report_path, report)
                print(
                    json.dumps(
                        {
                            "report": str(report_path),
                            "complete": report["complete"],
                            "source_count": len(report["results"]),
                        }
                    )
                )
                if not args.apply:
                    return int(failed or bool(report["untraceable_legacy_reaction_ids"]))
                return 0 if report["complete"] else 1
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(1414677315, 61)"))
    finally:
        for store in stores.values():
            store.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project-id", type=UUID)
    scope.add_argument("--all-projects", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="default: preflight then rollback")
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="reuse the same file to resume; never share it between databases",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
