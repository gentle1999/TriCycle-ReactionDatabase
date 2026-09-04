"""Relationship-driven persistence for manifest-declared reaction paths."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from uuid import UUID

from rdkit import Chem
from rdkit.Chem import rdChemReactions
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.dtos.reactions import (
    LogicalReactionParticipantRecord,
    LogicalReactionRecord,
    ManifestArtifactBindingRecord,
    MappedReactionEdgeRecord,
    MappedReactionNodeGeometryMappingRecord,
    MappedReactionNodeGeometryRecord,
    MappedReactionNodeRecord,
    MappedReactionRecord,
    WorkflowManifestRecord,
)
from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _assert_record_matches,
    _attach_pending_entities,
    _flush_new_entity,
    _new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.reaction_geometry_policy import (
    require_geometry_reaction_endpoint_eligibility,
    require_geometry_thermodynamic_property,
)
from tricycle_reaction_db.db.models import (
    ArtifactFile,
    Geometry,
    LogicalReaction,
    LogicalReactionParticipant,
    ManifestArtifactBinding,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionNodeGeometry,
    MappedReactionNodeGeometryMapping,
    MappedReactionParticipant,
    MolecularTopology,
    WorkflowManifest,
)
from tricycle_reaction_db.domain.enums import (
    ArtifactKind,
    ArtifactResolutionStatus,
    LogicalReactionParticipantSide,
    MappedReactionNodeRole,
    StereoStatus,
    StorageStatus,
    WorkflowManifestStatus,
)
from tricycle_reaction_db.ingestion.normalization import (
    ensure_serializable_double_bond_stereochemistry,
)

ParticipantIdentity = tuple[LogicalReactionParticipantSide, MolecularTopology, int]
MappedReactionConcreteIdentity = tuple[
    tuple[str, int, UUID, tuple[int, ...]],
    ...,
]

_ENDPOINT_NODE_KEY_ALIASES: dict[MappedReactionNodeRole, tuple[str, ...]] = {
    MappedReactionNodeRole.REACTANT: ("reactants", "reactant"),
    MappedReactionNodeRole.PRODUCT: ("products", "product"),
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


@lru_cache(maxsize=1024)
def _reaction_from_representation(
    reaction_representation: str,
) -> rdChemReactions.ChemicalReaction:
    try:
        if reaction_representation.lstrip().startswith("$RXN"):
            reaction = rdChemReactions.ReactionFromRxnBlock(
                reaction_representation,
                sanitize=True,
                removeHs=False,
                strictParsing=True,
            )
        else:
            reaction = rdChemReactions.ReactionFromSmarts(
                reaction_representation,
                useSmiles=True,
            )
    except (RuntimeError, ValueError) as exc:
        raise ValueError("RDKit could not parse reaction representation") from exc
    if reaction is None:
        raise ValueError("RDKit could not parse reaction representation")
    _, error_count = reaction.Validate(silent=True)
    if error_count:
        raise ValueError("RDKit rejected reaction representation")
    if reaction.GetNumReactantTemplates() == 0 or reaction.GetNumProductTemplates() == 0:
        raise ValueError("reaction representation requires reactant and product templates")
    return reaction


def _canonical_mapped_reaction_smiles(
    definition: rdChemReactions.ChemicalReaction,
) -> str:
    """Canonicalize mapped reactions while avoiding unstable metal stereo tags."""

    all_templates = (
        definition.GetReactants(),
        definition.GetAgents(),
        definition.GetProducts(),
    )
    # RDKit's ChemicalReaction template copy can segfault for some
    # multicoordinate metal graphs. Individual template serialization is both
    # deterministic and sufficient for the mapped reaction identity there.
    if any(
        _is_metal_atomic_number(atom.GetAtomicNum())
        for templates in all_templates
        for template in templates
        for atom in template.GetAtoms()
    ):
        serialized_sides = []
        for templates in all_templates:
            serialized_templates = []
            for template in templates:
                template_maps = [atom.GetAtomMapNum() for atom in template.GetAtoms()]
                normalized = ensure_serializable_double_bond_stereochemistry(
                    template,
                    preserve_atom_maps=bool(
                        template_maps
                        and all(number > 0 for number in template_maps)
                        and len(set(template_maps)) == len(template_maps)
                    ),
                )
                # RDKit's ChemicalReaction template copy can segfault for some
                # multicoordinate metal graphs.  Preserve every supported
                # non-metal/ bond stereo annotation, while retaining the
                # existing policy of omitting unstable metal-center tags from
                # the reaction-level identity.
                for atom in normalized.GetAtoms():  # type: ignore[no-untyped-call]
                    if _is_metal_atomic_number(atom.GetAtomicNum()):
                        atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
                serialized_templates.append(
                    Chem.MolToSmiles(
                        normalized,
                        canonical=True,
                        isomericSmiles=True,
                        allHsExplicit=True,
                    )
                )
            serialized_sides.append(".".join(sorted(serialized_templates)))
        reactants, agents, products = serialized_sides
        return f"{reactants}>{agents}>{products}" if agents else f"{reactants}>>{products}"

    stable = rdChemReactions.ChemicalReaction()
    for templates, add_template in (
        (definition.GetReactants(), stable.AddReactantTemplate),
        (definition.GetAgents(), stable.AddAgentTemplate),
        (definition.GetProducts(), stable.AddProductTemplate),
    ):
        for template in templates:
            normalized = Chem.Mol(template)
            template_maps = [
                atom.GetAtomMapNum()
                for atom in normalized.GetAtoms()  # type: ignore[no-untyped-call]
            ]
            normalized = ensure_serializable_double_bond_stereochemistry(
                normalized,
                preserve_atom_maps=bool(
                    template_maps
                    and all(number > 0 for number in template_maps)
                    and len(set(template_maps)) == len(template_maps)
                ),
            )
            # RDKit's unsupported metal stereo annotations are not stable across
            # a parse/serialize round trip. Supported non-metal stereo stays.
            for atom in normalized.GetAtoms():  # type: ignore[no-untyped-call]
                if _is_metal_atomic_number(atom.GetAtomicNum()):
                    atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
            add_template(normalized)
    return rdChemReactions.ReactionToSmiles(stable, True)


_METAL_ATOMIC_NUMBERS = frozenset(
    {
        3,
        4,
        11,
        12,
        13,
        19,
        20,
        *range(21, 32),
        *range(37, 51),
        *range(55, 85),
        *range(87, 117),
    }
)


def _is_metal_atomic_number(atomic_number: int) -> bool:
    """Return whether RDKit's atomic number denotes a chemical metal."""

    return atomic_number in _METAL_ATOMIC_NUMBERS


def _reaction_graph_smiles(definition: rdChemReactions.ChemicalReaction) -> str:
    """Render a reaction identity without unstable metal stereochemistry."""

    sides: list[str] = []
    for templates in (
        definition.GetReactants(),
        definition.GetAgents(),
        definition.GetProducts(),
    ):
        sides.append(
            ".".join(
                sorted(
                    Chem.MolToSmiles(
                        template,
                        canonical=True,
                        isomericSmiles=False,
                        allHsExplicit=True,
                    )
                    for template in templates
                )
            )
        )
    return ">".join(sides)


_mapped_reaction_from_smiles = _reaction_from_representation


def _logical_map_numbers_for_reaction(mapped_reaction: MappedReaction) -> frozenset[int]:
    """Read map numbers from persisted participants without reparsing the reaction.

    MolGR-derived reactions may contain multicoordinate metals that are valid in
    the trusted source graph but unsafe for RDKit ``ChemicalReaction`` traversal.
    Participant rows are already the authoritative mapping projection, so a
    second reaction parse here is both unnecessary and crash-prone.
    """

    return frozenset(
        atom_map
        for participant in mapped_reaction.participants
        for atom_map in participant.atom_map_numbers
        if atom_map > 0
    )


def atom_maps_from_source_order(
    geometry: Geometry,
    source_atom_map_numbers: Iterable[int],
    source_to_geometry_atom_indices: Iterable[int] | None = None,
) -> list[int]:
    """Convert one frame's source atom maps into Geometry/Topology order."""

    source_maps = list(source_atom_map_numbers)
    if len(source_maps) != geometry.atom_count:
        raise ValueError("source atom-map count must match Geometry.atom_count")
    if any(number <= 0 for number in source_maps) or len(set(source_maps)) != len(source_maps):
        raise ValueError("source atom-map numbers must be unique positive integers")
    source_to_geometry = (
        list(source_to_geometry_atom_indices)
        if source_to_geometry_atom_indices is not None
        else list(range(geometry.atom_count))
    )
    if sorted(source_to_geometry) != list(range(geometry.atom_count)):
        raise ValueError("source-to-Geometry atom indices must be a full permutation")

    topology_maps = [0] * geometry.atom_count
    for source_index, geometry_index in enumerate(source_to_geometry):
        topology_maps[geometry_index] = source_maps[source_index]
    return topology_maps


