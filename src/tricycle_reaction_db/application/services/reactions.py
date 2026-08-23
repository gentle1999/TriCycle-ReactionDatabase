"""Relationship-driven persistence for manifest-declared reaction paths."""

import json
from collections import Counter
from collections.abc import Iterable
from functools import lru_cache
from hashlib import sha256

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
    _flush_new_entity,
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
    StorageStatus,
    WorkflowManifestStatus,
)

ParticipantIdentity = tuple[LogicalReactionParticipantSide, MolecularTopology, int]

_ENDPOINT_NODE_KEY_ALIASES: dict[MappedReactionNodeRole, tuple[str, ...]] = {
    MappedReactionNodeRole.REACTANT: ("reactants", "reactant"),
    MappedReactionNodeRole.PRODUCT: ("products", "product"),
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


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


_mapped_reaction_from_smiles = _reaction_from_representation


@lru_cache(maxsize=1024)
def _logical_map_numbers_for_reaction(reaction_representation: str) -> frozenset[int]:
    """Cache the immutable atom-map projection used by mapping validation."""

    reaction_definition = _reaction_from_representation(reaction_representation)
    return frozenset(
        atom.GetAtomMapNum()
        for templates in (
            reaction_definition.GetReactants(),
            reaction_definition.GetProducts(),
        )
        for molecule in templates
        for atom in molecule.GetAtoms()
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
    return Chem.MolToSmiles(
        mapped,
        canonical=True,
        isomericSmiles=True,
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
) -> tuple[list[int], str]:
    """Resolve template atom maps into canonical topology atom order."""

    template_maps = [
        atom.GetAtomMapNum()
        for atom in template.GetAtoms()  # type: ignore[no-untyped-call]
    ]
    if not template_maps or any(number <= 0 for number in template_maps):
        raise ValueError("every mapped reaction template atom must have an atom map")
    if len(template_maps) != topology.atom_count:
        raise ValueError("mapped reaction template atom count does not match its Topology")

    expected_smiles = Chem.MolToSmiles(
        template,
        canonical=True,
        isomericSmiles=True,
        allHsExplicit=True,
    )
    query = Chem.Mol(template)
    for atom in query.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    matches = topology.mol.GetSubstructMatches(
        query,
        uniquify=False,
        useChirality=True,
        maxMatches=10_000,
    )
    for match in matches:
        if len(match) != topology.atom_count:
            continue
        topology_maps = [0] * topology.atom_count
        for template_index, topology_index in enumerate(match):
            topology_maps[topology_index] = template_maps[template_index]
        mapped_smiles = mapped_smiles_for_topology(topology, topology_maps)
        if mapped_smiles == expected_smiles:
            return topology_maps, mapped_smiles
    raise ValueError("mapped reaction template is not isomorphic to its referenced Topology")


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

    binding = ManifestArtifactBinding(
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
        return reaction
    reaction = LogicalReaction(**record.model_dump())
    _flush_new_entity(session, reaction, label="LogicalReaction")
    return reaction


def persist_logical_reaction_participant(
    session: Session,
    reaction: LogicalReaction,
    topology: MolecularTopology,
    record: LogicalReactionParticipantRecord,
) -> LogicalReactionParticipant:
    """Insert or reuse one mapped participant through LogicalReaction and Topology objects."""

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
                session.flush()
            elif participant.role is not record.role:
                raise ValueError(
                    "LogicalReactionParticipant identity resolved to different role: "
                    f"{participant.role!r} != {record.role!r}"
                )
        return participant

    participant = LogicalReactionParticipant(
        logical_reaction=reaction,
        topology=topology,
        **record.model_dump(),
    )
    _flush_new_entity(session, participant, label="LogicalReactionParticipant")
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
) -> MappedReaction:
    """Insert or reuse one explicit mapped reaction under a logical reaction."""

    reaction_id = _require_id(reaction, label="LogicalReaction")
    definition = _reaction_from_representation(record.mapped_reaction_smiles)
    canonical_smiles = rdChemReactions.ReactionToSmiles(definition, True)
    expected_hash = sha256(canonical_smiles.encode("utf-8")).hexdigest()
    if record.mapped_reaction_smiles != canonical_smiles:
        raise ValueError("mapped_reaction_smiles must use canonical RDKit serialization")
    if record.mapping_hash != expected_hash:
        raise ValueError("mapping_hash does not match mapped_reaction_smiles")

    _acquire_identity_locks(
        session,
        ("mapped_reaction", reaction_id, record.mapping_hash),
    )
    mapped_reaction = session.exec(
        select(MappedReaction).where(
            MappedReaction.logical_reaction_id == reaction_id,
            MappedReaction.mapping_hash == record.mapping_hash,
        )
    ).first()
    if mapped_reaction is None:
        mapped_reaction = MappedReaction(logical_reaction=reaction, **record.model_dump())
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
            for participant in unused:
                try:
                    atom_maps, mapped_smiles = _mapping_assignment_for_topology(
                        template, participant.topology
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
) -> MappedReactionParticipant:
    mapped_reaction_id = _require_id(mapped_reaction, label="MappedReaction")
    logical_participant_id = _require_id(logical_participant, label="LogicalReactionParticipant")
    if logical_participant.logical_reaction_id != mapped_reaction.logical_reaction_id:
        raise ValueError("mapped participant must belong to the same LogicalReaction")
    if mapped_smiles_for_topology(logical_participant.topology, atom_map_numbers) != mapped_smiles:
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
        if assignment.mapped_smiles != mapped_smiles:
            raise ValueError("mapped participant resolved to different mapped_smiles")
        if set(assignment.atom_map_numbers) != set(atom_map_numbers):
            raise ValueError("mapped participant resolved to a different atom-map set")
        return assignment
    assignment = MappedReactionParticipant(
        mapped_reaction=mapped_reaction,
        mapped_reaction_id=mapped_reaction_id,
        logical_reaction_participant=logical_participant,
        logical_reaction_participant_id=logical_participant_id,
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
            session.flush()
    node = MappedReactionNode(mapped_reaction=mapped_reaction, **record.model_dump())
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
        if logical_participant.topology_id != geometry.topology_id:
            raise ValueError("coordinate Geometry must match the participant Topology")
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
    if preloaded_bindings is None:
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

    existing_components = (
        [
            existing
            for existing in preloaded_bindings
            if existing.component_key == record.component_key
            or existing.component_index == record.component_index
        ]
        if preloaded_bindings is not None
        else session.exec(
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
    if preloaded_bindings is None:
        binding = session.exec(
            select(MappedReactionNodeGeometry).where(
                MappedReactionNodeGeometry.mapped_reaction_node_id == node_id,
                MappedReactionNodeGeometry.component_key == record.component_key,
                MappedReactionNodeGeometry.coordinate_index == record.coordinate_index,
            )
        ).first()
    if binding is not None:
        raise ValueError("node component coordinate is already assigned to a different Geometry")

    binding = MappedReactionNodeGeometry(
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
        session.flush()
    if not binding.is_primary:
        binding.is_primary = True
        session.add(binding)
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
    logical_map_numbers = _logical_map_numbers_for_reaction(mapped_reaction.mapped_reaction_smiles)
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
    binding = MappedReactionNodeGeometryMapping(
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

    edge = MappedReactionEdge(
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
    "ParticipantIdentity",
    "atom_maps_from_source_order",
    "mapped_smiles_for_topology",
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
