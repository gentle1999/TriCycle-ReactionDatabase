"""Upstream text tolerance must survive the real PostgreSQL JSONB boundary."""

import gzip
import json
import os
from hashlib import sha256
from pathlib import Path

import psycopg
import pytest

from tricycle_reaction_db.application.services.artifact_uploads import _parse_calculation_output
from tricycle_reaction_db.core.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1", reason="requires PostgreSQL"
    ),
]


def test_wrapped_utf8_comment_survives_postgresql_copy():
    fixture = (
        Path(__file__).parents[1]
        / "fixtures/da_bench_minimal/reac/000000000000/000000000000_02.ene.log.gz"
    )
    payload = gzip.decompress(fixture.read_bytes())
    marker = b"\\\\00000000000_best_conf_00\\\\"
    assert marker in payload
    payload = payload.replace(marker, b"\\\\TS6a-2\xe2\x80\n \x99\\\\")
    parsed = _parse_calculation_output(payload, "wrapped-comment.log")
    assert len(parsed.frame_records) == parsed.source_frame_count > 0
    comments = [record.frame.comments for record in parsed.frame_records]
    comments.append(parsed.chem_file.comments.model_dump(mode="json"))
    assert "TS6a-2" in json.dumps(comments)
    assert parsed.chem_file.artifact_sha256 == sha256(payload).hexdigest()
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection:
        connection.execute(
            "CREATE TEMP TABLE comment_jsonb_regression (payload jsonb) ON COMMIT DROP"
        )
        with (
            connection.cursor() as cursor,
            cursor.copy("COPY comment_jsonb_regression FROM STDIN") as copy,
        ):
            for comment in comments:
                copy.write_row((json.dumps(comment),))
        assert connection.execute("SELECT count(*) FROM comment_jsonb_regression").fetchone()[
            0
        ] == len(comments)