def mapped_smiles_for_topology(
    topology: MolecularTopology,
    atom_map_numbers: Iterable[int],
) -> str:
    """Render the exact explicit-H topology with business atom-map numbers."""

    atom_maps = list(atom_map_numbers)
    if len(atom_maps) != topology.atom_count:
        raise ValueError("atom-map count must match MolecularTopology.atom_count")
    if any(number <= 0 for number in atom_maps) or len(set(atom_maps)) != len(atom_maps):
        raise ValueError("atom-map numbers must be unique positive integers")

    mapped = Chem.Mol(topology.mol)
    for atom, map_number in zip(mapped.GetAtoms(), atom_maps, strict=True):  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(map_number)
    # PostgreSQL/RDKit preserves the BondStereo assignment but may drop the
    # neighboring BondDir flags required by the SMILES writer. Recreate those
    # flags on the mapped clone only when stereo is actually assigned. Unknown,
    # unassigned, conflicting, and ambiguous stereo must be connectivity-only;
    # otherwise stale direction flags can turn an unassigned terminal alkene
    # into an arbitrary E/Z reaction component.
    stereo_status = getattr(topology, "stereo_status", None)
    # ``None`` is retained for lightweight/legacy topology objects that do
    # not expose the status field; their graph itself is the only available
    # stereo evidence. Persisted MolecularTopology rows always carry an
    # explicit status and therefore require ASSIGNED before isomeric output.
    isomeric_smiles = stereo_status is None or stereo_status is StereoStatus.ASSIGNED
    if isomeric_smiles:
        mapped = ensure_serializable_double_bond_stereochemistry(
            mapped,
            preserve_atom_maps=True,
        )
    return Chem.MolToSmiles(
        mapped,
        canonical=True,
        isomericSmiles=isomeric_smiles,
        allHsExplicit=True,
    )


def _reaction_mapping_isomorphic(
    *,
    expected_atom_map_numbers: Iterable[int],
    expected_mapped_smiles: str,
    observed_atom_map_numbers: Iterable[int],
    observed_mapped_smiles: str,
) -> bool:
    """Check reaction-map equivalence without requiring one topology atom order.

    A symmetric molecular graph can admit several valid assignments of reaction
    map numbers to its canonical topology order.  The mapped canonical SMILES
    preserves the map-labelled molecular graph, whereas comparing two arrays
    position-by-position would turn such an automorphism into a false conflict.
    """

    return (
        set(expected_atom_map_numbers) == set(observed_atom_map_numbers)
        and expected_mapped_smiles == observed_mapped_smiles
    )


def reaction_hash_for_participants(participants: Iterable[ParticipantIdentity]) -> str:
    """Build a map- and ordering-independent net-reaction identity hash."""

    sides: dict[str, list[tuple[str, int, int]]] = {
        LogicalReactionParticipantSide.REACTANT.value: [],
        LogicalReactionParticipantSide.PRODUCT.value: [],
    }
    for side, topology, coefficient in participants:
        if coefficient <= 0:
            raise ValueError("participant coefficients must be positive")
        sides[side.value].append((topology.graph_hash, topology.formal_charge, coefficient))
    if not all(sides.values()):
        raise ValueError("reaction identity requires both reactant and product participants")
    for entries in sides.values():
        entries.sort()
    payload = {"schema_version": "reaction-identity-v1", "participants": sides}
    return sha256(_canonical_json(payload)).hexdigest()


def _ensure_manifest_mutable(manifest: WorkflowManifest) -> None:
    if manifest.status not in {
        WorkflowManifestStatus.RECEIVED,
        WorkflowManifestStatus.VALIDATED,
    }:
        raise ValueError(f"cannot change a {manifest.status.value} manifest aggregate")


def _ensure_reaction_mutable(reaction: LogicalReaction) -> None:
    # Logical reactions are global aggregates. A manifest is optional provenance,
    # not the owner of later mappings, nodes, geometries, or calculations.
    return None


def _mapping_assignment_for_topology(
    template: Chem.Mol,
    topology: MolecularTopology,
    *,
    source_atom_map_numbers: Iterable[int] | None = None,
) -> tuple[list[int], str]:
    """Transfer reaction maps to a source-order topology.

    MolOP TS endpoints and their MolGR-derived participant topologies retain
    the calculation-frame atom order.  The map labels in the reaction
    template therefore already describe that same source sequence.  A
    reaction may change bonds, charges, or radical annotations between sides;
    none of those changes is a reason to compare the endpoint graph with the
    template graph here.  This function only validates the map/count contract
    and renders the topology with its source-order map labels.
    """

    template_maps = [
        atom.GetAtomMapNum()
        for atom in template.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    if not template_maps or any(number <= 0 for number in template_maps):
        raise ValueError("every mapped reaction template atom must have an atom map")
    if len(set(template_maps)) != len(template_maps):
        raise ValueError("mapped reaction template atom maps must be unique")
    if len(template_maps) != topology.atom_count:
        raise ValueError("mapped reaction template atom count does not match its Topology")

    # MolOP endpoint fragments carry the original calculation-frame map number
    # for each atom.  Their map sequence is supplied by the caller because an
    # RDKit reaction template may reorder atoms while serializing a component.
    # A caller without that source evidence retains the template's direct atom
    # sequence, which is the correct contract for explicitly supplied paths.
    atom_maps = (
        list(source_atom_map_numbers) if source_atom_map_numbers is not None else template_maps
    )
    if len(atom_maps) != topology.atom_count:
        raise ValueError("source atom-map count must match MolecularTopology.atom_count")
    if any(number <= 0 for number in atom_maps) or len(set(atom_maps)) != len(atom_maps):
        raise ValueError("source atom-map numbers must be unique positive integers")
    if set(atom_maps) != set(template_maps):
        raise ValueError("source atom-map numbers do not match the reaction template")
    return atom_maps, mapped_smiles_for_topology(topology, atom_maps)


class MappingTransferAmbiguityError(ValueError):
    """Several symmetry-equivalent graph matches produce different mappings."""

    def __init__(
        self,
        *,
        logical_participant_id: UUID,
        source_topology_id: UUID,
        target_topology_id: UUID,
        candidate_atom_maps: Iterable[Iterable[int]],
    ) -> None:
        self.logical_participant_id = logical_participant_id
        self.source_topology_id = source_topology_id
        self.target_topology_id = target_topology_id
        self.candidate_atom_maps = tuple(
            tuple(int(number) for number in maps) for maps in candidate_atom_maps
        )
        super().__init__(
            "abstract topology mapping is ambiguous for logical participant "
            f"{logical_participant_id}: source={source_topology_id}, target={target_topology_id}, "
            f"candidates={len(self.candidate_atom_maps)}"
        )

    def evidence(self) -> dict[str, object]:
        return {
            "error_code": "reaction_mapping_transfer_ambiguous",
            "logical_participant_id": str(self.logical_participant_id),
            "source_topology_id": str(self.source_topology_id),
            "target_topology_id": str(self.target_topology_id),
            "candidate_atom_maps": [list(maps) for maps in self.candidate_atom_maps],
        }


@dataclass(frozen=True, slots=True)
class TransferredMappedReaction:
    """A strict mapped-reaction projection derived through logical topology."""

    mapped_reaction_smiles: str
    mapping_hash: str
    atom_maps_by_template: dict[tuple[LogicalReactionParticipantSide, int], tuple[int, ...]]
    mapped_smiles_by_template: dict[tuple[LogicalReactionParticipantSide, int], str]
    concrete_topologies_by_template: dict[
        tuple[LogicalReactionParticipantSide, int], MolecularTopology
    ]


def _resolve_topology_value(
    session: Session,
    value: object,
) -> MolecularTopology:
    if isinstance(value, MolecularTopology):
        _require_id(value, label="MolecularTopology")
        return value
    topology = session.get(MolecularTopology, value)
    if topology is None:
        topology = next(
            (
                entity
                for entity in (
                    *tuple(session.new),
                    *tuple(session.info.get("_fast_pending_entities", ())),
                )
                if isinstance(entity, MolecularTopology) and entity.id == value
            ),
            None,
        )
    if topology is None:
        raise ValueError(f"MolecularTopology {value!r} does not exist")
    return topology


def _canonical_atom_maps_for_topology(
    session: Session,
    topology: MolecularTopology,
    atom_maps: tuple[int, ...],
) -> tuple[int, ...]:
    """Canonicalize atom maps modulo connectivity-preserving automorphisms.

    Mapping transfer can encounter a symmetric endpoint through different
    source atom orders.  Those assignments can serialize to different mapped
    SMILES even though they describe the same concrete component.  Stereo is
    intentionally ignored while finding the automorphisms: the concrete
    topology id already carries the strict stereo identity, while the
    automorphism step only removes representation-level symmetry from the
    atom-map assignment.
    """

    topology_id = _require_id(topology, label="MolecularTopology")
    if len(atom_maps) != topology.atom_count:
        return atom_maps
    cache: dict[tuple[UUID, tuple[int, ...]], tuple[int, ...]] = session.info.setdefault(
        "_mapped_reaction_canonical_atom_maps", {}
    )
    cache_key = (topology_id, atom_maps)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    molecule = Chem.Mol(topology.mol)
    for atom in molecule.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    automorphisms = molecule.GetSubstructMatches(
        molecule,
        uniquify=False,
        useChirality=False,
    )
    candidates = tuple(tuple(atom_maps[index] for index in match) for match in automorphisms)
    canonical = min(candidates) if candidates else atom_maps
    cache[cache_key] = canonical
    return canonical


def mapped_reaction_concrete_identity(
    session: Session,
    mapped_reaction: MappedReaction,
    *,
    participants: Iterable[MappedReactionParticipant] | None = None,
) -> MappedReactionConcreteIdentity | None:
    """Return the concrete identity of one complete mapped reaction.

    ``mapping_hash`` is intentionally a strict text identity.  It is not a
    suitable identity for a physical mapped reaction, because equivalent
    atom-mapped projections can differ only in the SMILES traversal (for
    example, a symmetric alkene may be rendered with the opposite slash
    direction).  The durable identity used for materialization is therefore
    the concrete topology of every participant together with the atom-map
    assignment on that topology.

    The atom-map tuple is included deliberately: two different atom mappings
    of the same concrete components can represent different reaction paths and
    must remain separate.  The tuple is returned in template order-independent
    form so it is stable for a logical reaction whose participants were loaded
    in a different order.
    """

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    if participants is None:
        rows = tuple(
            session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == mapped_reaction_id
                )
            ).all()
        )
        rows += tuple(
            entity
            for entity in (
                *tuple(session.new),
                *tuple(session.info.get("_fast_pending_entities", ())),
            )
            if isinstance(entity, MappedReactionParticipant)
            and entity.mapped_reaction_id == mapped_reaction_id
        )
    else:
        rows = tuple(participants)

    identities: list[tuple[str, int, UUID, tuple[int, ...]]] = []
    template_keys: set[tuple[str, int]] = set()
    for participant in rows:
        logical_participant = participant.logical_reaction_participant
        if logical_participant is None:
            logical_participant = session.get(
                LogicalReactionParticipant,
                participant.logical_reaction_participant_id,
            )
        if logical_participant is None:
            return None
        concrete_topology_id = participant.concrete_topology_id or logical_participant.topology_id
        if concrete_topology_id is None:
            return None
        atom_maps = tuple(int(number) for number in participant.atom_map_numbers)
        if not atom_maps or any(number <= 0 for number in atom_maps):
            return None
        if len(set(atom_maps)) != len(atom_maps):
            return None
        concrete_topology = _resolve_topology_value(session, concrete_topology_id)
        atom_maps = _canonical_atom_maps_for_topology(session, concrete_topology, atom_maps)
        template_key = (participant.side.value, participant.template_index)
        if template_key in template_keys:
            return None
        template_keys.add(template_key)
        identities.append(
            (
                participant.side.value,
                participant.template_index,
                concrete_topology_id,
                atom_maps,
            )
        )
    if not identities:
        return None
    return tuple(sorted(identities))


