from __future__ import annotations

from pathlib import Path

from backend.exceptions.parser_errors import UnsupportedFileTypeError
from backend.parsers.organization_unit_xlsx import OrganizationUnitXlsxParser
from backend.parsers.structured_markdown import StructuredMarkdownParser


class ParserRouter:
    def __init__(self):
        self.markdown = StructuredMarkdownParser()
        self.organization_unit_xlsx = OrganizationUnitXlsxParser()
        self.processed_extensions = {".md", ".xlsx"}

    def parse_file(self, path: Path) -> tuple[str, dict]:
        suffix = path.suffix.lower()
        if suffix == ".xlsx" and self.organization_unit_xlsx.matches(path):
            text = self.organization_unit_xlsx.parse(path)
            return text, {
                "parser": "organization_unit_xlsx",
                **dict(self.organization_unit_xlsx.last_metadata),
            }
        if suffix == ".md":
            return self.markdown.parse(path)
        raise UnsupportedFileTypeError(
            "Knowledge indexing accepts only processed Markdown and the "
            "validated organization-unit workbook. Raw PDF/DOCX/PPTX and "
            f"generic XLSX inputs are not supported: {path}"
        )
