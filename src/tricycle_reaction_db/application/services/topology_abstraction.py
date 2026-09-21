"""Versioned stereo-configuration abstraction for molecular topologies.

The abstraction relation is directed from a more specific topology to a more
general topology.  A molecule with multiple stereo features therefore forms a
small DAG: each edge removes one feature, while the persisted topology rows
remain independently reusable molecular identities.
"""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, cast
from uuid import UUID

from rdkit import Chem
from sqlalchemy import case, literal, or_
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services._persistence import (
    _acquire_identity_locks,
    _flush_new_entity,
    _new_entity,
    _project_owner_predicate,
    _require_id,
)
from tricycle_reaction_db.application.services.molecular_geometry import (
    persist_molecular_topology,
)
from tricycle_reaction_db.application.services.rdkit_graph_matching import (
    get_substruct_matches,
)
from tricycle_reaction_db.core.chemistry_config import (
    STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION,
    STEREO_ABSTRACTION_POLICY_VERSION,
    STEREO_ABSTRACTION_RECONSTRUCTION_METHOD,
)
from tricycle_reaction_db.db.models import MolecularTopology, MolecularTopologyAbstraction
from tricycle_reaction_db.ingestion.normalization import (
    _stereo_signatures_match,
    normalize_topology_with_mapping,
)

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
_PROJECTION_ATOM_MAPPING_PROVENANCE = "normalized_projection_atom_order_v1"
_ABSTRACTION_MAPPING_CACHE_KEY = "_stereo_abstraction_mapping_edges"
_ABSTRACTION_MAPPING_CACHE_MARKER_KEY = "_stereo_abstraction_mapping_edges_marker"


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
    projected = clear_stereo_features(source, selected)
    # Database-backed RDKit molecules can retain directional writer flags
    # whose control atoms no longer agree with BondStereo.GetStereoAtoms().
    # They are not independent stereo evidence: if left behind, the next
    # normalization pass may infer the just-cleared E/Z feature again from a
    # different pair of neighboring bonds. BondStereo is authoritative here;
    # serialization will regenerate directions from the retained features.
    for bond in projected.GetBonds():  # type: ignore[no-untyped-call]
        bond.SetBondDir(Chem.BondDir.NONE)
    return StereoAbstractionProjection(
        cleared_features=selected,
        molecule=projected,
    )


def _map_free_copy(molecule: Chem.Mol) -> Chem.Mol:
    result = Chem.Mol(molecule)
    for atom in result.GetAtoms():  # type: ignore[no-untyped-call]
        atom.SetAtomMapNum(0)
    return result


def _canonical_stereo_agnostic_graph(
    molecule: Chem.Mol,
) -> tuple[str, tuple[int, ...]] | None:
    """Return a canonical no-stereo signature and its atom traversal.

    Stereo abstraction candidates are already restricted to the same molecular
    topology.  They do not need RDKit to rediscover an arbitrary substructure
    embedding: a canonical no-stereo SMILES gives both a deterministic graph
    identity and the atom order used to build the correspondence.  Returning
    ``None`` for an unusual graph keeps the caller conservative without
    entering the unbounded native substructure matcher.
    """

    projected = _map_free_copy(molecule)
    for property_name in ("_smilesAtomOutputOrder", "_canonicalAtomRanks"):
        if projected.HasProp(property_name):
            projected.ClearProp(property_name)
    try:
        Chem.RemoveStereochemistry(projected)
        signature = Chem.MolToSmiles(
            projected,
            canonical=True,
            isomericSmiles=False,
            allHsExplicit=True,
        )
    except (RuntimeError, ValueError):
        return None
    raw_order = projected.GetPropsAsDict(includePrivate=True, includeComputed=True).get(
        "_smilesAtomOutputOrder"
    )
    if raw_order is None:
        return None
    order = tuple(int(index) for index in raw_order)
    if sorted(order) != list(range(projected.GetNumAtoms())):
        return None
    return signature, order


def _canonical_stereo_agnostic_atom_mapping(
    specific: Chem.Mol,
    general: Chem.Mol,
) -> tuple[int, ...] | None:
    """Build a general-to-specific mapping without graph substructure search."""

    if (
        specific.GetNumAtoms() != general.GetNumAtoms()
        or specific.GetNumBonds() != general.GetNumBonds()
    ):
        return None
    specific_graph = _canonical_stereo_agnostic_graph(specific)
    general_graph = _canonical_stereo_agnostic_graph(general)
    if specific_graph is None or general_graph is None:
        return None
    specific_signature, specific_order = specific_graph
    general_signature, general_order = general_graph
    if specific_signature != general_signature:
        return None
    mapping = [0] * general.GetNumAtoms()
    for canonical_position, general_index in enumerate(general_order):
        mapping[general_index] = specific_order[canonical_position]
    return tuple(mapping)


