import csv
import io
import os

import pytest

from tricycle_reaction_db.application.services import ReactionThermodynamicAnalyticsService

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("TRICYCLE_RUN_DATABASE_TESTS") != "1",
        reason="set TRICYCLE_RUN_DATABASE_TESTS=1 to run database tests",
    ),
]


@pytest.mark.asyncio
async def test_statistics_and_export_cover_the_same_visible_profiles(
    development_query_principal: object,
) -> None:
    del development_query_principal
    statistics = await ReactionThermodynamicAnalyticsService.statistics()
    export_stream = await ReactionThermodynamicAnalyticsService.export_csv()
    payload = "".join([chunk async for chunk in export_stream])
    rows = list(csv.DictReader(io.StringIO(payload)))

    assert len(rows) == statistics.profile_count
    assert sum(item.count for item in statistics.activation_gibbs_free_energy_kcal_mol) == (
        statistics.activation_profile_count
    )
    assert sum(item.count for item in statistics.reaction_gibbs_free_energy_kcal_mol) == (
        statistics.reaction_profile_count
    )
    assert len(statistics.scatter) == min(statistics.complete_profile_count, 1_000)
