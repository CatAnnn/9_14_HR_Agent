from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.exceptions.parser_errors import ParserError, UnsupportedFileTypeError


class MarkItDownParser:
    """Parse user-uploaded documents with Microsoft's MarkItDown library."""

    supported_extensions = {
        ".csv",
        ".docx",
        ".html",
        ".htm",
        ".md",
        ".pdf",
        ".pptx",
        ".txt",
        ".xls",
        ".xlsx",
    }

    def __init__(self) -> None:
        self.last_metadata: dict[str, Any] = {}

    def parse(self, path: Path) -> str:
        started_at = time.perf_counter()
        suffix = path.suffix.lower()
        self.last_metadata = {
            "parser": "markitdown",
            "input_path": str(path),
            "input_suffix": suffix,
        }
        if suffix not in self.supported_extensions:
            raise UnsupportedFileTypeError(
                f"Unsupported upload file type: {suffix}. Supported file types: {sorted(self.supported_extensions)}"
            )
        if not path.exists():
            raise ParserError(f"待解析文件不存在: {path}")

        try:
            from markitdown import MarkItDown
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ParserError("MarkItDown 未安装。请重新构建 backend 镜像安装 backend/requirements.txt。") from exc

        try:
            result = MarkItDown().convert(str(path))
        except Exception as exc:  # noqa: BLE001
            raise ParserError(f"MarkItDown 解析失败: {path.name}: {exc}") from exc

        text = str(getattr(result, "text_content", "") or "").strip()
        if not text:
            raise ParserError(f"MarkItDown 未解析出文本内容: {path.name}")
        self.last_metadata.update(
            {
                "source_format": suffix.lstrip("."),
                "text_chars": len(text),
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            }
        )
        return text
