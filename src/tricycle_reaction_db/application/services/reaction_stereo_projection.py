"""Selective stereo projection used to build logical reaction topologies."""

from __future__ import annotations

from collections.abc import Iterable
from functools import lru_cache

from rdkit import Chem
from sqlmodel import Session

from tricycle_reaction_db.application.services.topology_abstraction import (
    StereoFeature,
    assigned_stereo_features,
    persist_stereo_abstraction_projection,
)
from tricycle_reaction_db.core.chemistry_config import (
    INVERSION_LABILE_RULES,
    INVERSION_STEREO_PROJECTION_POLICY_VERSION,
    InversionLabileRule,
)
from tricycle_reaction_db.db.models import MolecularTopology


@lru_cache(maxsize=32)
def _rule_query(atom_smarts: str) -> Chem.Mol:
    query = Chem.MolFromSmarts(atom_smarts)
    if query is None:
        raise ValueError(f"inversion-labile SMARTS could not be parsed: {atom_smarts}")
    return query


def inversion_labile_atom_indices(
    molecule: Chem.Mol,
    *,
    rules: Iterable[InversionLabileRule] = INVERSION_LABILE_RULES,
) -> tuple[tuple[str, int], ...]:
    """Return ``(rule_id, atom_index)`` matches in deterministic order."""

    matches: list[tuple[str, int]] = []
    for rule in rules:
        query = _rule_query(rule.atom_smarts)
        for match in molecule.GetSubstructMatches(query, useChirality=False, uniquify=True):
            if len(match) != 1:
                raise ValueError(
                    f"inversion-labile rule {rule.rule_id} must match one atom per result"
                )
            matches.append((rule.rule_id, int(match[0])))
    return tuple(sorted(set(matches), key=lambda item: (item[0], item[1])))


def inversion_labile_atom_map_numbers(
    topology: MolecularTopology,
    atom_map_numbers: Iterable[int],
    *,
    rules: Iterable[InversionLabileRule] = INVERSION_LABILE_RULES,
) -> tuple[tuple[str, int], ...]:
    """Find rule matches and attach the authoritative source atom maps."""

    atom_maps = tuple(int(number) for number in atom_map_numbers)
    if len(atom_maps) != topology.atom_count:
        raise ValueError("topology atom maps must cover every topology atom")
    if any(number <= 0 for number in atom_maps) or len(set(atom_maps)) != len(atom_maps):
        raise ValueError("topology atom maps must be unique positive integers")
    return tuple(
        (rule_id, atom_maps[atom_index])
        for rule_id, atom_index in inversion_labile_atom_indices(topology.mol, rules=rules)
    )


def stereo_features_to_clear_for_atom_maps(
    topology: MolecularTopology,
    atom_map_numbers: Iterable[int],
    labile_atom_map_numbers: Iterable[int],
) -> tuple[StereoFeature, ...]:
    """Select stereo features whose value depends on a labile atom.

    Inversion rules match atoms, not bonds.  RDKit stores an E/Z assignment on
    the double bond, but the substituent that defines one side of that bond is
    stored in ``Bond.GetStereoAtoms()`` and is connected to the double-bond
    endpoint by an adjacent single bond.  A labile atom can therefore be a
    *reference* atom for a bond stereo feature without being an endpoint of the
    stereogenic bond itself.

    Resolve the dependency from each matched atom through its one-hop
    neighbourhood.  This keeps the selection atom-centred and also handles
    direct bond-centred stereo features (for example atropisomeric bonds).
    """

    atom_maps = tuple(int(number) for number in atom_map_numbers)
    labile_maps = frozenset(int(number) for number in labile_atom_map_numbers)
    if len(atom_maps) != topology.atom_count:
        raise ValueError("topology atom maps must cover every topology atom")
    if any(number <= 0 for number in atom_maps) or len(set(atom_maps)) != len(atom_maps):
        raise ValueError("topology atom maps must be unique positive integers")
    if not labile_maps:
        return ()

    assigned = assigned_stereo_features(topology.mol)
    assigned_atom_features = {
        feature.index: feature for feature in assigned if feature.kind == "atom"
    }
    assigned_bond_features = {
        feature.index: feature for feature in assigned if feature.kind == "bond"
    }
    selected_keys: set[tuple[str, int]] = set()

    for atom_index, atom_map in enumerate(atom_maps):
        if atom_map not in labile_maps:
            continue
        atom = topology.mol.GetAtomWithIdx(atom_index)
        atom_feature = assigned_atom_features.get(atom_index)
        if atom_feature is not None:
            selected_keys.add((atom_feature.kind, atom_feature.index))

        # A bond-centred feature is selected when the matched atom is either
        # the stereobond endpoint or the reference substituent attached to
        # that endpoint.  The latter is the N-inversion/diene case: the N--N
        # directional bond is adjacent to the matched N, while the E/Z
        # feature itself belongs to the neighbouring C=N double bond.
        for adjacent_bond in atom.GetBonds():
            direct_feature = assigned_bond_features.get(adjacent_bond.GetIdx())
            if direct_feature is not None:
                selected_keys.add((direct_feature.kind, direct_feature.index))

            endpoint_index = adjacent_bond.GetOtherAtomIdx(atom_index)
            endpoint_atom = topology.mol.GetAtomWithIdx(endpoint_index)
            for candidate_bond in endpoint_atom.GetBonds():
                candidate_feature = assigned_bond_features.get(candidate_bond.GetIdx())
                if candidate_feature is None:
                    continue
                if atom_index in candidate_bond.GetStereoAtoms():
                    selected_keys.add((candidate_feature.kind, candidate_feature.index))

    # Preserve the graph's deterministic feature order rather than the order
    # in which atoms and their neighbouring bonds were traversed.
    return tuple(feature for feature in assigned if (feature.kind, feature.index) in selected_keys)


def project_logical_topology(
    session: Session,
    concrete_topology: MolecularTopology,
    atom_map_numbers: Iterable[int],
    labile_atom_map_numbers: Iterable[int],
    *,
    context: object | None = None,
    rule_ids: Iterable[str] = (),
) -> MolecularTopology:
    """Materialize one lazy logical projection, retaining unrelated stereo."""

    cleared_features = stereo_features_to_clear_for_atom_maps(
        concrete_topology,
        atom_map_numbers,
        labile_atom_map_numbers,
    )
    if not cleared_features:
        return concrete_topology
    projected, _edge = persist_stereo_abstraction_projection(
        session,
        concrete_topology,
        cleared_features,
        context=context,
        abstraction_metadata={
            "projection_policy_version": INVERSION_STEREO_PROJECTION_POLICY_VERSION,
            "rule_ids": sorted(set(rule_ids)),
            "labile_atom_map_numbers": sorted({int(number) for number in labile_atom_map_numbers}),
        },
    )
    return projected


__all__ = [
    "INVERSION_LABILE_RULES",
    "INVERSION_STEREO_PROJECTION_POLICY_VERSION",
    "InversionLabileRule",
    "inversion_labile_atom_indices",
    "inversion_labile_atom_map_numbers",
    "project_logical_topology",
    "stereo_features_to_clear_for_atom_maps",
]
