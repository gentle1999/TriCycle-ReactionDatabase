from uuid import UUID, uuid4

import pytest

from tricycle_reaction_db.application.dtos import GeometryEnergyView
from tricycle_reaction_db.application.services.geometry_energy import GeometryEnergyComposite
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    EndpointComponentRequirement,
    GeometryThermodynamicCandidate,
    build_mapped_reaction_thermodynamics,
    format_composite_level_of_theory,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    _runtime_for_geometry_ids,
)


def _candidate(
    *,
    topology_id: UUID,
    enthalpy: float,
    gibbs: float,
    entropy: float | None,
    source: str = "level-a",
) -> GeometryThermodynamicCandidate:
    geometry_id = uuid4()
    view = GeometryEnergyView(
        geometry_id=geometry_id,
        policy_version="geometry-energy-view-v1",
        electronic_selection_status="selected",
        electronic_candidate_frame_ids=[],
        electronic_energy_hartree=enthalpy - 0.1,
        thermochemistry_selection_status="selected",
        thermochemistry_candidate_frame_ids=[],
        temperature_kelvin=298.15,
        pressure_atm=1.0,
        enthalpy_hartree=enthalpy,
        gibbs_free_energy_hartree=gibbs,
        entropy_cal_mol_k=entropy,
    )
    return GeometryThermodynamicCandidate(
        geometry_id=geometry_id,
        topology_id=topology_id,
        composite=GeometryEnergyComposite(
            view=view,
            electronic_level=(source, "electronic"),
            thermochemistry_level=(source, "thermochemistry"),
        ),
    )


def test_mapped_reaction_thermodynamics_uses_lowest_gibbs_geometry_per_component() -> None:
    mapped_reaction_id = uuid4()
    reactant_a_participant = uuid4()
    reactant_b_participant = uuid4()
    product_participant = uuid4()
    reactant_a = uuid4()
    reactant_b = uuid4()
    product = uuid4()
    transition_state_topology = uuid4()

    reactant_a_higher_gibbs = _candidate(
        topology_id=reactant_a,
        enthalpy=-9.0,
        gibbs=-10.0,
        entropy=1.0,
    )
    reactant_a_minimum = _candidate(
        topology_id=reactant_a,
        enthalpy=-10.0,
        gibbs=-11.0,
        entropy=2.0,
    )
    reactant_b_minimum = _candidate(
        topology_id=reactant_b,
        enthalpy=-19.0,
        gibbs=-20.0,
        entropy=3.0,
    )
    product_minimum = _candidate(
        topology_id=product,
        enthalpy=-30.0,
        gibbs=-32.0,
        entropy=6.0,
    )
    transition_state_higher_gibbs = _candidate(
        topology_id=transition_state_topology,
        enthalpy=-27.0,
        gibbs=-29.0,
        entropy=4.0,
    )
    transition_state_minimum = _candidate(
        topology_id=transition_state_topology,
        enthalpy=-28.5,
        gibbs=-30.0,
        entropy=4.0,
    )

    result = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=[
            EndpointComponentRequirement("reactant", reactant_a_participant, reactant_a, 1),
            EndpointComponentRequirement("reactant", reactant_b_participant, reactant_b, 1),
            EndpointComponentRequirement("product", product_participant, product, 1),
        ],
        candidates_by_component={
            reactant_a_participant: [reactant_a_higher_gibbs, reactant_a_minimum],
            reactant_b_participant: [reactant_b_minimum],
            product_participant: [product_minimum],
        },
        transition_state_candidates=[
            transition_state_higher_gibbs,
            transition_state_minimum,
        ],
    )

    assert result.mapped_reaction_id == mapped_reaction_id
    assert len(result.profiles) == 1
    profile = result.profiles[0]
    assert profile.mapped_reaction_id == mapped_reaction_id
    assert {
        selection.mapped_reaction_participant_id for selection in profile.reactants.topologies
    } == {reactant_a_participant, reactant_b_participant}
    assert {
        selection.topology_id: selection.geometry_id for selection in profile.reactants.topologies
    } == {
        reactant_a: reactant_a_minimum.geometry_id,
        reactant_b: reactant_b_minimum.geometry_id,
    }
    assert profile.transition_state.topologies[0].geometry_id == (
        transition_state_minimum.geometry_id
    )
    assert profile.reactants.enthalpy_hartree == pytest.approx(-29.0)
    assert profile.reactants.gibbs_free_energy_hartree == pytest.approx(-31.0)
    assert profile.reactants.entropy_cal_mol_k == pytest.approx(5.0)
    assert profile.activation.enthalpy_kcal_mol == pytest.approx(313.754737)
    assert profile.activation.gibbs_free_energy_kcal_mol == pytest.approx(627.509474)
    assert profile.activation.entropy_cal_mol_k == pytest.approx(-1.0)
    assert profile.reaction.enthalpy_kcal_mol == pytest.approx(-627.509474)
    assert profile.reaction.gibbs_free_energy_kcal_mol == pytest.approx(-627.509474)
    assert profile.reaction.entropy_cal_mol_k == pytest.approx(1.0)


