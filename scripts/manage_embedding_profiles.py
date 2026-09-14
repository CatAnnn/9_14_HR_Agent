from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.repositories.postgres_repository import (  # noqa: E402
    PostgresRepository,
)
from backend.vectorstore.index_manager import IndexManager  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List, build, verify, or clean embedding repositories selected "
            "by model name and vector dimensions."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="List every embedding profile.")

    build = commands.add_parser(
        "build",
        help="Build and atomically activate a complete embedding profile.",
    )
    _add_profile_arguments(build)
    build.add_argument(
        "--provider-mode",
        choices=("local", "platform"),
        default=None,
        help=(
            "Transport used only while generating vectors. It is not part "
            "of the profile identity."
        ),
    )

    verify = commands.add_parser(
        "verify",
        help="Verify that one profile exactly covers the current KB.",
    )
    _add_profile_arguments(verify)

    clean = commands.add_parser(
        "clean",
        help="Delete inactive builds, optionally including the profile.",
    )
    _add_profile_arguments(clean)
    clean.add_argument(
        "--remove-profile",
        action="store_true",
        help="Also remove the profile and its active build.",
    )
    clean.add_argument(
        "--force",
        action="store_true",
        help="Allow removal when this is the configured runtime profile.",
    )
    return parser.parse_args(argv)


def _add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help="Exact embedding model name; defaults to the configured model.",
    )
    parser.add_argument(
        "--dimensions",
        type=int,
        default=None,
        help=(
            "Vector dimensions; defaults to EMBEDDING_LOCAL_DIMENSIONS or "
            "EMBEDDING_PLATFORM_DIMENSIONS for the active provider mode."
        ),
    )


def _profile_arguments(args: argparse.Namespace) -> tuple[str, int]:
    settings = get_settings()
    model = str(
        args.model or settings.effective_embedding_model
    ).strip()
    dimensions = int(
        args.dimensions or settings.effective_embedding_dimensions
    )
    return model, dimensions


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    settings = get_settings()
    if args.command == "build":
        model, dimensions = _profile_arguments(args)
        summary = IndexManager(settings=settings).build_embedding_profile(
            model_name=model,
            dimensions=dimensions,
            provider_mode=args.provider_mode,
        )
        return summary, 0

    repository = PostgresRepository(
        database_url=settings.admin_database_url
    )
    if args.command == "list":
        current_profile_id = settings.embedding_profile_id
        return (
            {
                "current_profile_id": current_profile_id,
                "profiles": [
                    {
                        **profile.as_dict(),
                        "configured": (
                            profile.profile_id == current_profile_id
                        ),
                    }
                    for profile in repository.list_embedding_profiles()
                ],
            },
            0,
        )

    model, dimensions = _profile_arguments(args)
    if args.command == "verify":
        state = repository.embedding_profile_state(model, dimensions)
        return {"embedding_profile": state.as_dict()}, 0 if state.ready else 2

    if args.command == "clean":
        return (
            repository.clean_embedding_profile(
                model,
                dimensions,
                remove_profile=bool(args.remove_profile),
                allow_active_removal=bool(args.force),
            ),
            0,
        )
    raise RuntimeError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload, exit_code = run(args)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
