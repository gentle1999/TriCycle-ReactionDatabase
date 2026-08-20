import pytest
from pydantic import ValidationError

from tricycle_reaction_db.application.dtos import MolecularFormulaRangeQuery
from tricycle_reaction_db.domain.formulas import (
    ELEMENT_COUNT_VECTOR_SIZE,
    element_count_vector_from_composition,
)


def _unconstrained_ranges() -> tuple[list[int | None], list[int | None]]:
    return [None] * ELEMENT_COUNT_VECTOR_SIZE, [None] * ELEMENT_COUNT_VECTOR_SIZE


def test_element_count_vector_uses_atomic_number_positions_and_merges_isotopes() -> None:
    vector = element_count_vector_from_composition(
        [
            {"atomic_number": 1, "isotope": 0, "count": 4},
            {"atomic_number": 6, "isotope": 12, "count": 5},
            {"atomic_number": 6, "isotope": 13, "count": 1},
            {"atomic_number": 8, "isotope": 0, "count": 2},
        ]
    )

    assert len(vector) == ELEMENT_COUNT_VECTOR_SIZE
    assert vector[0] == 4
    assert vector[5] == 6
    assert vector[7] == 2
    assert sum(vector) == 12
    assert sum(vector[1:5]) == 0


@pytest.mark.parametrize(
    ("atomic_number", "count"),
    [(0, 1), (119, 1), (6, -1)],
)
def test_element_count_vector_rejects_invalid_composition_values(
    atomic_number: int,
    count: int,
) -> None:
    with pytest.raises(ValueError):
        element_count_vector_from_composition(
            [{"atomic_number": atomic_number, "isotope": 0, "count": count}]
        )


def test_formula_range_query_requires_a_constraint_and_validates_bounds() -> None:
    minimum, maximum = _unconstrained_ranges()
    with pytest.raises(ValidationError, match="at least one constrained"):
        MolecularFormulaRangeQuery(minimum_counts=minimum, maximum_counts=maximum)

    minimum[5] = 7
    maximum[5] = 6
    with pytest.raises(ValidationError, match="cannot exceed"):
        MolecularFormulaRangeQuery(minimum_counts=minimum, maximum_counts=maximum)

    minimum[5] = -1
    maximum[5] = 6
    with pytest.raises(ValidationError, match="non-negative"):
        MolecularFormulaRangeQuery(minimum_counts=minimum, maximum_counts=maximum)


def test_formula_range_query_accepts_inclusive_per_element_bounds() -> None:
    minimum, maximum = _unconstrained_ranges()
    minimum[5] = 6
    maximum[0] = 14
    query = MolecularFormulaRangeQuery(minimum_counts=minimum, maximum_counts=maximum)

    assert query.minimum_counts[5] == 6
    assert query.maximum_counts[0] == 14
