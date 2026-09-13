"""Canonicalize protocol text and thermodynamic source keys."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

from tricycle_reaction_db.core.chemistry_config import CALCULATION_PROTOCOL_VERSION
from tricycle_reaction_db.core.protocol_normalization import (
    normalize_functional_and_dispersion,
    normalize_protocol_text,
)

revision: str = "0041_normalize_protocol_case"
down_revision: str | None = "0040_normalize_profile_nulls"
branch_labels: str | None = None
depends_on: str | None = None


def _canonical_protocol_spec(
    value: Any,
    *,
    functional: str | None,
    basis_set: str | None,
    auxiliary_basis_set: str | None,
    dispersion_model: str | None,
) -> dict[str, Any]:
    """Canonicalize the persisted projection while retaining raw provenance."""

    spec = dict(value) if isinstance(value, Mapping) else {}
    source_protocol = spec.get("protocol")
    if not isinstance(source_protocol, Mapping):
        return spec

    raw_protocol = dict(source_protocol)
    normalized_protocol = dict(raw_protocol)
    normalized_protocol.update(
        functional=functional,
        basis_set=basis_set,
        auxiliary_basis_set=auxiliary_basis_set,
        dispersion_correction=dispersion_model,
    )
    if normalized_protocol != raw_protocol:
        spec.setdefault("source_protocol", raw_protocol)
    spec["protocol"] = normalized_protocol
    return spec


def _protocol_identity_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Remove source spelling from the hash input; it is provenance only."""

    return {key: value for key, value in spec.items() if key != "source_protocol"}


def _protocol_row_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    functional, dispersion_model = normalize_functional_and_dispersion(
        row["functional"],
        row["dispersion_model"],
    )
    basis_set = normalize_protocol_text(row["basis_set"])
    auxiliary_basis_set = normalize_protocol_text(row["auxiliary_basis_set"])
    normalized_spec = _canonical_protocol_spec(
        row["normalized_spec"],
        functional=functional,
        basis_set=basis_set,
        auxiliary_basis_set=auxiliary_basis_set,
        dispersion_model=dispersion_model,
    )
    task_requests = sorted(set(row["task_requests"] or []))
    identity = {
        "schema_version": CALCULATION_PROTOCOL_VERSION,
        "qm_software": getattr(row["qm_software"], "value", row["qm_software"]),
        "qm_software_version": row["qm_software_version"],
        "model_chemistry": {
            "method_family": row["method_family"],
            "method": row["method"],
            "reference_method": row["reference_method"],
            "functional": functional,
            "basis_set": basis_set,
            "auxiliary_basis_set": auxiliary_basis_set,
            "dispersion_model": dispersion_model,
            "solvation_model": row["solvation_model"],
            "solvent": row["solvent"],
            "relativistic_method": row["relativistic_method"],
        },
        "task_requests": task_requests,
        "normalized_spec": _protocol_identity_spec(normalized_spec),
    }
    protocol_hash = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "protocol_hash": protocol_hash,
        "spec_schema_version": CALCULATION_PROTOCOL_VERSION,
        "functional": functional,
        "basis_set": basis_set,
        "auxiliary_basis_set": auxiliary_basis_set,
        "dispersion_model": dispersion_model,
        "task_requests": task_requests,
        "normalized_spec": normalized_spec,
    }


def _row_order(row: Mapping[str, Any]) -> tuple[bool, str, str]:
    created_at = row["created_at"]
    return (
        created_at is None,
        created_at.isoformat() if created_at is not None else "",
        str(row["id"]),
    )


