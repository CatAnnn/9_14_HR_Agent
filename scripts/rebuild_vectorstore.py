from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.vectorstore.index_manager import IndexManager  # noqa: E402


DATASET_CHOICES = ("all", "core", "organization_unit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild PostgreSQL knowledge resources from processed Markdown "
            "and the validated organization-unit workbook."
        )
    )
    parser.add_argument(
        "--vision-preflight-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--strict-images",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help=(
            "Remove documents absent from the selected dataset; with "
            "--dataset all, also remove absent collections."
        ),
    )
    parser.add_argument(
        "--dataset",
        choices=DATASET_CHOICES,
        default="all",
        help="Knowledge dataset to rebuild (default: all).",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        help=(
            "Override the configured raw knowledge-base directory. Requires "
            "--dataset core or --dataset organization_unit."
        ),
    )
    return parser


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True))


def _effective_source_roots(
    settings: Any,
    *,
    dataset: str,
    raw_dir: Path | None,
) -> dict[str, Path]:
    if raw_dir is not None:
        return {dataset: raw_dir}

    roots = {
        "core": Path(settings.kb_core_dir),
        "organization_unit": Path(settings.kb_organization_unit_dir),
    }
    if dataset == "all":
        return roots
    return {dataset: roots[dataset]}


def _accepted_inputs(dataset: str) -> list[str]:
    if dataset == "core":
        return ["markdown"]
    if dataset == "organization_unit":
        return ["organization_unit_xlsx"]
    return ["markdown", "organization_unit_xlsx"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.raw_dir is not None and args.dataset == "all":
        parser.error(
            "--raw-dir cannot be used with --dataset all; choose "
            "--dataset core or --dataset organization_unit"
        )
    if args.strict_images or args.vision_preflight_only:
        raise SystemExit(
            "Document vision/MinerU options were removed. Rebuild from "
            "processed Markdown and the validated organization-unit workbook."
        )
    settings = get_settings()
    source_roots = _effective_source_roots(
        settings,
        dataset=args.dataset,
        raw_dir=args.raw_dir,
    )
    raw_dir = (
        source_roots["core"]
        if args.dataset == "all"
        else source_roots[args.dataset]
    )
    manager = IndexManager(settings=settings)
    _print_json(
        {
            "event": "processed_kb_effective_config",
            "accepted_inputs": _accepted_inputs(args.dataset),
            "dataset": args.dataset,
            "raw_dir": raw_dir,
            "source_roots": source_roots,
            "chunk_size": settings.kb_chunk_size,
            "chunk_overlap": settings.kb_chunk_overlap,
            "index_version": settings.kb_index_version,
            "exact": bool(args.exact),
        }
    )

    chunks = manager.rebuild(
        raw_dir=args.raw_dir,
        dataset=args.dataset,
        exact=args.exact,
    )
    _print_json({"chunk_count": len(chunks), **manager.last_summary})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
