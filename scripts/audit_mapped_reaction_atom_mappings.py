#!/usr/bin/env python3
"""Read-only full audit of mapped reactions and their geometry atom mappings."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from rdkit import Chem
from sqlalchemy import create_engine
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    ReactionComponents,
)
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    canonical_reaction_identity,
)
from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    mapped_reaction_atom_signatures,
    validate_geometry_atom_map_elements,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    _mapped_reaction_matches_participant_projection,
    _reaction_components_from_source_endpoints,
)
from tricycle_reaction_db.application.services.reactions import (
    _mapped_smiles_for_molecule,
    mapped_smiles_for_topology,
)
from tricycle_reaction_db.core.chemistry_config import (
    REACTION_GEOMETRY_LINK_METHOD,
    REACTION_GEOMETRY_LINK_POLICY_VERSION,
    REACTION_TS_GEOMETRY_LINK_METHOD,
    REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models.calculations import CalculationFrame
from tricycle_reaction_db.db.models.chemistry import Geometry, MolecularTopology
from tricycle_reaction_db.db.models.reactions import (
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
)
from tricycle_reaction_db.db.models.uploads import TransitionStateEndpoint, TransitionStateInference
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionNodeRole,
    TransitionStateInferenceStatus,
)
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible


@dataclass(slots=True)
class FrameEndpoint:
    direction: Any
    topology_id: UUID
    atom_count: int
    source_to_topology: list[int]


@dataclass(slots=True)
class FrameSource:
    inference_id: UUID
    logical_reaction_id: UUID
    mapped_reaction_id: UUID
    frame_id: UUID
    geometry_id: UUID
    observed_to_geometry: list[int]
    frame_role: Any
    endpoints: list[FrameEndpoint] = field(default_factory=list)
    source_components: ReactionComponents | None = None
    source_error: str | None = None
    reaction_evidence: dict[bool, ReactionEvidence | None] = field(default_factory=dict)


@dataclass(slots=True)
class ReactionContext:
    reaction: MappedReaction
    participants: list[MappedReactionParticipant]
    components: ReactionComponents | None
    errors: list[str]
    warnings: list[tuple[UUID | None, str]] = field(default_factory=list)
    reaction_evidence: dict[bool, ReactionEvidence | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReactionEvidence:
    canonical_mapped_smiles: str
    source_map_to_canonical: dict[int, int]


@dataclass(slots=True)
class GeometryMappingRow:
    binding: MappedReactionNodeGeometry
    mapping: MappedReactionNodeGeometryMapping | None
    geometry: Geometry
    reaction_id: UUID
    logical_reaction_id: UUID
    role: MappedReactionNodeRole
    context: ReactionContext | None = None
    local_errors: list[str] = field(default_factory=list)
    local_warnings: list[str] = field(default_factory=list)
    direct_state: str = "absent"
    direct_details: list[str] = field(default_factory=list)


class Audit:
    def __init__(self, progress: bool) -> None:
        self.progress = progress
        self.counts: Counter[str] = Counter()
        self.mapping_versions: Counter[str] = Counter()
        self.mapping_methods: Counter[str] = Counter()
        self.findings: list[dict[str, Any]] = []
        self._progress_width = 0

    @staticmethod
    def reaction_url(reaction: MappedReaction) -> str:
        return f"https://10.66.0.18/reactions/{reaction.id}?project_id={reaction.project_id}"

    def add_finding(
        self,
        *,
        classification: str,
        category: str,
        code: str,
        reaction: MappedReaction,
        detail: str,
        geometry_id: UUID | None = None,
        node_geometry_id: UUID | None = None,
        participant_id: UUID | None = None,
        inference_id: UUID | None = None,
    ) -> None:
        self.findings.append(
            {
                "classification": classification,
                "category": category,
                "code": code,
                "mapped_reaction_id": str(reaction.id),
                "logical_reaction_id": str(reaction.logical_reaction_id),
                "project_id": str(reaction.project_id) if reaction.project_id else None,
                "geometry_id": str(geometry_id) if geometry_id else None,
                "node_geometry_id": str(node_geometry_id) if node_geometry_id else None,
                "mapped_reaction_participant_id": str(participant_id) if participant_id else None,
                "inference_id": str(inference_id) if inference_id else None,
                "url": self.reaction_url(reaction),
                "detail": detail,
            }
        )

    def progress_line(self, current: int, total: int) -> None:
        if not self.progress:
            return
        message = f"Audited logical reactions: {current}/{total}"
        print("\r" + message.ljust(self._progress_width), end="", file=sys.stderr, flush=True)
        self._progress_width = max(self._progress_width, len(message))

    def finish_progress(self) -> None:
        if self.progress and self._progress_width:
            print(file=sys.stderr)


def _chunks(values: list[UUID], size: int) -> list[list[UUID]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _canonical_mapped_smiles(smiles: str, *, include_stereochemistry: bool) -> str | None:
    try:
        molecule = Chem.MolFromSmiles(smiles)
        if molecule is None:
            return None
        if not include_stereochemistry:
            Chem.RemoveStereochemistry(molecule)
        return Chem.MolToSmiles(
            molecule,
            canonical=True,
            isomericSmiles=True,
            allHsExplicit=True,
        )
    except (RuntimeError, ValueError):
        return None


def _mapped_smiles_relation(observed: str, expected: str) -> str:
    if observed == expected:
        return "exact"
    observed_isomeric = _canonical_mapped_smiles(observed, include_stereochemistry=True)
    expected_isomeric = _canonical_mapped_smiles(expected, include_stereochemistry=True)
    if observed_isomeric is not None and observed_isomeric == expected_isomeric:
        return "canonical-equivalent"
    observed_connectivity = _canonical_mapped_smiles(observed, include_stereochemistry=False)
    expected_connectivity = _canonical_mapped_smiles(expected, include_stereochemistry=False)
    if observed_connectivity is not None and observed_connectivity == expected_connectivity:
        return "stereo-only-difference"
    return "different"


def _validate_geometry_map_subset(
    molecule: Chem.Mol,
    geometry_atom_maps: list[int],
    mapped_reaction_smiles: str,
) -> None:
    if len(geometry_atom_maps) != molecule.GetNumAtoms():
        raise ValueError("geometry atom-map count differs from geometry atom count")
    if any(number <= 0 for number in geometry_atom_maps):
        raise ValueError("geometry atom maps must be positive")
    if len(set(geometry_atom_maps)) != len(geometry_atom_maps):
        raise ValueError("geometry atom maps must be unique")
    reaction_signatures = mapped_reaction_atom_signatures(mapped_reaction_smiles)
    for atom, map_number in zip(molecule.GetAtoms(), geometry_atom_maps, strict=True):  # type: ignore[no-untyped-call]
        expected = reaction_signatures.get(map_number)
        if expected is None:
            raise ValueError(f"geometry atom map {map_number} is absent from mapped reaction")
        if (atom.GetAtomicNum(), atom.GetIsotope()) != expected:
            raise ValueError(
                f"geometry atom map {map_number} has nuclide "
                f"{atom.GetAtomicNum()}/{atom.GetIsotope()}, expected {expected[0]}/{expected[1]}"
            )


def _reaction_components_for_participants(
    participants: Sequence[MappedReactionParticipant],
    logical_participants: dict[UUID, LogicalReactionParticipant],
    topologies: dict[UUID, MolecularTopology],
    warnings: list[tuple[UUID | None, str]],
) -> tuple[ReactionComponents | None, list[str]]:
    components: dict[LogicalReactionParticipantSide, list[tuple[Chem.Mol, list[int]]]] = {
        LogicalReactionParticipantSide.REACTANT: [],
        LogicalReactionParticipantSide.PRODUCT: [],
    }
    errors: list[str] = []
    seen_keys: set[tuple[LogicalReactionParticipantSide, int]] = set()
    seen_logical_ids: set[UUID] = set()
    for participant in participants:
        key = participant.side, participant.template_index
        if key in seen_keys:
            errors.append("duplicate-participant-side-template-index")
            continue
        seen_keys.add(key)
        logical_id = participant.logical_reaction_participant_id
        if logical_id in seen_logical_ids:
            errors.append("duplicate-logical-reaction-participant")
        seen_logical_ids.add(logical_id)
        logical = logical_participants.get(logical_id)
        if logical is None:
            errors.append("missing-logical-reaction-participant")
            continue
        if logical.side is not participant.side:
            errors.append("participant-side-disagrees-with-logical-reaction")
        topology_id = participant.concrete_topology_id or logical.topology_id
        topology = topologies.get(topology_id)
        if topology is None:
            errors.append("participant-topology-missing")
            continue
        maps = [int(number) for number in participant.atom_map_numbers]
        if (
            not maps
            or len(maps) != topology.atom_count
            or any(number <= 0 for number in maps)
            or len(set(maps)) != len(maps)
        ):
            errors.append("participant-map-vector-invalid")
            continue
        try:
            expected_smiles = mapped_smiles_for_topology(topology, maps)
        except (RuntimeError, ValueError):
            errors.append("participant-mapped-smiles-cannot-be-rendered")
            continue
        if not isinstance(participant.mapped_smiles, str):
            errors.append("participant-mapped-smiles-missing-or-invalid")
        else:
            relation = _mapped_smiles_relation(expected_smiles, participant.mapped_smiles)
            if relation == "canonical-equivalent":
                warnings.append((participant.id, "participant-mapped-smiles-not-canonical"))
            elif relation == "stereo-only-difference":
                warnings.append(
                    (participant.id, "participant-mapped-smiles-stereochemistry-outdated")
                )
            elif relation == "different":
                errors.append("participant-mapped-smiles-disagrees-with-topology-and-map-vector")
        components[participant.side].append((Chem.Mol(topology.mol), maps))
    if not components[LogicalReactionParticipantSide.REACTANT]:
        errors.append("reaction-has-no-reactants")
    if not components[LogicalReactionParticipantSide.PRODUCT]:
        errors.append("reaction-has-no-products")
    return (components if not errors else None), errors


def _reaction_evidence(
    components: ReactionComponents,
    *,
    include_stereochemistry: bool,
) -> ReactionEvidence | None:
    # Use the same complete two-endpoint correspondence as ingestion. Independent
    # endpoint symmetry classes can accept a different cross-endpoint mapping.
    projected: dict[LogicalReactionParticipantSide, list[tuple[Chem.Mol, Sequence[int]]]] = {}
    for side, entries in components.items():
        projected[side] = []
        for molecule, maps in entries:
            molecule = Chem.Mol(molecule)
            if not include_stereochemistry:
                Chem.RemoveStereochemistry(molecule)
            projected[side].append((molecule, maps))
    try:
        identity = canonical_reaction_identity(projected)
    except (ValueError, RuntimeError):
        return None
    return ReactionEvidence(
        canonical_mapped_smiles=identity.smiles,
        source_map_to_canonical=identity.source_map_to_canonical,
    )


def _reaction_translation_exists(source: ReactionEvidence, target: ReactionEvidence) -> bool:
    return source.canonical_mapped_smiles == target.canonical_mapped_smiles


def _cached_reaction_evidence(
    components: ReactionComponents,
    cache: dict[bool, ReactionEvidence | None],
    *,
    include_stereochemistry: bool,
) -> ReactionEvidence | None:
    if include_stereochemistry not in cache:
        cache[include_stereochemistry] = _reaction_evidence(
            components,
            include_stereochemistry=include_stereochemistry,
        )
    return cache[include_stereochemistry]


def _mapping_matches_source_frame(
    *,
    frame: FrameSource,
    geometry: Geometry,
    geometry_maps: list[int],
    source_components: ReactionComponents,
    source_evidence: ReactionEvidence,
    target_evidence: ReactionEvidence,
) -> tuple[bool, str]:
    if not is_transition_state_frame_eligible(frame.frame_role):
        return False, "source-frame-role-not-eligible-for-transition-state-binding"
    atom_count = geometry.mol.GetNumAtoms()
    source_to_geometry = [int(index) for index in frame.observed_to_geometry]
    if sorted(source_to_geometry) != list(range(atom_count)):
        return False, "source-frame-to-geometry-order-is-not-a-permutation"
    if len(geometry_maps) != atom_count or len(set(geometry_maps)) != atom_count:
        return False, "stored-geometry-map-vector-has-invalid-length-or-duplicates"

    source_molecules = [
        molecule
        for side in (
            LogicalReactionParticipantSide.REACTANT,
            LogicalReactionParticipantSide.PRODUCT,
        )
        for molecule, _maps in source_components[side]
    ]
    if not source_molecules or any(
        molecule.GetNumAtoms() != atom_count for molecule in source_molecules
    ):
        return False, "source-endpoint-atom-count-differs-from-geometry"
    for molecule in source_molecules:
        if any(
            (atom.GetAtomicNum(), atom.GetIsotope())
            != (
                geometry.mol.GetAtomWithIdx(source_to_geometry[source_index]).GetAtomicNum(),
                geometry.mol.GetAtomWithIdx(source_to_geometry[source_index]).GetIsotope(),
            )
            for source_index, atom in enumerate(molecule.GetAtoms())  # type: ignore[no-untyped-call]
        ):
            return False, "source-endpoint-nuclide-order-disagrees-with-geometry"

    if not _reaction_translation_exists(source_evidence, target_evidence):
        return False, "complete-reaction-canonical-transform-could-not-be-established"
    source_links = source_evidence.source_map_to_canonical
    target_links = target_evidence.source_map_to_canonical
    if set(source_links) != set(range(1, atom_count + 1)) or len(target_links) != atom_count:
        return False, "reaction-maps-do-not-cover-the-source-atom-sequence"
    for source_index, geometry_index in enumerate(source_to_geometry):
        target_map = geometry_maps[geometry_index]
        if (
            target_map not in target_links
            or source_links[source_index + 1] != target_links[target_map]
        ):
            return False, "stored-map-links-a-source-atom-to-a-non-equivalent-reaction-atom"
    return True, "source-frame-transform-verified"


def _source_components_from_frame(
    frame: FrameSource,
    topologies: dict[UUID, MolecularTopology],
) -> tuple[ReactionComponents | None, str | None]:
    if frame.source_components is not None or frame.source_error is not None:
        return frame.source_components, frame.source_error
    if len(frame.endpoints) != 2:
        frame.source_error = "source-frame-does-not-have-exactly-two-persisted-endpoints"
        return None, frame.source_error
    source_molecules: list[tuple[Any, Chem.Mol]] = []
    for endpoint in frame.endpoints:
        topology = topologies.get(endpoint.topology_id)
        if topology is None:
            frame.source_error = "source-endpoint-topology-not-loaded-or-missing"
            return None, frame.source_error
        permutation = [int(index) for index in endpoint.source_to_topology]
        if topology.atom_count != endpoint.atom_count or sorted(permutation) != list(
            range(endpoint.atom_count)
        ):
            frame.source_error = "source-endpoint-to-topology-order-is-invalid"
            return None, frame.source_error
        try:
            molecule = Chem.RenumberAtoms(Chem.Mol(topology.mol), permutation)
        except (RuntimeError, ValueError):
            frame.source_error = "source-endpoint-molecule-could-not-be-reconstructed"
            return None, frame.source_error
        source_molecules.append((endpoint.direction, molecule))
    source_molecules.sort(
        key=lambda item: (
            -len(Chem.GetMolFrags(item[1])),
            str(getattr(item[0], "value", item[0])),
        )
    )
    components = _reaction_components_from_source_endpoints(
        source_molecules[0][1],
        source_molecules[1][1],
    )
    if components is None:
        frame.source_error = "source-endpoint-pair-has-incompatible-atoms-or-counts"
        return None, frame.source_error
    frame.source_components = components
    return frame.source_components, None


def _build_reaction_contexts(
    audit: Audit,
    reactions: Sequence[MappedReaction],
    participants: Sequence[MappedReactionParticipant],
    logical_participants: Sequence[LogicalReactionParticipant],
    topologies: dict[UUID, MolecularTopology],
) -> dict[UUID, ReactionContext]:
    participants_by_reaction: dict[UUID, list[MappedReactionParticipant]] = defaultdict(list)
    for participant in participants:
        participants_by_reaction[participant.mapped_reaction_id].append(participant)
    logical_by_id = {row.id: row for row in logical_participants if row.id is not None}
    contexts: dict[UUID, ReactionContext] = {}
    for reaction in reactions:
        reaction_id = reaction.id
        if reaction_id is None:
            continue
        reaction_participants = sorted(
            participants_by_reaction.get(reaction_id, []),
            key=lambda participant: (participant.side.value, participant.template_index),
        )
        warnings: list[tuple[UUID | None, str]] = []
        components, errors = _reaction_components_for_participants(
            reaction_participants,
            logical_by_id,
            topologies,
            warnings,
        )
        try:
            reaction_signatures = mapped_reaction_atom_signatures(reaction.mapped_reaction_smiles)
            if sorted(reaction_signatures) != list(range(1, len(reaction_signatures) + 1)):
                errors.append("reaction-map-numbers-are-not-contiguous-from-one")
        except ValueError as error:
            reaction_signatures = {}
            errors.append(f"mapped-reaction-smiles-invalid:{error}")
        if not _mapped_reaction_matches_participant_projection(
            reaction.mapped_reaction_smiles,
            reaction_participants,
        ):
            errors.append("mapped-reaction-serialization-disagrees-with-participants")
        evidence_cache: dict[bool, ReactionEvidence | None] = {}
        if components is not None:
            evidence_cache[True] = _reaction_evidence(
                components,
                include_stereochemistry=True,
            )
            if evidence_cache[True] is None:
                errors.append("participant-map-links-do-not-conserve-a-complete-reaction")
        if components is not None:
            participant_maps = {
                number
                for side in components.values()
                for _molecule, numbers in side
                for number in numbers
            }
            if participant_maps != set(reaction_signatures):
                errors.append("participant-map-set-disagrees-with-mapped-reaction-smiles")
        errors = list(dict.fromkeys(errors))
        contexts[reaction_id] = ReactionContext(
            reaction=reaction,
            participants=reaction_participants,
            components=components,
            errors=errors,
            warnings=warnings,
            reaction_evidence=evidence_cache,
        )
        for participant_id, warning in warnings:
            audit.counts["mapped_reaction_participants_normalization_required"] += 1
            audit.add_finding(
                classification="normalization-required",
                category="mapped-reaction-participant",
                code=warning,
                reaction=reaction,
                detail=warning,
                participant_id=participant_id,
            )
        audit.counts["mapped_reactions_checked"] += 1
        if errors:
            audit.counts["mapped_reactions_invalid"] += 1
            for detail in errors:
                audit.add_finding(
                    classification="invalid",
                    category="mapped-reaction",
                    code=detail.split(":", maxsplit=1)[0],
                    reaction=reaction,
                    detail=detail,
                )
        else:
            audit.counts["mapped_reactions_legal"] += 1
    return contexts


def _load_source_frames(
    session: Session,
    logical_ids: list[UUID],
) -> dict[tuple[UUID, UUID], list[FrameSource]]:
    if not logical_ids:
        return {}
    rows = session.execute(
        sa_select(
            col(TransitionStateInference.id),
            col(TransitionStateInference.logical_reaction_id),
            col(TransitionStateInference.mapped_reaction_id),
            col(CalculationFrame.id),
            col(CalculationFrame.geometry_id),
            col(CalculationFrame.observed_to_geometry_atom_indices),
            col(CalculationFrame.frame_role),
            col(TransitionStateEndpoint.direction),
            col(TransitionStateEndpoint.topology_id),
            col(TransitionStateEndpoint.atom_count),
            col(TransitionStateEndpoint.source_to_topology_atom_indices),
        )
        .join(
            CalculationFrame,
            col(CalculationFrame.id) == col(TransitionStateInference.calculation_frame_id),
        )
        .outerjoin(
            TransitionStateEndpoint,
            col(TransitionStateEndpoint.calculation_frame_id) == col(CalculationFrame.id),
        )
        .where(
            col(TransitionStateInference.logical_reaction_id).in_(logical_ids),
            col(TransitionStateInference.status) == TransitionStateInferenceStatus.SUCCEEDED,
        )
        .order_by(col(TransitionStateInference.id))
    ).all()
    by_frame: dict[UUID, FrameSource] = {}
    for row in rows:
        (
            inference_id,
            logical_id,
            mapped_reaction_id,
            frame_id,
            geometry_id,
            observed_to_geometry,
            frame_role,
            direction,
            topology_id,
            atom_count,
            source_to_topology,
        ) = row
        if any(
            value is None
            for value in (inference_id, logical_id, mapped_reaction_id, frame_id, geometry_id)
        ):
            continue
        source = by_frame.get(frame_id)
        if source is None:
            source = FrameSource(
                inference_id=inference_id,
                logical_reaction_id=logical_id,
                mapped_reaction_id=mapped_reaction_id,
                frame_id=frame_id,
                geometry_id=geometry_id,
                observed_to_geometry=list(observed_to_geometry or []),
                frame_role=frame_role,
            )
            by_frame[frame_id] = source
        if direction is not None and topology_id is not None and atom_count is not None:
            source.endpoints.append(
                FrameEndpoint(
                    direction=direction,
                    topology_id=topology_id,
                    atom_count=int(atom_count),
                    source_to_topology=list(source_to_topology or []),
                )
            )
    grouped: dict[tuple[UUID, UUID], list[FrameSource]] = defaultdict(list)
    for source in by_frame.values():
        grouped[(source.mapped_reaction_id, source.geometry_id)].append(source)
    return grouped


def _load_geometry_mappings(
    session: Session,
    logical_ids: list[UUID],
) -> list[GeometryMappingRow]:
    if not logical_ids:
        return []
    rows = session.execute(
        sa_select(
            MappedReactionNodeGeometry,
            MappedReactionNodeGeometryMapping,
            Geometry,
            MappedReaction,
            MappedReactionNode,
        )
        .join(
            MappedReactionNode,
            col(MappedReactionNode.id) == col(MappedReactionNodeGeometry.mapped_reaction_node_id),
        )
        .join(MappedReaction, col(MappedReaction.id) == col(MappedReactionNode.mapped_reaction_id))
        .join(Geometry, col(Geometry.id) == col(MappedReactionNodeGeometry.geometry_id))
        .outerjoin(
            MappedReactionNodeGeometryMapping,
            col(MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id)
            == col(MappedReactionNodeGeometry.id),
        )
        .where(col(MappedReaction.logical_reaction_id).in_(logical_ids))
        .order_by(col(MappedReaction.id), col(MappedReactionNodeGeometry.id))
    ).all()
    result: list[GeometryMappingRow] = []
    for binding, mapping, geometry, reaction, node in rows:
        if reaction.id is None or reaction.logical_reaction_id is None:
            continue
        result.append(
            GeometryMappingRow(
                binding=binding,
                mapping=mapping,
                geometry=geometry,
                reaction_id=reaction.id,
                logical_reaction_id=reaction.logical_reaction_id,
                role=node.role,
            )
        )
    return result


def _add_geometry_findings(
    audit: Audit,
    row: GeometryMappingRow,
    *,
    classification: str,
    category: str,
    details: list[str],
) -> None:
    if not details or row.context is None:
        return
    for detail in details:
        prefix, separator, suffix = detail.partition(":")
        inference_id: UUID | None = None
        code_detail = detail
        if separator:
            try:
                inference_id = UUID(prefix)
            except ValueError:
                pass
            else:
                code_detail = suffix
        audit.add_finding(
            classification=classification,
            category=category,
            code=code_detail.split(":", maxsplit=1)[0],
            reaction=row.context.reaction,
            detail=detail,
            geometry_id=row.geometry.id,
            node_geometry_id=row.binding.id,
            participant_id=row.binding.mapped_reaction_participant_id,
            inference_id=inference_id,
        )


def _check_endpoint_geometry_mapping(
    audit: Audit,
    row: GeometryMappingRow,
    logical_by_id: dict[UUID, LogicalReactionParticipant],
    topologies: dict[UUID, MolecularTopology],
) -> None:
    mapping = row.mapping
    context = row.context
    errors: list[str] = []
    if mapping is None:
        errors.append("endpoint-geometry-mapping-row-missing")
    elif row.binding.mapped_reaction_participant_id is None or context is None:
        errors.append("endpoint-geometry-does-not-reference-a-reaction-participant")
    else:
        participant = next(
            (
                candidate
                for candidate in context.participants
                if candidate.id == row.binding.mapped_reaction_participant_id
            ),
            None,
        )
        if participant is None:
            errors.append("endpoint-geometry-references-unknown-participant")
        else:
            logical = logical_by_id.get(participant.logical_reaction_participant_id)
            topology_id = participant.concrete_topology_id or (
                logical.topology_id if logical is not None else None
            )
            topology = topologies.get(topology_id) if topology_id is not None else None
            if topology is None:
                errors.append("endpoint-participant-topology-missing")
            else:
                if row.geometry.topology_id != topology.id:
                    errors.append("endpoint-geometry-topology-disagrees-with-participant")
                if mapping is not None:
                    maps = list(mapping.geometry_atom_map_numbers)
                    if len(maps) != row.geometry.atom_count:
                        errors.append("endpoint-geometry-map-vector-length-mismatch")
                    try:
                        _validate_geometry_map_subset(
                            row.geometry.mol,
                            maps,
                            context.reaction.mapped_reaction_smiles if context else "",
                        )
                    except ValueError as error:
                        errors.append(f"endpoint-geometry-map-nuclides-invalid:{error}")
                    try:
                        generated = _mapped_smiles_for_molecule(
                            row.geometry.mol,
                            row.geometry.atom_count,
                            maps,
                            stereo_status=topology.stereo_status,
                            include_stereochemistry=True,
                        )
                    except (RuntimeError, ValueError) as error:
                        generated = None
                        errors.append(f"endpoint-geometry-map-cannot-be-rendered:{error}")
                    if generated is not None and generated != participant.mapped_smiles:
                        relation = _mapped_smiles_relation(generated, participant.mapped_smiles)
                        if relation == "canonical-equivalent":
                            row.local_warnings.append(
                                "endpoint-participant-smiles-serialization-noncanonical"
                            )
                        elif relation == "stereo-only-difference":
                            row.local_warnings.append(
                                "endpoint-generated-stereochemistry-differs-from-participant"
                            )
                        elif relation != "canonical-equivalent":
                            errors.append(
                                "endpoint-geometry-map-does-not-project-to-participant-"
                                f"mapped-topology:{relation}"
                            )
                    if generated is not None and generated != mapping.mapped_smiles:
                        relation = _mapped_smiles_relation(generated, mapping.mapped_smiles)
                        if relation == "canonical-equivalent":
                            row.local_warnings.append(
                                "endpoint-mapping-smiles-serialization-noncanonical"
                            )
                        elif relation == "stereo-only-difference":
                            row.local_warnings.append(
                                "endpoint-mapping-smiles-stereochemistry-outdated"
                            )
                        elif relation != "canonical-equivalent":
                            errors.append(
                                f"endpoint-mapping-smiles-disagrees-with-geometry-map-vector:{relation}"
                            )
                    if generated is None:
                        errors.append("endpoint-geometry-map-projection-unavailable")
                    if (
                        mapping.mapping_method != REACTION_GEOMETRY_LINK_METHOD
                        or mapping.mapping_version != REACTION_GEOMETRY_LINK_POLICY_VERSION
                    ):
                        audit.counts["endpoint_mapping_policy_outdated"] += 1
    audit.counts["endpoint_geometry_mappings_checked"] += 1
    if errors:
        audit.counts["endpoint_geometry_mappings_invalid"] += 1
        _add_geometry_findings(
            audit,
            row,
            classification="invalid",
            category="endpoint-geometry-mapping",
            details=list(dict.fromkeys(errors)),
        )
    else:
        audit.counts["endpoint_geometry_mappings_legal"] += 1
        if row.local_warnings:
            warnings = list(dict.fromkeys(row.local_warnings))
            audit.counts["endpoint_geometry_mappings_normalization_required"] += 1
            _add_geometry_findings(
                audit,
                row,
                classification="normalization-required",
                category="endpoint-geometry-mapping",
                details=warnings,
            )


def _validate_direct_ts_row(
    row: GeometryMappingRow,
    sources: list[FrameSource],
    topologies: dict[UUID, MolecularTopology],
) -> None:
    if (
        row.mapping is None
        or row.context is None
        or row.context.components is None
        or row.context.errors
    ):
        row.direct_state = "unavailable"
        row.direct_details.append("source-proof-requires-valid-reaction-and-geometry-mapping")
        return
    usable = 0
    unavailable: list[str] = []
    contradictions: list[str] = []
    for source in sources:
        components, error = _source_components_from_frame(source, topologies)
        if error is not None or components is None:
            unavailable.append(f"{source.inference_id}:{error or 'source-reconstruction-failed'}")
            continue
        source_evidence = _cached_reaction_evidence(
            components,
            source.reaction_evidence,
            include_stereochemistry=True,
        )
        target_evidence = _cached_reaction_evidence(
            row.context.components,
            row.context.reaction_evidence,
            include_stereochemistry=True,
        )
        if source_evidence is None or target_evidence is None:
            contradictions.append(
                f"{source.inference_id}:complete-reaction-map-link-classes-could-not-be-built"
            )
            continue
        okay, detail = _mapping_matches_source_frame(
            frame=source,
            geometry=row.geometry,
            geometry_maps=list(row.mapping.geometry_atom_map_numbers),
            source_components=components,
            source_evidence=source_evidence,
            target_evidence=target_evidence,
        )
        if okay:
            usable += 1
        else:
            contradictions.append(f"{source.inference_id}:{detail}")
    if contradictions:
        row.direct_state = "contradiction"
        row.direct_details.extend(contradictions)
    elif usable and not unavailable:
        row.direct_state = "verified"
        row.direct_details.append(f"verified-from-{usable}-raw-source-frame(s)")
    elif usable:
        row.direct_state = "partial"
        row.direct_details.append(
            f"verified-from-{usable}-source-frame(s);some-evidence-unavailable"
        )
        row.direct_details.extend(unavailable)
    elif sources:
        row.direct_state = "unavailable"
        row.direct_details.extend(unavailable)
    else:
        row.direct_state = "absent"


def _sibling_transfer_proof(
    source_row: GeometryMappingRow,
    target_row: GeometryMappingRow,
) -> tuple[bool, str]:
    if (
        source_row.mapping is None
        or source_row.context is None
        or source_row.context.components is None
        or target_row.mapping is None
        or target_row.context is None
        or target_row.context.components is None
    ):
        return False, "source-or-target-reaction-context-invalid"
    source_evidence = _cached_reaction_evidence(
        source_row.context.components,
        source_row.context.reaction_evidence,
        include_stereochemistry=False,
    )
    target_evidence = _cached_reaction_evidence(
        target_row.context.components,
        target_row.context.reaction_evidence,
        include_stereochemistry=False,
    )
    if (
        source_evidence is None
        or target_evidence is None
        or not _reaction_translation_exists(source_evidence, target_evidence)
    ):
        return False, "stereo-abstracted-whole-reaction-map-transform-failed"
    source_maps = list(source_row.mapping.geometry_atom_map_numbers)
    observed_maps = list(target_row.mapping.geometry_atom_map_numbers)
    if len(source_maps) != len(observed_maps):
        return False, "source-and-target-geometry-map-vector-lengths-differ"
    for source_map, observed_map in zip(source_maps, observed_maps, strict=True):
        if (
            source_map not in source_evidence.source_map_to_canonical
            or observed_map not in target_evidence.source_map_to_canonical
            or source_evidence.source_map_to_canonical[source_map]
            != target_evidence.source_map_to_canonical[observed_map]
        ):
            return False, "sibling-map-changes-a-non-equivalent-reaction-atom"
    return True, "transferred-from-source-verified-sibling-mapping"


def _check_ts_geometry_mappings(
    audit: Audit,
    rows: list[GeometryMappingRow],
    source_frames: dict[tuple[UUID, UUID], list[FrameSource]],
    contexts: dict[UUID, ReactionContext],
    topologies: dict[UUID, MolecularTopology],
) -> None:
    ts_rows = [row for row in rows if row.role is MappedReactionNodeRole.TRANSITION_STATE]
    for row in ts_rows:
        row.context = contexts.get(row.reaction_id)
        mapping = row.mapping
        if mapping is None:
            row.local_errors.append("ts-geometry-mapping-row-missing")
        elif row.context is None or row.context.components is None or row.context.errors:
            row.local_errors.append("mapped-reaction-context-invalid")
        else:
            target_maps = {
                number
                for side in row.context.components.values()
                for _molecule, numbers in side
                for number in numbers
            }
            maps = list(mapping.geometry_atom_map_numbers)
            if (
                len(maps) != row.geometry.atom_count
                or len(set(maps)) != len(maps)
                or set(maps) != target_maps
            ):
                row.local_errors.append("ts-geometry-map-vector-does-not-cover-reaction-maps")
            try:
                validate_geometry_atom_map_elements(
                    row.geometry.mol,
                    maps,
                    row.context.reaction.mapped_reaction_smiles,
                )
            except ValueError as error:
                row.local_errors.append(f"ts-geometry-map-nuclides-invalid:{error}")
            try:
                generated = _mapped_smiles_for_molecule(
                    row.geometry.mol,
                    row.geometry.atom_count,
                    maps,
                    stereo_status=None,
                    include_stereochemistry=False,
                )
            except (RuntimeError, ValueError) as error:
                generated = None
                row.local_errors.append(f"ts-geometry-mapped-smiles-cannot-be-rendered:{error}")
            if generated is None:
                row.local_errors.append("ts-geometry-mapped-smiles-cannot-be-rendered")
            else:
                smiles_relation = _mapped_smiles_relation(generated, mapping.mapped_smiles)
                if smiles_relation == "canonical-equivalent":
                    row.local_warnings.append("ts-mapping-smiles-serialization-noncanonical")
                elif smiles_relation == "stereo-only-difference":
                    row.local_warnings.append("ts-mapping-smiles-stereochemistry-outdated")
                elif smiles_relation == "different":
                    row.local_errors.append(
                        "ts-mapping-smiles-graph-disagrees-with-geometry-map-vector"
                    )
            if not mapping.verified:
                row.local_errors.append("ts-geometry-mapping-not-marked-verified")
            audit.mapping_versions[mapping.mapping_version] += 1
            audit.mapping_methods[mapping.mapping_method] += 1
            if (
                mapping.mapping_method != REACTION_TS_GEOMETRY_LINK_METHOD
                or mapping.mapping_version != REACTION_TS_GEOMETRY_LINK_POLICY_VERSION
            ):
                audit.counts["ts_mapping_policy_outdated"] += 1
        row.direct_state = "absent"
        _validate_direct_ts_row(
            row,
            source_frames.get((row.reaction_id, row.binding.geometry_id), []),
            topologies,
        )

    siblings_by_identity: dict[tuple[UUID, UUID], list[GeometryMappingRow]] = defaultdict(list)
    for row in ts_rows:
        siblings_by_identity[(row.logical_reaction_id, row.binding.geometry_id)].append(row)
    for row in ts_rows:
        if row.local_errors:
            classification = "invalid"
            details = list(dict.fromkeys(row.local_errors))
        elif row.direct_state == "verified":
            classification = "verified"
            details = row.direct_details
        elif row.direct_state == "contradiction":
            classification = "invalid"
            details = row.direct_details
        else:
            sibling_proofs: list[str] = []
            for sibling in siblings_by_identity[(row.logical_reaction_id, row.binding.geometry_id)]:
                if sibling.reaction_id == row.reaction_id or sibling.direct_state != "verified":
                    continue
                okay, detail = _sibling_transfer_proof(sibling, row)
                if okay:
                    sibling_proofs.append(f"{sibling.reaction_id}:{detail}")
            if sibling_proofs:
                classification = "verified"
                details = sibling_proofs
            elif row.direct_state == "partial":
                classification = "partially-verifiable"
                details = row.direct_details
            else:
                classification = "unverifiable"
                details = row.direct_details or [
                    "no-successful-source-frame-for-this-reaction-geometry"
                ]

        audit.counts["ts_geometry_mappings_checked"] += 1
        audit.counts[f"ts_geometry_mappings_{classification}"] += 1
        if row.local_warnings:
            warnings = list(dict.fromkeys(row.local_warnings))
            audit.counts["ts_geometry_mappings_normalization_required"] += 1
            _add_geometry_findings(
                audit,
                row,
                classification="normalization-required",
                category="transition-state-geometry-mapping",
                details=warnings,
            )
        if classification != "verified":
            _add_geometry_findings(
                audit,
                row,
                classification=classification,
                category="transition-state-geometry-mapping",
                details=details,
            )


def run_audit(*, batch_size: int, progress: bool) -> dict[str, Any]:
    audit = Audit(progress=progress)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                with Session(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    logical_ids = list(
                        session.exec(
                            select(MappedReaction.logical_reaction_id)
                            .distinct()
                            .order_by(col(MappedReaction.logical_reaction_id))
                        ).all()
                    )
                    total = len(logical_ids)
                    completed = 0
                    for logical_batch in _chunks(logical_ids, batch_size):
                        reactions = session.exec(
                            select(MappedReaction).where(
                                col(MappedReaction.logical_reaction_id).in_(logical_batch)
                            )
                        ).all()
                        reaction_ids = [reaction.id for reaction in reactions if reaction.id]
                        participants = (
                            session.exec(
                                select(MappedReactionParticipant).where(
                                    col(MappedReactionParticipant.mapped_reaction_id).in_(
                                        reaction_ids
                                    )
                                )
                            ).all()
                            if reaction_ids
                            else []
                        )
                        logical_participants = session.exec(
                            select(LogicalReactionParticipant).where(
                                col(LogicalReactionParticipant.logical_reaction_id).in_(
                                    logical_batch
                                )
                            )
                        ).all()
                        sources = _load_source_frames(session, logical_batch)
                        geometry_rows = _load_geometry_mappings(session, logical_batch)
                        topology_ids = {
                            participant.concrete_topology_id
                            for participant in participants
                            if participant.concrete_topology_id is not None
                        }
                        topology_ids.update(
                            endpoint.topology_id
                            for frame_rows in sources.values()
                            for frame in frame_rows
                            for endpoint in frame.endpoints
                        )
                        logical_ids_missing_concrete_topology = {
                            participant.logical_reaction_participant_id
                            for participant in participants
                            if participant.concrete_topology_id is None
                        }
                        topology_ids.update(
                            logical.topology_id
                            for logical in logical_participants
                            if logical.id in logical_ids_missing_concrete_topology
                        )
                        topologies = {
                            topology.id: topology
                            for topology in session.exec(
                                select(MolecularTopology).where(
                                    col(MolecularTopology.id).in_(topology_ids)
                                )
                            ).all()
                            if topology.id is not None
                        }
                        logical_by_id = {
                            logical.id: logical
                            for logical in logical_participants
                            if logical.id is not None
                        }
                        contexts = _build_reaction_contexts(
                            audit,
                            reactions,
                            participants,
                            logical_participants,
                            topologies,
                        )
                        for row in geometry_rows:
                            row.context = contexts.get(row.reaction_id)
                            if (
                                row.mapping is not None
                                and row.role is not MappedReactionNodeRole.TRANSITION_STATE
                            ):
                                audit.mapping_versions[row.mapping.mapping_version] += 1
                                audit.mapping_methods[row.mapping.mapping_method] += 1
                            if row.role is not MappedReactionNodeRole.TRANSITION_STATE:
                                _check_endpoint_geometry_mapping(
                                    audit,
                                    row,
                                    logical_by_id,
                                    topologies,
                                )
                        _check_ts_geometry_mappings(
                            audit,
                            geometry_rows,
                            sources,
                            contexts,
                            topologies,
                        )
                        completed += len(logical_batch)
                        audit.progress_line(completed, total)
                        session.expunge_all()
                    audit.finish_progress()
                transaction.rollback()
            except Exception:
                transaction.rollback()
                raise
    finally:
        engine.dispose()
    return {
        "read_only": True,
        "database_transaction": "REPEATABLE READ, READ ONLY",
        "ts_mapping_policy_version": REACTION_TS_GEOMETRY_LINK_POLICY_VERSION,
        "endpoint_mapping_policy_version": REACTION_GEOMETRY_LINK_POLICY_VERSION,
        "counts": dict(sorted(audit.counts.items())),
        "mapping_versions": dict(sorted(audit.mapping_versions.items())),
        "mapping_methods": dict(sorted(audit.mapping_methods.items())),
        "finding_count": len(audit.findings),
        "findings": audit.findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=48)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    report = run_audit(batch_size=args.batch_size, progress=not args.quiet)
    if args.output:
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = {key: value for key, value in report.items() if key != "findings"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Full report written: {args.output}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
