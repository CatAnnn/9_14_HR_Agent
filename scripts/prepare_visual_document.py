from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.visual_document_preprocessor import (  # noqa: E402
    VisualDocumentPreprocessor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert one visual PDF into processed Markdown using MinerU, "
            "native PDF text recovery, and selective full-page vision."
        )
    )
    parser.add_argument("source", type=Path, help="Source PDF to parse.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination .md file consumed by the processed knowledge pipeline.",
    )
    parser.add_argument(
        "--skip-vision",
        action="store_true",
        help=(
            "Keep MinerU plus native-layout recovery without rendering or calling "
            "the downstream full-page vision model; MinerU backend behavior is unchanged."
        ),
    )
    parser.add_argument(
        "--strict-vision",
        action="store_true",
        help="Fail instead of degrading when selective visual analysis fails.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing destination file.",
    )
    return parser


def _write_text_atomic(
    path: Path,
    text: str,
    *,
    overwrite: bool = True,
) -> None:
    """Publish complete Markdown atomically with a predictable readable mode."""

    temporary_path: Path | None = None
    try:
        existing_mode: int | None = None
        if overwrite:
            try:
                existing_mode = stat.S_IMODE(path.stat().st_mode)
            except FileNotFoundError:
                pass
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(existing_mode if existing_mode is not None else 0o644)
        if overwrite:
            temporary_path.replace(path)
        else:
            # Same-directory hard-link publication is an atomic no-replace
            # operation: a target created while parsing causes FileExistsError
            # instead of silently overwriting the other worker's output.
            os.link(temporary_path, path)
            temporary_path.unlink()
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    if source.suffix.lower() != ".pdf":
        raise SystemExit("source must be a PDF")
    if not source.is_file():
        raise SystemExit(f"source PDF does not exist: {source}")
    if output.suffix.lower() != ".md":
        raise SystemExit("--output must end with .md")
    if output.exists() and not args.force:
        raise SystemExit(f"output already exists; pass --force to replace it: {output}")

    result = VisualDocumentPreprocessor().prepare(
        source,
        enable_vision=not args.skip_vision,
        strict_vision=bool(args.strict_vision),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_text_atomic(output, result.text, overwrite=bool(args.force))
    except FileExistsError:
        raise SystemExit(
            f"output was created while processing; pass --force to replace it: {output}"
        ) from None
    metadata = result.metadata
    print(
        json.dumps(
            {
                "source": str(source),
                "output": str(output),
                "markdown_chars": len(result.text),
                "page_count": len(result.pages),
                "block_count": len(result.blocks),
                "native_layout_fallback_count": metadata.get(
                    "native_layout_fallback_count", 0
                ),
                "visual_candidate_pages": metadata.get(
                    "visual_candidate_pages", []
                ),
                "document_vision": metadata.get("document_vision", {}),
                "warnings": metadata.get("document_vision_warnings", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