@lru_cache(maxsize=4096)
def _find_topology_matches_cached(
    specific_binary: bytes,
    general_binary: bytes,
) -> tuple[tuple[int, ...], ...]:
    """Run one stereo-aware match for a stable, map-free molecule pair."""

    specific_graph = Chem.Mol(cast(Any, specific_binary))
    general_graph = Chem.Mol(cast(Any, general_binary))
    return _find_topology_matches_on_graphs(specific_graph, general_graph)


def _find_topology_matches_on_graphs(
    specific_graph: Chem.Mol,
    general_graph: Chem.Mol,
) -> tuple[tuple[int, ...], ...]:
    """Run the uncached graph operation used by the compatibility fallback."""

    if specific_graph.GetNumAtoms() != general_graph.GetNumAtoms():
        return ()
    if specific_graph.GetNumBonds() != general_graph.GetNumBonds():
        return ()
    matches = get_substruct_matches(
        specific_graph,
        general_graph,
        use_chirality=True,
        hard_timeout_for_large_molecules=True,
    )
    return tuple(sorted(matches))


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

    # Avoid cloning/serializing molecules when the topology identity already
    # proves that a full graph match is impossible. This is especially useful
    # for reverse membership lookups, where scalar SQL predicates are followed
    # by this Python fallback for a bounded candidate set.
    if specific.GetNumAtoms() != general.GetNumAtoms():
        return ()
    if specific.GetNumBonds() != general.GetNumBonds():
        return ()
    specific_graph = _map_free_copy(specific)
    general_graph = _map_free_copy(general)
    try:
        return _find_topology_matches_cached(
            specific_graph.ToBinary(),
            general_graph.ToBinary(),
        )
    except (RuntimeError, TypeError, ValueError):
        # Keep the historical behavior for unusual/unserializable RDKit
        # molecules. They are rare and should not make the cache a new source
        # of ingestion failures.
        return _find_topology_matches_on_graphs(specific_graph, general_graph)


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
    mapping = _canonical_stereo_agnostic_atom_mapping(specific_graph, general_graph)
    if mapping is None:
        return None
    # The canonical no-stereo graph provides the correspondence.  Reuse the
    # linear validator so all atom/bond electronic fields and retained stereo
    # constraints are checked before an abstraction edge is accepted.
    return _stereo_abstraction_match_for_known_atom_mapping(
        specific_graph,
        general_graph,
        mapping,
    )


def _local_atom_stereo_signature(atom: Chem.Atom, identities: tuple[int, ...]) -> str:
    """Compare coordination parity on a bounded, provenance-labelled star.

    CIP labels need not exist for square-planar/other metal stereocentres and
    can change when remote stereo is removed. Unique dummy isotope labels
    encode the already-known ligand correspondence, not chemical isotopes.
    Bond insertion follows the source neighbour order, preserving RDKit's
    tetrahedral tag or non-tetrahedral permutation without a graph search.
    """
    star = Chem.RWMol()
    center = Chem.Atom(atom)
    center.SetAtomMapNum(0)
    for name in list(center.GetPropNames(includePrivate=True, includeComputed=True)):
        if name != "_chiralPermutation":
            center.ClearProp(name)
    star.AddAtom(center)
    for neighbor in atom.GetNeighbors():
        ligand = Chem.Atom(0)
        ligand.SetIsotope(identities[neighbor.GetIdx()] + 1)
        star.AddBond(0, star.AddAtom(ligand), Chem.BondType.SINGLE)
    star.UpdatePropertyCache(strict=False)
    return Chem.MolToSmiles(star, canonical=True, isomericSmiles=True)