def mapped_reaction_concrete_identity_for_templates(
    session: Session,
    reaction: LogicalReaction,
    *,
    atom_maps_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], Iterable[int]],
    concrete_topology_ids_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], object],
) -> MappedReactionConcreteIdentity:
    """Build the concrete identity before a mapped reaction is persisted."""

    participants_by_key = {
        (participant.side, participant.participant_index): participant
        for participant in reaction.participants
    }
    expected_keys = set(participants_by_key)
    if expected_keys != set(atom_maps_by_template) or expected_keys != set(
        concrete_topology_ids_by_template
    ):
        raise ValueError("concrete identity inputs must cover every logical participant")

    identities: list[tuple[str, int, UUID, tuple[int, ...]]] = []
    for side, template_index in sorted(
        expected_keys,
        key=lambda item: (item[0].value, item[1]),
    ):
        concrete_topology = _resolve_topology_value(
            session,
            concrete_topology_ids_by_template[(side, template_index)],
        )
        atom_maps = tuple(int(number) for number in atom_maps_by_template[(side, template_index)])
        if not atom_maps or any(number <= 0 for number in atom_maps):
            raise ValueError("concrete identity atom maps must be positive")
        if len(set(atom_maps)) != len(atom_maps):
            raise ValueError("concrete identity atom maps must be unique")
        atom_maps = _canonical_atom_maps_for_topology(session, concrete_topology, atom_maps)
        identities.append(
            (
                side.value,
                template_index,
                _require_id(concrete_topology, label="MolecularTopology"),
                atom_maps,
            )
        )
    return tuple(identities)


def find_mapped_reaction_by_concrete_identity(
    session: Session,
    logical_reaction_id: UUID,
    identity: MappedReactionConcreteIdentity,
    *,
    refresh: bool = False,
) -> MappedReaction | None:
    """Find a persisted or same-batch mapped reaction with one identity."""

    index_by_reaction: dict[
        UUID,
        dict[MappedReactionConcreteIdentity, MappedReaction],
    ] = session.info.setdefault("_mapped_reaction_concrete_identity_index", {})
    if refresh:
        index_by_reaction.pop(logical_reaction_id, None)
    index = index_by_reaction.get(logical_reaction_id)
    if index is None:
        candidates = tuple(
            session.exec(
                select(MappedReaction).where(
                    MappedReaction.logical_reaction_id == logical_reaction_id
                )
            ).all()
        )
        candidates += tuple(
            entity
            for entity in (
                *tuple(session.new),
                *tuple(session.info.get("_fast_pending_entities", ())),
            )
            if isinstance(entity, MappedReaction)
            and entity.logical_reaction_id == logical_reaction_id
        )
        by_id = {
            _require_id(candidate, label="MappedReaction"): candidate
            for candidate in candidates
            if isinstance(candidate.id, UUID)
        }
        index = {}
        for candidate in sorted(
            by_id.values(),
            key=lambda item: (item.mapping_hash, str(_require_id(item, label="MappedReaction"))),
        ):
            candidate_identity = mapped_reaction_concrete_identity(session, candidate)
            if candidate_identity is not None:
                index.setdefault(candidate_identity, candidate)
        index_by_reaction[logical_reaction_id] = index
    return index.get(identity)


def _register_mapped_reaction_concrete_identity(
    session: Session,
    mapped_reaction: MappedReaction,
) -> None:
    """Add a newly persisted mapping to the session-local identity index."""

    logical_reaction_id = mapped_reaction.logical_reaction_id
    index_by_reaction: dict[
        UUID,
        dict[MappedReactionConcreteIdentity, MappedReaction],
    ] = session.info.setdefault("_mapped_reaction_concrete_identity_index", {})
    index = index_by_reaction.setdefault(logical_reaction_id, {})
    identity = mapped_reaction_concrete_identity(session, mapped_reaction)
    if identity is not None:
        index.setdefault(identity, mapped_reaction)


def _transferred_atom_maps(
    *,
    logical_participant: LogicalReactionParticipant,
    source_topology: MolecularTopology,
    source_atom_maps: Iterable[int],
    target_topology: MolecularTopology,
) -> list[int]:
    """Compose source-map → abstract-index → target-index correspondences."""

    source_maps = list(source_atom_maps)
    if len(source_maps) != source_topology.atom_count:
        raise ValueError("source mapped participant does not cover its concrete topology")
    if any(number <= 0 for number in source_maps) or len(set(source_maps)) != len(source_maps):
        raise ValueError("source mapped participant atom maps must be unique and positive")
    if source_topology.formula_id != target_topology.formula_id:
        raise ValueError("source and target concrete topologies use different formulas")
    if source_topology.atom_count != target_topology.atom_count:
        raise ValueError("source and target concrete topologies use different atom counts")

    from tricycle_reaction_db.application.services.topology_abstraction import (
        find_topology_matches,
    )

    abstract_topology = logical_participant.topology
    source_matches = find_topology_matches(source_topology.mol, abstract_topology.mol)
    target_matches = find_topology_matches(target_topology.mol, abstract_topology.mol)
    if not source_matches or not target_matches:
        raise ValueError(
            "source or target concrete topology is not a stereo-aware match for its "
            "logical topology"
        )
    candidate_maps: set[tuple[int, ...]] = set()
    for source_match in source_matches:
        abstract_to_source_map = {
            abstract_index: source_maps[source_index]
            for abstract_index, source_index in enumerate(source_match)
        }
        for target_match in target_matches:
            target_maps = [0] * target_topology.atom_count
            for abstract_index, target_index in enumerate(target_match):
                target_maps[target_index] = abstract_to_source_map[abstract_index]
            if any(number <= 0 for number in target_maps) or len(set(target_maps)) != len(
                target_maps
            ):
                raise ValueError("abstract mapping transfer did not cover target topology")
            candidate_maps.add(tuple(target_maps))
    if not candidate_maps:
        raise ValueError("abstract mapping transfer produced no complete mapping")
    if len(candidate_maps) > 1:
        raise MappingTransferAmbiguityError(
            logical_participant_id=_require_id(
                logical_participant,
                label="LogicalReactionParticipant",
            ),
            source_topology_id=_require_id(source_topology, label="MolecularTopology"),
            target_topology_id=_require_id(target_topology, label="MolecularTopology"),
            candidate_atom_maps=sorted(candidate_maps),
        )
    return list(next(iter(candidate_maps)))


