"""Read-only NexusX use cases for the reaction database."""

import base64
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from math import isfinite
from typing import Annotated, Any, cast
from uuid import UUID

from molalchemy.helpers import rdkit_col
from molalchemy.rdkit.functions import (
    dice_sml,
    mol_amw,
    mol_from_smiles,
    mol_hba,
    mol_hbd,
    mol_logp,
    mol_murckoscaffold,
    mol_numrings,
    mol_to_smiles,
    mol_tpsa,
    morganbv_fp,
    qmol_from_smarts,
    reaction_from_smarts,
    reaction_from_smiles,
    reaction_structural_bfp,
    substruct_count,
    substruct_count_chiral,
    tanimoto_sml,
)
from molalchemy.types import CString
from nexusx import UseCaseService, query  # type: ignore[import-untyped]
from pydantic import Field
from rdkit import Chem
from rdkit.Chem import rdChemReactions
from sqlalchemy import Boolean, Text, and_, case, func, literal, not_, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by, array
from sqlalchemy.orm import defer, joinedload, load_only
from sqlmodel import col
from sqlmodel import select as sqlmodel_select

from tricycle_reaction_db.application.dtos import (
    ArtifactPage,
    ArtifactSummary,
    CalculationFrameDetail,
    CalculationFramePage,
    CalculationFrameSummary,
    CalculationProtocolView,
    CalculationStatusView,
    EnergyObservationView,
    FrameEnergyView,
    GeometryOptimizationView,
    LogicalReactionDetail,
    LogicalReactionPage,
    LogicalReactionParticipantView,
    LogicalReactionSummary,
    MappedReactionDetail,
    MappedReactionEdgeView,
    MappedReactionNodeGeometryView,
    MappedReactionNodeView,
    MappedReactionPage,
    MappedReactionParticipantView,
    MappedReactionSummary,
    MolecularFormulaPage,
    MolecularFormulaRangeQuery,
    MolecularFormulaSummary,
    MolecularTopologyDerivationView,
    MolecularTopologySearchPage,
    MolecularTopologySearchQuery,
    MolecularTopologySearchResult,
    NodeAdditivePropertiesView,
    NodeGeometryMappingView,
    PageInfo,
    ScientificArraySummary,
    SourceSpanView,
    ThermochemistryView,
    TransitionStateEndpointView,
    VibrationView,
)
from tricycle_reaction_db.application.query_cost import (
    QueryBudgetExceeded,
    enforce_structure_input_budget,
)
from tricycle_reaction_db.application.services.artifact_content import (
    artifact_preview_available,
)
from tricycle_reaction_db.application.services.authorization import ProjectPermission
from tricycle_reaction_db.application.services.geometry_energy import (
    GeometryEnergyComposite,
    geometry_energy_composites,
)
from tricycle_reaction_db.application.services.query_visibility import (
    frame_id_is_visible,
    geometry_id_is_visible,
    logical_reaction_id_is_visible,
    mapped_reaction_id_is_visible,
    query_visibility_scope,
    topology_id_is_visible,
    visible_frame_ids,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_thermodynamic_property_predicate,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    ArtifactIngestion,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    CalculationStatusResult,
    EnergyObservation,
    FrameEnergyResult,
    Geometry,
    GeometryOptimizationResult,
    LogicalReaction,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    ParseRevision,
    ProjectGeometryCatalog,
    ScientificArray,
    ScientificArrayAssignment,
    ThermochemistryResult,
    TransitionStateEndpoint,
    VibrationResult,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    ArtifactKind,
    FrameRole,
    LogicalReactionParticipantSide,
    MappedReactionKind,
    OptimizationStatus,
    ReactionClass,
    SCFStatus,
    SimilarityMetric,
    StereoStatus,
    StorageStatus,
    TopologySanitizationStatus,
)
from tricycle_reaction_db.domain.fingerprints import (
    MORGAN_BFP_RADIUS,
    REACTION_STRUCTURAL_BFP_RADIUS,
)
from tricycle_reaction_db.domain.precision import round_energy_hartree

PageLimit = Annotated[int, Field(ge=1, le=200, description="Maximum rows to return.")]
PageOffset = Annotated[int, Field(ge=0, description="Number of rows to skip.")]


def _required_uuid(value: UUID | None, label: str) -> UUID:
    if value is None:
        raise RuntimeError(f"persisted {label} is missing its UUID")
    return value


def _validate_range(
    minimum: Any,
    maximum: Any,
    *,
    minimum_name: str,
    maximum_name: str,
) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{minimum_name} cannot exceed {maximum_name}")


def reaction_smarts_from_mol_blocks(
    reactant_mol_block: str | None,
    product_mol_block: str | None,
) -> str | None:
    """Build a reaction SMARTS query from one or both topology-editor MolBlocks."""

    if reactant_mol_block is None and product_mol_block is None:
        return None

    smiles: dict[str, str] = {}
    for name, mol_block in (
        ("reactant_mol_block", reactant_mol_block),
        ("product_mol_block", product_mol_block),
    ):
        if mol_block is None:
            continue
        molecule = Chem.MolFromMolBlock(
            mol_block,
            sanitize=True,
            removeHs=True,
            strictParsing=False,
        )
        # ChemDoodle emits an RXN/MOL block without the optional leading header line.
        if molecule is None and not mol_block.startswith("\n"):
            molecule = Chem.MolFromMolBlock(
                f"\n{mol_block}",
                sanitize=True,
                removeHs=True,
                strictParsing=False,
            )
        if molecule is None:
            raise ValueError(f"{name} must contain a valid molecule")
        smiles[name] = Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
    reaction_smarts = (
        f"{smiles.get('reactant_mol_block', '')}>>{smiles.get('product_mol_block', '')}"
    )
    if rdChemReactions.ReactionFromSmarts(reaction_smarts) is None:
        raise ValueError("reaction structure inputs do not form a valid reaction")
    return reaction_smarts


_LOGICAL_REACTION_QUERY_EXPRESSION_FIELDS = frozenset(
    {
        "topology_id",
        "reaction_key",
        "label",
        "reaction_hash",
        "reaction_class",
        "smarts",
        "reactant_smarts",
        "product_smarts",
        "reaction_smarts",
        "rxn_smarts",
        "reactant_mol_block",
        "product_mol_block",
        "minimum_activation_gibbs_free_energy_kcal_mol",
        "maximum_activation_gibbs_free_energy_kcal_mol",
        "minimum_reaction_gibbs_free_energy_kcal_mol",
        "maximum_reaction_gibbs_free_energy_kcal_mol",
        "reactant_product_changed",
        "created_after",
        "created_before",
    }
)


def _logical_reaction_structure_predicate(
    field: str,
    value: object,
    scope: Any,
    structure_predicates: list[Any],
) -> Any:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    enforce_structure_input_budget(
        {field: value},
        maximum_characters=get_settings().structure_query_max_characters,
    )
    stored_reaction = cast(Any, col(MappedReaction.reaction))
    if field in {"smarts", "reactant_smarts", "product_smarts"}:
        if Chem.MolFromSmarts(value) is None:
            raise ValueError("smarts must contain a valid molecular SMARTS")
        side_smarts = (
            (f"{value}>>",)
            if field == "reactant_smarts"
            else (f">>{value}",)
            if field == "product_smarts"
            else (f"{value}>>", f">>{value}")
        )
        if any(rdChemReactions.ReactionFromSmarts(item) is None for item in side_smarts):
            raise ValueError("smarts must form a valid reaction-side query")
        structure_predicate = or_(
            *(
                stored_reaction.op("@>")(
                    reaction_from_smarts(cast(CString, sql_cast(item, CString)))
                )
                for item in side_smarts
            )
        )
    else:
        reaction_smarts = (
            value
            if field in {"reaction_smarts", "rxn_smarts"}
            else reaction_smarts_from_mol_blocks(
                value if field == "reactant_mol_block" else None,
                value if field == "product_mol_block" else None,
            )
        )
        if reaction_smarts is None or rdChemReactions.ReactionFromSmarts(reaction_smarts) is None:
            raise ValueError(f"{field} must contain a valid reaction structure")
        structure_predicate = stored_reaction.op("@>")(
            reaction_from_smarts(cast(CString, sql_cast(reaction_smarts, CString)))
        )
    structure_predicates.append(structure_predicate)
    mapped_structure_ids = (
        select(col(MappedReaction.logical_reaction_id))
        .where(
            mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
            structure_predicate,
        )
        .correlate(None)
    )
    return col(LogicalReaction.id).in_(mapped_structure_ids)


def _reaction_topology_side_signature(
    side: LogicalReactionParticipantSide,
    *,
    reaction_id_column: Any | None = None,
    correlation_entity: Any | None = None,
) -> Any:
    """Return an order-independent signature of one reaction side.

    ``canonical_isomeric_smiles`` is the persisted, atom-order-normalized
    topology representation. Including the stoichiometric coefficient and
    sorting inside ``array_agg`` makes this a multiset comparison rather than
    a comparison of input/template order.
    """

    component = func.concat(
        func.coalesce(col(MolecularTopology.canonical_isomeric_smiles), literal("<unknown>")),
        literal(":"),
        sql_cast(col(LogicalReactionParticipant.stoichiometric_coefficient), Text),
    )
    correlation_entity = correlation_entity or (
        MappedReaction if reaction_id_column is not None else LogicalReaction
    )
    correlated_reaction_id = (
        reaction_id_column if reaction_id_column is not None else col(LogicalReaction.id)
    )
    return (
        select(
            func.array_agg(
                aggregate_order_by(
                    component,
                    col(MolecularTopology.canonical_isomeric_smiles),
                    col(LogicalReactionParticipant.stoichiometric_coefficient),
                    col(LogicalReactionParticipant.participant_index),
                )
            )
        )
        .select_from(LogicalReactionParticipant)
        .join(
            MolecularTopology,
            col(LogicalReactionParticipant.topology_id) == col(MolecularTopology.id),
        )
        .where(
            col(LogicalReactionParticipant.logical_reaction_id) == correlated_reaction_id,
            col(LogicalReactionParticipant.side) == side,
        )
        .correlate(correlation_entity)
        .scalar_subquery()
    )


def _reaction_topology_changed_expression(
    *,
    reaction_id_column: Any | None = None,
    correlation_entity: Any | None = None,
) -> Any:
    """Compare canonical reactant/product topology multisets in SQL."""

    reactants = _reaction_topology_side_signature(
        LogicalReactionParticipantSide.REACTANT,
        reaction_id_column=reaction_id_column,
        correlation_entity=correlation_entity,
    )
    products = _reaction_topology_side_signature(
        LogicalReactionParticipantSide.PRODUCT,
        reaction_id_column=reaction_id_column,
        correlation_entity=correlation_entity,
    )
    return case(
        (reactants.is_(None) | products.is_(None), literal(None, type_=Boolean)),
        else_=reactants.is_distinct_from(products),
    )


