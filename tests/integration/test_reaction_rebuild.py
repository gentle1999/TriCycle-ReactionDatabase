"""Rebuild control flow in an explicitly isolated PostgreSQL schema."""

import importlib.util
import json
import os
from argparse import Namespace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import (
    CalculationFrame,
    LogicalReaction,
    MappedReaction,
    MappedReactionEdge,
    MappedReactionNode,
    Project,
    TransitionStateInference,
    metadata,
)
from tricycle_reaction_db.domain.enums import (
    MappedReactionEdgeKind,
    MappedReactionKind,
    MappedReactionNodeRole,
)
from tricycle_reaction_db.domain.identity import SYSTEM_ORGANIZATION_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="requires an explicitly enabled database",
    ),
]
SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rebuild_mapped_reactions.py"
spec = importlib.util.spec_from_file_location("reaction_rebuild_integration", SCRIPT)
assert spec and spec.loader
rebuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebuild)


@pytest.fixture
def isolated_project(monkeypatch):
    # The maintenance command commits and disposes its engine. Give it a
    # schema owned by this fixture, including every engine it creates.
    schema = "tricycle_test_" + uuid4().hex
    admin = create_engine(get_settings().database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        get_settings().database_url,
        connect_args={"options": f"-csearch_path={schema},public"},
    ).execution_options(schema_translate_map={None: schema})
    monkeypatch.setattr(rebuild, "create_engine", lambda *args, **kwargs: engine)
    project_id = uuid4()
    try:
        metadata.create_all(engine)
        # Bootstrap identities are read from the configured test database.
        # Explicit column names avoid relying on migration/metadata column order.
        with admin.begin() as connection:
            for name in ("user_account", "organization"):
                columns = ", ".join(f'"{column.name}"' for column in metadata.tables[name].columns)
                connection.execute(
                    text(
                        f'INSERT INTO "{schema}"."{name}" ({columns}) '
                        f'SELECT {columns} FROM "{name}"'
                    )
                )
        with Session(engine) as session:
            assert session.execute(text("SELECT current_schema()")).scalar_one() == schema
            session.add(
                Project(
                    id=project_id,
                    organization_id=SYSTEM_ORGANIZATION_ID,
                    name="Rebuild test",
                    slug=f"rebuild-{project_id.hex}",
                )
            )
            session.commit()
        yield engine, project_id
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _reaction(engine, project_id, *, current):
    logical_id, reaction_id = uuid4(), uuid4()
    smiles = "[He:1]>>[He:1]"
    with Session(engine) as session:
        session.add(
            LogicalReaction(
                id=logical_id,
                project_id=project_id,
                reaction_key=logical_id.hex,
                reaction_hash=sha256(logical_id.bytes).hexdigest(),
            )
        )
        session.flush()
        session.add(
            MappedReaction(
                id=reaction_id,
                logical_reaction_id=logical_id,
                project_id=project_id,
                mapped_reaction_key="curated",
                mapped_reaction_kind=MappedReactionKind.CURATED,
                mapped_reaction_smiles=smiles,
                mapping_hash=sha256(smiles.encode()).hexdigest(),
                normalization_metadata={
                    "policy": rebuild.REACTION_INDEX_POLICY,
                    "rdkit_version": "test",
                    "selected_mapped_reaction_smiles": smiles,
                }
                if current
                else None,
            )
        )
        session.commit()
    return reaction_id


@pytest.mark.parametrize("apply", [False, True])
def test_untraceable_legacy_reaction_is_reported_and_retained(isolated_project, tmp_path, apply):
    engine, project_id = isolated_project
    reaction_id = _reaction(engine, project_id, current=False)
    state = tmp_path / "state.json"
    args = Namespace(project_id=project_id, state_file=state, report=None, apply=apply)
    assert rebuild.run(args) == 1
    report = json.loads(state.with_suffix(".report.json").read_text())
    assert report["complete"] is False
    assert report["remaining_legacy_reaction_ids"] == [str(reaction_id)]
    assert report["untraceable_legacy_reaction_ids"] == [str(reaction_id)]
    with Session(engine) as session:
        assert session.get(MappedReaction, reaction_id) is not None


def test_current_reaction_is_retained_and_resume_is_idempotent(isolated_project, tmp_path):
    engine, project_id = isolated_project
    reaction_id = _reaction(engine, project_id, current=True)
    state = tmp_path / "state.json"
    args = Namespace(project_id=project_id, state_file=state, report=None, apply=True)
    assert rebuild.run(args) == 0
    original_state = state.read_bytes()
    assert rebuild.run(args) == 0
    assert state.read_bytes() == original_state
    report = json.loads(state.with_suffix(".report.json").read_text())
    assert report["complete"] is True
    assert report["cleanup"][str(reaction_id)]["status"] == "retained_current"
    with Session(engine) as session:
        assert session.get(MappedReaction, reaction_id) is not None


def test_cleanup_removes_restricting_edges_before_parent(isolated_project, monkeypatch):
    """Exercise real FK cleanup after the separately tested evidence-verification gate."""
    engine, project_id = isolated_project
    reaction_id = _reaction(engine, project_id, current=False)
    with Session(engine) as session:
        state = rebuild._snapshot(session, project_id, "test")
        entry = state["plan"]["reactions"][0]
        nodes = [
            MappedReactionNode(
                id=uuid4(),
                mapped_reaction_id=reaction_id,
                node_key=role.value,
                node_index=index,
                role=role,
            )
            for index, role in enumerate(
                (MappedReactionNodeRole.REACTANT, MappedReactionNodeRole.PRODUCT)
            )
        ]
        session.add_all(nodes)
        session.flush()
        edge_id = uuid4()
        session.add(
            MappedReactionEdge(
                id=edge_id,
                mapped_reaction_id=reaction_id,
                edge_key="step",
                source_node_id=nodes[0].id,
                target_node_id=nodes[1].id,
                edge_kind=MappedReactionEdgeKind.ELEMENTARY_STEP,
            )
        )
        session.commit()
        frame_id = uuid4()
        state["plan"]["inferences"] = [{"id": str(uuid4()), "mapped_reaction_id": str(reaction_id)}]
        original_get = session.get

        def verified_source(model, identifier, **kwargs):
            if model is TransitionStateInference:
                return SimpleNamespace(calculation_frame_id=frame_id)
            if model is CalculationFrame:
                return SimpleNamespace(geometry_id=uuid4())
            return original_get(model, identifier, **kwargs)

        monkeypatch.setattr(session, "get", verified_source)
        monkeypatch.setattr(rebuild, "_resume_complete", lambda *args: True)
        result = rebuild._cleanup_one(session, entry, state)
        assert result["status"] == "deleted"
        session.commit()
    with Session(engine) as session:
        assert session.get(MappedReaction, reaction_id) is None
        assert session.get(MappedReactionEdge, edge_id) is None
        assert session.get(Project, project_id) is not None