def _normalize_protocols(connection: sa.Connection) -> None:
    rows = list(
        connection.execute(
            sa.text(
                """
                SELECT id, created_at, project_id, protocol_hash,
                       qm_software, qm_software_version, method_family, method,
                       reference_method, functional, basis_set,
                       auxiliary_basis_set, dispersion_model, solvation_model,
                       solvent, relativistic_method, task_requests,
                       normalized_spec
                FROM calculation_protocol
                ORDER BY created_at NULLS LAST, id
                """
            )
        ).mappings()
    )
    prepared: list[tuple[Mapping[str, Any], dict[str, Any]]] = [
        (row, _protocol_row_payload(row)) for row in rows
    ]
    groups: defaultdict[tuple[Any, str], list[tuple[Mapping[str, Any], dict[str, Any]]]] = (
        defaultdict(list)
    )
    for row, payload in prepared:
        groups[(row["project_id"], payload["protocol_hash"])].append((row, payload))

    duplicate_ids: list[Any] = []
    keepers: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda item: _row_order(item[0]))
        keeper = ordered[0]
        keepers.append(keeper)
        for duplicate, _ in ordered[1:]:
            duplicate_ids.append(duplicate["id"])
            connection.execute(
                sa.text(
                    "UPDATE calculation_segment "
                    "SET protocol_id = :keeper_id "
                    "WHERE protocol_id = :duplicate_id"
                ),
                {"keeper_id": keeper[0]["id"], "duplicate_id": duplicate["id"]},
            )

    # Remove colliding rows before changing the keeper hashes. The duplicate
    # rows are semantically identical after canonicalization; segments have
    # already been moved to the deterministic keeper above.
    for duplicate_id in duplicate_ids:
        connection.execute(
            sa.text("DELETE FROM calculation_protocol WHERE id = :id"),
            {"id": duplicate_id},
        )

    update_statement = sa.text(
        """
        UPDATE calculation_protocol
        SET protocol_hash = :protocol_hash,
            spec_schema_version = :spec_schema_version,
            functional = :functional,
            basis_set = :basis_set,
            auxiliary_basis_set = :auxiliary_basis_set,
            dispersion_model = :dispersion_model,
            task_requests = CAST(:task_requests AS text[]),
            normalized_spec = CAST(:normalized_spec AS jsonb)
        WHERE id = :id
        """
    )
    for row, payload in keepers:
        connection.execute(
            update_statement,
            {
                "id": row["id"],
                **payload,
                "task_requests": payload["task_requests"],
                "normalized_spec": json.dumps(
                    payload["normalized_spec"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )


def _canonical_level(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    level = list(value)
    if len(level) > 4:
        level[4] = normalize_protocol_text(level[4])
    if len(level) > 5:
        level[5] = normalize_protocol_text(level[5])
    if len(level) > 3:
        dispersion = level[6] if len(level) > 6 else None
        functional, canonical_dispersion = normalize_functional_and_dispersion(
            level[3],
            dispersion,
        )
        level[3] = functional
        if len(level) > 6:
            level[6] = canonical_dispersion
    return level


def _profile_source_hash(
    electronic_level: Any,
    thermochemistry_level: Any,
    row: Mapping[str, Any],
) -> str:
    source_key = {
        "electronic_level": electronic_level,
        "thermochemistry_level": thermochemistry_level,
        "temperature_kelvin": row["temperature_kelvin"],
        "pressure_atm": row["pressure_atm"],
    }
    return hashlib.sha256(
        json.dumps(
            source_key,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _normalize_profiles(connection: sa.Connection) -> None:
    rows = list(
        connection.execute(
            sa.text(
                """
                SELECT id, created_at, mapped_reaction_id, electronic_level,
                       thermochemistry_level, temperature_kelvin, pressure_atm,
                       reactants, transition_state, products,
                       reactants_gibbs_free_energy_hartree,
                       transition_state_gibbs_free_energy_hartree,
                       products_gibbs_free_energy_hartree
                FROM mapped_reaction_thermodynamic_profile
                ORDER BY mapped_reaction_id, created_at NULLS LAST, id
                """
            )
        ).mappings()
    )
    prepared = []
    for row in rows:
        electronic_level = _canonical_level(row["electronic_level"])
        thermochemistry_level = _canonical_level(row["thermochemistry_level"])
        prepared.append(
            (
                row,
                electronic_level,
                thermochemistry_level,
                _profile_source_hash(electronic_level, thermochemistry_level, row),
            )
        )

    groups: defaultdict[tuple[Any, str], list[tuple[Any, Any, Any, str]]] = defaultdict(list)
    for item in prepared:
        row, _, _, source_hash = item
        groups[(row["mapped_reaction_id"], source_hash)].append(item)

    keepers = []
    duplicate_ids = []
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda item: (
                item[0]["reactants"] is None,
                item[0]["products"] is None,
                item[0]["reactants_gibbs_free_energy_hartree"] is None,
                item[0]["products_gibbs_free_energy_hartree"] is None,
                item[0]["transition_state_gibbs_free_energy_hartree"] is None,
                item[0]["transition_state"] is None,
                item[0]["created_at"] is None,
                item[0]["created_at"].isoformat() if item[0]["created_at"] is not None else "",
                str(item[0]["id"]),
            ),
        )
        keepers.append(ordered[0])
        duplicate_ids.extend(item[0]["id"] for item in ordered[1:])

    for duplicate_id in duplicate_ids:
        connection.execute(
            sa.text("DELETE FROM mapped_reaction_thermodynamic_profile WHERE id = :id"),
            {"id": duplicate_id},
        )

    update_statement = sa.text(
        """
        UPDATE mapped_reaction_thermodynamic_profile
        SET source_key_hash = :source_key_hash,
            electronic_level = CAST(:electronic_level AS jsonb),
            thermochemistry_level = CAST(:thermochemistry_level AS jsonb)
        WHERE id = :id
        """
    )
    for row, electronic_level, thermochemistry_level, source_hash in keepers:
        connection.execute(
            update_statement,
            {
                "id": row["id"],
                "source_key_hash": source_hash,
                "electronic_level": json.dumps(electronic_level, separators=(",", ":")),
                "thermochemistry_level": json.dumps(
                    thermochemistry_level,
                    separators=(",", ":"),
                ),
            },
        )

    # Keep denormalized screening bounds exact if two source spellings merged
    # into one profile row.
    connection.execute(
        sa.text(
            """
            WITH aggregates AS (
                SELECT mapped_reaction_id,
                       MIN(activation_gibbs_free_energy_kcal_mol) AS min_activation,
                       MAX(activation_gibbs_free_energy_kcal_mol) AS max_activation,
                       MIN(reaction_gibbs_free_energy_kcal_mol) AS min_reaction,
                       MAX(reaction_gibbs_free_energy_kcal_mol) AS max_reaction
                FROM mapped_reaction_thermodynamic_profile
                GROUP BY mapped_reaction_id
            )
            UPDATE mapped_reaction AS reaction
            SET minimum_activation_gibbs_free_energy_kcal_mol = aggregates.min_activation,
                maximum_activation_gibbs_free_energy_kcal_mol = aggregates.max_activation,
                minimum_reaction_gibbs_free_energy_kcal_mol = aggregates.min_reaction,
                maximum_reaction_gibbs_free_energy_kcal_mol = aggregates.max_reaction
            FROM aggregates
            WHERE reaction.id = aggregates.mapped_reaction_id
            """
        )
    )
    connection.execute(
        sa.text(
            """
            UPDATE mapped_reaction AS reaction
            SET minimum_activation_gibbs_free_energy_kcal_mol = NULL,
                maximum_activation_gibbs_free_energy_kcal_mol = NULL,
                minimum_reaction_gibbs_free_energy_kcal_mol = NULL,
                maximum_reaction_gibbs_free_energy_kcal_mol = NULL
            WHERE NOT EXISTS (
                SELECT 1
                FROM mapped_reaction_thermodynamic_profile AS profile
                WHERE profile.mapped_reaction_id = reaction.id
            )
            """
        )
    )


def upgrade() -> None:
    connection = op.get_bind()
    _normalize_protocols(connection)
    _normalize_profiles(connection)


def downgrade() -> None:
    # The canonical representation is intentionally not reverted: restoring
    # source casing would recreate duplicate protocol identities.
    pass
