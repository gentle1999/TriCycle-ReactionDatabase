"""Molecular representation routes derived from stored topology graphs."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from rdkit import Chem
from rdkit.Chem import rdChemReactions

from tricycle_reaction_db.application.services.depictions import (
    draw_molecule_molfile,
    draw_reaction_svg,
    get_geometry_dof_depiction,
    get_geometry_sdf,
    get_geometry_xyz,
    get_topology_depiction,
    get_topology_molfile,
    get_transition_state_anchor_sdf,
    get_transition_state_mode_dof_depiction,
)

router = APIRouter(tags=["molecular representations"])


class RepresentationConversionRequest(BaseModel):
    """One browser-editable molecular representation to canonicalize."""

    smiles: str | None = Field(default=None, min_length=1, max_length=16_384)
    molfile: str | None = Field(default=None, min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def require_one_representation(self) -> "RepresentationConversionRequest":
        if (self.smiles is None) == (self.molfile is None):
            raise ValueError("provide exactly one of smiles or molfile")
        return self


class RepresentationConversionResponse(BaseModel):
    smiles: str
    molfile: str


class ReactionRepresentationConversionRequest(BaseModel):
    """One browser-editable reaction representation to canonicalize."""

    reaction_smiles: str | None = Field(default=None, min_length=1, max_length=65_536)
    rxn: str | None = Field(default=None, min_length=1, max_length=131_072)

    @model_validator(mode="after")
    def require_one_representation(self) -> "ReactionRepresentationConversionRequest":
        if (self.reaction_smiles is None) == (self.rxn is None):
            raise ValueError("provide exactly one of reaction_smiles or rxn")
        return self


class ReactionRepresentationConversionResponse(BaseModel):
    reaction_smiles: str
    rxn: str


ChemistryValidationKind = Literal[
    "smiles",
    "smarts",
    "rxn_smiles",
    "rxn_smarts",
    "mol_block",
    "rxn",
]


class ChemistryValidationRequest(BaseModel):
    """A representation to validate while a browser query is being edited."""

    kind: ChemistryValidationKind
    value: str = Field(min_length=1, max_length=131_072)


class ChemistryValidationResponse(BaseModel):
    kind: ChemistryValidationKind
    valid: bool
    normalized: str | None = None
    error: str | None = None


def _parse_mol_block(value: str) -> Chem.Mol | None:
    """Parse RDKit and ChemDoodle MOL blocks with either header layout."""
    # ChemDoodle's editor omits the optional blank line before the counts line.
    # RDKit's strict parser expects that line, while RDKit-generated blocks keep it.
    for candidate in (value, f"\n{value}"):
        try:
            molecule = Chem.MolFromMolBlock(
                candidate,
                sanitize=True,
                removeHs=False,
                strictParsing=True,
            )
        except (TypeError, ValueError, RuntimeError, IndexError):
            continue
        if molecule is not None:
            return molecule
    return None


@router.post(
    "/api/chemistry/representations",
    response_model=RepresentationConversionResponse,
)
async def convert_representation(
    request: RepresentationConversionRequest,
) -> RepresentationConversionResponse:
    """Convert the isolated ChemDoodle editor's MOL payload with RDKit."""

    molecule: Chem.Mol | None
    try:
        if request.smiles is not None:
            molecule = Chem.MolFromSmiles(request.smiles)
        else:
            molecule = _parse_mol_block(request.molfile or "")
    except (TypeError, ValueError, RuntimeError, IndexError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="molecular representation could not be parsed",
        ) from error
    if molecule is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="molecular representation could not be parsed",
        )
    try:
        canonical_smiles = Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
        molfile = draw_molecule_molfile(molecule)
    except (TypeError, ValueError, RuntimeError, IndexError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="molecular representation could not be serialized",
        ) from error
    return RepresentationConversionResponse(smiles=canonical_smiles, molfile=molfile)


