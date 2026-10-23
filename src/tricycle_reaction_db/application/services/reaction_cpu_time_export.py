"""Live file-level CPU costs for mappings and complete substrate families."""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import and_
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.canonical_atom_mapping import _canonical_smiles
from tricycle_reaction_db.application.services.mapped_reaction_runtime import (
    mapped_reaction_file_time_statement,
)
from tricycle_reaction_db.db.models import (
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionParticipant,
    MolecularTopology,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side

STAGES = ("reactants", "transition_state", "products", "total")
CPU_TIME_COLUMNS = tuple(f"{stage}_cpu_time_hours" for stage in STAGES)
SUBSTRATE_TIME_COLUMNS = (
    "substrate_pair_key",
    "substrate_pair_reaction_count",
    *(f"substrate_pair_{column}" for column in CPU_TIME_COLUMNS),
)


def substrate_pair_key(project_id: UUID, components: Sequence[tuple[str, int]]) -> str:
    """Order-independent canonical stereo multiset, including stoichiometry."""
    counts: Counter[str] = Counter()
    for smiles, coefficient in components:
        counts[smiles] += coefficient
    payload = json.dumps(
        ["substrate-pair-v1", str(project_id), sorted(counts.items())], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class FileCpuTimes:
    """Keep each file's latest represented revision once per stage and union."""

    def __init__(self) -> None:
        self.files: dict[str, dict[UUID, tuple[int, float | None]]] = {
            stage: {} for stage in STAGES
        }

    def add(self, role: str, artifact_id: UUID, revision: int, seconds: float | None) -> None:
        for stage in (role, "total"):
            previous = self.files[stage].get(artifact_id)
            if previous is None or revision > previous[0]:
                self.files[stage][artifact_id] = (revision, seconds)

    def hours(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for stage, entries in self.files.items():
            values = [seconds for _, seconds in entries.values()]
            result[f"{stage}_cpu_time_hours"] = (
                sum(value for value in values if value is not None) / 3600.0
                if values and all(value is not None for value in values)
                else None
            )
        return result


class ReactionCpuTimeExport:
    """Request-local cache; group membership never depends on export filters/pages.

    Resolve canonical reactant topology multisets once, then load file witnesses
    only for requested groups. No persisted aggregate can become stale after a
    reparse. Incomplete participant definitions do not merge into a shared group.
    """

    def __init__(self, session: Session, project_id: UUID, visible_mappings: Any) -> None:
        rows = session.exec(
            select(
                col(MappedReaction.id),
                col(LogicalReactionParticipant.stoichiometric_coefficient),
                col(MolecularTopology.id),
                col(MolecularTopology.mol),
            )
            .select_from(MappedReaction)
            .outerjoin(
                LogicalReactionParticipant,
                and_(
                    col(LogicalReactionParticipant.logical_reaction_id)
                    == col(MappedReaction.logical_reaction_id),
                    col(LogicalReactionParticipant.side) == Side.REACTANT,
                ),
            )
            .outerjoin(
                MappedReactionParticipant,
                and_(
                    col(MappedReactionParticipant.mapped_reaction_id) == col(MappedReaction.id),
                    col(MappedReactionParticipant.logical_reaction_participant_id)
                    == col(LogicalReactionParticipant.id),
                    col(MappedReactionParticipant.side) == Side.REACTANT,
                ),
            )
            .outerjoin(
                MolecularTopology,
                and_(
                    col(MolecularTopology.id)
                    == col(MappedReactionParticipant.concrete_topology_id),
                    col(MolecularTopology.project_id) == col(MappedReaction.project_id),
                ),
            )
            .where(col(MappedReaction.project_id) == project_id, visible_mappings)
        ).all()
        topology_keys: dict[UUID, str | None] = {}
        components: dict[UUID, list[tuple[str, int]]] = defaultdict(list)
        incomplete: set[UUID] = set()
        for mid, coefficient, topology_id, molecule in rows:
            if not isinstance(mid, UUID):
                raise ValueError("persisted mapped reaction requires an ID")
            if topology_id is None or molecule is None or coefficient is None:
                incomplete.add(mid)
                continue
            if topology_id not in topology_keys:
                projection = _canonical_smiles(
                    molecule, include_stereochemistry=True, preserve_assigned_stereo=True
                )
                topology_keys[topology_id] = projection[0] if projection else None
            canonical = topology_keys[topology_id]
            if canonical is None:
                incomplete.add(mid)
            else:
                components[mid].append((canonical, coefficient))
        self.group_by_mapping = {
            mid: substrate_pair_key(project_id, entries)
            for mid, entries in components.items()
            if mid not in incomplete
        }
        self.members: dict[str, list[UUID]] = defaultdict(list)
        for mid, key in self.group_by_mapping.items():
            self.members[key].append(mid)
        self.values: dict[UUID, dict[str, Any]] = {}

    def load(self, session: Session, mapped_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        pending = set(mapped_ids).difference(self.values)
        keys = {self.group_by_mapping[mid] for mid in pending if mid in self.group_by_mapping}
        for key in keys:
            pending.update(self.members[key])
        individual = {mid: FileCpuTimes() for mid in pending}
        shared = {key: FileCpuTimes() for key in keys}
        ids = sorted(pending, key=str)
        for start in range(0, len(ids), 256):
            rows = session.exec(
                mapped_reaction_file_time_statement(ids[start : start + 256], cpu_time=True)
            )
            for mid, role, artifact_id, revision, seconds in rows:
                individual[mid].add(role, artifact_id, int(revision), seconds)
                key = self.group_by_mapping.get(mid)
                if key in shared:
                    shared[key].add(role, artifact_id, int(revision), seconds)
        shared_hours = {key: times.hours() for key, times in shared.items()}
        for mid, times in individual.items():
            key = self.group_by_mapping.get(mid)
            self.values[mid] = {
                **times.hours(),
                "substrate_pair_key": key,
                "substrate_pair_reaction_count": len(self.members[key]) if key else None,
                **{
                    f"substrate_pair_{column}": shared_hours[key][column] if key else None
                    for column in CPU_TIME_COLUMNS
                },
            }
        return {mid: self.values[mid] for mid in mapped_ids}
