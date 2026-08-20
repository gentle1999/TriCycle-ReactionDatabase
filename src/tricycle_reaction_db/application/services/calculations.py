"""Relationship-driven, idempotent persistence for calculation facts."""

from typing import Any, cast
from uuid import UUID

import numpy as np
import numpy.typing as npt
from sqlalchemy.orm import undefer
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.calculations import (
    AtomicPopulationSeriesRecord,
    BondOrderResultRecord,
    CalculationFrameRecord,
    CalculationSegmentRecord,
    CalculationStatusResultRecord,
    ChargeSpinPopulationResultRecord,
    ElectronicConfigurationRecord,
    ElectronicStateRecord,
    ElectronicStateSetRecord,
    EnergyObservationRecord,
    FrameEnergyResultRecord,
    GeometryOptimizationResultRecord,
    ImplicitSolvationResultRecord,
    MolecularOrbitalResultRecord,
    MultireferenceResultRecord,
    NMRResultRecord,
    NMRShieldingTensorRecord,
    ParseRevisionCompletionRecord,
    ParseRevisionRecord,
    PolarizabilityResultRecord,
    ScientificArrayAssignmentRecord,
    ScientificArrayRecord,
    SinglePointPropertyResultRecord,
    ThermochemistryResultRecord,
    TotalSpinResultRecord,
    VibrationResultRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _assert_record_matches,
    _fast_insert_enabled,
    _flush_new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    reconcile_geometry_with_reactions,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    AtomicPopulationSeries,
    BondOrderResult,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    CalculationStatusResult,
    ChargeSpinPopulationResult,
    ElectronicConfiguration,
    ElectronicState,
    ElectronicStateSet,
    EnergyObservation,
    FrameEnergyResult,
    Geometry,
    GeometryOptimizationResult,
    ImplicitSolvationResult,
    MolecularOrbitalResult,
    MolecularTopologyDerivation,
    MultireferenceResult,
    NMRResult,
    NMRShieldingTensor,
    ParseRevision,
    PolarizabilityResult,
    ScientificArray,
    ScientificArrayAssignment,
    SinglePointPropertyResult,
    ThermochemistryResult,
    TotalSpinResult,
    VibrationResult,
)
from tricycle_reaction_db.db.types import summarize_numpy_array
from tricycle_reaction_db.domain.enums import (
    ParseStatus,
    QMSoftware,
    ScientificArrayKind,
    ScientificArrayOwnerKind,
    SourceFormat,
)

_PARSE_REVISION_WORKFLOW_FIELDS = {
    "started_at",
    "completed_at",
    "error_code",
    "error_message",
    "error_metadata",
    "record_sha256",
    "status",
}


def _validate_segment_source_bounds(
    parse_revision: ParseRevision,
    record: CalculationSegmentRecord,
) -> None:
    source_size_bytes = parse_revision.source_size_bytes
    if source_size_bytes is None:
        source_size_bytes = parse_revision.artifact_file.size_bytes
    if record.source_end_byte > source_size_bytes:
        raise ValueError("CalculationSegment source span exceeds its parsed source byte size")


def _validate_segment_software(
    parse_revision: ParseRevision,
    protocol: CalculationProtocol,
) -> None:
    expected_software = {
        SourceFormat.GAUSSIAN_LOG: QMSoftware.GAUSSIAN,
        SourceFormat.ORCA_OUTPUT: QMSoftware.ORCA,
    }.get(parse_revision.source_format)
    if expected_software is not None and protocol.qm_software is not expected_software:
        raise ValueError("CalculationProtocol software does not match ParseRevision source format")


def _validate_frame_source_bounds(
    segment: CalculationSegment,
    record: CalculationFrameRecord,
) -> None:
    if not (
        segment.source_start_byte <= record.source_start_byte
        and record.source_end_byte <= segment.source_end_byte
    ):
        raise ValueError("CalculationFrame byte span must be contained by its segment")
    segment_has_char_span = segment.source_start_char is not None
    frame_has_char_span = record.source_start_char is not None
    if segment_has_char_span != frame_has_char_span:
        raise ValueError("CalculationFrame and segment must use the same character-span policy")
    if (
        segment.source_start_char is not None
        and segment.source_end_char is not None
        and record.source_start_char is not None
        and record.source_end_char is not None
        and not (
            segment.source_start_char <= record.source_start_char
            and record.source_end_char <= segment.source_end_char
        )
    ):
        raise ValueError("CalculationFrame character span must be contained by its segment")
    if not (
        segment.source_start_line <= record.source_start_line
        and record.source_end_line <= segment.source_end_line
    ):
        raise ValueError("CalculationFrame line span must be contained by its segment")


