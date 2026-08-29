"""Molecular representations derived from stored topology and geometry molecules."""

from collections.abc import Sequence
from io import StringIO
from typing import Any, Literal, cast
from uuid import UUID

import numpy as np
from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Draw import rdMolDraw2D
from rdkit.Geometry import Point3D
from rdkit_dof import (  # type: ignore[import-untyped]
    DofDrawSettings,
    MolsToDofSvgAnimation,
    MolToDofImage,
)
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


def draw_transition_state_mode_dof_svg(
    molecules: Sequence[Chem.Mol],
    *,
    width: int = 480,
    height: int = 320,
    frame_duration_ms: int = 140,
) -> str:
    """Render stored TS-mode coordinates with rdkit-dof's native SVG animation."""

    if not molecules:
        raise ValueError("transition-state mode must contain at least one frame")
    for molecule in molecules:
        if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
            raise ValueError("each TS-mode frame must contain one three-dimensional conformer")
    rendered = MolsToDofSvgAnimation(
        [Chem.Mol(molecule) for molecule in molecules],
        size=(width, height),
        duration=frame_duration_ms,
        loop=0,
        return_image=False,
        settings=_GEOMETRY_DOF_SETTINGS,
        clearBackground=False,
        padding=0.06,
        bondLineWidth=1.8,
    )
    if not isinstance(rendered, str):
        raise TypeError("rdkit-dof did not return animated SVG text")
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


TransitionStateAnchor = Literal["negative", "center", "positive"]
_TRANSITION_STATE_ANCHORS: tuple[TransitionStateAnchor, ...] = (
    "negative",
    "center",
    "positive",
)


async def _get_transition_state_anchor_molecules(
    frame_id: UUID,
    project_id: UUID | None = None,
) -> dict[TransitionStateAnchor, tuple[Chem.Mol, int, int]] | None:
    """Load TS anchors in the original MolOP source atom order."""

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
        endpoint_rows = (
            await session.execute(
                select(TransitionStateEndpoint, MolecularTopology)
                .join(
                    MolecularTopology,
                    col(TransitionStateEndpoint.topology_id) == col(MolecularTopology.id),
                )
                .where(col(TransitionStateEndpoint.calculation_frame_id) == frame_id)
                .options(undefer(cast(Any, TransitionStateEndpoint.source_coordinates)))
            )
        ).all()

    anchors: dict[TransitionStateAnchor, tuple[Chem.Mol, int, int]] = {
        "center": (
            _source_order_molecule(
                frame_topology.mol,
                frame.observed_coordinates,
                list(frame.observed_to_geometry_atom_indices),
            ),
            frame.charge,
            frame.multiplicity,
        )
    }
    for endpoint, endpoint_topology in endpoint_rows:
        direction = endpoint.direction.value
        if direction not in {"negative", "positive"}:
            continue
        anchors[cast(TransitionStateAnchor, direction)] = (
            _source_order_molecule(
                endpoint_topology.mol,
                endpoint.source_coordinates,
                list(endpoint.source_to_topology_atom_indices),
            ),
            endpoint.charge,
            endpoint.multiplicity,
        )
    if any(anchor not in anchors for anchor in _TRANSITION_STATE_ANCHORS):
        return None
    return anchors


