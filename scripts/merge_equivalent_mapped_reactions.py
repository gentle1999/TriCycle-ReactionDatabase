#!/usr/bin/env python3
"""Merge complete stereo-equivalent reaction mappings without rebuilding TS endpoints.

Every group is an independent transaction, dry-run by default. Only reaction
associations and map labels change; geometries, calculation frames, endpoint
records, topology records and their derivations are read-only. A durable JSON
backup is required before each committed group. Unsupported groups fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from audit_mapped_reaction_atom_mappings import (
    _load_geometry_mappings,
    _load_source_frames,
    _mapping_matches_source_frame,
    _source_components_from_frame,
)
from rdkit import Chem
from sqlalchemy import create_engine, text
from sqlmodel import Session, col, select

from tricycle_reaction_db.application.services.canonical_atom_mapping import (
    canonical_atom_index_mapping,
    canonical_reaction_atom_map_translation,
)
from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    REACTION_INDEX_POLICY,
    canonical_reaction_identity,
    derived_reaction_variant_key,
    serialize_reaction_components,
)
from tricycle_reaction_db.application.services.mapped_reaction_thermodynamics_persistence import (
    mark_mapped_reactions_thermodynamics_dirty,
)
from tricycle_reaction_db.application.services.reactions import (
    mapped_smiles_for_geometry,
    mapped_smiles_for_topology,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    LogicalReaction,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    MappedReactionParticipant,
    MolecularTopology,
    TransitionStateInference,
)
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side


def translate_maps(maps, translation):
    result = [translation[int(number)] for number in maps]
    if not result or len(set(result)) != len(result) or min(result) <= 0:
        raise ValueError("map translation must preserve distinct positive labels")
    return result


def participant_components(participants, topologies, translation=None):
    result = {Side.REACTANT: [], Side.PRODUCT: []}
    for participant in participants:
        maps = list(participant.atom_map_numbers)
        if translation is not None:
            maps = translate_maps(maps, translation)
        result[participant.side].append((topologies[participant.concrete_topology_id].mol, maps))
    return result


def without_stereo(components):
    result = {}
    for side, entries in components.items():
        result[side] = []
        for molecule, maps in entries:
            copy = Chem.Mol(molecule)
            Chem.RemoveStereochemistry(copy)
            result[side].append((copy, list(maps)))
    return result


def equivalent_geometry_labels(first, second, components):
    """Accept only a simultaneous automorphism of both labelled stereo graphs.

    An independent symmetry match on each endpoint would discard the reaction's
    cross-endpoint correspondence and can conflate distinct mapped reactions.
    """
    if len(first) != len(second) or set(first) != set(second):
        return False
    translation = dict(zip(first, second, strict=True))
    if len(translation) != len(first):
        return False
    translated = {
        side: [(mol, translate_maps(maps, translation)) for mol, maps in values]
        for side, values in components.items()
    }
    return serialize_reaction_components(translated) == serialize_reaction_components(components)


def archive_group(session, ids):
    params = {"ids": list(ids)}
    predicate = "mapped_reaction_id=any(cast(:ids as uuid[]))"
    nodes = f"select id from mapped_reaction_node where {predicate}"
    bindings = (
        f"select id from mapped_reaction_node_geometry where mapped_reaction_node_id in ({nodes})"
    )
    profiles = f"select id from mapped_reaction_thermodynamic_profile where {predicate}"
    scopes = {
        "mapped_reaction": "id=any(cast(:ids as uuid[]))",
        "mapped_reaction_participant": predicate,
        "mapped_reaction_node": predicate,
        "mapped_reaction_edge": predicate,
        "mapped_reaction_node_geometry": f"id in ({bindings})",
        "mapped_reaction_node_geometry_mapping": (
            f"mapped_reaction_node_geometry_id in ({bindings})"
        ),
        "transition_state_inference": predicate,
        "mapped_reaction_thermodynamic_profile": predicate,
        "mapped_reaction_thermodynamic_profile_source": f"profile_id in ({profiles})",
        "mapped_reaction_thermodynamic_profile_refresh_job": predicate,
    }
    return {
        table: session.execute(text(f"select to_jsonb(t) from {table} t where {where}"), params)
        .scalars()
        .all()
        for table, where in scopes.items()
    }


def merge_group(session, group, backup_dir, apply):
    logical_id = UUID(group["logical_reaction_id"])
    ids = [UUID(value) for value in group["mapped_reaction_ids"]]
    derived = group.get("mode") == "derived-variant"
    session.execute(text("SET LOCAL lock_timeout='5s'"))
    session.execute(text("SET LOCAL statement_timeout='120s'"))
    session.exec(
        select(LogicalReaction).where(LogicalReaction.id == logical_id).with_for_update()
    ).one()
    reactions = session.exec(
        select(MappedReaction)
        .where(col(MappedReaction.id).in_(ids))
        .order_by(MappedReaction.id)
        .with_for_update()
    ).all()
    if len(reactions) == 1:
        if reactions[0].mapped_reaction_smiles != group["canonical_smiles"]:
            raise ValueError("surviving reaction is not the audited canonical form")
        return {"status": "already-merged", "target": str(reactions[0].id)}
    if len(reactions) != len(ids) or len({r.project_id for r in reactions}) != 1:
        raise ValueError("reaction scope changed")
    if any(r.logical_reaction_id != logical_id for r in reactions):
        raise ValueError("logical reaction scope mismatch")
    participants = session.exec(
        select(MappedReactionParticipant)
        .where(col(MappedReactionParticipant.mapped_reaction_id).in_(ids))
        .with_for_update()
    ).all()
    by_reaction = defaultdict(list)
    topologies = {}
    for p in participants:
        by_reaction[p.mapped_reaction_id].append(p)
        if p.concrete_topology_id not in topologies:
            topologies[p.concrete_topology_id] = session.get(
                MolecularTopology, p.concrete_topology_id
            )
    identities = {
        r.id: canonical_reaction_identity(participant_components(by_reaction[r.id], topologies))
        for r in reactions
    }
    if derived:
        target = next(r for r in reactions if str(r.id) == group["target_id"])
        if identities[target.id].smiles != group["canonical_smiles"]:
            raise ValueError("selected observed or retained reaction changed since audit")
        variant_keys = {
            derived_reaction_variant_key(
                {
                    (p.side, p.template_index): (
                        topologies[p.concrete_topology_id].mol,
                        p.atom_map_numbers,
                    )
                    for p in by_reaction[r.id]
                }
            )
            for r in reactions
        }
        if len(variant_keys) != 1:
            raise ValueError(
                "derived candidates differ in endpoint stereo or connectivity correspondence"
            )
        retired_ids = [r.id for r in reactions if r.id != target.id]
        # Any independent source, even an unfinished inference, protects its row.
        if (
            session.exec(
                select(TransitionStateInference.id).where(
                    col(TransitionStateInference.mapped_reaction_id).in_(retired_ids)
                )
            ).first()
            is not None
        ):
            raise ValueError("refusing to retire a reaction with an independent source")
    else:
        if {identity.smiles for identity in identities.values()} != {group["canonical_smiles"]}:
            raise ValueError("complete stereo reaction identity changed since audit")
        target = min(
            reactions,
            key=lambda r: (r.mapped_reaction_smiles != group["canonical_smiles"], str(r.id)),
        )
    translations = {rid: identity.source_map_to_canonical for rid, identity in identities.items()}
    target_components = participant_components(
        by_reaction[target.id], topologies, translations[target.id]
    )
    if derived:
        for reaction in reactions:
            if reaction.id == target.id:
                continue
            transfer = canonical_reaction_atom_map_translation(
                participant_components(by_reaction[reaction.id], topologies),
                target_components,
                include_stereochemistry=False,
            )
            if transfer is None:
                raise ValueError("derived candidate changes full atom correspondence")
            translations[reaction.id] = transfer.source_to_target
    target_participants = {p.logical_reaction_participant_id: p for p in by_reaction[target.id]}
    participant_targets = {}
    participant_maps = {}
    participant_smiles = {}
    for p in participants:
        counterpart = target_participants.get(p.logical_reaction_participant_id)
        if counterpart is None or (p.side, p.template_index) != (
            counterpart.side,
            counterpart.template_index,
        ):
            raise ValueError("participant slot mismatch")
        maps = translate_maps(p.atom_map_numbers, translations[p.mapped_reaction_id])
        smiles = mapped_smiles_for_topology(topologies[p.concrete_topology_id], maps)
        expected = mapped_smiles_for_topology(
            topologies[counterpart.concrete_topology_id],
            translate_maps(counterpart.atom_map_numbers, translations[target.id]),
        )
        if smiles != expected:
            if not derived or p.mapped_reaction_id == target.id:
                raise ValueError("translated participant graph differs from target slot")
            if (
                canonical_atom_index_mapping(
                    topologies[p.concrete_topology_id].mol,
                    topologies[counterpart.concrete_topology_id].mol,
                )
                is None
            ):
                raise ValueError("derived candidate has a different concrete stereo topology")
        participant_targets[p.id] = counterpart.id
        participant_maps[p.id] = maps
        participant_smiles[p.id] = smiles
    nodes = session.exec(
        select(MappedReactionNode).where(col(MappedReactionNode.mapped_reaction_id).in_(ids))
    ).all()
    node_lookup = {n.id: n for n in nodes}
    target_nodes = {n.node_key: n for n in nodes if n.mapped_reaction_id == target.id}
    node_targets = {}
    for n in nodes:
        match = target_nodes.get(n.node_key)
        if match is None or (match.role, match.node_index) != (n.role, n.node_index):
            raise ValueError("reaction path node differs")
        node_targets[n.id] = match.id
    edges = session.exec(
        select(MappedReactionEdge).where(col(MappedReactionEdge.mapped_reaction_id).in_(ids))
    ).all()
    edge_shapes = defaultdict(set)
    for edge in edges:
        edge_shapes[edge.mapped_reaction_id].add(
            (
                edge.edge_key,
                edge.edge_kind,
                node_lookup[edge.source_node_id].node_key,
                node_lookup[edge.target_node_id].node_key,
                node_lookup[edge.transition_state_node_id].node_key
                if edge.transition_state_node_id
                else None,
            )
        )
    if any(edge_shapes[r.id] != edge_shapes[target.id] for r in reactions):
        raise ValueError("reaction path edges differ")
    # Protected endpoint records are read only; no endpoint rebuild is called.
    inferences = session.exec(
        select(TransitionStateInference)
        .where(col(TransitionStateInference.mapped_reaction_id).in_(ids))
        .with_for_update()
    ).all()
    frames = _load_source_frames(session, [logical_id])
    own_frames = [f for (rid, _), values in frames.items() if rid in ids for f in values]
    inference_by_id = {item.id: item for item in inferences}
    if set(inference_by_id) != {f.inference_id for f in own_frames}:
        raise ValueError("inference without complete succeeded source frame")
    for f in [frame for values in frames.values() for frame in values]:
        for endpoint in f.endpoints:
            if endpoint.topology_id not in topologies:
                topologies[endpoint.topology_id] = session.get(
                    MolecularTopology, endpoint.topology_id
                )
    rows = [row for row in _load_geometry_mappings(session, [logical_id]) if row.reaction_id in ids]
    buckets = defaultdict(list)
    for row in rows:
        p_id = participant_targets.get(row.binding.mapped_reaction_participant_id)
        key = (node_targets[row.binding.mapped_reaction_node_id], row.geometry.id, p_id)
        buckets[key].append(row)
    plans = []
    frame_maps = {}
    inherited_bindings = 0
    repaired_endpoint_mappings = 0
    for key, members in buckets.items():
        if len({(r.binding.component_key, r.binding.component_index) for r in members}) != 1:
            raise ValueError("coordinate component slot differs between duplicate mappings")
        keep = min(members, key=lambda row: (row.reaction_id != target.id, str(row.binding.id)))
        candidates = []
        members.sort(key=lambda row: (row is not keep, str(row.binding.id)))
        for row in members:
            if row.mapping is None or not row.mapping.verified:
                raise ValueError("missing or unverified coordinate mapping")
            candidate = translate_maps(
                row.mapping.geometry_atom_map_numbers, translations[row.reaction_id]
            )
            if candidate not in candidates:
                candidates.append(candidate)
        relevant = [f for f in own_frames if f.geometry_id == key[1]] if key[2] is None else []
        if key[2] is None:
            inherited = not relevant
            witness_target = target_components
            if inherited:
                # Match the existing inheritance policy: concrete stereo variants
                # share a TS through the COMPLETE connectivity correspondence.
                # This never relaxes the stereo identity used to merge reactions.
                relevant = [
                    f for values in frames.values() for f in values if f.geometry_id == key[1]
                ]
                witness_target = without_stereo(target_components)
                inherited_bindings += 1
            sources = []
            for frame in relevant:
                components, error = _source_components_from_frame(frame, topologies)
                if components is None:
                    raise ValueError(error)
                if inherited:
                    translation = canonical_reaction_atom_map_translation(
                        components, target_components, include_stereochemistry=False
                    )
                    if translation is None:
                        raise ValueError("shared TS source changes complete reaction connectivity")
                    source_maps = [
                        translation.source_to_target[i + 1] for i in range(keep.geometry.atom_count)
                    ]
                    components = without_stereo(components)
                else:
                    identity = canonical_reaction_identity(components)
                    if identity.smiles != group["canonical_smiles"]:
                        raise ValueError(
                            "TS source endpoints disagree with complete reaction identity"
                        )
                    source_maps = [
                        identity.source_map_to_canonical[i + 1]
                        for i in range(keep.geometry.atom_count)
                    ]
                candidate = [0] * len(source_maps)
                if sorted(frame.observed_to_geometry) != list(range(len(source_maps))):
                    raise ValueError("TS source frame order is not a permutation")
                for source_index, geometry_index in enumerate(frame.observed_to_geometry):
                    candidate[geometry_index] = source_maps[source_index]
                if candidate not in candidates:
                    if inherited:
                        candidates.insert(0, candidate)
                    else:
                        candidates.append(candidate)
                sources.append((frame, components))
            selected = None
            for candidate in candidates:
                if (
                    sources
                    or all(
                        equivalent_geometry_labels(candidate, other, target_components)
                        for other in candidates
                    )
                ) and all(
                    _mapping_matches_source_frame(
                        frame=replace(frame, source_atom_maps=None),
                        geometry=keep.geometry,
                        geometry_maps=candidate,
                        source_components=components,
                        target_components=witness_target,
                    )[0]
                    for frame, components in sources
                ):
                    selected = candidate
                    break
            if selected is None:
                raise ValueError(
                    "ambiguous TS coordinate maps without common source witness: "
                    f"geometry={key[1]}, sources={len(sources)}, candidates={len(candidates)}"
                )
            for frame, _ in sources:
                if frame.inference_id in inference_by_id:
                    frame_maps[frame.inference_id] = [
                        selected[i] for i in frame.observed_to_geometry
                    ]
            mapped_smiles = mapped_smiles_for_geometry(
                keep.geometry, selected, include_stereochemistry=False
            )
        else:
            expected = participant_smiles[key[2]]
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if mapped_smiles_for_geometry(keep.geometry, candidate) == expected
                ),
                None,
            )
            if selected is None:
                participant = next(p for p in by_reaction[target.id] if p.id == key[2])
                topology = topologies[participant.concrete_topology_id]
                witness = canonical_atom_index_mapping(keep.geometry.mol, topology.mol)
                if witness is not None:
                    candidate = [participant_maps[participant.id][index] for index in witness]
                    if mapped_smiles_for_geometry(keep.geometry, candidate) == expected:
                        selected = candidate
                        repaired_endpoint_mappings += 1
            if selected is None:
                raise ValueError(
                    "endpoint coordinate map differs from target participant: "
                    f"geometry={key[1]}, participant={key[2]}"
                )
            mapped_smiles = mapped_smiles_for_geometry(keep.geometry, selected)
        plans.append((key, keep, members, selected, mapped_smiles))
    if set(frame_maps) != set(inference_by_id):
        raise ValueError("a source inference has no preserved TS geometry binding")
    backup = archive_group(session, ids)
    # All validations have completed. Update only labels and reaction ownership.
    for p in by_reaction[target.id]:
        p.atom_map_numbers = participant_maps[p.id]
        p.mapped_smiles = participant_smiles[p.id]
        session.add(p)
    target.mapped_reaction_smiles = group["canonical_smiles"]
    target.mapping_hash = sha256(target.mapped_reaction_smiles.encode()).hexdigest()
    target.normalization_metadata = identities[target.id].normalization_metadata()
    session.add(target)
    used_coordinates = defaultdict(set)
    primary_components = set()
    # Keep existing target indices first; moved rows get fresh indices.
    plans.sort(
        key=lambda item: (
            item[1].reaction_id != target.id,
            not item[1].binding.is_primary,
            str(item[1].binding.id),
        )
    )
    discarded = []
    for key, keep, members, maps, smiles in plans:
        binding = keep.binding
        component = (key[0], binding.component_key)
        coordinate = binding.coordinate_index
        if coordinate in used_coordinates[component]:
            coordinate = max(used_coordinates[component], default=-1) + 1
        used_coordinates[component].add(coordinate)
        # Existing source rows retain their IDs, so backing mapping data and
        # scientific coordinates are preserved without copying per-atom arrays.
        binding.mapped_reaction_node_id = key[0]
        binding.mapped_reaction_participant_id = key[2]
        binding.coordinate_index = coordinate
        binding.is_primary = component not in primary_components
        primary_components.add(component)
        keep.mapping.geometry_atom_map_numbers = maps
        keep.mapping.mapped_smiles = smiles
        discarded.extend(row.binding.id for row in members if row is not keep)
    # Clear old primary flags first, avoiding immediate unique-index conflicts.
    session.execute(
        text(
            "update mapped_reaction_node_geometry set is_primary=false where "
            "mapped_reaction_node_id=any(cast(:ids as uuid[]))"
        ),
        {"ids": list(node_lookup)},
    )
    if discarded:
        session.execute(
            text("delete from mapped_reaction_node_geometry where id=any(cast(:ids as uuid[]))"),
            {"ids": discarded},
        )
    # SQLModel dirty flags can omit a same-valued primary assignment after the
    # bulk clear. Explicit updates make the chosen state unambiguous.
    for key, keep, _members, _maps, _smiles in plans:
        b = keep.binding
        session.execute(
            text("""update mapped_reaction_node_geometry set mapped_reaction_node_id=:node,
            mapped_reaction_participant_id=:participant,
            coordinate_index=:coordinate,is_primary=:primary where id=:id"""),
            {
                "node": key[0],
                "participant": key[2],
                "coordinate": b.coordinate_index,
                "primary": b.is_primary,
                "id": b.id,
            },
        )
        session.add(keep.mapping)
    for item in inferences:
        settings = dict(item.inference_settings or {})
        settings.update(
            source_atom_map_numbers=frame_maps[item.id],
            canonical_mapped_reaction_smiles=target.mapped_reaction_smiles,
            reaction_index_policy=REACTION_INDEX_POLICY,
        )
        item.inference_settings = settings
        item.mapped_reaction_id = target.id
        session.add(item)
    session.flush()
    removed = [rid for rid in ids if rid != target.id]
    session.execute(
        text("delete from mapped_reaction_edge where mapped_reaction_id=any(cast(:ids as uuid[]))"),
        {"ids": removed},
    )
    session.execute(
        text("delete from mapped_reaction where id=any(cast(:ids as uuid[]))"), {"ids": removed}
    )
    preserved_count = session.execute(
        text(
            "select count(*) from mapped_reaction_node_geometry ng join mapped_reaction_node n "
            "on n.id=ng.mapped_reaction_node_id where n.mapped_reaction_id=:id"
        ),
        {"id": target.id},
    ).scalar_one()
    if preserved_count != len(plans):
        raise ValueError("preserved geometry union changed during merge")
    preserved_inferences = set(
        session.execute(
            text("select id from transition_state_inference where mapped_reaction_id=:id"),
            {"id": target.id},
        ).scalars()
    )
    if preserved_inferences != set(inference_by_id):
        raise ValueError("source inference union changed during merge")
    mark_mapped_reactions_thermodynamics_dirty(session, [target])
    session.flush()
    report = {
        "status": "merged" if apply else "verified",
        "mode": "derived-variant" if derived else "strict-identity",
        "target": str(target.id),
        "removed": [str(rid) for rid in removed],
        "preserved_geometry_bindings": len(plans),
        "duplicate_geometry_bindings": len(discarded),
        "source_inferences": len(inferences),
        "shared_ts_bindings": inherited_bindings,
        "repaired_endpoint_mappings": repaired_endpoint_mappings,
    }
    if apply:
        path = backup_dir / (str(min(ids, key=str)) + ".json")
        with path.open("x") as handle:
            json.dump({"report": report, "rows": backup}, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        session.commit()
    else:
        session.rollback()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    settings = get_settings()
    for key in ("NO_PROXY", "no_proxy"):
        os.environ[key] = ",".join(
            filter(
                None,
                [
                    os.environ.get(key, ""),
                    urlsplit(settings.database_url).hostname,
                    urlsplit(settings.rustfs_endpoint_url).hostname,
                ],
            )
        )
    engine = create_engine(settings.database_url)
    groups = json.loads(args.audit.read_text())["duplicate_groups"]
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    with args.report.open("a") as output:
        for i, group in enumerate(groups[: args.limit]):
            with Session(engine, autoflush=False) as session:
                try:
                    result = merge_group(session, group, args.backup_dir, args.apply)
                except Exception as exc:
                    session.rollback()
                    result = {"status": "blocked", "error": str(exc).split("[SQL:")[0][:1000]}
                result.update(
                    index=i,
                    logical_reaction_id=group["logical_reaction_id"],
                    source_ids=group["mapped_reaction_ids"],
                )
                output.write(json.dumps(result) + "\n")
                output.flush()
                print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