def _validate_scientific_array_shape(
    frame: CalculationFrame,
    record: ScientificArrayRecord,
) -> None:
    atom_count = frame.geometry.atom_count
    shape = tuple(record.shape)
    expected_shape: tuple[int, ...]
    if record.kind is ScientificArrayKind.FORCES:
        expected_shape = (atom_count, 3)
    elif record.kind is ScientificArrayKind.HESSIAN:
        expected_shape = (3 * atom_count, 3 * atom_count)
    elif record.kind is ScientificArrayKind.NORMAL_MODES:
        if frame.frequency_count is None:
            raise ValueError("normal modes require a CalculationFrame frequency summary")
        expected_shape = (frame.frequency_count, atom_count, 3)
    elif record.kind is ScientificArrayKind.VIBRATIONAL_TEMPERATURES:
        if frame.frequency_count is None or frame.negative_frequency_count is None:
            raise ValueError(
                "vibrational temperatures require a complete CalculationFrame frequency summary"
            )
        expected_shape = (frame.frequency_count - frame.negative_frequency_count,)
        mode_indices = (record.array_metadata or {}).get("frequency_mode_indices")
        if (
            not isinstance(mode_indices, list)
            or any(type(index) is not int for index in mode_indices)
            or mode_indices != sorted(set(mode_indices))
            or len(mode_indices) != shape[0]
            or any(index < 0 or index >= frame.frequency_count for index in mode_indices)
        ):
            raise ValueError(
                "vibrational temperatures require ordered, unique frequency_mode_indices"
            )
    elif record.kind in {
        ScientificArrayKind.VIBRATIONAL_FREQUENCIES,
        ScientificArrayKind.REDUCED_MASSES,
        ScientificArrayKind.VIBRATIONAL_FORCE_CONSTANTS,
        ScientificArrayKind.IR_INTENSITIES,
    }:
        if frame.frequency_count is None:
            raise ValueError(f"{record.kind.value} requires a CalculationFrame frequency summary")
        expected_shape = (frame.frequency_count,)
    elif record.kind in {
        ScientificArrayKind.ATOMIC_POPULATION,
        ScientificArrayKind.FUKUI_POSITIVE,
        ScientificArrayKind.FUKUI_NEGATIVE,
        ScientificArrayKind.FUKUI_ZERO,
        ScientificArrayKind.FRACTIONAL_OCCUPATION_DENSITY,
    }:
        expected_shape = (atom_count,)
    elif record.kind is ScientificArrayKind.BOND_ORDER_MATRIX:
        expected_shape = (atom_count, atom_count)
    elif record.kind is ScientificArrayKind.NMR_SHIELDING_TENSOR:
        expected_shape = (3, 3)
    elif record.kind in {
        ScientificArrayKind.NMR_PRINCIPAL_VALUES,
        ScientificArrayKind.ELECTRIC_DIPOLE_MOMENT,
        ScientificArrayKind.DIPOLE,
        ScientificArrayKind.TRANSITION_DIPOLE,
    }:
        expected_shape = (3,)
    elif record.kind in {
        ScientificArrayKind.QUADRUPOLE,
        ScientificArrayKind.TRACELESS_QUADRUPOLE,
    }:
        expected_shape = (6,)
    elif record.kind is ScientificArrayKind.OCTAPOLE:
        expected_shape = (10,)
    elif record.kind is ScientificArrayKind.HEXADECAPOLE:
        expected_shape = (15,)
    elif record.kind in {
        ScientificArrayKind.NMR_COUPLING_K,
        ScientificArrayKind.NMR_COUPLING_J,
        ScientificArrayKind.NMR_COUPLING_K_COMPONENT,
        ScientificArrayKind.NMR_COUPLING_J_COMPONENT,
    }:
        if len(shape) != 2 or shape[0] != shape[1] or shape[0] > atom_count:
            raise ValueError(f"{record.kind.value} must be a square matrix within atom count")
        return
    elif record.kind is ScientificArrayKind.POLARIZABILITY_TENSOR:
        if shape not in {(6,), (3, 3)}:
            raise ValueError("polarizability_tensor shape must be (6,) or (3, 3)")
        return
    elif record.kind is ScientificArrayKind.ORBITAL_COEFFICIENT:
        if not shape or any(dimension <= 0 for dimension in shape):
            raise ValueError("orbital coefficients must be a non-empty array")
        return
    else:
        if len(shape) != 1 or shape[0] <= 0:
            raise ValueError(f"{record.kind.value} must be a non-empty one-dimensional array")
        return
    if shape != expected_shape:
        raise ValueError(
            f"{record.kind.value} shape must be {expected_shape!r} for this frame, got {shape!r}"
        )


