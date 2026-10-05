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
    args = Namespace(
        project_id=project_id,
        logical_reaction_ids=None,
        retire_scoped_logicals=False,
        state_file=state,
        report=None,
        apply=apply,
    )
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
    args = Namespace(
        project_id=project_id,
        logical_reaction_ids=None,
        retire_scoped_logicals=False,
        state_file=state,
        report=None,
        apply=True,
    )
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
                return SimpleNamespace(
                    mapped_reaction_id=reaction_id,
                    logical_reaction_id=uuid4(),
                    calculation_frame_id=frame_id,
                )
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


def test_retire_specific_reaction_accepts_frames_reparsed_to_multiple_mappings(
    isolated_project, monkeypatch
):
    """A specific logical can split into several concrete mappings of one abstract reaction."""

    engine, project_id = isolated_project
    old_reaction_id = _reaction(engine, project_id, current=False)
    exact_mapping_id, variant_mapping_id = uuid4(), uuid4()
    target_logical_id = uuid4()
    exact_smiles = "[He:1]>>[He:1]"
    variant_smiles = "[Ne:1]>>[Ne:1]"
    with Session(engine) as session:
        session.add(
            LogicalReaction(
                id=target_logical_id,
                project_id=project_id,
                reaction_key=target_logical_id.hex,
                reaction_hash=sha256(target_logical_id.bytes).hexdigest(),
            )
        )
        for reaction_id, key, smiles in (
            (exact_mapping_id, "mapping:exact", exact_smiles),
            (variant_mapping_id, "mapping:variant", variant_smiles),
        ):
            session.add(
                MappedReaction(
                    id=reaction_id,
                    logical_reaction_id=target_logical_id,
                    project_id=project_id,
                    mapped_reaction_key=key,
                    mapped_reaction_kind=MappedReactionKind.OTHER,
                    mapped_reaction_smiles=smiles,
                    mapping_hash=sha256(smiles.encode()).hexdigest(),
                )
            )
        session.commit()

    refreshes = []
    from tricycle_reaction_db.application.services import (
        mapped_reaction_thermodynamics_persistence,
    )

    monkeypatch.setattr(
        mapped_reaction_thermodynamics_persistence,
        "enqueue_mapped_reaction_profile_refresh",
        lambda session, reactions: refreshes.append({reaction.id for reaction in reactions}),
    )
    with Session(engine) as session:
        state = rebuild._snapshot(session, project_id, "test")
        entry = next(
            item for item in state["plan"]["reactions"] if item["id"] == str(old_reaction_id)
        )
        frame_ids = [uuid4(), uuid4()]
        inference_ids = [uuid4(), uuid4()]
        state["plan"]["retire_scoped_logicals"] = True
        state["plan"]["inferences"] = [
            {"id": str(inference_id), "mapped_reaction_id": str(old_reaction_id)}
            for inference_id in inference_ids
        ]
        fake_inferences = {
            inference_ids[0]: SimpleNamespace(
                mapped_reaction_id=exact_mapping_id,
                logical_reaction_id=target_logical_id,
                calculation_frame_id=frame_ids[0],
            ),
            inference_ids[1]: SimpleNamespace(
                mapped_reaction_id=variant_mapping_id,
                logical_reaction_id=target_logical_id,
                calculation_frame_id=frame_ids[1],
            ),
        }
        fake_frames = {
            frame_ids[0]: SimpleNamespace(geometry_id=uuid4()),
            frame_ids[1]: SimpleNamespace(geometry_id=uuid4()),
        }
        original_get = session.get

        def get_verified_source(model, identifier, **kwargs):
            if model is TransitionStateInference:
                return fake_inferences.get(identifier)
            if model is CalculationFrame:
                return fake_frames.get(identifier)
            return original_get(model, identifier, **kwargs)

        monkeypatch.setattr(session, "get", get_verified_source)
        monkeypatch.setattr(rebuild, "_resume_complete", lambda *args: True)
        result = rebuild._cleanup_one(session, entry, state)
        assert result["status"] == "deleted"
        assert set(result["target_reaction_ids"]) == {
            str(exact_mapping_id),
            str(variant_mapping_id),
        }
        session.commit()

    assert refreshes == [{exact_mapping_id, variant_mapping_id}]
    with Session(engine) as session:
        assert session.get(MappedReaction, old_reaction_id) is None
        assert session.get(MappedReaction, exact_mapping_id) is not None
        assert session.get(MappedReaction, variant_mapping_id) is not None


