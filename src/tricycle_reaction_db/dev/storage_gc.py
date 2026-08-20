"""Run one incremental RustFS garbage-collection pass."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from sqlalchemy import create_engine

from tricycle_reaction_db.application.services.storage_gc import (
    StorageGarbageCollectionSettings,
    run_incremental_storage_gc,
)
from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.storage.rustfs import RustFSObjectStore, RustFSSettings


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def main() -> None:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection, RustFSObjectStore(RustFSSettings()) as store:
            store.ensure_bucket()
            result = run_incremental_storage_gc(
                connection,
                store,
                settings=StorageGarbageCollectionSettings(),
            )
        print(json.dumps(asdict(result), sort_keys=True, default=_json_default))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