def persist_parse_revision(
    session: Session,
    artifact_file: ArtifactFile,
    record: ParseRevisionRecord,
    *,
    force_new_revision: bool = False,
) -> ParseRevision:
    """Insert or reuse one parse revision under its immutable artifact."""

    artifact_file_id = _require_id(artifact_file, label="ArtifactFile")
    _acquire_identity_locks(
        session,
        ("parse_revision_artifact", artifact_file_id),
        (
            "parse_revision",
            artifact_file_id,
            record.export_schema_version,
            record.parser_provenance_hash,
            record.parser_config_hash,
            record.reconstruction_config_hash,
        ),
    )
    revisions = session.exec(
        select(ParseRevision)
        .where(
            ParseRevision.artifact_file_id == artifact_file_id,
            ParseRevision.export_schema_version == record.export_schema_version,
            ParseRevision.parser_provenance_hash == record.parser_provenance_hash,
            ParseRevision.parser_config_hash == record.parser_config_hash,
            ParseRevision.reconstruction_config_hash == record.reconstruction_config_hash,
        )
        .order_by(col(ParseRevision.revision_number).desc())
    ).all()
    revision = revisions[0] if revisions else None
    if revision is not None and not force_new_revision:
        source_identity_fields = (
            "source_content_sha256",
            "source_size_bytes",
            "source_compression",
        )
        source_identity_changed = False
        for field_name in source_identity_fields:
            actual = getattr(revision, field_name)
            expected = getattr(record, field_name)
            if actual is None and expected is not None:
                setattr(revision, field_name, expected)
                source_identity_changed = True
        if source_identity_changed:
            session.add(revision)
            session.flush()
        if record.status is ParseStatus.PENDING:
            _assert_record_matches(
                revision,
                record,
                label="ParseRevision",
                exclude=_PARSE_REVISION_WORKFLOW_FIELDS,
            )
        else:
            _assert_record_matches(revision, record, label="ParseRevision")
        return revision

    if record.status is ParseStatus.SUCCEEDED:
        raise ValueError("create a pending ParseRevision and finalize it after persisting segments")

    latest_revision = session.exec(
        select(ParseRevision)
        .where(ParseRevision.artifact_file_id == artifact_file_id)
        .order_by(col(ParseRevision.revision_number).desc())
    ).first()

    revision = ParseRevision(
        artifact_file=artifact_file,
        revision_number=(latest_revision.revision_number + 1 if latest_revision is not None else 1),
        reparse_of_id=(
            _require_id(latest_revision, label="ParseRevision")
            if force_new_revision and latest_revision is not None
            else None
        ),
        **record.model_dump(),
    )
    _flush_new_entity(session, revision, label="ParseRevision")
    return revision


def persist_calculation_segment(
    session: Session,
    parse_revision: ParseRevision,
    protocol: CalculationProtocol | None,
    record: CalculationSegmentRecord,
) -> CalculationSegment:
    """Insert or reuse one ordered segment through its parent relationships."""

    parse_revision_id = _require_id(parse_revision, label="ParseRevision")
    protocol_id = (
        _require_id(protocol, label="CalculationProtocol") if protocol is not None else None
    )
    _validate_segment_source_bounds(parse_revision, record)
    if protocol is not None:
        _validate_segment_software(parse_revision, protocol)
    _acquire_identity_locks(
        session,
        ("calculation_segment", parse_revision_id, record.segment_index),
    )
    segment = session.exec(
        select(CalculationSegment).where(
            CalculationSegment.parse_revision_id == parse_revision_id,
            CalculationSegment.segment_index == record.segment_index,
        )
    ).first()
    if segment is not None:
        if segment.protocol_id != protocol_id:
            raise ValueError("CalculationSegment identity resolved to a different protocol")
        _assert_record_matches(segment, record, label="CalculationSegment")
        return segment

    segment = CalculationSegment(
        parse_revision=parse_revision,
        protocol=protocol,
        **record.model_dump(),
    )
    _flush_new_entity(session, segment, label="CalculationSegment")
    return segment


def persist_calculation_frame(
    session: Session,
    segment: CalculationSegment,
    geometry: Geometry,
    topology_derivation: MolecularTopologyDerivation,
    record: CalculationFrameRecord,
    *,
    reconcile: bool = True,
) -> CalculationFrame:
    """Insert or reuse one frame while preserving both source-order identities."""

    segment_id = _require_id(segment, label="CalculationSegment")
    geometry_id = _require_id(geometry, label="Geometry")
    topology_derivation_id = _require_id(
        topology_derivation,
        label="MolecularTopologyDerivation",
    )
    parse_revision_id = segment.parse_revision_id
    if not isinstance(parse_revision_id, UUID):
        raise RuntimeError("CalculationSegment must have a parse revision before adding frames")
    _validate_frame_source_bounds(segment, record)
    if topology_derivation.topology_id != geometry.topology_id:
        raise ValueError("Topology derivation does not belong to the Geometry topology")
    _acquire_identity_locks(
        session,
        ("calculation_frame_segment", segment_id, record.frame_index),
        ("calculation_frame_file", parse_revision_id, record.file_frame_index),
    )

    observed_indices = record.observed_to_geometry_atom_indices
    if len(observed_indices) != geometry.atom_count or sorted(observed_indices) != list(
        range(geometry.atom_count)
    ):
        raise ValueError("observed-to-geometry atom indices must be a full Geometry permutation")
    if record.observed_coordinates.shape != (geometry.atom_count, 3):
        raise ValueError("observed coordinates must match the Geometry atom count")

    if _fast_insert_enabled(session):
        new_frame = CalculationFrame(
            segment=segment,
            geometry=geometry,
            topology_derivation=topology_derivation,
            **record.model_dump(),
        )
        _flush_new_entity(session, new_frame, label="CalculationFrame")
        return new_frame

    frame = session.exec(
        select(CalculationFrame)
        .where(
            CalculationFrame.segment_id == segment_id,
            CalculationFrame.frame_index == record.frame_index,
        )
        .options(undefer(cast(Any, CalculationFrame.observed_coordinates)))
    ).first()
    file_frame = session.exec(
        select(CalculationFrame)
        .where(
            CalculationFrame.parse_revision_id == parse_revision_id,
            CalculationFrame.file_frame_index == record.file_frame_index,
        )
        .options(undefer(cast(Any, CalculationFrame.observed_coordinates)))
    ).first()

    if frame is not None:
        if file_frame is not None and file_frame.id != frame.id:
            raise ValueError("frame identities resolve to different CalculationFrame rows")
        if frame.geometry_id != geometry_id:
            raise ValueError("CalculationFrame identity resolved to a different geometry")
        if frame.topology_derivation_id != topology_derivation_id:
            raise ValueError(
                "CalculationFrame identity resolved to a different topology derivation"
            )
        _assert_record_matches(frame, record, label="CalculationFrame")
        if reconcile:
            reconcile_geometry_with_reactions(session, geometry)
        return frame
    if file_frame is not None:
        raise ValueError("file_frame_index is already assigned to a different segment frame")

    frame = CalculationFrame(
        segment=segment,
        geometry=geometry,
        topology_derivation=topology_derivation,
        **record.model_dump(),
    )
    _flush_new_entity(session, frame, label="CalculationFrame")
    if reconcile:
        reconcile_geometry_with_reactions(session, geometry)
    return frame


