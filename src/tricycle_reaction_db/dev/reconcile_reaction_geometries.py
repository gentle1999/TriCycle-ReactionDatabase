"""Backfill missing reaction-participant Geometry bindings in bounded transactions."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from functools import partial
from multiprocessing.connection import Connection
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, exists, or_, text
from sqlalchemy.orm import Session as SQLAlchemySession
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    refresh_mapped_reaction_thermodynamics,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    _reaction_geometry_predicate,
    reconcile_mapped_reaction_with_geometries,
)
from tricycle_reaction_db.db.models import (
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionParticipant,
)
from tricycle_reaction_db.db.session import dispose_engine, session_factory
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionNodeRole,
)

DEFAULT_BATCH_SIZE = 100
DEFAULT_STATEMENT_TIMEOUT_MS = 300_000
MAX_STATEMENT_TIMEOUT_MS = 3_600_000
DEFAULT_REACTION_TIMEOUT_SECONDS = 300.0
MAX_REACTION_TIMEOUT_SECONDS = 86_400.0


@dataclass(slots=True)
class ReactionGeometryMaintenanceResult:
    dry_run: bool
    scanned_reactions: int = 0
    reconciled_reactions: int = 0
    failed_reactions: int = 0
    matched_bindings: int = 0
    created_bindings: int = 0
    committed_transactions: int = 0
    crashed_reactions: int = 0
    timed_out_reactions: int = 0
    last_reaction_id: UUID | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class _BatchResult:
    scanned_reactions: int = 0
    reconciled_reactions: int = 0
    failed_reactions: int = 0
    matched_bindings: int = 0
    created_bindings: int = 0
    last_reaction_id: UUID | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def _missing_binding_candidate() -> Any:
    """Return a correlated predicate for an eligible but unbound endpoint Geometry."""

    binding_exists = exists(
        select(MappedReactionNodeGeometry.id).where(
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
            col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
            col(MappedReactionNodeGeometry.mapped_reaction_participant_id)
            == col(MappedReactionParticipant.id),
        )
    )
    endpoint_role_matches_side = or_(
        and_(
            col(MappedReactionParticipant.side) == LogicalReactionParticipantSide.REACTANT,
            col(MappedReactionNode.role) == MappedReactionNodeRole.REACTANT,
        ),
        and_(
            col(MappedReactionParticipant.side) == LogicalReactionParticipantSide.PRODUCT,
            col(MappedReactionNode.role) == MappedReactionNodeRole.PRODUCT,
        ),
    )
    return exists(
        select(MappedReactionParticipant.id)
        .join(
            LogicalReactionParticipant,
            col(MappedReactionParticipant.logical_reaction_participant_id)
            == col(LogicalReactionParticipant.id),
        )
        .join(
            Geometry,
            col(Geometry.topology_id) == col(LogicalReactionParticipant.topology_id),
        )
        .join(
            MappedReactionNode,
            and_(
                col(MappedReactionNode.mapped_reaction_id)
                == col(MappedReactionParticipant.mapped_reaction_id),
                endpoint_role_matches_side,
            ),
        )
        .where(
            col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
            _reaction_geometry_predicate(),
            ~binding_exists,
        )
    )


def _participant_binding_ids(session: Session, mapped_reaction_id: UUID) -> set[UUID]:
    return {
        binding_id
        for binding_id in session.exec(
            select(MappedReactionNodeGeometry.id)
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNode.mapped_reaction_id) == mapped_reaction_id,
                col(MappedReactionNodeGeometry.mapped_reaction_participant_id).is_not(None),
            )
        ).all()
        if isinstance(binding_id, UUID)
    }


def select_reaction_geometry_candidates(
    session: Session,
    *,
    batch_size: int,
    start_after: UUID | None = None,
    mapped_reaction_id: UUID | None = None,
    scan_all: bool = False,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> list[UUID]:
    """Select one keyset page without running RDKit-backed reconciliation."""

    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)").bindparams(
            timeout=f"{statement_timeout_ms}ms"
        )
    )
    statement = select(MappedReaction.id)
    if mapped_reaction_id is not None:
        statement = statement.where(col(MappedReaction.id) == mapped_reaction_id)
    else:
        if start_after is not None:
            statement = statement.where(col(MappedReaction.id) > start_after)
        if not scan_all:
            statement = statement.where(_missing_binding_candidate())
    return [
        reaction_id
        for reaction_id in session.exec(
            statement.order_by(col(MappedReaction.id)).limit(batch_size)
        ).all()
        if isinstance(reaction_id, UUID)
    ]


def reconcile_reaction_geometry_batch(
    session: Session,
    *,
    batch_size: int,
    start_after: UUID | None = None,
    mapped_reaction_id: UUID | None = None,
    scan_all: bool = False,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> _BatchResult:
    """Reconcile one keyset page while isolating each reaction with a savepoint."""

    session.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)").bindparams(
            timeout=f"{statement_timeout_ms}ms"
        )
    )
    candidate_ids = select_reaction_geometry_candidates(
        session,
        batch_size=batch_size,
        start_after=start_after,
        mapped_reaction_id=mapped_reaction_id,
        scan_all=scan_all,
        statement_timeout_ms=statement_timeout_ms,
    )
    reactions = session.exec(
        select(MappedReaction)
        .where(col(MappedReaction.id).in_(candidate_ids))
        .order_by(col(MappedReaction.id))
    ).all()

    result = _BatchResult()
    for mapped_reaction in reactions:
        reaction_id = mapped_reaction.id
        if not isinstance(reaction_id, UUID):
            raise RuntimeError("persisted MappedReaction is missing its UUID")
        result.scanned_reactions += 1
        result.last_reaction_id = reaction_id
        try:
            with session.begin_nested():
                existing_ids = _participant_binding_ids(session, reaction_id)
                reconciliation = reconcile_mapped_reaction_with_geometries(
                    session,
                    mapped_reaction,
                    refresh_thermodynamics=False,
                )
                matched_ids = set(reconciliation.node_geometry_ids)
                created_ids = matched_ids - existing_ids
                if created_ids:
                    refresh_mapped_reaction_thermodynamics(session, mapped_reaction)
            result.reconciled_reactions += 1
            result.matched_bindings += len(matched_ids)
            result.created_bindings += len(created_ids)
        except Exception as error:
            result.failed_reactions += 1
            result.errors.append(
                {
                    "mapped_reaction_id": str(reaction_id),
                    "error_type": type(error).__name__,
                    "error_message": str(error) or type(error).__name__,
                }
            )
    return result


def _run_reconcile_reaction_geometry_batch(
    session: SQLAlchemySession,
    **kwargs: Any,
) -> _BatchResult:
    return reconcile_reaction_geometry_batch(cast(Session, session), **kwargs)


def _run_select_reaction_geometry_candidates(
    session: SQLAlchemySession,
    **kwargs: Any,
) -> list[UUID]:
    return select_reaction_geometry_candidates(cast(Session, session), **kwargs)


async def _process_reaction(
    mapped_reaction_id: UUID,
    *,
    dry_run: bool,
    statement_timeout_ms: int,
) -> _BatchResult:
    async with session_factory() as session:
        result = await session.run_sync(
            partial(
                _run_reconcile_reaction_geometry_batch,
                batch_size=1,
                mapped_reaction_id=mapped_reaction_id,
                statement_timeout_ms=statement_timeout_ms,
            )
        )
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
        return result


async def _worker_loop(connection: Connection) -> None:
    try:
        while True:
            try:
                request = await asyncio.to_thread(connection.recv)
            except EOFError:
                break
            if request is None:
                break
            mapped_reaction_id, dry_run, statement_timeout_ms = request
            try:
                result = await _process_reaction(
                    mapped_reaction_id,
                    dry_run=dry_run,
                    statement_timeout_ms=statement_timeout_ms,
                )
                connection.send(("ok", result))
            except Exception as error:
                connection.send(
                    (
                        "error",
                        {
                            "error_type": type(error).__name__,
                            "error_message": str(error) or type(error).__name__,
                        },
                    )
                )
    finally:
        connection.close()
        await dispose_engine()


def _worker_entry(connection: Connection) -> None:
    asyncio.run(_worker_loop(connection))


class _WorkerUnavailable(RuntimeError):
    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


class _IsolatedReactionWorker:
    """Reuse one spawned worker and replace it after native failure or timeout."""

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._process: Any = None
        self._connection: Connection | None = None

    def _start(self) -> None:
        parent_connection, child_connection = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_entry,
            args=(child_connection,),
            name="reaction-geometry-maintenance",
        )
        process.start()
        child_connection.close()
        self._process = process
        self._connection = parent_connection

    async def process(
        self,
        mapped_reaction_id: UUID,
        *,
        dry_run: bool,
        statement_timeout_ms: int,
        timeout_seconds: float,
    ) -> _BatchResult:
        if self._process is None or not self._process.is_alive():
            await self.close()
            self._start()
        if self._connection is None:
            raise RuntimeError("reaction Geometry worker did not initialize")
        try:
            self._connection.send((mapped_reaction_id, dry_run, statement_timeout_ms))
            ready = await asyncio.to_thread(self._connection.poll, timeout_seconds)
            if not ready:
                raise _WorkerUnavailable(
                    f"reaction reconciliation exceeded {timeout_seconds:g} seconds",
                    timed_out=True,
                )
            status, payload = self._connection.recv()
        except (BrokenPipeError, EOFError, OSError) as error:
            exit_code = self._process.exitcode if self._process is not None else None
            raise _WorkerUnavailable(
                f"reaction reconciliation worker exited unexpectedly (exit_code={exit_code})"
            ) from error
        if status == "error":
            raise RuntimeError(f"{payload['error_type']}: {payload['error_message']}")
        if not isinstance(payload, _BatchResult):
            raise RuntimeError("reaction reconciliation worker returned an invalid result")
        return payload

    async def close(self) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            if process is not None and process.is_alive():
                with suppress(BrokenPipeError, EOFError, OSError):
                    connection.send(None)
            connection.close()
        if process is None:
            return
        await asyncio.to_thread(process.join, 5.0)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5.0)
        process.close()


async def run_reaction_geometry_maintenance(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    start_after: UUID | None = None,
    mapped_reaction_id: UUID | None = None,
    scan_all: bool = False,
    dry_run: bool = False,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    reaction_timeout_seconds: float = DEFAULT_REACTION_TIMEOUT_SECONDS,
    show_progress: bool = False,
) -> ReactionGeometryMaintenanceResult:
    """Run bounded reconciliation pages until no matching reaction remains."""

    result = ReactionGeometryMaintenanceResult(dry_run=dry_run)
    cursor = start_after
    remaining = limit
    worker = _IsolatedReactionWorker()
    try:
        while remaining is None or remaining > 0:
            page_size = batch_size if remaining is None else min(batch_size, remaining)
            async with session_factory() as session:
                candidate_ids = await session.run_sync(
                    partial(
                        _run_select_reaction_geometry_candidates,
                        batch_size=page_size,
                        start_after=cursor,
                        mapped_reaction_id=mapped_reaction_id,
                        scan_all=scan_all,
                        statement_timeout_ms=statement_timeout_ms,
                    )
                )
                await session.rollback()
            if not candidate_ids:
                break

            for reaction_id in candidate_ids:
                result.scanned_reactions += 1
                result.last_reaction_id = reaction_id
                try:
                    batch = await worker.process(
                        reaction_id,
                        dry_run=dry_run,
                        statement_timeout_ms=statement_timeout_ms,
                        timeout_seconds=reaction_timeout_seconds,
                    )
                except _WorkerUnavailable as error:
                    result.failed_reactions += 1
                    result.timed_out_reactions += int(error.timed_out)
                    result.crashed_reactions += int(not error.timed_out)
                    result.errors.append(
                        {
                            "mapped_reaction_id": str(reaction_id),
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        }
                    )
                    await worker.close()
                except Exception as error:
                    result.failed_reactions += 1
                    result.errors.append(
                        {
                            "mapped_reaction_id": str(reaction_id),
                            "error_type": type(error).__name__,
                            "error_message": str(error) or type(error).__name__,
                        }
                    )
                else:
                    result.reconciled_reactions += batch.reconciled_reactions
                    result.failed_reactions += batch.failed_reactions
                    result.matched_bindings += batch.matched_bindings
                    result.created_bindings += batch.created_bindings
                    result.errors.extend(batch.errors)
                    if not dry_run and batch.reconciled_reactions:
                        result.committed_transactions += 1
                if show_progress:
                    print(
                        "reaction Geometry reconciliation: "
                        f"scanned={result.scanned_reactions} created={result.created_bindings} "
                        f"failed={result.failed_reactions} last={result.last_reaction_id}",
                        file=sys.stderr,
                        flush=True,
                    )

            # Bound native-library lifetime and memory growth to one candidate
            # page even when every reaction completes successfully.
            await worker.close()
            if mapped_reaction_id is not None:
                break
            cursor = candidate_ids[-1]
            if remaining is not None:
                remaining -= len(candidate_ids)
            if len(candidate_ids) < page_size:
                break
    finally:
        await worker.close()
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--start-after", type=UUID, default=None, metavar="UUID")
    parser.add_argument("--mapped-reaction-id", type=UUID, default=None, metavar="UUID")
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="check every mapped reaction instead of selecting only missing-binding candidates",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="execute reconciliation checks but roll back every batch",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=DEFAULT_STATEMENT_TIMEOUT_MS,
        help=f"per-statement timeout (default: {DEFAULT_STATEMENT_TIMEOUT_MS})",
    )
    parser.add_argument(
        "--reaction-timeout-seconds",
        type=float,
        default=DEFAULT_REACTION_TIMEOUT_SECONDS,
        help=f"per-reaction worker timeout (default: {DEFAULT_REACTION_TIMEOUT_SECONDS:g})",
    )
    return parser


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.batch_size < 1:
        parser.error("--batch-size must be positive")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be positive")
    if not 100 <= arguments.statement_timeout_ms <= MAX_STATEMENT_TIMEOUT_MS:
        parser.error(f"--statement-timeout-ms must be between 100 and {MAX_STATEMENT_TIMEOUT_MS}")
    if not 1 <= arguments.reaction_timeout_seconds <= MAX_REACTION_TIMEOUT_SECONDS:
        parser.error(
            f"--reaction-timeout-seconds must be between 1 and {MAX_REACTION_TIMEOUT_SECONDS:g}"
        )
    if arguments.mapped_reaction_id is not None and arguments.start_after is not None:
        parser.error("--mapped-reaction-id cannot be combined with --start-after")
    return arguments


async def _run(arguments: argparse.Namespace) -> ReactionGeometryMaintenanceResult:
    try:
        return await run_reaction_geometry_maintenance(
            batch_size=arguments.batch_size,
            limit=arguments.limit,
            start_after=arguments.start_after,
            mapped_reaction_id=arguments.mapped_reaction_id,
            scan_all=arguments.scan_all,
            dry_run=arguments.dry_run,
            statement_timeout_ms=arguments.statement_timeout_ms,
            reaction_timeout_seconds=arguments.reaction_timeout_seconds,
            show_progress=True,
        )
    finally:
        await dispose_engine()


def main() -> None:
    arguments = _arguments()
    result = asyncio.run(_run(arguments))
    print(json.dumps(asdict(result), sort_keys=True, default=str))
    if arguments.mapped_reaction_id is not None and result.scanned_reactions == 0:
        raise SystemExit(2)
    if result.failed_reactions:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