def _stereo_abstraction_match_for_known_atom_mapping(
    specific: Chem.Mol,
    general: Chem.Mol,
    general_to_specific_atom_indices: tuple[int, ...],
) -> StereoAbstractionMatch | None:
    """Validate a provenance-derived atom correspondence in linear time.

    This is for projections made from a known source molecule: normalization
    returns the source-to-canonical atom permutation, so there is no reason to
    ask RDKit to rediscover that correspondence with a substructure search.
    If normalization changed any graph facts or stereo that this check cannot
    verify directly, callers should use the bounded graph-matching fallback.
    """

    atom_count = specific.GetNumAtoms()
    if (
        atom_count != general.GetNumAtoms()
        or len(general_to_specific_atom_indices) != atom_count
        or sorted(general_to_specific_atom_indices) != list(range(atom_count))
        or specific.GetNumBonds() != general.GetNumBonds()
    ):
        return None

    abstracted_atoms: list[int] = []
    for general_index, specific_index in enumerate(general_to_specific_atom_indices):
        general_atom = general.GetAtomWithIdx(general_index)
        specific_atom = specific.GetAtomWithIdx(specific_index)
        general_signature = (
            general_atom.GetAtomicNum(),
            general_atom.GetIsotope(),
            general_atom.GetFormalCharge(),
            general_atom.GetNumRadicalElectrons(),
            general_atom.GetNumExplicitHs(),
            general_atom.GetNoImplicit(),
            general_atom.GetIsAromatic(),
        )
        specific_signature = (
            specific_atom.GetAtomicNum(),
            specific_atom.GetIsotope(),
            specific_atom.GetFormalCharge(),
            specific_atom.GetNumRadicalElectrons(),
            specific_atom.GetNumExplicitHs(),
            specific_atom.GetNoImplicit(),
            specific_atom.GetIsAromatic(),
        )
        if general_signature != specific_signature:
            return None

        general_has_stereo = _is_assigned_atom_stereo(general_atom)
        specific_has_stereo = _is_assigned_atom_stereo(specific_atom)
        if general_has_stereo and not specific_has_stereo:
            return None
        if general_has_stereo and specific_has_stereo:
            if _local_atom_stereo_signature(
                general_atom, general_to_specific_atom_indices
            ) != _local_atom_stereo_signature(specific_atom, tuple(range(atom_count))):
                return None
        elif specific_has_stereo:
            abstracted_atoms.append(general_index)

    specific_bonds = {
        frozenset((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())): bond
        # Use frozen normalized bond/control pairs, never reassigned CIP state.
        for bond in specific.GetBonds()  # type: ignore[no-untyped-call]
    }
    abstracted_bonds: list[int] = []
    for general_bond in general.GetBonds():  # type: ignore[no-untyped-call]
        mapped_begin = general_to_specific_atom_indices[general_bond.GetBeginAtomIdx()]
        mapped_end = general_to_specific_atom_indices[general_bond.GetEndAtomIdx()]
        specific_bond = specific_bonds.get(frozenset((mapped_begin, mapped_end)))
        if specific_bond is None:
            return None
        if (
            general_bond.GetBondType() != specific_bond.GetBondType()
            or general_bond.GetIsAromatic() != specific_bond.GetIsAromatic()
            or general_bond.GetIsConjugated() != specific_bond.GetIsConjugated()
        ):
            return None

        general_has_stereo = _is_assigned_bond_stereo(general_bond)
        specific_has_stereo = _is_assigned_bond_stereo(specific_bond)
        if general_has_stereo and not specific_has_stereo:
            return None
        if general_has_stereo and specific_has_stereo:
            general_stereo_atoms = tuple(int(index) for index in general_bond.GetStereoAtoms())
            specific_stereo_atoms = tuple(int(index) for index in specific_bond.GetStereoAtoms())
            if len(general_stereo_atoms) != 2 or len(specific_stereo_atoms) != 2:
                return None
            expected_stereo_atoms = tuple(
                general_to_specific_atom_indices[index] for index in general_stereo_atoms
            )
            if (mapped_begin, mapped_end) != (
                specific_bond.GetBeginAtomIdx(),
                specific_bond.GetEndAtomIdx(),
            ):
                expected_stereo_atoms = tuple(reversed(expected_stereo_atoms))
            aliases = {
                Chem.BondStereo.STEREOCIS: Chem.BondStereo.STEREOZ,
                Chem.BondStereo.STEREOTRANS: Chem.BondStereo.STEREOE,
            }
            general_stereo = aliases.get(general_bond.GetStereo(), general_bond.GetStereo())
            specific_stereo = aliases.get(specific_bond.GetStereo(), specific_bond.GetStereo())
            if general_stereo in (Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ):
                order = (specific_bond.GetBeginAtomIdx(), specific_bond.GetEndAtomIdx())
                edge = frozenset(order)
                if not _stereo_signatures_match(
                    {
                        edge: (
                            general_stereo,
                            (expected_stereo_atoms[0], expected_stereo_atoms[1]),
                            order,
                        )
                    },
                    {edge: (specific_stereo, specific_stereo_atoms, order)},
                ):
                    return None
            elif (
                general_stereo != specific_stereo or expected_stereo_atoms != specific_stereo_atoms
            ):
                return None
        elif specific_has_stereo:
            abstracted_bonds.append(general_bond.GetIdx())

    result = StereoAbstractionMatch(
        general_to_specific_atom_indices=general_to_specific_atom_indices,
        abstracted_atom_indices=tuple(abstracted_atoms),
        abstracted_bond_indices=tuple(abstracted_bonds),
    )
    return result if result.abstracted_feature_count > 0 else None


def _abstraction_mapping_cache_marker(session: Session) -> tuple[object, ...]:
    pending = session.info.get("_fast_pending_entities")
    pending_marker = (id(pending), len(pending)) if isinstance(pending, list) else (None, 0)
    return (
        id(session.get_transaction()),
        id(session.get_nested_transaction()),
        pending_marker,
        len(session.new),
    )


def _invalidate_abstraction_mapping_cache(session: Session) -> None:
    session.info.pop(_ABSTRACTION_MAPPING_CACHE_KEY, None)
    session.info.pop(_ABSTRACTION_MAPPING_CACHE_MARKER_KEY, None)


