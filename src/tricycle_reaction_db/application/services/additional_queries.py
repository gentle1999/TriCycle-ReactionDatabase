"""Read-only queries for normalized geometry and ingestion provenance."""

import json
from collections.abc import Iterable, Mapping
from typing import Any, cast
from uuid import UUID

from molalchemy.helpers import rdkit_col
from molalchemy.rdkit.functions import dice_sml, mol_from_smiles, morganbv_fp, tanimoto_sml
from molalchemy.types import CString
from nexusx import UseCaseService, query  # type: ignore[import-untyped]
from rdkit import Chem
from sqlalchemy import Text, and_, desc, func, literal, not_, or_, select, true
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import defer
from sqlmodel import col

from tricycle_reaction_db.application.dtos import (
    ArtifactIngestionPage,
    ArtifactIngestionSummary,
    CalculationProtocolDetail,
    CalculationProtocolPage,
    CalculationProtocolSummary,
    CalculationProtocolView,
    CalculationSegmentPage,
    CalculationSegmentSummary,
    GeometryAtomCoordinate,
    GeometryDetail,
    GeometryPage,
    GeometrySummary,
    MappedReactionThermodynamics,
    MappedReactionThermodynamicsProfile,
    MolecularFormulaDetail,
    MolecularFormulaSummary,
    MolecularTopologyDetail,
    PageInfo,
    ParseRevisionPage,
    ParseRevisionSummary,
    ReactionEnergyEdgeView,
    ReactionEnergyPoint,
    ReactionEnergyProfile,
    ScientificArrayPage,
    ScientificArraySummary,
    ThermodynamicDifferenceView,
    ThermodynamicStateView,
    TransitionStateInferencePage,
    TransitionStateInferenceSummary,
)
from tricycle_reaction_db.application.query_cost import enforce_structure_input_budget
from tricycle_reaction_db.application.services.geometry_energy import (
    geometry_energy_composites,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics import (
    format_composite_level_of_theory,
)
from tricycle_reaction_db.application.services.queries import (
    MappedReactionQueryService,
    MolecularTopologyQueryService,
    PageLimit,
    PageOffset,
    _array_assignment_owner,
    _array_population_name,
    _enum_value,
    _frame_select,
    _frame_summary,
    _reaction_topology_changed_expression,
    _required_uuid,
    _validate_range,
)
from tricycle_reaction_db.application.services.query_visibility import (
    artifact_id_is_visible,
    calculation_frame_is_visible,
    calculation_protocol_id_is_visible,
    frame_id_is_visible,
    geometry_id_is_visible,
    logical_reaction_id_is_visible,
    mapped_reaction_id_is_visible,
    parse_revision_id_is_visible,
    query_visibility_scope,
    topology_id_is_visible,
    visible_parse_revision_ids,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    geometry_has_thermodynamic_property_predicate,
    geometry_ids_with_thermodynamic_property,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    ArtifactIngestion,
    CalculationFrame,
    CalculationProtocol,
    CalculationSegment,
    Geometry,
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionThermodynamicProfile,
    MolecularFormula,
    MolecularTopology,
    MolecularTopologyDerivation,
    ParseRevision,
    ProjectGeometryCatalog,
    ProjectGeometryCatalogCount,
    ScientificArray,
    ScientificArrayAssignment,
    ThermochemistryResult,
    TransitionStateInference,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    ArtifactIngestionStatus,
    MappedReactionNodeRole,
    SimilarityMetric,
)
from tricycle_reaction_db.domain.fingerprints import MORGAN_BFP_RADIUS
from tricycle_reaction_db.domain.precision import round_energy_hartree

# NexusX's schema builder accepts constrained scalar aliases, but not a Pydantic
# input model. Keep pagination definitions local to this module for registration.
QueryLimit = PageLimit
QueryOffset = PageOffset
HARTREE_TO_KCAL_MOL = 627.5094740631
REACTION_ENERGY_KINDS = frozenset(
    {
        "electronic_energy_hartree",
        "zero_point_energy_hartree",
        "thermal_internal_energy_hartree",
        "enthalpy_hartree",
        "gibbs_free_energy_hartree",
    }
)


def _page(total: int, limit: int, offset: int) -> PageInfo:
    return PageInfo(total=total, limit=limit, offset=offset)


def _geometry_coordinates(geometry: Geometry) -> list[GeometryAtomCoordinate]:
    """Project the stored conformer without changing its canonical atom order."""

    conformer = geometry.mol.GetConformer()
    return [
        GeometryAtomCoordinate(
            atom_index=atom.GetIdx(),
            element=atom.GetSymbol(),
            x_angstrom=float(position.x),
            y_angstrom=float(position.y),
            z_angstrom=float(position.z),
        )
        for atom in geometry.mol.GetAtoms()  # type: ignore[no-untyped-call]
        for position in (conformer.GetAtomPosition(atom.GetIdx()),)
    ]


def _protocol_view(protocol: CalculationProtocol) -> CalculationProtocolView:
    return CalculationProtocolView(
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
        task_requests=list(protocol.task_requests),
    )


def _protocol_summary(protocol: CalculationProtocol) -> CalculationProtocolSummary:
    return CalculationProtocolSummary(
        **_protocol_view(protocol).model_dump(),
        protocol_hash=protocol.protocol_hash,
    )


def _geometry_summary(
    geometry: Geometry,
    topology: MolecularTopology,
    calculation_count: int,
    reaction_binding_count: int,
    is_transition_state: bool,
    has_frequency_data: bool,
    has_imaginary_frequency: bool,
    similarity_score: float | None = None,
) -> GeometrySummary:
    imaginary_frequency_status = (
        "present" if has_imaginary_frequency else "absent" if has_frequency_data else "unavailable"
    )
    return GeometrySummary(
        id=_required_uuid(geometry.id, "Geometry"),
        topology_id=geometry.topology_id,
        canonical_isomeric_smiles=topology.canonical_isomeric_smiles,
        atom_count=topology.atom_count,
        geometry_hash=geometry.geometry_hash,
        internal_coordinate_hash=geometry.internal_coordinate_hash,
        canonicalization_version=geometry.canonicalization_version,
        charge=geometry.charge,
        multiplicity=geometry.multiplicity,
        calculation_count=calculation_count,
        reaction_binding_count=reaction_binding_count,
        is_transition_state=is_transition_state,
        imaginary_frequency_status=imaginary_frequency_status,
        similarity_score=similarity_score,
    )


def _geometry_has_frequency_data_predicate(scope: Any, geometry_id: Any) -> Any:
    return (
        select(col(CalculationFrame.id))
        .where(
            col(CalculationFrame.geometry_id) == geometry_id,
            calculation_frame_is_visible(scope, col(CalculationFrame.parse_revision_id)),
            col(CalculationFrame.frequency_count).is_not(None),
        )
        .exists()
    )


def _geometry_has_imaginary_frequency_predicate(scope: Any, geometry_id: Any) -> Any:
    return (
        select(col(CalculationFrame.id))
        .where(
            col(CalculationFrame.geometry_id) == geometry_id,
            calculation_frame_is_visible(scope, col(CalculationFrame.parse_revision_id)),
            col(CalculationFrame.negative_frequency_count) > 0,
        )
        .exists()
    )


def _geometry_is_transition_state_predicate(scope: Any, geometry_id: Any) -> Any:
    """Identify mapped transition-state geometries independently of frequency data."""

    return (
        select(col(MappedReactionNodeGeometry.id))
        .join(
            MappedReactionNode,
            col(MappedReactionNodeGeometry.mapped_reaction_node_id) == col(MappedReactionNode.id),
        )
        .where(
            col(MappedReactionNodeGeometry.geometry_id) == geometry_id,
            col(MappedReactionNode.role) == MappedReactionNodeRole.TRANSITION_STATE,
            mapped_reaction_id_is_visible(scope, col(MappedReactionNode.mapped_reaction_id)),
        )
        .exists()
    )


_GEOMETRY_QUERY_EXPRESSION_FIELDS = frozenset(
    {
        "topology_id",
        "geometry_hash",
        "internal_coordinate_hash",
        "canonicalization_version",
        "topology_derivation_id",
        "reaction_node_role",
        "topology_smiles",
        "topology_mol_block",
        "topology_smarts",
        "thermodynamic_only",
        "imaginary_frequency_status",
        "minimum_atom_count",
        "maximum_atom_count",
    }
)


def _geometry_structure_predicate(field: str, value: object) -> Any:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if field == "topology_smarts":
        if Chem.MolFromSmarts(value) is None:
            raise ValueError("topology_smarts must be a valid SMARTS pattern")
        return rdkit_col(MolecularTopology.mol).has_smarts(value)
    query_molecule = (
        Chem.MolFromSmiles(value)
        if field == "topology_smiles"
        else Chem.MolFromMolBlock(value, sanitize=True, removeHs=True, strictParsing=False)
    )
    if query_molecule is None:
        raise ValueError(f"{field} must contain a valid molecule")
    query_smiles = Chem.MolToSmiles(
        Chem.RemoveHs(Chem.Mol(query_molecule)),
        canonical=True,
        isomericSmiles=True,
    )
    query_expression = mol_from_smiles(cast(CString, sql_cast(query_smiles, Text)))
    return rdkit_col(MolecularTopology.mol).has_substructure(cast(str, query_expression))


def _geometry_query_leaf_predicate(field: object, value: object, scope: Any) -> Any:
    if not isinstance(field, str) or field not in _GEOMETRY_QUERY_EXPRESSION_FIELDS:
        choices = ", ".join(sorted(_GEOMETRY_QUERY_EXPRESSION_FIELDS))
        raise ValueError(f"unsupported geometry query field; expected one of: {choices}")
    field_name: str = field
    if field_name in {"topology_id", "topology_derivation_id"}:
        try:
            parsed_id = UUID(str(value))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be a valid UUID") from error
        if field_name == "topology_id":
            return col(Geometry.topology_id) == parsed_id
        return (
            select(col(CalculationFrame.id))
            .where(
                col(CalculationFrame.geometry_id) == col(Geometry.id),
                col(CalculationFrame.topology_derivation_id) == parsed_id,
                frame_id_is_visible(scope, col(CalculationFrame.id)),
            )
            .exists()
        )
    if field_name in {
        "geometry_hash",
        "internal_coordinate_hash",
        "canonicalization_version",
        "reaction_node_role",
    }:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        column = {
            "geometry_hash": Geometry.geometry_hash,
            "internal_coordinate_hash": Geometry.internal_coordinate_hash,
            "canonicalization_version": Geometry.canonicalization_version,
        }.get(field_name)
        if column is not None:
            return col(column) == value
        return (
            select(col(MappedReactionNodeGeometry.id))
            .join(
                MappedReactionNode,
                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                == col(MappedReactionNode.id),
            )
            .where(
                col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
                col(MappedReactionNode.role) == value,
                mapped_reaction_id_is_visible(scope, col(MappedReactionNode.mapped_reaction_id)),
                geometry_has_thermodynamic_property_predicate(
                    col(MappedReactionNodeGeometry.geometry_id)
                ),
            )
            .exists()
        )
    if field_name in {"topology_smiles", "topology_mol_block", "topology_smarts"}:
        enforce_structure_input_budget(
            {field_name: cast(str | None, value) if isinstance(value, str) else None},
            maximum_characters=get_settings().structure_query_max_characters,
        )
        return _geometry_structure_predicate(field_name, value)
    if field_name == "thermodynamic_only":
        if value is not True:
            raise ValueError("thermodynamic_only expression must be true")
        return geometry_has_thermodynamic_property_predicate(col(Geometry.id))
    if field_name == "imaginary_frequency_status":
        if value not in {"present", "absent", "unavailable"}:
            raise ValueError("imaginary_frequency_status must be present, absent, or unavailable")
        has_frequency_data = _geometry_has_frequency_data_predicate(scope, col(Geometry.id))
        has_imaginary_frequency = _geometry_has_imaginary_frequency_predicate(
            scope,
            col(Geometry.id),
        )
        if value == "present":
            return has_imaginary_frequency
        if value == "absent":
            return and_(has_frequency_data, not_(has_imaginary_frequency))
        return not_(has_frequency_data)
    if field_name in {"minimum_atom_count", "maximum_atom_count"}:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"{field_name} must be an integer")
        try:
            parsed_count = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be an integer") from error
        if parsed_count < 1:
            raise ValueError(f"{field_name} must be greater than or equal to 1")
        return (
            col(MolecularTopology.atom_count) >= parsed_count
            if field_name == "minimum_atom_count"
            else col(MolecularTopology.atom_count) <= parsed_count
        )
    raise AssertionError(f"unhandled geometry query field: {field_name}")


