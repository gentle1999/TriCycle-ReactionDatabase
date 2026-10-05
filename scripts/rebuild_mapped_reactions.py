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
from tricycle_reaction_db.application.services.reactions import (
    reindex_mapped_reaction_endpoint_geometry_components,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    CalculationFrame,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MappedReactionThermodynamicProfile,
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


def _logical_scope_filter(
    logical_reaction_ids: tuple[UUID, ...] | None,
    *,
    column: str,
) -> tuple[str, dict[str, str]]:
    if logical_reaction_ids is None:
        return "", {}
    return (
        f" AND {column} IN ("
        "SELECT value::uuid FROM jsonb_array_elements_text("
        "CAST(:logical_reaction_ids AS jsonb)) AS scope(value))",
        {"logical_reaction_ids": json.dumps([str(item) for item in logical_reaction_ids])},
    )


def _snapshot(
    session: Session,
    project_id: UUID | None,
    database_key: str,
    *,
    include_evidence: bool = True,
    logical_reaction_ids: tuple[UUID, ...] | None = None,
    retire_scoped_logicals: bool = False,
) -> dict[str, Any]:
    params = {"project": project_id}
    reaction_filter, reaction_filter_params = _logical_scope_filter(
        logical_reaction_ids,
        column="logical_reaction_id",
    )
    inference_filter, inference_filter_params = _logical_scope_filter(
        logical_reaction_ids,
        column="i.logical_reaction_id",
    )
    association_filter, association_filter_params = _logical_scope_filter(
        logical_reaction_ids,
        column="r.logical_reaction_id",
    )
    # Snapshot all existing reactions, including already-normalized ones. Full
    # update re-infers their TS sources too; no old reaction string is input.
    reaction_rows = (
        session.execute(
            text(f"""
        SELECT id, logical_reaction_id, project_id, mapped_reaction_smiles, mapping_hash,
               normalization_metadata, mapped_reaction_key, mapped_reaction_kind
        FROM mapped_reaction
        WHERE (CAST(:project AS uuid) IS NULL OR project_id=CAST(:project AS uuid))
          {reaction_filter}
        ORDER BY id
    """),
            {**params, **reaction_filter_params},
        )
        .mappings()
        .all()
    )
    profile_counts = {
        row[0]: (row[1], row[2])
        for row in session.execute(
            text(f"""
            SELECT r.id, count(DISTINCT p.id) AS profile_count,
                   count(DISTINCT s.id) AS profile_source_count
            FROM mapped_reaction r
            LEFT JOIN mapped_reaction_thermodynamic_profile p
              ON p.mapped_reaction_id=r.id
            LEFT JOIN mapped_reaction_thermodynamic_profile_source s
              ON s.profile_id=p.id
            WHERE (CAST(:project AS uuid) IS NULL OR r.project_id=CAST(:project AS uuid))
              {reaction_filter}
            GROUP BY r.id
        """),
            {**params, **reaction_filter_params},
        ).all()
    }
    reactions: list[dict[str, Any]] = [
        {
            **dict(reaction),
            "thermodynamic_profile_count": profile_counts.get(reaction["id"], (0, 0))[0],
            "thermodynamic_profile_source_count": profile_counts.get(reaction["id"], (0, 0))[1],
        }
        for reaction in reaction_rows
    ]
    inferences = (
        session.execute(
            text(f"""
        SELECT i.id, i.mapped_reaction_id, i.logical_reaction_id, i.calculation_frame_id,
               i.parse_revision_id, i.file_frame_index, a.id AS artifact_id,
               a.content_sha256, a.project_id, i.inference_settings
        FROM transition_state_inference i
        JOIN parse_revision r ON r.id=i.parse_revision_id
        JOIN artifact_file a ON a.id=r.artifact_file_id
        WHERE i.mapped_reaction_id IS NOT NULL
          AND (CAST(:project AS uuid) IS NULL OR a.project_id=CAST(:project AS uuid))
          {inference_filter}
        ORDER BY a.id, i.file_frame_index, i.id
    """),
            {**params, **inference_filter_params},
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
        "logical_reaction_ids": (
            None if logical_reaction_ids is None else [str(item) for item in logical_reaction_ids]
        ),
        "retire_scoped_logicals": retire_scoped_logicals,
        "policy": REACTION_INDEX_POLICY,
        "reactions": encode(reactions),
        "inferences": encode(inferences),
    }
    if include_evidence:
        associations = (
            session.execute(
                text(f"""
            SELECT n.mapped_reaction_id, n.node_key, n.role, g.id AS association_id,
                   g.geometry_id, g.mapped_reaction_participant_id, g.component_key,
                   m.geometry_atom_map_numbers, m.mapped_smiles, m.mapping_method,
                   m.mapping_version, m.verified
            FROM mapped_reaction_node n
            JOIN mapped_reaction r ON r.id=n.mapped_reaction_id
            JOIN mapped_reaction_node_geometry g ON g.mapped_reaction_node_id=n.id
            LEFT JOIN mapped_reaction_node_geometry_mapping m
              ON m.mapped_reaction_node_geometry_id=g.id
            WHERE (CAST(:project AS uuid) IS NULL OR r.project_id=CAST(:project AS uuid))
              {association_filter}
            ORDER BY n.mapped_reaction_id, g.id
        """),
                {**params, **association_filter_params},
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


def _validate_state(
    state: dict[str, Any],
    project_id: UUID | None,
    database_key: str,
    logical_reaction_ids: tuple[UUID, ...] | None,
    retire_scoped_logicals: bool,
) -> None:
    plan = state["plan"]
    expected_logical_reaction_ids = (
        None if logical_reaction_ids is None else [str(item) for item in logical_reaction_ids]
    )
    if (
        state.get("version") != STATE_VERSION
        or state.get("plan_digest") != _digest(plan)
        or plan.get("database_key") != database_key
        or plan.get("project_id") != (str(project_id) if project_id else None)
        or plan.get("logical_reaction_ids") != expected_logical_reaction_ids
        or plan.get("retire_scoped_logicals", False) != retire_scoped_logicals
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
    if (
        state["plan"].get("retire_scoped_logicals")
        and str(inference.logical_reaction_id) == entry["logical_reaction_id"]
    ):
        raise RebuildBlocked("source inference remains under its specific logical reaction")
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
    if (
        state["plan"].get("retire_scoped_logicals")
        and str(inference.logical_reaction_id) == entry["logical_reaction_id"]
    ):
        raise RebuildBlocked("source inference did not move to an abstract logical reaction")
    result = {
        "status": "updated",
        "old_reaction_id": entry["mapped_reaction_id"],
        "new_reaction_id": str(reaction.id),
        "old_logical_reaction_id": entry["logical_reaction_id"],
        "new_logical_reaction_id": str(inference.logical_reaction_id),
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


def _geometry_binding_signatures(session: Session, reaction_id: UUID) -> set[tuple[Any, ...]]:
    rows = session.execute(
        text("""
        SELECT n.node_key, n.role, g.geometry_id, g.component_key, g.component_index,
               g.coordinate_index, g.is_primary, p.side, p.template_index, p.atom_map_numbers,
               p.mapped_smiles, m.geometry_atom_map_numbers, m.mapped_smiles,
               m.mapping_method, m.mapping_version, m.verified
        FROM mapped_reaction_node n
        JOIN mapped_reaction_node_geometry g ON g.mapped_reaction_node_id=n.id
        LEFT JOIN mapped_reaction_participant p ON p.id=g.mapped_reaction_participant_id
        LEFT JOIN mapped_reaction_node_geometry_mapping m
          ON m.mapped_reaction_node_geometry_id=g.id
        WHERE n.mapped_reaction_id=CAST(:reaction_id AS uuid)
    """),
        {"reaction_id": reaction_id},
    ).all()
    return {
        tuple(tuple(value) if isinstance(value, list) else value for value in row) for row in rows
    }


def _endpoint_geometry_binding_signatures(
    session: Session, reaction_id: UUID
) -> set[tuple[Any, ...]]:
    """Describe endpoint evidence independently of logical participant ordering.

    Rebuilding a logical reaction can reorder its participant templates while
    retaining the same concrete participant and atom-map assignment. Geometry
    evidence is equivalent when its endpoint role, concrete topology and
    verified geometry-to-map binding are unchanged.
    """

    rows = session.execute(
        text("""
        SELECT n.role, g.geometry_id, p.side, p.concrete_topology_id,
               m.geometry_atom_map_numbers, m.mapped_smiles,
               m.mapping_method, m.mapping_version, m.verified
        FROM mapped_reaction_node n
        JOIN mapped_reaction_node_geometry g ON g.mapped_reaction_node_id=n.id
        LEFT JOIN mapped_reaction_participant p ON p.id=g.mapped_reaction_participant_id
        LEFT JOIN mapped_reaction_node_geometry_mapping m
          ON m.mapped_reaction_node_geometry_id=g.id
        WHERE n.mapped_reaction_id=CAST(:reaction_id AS uuid)
          AND n.role IN ('reactant', 'product')
    """),
        {"reaction_id": reaction_id},
    ).all()
    return {
        tuple(tuple(value) if isinstance(value, list) else value for value in row) for row in rows
    }


def _reparent_distinct_mapping(
    session: Session,
    reaction: MappedReaction,
    target_logical_id: UUID,
) -> tuple[UUID, ...]:
    """Attach a distinct valid concrete mapping to its abstract reaction.

    Some legacy mappings are not reproduced byte-for-byte when their TS
    sources are reparsed: the new source may choose a different atom-map
    assignment, leaving the old mapping hash without a row under the abstract
    reaction.  Keep that mapping and its geometry bindings, but only after the
    same reaction-wide inversion projection proves that every endpoint lands
    on the target logical participant slots.
    """

    from tricycle_reaction_db.application.services.molecular_geometry import (
        GeometryPersistenceContext,
    )
    from tricycle_reaction_db.application.services.reaction_commands import (
        _align_participant_indices,
        _has_complete_mapping,
        _logicalize_components,
        _ResolvedComponent,
    )
    from tricycle_reaction_db.application.services.reaction_topology_membership import (
        persist_logical_participant_concrete_topology,
    )
    from tricycle_reaction_db.application.services.reactions import reaction_hash_for_participants
    from tricycle_reaction_db.db.models import (
        MappedReactionParticipant,
        MolecularTopology,
    )
    from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide

    project_id = reaction.project_id
    if not isinstance(project_id, UUID):
        raise RebuildBlocked("distinct mapping has no project owner")
    reaction_id = reaction.id
    if not isinstance(reaction_id, UUID):
        raise RebuildBlocked("distinct mapping has no persisted identifier")
    geometry_before = _geometry_binding_signatures(session, reaction_id)
    target_logical = session.exec(
        select(LogicalReaction)
        .where(
            LogicalReaction.id == target_logical_id,
            LogicalReaction.project_id == project_id,
        )
        .with_for_update()
    ).first()
    if target_logical is None:
        raise RebuildBlocked("abstract destination reaction is missing")

    participants = session.exec(
        select(MappedReactionParticipant)
        .where(MappedReactionParticipant.mapped_reaction_id == reaction.id)
        .order_by(
            col(MappedReactionParticipant.side),
            col(MappedReactionParticipant.template_index),
        )
        .with_for_update()
    ).all()
    components: list[_ResolvedComponent] = []
    for participant in participants:
        if participant.concrete_topology_id is None:
            raise RebuildBlocked("distinct mapping has a participant without concrete topology")
        topology = session.get(MolecularTopology, participant.concrete_topology_id)
        if topology is None or topology.project_id != project_id:
            raise RebuildBlocked("distinct mapping participant topology is missing or foreign")
        if topology.formula is None:
            raise RebuildBlocked("distinct mapping participant topology has no formula")
        components.append(
            _ResolvedComponent(
                side=participant.side,
                template_index=participant.template_index,
                formula=topology.formula,
                topology=topology,
                topology_atom_map_numbers=list(participant.atom_map_numbers),
            )
        )
    if not components or not _has_complete_mapping(components):
        raise RebuildBlocked("distinct mapping does not have a complete atom mapping")

    logical_components = _logicalize_components(
        session,
        components,
        topology_context=GeometryPersistenceContext(project_id=project_id),
    )
    projected_hash = reaction_hash_for_participants(
        (component.side, component.logical_topology or component.topology, 1)
        for component in logical_components
    )
    if projected_hash != target_logical.reaction_hash:
        raise RebuildBlocked("distinct mapping does not project to the reparsed abstract reaction")

    aligned_components, aligned_logical_components = _align_participant_indices(
        components,
        logical_components,
        list(target_logical.participants),
    )
    target_participants = {
        (item.side, item.participant_index): item for item in target_logical.participants
    }
    participant_updates: list[tuple[MappedReactionParticipant, UUID, int]] = []
    for mapped_participant, component, logical_component in zip(
        participants,
        aligned_components,
        aligned_logical_components,
        strict=True,
    ):
        target_participant = target_participants.get((component.side, component.template_index))
        if (
            target_participant is None
            or target_participant.topology_id
            != (logical_component.logical_topology or logical_component.topology).id
        ):
            raise RebuildBlocked("distinct mapping does not match abstract participant slots")
        persist_logical_participant_concrete_topology(
            session,
            target_participant,
            component.topology,
        )
        target_participant_id = target_participant.id
        if not isinstance(target_participant_id, UUID):
            raise RebuildBlocked("abstract participant has no persisted identifier")
        participant_updates.append(
            (mapped_participant, target_participant_id, component.template_index)
        )

    existing_by_hash = session.exec(
        select(MappedReaction).where(
            MappedReaction.project_id == project_id,
            MappedReaction.logical_reaction_id == target_logical_id,
            MappedReaction.mapping_hash == reaction.mapping_hash,
        )
    ).all()
    if existing_by_hash:
        raise RebuildBlocked("distinct mapping hash already exists under the abstract reaction")
    existing_by_key = session.exec(
        select(MappedReaction).where(
            MappedReaction.project_id == project_id,
            MappedReaction.logical_reaction_id == target_logical_id,
            MappedReaction.mapped_reaction_key == reaction.mapped_reaction_key,
        )
    ).all()
    if existing_by_key:
        raise RebuildBlocked("distinct mapping key already exists under the abstract reaction")

    # Move indices out of the way before assigning target slots.  This avoids
    # transient conflicts when the old stereo-specific ordering differs from
    # the abstract participant ordering and the unique constraint is immediate.
    reserved_by_side: dict[Any, list[int]] = {}
    for side in LogicalReactionParticipantSide:
        old_indices = {item.template_index for item in participants if item.side is side}
        target_indices = {
            index for item, _participant_id, index in participant_updates if item.side is side
        }
        reserved = [
            value
            for value in range(32767, -1, -1)
            if value not in old_indices and value not in target_indices
        ][: sum(item.side is side for item in participants)]
        if len(reserved) != sum(item.side is side for item in participants):
            raise RebuildBlocked("no temporary participant indices are available")
        reserved_by_side[side] = reserved
    for side, reserved in reserved_by_side.items():
        side_rows = [item for item in participants if item.side is side]
        for participant, temporary_index in zip(side_rows, reserved, strict=True):
            participant.template_index = temporary_index
            session.add(participant)
    session.flush()

    old_logical_id = reaction.logical_reaction_id
    reaction.logical_reaction_id = target_logical_id
    session.add(reaction)
    for mapped_participant, target_participant_id, target_index in participant_updates:
        mapped_participant.logical_reaction_participant_id = target_participant_id
        mapped_participant.template_index = target_index
        session.add(mapped_participant)
    session.flush()
    reindex_mapped_reaction_endpoint_geometry_components(session, reaction_id)
    if reaction.logical_reaction_id != target_logical_id:
        raise RebuildBlocked("distinct mapping did not move to the abstract reaction")
    geometry_after = _geometry_binding_signatures(session, reaction_id)
    # The component key/index and participant template index are expected to
    # change together when a mapping moves to the abstract reaction's slots.
    # Geometry identity, per-component coordinate order, primary status and
    # mapping witnesses must remain unchanged.
    normalized_before = {(*item[:3], *item[5:8], *item[9:]) for item in geometry_before}
    normalized_after = {(*item[:3], *item[5:8], *item[9:]) for item in geometry_after}
    if normalized_before != normalized_after:
        raise RebuildBlocked("distinct mapping geometry evidence changed during reparenting")

    return (old_logical_id,)


def _cleanup_one(session: Session, entry: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    reaction_id = UUID(entry["id"])
    reaction = session.exec(
        select(MappedReaction).where(MappedReaction.id == reaction_id).with_for_update()
    ).first()
    if reaction is None:
        return {"status": "absent"}
    retire_scoped_logicals = state["plan"].get("retire_scoped_logicals", False)
    if _is_current(reaction) and not retire_scoped_logicals:
        return {"status": "retained_current"}
    if (
        str(reaction.project_id) != str(entry["project_id"])
        or (
            str(reaction.logical_reaction_id) != entry["logical_reaction_id"]
            and not retire_scoped_logicals
        )
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
    expanded_mapping_sibling = False
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
        expanded_mapping_sibling = bool(source_entries)
    if not source_entries:
        raise RebuildBlocked("old reaction has no source TS in the plan; retained for review")
    target_geometry_ids = set()
    target_reaction_ids: set[UUID] = set()
    target_logical_ids: set[UUID] = set()
    mapping_reparented_now = False
    target_reaction_id: UUID | None = None
    profiles: list[MappedReactionThermodynamicProfile] = []
    for source in source_entries:
        inference = session.get(TransitionStateInference, UUID(source["id"]))
        if inference is None or not _resume_complete(session, inference, source, state):
            raise RebuildBlocked("source re-inference has no verified durable checkpoint")
        if inference.mapped_reaction_id is None:
            raise RebuildBlocked("source re-inference is not bound to a mapped reaction")
        target_reaction_ids.add(inference.mapped_reaction_id)
        assert inference.logical_reaction_id is not None
        target_logical_ids.add(inference.logical_reaction_id)
        frame = session.get(CalculationFrame, inference.calculation_frame_id)
        assert frame is not None
        target_geometry_ids.add(frame.geometry_id)
    if retire_scoped_logicals:
        if expanded_mapping_sibling:
            if len(target_logical_ids) != 1:
                raise RebuildBlocked(
                    "expanded mapping sources do not converge on one logical reaction"
                )
            target_logical_id = next(iter(target_logical_ids))
            matching_targets = session.exec(
                select(MappedReaction)
                .where(
                    MappedReaction.project_id == reaction.project_id,
                    MappedReaction.logical_reaction_id == target_logical_id,
                    MappedReaction.mapping_hash == reaction.mapping_hash,
                )
                .with_for_update()
            ).all()
            if len(matching_targets) > 1:
                raise RebuildBlocked(
                    "expanded mapping has no unique concrete mapping under its abstract reaction"
                )
            if not matching_targets:
                _reparent_distinct_mapping(session, reaction, target_logical_id)
                mapping_reparented_now = True
                matching_targets = [reaction]
            target_reaction_id = matching_targets[0].id
            if not isinstance(target_reaction_id, UUID):
                raise RebuildBlocked("expanded mapping target is missing its persisted identifier")
            if not _geometry_binding_signatures(session, reaction_id).issubset(
                _geometry_binding_signatures(session, target_reaction_id)
            ):
                raise RebuildBlocked(
                    "expanded mapping geometry associations are not preserved by its target mapping"
                )
            target_reaction_ids = {target_reaction_id}
        if len(target_logical_ids) != 1:
            raise RebuildBlocked("old mapping sources do not converge on one abstract reaction")
        target_logical_id = next(iter(target_logical_ids))
        already_reparented = reaction.logical_reaction_id == target_logical_id
        if already_reparented:
            parent_ids = session.exec(
                select(LogicalReactionParticipant.logical_reaction_id)
                .join(MappedReactionParticipant)
                .where(MappedReactionParticipant.mapped_reaction_id == reaction_id)
            ).all()
            if not parent_ids or any(item != target_logical_id for item in parent_ids):
                raise RebuildBlocked("reparented mapping has inconsistent participant ownership")
        elif reaction.logical_reaction_id != UUID(entry["logical_reaction_id"]):
            raise RebuildBlocked("old reaction moved to an unexpected logical reaction")
        if target_logical_id == UUID(entry["logical_reaction_id"]):
            raise RebuildBlocked("source mapping remained under its specific logical reaction")
        target_reactions = session.exec(
            select(MappedReaction)
            .where(
                col(MappedReaction.id).in_(target_reaction_ids),
                MappedReaction.project_id == reaction.project_id,
                MappedReaction.logical_reaction_id == target_logical_id,
            )
            .with_for_update()
        ).all()
        if len(target_reactions) != len(target_reaction_ids):
            raise RebuildBlocked(
                "source mapping did not converge on the expected abstract reaction"
            )
        matching_targets = session.exec(
            select(MappedReaction)
            .where(
                MappedReaction.project_id == reaction.project_id,
                MappedReaction.logical_reaction_id == target_logical_id,
                MappedReaction.mapping_hash == reaction.mapping_hash,
            )
            .with_for_update()
        ).all()
        if len(matching_targets) != 1:
            if matching_targets:
                raise RebuildBlocked(
                    "old concrete mapping has no unique destination under its abstract reaction"
                )
            if already_reparented:
                raise RebuildBlocked("reparented mapping is missing from its abstract reaction")
            _reparent_distinct_mapping(session, reaction, target_logical_id)
            mapping_reparented_now = True
            target_reaction = reaction
            already_reparented = True
        else:
            target_reaction = matching_targets[0]
        target_reaction_id = target_reaction.id
        if (
            not isinstance(target_reaction_id, UUID)
            or target_reaction.mapped_reaction_smiles != reaction.mapped_reaction_smiles
        ):
            raise RebuildBlocked(
                "matching target mapping does not preserve the old mapped reaction"
            )
        if not expanded_mapping_sibling and not _endpoint_geometry_binding_signatures(
            session, reaction_id
        ).issubset(_endpoint_geometry_binding_signatures(session, target_reaction_id)):
            raise RebuildBlocked(
                "old endpoint geometry evidence is not preserved by its abstract mapping"
            )
        profiles = list(
            session.exec(
                select(MappedReactionThermodynamicProfile).where(
                    MappedReactionThermodynamicProfile.mapped_reaction_id == reaction_id
                )
            ).all()
        )
        from tricycle_reaction_db.application.services import (
            mapped_reaction_thermodynamics_persistence,
        )

        refresh_targets = {item.id: item for item in target_reactions}
        refresh_targets[target_reaction_id] = target_reaction
        if (not expanded_mapping_sibling or profiles) and not (already_reparented and not profiles):
            mapped_reaction_thermodynamics_persistence.enqueue_mapped_reaction_profile_refresh(
                session, list(refresh_targets.values())
            )
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
    logical_id = UUID(entry["logical_reaction_id"])
    if retire_scoped_logicals and reaction.logical_reaction_id == next(iter(target_logical_ids)):
        if not isinstance(target_reaction_id, UUID):
            raise RebuildBlocked("abstract destination mapping identifier is missing")
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
        return {
            "status": "already_reparented" if not mapping_reparented_now else "reparented",
            "old_reaction_id": entry["id"],
            "target_reaction_id": str(target_reaction_id),
            "target_reaction_ids": sorted(str(item) for item in target_reaction_ids),
            "abstract_logical_reaction_id": str(next(iter(target_logical_ids))),
            "geometry_evidence_preserved": True,
        }
    # Edges restrict their own node deletions; remove them before parent CASCADE.
    session.exec(
        delete(MappedReactionEdge).where(col(MappedReactionEdge.mapped_reaction_id) == reaction_id)
    )
    session.exec(delete(MappedReaction).where(col(MappedReaction.id) == reaction_id))
    session.flush()
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
    if retire_scoped_logicals and not isinstance(target_reaction_id, UUID):
        raise RebuildBlocked("abstract destination mapping identifier is missing")
    target_reaction_id_value = (
        str(target_reaction_id) if isinstance(target_reaction_id, UUID) else None
    )
    return {
        "status": "deleted",
        "old_reaction_id": entry["id"],
        "target_reaction_id": target_reaction_id_value,
        "target_reaction_ids": sorted(str(item) for item in target_reaction_ids)
        if retire_scoped_logicals
        else [],
        "thermodynamic_profiles_rebuilt_on_target": len(profiles) if retire_scoped_logicals else 0,
        "expanded_mapping_reused": expanded_mapping_sibling,
    }


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
    logical_reaction_ids: tuple[UUID, ...] | None = args.logical_reaction_ids
    retire_scoped_logicals: bool = args.retire_scoped_logicals
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
                        _validate_state(
                            state,
                            args.project_id,
                            database_key,
                            logical_reaction_ids,
                            retire_scoped_logicals,
                        )
                    else:
                        state = _snapshot(
                            session,
                            args.project_id,
                            database_key,
                            logical_reaction_ids=logical_reaction_ids,
                            retire_scoped_logicals=retire_scoped_logicals,
                        )
                        _save(state_path, state)
                report: dict[str, Any] = {
                    "run_id": state["run_id"],
                    "apply": args.apply,
                    "scope_logical_reaction_count": (
                        None if logical_reaction_ids is None else len(logical_reaction_ids)
                    ),
                    "retire_scoped_logicals": retire_scoped_logicals,
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
                report["planned_thermodynamic_profile_count"] = sum(
                    item.get("thermodynamic_profile_count", 0)
                    for item in state["plan"]["reactions"]
                )
                report["planned_thermodynamic_profile_source_count"] = sum(
                    item.get("thermodynamic_profile_source_count", 0)
                    for item in state["plan"]["reactions"]
                )
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
                        session,
                        args.project_id,
                        database_key,
                        include_evidence=False,
                        logical_reaction_ids=logical_reaction_ids,
                        retire_scoped_logicals=retire_scoped_logicals,
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
                if retire_scoped_logicals and logical_reaction_ids is not None:
                    with Session(engine) as session:
                        remaining_rows = (
                            session.execute(
                                text("""
                            SELECT id
                            FROM logical_reaction
                            WHERE project_id=CAST(:project AS uuid)
                              AND id IN (
                                SELECT value::uuid
                                FROM jsonb_array_elements_text(CAST(:logical_reaction_ids AS jsonb))
                              )
                            ORDER BY id
                        """),
                                {
                                    "project": args.project_id,
                                    "logical_reaction_ids": json.dumps(
                                        [str(item) for item in logical_reaction_ids]
                                    ),
                                },
                            )
                            .scalars()
                            .all()
                        )
                    report["remaining_scoped_logical_reaction_ids"] = [
                        str(item) for item in remaining_rows
                    ]
                else:
                    report["remaining_scoped_logical_reaction_ids"] = []
                report["complete"] = bool(
                    args.apply
                    and not failed
                    and not report["new_inference_ids"]
                    and not report["remaining_legacy_reaction_ids"]
                    and not report["remaining_scoped_logical_reaction_ids"]
                    and not any(item["status"] == "blocked" for item in report["cleanup"].values())
                )
                _save(report_path, report)
                print(
                    json.dumps(
                        {
                            "report": str(report_path),
                            "complete": report["complete"],
                            "source_count": len(report["results"]),
                            "remaining_scoped_logicals": len(
                                report["remaining_scoped_logical_reaction_ids"]
                            ),
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
    parser.add_argument(
        "--logical-reaction-ids-file",
        type=Path,
        help=(
            "newline-delimited logical reaction UUIDs to rebuild; requires --project-id "
            "and limits cleanup to this exact set"
        ),
    )
    parser.add_argument(
        "--retire-scoped-logicals",
        action="store_true",
        help=(
            "after all selected TS sources reparse successfully, remove the selected old "
            "logical-reaction identities; requires --project-id and --logical-reaction-ids-file"
        ),
    )
    args = parser.parse_args()
    if args.logical_reaction_ids_file is not None:
        if args.project_id is None:
            parser.error("--logical-reaction-ids-file requires --project-id")
        logical_reaction_ids = tuple(
            sorted(
                {
                    UUID(line.partition("#")[0].strip())
                    for line in args.logical_reaction_ids_file.read_text().splitlines()
                    if line.partition("#")[0].strip()
                },
                key=str,
            )
        )
        if not logical_reaction_ids:
            parser.error("--logical-reaction-ids-file contains no UUIDs")
        args.logical_reaction_ids = logical_reaction_ids
    else:
        args.logical_reaction_ids = None
    if args.retire_scoped_logicals and args.logical_reaction_ids is None:
        parser.error("--retire-scoped-logicals requires --logical-reaction-ids-file")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
