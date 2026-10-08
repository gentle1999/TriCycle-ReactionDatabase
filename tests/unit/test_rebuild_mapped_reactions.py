"""Recovery and cleanup contracts for the opt-in full reaction rebuild."""

import importlib.util
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rebuild_mapped_reactions.py"
spec = importlib.util.spec_from_file_location("reaction_rebuild_script", SCRIPT)
assert spec and spec.loader
rebuild = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebuild)


def _state():
    plan = {
        "database_key": "test-db",
        "project_id": None,
        "logical_reaction_ids": None,
        "retire_scoped_logicals": False,
        "policy": rebuild.REACTION_INDEX_POLICY,
        "inferences": [],
        "reactions": [],
    }
    return {
        "version": rebuild.STATE_VERSION,
        "run_id": str(uuid4()),
        "plan": plan,
        "plan_digest": rebuild._digest(plan),
    }


def _entry():
    return {
        "id": str(uuid4()),
        "mapped_reaction_id": str(uuid4()),
        "logical_reaction_id": str(uuid4()),
        "parse_revision_id": str(uuid4()),
        "calculation_frame_id": str(uuid4()),
        "file_frame_index": 3,
        "content_sha256": "a" * 64,
    }


def _inference(entry):
    return SimpleNamespace(
        **{key: value for key, value in entry.items() if key != "content_sha256"},
        artifact_ingestion_id=uuid4(),
        inference_settings={},
    )


@pytest.mark.parametrize("version", ["0061_reaction_normal_form", "0062_staged_upload_claim_index"])
def test_rebuild_accepts_required_schema_and_known_descendants(version):
    rebuild._require_reaction_schema(version)


@pytest.mark.parametrize("version", ["0001_initial_schema", "9999_unknown", "head", "0061"])
def test_rebuild_rejects_old_unknown_or_nonliteral_schema_versions(version):
    with pytest.raises(rebuild.RebuildBlocked, match="schema must be upgraded"):
        rebuild._require_reaction_schema(version)


def test_rebuild_checks_migration_ancestry_instead_of_revision_number(monkeypatch, tmp_path):
    from alembic.script import ScriptDirectory

    versions = tmp_path / "versions"
    versions.mkdir()
    for version, parent in (
        (rebuild.MINIMUM_SCHEMA_REVISION, None),
        ("future_queue_index", rebuild.MINIMUM_SCHEMA_REVISION),
        ("9999_unrelated", None),
    ):
        (versions / f"{version}.py").write_text(
            f"revision = {version!r}\ndown_revision = {parent!r}\n"
        )
    monkeypatch.setattr(rebuild, "ScriptDirectory", lambda _: ScriptDirectory(str(tmp_path)))
    rebuild._require_reaction_schema("future_queue_index")
    with pytest.raises(rebuild.RebuildBlocked, match="schema must be upgraded"):
        rebuild._require_reaction_schema("9999_unrelated")


@pytest.mark.parametrize("change", ["database", "project", "policy", "plan_digest"])
def test_resume_rejects_wrong_scope_or_modified_plan(change):
    state = _state()
    project = None
    database = "test-db"
    if change == "database":
        database = "different-db"
    elif change == "project":
        project = uuid4()
    elif change == "policy":
        state["plan"]["policy"] = "old-policy"
        state["plan_digest"] = rebuild._digest(state["plan"])
    else:
        state["plan"]["inferences"].append({"id": str(uuid4())})
    with pytest.raises(rebuild.RebuildBlocked, match="does not match"):
        rebuild._validate_state(state, project, database, None, False)


def test_snapshot_file_roundtrip_is_atomic_and_private(tmp_path):
    path = tmp_path / "run.json"
    state = _state()
    rebuild._save(path, state)
    rebuild._validate_state(json.loads(path.read_text()), None, "test-db", None, False)
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_selected_form_is_verified_without_rdkit_restandardization():
    smiles = "selected-output"
    metadata = {
        "policy": rebuild.REACTION_INDEX_POLICY,
        "rdkit_version": "recorded-version",
        "selected_mapped_reaction_smiles": smiles,
    }
    digest = sha256(smiles.encode()).hexdigest()
    assert rebuild._current_form(smiles, digest, metadata)
    assert not rebuild._current_form(smiles, digest, None)
    assert not rebuild._current_form(smiles, "0" * 64, metadata)
    assert not rebuild._current_form("other-output", digest, metadata)