@router.post(
    "/api/chemistry/reactions",
    response_model=ReactionRepresentationConversionResponse,
)
async def convert_reaction_representation(
    request: ReactionRepresentationConversionRequest,
) -> ReactionRepresentationConversionResponse:
    """Convert between reaction SMILES and ChemDoodle-compatible RXN blocks."""

    try:
        reaction = (
            rdChemReactions.ReactionFromSmiles(request.reaction_smiles)
            if request.reaction_smiles is not None
            else rdChemReactions.ReactionFromRxnBlock(request.rxn or "", sanitize=True)
        )
        if reaction is None:
            raise ValueError("reaction could not be parsed")
        rdChemReactions.SanitizeRxn(reaction)
        reaction_smiles = rdChemReactions.ReactionToSmiles(reaction)
        rxn = rdChemReactions.ReactionToRxnBlock(reaction)
    except (TypeError, ValueError, RuntimeError, IndexError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reaction representation could not be parsed or serialized",
        ) from error
    return ReactionRepresentationConversionResponse(reaction_smiles=reaction_smiles, rxn=rxn)


@router.post(
    "/api/chemistry/reactions/validate",
    response_model=ChemistryValidationResponse,
)
async def validate_chemistry_representation(
    request: ChemistryValidationRequest,
) -> ChemistryValidationResponse:
    """Validate molecular and reaction representations without executing a query."""

    molecule: Chem.Mol | None
    try:
        normalized: str
        if request.kind == "smiles":
            molecule = Chem.MolFromSmiles(request.value)
            if molecule is None:
                raise ValueError("SMILES could not be parsed")
            normalized = Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
        elif request.kind == "smarts":
            molecule = Chem.MolFromSmarts(request.value)
            if molecule is None:
                raise ValueError("SMARTS could not be parsed")
            normalized = Chem.MolToSmarts(molecule)
        else:
            if request.kind == "rxn_smiles":
                reaction = rdChemReactions.ReactionFromSmiles(request.value)
            elif request.kind == "rxn_smarts":
                reaction = rdChemReactions.ReactionFromSmarts(request.value)
            elif request.kind == "rxn":
                reaction = rdChemReactions.ReactionFromRxnBlock(request.value, sanitize=True)
            else:
                molecule = _parse_mol_block(request.value)
                if molecule is None:
                    raise ValueError("MOL block could not be parsed")
                normalized = Chem.MolToMolBlock(molecule)
                return ChemistryValidationResponse(
                    kind=request.kind,
                    valid=True,
                    normalized=normalized,
                )
            if reaction is None:
                raise ValueError("reaction could not be parsed")
            if reaction.GetNumReactantTemplates() == 0 or reaction.GetNumProductTemplates() == 0:
                raise ValueError("reaction must contain at least one reactant and one product")
            if request.kind == "rxn_smarts":
                normalized = rdChemReactions.ReactionToSmarts(reaction)
            elif request.kind == "rxn":
                normalized = rdChemReactions.ReactionToRxnBlock(reaction)
            else:
                normalized = rdChemReactions.ReactionToSmiles(reaction)
        return ChemistryValidationResponse(
            kind=request.kind,
            valid=True,
            normalized=normalized,
        )
    except (TypeError, ValueError, RuntimeError, IndexError):
        return ChemistryValidationResponse(
            kind=request.kind,
            valid=False,
            error={
                "smiles": "SMILES 无法解析",
                "smarts": "SMARTS 无法解析",
                "rxn_smiles": "RXN SMILES 无法解析",
                "rxn_smarts": "RXN SMARTS 无法解析",
                "rxn": "RXN block 无法解析",
                "mol_block": "MOL block 无法解析",
            }[request.kind],
        )


