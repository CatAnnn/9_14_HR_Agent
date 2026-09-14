from __future__ import annotations

from backend.schemas.document import ParsedDocument


class TextParser:
    def parse(self, text: str, document_id: str, filename: str | None = None) -> ParsedDocument:
        cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if not cleaned:
            raise ValueError("输入文本为空。")
        return ParsedDocument(document_id=document_id, filename=filename, text=cleaned, metadata={"parser": "pasted_text"})
