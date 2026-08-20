from datetime import UTC, datetime, timedelta

import pytest

from tricycle_reaction_db.application.services.storage_gc import (
    StorageGarbageCollectionSettings,
    hourly_partition_prefixes,
)


def test_hourly_partition_prefixes_are_bounded_and_padded_once() -> None:
    prefixes = hourly_partition_prefixes(
        root_prefix="uploads",
        scan_after=datetime(2026, 8, 10, 10, 30, tzinfo=UTC),
        scan_until=datetime(2026, 8, 10, 12, 5, tzinfo=UTC),
        clock_skew=timedelta(hours=1),
    )
    assert prefixes == (
        "uploads/2026/08/10/09",
        "uploads/2026/08/10/10",
        "uploads/2026/08/10/11",
        "uploads/2026/08/10/12",
        "uploads/2026/08/10/13",
    )


def test_gc_settings_require_an_initial_window_longer_than_grace() -> None:
    with pytest.raises(ValueError):
        StorageGarbageCollectionSettings(
            _env_file=None,
            grace_period_seconds=3600,
            initial_lookback_seconds=3600,
        )
