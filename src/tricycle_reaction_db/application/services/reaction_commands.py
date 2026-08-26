"""NexusX commands for topology-first reaction creation."""

from dataclasses import dataclass
from hashlib import sha256
from typing import cast

from nexusx import UseCaseService, mutation  # type: ignore[import-untyped]
from rdkit import Chem
from rdkit import __version__ as rdkit_version
from rdkit.Chem import rdChemReactions
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import (
    CreateReactionCommand,
    CreateReactionResult,
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    MappedReactionRecord,
)
from tricycle_reaction_db.application.query_cost import enforce_structure_input_budget
from tricycle_reaction_db.application.services._persistence import _require_id
from tricycle_reaction_db.application.services.audit import AuditService
from tricycle_reaction_db.application.services.authentication import current_principal
from tricycle_reaction_db.application.services.authorization import AuthorizationService
from tricycle_reaction_db.application.services.molecular_geometry import (
    GeometryPersistenceContext,
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.reaction_geometry_reconciliation import (
    ReconciliationBatchCache,
    reconcile_mapped_reaction_with_geometries,
    resolve_endpoint_node,
)
from tricycle_reaction_db.application.services.reactions import (
    _canonical_mapped_reaction_smiles,
    _reaction_from_representation,
    persist_logical_reaction,
    persist_logical_reaction_participant,
    persist_mapped_reaction,
    reaction_hash_for_participants,
    validate_logical_reaction,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    MappedReaction,
    MolecularFormula,
    MolecularTopology,
)
from tricycle_reaction_db.db.session import session_factory
from tricycle_reaction_db.domain.enums import (
    LogicalReactionParticipantSide,
    MappedReactionKind,
    ReactionClass,
)
from tricycle_reaction_db.ingestion.normalization import normalize_topology


@dataclass(frozen=True, slots=True)
class _ResolvedComponent:
    side: LogicalReactionParticipantSide
    template_index: int
    template: Chem.Mol
    formula: MolecularFormula
    topology: MolecularTopology


def _resolve_components(
    session: Session,
    definition: rdChemReactions.ChemicalReaction,
    *,
    topology_context: GeometryPersistenceContext | None = None,
    include_creation_metadata: bool = True,
    precomputed_topology_records: tuple[object, ...] | None = None,
) -> tuple[list[_ResolvedComponent], int]:
    components: list[_ResolvedComponent] = []
    topologies_created = 0
    component_index = 0
    for side, templates in (
        (LogicalReactionParticipantSide.REACTANT, definition.GetReactants()),
        (LogicalReactionParticipantSide.PRODUCT, definition.GetProducts()),
    ):
        for template_index, template in enumerate(templates):
            if precomputed_topology_records is not None:
                try:
                    normalized = precomputed_topology_records[component_index]
                except IndexError as error:
                    raise ValueError(
                        "precomputed reaction topology record count mismatch"
                    ) from error
                component_index += 1
            else:
                normalized = normalize_topology(
                    template,
                    add_hydrogens=True,
                    reconstruction_method="rdkit/reaction-representation",
                    reconstruction_version=rdkit_version,
                    reconstruction_metadata={
                        "side": side.value,
                        "template_index": template_index,
                    },
                )
            existing = (
                session.exec(
                    select(MolecularTopology).where(
                        MolecularTopology.identity_schema_version
                        == normalized.topology.identity_schema_version,
                        MolecularTopology.graph_hash == normalized.topology.graph_hash,
                    )
                ).first()
                if include_creation_metadata
                else None
            )
            persisted = persist_molecular_topology(
                session,
                normalized,
                context=topology_context,
            )
            if include_creation_metadata and existing is None:
                topologies_created += 1
            components.append(
                _ResolvedComponent(
                    side=side,
                    template_index=template_index,
                    template=template,
                    formula=persisted.formula,
                    topology=persisted.topology,
                )
            )
    if precomputed_topology_records is not None and component_index != len(
        precomputed_topology_records
    ):
        raise ValueError("precomputed reaction topology record count mismatch")
    return components, topologies_created


def reaction_topology_records(
    definition: rdChemReactions.ChemicalReaction,
) -> list[object]:
    """Build the exact topology records used by reaction component resolution."""

    return [
        normalize_topology(
            template,
            add_hydrogens=True,
            reconstruction_method="rdkit/reaction-representation",
            reconstruction_version=rdkit_version,
            reconstruction_metadata={
                "side": side.value,
                "template_index": template_index,
            },
        )
        for side, templates in (
            (LogicalReactionParticipantSide.REACTANT, definition.GetReactants()),
            (LogicalReactionParticipantSide.PRODUCT, definition.GetProducts()),
        )
        for template_index, template in enumerate(templates)
    ]


def _has_complete_mapping(components: list[_ResolvedComponent]) -> bool:
    map_sets: dict[LogicalReactionParticipantSide, set[int]] = {}
    any_mapping = False
    for side in LogicalReactionParticipantSide:
        side_maps: list[int] = []
        for component in components:
            if component.side is not side:
                continue
            template_maps = [
                atom.GetAtomMapNum()
                for atom in component.template.GetAtoms()  # type: ignore[no-untyped-call]
            ]
            side_maps.extend(template_maps)
            any_mapping = any_mapping or any(template_maps)
            if (
                any(template_maps)
                and component.template.GetNumAtoms() != component.topology.atom_count
            ):
                raise ValueError(
                    "mapped reaction components must explicitly contain every topology atom"
                )
        positive_maps = [number for number in side_maps if number > 0]
        if len(positive_maps) != len(side_maps) and positive_maps:
            raise ValueError("reaction atom mapping must be either absent or complete")
        if len(set(positive_maps)) != len(positive_maps):
            raise ValueError("reaction atom-map numbers must be unique on each side")
        map_sets[side] = set(positive_maps)
    if not any_mapping:
        return False
    if (
        map_sets[LogicalReactionParticipantSide.REACTANT]
        != map_sets[LogicalReactionParticipantSide.PRODUCT]
    ):
        raise ValueError("reactant and product atom-map sets must match")
    return True


def _automatic_reaction_label(
    components: list[_ResolvedComponent],
    reaction_hash: str,
) -> str:
    """Create a stable, compact label without relying on input ordering."""

    sides: dict[LogicalReactionParticipantSide, list[tuple[str, str]]] = {
        LogicalReactionParticipantSide.REACTANT: [],
        LogicalReactionParticipantSide.PRODUCT: [],
    }
    for component in components:
        sides[component.side].append(
            (component.formula.hill_formula, component.topology.graph_hash)
        )

    def format_side(side: LogicalReactionParticipantSide) -> str:
        return " + ".join(formula for formula, _ in sorted(sides[side], key=lambda item: item[0:2]))

    return (
        f"{format_side(LogicalReactionParticipantSide.REACTANT)} -> "
        f"{format_side(LogicalReactionParticipantSide.PRODUCT)} [{reaction_hash[:8]}]"
    )


def _create_reaction(
    session: Session,
    command: CreateReactionCommand,
    *,
    defer_thermodynamic_refresh: bool = False,
    topology_context: GeometryPersistenceContext | None = None,
    include_creation_metadata: bool = True,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    precomputed_topology_records: tuple[object, ...] | None = None,
) -> CreateReactionResult:
    definition = _reaction_from_representation(command.reaction)
    components, topologies_created = _resolve_components(
        session,
        definition,
        topology_context=topology_context,
        include_creation_metadata=include_creation_metadata,
        precomputed_topology_records=precomputed_topology_records,
    )
    mapping_complete = _has_complete_mapping(components)
    identities = [(component.side, component.topology, 1) for component in components]
    reaction_hash = reaction_hash_for_participants(identities)
    automatic_label = _automatic_reaction_label(components, reaction_hash)
    existing_logical = session.exec(
        select(LogicalReaction).where(LogicalReaction.reaction_hash == reaction_hash)
    ).first()
    logical_reaction = persist_logical_reaction(
        session,
        LogicalReactionRecord(
            reaction_key=f"reaction:{reaction_hash}",
            label=command.label or automatic_label,
            reaction_class=command.reaction_class,
            cycloaddition_pattern=command.cycloaddition_pattern,
            reaction_hash=reaction_hash,
        ),
    )
    logical_created = existing_logical is None
    if logical_created:
        for component in components:
            persist_logical_reaction_participant(
                session,
                logical_reaction,
                component.topology,
                LogicalReactionParticipantRecord(
                    side=component.side,
                    participant_index=component.template_index,
                ),
            )
    validate_logical_reaction(logical_reaction)

    topology_ids = [
        _require_id(component.topology, label="MolecularTopology") for component in components
    ]
    if not mapping_complete:
        return CreateReactionResult(
            logical_reaction_id=_require_id(logical_reaction, label="LogicalReaction"),
            mapped_reaction_id=None,
            reactant_node_id=None,
            product_node_id=None,
            reaction_hash=reaction_hash,
            topology_ids=topology_ids,
            topologies_created=topologies_created,
            mapping_complete=False,
            logical_reaction_created=logical_created,
            mapped_reaction_created=False,
        )

    canonical_smiles = _canonical_mapped_reaction_smiles(definition)
    mapping_hash = sha256(canonical_smiles.encode("utf-8")).hexdigest()
    existing_mapped = (
        session.exec(
            select(MappedReaction).where(
                MappedReaction.logical_reaction_id == logical_reaction.id,
                MappedReaction.mapping_hash == mapping_hash,
            )
        ).first()
        if include_creation_metadata
        else None
    )
    mapped_reaction = persist_mapped_reaction(
        session,
        logical_reaction,
        MappedReactionRecord(
            mapped_reaction_key=(command.mapped_reaction_key or f"mapping:{mapping_hash}"),
            label=command.label or f"{automatic_label} [{mapping_hash[:8]}]",
            mapped_reaction_kind=command.mapped_reaction_kind,
            mapped_reaction_smiles=canonical_smiles,
            mapping_hash=mapping_hash,
        ),
    )
    reactant_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.REACTANT,
        cache=reconciliation_cache,
    )
    product_node = resolve_endpoint_node(
        session,
        mapped_reaction,
        LogicalReactionParticipantSide.PRODUCT,
        cache=reconciliation_cache,
    )
    reconcile_mapped_reaction_with_geometries(
        session,
        mapped_reaction,
        refresh_thermodynamics=not defer_thermodynamic_refresh,
        cache=reconciliation_cache,
    )
    return CreateReactionResult(
        logical_reaction_id=_require_id(logical_reaction, label="LogicalReaction"),
        mapped_reaction_id=_require_id(mapped_reaction, label="MappedReaction"),
        reactant_node_id=_require_id(reactant_node, label="MappedReactionNode"),
        product_node_id=_require_id(product_node, label="MappedReactionNode"),
        reaction_hash=reaction_hash,
        topology_ids=topology_ids,
        topologies_created=topologies_created,
        mapping_complete=True,
        logical_reaction_created=logical_created,
        mapped_reaction_created=(existing_mapped is None if include_creation_metadata else False),
    )