def _logical_reaction_query_leaf_predicate(
    field: object,
    value: object,
    scope: Any,
    structure_predicates: list[Any],
) -> Any:
    if not isinstance(field, str) or field not in _LOGICAL_REACTION_QUERY_EXPRESSION_FIELDS:
        choices = ", ".join(sorted(_LOGICAL_REACTION_QUERY_EXPRESSION_FIELDS))
        raise ValueError(f"unsupported logical reaction query field; expected one of: {choices}")
    field_name: str = field
    if field_name == "topology_id":
        try:
            topology_id = UUID(str(value))
        except (TypeError, ValueError) as error:
            raise ValueError("topology_id must be a valid UUID") from error
        reaction_ids = select(col(LogicalReactionParticipant.logical_reaction_id)).where(
            col(LogicalReactionParticipant.topology_id) == topology_id
        )
        return col(LogicalReaction.id).in_(reaction_ids)
    if field_name in {"reaction_key", "label", "reaction_hash"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        text_column = {
            "reaction_key": LogicalReaction.reaction_key,
            "label": LogicalReaction.label,
            "reaction_hash": LogicalReaction.reaction_hash,
        }[field_name]
        return col(text_column) == value
    if field_name == "reaction_class":
        try:
            reaction_class = ReactionClass(str(value))
        except ValueError as error:
            choices = ", ".join(item.value for item in ReactionClass)
            raise ValueError(f"reaction_class must be one of: {choices}") from error
        return col(LogicalReaction.reaction_class) == reaction_class
    if field_name in {
        "smarts",
        "reactant_smarts",
        "product_smarts",
        "reaction_smarts",
        "rxn_smarts",
        "reactant_mol_block",
        "product_mol_block",
    }:
        return _logical_reaction_structure_predicate(
            field_name,
            value,
            scope,
            structure_predicates,
        )
    if field_name in {
        "minimum_activation_gibbs_free_energy_kcal_mol",
        "maximum_activation_gibbs_free_energy_kcal_mol",
        "minimum_reaction_gibbs_free_energy_kcal_mol",
        "maximum_reaction_gibbs_free_energy_kcal_mol",
    }:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{field_name} must be a finite number")
        try:
            parsed_value = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be a finite number") from error
        if not isfinite(parsed_value):
            raise ValueError(f"{field_name} must be a finite number")
        column = {
            "minimum_activation_gibbs_free_energy_kcal_mol": (
                MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol
            ),
            "maximum_activation_gibbs_free_energy_kcal_mol": (
                MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol
            ),
            "minimum_reaction_gibbs_free_energy_kcal_mol": (
                MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol
            ),
            "maximum_reaction_gibbs_free_energy_kcal_mol": (
                MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol
            ),
        }[field_name]
        comparison = (
            col(column) >= parsed_value
            if field_name.startswith("minimum_")
            else col(column) <= parsed_value
        )
        mapped_screening_ids = select(col(MappedReaction.logical_reaction_id)).where(
            mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
            comparison,
        )
        return col(LogicalReaction.id).in_(mapped_screening_ids)
    if field_name == "reactant_product_changed":
        if not isinstance(value, bool):
            raise ValueError("reactant_product_changed must be a boolean")
        return _reaction_topology_changed_expression() == value
    if field_name in {"created_after", "created_before"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be an ISO 8601 datetime")
        try:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO 8601 datetime") from error
        return (
            col(LogicalReaction.created_at) >= parsed_datetime
            if field_name == "created_after"
            else col(LogicalReaction.created_at) <= parsed_datetime
        )
    raise AssertionError(f"unhandled logical reaction query field: {field_name}")


def _logical_reaction_query_expression_predicate(
    node: object,
    scope: Any,
    structure_predicates: list[Any],
    depth: int = 0,
) -> Any:
    if depth > 12:
        raise ValueError("logical reaction query expression is too deeply nested")
    if not isinstance(node, Mapping):
        raise ValueError("logical reaction query expression nodes must be objects")
    operator = node.get("operator")
    if operator is not None:
        children = node.get("conditions")
        if operator not in {"and", "or", "not"} or not isinstance(children, list) or not children:
            raise ValueError("logical nodes require operator and non-empty conditions")
        if operator == "not" and len(children) != 1:
            raise ValueError("not nodes require exactly one condition")
        predicates = [
            _logical_reaction_query_expression_predicate(
                child,
                scope,
                structure_predicates,
                depth + 1,
            )
            for child in children
        ]
        if operator == "and":
            return and_(*predicates)
        if operator == "or":
            return or_(*predicates)
        return not_(predicates[0])
    if "field" not in node or "value" not in node:
        raise ValueError("leaf nodes require field and value")
    predicate = _logical_reaction_query_leaf_predicate(
        node.get("field"),
        node.get("value"),
        scope,
        structure_predicates,
    )
    if node.get("negated", False) is True:
        return not_(predicate)
    if node.get("negated", False) not in {False, None}:
        raise ValueError("negated must be a boolean")
    return predicate


def logical_reaction_filter_expression_predicate(
    filter_expression: str,
    scope: Any,
    structure_predicates: list[Any],
) -> Any:
    """Parse one public filter expression with the logical-reaction query rules."""

    if len(filter_expression) > get_settings().structure_query_max_characters:
        raise ValueError("filter_expression exceeds the configured character budget")
    try:
        expression = json.loads(filter_expression)
    except json.JSONDecodeError as error:
        raise ValueError("filter_expression must contain valid JSON") from error
    return _logical_reaction_query_expression_predicate(
        expression,
        scope,
        structure_predicates,
    )


async def _enforce_candidate_limit(
    statement: Any,
    *,
    label: str,
    session: Any,
) -> None:
    maximum = get_settings().structure_candidate_limit
    candidate_ids = (await session.exec(statement.limit(maximum + 1))).all()
    if len(candidate_ids) > maximum:
        raise QueryBudgetExceeded(
            f"{label} candidate set exceeds the {maximum}-row limit; add a selective prefilter"
        )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _optional_enum_value(value: Any) -> str | None:
    return None if value is None else _enum_value(value)


def _complete_sum(values: Sequence[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return round_energy_hartree(sum(value for value in values if value is not None))


def _complete_scalar_sum(values: Sequence[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 6)


def _aggregate_primary_coordinates(
    coordinates: Sequence[GeometryEnergyComposite],
) -> NodeAdditivePropertiesView | None:
    if not coordinates:
        return None
    electronic_levels = {coordinate.electronic_level for coordinate in coordinates}
    thermochemistry_levels = {coordinate.thermochemistry_level for coordinate in coordinates}
    conditions = {
        (coordinate.view.temperature_kelvin, coordinate.view.pressure_atm)
        for coordinate in coordinates
    }
    levels_compatible = (
        None not in electronic_levels
        and len(electronic_levels) == 1
        and None not in thermochemistry_levels
        and len(thermochemistry_levels) == 1
        and None not in {item for condition in conditions for item in condition}
        and len(conditions) == 1
    )
    first = coordinates[0].view

    def total(attribute: str) -> float | None:
        if not levels_compatible:
            return None
        values = [getattr(coordinate.view, attribute) for coordinate in coordinates]
        return (
            _complete_scalar_sum(values)
            if attribute == "entropy_cal_mol_k"
            else _complete_sum(values)
        )

    return NodeAdditivePropertiesView(
        component_count=len(coordinates),
        policy_version=first.policy_version,
        source_levels_compatible=levels_compatible,
        electronic_energy_hartree=total("electronic_energy_hartree"),
        temperature_kelvin=first.temperature_kelvin if levels_compatible else None,
        pressure_atm=first.pressure_atm if levels_compatible else None,
        zero_point_energy_hartree=total("zero_point_energy_hartree"),
        thermal_internal_energy_hartree=total("thermal_internal_energy_hartree"),
        enthalpy_hartree=total("enthalpy_hartree"),
        gibbs_free_energy_hartree=total("gibbs_free_energy_hartree"),
        entropy_cal_mol_k=total("entropy_cal_mol_k"),
    )


def _canonical_reactant_product_changed(
    participant_rows: Sequence[tuple[LogicalReactionParticipant, MolecularTopology]],
) -> bool | None:
    """Compare participant topology multisets using canonical topology strings."""

    signatures: dict[LogicalReactionParticipantSide, list[tuple[str | None, int]]] = {
        LogicalReactionParticipantSide.REACTANT: [],
        LogicalReactionParticipantSide.PRODUCT: [],
    }
    for participant, topology in participant_rows:
        signatures[participant.side].append(
            (topology.canonical_isomeric_smiles, participant.stoichiometric_coefficient)
        )
    if (
        not signatures[LogicalReactionParticipantSide.REACTANT]
        or not signatures[LogicalReactionParticipantSide.PRODUCT]
    ):
        return None
    if any(smiles is None for side in signatures.values() for smiles, _coefficient in side):
        return None

    def sort_key(item: tuple[str | None, int]) -> tuple[str, int]:
        return item[0] or "", item[1]

    return sorted(signatures[LogicalReactionParticipantSide.REACTANT], key=sort_key) != sorted(
        signatures[LogicalReactionParticipantSide.PRODUCT], key=sort_key
    )


def _reaction_summary(
    reaction: LogicalReaction,
    *,
    reactant_product_changed: bool | None = None,
    similarity_score: float | None = None,
    reactant_topology_ids: list[UUID] | None = None,
    product_topology_ids: list[UUID] | None = None,
    transition_state_geometry_id: UUID | None = None,
    minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
) -> LogicalReactionSummary:
    return LogicalReactionSummary(
        id=_required_uuid(reaction.id, "LogicalReaction"),
        reaction_key=reaction.reaction_key,
        label=reaction.label,
        reaction_class=_optional_enum_value(reaction.reaction_class),
        cycloaddition_pattern=reaction.cycloaddition_pattern,
        reaction_hash=reaction.reaction_hash,
        reactant_product_changed=reactant_product_changed,
        similarity_score=similarity_score,
        created_at=reaction.created_at,
        reactant_topology_ids=reactant_topology_ids or [],
        product_topology_ids=product_topology_ids or [],
        transition_state_geometry_id=transition_state_geometry_id,
        minimum_activation_gibbs_free_energy_kcal_mol=(
            minimum_activation_gibbs_free_energy_kcal_mol
        ),
        maximum_activation_gibbs_free_energy_kcal_mol=(
            maximum_activation_gibbs_free_energy_kcal_mol
        ),
        minimum_reaction_gibbs_free_energy_kcal_mol=(minimum_reaction_gibbs_free_energy_kcal_mol),
        maximum_reaction_gibbs_free_energy_kcal_mol=(maximum_reaction_gibbs_free_energy_kcal_mol),
    )


def _mapped_reaction_summary(
    path: MappedReaction,
    *,
    reactant_product_changed: bool | None = None,
    reaction_smarts_match: bool | None = None,
    similarity_score: float | None = None,
    minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
    maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
) -> MappedReactionSummary:
    return MappedReactionSummary(
        id=_required_uuid(path.id, "MappedReaction"),
        logical_reaction_id=path.logical_reaction_id,
        mapped_reaction_key=path.mapped_reaction_key,
        label=path.label,
        mapped_reaction_kind=_enum_value(path.mapped_reaction_kind),
        mapped_reaction_smiles=path.mapped_reaction_smiles,
        mapping_hash=path.mapping_hash,
        reaction_structural_bfp_schema_version=(path.reaction_structural_bfp_schema_version),
        reactant_product_changed=reactant_product_changed,
        created_at=path.created_at,
        reaction_smarts_match=reaction_smarts_match,
        similarity_score=similarity_score,
        minimum_activation_gibbs_free_energy_kcal_mol=(
            path.minimum_activation_gibbs_free_energy_kcal_mol
        ),
        maximum_activation_gibbs_free_energy_kcal_mol=(
            path.maximum_activation_gibbs_free_energy_kcal_mol
        ),
        minimum_reaction_gibbs_free_energy_kcal_mol=(
            path.minimum_reaction_gibbs_free_energy_kcal_mol
        ),
        maximum_reaction_gibbs_free_energy_kcal_mol=(
            path.maximum_reaction_gibbs_free_energy_kcal_mol
        ),
    )


def _frame_summary(
    frame: CalculationFrame,
    segment: CalculationSegment,
    revision: ParseRevision,
    artifact: ArtifactFile,
    geometry: Geometry,
    topology: MolecularTopology,
) -> CalculationFrameSummary:
    return CalculationFrameSummary(
        id=_required_uuid(frame.id, "CalculationFrame"),
        artifact_file_id=revision.artifact_file_id,
        original_filename=artifact.original_filename,
        parse_revision_id=frame.parse_revision_id,
        segment_id=frame.segment_id,
        segment_index=segment.segment_index,
        frame_index=frame.frame_index,
        file_frame_index=frame.file_frame_index,
        frame_role=_enum_value(frame.frame_role),
        geometry_id=frame.geometry_id,
        topology_id=geometry.topology_id,
        topology_derivation_id=frame.topology_derivation_id,
        protocol_id=segment.protocol_id,
        canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
        charge=frame.charge,
        multiplicity=frame.multiplicity,
        coordinate_decimal_places=frame.coordinate_decimal_places,
        scf_status=_enum_value(frame.scf_status),
        optimization_status=_enum_value(frame.optimization_status),
        selected_energy_hartree=frame.selected_energy_hartree,
        selected_energy_kind=(
            _enum_value(frame.selected_energy_kind)
            if frame.selected_energy_kind is not None
            else None
        ),
        frequency_count=frame.frequency_count,
        negative_frequency_count=frame.negative_frequency_count,
        running_time_seconds=frame.running_time_seconds,
    )


def _array_assignment_owner(
    assignment: ScientificArrayAssignment | None,
) -> tuple[str | None, UUID | None]:
    if assignment is None:
        return None, None
    for owner_kind, field_name in (
        ("molecular_orbital_result", "molecular_orbital_result_id"),
        ("atomic_population_series", "atomic_population_series_id"),
        ("polarizability_result", "polarizability_result_id"),
        ("nmr_result", "nmr_result_id"),
        ("nmr_shielding_tensor", "nmr_shielding_tensor_id"),
        ("bond_order_result", "bond_order_result_id"),
        ("single_point_property_result", "single_point_property_result_id"),
        ("electronic_state", "electronic_state_id"),
    ):
        owner_id = getattr(assignment, field_name)
        if owner_id is not None:
            return owner_kind, owner_id
    raise RuntimeError("ScientificArrayAssignment has no result owner")


def _array_population_name(array: ScientificArray) -> str | None:
    metadata = array.array_metadata or {}
    name = metadata.get("population_name")
    if isinstance(name, str) and name:
        return name
    source_field = metadata.get("source_field")
    prefix = "charge_spin_populations.populations."
    suffix = ".values"
    if (
        isinstance(source_field, str)
        and source_field.startswith(prefix)
        and source_field.endswith(suffix)
    ):
        candidate = source_field[len(prefix) : -len(suffix)]
        return candidate or None
    return None


def _frame_select(*, lightweight: bool = False) -> Any:
    """Build frame joins, optionally limiting columns for list summaries.

    A full ``MolecularTopology`` includes a PostgreSQL RDKit ``mol`` value.
    Deserializing that value for every row is unnecessary for a list page,
    which only displays the topology id and serialized SMILES.
    """

    statement = (
        select(
            CalculationFrame,
            CalculationSegment,
            ParseRevision,
            ArtifactFile,
            Geometry,
            MolecularTopology,
        )
        .join(
            CalculationSegment,
            col(CalculationFrame.segment_id) == col(CalculationSegment.id),
        )
        .join(
            ParseRevision,
            col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
        )
        .join(
            ArtifactFile,
            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
        )
        .join(Geometry, col(CalculationFrame.geometry_id) == col(Geometry.id))
        .join(
            MolecularTopology,
            col(Geometry.topology_id) == col(MolecularTopology.id),
        )
    )
    if lightweight:
        frame_orm = cast(Any, CalculationFrame)
        segment_orm = cast(Any, CalculationSegment)
        revision_orm = cast(Any, ParseRevision)
        artifact_orm = cast(Any, ArtifactFile)
        geometry_orm = cast(Any, Geometry)
        topology_orm = cast(Any, MolecularTopology)
        statement = statement.options(
            load_only(
                frame_orm.id,
                frame_orm.parse_revision_id,
                frame_orm.segment_id,
                frame_orm.frame_index,
                frame_orm.file_frame_index,
                frame_orm.frame_role,
                frame_orm.geometry_id,
                frame_orm.topology_derivation_id,
                frame_orm.charge,
                frame_orm.multiplicity,
                frame_orm.coordinate_decimal_places,
                frame_orm.scf_status,
                frame_orm.optimization_status,
                frame_orm.selected_energy_hartree,
                frame_orm.selected_energy_kind,
                frame_orm.frequency_count,
                frame_orm.negative_frequency_count,
                frame_orm.running_time_seconds,
            ),
            load_only(
                segment_orm.id,
                segment_orm.segment_index,
                segment_orm.protocol_id,
            ),
            load_only(revision_orm.id, revision_orm.artifact_file_id),
            load_only(artifact_orm.id, artifact_orm.original_filename),
            load_only(geometry_orm.id, geometry_orm.topology_id),
            load_only(topology_orm.id, topology_orm.canonical_isomeric_smiles),
        )
    return statement


def _artifact_summary(
    artifact: ArtifactFile,
    *,
    running_time_seconds: float | None = None,
) -> ArtifactSummary:
    ingestion = artifact.ingestion
    return ArtifactSummary(
        id=_required_uuid(artifact.id, "ArtifactFile"),
        project_id=artifact.project_id,
        created_by_user_id=artifact.created_by_user_id,
        visibility=_enum_value(artifact.visibility),
        original_filename=artifact.original_filename,
        content_sha256=artifact.content_sha256,
        size_bytes=artifact.size_bytes,
        media_type=artifact.media_type,
        artifact_kind=_enum_value(artifact.artifact_kind),
        storage_status=_enum_value(artifact.storage_status),
        storage_verified_at=artifact.storage_verified_at,
        preview_available=artifact_preview_available(artifact.media_type),
        ingestion_status=_enum_value(ingestion.status) if ingestion is not None else None,
        source_frame_count=(ingestion.source_frame_count if ingestion is not None else None),
        transition_state_frame_count=(
            ingestion.transition_state_frame_count if ingestion is not None else None
        ),
        running_time_seconds=running_time_seconds,
        ingestion_error_code=ingestion.error_code if ingestion is not None else None,
        ingestion_error_message=ingestion.error_message if ingestion is not None else None,
    )


def _encode_artifact_cursor(artifact: ArtifactFile) -> str:
    if artifact.id is None or artifact.created_at is None:
        raise RuntimeError("persisted artifact is missing cursor fields")
    value = f"{artifact.created_at.isoformat()}|{artifact.id}"
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_artifact_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_at_raw, artifact_id_raw = decoded.split("|", 1)
        created_at = datetime.fromisoformat(created_at_raw)
        artifact_id = UUID(artifact_id_raw)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid artifact cursor") from error
    if created_at.tzinfo is None:
        raise ValueError("invalid artifact cursor")
    return created_at, artifact_id


def _ordered_sort_expression(expression: Any, direction: str) -> Any:
    """Return a null-last sort expression after validating the direction."""

    if direction == "asc":
        return expression.asc().nulls_last()
    if direction == "desc":
        return expression.desc().nulls_last()
    raise ValueError("sort_direction must be asc or desc")


class ArtifactQueryService(UseCaseService):  # type: ignore[misc]
    """Browse immutable calculation and manifest artifacts."""

    @query  # type: ignore[untyped-decorator]
    async def list_artifacts(
        cls,
        artifact_id: UUID | None = None,
        artifact_kind: ArtifactKind | None = None,
        project_id: UUID | None = None,
        content_sha256: str | None = None,
        storage_status: StorageStatus | None = None,
        ingestion_status: ArtifactIngestionStatus | None = None,
        original_filename_contains: str | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
        cursor: str | None = None,
        sort_by: str = "created_at",
        sort_direction: str = "desc",
    ) -> ArtifactPage:
        """List artifact catalogue entries without exposing RustFS credentials."""

        latest_running_time = (
            select(col(ParseRevision.running_time_seconds))
            .where(col(ParseRevision.artifact_file_id) == col(ArtifactFile.id))
            .order_by(
                col(ParseRevision.revision_number).desc(),
                col(ParseRevision.id).desc(),
            )
            .limit(1)
            .correlate(ArtifactFile)
            .scalar_subquery()
            .label("running_time_seconds")
        )
        artifact_sort_fields = {
            "created_at": col(ArtifactFile.created_at),
            "original_filename": col(ArtifactFile.original_filename),
            "size_bytes": col(ArtifactFile.size_bytes),
            "artifact_kind": col(ArtifactFile.artifact_kind),
            "storage_status": col(ArtifactFile.storage_status),
            "running_time_seconds": latest_running_time,
        }
        sort_expression = artifact_sort_fields.get(sort_by)
        if sort_expression is None:
            raise ValueError(
                "sort_by must be created_at, original_filename, size_bytes, artifact_kind, "
                "storage_status, or running_time_seconds"
            )
        if cursor is not None and (sort_by != "created_at" or sort_direction != "desc"):
            raise ValueError("cursor pagination only supports created_at descending order")

        count_statement = sqlmodel_select(func.count()).select_from(ArtifactFile)
        statement = sqlmodel_select(ArtifactFile, latest_running_time).options(
            joinedload(cast(Any, ArtifactFile.ingestion))
        )
        active_criterion = col(ArtifactFile.storage_status) != StorageStatus.RETIRED
        count_statement = count_statement.where(active_criterion)
        statement = statement.where(active_criterion)
        scope = await query_visibility_scope(ProjectPermission.ARTIFACT_READ, project_id=project_id)
        visibility_criterion = scope.artifact_predicate()
        count_statement = count_statement.where(visibility_criterion)
        statement = statement.where(visibility_criterion)
        if artifact_kind is not None:
            criterion = col(ArtifactFile.artifact_kind) == artifact_kind
            count_statement = count_statement.where(criterion)
            statement = statement.where(criterion)
        for exact_field, exact_value in (
            (ArtifactFile.id, artifact_id),
            (ArtifactFile.project_id, project_id),
            (ArtifactFile.content_sha256, content_sha256),
            (ArtifactFile.storage_status, storage_status),
        ):
            if exact_value is not None:
                criterion = col(exact_field) == exact_value
                count_statement = count_statement.where(criterion)
                statement = statement.where(criterion)
        if ingestion_status is not None:
            criterion = (
                select(literal(1))
                .select_from(ArtifactIngestion)
                .where(
                    col(ArtifactIngestion.artifact_file_id) == col(ArtifactFile.id),
                    col(ArtifactIngestion.status) == ingestion_status,
                )
                .exists()
            )
            count_statement = count_statement.where(criterion)
            statement = statement.where(criterion)
        if original_filename_contains is not None:
            criterion = col(ArtifactFile.original_filename).ilike(f"%{original_filename_contains}%")
            count_statement = count_statement.where(criterion)
            statement = statement.where(criterion)
        # Presence of the cursor parameter selects keyset mode.  An empty value
        # represents its first page and therefore avoids an exact COUNT query.
        cursor_mode = cursor is not None
        decoded_cursor = _decode_artifact_cursor(cursor) if cursor else None
        if decoded_cursor is not None:
            cursor_created_at, cursor_id = decoded_cursor
            statement = statement.where(
                or_(
                    col(ArtifactFile.created_at) < cursor_created_at,
                    and_(
                        col(ArtifactFile.created_at) == cursor_created_at,
                        col(ArtifactFile.id) > cursor_id,
                    ),
                )
            )
        statement = statement.order_by(
            _ordered_sort_expression(sort_expression, sort_direction),
            col(ArtifactFile.id),
        )
        if not cursor_mode:
            statement = statement.offset(offset)
        statement = statement.limit(limit + 1)
        async with session_factory() as session:
            total = -1 if cursor_mode else int((await session.exec(count_statement)).one())
            rows = (await session.exec(statement)).all()
        has_more = len(rows) > limit
        artifact_rows = rows[:limit]
        next_cursor = (
            _encode_artifact_cursor(artifact_rows[-1][0]) if has_more and artifact_rows else None
        )
        return ArtifactPage(
            items=[
                _artifact_summary(artifact, running_time_seconds=running_time_seconds)
                for artifact, running_time_seconds in artifact_rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset, next_cursor=next_cursor),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_artifact(cls, artifact_id: UUID) -> ArtifactSummary | None:
        scope = await query_visibility_scope(ProjectPermission.ARTIFACT_READ)
        latest_running_time = (
            select(col(ParseRevision.running_time_seconds))
            .where(col(ParseRevision.artifact_file_id) == col(ArtifactFile.id))
            .order_by(
                col(ParseRevision.revision_number).desc(),
                col(ParseRevision.id).desc(),
            )
            .limit(1)
            .correlate(ArtifactFile)
            .scalar_subquery()
            .label("running_time_seconds")
        )
        async with session_factory() as session:
            row = (
                await session.exec(
                    sqlmodel_select(ArtifactFile, latest_running_time)
                    .options(joinedload(cast(Any, ArtifactFile.ingestion)))
                    .where(
                        col(ArtifactFile.id) == artifact_id,
                        scope.artifact_predicate(),
                    )
                )
            ).first()
        if row is None:
            return None
        artifact, running_time_seconds = row
        return _artifact_summary(artifact, running_time_seconds=running_time_seconds)


def molecular_formula_range_predicates(
    query: MolecularFormulaRangeQuery,
) -> list[Any]:
    """Encode vector ranges as AND-combined PostgreSQL array comparisons."""

    vector = cast(Any, col(MolecularFormula.element_count_vector))
    predicates: list[Any] = []
    exact_tokens: list[str] = []
    for array_index, (minimum, maximum) in enumerate(
        zip(query.minimum_counts, query.maximum_counts, strict=True),
        start=1,
    ):
        element_count = vector[array_index]
        if minimum is not None:
            predicates.append(element_count >= minimum)
        if maximum is not None:
            predicates.append(element_count <= maximum)
        if minimum is not None and minimum == maximum:
            exact_tokens.append(f"{array_index}:{minimum}")
    if exact_tokens:
        tokens = cast(Any, col(MolecularFormula.element_count_tokens))
        predicates.append(tokens.contains(sql_cast(array(exact_tokens), ARRAY(Text))))
    return predicates


class MolecularFormulaQueryService(UseCaseService):  # type: ignore[misc]
    """Search formula identities by inclusive per-element count ranges."""

    @query  # type: ignore[untyped-decorator]
    async def search_formulas(
        cls,
        minimum_counts: list[int | None],
        maximum_counts: list[int | None],
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> MolecularFormulaPage:
        ranges = MolecularFormulaRangeQuery(
            minimum_counts=minimum_counts,
            maximum_counts=maximum_counts,
        )
        predicates = molecular_formula_range_predicates(ranges)
        count_statement = (
            sqlmodel_select(func.count()).select_from(MolecularFormula).where(*predicates)
        )
        statement = (
            sqlmodel_select(MolecularFormula)
            .where(*predicates)
            .order_by(col(MolecularFormula.hill_formula), col(MolecularFormula.id))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.exec(count_statement)).one())
            formulas = (await session.exec(statement)).all()
        return MolecularFormulaPage(
            items=[
                MolecularFormulaSummary(
                    id=_required_uuid(formula.id, "MolecularFormula"),
                    hill_formula=formula.hill_formula,
                    atom_count=formula.atom_count,
                    composition_hash=formula.composition_hash,
                    element_count_vector=list(formula.element_count_vector),
                )
                for formula in formulas
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )


class MolecularTopologyQueryService(UseCaseService):  # type: ignore[misc]
    """Search reusable molecular graphs before expanding geometry or reaction data."""

    @classmethod
    async def list_visible_topologies(
        cls,
        *,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> MolecularTopologySearchPage:
        """Page through visible topologies without opening unfiltered structure search."""

        scope = await query_visibility_scope()
        predicates = (
            [] if scope.unrestricted else [topology_id_is_visible(scope, col(MolecularTopology.id))]
        )
        count_statement = (
            sqlmodel_select(func.count()).select_from(MolecularTopology).where(*predicates)
        )
        statement = (
            sqlmodel_select(MolecularTopology, MolecularFormula)
            .add_columns(
                col(MolecularTopology.morgan_bfp).is_not(None).label("morgan_bfp_available")
            )
            .options(
                defer(cast(Any, MolecularTopology.mol)),
                defer(cast(Any, MolecularTopology.morgan_bfp)),
            )
            .join(
                MolecularFormula,
                col(MolecularTopology.formula_id) == col(MolecularFormula.id),
            )
            .where(*predicates)
            .order_by(
                col(MolecularFormula.hill_formula),
                col(MolecularTopology.canonical_isomeric_smiles),
                col(MolecularTopology.id),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.exec(count_statement)).one())
            rows = (await session.execute(statement)).all()
        return MolecularTopologySearchPage(
            items=[
                MolecularTopologySearchResult(
                    id=_required_uuid(topology.id, "MolecularTopology"),
                    formula_id=topology.formula_id,
                    hill_formula=formula.hill_formula,
                    formula_composition_hash=formula.composition_hash,
                    canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
                    graph_hash=topology.graph_hash,
                    atom_count=topology.atom_count,
                    heavy_atom_count=topology.heavy_atom_count,
                    formal_charge=topology.formal_charge,
                    radical_electron_count=topology.radical_electron_count,
                    fragment_count=topology.fragment_count,
                    stereo_status=_enum_value(topology.stereo_status),
                    sanitization_status=_enum_value(topology.sanitization_status),
                    sanitization_error=topology.sanitization_error,
                    substructure_match_count=None,
                    morgan_bfp_schema_version=topology.morgan_bfp_schema_version,
                    morgan_bfp_available=bool(morgan_bfp_available),
                    similarity_score=None,
                    molecular_weight=None,
                    logp=None,
                    tpsa=None,
                    hba_count=None,
                    hbd_count=None,
                    ring_count=None,
                    scaffold_smiles=None,
                )
                for topology, formula, morgan_bfp_available in rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def search_topologies(
        cls,
        project_id: UUID | None = None,
        topology_id: UUID | None = None,
        formula_id: UUID | None = None,
        formula_composition_hash: str | None = None,
        formula_hill_formula: str | None = None,
        exact_smiles: str | None = None,
        mol_block: str | None = None,
        similarity_smiles: str | None = None,
        similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
        minimum_similarity: float | None = None,
        smarts: str | None = None,
        match_chirality: bool = False,
        minimum_substructure_matches: int | None = None,
        unique_substructure_matches: bool = True,
        formal_charge: int | None = None,
        atom_count: int | None = None,
        heavy_atom_count: int | None = None,
        stereo_status: StereoStatus | None = None,
        sanitization_status: TopologySanitizationStatus | None = None,
        minimum_molecular_weight: float | None = None,
        maximum_molecular_weight: float | None = None,
        minimum_logp: float | None = None,
        maximum_logp: float | None = None,
        minimum_tpsa: float | None = None,
        maximum_tpsa: float | None = None,
        minimum_hba_count: int | None = None,
        maximum_hba_count: int | None = None,
        minimum_hbd_count: int | None = None,
        maximum_hbd_count: int | None = None,
        minimum_ring_count: int | None = None,
        maximum_ring_count: int | None = None,
        scaffold_smiles: str | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> MolecularTopologySearchPage:
        """Apply Formula prefilters before topology and fingerprint matching.

        ``exact_smiles`` is a B-tree lookup against the normalized display identity.
        ``has_smarts`` uses the ``mol`` GiST index. Count and chirality predicates are
        applied only after that indexed candidate predicate, keeping cartridge
        functions bounded by the structural candidate set. Similarity uses the
        versioned Morgan bfp GiST projection and never computes fingerprints per row.
        """

        settings = get_settings()
        enforce_structure_input_budget(
            {
                "exact_smiles": exact_smiles,
                "mol_block": mol_block,
                "similarity_smiles": similarity_smiles,
                "smarts": smarts,
                "scaffold_smiles": scaffold_smiles,
            },
            maximum_characters=settings.structure_query_max_characters,
        )

        scope = await query_visibility_scope(project_id=project_id)

        search = MolecularTopologySearchQuery(
            topology_id=topology_id,
            formula_id=formula_id,
            formula_composition_hash=formula_composition_hash,
            formula_hill_formula=formula_hill_formula,
            exact_smiles=exact_smiles,
            mol_block=mol_block,
            similarity_smiles=similarity_smiles,
            similarity_metric=similarity_metric,
            minimum_similarity=minimum_similarity,
            smarts=smarts,
            match_chirality=match_chirality,
            minimum_substructure_matches=minimum_substructure_matches,
            unique_substructure_matches=unique_substructure_matches,
            formal_charge=formal_charge,
            atom_count=atom_count,
            heavy_atom_count=heavy_atom_count,
            stereo_status=stereo_status,
            sanitization_status=sanitization_status,
            minimum_molecular_weight=minimum_molecular_weight,
            maximum_molecular_weight=maximum_molecular_weight,
            minimum_logp=minimum_logp,
            maximum_logp=maximum_logp,
            minimum_tpsa=minimum_tpsa,
            maximum_tpsa=maximum_tpsa,
            minimum_hba_count=minimum_hba_count,
            maximum_hba_count=maximum_hba_count,
            minimum_hbd_count=minimum_hbd_count,
            maximum_hbd_count=maximum_hbd_count,
            minimum_ring_count=minimum_ring_count,
            maximum_ring_count=maximum_ring_count,
            scaffold_smiles=scaffold_smiles,
        )
        predicates: list[Any] = []
        if not scope.unrestricted:
            predicates.append(topology_id_is_visible(scope, col(MolecularTopology.id)))
        if search.topology_id is not None:
            predicates.append(col(MolecularTopology.id) == search.topology_id)
        if search.formula_id is not None:
            predicates.append(col(MolecularTopology.formula_id) == search.formula_id)
        if search.formula_composition_hash is not None:
            predicates.append(
                col(MolecularFormula.composition_hash) == search.formula_composition_hash
            )
        if search.formula_hill_formula is not None:
            predicates.append(col(MolecularFormula.hill_formula) == search.formula_hill_formula)
        exact_topology_smiles = search.exact_smiles
        if search.mol_block is not None:
            mol_block_molecule = Chem.MolFromMolBlock(
                search.mol_block,
                sanitize=True,
                removeHs=True,
                strictParsing=False,
            )
            if mol_block_molecule is None:
                raise ValueError("validated mol_block could not be parsed")
            exact_topology_smiles = Chem.MolToSmiles(
                mol_block_molecule,
                canonical=True,
                isomericSmiles=True,
            )
        if exact_topology_smiles is not None:
            predicates.append(
                col(MolecularTopology.canonical_isomeric_smiles) == exact_topology_smiles
            )
        if search.formal_charge is not None:
            predicates.append(col(MolecularTopology.formal_charge) == search.formal_charge)
        if search.atom_count is not None:
            predicates.append(col(MolecularTopology.atom_count) == search.atom_count)
        if search.heavy_atom_count is not None:
            predicates.append(col(MolecularTopology.heavy_atom_count) == search.heavy_atom_count)
        if search.stereo_status is not None:
            predicates.append(col(MolecularTopology.stereo_status) == search.stereo_status)
        if search.sanitization_status is not None:
            predicates.append(
                col(MolecularTopology.sanitization_status) == search.sanitization_status
            )

        candidate_predicates = list(predicates)
        requires_candidate_limit = any(
            value is not None
            for value in (
                search.minimum_molecular_weight,
                search.maximum_molecular_weight,
                search.minimum_logp,
                search.maximum_logp,
                search.minimum_tpsa,
                search.maximum_tpsa,
                search.minimum_hba_count,
                search.maximum_hba_count,
                search.minimum_hbd_count,
                search.maximum_hbd_count,
                search.minimum_ring_count,
                search.maximum_ring_count,
                search.scaffold_smiles,
            )
        )

        sanitizable = (
            col(MolecularTopology.sanitization_status) == TopologySanitizationStatus.SANITIZED
        )
        molecular_weight = case(
            (sanitizable, cast(Any, mol_amw(MolecularTopology.mol))),
            else_=None,
        )
        logp = case((sanitizable, cast(Any, mol_logp(MolecularTopology.mol))), else_=None)
        tpsa = case((sanitizable, cast(Any, mol_tpsa(MolecularTopology.mol))), else_=None)
        hba_count = case((sanitizable, cast(Any, mol_hba(MolecularTopology.mol))), else_=None)
        hbd_count = case((sanitizable, cast(Any, mol_hbd(MolecularTopology.mol))), else_=None)
        ring_count = case(
            (sanitizable, cast(Any, mol_numrings(MolecularTopology.mol))),
            else_=None,
        )
        scaffold = case(
            (
                sanitizable,
                sql_cast(
                    mol_to_smiles(cast(Any, mol_murckoscaffold(MolecularTopology.mol))),
                    Text,
                ),
            ),
            else_=None,
        )
        for expression, minimum, maximum in (
            (molecular_weight, search.minimum_molecular_weight, search.maximum_molecular_weight),
            (logp, search.minimum_logp, search.maximum_logp),
            (tpsa, search.minimum_tpsa, search.maximum_tpsa),
            (hba_count, search.minimum_hba_count, search.maximum_hba_count),
            (hbd_count, search.minimum_hbd_count, search.maximum_hbd_count),
            (ring_count, search.minimum_ring_count, search.maximum_ring_count),
        ):
            if minimum is not None:
                predicates.append(expression >= minimum)
            if maximum is not None:
                predicates.append(expression <= maximum)
        if search.scaffold_smiles is not None:
            predicates.append(scaffold == search.scaffold_smiles)

        match_count: Any = literal(None)
        if search.smarts is not None:
            query_molecule = qmol_from_smarts(cast(CString, sql_cast(search.smarts, CString)))
            # This condition has a matching ``gist_mol_ops`` strategy and must remain
            # present even when a count/chirality predicate is also requested.
            smarts_predicate = rdkit_col(MolecularTopology.mol).has_smarts(search.smarts)
            predicates.append(smarts_predicate)
            candidate_predicates.append(smarts_predicate)
            requires_candidate_limit = True
            if search.match_chirality:
                match_count = substruct_count_chiral(
                    MolecularTopology.mol,
                    query_molecule,
                    cast(Boolean, search.unique_substructure_matches),
                )
            else:
                match_count = substruct_count(
                    MolecularTopology.mol,
                    query_molecule,
                    search.unique_substructure_matches,
                )
            if search.minimum_substructure_matches is not None:
                predicates.append(match_count >= search.minimum_substructure_matches)
            elif search.match_chirality:
                predicates.append(match_count >= 1)

        similarity_score: Any = literal(None)
        similarity_fingerprint: Any | None = None
        similarity_threshold_setting: str | None = None
        fingerprint_column: Any | None = None
        if search.similarity_smiles is not None:
            query_molecule = Chem.MolFromSmiles(search.similarity_smiles)
            if query_molecule is None:
                raise RuntimeError("validated similarity SMILES could not be reconstructed")
            explicit_query_smiles = Chem.MolToSmiles(
                Chem.AddHs(query_molecule),
                canonical=True,
                isomericSmiles=True,
                allHsExplicit=True,
            )
            similarity_query_molecule = mol_from_smiles(
                cast(CString, sql_cast(explicit_query_smiles, CString))
            )
            similarity_fingerprint = morganbv_fp(
                similarity_query_molecule,
                MORGAN_BFP_RADIUS,
            )
            fingerprint_column = cast(Any, col(MolecularTopology.morgan_bfp))
            assert similarity_fingerprint is not None
            assert fingerprint_column is not None
            similarity_score = (
                tanimoto_sml(fingerprint_column, similarity_fingerprint)
                if search.similarity_metric == "tanimoto"
                else dice_sml(fingerprint_column, similarity_fingerprint)
            )
            if search.minimum_similarity is not None:
                threshold_operator = "%" if search.similarity_metric == "tanimoto" else "#"
                threshold_predicate = fingerprint_column.op(threshold_operator)(
                    similarity_fingerprint
                )
                predicates.append(threshold_predicate)
                candidate_predicates.append(threshold_predicate)
                requires_candidate_limit = True
                similarity_threshold_setting = f"rdkit.{search.similarity_metric}_threshold"

        count_statement = (
            sqlmodel_select(func.count())
            .select_from(MolecularTopology)
            .join(
                MolecularFormula,
                col(MolecularTopology.formula_id) == col(MolecularFormula.id),
            )
            .where(*predicates)
        )
        order_by: list[Any] = [
            col(MolecularFormula.hill_formula),
            col(MolecularTopology.canonical_isomeric_smiles),
            col(MolecularTopology.id),
        ]
        if similarity_fingerprint is not None and fingerprint_column is not None:
            nearest_operator = "<%>" if search.similarity_metric == "tanimoto" else "<#>"
            order_by = [
                fingerprint_column.op(nearest_operator)(similarity_fingerprint),
                *order_by,
            ]
        statement = (
            cast(Any, sqlmodel_select)(
                MolecularTopology,
                MolecularFormula,
                match_count.label("substructure_match_count"),
                similarity_score.label("similarity_score"),
                molecular_weight.label("molecular_weight"),
                logp.label("logp"),
                tpsa.label("tpsa"),
                hba_count.label("hba_count"),
                hbd_count.label("hbd_count"),
                ring_count.label("ring_count"),
                scaffold.label("scaffold_smiles"),
                col(MolecularTopology.morgan_bfp).is_not(None).label("morgan_bfp_available"),
            )
            .options(
                defer(cast(Any, MolecularTopology.mol)),
                defer(cast(Any, MolecularTopology.morgan_bfp)),
            )
            .join(
                MolecularFormula,
                col(MolecularTopology.formula_id) == col(MolecularFormula.id),
            )
            .where(*predicates)
            .order_by(*order_by)
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            if similarity_threshold_setting is not None and search.minimum_similarity is not None:
                await session.exec(
                    sqlmodel_select(
                        func.set_config(
                            similarity_threshold_setting,
                            str(search.minimum_similarity),
                            True,
                        )
                    )
                )
            if requires_candidate_limit:
                await _enforce_candidate_limit(
                    sqlmodel_select(col(MolecularTopology.id))
                    .select_from(MolecularTopology)
                    .join(
                        MolecularFormula,
                        col(MolecularTopology.formula_id) == col(MolecularFormula.id),
                    )
                    .where(*candidate_predicates),
                    label="topology structure query",
                    session=session,
                )
            total = int((await session.exec(count_statement)).one())
            rows = (await session.exec(statement)).all()
        return MolecularTopologySearchPage(
            items=[
                MolecularTopologySearchResult(
                    id=_required_uuid(topology.id, "MolecularTopology"),
                    formula_id=topology.formula_id,
                    hill_formula=formula.hill_formula,
                    formula_composition_hash=formula.composition_hash,
                    canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
                    graph_hash=topology.graph_hash,
                    atom_count=topology.atom_count,
                    heavy_atom_count=topology.heavy_atom_count,
                    formal_charge=topology.formal_charge,
                    radical_electron_count=topology.radical_electron_count,
                    fragment_count=topology.fragment_count,
                    stereo_status=_enum_value(topology.stereo_status),
                    sanitization_status=_enum_value(topology.sanitization_status),
                    sanitization_error=topology.sanitization_error,
                    substructure_match_count=(
                        int(match_count_value) if match_count_value is not None else None
                    ),
                    morgan_bfp_schema_version=topology.morgan_bfp_schema_version,
                    morgan_bfp_available=bool(morgan_bfp_available),
                    similarity_score=(
                        float(similarity_score_value)
                        if similarity_score_value is not None
                        else None
                    ),
                    molecular_weight=(
                        float(molecular_weight_value)
                        if molecular_weight_value is not None
                        else None
                    ),
                    logp=float(logp_value) if logp_value is not None else None,
                    tpsa=float(tpsa_value) if tpsa_value is not None else None,
                    hba_count=int(hba_count_value) if hba_count_value is not None else None,
                    hbd_count=int(hbd_count_value) if hbd_count_value is not None else None,
                    ring_count=int(ring_count_value) if ring_count_value is not None else None,
                    scaffold_smiles=str(scaffold_value) if scaffold_value else None,
                )
                for (
                    topology,
                    formula,
                    match_count_value,
                    similarity_score_value,
                    molecular_weight_value,
                    logp_value,
                    tpsa_value,
                    hba_count_value,
                    hbd_count_value,
                    ring_count_value,
                    scaffold_value,
                    morgan_bfp_available,
                ) in rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )


class LogicalReactionQueryService(UseCaseService):  # type: ignore[misc]
    """Browse topology-defined logical reactions."""

    @query  # type: ignore[untyped-decorator]
    async def list_logical_reactions(
        cls,
        project_id: UUID | None = None,
        topology_id: UUID | None = None,
        reaction_key: str | None = None,
        label: str | None = None,
        reaction_hash: str | None = None,
        reaction_class: ReactionClass | None = None,
        reaction_smarts: str | None = None,
        similarity_reaction_smiles: str | None = None,
        similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
        reactant_mol_block: str | None = None,
        product_mol_block: str | None = None,
        minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
        maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
        minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
        maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
        has_activation_gibbs_free_energy: bool | None = None,
        has_reaction_gibbs_free_energy: bool | None = None,
        reactant_product_changed: bool | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
        filter_expression: str | None = None,
        sort_by: str = "default",
        sort_direction: str = "asc",
    ) -> LogicalReactionPage:
        """List reactions, optionally restricting them by participant topology."""

        if sort_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        reaction_sort_fields = {
            "created_at": col(LogicalReaction.created_at),
            "reaction_key": col(LogicalReaction.reaction_key),
            "reaction_class": col(LogicalReaction.reaction_class),
        }
        energy_sort_fields = {
            "minimum_activation_gibbs_free_energy": (
                "minimum_activation_gibbs_free_energy_kcal_mol"
            ),
            "minimum_reaction_gibbs_free_energy": ("minimum_reaction_gibbs_free_energy_kcal_mol"),
        }
        if sort_by not in {"default", "similarity", *reaction_sort_fields, *energy_sort_fields}:
            raise ValueError(
                "sort_by must be default, similarity, created_at, reaction_key, reaction_class, "
                "minimum_activation_gibbs_free_energy, or minimum_reaction_gibbs_free_energy"
            )
        if sort_by == "similarity" and similarity_reaction_smiles is None:
            raise ValueError("similarity sorting requires similarity_reaction_smiles")

        settings = get_settings()
        flat_filter_values = (
            topology_id,
            reaction_key,
            label,
            reaction_hash,
            reaction_class,
            reaction_smarts,
            similarity_reaction_smiles,
            reactant_mol_block,
            product_mol_block,
            minimum_activation_gibbs_free_energy_kcal_mol,
            maximum_activation_gibbs_free_energy_kcal_mol,
            minimum_reaction_gibbs_free_energy_kcal_mol,
            maximum_reaction_gibbs_free_energy_kcal_mol,
            reactant_product_changed,
            created_after,
            created_before,
        )
        if filter_expression is not None and any(value is not None for value in flat_filter_values):
            raise ValueError("filter_expression conflicts with flat logical reaction filters")
        structure_inputs = {
            "reaction_smarts": reaction_smarts,
            "similarity_reaction_smiles": similarity_reaction_smiles,
            "reactant_mol_block": reactant_mol_block,
            "product_mol_block": product_mol_block,
        }
        enforce_structure_input_budget(
            structure_inputs,
            maximum_characters=settings.structure_query_max_characters,
        )
        if reaction_smarts is not None and (
            reactant_mol_block is not None or product_mol_block is not None
        ):
            raise ValueError("reaction_smarts conflicts with reaction MolBlock inputs")
        reaction_smarts = reaction_smarts or reaction_smarts_from_mol_blocks(
            reactant_mol_block,
            product_mol_block,
        )
        scope = await query_visibility_scope(project_id=project_id)
        predicates: list[Any] = [logical_reaction_id_is_visible(scope, col(LogicalReaction.id))]
        if topology_id is not None:
            reaction_ids = select(col(LogicalReactionParticipant.logical_reaction_id)).where(
                col(LogicalReactionParticipant.topology_id) == topology_id
            )
            predicates.append(col(LogicalReaction.id).in_(reaction_ids))
        if reaction_key is not None:
            predicates.append(col(LogicalReaction.reaction_key) == reaction_key)
        if label is not None:
            predicates.append(col(LogicalReaction.label) == label)
        if reaction_hash is not None:
            predicates.append(col(LogicalReaction.reaction_hash) == reaction_hash)
        if reaction_class is not None:
            predicates.append(col(LogicalReaction.reaction_class) == reaction_class)
        if reactant_product_changed is not None:
            changed_expression = _reaction_topology_changed_expression()
            predicates.append(changed_expression == reactant_product_changed)
        reaction_structure_predicates: list[Any] = []
        if reaction_smarts is not None:
            if rdChemReactions.ReactionFromSmarts(reaction_smarts) is None:
                raise ValueError("reaction_smarts must be a valid reaction SMARTS")
            structure_query = reaction_from_smarts(
                cast(CString, sql_cast(reaction_smarts, CString))
            )
            stored_reaction = cast(Any, col(MappedReaction.reaction))
            structure_predicate = stored_reaction.op("@>")(structure_query)
            reaction_structure_predicates.append(structure_predicate)
            mapped_structure_ids = (
                select(col(MappedReaction.logical_reaction_id))
                .where(
                    mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                    structure_predicate,
                )
                .correlate(None)
            )
            predicates.append(col(LogicalReaction.id).in_(mapped_structure_ids))
        similarity_score_expression: Any = literal(None)
        similarity_rank: Any | None = None
        if similarity_reaction_smiles is not None:
            if (
                rdChemReactions.ReactionFromSmarts(
                    similarity_reaction_smiles,
                    useSmiles=True,
                )
                is None
            ):
                raise ValueError("similarity_reaction_smiles must be a valid reaction SMILES")
            similarity_query_reaction = reaction_from_smiles(
                cast(CString, sql_cast(similarity_reaction_smiles, CString))
            )
            stored_reaction_fp = cast(Any, col(MappedReaction.reaction_structural_bfp))
            similarity_fingerprint = reaction_structural_bfp(
                similarity_query_reaction,
                REACTION_STRUCTURAL_BFP_RADIUS,
            )
            similarity_score_expression = (
                tanimoto_sml(stored_reaction_fp, similarity_fingerprint)
                if similarity_metric == SimilarityMetric.tanimoto
                else dice_sml(stored_reaction_fp, similarity_fingerprint)
            )
            similarity_rank = (
                select(
                    col(MappedReaction.logical_reaction_id).label("logical_reaction_id"),
                    func.max(similarity_score_expression).label("similarity_score"),
                )
                .where(
                    mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                    stored_reaction_fp.is_not(None),
                    *reaction_structure_predicates,
                )
                .group_by(col(MappedReaction.logical_reaction_id))
                .subquery()
            )
            predicates.append(
                col(LogicalReaction.id).in_(select(col(similarity_rank.c.logical_reaction_id)))
            )
        if filter_expression is not None:
            predicates.append(
                logical_reaction_filter_expression_predicate(
                    filter_expression,
                    scope,
                    reaction_structure_predicates,
                )
            )
        _validate_range(
            minimum_activation_gibbs_free_energy_kcal_mol,
            maximum_activation_gibbs_free_energy_kcal_mol,
            minimum_name="minimum_activation_gibbs_free_energy_kcal_mol",
            maximum_name="maximum_activation_gibbs_free_energy_kcal_mol",
        )
        _validate_range(
            minimum_reaction_gibbs_free_energy_kcal_mol,
            maximum_reaction_gibbs_free_energy_kcal_mol,
            minimum_name="minimum_reaction_gibbs_free_energy_kcal_mol",
            maximum_name="maximum_reaction_gibbs_free_energy_kcal_mol",
        )
        if (
            minimum_activation_gibbs_free_energy_kcal_mol is not None
            or maximum_activation_gibbs_free_energy_kcal_mol is not None
            or minimum_reaction_gibbs_free_energy_kcal_mol is not None
            or maximum_reaction_gibbs_free_energy_kcal_mol is not None
            or has_activation_gibbs_free_energy
            or has_reaction_gibbs_free_energy
        ):
            mapped_screening_ids = select(col(MappedReaction.logical_reaction_id)).where(
                mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
            )
            if minimum_activation_gibbs_free_energy_kcal_mol is not None:
                mapped_screening_ids = mapped_screening_ids.where(
                    col(MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol)
                    >= minimum_activation_gibbs_free_energy_kcal_mol
                )
            if maximum_activation_gibbs_free_energy_kcal_mol is not None:
                mapped_screening_ids = mapped_screening_ids.where(
                    col(MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol)
                    <= maximum_activation_gibbs_free_energy_kcal_mol
                )
            if minimum_reaction_gibbs_free_energy_kcal_mol is not None:
                mapped_screening_ids = mapped_screening_ids.where(
                    col(MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol)
                    >= minimum_reaction_gibbs_free_energy_kcal_mol
                )
            if maximum_reaction_gibbs_free_energy_kcal_mol is not None:
                mapped_screening_ids = mapped_screening_ids.where(
                    col(MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol)
                    <= maximum_reaction_gibbs_free_energy_kcal_mol
                )
            if has_activation_gibbs_free_energy:
                mapped_screening_ids = mapped_screening_ids.where(
                    or_(
                        col(MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol).is_not(
                            None
                        ),
                        col(MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol).is_not(
                            None
                        ),
                    )
                )
            if has_reaction_gibbs_free_energy:
                mapped_screening_ids = mapped_screening_ids.where(
                    or_(
                        col(MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol).is_not(
                            None
                        ),
                        col(MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol).is_not(
                            None
                        ),
                    )
                )
            predicates.append(col(LogicalReaction.id).in_(mapped_screening_ids))
        _validate_range(
            created_after,
            created_before,
            minimum_name="created_after",
            maximum_name="created_before",
        )
        if created_after is not None:
            predicates.append(col(LogicalReaction.created_at) >= created_after)
        if created_before is not None:
            predicates.append(col(LogicalReaction.created_at) <= created_before)
        async with session_factory() as session:
            for reaction_structure_predicate in reaction_structure_predicates:
                await _enforce_candidate_limit(
                    sqlmodel_select(col(MappedReaction.id))
                    .select_from(MappedReaction)
                    .where(
                        mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                        reaction_structure_predicate,
                    ),
                    label="reaction structure query",
                    session=session,
                )
            total = int(
                (
                    await session.execute(
                        select(func.count()).select_from(LogicalReaction).where(*predicates)
                    )
                ).scalar_one()
            )
            if similarity_rank is not None and sort_by in {"default", "similarity"}:
                reaction_statement = (
                    select(LogicalReaction, similarity_rank.c.similarity_score)
                    .join(
                        similarity_rank,
                        col(LogicalReaction.id) == similarity_rank.c.logical_reaction_id,
                    )
                    .where(*predicates)
                    .order_by(
                        similarity_rank.c.similarity_score.desc().nulls_last(),
                        col(LogicalReaction.id),
                    )
                )
            else:
                reaction_statement = select(LogicalReaction).where(*predicates)
            if similarity_rank is None and sort_by == "default":
                reaction_statement = reaction_statement.order_by(
                    col(LogicalReaction.reactant_sort_key).nulls_last(),
                    col(LogicalReaction.created_at),
                    col(LogicalReaction.id),
                )
            elif similarity_rank is None and sort_by in energy_sort_fields:
                energy_field = energy_sort_fields[sort_by]
                energy_sort_keys = (
                    select(
                        col(MappedReaction.logical_reaction_id).label("logical_reaction_id"),
                        func.min(col(getattr(MappedReaction, energy_field))).label(
                            "energy_sort_key"
                        ),
                    )
                    .where(mapped_reaction_id_is_visible(scope, col(MappedReaction.id)))
                    .group_by(col(MappedReaction.logical_reaction_id))
                    .subquery()
                )
                reaction_statement = reaction_statement.outerjoin(
                    energy_sort_keys,
                    col(LogicalReaction.id) == energy_sort_keys.c.logical_reaction_id,
                ).order_by(
                    _ordered_sort_expression(energy_sort_keys.c.energy_sort_key, sort_direction),
                    col(LogicalReaction.id),
                )
            elif similarity_rank is None:
                reaction_statement = reaction_statement.order_by(
                    _ordered_sort_expression(reaction_sort_fields[sort_by], sort_direction),
                    col(LogicalReaction.id),
                )
            if similarity_rank is not None and sort_by in {"default", "similarity"}:
                reaction_rows = (
                    await session.execute(reaction_statement.offset(offset).limit(limit))
                ).all()
                reactions = [reaction for reaction, _similarity_score in reaction_rows]
                similarity_scores_by_reaction = {
                    _required_uuid(reaction.id, "LogicalReaction"): (
                        float(similarity_score) if similarity_score is not None else None
                    )
                    for reaction, similarity_score in reaction_rows
                }
            else:
                reactions = list(
                    (await session.execute(reaction_statement.offset(offset).limit(limit)))
                    .scalars()
                    .all()
                )
                similarity_scores_by_reaction = {}
            page_reaction_ids = [
                _required_uuid(reaction.id, "LogicalReaction") for reaction in reactions
            ]
            participant_rows = (
                (
                    await session.execute(
                        select(LogicalReactionParticipant, MolecularTopology)
                        .options(defer(MolecularTopology.mol))
                        .join(
                            MolecularTopology,
                            col(LogicalReactionParticipant.topology_id)
                            == col(MolecularTopology.id),
                        )
                        .where(
                            col(LogicalReactionParticipant.logical_reaction_id).in_(
                                page_reaction_ids
                            )
                        )
                        .order_by(
                            col(LogicalReactionParticipant.logical_reaction_id),
                            col(LogicalReactionParticipant.side),
                            col(LogicalReactionParticipant.participant_index),
                        )
                    )
                ).all()
                if page_reaction_ids
                else []
            )
            participants_by_reaction: dict[
                UUID, list[tuple[LogicalReactionParticipant, MolecularTopology]]
            ] = {}
            for participant, topology in participant_rows:
                participants_by_reaction.setdefault(participant.logical_reaction_id, []).append(
                    (participant, topology)
                )
            transition_rows = (
                (
                    await session.execute(
                        select(
                            col(MappedReaction.logical_reaction_id),
                            col(MappedReactionNodeGeometry.geometry_id),
                        )
                        .join(
                            MappedReactionNode,
                            col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
                        )
                        .join(
                            MappedReactionNodeGeometry,
                            col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                            == col(MappedReactionNode.id),
                        )
                        .where(
                            col(MappedReaction.logical_reaction_id).in_(page_reaction_ids),
                            col(MappedReactionNode.role) == "transition_state",
                            mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                            geometry_has_thermodynamic_property_predicate(
                                col(MappedReactionNodeGeometry.geometry_id)
                            ),
                        )
                        .order_by(
                            col(MappedReaction.logical_reaction_id),
                            col(MappedReactionNodeGeometry.geometry_id),
                        )
                    )
                ).all()
                if page_reaction_ids
                else []
            )
            transition_by_reaction: dict[UUID, UUID] = {}
            for logical_id, geometry_id in transition_rows:
                transition_by_reaction.setdefault(logical_id, geometry_id)
            barrier_rows = (
                (
                    await session.execute(
                        select(
                            col(MappedReaction.logical_reaction_id),
                            func.min(
                                col(MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol)
                            ),
                            func.max(
                                col(MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol)
                            ),
                            func.min(
                                col(MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol)
                            ),
                            func.max(
                                col(MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol)
                            ),
                        )
                        .where(
                            col(MappedReaction.logical_reaction_id).in_(page_reaction_ids),
                            mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                        )
                        .group_by(col(MappedReaction.logical_reaction_id))
                    )
                ).all()
                if page_reaction_ids
                else []
            )
            barriers_by_reaction = {
                logical_id: (minimum, maximum, reaction_minimum, reaction_maximum)
                for logical_id, minimum, maximum, reaction_minimum, reaction_maximum in barrier_rows
            }
        return LogicalReactionPage(
            items=[
                _reaction_summary(
                    reaction,
                    reactant_product_changed=_canonical_reactant_product_changed(
                        participants_by_reaction.get(
                            _required_uuid(reaction.id, "LogicalReaction"), []
                        )
                    ),
                    reactant_topology_ids=[
                        participant.topology_id
                        for participant, _topology in participants_by_reaction.get(
                            _required_uuid(reaction.id, "LogicalReaction"), []
                        )
                        if participant.side == "reactant"
                    ],
                    product_topology_ids=[
                        participant.topology_id
                        for participant, _topology in participants_by_reaction.get(
                            _required_uuid(reaction.id, "LogicalReaction"), []
                        )
                        if participant.side == "product"
                    ],
                    transition_state_geometry_id=transition_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction")
                    ),
                    minimum_activation_gibbs_free_energy_kcal_mol=barriers_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction"), (None, None, None, None)
                    )[0],
                    maximum_activation_gibbs_free_energy_kcal_mol=barriers_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction"), (None, None, None, None)
                    )[1],
                    minimum_reaction_gibbs_free_energy_kcal_mol=barriers_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction"), (None, None, None, None)
                    )[2],
                    maximum_reaction_gibbs_free_energy_kcal_mol=barriers_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction"), (None, None, None, None)
                    )[3],
                    similarity_score=similarity_scores_by_reaction.get(
                        _required_uuid(reaction.id, "LogicalReaction")
                    ),
                )
                for reaction in reactions
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_logical_reaction(
        cls,
        logical_reaction_id: UUID,
        project_id: UUID | None = None,
    ) -> LogicalReactionDetail | None:
        """Get one logical reaction with topology participants and mapped reactions."""

        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            reaction = (
                await session.execute(
                    select(LogicalReaction).where(
                        col(LogicalReaction.id) == logical_reaction_id,
                        logical_reaction_id_is_visible(scope, col(LogicalReaction.id)),
                    )
                )
            ).scalar_one_or_none()
            if reaction is None:
                return None
            participant_rows = [
                (participant, topology)
                for participant, topology in (
                    await session.execute(
                        select(LogicalReactionParticipant, MolecularTopology)
                        .options(defer(cast(Any, MolecularTopology.mol)))
                        .join(
                            MolecularTopology,
                            col(LogicalReactionParticipant.topology_id)
                            == col(MolecularTopology.id),
                        )
                        .where(
                            col(LogicalReactionParticipant.logical_reaction_id)
                            == logical_reaction_id
                        )
                        .order_by(
                            col(LogicalReactionParticipant.side),
                            col(LogicalReactionParticipant.participant_index),
                        )
                    )
                ).all()
            ]
            paths = (
                (
                    await session.execute(
                        select(MappedReaction)
                        .where(
                            col(MappedReaction.logical_reaction_id) == logical_reaction_id,
                            mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                        )
                        .order_by(col(MappedReaction.created_at), col(MappedReaction.id))
                    )
                )
                .scalars()
                .all()
            )
        path_minima = [
            path.minimum_activation_gibbs_free_energy_kcal_mol
            for path in paths
            if path.minimum_activation_gibbs_free_energy_kcal_mol is not None
        ]
        path_maxima = [
            path.maximum_activation_gibbs_free_energy_kcal_mol
            for path in paths
            if path.maximum_activation_gibbs_free_energy_kcal_mol is not None
        ]
        reaction_path_minima = [
            path.minimum_reaction_gibbs_free_energy_kcal_mol
            for path in paths
            if path.minimum_reaction_gibbs_free_energy_kcal_mol is not None
        ]
        reaction_path_maxima = [
            path.maximum_reaction_gibbs_free_energy_kcal_mol
            for path in paths
            if path.maximum_reaction_gibbs_free_energy_kcal_mol is not None
        ]
        summary = _reaction_summary(
            reaction,
            reactant_product_changed=_canonical_reactant_product_changed(participant_rows),
            minimum_activation_gibbs_free_energy_kcal_mol=(
                min(path_minima) if path_minima else None
            ),
            maximum_activation_gibbs_free_energy_kcal_mol=(
                max(path_maxima) if path_maxima else None
            ),
            minimum_reaction_gibbs_free_energy_kcal_mol=(
                min(reaction_path_minima) if reaction_path_minima else None
            ),
            maximum_reaction_gibbs_free_energy_kcal_mol=(
                max(reaction_path_maxima) if reaction_path_maxima else None
            ),
        )
        return LogicalReactionDetail(
            **summary.model_dump(),
            participants=[
                LogicalReactionParticipantView(
                    id=_required_uuid(participant.id, "LogicalReactionParticipant"),
                    side=_enum_value(participant.side),
                    participant_index=participant.participant_index,
                    role=_enum_value(participant.role) if participant.role is not None else None,
                    topology_id=participant.topology_id,
                    canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
                    stoichiometric_coefficient=participant.stoichiometric_coefficient,
                )
                for participant, topology in participant_rows
            ],
            mapped_reactions=[
                _mapped_reaction_summary(
                    path,
                    reactant_product_changed=summary.reactant_product_changed,
                )
                for path in paths
            ],
        )


class MappedReactionQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect logical mappings, node coordinates, calculation levels, and composites."""

    @query  # type: ignore[untyped-decorator]
    async def list_mapped_reactions(
        cls,
        project_id: UUID | None = None,
        logical_reaction_id: UUID | None = None,
        topology_id: UUID | None = None,
        geometry_id: UUID | None = None,
        mapping_hash: str | None = None,
        mapped_reaction_kind: MappedReactionKind | None = None,
        label: str | None = None,
        node_role: str | None = None,
        minimum_transition_state_geometry_count: int | None = None,
        maximum_transition_state_geometry_count: int | None = None,
        minimum_geometry_count: int | None = None,
        maximum_geometry_count: int | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        reaction_smarts: str | None = None,
        similarity_reaction_smiles: str | None = None,
        similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
        minimum_similarity: float | None = None,
        minimum_activation_gibbs_free_energy_kcal_mol: float | None = None,
        maximum_activation_gibbs_free_energy_kcal_mol: float | None = None,
        minimum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
        maximum_reaction_gibbs_free_energy_kcal_mol: float | None = None,
        reactant_product_changed: bool | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> MappedReactionPage:
        """List mapped paths, including topology and geometry reverse lookups."""

        settings = get_settings()
        enforce_structure_input_budget(
            {
                "reaction_smarts": reaction_smarts,
                "similarity_reaction_smiles": similarity_reaction_smiles,
            },
            maximum_characters=settings.structure_query_max_characters,
        )
        scope = await query_visibility_scope(project_id=project_id)
        predicates: list[Any] = [mapped_reaction_id_is_visible(scope, col(MappedReaction.id))]
        if logical_reaction_id is not None:
            predicates.append(col(MappedReaction.logical_reaction_id) == logical_reaction_id)
        if topology_id is not None:
            logical_ids = select(col(LogicalReactionParticipant.logical_reaction_id)).where(
                col(LogicalReactionParticipant.topology_id) == topology_id
            )
            predicates.append(col(MappedReaction.logical_reaction_id).in_(logical_ids))
        if geometry_id is not None:
            mapped_ids = (
                select(col(MappedReactionNode.mapped_reaction_id))
                .join(
                    MappedReactionNodeGeometry,
                    col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                    == col(MappedReactionNode.id),
                )
                .where(
                    col(MappedReactionNodeGeometry.geometry_id) == geometry_id,
                    geometry_has_thermodynamic_property_predicate(
                        col(MappedReactionNodeGeometry.geometry_id)
                    ),
                )
            )
            predicates.append(col(MappedReaction.id).in_(mapped_ids))
            predicates.append(geometry_id_is_visible(scope, literal(geometry_id)))
        if mapping_hash is not None:
            predicates.append(col(MappedReaction.mapping_hash) == mapping_hash)
        if mapped_reaction_kind is not None:
            predicates.append(col(MappedReaction.mapped_reaction_kind) == mapped_reaction_kind)
        if label is not None:
            predicates.append(col(MappedReaction.label) == label)
        if reactant_product_changed is not None:
            predicates.append(
                _reaction_topology_changed_expression(
                    reaction_id_column=col(MappedReaction.logical_reaction_id)
                )
                == reactant_product_changed
            )
        _validate_range(
            minimum_activation_gibbs_free_energy_kcal_mol,
            maximum_activation_gibbs_free_energy_kcal_mol,
            minimum_name="minimum_activation_gibbs_free_energy_kcal_mol",
            maximum_name="maximum_activation_gibbs_free_energy_kcal_mol",
        )
        _validate_range(
            minimum_reaction_gibbs_free_energy_kcal_mol,
            maximum_reaction_gibbs_free_energy_kcal_mol,
            minimum_name="minimum_reaction_gibbs_free_energy_kcal_mol",
            maximum_name="maximum_reaction_gibbs_free_energy_kcal_mol",
        )
        if minimum_activation_gibbs_free_energy_kcal_mol is not None:
            predicates.append(
                col(MappedReaction.maximum_activation_gibbs_free_energy_kcal_mol)
                >= minimum_activation_gibbs_free_energy_kcal_mol
            )
        if maximum_activation_gibbs_free_energy_kcal_mol is not None:
            predicates.append(
                col(MappedReaction.minimum_activation_gibbs_free_energy_kcal_mol)
                <= maximum_activation_gibbs_free_energy_kcal_mol
            )
        if minimum_reaction_gibbs_free_energy_kcal_mol is not None:
            predicates.append(
                col(MappedReaction.maximum_reaction_gibbs_free_energy_kcal_mol)
                >= minimum_reaction_gibbs_free_energy_kcal_mol
            )
        if maximum_reaction_gibbs_free_energy_kcal_mol is not None:
            predicates.append(
                col(MappedReaction.minimum_reaction_gibbs_free_energy_kcal_mol)
                <= maximum_reaction_gibbs_free_energy_kcal_mol
            )
        if node_role is not None:
            predicates.append(
                select(col(MappedReactionNode.id))
                .where(
                    col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
                    col(MappedReactionNode.role) == node_role,
                )
                .exists()
            )
        geometry_count = (
            select(func.count())
            .select_from(MappedReactionNodeGeometry)
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
                geometry_id_is_visible(
                    scope,
                    col(MappedReactionNodeGeometry.geometry_id),
                ),
                geometry_has_thermodynamic_property_predicate(
                    col(MappedReactionNodeGeometry.geometry_id)
                ),
            )
            .correlate(MappedReaction)
            .scalar_subquery()
        )
        transition_state_geometry_count = (
            select(func.count())
            .select_from(MappedReactionNodeGeometry)
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNode.mapped_reaction_id) == col(MappedReaction.id),
                col(MappedReactionNode.role) == "transition_state",
                geometry_id_is_visible(
                    scope,
                    col(MappedReactionNodeGeometry.geometry_id),
                ),
                geometry_has_thermodynamic_property_predicate(
                    col(MappedReactionNodeGeometry.geometry_id)
                ),
            )
            .correlate(MappedReaction)
            .scalar_subquery()
        )
        for minimum, maximum, expression, minimum_name, maximum_name in (
            (
                minimum_transition_state_geometry_count,
                maximum_transition_state_geometry_count,
                transition_state_geometry_count,
                "minimum_transition_state_geometry_count",
                "maximum_transition_state_geometry_count",
            ),
            (
                minimum_geometry_count,
                maximum_geometry_count,
                geometry_count,
                "minimum_geometry_count",
                "maximum_geometry_count",
            ),
        ):
            _validate_range(
                minimum,
                maximum,
                minimum_name=minimum_name,
                maximum_name=maximum_name,
            )
            if minimum is not None:
                predicates.append(expression >= minimum)
            if maximum is not None:
                predicates.append(expression <= maximum)
        _validate_range(
            created_after,
            created_before,
            minimum_name="created_after",
            maximum_name="created_before",
        )
        if created_after is not None:
            predicates.append(col(MappedReaction.created_at) >= created_after)
        if created_before is not None:
            predicates.append(col(MappedReaction.created_at) <= created_before)
        candidate_predicates = list(predicates)
        stored_reaction = cast(Any, col(MappedReaction.reaction))
        smarts_match: Any = literal(None)
        if reaction_smarts is not None:
            if rdChemReactions.ReactionFromSmarts(reaction_smarts) is None:
                raise ValueError("reaction_smarts must be a valid reaction SMARTS")
            smarts_query_reaction = reaction_from_smarts(
                cast(CString, sql_cast(reaction_smarts, CString))
            )
            smarts_match = stored_reaction.op("@>")(smarts_query_reaction)
            predicates.append(smarts_match)
            candidate_predicates.append(smarts_match)

        similarity_score: Any = literal(None)
        similarity_fingerprint: Any | None = None
        similarity_threshold_setting: str | None = None
        stored_fp: Any = cast(Any, col(MappedReaction.reaction_structural_bfp))
        if similarity_reaction_smiles is not None:
            if (
                rdChemReactions.ReactionFromSmarts(
                    similarity_reaction_smiles,
                    useSmiles=True,
                )
                is None
            ):
                raise ValueError("similarity_reaction_smiles must be a valid reaction SMILES")
            similarity_query_reaction = reaction_from_smiles(
                cast(CString, sql_cast(similarity_reaction_smiles, CString))
            )
            similarity_fingerprint = reaction_structural_bfp(
                similarity_query_reaction,
                REACTION_STRUCTURAL_BFP_RADIUS,
            )
            similarity_score = (
                tanimoto_sml(stored_fp, similarity_fingerprint)
                if similarity_metric is SimilarityMetric.tanimoto
                else dice_sml(stored_fp, similarity_fingerprint)
            )
            if minimum_similarity is not None:
                if not 0 < minimum_similarity <= 1:
                    raise ValueError("minimum_similarity must be in (0, 1]")
                threshold_operator = "%" if similarity_metric is SimilarityMetric.tanimoto else "#"
                threshold_predicate = stored_fp.op(threshold_operator)(similarity_fingerprint)
                predicates.append(threshold_predicate)
                candidate_predicates.append(threshold_predicate)
                similarity_threshold_setting = f"rdkit.{similarity_metric}_threshold"
        elif minimum_similarity is not None:
            raise ValueError("minimum_similarity requires similarity_reaction_smiles")
        count_statement = select(func.count()).select_from(MappedReaction).where(*predicates)
        order_by: list[Any] = [
            col(MappedReaction.created_at),
            col(MappedReaction.id),
        ]
        if similarity_fingerprint is not None:
            nearest_operator = "<%>" if similarity_metric is SimilarityMetric.tanimoto else "<#>"
            order_by = [
                stored_fp.op(nearest_operator)(similarity_fingerprint),
                *order_by,
            ]
        statement = (
            select(
                MappedReaction,
                smarts_match.label("reaction_smarts_match"),
                similarity_score.label("similarity_score"),
                _reaction_topology_changed_expression(
                    reaction_id_column=col(MappedReaction.logical_reaction_id)
                ).label("reactant_product_changed"),
            )
            .where(*predicates)
            .order_by(*order_by)
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            if similarity_threshold_setting is not None and minimum_similarity is not None:
                await session.exec(
                    sqlmodel_select(
                        func.set_config(
                            similarity_threshold_setting,
                            str(minimum_similarity),
                            True,
                        )
                    )
                )
            if reaction_smarts is not None or minimum_similarity is not None:
                await _enforce_candidate_limit(
                    sqlmodel_select(col(MappedReaction.id))
                    .select_from(MappedReaction)
                    .where(*candidate_predicates),
                    label="reaction structure query",
                    session=session,
                )
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return MappedReactionPage(
            items=[
                _mapped_reaction_summary(
                    reaction,
                    reactant_product_changed=(
                        bool(changed_value) if changed_value is not None else None
                    ),
                    reaction_smarts_match=(
                        bool(smarts_match_value) if smarts_match_value is not None else None
                    ),
                    similarity_score=(
                        float(similarity_score_value)
                        if similarity_score_value is not None
                        else None
                    ),
                )
                for (
                    reaction,
                    smarts_match_value,
                    similarity_score_value,
                    changed_value,
                ) in rows
            ],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_mapped_reaction(
        cls,
        mapped_reaction_id: UUID,
        project_id: UUID | None = None,
    ) -> MappedReactionDetail | None:
        """Get one mapped reaction whose nodes reference Geometry-owned calculations."""

        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            path_row = (
                await session.execute(
                    select(MappedReaction, LogicalReaction)
                    .join(
                        LogicalReaction,
                        col(MappedReaction.logical_reaction_id) == col(LogicalReaction.id),
                    )
                    .where(
                        col(MappedReaction.id) == mapped_reaction_id,
                        mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                    )
                )
            ).first()
            if path_row is None:
                return None
            path, reaction = path_row
            participant_rows = (
                await session.execute(
                    select(
                        MappedReactionParticipant,
                        LogicalReactionParticipant,
                        MolecularTopology,
                    )
                    .join(
                        LogicalReactionParticipant,
                        col(MappedReactionParticipant.logical_reaction_participant_id)
                        == col(LogicalReactionParticipant.id),
                    )
                    .join(
                        MolecularTopology,
                        col(LogicalReactionParticipant.topology_id) == col(MolecularTopology.id),
                    )
                    .where(col(MappedReactionParticipant.mapped_reaction_id) == mapped_reaction_id)
                    .order_by(
                        col(MappedReactionParticipant.side),
                        col(MappedReactionParticipant.template_index),
                    )
                )
            ).all()
            nodes = (
                (
                    await session.execute(
                        select(MappedReactionNode)
                        .where(col(MappedReactionNode.mapped_reaction_id) == mapped_reaction_id)
                        .order_by(col(MappedReactionNode.node_index))
                    )
                )
                .scalars()
                .all()
            )
            node_ids = [_required_uuid(node.id, "MappedReactionNode") for node in nodes]
            geometry_rows: Sequence[Any] = ()
            if node_ids:
                geometry_rows = (
                    await session.execute(
                        select(
                            MappedReactionNodeGeometry,
                            Geometry,
                            MolecularTopology,
                            MappedReactionParticipant,
                            LogicalReactionParticipant,
                        )
                        .join(
                            Geometry,
                            col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
                        )
                        .join(
                            MolecularTopology,
                            col(Geometry.topology_id) == col(MolecularTopology.id),
                        )
                        .outerjoin(
                            MappedReactionParticipant,
                            col(MappedReactionNodeGeometry.mapped_reaction_participant_id)
                            == col(MappedReactionParticipant.id),
                        )
                        .outerjoin(
                            LogicalReactionParticipant,
                            col(MappedReactionParticipant.logical_reaction_participant_id)
                            == col(LogicalReactionParticipant.id),
                        )
                        .where(
                            col(MappedReactionNodeGeometry.mapped_reaction_node_id).in_(node_ids),
                            geometry_id_is_visible(
                                scope,
                                col(MappedReactionNodeGeometry.geometry_id),
                            ),
                            geometry_has_thermodynamic_property_predicate(
                                col(MappedReactionNodeGeometry.geometry_id)
                            ),
                        )
                        .order_by(
                            col(MappedReactionNodeGeometry.mapped_reaction_node_id),
                            col(MappedReactionNodeGeometry.component_index),
                            col(MappedReactionNodeGeometry.coordinate_index),
                        )
                    )
                ).all()
            node_geometry_ids = [
                _required_uuid(binding.id, "MappedReactionNodeGeometry")
                for binding, _, _, _, _ in geometry_rows
            ]
            geometry_ids = list(
                dict.fromkeys(geometry.id for _, geometry, _, _, _ in geometry_rows)
            )
            calculation_rows: Sequence[Any] = ()
            geometry_mapping_rows: Sequence[MappedReactionNodeGeometryMapping] = ()
            if node_geometry_ids:
                geometry_mapping_rows = (
                    (
                        await session.execute(
                            select(MappedReactionNodeGeometryMapping)
                            .where(
                                col(
                                    MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id
                                ).in_(node_geometry_ids)
                            )
                            .order_by(
                                col(
                                    MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            if geometry_ids:
                calculation_rows = (
                    await session.execute(
                        select(
                            CalculationFrame,
                            CalculationSegment,
                            ParseRevision,
                            ArtifactFile,
                            Geometry,
                            MolecularTopology,
                            CalculationProtocol,
                            ThermochemistryResult,
                        )
                        .join(
                            CalculationSegment,
                            col(CalculationFrame.segment_id) == col(CalculationSegment.id),
                        )
                        .join(
                            ParseRevision,
                            col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
                        )
                        .join(
                            ArtifactFile,
                            col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
                        )
                        .join(Geometry, col(CalculationFrame.geometry_id) == col(Geometry.id))
                        .join(
                            MolecularTopology,
                            col(Geometry.topology_id) == col(MolecularTopology.id),
                        )
                        .outerjoin(
                            CalculationProtocol,
                            col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
                        )
                        .outerjoin(
                            ThermochemistryResult,
                            col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
                        )
                        .where(
                            col(CalculationFrame.geometry_id).in_(geometry_ids),
                            col(CalculationFrame.id).in_(visible_frame_ids(scope)),
                        )
                        .order_by(
                            col(CalculationFrame.geometry_id),
                            col(CalculationFrame.file_frame_index),
                        )
                    )
                ).all()
            edges = (
                (
                    await session.execute(
                        select(MappedReactionEdge)
                        .where(col(MappedReactionEdge.mapped_reaction_id) == mapped_reaction_id)
                        .order_by(col(MappedReactionEdge.edge_key))
                    )
                )
                .scalars()
                .all()
            )

        mappings_by_geometry: dict[UUID, list[NodeGeometryMappingView]] = {
            geometry_id: [] for geometry_id in node_geometry_ids
        }
        for mapping in geometry_mapping_rows:
            mappings_by_geometry[mapping.mapped_reaction_node_geometry_id].append(
                NodeGeometryMappingView(
                    id=_required_uuid(mapping.id, "MappedReactionNodeGeometryMapping"),
                    geometry_atom_map_numbers=mapping.geometry_atom_map_numbers,
                    mapped_smiles=mapping.mapped_smiles,
                    mapping_method=mapping.mapping_method,
                    mapping_version=mapping.mapping_version,
                    verified=mapping.verified,
                )
            )
        calculations_by_geometry: dict[UUID, list[CalculationFrameSummary]] = {
            geometry_id: [] for geometry_id in geometry_ids
        }
        energy_rows: list[
            tuple[CalculationFrame, CalculationProtocol | None, ThermochemistryResult | None]
        ] = []
        for (
            frame,
            segment,
            revision,
            artifact,
            geometry,
            topology,
            protocol,
            thermochemistry,
        ) in calculation_rows:
            calculations_by_geometry[frame.geometry_id].append(
                _frame_summary(frame, segment, revision, artifact, geometry, topology)
            )
            energy_rows.append((frame, protocol, thermochemistry))
        energy_composites = geometry_energy_composites(geometry_ids, energy_rows)

        geometry_views_by_node: dict[UUID, list[MappedReactionNodeGeometryView]] = {
            node_id: [] for node_id in node_ids
        }
        primary_composites_by_node: dict[UUID, list[GeometryEnergyComposite]] = {
            node_id: [] for node_id in node_ids
        }
        for (
            node_geometry,
            geometry,
            topology,
            mapped_participant,
            logical_participant,
        ) in geometry_rows:
            node_geometry_id = _required_uuid(
                node_geometry.id,
                "MappedReactionNodeGeometry",
            )
            composite = energy_composites[geometry.id]
            geometry_views_by_node[node_geometry.mapped_reaction_node_id].append(
                MappedReactionNodeGeometryView(
                    id=node_geometry_id,
                    component_key=node_geometry.component_key,
                    component_index=node_geometry.component_index,
                    coordinate_index=node_geometry.coordinate_index,
                    is_primary=node_geometry.is_primary,
                    mapped_reaction_participant_id=(
                        mapped_participant.id if mapped_participant is not None else None
                    ),
                    logical_reaction_participant_id=(
                        logical_participant.id if logical_participant is not None else None
                    ),
                    participant_role=(
                        _enum_value(logical_participant.role)
                        if logical_participant is not None and logical_participant.role is not None
                        else None
                    ),
                    geometry_id=node_geometry.geometry_id,
                    topology_id=geometry.topology_id,
                    canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
                    mappings=mappings_by_geometry[node_geometry_id],
                    calculations=calculations_by_geometry[geometry.id],
                    energy_view=composite.view,
                )
            )
            if node_geometry.is_primary:
                primary_composites_by_node[node_geometry.mapped_reaction_node_id].append(composite)
        summary = _mapped_reaction_summary(
            path,
            reactant_product_changed=_canonical_reactant_product_changed(
                [
                    (logical_participant, topology)
                    for _, logical_participant, topology in participant_rows
                ]
            ),
        )
        return MappedReactionDetail(
            **summary.model_dump(),
            reaction_key=reaction.reaction_key,
            participants=[
                MappedReactionParticipantView(
                    id=_required_uuid(mapped_participant.id, "MappedReactionParticipant"),
                    logical_reaction_participant_id=logical_participant.id,
                    side=_enum_value(mapped_participant.side),
                    template_index=mapped_participant.template_index,
                    topology_id=topology.id,
                    atom_map_numbers=mapped_participant.atom_map_numbers,
                    mapped_smiles=mapped_participant.mapped_smiles,
                )
                for mapped_participant, logical_participant, topology in participant_rows
            ],
            nodes=[
                MappedReactionNodeView(
                    id=_required_uuid(node.id, "MappedReactionNode"),
                    node_key=node.node_key,
                    node_index=node.node_index,
                    role=_enum_value(node.role),
                    geometries=geometry_views_by_node[
                        _required_uuid(node.id, "MappedReactionNode")
                    ],
                    additive_properties=_aggregate_primary_coordinates(
                        primary_composites_by_node[_required_uuid(node.id, "MappedReactionNode")]
                    ),
                )
                for node in nodes
            ],
            edges=[
                MappedReactionEdgeView(
                    id=_required_uuid(edge.id, "MappedReactionEdge"),
                    edge_key=edge.edge_key,
                    source_node_id=edge.source_node_id,
                    target_node_id=edge.target_node_id,
                    transition_state_node_id=edge.transition_state_node_id,
                    edge_kind=_enum_value(edge.edge_kind),
                )
                for edge in edges
            ],
        )


class CalculationQueryService(UseCaseService):  # type: ignore[misc]
    """Browse calculation frames and normalized MolOP sub-results."""

    @query  # type: ignore[untyped-decorator]
    async def list_calculation_frames(
        cls,
        project_id: UUID | None = None,
        artifact_file_id: UUID | None = None,
        geometry_id: UUID | None = None,
        topology_id: UUID | None = None,
        protocol_id: UUID | None = None,
        segment_index: int | None = None,
        frame_index: int | None = None,
        file_frame_index: int | None = None,
        frame_role: FrameRole | None = None,
        scf_status: SCFStatus | None = None,
        optimization_status: OptimizationStatus | None = None,
        charge: int | None = None,
        multiplicity: int | None = None,
        minimum_frequency_count: int | None = None,
        maximum_frequency_count: int | None = None,
        minimum_negative_frequency_count: int | None = None,
        maximum_negative_frequency_count: int | None = None,
        minimum_lowest_frequency_cm1: float | None = None,
        maximum_lowest_frequency_cm1: float | None = None,
        minimum_energy_hartree: float | None = None,
        maximum_energy_hartree: float | None = None,
        has_selected_energy: bool | None = None,
        has_frequencies: bool | None = None,
        limit: PageLimit = 100,
        offset: PageOffset = 0,
    ) -> CalculationFramePage:
        """List frame summaries with composable identity and calculation filters."""

        # The list response never needs source payloads or the RDKit molecule
        # stored on MolecularTopology.  Keep this query column-limited.
        statement = _frame_select(lightweight=True)
        scope = await query_visibility_scope(project_id=project_id)
        visibility_criterion = scope.artifact_predicate()
        count_statement = (
            select(func.count())
            .select_from(CalculationFrame)
            .join(
                ParseRevision,
                col(CalculationFrame.parse_revision_id) == col(ParseRevision.id),
            )
            .join(
                ArtifactFile,
                col(ParseRevision.artifact_file_id) == col(ArtifactFile.id),
            )
            .where(visibility_criterion)
        )
        predicates: list[Any] = []
        if artifact_file_id is not None:
            # The query already joins ParseRevision and ArtifactFile.  Keep
            # this as a direct equality instead of an ``IN`` subquery; the
            # latter makes PostgreSQL consider a broad semi-join over every
            # visible frame before applying the requested file filter.
            predicates.append(col(ParseRevision.artifact_file_id) == artifact_file_id)
        if geometry_id is not None:
            predicates.append(col(CalculationFrame.geometry_id) == geometry_id)
        if topology_id is not None:
            geometry_ids = select(col(Geometry.id)).where(col(Geometry.topology_id) == topology_id)
            predicates.append(col(CalculationFrame.geometry_id).in_(geometry_ids))
        if protocol_id is not None:
            segment_ids = select(col(CalculationSegment.id)).where(
                col(CalculationSegment.protocol_id) == protocol_id
            )
            predicates.append(col(CalculationFrame.segment_id).in_(segment_ids))
        if segment_index is not None:
            segment_ids = select(col(CalculationSegment.id)).where(
                col(CalculationSegment.segment_index) == segment_index
            )
            predicates.append(col(CalculationFrame.segment_id).in_(segment_ids))
        for exact_field, exact_value in (
            (CalculationFrame.frame_index, frame_index),
            (CalculationFrame.file_frame_index, file_frame_index),
            (CalculationFrame.charge, charge),
            (CalculationFrame.multiplicity, multiplicity),
        ):
            if exact_value is not None:
                predicates.append(col(exact_field) == exact_value)
        if frame_role is not None:
            predicates.append(col(CalculationFrame.frame_role) == frame_role)
        if scf_status is not None:
            predicates.append(col(CalculationFrame.scf_status) == scf_status)
        if optimization_status is not None:
            predicates.append(col(CalculationFrame.optimization_status) == optimization_status)
        for minimum, maximum, range_field, minimum_name, maximum_name in (
            (
                minimum_frequency_count,
                maximum_frequency_count,
                CalculationFrame.frequency_count,
                "minimum_frequency_count",
                "maximum_frequency_count",
            ),
            (
                minimum_negative_frequency_count,
                maximum_negative_frequency_count,
                CalculationFrame.negative_frequency_count,
                "minimum_negative_frequency_count",
                "maximum_negative_frequency_count",
            ),
            (
                minimum_lowest_frequency_cm1,
                maximum_lowest_frequency_cm1,
                CalculationFrame.lowest_frequency_cm1,
                "minimum_lowest_frequency_cm1",
                "maximum_lowest_frequency_cm1",
            ),
        ):
            _validate_range(
                minimum,
                maximum,
                minimum_name=minimum_name,
                maximum_name=maximum_name,
            )
            if minimum is not None:
                predicates.append(col(range_field) >= minimum)
            if maximum is not None:
                predicates.append(col(range_field) <= maximum)
        if minimum_energy_hartree is not None:
            predicates.append(
                col(CalculationFrame.selected_energy_hartree)
                >= round_energy_hartree(minimum_energy_hartree)
            )
        if maximum_energy_hartree is not None:
            predicates.append(
                col(CalculationFrame.selected_energy_hartree)
                <= round_energy_hartree(maximum_energy_hartree)
            )
        _validate_range(
            minimum_energy_hartree,
            maximum_energy_hartree,
            minimum_name="minimum_energy_hartree",
            maximum_name="maximum_energy_hartree",
        )
        if has_selected_energy is not None:
            energy_column = col(CalculationFrame.selected_energy_hartree)
            predicates.append(
                energy_column.is_not(None) if has_selected_energy else energy_column.is_(None)
            )
        if has_frequencies is not None:
            frequency_column = col(CalculationFrame.frequency_count)
            predicates.append(
                frequency_column.is_not(None) if has_frequencies else frequency_column.is_(None)
            )
        # The statement joins ArtifactFile directly, so its visibility
        # predicate both enforces authorization and gives PostgreSQL a
        # selective artifact/revision/frame join order.  The revision-level
        # visibility expression is equivalent here and would introduce a
        # broad semi-join over all visible frames.
        statement = statement.where(visibility_criterion, *predicates)
        count_statement = count_statement.where(*predicates)
        if artifact_file_id is not None:
            # A file has one stable filename.  Ordering directly by the
            # revision/file index avoids sorting the joined topology rows and
            # keeps pagination deterministic when a file has been reparsed.
            statement = statement.order_by(
                col(ParseRevision.revision_number),
                col(CalculationFrame.file_frame_index),
            )
        else:
            statement = statement.order_by(
                col(ArtifactFile.original_filename),
                col(CalculationFrame.file_frame_index),
            )
        statement = statement.offset(offset).limit(limit)
        async with session_factory() as session:
            if scope.uses_project_geometry_catalog and not predicates:
                # The project catalogue already maintains one frame count per
                # visible Geometry.  Avoid joining every frame to its artifact
                # merely to populate the catalogue totals panel.
                total_statement = select(
                    func.coalesce(func.sum(ProjectGeometryCatalog.frame_count), 0)
                ).where(col(ProjectGeometryCatalog.project_id) == scope.requested_project_id)
                total = int((await session.execute(total_statement)).scalar_one())
            else:
                total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return CalculationFramePage(
            items=[_frame_summary(*row) for row in rows],
            page=PageInfo(total=total, limit=limit, offset=offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_calculation_frame(
        cls,
        frame_id: UUID,
        project_id: UUID | None = None,
    ) -> CalculationFrameDetail | None:
        """Get one frame with scalar results and array metadata, excluding array payloads."""

        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            row = (
                await session.execute(
                    _frame_select().where(
                        col(CalculationFrame.id) == frame_id,
                        frame_id_is_visible(scope, col(CalculationFrame.id)),
                    )
                )
            ).first()
            if row is None:
                return None
            frame, segment, revision, artifact, geometry, topology = row
            topology_derivation = (
                await session.execute(
                    select(MolecularTopologyDerivation).where(
                        col(MolecularTopologyDerivation.id) == frame.topology_derivation_id
                    )
                )
            ).scalar_one()
            protocol = (
                (
                    await session.execute(
                        select(CalculationProtocol).where(
                            col(CalculationProtocol.id) == segment.protocol_id
                        )
                    )
                ).scalar_one_or_none()
                if segment.protocol_id is not None
                else None
            )
            energy = (
                await session.execute(
                    select(FrameEnergyResult).where(col(FrameEnergyResult.frame_id) == frame_id)
                )
            ).scalar_one_or_none()
            observations: Sequence[EnergyObservation] = ()
            if energy is not None:
                energy_id = _required_uuid(energy.id, "FrameEnergyResult")
                observations = (
                    (
                        await session.execute(
                            select(EnergyObservation)
                            .where(col(EnergyObservation.energy_result_id) == energy_id)
                            .order_by(col(EnergyObservation.observation_index))
                        )
                    )
                    .scalars()
                    .all()
                )
            optimization = (
                await session.execute(
                    select(GeometryOptimizationResult).where(
                        col(GeometryOptimizationResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            vibration = (
                await session.execute(
                    select(VibrationResult).where(col(VibrationResult.frame_id) == frame_id)
                )
            ).scalar_one_or_none()
            thermochemistry = (
                await session.execute(
                    select(ThermochemistryResult).where(
                        col(ThermochemistryResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            calculation_status = (
                await session.execute(
                    select(CalculationStatusResult).where(
                        col(CalculationStatusResult.frame_id) == frame_id
                    )
                )
            ).scalar_one_or_none()
            array_rows = (
                await session.execute(
                    select(ScientificArray, ScientificArrayAssignment)
                    .outerjoin(
                        ScientificArrayAssignment,
                        col(ScientificArrayAssignment.scientific_array_id)
                        == col(ScientificArray.id),
                    )
                    .where(col(ScientificArray.frame_id) == frame_id)
                    .order_by(col(ScientificArray.kind), col(ScientificArray.ordinal))
                )
            ).all()
            endpoint_rows = (
                (
                    await session.execute(
                        select(TransitionStateEndpoint)
                        .where(col(TransitionStateEndpoint.calculation_frame_id) == frame_id)
                        .order_by(col(TransitionStateEndpoint.direction))
                    )
                )
                .scalars()
                .all()
            )

        summary = _frame_summary(frame, segment, revision, artifact, geometry, topology)
        return CalculationFrameDetail(
            **summary.model_dump(),
            source_span=(
                SourceSpanView(
                    start_byte=frame.source_start_byte,
                    end_byte=frame.source_end_byte,
                    start_char=frame.source_start_char,
                    end_char=frame.source_end_char,
                    start_line=frame.source_start_line,
                    end_line=frame.source_end_line,
                    block_sha256=frame.source_block_sha256,
                )
                if frame.source_start_byte is not None
                else None
            ),
            parse_completeness=_enum_value(frame.parse_completeness),
            geometry_assignment_kind=_enum_value(frame.geometry_assignment_kind),
            observed_coordinate_hash=frame.observed_coordinate_hash,
            observed_to_geometry_atom_indices=frame.observed_to_geometry_atom_indices,
            observed_to_geometry_transform=frame.observed_to_geometry_transform,
            geometry_assignment_rmsd_angstrom=frame.geometry_assignment_rmsd_angstrom,
            geometry_assignment_max_abs_angstrom=(frame.geometry_assignment_max_abs_angstrom),
            geometry_assignment_policy_version=frame.geometry_assignment_policy_version,
            electronic_state_kind=_enum_value(frame.electronic_state_kind),
            electronic_state_index=frame.electronic_state_index,
            topology_derivation=MolecularTopologyDerivationView(
                id=_required_uuid(
                    topology_derivation.id,
                    "MolecularTopologyDerivation",
                ),
                reconstruction_method=topology_derivation.reconstruction_method,
                reconstruction_version=topology_derivation.reconstruction_version,
                reconstruction_metadata_json=json.dumps(
                    topology_derivation.reconstruction_metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                provenance_schema_version=topology_derivation.provenance_schema_version,
                provenance_hash=topology_derivation.provenance_hash,
            ),
            protocol=(
                CalculationProtocolView(
                    id=_required_uuid(protocol.id, "CalculationProtocol"),
                    qm_software=_enum_value(protocol.qm_software),
                    qm_software_version=protocol.qm_software_version,
                    method_family=protocol.method_family,
                    method=protocol.method,
                    reference_method=protocol.reference_method,
                    functional=protocol.functional,
                    basis_set=protocol.basis_set,
                    auxiliary_basis_set=protocol.auxiliary_basis_set,
                    dispersion_model=protocol.dispersion_model,
                    solvation_model=protocol.solvation_model,
                    solvent=protocol.solvent,
                    task_requests=protocol.task_requests,
                )
                if protocol is not None
                else None
            ),
            energy=(
                FrameEnergyView.model_validate(energy, from_attributes=True)
                if energy is not None
                else None
            ),
            energy_observations=[
                EnergyObservationView(
                    observation_index=observation.observation_index,
                    method=observation.method,
                    quantity_semantics=_enum_value(observation.quantity_semantics),
                    value_hartree=observation.value_hartree,
                    source_label=observation.source_label,
                )
                for observation in observations
            ],
            optimization=(
                GeometryOptimizationView.model_validate(optimization, from_attributes=True)
                if optimization is not None
                else None
            ),
            vibration=(
                VibrationView.model_validate(vibration, from_attributes=True)
                if vibration is not None
                else None
            ),
            transition_state_endpoints=[
                TransitionStateEndpointView(
                    direction=_enum_value(endpoint.direction),
                    topology_id=endpoint.topology_id,
                    charge=endpoint.charge,
                    multiplicity=endpoint.multiplicity,
                    atom_count=endpoint.atom_count,
                    displacement_ratio=endpoint.displacement_ratio,
                    source_coordinate_hash=endpoint.source_coordinate_hash,
                    source_to_topology_atom_indices=(endpoint.source_to_topology_atom_indices),
                )
                for endpoint in endpoint_rows
            ],
            thermochemistry=(
                ThermochemistryView.model_validate(thermochemistry, from_attributes=True)
                if thermochemistry is not None
                else None
            ),
            calculation_status=(
                CalculationStatusView.model_validate(calculation_status, from_attributes=True)
                if calculation_status is not None
                else None
            ),
            scientific_arrays=[
                ScientificArraySummary(
                    id=_required_uuid(array.id, "ScientificArray"),
                    kind=_enum_value(array.kind),
                    ordinal=array.ordinal,
                    unit=array.unit,
                    dtype=array.dtype,
                    shape=array.shape,
                    array_nbytes=array.array_nbytes,
                    payload_sha256=array.payload_sha256,
                    owner_kind=_array_assignment_owner(assignment)[0],
                    owner_id=_array_assignment_owner(assignment)[1],
                    slot=assignment.slot if assignment is not None else None,
                    slot_ordinal=(assignment.slot_ordinal if assignment is not None else None),
                    source_field=(array.array_metadata or {}).get("source_field"),
                    source_unit=(array.array_metadata or {}).get("source_unit") or array.unit,
                    population_name=_array_population_name(array),
                    population_scheme=(array.array_metadata or {}).get("population_scheme"),
                    population_quantity=(array.array_metadata or {}).get("population_quantity"),
                    population_spin_channel=(array.array_metadata or {}).get(
                        "population_spin_channel"
                    ),
                    population_source_label=(array.array_metadata or {}).get(
                        "population_source_label"
                    ),
                )
                for array, assignment in array_rows
            ],
        )


__all__ = [
    "ArtifactQueryService",
    "CalculationQueryService",
    "MappedReactionQueryService",
    "LogicalReactionQueryService",
    "MolecularTopologyQueryService",
]