def persist_frame_energy_result(
    session: Session,
    frame: CalculationFrame,
    record: FrameEnergyResultRecord,
) -> FrameEnergyResult:
    """Insert or reuse the one MolOP ``Energies`` result for a frame."""

    frame_id = _require_id(frame, label="CalculationFrame")
    if _fast_insert_enabled(session):
        new_result = FrameEnergyResult(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label="FrameEnergyResult")
        return new_result
    _acquire_identity_locks(session, ("frame_energy_result", frame_id))
    result = session.exec(
        select(FrameEnergyResult).where(FrameEnergyResult.frame_id == frame_id)
    ).first()
    if result is not None:
        _assert_record_matches(result, record, label="FrameEnergyResult")
        return result
    result = FrameEnergyResult(frame=frame, **record.model_dump())
    _flush_new_entity(session, result, label="FrameEnergyResult")
    return result


def persist_energy_observation(
    session: Session,
    energy_result: FrameEnergyResult,
    record: EnergyObservationRecord,
) -> EnergyObservation:
    """Insert or reuse one ordered source energy observation."""

    energy_result_id = _require_id(energy_result, label="FrameEnergyResult")
    if _fast_insert_enabled(session):
        new_observation = EnergyObservation(energy_result=energy_result, **record.model_dump())
        _flush_new_entity(session, new_observation, label="EnergyObservation")
        return new_observation
    _acquire_identity_locks(
        session,
        ("energy_observation", energy_result_id, record.observation_index),
    )
    observation = session.exec(
        select(EnergyObservation).where(
            EnergyObservation.energy_result_id == energy_result_id,
            EnergyObservation.observation_index == record.observation_index,
        )
    ).first()
    if observation is not None:
        _assert_record_matches(observation, record, label="EnergyObservation")
        return observation
    observation = EnergyObservation(energy_result=energy_result, **record.model_dump())
    _flush_new_entity(session, observation, label="EnergyObservation")
    return observation


def persist_geometry_optimization_result(
    session: Session,
    frame: CalculationFrame,
    record: GeometryOptimizationResultRecord,
) -> GeometryOptimizationResult:
    """Insert or reuse one MolOP optimization result for a frame."""

    frame_id = _require_id(frame, label="CalculationFrame")
    if _fast_insert_enabled(session):
        new_result = GeometryOptimizationResult(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label="GeometryOptimizationResult")
        return new_result
    _acquire_identity_locks(session, ("geometry_optimization_result", frame_id))
    result = session.exec(
        select(GeometryOptimizationResult).where(GeometryOptimizationResult.frame_id == frame_id)
    ).first()
    if result is not None:
        _assert_record_matches(result, record, label="GeometryOptimizationResult")
        return result
    result = GeometryOptimizationResult(frame=frame, **record.model_dump())
    _flush_new_entity(session, result, label="GeometryOptimizationResult")
    return result


def persist_vibration_result(
    session: Session,
    frame: CalculationFrame,
    record: VibrationResultRecord,
) -> VibrationResult:
    """Insert or reuse one MolOP vibration result for a frame."""

    frame_id = _require_id(frame, label="CalculationFrame")
    if _fast_insert_enabled(session):
        new_result = VibrationResult(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label="VibrationResult")
        return new_result
    _acquire_identity_locks(session, ("vibration_result", frame_id))
    result = session.exec(
        select(VibrationResult).where(VibrationResult.frame_id == frame_id)
    ).first()
    if result is not None:
        _assert_record_matches(result, record, label="VibrationResult")
        return result
    result = VibrationResult(frame=frame, **record.model_dump())
    _flush_new_entity(session, result, label="VibrationResult")
    return result


def persist_calculation_status_result(
    session: Session,
    frame: CalculationFrame,
    record: CalculationStatusResultRecord,
) -> CalculationStatusResult:
    """Insert or reuse one direct MolOP frame status result."""

    frame_id = _require_id(frame, label="CalculationFrame")
    if _fast_insert_enabled(session):
        new_result = CalculationStatusResult(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label="CalculationStatusResult")
        return new_result
    _acquire_identity_locks(session, ("calculation_status_result", frame_id))
    result = session.exec(
        select(CalculationStatusResult).where(CalculationStatusResult.frame_id == frame_id)
    ).first()
    if result is not None:
        _assert_record_matches(result, record, label="CalculationStatusResult")
        return result
    result = CalculationStatusResult(frame=frame, **record.model_dump())
    _flush_new_entity(session, result, label="CalculationStatusResult")
    return result