class ReactionCommandService(UseCaseService):  # type: ignore[misc]
    """Create reactions from one representation without database identifiers."""

    @mutation  # type: ignore[untyped-decorator]
    async def create_reaction(
        cls,
        reaction: str,
        label: str | None = None,
        reaction_class: ReactionClass = ReactionClass.CYCLOADDITION,
        cycloaddition_pattern: str | None = None,
        mapped_reaction_key: str | None = None,
        mapped_reaction_kind: MappedReactionKind = MappedReactionKind.CURATED,
    ) -> CreateReactionResult:
        """Create or reuse a global reaction as a system curator."""

        principal = current_principal()
        if principal is None:
            raise PermissionError("authenticated system curator is required")
        await AuthorizationService.require_system_curator(principal.user_id)
        enforce_structure_input_budget(
            {"reaction": reaction},
            maximum_characters=get_settings().structure_query_max_characters,
        )
        command = CreateReactionCommand(
            reaction=reaction,
            label=label,
            reaction_class=reaction_class,
            cycloaddition_pattern=cycloaddition_pattern,
            mapped_reaction_key=mapped_reaction_key,
            mapped_reaction_kind=mapped_reaction_kind,
        )
        async with session_factory() as session:
            result = await session.run_sync(
                lambda sync_session: _create_reaction(cast(Session, sync_session), command)
            )
            await session.commit()
        await AuditService.record(
            action="reaction.curated",
            entity_type="logical_reaction",
            entity_id=result.logical_reaction_id,
            actor_user_id=principal.user_id,
            metadata={
                "mapped_reaction_id": (
                    str(result.mapped_reaction_id) if result.mapped_reaction_id else None
                ),
                "logical_reaction_created": result.logical_reaction_created,
                "mapped_reaction_created": result.mapped_reaction_created,
            },
        )
        return result


create_reaction_in_session = _create_reaction


__all__ = ["ReactionCommandService", "create_reaction_in_session", "reaction_topology_records"]