def transfer_mapped_reaction_to_concrete_topologies(
    session: Session,
    mapped_reaction: MappedReaction,
    concrete_topologies_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], object],
) -> TransferredMappedReaction:
    """Transfer one complete mapping through each logical participant topology.

    The existing mapped reaction is the only source of atom-map labels.  Both
    the source and target concrete topologies are matched to the same logical
    graph, and the two graph correspondences are composed.  Isomeric SMILES is
    rendered only after this graph operation; it is never used to infer the
    atom mapping.
    """

    _require_id(mapped_reaction, label="MappedReaction")
    source_participants = tuple(mapped_reaction.participants)
    if not source_participants:
        persisted_participants = tuple(
            session.exec(
                select(MappedReactionParticipant).where(
                    MappedReactionParticipant.mapped_reaction_id == mapped_reaction.id
                )
            ).all()
        )
        pending_participants = tuple(
            entity
            for entity in (
                *tuple(session.new),
                *tuple(session.info.get("_fast_pending_entities", ())),
            )
            if isinstance(entity, MappedReactionParticipant)
            and entity.mapped_reaction_id == mapped_reaction.id
        )
        participants_by_id = {
            _require_id(participant, label="MappedReactionParticipant"): participant
            for participant in (*persisted_participants, *pending_participants)
            if isinstance(participant.id, UUID)
        }
        source_participants = tuple(participants_by_id.values())
    expected_keys = {
        (participant.side, participant.template_index) for participant in source_participants
    }
    if expected_keys != set(concrete_topologies_by_template):
        raise ValueError("concrete topology keys must match every mapped reaction participant")

    atom_maps_by_template: dict[tuple[LogicalReactionParticipantSide, int], tuple[int, ...]] = {}
    mapped_smiles_by_template: dict[tuple[LogicalReactionParticipantSide, int], str] = {}
    resolved_topologies: dict[tuple[LogicalReactionParticipantSide, int], MolecularTopology] = {}
    for source_participant in sorted(
        source_participants,
        key=lambda participant: (participant.side.value, participant.template_index),
    ):
        key = (source_participant.side, source_participant.template_index)
        logical_participant = source_participant.logical_reaction_participant
        source_topology = (
            _resolve_topology_value(session, source_participant.concrete_topology_id)
            if source_participant.concrete_topology_id is not None
            else logical_participant.topology
        )
        target_topology = _resolve_topology_value(
            session,
            concrete_topologies_by_template[key],
        )
        if source_topology.id == target_topology.id:
            atom_maps = list(source_participant.atom_map_numbers)
        else:
            atom_maps = _transferred_atom_maps(
                logical_participant=logical_participant,
                source_topology=source_topology,
                source_atom_maps=source_participant.atom_map_numbers,
                target_topology=target_topology,
            )
        mapped_smiles = mapped_smiles_for_topology(target_topology, atom_maps)
        atom_maps_by_template[key] = tuple(atom_maps)
        mapped_smiles_by_template[key] = mapped_smiles
        resolved_topologies[key] = target_topology

    canonical_sides: dict[LogicalReactionParticipantSide, list[tuple[int, str]]] = {
        LogicalReactionParticipantSide.REACTANT: [],
        LogicalReactionParticipantSide.PRODUCT: [],
    }
    side_map_sets: dict[LogicalReactionParticipantSide, set[int]] = {}
    for key, mapped_smiles in mapped_smiles_by_template.items():
        side, template_index = key
        canonical_sides[side].append((template_index, mapped_smiles))
        side_map_sets.setdefault(side, set()).update(atom_maps_by_template[key])
    if (
        not canonical_sides[LogicalReactionParticipantSide.REACTANT]
        or not canonical_sides[LogicalReactionParticipantSide.PRODUCT]
    ):
        raise ValueError("mapped reaction transfer requires reactant and product participants")
    if (
        side_map_sets[LogicalReactionParticipantSide.REACTANT]
        != side_map_sets[LogicalReactionParticipantSide.PRODUCT]
    ):
        raise ValueError("transferred reaction atom maps do not conserve both sides")
    reactants = ".".join(
        mapped_smiles
        for _, mapped_smiles in sorted(canonical_sides[LogicalReactionParticipantSide.REACTANT])
    )
    products = ".".join(
        mapped_smiles
        for _, mapped_smiles in sorted(canonical_sides[LogicalReactionParticipantSide.PRODUCT])
    )
    mapped_reaction_smiles = f"{reactants}>>{products}"
    return TransferredMappedReaction(
        mapped_reaction_smiles=mapped_reaction_smiles,
        mapping_hash=sha256(mapped_reaction_smiles.encode("utf-8")).hexdigest(),
        atom_maps_by_template=atom_maps_by_template,
        mapped_smiles_by_template=mapped_smiles_by_template,
        concrete_topologies_by_template=resolved_topologies,
    )


def persist_workflow_manifest(
    session: Session,
    artifact_file: ArtifactFile,
    record: WorkflowManifestRecord,
    *,
    supersedes: WorkflowManifest | None = None,
) -> WorkflowManifest:
    """Insert or reuse one immutable manifest revision and its source artifact."""

    artifact_file_id = _require_id(artifact_file, label="ArtifactFile")
    if artifact_file.artifact_kind is not ArtifactKind.WORKFLOW_MANIFEST:
        raise ValueError("WorkflowManifest requires a workflow_manifest ArtifactFile")
    if artifact_file.content_sha256 != record.payload_sha256:
        raise ValueError("manifest payload hash must match its ArtifactFile content hash")
    if record.status in {
        WorkflowManifestStatus.PUBLISHED,
        WorkflowManifestStatus.SUPERSEDED,
    }:
        raise ValueError("publication transitions require the future QC publication service")

    supersedes_id = None
    if supersedes is None:
        if record.revision != 1:
            raise ValueError(
                "manifest revisions after 1 must explicitly supersede their predecessor"
            )
    else:
        supersedes_id = _require_id(supersedes, label="WorkflowManifest")
        if supersedes.manifest_key != record.manifest_key:
            raise ValueError("a manifest can only supersede a revision in the same series")
        if record.revision != supersedes.revision + 1:
            raise ValueError("manifest revisions must advance their predecessor by exactly one")

    _acquire_identity_locks(
        session,
        ("workflow_manifest", record.manifest_key, record.revision),
        ("workflow_manifest_artifact", artifact_file_id),
    )
    manifest = session.exec(
        select(WorkflowManifest).where(
            WorkflowManifest.manifest_key == record.manifest_key,
            WorkflowManifest.revision == record.revision,
        )
    ).first()
    if manifest is not None:
        if manifest.artifact_file_id != artifact_file_id:
            raise ValueError("WorkflowManifest identity resolved to a different artifact")
        if manifest.supersedes_id != supersedes_id:
            raise ValueError("WorkflowManifest identity resolved to a different predecessor")
        _assert_record_matches(manifest, record, label="WorkflowManifest")
        return manifest

    manifest = WorkflowManifest(
        artifact_file=artifact_file,
        **record.model_dump(),
    )
    if supersedes is not None:
        manifest.supersedes = supersedes
    _flush_new_entity(session, manifest, label="WorkflowManifest")
    return manifest