def test_mapped_reaction_thermodynamics_rejects_incomplete_or_incompatible_sources() -> None:
    mapped_reaction_id = uuid4()
    reactant_participant = uuid4()
    product_participant = uuid4()
    reactant = uuid4()
    product = uuid4()
    transition_state_topology = uuid4()
    requirements = [
        EndpointComponentRequirement("reactant", reactant_participant, reactant, 1),
        EndpointComponentRequirement("product", product_participant, product, 1),
    ]
    transition_state = _candidate(
        topology_id=transition_state_topology,
        enthalpy=-9.0,
        gibbs=-10.0,
        entropy=2.0,
    )
    incomplete = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=requirements,
        candidates_by_component={
            reactant_participant: [
                _candidate(
                    topology_id=reactant,
                    enthalpy=-10.0,
                    gibbs=-11.0,
                    entropy=1.0,
                )
            ],
            product_participant: [
                _candidate(
                    topology_id=product,
                    enthalpy=-12.0,
                    gibbs=-13.0,
                    entropy=None,
                )
            ],
        },
        transition_state_candidates=[transition_state],
    )
    incompatible = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=requirements,
        candidates_by_component={
            reactant_participant: [
                _candidate(
                    topology_id=reactant,
                    enthalpy=-10.0,
                    gibbs=-11.0,
                    entropy=1.0,
                )
            ],
            product_participant: [
                _candidate(
                    topology_id=product,
                    enthalpy=-12.0,
                    gibbs=-13.0,
                    entropy=2.0,
                    source="level-b",
                )
            ],
        },
        transition_state_candidates=[transition_state],
    )

    assert len(incomplete.profiles) == 1
    assert incomplete.profiles[0].activation is not None
    assert incomplete.profiles[0].reaction is None
    assert incomplete.profiles[0].products is None
    assert len(incompatible.profiles) == 1
    assert incompatible.profiles[0].activation is not None
    assert incompatible.profiles[0].reaction is None
    assert incompatible.profiles[0].products is None


def test_mapped_reaction_thermodynamics_keeps_reaction_without_transition_state() -> None:
    mapped_reaction_id = uuid4()
    reactant_participant = uuid4()
    product_participant = uuid4()
    reactant_topology = uuid4()
    product_topology = uuid4()

    result = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=[
            EndpointComponentRequirement("reactant", reactant_participant, reactant_topology, 1),
            EndpointComponentRequirement("product", product_participant, product_topology, 1),
        ],
        candidates_by_component={
            reactant_participant: [
                _candidate(topology_id=reactant_topology, enthalpy=-10.0, gibbs=-11.0, entropy=1.0)
            ],
            product_participant: [
                _candidate(topology_id=product_topology, enthalpy=-12.0, gibbs=-13.0, entropy=2.0)
            ],
        },
        transition_state_candidates=[],
    )

    assert len(result.profiles) == 1
    profile = result.profiles[0]
    assert profile.products is not None
    assert profile.reaction is not None
    assert profile.transition_state is None
    assert profile.activation is None


