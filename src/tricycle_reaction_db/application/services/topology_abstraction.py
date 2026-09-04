"""Versioned stereo-configuration abstraction for molecular topologies.

The abstraction relation is directed from a more specific topology to a more
general topology.  A molecule with multiple stereo features therefore forms a
small DAG: each edge removes one feature, while the persisted topology rows
remain independently reusable molecular identities.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from rdkit import Chem
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _flush_new_entity,
    _new_entity,
    _require_id,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_topology,
)
from tricycle_reaction_db.core.chemistry_config import (
    STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
    STEREO_ABSTRACTION_POLICY_VERSION,
    STEREO_ABSTRACTION_RECONSTRUCTION_METHOD,
)
from tricycle_reaction_db.db.models import MolecularTopology, MolecularTopologyAbstraction
from tricycle_reaction_db.ingestion.normalization import normalize_topology

StereoFeatureKind = Literal["atom", "bond"]

_ASSIGNED_BOND_STEREO = frozenset(
    {
        Chem.BondStereo.STEREOCIS,
        Chem.BondStereo.STEREOTRANS,
        Chem.BondStereo.STEREOE,
        Chem.BondStereo.STEREOZ,
        Chem.BondStereo.STEREOATROPCW,
        Chem.BondStereo.STEREOATROPCCW,
    }
)


class StereoAbstractionError(ValueError):
    """The proposed specific/general topology relation is invalid."""


@dataclass(frozen=True, slots=True)
class StereoFeature:
    """One assigned atom- or bond-centred stereo feature in a graph copy."""

    kind: StereoFeatureKind
    index: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.index}"


@dataclass(frozen=True, slots=True)
class StereoAbstractionProjection:
    """One graph-only projection after clearing a selected feature subset."""

    cleared_features: tuple[StereoFeature, ...]
    molecule: Chem.Mol


@dataclass(frozen=True, slots=True)
class StereoAbstractionMatch:
    """A verified mapping from a general topology query to a specific target."""

    general_to_specific_atom_indices: tuple[int, ...]
    abstracted_atom_indices: tuple[int, ...]
    abstracted_bond_indices: tuple[int, ...]

    @property
    def abstracted_feature_count(self) -> int:
        return len(self.abstracted_atom_indices) + len(self.abstracted_bond_indices)

    def metadata(self) -> dict[str, Any]:
        return {
            "match_schema_version": STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
            "general_to_specific_atom_indices": list(self.general_to_specific_atom_indices),
            "abstracted_atom_indices": list(self.abstracted_atom_indices),
            "abstracted_bond_indices": list(self.abstracted_bond_indices),
            "abstracted_feature_count": self.abstracted_feature_count,
        }


def assigned_stereo_features(molecule: Chem.Mol) -> tuple[StereoFeature, ...]:
    """Return assigned atom/bond stereo features in deterministic graph order."""

    features = [
        StereoFeature("atom", atom.GetIdx())
        for atom in molecule.GetAtoms()  # type: ignore[no-untyped-call]
        if atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED
    ]
    features.extend(
        StereoFeature("bond", bond.GetIdx())
        for bond in molecule.GetBonds()  # type: ignore[no-untyped-call]
        if bond.GetStereo() in _ASSIGNED_BOND_STEREO
    )
    return tuple(features)


def clear_stereo_features(
    molecule: Chem.Mol,
    features: Iterable[StereoFeature],
) -> Chem.Mol:
    """Return a graph-only copy with exactly the selected stereo features cleared.

    Clearing an E/Z feature also clears the neighbouring single-bond direction
    flags used by the SMILES writer.  Unrelated stereo features are retained.
    """

    projected = Chem.Mol(molecule)
    projected.RemoveAllConformers()
    selected = {(feature.kind, feature.index) for feature in features}
    for kind, index in selected:
        if index < 0:
            raise ValueError("stereo feature indices must be non-negative")
        if kind == "atom":
            if index >= projected.GetNumAtoms():
                raise ValueError("atom stereo feature index is outside the molecule")
            atom = projected.GetAtomWithIdx(index)
            atom.SetChiralTag(Chem.ChiralType.CHI_UNSPECIFIED)
            for property_name in ("_CIPCode", "_CIPRank"):
                if atom.HasProp(property_name):
                    atom.ClearProp(property_name)
            continue
        if kind != "bond" or index >= projected.GetNumBonds():
            raise ValueError("bond stereo feature index is outside the molecule")
        bond = projected.GetBondWithIdx(index)
        stereo_atoms = tuple(int(atom_index) for atom_index in bond.GetStereoAtoms())
        endpoints = (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
        for endpoint, stereo_atom in zip(endpoints, stereo_atoms, strict=False):
            direction_bond = projected.GetBondBetweenAtoms(endpoint, stereo_atom)
            if direction_bond is not None:
                direction_bond.SetBondDir(Chem.BondDir.NONE)
        bond.SetStereo(Chem.BondStereo.STEREONONE)
    return projected


def stereo_abstraction_projection(
    molecule: Chem.Mol,
    cleared_features: Iterable[StereoFeature],
) -> StereoAbstractionProjection:
    """Project exactly one explicitly requested stereo abstraction level.

    The caller supplies the feature set selected by a versioned chemistry rule.
    This function deliberately does not discover or enumerate other subsets of
    the molecule's stereochemistry.
    """

    source = Chem.Mol(molecule)
    source.RemoveAllConformers()
    requested = frozenset(cleared_features)
    if not requested:
        raise StereoAbstractionError("at least one stereo feature must be cleared")
    assigned = assigned_stereo_features(source)
    missing = requested - set(assigned)
    if missing:
        raise StereoAbstractionError(
            "requested stereo features are not assigned: "
            + ", ".join(sorted(feature.key for feature in missing))
        )
    selected = tuple(feature for feature in assigned if feature in requested)
    return StereoAbstractionProjection(
        cleared_features=selected,
        molecule=clear_stereo_features(source, selected),
    )


def _map_free_copy(molecule: Chem.Mol) -> Chem.Mol:
    result = Chem.Mol(molecule)
    for atom in result.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    return result


def _is_assigned_atom_stereo(atom: Chem.Atom) -> bool:
    return atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED


def _is_assigned_bond_stereo(bond: Chem.Bond) -> bool:
    return bond.GetStereo() in _ASSIGNED_BOND_STEREO


def find_topology_matches(
    specific: Chem.Mol,
    general: Chem.Mol,
) -> tuple[tuple[int, ...], ...]:
    """Return stereo-aware atom matches from a general graph to a concrete graph.

    The returned tuple is indexed by atom order in ``general`` and contains
    atom indices in ``specific``.  Topology identity is still constrained to
    the same atom and bond counts: this is a graph-inclusion operation for
    stereo abstraction, not a way to treat a different connectivity or a
    fragment as the same reaction participant.  ``uniquify=True`` preserves
    distinct legal matches while removing duplicate automorphism reports.
    """

    specific_graph = _map_free_copy(specific)
    general_graph = _map_free_copy(general)
    if specific_graph.GetNumAtoms() != general_graph.GetNumAtoms():
        return ()
    if specific_graph.GetNumBonds() != general_graph.GetNumBonds():
        return ()
    matches = specific_graph.GetSubstructMatches(
        general_graph,
        useChirality=True,
        uniquify=True,
    )
    return tuple(sorted(tuple(int(index) for index in match) for match in matches))


def find_topology_match(
    specific: Chem.Mol,
    general: Chem.Mol,
) -> tuple[int, ...] | None:
    """Return one deterministic general-to-specific graph match, if present."""

    matches = find_topology_matches(specific, general)
    return matches[0] if matches else None


def find_stereo_abstraction_match(
    specific: Chem.Mol,
    general: Chem.Mol,
) -> StereoAbstractionMatch | None:
    """Check whether ``specific`` is a strict stereo specialization of ``general``.

    The general graph is the RDKit query and the specific graph is the target.
    RDKit enforces all stereo constraints that remain specified in the query;
    unspecified query stereo can match an assigned target.  A custom delta
    check then requires at least one assigned target feature to be omitted from
    the general graph.
    """

    specific_graph = _map_free_copy(specific)
    general_graph = _map_free_copy(general)
    matches = find_topology_matches(specific_graph, general_graph)
    for match in sorted(matches):
        abstracted_atoms = tuple(
            general_atom_index
            for general_atom_index, specific_atom_index in enumerate(match)
            if not _is_assigned_atom_stereo(general_graph.GetAtomWithIdx(general_atom_index))
            and _is_assigned_atom_stereo(specific_graph.GetAtomWithIdx(specific_atom_index))
        )
        abstracted_bonds: list[int] = []
        for general_bond in general_graph.GetBonds():  # type: ignore[no-untyped-call]
            specific_bond = specific_graph.GetBondBetweenAtoms(
                match[general_bond.GetBeginAtomIdx()],
                match[general_bond.GetEndAtomIdx()],
            )
            if specific_bond is None:
                break
            if not _is_assigned_bond_stereo(general_bond) and _is_assigned_bond_stereo(
                specific_bond
            ):
                abstracted_bonds.append(general_bond.GetIdx())
        else:
            result = StereoAbstractionMatch(
                general_to_specific_atom_indices=tuple(int(index) for index in match),
                abstracted_atom_indices=abstracted_atoms,
                abstracted_bond_indices=tuple(abstracted_bonds),
            )
            if result.abstracted_feature_count > 0:
                return result
    return None


def persist_stereo_abstraction(
    session: Session,
    specific_topology: MolecularTopology,
    general_topology: MolecularTopology,
    *,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    abstraction_metadata: dict[str, Any] | None = None,
) -> MolecularTopologyAbstraction:
    """Validate and idempotently persist one directed abstraction edge."""

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    general_id = _require_id(general_topology, label="general MolecularTopology")
    if specific_id == general_id:
        raise StereoAbstractionError("specific and general topology must be different")
    if not general_topology.is_stereo_abstraction_upstream:
        raise StereoAbstractionError(
            "general topology is not marked as a stereo-abstraction upstream"
        )
    match = find_stereo_abstraction_match(specific_topology.mol, general_topology.mol)
    if match is None:
        raise StereoAbstractionError(
            "specific topology is not a strict stereo specialization of general topology"
        )
    # Edges are stored as ``specific -> general``.  A cycle would therefore
    # already exist when the proposed general node is reachable below the
    # proposed specific node.  The opposite direction is the normal
    # idempotent case: the specific topology is already below this general
    # topology and the existing edge should simply be reused below.
    if general_id in specialized_topology_ids(
        session,
        specific_topology,
        abstraction_policy_version=abstraction_policy_version,
    ):
        raise StereoAbstractionError("stereo abstraction edges must form an acyclic graph")
    _acquire_identity_locks(
        session,
        (
            "molecular_topology_abstraction",
            specific_id,
            general_id,
            abstraction_policy_version,
        ),
    )
    existing = session.exec(
        select(MolecularTopologyAbstraction).where(
            MolecularTopologyAbstraction.specific_topology_id == specific_id,
            MolecularTopologyAbstraction.general_topology_id == general_id,
            MolecularTopologyAbstraction.abstraction_policy_version == abstraction_policy_version,
        )
    ).first()
    if existing is not None:
        return existing
    for pending in session.info.get("_fast_pending_entities", ()):
        if not isinstance(pending, MolecularTopologyAbstraction):
            continue
        if (
            pending.specific_topology_id == specific_id
            and pending.general_topology_id == general_id
            and pending.abstraction_policy_version == abstraction_policy_version
        ):
            return pending
    metadata = match.metadata()
    if abstraction_metadata:
        metadata["caller_metadata"] = dict(abstraction_metadata)
    edge = _new_entity(
        session,
        MolecularTopologyAbstraction,
        specific_topology=specific_topology,
        general_topology=general_topology,
        abstraction_policy_version=abstraction_policy_version,
        abstraction_metadata=metadata,
    )
    _flush_new_entity(session, edge, label="MolecularTopologyAbstraction")
    return edge


def find_upstream_topologies(
    session: Session,
    specific_topology: MolecularTopology,
    *,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    candidate_topologies: Iterable[MolecularTopology] = (),
) -> tuple[MolecularTopology, ...]:
    """Find existing marked upstream topologies for one specific topology.

    Candidate lookup is deliberately restricted to the same formula and to
    rows explicitly marked as abstraction upstreams. If no marked topology
    matches, the reflexive fallback is the specific topology itself; no
    self-edge is written to the DAG.
    """

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    candidates_by_id: dict[UUID, MolecularTopology] = {}
    for candidate in candidate_topologies:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if (
            candidate_id != specific_id
            and candidate.is_stereo_abstraction_upstream
            and candidate.formula_id == specific_topology.formula_id
        ):
            candidates_by_id[candidate_id] = candidate
    for candidate in session.exec(
        select(MolecularTopology).where(
            col(MolecularTopology.formula_id) == specific_topology.formula_id,
            col(MolecularTopology.atom_count) == specific_topology.atom_count,
            col(MolecularTopology.formal_charge) == specific_topology.formal_charge,
            col(MolecularTopology.is_stereo_abstraction_upstream).is_(True),
            col(MolecularTopology.id) != specific_id,
        )
    ).all():
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        candidates_by_id[candidate_id] = candidate

    matches = tuple(
        candidate
        for candidate in sorted(
            candidates_by_id.values(),
            key=lambda item: (
                item.graph_hash,
                str(_require_id(item, label="MolecularTopology")),
            ),
        )
        if find_stereo_abstraction_match(specific_topology.mol, candidate.mol) is not None
    )
    return matches or (specific_topology,)


def ensure_topology_upstreams(
    session: Session,
    specific_topology: MolecularTopology,
    *,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    candidate_topologies: Iterable[MolecularTopology] = (),
    abstraction_metadata: dict[str, Any] | None = None,
) -> tuple[MolecularTopology, ...]:
    """Register all matching marked upstreams, or return the topology itself.

    The returned tuple is the topology's effective upstream set. A singleton
    containing the specific topology is the deterministic no-match fallback,
    not a persisted self-loop.
    """

    upstreams = find_upstream_topologies(
        session,
        specific_topology,
        abstraction_policy_version=abstraction_policy_version,
        candidate_topologies=candidate_topologies,
    )
    if upstreams == (specific_topology,):
        return upstreams
    for upstream in upstreams:
        persist_stereo_abstraction(
            session,
            specific_topology,
            upstream,
            abstraction_policy_version=abstraction_policy_version,
            abstraction_metadata=abstraction_metadata,
        )
    return upstreams


def _pending_abstraction_entities(session: Session) -> tuple[MolecularTopologyAbstraction, ...]:
    """Return abstraction edges queued by the fast persistence path."""

    return tuple(
        entity
        for entity in (
            *tuple(session.new),
            *tuple(session.info.get("_fast_pending_entities", ())),
        )
        if isinstance(entity, MolecularTopologyAbstraction)
    )


def _topology_reaches_general(
    general_by_specific: dict[UUID, set[UUID]],
    specific_topology_id: UUID,
    general_topology_id: UUID,
) -> bool:
    """Check the existing directed DAG before adding a redundant edge."""

    reached: set[UUID] = set()
    frontier = [specific_topology_id]
    while frontier:
        current_id = frontier.pop()
        if current_id in reached:
            continue
        reached.add(current_id)
        if current_id == general_topology_id:
            return True
        frontier.extend(general_by_specific.get(current_id, ()))
    return False


def backfill_stereo_abstraction_downstreams(
    session: Session,
    general_topology: MolecularTopology,
    *,
    candidate_topologies: Iterable[MolecularTopology] = (),
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    abstraction_metadata: dict[str, Any] | None = None,
) -> tuple[MolecularTopologyAbstraction, ...]:
    """Attach already-materialized specializations to a new general topology.

    Topology creation is intentionally incremental: this function never
    generates hypothetical stereoisomers.  It only repairs the reverse side
    of the creation-order case where a concrete topology was stored before
    the abstraction topology became available.
    """

    general_id = _require_id(general_topology, label="general MolecularTopology")
    if not general_topology.is_stereo_abstraction_upstream:
        raise StereoAbstractionError(
            "general topology is not marked as a stereo-abstraction upstream"
        )

    candidates_by_id: dict[UUID, MolecularTopology] = {}
    for candidate in candidate_topologies:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if candidate_id != general_id:
            candidates_by_id[candidate_id] = candidate
    for candidate in session.exec(
        select(MolecularTopology).where(
            col(MolecularTopology.formula_id) == general_topology.formula_id,
            col(MolecularTopology.atom_count) == general_topology.atom_count,
            col(MolecularTopology.formal_charge) == general_topology.formal_charge,
            col(MolecularTopology.id) != general_id,
        )
    ).all():
        candidates_by_id[_require_id(candidate, label="candidate MolecularTopology")] = candidate

    matches = []
    for candidate in candidates_by_id.values():
        match = find_stereo_abstraction_match(candidate.mol, general_topology.mol)
        if match is not None:
            matches.append((match.abstracted_feature_count, candidate, match))
    # Add the nearest materialized level first.  This preserves the intended
    # abstraction chain and avoids a redundant direct edge from a two-centre
    # topology to a zero-centre topology when a one-centre path already exists.
    matches.sort(
        key=lambda item: (
            item[0],
            item[1].graph_hash,
            str(_require_id(item[1], label="candidate MolecularTopology")),
        )
    )
    pending_edges = list(_pending_abstraction_entities(session))
    rows = session.exec(
        select(
            MolecularTopologyAbstraction.specific_topology_id,
            MolecularTopologyAbstraction.general_topology_id,
        )
        .join(
            MolecularTopology,
            col(MolecularTopology.id) == col(MolecularTopologyAbstraction.specific_topology_id),
        )
        .where(
            col(MolecularTopology.formula_id) == general_topology.formula_id,
            col(MolecularTopology.atom_count) == general_topology.atom_count,
            col(MolecularTopology.formal_charge) == general_topology.formal_charge,
            col(MolecularTopologyAbstraction.abstraction_policy_version)
            == abstraction_policy_version,
        )
    ).all()
    general_by_specific: dict[UUID, set[UUID]] = {}
    for specific_id, parent_id in rows:
        general_by_specific.setdefault(specific_id, set()).add(parent_id)
    for edge in pending_edges:
        edge_specific_id = getattr(edge, "specific_topology_id", None)
        edge_general_id = getattr(edge, "general_topology_id", None)
        if (
            isinstance(edge_specific_id, UUID)
            and isinstance(edge_general_id, UUID)
            and edge.abstraction_policy_version == abstraction_policy_version
        ):
            general_by_specific.setdefault(edge_specific_id, set()).add(edge_general_id)
    edges: list[MolecularTopologyAbstraction] = []
    for _feature_count, candidate, _match in matches:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if _topology_reaches_general(
            general_by_specific,
            candidate_id,
            general_id,
        ):
            continue
        edge = persist_stereo_abstraction(
            session,
            candidate,
            general_topology,
            abstraction_policy_version=abstraction_policy_version,
            abstraction_metadata={
                **(abstraction_metadata or {}),
                "backfill_existing_downstream": True,
            },
        )
        edges.append(edge)
        general_by_specific.setdefault(candidate_id, set()).add(general_id)
    return tuple(edges)


def persist_stereo_abstraction_projection(
    session: Session,
    specific_topology: MolecularTopology,
    cleared_features: Iterable[StereoFeature],
    *,
    context: Any | None = None,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    abstraction_metadata: dict[str, Any] | None = None,
) -> tuple[MolecularTopology, MolecularTopologyAbstraction]:
    """Materialize one requested abstraction node and its directed edge.

    This is the lazy entry point for topology ingestion and logical-reaction
    creation. It creates one projected topology only; no sibling or transitive
    stereo projections are generated implicitly.
    """

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    projection = stereo_abstraction_projection(specific_topology.mol, cleared_features)
    normalized = normalize_topology(
        projection.molecule,
        add_hydrogens=False,
        reconstruction_method=STEREO_ABSTRACTION_RECONSTRUCTION_METHOD,
        reconstruction_version=abstraction_policy_version,
        reconstruction_metadata={
            "source_topology_id": str(specific_id),
            "cleared_feature_keys": [feature.key for feature in projection.cleared_features],
            "topology_source_trusted": True,
            "stereo_abstraction": True,
            "is_stereo_abstraction_upstream": True,
        },
    )
    persisted = persist_molecular_topology(session, normalized, context=context)
    edge = persist_stereo_abstraction(
        session,
        specific_topology,
        persisted.topology,
        abstraction_policy_version=abstraction_policy_version,
        abstraction_metadata=abstraction_metadata,
    )
    context_candidates = tuple(
        topology
        for topology in getattr(context, "topologies_by_identity", {}).values()
        if isinstance(topology, MolecularTopology)
    )
    backfill_stereo_abstraction_downstreams(
        session,
        persisted.topology,
        candidate_topologies=context_candidates,
        abstraction_policy_version=abstraction_policy_version,
        abstraction_metadata=abstraction_metadata,
    )
    if context is not None:
        # A concrete topology may have been resolved before this abstraction
        # appeared in the same ingestion context.  Do not let a cached
        # reflexive upstream result hide the newly repaired DAG edge.
        getattr(context, "topology_upstreams_by_key", {}).clear()
    return persisted.topology, edge


def specialized_topology_ids(
    session: Session,
    general_topology: MolecularTopology | UUID,
    *,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
) -> tuple[UUID, ...]:
    """Return every topology reachable below a general topology in the DAG.

    Edges are stored as ``specific -> general`` because that makes the
    specialization claim explicit on the row.  Traversal starts at the
    general endpoint and follows the reverse direction, so a logical topology
    can enumerate one-centre and multi-centre concrete variants alike.
    """

    root_id = (
        general_topology
        if isinstance(general_topology, UUID)
        else _require_id(general_topology, label="general MolecularTopology")
    )
    edge_table = cast(Any, MolecularTopologyAbstraction).__table__
    seed = (
        select(
            edge_table.c.general_topology_id.label("ancestor_id"),
            edge_table.c.specific_topology_id.label("descendant_id"),
        )
        .where(
            edge_table.c.general_topology_id == root_id,
            edge_table.c.abstraction_policy_version == abstraction_policy_version,
        )
        .cte("molecular_topology_specializations", recursive=True)
    )
    recursive_term = select(
        seed.c.ancestor_id,
        edge_table.c.specific_topology_id.label("descendant_id"),
    ).join(
        edge_table,
        (edge_table.c.general_topology_id == seed.c.descendant_id)
        & (edge_table.c.abstraction_policy_version == abstraction_policy_version),
    )
    # UNION (rather than UNION ALL) also terminates safely if a manually
    # repaired database contains a cycle; normal writes reject cycles below.
    reachable = seed.union(recursive_term)
    rows = session.exec(
        select(reachable.c.descendant_id).distinct().order_by(reachable.c.descendant_id)
    ).all()
    return tuple(row if isinstance(row, UUID) else row[0] for row in rows)


def specialized_topologies(
    session: Session,
    general_topology: MolecularTopology | UUID,
    *,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    include_general: bool = False,
) -> tuple[MolecularTopology, ...]:
    """Load every topology reachable below a general topology."""

    root_id = (
        general_topology
        if isinstance(general_topology, UUID)
        else _require_id(general_topology, label="general MolecularTopology")
    )
    topology_ids = specialized_topology_ids(
        session,
        root_id,
        abstraction_policy_version=abstraction_policy_version,
    )
    if include_general:
        topology_ids = (root_id, *topology_ids)
    if not topology_ids:
        return ()
    return tuple(
        session.exec(
            select(MolecularTopology)
            .where(col(MolecularTopology.id).in_(topology_ids))
            .order_by(col(MolecularTopology.graph_hash), col(MolecularTopology.id))
        ).all()
    )


__all__ = [
    "STEREO_ABSTRACTION_POLICY_VERSION",
    "STEREO_ABSTRACTION_RECONSTRUCTION_METHOD",
    "StereoAbstractionError",
    "StereoAbstractionMatch",
    "StereoAbstractionProjection",
    "StereoFeature",
    "assigned_stereo_features",
    "backfill_stereo_abstraction_downstreams",
    "clear_stereo_features",
    "ensure_topology_upstreams",
    "find_topology_match",
    "find_topology_matches",
    "find_stereo_abstraction_match",
    "find_upstream_topologies",
    "persist_stereo_abstraction",
    "persist_stereo_abstraction_projection",
    "specialized_topologies",
    "specialized_topology_ids",
    "stereo_abstraction_projection",
]
