"""Preserve MolOP 0.2.21 clocks, result semantics, and Raman arrays.

Existing values are not relabelled or recomputed: new nullable fields remain
unknown until a versioned reparse reads the original source. No historical
parse revision, scientific array, or artifact is deleted by this migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0063_molop_result_semantics"
down_revision = "0062_staged_upload_claim_index"
branch_labels = None
depends_on = None


_RESULT_COLUMNS = {
    "vibration_result": [
        sa.Column("source_mode_indices", ARRAY(sa.Integer()), nullable=True),
        sa.Column("source_program", sa.String(128), nullable=True),
        sa.Column("frequency_method", sa.String(128), nullable=True),
        sa.Column("raman_activity_method", sa.String(128), nullable=True),
        sa.Column("orientation", sa.String(32), nullable=True),
    ],
    "molecular_orbital_result": [
        sa.Column("orbital_basis", sa.String(32), nullable=True),
        sa.Column("occupancy_origin", sa.String(32), nullable=True),
        sa.Column("total_occupancies", ARRAY(sa.Float()), nullable=True),
        sa.Column("reported_homo_index", sa.Integer(), nullable=True),
        sa.Column("reported_lumo_index", sa.Integer(), nullable=True),
    ],
    "thermochemistry_result": [
        sa.Column("source_program", sa.String(128), nullable=True),
        sa.Column("entropy_model", sa.String(16), nullable=True),
        sa.Column("frequency_scale_factor", sa.Float(), nullable=True),
        sa.Column("low_frequency_cutoff_cm1", sa.Float(), nullable=True),
        sa.Column("rotor_reference_frequency_cm1", sa.Float(), nullable=True),
        sa.Column("imaginary_frequency_cutoff_cm1", sa.Float(), nullable=True),
        sa.Column("derivations", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    ],
    "electronic_state": [sa.Column("is_ground_state", sa.Boolean(), nullable=True)],
}


_ARRAY_KINDS = (
    "kind IN ('forces', 'hessian', 'vibrational_frequencies', 'reduced_masses', 'vibrational_"
    "force_constants', 'ir_intensities', 'raman_activities', 'raman_depolarization_ratios', '"
    "raman_depolarization_ratios_polarized', 'raman_depolarization_ratios_unpolarized', 'norm"
    "al_modes', 'moments_of_inertia', 'rotational_temperatures', 'rotational_constants', 'vib"
    "rational_temperatures', 'orbital_alpha_energies', 'orbital_beta_energies', 'orbital_coef"
    "ficient', 'atomic_population', 'polarizability_tensor', 'electric_dipole_moment', 'dipol"
    "e', 'quadrupole', 'traceless_quadrupole', 'octapole', 'hexadecapole', 'nmr_shielding_ten"
    "sor', 'nmr_principal_values', 'nmr_coupling_k', 'nmr_coupling_j', 'nmr_coupling_k_compon"
    "ent', 'nmr_coupling_j_component', 'bond_order_matrix', 'fukui_positive', 'fukui_negative"
    "', 'fukui_zero', 'fractional_occupation_density', 'transition_dipole')"
)
_ARRAY_UNITS = (
    "(kind = 'forces' AND unit = 'hartree/bohr') OR (kind = 'hessian' AND unit = 'hartree/boh"
    "r^2') OR (kind = 'vibrational_frequencies' AND unit = 'cm^-1') OR (kind = 'reduced_masse"
    "s' AND unit = 'amu') OR (kind = 'vibrational_force_constants' AND unit = 'mdyne/angstrom"
    "') OR (kind = 'ir_intensities' AND unit = 'km/mol') OR (kind = 'raman_activities' AND un"
    "it = 'angstrom^4/amu') OR (kind IN ('raman_depolarization_ratios', 'raman_depolarization"
    "_ratios_polarized', 'raman_depolarization_ratios_unpolarized') AND unit = 'dimensionless"
    "') OR (kind = 'normal_modes' AND unit = 'angstrom') OR (kind = 'moments_of_inertia' AND "
    "unit = 'amu*bohr^2') OR (kind = 'rotational_temperatures' AND unit = 'kelvin') OR (kind "
    "= 'rotational_constants' AND unit = 'gigahertz') OR (kind = 'vibrational_temperatures' A"
    "ND unit = 'kelvin') OR (kind IN ('orbital_alpha_energies', 'orbital_beta_energies') AND "
    "unit = 'hartree') OR (kind IN ('orbital_coefficient', 'atomic_population', 'bond_order_m"
    "atrix', 'fukui_positive', 'fukui_negative', 'fukui_zero', 'fractional_occupation_density"
    "') AND unit = 'dimensionless') OR (kind = 'polarizability_tensor' AND unit = 'bohr^3') O"
    "R (kind IN ('electric_dipole_moment', 'dipole', 'transition_dipole') AND unit = 'debye')"
    " OR (kind IN ('quadrupole', 'traceless_quadrupole') AND unit = 'debye*angstrom') OR (kin"
    "d = 'octapole' AND unit = 'debye*angstrom^2') OR (kind = 'hexadecapole' AND unit = 'deby"
    "e*angstrom^3') OR (kind IN ('nmr_shielding_tensor', 'nmr_principal_values') AND unit = '"
    "ppm') OR (kind IN ('nmr_coupling_k', 'nmr_coupling_j', 'nmr_coupling_k_component', 'nmr_"
    "coupling_j_component') AND unit = 'hertz')"
)


def upgrade() -> None:
    for table in ("parse_revision", "calculation_frame"):
        op.add_column(table, sa.Column("wall_time_seconds", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("cpu_time_seconds", sa.Float(), nullable=True))
        op.add_column(table, sa.Column("cpu_time_source", sa.String(16), nullable=True))
        op.add_column(table, sa.Column("cpu_time_num_processes", sa.Integer(), nullable=True))
    for table, columns in _RESULT_COLUMNS.items():
        for column in columns:
            op.add_column(table, column)
    for name in ("temperature_kelvin", "pressure_atm"):
        op.alter_column("thermochemistry_result", name, existing_type=sa.Float(), nullable=True)
    op.drop_constraint("scientific_array_kind", "scientific_array", type_="check")
    op.drop_constraint("ck_scientific_array_kind_unit", "scientific_array", type_="check")
    op.alter_column("scientific_array", "kind", type_=sa.String(39))
    op.create_check_constraint("scientific_array_kind", "scientific_array", _ARRAY_KINDS)
    op.create_check_constraint("ck_scientific_array_kind_unit", "scientific_array", _ARRAY_UNITS)


def downgrade() -> None:
    raise RuntimeError(
        "Restore a backup to downgrade MolOP result semantics; dropping these fields "
        "would discard scientific provenance and Raman data."
    )