def test_mapped_reaction_thermodynamics_keeps_all_source_keys() -> None:
    mapped_reaction_id = uuid4()
    reactant_participant = uuid4()
    product_participant = uuid4()
    reactant_topology = uuid4()
    product_topology = uuid4()
    transition_state_topology = uuid4()

    result = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=[
            EndpointComponentRequirement("reactant", reactant_participant, reactant_topology, 1),
            EndpointComponentRequirement("product", product_participant, product_topology, 1),
        ],
        candidates_by_component={
            reactant_participant: [
                _candidate(
                    topology_id=reactant_topology,
                    enthalpy=-10.0,
                    gibbs=-11.0,
                    entropy=1.0,
                    source="level-a",
                ),
                _candidate(
                    topology_id=reactant_topology,
                    enthalpy=-20.0,
                    gibbs=-21.0,
                    entropy=2.0,
                    source="level-b",
                ),
            ],
            product_participant: [
                _candidate(
                    topology_id=product_topology,
                    enthalpy=-12.0,
                    gibbs=-13.0,
                    entropy=2.0,
                    source="level-a",
                ),
                _candidate(
                    topology_id=product_topology,
                    enthalpy=-22.0,
                    gibbs=-23.0,
                    entropy=3.0,
                    source="level-b",
                ),
            ],
        },
        transition_state_candidates=[
            _candidate(
                topology_id=transition_state_topology,
                enthalpy=-9.0,
                gibbs=-10.0,
                entropy=2.0,
                source="level-a",
            ),
            _candidate(
                topology_id=transition_state_topology,
                enthalpy=-19.0,
                gibbs=-20.0,
                entropy=3.0,
                source="level-b",
            ),
        ],
    )

    assert len(result.profiles) == 2
    assert {tuple(profile.electronic_level) for profile in result.profiles} == {
        ("level-a", "electronic"),
        ("level-b", "electronic"),
    }


def test_composite_level_uses_raw_method_and_basis_in_thermochemistry_first_order() -> None:
    electronic = ("DFT", "DFT", None, "wB97M-V", "Def2TZVPP", None, None, None, None)
    thermochemistry = ("DFT", "DFT", None, "B3LYP-D3BJ", "Def2SVP", None, None, None, None)

    assert format_composite_level_of_theory(electronic, thermochemistry) == (
        "B3LYP-D3BJ/Def2SVP//wB97M-V/Def2TZVPP"
    )


def test_mapped_reaction_thermodynamics_ignores_candidates_outside_participant() -> None:
    mapped_reaction_id = uuid4()
    reactant_participant = uuid4()
    other_mapping_participant = uuid4()
    product_participant = uuid4()
    reactant_topology = uuid4()
    other_mapping_topology = uuid4()
    product_topology = uuid4()
    transition_state_topology = uuid4()
    expected_reactant = _candidate(
        topology_id=reactant_topology,
        enthalpy=-10.0,
        gibbs=-11.0,
        entropy=1.0,
    )
    wrong_mapping_candidate = _candidate(
        topology_id=other_mapping_topology,
        enthalpy=-20.0,
        gibbs=-21.0,
        entropy=1.0,
    )
    foreign_participant_candidate = _candidate(
        topology_id=reactant_topology,
        enthalpy=-30.0,
        gibbs=-31.0,
        entropy=1.0,
    )

    result = build_mapped_reaction_thermodynamics(
        mapped_reaction_id=mapped_reaction_id,
        endpoint_requirements=[
            EndpointComponentRequirement(
                "reactant",
                reactant_participant,
                reactant_topology,
                1,
            ),
            EndpointComponentRequirement(
                "product",
                product_participant,
                product_topology,
                1,
            ),
        ],
        candidates_by_component={
            reactant_participant: [wrong_mapping_candidate, expected_reactant],
            other_mapping_participant: [foreign_participant_candidate],
            product_participant: [
                _candidate(
                    topology_id=product_topology,
                    enthalpy=-12.0,
                    gibbs=-13.0,
                    entropy=2.0,
                )
            ],
        },
        transition_state_candidates=[
            _candidate(
                topology_id=transition_state_topology,
                enthalpy=-9.0,
                gibbs=-10.0,
                entropy=2.0,
            )
        ],
    )

    assert len(result.profiles) == 1
    assert result.profiles[0].reactants.topologies[0].geometry_id == (expected_reactant.geometry_id)


def test_profile_runtime_deduplicates_files_and_uses_latest_revision() -> None:
    shared_file = uuid4()
    second_file = uuid4()
    first_geometry = uuid4()
    second_geometry = uuid4()
    runtimes = {
        first_geometry: {
            shared_file: (1, 120.0),
            second_file: (1, 30.0),
        },
        second_geometry: {
            shared_file: (2, 125.0),
        },
    }

    assert _runtime_for_geometry_ids(
        {first_geometry, second_geometry},
        runtimes,
    ) == pytest.approx(155.0)
