from uuid import uuid4

from sqlalchemy.dialects import postgresql
from sqlmodel import col, select

from tricycle_reaction_db.application.services.queries import (
    mapped_reaction_has_thermodynamic_profile,
)
from tricycle_reaction_db.application.services.query_visibility import (
    QueryVisibilityScope,
    formula_id_is_visible,
    frame_id_is_visible,
    logical_reaction_id_is_visible,
    mapped_reaction_id_is_visible,
    thermodynamic_profile_is_visible,
    topology_id_is_visible,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    MappedReactionThermodynamicProfile,
    MolecularFormula,
    MolecularTopology,
    ScientificArray,
)


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_profile_range_filter_keeps_all_conditions_in_one_exists() -> None:
    scope = QueryVisibilityScope(
        principal=None,
        project_ids=frozenset(),
        unrestricted=True,
    )
    statement = select(MappedReaction.id).where(
        mapped_reaction_has_thermodynamic_profile(
            scope,
            col(MappedReaction.id),
            minimum_activation_gibbs_free_energy_kcal_mol=10.0,
            maximum_activation_gibbs_free_energy_kcal_mol=20.0,
            minimum_reaction_gibbs_free_energy_kcal_mol=-5.0,
            maximum_reaction_gibbs_free_energy_kcal_mol=5.0,
        )
    )

    compiled = _sql(statement)

    assert compiled.count("FROM mapped_reaction_thermodynamic_profile") == 1
    assert "activation_gibbs_free_energy_kcal_mol >=" in compiled
    assert "activation_gibbs_free_energy_kcal_mol <=" in compiled
    assert "reaction_gibbs_free_energy_kcal_mol >=" in compiled
    assert "reaction_gibbs_free_energy_kcal_mol <=" in compiled
    assert "minimum_activation_gibbs_free_energy_kcal_mol" not in compiled
    assert "maximum_activation_gibbs_free_energy_kcal_mol" not in compiled


def test_restricted_profile_visibility_requires_successful_source_provenance() -> None:
    project_id = uuid4()
    scope = QueryVisibilityScope(
        principal=None,
        project_ids=frozenset({project_id}),
        requested_project_id=project_id,
        requested_project_permitted=True,
    )
    statement = select(MappedReactionThermodynamicProfile.id).where(
        thermodynamic_profile_is_visible(scope)
    )

    compiled = _sql(statement)

    assert "artifact_ingestion.status" in compiled
    assert "parse_revision.status" in compiled
    assert compiled.count("selection ?") >= 4
    assert "AS thermodynamic_profile_reactants(selection)" in compiled
    assert "AS thermodynamic_profile_transition_state(selection)" in compiled
    assert "AS thermodynamic_profile_products(selection)" in compiled


def test_project_scope_uses_direct_project_ownership_for_derived_roots() -> None:
    project_id = uuid4()
    scope = QueryVisibilityScope(
        principal=None,
        project_ids=frozenset({project_id}),
        requested_project_id=project_id,
        requested_project_permitted=True,
    )

    statements = (
        select(MappedReaction.id).where(
            mapped_reaction_id_is_visible(scope, col(MappedReaction.id))
        ),
        select(LogicalReaction.id).where(
            logical_reaction_id_is_visible(scope, col(LogicalReaction.id))
        ),
        select(MolecularTopology.id).where(
            topology_id_is_visible(scope, col(MolecularTopology.id))
        ),
        select(MolecularFormula.id).where(formula_id_is_visible(scope, col(MolecularFormula.id))),
    )

    for statement in statements:
        compiled = _sql(statement)
        assert "project_id =" in compiled
        # Source-chain auditing is intentionally not repeated for every
        # project-scoped row.  0035--0037 make the ownership columns
        # immutable and reject cross-project writes at the database boundary.
        assert "artifact_ingestion.status" not in compiled
        assert "parse_revision.status" not in compiled


def test_frame_visibility_correlates_to_outer_frame_or_array_without_extra_outer_from() -> None:
    project_id = uuid4()
    scope = QueryVisibilityScope(
        principal=None,
        project_ids=frozenset({project_id}),
        requested_project_id=project_id,
        requested_project_permitted=True,
    )

    frame_statement = select(CalculationFrame.id).where(
        frame_id_is_visible(scope, col(CalculationFrame.id))
    )
    array_statement = select(ScientificArray.id).where(
        frame_id_is_visible(scope, col(ScientificArray.frame_id))
    )

    frame_sql = _sql(frame_statement)
    array_sql = _sql(array_statement)

    assert "visibility_fast_frame.id = calculation_frame.id" in frame_sql
    assert ", calculation_frame\n" not in frame_sql
    assert "visibility_fast_frame.id = scientific_array.frame_id" in array_sql
    assert ", scientific_array\n" not in array_sql
