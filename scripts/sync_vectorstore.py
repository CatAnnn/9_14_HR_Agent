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
from backend.services.embedding_service import EmbeddingService  # noqa: E402
from backend.services.resumable_embedding_service import (  # noqa: E402
    ResumableEmbeddingService,
)
from backend.vectorstore.index_manager import IndexManager  # noqa: E402


DATASET_CHOICES = ("all", "core", "organization_unit")


class _CheckOnlyEmbeddingService:
    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise RuntimeError("Embedding is unavailable during a source-only check.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally synchronize changed knowledge-base sources with a "
            "persistent, resumable ingestion-embedding cache."
        )
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        help=(
            "Override the configured raw knowledge-base directory. Requires "
            "--dataset core or --dataset organization_unit."
        ),
    )
    parser.add_argument(
        "--dataset",
        choices=DATASET_CHOICES,
        default="all",
        help="Knowledge dataset to synchronize (default: all).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and list source files without embedding or database writes.",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        help=(
            "SQLite cache path. Defaults to "
            "<runtime_dir>/kb_embedding_cache.sqlite3."
        ),
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable the persistent embedding cache for this run.",
    )
    return parser


def _print_json(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True),
        flush=True,
    )


def _disabled_cache_stats() -> dict[str, Any]:
    return {
        "enabled": False,
        "cache_path": None,
        "embed_call_count": 0,
        "requested_text_count": 0,
        "cache_hit_count": 0,
        "cache_miss_count": 0,
        "embedded_text_count": 0,
        "cache_write_count": 0,
        "invalid_cache_entry_count": 0,
    }


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


def _legacy_raw_dir(roots: dict[str, Path], *, dataset: str) -> Path:
    if dataset == "all":
        return roots["core"]
    return roots[dataset]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.raw_dir is not None and args.dataset == "all":
        parser.error(
            "--raw-dir cannot be used with --dataset all; choose "
            "--dataset core or --dataset organization_unit"
        )

    settings = get_settings()
    source_roots = _effective_source_roots(
        settings,
        dataset=args.dataset,
        raw_dir=args.raw_dir,
    )
    raw_dir = _legacy_raw_dir(source_roots, dataset=args.dataset)
    cache_path = args.cache_path or (
        settings.runtime_dir / "kb_embedding_cache.sqlite3"
    )
    resume_enabled = not args.no_resume and not args.check

    _print_json(
        {
            "event": "kb_sync_effective_config",
            "raw_dir": raw_dir,
            "source_roots": source_roots,
            "dataset": args.dataset,
            "check_only": bool(args.check),
            "resume_enabled": resume_enabled,
            "cache_path": cache_path if not args.check else None,
            "embedding_model": settings.effective_embedding_model,
            "embedding_dimensions": settings.effective_embedding_dimensions,
            "embedding_profile_id": settings.embedding_profile_id,
            "ingest_batch_size": settings.kb_ingest_batch_size,
        }
    )

    if args.check:
        manager = IndexManager(
            settings=settings,
            embedding_service=_CheckOnlyEmbeddingService(),
            initialize_repository=False,
        )
        try:
            summary = manager.check_sources(
                raw_dir=args.raw_dir,
                dataset=args.dataset,
            )
        except Exception as exc:
            _print_json(
                {
                    "event": "kb_source_check_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            raise
        _print_json({"event": "kb_source_check_complete", **summary})
        return 0

    base_embedding_service = EmbeddingService(settings=settings)
    resumable_service: ResumableEmbeddingService | None = None
    embedding_service: Any = base_embedding_service
    if resume_enabled:
        resumable_service = ResumableEmbeddingService(
            base_embedding_service,
            cache_path=cache_path,
            profile_id=settings.embedding_profile_id,
            dimensions=settings.effective_embedding_dimensions,
            progress_callback=lambda stats: _print_json(
                {"event": "kb_embedding_cache_progress", **stats}
            ),
        )
        embedding_service = resumable_service

    manager = IndexManager(
        settings=settings,
        embedding_service=embedding_service,
    )
    try:
        summary = manager.sync_changed(
            raw_dir=args.raw_dir,
            dataset=args.dataset,
        )
    except Exception as exc:
        cache_stats = (
            resumable_service.stats
            if resumable_service is not None
            else _disabled_cache_stats()
        )
        _print_json(
            {
                "event": "kb_sync_failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "cache_stats": cache_stats,
            }
        )
        raise
    else:
        cache_stats = (
            resumable_service.stats
            if resumable_service is not None
            else _disabled_cache_stats()
        )
        _print_json(
            {
                "event": "kb_sync_complete",
                **summary,
                "cache_stats": cache_stats,
            }
        )
        return 0
    finally:
        if resumable_service is not None:
            resumable_service.close()


if __name__ == "__main__":
    raise SystemExit(main())
