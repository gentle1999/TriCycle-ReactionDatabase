import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from tricycle_reaction_db.application.dtos.query_views import (
    LogicalReactionDetail,
    MappedReactionSummary,
)
from tricycle_reaction_db.application.services import reaction_compatibility as compatibility
from tricycle_reaction_db.application.services.queries import (
    logical_reaction_filter_expression_predicate,
)
from tricycle_reaction_db.db.models import LogicalReaction, MappedReaction


@pytest.mark.parametrize("field", sorted(compatibility.FALLBACK_FILTERS))
def test_advanced_filter_accepts_boolean_and_rejects_text(field):
    for value in (True, False):
        expression = json.dumps(
            {"operator": "and", "conditions": [{"field": field, "value": value}]}
        )
        predicate = logical_reaction_filter_expression_predicate(expression, None, [])
        assert "EXISTS" in str(predicate)
    expression = json.dumps({"operator": "and", "conditions": [{"field": field, "value": "false"}]})
    with pytest.raises(ValueError, match="must be a boolean"):
        logical_reaction_filter_expression_predicate(expression, None, [])


@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("field", sorted(compatibility.FALLBACK_FILTERS))
def test_filter_is_correlated_exists_without_graph_matching(mapped, field):
    model = MappedReaction if mapped else LogicalReaction
    statement = select(model.id).where(compatibility.fallback_predicate(field, mapped=mapped))
    sql = str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "EXISTS" in sql
    assert "endpoint_validation" in sql
    assert "coalesce" in sql
    assert f"= {model.__tablename__}.id" in sql
    assert "molecular_topology" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sides, single, dual",
    [
        ([], False, False),
        ([(True, False)], True, False),
        ([(False, True)], True, False),
        ([(True, True)], False, True),
        ([(True, False), (False, True)], True, False),
        ([(True, False), (True, True)], True, True),
    ],
)
async def test_summary_preserves_mixed_evidence_and_same_inference_semantics(
    monkeypatch, sides, single, dual
):
    logical_id, mapped_id = uuid4(), uuid4()
    mapped = MappedReactionSummary(
        id=mapped_id,
        logical_reaction_id=logical_id,
        mapped_reaction_key="m",
        mapped_reaction_kind="other",
        mapped_reaction_smiles="[H:1]>>[H:1]",
        mapping_hash="h",
        reaction_structural_bfp_schema_version="test",
    )
    result = LogicalReactionDetail(
        id=logical_id,
        reaction_key="r",
        reaction_hash="h",
        mapped_reaction_count=1,
        participants=[],
        mapped_reactions=[mapped],
    )
    calls = []

    class Session:
        async def exec(self, statement):
            calls.append(statement)
            return SimpleNamespace(all=lambda: [(logical_id, mapped_id, n, p) for n, p in sides])

    @asynccontextmanager
    async def session():
        yield Session()

    monkeypatch.setattr(compatibility, "session_factory", session)
    updated = await compatibility.annotate_reaction_compatibility(result)
    assert not result.has_compatibility_endpoints
    assert len(calls) == 1
    for item in [updated, *updated.mapped_reactions]:
        assert item.has_compatibility_endpoints == (single or dual)
        assert item.has_single_endpoint_fallback == single
        assert item.has_dual_endpoint_fallback == dual
