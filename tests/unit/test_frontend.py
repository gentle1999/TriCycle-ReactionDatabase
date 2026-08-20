import pytest
from httpx import ASGITransport, AsyncClient
from rdkit import Chem

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.application.services.depictions import (
    draw_geometry_dof_svg,
    draw_geometry_sdf,
    draw_molecule_molfile,
    draw_molecule_svg,
)


def test_rdkit_depiction_returns_svg_without_mutating_molecule() -> None:
    molecule = Chem.MolFromSmiles("C1=CC=CC=C1")
    assert molecule is not None
    original_smiles = Chem.MolToSmiles(molecule)

    svg = draw_molecule_svg(molecule)

    assert svg.startswith("<?xml")
    assert "<svg" in svg
    assert "bond-" in svg
    assert Chem.MolToSmiles(molecule) == original_smiles


def test_rdkit_molfile_has_2d_coordinates_without_mutating_molecule() -> None:
    molecule = Chem.MolFromSmiles("C1=CC=CC=C1")
    assert molecule is not None
    original_smiles = Chem.MolToSmiles(molecule)
    original_conformer_count = molecule.GetNumConformers()

    molfile = draw_molecule_molfile(molecule)
    rendered = Chem.MolFromMolBlock(molfile, removeHs=False)

    assert rendered is not None
    assert rendered.GetNumConformers() == 1
    assert any(
        abs(value) > 1e-6
        for atom_index in range(rendered.GetNumAtoms())
        for value in (
            rendered.GetConformer().GetAtomPosition(atom_index).x,
            rendered.GetConformer().GetAtomPosition(atom_index).y,
        )
    )
    assert Chem.MolToSmiles(molecule) == original_smiles
    assert molecule.GetNumConformers() == original_conformer_count


def test_geometry_sdf_preserves_stored_three_dimensional_conformer() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    conformer = Chem.Conformer(molecule.GetNumAtoms())
    conformer.Set3D(True)
    for atom_index in range(molecule.GetNumAtoms()):
        conformer.SetAtomPosition(
            atom_index,
            (float(atom_index), float(atom_index % 2), float(atom_index) / 3.0),
        )
    molecule.AddConformer(conformer)
    expected_positions = molecule.GetConformer().GetPositions().copy()

    sdf = draw_geometry_sdf(molecule)
    rendered = Chem.MolFromMolBlock(sdf.split("$$$$", maxsplit=1)[0], removeHs=False)

    assert sdf.rstrip().endswith("$$$$")
    assert rendered is not None
    assert rendered.GetConformer().Is3D()
    assert rendered.GetConformer().GetPositions() == pytest.approx(expected_positions, abs=1e-4)
    assert molecule.GetConformer().GetPositions() == pytest.approx(expected_positions)


def test_geometry_dof_svg_uses_stored_depth_without_mutating_molecule() -> None:
    molecule = Chem.AddHs(Chem.MolFromSmiles("CO"))
    conformer = Chem.Conformer(molecule.GetNumAtoms())
    conformer.Set3D(True)
    for atom_index in range(molecule.GetNumAtoms()):
        conformer.SetAtomPosition(
            atom_index,
            (float(atom_index), float(atom_index % 2), float(atom_index) / 3.0),
        )
    molecule.AddConformer(conformer)
    expected_positions = molecule.GetConformer().GetPositions().copy()

    svg = draw_geometry_dof_svg(molecule, width=320, height=210)

    assert svg.startswith("<?xml")
    assert "<svg" in svg
    assert "opacity" in svg
    assert molecule.GetConformer().GetPositions() == pytest.approx(expected_positions)


@pytest.mark.asyncio
async def test_fastapi_does_not_serve_frontend_or_static_assets() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/")
        asset = await client.get("/assets/index.js")

    assert page.status_code == 404
    assert asset.status_code == 404


def test_combined_app_exposes_core_rest_routes() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/api/logical-reactions" in paths
    assert "/api/artifacts/{artifact_id}/preview" in paths
    assert "/api/artifacts/{artifact_id}/download" in paths
    assert "/api/auth/me" in paths
    assert "/api/auth/mcp-tokens" in paths
    assert "/api/auth/mcp-tokens/{token_id}" in paths
    assert "/api/mapped-reactions/{mapped_reaction_id}" in paths
    assert "/api/mapped-reactions/thermodynamics/statistics" in paths
    assert "/api/mapped-reactions/thermodynamics/export.csv" in paths
    assert "/api/calculation-frames" in paths
    assert "/api/depictions/geometry/{geometry_id}.sdf" in paths
    assert "/api/depictions/geometry/{geometry_id}.svg" in paths
    assert "/api/artifacts" in paths
    assert "/api/artifacts/batch" in paths
    assert "/api/artifacts/validate" in paths
    assert "/voyager" in paths
    assert "/api/artifacts/{artifact_id}/reparse" in paths
    assert "/api/chemistry/reactions" in paths
    assert "/api/chemistry/reactions/validate" in paths


@pytest.mark.asyncio
async def test_chemistry_representation_conversion_round_trips_smiles_and_molfile() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        smiles_response = await client.post(
            "/api/chemistry/representations",
            json={"smiles": "CCO"},
        )
        assert smiles_response.status_code == 200
        payload = smiles_response.json()
        assert payload["smiles"] == "CCO"
        assert "V2000" in payload["molfile"]

        molfile_response = await client.post(
            "/api/chemistry/representations",
            json={"molfile": payload["molfile"]},
        )

    assert molfile_response.status_code == 200
    assert molfile_response.json()["smiles"] == "CCO"


@pytest.mark.asyncio
async def test_reaction_representation_conversion_round_trips_reaction_smiles_and_rxn() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        smiles_response = await client.post(
            "/api/chemistry/reactions",
            json={"reaction_smiles": "C=C>>CC"},
        )
        assert smiles_response.status_code == 200
        payload = smiles_response.json()
        assert payload["reaction_smiles"] == "C=C>>CC"
        assert payload["rxn"].startswith("$RXN")

        rxn_response = await client.post(
            "/api/chemistry/reactions",
            json={"rxn": payload["rxn"]},
        )

    assert rxn_response.status_code == 200
    assert rxn_response.json()["reaction_smiles"] == "C=C>>CC"


@pytest.mark.asyncio
async def test_chemistry_validation_endpoint_covers_smiles_smarts_and_reaction_forms() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cases = [
            ("smiles", "CCO", True),
            ("smarts", "[C;H3][O;H1]", True),
            ("rxn_smiles", "C=C>>CC", True),
            ("rxn_smarts", "[C:1]=[C:2]>>[C:1]-[C:2]", True),
            ("smiles", "not a molecule", False),
            ("rxn_smarts", "C>>", False),
        ]
        for kind, value, expected in cases:
            response = await client.post(
                "/api/chemistry/reactions/validate",
                json={"kind": kind, "value": value},
            )
            assert response.status_code == 200
            assert response.json()["valid"] is expected
