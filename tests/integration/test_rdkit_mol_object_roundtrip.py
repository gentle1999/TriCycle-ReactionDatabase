import os
from collections.abc import Iterator

import pytest
import rdkit
from molalchemy.rdkit.types import RdkitMol
from pydantic import ConfigDict
from rdkit import Chem
from rdkit.Geometry import Point3D
from sqlalchemy import Engine, create_engine, text
from sqlmodel import Field, Session, SQLModel, select

from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


class RdkitMolObjectProbe(SQLModel, table=True):
    __tablename__ = "_m1_rdkit_mol_object_probe"
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int | None = Field(default=None, primary_key=True)
    structure: Chem.Mol = Field(
        sa_type=RdkitMol(return_type="mol"),
        nullable=False,
    )


@pytest.fixture
def database_engine() -> Iterator[Engine]:
    database_engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    table = RdkitMolObjectProbe.__table__

    try:
        with database_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS rdkit"))
            table.drop(connection, checkfirst=True)
            table.create(connection)
        yield database_engine
    finally:
        with database_engine.begin() as connection:
            table.drop(connection, checkfirst=True)
        database_engine.dispose()


def _round_trip(database_engine: Engine, molecules: list[Chem.Mol]) -> list[Chem.Mol]:
    with Session(database_engine) as session:
        session.add_all(RdkitMolObjectProbe(structure=molecule) for molecule in molecules)
        session.commit()
        records = session.exec(select(RdkitMolObjectProbe).order_by(RdkitMolObjectProbe.id)).all()
        return [Chem.Mol(record.structure) for record in records]


def _atom_signature(molecule: Chem.Mol) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            atom.GetIdx(),
            atom.GetAtomicNum(),
            atom.GetIsotope(),
            atom.GetFormalCharge(),
            atom.GetNumRadicalElectrons(),
            int(atom.GetChiralTag()),
            atom.GetAtomMapNum(),
            atom.GetIsAromatic(),
            atom.IsInRing(),
            atom.GetNoImplicit(),
            atom.GetNumExplicitHs(),
            atom.GetNumImplicitHs(),
        )
        for atom in molecule.GetAtoms()
    )


def _bond_signature(molecule: Chem.Mol) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            bond.GetIdx(),
            bond.GetBeginAtomIdx(),
            bond.GetEndAtomIdx(),
            str(bond.GetBondType()),
            bond.GetIsAromatic(),
            bond.GetIsConjugated(),
            bond.IsInRing(),
            int(bond.GetBondDir()),
            int(bond.GetStereo()),
            tuple(bond.GetStereoAtoms()),
        )
        for bond in molecule.GetBonds()
    )


def _stereo_signature(molecule: Chem.Mol) -> tuple[object, ...]:
    molecule = Chem.Mol(molecule)
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    atom_cip = tuple(
        (atom.GetIdx(), atom.GetProp("_CIPCode"))
        for atom in molecule.GetAtoms()
        if atom.HasProp("_CIPCode")
    )
    bond_stereo = tuple(
        (bond.GetIdx(), int(bond.GetStereo()), tuple(bond.GetStereoAtoms()))
        for bond in molecule.GetBonds()
        if bond.GetStereo() != Chem.BondStereo.STEREONONE
    )
    stereo_groups = tuple(
        (str(group.GetGroupType()), tuple(atom.GetIdx() for atom in group.GetAtoms()))
        for group in molecule.GetStereoGroups()
    )
    return atom_cip, bond_stereo, stereo_groups


def test_python_and_postgresql_rdkit_toolkit_versions_match(database_engine: Engine) -> None:
    with database_engine.connect() as connection:
        postgresql_rdkit_version = connection.execute(
            text("SELECT rdkit_toolkit_version()")
        ).scalar_one()

    assert postgresql_rdkit_version == rdkit.__version__


def test_python_mol_round_trip_preserves_chemical_graph(database_engine: Engine) -> None:
    smiles_samples = [
        "F[C@H](Cl)Br",
        "F/C=C/Cl",
        "[13CH3:1][C@@H:2]([OH:3])[NH3+:4]",
        "[CH2]C",
        "c1cc[nH]c1",
        "[2H][C@](F)(Cl)Br",
        "[NH3:1]->[Cu+2:2]",
        "F[C@H](Cl)[C@H](Br)I |&1:1,3|",
    ]
    source_molecules = [Chem.MolFromSmiles(smiles) for smiles in smiles_samples]
    assert all(molecule is not None for molecule in source_molecules)

    source_molecules = [molecule for molecule in source_molecules if molecule is not None]
    loaded_molecules = _round_trip(database_engine, source_molecules)

    assert len(loaded_molecules) == len(source_molecules)
    for source, loaded in zip(source_molecules, loaded_molecules, strict=True):
        assert isinstance(loaded, Chem.Mol)
        assert Chem.MolToCXSmiles(loaded, canonical=False) == Chem.MolToCXSmiles(
            source, canonical=False
        )
        assert Chem.MolToCXSmiles(loaded, canonical=True) == Chem.MolToCXSmiles(
            source, canonical=True
        )
        assert _atom_signature(loaded) == _atom_signature(source)
        assert _bond_signature(loaded) == _bond_signature(source)
        assert _stereo_signature(loaded) == _stereo_signature(source)


def test_conformers_are_approximate_and_custom_properties_are_not_persisted(
    database_engine: Engine,
) -> None:
    source = Chem.MolFromSmiles("[13CH3:7][C@H:8](F)/C=C\\[2H:9]")
    assert source is not None

    source.SetProp("workflow_label", "gaussian-ts")
    source.GetAtomWithIdx(0).SetDoubleProp("partial_charge", -0.125)
    source.GetBondWithIdx(0).SetProp("bond_annotation", "forming")

    for conformer_id, offset in ((17, 0.0), (29, 1.25)):
        conformer = Chem.Conformer(source.GetNumAtoms())
        conformer.SetId(conformer_id)
        conformer.Set3D(True)
        conformer.SetProp("source_geometry", f"gaussian-{conformer_id}")
        for atom_index in range(source.GetNumAtoms()):
            conformer.SetAtomPosition(
                atom_index,
                Point3D(
                    offset + atom_index * 0.123456789,
                    offset - atom_index * 0.234567891,
                    offset + atom_index * 0.345678912,
                ),
            )
        source.AddConformer(conformer, assignId=False)

    loaded = _round_trip(database_engine, [source])[0]

    assert loaded.GetNumConformers() == source.GetNumConformers()
    for source_conformer, loaded_conformer in zip(
        source.GetConformers(), loaded.GetConformers(), strict=True
    ):
        assert loaded_conformer.GetId() == source_conformer.GetId()
        assert loaded_conformer.Is3D() == source_conformer.Is3D()
        for atom_index in range(source.GetNumAtoms()):
            source_position = source_conformer.GetAtomPosition(atom_index)
            loaded_position = loaded_conformer.GetAtomPosition(atom_index)
            assert loaded_position.x == pytest.approx(source_position.x, abs=1e-6)
            assert loaded_position.y == pytest.approx(source_position.y, abs=1e-6)
            assert loaded_position.z == pytest.approx(source_position.z, abs=1e-6)

    assert not loaded.HasProp("workflow_label")
    assert not loaded.GetAtomWithIdx(0).HasProp("partial_charge")
    assert not loaded.GetBondWithIdx(0).HasProp("bond_annotation")
    assert all(not conformer.HasProp("source_geometry") for conformer in loaded.GetConformers())