def _geometry_query_expression_predicate(node: object, scope: Any, depth: int = 0) -> Any:
    if depth > 12:
        raise ValueError("geometry query expression is too deeply nested")
    if not isinstance(node, Mapping):
        raise ValueError("geometry query expression nodes must be objects")
    operator = node.get("operator")
    if operator is not None:
        children = node.get("conditions")
        if operator not in {"and", "or", "not"} or not isinstance(children, list) or not children:
            raise ValueError("logical nodes require operator and non-empty conditions")
        if operator == "not" and len(children) != 1:
            raise ValueError("not nodes require exactly one condition")
        predicates = [
            _geometry_query_expression_predicate(child, scope, depth + 1) for child in children
        ]
        if operator == "and":
            return and_(*predicates)
        if operator == "or":
            return or_(*predicates)
        return not_(predicates[0])
    if "field" not in node or "value" not in node:
        raise ValueError("leaf nodes require field and value")
    predicate = _geometry_query_leaf_predicate(node.get("field"), node.get("value"), scope)
    if node.get("negated", False) is True:
        return not_(predicate)
    if node.get("negated", False) not in {False, None}:
        raise ValueError("negated must be a boolean")
    return predicate


def _ingestion_summary(ingestion: ArtifactIngestion) -> ArtifactIngestionSummary:
    return ArtifactIngestionSummary(
        id=_required_uuid(ingestion.id, "ArtifactIngestion"),
        artifact_file_id=ingestion.artifact_file_id,
        status=_enum_value(ingestion.status),
        molop_version=ingestion.parser_version,
        source_frame_count=ingestion.source_frame_count,
        transition_state_frame_count=ingestion.transition_state_frame_count,
        error_code=ingestion.error_code,
        error_message=ingestion.error_message,
        started_at=ingestion.started_at,
        completed_at=ingestion.completed_at,
    )


def _parse_revision_summary(revision: ParseRevision) -> ParseRevisionSummary:
    return ParseRevisionSummary(
        id=_required_uuid(revision.id, "ParseRevision"),
        artifact_file_id=revision.artifact_file_id,
        revision_number=revision.revision_number,
        reparse_of_id=revision.reparse_of_id,
        export_schema_version=revision.export_schema_version,
        parser_name=revision.parser_name,
        parser_version=revision.parser_version,
        molop_version=revision.molop_version,
        source_format=_enum_value(revision.source_format),
        parse_completeness=_enum_value(revision.parse_completeness),
        status=_enum_value(revision.status),
        record_sha256=revision.record_sha256,
        running_time_seconds=revision.running_time_seconds,
        error_code=revision.error_code,
        error_message=revision.error_message,
        started_at=revision.started_at,
        completed_at=revision.completed_at,
    )


def _segment_summary(segment: CalculationSegment) -> CalculationSegmentSummary:
    return CalculationSegmentSummary(
        id=_required_uuid(segment.id, "CalculationSegment"),
        parse_revision_id=segment.parse_revision_id,
        protocol_id=segment.protocol_id,
        segment_index=segment.segment_index,
        segment_label=segment.segment_label,
        source_frame_count=segment.source_frame_count,
        parse_completeness=_enum_value(segment.parse_completeness),
        termination_status=_enum_value(segment.termination_status),
        scf_status=_enum_value(segment.scf_status),
        source_start_line=segment.source_start_line,
        source_end_line=segment.source_end_line,
    )


def _ts_summary(
    inference: TransitionStateInference,
    *,
    reactant_product_changed: bool | None = None,
) -> TransitionStateInferenceSummary:
    return TransitionStateInferenceSummary(
        id=_required_uuid(inference.id, "TransitionStateInference"),
        artifact_ingestion_id=inference.artifact_ingestion_id,
        parse_revision_id=inference.parse_revision_id,
        file_frame_index=inference.file_frame_index,
        imaginary_mode_index=inference.imaginary_mode_index,
        imaginary_frequency_cm1=inference.imaginary_frequency_cm1,
        status=_enum_value(inference.status),
        logical_reaction_id=inference.logical_reaction_id,
        mapped_reaction_id=inference.mapped_reaction_id,
        calculation_frame_id=inference.calculation_frame_id,
        reactant_product_changed=reactant_product_changed,
        error_code=inference.error_code,
        error_message=inference.error_message,
    )


