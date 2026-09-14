from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config.settings import get_settings  # noqa: E402
from backend.parsers.parser_router import ParserRouter  # noqa: E402
from backend.vectorstore.index_manager import IndexManager  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate processed Markdown/XLSX structure without calling "
            "Embedding, Reranker, or PostgreSQL."
        )
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="Processed knowledge root; defaults to data/kb_raw.",
    )
    return parser


def validate(raw_dir: Path | None = None) -> dict[str, object]:
    settings = get_settings()
    root = (raw_dir or settings.data_dir / "kb_raw").resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Processed knowledge root does not exist: {root}")

    files = sorted(path for path in root.rglob("*") if path.is_file())
    unsupported = [
        str(path.relative_to(root))
        for path in files
        if path.suffix.lower() not in IndexManager._SUPPORTED_EXTENSIONS
    ]
    if unsupported:
        raise RuntimeError(
            "Processed knowledge root contains unsupported files: "
            + ", ".join(unsupported)
        )

    manager = object.__new__(IndexManager)
    manager.settings = settings
    manager.parser = ParserRouter()
    chunks_by_scope, documents = manager._build_chunks(root)
    chunks = [
        chunk
        for scope_chunks in chunks_by_scope.values()
        for chunk in scope_chunks
    ]
    if not chunks or not documents:
        raise RuntimeError("Processed knowledge validation produced no documents.")

    oversized = [
        chunk.chunk_id
        for chunk in chunks
        if len(chunk.text) > settings.kb_chunk_size
    ]
    missing_parent = [
        chunk.chunk_id
        for chunk in chunks
        if not str((chunk.metadata or {}).get("parent_section_id") or "")
    ]
    missing_search_text = [
        chunk.chunk_id
        for chunk in chunks
        if not str((chunk.metadata or {}).get("search_text") or "").strip()
    ]
    wrong_version = [
        chunk.chunk_id
        for chunk in chunks
        if str((chunk.metadata or {}).get("index_version") or "")
        != settings.kb_index_version
    ]
    errors = {
        "oversized_chunks": oversized,
        "missing_parent_sections": missing_parent,
        "missing_search_text": missing_search_text,
        "wrong_index_version": wrong_version,
    }
    failed = {name: values for name, values in errors.items() if values}
    if failed:
        raise RuntimeError(
            "Structured knowledge validation failed: "
            + json.dumps(failed, ensure_ascii=False, sort_keys=True)
        )

    all_sections = [
        section
        for document in documents
        for section in document.get("_sections") or []
    ]
    content_ids = [
        str((chunk.metadata or {}).get("canonical_content_id") or "")
        for chunk in chunks
    ]
    duplicate_groups = sum(
        1 for count in Counter(content_ids).values() if count > 1
    )
    exact_fact_count = sum(
        len((chunk.metadata or {}).get("exact_facts") or [])
        for chunk in chunks
    )
    return {
        "event": "structured_kb_validation_passed",
        "raw_dir": str(root),
        "index_version": settings.kb_index_version,
        "chunk_size": settings.kb_chunk_size,
        "chunk_overlap": settings.kb_chunk_overlap,
        "file_count": len(files),
        "markdown_count": sum(path.suffix.lower() == ".md" for path in files),
        "organization_workbook_count": sum(
            path.suffix.lower() == ".xlsx" for path in files
        ),
        "document_count": len(documents),
        "scope_count": len(chunks_by_scope),
        "scopes": sorted(chunks_by_scope),
        "section_count": len(all_sections),
        "chunk_count": len(chunks),
        "max_parent_section_chars": max(
            (len(str(section.get("text") or "")) for section in all_sections),
            default=0,
        ),
        "max_child_chunk_chars": max(len(chunk.text) for chunk in chunks),
        "exact_fact_count": exact_fact_count,
        "duplicate_content_groups": duplicate_groups,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        json.dumps(
            validate(args.raw_dir),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
