from __future__ import annotations

from pathlib import Path

from backend.parsers.markitdown_parser import MarkItDownParser
from backend.parsers.text_parser import TextParser
from backend.schemas.document import ParsedDocument


class DocumentPipeline:
    def __init__(self):
        self.text_parser = TextParser()

    def parse_text(self, text: str, document_id: str, filename: str | None = None) -> ParsedDocument:
        return self.text_parser.parse(text=text, document_id=document_id, filename=filename)

    def parse_file(self, path: Path, document_id: str) -> ParsedDocument:
        # Uploads can be parsed concurrently in worker threads. Keep metadata
        # request-local instead of sharing one mutable parser instance.
        upload_parser = MarkItDownParser()
        text = upload_parser.parse(path)
        metadata = dict(upload_parser.last_metadata)
        parsed = ParsedDocument(
            document_id=document_id,
            filename=path.name,
            text=text,
            pages=list(metadata.get("pages") or []),
            blocks=list(metadata.get("structured_blocks") or []),
            tables=list(metadata.get("tables") or []),
            images=list(metadata.get("images") or []),
            metadata=metadata,
        )
        return parsed