def _validated_array_copy(
    record: ScientificArrayRecord,
) -> npt.NDArray[np.generic]:
    data: npt.NDArray[np.generic] = np.array(
        record.data,
        copy=True,
        order="A",
        subok=False,
    )
    summary = summarize_numpy_array(data)
    expected_summary = (
        record.dtype,
        tuple(record.shape),
        record.array_nbytes,
        record.payload_sha256,
    )
    actual_summary = (summary.dtype, summary.shape, summary.nbytes, summary.sha256)
    if actual_summary != expected_summary:
        raise ValueError(
            "ScientificArray summary does not match its exact NPY payload: "
            f"{actual_summary!r} != {expected_summary!r}"
        )
    data.setflags(write=False)
    return data


def persist_scientific_array(
    session: Session,
    frame: CalculationFrame,
    record: ScientificArrayRecord,
) -> ScientificArray:
    """Insert or reuse a typed array after validating and freezing an owned copy."""

    frame_id = _require_id(frame, label="CalculationFrame")
    _validate_scientific_array_shape(frame, record)
    if _fast_insert_enabled(session):
        data = _validated_array_copy(record)
        new_array = ScientificArray(
            frame=frame,
            data=data,
            **record.model_dump(exclude={"data"}),
        )
        _flush_new_entity(session, new_array, label="ScientificArray")
        return new_array
    _acquire_identity_locks(
        session,
        ("scientific_array", frame_id, record.kind.value, record.ordinal),
    )
    data = _validated_array_copy(record)
    scientific_array = session.exec(
        select(ScientificArray).where(
            ScientificArray.frame_id == frame_id,
            ScientificArray.kind == record.kind,
            ScientificArray.ordinal == record.ordinal,
        )
    ).first()
    if scientific_array is not None:
        _assert_record_matches(
            scientific_array,
            record,
            label="ScientificArray",
            exclude={"data"},
        )
        return scientific_array

    scientific_array = ScientificArray(
        frame=frame,
        data=data,
        **record.model_dump(exclude={"data"}),
    )
    _flush_new_entity(session, scientific_array, label="ScientificArray")
    return scientific_array


def persist_thermochemistry_result(
    session: Session,
    frame: CalculationFrame,
    record: ThermochemistryResultRecord,
    *,
    reconcile: bool = True,
) -> ThermochemistryResult:
    """Insert or reuse the optional one-to-one thermochemistry result for a frame."""

    frame_id = _require_id(frame, label="CalculationFrame")
    if frame.frequency_count is None or frame.frequency_count <= 0:
        raise ValueError("ThermochemistryResult requires a frame with frequency evidence")
    segment = frame.segment
    # Some Gaussian checkpoint follow-up segments expose frequency-derived
    # thermochemistry while MolOP marks the route's freq request disabled.
    # The frame's parsed frequency evidence is the authoritative admission
    # signal; retain the protocol as supplied for provenance.
    if record.source_schema_version != segment.parse_revision.export_schema_version:
        raise ValueError("thermochemistry source schema must match its ParseRevision export schema")
    if _fast_insert_enabled(session):
        new_result = ThermochemistryResult(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label="ThermochemistryResult")
        return new_result
    _acquire_identity_locks(session, ("thermochemistry_result", frame_id))
    result = session.exec(
        select(ThermochemistryResult).where(ThermochemistryResult.frame_id == frame_id)
    ).first()
    if result is not None:
        _assert_record_matches(result, record, label="ThermochemistryResult")
        return result

    result = ThermochemistryResult(
        frame=frame,
        **record.model_dump(),
    )
    _flush_new_entity(session, result, label="ThermochemistryResult")
    if reconcile:
        reconcile_geometry_with_reactions(session, frame.geometry)
    return result


def _persist_frame_result[FrameResultT](
    session: Session,
    frame: CalculationFrame,
    record: Any,
    model: type[FrameResultT],
    *,
    label: str,
) -> FrameResultT:
    frame_id = _require_id(frame, label="CalculationFrame")
    model_with_columns = cast(Any, model)
    if _fast_insert_enabled(session):
        new_result = model_with_columns(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_result, label=label)
        return cast(FrameResultT, new_result)
    _acquire_identity_locks(session, (label, frame_id))
    result = session.exec(select(model).where(model_with_columns.frame_id == frame_id)).first()
    if result is not None:
        _assert_record_matches(result, record, label=label)
        return result
    result = model_with_columns(frame=frame, **record.model_dump())
    _flush_new_entity(session, result, label=label)
    return cast(FrameResultT, result)


def persist_molecular_orbital_result(
    session: Session, frame: CalculationFrame, record: MolecularOrbitalResultRecord
) -> MolecularOrbitalResult:
    return _persist_frame_result(
        session, frame, record, MolecularOrbitalResult, label="MolecularOrbitalResult"
    )


def persist_charge_spin_population_result(
    session: Session, frame: CalculationFrame, record: ChargeSpinPopulationResultRecord
) -> ChargeSpinPopulationResult:
    return _persist_frame_result(
        session,
        frame,
        record,
        ChargeSpinPopulationResult,
        label="ChargeSpinPopulationResult",
    )