def _array_summary(
    array: ScientificArray,
    assignment: ScientificArrayAssignment | None,
) -> ScientificArraySummary:
    owner_kind, owner_id = _array_assignment_owner(assignment)
    return ScientificArraySummary(
        id=_required_uuid(array.id, "ScientificArray"),
        kind=_enum_value(array.kind),
        ordinal=array.ordinal,
        unit=array.unit,
        dtype=array.dtype,
        shape=list(array.shape),
        array_nbytes=array.array_nbytes,
        payload_sha256=array.payload_sha256,
        owner_kind=owner_kind,
        owner_id=owner_id,
        slot=assignment.slot if assignment is not None else None,
        slot_ordinal=assignment.slot_ordinal if assignment is not None else None,
        source_field=(array.array_metadata or {}).get("source_field"),
        source_unit=(array.array_metadata or {}).get("source_unit") or array.unit,
        population_name=_array_population_name(array),
        population_scheme=(array.array_metadata or {}).get("population_scheme"),
        population_quantity=(array.array_metadata or {}).get("population_quantity"),
        population_spin_channel=(array.array_metadata or {}).get("population_spin_channel"),
        population_source_label=(array.array_metadata or {}).get("population_source_label"),
    )


class MolecularFormulaDetailQueryService(UseCaseService):  # type: ignore[misc]
    """Direct formula identity and topology-count queries."""

    @query  # type: ignore[untyped-decorator]
    async def get_formula(cls, formula_id: UUID) -> MolecularFormulaDetail | None:
        async with session_factory() as session:
            formula = await session.get(MolecularFormula, formula_id)
            if formula is None:
                return None
            topology_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(MolecularTopology)
                        .where(col(MolecularTopology.formula_id) == formula_id)
                    )
                ).scalar_one()
            )
        return MolecularFormulaDetail(
            **MolecularFormulaSummary(
                id=_required_uuid(formula.id, "MolecularFormula"),
                hill_formula=formula.hill_formula,
                atom_count=formula.atom_count,
                composition_hash=formula.composition_hash,
                element_count_vector=list(formula.element_count_vector),
            ).model_dump(),
            topology_count=topology_count,
        )


class MolecularTopologyDetailQueryService(UseCaseService):  # type: ignore[misc]
    """Direct topology detail, including geometry and reaction usage counts."""

    @query  # type: ignore[untyped-decorator]
    async def get_topology(cls, topology_id: UUID) -> MolecularTopologyDetail | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            topology = (
                await session.execute(
                    select(MolecularTopology)
                    .where(col(MolecularTopology.id) == topology_id)
                    .where(topology_id_is_visible(scope, col(MolecularTopology.id)))
                )
            ).scalar_one_or_none()
            if topology is None:
                return None
            geometry_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Geometry)
                        .where(
                            col(Geometry.topology_id) == topology_id,
                            geometry_id_is_visible(scope, col(Geometry.id)),
                        )
                    )
                ).scalar_one()
            )
            logical_reaction_count = int(
                (
                    await session.execute(
                        select(
                            func.count(
                                func.distinct(LogicalReactionParticipant.logical_reaction_id)
                            )
                        )
                        .select_from(LogicalReactionParticipant)
                        .where(
                            col(LogicalReactionParticipant.topology_id) == topology_id,
                            logical_reaction_id_is_visible(
                                scope,
                                col(LogicalReactionParticipant.logical_reaction_id),
                            ),
                        )
                    )
                ).scalar_one()
            )
            derivation_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(MolecularTopologyDerivation)
                        .where(
                            col(MolecularTopologyDerivation.topology_id) == topology_id,
                            select(col(CalculationFrame.id))
                            .where(
                                col(CalculationFrame.topology_derivation_id)
                                == col(MolecularTopologyDerivation.id),
                                frame_id_is_visible(scope, col(CalculationFrame.id)),
                            )
                            .exists(),
                        )
                    )
                ).scalar_one()
            )
        search_page = cast(
            Any,
            await MolecularTopologyQueryService.search_topologies(
                topology_id=_required_uuid(topology.id, "MolecularTopology"),
                limit=1,
                offset=0,
            ),
        )
        topology_uuid = _required_uuid(topology.id, "MolecularTopology")
        result = next(
            (item for item in search_page.items if item.id == topology_uuid),
            None,
        )
        if result is None:
            raise RuntimeError("persisted topology was not returned by its identity query")
        return MolecularTopologyDetail(
            **result.model_dump(),
            geometry_count=geometry_count,
            logical_reaction_count=logical_reaction_count,
            derivation_count=derivation_count,
        )


