"""Read-only audit and immutable-source reparse manifest for the chemistry refactor."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from rdkit import Chem
from sqlalchemy import create_engine, text

from tricycle_reaction_db.application.services.canonical_reaction_identity import (
    REACTION_INDEX_POLICY,
    canonical_reaction_identity,
)
from tricycle_reaction_db.application.services.mapped_geometry_atom_order import (
    parse_mapped_reaction_smiles,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.domain.enums import LogicalReactionParticipantSide as Side
from tricycle_reaction_db.domain.explicit_hydrogens import require_explicit_hydrogens


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--reparse-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists() or args.reparse_manifest.exists():
        raise ValueError(
            "report/manifest already exists; choose new paths to preserve audit evidence"
        )
    issues: list[dict[str, str]] = []
    bad_topologies: list[UUID] = []
    bad_geometries: list[UUID] = []
    bad_reactions: list[UUID] = []
    bad_inferences: list[UUID] = []
    reaction_reindex_candidates: list[dict[str, object]] = []
    reaction_ids_by_canonical_smiles: dict[str, list[str]] = {}
    params = {"project": args.project_id}
    engine = create_engine(get_settings().database_url, connect_args={"connect_timeout": 10})
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        for table, query, ids in (
            (
                "molecular_topology",
                "SELECT id, mol_send(mol), atom_count FROM molecular_topology "
                "WHERE project_id=:project",
                bad_topologies,
            ),
            (
                "geometry",
                "SELECT g.id, mol_send(g.mol), t.atom_count FROM geometry g "
                "JOIN molecular_topology t ON t.id=g.topology_id WHERE g.project_id=:project",
                bad_geometries,
            ),
        ):
            for row_id, payload, atom_count in connection.execute(text(query), params).yield_per(
                256
            ):
                try:
                    molecule = Chem.Mol(bytes(payload))  # type: ignore[call-overload]
                    require_explicit_hydrogens(molecule)
                    if molecule.GetNumAtoms() != atom_count:
                        raise ValueError("MOL atom count differs from topology inventory")
                except (ValueError, RuntimeError) as error:
                    ids.append(row_id)
                    issues.append({"table": table, "id": str(row_id), "reason": str(error)})
        for row_id, smiles, stored_hash, normalization in connection.execute(
            text(
                "SELECT id, mapped_reaction_smiles, mapping_hash, normalization_metadata "
                "FROM mapped_reaction "
                "WHERE project_id=:project ORDER BY id"
            ),
            params,
        ).yield_per(256):
            try:
                reaction = parse_mapped_reaction_smiles(smiles)
                components: dict[Side, list[tuple[Chem.Mol, list[int]]]] = {}
                for side, templates in (
                    (Side.REACTANT, reaction.GetReactants()),
                    (Side.PRODUCT, reaction.GetProducts()),
                ):
                    components[side] = []
                    for molecule in templates:
                        require_explicit_hydrogens(molecule)
                        components[side].append(
                            (molecule, [a.GetAtomMapNum() for a in molecule.GetAtoms()])
                        )
                if normalization is not None:
                    if (
                        normalization.get("policy") != REACTION_INDEX_POLICY
                        or not normalization.get("rdkit_version")
                        or normalization.get("selected_mapped_reaction_smiles") != smiles
                        or stored_hash != sha256(smiles.encode("utf-8")).hexdigest()
                    ):
                        raise ValueError("stored selected reaction form/provenance is inconsistent")
                    # The persisted representative is authoritative. Do not
                    # classify RDKit round-trip oscillation as corrupt data.
                    reaction_ids_by_canonical_smiles.setdefault(smiles, []).append(str(row_id))
                    continue
                identity = canonical_reaction_identity(components)
                canonical_hash = sha256(identity.smiles.encode("utf-8")).hexdigest()
                requires_reindex = identity.smiles != smiles or canonical_hash != stored_hash
                reaction_reindex_candidates.append(
                    {
                        "mapped_reaction_id": str(row_id),
                        "old_mapped_reaction_smiles": smiles,
                        "old_mapping_hash": stored_hash,
                        "canonical_mapped_reaction_smiles": identity.smiles,
                        "canonical_mapping_hash": canonical_hash,
                        "old_map_to_canonical_map": {
                            str(number): target
                            for number, target in sorted(identity.source_map_to_canonical.items())
                        },
                        "requires_reindex": requires_reindex,
                        "requires_source_reinference": True,
                    }
                )
                # Include already-canonical rows: they may be the destination
                # of several legacy identities and must take part in merging.
                reaction_ids_by_canonical_smiles.setdefault(identity.smiles, []).append(str(row_id))
                raise ValueError("legacy reaction requires re-inference from its original TS frame")
            except (ValueError, RuntimeError) as error:
                bad_reactions.append(row_id)
                issues.append({"table": "mapped_reaction", "id": str(row_id), "reason": str(error)})
        for row_id, settings, atom_count in connection.execute(
            text("""
            SELECT i.id, i.inference_settings,
                   cardinality(f.observed_to_geometry_atom_indices)
            FROM transition_state_inference i
            JOIN calculation_frame f ON f.id=i.calculation_frame_id
            JOIN geometry g ON g.id=f.geometry_id
            WHERE g.project_id=:project AND i.status='succeeded'
        """),
            params,
        ).yield_per(256):
            maps = (settings or {}).get("source_atom_map_numbers")
            if (
                not isinstance(maps, list)
                or not all(type(number) is int for number in maps)
                or sorted(maps) != list(range(1, atom_count + 1))
            ):
                bad_inferences.append(row_id)
                issues.append(
                    {
                        "table": "transition_state_inference",
                        "id": str(row_id),
                        "reason": "missing or invalid source-to-reaction atom permutation",
                    }
                )
        params.update(
            topologies=bad_topologies,
            geometries=bad_geometries,
            reactions=bad_reactions,
            inferences=bad_inferences,
        )
        # Include every source sharing an affected identity, before the clean-first
        # runner removes any materialization. This also covers endpoint-only graphs.
        rows = connection.execute(
            text("""
            SELECT DISTINCT a.id, a.project_id, a.content_sha256, a.original_filename,
                            a.storage_status
            FROM artifact_file a JOIN parse_revision r ON r.artifact_file_id=a.id
            JOIN calculation_frame f ON f.parse_revision_id=r.id
            JOIN geometry g ON g.id=f.geometry_id
            LEFT JOIN transition_state_endpoint e ON e.calculation_frame_id=f.id
            LEFT JOIN transition_state_inference i ON i.calculation_frame_id=f.id
            LEFT JOIN mapped_reaction_participant p ON p.mapped_reaction_id=i.mapped_reaction_id
            WHERE a.project_id=:project AND (
                g.id=ANY(CAST(:geometries AS uuid[])) OR
                g.topology_id=ANY(CAST(:topologies AS uuid[])) OR
                e.topology_id=ANY(CAST(:topologies AS uuid[])) OR
                p.concrete_topology_id=ANY(CAST(:topologies AS uuid[])) OR
                i.mapped_reaction_id=ANY(CAST(:reactions AS uuid[])) OR
                i.id=ANY(CAST(:inferences AS uuid[])))
            ORDER BY a.id
        """),
            params,
        ).all()
    manifest = []
    unavailable = []
    for artifact, project, checksum, filename, status in rows:
        if status != "available":
            unavailable.append(str(artifact))
            continue
        manifest.append(
            {
                "phase": "manifest",
                "artifact_id": str(artifact),
                "project_id": str(project),
                "content_sha256": checksum,
                "filename": filename,
            }
        )
    report = {
        "project_id": str(args.project_id),
        "issues": issues,
        "reaction_reindex_candidates": reaction_reindex_candidates,
        "reaction_merge_groups": [
            {
                "canonical_mapped_reaction_smiles": smiles,
                "canonical_mapping_hash": sha256(smiles.encode("utf-8")).hexdigest(),
                "mapped_reaction_ids": ids,
            }
            for smiles, ids in sorted(reaction_ids_by_canonical_smiles.items())
            if len(ids) > 1
        ],
        "reaction_reindex_note": (
            "Legacy-string candidates are diagnostic only, never migration input. "
            "Reconstruct endpoints from original TS calculation frames using the import "
            "pipeline, then persist its selected normal form and actual association maps. "
            "Reactions "
            "with invalid or missing atom inventories are listed in issues instead."
        ),
        "repair_artifact_count": len(manifest),
        "unavailable_artifacts": unavailable,
        "repair_policy": (
            "reparse immutable source; rerun audit after repair; "
            "unreferenced or manually created rows require separate resolution"
        ),
    }
    for path in (args.report, args.reparse_manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.reparse_manifest.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in manifest)
    )
    print(
        json.dumps(
            {
                "issues": len(issues),
                "repair_artifacts": len(manifest),
                "unavailable_artifacts": len(unavailable),
            }
        )
    )
    engine.dispose()


if __name__ == "__main__":
    main()