def test_reparents_distinct_mapping_when_abstract_hash_has_no_destination(
    isolated_project, monkeypatch
):
    """Keep a distinct concrete map under the abstract reaction after proof succeeds."""

    engine, project_id = isolated_project
    old_reaction_id = _reaction(engine, project_id, current=False)
    target_logical_id, target_reaction_id = uuid4(), uuid4()
    target_smiles = "[Ne:1]>>[Ne:1]"
    target_hash = sha256(target_smiles.encode()).hexdigest()
    with Session(engine) as session:
        old_reaction = session.get(MappedReaction, old_reaction_id)
        assert old_reaction is not None
        old_logical_id = old_reaction.logical_reaction_id
        session.add(
            LogicalReaction(
                id=target_logical_id,
                project_id=project_id,
                reaction_key=target_logical_id.hex,
                reaction_hash=sha256(target_logical_id.bytes).hexdigest(),
            )
        )
        session.add(
            MappedReaction(
                id=target_reaction_id,
                logical_reaction_id=target_logical_id,
                project_id=project_id,
                mapped_reaction_key="mapping:target",
                mapped_reaction_kind=MappedReactionKind.OTHER,
                mapped_reaction_smiles=target_smiles,
                mapping_hash=target_hash,
            )
        )
        session.commit()

    with Session(engine) as session:
        state = rebuild._snapshot(
            session,
            project_id,
            "test",
            logical_reaction_ids=(old_logical_id,),
            retire_scoped_logicals=True,
        )
        entry = next(
            item for item in state["plan"]["reactions"] if item["id"] == str(old_reaction_id)
        )
    source = {"id": str(uuid4()), "mapped_reaction_id": str(old_reaction_id)}
    state["plan"]["inferences"] = [source]
    frame_id, geometry_id = uuid4(), uuid4()
    inference = SimpleNamespace(
        mapped_reaction_id=target_reaction_id,
        logical_reaction_id=target_logical_id,
        calculation_frame_id=frame_id,
    )

    def reparent(_session, reaction, logical_id):
        assert logical_id == target_logical_id
        reaction.logical_reaction_id = logical_id

    with Session(engine) as session:
        original_get = session.get

        def verified_source(model, identifier, **kwargs):
            if model is TransitionStateInference:
                return inference
            if model is CalculationFrame:
                return SimpleNamespace(geometry_id=geometry_id)
            return original_get(model, identifier, **kwargs)

        monkeypatch.setattr(session, "get", verified_source)
        monkeypatch.setattr(rebuild, "_resume_complete", lambda *args: True)
        monkeypatch.setattr(rebuild, "_reparent_distinct_mapping", reparent)
        from tricycle_reaction_db.application.services import (
            mapped_reaction_thermodynamics_persistence,
        )

        monkeypatch.setattr(
            mapped_reaction_thermodynamics_persistence,
            "enqueue_mapped_reaction_profile_refresh",
            lambda session, reactions: None,
        )
        result = rebuild._cleanup_one(session, entry, state)
        session.commit()

    assert result["status"] == "reparented"
    assert result["target_reaction_id"] == str(old_reaction_id)
    with Session(engine) as session:
        old = session.get(MappedReaction, old_reaction_id)
        assert old is not None
        assert old.logical_reaction_id == target_logical_id
        assert session.get(LogicalReaction, old_logical_id) is None