def persist_manifest_artifact_binding(
    session: Session,
    workflow_manifest: WorkflowManifest,
    record: ManifestArtifactBindingRecord,
    *,
    artifact_file: ArtifactFile | None = None,
    source_geometry_binding: ManifestArtifactBinding | None = None,
) -> ManifestArtifactBinding:
    """Insert or reuse an expected artifact declaration under a manifest."""

    manifest_id = _require_id(workflow_manifest, label="WorkflowManifest")
    _ensure_manifest_mutable(workflow_manifest)
    artifact_file_id = (
        _require_id(artifact_file, label="ArtifactFile") if artifact_file is not None else None
    )
    source_binding_id = (
        _require_id(source_geometry_binding, label="ManifestArtifactBinding")
        if source_geometry_binding is not None
        else None
    )
    if record.resolution_status is ArtifactResolutionStatus.RESOLVED and artifact_file is None:
        raise ValueError("a resolved artifact binding requires an ArtifactFile")
    if artifact_file is not None:
        if artifact_file.artifact_kind is not ArtifactKind.CALCULATION_OUTPUT:
            raise ValueError("calculation bindings require calculation_output artifacts")
        if record.expected_content_sha256 != artifact_file.content_sha256:
            raise ValueError("artifact binding hash does not match its resolved ArtifactFile")
        if (
            record.resolution_status is ArtifactResolutionStatus.RESOLVED
            and artifact_file.storage_status is not StorageStatus.AVAILABLE
        ):
            raise ValueError("a resolved artifact binding requires an available ArtifactFile")
    if source_geometry_binding is None:
        if record.source_geometry_artifact_key is not None:
            raise ValueError("source_geometry_artifact_key must resolve within the same manifest")
    else:
        if source_geometry_binding.workflow_manifest_id != manifest_id:
            raise ValueError("source geometry binding must belong to the same manifest")
        if source_geometry_binding.artifact_key != record.source_geometry_artifact_key:
            raise ValueError("source geometry binding does not match the declared artifact key")

    _acquire_identity_locks(
        session,
        ("manifest_artifact_binding", manifest_id, record.artifact_key),
    )
    binding = session.exec(
        select(ManifestArtifactBinding).where(
            ManifestArtifactBinding.workflow_manifest_id == manifest_id,
            ManifestArtifactBinding.artifact_key == record.artifact_key,
        )
    ).first()
    if binding is not None:
        if binding.artifact_file_id != artifact_file_id:
            raise ValueError("artifact binding identity resolved to a different ArtifactFile")
        if (
            source_binding_id is not None
            and binding.source_geometry_binding is not None
            and binding.source_geometry_binding.id != source_binding_id
        ):
            raise ValueError("artifact binding identity resolved to a different geometry source")
        _assert_record_matches(binding, record, label="ManifestArtifactBinding")
        return binding

    binding = _new_entity(
        session,
        ManifestArtifactBinding,
        workflow_manifest=workflow_manifest,
        artifact_file=artifact_file,
        **record.model_dump(),
    )
    if source_geometry_binding is not None:
        binding.source_geometry_binding = source_geometry_binding
    _flush_new_entity(session, binding, label="ManifestArtifactBinding")
    return binding


def persist_logical_reaction(
    session: Session,
    record: LogicalReactionRecord,
) -> LogicalReaction:
    """Insert or reuse a topology-defined logical reaction globally."""

    _acquire_identity_locks(session, ("logical_reaction", record.reaction_hash))
    reaction = session.exec(
        select(LogicalReaction).where(LogicalReaction.reaction_hash == record.reaction_hash)
    ).first()
    if reaction is not None:
        # Automatic endpoint inference leaves the class unset. A later curator
        # command may explicitly classify that same topology identity.
        if reaction.reaction_class is None and record.reaction_class is not None:
            reaction.reaction_class = record.reaction_class
            session.flush()
        return reaction
    reaction = _new_entity(session, LogicalReaction, **record.model_dump())
    _flush_new_entity(session, reaction, label="LogicalReaction")
    return reaction


def persist_logical_reaction_participant(
    session: Session,
    reaction: LogicalReaction,
    topology: MolecularTopology,
    record: LogicalReactionParticipantRecord,
    *,
    candidate_topologies: Iterable[MolecularTopology] = (),
) -> LogicalReactionParticipant:
    """Insert/reuse a logical participant and register its concrete members."""

    reaction_id = _require_id(reaction, label="LogicalReaction")
    topology_id = _require_id(topology, label="MolecularTopology")

    _acquire_identity_locks(
        session,
        ("logical_reaction_participant", reaction_id, record.side.value, record.participant_index),
    )
    participant = session.exec(
        select(LogicalReactionParticipant).where(
            LogicalReactionParticipant.logical_reaction_id == reaction_id,
            LogicalReactionParticipant.side == record.side,
            LogicalReactionParticipant.participant_index == record.participant_index,
        )
    ).first()
    if participant is not None:
        if participant.topology_id != topology_id:
            raise ValueError("LogicalReactionParticipant identity resolved to a different Topology")
        _assert_record_matches(
            participant,
            record,
            label="LogicalReactionParticipant",
            exclude={"role"},
        )
        if record.role is not None:
            if participant.role is None:
                participant.role = record.role
                session.add(participant)
                _attach_pending_entities(session)
                session.flush()
            elif participant.role is not record.role:
                raise ValueError(
                    "LogicalReactionParticipant identity resolved to different role: "
                    f"{participant.role!r} != {record.role!r}"
                )
        from tricycle_reaction_db.application.services.reaction_topology_membership import (
            ensure_logical_participant_concrete_memberships,
        )

        ensure_logical_participant_concrete_memberships(
            session,
            participant,
            candidate_topologies=candidate_topologies,
        )
        return participant

    participant = LogicalReactionParticipant(
        logical_reaction=reaction,
        topology=topology,
        **record.model_dump(),
    )
    _flush_new_entity(session, participant, label="LogicalReactionParticipant")
    from tricycle_reaction_db.application.services.reaction_topology_membership import (
        ensure_logical_participant_concrete_memberships,
    )

    ensure_logical_participant_concrete_memberships(
        session,
        participant,
        candidate_topologies=candidate_topologies,
    )
    return participant


def validate_logical_reaction(reaction: LogicalReaction) -> None:
    """Validate net identity, conservation, and cross-side atom correspondence."""

    participants = list(reaction.participants)
    reactants = [
        participant
        for participant in participants
        if participant.side is LogicalReactionParticipantSide.REACTANT
    ]
    products = [
        participant
        for participant in participants
        if participant.side is LogicalReactionParticipantSide.PRODUCT
    ]
    if not reactants or not products:
        raise ValueError("a reaction requires at least one participant on each side")

    actual_hash = reaction_hash_for_participants(
        (participant.side, participant.topology, participant.stoichiometric_coefficient)
        for participant in participants
    )
    if reaction.reaction_hash != actual_hash:
        raise ValueError("LogicalReaction.reaction_hash does not match its persisted participants")

    def composition(rows: list[LogicalReactionParticipant]) -> Counter[tuple[int, int]]:
        result: Counter[tuple[int, int]] = Counter()
        for participant in rows:
            coefficient = participant.stoichiometric_coefficient
            for item in participant.topology.formula.composition:
                result[(item["atomic_number"], item["isotope"])] += item["count"] * coefficient
        return result

    if composition(reactants) != composition(products):
        raise ValueError("reaction participants do not conserve elements and isotopes")
    reactant_charge = sum(
        participant.topology.formal_charge * participant.stoichiometric_coefficient
        for participant in reactants
    )
    product_charge = sum(
        participant.topology.formal_charge * participant.stoichiometric_coefficient
        for participant in products
    )
    if reactant_charge != product_charge:
        raise ValueError("reaction participants do not conserve formal charge")


