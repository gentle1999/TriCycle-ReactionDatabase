from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal, cast

from alembic import context
from molalchemy import alembic_helpers
from molalchemy.rdkit.types import RdkitBitFingerprint, RdkitMol
from sqlalchemy import engine_from_config, pool
from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlmodel.sql.sqltypes import AutoString

from tricycle_reaction_db.core.config import get_settings
from tricycle_reaction_db.db.models import metadata
from tricycle_reaction_db.db.types import NumpyArray

config = context.config

# MolAlchemy 0.0.7 does not register cartridge types for PostgreSQL reflection.
ischema_names.setdefault("mol", RdkitMol)
ischema_names.setdefault("bfp", RdkitBitFingerprint)

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _database_url() -> str:
    return get_settings().database_url


def _render_item(type_: str, obj: object, autogen_context: object) -> str | Literal[False]:
    if type_ == "type" and isinstance(obj, NumpyArray):
        return "sa.LargeBinary()"
    if type_ == "type" and isinstance(obj, AutoString):
        length = f"length={obj.length}" if obj.length is not None else ""
        return f"sa.String({length})"
    rendered = alembic_helpers.render_item(  # type: ignore[no-untyped-call]
        type_, obj, autogen_context
    )
    return cast(str | Literal[False], rendered)


def _context_options() -> dict[str, Any]:
    return {
        "compare_server_default": True,
        "compare_type": True,
        "render_item": _render_item,
        "target_metadata": target_metadata,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_context_options(),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url().replace("%", "%%")
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, **_context_options())

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
