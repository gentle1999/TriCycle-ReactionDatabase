from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest
from rdkit import Chem

from tricycle_reaction_db.application.services import molecular_geometry
from tricycle_reaction_db.application.services.molecular_geometry import (
    _coordinate_alignment,
)
from tricycle_reaction_db.ingestion.normalization import normalize_topology


def _alternate_projection_record():
    record = normalize_topology(
        Chem.MolFromSmiles("CCO"),
        add_hydrogens=True,
        reconstruction_method="test",
        reconstruction_version="test",
    )
    alternate_topology = record.topology.model_copy(
        update={
            "canonical_isomeric_smiles": "projection-specific-smiles",
            "heavy_atom_count": record.topology.heavy_atom_count + 1,
            "formal_charge": record.topology.formal_charge + 1,
            "stereo_status": record.topology.stereo_status,
        }
    )
    return record.model_copy(update={"topology": alternate_topology})


def test_cached_topology_allows_an_alternate_graph_projection() -> None:
    record = _alternate_projection_record()
    formula_id = UUID("00000000-0000-7000-8000-000000000401")
    persisted = molecular_geometry.PersistedMolecularTopology(
        formula=SimpleNamespace(
            id=formula_id,
            composition_hash=record.formula.composition_hash,
        ),
        topology=SimpleNamespace(
            formula_id=formula_id,
            identity_schema_version=record.topology.identity_schema_version,
            graph_hash=record.topology.graph_hash,
            canonical_isomeric_smiles=record.topology.canonical_isomeric_smiles,
        ),
        topology_derivation=record.topology_derivation,
    )

    molecular_geometry._validate_cached_topology(persisted, record)


def test_database_topology_reuses_identity_with_an_alternate_projection(monkeypatch) -> None:
    record = _alternate_projection_record()
    formula_id = UUID("00000000-0000-7000-8000-000000000402")
    topology_id = UUID("00000000-0000-7000-8000-000000000403")
    formula = SimpleNamespace(id=formula_id, composition_hash=record.formula.composition_hash)
    topology = SimpleNamespace(
        id=topology_id,
        formula_id=formula_id,
        identity_schema_version=record.topology.identity_schema_version,
        graph_hash=record.topology.graph_hash,
        canonical_isomeric_smiles="different-persisted-projection",
        atom_count=record.topology.atom_count,
        heavy_atom_count=record.topology.heavy_atom_count,
        formal_charge=record.topology.formal_charge,
        radical_electron_count=record.topology.radical_electron_count,
        fragment_count=record.topology.fragment_count,
        stereo_status=record.topology.stereo_status,
        sanitization_status=record.topology.sanitization_status,
        sanitization_error=record.topology.sanitization_error,
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def first(self):
            return self.value

    class Session:
        def __init__(self):
            self.results = iter(
                [
                    Result(formula),
                    Result(topology),
                    Result(SimpleNamespace(**record.topology_derivation.model_dump())),
                ]
            )

        def exec(self, _statement):
            return next(self.results)

    monkeypatch.setattr(molecular_geometry, "_acquire_identity_locks", lambda *_args: None)

    persisted = molecular_geometry.persist_molecular_topology(Session(), record)

    assert persisted.topology is topology
    assert persisted.topology.id == topology_id


def test_geometry_error_removes_translation_and_proper_rotation() -> None:
    reference = np.asarray(
        [[0.0, 0.0, 0.0], [1.2, 0.1, 0.0], [-0.2, 0.9, 0.4]],
        dtype=np.float64,
    )
    angle = np.deg2rad(63.0)
    rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    observed = reference @ rotation + np.asarray([7.0, -3.5, 2.25])

    rmsd, max_abs, transform_values = _coordinate_alignment(observed, reference)
    transform = np.asarray(transform_values).reshape(4, 4)
    homogeneous = np.column_stack((observed, np.ones(observed.shape[0])))
    aligned = (transform @ homogeneous.T).T[:, :3]

    assert rmsd < 1e-12
    assert max_abs < 1e-12
    assert aligned == pytest.approx(reference, abs=1e-12)
    assert np.linalg.det(transform[:3, :3]) == pytest.approx(1.0)