def persist_mapped_reaction(
    session: Session,
    reaction: LogicalReaction,
    record: MappedReactionRecord,
    *,
    source_atom_maps_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], Iterable[int]]
    | None = None,
    topology_ids_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], object]
    | None = None,
    concrete_topology_ids_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], object]
    | None = None,
    precomputed_mapped_smiles_by_template: Mapping[tuple[LogicalReactionParticipantSide, int], str]
    | None = None,
) -> MappedReaction:
    """Insert or reuse one explicit mapped reaction under a logical reaction."""

    reaction_id = _require_id(reaction, label="LogicalReaction")
    if precomputed_mapped_smiles_by_template is not None:
        if source_atom_maps_by_template is None or topology_ids_by_template is None:
            raise ValueError(
                "precomputed mapped reaction participants require topology and atom-map bindings"
            )
        component_keys = set(precomputed_mapped_smiles_by_template)
        if component_keys != set(source_atom_maps_by_template) or component_keys != set(
            topology_ids_by_template
        ):
            raise ValueError("precomputed mapped reaction participant keys must match")
        if concrete_topology_ids_by_template is not None and component_keys != set(
            concrete_topology_ids_by_template
        ):
            raise ValueError("concrete mapped reaction participant keys must match")
        participants_by_key = {
            (participant.side, participant.participant_index): participant
            for participant in reaction.participants
        }
        if component_keys != set(participants_by_key):
            raise ValueError(
                "precomputed mapped reaction components must match logical participants"
            )
        normalized_atom_maps = {
            key: tuple(int(number) for number in atom_maps)
            for key, atom_maps in source_atom_maps_by_template.items()
        }
        canonical_sides: dict[LogicalReactionParticipantSide, list[tuple[int, str]]] = {
            LogicalReactionParticipantSide.REACTANT: [],
            LogicalReactionParticipantSide.PRODUCT: [],
        }
        for (side, template_index), mapped_smiles in precomputed_mapped_smiles_by_template.items():
            canonical_sides[side].append((template_index, mapped_smiles))
        reactants = ".".join(
            smiles for _, smiles in sorted(canonical_sides[LogicalReactionParticipantSide.REACTANT])
        )
        products = ".".join(
            smiles for _, smiles in sorted(canonical_sides[LogicalReactionParticipantSide.PRODUCT])
        )
        canonical_smiles = f"{reactants}>>{products}"
        expected_hash = sha256(canonical_smiles.encode("utf-8")).hexdigest()
        if record.mapped_reaction_smiles != canonical_smiles:
            raise ValueError(
                "precomputed mapped_reaction_smiles must match its trusted topology components"
            )
        if record.mapping_hash != expected_hash:
            raise ValueError("mapping_hash does not match mapped_reaction_smiles")

        concrete_topologies_by_key: dict[
            tuple[LogicalReactionParticipantSide, int], MolecularTopology
        ] = {}
        for component_key in component_keys:
            participant = participants_by_key[component_key]
            expected_topology_id = topology_ids_by_template[component_key]
            if participant.topology_id != expected_topology_id:
                raise ValueError(
                    "precomputed mapped reaction component resolved to a different topology"
                )
            concrete_topology_value = (
                concrete_topology_ids_by_template.get(component_key)
                if concrete_topology_ids_by_template is not None
                else participant.topology_id
            )
            concrete_topologies_by_key[component_key] = _resolve_topology_value(
                session,
                concrete_topology_value,
            )

        # The text hash above remains the strict mapped-SMILES identity, but
        # it cannot distinguish a new physical mapping from a different
        # serialization of the same concrete components.  Before creating a
        # row, also lock and check the concrete topology + atom-map identity.
        # This is the important idempotency barrier for mappings transferred
        # through the logical-topology DAG.
        if concrete_topology_ids_by_template is not None:
            concrete_identity = mapped_reaction_concrete_identity_for_templates(
                session,
                reaction,
                atom_maps_by_template=normalized_atom_maps,
                concrete_topology_ids_by_template=concrete_topology_ids_by_template,
            )
            _acquire_identity_locks(
                session,
                ("mapped_reaction_concrete_identity", reaction_id, concrete_identity),
            )
            existing_concrete = find_mapped_reaction_by_concrete_identity(
                session,
                reaction_id,
                concrete_identity,
                refresh=True,
            )
            if existing_concrete is not None:
                return existing_concrete

        _acquire_identity_locks(
            session,
            ("mapped_reaction", reaction_id, record.mapping_hash),
        )
        mapped_reaction_result = session.exec(
            select(MappedReaction).where(
                MappedReaction.logical_reaction_id == reaction_id,
                MappedReaction.mapping_hash == record.mapping_hash,
            )
        )
        mapped_reaction = mapped_reaction_result.first()
        if mapped_reaction is None:
            mapped_reaction = _new_entity(
                session,
                MappedReaction,
                logical_reaction=reaction,
                **record.model_dump(),
            )
            _flush_new_entity(session, mapped_reaction, label="MappedReaction")

        for component_key in sorted(
            component_keys,
            key=lambda item: (item[0].value, item[1]),
        ):
            participant = participants_by_key[component_key]
            persist_mapped_reaction_participant(
                session,
                mapped_reaction,
                participant,
                template_index=component_key[1],
                atom_map_numbers=list(normalized_atom_maps[component_key]),
                mapped_smiles=precomputed_mapped_smiles_by_template[component_key],
                concrete_topology=concrete_topologies_by_key[component_key],
            )
        _register_mapped_reaction_concrete_identity(session, mapped_reaction)
        return mapped_reaction

    definition = _reaction_from_representation(record.mapped_reaction_smiles)
    canonical_smiles = _canonical_mapped_reaction_smiles(definition)
    expected_hash = sha256(canonical_smiles.encode("utf-8")).hexdigest()
    if record.mapped_reaction_smiles != canonical_smiles:
        canonical_definition = _reaction_from_representation(canonical_smiles)
        if _reaction_graph_smiles(definition) != _reaction_graph_smiles(canonical_definition):
            raise ValueError("mapped_reaction_smiles must use canonical RDKit serialization")
        # RDKit can rewrite unsupported metal stereochemistry differently on
        # each parse. Keep the graph-equivalent canonical projection in storage.
        record = record.model_copy(update={"mapped_reaction_smiles": canonical_smiles})
        definition = canonical_definition
    if record.mapping_hash != expected_hash:
        raise ValueError("mapping_hash does not match mapped_reaction_smiles")

    normalized_source_atom_maps = (
        {
            key: tuple(int(number) for number in atom_maps)
            for key, atom_maps in source_atom_maps_by_template.items()
        }
        if source_atom_maps_by_template is not None
        else None
    )
    _acquire_identity_locks(
        session,
        ("mapped_reaction", reaction_id, record.mapping_hash),
    )
    mapped_reaction_result = session.exec(
        select(MappedReaction).where(
            MappedReaction.logical_reaction_id == reaction_id,
            MappedReaction.mapping_hash == record.mapping_hash,
        )
    )
    mapped_reaction = mapped_reaction_result.first()
    if mapped_reaction is None:
        mapped_reaction = _new_entity(
            session, MappedReaction, logical_reaction=reaction, **record.model_dump()
        )
        _flush_new_entity(session, mapped_reaction, label="MappedReaction")

    for side, templates in (
        (LogicalReactionParticipantSide.REACTANT, definition.GetReactants()),
        (LogicalReactionParticipantSide.PRODUCT, definition.GetProducts()),
    ):
        unused = [participant for participant in reaction.participants if participant.side is side]
        if len(unused) != len(templates):
            raise ValueError("mapped reaction template count must match logical participants")
        for template_index, template in enumerate(templates):
            match = None
            template_key = (side, template_index)
            source_atom_maps = (
                normalized_source_atom_maps.get(template_key)
                if normalized_source_atom_maps is not None
                else None
            )
            expected_topology_id = (
                topology_ids_by_template.get(template_key)
                if topology_ids_by_template is not None
                else None
            )
            expected_concrete_topology_value = (
                concrete_topology_ids_by_template.get(template_key)
                if concrete_topology_ids_by_template is not None
                else None
            )
            expected_concrete_topology = (
                _resolve_topology_value(session, expected_concrete_topology_value)
                if expected_concrete_topology_value is not None
                else None
            )
            for participant in unused:
                if (
                    expected_topology_id is not None
                    and participant.topology_id != expected_topology_id
                ):
                    continue
                try:
                    atom_maps, logical_mapped_smiles = _mapping_assignment_for_topology(
                        template,
                        participant.topology,
                        source_atom_map_numbers=source_atom_maps,
                    )
                    mapped_smiles = (
                        mapped_smiles_for_topology(expected_concrete_topology, atom_maps)
                        if expected_concrete_topology is not None
                        else logical_mapped_smiles
                    )
                except ValueError:
                    continue
                match = participant, atom_maps, mapped_smiles
                break
            if match is None:
                raise ValueError(
                    "mapped reaction templates cannot be assigned to logical topologies"
                )
            participant, atom_maps, mapped_smiles = match
            unused.remove(participant)
            persist_mapped_reaction_participant(
                session,
                mapped_reaction,
                participant,
                template_index=template_index,
                atom_map_numbers=atom_maps,
                mapped_smiles=mapped_smiles,
                concrete_topology=expected_concrete_topology,
            )
    return mapped_reaction


