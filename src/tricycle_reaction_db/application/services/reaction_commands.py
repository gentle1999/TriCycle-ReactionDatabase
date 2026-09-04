"""NexusX commands for topology-first reaction creation."""

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import cast

from nexusx import UseCaseService, mutation  # type: ignore[import-untyped]
from rdkit import __version__ as rdkit_version
from rdkit.Chem import rdChemReactions
from sqlmodel import Session, select

from tricycle_reaction_db.application.dtos import (
    CreateReactionCommand,
    CreateReactionResult,
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    MappedReactionRecord,
    NormalizedTopologyRecord,
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
from tricycle_reaction_db.application.services.reaction_stereo_projection import (
    inversion_labile_atom_map_numbers,
    project_logical_topology,
)
from tricycle_reaction_db.application.services.reactions import (
    _reaction_from_representation,
    mapped_smiles_for_topology,
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
from tricycle_reaction_db.ingestion.normalization import (
    normalize_topology,
    normalize_topology_with_mapping,
)


@dataclass(frozen=True, slots=True)
class _ResolvedComponent:
    side: LogicalReactionParticipantSide
    template_index: int
    formula: MolecularFormula
    topology: MolecularTopology
    topology_atom_map_numbers: list[int]
    logical_topology: MolecularTopology | None = None


def _resolve_components(
    session: Session,
    definition: rdChemReactions.ChemicalReaction | None,
    *,
    topology_context: GeometryPersistenceContext | None = None,
    include_creation_metadata: bool = True,
    precomputed_topology_records: tuple[NormalizedTopologyRecord, ...] | None = None,
) -> tuple[list[_ResolvedComponent], int]:
    components: list[_ResolvedComponent] = []
    topologies_created = 0
    if precomputed_topology_records is not None:
        for normalized in precomputed_topology_records:
            metadata = normalized.topology_derivation.reconstruction_metadata
            try:
                side = LogicalReactionParticipantSide(str(metadata["side"]))
                template_index = int(metadata["template_index"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "precomputed reaction topology requires side and template_index metadata"
                ) from error
            topology_atom_maps = metadata.get("topology_atom_map_numbers")
            if not isinstance(topology_atom_maps, list):
                raise ValueError(
                    "precomputed mapped reaction topology requires topology atom-map numbers"
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
                    formula=persisted.formula,
                    topology=persisted.topology,
                    topology_atom_map_numbers=[int(number) for number in topology_atom_maps],
                )
            )
        component_keys = [(component.side, component.template_index) for component in components]
        if len(set(component_keys)) != len(component_keys):
            raise ValueError("precomputed reaction topology component keys must be unique")
        return components, topologies_created

    if definition is None:
        raise ValueError("reaction definition is required without precomputed topologies")
    for side, templates in (
        (LogicalReactionParticipantSide.REACTANT, definition.GetReactants()),
        (LogicalReactionParticipantSide.PRODUCT, definition.GetProducts()),
    ):
        for template_index, template in enumerate(templates):
            normalized, source_to_topology = normalize_topology_with_mapping(
                template,
                add_hydrogens=True,
                reconstruction_method="rdkit/reaction-representation",
                reconstruction_version=rdkit_version,
                reconstruction_metadata={
                    "side": side.value,
                    "template_index": template_index,
                },
            )
            template_atom_maps = [atom.GetAtomMapNum() for atom in template.GetAtoms()]
            if any(template_atom_maps):
                topology_atom_map_numbers = [0] * normalized.topology.atom_count
                for source_index, topology_index in enumerate(source_to_topology):
                    if source_index >= len(template_atom_maps):
                        break
                    topology_atom_map_numbers[topology_index] = template_atom_maps[source_index]
            else:
                topology_atom_map_numbers = []
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
                    formula=persisted.formula,
                    topology=persisted.topology,
                    topology_atom_map_numbers=topology_atom_map_numbers,
                )
            )
    return components, topologies_created


def reaction_topology_records(
    definition: rdChemReactions.ChemicalReaction,
) -> list[NormalizedTopologyRecord]:
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
            template_maps = component.topology_atom_map_numbers
            side_maps.extend(template_maps)
            any_mapping = any_mapping or any(template_maps)
            if any(template_maps) and len(template_maps) != component.topology.atom_count:
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


