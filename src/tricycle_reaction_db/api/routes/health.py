from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from tricycle_reaction_db import __version__
from tricycle_reaction_db.db.session import get_database_status

router = APIRouter(prefix="/health", tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ReadyResponse(LiveResponse):
    database: str
    postgresql_version: str
    rdkit_extension_version: str


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    return LiveResponse(version=__version__)


@router.get("/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse:
    try:
        database = await get_database_status()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database is unavailable",
        ) from exc

    return ReadyResponse(version=__version__, **database)