def persist_atomic_population_series(
    session: Session,
    result: ChargeSpinPopulationResult,
    record: AtomicPopulationSeriesRecord,
) -> AtomicPopulationSeries:
    result_id = _require_id(result, label="ChargeSpinPopulationResult")
    if record.value_count != result.frame.geometry.atom_count:
        raise ValueError("atomic population length must match the source Geometry atom count")
    if _fast_insert_enabled(session):
        new_series = AtomicPopulationSeries(result=result, **record.model_dump())
        _flush_new_entity(session, new_series, label="AtomicPopulationSeries")
        return new_series
    _acquire_identity_locks(session, ("AtomicPopulationSeries", result_id, record.series_key))
    series = session.exec(
        select(AtomicPopulationSeries).where(
            AtomicPopulationSeries.result_id == result_id,
            AtomicPopulationSeries.series_key == record.series_key,
        )
    ).first()
    if series is not None:
        _assert_record_matches(series, record, label="AtomicPopulationSeries")
        return series
    series = AtomicPopulationSeries(result=result, **record.model_dump())
    _flush_new_entity(session, series, label="AtomicPopulationSeries")
    return series


def persist_polarizability_result(
    session: Session, frame: CalculationFrame, record: PolarizabilityResultRecord
) -> PolarizabilityResult:
    return _persist_frame_result(
        session, frame, record, PolarizabilityResult, label="PolarizabilityResult"
    )


def persist_nmr_result(
    session: Session, frame: CalculationFrame, record: NMRResultRecord
) -> NMRResult:
    if any(index >= frame.geometry.atom_count for index in record.coupling_atom_indices):
        raise ValueError("NMR coupling atom index exceeds the source Geometry atom count")
    return _persist_frame_result(session, frame, record, NMRResult, label="NMRResult")


def persist_nmr_shielding_tensor(
    session: Session,
    result: NMRResult,
    record: NMRShieldingTensorRecord,
) -> NMRShieldingTensor:
    result_id = _require_id(result, label="NMRResult")
    if record.atom_index >= result.frame.geometry.atom_count:
        raise ValueError("NMR shielding atom index exceeds the source Geometry atom count")
    if _fast_insert_enabled(session):
        new_tensor = NMRShieldingTensor(result=result, **record.model_dump())
        _flush_new_entity(session, new_tensor, label="NMRShieldingTensor")
        return new_tensor
    _acquire_identity_locks(session, ("NMRShieldingTensor", result_id, record.atom_index))
    tensor = session.exec(
        select(NMRShieldingTensor).where(
            NMRShieldingTensor.result_id == result_id,
            NMRShieldingTensor.atom_index == record.atom_index,
        )
    ).first()
    if tensor is not None:
        _assert_record_matches(tensor, record, label="NMRShieldingTensor")
        return tensor
    tensor = NMRShieldingTensor(result=result, **record.model_dump())
    _flush_new_entity(session, tensor, label="NMRShieldingTensor")
    return tensor


def persist_bond_order_result(
    session: Session, frame: CalculationFrame, record: BondOrderResultRecord
) -> BondOrderResult:
    return _persist_frame_result(session, frame, record, BondOrderResult, label="BondOrderResult")


def persist_total_spin_result(
    session: Session, frame: CalculationFrame, record: TotalSpinResultRecord
) -> TotalSpinResult:
    return _persist_frame_result(session, frame, record, TotalSpinResult, label="TotalSpinResult")


def persist_single_point_property_result(
    session: Session, frame: CalculationFrame, record: SinglePointPropertyResultRecord
) -> SinglePointPropertyResult:
    return _persist_frame_result(
        session,
        frame,
        record,
        SinglePointPropertyResult,
        label="SinglePointPropertyResult",
    )


def persist_electronic_state_set(
    session: Session, frame: CalculationFrame, record: ElectronicStateSetRecord
) -> ElectronicStateSet:
    frame_id = _require_id(frame, label="CalculationFrame")
    if _fast_insert_enabled(session):
        new_state_set = ElectronicStateSet(frame=frame, **record.model_dump())
        _flush_new_entity(session, new_state_set, label="ElectronicStateSet")
        return new_state_set
    _acquire_identity_locks(session, ("ElectronicStateSet", frame_id, record.kind.value))
    state_set = session.exec(
        select(ElectronicStateSet).where(
            ElectronicStateSet.frame_id == frame_id,
            ElectronicStateSet.kind == record.kind,
        )
    ).first()
    if state_set is not None:
        _assert_record_matches(state_set, record, label="ElectronicStateSet")
        return state_set
    state_set = ElectronicStateSet(frame=frame, **record.model_dump())
    _flush_new_entity(session, state_set, label="ElectronicStateSet")
    return state_set


