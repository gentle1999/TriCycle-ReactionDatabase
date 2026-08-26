"""Canonical ScientificArray records from public MolOP calculation fields."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
from molop.io.base_models.ChemFileFrame import BaseCalcFrame

from tricycle_reaction_db.application.dtos import (
    ScientificArrayAssignmentRecord,
    ScientificArrayRecord,
)
from tricycle_reaction_db.db.types import encode_numpy_array, summarize_numpy_array
from tricycle_reaction_db.domain.enums import (
    ElectronicStateSetKind,
    ScientificArrayKind,
    ScientificArrayOwnerKind,
)

_ARRAY_METADATA_SCHEMA_VERSION = "molop-array-source-v1"


def _quantity_array(quantity: Any, unit: str) -> npt.NDArray[np.float64]:
    return np.array(
        quantity.to(unit).magnitude,
        dtype="<f8",
        copy=True,
        order="C",
    )


def _record(
    *,
    kind: ScientificArrayKind,
    unit: str,
    data: npt.NDArray[np.float64],
    source_field: str,
    source_unit: str,
    ordinal: int = 0,
    metadata: dict[str, Any] | None = None,
) -> ScientificArrayRecord:
    encoded_data, _ = encode_numpy_array(data)
    encoded_data.setflags(write=False)
    summary = summarize_numpy_array(encoded_data)
    array_metadata: dict[str, Any] = {
        "source": "molop",
        "source_field": source_field,
        "source_unit": source_unit,
    }
    if metadata:
        array_metadata.update(metadata)
    return ScientificArrayRecord(
        kind=kind,
        ordinal=ordinal,
        unit=unit,
        dtype=summary.dtype,
        shape=list(summary.shape),
        array_nbytes=summary.nbytes,
        payload_sha256=summary.sha256,
        data=encoded_data,
        metadata_schema_version=_ARRAY_METADATA_SCHEMA_VERSION,
        array_metadata=array_metadata,
    )


def _append_quantity(
    records: list[ScientificArrayRecord],
    quantity: Any,
    *,
    kind: ScientificArrayKind,
    pint_unit: str,
    database_unit: str,
    source_field: str,
    ordinal: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    if quantity is None:
        return
    data = _quantity_array(quantity, pint_unit)
    if data.size == 0:
        return
    records.append(
        _record(
            kind=kind,
            ordinal=ordinal,
            unit=database_unit,
            data=data,
            source_field=source_field,
            source_unit=str(quantity.units),
            metadata=metadata,
        )
    )


def _scientific_array_export_from_molop_frame(
    frame: BaseCalcFrame[Any],
    *,
    frame_payload: dict[str, Any] | None = None,
) -> tuple[list[ScientificArrayRecord], list[ScientificArrayAssignmentRecord]]:
    """Return every supported, non-empty numerical array exposed by one MolOP frame."""

    if frame_payload is None:
        frame_payload = frame.model_dump(mode="python", exclude_none=False)
    records: list[ScientificArrayRecord] = []
    assignments: list[ScientificArrayAssignmentRecord] = []
    thermal = frame_payload["thermal_informations"]
    _append_quantity(
        records,
        frame_payload["forces"],
        kind=ScientificArrayKind.FORCES,
        pint_unit="hartree / bohr",
        database_unit="hartree/bohr",
        source_field="forces",
        metadata={
            "atom_order": "geometry_source_atom_order",
            "axis_order": ["source_atom", "xyz"],
            "coordinate_reference": "molop.frame.coords",
        },
    )
    _append_quantity(
        records,
        frame_payload["hessian"],
        kind=ScientificArrayKind.HESSIAN,
        pint_unit="hartree / bohr ** 2",
        database_unit="hartree/bohr^2",
        source_field="hessian",
        metadata={
            "atom_order": "geometry_source_atom_order",
            "axis_order": ["source_atom_xyz", "source_atom_xyz"],
            "coordinate_reference": "molop.frame.coords",
        },
    )
    rotation_metadata: dict[str, Any] = {}
    if thermal is not None and thermal.get("rotational_constants") is not None:
        frame_constants = _quantity_array(frame_payload["rotation_constants"], "gigahertz")
        thermal_constants = _quantity_array(thermal["rotational_constants"], "gigahertz")
        if frame_constants.shape != thermal_constants.shape or not np.allclose(
            frame_constants,
            thermal_constants,
            rtol=0.0,
            # Gaussian prints the thermal duplicate at five decimal places;
            # allow its half-unit rounding error, including binary float noise.
            atol=5.5e-6,
        ):
            raise ValueError("MolOP rotational-constant sources disagree")
        rotation_metadata = {
            "authority_policy": "prefer-higher-precision-frame-value-v1",
            "thermal_duplicate_max_abs_difference_ghz": float(
                np.max(np.abs(frame_constants - thermal_constants))
            ),
            "thermal_duplicate_source_field": "thermal_informations.rotational_constants",
        }
    _append_quantity(
        records,
        frame_payload["rotation_constants"],
        kind=ScientificArrayKind.ROTATIONAL_CONSTANTS,
        pint_unit="gigahertz",
        database_unit="gigahertz",
        source_field="rotation_constants",
        metadata=rotation_metadata,
    )

    vibrations = frame_payload["vibrations"]
    if vibrations is not None:
        _append_quantity(
            records,
            vibrations["frequencies"],
            kind=ScientificArrayKind.VIBRATIONAL_FREQUENCIES,
            pint_unit="1 / centimeter",
            database_unit="cm^-1",
            source_field="vibrations.frequencies",
        )
        _append_quantity(
            records,
            vibrations["reduced_masses"],
            kind=ScientificArrayKind.REDUCED_MASSES,
            pint_unit="unified_atomic_mass_unit",
            database_unit="amu",
            source_field="vibrations.reduced_masses",
        )
        _append_quantity(
            records,
            vibrations["force_constants"],
            kind=ScientificArrayKind.VIBRATIONAL_FORCE_CONSTANTS,
            pint_unit="millidyne / angstrom",
            database_unit="mdyne/angstrom",
            source_field="vibrations.force_constants",
        )
        _append_quantity(
            records,
            vibrations["IR_intensities"],
            kind=ScientificArrayKind.IR_INTENSITIES,
            pint_unit="kilometer / mole",
            database_unit="km/mol",
            source_field="vibrations.IR_intensities",
        )
        if vibrations["vibration_modes"]:
            normal_modes = np.stack(
                [_quantity_array(mode, "angstrom") for mode in vibrations["vibration_modes"]]
            )
            records.append(
                _record(
                    kind=ScientificArrayKind.NORMAL_MODES,
                    unit="angstrom",
                    data=normal_modes,
                    source_field="vibrations.vibration_modes",
                    source_unit=str(vibrations["vibration_modes"][0].units),
                    metadata={
                        "atom_order": "geometry_source_atom_order",
                        "axis_order": ["mode", "source_atom", "xyz"],
                        "coordinate_reference": "molop.frame.coords",
                    },
                )
            )

    if thermal is not None:
        _append_quantity(
            records,
            thermal.get("moments_of_inertia"),
            kind=ScientificArrayKind.MOMENTS_OF_INERTIA,
            pint_unit="unified_atomic_mass_unit * bohr ** 2",
            database_unit="amu*bohr^2",
            source_field="thermal_informations.moments_of_inertia",
        )
        _append_quantity(
            records,
            thermal.get("rotational_temperatures"),
            kind=ScientificArrayKind.ROTATIONAL_TEMPERATURES,
            pint_unit="kelvin",
            database_unit="kelvin",
            source_field="thermal_informations.rotational_temperatures",
        )
        vibrational_temperatures = thermal.get("vibrational_temperatures")
        if vibrational_temperatures is not None:
            data = _quantity_array(vibrational_temperatures, "kelvin")
            if data.size:
                frequencies = (
                    _quantity_array(vibrations["frequencies"], "1 / centimeter")
                    if vibrations is not None
                    else np.array([], dtype="<f8")
                )
                positive_mode_indices = np.flatnonzero(frequencies > 0).tolist()
                if len(positive_mode_indices) != data.shape[0]:
                    raise ValueError(
                        "MolOP vibrational temperatures do not match the positive-frequency modes"
                    )
                records.append(
                    _record(
                        kind=ScientificArrayKind.VIBRATIONAL_TEMPERATURES,
                        unit="kelvin",
                        data=data,
                        source_field="thermal_informations.vibrational_temperatures",
                        source_unit=str(vibrational_temperatures.units),
                        metadata={"frequency_mode_indices": positive_mode_indices},
                    )
                )
    next_ordinal = {
        kind: max((record.ordinal for record in records if record.kind is kind), default=-1) + 1
        for kind in ScientificArrayKind
    }

    def append_owned(
        *,
        kind: ScientificArrayKind,
        unit: str,
        data: npt.NDArray[np.float64],
        source_field: str,
        source_unit: str,
        owner_kind: ScientificArrayOwnerKind,
        slot: str,
        owner_key: str | None = None,
        slot_ordinal: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if data.size == 0:
            return
        ordinal = next_ordinal[kind]
        next_ordinal[kind] += 1
        records.append(
            _record(
                kind=kind,
                ordinal=ordinal,
                unit=unit,
                data=np.array(data, dtype="<f8", copy=True, order="C"),
                source_field=source_field,
                source_unit=source_unit,
                metadata=metadata,
            )
        )
        assignments.append(
            ScientificArrayAssignmentRecord(
                array_kind=kind,
                array_ordinal=ordinal,
                owner_kind=owner_kind,
                owner_key=owner_key,
                slot=slot,
                slot_ordinal=slot_ordinal,
            )
        )

    molecular_orbitals = frame_payload["molecular_orbitals"]
    if molecular_orbitals is not None:
        append_owned(
            kind=ScientificArrayKind.ORBITAL_ALPHA_ENERGIES,
            unit="hartree",
            data=_quantity_array(molecular_orbitals["alpha_energies"], "hartree"),
            source_field="molecular_orbitals.alpha_energies",
            source_unit=str(molecular_orbitals["alpha_energies"].units),
            owner_kind=ScientificArrayOwnerKind.MOLECULAR_ORBITAL_RESULT,
            slot="alpha_energies",
        )
        append_owned(
            kind=ScientificArrayKind.ORBITAL_BETA_ENERGIES,
            unit="hartree",
            data=_quantity_array(molecular_orbitals["beta_energies"], "hartree"),
            source_field="molecular_orbitals.beta_energies",
            source_unit=str(molecular_orbitals["beta_energies"].units),
            owner_kind=ScientificArrayOwnerKind.MOLECULAR_ORBITAL_RESULT,
            slot="beta_energies",
        )
        for coefficient_index, coefficient in enumerate(molecular_orbitals["coefficients"]):
            if coefficient is None:
                continue
            append_owned(
                kind=ScientificArrayKind.ORBITAL_COEFFICIENT,
                unit="dimensionless",
                data=np.asarray(coefficient, dtype=np.float64),
                source_field="molecular_orbitals.coefficients",
                source_unit="dimensionless",
                owner_kind=ScientificArrayOwnerKind.MOLECULAR_ORBITAL_RESULT,
                slot="coefficient",
                slot_ordinal=coefficient_index,
                metadata={"orbital_index": coefficient_index},
            )

    populations = frame_payload["charge_spin_populations"]
    if populations is not None:
        for series_key, series in populations["populations"].items():
            append_owned(
                kind=ScientificArrayKind.ATOMIC_POPULATION,
                unit="dimensionless",
                data=np.asarray(series["values"], dtype=np.float64),
                source_field=f"charge_spin_populations.populations.{series_key}.values",
                source_unit="dimensionless",
                owner_kind=ScientificArrayOwnerKind.ATOMIC_POPULATION_SERIES,
                owner_key=series_key,
                slot="values",
            )

    polarizability = frame_payload["polarizability"]
    if polarizability is not None:
        polarizability_fields = (
            (
                "polarizability_tensor",
                ScientificArrayKind.POLARIZABILITY_TENSOR,
                "bohr ** 3",
                "bohr^3",
            ),
            (
                "electric_dipole_moment",
                ScientificArrayKind.ELECTRIC_DIPOLE_MOMENT,
                "debye",
                "debye",
            ),
            ("dipole", ScientificArrayKind.DIPOLE, "debye", "debye"),
            ("quadrupole", ScientificArrayKind.QUADRUPOLE, "debye * angstrom", "debye*angstrom"),
            (
                "traceless_quadrupole",
                ScientificArrayKind.TRACELESS_QUADRUPOLE,
                "debye * angstrom",
                "debye*angstrom",
            ),
            ("octapole", ScientificArrayKind.OCTAPOLE, "debye * angstrom ** 2", "debye*angstrom^2"),
            (
                "hexadecapole",
                ScientificArrayKind.HEXADECAPOLE,
                "debye * angstrom ** 3",
                "debye*angstrom^3",
            ),
        )
        for field_name, kind, pint_unit, database_unit in polarizability_fields:
            value = polarizability.get(field_name)
            if value is None:
                continue
            append_owned(
                kind=kind,
                unit=database_unit,
                data=_quantity_array(value, pint_unit),
                source_field=f"polarizability.{field_name}",
                source_unit=str(value.units),
                owner_kind=ScientificArrayOwnerKind.POLARIZABILITY_RESULT,
                slot=field_name,
            )

    nmr = frame_payload["nmr"]
    if nmr is not None:
        for shielding in nmr["shielding_tensors"]:
            owner_key = str(shielding["atom_index"])
            append_owned(
                kind=ScientificArrayKind.NMR_SHIELDING_TENSOR,
                unit="ppm",
                data=_quantity_array(shielding["shielding_tensor"], "ppm"),
                source_field="nmr.shielding_tensors.shielding_tensor",
                source_unit=str(shielding["shielding_tensor"].units),
                owner_kind=ScientificArrayOwnerKind.NMR_SHIELDING_TENSOR,
                owner_key=owner_key,
                slot="shielding_tensor",
            )
            if shielding["principal_values"] is not None:
                append_owned(
                    kind=ScientificArrayKind.NMR_PRINCIPAL_VALUES,
                    unit="ppm",
                    data=_quantity_array(shielding["principal_values"], "ppm"),
                    source_field="nmr.shielding_tensors.principal_values",
                    source_unit=str(shielding["principal_values"].units),
                    owner_kind=ScientificArrayOwnerKind.NMR_SHIELDING_TENSOR,
                    owner_key=owner_key,
                    slot="principal_values",
                )
        coupling_fields = (
            ("spin_spin_coupling_k", ScientificArrayKind.NMR_COUPLING_K),
            ("spin_spin_coupling_j", ScientificArrayKind.NMR_COUPLING_J),
        )
        for field_name, kind in coupling_fields:
            value = nmr.get(field_name)
            if value is not None:
                append_owned(
                    kind=kind,
                    unit="hertz",
                    data=_quantity_array(value, "hertz"),
                    source_field=f"nmr.{field_name}",
                    source_unit=str(value.units),
                    owner_kind=ScientificArrayOwnerKind.NMR_RESULT,
                    slot=field_name,
                )
        component_fields = (
            ("spin_spin_coupling_k_components", ScientificArrayKind.NMR_COUPLING_K_COMPONENT),
            ("spin_spin_coupling_j_components", ScientificArrayKind.NMR_COUPLING_J_COMPONENT),
        )
        for field_name, kind in component_fields:
            for component_index, (component, value) in enumerate(nmr.get(field_name, {}).items()):
                append_owned(
                    kind=kind,
                    unit="hertz",
                    data=_quantity_array(value, "hertz"),
                    source_field=f"nmr.{field_name}.{component}",
                    source_unit=str(value.units),
                    owner_kind=ScientificArrayOwnerKind.NMR_RESULT,
                    slot=field_name,
                    slot_ordinal=component_index,
                    metadata={"component": component},
                )

    bond_orders = frame_payload["bond_orders"]
    if bond_orders is not None:
        for field_name in (
            "wiberg_bond_order",
            "mo_bond_order",
            "mayer_bond_order",
            "atom_atom_overlap_bond_order",
            "nbo_bond_order",
            "nbo_bond_order_for_alpha_spin",
            "nbo_bond_order_for_beta_spin",
        ):
            value = np.asarray(bond_orders.get(field_name, []), dtype=np.float64)
            append_owned(
                kind=ScientificArrayKind.BOND_ORDER_MATRIX,
                unit="dimensionless",
                data=value,
                source_field=f"bond_orders.{field_name}",
                source_unit="dimensionless",
                owner_kind=ScientificArrayOwnerKind.BOND_ORDER_RESULT,
                slot=field_name,
            )

    single_point = frame_payload["single_point_properties"]
    if single_point is not None:
        for field_name, kind in (
            ("fukui_positive", ScientificArrayKind.FUKUI_POSITIVE),
            ("fukui_negative", ScientificArrayKind.FUKUI_NEGATIVE),
            ("fukui_zero", ScientificArrayKind.FUKUI_ZERO),
            ("fod", ScientificArrayKind.FRACTIONAL_OCCUPATION_DENSITY),
        ):
            append_owned(
                kind=kind,
                unit="dimensionless",
                data=np.asarray(single_point.get(field_name, []), dtype=np.float64),
                source_field=f"single_point_properties.{field_name}",
                source_unit="dimensionless",
                owner_kind=ScientificArrayOwnerKind.SINGLE_POINT_PROPERTY_RESULT,
                slot=field_name,
            )

    state_sets = (
        (ElectronicStateSetKind.FRAME, frame_payload["electronic_states"]),
        (
            ElectronicStateSetKind.MULTIREFERENCE,
            frame_payload["multireference_result"]["electronic_states"]
            if frame_payload["multireference_result"] is not None
            else None,
        ),
    )
    for set_kind, state_set in state_sets:
        if state_set is None:
            continue
        for state_ordinal, state in enumerate(state_set["states"]):
            if state.get("transition_dipole") is None:
                continue
            append_owned(
                kind=ScientificArrayKind.TRANSITION_DIPOLE,
                unit="debye",
                data=_quantity_array(state["transition_dipole"], "debye"),
                source_field=f"{set_kind.value}.electronic_states.transition_dipole",
                source_unit=str(state["transition_dipole"].units),
                owner_kind=ScientificArrayOwnerKind.ELECTRONIC_STATE,
                owner_key=f"{set_kind.value}:{state_ordinal}",
                slot="transition_dipole",
            )

    return records, assignments


def scientific_array_records_from_molop_frame(
    frame: BaseCalcFrame[Any],
) -> list[ScientificArrayRecord]:
    """Return every supported, non-empty numerical array exposed by one MolOP frame."""

    return _scientific_array_export_from_molop_frame(frame)[0]


def scientific_array_export_from_molop_frame(
    frame: BaseCalcFrame[Any],
    *,
    frame_payload: dict[str, Any] | None = None,
) -> tuple[list[ScientificArrayRecord], list[ScientificArrayAssignmentRecord]]:
    """Return numerical arrays together with their explicit result ownership."""

    return _scientific_array_export_from_molop_frame(frame, frame_payload=frame_payload)


__all__ = [
    "scientific_array_export_from_molop_frame",
    "scientific_array_records_from_molop_frame",
]
