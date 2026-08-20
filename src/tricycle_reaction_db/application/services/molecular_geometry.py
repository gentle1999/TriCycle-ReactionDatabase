"""Idempotent persistence for Formula -> Topology -> Geometry."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

import numpy as np
import numpy.typing as npt
from sqlalchemy import Float, SmallInteger, func, literal
from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos.chemistry import (
    NormalizedMoleculeRecord,
    NormalizedTopologyRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _assert_record_matches,
    _flush_shared_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    reconcile_geometry_with_reactions,
)
from tricycle_reaction_db.db.models import (
    Geometry,
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
    geometries_by_hash: dict[tuple[UUID, str, str], Geometry] = field(default_factory=dict)
    geometries_to_reconcile: dict[UUID, Geometry] = field(default_factory=dict)


GEOMETRY_MATCH_POLICY_VERSION = "geometry-internal-coordinate-match-v3"


class GeometryAssignmentAmbiguityError(ValueError):
    """A coordinate observation matched more than one persisted Geometry."""

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


def _find_database_geometry_match(
    session: Session,
    *,
    topology: MolecularTopology,
    record: NormalizedMoleculeRecord,
    coordinate_decimal_places: int | None,
) -> tuple[Geometry, list[int], float, float, tuple[float, ...]] | None:
    """Let PostgreSQL find the unique coordinate-equivalent Geometry."""

    observed = record.geometry
    observed_topology_coords = _topology_order_coordinates(
        record.observed_coordinates,
        record.observed_to_geometry_atom_indices,
    )
    distances, angles, dihedrals = _internal_coordinate_projection(observed.internal_coordinates)
    matching_ids = list(
        session.exec(
            select(Geometry.id).where(
                Geometry.topology_id == topology.id,
                Geometry.canonicalization_version == observed.canonicalization_version,
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
    if len(matching_ids) > 1:
        raise GeometryAssignmentAmbiguityError(
            topology_id=topology.id,
            observed_geometry_hash=record.geometry.geometry_hash,
            candidate_ids=matching_ids,
        )
    if not matching_ids:
        return None
    candidate = session.get(Geometry, matching_ids[0])
    if candidate is None:
        raise RuntimeError("database returned a missing Geometry match")
    candidate_coordinates = np.asarray(
        candidate.mol.GetConformer().GetPositions(),
        dtype=np.float64,
    )
    rmsd, max_abs, transform = _coordinate_alignment(
        observed_topology_coords,
        candidate_coordinates,
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
        or topology.canonical_isomeric_smiles != record.topology.canonical_isomeric_smiles
        or topology.atom_count != record.topology.atom_count
        or topology.heavy_atom_count != record.topology.heavy_atom_count
        or topology.formal_charge != record.topology.formal_charge
        or topology.radical_electron_count != record.topology.radical_electron_count
        or topology.fragment_count != record.topology.fragment_count
        or topology.stereo_status != record.topology.stereo_status
        or topology.sanitization_status != record.topology.sanitization_status
        or topology.sanitization_error != record.topology.sanitization_error
    ):
        raise ValueError("cached topology identity resolved to different graph projections")
    _assert_record_matches(
        persisted.topology_derivation,
        record.topology_derivation,
        label="MolecularTopologyDerivation",
    )


def persist_molecular_topology(
    session: Session,
    record: NormalizedTopologyRecord,
    *,
    context: GeometryPersistenceContext | None = None,
) -> PersistedMolecularTopology:
    """Insert or reuse Formula and Topology without requiring a Geometry."""

    context_key = _topology_context_key(record)
    if context is not None and (cached := context.topologies.get(context_key)) is not None:
        _validate_cached_topology(cached, record)
        return cached

    _acquire_identity_locks(
        session,
        ("molecular_formula", record.formula.composition_hash),
        (
            "molecular_topology",
            record.topology.identity_schema_version,
            record.topology.graph_hash,
        ),
    )
    formula = session.exec(
        select(MolecularFormula).where(
            MolecularFormula.composition_hash == record.formula.composition_hash
        )
    ).first()
    if formula is None:
        formula = MolecularFormula(**record.formula.model_dump())
        _flush_shared_entity(session, formula, label="MolecularFormula")
    if formula.id is None:
        raise RuntimeError("database did not generate MolecularFormula.id")

    topology = session.exec(
        select(MolecularTopology).where(
            MolecularTopology.identity_schema_version == record.topology.identity_schema_version,
            MolecularTopology.graph_hash == record.topology.graph_hash,
        )
    ).first()
    if topology is None:
        topology = MolecularTopology(
            formula=formula,
            **record.topology.model_dump(),
        )
        _flush_shared_entity(session, topology, label="MolecularTopology")
    elif topology.formula_id != formula.id:
        raise ValueError("topology identity resolved to a different molecular formula")
    elif (
        topology.canonical_isomeric_smiles != record.topology.canonical_isomeric_smiles
        or topology.atom_count != record.topology.atom_count
        or topology.heavy_atom_count != record.topology.heavy_atom_count
        or topology.formal_charge != record.topology.formal_charge
        or topology.radical_electron_count != record.topology.radical_electron_count
        or topology.fragment_count != record.topology.fragment_count
        or topology.stereo_status != record.topology.stereo_status
        or topology.sanitization_status != record.topology.sanitization_status
        or topology.sanitization_error != record.topology.sanitization_error
    ):
        raise ValueError("topology identity resolved to different graph projections")
    if topology.id is None:
        raise RuntimeError("database did not generate MolecularTopology.id")
    topology_id = _require_id(topology, label="MolecularTopology")

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
        topology_derivation = MolecularTopologyDerivation(
            topology=topology,
            **record.topology_derivation.model_dump(),
        )
        _flush_shared_entity(
            session,
            topology_derivation,
            label="MolecularTopologyDerivation",
        )
    else:
        _assert_record_matches(
            topology_derivation,
            record.topology_derivation,
            label="MolecularTopologyDerivation",
        )
    persisted = PersistedMolecularTopology(
        formula=formula,
        topology=topology,
        topology_derivation=topology_derivation,
    )
    if context is not None:
        context.topologies[context_key] = persisted
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
    )
    geometry = context.geometries_by_hash.get(geometry_key) if context is not None else None
    if geometry is None:
        geometry = session.exec(
            select(Geometry).where(
                Geometry.topology_id == topology.id,
                Geometry.canonicalization_version == record.geometry.canonicalization_version,
                Geometry.geometry_hash == record.geometry.geometry_hash,
            )
        ).first()
        if geometry is not None and context is not None:
            context.geometries_by_hash[geometry_key] = geometry
    assignment_kind = GeometryAssignmentKind.PARSED_EXACT
    assignment_indices: list[int] | None = list(record.observed_to_geometry_atom_indices)
    assignment_transform = tuple(record.observed_to_geometry_transform)
    assignment_rmsd = record.geometry_assignment_rmsd_angstrom
    assignment_max_abs = record.geometry_assignment_max_abs_angstrom
    if geometry is None:
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
    if geometry is None:
        distances, angles, dihedrals = _internal_coordinate_projection(
            record.geometry.internal_coordinates
        )
        geometry = Geometry(
            topology=topology,
            **record.geometry.model_dump(),
            internal_coordinate_distances_angstrom=distances,
            internal_coordinate_angles_degrees=angles,
            internal_coordinate_dihedrals_degrees=dihedrals,
            minimum_coordinate_decimal_places=coordinate_decimal_places,
        )
        _flush_shared_entity(session, geometry, label="Geometry")
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
            _flush_shared_entity(session, geometry, label="Geometry")
    if context is not None and geometry.geometry_hash == record.geometry.geometry_hash:
        context.geometries_by_hash[geometry_key] = geometry
    geometry_id = _require_id(geometry, label="Geometry")
    if context is None:
        reconcile_geometry_with_reactions(session, geometry)
    else:
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


__all__ = [
    "GEOMETRY_MATCH_POLICY_VERSION",
    "GeometryPersistenceContext",
    "GeometryAssignmentAmbiguityError",
    "PersistedMolecularGeometry",
    "PersistedMolecularTopology",
    "persist_molecular_geometry",
    "persist_molecular_topology",
]