def _interpolate_transition_state_mode_frame(
    template: Chem.Mol,
    center: Chem.Mol,
    endpoint: Chem.Mol,
    fraction: float,
) -> Chem.Mol:
    """Copy one stored endpoint graph and interpolate only source-ordered coordinates."""

    if not 0.0 <= fraction <= 1.0:
        raise ValueError("TS-mode interpolation fraction must be between zero and one")
    atom_count = center.GetNumAtoms()
    if template.GetNumAtoms() != atom_count or endpoint.GetNumAtoms() != atom_count:
        raise ValueError("TS-mode anchors have incompatible atom counts")
    template_atoms = template.GetAtoms()  # type: ignore[no-untyped-call]
    center_atoms = center.GetAtoms()  # type: ignore[no-untyped-call]
    endpoint_atoms = endpoint.GetAtoms()  # type: ignore[no-untyped-call]
    if any(
        template_atom.GetAtomicNum() != center_atom.GetAtomicNum()
        or endpoint_atom.GetAtomicNum() != center_atom.GetAtomicNum()
        for template_atom, center_atom, endpoint_atom in zip(
            template_atoms, center_atoms, endpoint_atoms, strict=True
        )
    ):
        raise ValueError("TS-mode anchors have incompatible source atom order")
    for molecule in (template, center, endpoint):
        if molecule.GetNumConformers() != 1 or not molecule.GetConformer().Is3D():
            raise ValueError("TS-mode anchor must contain one three-dimensional conformer")

    center_coordinates = center.GetConformer().GetPositions()
    endpoint_coordinates = endpoint.GetConformer().GetPositions()
    if (
        center_coordinates.shape != (atom_count, 3)
        or endpoint_coordinates.shape != (atom_count, 3)
        or not np.isfinite(center_coordinates).all()
        or not np.isfinite(endpoint_coordinates).all()
    ):
        raise ValueError("TS-mode anchor coordinates are invalid")
    interpolated = Chem.Mol(template)
    conformer = interpolated.GetConformer()
    for atom_index, coordinates in enumerate(
        center_coordinates + fraction * (endpoint_coordinates - center_coordinates)
    ):
        conformer.SetAtomPosition(
            atom_index,
            Point3D(float(coordinates[0]), float(coordinates[1]), float(coordinates[2])),
        )
    return interpolated


def _interpolate_transition_state_mode_frames(
    anchors: dict[TransitionStateAnchor, tuple[Chem.Mol, int, int]],
) -> list[Chem.Mol]:
    """Match ChemDoodle's persisted signed-anchor sequence without graph rebuilding."""

    negative = anchors["negative"][0]
    center = anchors["center"][0]
    positive = anchors["positive"][0]
    frames = [
        _interpolate_transition_state_mode_frame(negative, center, negative, step / 10)
        for step in range(10, 0, -1)
    ]
    frames.append(Chem.Mol(center))
    frames.extend(
        _interpolate_transition_state_mode_frame(positive, center, positive, step / 10)
        for step in range(1, 11)
    )
    return frames


async def get_transition_state_mode_dof_depiction(
    frame_id: UUID,
    project_id: UUID | None = None,
) -> str | None:
    """Return a looping rdkit-dof SMIL animation for a persisted TS imaginary mode."""

    anchors = await _get_transition_state_anchor_molecules(frame_id, project_id=project_id)
    if anchors is None:
        return None
    return draw_transition_state_mode_dof_svg(_interpolate_transition_state_mode_frames(anchors))


async def get_transition_state_anchor_sdf(
    frame_id: UUID,
    anchor: Literal["negative", "center", "positive"],
    project_id: UUID | None = None,
) -> str | None:
    """Return one TS-mode anchor in the shared MolOP source coordinate frame."""

    anchors = await _get_transition_state_anchor_molecules(frame_id, project_id=project_id)
    if anchors is None:
        return None
    molecule, charge, multiplicity = anchors[anchor]
    return draw_geometry_sdf(molecule, charge=charge, multiplicity=multiplicity)


__all__ = [
    "draw_molecule_molfile",
    "draw_molecule_svg",
    "draw_geometry_dof_svg",
    "draw_transition_state_mode_dof_svg",
    "draw_geometry_sdf",
    "draw_geometry_xyz",
    "get_geometry_dof_depiction",
    "get_geometry_sdf",
    "get_geometry_xyz",
    "get_transition_state_anchor_sdf",
    "get_transition_state_mode_dof_depiction",
    "get_topology_depiction",
    "get_topology_molfile",
]
