"""Molecular representations derived from stored topology and geometry molecules."""

from io import StringIO
from typing import Any, Literal, cast
from uuid import UUID

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D
from rdkit.Geometry import Point3D
from rdkit_dof import DofDrawSettings, MolToDofImage  # type: ignore[import-untyped]
from sqlalchemy.orm import undefer
from sqlmodel import col, select

from tricycle_reaction_db.application.services.query_visibility import (
    frame_id_is_visible,
    geometry_id_is_visible,
    query_visibility_scope,
    topology_id_is_visible,
)
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    Geometry,
    MolecularTopology,
    TransitionStateEndpoint,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import TransitionStateEndpointDirection

_CARD_BACKGROUND = (248 / 255, 250 / 255, 247 / 255)
_GEOMETRY_DOF_SETTINGS = DofDrawSettings(
    preset_style="default",
    fog_color=_CARD_BACKGROUND,
    min_alpha=0.32,
    default_size=(480, 320),
    enable_ipython=False,
    env_file=None,
)


def draw_molecule_svg(molecule: Chem.Mol, *, width: int = 360, height: int = 220) -> str:
    """Render a topology graph without changing the persisted RDKit molecule."""

    drawable = Chem.Mol(molecule)
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    options: Any = drawer.drawOptions()
    options.clearBackground = False
    options.padding = 0.08
    options.bondLineWidth = 1.8
    options.addStereoAnnotation = True
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, drawable)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def draw_molecule_molfile(molecule: Chem.Mol) -> str:
    """Create a 2D molfile for browser rendering without mutating stored topology."""

    drawable = Chem.Mol(molecule)
    drawable.RemoveAllConformers()
    rdDepictor.Compute2DCoords(drawable, canonOrient=True)
    return Chem.MolToMolBlock(drawable, includeStereo=True, kekulize=False)


def draw_geometry_sdf(
    molecule: Chem.Mol,
    *,
    charge: int | None = None,
    multiplicity: int | None = None,
) -> str:
    """Serialize one conformer and, when available, its global electronic state."""

    if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
        raise ValueError("Geometry.mol must contain exactly one three-dimensional conformer")
    drawable = Chem.Mol(molecule)
    if charge is not None:
        drawable.SetIntProp("total_charge", int(charge))
    if multiplicity is not None:
        if multiplicity < 1:
            raise ValueError("spin multiplicity must be positive")
        drawable.SetIntProp("spin_multiplicity", int(multiplicity))
    buffer = StringIO()
    writer = Chem.SDWriter(buffer)
    writer.SetKekulize(False)
    writer.write(drawable)
    writer.close()
    return buffer.getvalue()


def draw_geometry_xyz(
    molecule: Chem.Mol,
    *,
    charge: int | None = None,
    multiplicity: int | None = None,
) -> str:
    """Serialize one stored conformer and its global charge/spin as XYZ text."""

    if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
        raise ValueError("Geometry.mol must contain exactly one three-dimensional conformer")
    if multiplicity is not None and multiplicity < 1:
        raise ValueError("spin multiplicity must be positive")
    conformer = molecule.GetConformer()
    total_charge = (
        charge
        if charge is not None
        else sum(atom.GetFormalCharge() for atom in molecule.GetAtoms())
    )
    total_spin = (multiplicity - 1) / 2 if multiplicity is not None else None
    total_spin_text = (
        str(int(total_spin))
        if total_spin is not None and total_spin.is_integer()
        else str(total_spin)
    )
    coordinates = [
        (
            atom.GetSymbol(),
            conformer.GetAtomPosition(atom_index),
        )
        for atom_index, atom in enumerate(molecule.GetAtoms())
    ]
    lines = [
        str(molecule.GetNumAtoms()),
        (
            "Geometry conformer; canonical topology atom order; coordinates in angstrom; "
            f"total_charge={total_charge}; "
            f"spin_multiplicity={multiplicity if multiplicity is not None else 'unavailable'}; "
            f"total_spin_S={total_spin_text if total_spin is not None else 'unavailable'}"
        ),
        *(
            f"{element} {position.x:.8f} {position.y:.8f} {position.z:.8f}"
            for element, position in coordinates
        ),
    ]
    return "\n".join(lines) + "\n"