class GeometryQueryService(UseCaseService):  # type: ignore[misc]
    """List invariant coordinate identities and their calculation frames."""

    @query  # type: ignore[untyped-decorator]
    async def list_geometries(
        cls,
        topology_id: UUID | None = None,
        geometry_hash: str | None = None,
        internal_coordinate_hash: str | None = None,
        canonicalization_version: str | None = None,
        topology_derivation_id: UUID | None = None,
        reaction_node_role: str | None = None,
        topology_smiles: str | None = None,
        topology_mol_block: str | None = None,
        topology_smarts: str | None = None,
        similarity_smiles: str | None = None,
        similarity_metric: SimilarityMetric = SimilarityMetric.tanimoto,
        thermodynamic_only: bool = False,
        imaginary_frequency_status: str | None = None,
        minimum_atom_count: int | None = None,
        maximum_atom_count: int | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
        project_id: UUID | None = None,
        filter_expression: str | None = None,
        sort_by: str = "default",
        sort_direction: str = "asc",
    ) -> GeometryPage:
        if sort_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        if sort_by not in {
            "default",
            "similarity",
            "created_at",
            "atom_count",
            "calculation_count",
        }:
            raise ValueError(
                "sort_by must be default, similarity, created_at, atom_count, or calculation_count"
            )
        if sort_by == "similarity" and similarity_smiles is None:
            raise ValueError("similarity sorting requires similarity_smiles")
        structure_inputs = {
            "topology_smiles": topology_smiles,
            "topology_mol_block": topology_mol_block,
            "topology_smarts": topology_smarts,
            "similarity_smiles": similarity_smiles,
        }
        if sum(value is not None for value in structure_inputs.values()) > 1:
            raise ValueError("topology structure filters and similarity_smiles conflict")
        enforce_structure_input_budget(
            structure_inputs,
            maximum_characters=get_settings().structure_query_max_characters,
        )
        if imaginary_frequency_status not in {None, "present", "absent", "unavailable"}:
            raise ValueError("imaginary_frequency_status must be present, absent, or unavailable")
        flat_filter_values = (
            topology_id,
            geometry_hash,
            internal_coordinate_hash,
            canonicalization_version,
            topology_derivation_id,
            reaction_node_role,
            topology_smiles,
            topology_mol_block,
            topology_smarts,
            similarity_smiles,
            imaginary_frequency_status,
            minimum_atom_count,
            maximum_atom_count,
        )
        if filter_expression is not None and (
            thermodynamic_only or any(value is not None for value in flat_filter_values)
        ):
            raise ValueError("filter_expression conflicts with flat geometry filters")

        scope = await query_visibility_scope(project_id=project_id)
        if sort_by == "calculation_count" and not scope.uses_project_geometry_catalog:
            raise ValueError("calculation_count sorting requires an authorized project scope")
        visibility_criterion = geometry_id_is_visible(scope, col(Geometry.id))
        uses_catalog_summary = scope.uses_project_geometry_catalog
        listing_visibility_criterion = true() if uses_catalog_summary else visibility_criterion
        predicates: list[Any] = []
        topology_predicates: list[Any] = []
        requires_geometry_count_join = False
        requires_topology_count_join = False
        if uses_catalog_summary:
            has_frequency_data = col(ProjectGeometryCatalog.has_frequency_data)
            has_imaginary_frequency = col(ProjectGeometryCatalog.has_imaginary_frequency)
        else:
            has_frequency_data = _geometry_has_frequency_data_predicate(scope, col(Geometry.id))
            has_imaginary_frequency = _geometry_has_imaginary_frequency_predicate(
                scope,
                col(Geometry.id),
            )
        if filter_expression is not None:
            if len(filter_expression) > get_settings().structure_query_max_characters:
                raise ValueError("filter_expression exceeds the configured character budget")
            try:
                expression = json.loads(filter_expression)
            except json.JSONDecodeError as error:
                raise ValueError("filter_expression must contain valid JSON") from error
            predicates.append(_geometry_query_expression_predicate(expression, scope))
            requires_geometry_count_join = True
            # A composable expression can refer to MolecularTopology fields;
            # retain the join for its count query even when the expression is
            # otherwise represented as a single SQL predicate.
            requires_topology_count_join = True
        for field, value in (
            (Geometry.topology_id, topology_id),
            (Geometry.geometry_hash, geometry_hash),
            (Geometry.internal_coordinate_hash, internal_coordinate_hash),
            (Geometry.canonicalization_version, canonicalization_version),
        ):
            if value is not None:
                predicates.append(col(field) == value)
                requires_geometry_count_join = True
        if topology_derivation_id is not None:
            predicates.append(
                select(col(CalculationFrame.id))
                .where(
                    col(CalculationFrame.geometry_id) == col(Geometry.id),
                    col(CalculationFrame.topology_derivation_id) == topology_derivation_id,
                    frame_id_is_visible(scope, col(CalculationFrame.id)),
                )
                .exists()
            )
            requires_geometry_count_join = True
        if reaction_node_role is not None:
            predicates.append(
                select(col(MappedReactionNodeGeometry.id))
                .join(
                    MappedReactionNode,
                    col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                    == col(MappedReactionNode.id),
                )
                .where(
                    col(MappedReactionNodeGeometry.geometry_id) == col(Geometry.id),
                    col(MappedReactionNode.role) == reaction_node_role,
                    mapped_reaction_id_is_visible(
                        scope,
                        col(MappedReactionNode.mapped_reaction_id),
                    ),
                    geometry_has_thermodynamic_property_predicate(
                        col(MappedReactionNodeGeometry.geometry_id)
                    ),
                )
                .exists()
            )
            requires_geometry_count_join = True
        if thermodynamic_only:
            predicates.append(
                col(ProjectGeometryCatalog.has_thermodynamic_property)
                if uses_catalog_summary
                else geometry_has_thermodynamic_property_predicate(col(Geometry.id))
            )
            requires_geometry_count_join = requires_geometry_count_join or not uses_catalog_summary
        if imaginary_frequency_status == "present":
            predicates.append(has_imaginary_frequency)
        elif imaginary_frequency_status == "absent":
            predicates.extend((has_frequency_data, ~has_imaginary_frequency))
        elif imaginary_frequency_status == "unavailable":
            predicates.append(~has_frequency_data)
        if topology_smarts is not None:
            if Chem.MolFromSmarts(topology_smarts) is None:
                raise ValueError("topology_smarts must be a valid SMARTS pattern")
            topology_predicates.append(rdkit_col(MolecularTopology.mol).has_smarts(topology_smarts))
        elif topology_smiles is not None or topology_mol_block is not None:
            query_molecule = (
                Chem.MolFromSmiles(topology_smiles)
                if topology_smiles is not None
                else Chem.MolFromMolBlock(
                    cast(str, topology_mol_block),
                    sanitize=True,
                    removeHs=True,
                    strictParsing=False,
                )
            )
            input_name = "topology_smiles" if topology_smiles is not None else "topology_mol_block"
            if query_molecule is None:
                raise ValueError(f"{input_name} must contain a valid molecule")
            query_smiles = Chem.MolToSmiles(
                Chem.RemoveHs(Chem.Mol(query_molecule)),
                canonical=True,
                isomericSmiles=True,
            )
            query_expression = mol_from_smiles(cast(CString, sql_cast(query_smiles, Text)))
            topology_predicates.append(
                rdkit_col(MolecularTopology.mol).has_substructure(cast(str, query_expression))
            )
        similarity_score: Any = literal(None)
        similarity_fingerprint: Any | None = None
        fingerprint_column: Any | None = None
        if similarity_smiles is not None:
            query_molecule = Chem.MolFromSmiles(similarity_smiles)
            if query_molecule is None:
                raise ValueError("similarity_smiles must contain a valid molecule")
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
                if similarity_metric == SimilarityMetric.tanimoto
                else dice_sml(fingerprint_column, similarity_fingerprint)
            )
            topology_predicates.append(fingerprint_column.is_not(None))
        _validate_range(
            minimum_atom_count,
            maximum_atom_count,
            minimum_name="minimum_atom_count",
            maximum_name="maximum_atom_count",
        )
        if minimum_atom_count is not None or maximum_atom_count is not None:
            requires_geometry_count_join = True
            if minimum_atom_count is not None:
                topology_predicates.append(col(MolecularTopology.atom_count) >= minimum_atom_count)
            if maximum_atom_count is not None:
                topology_predicates.append(col(MolecularTopology.atom_count) <= maximum_atom_count)
        if uses_catalog_summary and not predicates and not topology_predicates:
            count_statement = select(cast(Any, ProjectGeometryCatalogCount.geometry_count)).where(
                col(ProjectGeometryCatalogCount.project_id) == scope.requested_project_id
            )
        elif uses_catalog_summary:
            count_statement = (
                select(func.count())
                .select_from(ProjectGeometryCatalog)
                .where(col(ProjectGeometryCatalog.project_id) == scope.requested_project_id)
            )
            if requires_geometry_count_join or topology_predicates:
                count_statement = count_statement.join(
                    Geometry,
                    col(ProjectGeometryCatalog.geometry_id) == col(Geometry.id),
                )
            if requires_topology_count_join or topology_predicates:
                count_statement = count_statement.join(
                    MolecularTopology,
                    col(Geometry.topology_id) == col(MolecularTopology.id),
                )
            count_statement = count_statement.where(*predicates, *topology_predicates)
        elif not scope.unrestricted and not predicates and not topology_predicates:
            # Visibility for a Geometry is defined by at least one visible frame.
            # Counting those distinct geometry IDs avoids a second full Geometry scan.
            count_statement = select(func.count(func.distinct(CalculationFrame.geometry_id))).where(
                calculation_frame_is_visible(scope, col(CalculationFrame.parse_revision_id))
            )
        else:
            # The topology join is only needed for structure/atom filters. For
            # ordinary geometry filters, counting directly from Geometry keeps
            # this request index-only and avoids touching the large mol column.
            count_statement = select(func.count()).select_from(Geometry)
            if requires_topology_count_join or topology_predicates:
                count_statement = count_statement.join(
                    MolecularTopology,
                    col(Geometry.topology_id) == col(MolecularTopology.id),
                )
            count_statement = count_statement.where(
                visibility_criterion,
                *predicates,
                *topology_predicates,
            )
        calculation_count = (
            select(func.count())
            .select_from(CalculationFrame)
            .where(
                col(CalculationFrame.geometry_id) == col(Geometry.id),
                calculation_frame_is_visible(
                    scope,
                    col(CalculationFrame.parse_revision_id),
                ),
            )
            .scalar_subquery()
            .label("calculation_count")
        )
        if uses_catalog_summary:
            calculation_count = col(ProjectGeometryCatalog.frame_count).label("calculation_count")
        base_statement = (
            select(
                Geometry,
                MolecularTopology,
                calculation_count,
                has_frequency_data.label("has_frequency_data"),
                has_imaginary_frequency.label("has_imaginary_frequency"),
                similarity_score.label("similarity_score"),
            )
            .options(
                defer(cast(Any, Geometry.mol)),
                defer(cast(Any, Geometry.internal_coordinate_distances_angstrom)),
                defer(cast(Any, Geometry.internal_coordinate_angles_degrees)),
                defer(cast(Any, Geometry.internal_coordinate_dihedrals_degrees)),
                defer(cast(Any, MolecularTopology.mol)),
            )
            .join(MolecularTopology, col(Geometry.topology_id) == col(MolecularTopology.id))
        )
        if uses_catalog_summary:
            base_statement = base_statement.join(
                ProjectGeometryCatalog,
                and_(
                    col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                    col(ProjectGeometryCatalog.geometry_id) == col(Geometry.id),
                ),
            )
        filtered_statement = base_statement.where(
            listing_visibility_criterion,
            *predicates,
            *topology_predicates,
        )
        catalogue_order_by = (
            (col(ProjectGeometryCatalog.geometry_created_at), col(Geometry.id))
            if uses_catalog_summary
            else (col(Geometry.created_at), col(Geometry.id))
        )
        fast_catalogue_page = (
            sort_by == "default"
            and not scope.unrestricted
            and not predicates
            and not topology_predicates
            and similarity_fingerprint is None
        )
        async with session_factory() as session:
            total_value = (await session.execute(count_statement)).scalar_one_or_none()
            total = int(total_value or 0)
            if fast_catalogue_page and uses_catalog_summary:
                # Page the narrow catalogue index first. Joining Geometry before
                # applying OFFSET makes PostgreSQL sort/scan the multi-GB table
                # for deep pages.
                thermodynamic_catalog = (
                    select(col(ProjectGeometryCatalog.geometry_id))
                    .where(
                        col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                        col(ProjectGeometryCatalog.has_thermodynamic_property),
                    )
                    .order_by(
                        col(ProjectGeometryCatalog.geometry_created_at),
                        col(ProjectGeometryCatalog.geometry_id),
                    )
                )
                non_thermodynamic_catalog = (
                    select(col(ProjectGeometryCatalog.geometry_id))
                    .where(
                        col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                        ~col(ProjectGeometryCatalog.has_thermodynamic_property),
                    )
                    .order_by(
                        col(ProjectGeometryCatalog.geometry_created_at),
                        col(ProjectGeometryCatalog.geometry_id),
                    )
                )
                thermodynamic_total = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(ProjectGeometryCatalog)
                            .where(
                                col(ProjectGeometryCatalog.project_id)
                                == scope.requested_project_id,
                                col(ProjectGeometryCatalog.has_thermodynamic_property),
                            )
                        )
                    ).scalar_one()
                )
                if offset < thermodynamic_total:
                    page_ids = thermodynamic_catalog.offset(offset).limit(limit)
                    thermodynamic_statement = filtered_statement.where(
                        col(Geometry.id).in_(page_ids)
                    ).order_by(*catalogue_order_by)
                    rows = list((await session.execute(thermodynamic_statement)).all())
                    remaining = limit - len(rows)
                    if remaining:
                        page_ids = non_thermodynamic_catalog.limit(remaining)
                        non_thermodynamic_statement = filtered_statement.where(
                            col(Geometry.id).in_(page_ids)
                        ).order_by(*catalogue_order_by)
                        rows.extend((await session.execute(non_thermodynamic_statement)).all())
                else:
                    page_ids = non_thermodynamic_catalog.offset(offset - thermodynamic_total).limit(
                        limit
                    )
                    non_thermodynamic_statement = filtered_statement.where(
                        col(Geometry.id).in_(page_ids)
                    ).order_by(*catalogue_order_by)
                    rows = list((await session.execute(non_thermodynamic_statement)).all())
            elif fast_catalogue_page:
                thermodynamic_geometry_ids = geometry_ids_with_thermodynamic_property(
                    calculation_frame_is_visible(scope, col(CalculationFrame.parse_revision_id))
                ).distinct()
                thermodynamic_total = int(
                    (
                        await session.execute(
                            select(func.count()).select_from(thermodynamic_geometry_ids.subquery())
                        )
                    ).scalar_one()
                )
                # The ID subquery is derived from visible frames, so applying the
                # outer Geometry visibility predicate here would repeat its full scan.
                thermodynamic_statement = base_statement.where(
                    col(Geometry.id).in_(thermodynamic_geometry_ids)
                ).order_by(*catalogue_order_by)
                non_thermodynamic_statement = filtered_statement.where(
                    ~col(Geometry.id).in_(thermodynamic_geometry_ids)
                ).order_by(*catalogue_order_by)
                if offset < thermodynamic_total:
                    rows = list(
                        (
                            await session.execute(
                                thermodynamic_statement.offset(offset).limit(limit)
                            )
                        ).all()
                    )
                    remaining = limit - len(rows)
                    if remaining:
                        rows.extend(
                            (
                                await session.execute(non_thermodynamic_statement.limit(remaining))
                            ).all()
                        )
                else:
                    rows = list(
                        (
                            await session.execute(
                                non_thermodynamic_statement.offset(
                                    offset - thermodynamic_total
                                ).limit(limit)
                            )
                        ).all()
                    )
            elif similarity_fingerprint is not None and sort_by in {"default", "similarity"}:
                assert fingerprint_column is not None
                nearest_operator = (
                    "<%>" if similarity_metric == SimilarityMetric.tanimoto else "<#>"
                )
                rows = list(
                    (
                        await session.execute(
                            filtered_statement.order_by(
                                fingerprint_column.op(nearest_operator)(similarity_fingerprint),
                                col(Geometry.id),
                            )
                            .offset(offset)
                            .limit(limit)
                        )
                    ).all()
                )
            elif (
                uses_catalog_summary
                and sort_by in {"created_at", "calculation_count"}
                and not topology_predicates
                and not requires_geometry_count_join
            ):
                page_order = (
                    (
                        col(ProjectGeometryCatalog.geometry_created_at)
                        if sort_by == "created_at"
                        else col(ProjectGeometryCatalog.frame_count)
                    ).asc()
                    if sort_direction == "asc"
                    else (
                        col(ProjectGeometryCatalog.geometry_created_at)
                        if sort_by == "created_at"
                        else col(ProjectGeometryCatalog.frame_count)
                    )
                    .desc()
                    .nulls_last()
                )
                page_id_order = (
                    col(ProjectGeometryCatalog.geometry_id).asc()
                    if sort_direction == "asc"
                    else col(ProjectGeometryCatalog.geometry_id).desc()
                )
                page_ids = (
                    select(col(ProjectGeometryCatalog.geometry_id))
                    .where(
                        col(ProjectGeometryCatalog.project_id) == scope.requested_project_id,
                        *predicates,
                    )
                    .order_by(page_order, page_id_order)
                    .offset(offset)
                    .limit(limit)
                )
                rows = list(
                    (
                        await session.execute(
                            filtered_statement.where(col(Geometry.id).in_(page_ids)).order_by(
                                page_order,
                                page_id_order,
                            )
                        )
                    ).all()
                )
            elif sort_by == "default":
                thermodynamic_sort = (
                    col(ProjectGeometryCatalog.has_thermodynamic_property)
                    if uses_catalog_summary
                    else geometry_has_thermodynamic_property_predicate(col(Geometry.id))
                )
                rows = list(
                    (
                        await session.execute(
                            filtered_statement.order_by(
                                desc(thermodynamic_sort),
                                *catalogue_order_by,
                            )
                            .offset(offset)
                            .limit(limit)
                        )
                    ).all()
                )
            else:
                geometry_sort_fields = {
                    "created_at": (
                        col(ProjectGeometryCatalog.geometry_created_at)
                        if uses_catalog_summary
                        else col(Geometry.created_at)
                    ),
                    "atom_count": col(MolecularTopology.atom_count),
                    "calculation_count": calculation_count,
                }
                sort_expression = geometry_sort_fields[sort_by]
                ordered_sort = (
                    sort_expression.asc().nulls_last()
                    if sort_direction == "asc"
                    else sort_expression.desc().nulls_last()
                )
                rows = list(
                    (
                        await session.execute(
                            filtered_statement.order_by(
                                ordered_sort,
                                col(Geometry.id),
                            )
                            .offset(offset)
                            .limit(limit)
                        )
                    ).all()
                )
            page_geometry_ids = [
                _required_uuid(geometry.id, "Geometry") for geometry, _, _, _, _, _ in rows
            ]
            reaction_binding_counts: dict[UUID, int] = {}
            transition_state_geometry_ids: set[UUID] = set()
            if page_geometry_ids:
                binding_rows = (
                    await session.execute(
                        select(
                            col(MappedReactionNodeGeometry.geometry_id),
                            func.count().label("reaction_binding_count"),
                        )
                        .join(
                            MappedReactionNode,
                            col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                            == col(MappedReactionNode.id),
                        )
                        .where(
                            col(MappedReactionNodeGeometry.geometry_id).in_(page_geometry_ids),
                            mapped_reaction_id_is_visible(
                                scope,
                                col(MappedReactionNode.mapped_reaction_id),
                            ),
                            geometry_has_thermodynamic_property_predicate(
                                col(MappedReactionNodeGeometry.geometry_id)
                            ),
                        )
                        .group_by(col(MappedReactionNodeGeometry.geometry_id))
                    )
                ).all()
                reaction_binding_counts = {
                    geometry_id: int(binding_count) for geometry_id, binding_count in binding_rows
                }
                transition_state_geometry_ids = set(
                    (
                        await session.execute(
                            select(col(MappedReactionNodeGeometry.geometry_id))
                            .distinct()
                            .join(
                                MappedReactionNode,
                                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                                == col(MappedReactionNode.id),
                            )
                            .where(
                                col(MappedReactionNodeGeometry.geometry_id).in_(page_geometry_ids),
                                col(MappedReactionNode.role)
                                == MappedReactionNodeRole.TRANSITION_STATE,
                                mapped_reaction_id_is_visible(
                                    scope,
                                    col(MappedReactionNode.mapped_reaction_id),
                                ),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        return GeometryPage(
            items=[
                _geometry_summary(
                    geometry,
                    topology,
                    int(calculations),
                    reaction_binding_counts.get(_required_uuid(geometry.id, "Geometry"), 0),
                    _required_uuid(geometry.id, "Geometry") in transition_state_geometry_ids,
                    bool(has_frequency_data),
                    bool(has_imaginary_frequency),
                    float(similarity_score) if similarity_score is not None else None,
                )
                for (
                    geometry,
                    topology,
                    calculations,
                    has_frequency_data,
                    has_imaginary_frequency,
                    similarity_score,
                ) in rows
            ],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_geometry(
        cls,
        geometry_id: UUID,
        project_id: UUID | None = None,
    ) -> GeometryDetail | None:
        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Geometry, MolecularTopology)
                    .join(MolecularTopology, col(Geometry.topology_id) == col(MolecularTopology.id))
                    .where(
                        col(Geometry.id) == geometry_id,
                        geometry_id_is_visible(scope, col(Geometry.id)),
                    )
                )
            ).first()
            if row is None:
                return None
            geometry, topology = row
            has_frequency_data, has_imaginary_frequency, is_transition_state = (
                await session.execute(
                    select(
                        _geometry_has_frequency_data_predicate(scope, geometry_id),
                        _geometry_has_imaginary_frequency_predicate(scope, geometry_id),
                        _geometry_is_transition_state_predicate(scope, geometry_id),
                    )
                )
            ).one()
            summary = _geometry_summary(
                geometry,
                topology,
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(CalculationFrame)
                            .where(
                                col(CalculationFrame.geometry_id) == geometry_id,
                                frame_id_is_visible(scope, col(CalculationFrame.id)),
                            )
                        )
                    ).scalar_one()
                ),
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(MappedReactionNodeGeometry)
                            .join(
                                MappedReactionNode,
                                col(MappedReactionNodeGeometry.mapped_reaction_node_id)
                                == col(MappedReactionNode.id),
                            )
                            .where(
                                col(MappedReactionNodeGeometry.geometry_id) == geometry_id,
                                mapped_reaction_id_is_visible(
                                    scope,
                                    col(MappedReactionNode.mapped_reaction_id),
                                ),
                                geometry_has_thermodynamic_property_predicate(
                                    col(MappedReactionNodeGeometry.geometry_id)
                                ),
                            )
                        )
                    ).scalar_one()
                ),
                bool(is_transition_state),
                bool(has_frequency_data),
                bool(has_imaginary_frequency),
            )
            frame_rows = (
                await session.execute(
                    _frame_select()
                    .where(
                        col(CalculationFrame.geometry_id) == geometry_id,
                        frame_id_is_visible(scope, col(CalculationFrame.id)),
                    )
                    .order_by(col(CalculationFrame.file_frame_index))
                )
            ).all()
            energy_rows = (
                await session.execute(
                    select(CalculationFrame, CalculationProtocol, ThermochemistryResult)
                    .join(
                        CalculationSegment,
                        col(CalculationFrame.segment_id) == col(CalculationSegment.id),
                    )
                    .outerjoin(
                        ThermochemistryResult,
                        col(ThermochemistryResult.frame_id) == col(CalculationFrame.id),
                    )
                    .outerjoin(
                        CalculationProtocol,
                        col(CalculationSegment.protocol_id) == col(CalculationProtocol.id),
                    )
                    .where(
                        col(CalculationFrame.geometry_id) == geometry_id,
                        frame_id_is_visible(scope, col(CalculationFrame.id)),
                    )
                )
            ).all()
            energy_view = geometry_energy_composites(
                [geometry_id],
                cast(
                    Iterable[
                        tuple[
                            CalculationFrame,
                            CalculationProtocol | None,
                            ThermochemistryResult | None,
                        ]
                    ],
                    energy_rows,
                ),
            )[geometry_id].view
            energy_view = energy_view.model_copy(
                update={"charge": geometry.charge, "multiplicity": geometry.multiplicity}
            )
        return GeometryDetail(
            **summary.model_dump(),
            frames=[_frame_summary(*frame_row) for frame_row in frame_rows],
            energy_view=energy_view,
            coordinates=_geometry_coordinates(geometry),
        )


