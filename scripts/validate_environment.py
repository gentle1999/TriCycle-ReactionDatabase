"""Validate environment settings before starting or building services."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import ValidationError

from tricycle_reaction_db.core.config import (
    ComposeBuildSettings,
    FrontendBuildSettings,
    load_configuration,
    load_project_configuration,
    load_settings_model,
)


def validate_environment(target: str, env_file: Path) -> None:
    if target == "all":
        load_project_configuration(_env_file=env_file)
        return
    if target == "backend":
        load_configuration(_env_file=env_file)
    if target == "frontend":
        load_settings_model(FrontendBuildSettings, env_file)
    if target == "compose":
        load_settings_model(ComposeBuildSettings, env_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=("backend", "frontend", "compose", "all"), default="all"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        validate_environment(args.target, args.env_file)
    except ValidationError as exc:
        for error in exc.errors(include_input=False):
            location = ".".join(str(part) for part in error["loc"]) or args.target
            parser.exit(2, f"invalid configuration: {location}: {error['msg']}\n")
    print(f"Valid {args.target} configuration")


if __name__ == "__main__":
    main()