def _source_order_molecule(
    topology_molecule: Chem.Mol,
    source_coordinates: object,
    source_to_topology_atom_indices: list[int],
) -> Chem.Mol:
    """Attach source-order coordinates to a canonical reusable topology."""

    atom_count = topology_molecule.GetNumAtoms()
    if sorted(source_to_topology_atom_indices) != list(range(atom_count)):
        raise ValueError("source-to-topology atom indices must be a full permutation")
    coordinates = np.asarray(source_coordinates, dtype=np.float64)
    if coordinates.shape != (atom_count, 3) or not np.isfinite(coordinates).all():
        raise ValueError("source coordinates do not match the topology atom count")
    source_ordered = Chem.RenumberAtoms(
        Chem.Mol(topology_molecule),
        source_to_topology_atom_indices,
    )
    source_ordered.RemoveAllConformers()
    conformer = Chem.Conformer(atom_count)
    conformer.SetId(0)
    conformer.Set3D(True)
    for atom_index, (x, y, z) in enumerate(coordinates):
        conformer.SetAtomPosition(atom_index, Point3D(float(x), float(y), float(z)))
    source_ordered.AddConformer(conformer, assignId=False)
    return source_ordered


def draw_geometry_dof_svg(
    molecule: Chem.Mol,
    *,
    width: int = 480,
    height: int = 320,
) -> str:
    """Render one stored 3D conformer as an rdkit-dof depth SVG."""

    if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
        raise ValueError("Geometry.mol must contain exactly one three-dimensional conformer")
    rendered = MolToDofImage(
        Chem.Mol(molecule),
        size=(width, height),
        use_svg=True,
        return_image=False,
        settings=_GEOMETRY_DOF_SETTINGS,
        clearBackground=False,
        padding=0.06,
        bondLineWidth=1.8,
    )
    if not isinstance(rendered, str):
        raise TypeError("rdkit-dof did not return SVG text")
    return rendered


async def _get_topology_molecule(
    topology_id: UUID,
    project_id: UUID | None = None,
) -> Chem.Mol | None:
    scope = await query_visibility_scope(project_id=project_id)
    async with session_factory() as session:
        return (
            await session.execute(
                select(MolecularTopology.mol).where(
                    col(MolecularTopology.id) == topology_id,
                    topology_id_is_visible(scope, col(MolecularTopology.id)),
                )
            )
        ).scalar_one_or_none()