def _abstraction_edges_below(
    session: Session,
    general_topology_id: UUID,
    *,
    project_id: UUID,
    abstraction_policy_version: str,
) -> tuple[MolecularTopologyAbstraction, ...]:
    """Load one general topology's DAG edges once per active transaction."""

    cache_marker = _abstraction_mapping_cache_marker(session)
    cached_marker = session.info.get(_ABSTRACTION_MAPPING_CACHE_MARKER_KEY)
    cache = session.info.get(_ABSTRACTION_MAPPING_CACHE_KEY)
    if cached_marker != cache_marker or not isinstance(cache, dict):
        cache = {}
        session.info[_ABSTRACTION_MAPPING_CACHE_KEY] = cache
        session.info[_ABSTRACTION_MAPPING_CACHE_MARKER_KEY] = cache_marker
    cache_key = (project_id, general_topology_id, abstraction_policy_version)
    cached_edges = cache.get(cache_key)
    if isinstance(cached_edges, tuple):
        return cast(tuple[MolecularTopologyAbstraction, ...], cached_edges)

    edge_table = cast(Any, MolecularTopologyAbstraction).__table__
    seed = select(literal(general_topology_id).label("topology_id")).cte(
        "stereo_abstraction_mapping_nodes",
        recursive=True,
    )
    recursive_term = (
        select(edge_table.c.specific_topology_id.label("topology_id"))
        .join(seed, edge_table.c.general_topology_id == seed.c.topology_id)
        .where(
            edge_table.c.abstraction_policy_version == abstraction_policy_version,
            _project_owner_predicate(edge_table.c.project_id, project_id),
        )
    )
    reachable = seed.union(recursive_term)
    persisted_edges = tuple(
        session.exec(
            select(MolecularTopologyAbstraction)
            .join(
                reachable,
                col(MolecularTopologyAbstraction.general_topology_id) == reachable.c.topology_id,
            )
            .where(
                col(MolecularTopologyAbstraction.abstraction_policy_version)
                == abstraction_policy_version,
                _project_owner_predicate(
                    col(MolecularTopologyAbstraction.project_id),
                    project_id,
                ),
            )
        ).all()
    )
    edges = tuple(
        edge
        for edge in (*persisted_edges, *_pending_abstraction_entities(session))
        if edge.project_id == project_id
        and edge.abstraction_policy_version == abstraction_policy_version
    )

    # A query can autobegin the Session's transaction; store the result under
    # the post-query marker so the next participant in the batch can reuse it.
    current_marker = _abstraction_mapping_cache_marker(session)
    if session.info.get(_ABSTRACTION_MAPPING_CACHE_MARKER_KEY) != current_marker:
        cache = {}
        session.info[_ABSTRACTION_MAPPING_CACHE_KEY] = cache
        session.info[_ABSTRACTION_MAPPING_CACHE_MARKER_KEY] = current_marker
    elif len(cache) >= 64 and cache_key not in cache:
        cache.clear()
    cache[cache_key] = edges
    return edges


def topology_abstraction_mapping_witness(
    session: Session,
    specific_topology: MolecularTopology,
    general_topology: MolecularTopology,
    *,
    project_id: UUID | None = None,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    require_projection_provenance: bool = False,
    require_unique: bool = False,
) -> tuple[int, ...] | None:
    """Return a DAG-proven general-to-specific atom mapping, if available.

    Every edge stores a mapping from its general node's atom indices to its
    specific node's indices. Paths compose those mappings without molecular
    graph search. Generic persisted matches are enough to prove membership;
    atom-map transfer can require the stronger source-order projection
    provenance and uniqueness guarantees.
    """

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    general_id = _require_id(general_topology, label="general MolecularTopology")
    atom_count = general_topology.atom_count
    if (
        specific_topology.project_id != general_topology.project_id
        or specific_topology.atom_count != atom_count
        or specific_topology.project_id is None
    ):
        return None
    owner_project_id = specific_topology.project_id if project_id is None else project_id
    if owner_project_id != specific_topology.project_id:
        return None
    if specific_id == general_id:
        return tuple(range(atom_count))

    reachable_edges = _abstraction_edges_below(
        session,
        general_id,
        project_id=owner_project_id,
        abstraction_policy_version=abstraction_policy_version,
    )
    edges_by_general: dict[UUID, list[tuple[UUID, tuple[int, ...], bool]]] = {}
    for edge in reachable_edges:
        if (
            edge.project_id != owner_project_id
            or edge.abstraction_policy_version != abstraction_policy_version
        ):
            continue
        specific_edge_id = edge.specific_topology_id
        general_edge_id = edge.general_topology_id
        metadata = edge.abstraction_metadata
        if not isinstance(specific_edge_id, UUID) or not isinstance(general_edge_id, UUID):
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("match_schema_version") != STEREO_ABSTRACTION_MATCH_SCHEMA_VERSION:
            continue
        raw_mapping = metadata.get("general_to_specific_atom_indices")
        if not isinstance(raw_mapping, list) or len(raw_mapping) != atom_count:
            continue
        if any(not isinstance(index, int) or isinstance(index, bool) for index in raw_mapping):
            continue
        mapping = tuple(raw_mapping)
        if sorted(mapping) != list(range(atom_count)):
            continue
        caller_metadata = metadata.get("caller_metadata")
        trusted_mapping = (
            isinstance(caller_metadata, dict)
            and caller_metadata.get("atom_mapping_provenance")
            == _PROJECTION_ATOM_MAPPING_PROVENANCE
        )
        edges_by_general.setdefault(general_edge_id, []).append(
            (specific_edge_id, mapping, trusted_mapping)
        )

    initial_mapping = tuple(range(atom_count))
    frontier = deque([(general_id, initial_mapping, True)])
    visited: set[tuple[UUID, tuple[int, ...], bool]] = {(general_id, initial_mapping, True)}
    witnesses: set[tuple[int, ...]] = set()
    max_states = 4096
    while frontier:
        current_id, root_to_current, path_is_trusted = frontier.popleft()
        for child_id, current_to_child, edge_is_trusted in edges_by_general.get(current_id, ()):
            next_is_trusted = path_is_trusted and edge_is_trusted
            if require_projection_provenance and not next_is_trusted:
                continue
            composed = tuple(current_to_child[index] for index in root_to_current)
            if child_id == specific_id:
                witnesses.add(composed)
                if not require_unique:
                    return composed
                if len(witnesses) > 1:
                    return None
                continue
            state = (child_id, composed, next_is_trusted)
            if state in visited:
                continue
            visited.add(state)
            if len(visited) > max_states:
                # The provenance graph is unexpectedly large/ambiguous. The
                # caller can retain the bounded, exact matching fallback.
                return None
            frontier.append(state)
    return next(iter(witnesses)) if len(witnesses) == 1 else None


