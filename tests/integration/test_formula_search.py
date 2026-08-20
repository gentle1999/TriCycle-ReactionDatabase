import asyncio
import hashlib
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func
from sqlmodel import Session, select

from tricycle_reaction_db.api.app import create_app
from tricycle_reaction_db.api.apps import use_case_rest_app
from tricycle_reaction_db.application.dtos import MolecularFormulaRangeQuery
from tricycle_reaction_db.application.services.queries import (
    MolecularFormulaQueryService,
    molecular_formula_range_predicates,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import MolecularFormula
from tricycle_reaction_db.db.session import dispose_engine
from tricycle_reaction_db.domain.formulas import ELEMENT_COUNT_VECTOR_SIZE

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _ranges(
    *,
    minimum: dict[int, int] | None = None,
    maximum: dict[int, int] | None = None,
) -> MolecularFormulaRangeQuery:
    minimum_counts: list[int | None] = [None] * ELEMENT_COUNT_VECTOR_SIZE
    maximum_counts: list[int | None] = [None] * ELEMENT_COUNT_VECTOR_SIZE
    for atomic_number, count in (minimum or {}).items():
        minimum_counts[atomic_number - 1] = count
    for atomic_number, count in (maximum or {}).items():
        maximum_counts[atomic_number - 1] = count
    return MolecularFormulaRangeQuery(
        minimum_counts=minimum_counts,
        maximum_counts=maximum_counts,
    )


def _formula(hill_formula: str, vector: list[int], suffix: str) -> MolecularFormula:
    composition = [
        {"atomic_number": index + 1, "isotope": 0, "count": count}
        for index, count in enumerate(vector)
        if count
    ]
    digest = hashlib.sha256(f"formula-search-{suffix}-{uuid4()}".encode()).hexdigest()
    return MolecularFormula(
        hill_formula=hill_formula,
        composition=composition,
        composition_schema_version="formula-composition-v1",
        atom_count=sum(vector),
        composition_hash=digest,
        element_count_vector=vector,
    )


@pytest.fixture
def inserted_formulas() -> Iterator[list[MolecularFormula]]:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    formulas: list[MolecularFormula] = []
    for suffix, carbon, hydrogen in (("low", 6, 10), ("high", 8, 14), ("outside", 5, 20)):
        vector = [0] * ELEMENT_COUNT_VECTOR_SIZE
        vector[0] = hydrogen
        vector[5] = carbon
        vector[117] = 2
        formulas.append(_formula(f"C{carbon}H{hydrogen}Og2", vector, suffix))
    try:
        with Session(engine, expire_on_commit=False) as session:
            session.add_all(formulas)
            session.commit()
        yield formulas
    finally:
        with Session(engine) as session:
            for formula in formulas:
                if formula.id is not None:
                    persisted = session.get(MolecularFormula, formula.id)
                    if persisted is not None:
                        session.delete(persisted)
            session.commit()
        engine.dispose()


def test_postgresql_array_subscript_predicates_are_inclusive(
    inserted_formulas: list[MolecularFormula],
) -> None:
    query = _ranges(minimum={6: 6, 1: 10, 118: 2}, maximum={6: 8, 1: 14, 118: 2})
    predicates = molecular_formula_range_predicates(query)
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(MolecularFormula).where(*predicates).order_by(MolecularFormula.hill_formula)
            ).all()
            assert [row.hill_formula for row in rows] == ["C6H10Og2", "C8H14Og2"]
            assert session.exec(select(func.count()).select_from(MolecularFormula)).one() >= 3
    finally:
        engine.dispose()


def test_formula_search_service_returns_filtered_page(
    inserted_formulas: list[MolecularFormula],
) -> None:
    result = asyncio.run(
        MolecularFormulaQueryService.search_formulas(
            **_ranges(
                minimum={6: 6, 118: 2},
                maximum={6: 8, 1: 14, 118: 2},
            ).model_dump(),
            limit=1,
            offset=1,
        )
    )

    assert result.page.total == 2
    assert result.page.limit == 1
    assert result.page.offset == 1
    assert [item.hill_formula for item in result.items] == ["C8H14Og2"]
    assert len(result.items[0].element_count_vector) == ELEMENT_COUNT_VECTOR_SIZE


@pytest.mark.asyncio
async def test_formula_search_rest_endpoint_returns_filtered_page(
    inserted_formulas: list[MolecularFormula],
) -> None:
    minimum_counts: list[int | None] = [None] * ELEMENT_COUNT_VECTOR_SIZE
    maximum_counts: list[int | None] = [None] * ELEMENT_COUNT_VECTOR_SIZE
    minimum_counts[5] = 6
    minimum_counts[117] = 2
    maximum_counts[5] = 8
    maximum_counts[0] = 14
    maximum_counts[117] = 2
    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/formulas/search?limit=10",
                json={"minimum_counts": minimum_counts, "maximum_counts": maximum_counts},
            )
    finally:
        await dispose_engine()

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == {"total": 2, "limit": 10, "offset": 0}
    assert [item["hill_formula"] for item in payload["items"]] == ["C6H10Og2", "C8H14Og2"]


@pytest.mark.asyncio
async def test_nexusx_generated_formula_search_rest_route(
    inserted_formulas: list[MolecularFormula],
) -> None:
    ranges = _ranges(minimum={6: 6, 118: 2}, maximum={6: 8, 1: 14, 118: 2})
    transport = ASGITransport(app=use_case_rest_app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/molecular_formula_query_service/search_formulas",
                json={**ranges.model_dump(), "limit": 10},
            )
    finally:
        await dispose_engine()

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == {"total": 2, "limit": 10, "offset": 0}
    assert [item["hill_formula"] for item in payload["items"]] == ["C6H10Og2", "C8H14Og2"]
    assert all(
        len(item["element_count_vector"]) == ELEMENT_COUNT_VECTOR_SIZE for item in payload["items"]
    )