def test_only_committed_database_marker_allows_resume_skip(monkeypatch):
    state, entry = _state(), _entry()
    inference = _inference(entry)
    state["results"] = {entry["id"]: {"status": "updated"}}
    assert not rebuild._resume_complete(None, inference, entry, state)
    inference.inference_settings[rebuild.CHECKPOINT_KEY] = {
        "run_id": state["run_id"],
        "source_sha256": entry["content_sha256"],
        "policy": rebuild.REACTION_INDEX_POLICY,
        "old_reaction_id": entry["mapped_reaction_id"],
    }
    verified = []
    monkeypatch.setattr(rebuild, "_verify_binding", lambda s, i: verified.append(i))
    assert rebuild._resume_complete(None, inference, entry, state)
    assert verified == [inference]
    inference.file_frame_index += 1
    with pytest.raises(rebuild.RebuildBlocked, match="checkpoint source"):
        rebuild._resume_complete(None, inference, entry, state)


def test_rebuild_uses_shared_pipeline_and_defers_cleanup(monkeypatch):
    state, entry = _state(), _entry()
    inference = _inference(entry)
    target = SimpleNamespace(id=uuid4(), mapped_reaction_smiles="chosen form")
    calls = []

    def persist(session, **kwargs):
        calls.append(kwargs)
        inference.mapped_reaction_id = target.id
        inference.inference_settings = {"source_atom_map_numbers": [2, 1]}

    monkeypatch.setattr(rebuild, "_mark_reinference_succeeded", persist)
    monkeypatch.setattr(rebuild, "_verify_binding", lambda *args: target)
    monkeypatch.setattr(rebuild, "_refresh_latest_ingestion_status", lambda *a, **k: None)
    session = SimpleNamespace(add=lambda value: None, flush=lambda: None)
    result = rebuild._rebuild_one(session, inference, object(), entry, state)
    assert calls[0]["cleanup_obsolete"] is False
    assert result["new_reaction_id"] == str(target.id)
    assert result["old_reaction_id"] == entry["mapped_reaction_id"]
    assert inference.inference_settings["source_atom_map_numbers"] == [2, 1]
    assert inference.inference_settings[rebuild.CHECKPOINT_KEY]["run_id"] == state["run_id"]


def test_concurrent_relink_is_not_overwritten():
    state, entry = _state(), _entry()
    inference = _inference(entry)
    inference.mapped_reaction_id = uuid4()
    with pytest.raises(rebuild.RebuildBlocked, match="reaction changed"):
        rebuild._rebuild_one(None, inference, object(), entry, state)


def test_missing_source_prevents_old_record_deletion():
    entry = {
        "id": str(uuid4()),
        "logical_reaction_id": str(uuid4()),
        "project_id": str(uuid4()),
        "mapped_reaction_smiles": "old",
        "mapping_hash": "a" * 64,
    }
    reaction = SimpleNamespace(
        **entry,
        normalization_metadata=None,
        mapped_reaction_kind=rebuild.MappedReactionKind.CURATED,
        mapped_reaction_key="curated",
    )
    responses = iter([reaction, None])
    session = SimpleNamespace(exec=lambda *args: SimpleNamespace(first=lambda: next(responses)))
    with pytest.raises(rebuild.RebuildBlocked, match="no source TS"):
        rebuild._cleanup_one(session, entry, _state())


def test_event_journal_is_append_only(tmp_path):
    path = tmp_path / "events.jsonl"
    for status in ("blocked", "updated"):
        rebuild._event(
            path, run_id="run", phase="apply", row_id="source", result={"status": status}
        )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["blocked", "updated"]


def test_backend_bypass_is_scoped_and_preserves_existing_entries(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "existing.example")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setattr(
        rebuild,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://user:password@db.example:5432/test"),
    )
    monkeypatch.setattr(
        rebuild, "RustFSSettings", lambda: SimpleNamespace(endpoint_url="https://objects.example")
    )
    rebuild._configure_backend_access()
    assert rebuild.os.environ["NO_PROXY"] == "existing.example,db.example,objects.example"
    assert rebuild.os.environ["no_proxy"] == "localhost,db.example,objects.example"
