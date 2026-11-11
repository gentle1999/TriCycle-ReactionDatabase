import uvicorn

from tricycle_reaction_db.core.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "tricycle_reaction_db.api.app:app",
        host=settings.runtime.api_host,
        port=settings.runtime.api_port,
        reload=settings.runtime.debug,
    )


if __name__ == "__main__":
    main()
