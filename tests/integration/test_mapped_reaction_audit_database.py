"""Audit a freshly inferred TS, including its stored atom-order witness."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from sqlmodel import Session, select
from test_parse_replacement_collection import _ingested_connection

from tricycle_reaction_db.db.models import (
    LogicalReactionParticipant,
    MappedReaction,
    MappedReactionParticipant,
    MolecularTopology,
    ParseRevision,
    TransitionStateInference,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="requires database"),
]
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit_mapped_reaction_atom_mappings.py"
spec = importlib.util.spec_from_file_location("audit_database_script", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_audit_validates_ingested_ts_and_detects_corrupted_map():
    with (
        _ingested_connection() as (connection, artifact_id),
        Session(bind=connection, join_transaction_mode="create_savepoint") as session,
    ):
        inference = session.exec(
            select(TransitionStateInference)
            .join(ParseRevision)
            .where(ParseRevision.artifact_file_id == artifact_id)
        ).one()
        reaction = session.get(MappedReaction, inference.mapped_reaction_id)
        assert reaction is not None
        logical_ids = [reaction.logical_reaction_id]
        participants = session.exec(
            select(MappedReactionParticipant).where(
                MappedReactionParticipant.mapped_reaction_id == reaction.id,
            )
        ).all()
        logical_participants = session.exec(
            select(LogicalReactionParticipant).where(
                LogicalReactionParticipant.logical_reaction_id == reaction.logical_reaction_id,
            )
        ).all()
        topologies = {
            topology.id: topology
            for topology in session.exec(
                select(MolecularTopology).where(MolecularTopology.project_id == reaction.project_id)
            ).all()
        }
        audit = module.Audit(progress=False)
        contexts = module._build_reaction_contexts(
            audit, [reaction], participants, logical_participants, topologies
        )
        sources = module._load_source_frames(session, logical_ids)
        rows = module._load_geometry_mappings(session, logical_ids)
        module._check_ts_geometry_mappings(audit, rows, sources, contexts, topologies)
        assert audit.counts["ts_geometry_mappings_verified"] == 1, audit.findings
        assert audit.counts["ts_geometry_mappings_invalid"] == 0, audit.findings
        row = next(
            row for row in rows if row.mapping is not None and row.role.value == "transition_state"
        )
        row.mapping.geometry_atom_map_numbers = [1] * row.geometry.atom_count
        corrupted = module.Audit(progress=False)
        module._check_ts_geometry_mappings(corrupted, rows, sources, contexts, topologies)
        assert corrupted.counts["ts_geometry_mappings_invalid"] == 1
        session.rollback()