def persist_mapped_reaction_participant(
    session: Session,
    mapped_reaction: MappedReaction,
    logical_participant: LogicalReactionParticipant,
    *,
    template_index: int,
    atom_map_numbers: list[int],
    mapped_smiles: str,
    concrete_topology: MolecularTopology | None = None,
    concrete_topology_id: UUID | None = None,
) -> MappedReactionParticipant:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    logical_participant_id = _require_id(logical_participant, label="LogicalReactionParticipant")
    if logical_participant.logical_reaction_id != mapped_reaction.logical_reaction_id:
        raise ValueError("mapped participant must belong to the same LogicalReaction")
    if (
        concrete_topology is not None
        and concrete_topology_id is not None
        and (_require_id(concrete_topology, label="MolecularTopology") != concrete_topology_id)
    ):
        raise ValueError("concrete topology object and id do not match")
    if concrete_topology is None:
        concrete_topology = (
            _resolve_topology_value(session, concrete_topology_id)
            if concrete_topology_id is not None
            else logical_participant.topology
        )
    if concrete_topology is None:
        raise ValueError("mapped participant requires a concrete MolecularTopology")
    concrete_topology_id = _require_id(concrete_topology, label="MolecularTopology")
    from tricycle_reaction_db.application.services.reaction_topology_membership import (
        persist_logical_participant_concrete_topology,
    )

    persist_logical_participant_concrete_topology(
        session,
        logical_participant,
        concrete_topology,
    )
    if mapped_smiles_for_topology(concrete_topology, atom_map_numbers) != mapped_smiles:
        raise ValueError("mapped participant SMILES does not match its Topology atom maps")
    _acquire_identity_locks(
        session,
        ("mapped_reaction_participant", mapped_reaction_id, logical_participant_id),
    )
    assignment = session.exec(
        select(MappedReactionParticipant).where(
            MappedReactionParticipant.mapped_reaction_id == mapped_reaction_id,
            MappedReactionParticipant.logical_reaction_participant_id == logical_participant_id,
        )
    ).first()
    if assignment is not None:
        if assignment.side is not logical_participant.side:
            raise ValueError("mapped participant resolved to different side")
        if assignment.template_index != template_index:
            raise ValueError("mapped participant resolved to different template_index")
        if assignment.concrete_topology_id not in {None, concrete_topology_id}:
            raise ValueError("mapped participant resolved to a different concrete topology")
        if assignment.mapped_smiles != mapped_smiles:
            raise ValueError("mapped participant resolved to different mapped_smiles")
        if set(assignment.atom_map_numbers) != set(atom_map_numbers):
            raise ValueError("mapped participant resolved to a different atom-map set")
        if assignment.concrete_topology_id is None:
            assignment.concrete_topology_id = concrete_topology_id
            session.add(assignment)
            session.flush()
        return assignment
    assignment = _new_entity(
        session,
        MappedReactionParticipant,
        mapped_reaction=mapped_reaction,
        mapped_reaction_id=mapped_reaction_id,
        logical_reaction_participant=logical_participant,
        logical_reaction_participant_id=logical_participant_id,
        concrete_topology=concrete_topology,
        concrete_topology_id=concrete_topology_id,
        side=logical_participant.side,
        template_index=template_index,
        atom_map_numbers=atom_map_numbers,
        mapped_smiles=mapped_smiles,
    )
    _flush_new_entity(session, assignment, label="MappedReactionParticipant")
    return assignment


def persist_mapped_reaction_node(
    session: Session,
    mapped_reaction: MappedReaction,
    record: MappedReactionNodeRecord,
) -> MappedReactionNode:
    """Persist a logical path node, reordering an existing named node if needed.

    ``node_key`` is the stable identity used by reaction edges and declarations.
    ``node_index`` is the display/path order.  An automatic reaction may create
    conventional nodes before a curated path declares its preferred order, so a
    later declaration may safely reorder the same named node.  Declaring a new
    node at an occupied position inserts it into that path order and shifts the
    later nodes forward.
    """

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    aliases = _ENDPOINT_NODE_KEY_ALIASES.get(record.role, ())
    preferred_key = aliases[0] if record.node_key in aliases else record.node_key
    _acquire_identity_locks(
        session,
        ("mapped_reaction_node_order", mapped_reaction_id),
        ("mapped_reaction_node_key", mapped_reaction_id, preferred_key),
        ("mapped_reaction_node_key", mapped_reaction_id, record.node_key),
        ("mapped_reaction_node_index", mapped_reaction_id, record.node_index),
    )
    node = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.node_key == preferred_key,
        )
    ).first()
    if node is None and preferred_key != record.node_key:
        node = session.exec(
            select(MappedReactionNode).where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                MappedReactionNode.node_key == record.node_key,
            )
        ).first()
    indexed = session.exec(
        select(MappedReactionNode).where(
            MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
            MappedReactionNode.node_index == record.node_index,
        )
    ).first()
    if node is not None:
        _assert_record_matches(
            node,
            record,
            label="MappedReactionNode",
            exclude={"node_key", "node_index"},
        )
        if node.node_index != record.node_index:
            if indexed is not None and indexed.id != node.id:
                highest_index = session.exec(
                    select(MappedReactionNode.node_index)
                    .where(MappedReactionNode.mapped_reaction_id == mapped_reaction_id)
                    .order_by(col(MappedReactionNode.node_index).desc())
                    .limit(1)
                ).one()
                indexed.node_index = highest_index + 1
                session.add(indexed)
                session.flush()
            node.node_index = record.node_index
            session.add(node)
            _attach_pending_entities(session)
            session.flush()
        return node
    if indexed is not None:
        later_nodes = session.exec(
            select(MappedReactionNode)
            .where(
                MappedReactionNode.mapped_reaction_id == mapped_reaction_id,
                MappedReactionNode.node_index >= record.node_index,
            )
            .order_by(col(MappedReactionNode.node_index).desc())
        ).all()
        for later_node in later_nodes:
            later_node.node_index += 1
            session.add(later_node)
            # Descending updates leave each target slot free before the next
            # row moves, satisfying the non-deferrable unique constraint.
            _attach_pending_entities(session)
            session.flush()
    node = _new_entity(
        session, MappedReactionNode, mapped_reaction=mapped_reaction, **record.model_dump()
    )
    _flush_new_entity(session, node, label="MappedReactionNode")
    return node


def persist_mapped_reaction_node_geometry(
    session: Session,
    node: MappedReactionNode,
    geometry: Geometry,
    record: MappedReactionNodeGeometryRecord,
    *,
    mapped_reaction_participant: MappedReactionParticipant | None = None,
    preloaded_bindings: list[MappedReactionNodeGeometry] | None = None,
    thermodynamic_property_verified: bool = False,
) -> MappedReactionNodeGeometry:
    """Bind one Geometry conformer to a logical path node.

    Geometry is the conformer identity.  ``coordinate_index`` is only a
    stable display order within one node component, so it cannot decide
    whether an existing geometry binding is reused.
    """

    node_id = _require_id(node, label="MappedReactionNode")
    geometry_id = _require_id(geometry, label="Geometry")
    if mapped_reaction_participant is not None:
        if not thermodynamic_property_verified:
            require_geometry_reaction_endpoint_eligibility(session, geometry)
    elif not thermodynamic_property_verified:
        require_geometry_thermodynamic_property(session, geometry)
    participant_id = None
    if mapped_reaction_participant is not None:
        participant_id = _require_id(mapped_reaction_participant, label="MappedReactionParticipant")
        if mapped_reaction_participant.mapped_reaction_id != node.mapped_reaction_id:
            raise ValueError("coordinate participant must belong to the same MappedReaction")
        logical_participant = mapped_reaction_participant.logical_reaction_participant
        participant_topology_id = (
            mapped_reaction_participant.concrete_topology_id or logical_participant.topology_id
        )
        if participant_topology_id != geometry.topology_id:
            raise ValueError("coordinate Geometry must match the participant concrete Topology")
        expected_side = (
            LogicalReactionParticipantSide.REACTANT
            if node.role is MappedReactionNodeRole.REACTANT
            else LogicalReactionParticipantSide.PRODUCT
            if node.role is MappedReactionNodeRole.PRODUCT
            else None
        )
        if expected_side is None or mapped_reaction_participant.side is not expected_side:
            raise ValueError("coordinate participant side does not match the node role")
    elif node.role in {MappedReactionNodeRole.REACTANT, MappedReactionNodeRole.PRODUCT}:
        raise ValueError("reactant/product coordinates require a MappedReactionParticipant")

    lock_keys: list[tuple[object, ...]] = [
        (
            "mapped_reaction_node_geometry_identity",
            node_id,
            geometry_id,
            participant_id or "unassigned",
        ),
        (
            "mapped_reaction_node_geometry",
            node_id,
            record.component_key,
            record.coordinate_index,
        ),
    ]
    if record.is_primary:
        lock_keys.append(("mapped_reaction_node_primary_geometry", node_id, record.component_key))
    _acquire_identity_locks(session, *lock_keys)

    identity_statement = select(MappedReactionNodeGeometry).where(
        MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
        MappedReactionNodeGeometry.geometry_id == geometry_id,
    )
    if participant_id is None:
        identity_statement = identity_statement.where(
            col(MappedReactionNodeGeometry.mapped_reaction_participant_id).is_(None)
        )
    else:
        identity_statement = identity_statement.where(
            MappedReactionNodeGeometry.mapped_reaction_participant_id == participant_id
        )
    binding = next(
        (
            existing
            for existing in preloaded_bindings or ()
            if existing.geometry_id == geometry_id
            and existing.mapped_reaction_participant_id == participant_id
        ),
        None,
    )
    if binding is None:
        # ``preloaded_bindings`` is an optimization, not an authority.  It
        # can be a partial view after a node was resolved before its geometry
        # collection was loaded, so a miss must be checked against PostgreSQL
        # before creating a row with the same NULL-participant identity.
        binding = session.exec(identity_statement).first()
    if binding is not None:
        if (
            binding.component_key != record.component_key
            or binding.component_index != record.component_index
        ):
            raise ValueError("node Geometry identity resolved to inconsistent component identity")
        if record.is_primary:
            _promote_mapped_reaction_node_geometry(session, binding)
        return binding

    existing_components = [
        existing
        for existing in preloaded_bindings or ()
        if existing.component_key == record.component_key
        or existing.component_index == record.component_index
    ]
    # Merge the database view even when a caller supplied a preloaded list.
    # The list may include pending fast-path rows, so retain both views.
    existing_components.extend(
        session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                (
                    (MappedReactionNodeGeometry.component_key == record.component_key)
                    | (MappedReactionNodeGeometry.component_index == record.component_index)
                ),
            )
        ).all()
    )
    for existing in existing_components:
        if (
            existing.component_key != record.component_key
            or existing.component_index != record.component_index
            or existing.mapped_reaction_participant_id != participant_id
        ):
            raise ValueError("component key/index resolves to inconsistent coordinate identity")

    binding = next(
        (
            existing
            for existing in preloaded_bindings or ()
            if existing.component_key == record.component_key
            and existing.coordinate_index == record.coordinate_index
        ),
        None,
    )
    if binding is None:
        binding = session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                MappedReactionNodeGeometry.component_key == record.component_key,
                MappedReactionNodeGeometry.coordinate_index == record.coordinate_index,
            )
        ).first()
    if binding is not None:
        raise ValueError("node component coordinate is already assigned to a different Geometry")

    binding = _new_entity(
        session,
        MappedReactionNodeGeometry,
        mapped_reaction_node=node,
        geometry=geometry,
        mapped_reaction_participant=mapped_reaction_participant,
        **record.model_copy(update={"is_primary": False}).model_dump(),
    )
    _flush_new_entity(session, binding, label="MappedReactionNodeGeometry")
    if record.is_primary:
        _promote_mapped_reaction_node_geometry(session, binding)
    return binding


