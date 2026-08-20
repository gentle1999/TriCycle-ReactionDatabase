import asyncio
import hashlib
import json
import os
from uuid import uuid4

import pytest
from rdkit import Chem
from sqlalchemy import create_engine, text
from sqlmodel import Session

from tricycle_reaction_db.application.query_cost import QueryBudgetExceeded
from tricycle_reaction_db.application.services import (
    LogicalReactionQueryService,
    MappedReactionQueryService,
)
from tricycle_reaction_db.application.services import queries as query_services
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import LogicalReaction, MappedReaction
from tricycle_reaction_db.domain.enums import MappedReactionKind, ReactionClass

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


def _hash(label: str) -> str:
    return hashlib.sha256(f"{label}-{uuid4()}".encode()).hexdigest()


def test_reaction_smarts_and_similarity_use_indexed_rdkit_projections(
    monkeypatch: pytest.MonkeyPatch,
    development_query_principal: object,
) -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    logical_ids = []
    logical_hashes = []
    mapped_hashes = []
    query_smiles = "[C:1]=[C:2]>>[C:1]-[C:2]"
    rare_smiles = "[Ra+2:1]>>[Ra+2:1]"
    try:
        with Session(engine, expire_on_commit=False) as session:
            for index, reaction_smiles in enumerate(
                (
                    query_smiles,
                    query_smiles,
                    "[H:1][H:2]>>[H:1][H:2]",
                    rare_smiles,
                )
            ):
                logical_hash = _hash(f"logical-{index}")
                logical = LogicalReaction(
                    reaction_key=f"reaction-search-{uuid4()}",
                    reaction_class=ReactionClass.CYCLOADDITION,
                    reaction_hash=logical_hash,
                )
                mapped = MappedReaction(
                    logical_reaction=logical,
                    mapped_reaction_key=f"path-{index}",
                    mapped_reaction_kind=MappedReactionKind.OTHER,
                    mapped_reaction_smiles=reaction_smiles,
                    mapping_hash=_hash(f"mapped-{index}"),
                )
                session.add(mapped)
                session.flush()
                assert logical.id is not None
                logical_ids.append(logical.id)
                logical_hashes.append(logical_hash)
                mapped_hashes.append(mapped.mapping_hash)
            session.commit()

        smarts = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                reaction_smarts=query_smiles,
                mapping_hash=mapped_hashes[0],
                limit=20,
                offset=0,
            )
        )
        assert any(
            item.mapped_reaction_smiles == query_smiles and item.reaction_smarts_match
            for item in smarts.items
        )

        reactant = Chem.MolFromSmiles("C=C")
        product = Chem.MolFromSmiles("CC")
        assert reactant is not None and product is not None
        logical_structure = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reaction_hash=logical_hashes[0],
                reactant_mol_block=Chem.MolToMolBlock(reactant),
                product_mol_block=Chem.MolToMolBlock(product),
                limit=20,
                offset=0,
            )
        )
        assert [item.id for item in logical_structure.items] == [logical_ids[0]]

        logical_and = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                filter_expression=json.dumps(
                    {
                        "operator": "and",
                        "conditions": [
                            {"field": "reaction_hash", "value": logical_hashes[0]},
                            {"field": "reaction_class", "value": "cycloaddition"},
                        ],
                    }
                ),
                limit=20,
                offset=0,
            )
        )
        assert [item.id for item in logical_and.items] == [logical_ids[0]]

        logical_or = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                filter_expression=json.dumps(
                    {
                        "operator": "or",
                        "conditions": [
                            {"field": "reaction_hash", "value": logical_hashes[0]},
                            {"field": "reaction_hash", "value": logical_hashes[1]},
                        ],
                    }
                ),
                limit=20,
                offset=0,
            )
        )
        assert {item.id for item in logical_or.items} == {logical_ids[0], logical_ids[1]}

        logical_not = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                filter_expression=json.dumps(
                    {
                        "operator": "and",
                        "conditions": [
                            {"field": "reaction_class", "value": "cycloaddition"},
                            {
                                "field": "reaction_hash",
                                "value": logical_hashes[0],
                                "negated": True,
                            },
                        ],
                    }
                ),
                limit=200,
                offset=0,
            )
        )
        assert logical_ids[0] not in {item.id for item in logical_not.items}
        assert logical_ids[1] in {item.id for item in logical_not.items}

        reactant_only = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                reactant_mol_block=Chem.MolToMolBlock(reactant),
                limit=200,
                offset=0,
            )
        )
        assert {item.id for item in reactant_only.items} >= {logical_ids[0], logical_ids[1]}

        product_only = asyncio.run(
            LogicalReactionQueryService.list_logical_reactions(
                product_mol_block=Chem.MolToMolBlock(product),
                limit=200,
                offset=0,
            )
        )
        assert {item.id for item in product_only.items} >= {logical_ids[0], logical_ids[1]}

        similar = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                similarity_reaction_smiles=query_smiles,
                minimum_similarity=0.999,
                limit=20,
                offset=0,
            )
        )
        exact = next(item for item in similar.items if item.mapped_reaction_smiles == query_smiles)
        assert exact.similarity_score == pytest.approx(1.0)
        assert exact.reaction_structural_bfp_schema_version == "reaction-structural-bfp-r5-v1"

        settings = get_settings().model_copy(update={"structure_candidate_limit": 1})
        monkeypatch.setattr(query_services, "get_settings", lambda: settings)
        with pytest.raises(QueryBudgetExceeded, match="candidate set exceeds the 1-row limit"):
            asyncio.run(
                MappedReactionQueryService.list_mapped_reactions(
                    reaction_smarts=query_smiles,
                    limit=1,
                    offset=0,
                )
            )

        indexed_smarts = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                reaction_smarts=rare_smiles,
                limit=1,
                offset=0,
            )
        )
        assert [item.mapped_reaction_smiles for item in indexed_smarts.items] == [rare_smiles]

        indexed_threshold = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                similarity_reaction_smiles=rare_smiles,
                minimum_similarity=1.0,
                limit=1,
                offset=0,
            )
        )
        assert [item.mapped_reaction_smiles for item in indexed_threshold.items] == [rare_smiles]

        nearest = asyncio.run(
            MappedReactionQueryService.list_mapped_reactions(
                similarity_reaction_smiles=rare_smiles,
                limit=1,
                offset=0,
            )
        )
        assert [item.mapped_reaction_smiles for item in nearest.items] == [rare_smiles]

        with engine.begin() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            smarts_plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT id FROM mapped_reaction "
                        "WHERE reaction @> reaction_from_smarts(CAST(:query AS cstring))"
                    ),
                    {"query": query_smiles},
                )
            )
            similarity_plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text(
                        "EXPLAIN (COSTS OFF) SELECT id FROM mapped_reaction ORDER BY "
                        "reaction_structural_bfp <%> reaction_structural_bfp("
                        "reaction_from_smiles(CAST(:query AS cstring)), 5) LIMIT 20"
                    ),
                    {"query": query_smiles},
                )
            )
        assert "ix_mapped_reaction_reaction_gist" in smarts_plan
        assert "ix_mapped_reaction_structural_bfp_gist" in similarity_plan
    finally:
        with Session(engine) as session:
            for logical_id in logical_ids:
                logical = session.get(LogicalReaction, logical_id)
                if logical is not None:
                    session.delete(logical)
            session.commit()
        engine.dispose()