class CalculationProtocolQueryService(UseCaseService):  # type: ignore[misc]
    """Search normalized QM protocol identities."""

    @query  # type: ignore[untyped-decorator]
    async def list_calculation_protocols(
        cls,
        protocol_hash: str | None = None,
        qm_software: str | None = None,
        qm_software_version: str | None = None,
        method_family: str | None = None,
        method: str | None = None,
        basis_set: str | None = None,
        solvation_model: str | None = None,
        solvent: str | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
    ) -> CalculationProtocolPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            calculation_protocol_id_is_visible(scope, col(CalculationProtocol.id))
        ]
        for field, value in (
            (CalculationProtocol.protocol_hash, protocol_hash),
            (CalculationProtocol.qm_software, qm_software),
            (CalculationProtocol.qm_software_version, qm_software_version),
            (CalculationProtocol.method_family, method_family),
            (CalculationProtocol.method, method),
            (CalculationProtocol.basis_set, basis_set),
            (CalculationProtocol.solvation_model, solvation_model),
            (CalculationProtocol.solvent, solvent),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        count_statement = select(func.count()).select_from(CalculationProtocol).where(*predicates)
        statement = (
            select(CalculationProtocol)
            .where(*predicates)
            .order_by(col(CalculationProtocol.created_at), col(CalculationProtocol.id))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            protocols = (await session.execute(statement)).scalars().all()
        return CalculationProtocolPage(
            items=[_protocol_summary(protocol) for protocol in protocols],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_calculation_protocol(cls, protocol_id: UUID) -> CalculationProtocolDetail | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            protocol = (
                await session.execute(
                    select(CalculationProtocol).where(
                        col(CalculationProtocol.id) == protocol_id,
                        calculation_protocol_id_is_visible(
                            scope,
                            col(CalculationProtocol.id),
                        ),
                    )
                )
            ).scalar_one_or_none()
            if protocol is None:
                return None
            segment_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(CalculationSegment)
                        .where(
                            col(CalculationSegment.protocol_id) == protocol_id,
                            col(CalculationSegment.parse_revision_id).in_(
                                visible_parse_revision_ids(scope)
                            ),
                        )
                    )
                ).scalar_one()
            )
        return CalculationProtocolDetail(
            **_protocol_summary(protocol).model_dump(),
            normalized_spec_json=json.dumps(
                protocol.normalized_spec, sort_keys=True, separators=(",", ":")
            ),
            segment_count=segment_count,
        )


class ArtifactIngestionQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect the automatic upload-to-database ingestion lifecycle."""

    @query  # type: ignore[untyped-decorator]
    async def list_artifact_ingestions(
        cls,
        artifact_file_id: UUID | None = None,
        status: ArtifactIngestionStatus | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
    ) -> ArtifactIngestionPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            artifact_id_is_visible(scope, col(ArtifactIngestion.artifact_file_id))
        ]
        if artifact_file_id is not None:
            predicates.append(col(ArtifactIngestion.artifact_file_id) == artifact_file_id)
        if status is not None:
            predicates.append(col(ArtifactIngestion.status) == status)
        count_statement = select(func.count()).select_from(ArtifactIngestion).where(*predicates)
        statement = (
            select(ArtifactIngestion)
            .where(*predicates)
            .order_by(col(ArtifactIngestion.created_at), col(ArtifactIngestion.id))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).scalars().all()
        return ArtifactIngestionPage(
            items=[_ingestion_summary(row) for row in rows], page=_page(total, limit, offset)
        )

    @query  # type: ignore[untyped-decorator]
    async def get_artifact_ingestion(cls, ingestion_id: UUID) -> ArtifactIngestionSummary | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ArtifactIngestion).where(
                        col(ArtifactIngestion.id) == ingestion_id,
                        artifact_id_is_visible(scope, col(ArtifactIngestion.artifact_file_id)),
                    )
                )
            ).scalar_one_or_none()
        return _ingestion_summary(row) if row is not None else None


class ParseRevisionQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect immutable parser revisions produced for an artifact."""

    @query  # type: ignore[untyped-decorator]
    async def list_parse_revisions(
        cls,
        artifact_file_id: UUID | None = None,
        status: str | None = None,
        source_format: str | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
    ) -> ParseRevisionPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [parse_revision_id_is_visible(scope, col(ParseRevision.id))]
        if artifact_file_id is not None:
            predicates.append(col(ParseRevision.artifact_file_id) == artifact_file_id)
        if status is not None:
            predicates.append(col(ParseRevision.status) == status)
        if source_format is not None:
            predicates.append(col(ParseRevision.source_format) == source_format)
        count_statement = select(func.count()).select_from(ParseRevision).where(*predicates)
        statement = (
            select(ParseRevision)
            .where(*predicates)
            .order_by(col(ParseRevision.artifact_file_id), col(ParseRevision.revision_number))
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).scalars().all()
        return ParseRevisionPage(
            items=[_parse_revision_summary(row) for row in rows], page=_page(total, limit, offset)
        )

    @query  # type: ignore[untyped-decorator]
    async def get_parse_revision(cls, revision_id: UUID) -> ParseRevisionSummary | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            revision = (
                await session.execute(
                    select(ParseRevision).where(
                        col(ParseRevision.id) == revision_id,
                        parse_revision_id_is_visible(scope, col(ParseRevision.id)),
                    )
                )
            ).scalar_one_or_none()
        return _parse_revision_summary(revision) if revision is not None else None


class CalculationSegmentQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect source-local calculation segments and termination states."""

    @query  # type: ignore[untyped-decorator]
    async def list_calculation_segments(
        cls,
        parse_revision_id: UUID | None = None,
        protocol_id: UUID | None = None,
        termination_status: str | None = None,
        scf_status: str | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
    ) -> CalculationSegmentPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            parse_revision_id_is_visible(scope, col(CalculationSegment.parse_revision_id))
        ]
        for field, value in (
            (CalculationSegment.parse_revision_id, parse_revision_id),
            (CalculationSegment.protocol_id, protocol_id),
            (CalculationSegment.termination_status, termination_status),
            (CalculationSegment.scf_status, scf_status),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        count_statement = select(func.count()).select_from(CalculationSegment).where(*predicates)
        statement = (
            select(CalculationSegment)
            .where(*predicates)
            .order_by(
                col(CalculationSegment.parse_revision_id), col(CalculationSegment.segment_index)
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).scalars().all()
        return CalculationSegmentPage(
            items=[_segment_summary(row) for row in rows], page=_page(total, limit, offset)
        )

    @query  # type: ignore[untyped-decorator]
    async def get_calculation_segment(cls, segment_id: UUID) -> CalculationSegmentSummary | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            segment = (
                await session.execute(
                    select(CalculationSegment).where(
                        col(CalculationSegment.id) == segment_id,
                        parse_revision_id_is_visible(
                            scope, col(CalculationSegment.parse_revision_id)
                        ),
                    )
                )
            ).scalar_one_or_none()
        return _segment_summary(segment) if segment is not None else None


class TransitionStateInferenceQueryService(UseCaseService):  # type: ignore[misc]
    """Inspect TS inference outcomes and their persisted reaction/frame links."""

    @query  # type: ignore[untyped-decorator]
    async def list_transition_state_inferences(
        cls,
        artifact_ingestion_id: UUID | None = None,
        parse_revision_id: UUID | None = None,
        status: str | None = None,
        logical_reaction_id: UUID | None = None,
        mapped_reaction_id: UUID | None = None,
        calculation_frame_id: UUID | None = None,
        minimum_imaginary_frequency_cm1: float | None = None,
        maximum_imaginary_frequency_cm1: float | None = None,
        reactant_product_changed: bool | None = None,
        limit: QueryLimit = 50,
        offset: QueryOffset = 0,
    ) -> TransitionStateInferencePage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [
            parse_revision_id_is_visible(scope, col(TransitionStateInference.parse_revision_id))
        ]
        for field, value in (
            (TransitionStateInference.artifact_ingestion_id, artifact_ingestion_id),
            (TransitionStateInference.parse_revision_id, parse_revision_id),
            (TransitionStateInference.status, status),
            (TransitionStateInference.logical_reaction_id, logical_reaction_id),
            (TransitionStateInference.mapped_reaction_id, mapped_reaction_id),
            (TransitionStateInference.calculation_frame_id, calculation_frame_id),
        ):
            if value is not None:
                predicates.append(col(field) == value)
        _validate_range(
            minimum_imaginary_frequency_cm1,
            maximum_imaginary_frequency_cm1,
            minimum_name="minimum_imaginary_frequency_cm1",
            maximum_name="maximum_imaginary_frequency_cm1",
        )
        if minimum_imaginary_frequency_cm1 is not None:
            predicates.append(
                col(TransitionStateInference.imaginary_frequency_cm1)
                >= minimum_imaginary_frequency_cm1
            )
        if maximum_imaginary_frequency_cm1 is not None:
            predicates.append(
                col(TransitionStateInference.imaginary_frequency_cm1)
                <= maximum_imaginary_frequency_cm1
            )
        changed_expression = _reaction_topology_changed_expression(
            reaction_id_column=col(TransitionStateInference.logical_reaction_id),
            correlation_entity=TransitionStateInference,
        )
        if reactant_product_changed is not None:
            predicates.append(changed_expression == reactant_product_changed)
        count_statement = (
            select(func.count()).select_from(TransitionStateInference).where(*predicates)
        )
        statement = (
            select(
                TransitionStateInference,
                changed_expression.label("reactant_product_changed"),
            )
            .where(*predicates)
            .order_by(
                col(TransitionStateInference.artifact_ingestion_id),
                col(TransitionStateInference.file_frame_index),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return TransitionStateInferencePage(
            items=[
                _ts_summary(
                    inference,
                    reactant_product_changed=(
                        bool(changed_value) if changed_value is not None else None
                    ),
                )
                for inference, changed_value in rows
            ],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_transition_state_inference(
        cls,
        inference_id: UUID,
    ) -> TransitionStateInferenceSummary | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            inference = (
                await session.execute(
                    select(TransitionStateInference).where(
                        col(TransitionStateInference.id) == inference_id,
                        parse_revision_id_is_visible(
                            scope, col(TransitionStateInference.parse_revision_id)
                        ),
                    )
                )
            ).scalar_one_or_none()
        return _ts_summary(inference) if inference is not None else None


class ScientificArrayQueryService(UseCaseService):  # type: ignore[misc]
    """List scientific-array metadata without implicitly loading deferred payloads."""

    @query  # type: ignore[untyped-decorator]
    async def list_scientific_arrays(
        cls,
        frame_id: UUID | None = None,
        kind: str | None = None,
        owner_kind: str | None = None,
        dtype: str | None = None,
        shape: list[int] | None = None,
        payload_sha256: str | None = None,
        limit: QueryLimit = 100,
        offset: QueryOffset = 0,
    ) -> ScientificArrayPage:
        scope = await query_visibility_scope()
        predicates: list[Any] = [frame_id_is_visible(scope, col(ScientificArray.frame_id))]
        if frame_id is not None:
            predicates.append(col(ScientificArray.frame_id) == frame_id)
        if kind is not None:
            predicates.append(col(ScientificArray.kind) == kind)
        if dtype is not None:
            predicates.append(col(ScientificArray.dtype) == dtype)
        if shape is not None:
            if not shape or any(dimension < 0 for dimension in shape):
                raise ValueError("shape must contain nonnegative dimensions")
            predicates.append(col(ScientificArray.shape) == shape)
        if payload_sha256 is not None:
            predicates.append(col(ScientificArray.payload_sha256) == payload_sha256)
        owner_fields = {
            "molecular_orbital_result": ScientificArrayAssignment.molecular_orbital_result_id,
            "atomic_population_series": ScientificArrayAssignment.atomic_population_series_id,
            "polarizability_result": ScientificArrayAssignment.polarizability_result_id,
            "nmr_result": ScientificArrayAssignment.nmr_result_id,
            "nmr_shielding_tensor": ScientificArrayAssignment.nmr_shielding_tensor_id,
            "bond_order_result": ScientificArrayAssignment.bond_order_result_id,
            "single_point_property_result": (
                ScientificArrayAssignment.single_point_property_result_id
            ),
            "electronic_state": ScientificArrayAssignment.electronic_state_id,
        }
        if owner_kind is not None:
            owner_field = owner_fields.get(owner_kind)
            if owner_field is None:
                raise ValueError(f"unsupported scientific-array owner_kind: {owner_kind}")
            predicates.append(cast(Any, owner_field).is_not(None))
        count_statement = select(func.count()).select_from(ScientificArray).where(*predicates)
        statement = (
            select(ScientificArray, ScientificArrayAssignment)
            .outerjoin(
                ScientificArrayAssignment,
                col(ScientificArrayAssignment.scientific_array_id) == col(ScientificArray.id),
            )
            .where(*predicates)
            .order_by(
                col(ScientificArray.frame_id),
                col(ScientificArray.kind),
                col(ScientificArray.ordinal),
            )
            .offset(offset)
            .limit(limit)
        )
        async with session_factory() as session:
            # Assignment filters belong to the outer-join relation, so apply them
            # to the count after building the same join shape.
            if owner_kind is not None:
                count_statement = count_statement.select_from(ScientificArray).join(
                    ScientificArrayAssignment,
                    col(ScientificArrayAssignment.scientific_array_id) == col(ScientificArray.id),
                )
            total = int((await session.execute(count_statement)).scalar_one())
            rows = (await session.execute(statement)).all()
        return ScientificArrayPage(
            items=[_array_summary(array, assignment) for array, assignment in rows],
            page=_page(total, limit, offset),
        )

    @query  # type: ignore[untyped-decorator]
    async def get_scientific_array(cls, array_id: UUID) -> ScientificArraySummary | None:
        scope = await query_visibility_scope()
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ScientificArray, ScientificArrayAssignment)
                    .outerjoin(
                        ScientificArrayAssignment,
                        col(ScientificArrayAssignment.scientific_array_id)
                        == col(ScientificArray.id),
                    )
                    .where(
                        col(ScientificArray.id) == array_id,
                        frame_id_is_visible(scope, col(ScientificArray.frame_id)),
                    )
                )
            ).first()
        return _array_summary(*row) if row is not None else None


class ReactionEnergyQueryService(UseCaseService):  # type: ignore[misc]
    """Build relative reaction energies and edge barriers from coordinate composites."""

    @query  # type: ignore[untyped-decorator]
    async def get_reaction_energy_profile(
        cls,
        mapped_reaction_id: UUID,
        project_id: UUID | None = None,
        energy_kind: str = "gibbs_free_energy_hartree",
        reference_node_id: UUID | None = None,
    ) -> ReactionEnergyProfile | None:
        if energy_kind not in REACTION_ENERGY_KINDS:
            choices = ", ".join(sorted(REACTION_ENERGY_KINDS))
            raise ValueError(f"unsupported energy_kind; expected one of: {choices}")
        mapped_reaction_kwargs: dict[str, Any] = {"mapped_reaction_id": mapped_reaction_id}
        if project_id is not None:
            mapped_reaction_kwargs["project_id"] = project_id
        reaction = cast(
            Any,
            await MappedReactionQueryService.get_mapped_reaction(**mapped_reaction_kwargs),
        )
        if reaction is None:
            return None

        energies: dict[UUID, float | None] = {}
        for node in reaction.nodes:
            properties = node.additive_properties
            value = getattr(properties, energy_kind) if properties is not None else None
            energies[node.id] = float(value) if value is not None else None

        resolved_reference_id: UUID | None
        if reference_node_id is not None:
            if reference_node_id not in energies:
                raise ValueError("reference_node_id is not a node of this mapped reaction")
            resolved_reference_id = reference_node_id
        else:
            source_ids = [edge.source_node_id for edge in reaction.edges]
            resolved_reference_id = next(
                (node_id for node_id in source_ids if energies.get(node_id) is not None),
                next(
                    (node.id for node in reaction.nodes if energies[node.id] is not None),
                    None,
                ),
            )
        reference_energy = (
            energies[resolved_reference_id] if resolved_reference_id is not None else None
        )

        def difference(left: float | None, right: float | None) -> float | None:
            if left is None or right is None:
                return None
            return round((left - right) * HARTREE_TO_KCAL_MOL, 6)

        return ReactionEnergyProfile(
            mapped_reaction_id=mapped_reaction_id,
            energy_kind=energy_kind,
            reference_node_id=resolved_reference_id,
            points=[
                ReactionEnergyPoint(
                    node_id=node.id,
                    node_key=node.node_key,
                    node_index=node.node_index,
                    role=node.role,
                    energy_kind=energy_kind,
                    energy_hartree=(
                        round_energy_hartree(energies[node.id])
                        if energies[node.id] is not None
                        else None
                    ),
                    relative_energy_kcal_mol=difference(energies[node.id], reference_energy),
                )
                for node in reaction.nodes
            ],
            edges=[
                ReactionEnergyEdgeView(
                    edge_id=edge.id,
                    edge_key=edge.edge_key,
                    source_node_id=edge.source_node_id,
                    target_node_id=edge.target_node_id,
                    transition_state_node_id=edge.transition_state_node_id,
                    reaction_energy_kcal_mol=difference(
                        energies.get(edge.target_node_id),
                        energies.get(edge.source_node_id),
                    ),
                    forward_barrier_kcal_mol=(
                        difference(
                            energies.get(edge.transition_state_node_id),
                            energies.get(edge.source_node_id),
                        )
                        if edge.transition_state_node_id is not None
                        else None
                    ),
                    reverse_barrier_kcal_mol=(
                        difference(
                            energies.get(edge.transition_state_node_id),
                            energies.get(edge.target_node_id),
                        )
                        if edge.transition_state_node_id is not None
                        else None
                    ),
                )
                for edge in reaction.edges
            ],
        )

    @query  # type: ignore[untyped-decorator]
    async def get_mapped_reaction_thermodynamics(
        cls,
        mapped_reaction_id: UUID,
        project_id: UUID | None = None,
    ) -> MappedReactionThermodynamics | None:
        """Return the materialized profiles for one mapped reaction.

        Profile rows are written by source-change workflows. Read requests only
        touch the indexed materialization and never rebuild geometry/calculation
        composites in the middleware.
        """

        scope = await query_visibility_scope(project_id=project_id)
        async with session_factory() as session:
            mapped_reaction = (
                await session.execute(
                    select(MappedReaction).where(
                        col(MappedReaction.id) == mapped_reaction_id,
                        mapped_reaction_id_is_visible(scope, col(MappedReaction.id)),
                    )
                )
            ).scalar_one_or_none()
            if mapped_reaction is None:
                return None
            rows = (
                (
                    await session.execute(
                        select(MappedReactionThermodynamicProfile)
                        .where(
                            col(MappedReactionThermodynamicProfile.mapped_reaction_id)
                            == mapped_reaction_id
                        )
                        .order_by(
                            col(MappedReactionThermodynamicProfile.electronic_level),
                            col(MappedReactionThermodynamicProfile.thermochemistry_level),
                            col(MappedReactionThermodynamicProfile.temperature_kelvin),
                            col(MappedReactionThermodynamicProfile.pressure_atm),
                            col(MappedReactionThermodynamicProfile.id),
                        )
                    )
                )
                .scalars()
                .all()
            )

        profiles = [
            MappedReactionThermodynamicsProfile(
                mapped_reaction_id=mapped_reaction_id,
                policy_version=row.policy_version,
                electronic_level=list(row.electronic_level),
                thermochemistry_level=list(row.thermochemistry_level),
                level_of_theory=format_composite_level_of_theory(
                    row.electronic_level,
                    row.thermochemistry_level,
                ),
                temperature_kelvin=row.temperature_kelvin,
                pressure_atm=row.pressure_atm,
                reactants=ThermodynamicStateView.model_validate(row.reactants),
                transition_state=(
                    ThermodynamicStateView.model_validate(row.transition_state)
                    if row.transition_state is not None
                    else None
                ),
                products=(
                    ThermodynamicStateView.model_validate(row.products)
                    if row.products is not None
                    else None
                ),
                activation=(
                    ThermodynamicDifferenceView(
                        enthalpy_kcal_mol=float(row.activation_enthalpy_kcal_mol),
                        gibbs_free_energy_kcal_mol=float(row.activation_gibbs_free_energy_kcal_mol),
                        entropy_cal_mol_k=float(row.activation_entropy_cal_mol_k),
                    )
                    if row.activation_enthalpy_kcal_mol is not None
                    and row.activation_gibbs_free_energy_kcal_mol is not None
                    and row.activation_entropy_cal_mol_k is not None
                    else None
                ),
                reaction=(
                    ThermodynamicDifferenceView(
                        enthalpy_kcal_mol=float(row.reaction_enthalpy_kcal_mol),
                        gibbs_free_energy_kcal_mol=float(row.reaction_gibbs_free_energy_kcal_mol),
                        entropy_cal_mol_k=float(row.reaction_entropy_cal_mol_k),
                    )
                    if row.reaction_enthalpy_kcal_mol is not None
                    and row.reaction_gibbs_free_energy_kcal_mol is not None
                    and row.reaction_entropy_cal_mol_k is not None
                    else None
                ),
                reactants_running_time_seconds=row.reactants_running_time_seconds,
                transition_state_running_time_seconds=row.transition_state_running_time_seconds,
                products_running_time_seconds=row.products_running_time_seconds,
                total_running_time_seconds=row.total_running_time_seconds,
            )
            for row in rows
        ]
        return MappedReactionThermodynamics(
            mapped_reaction_id=mapped_reaction_id,
            profiles=profiles,
        )


__all__ = [
    "ArtifactIngestionQueryService",
    "CalculationProtocolQueryService",
    "CalculationSegmentQueryService",
    "GeometryQueryService",
    "MolecularFormulaDetailQueryService",
    "MolecularTopologyDetailQueryService",
    "ParseRevisionQueryService",
    "ReactionEnergyQueryService",
    "ScientificArrayQueryService",
    "TransitionStateInferenceQueryService",
]
