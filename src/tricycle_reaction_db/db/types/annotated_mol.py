"""RDKit search graphs with separately persisted MolGR atom annotations."""

from functools import partial
from typing import Any

from molalchemy.rdkit.types import RdkitMol
from rdkit import Chem
from sqlalchemy import func
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement

from tricycle_reaction_db.domain.explicit_hydrogens import require_explicit_hydrogens
from tricycle_reaction_db.domain.mol_properties import restore_mol_atom_properties


class AnnotatedRdkitMol(RdkitMol):
    """Keep native mol comparisons while restoring annotations on every read.

    column_expression also covers scalar and aliased MOL selects, which ORM
    load hooks would miss. The PostgreSQL column itself remains type ``mol``.
    """

    cache_ok = True

    def bind_processor(self, dialect: Dialect) -> Any:
        parent = super().bind_processor(dialect)  # type: ignore[no-untyped-call]

        def process(value: Any) -> Any:
            if value is not None:
                if not isinstance(value, Chem.Mol):
                    raise ValueError("persisted MOL requires an explicit-hydrogen RDKit molecule")
                require_explicit_hydrogens(value)
            return parent(value)

        return process

    def column_expression(self, colexpr: Any) -> ColumnElement[Any]:
        return func.jsonb_build_array(
            func.encode(func.mol_send(colexpr), "hex"),
            colexpr.table.c.mol_atom_properties,
            type_=self,
        )

    def result_processor(self, dialect: Dialect, coltype: object) -> partial[Chem.Mol | None]:
        def process(value: Any) -> Chem.Mol | None:
            if value is None or value[0] is None:
                return None
            # RDKit accepts binary pickles, but its stubs only list str.
            molecule = Chem.Mol(bytes.fromhex(value[0]))  # type: ignore[call-overload]
            return restore_mol_atom_properties(molecule, value[1] or {})

        return partial(process)
