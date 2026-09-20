"""Bounded RDKit graph matching primitives.

RDKit's substructure matcher does not expose a wall-clock timeout.  A Python
thread timeout is therefore insufficient: the native matcher can keep running
after the caller has stopped waiting.  Large, potentially expensive matches
are isolated in a short-lived spawn process so the process can be terminated
when its budget expires.
"""

from __future__ import annotations

import multiprocessing
from contextlib import suppress
from multiprocessing.connection import Connection
from typing import Any, cast

from rdkit import Chem

from tricycle_reaction_db.core.config import get_settings


class MolecularGraphMatchTimeoutError(TimeoutError):
    """A native RDKit graph match exceeded its wall-clock budget."""

    def __init__(
        self,
        *,
        target_atom_count: int,
        query_atom_count: int,
        timeout_seconds: float,
    ) -> None:
        self.target_atom_count = target_atom_count
        self.query_atom_count = query_atom_count
        self.timeout_seconds = timeout_seconds
        super().__init__(
            "RDKit graph matching exceeded "
            f"{timeout_seconds:g}s for {target_atom_count} target atoms and "
            f"{query_atom_count} query atoms"
        )


class MolecularGraphMatchProcessError(RuntimeError):
    """The isolated matcher could not return a result."""


def _run_substructure_matches(
    target_binary: bytes,
    query_binary: bytes,
    *,
    use_chirality: bool,
    uniquify: bool,
    max_matches: int,
) -> tuple[tuple[int, ...], ...]:
    """Execute one RDKit match in the current process.

    ``target_binary`` and ``query_binary`` are used instead of RDKit objects so
    the same function can be called by the isolated child process.
    """

    target = Chem.Mol(cast(Any, target_binary))
    query = Chem.Mol(cast(Any, query_binary))
    parameters = cast(Any, Chem.SubstructMatchParameters())
    parameters.useChirality = use_chirality
    parameters.uniquify = uniquify
    parameters.maxMatches = max_matches
    matches = target.GetSubstructMatches(query, parameters)
    return tuple(tuple(int(index) for index in match) for match in matches)


def _substructure_match_worker(
    target_binary: bytes,
    query_binary: bytes,
    use_chirality: bool,
    uniquify: bool,
    max_matches: int,
    sender: Connection,
) -> None:
    """Spawn-safe worker entry point for one isolated native operation."""

    try:
        sender.send(
            (
                "ok",
                _run_substructure_matches(
                    target_binary,
                    query_binary,
                    use_chirality=use_chirality,
                    uniquify=uniquify,
                    max_matches=max_matches,
                ),
            )
        )
    except BaseException as error:  # pragma: no cover - native failures vary by RDKit build
        with suppress(BrokenPipeError, EOFError, OSError):
            sender.send(("error", type(error).__name__, str(error)))
    finally:
        sender.close()


def _run_isolated_substructure_matches(
    target_binary: bytes,
    query_binary: bytes,
    *,
    use_chirality: bool,
    uniquify: bool,
    max_matches: int,
    timeout_seconds: float,
    target_atom_count: int,
    query_atom_count: int,
) -> tuple[tuple[int, ...], ...]:
    """Run one match in a child process that can be hard-stopped."""

    if multiprocessing.current_process().daemon:
        raise MolecularGraphMatchProcessError(
            "isolated RDKit graph matching cannot start from a daemon process"
        )

    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_substructure_match_worker,
        args=(
            target_binary,
            query_binary,
            use_chirality,
            uniquify,
            max_matches,
            child_connection,
        ),
        name="rdkit-graph-match",
    )
    try:
        process.start()
    except BaseException:
        parent_connection.close()
        child_connection.close()
        raise
    child_connection.close()

    timed_out = False
    try:
        if not parent_connection.poll(timeout_seconds):
            timed_out = True
            raise MolecularGraphMatchTimeoutError(
                target_atom_count=target_atom_count,
                query_atom_count=query_atom_count,
                timeout_seconds=timeout_seconds,
            )
        try:
            response = parent_connection.recv()
        except (EOFError, OSError) as error:
            raise MolecularGraphMatchProcessError(
                "isolated RDKit graph matcher exited without a result"
            ) from error
        if not isinstance(response, tuple) or not response:
            raise MolecularGraphMatchProcessError(
                "isolated RDKit graph matcher returned an invalid response"
            )
        if response[0] == "error":
            error_type = response[1] if len(response) > 1 else "RuntimeError"
            error_message = response[2] if len(response) > 2 else "unknown native error"
            raise MolecularGraphMatchProcessError(f"{error_type}: {error_message}")
        if response[0] != "ok" or len(response) != 2:
            raise MolecularGraphMatchProcessError(
                "isolated RDKit graph matcher returned an invalid status"
            )
        matches = response[1]
        if not isinstance(matches, tuple):
            raise MolecularGraphMatchProcessError(
                "isolated RDKit graph matcher returned invalid matches"
            )
        return cast(tuple[tuple[int, ...], ...], matches)
    finally:
        parent_connection.close()
        if timed_out and process.is_alive():
            process.terminate()
        process.join(1.0)
        if process.is_alive():
            process.kill()
            process.join(1.0)
        process.close()


def get_substruct_matches(
    target: Chem.Mol,
    query: Chem.Mol,
    *,
    use_chirality: bool = False,
    uniquify: bool = True,
    max_matches: int | None = None,
    hard_timeout_for_large_molecules: bool = True,
    timeout_seconds: float | None = None,
) -> tuple[tuple[int, ...], ...]:
    """Return bounded RDKit matches without allowing risky native work to hang.

    Small matches remain in-process to avoid creating a child for every normal
    topology operation.  Full-graph callers set
    ``hard_timeout_for_large_molecules``; once the configured atom threshold is
    reached, those operations run in a terminable child process.  Every path
    also sets RDKit's explicit ``maxMatches`` limit.
    """

    settings = get_settings()
    resolved_max_matches = (
        settings.molecular_graph_match_max_results if max_matches is None else max_matches
    )
    if resolved_max_matches < 1:
        raise ValueError("max_matches must be positive")
    resolved_timeout = (
        settings.molecular_graph_match_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    if resolved_timeout <= 0:
        raise ValueError("timeout_seconds must be positive")

    target_atom_count = target.GetNumAtoms()
    query_atom_count = query.GetNumAtoms()
    target_binary = target.ToBinary()
    query_binary = query.ToBinary()
    should_isolate = (
        hard_timeout_for_large_molecules
        and max(
            target_atom_count,
            query_atom_count,
        )
        >= settings.molecular_graph_match_isolation_atom_count
    )
    if not should_isolate:
        return _run_substructure_matches(
            target_binary,
            query_binary,
            use_chirality=use_chirality,
            uniquify=uniquify,
            max_matches=resolved_max_matches,
        )
    return _run_isolated_substructure_matches(
        target_binary,
        query_binary,
        use_chirality=use_chirality,
        uniquify=uniquify,
        max_matches=resolved_max_matches,
        timeout_seconds=resolved_timeout,
        target_atom_count=target_atom_count,
        query_atom_count=query_atom_count,
    )


__all__ = [
    "MolecularGraphMatchProcessError",
    "MolecularGraphMatchTimeoutError",
    "get_substruct_matches",
]