def _promote_mapped_reaction_node_geometry(
    session: Session,
    binding: MappedReactionNodeGeometry,
) -> None:
    """Make a curated Geometry the one display-primary conformer of its component."""

    node_id = _require_id(binding.mapped_reaction_node, label="MappedReactionNode")
    binding_id = _require_id(binding, label="MappedReactionNodeGeometry")
    _acquire_identity_locks(
        session,
        ("mapped_reaction_node_primary_geometry", node_id, binding.component_key),
    )
    current_primaries = session.exec(
        select(MappedReactionNodeGeometry).where(
            MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
            MappedReactionNodeGeometry.component_key == binding.component_key,
            col(MappedReactionNodeGeometry.is_primary).is_(True),
        )
    ).all()
    for primary in current_primaries:
        if primary.id != binding_id:
            primary.is_primary = False
            session.add(primary)
    if any(primary.id != binding_id for primary in current_primaries):
        # The partial unique index requires former primaries to be cleared
        # before promoting the requested Geometry.
        _attach_pending_entities(session)
        session.flush()
    if not binding.is_primary:
        binding.is_primary = True
        session.add(binding)
        _attach_pending_entities(session)
        session.flush()


def persist_mapped_reaction_node_geometry_mapping(
    session: Session,
    node_geometry: MappedReactionNodeGeometry,
    record: MappedReactionNodeGeometryMappingRecord,
    *,
    identity_is_new: bool = False,
) -> MappedReactionNodeGeometryMapping:
    """Persist one verified mapped-reaction to Geometry atom-order conversion."""

    node_geometry_id = _require_id(node_geometry, label="MappedReactionNodeGeometry")
    node = node_geometry.mapped_reaction_node
    mapped_reaction = node.mapped_reaction
    if len(record.geometry_atom_map_numbers) != node_geometry.geometry.atom_count:
        raise ValueError("Geometry atom-map count must match the bound Geometry")
    expected_smiles = mapped_smiles_for_topology(
        node_geometry.geometry.topology,
        record.geometry_atom_map_numbers,
    )
    if record.mapped_smiles != expected_smiles:
        raise ValueError("mapped_smiles does not match the converted coordinate mapping")
    logical_map_numbers = _logical_map_numbers_for_reaction(mapped_reaction)
    if not set(record.geometry_atom_map_numbers).issubset(logical_map_numbers):
        raise ValueError("coordinate mapping contains atom maps absent from the logical path")
    participant = node_geometry.mapped_reaction_participant
    if participant is not None and not _reaction_mapping_isomorphic(
        expected_atom_map_numbers=participant.atom_map_numbers,
        expected_mapped_smiles=participant.mapped_smiles,
        observed_atom_map_numbers=record.geometry_atom_map_numbers,
        observed_mapped_smiles=record.mapped_smiles,
    ):
        raise ValueError("coordinate mapping must match its MappedReactionParticipant")

    binding = None
    if not identity_is_new:
        _acquire_identity_locks(
            session,
            ("mapped_reaction_node_geometry_mapping", node_geometry_id),
        )
        binding = session.exec(
            select(MappedReactionNodeGeometryMapping).where(
                MappedReactionNodeGeometryMapping.mapped_reaction_node_geometry_id
                == node_geometry_id,
            )
        ).first()
    if binding is not None:
        if not _reaction_mapping_isomorphic(
            expected_atom_map_numbers=binding.geometry_atom_map_numbers,
            expected_mapped_smiles=binding.mapped_smiles,
            observed_atom_map_numbers=record.geometry_atom_map_numbers,
            observed_mapped_smiles=record.mapped_smiles,
        ):
            raise ValueError("node Geometry has an incompatible reaction mapping")
        # A Geometry mapping is expressed in canonical Geometry/Topology
        # order.  Source atom order and its permutation belong to each Frame,
        # so an equivalent mapping is reusable across QM programs and files.
        return binding
    binding = _new_entity(
        session,
        MappedReactionNodeGeometryMapping,
        mapped_reaction_node_geometry=node_geometry,
        **record.model_dump(),
    )
    _flush_new_entity(session, binding, label="MappedReactionNodeGeometryMapping")
    return binding


def persist_mapped_reaction_edge(
    session: Session,
    mapped_reaction: MappedReaction,
    source_node: MappedReactionNode,
    target_node: MappedReactionNode,
    record: MappedReactionEdgeRecord,
    *,
    transition_state_node: MappedReactionNode | None = None,
) -> MappedReactionEdge:
    """Insert or reuse one directed elementary edge declared by the manifest."""

    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    source_id = _require_id(source_node, label="source MappedReactionNode")
    target_id = _require_id(target_node, label="target MappedReactionNode")
    transition_state_id = (
        _require_id(transition_state_node, label="transition-state MappedReactionNode")
        if transition_state_node is not None
        else None
    )
    for label, node in (("source", source_node), ("target", target_node)):
        if node.mapped_reaction_id != mapped_reaction_id:
            raise ValueError(f"{label} node must belong to the edge MappedReaction")
    if source_id == target_id:
        raise ValueError("reaction path edge source and target must differ")
    if transition_state_node is not None:
        if transition_state_node.mapped_reaction_id != mapped_reaction_id:
            raise ValueError("transition-state node must belong to the edge MappedReaction")
        if transition_state_node.role is not MappedReactionNodeRole.TRANSITION_STATE:
            raise ValueError("transition_state_node must have the transition_state role")
        if transition_state_id in {source_id, target_id}:
            raise ValueError("transition-state node must differ from both edge endpoints")

    _acquire_identity_locks(session, ("mapped_reaction_edge", mapped_reaction_id, record.edge_key))
    edge = session.exec(
        select(MappedReactionEdge).where(
            MappedReactionEdge.mapped_reaction_id == mapped_reaction_id,
            MappedReactionEdge.edge_key == record.edge_key,
        )
    ).first()
    if edge is not None:
        if (
            edge.source_node_id != source_id
            or edge.target_node_id != target_id
            or edge.transition_state_node_id != transition_state_id
        ):
            raise ValueError("MappedReactionEdge identity resolved to different endpoint nodes")
        _assert_record_matches(edge, record, label="MappedReactionEdge")
        return edge

    edge = _new_entity(
        session,
        MappedReactionEdge,
        mapped_reaction=mapped_reaction,
        source_node=source_node,
        target_node=target_node,
        **record.model_dump(),
    )
    if transition_state_node is not None:
        edge.transition_state_node = transition_state_node
    _flush_new_entity(session, edge, label="MappedReactionEdge")
    return edge


__all__ = [
    "MappedReactionConcreteIdentity",
    "ParticipantIdentity",
    "atom_maps_from_source_order",
    "find_mapped_reaction_by_concrete_identity",
    "mapped_smiles_for_topology",
    "mapped_reaction_concrete_identity",
    "mapped_reaction_concrete_identity_for_templates",
    "persist_manifest_artifact_binding",
    "persist_logical_reaction",
    "persist_logical_reaction_participant",
    "persist_mapped_reaction",
    "persist_mapped_reaction_edge",
    "persist_mapped_reaction_node",
    "persist_mapped_reaction_node_geometry",
    "persist_mapped_reaction_node_geometry_mapping",
    "persist_mapped_reaction_participant",
    "persist_workflow_manifest",
    "reaction_hash_for_participants",
    "validate_logical_reaction",
]
