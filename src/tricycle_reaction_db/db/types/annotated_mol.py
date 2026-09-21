"""RDKit search graphs with separately persisted MolGR atom annotations."""

from molalchemy.rdkit.types import RdkitMol
from rdkit import Chem
from sqlalchemy import func

from tricycle_reaction_db.domain.mol_properties import restore_mol_atom_properties


class AnnotatedRdkitMol(RdkitMol):
    """Keep native mol comparisons while restoring annotations on every read.

    column_expression also covers scalar and aliased MOL selects, which ORM
    load hooks would miss. The PostgreSQL column itself remains type ``mol``.
    """

    cache_ok = True

    def column_expression(self, colexpr):
        return func.jsonb_build_array(
            func.encode(func.mol_send(colexpr), "hex"),
            colexpr.table.c.mol_atom_properties,
            type_=self,
        )

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None or value[0] is None:
                return None
            molecule = Chem.Mol(bytes.fromhex(value[0]))
            return restore_mol_atom_properties(molecule, value[1] or {})

        return process
