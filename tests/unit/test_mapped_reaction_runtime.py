from uuid import uuid4

from tricycle_reaction_db.application.services.mapped_reaction_runtime import (
    aggregate_mapped_reaction_runtimes,
)


def test_runtime_counts_all_candidates_and_deduplicates_stages_and_reparses() -> None:
    mapping, empty_mapping = uuid4(), uuid4()
    precursor, second_precursor, ts_low, ts_high, product = [uuid4() for _ in range(5)]
    values = aggregate_mapped_reaction_runtimes(
        [mapping, empty_mapping],
        [
            (mapping, "reactants", precursor, 1, 10.0),
            (mapping, "reactants", precursor, 2, 15.0),
            (mapping, "reactants", precursor, 2, 15.0),
            (mapping, "reactants", second_precursor, 1, 20.0),
            (mapping, "transition_state", ts_low, 1, 30.0),
            (mapping, "transition_state", ts_high, 1, 40.0),
            (mapping, "products", product, 1, 50.0),
            (mapping, "products", precursor, 2, 15.0),
        ],
    )
    assert values[mapping] == {
        "reactants_running_time_seconds": 35.0,
        "transition_state_running_time_seconds": 70.0,
        "products_running_time_seconds": 65.0,
        "total_running_time_seconds": 155.0,
    }
    assert all(value is None for value in values[empty_mapping].values())


def test_missing_candidate_runtime_is_not_silently_ignored() -> None:
    mapping, precursor, unknown, ts = [uuid4() for _ in range(4)]
    values = aggregate_mapped_reaction_runtimes(
        [mapping],
        [
            (mapping, "reactants", precursor, 1, 10.0),
            (mapping, "reactants", unknown, 1, 20.0),
            (mapping, "reactants", unknown, 2, None),
            (mapping, "transition_state", ts, 1, 30.0),
        ],
    )[mapping]
    assert values["reactants_running_time_seconds"] is None
    assert values["total_running_time_seconds"] is None
    assert values["transition_state_running_time_seconds"] == 30.0