def persist_stereo_abstraction(
    session: Session,
    specific_topology: MolecularTopology,
    general_topology: MolecularTopology,
    *,
    project_id: UUID | None = None,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    abstraction_metadata: dict[str, Any] | None = None,
    known_match: StereoAbstractionMatch | None = None,
) -> MolecularTopologyAbstraction | None:
    """Validate and idempotently persist one directed abstraction edge."""

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    general_id = _require_id(general_topology, label="general MolecularTopology")
    specific_project_id = getattr(specific_topology, "project_id", None)
    general_project_id = getattr(general_topology, "project_id", None)
    owner_project_id = specific_project_id if project_id is None else project_id
    if specific_project_id != owner_project_id or general_project_id != owner_project_id:
        raise StereoAbstractionError("stereo abstraction endpoints must belong to the same project")
    if specific_id == general_id:
        # A normalized projection may remove only redundant stereo. This is
        # an identity operation, not a failed reaction and not a DAG self-edge.
        return None
    if not general_topology.is_stereo_abstraction_upstream:
        raise StereoAbstractionError(
            "general topology is not marked as a stereo-abstraction upstream"
        )

    verified_known_match = (
        _stereo_abstraction_match_for_known_atom_mapping(
            specific_topology.mol,
            general_topology.mol,
            known_match.general_to_specific_atom_indices,
        )
        if known_match is not None
        else None
    )
    _acquire_identity_locks(
        session,
        (
            "molecular_topology_abstraction",
            owner_project_id,
            specific_id,
            general_id,
            abstraction_policy_version,
        ),
    )

    def match_metadata(
        match: StereoAbstractionMatch,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = match.metadata()
        previous_caller_metadata = (
            previous.get("caller_metadata") if isinstance(previous, dict) else None
        )
        caller_metadata = (
            dict(previous_caller_metadata) if isinstance(previous_caller_metadata, dict) else {}
        )
        caller_metadata.update(abstraction_metadata or {})
        if verified_known_match is not None:
            caller_metadata["atom_mapping_provenance"] = _PROJECTION_ATOM_MAPPING_PROVENANCE
        else:
            caller_metadata.pop("atom_mapping_provenance", None)
        if caller_metadata:
            metadata["caller_metadata"] = caller_metadata
        return metadata

    existing = session.exec(
        select(MolecularTopologyAbstraction).where(
            MolecularTopologyAbstraction.specific_topology_id == specific_id,
            MolecularTopologyAbstraction.general_topology_id == general_id,
            MolecularTopologyAbstraction.abstraction_policy_version == abstraction_policy_version,
            _project_owner_predicate(
                MolecularTopologyAbstraction.project_id,
                owner_project_id,
            ),
        )
    ).first()
    if existing is not None:
        if verified_known_match is not None:
            existing.abstraction_metadata = match_metadata(
                verified_known_match,
                existing.abstraction_metadata,
            )
            _invalidate_abstraction_mapping_cache(session)
        return existing
    for pending in _pending_abstraction_entities(session):
        if not isinstance(pending, MolecularTopologyAbstraction):
            continue
        if (
            pending.project_id == owner_project_id
            and pending.specific_topology_id == specific_id
            and pending.general_topology_id == general_id
            and pending.abstraction_policy_version == abstraction_policy_version
        ):
            if verified_known_match is not None:
                pending.abstraction_metadata = match_metadata(
                    verified_known_match,
                    pending.abstraction_metadata,
                )
                _invalidate_abstraction_mapping_cache(session)
            return pending

    match = verified_known_match or find_stereo_abstraction_match(
        specific_topology.mol,
        general_topology.mol,
    )
    if match is None:
        raise StereoAbstractionError(
            "specific topology is not a strict stereo specialization of general topology"
        )
    # Edges are stored as ``specific -> general``. A cycle would exist when
    # the proposed general node is already reachable below the specific node.
    if general_id in specialized_topology_ids(
        session,
        specific_topology,
        project_id=owner_project_id,
        abstraction_policy_version=abstraction_policy_version,
    ):
        raise StereoAbstractionError("stereo abstraction edges must form an acyclic graph")

    edge = _new_entity(
        session,
        MolecularTopologyAbstraction,
        specific_topology=specific_topology,
        general_topology=general_topology,
        project_id=owner_project_id,
        abstraction_policy_version=abstraction_policy_version,
        abstraction_metadata=match_metadata(match),
    )
    _flush_new_entity(session, edge, label="MolecularTopologyAbstraction")
    _invalidate_abstraction_mapping_cache(session)
    return edge


def find_upstream_topologies(
    session: Session,
    specific_topology: MolecularTopology,
    *,
    project_id: UUID | None = None,
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
    specific_project_id = getattr(specific_topology, "project_id", None)
    owner_project_id = specific_project_id if project_id is None else project_id
    specific_graph_hash = getattr(specific_topology, "stereo_agnostic_graph_hash", None)
    candidates_by_id: dict[UUID, MolecularTopology] = {}
    for candidate in candidate_topologies:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if (
            candidate_id != specific_id
            and candidate.is_stereo_abstraction_upstream
            and candidate.formula_id == specific_topology.formula_id
            and (
                specific_graph_hash is None
                or getattr(candidate, "stereo_agnostic_graph_hash", None) == specific_graph_hash
            )
            and getattr(candidate, "project_id", None) == owner_project_id
        ):
            candidates_by_id[candidate_id] = candidate
    upstream_predicates = [
        col(MolecularTopology.formula_id) == specific_topology.formula_id,
        col(MolecularTopology.atom_count) == specific_topology.atom_count,
        col(MolecularTopology.formal_charge) == specific_topology.formal_charge,
        col(MolecularTopology.is_stereo_abstraction_upstream).is_(True),
        col(MolecularTopology.id) != specific_id,
        _project_owner_predicate(col(MolecularTopology.project_id), owner_project_id),
    ]
    if specific_graph_hash is not None:
        upstream_predicates.append(
            col(MolecularTopology.stereo_agnostic_graph_hash) == specific_graph_hash
        )
    for candidate in session.exec(select(MolecularTopology).where(*upstream_predicates)).all():
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        candidates_by_id[candidate_id] = candidate

    matches: list[MolecularTopology] = []
    for candidate in sorted(
        candidates_by_id.values(),
        key=lambda item: (
            item.graph_hash,
            str(_require_id(item, label="MolecularTopology")),
        ),
    ):
        # A previously validated DAG path is already a complete proof. Do not
        # rediscover the same atom correspondence for every repeated ingestion.
        if (
            topology_abstraction_mapping_witness(
                session,
                specific_topology,
                candidate,
                project_id=owner_project_id,
                abstraction_policy_version=abstraction_policy_version,
            )
            is not None
        ) or find_stereo_abstraction_match(specific_topology.mol, candidate.mol) is not None:
            matches.append(candidate)
    return tuple(matches) or (specific_topology,)


def ensure_topology_upstreams(
    session: Session,
    specific_topology: MolecularTopology,
    *,
    project_id: UUID | None = None,
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
        project_id=project_id,
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
            project_id=(
                getattr(specific_topology, "project_id", None) if project_id is None else project_id
            ),
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
    project_id: UUID | None = None,
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
    general_project_id = getattr(general_topology, "project_id", None)
    owner_project_id = general_project_id if project_id is None else project_id
    if general_project_id != owner_project_id:
        raise StereoAbstractionError("stereo abstraction topology has a different project owner")
    if not general_topology.is_stereo_abstraction_upstream:
        raise StereoAbstractionError(
            "general topology is not marked as a stereo-abstraction upstream"
        )

    general_graph_hash = getattr(general_topology, "stereo_agnostic_graph_hash", None)
    candidates_by_id: dict[UUID, MolecularTopology] = {}
    for candidate in candidate_topologies:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if (
            candidate_id != general_id
            and (
                general_graph_hash is None
                or getattr(candidate, "stereo_agnostic_graph_hash", None) == general_graph_hash
            )
            and getattr(candidate, "project_id", None) == owner_project_id
        ):
            candidates_by_id[candidate_id] = candidate
    downstream_predicates = [
        col(MolecularTopology.formula_id) == general_topology.formula_id,
        col(MolecularTopology.atom_count) == general_topology.atom_count,
        col(MolecularTopology.formal_charge) == general_topology.formal_charge,
        col(MolecularTopology.id) != general_id,
        _project_owner_predicate(col(MolecularTopology.project_id), owner_project_id),
    ]
    if general_graph_hash is not None:
        downstream_predicates.append(
            col(MolecularTopology.stereo_agnostic_graph_hash) == general_graph_hash
        )
    for candidate in session.exec(select(MolecularTopology).where(*downstream_predicates)).all():
        candidates_by_id[_require_id(candidate, label="candidate MolecularTopology")] = candidate

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
            _project_owner_predicate(
                col(MolecularTopologyAbstraction.project_id),
                owner_project_id,
            ),
            _project_owner_predicate(col(MolecularTopology.project_id), owner_project_id),
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
            and edge.project_id == owner_project_id
        ):
            general_by_specific.setdefault(edge_specific_id, set()).add(edge_general_id)

    matches = []
    for candidate in candidates_by_id.values():
        # Existing DAG descendants were already validated when their edge was
        # written; backfill should only run expensive chemistry matching for
        # disconnected candidates that may need a new relation.
        if _topology_reaches_general(
            general_by_specific,
            _require_id(candidate, label="candidate MolecularTopology"),
            general_id,
        ):
            continue
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
    edges: list[MolecularTopologyAbstraction] = []
    for _feature_count, candidate, _match in matches:
        candidate_id = _require_id(candidate, label="candidate MolecularTopology")
        if _topology_reaches_general(
            general_by_specific,
            candidate_id,
            general_id,
        ):
            continue
        persisted_edge = persist_stereo_abstraction(
            session,
            candidate,
            general_topology,
            project_id=owner_project_id,
            abstraction_policy_version=abstraction_policy_version,
            abstraction_metadata={
                **(abstraction_metadata or {}),
                "backfill_existing_downstream": True,
            },
        )
        if persisted_edge is not None:
            edges.append(persisted_edge)
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
) -> tuple[MolecularTopology, MolecularTopologyAbstraction | None]:
    """Materialize one requested abstraction node and its directed edge.

    This is the lazy entry point for topology ingestion and logical-reaction
    creation. It creates one projected topology only; no sibling or transitive
    stereo projections are generated implicitly.
    """

    specific_id = _require_id(specific_topology, label="specific MolecularTopology")
    projection = stereo_abstraction_projection(specific_topology.mol, cleared_features)
    normalized, source_to_general = normalize_topology_with_mapping(
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
    if normalized.topology.graph_hash == specific_topology.graph_hash:
        return specific_topology, None
    persisted = persist_molecular_topology(
        session,
        normalized,
        context=context,
        register_upstream=False,
    )
    general_to_specific = [0] * len(source_to_general)
    for specific_index, general_index in enumerate(source_to_general):
        general_to_specific[general_index] = specific_index
    known_match = _stereo_abstraction_match_for_known_atom_mapping(
        specific_topology.mol,
        persisted.topology.mol,
        tuple(general_to_specific),
    )
    edge = persist_stereo_abstraction(
        session,
        specific_topology,
        persisted.topology,
        project_id=(
            context.project_id
            if context is not None
            else getattr(persisted.topology, "project_id", None)
        ),
        abstraction_policy_version=abstraction_policy_version,
        abstraction_metadata=abstraction_metadata,
        known_match=known_match,
    )
    context_candidates = tuple(
        topology
        for topology in getattr(context, "topologies_by_identity", {}).values()
        if isinstance(topology, MolecularTopology)
    )
    backfill_stereo_abstraction_downstreams(
        session,
        persisted.topology,
        project_id=(
            context.project_id
            if context is not None
            else getattr(persisted.topology, "project_id", None)
        ),
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
    project_id: UUID | None = None,
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
    owner_project_id = project_id
    if owner_project_id is None:
        if isinstance(general_topology, UUID):
            owner_project_id = session.exec(
                select(MolecularTopology.project_id).where(MolecularTopology.id == root_id)
            ).first()
        else:
            owner_project_id = getattr(general_topology, "project_id", None)
    edge_table = cast(Any, MolecularTopologyAbstraction).__table__
    seed = (
        select(
            edge_table.c.general_topology_id.label("ancestor_id"),
            edge_table.c.specific_topology_id.label("descendant_id"),
        )
        .where(
            edge_table.c.general_topology_id == root_id,
            edge_table.c.abstraction_policy_version == abstraction_policy_version,
            _project_owner_predicate(edge_table.c.project_id, owner_project_id),
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
    recursive_term = recursive_term.where(
        _project_owner_predicate(edge_table.c.project_id, owner_project_id)
    )
    # UNION (rather than UNION ALL) also terminates safely if a manually
    # repaired database contains a cycle; normal writes reject cycles below.
    reachable = seed.union(recursive_term)
    rows = session.exec(
        select(reachable.c.descendant_id).distinct().order_by(reachable.c.descendant_id)
    ).all()
    return tuple(row if isinstance(row, UUID) else row[0] for row in rows)


def topology_dag_components_by_root(
    session: Session,
    topology_ids: Iterable[UUID],
    *,
    project_id: UUID,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
) -> dict[UUID, tuple[UUID, ...]]:
    """Return each root's bounded inheritance component in one recursive query.

    The abstraction edge is stored as ``specific -> general``. Compatibility
    lookup needs both directions because a source geometry may be a strict
    specialization, a generalization, or a sibling reached through their
    shared abstraction parent. The root identity is carried through the
    recursive CTE so components stay separate even when several reactions are
    processed in one batch.
    """

    roots = tuple(sorted(set(topology_ids), key=str))
    if not roots:
        return {}
    edge_table = cast(Any, MolecularTopologyAbstraction).__table__
    seed: Any = (
        select(
            col(MolecularTopology.id).label("root_id"),
            col(MolecularTopology.id).label("topology_id"),
        )
        .where(
            col(MolecularTopology.id).in_(roots),
            col(MolecularTopology.project_id) == project_id,
        )
        .cte("molecular_topology_dag_components_by_root", recursive=True)
    )
    recursive_term = (
        select(
            seed.c.root_id,
            case(
                (
                    edge_table.c.specific_topology_id == seed.c.topology_id,
                    edge_table.c.general_topology_id,
                ),
                else_=edge_table.c.specific_topology_id,
            ).label("topology_id"),
        )
        .select_from(seed)
        .join(
            edge_table,
            or_(
                edge_table.c.specific_topology_id == seed.c.topology_id,
                edge_table.c.general_topology_id == seed.c.topology_id,
            ),
        )
        .where(
            edge_table.c.project_id == project_id,
            edge_table.c.abstraction_policy_version == abstraction_policy_version,
        )
    )
    reachable = seed.union(recursive_term)
    rows = session.exec(
        select(reachable.c.root_id, reachable.c.topology_id).select_from(reachable).distinct()
    ).all()
    components: dict[UUID, set[UUID]] = {root_id: set() for root_id in roots}
    for row in rows:
        root_id = cast(UUID | None, row[0])
        topology_id = cast(UUID | None, row[1])
        if isinstance(root_id, UUID) and isinstance(topology_id, UUID):
            components.setdefault(root_id, set()).add(topology_id)
    return {
        root_id: tuple(sorted(component_ids, key=str))
        for root_id, component_ids in components.items()
    }


def topology_dag_component_ids(
    session: Session,
    topology_ids: Iterable[UUID],
    *,
    project_id: UUID,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
) -> tuple[UUID, ...]:
    """Return the union of the bounded components around the supplied roots.

    Use :func:`topology_dag_components_by_root` when candidate ownership must
    remain scoped to an individual root.
    """

    components = topology_dag_components_by_root(
        session,
        topology_ids,
        project_id=project_id,
        abstraction_policy_version=abstraction_policy_version,
    )
    return tuple(
        sorted(
            {topology_id for component_ids in components.values() for topology_id in component_ids},
            key=str,
        )
    )


def specialized_topologies(
    session: Session,
    general_topology: MolecularTopology | UUID,
    *,
    project_id: UUID | None = None,
    abstraction_policy_version: str = STEREO_ABSTRACTION_POLICY_VERSION,
    include_general: bool = False,
) -> tuple[MolecularTopology, ...]:
    """Load every topology reachable below a general topology."""

    root_id = (
        general_topology
        if isinstance(general_topology, UUID)
        else _require_id(general_topology, label="general MolecularTopology")
    )
    owner_project_id = project_id
    if owner_project_id is None:
        if isinstance(general_topology, UUID):
            owner_project_id = session.exec(
                select(MolecularTopology.project_id).where(MolecularTopology.id == root_id)
            ).first()
        else:
            owner_project_id = getattr(general_topology, "project_id", None)
    topology_ids = specialized_topology_ids(
        session,
        general_topology,
        project_id=owner_project_id,
        abstraction_policy_version=abstraction_policy_version,
    )
    if include_general:
        topology_ids = (root_id, *topology_ids)
    if not topology_ids:
        return ()
    return tuple(
        session.exec(
            select(MolecularTopology)
            .where(
                col(MolecularTopology.id).in_(topology_ids),
                _project_owner_predicate(col(MolecularTopology.project_id), owner_project_id),
            )
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
    "topology_dag_component_ids",
    "topology_dag_components_by_root",
    "topology_abstraction_mapping_witness",
]