@router.get(
    "/api/depictions/reaction.svg",
    response_class=Response,
)
async def reaction_depiction(
    reaction_smiles: str = Query(min_length=1, max_length=65_536),
) -> Response:
    """Render one already-persisted reaction representation as a read-only SVG."""

    try:
        svg = draw_reaction_svg(reaction_smiles)
    except (TypeError, ValueError, RuntimeError, IndexError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reaction representation could not be rendered",
        ) from error
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Depiction-Renderer": "rdkit-reaction",
        },
    )


@router.get(
    "/api/depictions/calculation-frame/{frame_id}/transition-state.svg",
    response_class=Response,
)
async def transition_state_mode_dof_depiction(
    frame_id: UUID,
    project_id: UUID | None = None,
) -> Response:
    svg = await get_transition_state_mode_dof_depiction(frame_id, project_id=project_id)
    if svg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="transition-state mode anchors not found",
        )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, no-store",
            "X-Depiction-Renderer": "rdkit-dof",
            "X-Depiction-Animation": "smil",
            "X-Transition-State-Frame-Count": "21",
        },
    )


@router.get(
    "/api/depictions/calculation-frame/{frame_id}/transition-state/{anchor}.sdf",
    response_class=Response,
)
async def transition_state_anchor_sdf(
    frame_id: UUID,
    anchor: Literal["negative", "center", "positive"],
    project_id: UUID | None = None,
) -> Response:
    sdf = await get_transition_state_anchor_sdf(frame_id, anchor, project_id=project_id)
    if sdf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="transition-state mode anchor not found",
        )
    return Response(
        content=sdf,
        media_type="chemical/x-mdl-sdfile",
        headers={
            "Cache-Control": "private, no-store",
            "X-Coordinate-Unit": "angstrom",
            "X-Coordinate-Frame": "molop-source-atom-order",
            "X-Transition-State-Anchor": anchor,
        },
    )


@router.get(
    "/api/depictions/geometry/{geometry_id}.svg",
    response_class=Response,
)
async def geometry_depiction(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> Response:
    svg = await get_geometry_dof_depiction(geometry_id, project_id=project_id)
    if svg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="molecular geometry not found",
        )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "private, no-store",
            "X-Depiction-Renderer": "rdkit-dof",
        },
    )


@router.get(
    "/api/depictions/geometry/{geometry_id}.sdf",
    response_class=Response,
)
async def geometry_sdf(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> Response:
    sdf = await get_geometry_sdf(geometry_id, project_id=project_id)
    if sdf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="molecular geometry not found",
        )
    return Response(
        content=sdf,
        media_type="chemical/x-mdl-sdfile",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="geometry-{geometry_id}.sdf"',
            "X-Coordinate-Unit": "angstrom",
            "X-Geometry-Dimension": "3",
        },
    )


@router.get(
    "/api/depictions/geometry/{geometry_id}.xyz",
    response_class=Response,
)
async def geometry_xyz(
    geometry_id: UUID,
    project_id: UUID | None = None,
) -> Response:
    xyz = await get_geometry_xyz(geometry_id, project_id=project_id)
    if xyz is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="molecular geometry not found",
        )
    return Response(
        content=xyz,
        media_type="chemical/x-xyz",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="geometry-{geometry_id}.xyz"',
            "X-Coordinate-Unit": "angstrom",
            "X-Geometry-Dimension": "3",
        },
    )


@router.get(
    "/api/depictions/topology/{topology_id}.svg",
    response_class=Response,
)
async def topology_depiction(topology_id: UUID) -> Response:
    svg = await get_topology_depiction(topology_id)
    if svg is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="molecular topology not found",
        )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/api/depictions/topology/{topology_id}.mol",
    response_class=Response,
)
async def topology_molfile(topology_id: UUID) -> Response:
    molfile = await get_topology_molfile(topology_id)
    if molfile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="molecular topology not found",
        )
    return Response(
        content=molfile,
        media_type="chemical/x-mdl-molfile",
        headers={"Cache-Control": "private, no-store"},
    )


__all__ = ["router"]
