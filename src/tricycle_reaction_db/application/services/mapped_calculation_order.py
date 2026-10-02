"""One permutation for all atom-indexed scientific data in a mapped export."""

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from tricycle_reaction_db.domain.enums import ScientificArrayKind as Kind

_ATOM_AXES = {
    Kind.FORCES: (0,),
    Kind.NORMAL_MODES: (1,),
    Kind.ATOMIC_POPULATION: (0,),
    Kind.BOND_ORDER_MATRIX: (0, 1),
    Kind.FUKUI_POSITIVE: (0,),
    Kind.FUKUI_NEGATIVE: (0,),
    Kind.FUKUI_ZERO: (0,),
    Kind.FRACTIONAL_OCCUPATION_DENSITY: (0,),
}
_COUPLINGS = {
    Kind.NMR_COUPLING_K,
    Kind.NMR_COUPLING_J,
    Kind.NMR_COUPLING_K_COMPONENT,
    Kind.NMR_COUPLING_J_COMPONENT,
}


@dataclass(frozen=True)
class MappedCalculationOrder:
    """Source-frame index -> map-1, composed through the stored Geometry link."""

    source_to_mapped: tuple[int, ...]

    def __post_init__(self) -> None:
        if sorted(self.source_to_mapped) != list(range(len(self.source_to_mapped))):
            raise ValueError("source-to-mapped atom order must be a complete permutation")

    @classmethod
    def from_geometry(
        cls, source_to_geometry: list[int], geometry_maps: list[int]
    ) -> "MappedCalculationOrder":
        count = len(geometry_maps)
        if sorted(source_to_geometry) != list(range(count)):
            raise ValueError("frame-to-Geometry atom order must be a complete permutation")
        return cls(tuple(geometry_maps[index] - 1 for index in source_to_geometry))

    @property
    def order(self) -> list[int]:
        return sorted(range(len(self.source_to_mapped)), key=self.source_to_mapped.__getitem__)

    def atom_indices(self, indices: list[int]) -> list[int]:
        if any(index < 0 or index >= len(self.source_to_mapped) for index in indices):
            raise ValueError("property references an atom outside the source frame")
        return [self.source_to_mapped[index] for index in indices]

    def array(self, data: Any, *, axes: tuple[int, ...]) -> npt.NDArray[Any]:
        result = np.asarray(data)
        for axis in axes:
            if axis >= result.ndim or result.shape[axis] != len(self.order):
                raise ValueError("scientific array atom axis does not match the source frame")
            result = np.take(result, self.order, axis=axis)
        return result.copy()

    def scientific_array(
        self,
        kind: Kind,
        data: Any,
        *,
        coupling_atom_indices: list[int] | None = None,
    ) -> tuple[npt.NDArray[Any], dict[str, Any]]:
        if kind is Kind.HESSIAN:
            value = np.asarray(data)
            if value.shape != (3 * len(self.order), 3 * len(self.order)):
                raise ValueError("Hessian must have shape (3N, 3N)")
            cartesian_order = [3 * index + axis for index in self.order for axis in range(3)]
            return value[np.ix_(cartesian_order, cartesian_order)], {
                "axis_order": ["mapped_atom_xyz", "mapped_atom_xyz"]
            }
        if kind in _COUPLINGS:
            if coupling_atom_indices is None:
                raise ValueError("NMR coupling export requires its source atom indices")
            mapped = self.atom_indices(coupling_atom_indices)
            if len(set(mapped)) != len(mapped):
                raise ValueError("NMR coupling atom indices must be unique")
            order = sorted(range(len(mapped)), key=mapped.__getitem__)
            value = np.asarray(data)
            if value.shape != (len(mapped), len(mapped)):
                raise ValueError("NMR coupling dimensions do not match its atom subset")
            return value[np.ix_(order, order)], {"atom_indices": sorted(mapped)}
        axes = _ATOM_AXES.get(kind, ())
        return self.array(data, axes=axes), {"atom_axes": list(axes)}
