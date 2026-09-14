from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


_EBOOK_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_COVER_PREFIX = "/assets/resource/ebooks/covers/"


class EbookCatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EbookRecord:
    id: str
    source_file: str
    title: str
    authors: tuple[str, ...]
    publisher: str | None
    published_at: str | None
    summary: str
    tags: tuple[str, ...]
    cover: str
    file_path: Path
    cover_path: Path

    @property
    def download_filename(self) -> str:
        return f"{self.id}.epub"


class EbookCatalogService:
    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (
            project_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.ebook_root = (self.project_root / "Ebook").resolve()
        self.catalog_path = self.ebook_root / "catalog.json"
        self._records = self._load_and_validate()

    def all(self) -> tuple[EbookRecord, ...]:
        return tuple(self._records.values())

    def get(self, book_id: str) -> EbookRecord | None:
        return self._records.get(str(book_id or "").strip())

    def require(self, book_id: str) -> EbookRecord:
        record = self.get(book_id)
        if record is None:
            raise KeyError(book_id)
        return record

    def _load_and_validate(self) -> dict[str, EbookRecord]:
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EbookCatalogError(
                f"Invalid ebook catalog: {self.catalog_path}"
            ) from exc

        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise EbookCatalogError("Ebook catalog version must be 1.")
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise EbookCatalogError("Ebook catalog items must be a non-empty list.")

        records: dict[str, EbookRecord] = {}
        for index, raw_item in enumerate(items):
            record = self._validate_item(raw_item, index)
            if record.id in records:
                raise EbookCatalogError(f'Duplicate ebook id "{record.id}".')
            records[record.id] = record
        return records

    def _validate_item(self, raw_item: Any, index: int) -> EbookRecord:
        if not isinstance(raw_item, dict):
            raise EbookCatalogError(f"Ebook item {index} must be an object.")

        book_id = self._required_text(raw_item, "id", index)
        if not _EBOOK_ID_PATTERN.fullmatch(book_id):
            raise EbookCatalogError(f'Invalid ebook id "{book_id}".')

        source_file = self._required_text(raw_item, "source_file", index)
        source_path = Path(source_file)
        if source_path.is_absolute() or source_path.name != source_file:
            raise EbookCatalogError(
                f'Ebook "{book_id}" source_file must be a file name inside Ebook/.'
            )
        file_path = (self.ebook_root / source_file).resolve()
        if file_path.parent != self.ebook_root or not file_path.is_file():
            raise EbookCatalogError(f'Ebook "{book_id}" source file is missing.')

        cover = self._required_text(raw_item, "cover", index)
        if not cover.startswith(_COVER_PREFIX):
            raise EbookCatalogError(
                f'Ebook "{book_id}" cover must use {_COVER_PREFIX}.'
            )
        cover_relative = Path(cover.removeprefix("/"))
        cover_path = (self.project_root / "data" / "frontend" / cover_relative).resolve()
        allowed_cover_root = (
            self.project_root
            / "data"
            / "frontend"
            / "assets"
            / "resource"
            / "ebooks"
            / "covers"
        ).resolve()
        if cover_path.parent != allowed_cover_root or not cover_path.is_file():
            raise EbookCatalogError(f'Ebook "{book_id}" cover is missing.')
        self._validate_cover(cover_path, book_id)

        authors = self._required_text_list(raw_item, "authors", index)
        tags = self._required_text_list(raw_item, "tags", index)
        self._validate_epub(file_path, book_id)

        return EbookRecord(
            id=book_id,
            source_file=source_file,
            title=self._required_text(raw_item, "title", index),
            authors=authors,
            publisher=self._optional_text(raw_item.get("publisher"), "publisher", book_id),
            published_at=self._optional_text(
                raw_item.get("published_at"), "published_at", book_id
            ),
            summary=self._required_text(raw_item, "summary", index),
            tags=tags,
            cover=cover,
            file_path=file_path,
            cover_path=cover_path,
        )

    @staticmethod
    def _required_text(raw_item: dict[str, Any], field: str, index: int) -> str:
        value = raw_item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EbookCatalogError(f"Ebook item {index} field {field} must not be empty.")
        return value.strip()

    @staticmethod
    def _optional_text(value: Any, field: str, book_id: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise EbookCatalogError(f'Ebook "{book_id}" field {field} is invalid.')
        return value.strip()

    @staticmethod
    def _required_text_list(
        raw_item: dict[str, Any], field: str, index: int
    ) -> tuple[str, ...]:
        values = raw_item.get(field)
        if not isinstance(values, list) or not values:
            raise EbookCatalogError(f"Ebook item {index} field {field} must not be empty.")
        normalized = tuple(
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        )
        if len(normalized) != len(values):
            raise EbookCatalogError(f"Ebook item {index} field {field} is invalid.")
        return normalized

    @staticmethod
    def _validate_cover(path: Path, book_id: str) -> None:
        try:
            header = path.read_bytes()[:12]
        except OSError as exc:
            raise EbookCatalogError(f'Ebook "{book_id}" cover is unreadable.') from exc
        is_jpeg = header.startswith(b"\xff\xd8\xff")
        is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
        is_webp = header.startswith(b"RIFF") and header[8:12] == b"WEBP"
        if not (is_jpeg or is_png or is_webp):
            raise EbookCatalogError(f'Ebook "{book_id}" cover format is invalid.')

    @staticmethod
    def _validate_epub(path: Path, book_id: str) -> None:
        try:
            with ZipFile(path) as archive:
                invalid_member = archive.testzip()
                if invalid_member is not None:
                    raise EbookCatalogError(
                        f'Ebook "{book_id}" contains a damaged ZIP member.'
                    )
                container = ElementTree.fromstring(
                    archive.read("META-INF/container.xml")
                )
                rootfile = container.find(".//{*}rootfile")
                opf_name = rootfile.get("full-path") if rootfile is not None else None
                if not opf_name:
                    raise EbookCatalogError(f'Ebook "{book_id}" has no OPF package.')
                opf_path = PurePosixPath(opf_name)
                if opf_path.is_absolute() or ".." in opf_path.parts:
                    raise EbookCatalogError(f'Ebook "{book_id}" has an unsafe OPF path.')
                package = ElementTree.fromstring(archive.read(opf_name))
                title = package.find(".//{*}metadata/{*}title")
                if title is None or not "".join(title.itertext()).strip():
                    raise EbookCatalogError(f'Ebook "{book_id}" has no OPF title.')
                manifest: dict[str, tuple[str, str, frozenset[str]]] = {}
                for item in package.findall(".//{*}manifest/{*}item"):
                    item_id = (item.get("id") or "").strip()
                    href = (item.get("href") or "").strip()
                    if not item_id or not href:
                        continue
                    member_name = EbookCatalogService._resolve_epub_member(
                        opf_path,
                        href,
                        book_id,
                    )
                    manifest[item_id] = (
                        member_name,
                        (item.get("media-type") or "").strip(),
                        frozenset((item.get("properties") or "").split()),
                    )

                spine = package.find(".//{*}spine")
                spine_refs = (
                    [
                        (item_ref.get("idref") or "").strip()
                        for item_ref in spine.findall("./{*}itemref")
                    ]
                    if spine is not None
                    else []
                )
                spine_refs = [item_id for item_id in spine_refs if item_id]
                if not spine_refs:
                    raise EbookCatalogError(f'Ebook "{book_id}" has no readable spine.')
                first_spine_item = manifest.get(spine_refs[0])
                if first_spine_item is None:
                    raise EbookCatalogError(
                        f'Ebook "{book_id}" first spine item is missing.'
                    )
                first_chapter = ElementTree.fromstring(
                    archive.read(first_spine_item[0])
                )
                if not any(
                    str(element.tag).rsplit("}", 1)[-1].casefold() == "body"
                    for element in first_chapter.iter()
                ):
                    raise EbookCatalogError(
                        f'Ebook "{book_id}" first spine item has no body.'
                    )

                nav_member = next(
                    (
                        member_name
                        for member_name, _media_type, properties in manifest.values()
                        if "nav" in properties
                    ),
                    None,
                )
                if nav_member is not None:
                    navigation = ElementTree.fromstring(archive.read(nav_member))
                    has_entries = any(
                        str(element.tag).rsplit("}", 1)[-1].casefold() == "a"
                        and bool((element.get("href") or "").strip())
                        for element in navigation.iter()
                    )
                else:
                    ncx_id = (spine.get("toc") or "").strip() if spine is not None else ""
                    ncx_item = manifest.get(ncx_id) if ncx_id else next(
                        (
                            item
                            for item in manifest.values()
                            if item[1] == "application/x-dtbncx+xml"
                        ),
                        None,
                    )
                    if ncx_item is None:
                        raise EbookCatalogError(
                            f'Ebook "{book_id}" has no navigation document.'
                        )
                    navigation = ElementTree.fromstring(archive.read(ncx_item[0]))
                    has_entries = any(
                        str(element.tag).rsplit("}", 1)[-1].casefold() == "navpoint"
                        for element in navigation.iter()
                    )
                if not has_entries:
                    raise EbookCatalogError(
                        f'Ebook "{book_id}" navigation document is empty.'
                    )
        except EbookCatalogError:
            raise
        except (BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
            raise EbookCatalogError(f'Ebook "{book_id}" is not a valid EPUB.') from exc

    @staticmethod
    def _resolve_epub_member(
        opf_path: PurePosixPath,
        href: str,
        book_id: str,
    ) -> str:
        decoded_path = unquote(urlsplit(href).path)
        if not decoded_path:
            raise EbookCatalogError(f'Ebook "{book_id}" has an empty resource path.')
        member_name = posixpath.normpath(
            posixpath.join(opf_path.parent.as_posix(), decoded_path)
        )
        if (
            member_name.startswith("/")
            or member_name == ".."
            or member_name.startswith("../")
        ):
            raise EbookCatalogError(
                f'Ebook "{book_id}" contains an unsafe resource path.'
            )
        return member_name
