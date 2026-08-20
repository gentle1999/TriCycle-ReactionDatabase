"""Element-count vector primitives used by formula identity and search."""

from collections.abc import Mapping, Sequence

ELEMENT_COUNT_VECTOR_SIZE = 118
ELEMENT_COUNT_VECTOR_SCHEMA_VERSION = "atomic-number-count-v1"


def element_count_vector_from_composition(
    composition: Sequence[Mapping[str, int]],
) -> list[int]:
    """Collapse isotope-specific composition into atomic-number counts."""

    vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
    for component in composition:
        atomic_number = component.get("atomic_number")
        count = component.get("count")
        if not isinstance(atomic_number, int) or not 1 <= atomic_number <= 118:
            raise ValueError("composition atomic_number must be between 1 and 118")
        if not isinstance(count, int) or count < 0:
            raise ValueError("composition count must be a non-negative integer")
        vector[atomic_number - 1] += count
    return vector


__all__ = [
    "ELEMENT_COUNT_VECTOR_SCHEMA_VERSION",
    "ELEMENT_COUNT_VECTOR_SIZE",
    "element_count_vector_from_composition",
]