def _logicalize_components(
    session: Session,
    components: list[_ResolvedComponent],
    *,
    topology_context: GeometryPersistenceContext | None = None,
) -> list[_ResolvedComponent]:
    """Project inversion-labile stereo symmetrically across both endpoints.

    Reactant/product labels are a storage convention, not a direction for the
    inversion rule.  A mapped atom may satisfy an inversion rule on either
    endpoint (normally the N is ``sp3`` on one side and ``sp2`` on the other),
    so collect rule evidence from the complete mapped reaction first.  The
    resulting atom-map set is then applied to every endpoint, allowing the
    opposite endpoint to remove the stereo feature that depends on that atom.
    """

    complete_mapping = _has_complete_mapping(components)
    if not complete_mapping:
        return [replace(component, logical_topology=component.topology) for component in components]

    labile_atom_maps: set[int] = set()
    reaction_rule_ids: set[str] = set()
    for component in components:
        rule_matches = inversion_labile_atom_map_numbers(
            component.topology,
            component.topology_atom_map_numbers,
        )
        for rule_id, atom_map in rule_matches:
            reaction_rule_ids.add(rule_id)
            labile_atom_maps.add(atom_map)

    logical_components: list[_ResolvedComponent] = []
    for component in components:
        logical_topology = project_logical_topology(
            session,
            component.topology,
            component.topology_atom_map_numbers,
            labile_atom_maps,
            context=topology_context,
            rule_ids=reaction_rule_ids,
        )
        logical_components.append(replace(component, logical_topology=logical_topology))
    return logical_components


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
        topology = component.logical_topology or component.topology
        sides[component.side].append((component.formula.hill_formula, topology.graph_hash))

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
    defer_geometry_reconciliation: bool = False,
    topology_context: GeometryPersistenceContext | None = None,
    include_creation_metadata: bool = True,
    reconciliation_cache: ReconciliationBatchCache | None = None,
    precomputed_topology_records: tuple[NormalizedTopologyRecord, ...] | None = None,
) -> CreateReactionResult:
    definition = (
        None
        if precomputed_topology_records is not None
        else _reaction_from_representation(command.reaction)
    )
    components, topologies_created = _resolve_components(
        session,
        definition,
        topology_context=topology_context,
        include_creation_metadata=include_creation_metadata,
        precomputed_topology_records=precomputed_topology_records,
    )
    mapping_complete = _has_complete_mapping(components)
    logical_components = _logicalize_components(
        session,
        components,
        topology_context=topology_context,
    )
    identities = [
        (component.side, component.logical_topology or component.topology, 1)
        for component in logical_components
    ]
    reaction_hash = reaction_hash_for_participants(identities)
    automatic_label = _automatic_reaction_label(logical_components, reaction_hash)
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
    for component in logical_components:
        persist_logical_reaction_participant(
            session,
            logical_reaction,
            component.logical_topology or component.topology,
            LogicalReactionParticipantRecord(
                side=component.side,
                participant_index=component.template_index,
            ),
            candidate_topologies=(component.topology,),
        )
    validate_logical_reaction(logical_reaction)

    topology_ids = [
        _require_id(component.logical_topology or component.topology, label="MolecularTopology")
        for component in logical_components
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

    precomputed_mapped_smiles_by_template = {
        (component.side, component.template_index): mapped_smiles_for_topology(
            component.topology,
            component.topology_atom_map_numbers,
        )
        for component in components
    }
    canonical_sides: dict[LogicalReactionParticipantSide, list[tuple[int, str]]] = {
        LogicalReactionParticipantSide.REACTANT: [],
        LogicalReactionParticipantSide.PRODUCT: [],
    }
    for component_key, mapped_smiles in precomputed_mapped_smiles_by_template.items():
        side, template_index = component_key
        canonical_sides[side].append((template_index, mapped_smiles))
    reactants = ".".join(
        smiles for _, smiles in sorted(canonical_sides[LogicalReactionParticipantSide.REACTANT])
    )
    products = ".".join(
        smiles for _, smiles in sorted(canonical_sides[LogicalReactionParticipantSide.PRODUCT])
    )
    canonical_smiles = f"{reactants}>>{products}"
    mapping_hash = sha256(canonical_smiles.encode("utf-8")).hexdigest()
    source_atom_maps_by_template = {
        (component.side, component.template_index): component.topology_atom_map_numbers
        for component in components
    }
    topology_ids_by_template = {
        (component.side, component.template_index): _require_id(
            component.logical_topology or component.topology,
            label="MolecularTopology",
        )
        for component in logical_components
    }
    concrete_topology_ids_by_template = {
        (component.side, component.template_index): _require_id(
            component.topology,
            label="MolecularTopology",
        )
        for component in components
    }
    existing_mapped = (
        session.exec(
            select(MappedReaction).where(
                MappedReaction.logical_reaction_id
                == _require_id(logical_reaction, label="LogicalReaction"),
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
        source_atom_maps_by_template=source_atom_maps_by_template,
        topology_ids_by_template=topology_ids_by_template,
        concrete_topology_ids_by_template=concrete_topology_ids_by_template,
        precomputed_mapped_smiles_by_template=precomputed_mapped_smiles_by_template,
    )
    # A logical reaction may have been created after other concrete topology
    # rows were already persisted.  Expand those existing DAG members now
    # whenever the caller is not deferring the batch barrier.  The deferred
    # path performs the same reaction-level pass after all pending rows are
    # flushed, because fast insertion deliberately hides them from SQL reads.
    if not defer_geometry_reconciliation:
        from tricycle_reaction_db.application.services.reaction_mapping_resolution import (
            ensure_mapped_reactions_for_logical_reaction,
        )

        ensure_mapped_reactions_for_logical_reaction(
            session,
            logical_reaction,
            reconciliation_cache=reconciliation_cache,
            refresh_thermodynamics=not defer_thermodynamic_refresh,
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
    # Batch ingestion persists Geometry rows before the inferred reaction
    # participants.  Defer this candidate scan until the batch barrier so the
    # query can see every participant and run once over the complete Geometry
    # context.  The endpoint nodes above are still created immediately.
    if defer_geometry_reconciliation:
        if topology_context is None:
            raise ValueError("deferred Geometry reconciliation requires a topology context")
        mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
        topology_context.mapped_reactions_to_reconcile[mapped_reaction_id] = mapped_reaction
    else:
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
        mapped_reaction_created=(
            existing_mapped is None and mapped_reaction.mapping_hash == mapping_hash
            if include_creation_metadata
            else False
        ),
    )


class ReactionCommandService(UseCaseService):  # type: ignore[misc]
    """Create reactions from one representation without database identifiers."""

    @mutation  # type: ignore[untyped-decorator]
    async def create_reaction(
        cls,
        reaction: str,
        label: str | None = None,
        reaction_class: ReactionClass | None = None,
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
