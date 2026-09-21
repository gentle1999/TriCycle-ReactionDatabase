import gzip
import json
from hashlib import sha256
from pathlib import Path

import pytest

from tricycle_reaction_db.application.services import artifact_uploads as uploads


@pytest.mark.parametrize("entrypoint", ["worker", "validation", "inference"])
def test_all_parse_entrypoints_enable_native_byte_preserving_tolerance(monkeypatch, entrypoint):
    calls = []

    def parse(path, **kwargs):
        calls.append(kwargs["parse_options"])
        return []

    monkeypatch.setattr(uploads, "AutoFileParser", parse)
    monkeypatch.setattr(uploads, "configure_molecular_graph_reconstruction", lambda: None)
    monkeypatch.setattr(uploads, "_parsed_artifact_from_chem_file", lambda *a, **k: "parsed")
    if entrypoint == "worker":
        assert uploads._parse_calculation_path_worker("example.log", None) == ("parsed", None)
    elif entrypoint == "validation":
        assert uploads._parse_calculation_output(b"source", "example.log") == "parsed"
    else:
        assert (
            uploads.infer_transition_states_from_calculation_output(b"source", "example.log") == ()
        )
    assert len(calls) == 1
    assert calls[0].source_decode_errors == "surrogateescape"
    assert calls[0].capture_source_evidence
    assert calls[0].release_file_content


@pytest.mark.parametrize("suffix", [b"\xe2\n \x80\x93", b"\xff"])
def test_native_gaussian_tolerance_retains_original_artifact_bytes(tmp_path, suffix):
    fixture = (
        Path(__file__).parents[1]
        / "fixtures/da_bench_minimal/reac/000000000000/000000000000_02.ene.log.gz"
    )
    payload = (
        gzip.decompress(fixture.read_bytes()) + b"\nNon-structural annotation: " + suffix + b"\n"
    )
    source = tmp_path / "source.log"
    source.write_bytes(payload)
    parsed, error = uploads._parse_calculation_path_worker(str(source), None)
    assert error is None
    assert parsed is not None
    assert parsed.source_frame_count > 0
    assert parsed.chem_file.artifact_sha256 == sha256(payload).hexdigest()
    assert parsed.chem_file.artifact_size_bytes == len(payload)
    assert source.read_bytes() == payload
    assert "tricycle_input_transform" not in parsed.chem_file.parser_provenance.effective_config


def test_native_archive_comments_are_safe_for_unicode_json():
    fixture = (
        Path(__file__).parents[1]
        / "fixtures/da_bench_minimal/reac/000000000000/000000000000_02.ene.log.gz"
    )
    payload = gzip.decompress(fixture.read_bytes())
    marker = b"\\\\00000000000_best_conf_00\\\\"
    assert marker in payload
    payload = payload.replace(marker, b"\\\\TS6a-2\xe2\x80\n \x99\\\\")
    parsed = uploads._parse_calculation_output(payload, "wrapped-comment.log")
    assert len(parsed.frame_records) == parsed.source_frame_count > 0
    comments = [record.frame.comments for record in parsed.frame_records]
    comments.append(parsed.chem_file.comments.model_dump(mode="json"))
    encoded = json.dumps(comments, ensure_ascii=False)
    assert "TS6a-2" in encoded
    encoded.encode("utf-8", errors="strict")
    assert parsed.chem_file.artifact_sha256 == sha256(payload).hexdigest()