def persist_electronic_state(
    session: Session,
    state_set: ElectronicStateSet,
    record: ElectronicStateRecord,
) -> ElectronicState:
    state_set_id = _require_id(state_set, label="ElectronicStateSet")
    if record.set_kind is not state_set.kind:
        raise ValueError("ElectronicState record kind does not match its state set")
    values = record.model_dump(exclude={"set_kind"})
    if _fast_insert_enabled(session):
        new_state = ElectronicState(state_set=state_set, **values)
        _flush_new_entity(session, new_state, label="ElectronicState")
        return new_state
    _acquire_identity_locks(session, ("ElectronicState", state_set_id, record.state_ordinal))
    state = session.exec(
        select(ElectronicState).where(
            ElectronicState.state_set_id == state_set_id,
            ElectronicState.state_ordinal == record.state_ordinal,
        )
    ).first()
    if state is not None:
        _assert_record_matches(
            state,
            record,
            label="ElectronicState",
            exclude={"set_kind"},
        )
        return state
    state = ElectronicState(state_set=state_set, **values)
    _flush_new_entity(session, state, label="ElectronicState")
    return state


def persist_electronic_configuration(
    session: Session,
    electronic_state: ElectronicState,
    record: ElectronicConfigurationRecord,
) -> ElectronicConfiguration:
    state_id = _require_id(electronic_state, label="ElectronicState")
    if (
        record.set_kind is not electronic_state.state_set.kind
        or record.state_ordinal != electronic_state.state_ordinal
    ):
        raise ValueError("ElectronicConfiguration record does not match its state")
    values = record.model_dump(exclude={"set_kind", "state_ordinal"})
    if _fast_insert_enabled(session):
        new_configuration = ElectronicConfiguration(electronic_state=electronic_state, **values)
        _flush_new_entity(session, new_configuration, label="ElectronicConfiguration")
        return new_configuration
    _acquire_identity_locks(
        session, ("ElectronicConfiguration", state_id, record.configuration_ordinal)
    )
    configuration = session.exec(
        select(ElectronicConfiguration).where(
            ElectronicConfiguration.electronic_state_id == state_id,
            ElectronicConfiguration.configuration_ordinal == record.configuration_ordinal,
        )
    ).first()
    if configuration is not None:
        _assert_record_matches(
            configuration,
            record,
            label="ElectronicConfiguration",
            exclude={"set_kind", "state_ordinal"},
        )
        return configuration
    configuration = ElectronicConfiguration(electronic_state=electronic_state, **values)
    _flush_new_entity(session, configuration, label="ElectronicConfiguration")
    return configuration


def persist_multireference_result(
    session: Session,
    frame: CalculationFrame,
    record: MultireferenceResultRecord,
    electronic_state_set: ElectronicStateSet | None,
) -> MultireferenceResult:
    frame_id = _require_id(frame, label="CalculationFrame")
    state_set_id = (
        _require_id(electronic_state_set, label="ElectronicStateSet")
        if electronic_state_set is not None
        else None
    )
    _acquire_identity_locks(session, ("MultireferenceResult", frame_id))
    result = session.exec(
        select(MultireferenceResult).where(MultireferenceResult.frame_id == frame_id)
    ).first()
    if result is not None:
        if result.electronic_state_set_id != state_set_id:
            raise ValueError("MultireferenceResult resolved to a different electronic state set")
        _assert_record_matches(result, record, label="MultireferenceResult")
        return result
    result = MultireferenceResult(
        frame=frame,
        electronic_state_set=electronic_state_set,
        **record.model_dump(),
    )
    _flush_new_entity(session, result, label="MultireferenceResult")
    return result


def persist_implicit_solvation_result(
    session: Session, frame: CalculationFrame, record: ImplicitSolvationResultRecord
) -> ImplicitSolvationResult:
    return _persist_frame_result(
        session,
        frame,
        record,
        ImplicitSolvationResult,
        label="ImplicitSolvationResult",
    )


ScientificArrayOwner = (
    MolecularOrbitalResult
    | AtomicPopulationSeries
    | PolarizabilityResult
    | NMRResult
    | NMRShieldingTensor
    | BondOrderResult
    | SinglePointPropertyResult
    | ElectronicState
)