async def _get_geometry_molecule(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> Chem.Mol | None:
    scope = await query_visibility_scope(project_id=project_id)
    async with session_factory() as session:
        return (
            await session.execute(
                select(Geometry.mol).where(
                    col(Geometry.id) == geometry_id,
                    geometry_id_is_visible(scope, col(Geometry.id)),
                )
            )
        ).scalar_one_or_none()


async def get_topology_depiction(
    topology_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Load one visible topology and return an SVG depiction."""

    molecule = await _get_topology_molecule(topology_id, project_id=project_id)
    if molecule is None:
        return None
    return draw_molecule_svg(molecule)


async def get_topology_molfile(
    topology_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Load one visible topology and return a ChemDoodle-compatible molfile."""

    molecule = await _get_topology_molecule(topology_id, project_id=project_id)
    if molecule is None:
        return None
    return draw_molecule_molfile(molecule)


async def get_geometry_sdf(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Load one stored Geometry.mol and preserve its conformer in an SDF record."""

    molecule = await _get_geometry_molecule(geometry_id, project_id=project_id)
    if molecule is None:
        return None
    return draw_geometry_sdf(molecule)


async def _get_geometry_xyz_export(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> tuple[Chem.Mol, int, int] | None:
    """Load a visible Geometry and its persisted electronic state."""

    scope = await query_visibility_scope(project_id=project_id)
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Geometry.mol, Geometry.charge, Geometry.multiplicity).where(
                    col(Geometry.id) == geometry_id,
                    geometry_id_is_visible(scope, col(Geometry.id)),
                )
            )
        ).one_or_none()
        if row is None:
            return None
    molecule, charge, multiplicity = row
    return molecule, int(charge), int(multiplicity)


async def get_geometry_xyz(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Load one stored Geometry.mol and export its persisted electronic state."""

    geometry_export = await _get_geometry_xyz_export(geometry_id, project_id=project_id)
    if geometry_export is None:
        return None
    molecule, charge, multiplicity = geometry_export
    return draw_geometry_xyz(molecule, charge=charge, multiplicity=multiplicity)


async def get_geometry_dof_depiction(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Load one visible Geometry and return a cacheable rdkit-dof SVG."""

    molecule = await _get_geometry_molecule(geometry_id, project_id=project_id)
    if molecule is None:
        return None
    return draw_geometry_dof_svg(molecule)


async def get_transition_state_anchor_sdf(
    frame_id: UUID,
    anchor: Literal["negative", "center", "positive"],
    project_id: UUID | None = None,
) -> str | None:
    """Return one TS-mode anchor in the shared MolOP source coordinate frame."""

    scope = await query_visibility_scope(project_id=project_id)
    async with session_factory() as session:
        frame_row = (
            await session.execute(
                select(CalculationFrame, MolecularTopology)
                .join(Geometry, col(CalculationFrame.geometry_id) == col(Geometry.id))
                .join(
                    MolecularTopology,
                    col(Geometry.topology_id) == col(MolecularTopology.id),
                )
                .where(
                    col(CalculationFrame.id) == frame_id,
                    frame_id_is_visible(scope, col(CalculationFrame.id)),
                )
                .options(undefer(cast(Any, CalculationFrame.observed_coordinates)))
            )
        ).first()
        if frame_row is None:
            return None
        frame, frame_topology = frame_row
        if anchor == "center":
            molecule = _source_order_molecule(
                frame_topology.mol,
                frame.observed_coordinates,
                list(frame.observed_to_geometry_atom_indices),
            )
            return draw_geometry_sdf(
                molecule,
                charge=frame.charge,
                multiplicity=frame.multiplicity,
            )

        direction = TransitionStateEndpointDirection(anchor)
        endpoint_row = (
            await session.execute(
                select(TransitionStateEndpoint, MolecularTopology)
                .join(
                    MolecularTopology,
                    col(TransitionStateEndpoint.topology_id) == col(MolecularTopology.id),
                )
                .where(
                    col(TransitionStateEndpoint.calculation_frame_id) == frame_id,
                    col(TransitionStateEndpoint.direction) == direction,
                )
                .options(undefer(cast(Any, TransitionStateEndpoint.source_coordinates)))
            )
        ).first()
        if endpoint_row is None:
            return None
        endpoint, endpoint_topology = endpoint_row
        molecule = _source_order_molecule(
            endpoint_topology.mol,
            endpoint.source_coordinates,
            list(endpoint.source_to_topology_atom_indices),
        )
        return draw_geometry_sdf(
            molecule,
            charge=endpoint.charge,
            multiplicity=endpoint.multiplicity,
        )


__all__ = [
    "draw_molecule_molfile",
    "draw_molecule_svg",
    "draw_geometry_dof_svg",
    "draw_geometry_sdf",
    "draw_geometry_xyz",
    "get_geometry_dof_depiction",
    "get_geometry_sdf",
    "get_geometry_xyz",
    "get_transition_state_anchor_sdf",
    "get_topology_depiction",
    "get_topology_molfile",
]
