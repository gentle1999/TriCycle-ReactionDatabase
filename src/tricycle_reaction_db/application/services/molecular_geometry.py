"""Idempotent persistence for Formula -> Topology -> Geometry."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

import numpy as np
import numpy.typing as npt
from sqlalchemy import Float, SmallInteger, String, and_, func, literal, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import load_only
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.chemistry import (
    NormalizedMoleculeRecord,
    NormalizedTopologyRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _assert_record_matches,
    _flush_shared_entity,
    _new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    reconcile_geometry_with_reactions,
)
from tricycle_reaction_db.core.chemistry_config import GEOMETRY_MATCH_POLICY_VERSION
from tricycle_reaction_db.core.units import radians_to_degrees
from tricycle_reaction_db.db.models import (
    Geometry,
    MappedReaction,
    MappedReactionParticipant,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
)
from tricycle_reaction_db.domain.enums import GeometryAssignmentKind
from tricycle_reaction_db.domain.internal_coordinates import proper_rigid_alignment

_IDENTITY_TRANSFORM: tuple[float, ...] = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


@dataclass(frozen=True, slots=True)
class PersistedMolecularTopology:
    formula: MolecularFormula
    topology: MolecularTopology
    topology_derivation: MolecularTopologyDerivation


@dataclass(frozen=True, slots=True)
class PersistedMolecularGeometry(PersistedMolecularTopology):
    geometry: Geometry
    geometry_assignment_kind: GeometryAssignmentKind = GeometryAssignmentKind.PARSED_EXACT
    observed_to_geometry_atom_indices: list[int] | None = None
    observed_to_geometry_transform: tuple[float, ...] = _IDENTITY_TRANSFORM
    coordinate_rmsd_angstrom: float = 0.0
    coordinate_max_abs_angstrom: float = 0.0


@dataclass(slots=True)
class GeometryPersistenceContext:
    """File-transaction caches for normalized identities and Geometry matching."""

    topologies: dict[tuple[str, ...], PersistedMolecularTopology] = field(default_factory=dict)
    formulas_by_hash: dict[str, MolecularFormula] = field(default_factory=dict)
    topologies_by_identity: dict[tuple[str, str], MolecularTopology] = field(default_factory=dict)
    topology_derivations_by_key: dict[tuple[UUID, str, str], MolecularTopologyDerivation] = field(
        default_factory=dict
    )
    # Effective upstreams are cached only for this transaction.  The cache is
    # invalidated whenever a newly materialized abstraction upstream appears
    # in the context, so a previous reflexive fallback cannot hide it.
    topology_upstreams_by_key: dict[tuple[UUID, str], tuple[MolecularTopology, ...]] = field(
        default_factory=dict
    )
    geometries_by_hash: dict[tuple[UUID, str, str, int, int], Geometry] = field(
        default_factory=dict
    )
    # Keys populated by one PostgreSQL bulk lookup. A missing key is useful
    # information too: it lets the frame loop skip a per-Geometry SELECT.
    exact_geometry_keys_loaded: set[tuple[UUID, str, str, int, int]] = field(default_factory=set)
    equivalent_geometry_by_key: dict[tuple[UUID, str, str, int, int], Geometry] = field(
        default_factory=dict
    )
    equivalent_geometry_candidates: dict[tuple[UUID, str, str, int, int], tuple[UUID, ...]] = field(
        default_factory=dict
    )
    # Keep the complete preloaded candidate rows so the closest candidate can
    # be selected against each observation without another database roundtrip.
    equivalent_geometry_rows_by_key: dict[tuple[UUID, str, str, int, int], tuple[Geometry, ...]] = (
        field(default_factory=dict)
    )
    equivalent_geometry_keys_loaded: set[tuple[UUID, str, str, int, int]] = field(
        default_factory=set
    )
    # Geometry rows created in the current SQLAlchemy transaction are not
    # visible to PostgreSQL until the fast-insert queue is flushed.  Keep a
    # small in-memory index so two near-equivalent observations in one
    # persistence microbatch reuse the first row instead of creating a pair
    # of hash-distinct Geometry records.
    in_memory_geometries_by_identity: dict[tuple[UUID, str, int, int], tuple[Geometry, ...]] = (
        field(default_factory=dict)
    )
    geometries_to_reconcile: dict[UUID, Geometry] = field(default_factory=dict)
    # Reaction participants are keyed by topology because reconciliation runs
    # once per newly observed Geometry.  Reusing this lookup avoids one SELECT
    # per Geometry when a batch contains many conformers of the same topology.
    reaction_participants_by_topology: dict[UUID, tuple[MappedReactionParticipant, ...]] = field(
        default_factory=dict
    )
    mapped_reactions_by_id: dict[UUID, MappedReaction] = field(default_factory=dict)
    # A mapped reaction can be created after its endpoint Geometry rows were
    # committed in an earlier ingestion microbatch. Keep those reactions in
    # the batch context so the reconciliation barrier performs the reverse
    # (reaction -> existing Geometry) lookup as well as the normal Geometry ->
    # existing reaction lookup.
    mapped_reactions_to_reconcile: dict[UUID, MappedReaction] = field(default_factory=dict)
    # Concrete topologies are resolved after the batch's deferred TS reactions
    # have been flushed, so a Geometry arriving before its logical template is
    # not missed and the frame loop does not repeat the same global lookup.
    topologies_to_resolve_reactions: set[UUID] = field(default_factory=set)
    # TS inference commonly repeats the same strict endpoint reaction across
    # files. The cache key is a digest of the mapped reaction plus the strict
    # endpoint/topology identities; reaction_smiles alone is not sufficient
    # because one logical reaction can have several concrete stereochemical
    # variants.
    inferred_reaction_ids_by_key: dict[str, tuple[UUID, UUID]] = field(default_factory=dict)
    inferred_reaction_topology_records_by_key: dict[str, tuple[Any, ...]] = field(
        default_factory=dict
    )
    inferred_reaction_cache_hits: int = 0
    # Created lazily by batch reconciliation to avoid a module import cycle.
    reconciliation_cache: Any = None


# Keep each set-based equivalence statement below the database statement
# timeout even when one file batch contains many thousands of frame keys.
GEOMETRY_MATCH_INPUT_BATCH_SIZE = 128


class GeometryAssignmentAmbiguityError(ValueError):
    """Legacy error type retained for callers handling pre-v4 failures."""

    error_code = "geometry_assignment_ambiguous"
    rule_id = "geometry.unique-coordinate-match"

    def __init__(
        self,
        *,
        topology_id: object,
        observed_geometry_hash: str,
        candidate_ids: Sequence[object],
    ) -> None:
        self.topology_id = topology_id
        self.observed_geometry_hash = observed_geometry_hash
        self.candidate_ids = list(candidate_ids)
        super().__init__(
            "Geometry coordinate match is ambiguous under "
            f"{GEOMETRY_MATCH_POLICY_VERSION}: candidates={candidate_ids}"
        )

    def evidence(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "policy_version": GEOMETRY_MATCH_POLICY_VERSION,
            "outcome": "fail",
            "error_code": self.error_code,
            "topology_id": str(self.topology_id),
            "observed_geometry_hash": self.observed_geometry_hash,
            "candidate_geometry_ids": [str(candidate_id) for candidate_id in self.candidate_ids],
            "candidate_count": len(self.candidate_ids),
        }


def _topology_order_coordinates(
    coordinates: npt.NDArray[np.generic],
    topology_atom_indices: list[int],
) -> npt.NDArray[np.float64]:
    ordered = np.empty_like(np.asarray(coordinates, dtype=np.float64))
    for source_index, topology_index in enumerate(topology_atom_indices):
        ordered[topology_index] = coordinates[source_index]
    return ordered


def _coordinate_alignment(
    observed: npt.NDArray[np.float64],
    reference: npt.NDArray[np.float64],
) -> tuple[float, float, tuple[float, ...]]:
    """Align observed onto reference with a proper rigid transform."""

    return proper_rigid_alignment(observed, reference)


def _nearest_geometry_candidate(
    record: NormalizedMoleculeRecord,
    candidates: Sequence[Geometry],
) -> tuple[Geometry, float, float, tuple[float, ...]]:
    """Choose the closest equivalent Geometry with a deterministic tie-break."""

    if not candidates:
        raise ValueError("at least one Geometry candidate is required")
    observed_topology_coords = _topology_order_coordinates(
        record.observed_coordinates,
        record.observed_to_geometry_atom_indices,
    )
    scored: list[tuple[float, float, str, Geometry, tuple[float, ...]]] = []
    for candidate in candidates:
        candidate_id = _require_id(candidate, label="Geometry")
        candidate_coordinates = np.asarray(
            candidate.mol.GetConformer().GetPositions(),
            dtype=np.float64,
        )
        rmsd, max_abs, transform = _coordinate_alignment(
            observed_topology_coords,
            candidate_coordinates,
        )
        scored.append((rmsd, max_abs, str(candidate_id), candidate, transform))
    rmsd, max_abs, _candidate_id, geometry, transform = min(
        scored,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return geometry, rmsd, max_abs, transform


def _coordinate_error(
    observed: npt.NDArray[np.float64],
    reference: npt.NDArray[np.float64],
) -> tuple[float, float]:
    rmsd, max_abs, _ = _coordinate_alignment(observed, reference)
    return rmsd, max_abs


def _internal_coordinate_projection(
    internal_coordinates: npt.NDArray[np.generic],
) -> tuple[list[float], list[float], list[float]]:
    values = np.asarray(internal_coordinates, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("Geometry internal coordinates must have shape (atom_count, 3)")
    return (
        [float(value) for value in values[:, 0]],
        [float(value) for value in values[:, 1]],
        [float(value) for value in values[:, 2]],
    )


def _internal_coordinate_arrays_equivalent(
    candidate_distances: Sequence[float],
    candidate_angles: Sequence[float],
    candidate_dihedrals: Sequence[float],
    candidate_minimum_coordinate_decimal_places: int | None,
    observed_distances: Sequence[float],
    observed_angles: Sequence[float],
    observed_dihedrals: Sequence[float],
    observed_coordinate_decimal_places: int | None,
) -> bool:
    """Mirror the PostgreSQL geometry-equivalence predicate for pending rows."""

    candidate_distance_values = np.asarray(candidate_distances, dtype=np.float64)
    candidate_angle_values = np.asarray(candidate_angles, dtype=np.float64)
    candidate_dihedral_values = np.asarray(candidate_dihedrals, dtype=np.float64)
    observed_distance_values = np.asarray(observed_distances, dtype=np.float64)
    observed_angle_values = np.asarray(observed_angles, dtype=np.float64)
    observed_dihedral_values = np.asarray(observed_dihedrals, dtype=np.float64)
    arrays = (
        candidate_distance_values,
        candidate_angle_values,
        candidate_dihedral_values,
        observed_distance_values,
        observed_angle_values,
        observed_dihedral_values,
    )
    if any(array.ndim != 1 for array in arrays):
        return False
    candidate_length = candidate_distance_values.size
    observed_length = observed_distance_values.size
    if (
        candidate_length == 0
        or candidate_length != observed_length
        or candidate_angle_values.size != candidate_length
        or candidate_dihedral_values.size != candidate_length
        or observed_angle_values.size != observed_length
        or observed_dihedral_values.size != observed_length
        or any(not np.isfinite(array).all() for array in arrays)
    ):
        return False

    if (
        candidate_minimum_coordinate_decimal_places is None
        and observed_coordinate_decimal_places is None
    ):
        coordinate_tolerance = 1e-6
    else:
        known_places = min(
            candidate_minimum_coordinate_decimal_places
            if candidate_minimum_coordinate_decimal_places is not None
            else 18,
            observed_coordinate_decimal_places
            if observed_coordinate_decimal_places is not None
            else 18,
        )
        coordinate_tolerance = max(1e-8, 1.1 * 10.0 ** (-known_places))
    distance_tolerance = 2.2 * coordinate_tolerance
    if np.any(np.abs(candidate_distance_values - observed_distance_values) > distance_tolerance):
        return False

    positive_distances = np.concatenate(
        (
            candidate_distance_values[candidate_distance_values > 1e-8],
            observed_distance_values[observed_distance_values > 1e-8],
        )
    )
    minimum_positive_distance = (
        float(np.min(positive_distances)) if positive_distances.size else 1.0
    )
    length_scale = max(minimum_positive_distance, 0.1)
    angular_tolerance = max(
        1e-6,
        float(radians_to_degrees(4.0 * coordinate_tolerance / length_scale)),
    )
    if np.any(np.abs(candidate_angle_values - observed_angle_values) > angular_tolerance):
        return False

    candidate_is_linear = (
        np.minimum(
            np.abs(candidate_angle_values),
            np.abs(180.0 - candidate_angle_values),
        )
        <= angular_tolerance
    )
    observed_is_linear = (
        np.minimum(
            np.abs(observed_angle_values),
            np.abs(180.0 - observed_angle_values),
        )
        <= angular_tolerance
    )
    non_linear = ~(candidate_is_linear | observed_is_linear)
    if np.any(non_linear):
        dihedral_delta = candidate_dihedral_values - observed_dihedral_values
        dihedral_delta = np.abs(dihedral_delta - 360.0 * np.floor((dihedral_delta + 180.0) / 360.0))
        if np.any(dihedral_delta[non_linear] > angular_tolerance):
            return False
    return True


def _geometry_match_identity(
    *,
    topology_id: UUID,
    canonicalization_version: str,
    charge: int,
    multiplicity: int,
) -> tuple[UUID, str, int, int]:
    return topology_id, canonicalization_version, charge, multiplicity


def _geometry_projection_from_entity(
    geometry: Geometry,
) -> tuple[Sequence[float], Sequence[float], Sequence[float]] | None:
    """Read the non-deferred projection without triggering a lazy load."""

    values = tuple(
        geometry.__dict__.get(field_name)
        for field_name in (
            "internal_coordinate_distances_angstrom",
            "internal_coordinate_angles_degrees",
            "internal_coordinate_dihedrals_degrees",
        )
    )
    if any(value is None for value in values):
        return None
    return cast(
        tuple[Sequence[float], Sequence[float], Sequence[float]],
        values,
    )


def _register_in_memory_geometry(
    context: GeometryPersistenceContext,
    geometry: Geometry,
    *,
    topology_id: UUID,
    canonicalization_version: str,
    charge: int,
    multiplicity: int,
) -> None:
    """Make a transaction-local Geometry visible to later frame writes."""

    if not isinstance(geometry.id, UUID):
        return
    if _geometry_projection_from_entity(geometry) is None:
        return
    identity = _geometry_match_identity(
        topology_id=topology_id,
        canonicalization_version=canonicalization_version,
        charge=charge,
        multiplicity=multiplicity,
    )
    candidates = context.in_memory_geometries_by_identity.get(identity, ())
    if all(candidate.id != geometry.id for candidate in candidates):
        context.in_memory_geometries_by_identity[identity] = (*candidates, geometry)


def _find_in_memory_geometry_match(
    context: GeometryPersistenceContext,
    *,
    topology_id: UUID,
    record: NormalizedMoleculeRecord,
    coordinate_decimal_places: int | None,
) -> Geometry | None:
    """Find the closest equivalent Geometry among pending transaction rows."""

    identity = _geometry_match_identity(
        topology_id=topology_id,
        canonicalization_version=record.geometry.canonicalization_version,
        charge=record.charge,
        multiplicity=record.multiplicity,
    )
    observed_distances, observed_angles, observed_dihedrals = _internal_coordinate_projection(
        record.geometry.internal_coordinates
    )
    matches: list[Geometry] = []
    for candidate in context.in_memory_geometries_by_identity.get(identity, ()):
        projection = _geometry_projection_from_entity(candidate)
        if projection is None:
            continue
        candidate_distances, candidate_angles, candidate_dihedrals = projection
        if _internal_coordinate_arrays_equivalent(
            candidate_distances,
            candidate_angles,
            candidate_dihedrals,
            candidate.minimum_coordinate_decimal_places,
            observed_distances,
            observed_angles,
            observed_dihedrals,
            coordinate_decimal_places,
        ):
            matches.append(candidate)
    return _nearest_geometry_candidate(record, matches)[0] if matches else None


def _find_database_geometry_match(
    session: Session,
    *,
    topology: MolecularTopology,
    record: NormalizedMoleculeRecord,
    coordinate_decimal_places: int | None,
) -> tuple[Geometry, list[int], float, float, tuple[float, ...]] | None:
    """Let PostgreSQL narrow candidates, then choose the closest Geometry."""

    observed = record.geometry
    distances, angles, dihedrals = _internal_coordinate_projection(observed.internal_coordinates)
    matching_ids = list(
        session.exec(
            select(Geometry.id).where(
                Geometry.topology_id == topology.id,
                Geometry.canonicalization_version == observed.canonicalization_version,
                Geometry.charge == record.charge,
                Geometry.multiplicity == record.multiplicity,
                func.geometry_internal_coordinates_equivalent(
                    Geometry.internal_coordinate_distances_angstrom,
                    Geometry.internal_coordinate_angles_degrees,
                    Geometry.internal_coordinate_dihedrals_degrees,
                    Geometry.minimum_coordinate_decimal_places,
                    literal(distances, type_=ARRAY(Float)),
                    literal(angles, type_=ARRAY(Float)),
                    literal(dihedrals, type_=ARRAY(Float)),
                    literal(coordinate_decimal_places, type_=SmallInteger),
                ),
            )
        ).all()
    )
    if not matching_ids:
        return None
    geometry_columns = cast(Any, Geometry)
    candidates = tuple(
        session.exec(select(Geometry).where(geometry_columns.id.in_(matching_ids))).all()
    )
    if len(candidates) != len(matching_ids):
        raise RuntimeError("database returned an incomplete Geometry candidate set")
    candidate, rmsd, max_abs, transform = _nearest_geometry_candidate(
        record,
        candidates,
    )
    return (
        candidate,
        list(record.observed_to_geometry_atom_indices),
        rmsd,
        max_abs,
        transform,
    )


def _topology_context_key(record: NormalizedTopologyRecord) -> tuple[str, ...]:
    return (
        record.formula.composition_hash,
        record.topology.identity_schema_version,
        record.topology.graph_hash,
        record.topology_derivation.provenance_schema_version,
        record.topology_derivation.provenance_hash,
    )


def _validate_cached_topology(
    persisted: PersistedMolecularTopology,
    record: NormalizedTopologyRecord,
) -> None:
    if persisted.formula.composition_hash != record.formula.composition_hash:
        raise ValueError("cached molecular formula identity is inconsistent")
    topology = persisted.topology
    if (
        topology.formula_id != persisted.formula.id
        or topology.identity_schema_version != record.topology.identity_schema_version
        or topology.graph_hash != record.topology.graph_hash
    ):
        raise ValueError("cached molecular topology identity is inconsistent")
    _assert_record_matches(
        persisted.topology_derivation,
        record.topology_derivation,
        label="MolecularTopologyDerivation",
    )


def _promote_stereo_abstraction_upstream_marker(
    session: Session,
    topology: MolecularTopology,
    should_mark: bool,
    *,
    context: GeometryPersistenceContext | None,
) -> None:
    """Monotonically promote a topology proven by the TS abstraction path."""

    if not should_mark or topology.is_stereo_abstraction_upstream:
        return
    topology.is_stereo_abstraction_upstream = True
    if context is not None:
        context.topology_upstreams_by_key.clear()

    state = cast(Any, sa_inspect(topology))
    if state.persistent:
        session.flush([topology])
    elif state.detached:
        # Fast bulk insertion detaches rows after the Core INSERT.  A later
        # explicit proof of the marker must still update that already inserted
        # row, while transient/pending rows will carry the changed field into
        # their deferred INSERT naturally.
        topology_id = _require_id(topology, label="MolecularTopology")
        session.execute(
            text(
                "UPDATE molecular_topology "
                "SET is_stereo_abstraction_upstream = true "
                "WHERE id = :topology_id"
            ),
            {"topology_id": topology_id},
        )


def _register_topology_upstreams(
    session: Session,
    topology: MolecularTopology,
    *,
    context: GeometryPersistenceContext | None,
) -> tuple[MolecularTopology, ...]:
    """Resolve and persist marked abstraction upstreams for one topology."""

    from tricycle_reaction_db.application.services.topology_abstraction import (
        STEREO_ABSTRACTION_POLICY_VERSION,
        ensure_topology_upstreams,
    )

    topology_id = _require_id(topology, label="MolecularTopology")
    cache_key = (topology_id, STEREO_ABSTRACTION_POLICY_VERSION)
    if context is not None:
        cached = context.topology_upstreams_by_key.get(cache_key)
        if cached is not None:
            return cached

    candidates: list[MolecularTopology] = []
    if context is not None:
        candidates.extend(context.topologies_by_identity.values())
    session_info = getattr(session, "info", {})
    candidates.extend(
        entity
        for entity in session_info.get("_fast_pending_entities", ())
        if isinstance(entity, MolecularTopology)
    )
    upstreams = ensure_topology_upstreams(
        session,
        topology,
        abstraction_policy_version=STEREO_ABSTRACTION_POLICY_VERSION,
        candidate_topologies=candidates,
    )
    if context is not None:
        context.topology_upstreams_by_key[cache_key] = upstreams
    return upstreams


def _preload_molecular_topologies(
    session: Session,
    records: Sequence[NormalizedTopologyRecord],
    *,
    context: GeometryPersistenceContext,
) -> None:
    """Resolve a batch of shared molecular identities with set-based reads."""

    pending: dict[tuple[str, ...], NormalizedTopologyRecord] = {}
    for record in records:
        context_key = _topology_context_key(record)
        if context_key not in context.topologies:
            pending.setdefault(context_key, record)
    if not pending:
        return

    formula_hashes = {
        record.formula.composition_hash
        for record in pending.values()
        if record.formula.composition_hash not in context.formulas_by_hash
    }
    topology_identity_keys = {
        (record.topology.identity_schema_version, record.topology.graph_hash)
        for record in pending.values()
        if (
            record.topology.identity_schema_version,
            record.topology.graph_hash,
        )
        not in context.topologies_by_identity
    }
    if formula_hashes or topology_identity_keys:
        _acquire_identity_locks(
            session,
            *(
                ("molecular_formula", composition_hash)
                for composition_hash in sorted(formula_hashes)
            ),
            *(
                ("molecular_topology", schema_version, graph_hash)
                for schema_version, graph_hash in sorted(topology_identity_keys)
            ),
        )

    if formula_hashes:
        for existing_formula in session.exec(
            select(MolecularFormula).where(
                col(MolecularFormula.composition_hash).in_(formula_hashes)
            )
        ).all():
            context.formulas_by_hash[existing_formula.composition_hash] = existing_formula
    for record in pending.values():
        composition_hash = record.formula.composition_hash
        formula = context.formulas_by_hash.get(composition_hash)
        if formula is not None:
            continue
        formula = _new_entity(session, MolecularFormula, **record.formula.model_dump())
        _flush_shared_entity(
            session,
            formula,
            label="MolecularFormula",
            defer_if_fast=True,
        )
        context.formulas_by_hash[composition_hash] = formula

    if topology_identity_keys:
        for existing_topology in session.exec(
            select(MolecularTopology).where(
                col(MolecularTopology.identity_schema_version).in_(
                    {schema_version for schema_version, _ in topology_identity_keys}
                ),
                col(MolecularTopology.graph_hash).in_(
                    {graph_hash for _, graph_hash in topology_identity_keys}
                ),
            )
        ).all():
            context.topologies_by_identity[
                (existing_topology.identity_schema_version, existing_topology.graph_hash)
            ] = existing_topology
    for record in pending.values():
        identity_key = (
            record.topology.identity_schema_version,
            record.topology.graph_hash,
        )
        formula = context.formulas_by_hash[record.formula.composition_hash]
        topology = context.topologies_by_identity.get(identity_key)
        if topology is not None:
            if topology.formula_id != formula.id:
                raise ValueError("topology identity resolved to a different molecular formula")
            _promote_stereo_abstraction_upstream_marker(
                session,
                topology,
                record.topology.is_stereo_abstraction_upstream,
                context=context,
            )
            continue
        topology = _new_entity(
            session, MolecularTopology, formula=formula, **record.topology.model_dump()
        )
        _flush_shared_entity(
            session,
            topology,
            label="MolecularTopology",
            defer_if_fast=True,
        )
        context.topologies_by_identity[identity_key] = topology
        if topology.is_stereo_abstraction_upstream:
            context.topology_upstreams_by_key.clear()

    derivation_keys = {
        (
            _require_id(
                context.topologies_by_identity[
                    (record.topology.identity_schema_version, record.topology.graph_hash)
                ],
                label="MolecularTopology",
            ),
            record.topology_derivation.provenance_schema_version,
            record.topology_derivation.provenance_hash,
        )
        for record in pending.values()
    }
    missing_derivation_keys = {
        key for key in derivation_keys if key not in context.topology_derivations_by_key
    }
    if missing_derivation_keys:
        _acquire_identity_locks(
            session,
            *(
                (
                    "molecular_topology_derivation",
                    topology_id,
                    provenance_schema_version,
                    provenance_hash,
                )
                for topology_id, provenance_schema_version, provenance_hash in sorted(
                    missing_derivation_keys,
                    key=str,
                )
            ),
        )
        for derivation in session.exec(
            select(MolecularTopologyDerivation).where(
                col(MolecularTopologyDerivation.topology_id).in_(
                    {key[0] for key in missing_derivation_keys}
                ),
                col(MolecularTopologyDerivation.provenance_schema_version).in_(
                    {key[1] for key in missing_derivation_keys}
                ),
                col(MolecularTopologyDerivation.provenance_hash).in_(
                    {key[2] for key in missing_derivation_keys}
                ),
            )
        ).all():
            context.topology_derivations_by_key[
                (
                    derivation.topology_id,
                    derivation.provenance_schema_version,
                    derivation.provenance_hash,
                )
            ] = derivation
    for context_key, record in pending.items():
        topology = context.topologies_by_identity[
            (record.topology.identity_schema_version, record.topology.graph_hash)
        ]
        topology_id = _require_id(topology, label="MolecularTopology")
        derivation_key = (
            topology_id,
            record.topology_derivation.provenance_schema_version,
            record.topology_derivation.provenance_hash,
        )
        topology_derivation = context.topology_derivations_by_key.get(derivation_key)
        if topology_derivation is None:
            topology_derivation = _new_entity(
                session,
                MolecularTopologyDerivation,
                topology=topology,
                **record.topology_derivation.model_dump(),
            )
            _flush_shared_entity(
                session,
                topology_derivation,
                label="MolecularTopologyDerivation",
                defer_if_fast=True,
            )
            context.topology_derivations_by_key[derivation_key] = topology_derivation
        else:
            _assert_record_matches(
                topology_derivation,
                record.topology_derivation,
                label="MolecularTopologyDerivation",
            )
        persisted = PersistedMolecularTopology(
            formula=context.formulas_by_hash[record.formula.composition_hash],
            topology=topology,
            topology_derivation=topology_derivation,
        )
        context.topologies[context_key] = persisted
        _register_topology_upstreams(session, topology, context=context)


def persist_molecular_topology(
    session: Session,
    record: NormalizedTopologyRecord,
    *,
    context: GeometryPersistenceContext | None = None,
    register_upstream: bool = True,
) -> PersistedMolecularTopology:
    """Insert or reuse Formula and Topology without requiring a Geometry."""

    context_key = _topology_context_key(record)
    if context is not None and (cached := context.topologies.get(context_key)) is not None:
        _validate_cached_topology(cached, record)
        _promote_stereo_abstraction_upstream_marker(
            session,
            cached.topology,
            record.topology.is_stereo_abstraction_upstream,
            context=context,
        )
        if register_upstream:
            _register_topology_upstreams(session, cached.topology, context=context)
        return cached

    formula = (
        context.formulas_by_hash.get(record.formula.composition_hash)
        if context is not None
        else None
    )
    topology_identity = (
        record.topology.identity_schema_version,
        record.topology.graph_hash,
    )
    topology = (
        context.topologies_by_identity.get(topology_identity) if context is not None else None
    )
    if formula is None or topology is None:
        lock_keys: list[tuple[object, ...]] = []
        if formula is None:
            lock_keys.append(("molecular_formula", record.formula.composition_hash))
        if topology is None:
            lock_keys.append(("molecular_topology", *topology_identity))
        _acquire_identity_locks(session, *lock_keys)
    if formula is None:
        formula = session.exec(
            select(MolecularFormula).where(
                MolecularFormula.composition_hash == record.formula.composition_hash
            )
        ).first()
    if formula is None:
        formula = _new_entity(session, MolecularFormula, **record.formula.model_dump())
        _flush_shared_entity(session, formula, label="MolecularFormula", defer_if_fast=True)
    if context is not None:
        context.formulas_by_hash[record.formula.composition_hash] = formula
    if formula.id is None:
        raise RuntimeError("database did not generate MolecularFormula.id")

    if topology is None:
        topology = session.exec(
            select(MolecularTopology).where(
                MolecularTopology.identity_schema_version
                == record.topology.identity_schema_version,
                MolecularTopology.graph_hash == record.topology.graph_hash,
            )
        ).first()
    topology_created = topology is None
    if topology is None:
        topology = _new_entity(
            session,
            MolecularTopology,
            formula=formula,
            **record.topology.model_dump(),
        )
        _flush_shared_entity(session, topology, label="MolecularTopology", defer_if_fast=True)
    elif topology.formula_id != formula.id:
        raise ValueError("topology identity resolved to a different molecular formula")
    _promote_stereo_abstraction_upstream_marker(
        session,
        topology,
        record.topology.is_stereo_abstraction_upstream,
        context=context,
    )
    if context is not None:
        context.topologies_by_identity[topology_identity] = topology
        if topology_created and topology.is_stereo_abstraction_upstream:
            context.topology_upstreams_by_key.clear()
    # The graph identity is canonical, but the stored descriptors are one
    # projection of that graph.  A batch may legitimately produce another
    # projection for the same identity, so the first persisted projection wins.
    if topology.id is None:
        raise RuntimeError("database did not generate MolecularTopology.id")
    topology_id = _require_id(topology, label="MolecularTopology")

    derivation_key = (
        topology_id,
        record.topology_derivation.provenance_schema_version,
        record.topology_derivation.provenance_hash,
    )
    topology_derivation = (
        context.topology_derivations_by_key.get(derivation_key) if context is not None else None
    )
    if topology_derivation is None:
        _acquire_identity_locks(
            session,
            (
                "molecular_topology_derivation",
                topology_id,
                record.topology_derivation.provenance_schema_version,
                record.topology_derivation.provenance_hash,
            ),
        )
        topology_derivation = session.exec(
            select(MolecularTopologyDerivation).where(
                MolecularTopologyDerivation.topology_id == topology_id,
                MolecularTopologyDerivation.provenance_schema_version
                == record.topology_derivation.provenance_schema_version,
                MolecularTopologyDerivation.provenance_hash
                == record.topology_derivation.provenance_hash,
            )
        ).first()
    if topology_derivation is None:
        topology_derivation = _new_entity(
            session,
            MolecularTopologyDerivation,
            topology=topology,
            **record.topology_derivation.model_dump(),
        )
        _flush_shared_entity(
            session,
            topology_derivation,
            label="MolecularTopologyDerivation",
            defer_if_fast=True,
        )
    else:
        _assert_record_matches(
            topology_derivation,
            record.topology_derivation,
            label="MolecularTopologyDerivation",
        )
    if context is not None:
        context.topology_derivations_by_key[derivation_key] = topology_derivation
    persisted = PersistedMolecularTopology(
        formula=formula,
        topology=topology,
        topology_derivation=topology_derivation,
    )
    if context is not None:
        context.topologies[context_key] = persisted
    if register_upstream:
        _register_topology_upstreams(session, topology, context=context)
    return persisted


def persist_molecular_geometry(
    session: Session,
    record: NormalizedMoleculeRecord,
    *,
    coordinate_decimal_places: int | None = None,
    context: GeometryPersistenceContext | None = None,
) -> PersistedMolecularGeometry:
    """Insert or reuse one normalized three-level chemical record."""

    persisted_topology = persist_molecular_topology(
        session,
        NormalizedTopologyRecord(
            formula=record.formula,
            topology=record.topology,
            topology_derivation=record.topology_derivation,
        ),
        context=context,
    )
    formula = persisted_topology.formula
    topology = persisted_topology.topology
    topology_derivation = persisted_topology.topology_derivation

    topology_id = _require_id(topology, label="MolecularTopology")
    geometry_key = (
        topology_id,
        record.geometry.canonicalization_version,
        record.geometry.geometry_hash,
        record.charge,
        record.multiplicity,
    )
    # Serialize tolerance-equivalence decisions for this topology/electronic
    # state across concurrent transactions.  The database uniqueness
    # constraint is exact, while the reuse decision is tolerance-based.
    _acquire_identity_locks(
        session,
        (
            "geometry-equivalence",
            topology_id,
            record.geometry.canonicalization_version,
            record.charge,
            record.multiplicity,
        ),
    )
    geometry = context.geometries_by_hash.get(geometry_key) if context is not None else None
    equivalent_match = False
    if geometry is None and (
        context is None or geometry_key not in context.exact_geometry_keys_loaded
    ):
        geometry = session.exec(
            select(Geometry).where(
                Geometry.topology_id == topology.id,
                Geometry.canonicalization_version == record.geometry.canonicalization_version,
                Geometry.geometry_hash == record.geometry.geometry_hash,
                Geometry.charge == record.charge,
                Geometry.multiplicity == record.multiplicity,
            )
        ).first()
        if geometry is not None and context is not None:
            context.geometries_by_hash[geometry_key] = geometry
    if geometry is None and context is not None:
        geometry = context.equivalent_geometry_by_key.get(geometry_key)
        if geometry is None:
            candidate_rows = context.equivalent_geometry_rows_by_key.get(geometry_key)
            if candidate_rows is None:
                candidate_ids = context.equivalent_geometry_candidates.get(geometry_key, ())
                if candidate_ids:
                    geometry_columns = cast(Any, Geometry)
                    candidate_rows = tuple(
                        session.exec(
                            select(Geometry).where(geometry_columns.id.in_(candidate_ids))
                        ).all()
                    )
                    if len(candidate_rows) != len(candidate_ids):
                        raise RuntimeError("database returned an incomplete Geometry candidate set")
                    context.equivalent_geometry_rows_by_key[geometry_key] = candidate_rows
            if candidate_rows:
                geometry = _nearest_geometry_candidate(record, candidate_rows)[0]
        equivalent_match = geometry is not None
    if geometry is None and context is not None:
        geometry = _find_in_memory_geometry_match(
            context,
            topology_id=topology_id,
            record=record,
            coordinate_decimal_places=coordinate_decimal_places,
        )
        equivalent_match = geometry is not None
        if geometry is not None:
            context.equivalent_geometry_by_key[geometry_key] = geometry
            context.equivalent_geometry_candidates[geometry_key] = (
                _require_id(geometry, label="Geometry"),
            )
    assignment_kind = GeometryAssignmentKind.PARSED_EXACT
    assignment_indices: list[int] | None = list(record.observed_to_geometry_atom_indices)
    assignment_transform = tuple(record.observed_to_geometry_transform)
    assignment_rmsd = record.geometry_assignment_rmsd_angstrom
    assignment_max_abs = record.geometry_assignment_max_abs_angstrom
    if equivalent_match and geometry is not None:
        observed_topology_coords = _topology_order_coordinates(
            record.observed_coordinates,
            record.observed_to_geometry_atom_indices,
        )
        candidate_coordinates = np.asarray(
            geometry.mol.GetConformer().GetPositions(),
            dtype=np.float64,
        )
        assignment_rmsd, assignment_max_abs, assignment_transform = _coordinate_alignment(
            observed_topology_coords, candidate_coordinates
        )
        assignment_kind = GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
    if geometry is None and (
        context is None or geometry_key not in context.equivalent_geometry_keys_loaded
    ):
        matched = _find_database_geometry_match(
            session,
            topology=topology,
            record=record,
            coordinate_decimal_places=coordinate_decimal_places,
        )
        if matched is not None:
            (
                geometry,
                assignment_indices,
                assignment_rmsd,
                assignment_max_abs,
                assignment_transform,
            ) = matched
            assignment_kind = GeometryAssignmentKind.MATCHED_EXISTING_GEOMETRY
            if context is not None:
                context.equivalent_geometry_by_key[geometry_key] = geometry
    if geometry is None:
        distances, angles, dihedrals = _internal_coordinate_projection(
            record.geometry.internal_coordinates
        )
        geometry = _new_entity(
            session,
            Geometry,
            topology=topology,
            **record.geometry.model_dump(),
            internal_coordinate_distances_angstrom=distances,
            internal_coordinate_angles_degrees=angles,
            internal_coordinate_dihedrals_degrees=dihedrals,
            minimum_coordinate_decimal_places=coordinate_decimal_places,
        )
        _flush_shared_entity(session, geometry, label="Geometry", defer_if_fast=True)
    if coordinate_decimal_places is not None:
        current = geometry.minimum_coordinate_decimal_places
        minimum_places = (
            coordinate_decimal_places
            if current is None
            else min(current, coordinate_decimal_places)
        )
        if minimum_places != current:
            geometry.minimum_coordinate_decimal_places = minimum_places
            # Fast ingestion disables autoflush. Persist only this shared
            # projection so a later SQL-side match sees the strictest precision.
            _flush_shared_entity(session, geometry, label="Geometry", defer_if_fast=True)
    if context is not None and geometry.geometry_hash == record.geometry.geometry_hash:
        context.geometries_by_hash[geometry_key] = geometry
    if context is not None:
        _register_in_memory_geometry(
            context,
            geometry,
            topology_id=topology_id,
            canonicalization_version=record.geometry.canonicalization_version,
            charge=record.charge,
            multiplicity=record.multiplicity,
        )
    geometry_id = _require_id(geometry, label="Geometry")
    # A concrete Geometry may arrive after its logical reaction/template.  In
    # that direction, materialize any strict mapped-reaction instance before
    # the normal Geometry -> existing participant reconciliation pass.
    from tricycle_reaction_db.application.services.reaction_mapping_resolution import (
        ensure_mapped_reactions_for_concrete_topology,
    )

    if context is None:
        ensure_mapped_reactions_for_concrete_topology(
            session,
            topology,
            refresh_thermodynamics=True,
        )
        reconcile_geometry_with_reactions(session, geometry)
    else:
        context.topologies_to_resolve_reactions.add(topology_id)
        context.geometries_to_reconcile[geometry_id] = geometry
    return PersistedMolecularGeometry(
        formula=formula,
        topology=topology,
        topology_derivation=topology_derivation,
        geometry=geometry,
        geometry_assignment_kind=assignment_kind,
        observed_to_geometry_atom_indices=assignment_indices,
        observed_to_geometry_transform=assignment_transform,
        coordinate_rmsd_angstrom=assignment_rmsd,
        coordinate_max_abs_angstrom=assignment_max_abs,
    )


def preload_molecular_geometry_context(
    session: Session,
    records: Sequence[tuple[NormalizedMoleculeRecord, int | None]],
    *,
    context: GeometryPersistenceContext,
    topology_records: Sequence[NormalizedTopologyRecord] = (),
) -> None:
    """Resolve shared topology identities, then bulk-load exact Geometry rows.

    The frame loop still delegates tolerance matching to PostgreSQL. This
    preflight only removes the redundant exact-hash lookup for every frame;
    missing keys are recorded so a new Geometry can be added without another
    round trip.
    """

    frame_topology_records = [
        NormalizedTopologyRecord(
            formula=record.formula,
            topology=record.topology,
            topology_derivation=record.topology_derivation,
        )
        for record, _coordinate_decimal_places in records
    ]
    _preload_molecular_topologies(
        session,
        [*frame_topology_records, *topology_records],
        context=context,
    )

    keys: set[tuple[UUID, str, str, int, int]] = set()
    records_by_key: dict[
        tuple[UUID, str, str, int, int], tuple[NormalizedMoleculeRecord, int | None]
    ] = {}
    for record, coordinate_decimal_places in records:
        topology_record = NormalizedTopologyRecord(
            formula=record.formula,
            topology=record.topology,
            topology_derivation=record.topology_derivation,
        )
        persisted_topology = persist_molecular_topology(
            session,
            topology_record,
            context=context,
        )
        key = (
            _require_id(persisted_topology.topology, label="MolecularTopology"),
            record.geometry.canonicalization_version,
            record.geometry.geometry_hash,
            record.charge,
            record.multiplicity,
        )
        keys.add(key)
        records_by_key.setdefault(key, (record, coordinate_decimal_places))
    if not keys:
        return
    # Hold these locks before taking the database snapshot below. Otherwise
    # two concurrent upload transactions can both observe an empty candidate
    # set and create hash-distinct but tolerance-equivalent Geometry rows.
    _acquire_identity_locks(
        session,
        *sorted(
            {
                (
                    "geometry-equivalence",
                    topology_id,
                    canonicalization_version,
                    charge,
                    multiplicity,
                )
                for (
                    topology_id,
                    canonicalization_version,
                    _geometry_hash,
                    charge,
                    multiplicity,
                ) in keys
            },
            key=str,
        ),
    )
    geometry_columns = cast(Any, Geometry)
    exact_inputs = (
        text(
            """
            SELECT topology_id, canonicalization_version, geometry_hash, charge, multiplicity
            FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS input(
                topology_id uuid,
                canonicalization_version text,
                geometry_hash text,
                charge smallint,
                multiplicity smallint
            )
            """
        )
        .bindparams(
            payload=json.dumps(
                [
                    {
                        "topology_id": str(topology_id),
                        "canonicalization_version": canonicalization_version,
                        "geometry_hash": geometry_hash,
                        "charge": charge,
                        "multiplicity": multiplicity,
                    }
                    for (
                        topology_id,
                        canonicalization_version,
                        geometry_hash,
                        charge,
                        multiplicity,
                    ) in keys
                ]
            )
        )
        .columns(
            topology_id=PG_UUID(as_uuid=True),
            canonicalization_version=String,
            geometry_hash=String,
            charge=SmallInteger,
            multiplicity=SmallInteger,
        )
        .subquery("exact_geometry_inputs")
    )
    rows = session.exec(
        select(Geometry)
        .options(
            load_only(
                geometry_columns.id,
                geometry_columns.topology_id,
                geometry_columns.canonicalization_version,
                geometry_columns.geometry_hash,
                geometry_columns.minimum_coordinate_decimal_places,
            )
        )
        .join(
            exact_inputs,
            and_(
                geometry_columns.topology_id == exact_inputs.c.topology_id,
                geometry_columns.canonicalization_version
                == exact_inputs.c.canonicalization_version,
                geometry_columns.geometry_hash == exact_inputs.c.geometry_hash,
                geometry_columns.charge == exact_inputs.c.charge,
                geometry_columns.multiplicity == exact_inputs.c.multiplicity,
            ),
        )
    ).all()
    context.exact_geometry_keys_loaded.update(keys)
    for exact_geometry in rows:
        key = (
            exact_geometry.topology_id,
            exact_geometry.canonicalization_version,
            exact_geometry.geometry_hash,
            exact_geometry.charge,
            exact_geometry.multiplicity,
        )
        context.geometries_by_hash[key] = exact_geometry

    # For non-exact hashes, evaluate the existing PostgreSQL equivalence
    # function in bounded set-based queries. A file batch may contain many
    # thousands of frame keys; putting all of them in one statement makes the
    # nested candidate scans exceed the database statement timeout.
    unmatched = [key for key in keys if key not in context.geometries_by_hash]
    if not unmatched:
        return
    input_rows: list[dict[str, object]] = []
    key_by_input: dict[str, tuple[UUID, str, str, int, int]] = {}
    for input_index, key in enumerate(unmatched):
        record, coordinate_decimal_places = records_by_key[key]
        distances, angles, dihedrals = _internal_coordinate_projection(
            record.geometry.internal_coordinates
        )
        input_key = str(input_index)
        key_by_input[input_key] = key
        input_rows.append(
            {
                "input_key": input_key,
                "topology_id": str(key[0]),
                "canonicalization_version": key[1],
                "charge": key[3],
                "multiplicity": key[4],
                "distances": distances,
                "angles": angles,
                "dihedrals": dihedrals,
                "coordinate_decimal_places": coordinate_decimal_places,
            }
        )
    matched_ids: dict[str, list[UUID]] = {}
    statement = text(
        """
        WITH inputs AS (
            SELECT *
            FROM jsonb_to_recordset(CAST(:payload AS jsonb)) AS input(
                input_key text,
                topology_id uuid,
                canonicalization_version text,
                charge smallint,
                multiplicity smallint,
                distances double precision[],
                angles double precision[],
                dihedrals double precision[],
                coordinate_decimal_places smallint
            )
        )
        SELECT inputs.input_key, geometry.id
        FROM inputs
        JOIN geometry
         ON geometry.topology_id = inputs.topology_id
         AND geometry.canonicalization_version = inputs.canonicalization_version
         AND geometry.charge = inputs.charge
         AND geometry.multiplicity = inputs.multiplicity
         AND geometry_internal_coordinates_equivalent(
                geometry.internal_coordinate_distances_angstrom,
                geometry.internal_coordinate_angles_degrees,
                geometry.internal_coordinate_dihedrals_degrees,
                geometry.minimum_coordinate_decimal_places,
                inputs.distances,
                inputs.angles,
                inputs.dihedrals,
                inputs.coordinate_decimal_places
            )
        ORDER BY inputs.input_key, geometry.id
        """
    )
    connection = session.connection()
    for start in range(0, len(input_rows), GEOMETRY_MATCH_INPUT_BATCH_SIZE):
        chunk = input_rows[start : start + GEOMETRY_MATCH_INPUT_BATCH_SIZE]
        matches = connection.execute(
            statement,
            {"payload": json.dumps(chunk, separators=(",", ":"))},
        ).all()
        for input_key, geometry_id in matches:
            if isinstance(input_key, str) and isinstance(geometry_id, UUID):
                matched_ids.setdefault(input_key, []).append(geometry_id)
    all_matching_ids = {
        geometry_id for geometry_ids in matched_ids.values() for geometry_id in geometry_ids
    }
    context.equivalent_geometry_keys_loaded.update(unmatched)
    for input_key, key in key_by_input.items():
        context.equivalent_geometry_candidates[key] = tuple(matched_ids.get(input_key, ()))
    if all_matching_ids:
        matching_geometries = session.exec(
            select(Geometry).where(geometry_columns.id.in_(all_matching_ids))
        ).all()
        geometries_by_id = {
            matching_geometry.id: matching_geometry for matching_geometry in matching_geometries
        }
        for _input_key, key in key_by_input.items():
            geometry_ids = context.equivalent_geometry_candidates[key]
            candidate_rows = tuple(
                geometries_by_id[geometry_id]
                for geometry_id in geometry_ids
                if geometry_id in geometries_by_id
            )
            if len(candidate_rows) != len(geometry_ids):
                raise RuntimeError("database returned an incomplete Geometry candidate set")
            context.equivalent_geometry_rows_by_key[key] = candidate_rows
            if len(candidate_rows) == 1:
                context.equivalent_geometry_by_key[key] = candidate_rows[0]
            else:
                context.equivalent_geometry_by_key.pop(key, None)


__all__ = [
    "GEOMETRY_MATCH_POLICY_VERSION",
    "GeometryPersistenceContext",
    "GeometryAssignmentAmbiguityError",
    "PersistedMolecularGeometry",
    "PersistedMolecularTopology",
    "persist_molecular_geometry",
    "persist_molecular_topology",
    "preload_molecular_geometry_context",
]