def persist_scientific_array_assignment(
    session: Session,
    scientific_array: ScientificArray,
    owner: ScientificArrayOwner,
    record: ScientificArrayAssignmentRecord,
) -> ScientificArrayAssignment:
    array_id = _require_id(scientific_array, label="ScientificArray")
    owner_id = _require_id(owner, label=type(owner).__name__)
    owner_fields: dict[str, Any]
    expected_kind: ScientificArrayOwnerKind
    expected_key: str | None = None
    if isinstance(owner, MolecularOrbitalResult):
        expected_kind = ScientificArrayOwnerKind.MOLECULAR_ORBITAL_RESULT
        owner_fields = {"molecular_orbital_result": owner}
        owner_frame_id = owner.frame_id
    elif isinstance(owner, AtomicPopulationSeries):
        expected_kind = ScientificArrayOwnerKind.ATOMIC_POPULATION_SERIES
        expected_key = owner.series_key
        owner_fields = {"atomic_population_series": owner}
        owner_frame_id = owner.result.frame_id
    elif isinstance(owner, PolarizabilityResult):
        expected_kind = ScientificArrayOwnerKind.POLARIZABILITY_RESULT
        owner_fields = {"polarizability_result": owner}
        owner_frame_id = owner.frame_id
    elif isinstance(owner, NMRResult):
        expected_kind = ScientificArrayOwnerKind.NMR_RESULT
        owner_fields = {"nmr_result": owner}
        owner_frame_id = owner.frame_id
    elif isinstance(owner, NMRShieldingTensor):
        expected_kind = ScientificArrayOwnerKind.NMR_SHIELDING_TENSOR
        expected_key = str(owner.atom_index)
        owner_fields = {"nmr_shielding_tensor": owner}
        owner_frame_id = owner.result.frame_id
    elif isinstance(owner, BondOrderResult):
        expected_kind = ScientificArrayOwnerKind.BOND_ORDER_RESULT
        owner_fields = {"bond_order_result": owner}
        owner_frame_id = owner.frame_id
    elif isinstance(owner, SinglePointPropertyResult):
        expected_kind = ScientificArrayOwnerKind.SINGLE_POINT_PROPERTY_RESULT
        owner_fields = {"single_point_property_result": owner}
        owner_frame_id = owner.frame_id
    else:
        expected_kind = ScientificArrayOwnerKind.ELECTRONIC_STATE
        expected_key = f"{owner.state_set.kind.value}:{owner.state_ordinal}"
        owner_fields = {"electronic_state": owner}
        owner_frame_id = owner.state_set.frame_id
    if record.owner_kind is not expected_kind or record.owner_key != expected_key:
        raise ValueError("ScientificArrayAssignment owner descriptor does not match its owner")
    if scientific_array.frame_id != owner_frame_id:
        raise ValueError("ScientificArrayAssignment owner must belong to the same frame")
    if (
        scientific_array.kind is not record.array_kind
        or scientific_array.ordinal != record.array_ordinal
    ):
        raise ValueError("ScientificArrayAssignment locator does not match its array")
    if _fast_insert_enabled(session):
        new_assignment = ScientificArrayAssignment(
            scientific_array=scientific_array,
            slot=record.slot,
            slot_ordinal=record.slot_ordinal,
            **owner_fields,
        )
        _flush_new_entity(session, new_assignment, label="ScientificArrayAssignment")
        return new_assignment
    _acquire_identity_locks(session, ("ScientificArrayAssignment", array_id, owner_id))
    assignment = session.exec(
        select(ScientificArrayAssignment).where(
            ScientificArrayAssignment.scientific_array_id == array_id
        )
    ).first()
    if assignment is not None:
        if assignment.slot != record.slot or assignment.slot_ordinal != record.slot_ordinal:
            raise ValueError("ScientificArray is already assigned to a different semantic slot")
        return assignment
    assignment = ScientificArrayAssignment(
        scientific_array=scientific_array,
        slot=record.slot,
        slot_ordinal=record.slot_ordinal,
        **owner_fields,
    )
    _flush_new_entity(session, assignment, label="ScientificArrayAssignment")
    return assignment


def finalize_parse_revision(
    session: Session,
    revision: ParseRevision,
    completion: ParseRevisionCompletionRecord,
) -> ParseRevision:
    """Mark a populated pending revision as succeeded after relationship validation."""

    revision_id = _require_id(revision, label="ParseRevision")
    _acquire_identity_locks(session, ("parse_revision_finalize", revision_id))
    session.refresh(revision)
    if revision.status is ParseStatus.SUCCEEDED:
        if revision.record_sha256 != completion.record_sha256:
            raise ValueError("ParseRevision was already finalized with a different payload")
        return revision
    if revision.status is not ParseStatus.PENDING:
        raise ValueError("only pending ParseRevision rows can be finalized as succeeded")
    if revision.started_at is not None and completion.completed_at < revision.started_at:
        raise ValueError("ParseRevision completion cannot precede its start time")
    if not revision.segments:
        raise ValueError("a succeeded ParseRevision requires at least one CalculationSegment")
    for segment in revision.segments:
        frame_count = len(segment.frames)
        if segment.source_frame_count is None and frame_count == 0:
            raise ValueError("a segment without source frame evidence requires a frame")
        if revision.source_complete is True and segment.source_frame_count != frame_count:
            raise ValueError("complete source capture must persist every located segment frame")
        if segment.source_frame_count is not None and frame_count > segment.source_frame_count:
            raise ValueError("persisted frame count cannot exceed the located source frame count")

    revision.record_sha256 = completion.record_sha256
    revision.completed_at = completion.completed_at
    revision.status = ParseStatus.SUCCEEDED
    revision.error_code = None
    revision.error_message = None
    revision.error_metadata = None
    session.add(revision)
    session.flush()
    return revision


__all__ = [
    "finalize_parse_revision",
    "persist_atomic_population_series",
    "persist_bond_order_result",
    "persist_calculation_frame",
    "persist_calculation_segment",
    "persist_calculation_status_result",
    "persist_charge_spin_population_result",
    "persist_energy_observation",
    "persist_electronic_configuration",
    "persist_electronic_state",
    "persist_electronic_state_set",
    "persist_frame_energy_result",
    "persist_geometry_optimization_result",
    "persist_implicit_solvation_result",
    "persist_molecular_orbital_result",
    "persist_multireference_result",
    "persist_nmr_result",
    "persist_nmr_shielding_tensor",
    "persist_parse_revision",
    "persist_scientific_array",
    "persist_scientific_array_assignment",
    "persist_single_point_property_result",
    "persist_thermochemistry_result",
    "persist_total_spin_result",
    "persist_polarizability_result",
    "persist_vibration_result",
]
