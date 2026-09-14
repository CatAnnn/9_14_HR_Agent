from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import mimetypes
import random
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree, html
from lxml.html import HtmlElement
from playwright.async_api import (
    APIResponse,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

DEFAULT_START_URL = (
    "https://bgn.bosch.com/FIRSTspiritWeb/wcms/wcms_c/en/"
    "bosch-globalnet/organization/organization-startpage.html"
)

ALLOWED_HOSTS = {
    "bgn.bosch.com",
    "inside-ws.bosch.com",
}

IGNORED_QUERY_KEYS = {
    "sso",
    "shownavigation",
    "locale",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "wt_mc",
}

BLOCKED_EXTENSIONS = {
    ".7z", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".exe",
    ".gif", ".ico", ".jpeg", ".jpg", ".js", ".json", ".mov", ".mp3",
    ".mp4", ".mpeg", ".pdf", ".png", ".ppt", ".pptx", ".rar", ".svg",
    ".tar", ".tif", ".tiff", ".wav", ".webm", ".webp", ".xls", ".xlsx",
    ".xml", ".zip",
}

DEFAULT_DOWNLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".txt", ".rtf", ".odt", ".ods", ".odp", ".zip",
}

IMAGE_DOWNLOAD_EXTENSIONS = {
    ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff",
    ".webp",
}

MIME_TO_EXTENSION = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/rtf": ".rtf",
    "text/rtf": ".rtf",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}

FILE_LINK_HINT_RE = re.compile(
    r"\b(?:download|attachment|document|brochure|one[ -]?pager|presentation|"
    r"spreadsheet|pdf|word|excel|powerpoint|zip)\b",
    re.IGNORECASE,
)

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._()\[\] -]+")

BLOCKED_PATH_PARTS = {
    "/media/",
    "/system/",
    "/javascript/",
    "/stylesheets/",
    "/redirect/",
    "/search/",
    "/bgnuserdataservice/",
    "/webaccess/",
}

GENERIC_LABELS = {
    "about",
    "home",
    "homepage",
    "overview",
    "start page",
    "startpage",
}

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
SPACE_RE = re.compile(r"\s+")
HIDDEN_CLASS_RE = re.compile(
    r"\.([A-Za-z0-9_-]+)\s*\{[^{}]*display\s*:\s*none",
    re.IGNORECASE,
)
CLASS_XPATH = (
    "contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')"
)

OUTPUT_FILES = {
    # User-facing results.
    "page": "pages.jsonl",
    "relation": "relations.jsonl",
    "file": "files.jsonl",
    "error": "errors.jsonl",
    # Internal resume state. These are intentionally hidden from normal output.
    "frontier": ".state/page_frontier.jsonl",
    "file_frontier": ".state/file_frontier.jsonl",
}


class RetryableFetchError(RuntimeError):
    """A transient request error that may succeed after retrying."""


class PermanentFetchError(RuntimeError):
    """A non-transient request error that should not be retried."""


class SkippableFileError(RuntimeError):
    """A non-actionable file candidate that should be ignored without error output."""


@dataclass(frozen=True, slots=True)
class CrawlTask:
    url: str
    parent_url: str | None
    path: tuple[str, ...]
    depth: int
    source: str
    link_text: str
    root_department: str | None


@dataclass(frozen=True, slots=True)
class LinkRecord:
    url: str
    text: str
    source: str
    relation_type: str
    crawlable: bool


@dataclass(frozen=True, slots=True)
class FileLinkRecord:
    url: str
    text: str
    source: str
    expected_extension: str


@dataclass(frozen=True, slots=True)
class FileTask:
    url: str
    source_page_url: str
    source_page_title: str
    department_path: tuple[str, ...]
    root_department: str | None
    link_text: str
    expected_extension: str
    source: str


@dataclass(slots=True)
class CrawlCounters:
    enqueued: int = 0
    completed: int = 0
    failed: int = 0
    duplicates: int = 0
    edges: int = 0
    max_depth_seen: int = 0
    rendered_fallbacks: int = 0
    retried_requests: int = 0
    files_enqueued: int = 0
    files_downloaded: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    file_duplicates: int = 0
    file_bytes_downloaded: int = 0


class AsyncRateLimiter:
    """Simple global request-rate limiter shared by all workers."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = (
            0.0 if requests_per_second <= 0 else 1.0 / requests_per_second
        )
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self.interval


class StreamingJSONLWriter:
    """Write crawl records asynchronously in batches."""

    def __init__(
        self,
        output_dir: Path,
        *,
        append: bool,
        batch_size: int,
    ) -> None:
        self.output_dir = output_dir
        self.append = append
        self.batch_size = max(1, batch_size)
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = (
            asyncio.Queue(maxsize=self.batch_size * 20)
        )
        self._task: asyncio.Task[None] | None = None
        self._files: dict[str, TextIO] = {}

    async def start(self) -> None:
        mode = "a" if self.append else "w"
        for kind, filename in OUTPUT_FILES.items():
            path = self.output_dir / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            self._files[kind] = path.open(
                mode,
                encoding="utf-8",
                buffering=1024 * 1024,
            )
        self._task = asyncio.create_task(self._run())

    async def write(self, kind: str, record: dict[str, Any]) -> None:
        if kind not in self._files:
            raise ValueError(f"Unknown output kind: {kind}")
        await self.queue.put((kind, record))

    async def close(self) -> None:
        await self.queue.put(None)
        if self._task is not None:
            await self._task
        for handle in self._files.values():
            handle.flush()
            handle.close()

    async def _run(self) -> None:
        buffers: dict[str, list[str]] = {kind: [] for kind in self._files}

        async def flush_kind(kind: str) -> None:
            buffer = buffers[kind]
            if not buffer:
                return
            self._files[kind].write("".join(buffer))
            buffer.clear()

        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    for kind in buffers:
                        await flush_kind(kind)
                    return

                kind, record = item
                buffers[kind].append(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
                if len(buffers[kind]) >= self.batch_size:
                    await flush_kind(kind)
            finally:
                self.queue.task_done()


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return SPACE_RE.sub(" ", ZERO_WIDTH_RE.sub("", value)).strip()


def clean_multiline_text(parts: Iterable[str]) -> str:
    lines: list[str] = []
    previous = ""
    for raw_part in parts:
        for raw_line in raw_part.splitlines():
            line = clean_text(raw_line)
            if not line or line == previous:
                continue
            lines.append(line)
            previous = line
    return "\n".join(lines)


def canonicalize_url(base_url: str, href: str) -> str | None:
    href = clean_text(href)
    if not href or href.lower().startswith(
        ("#", "javascript:", "mailto:", "tel:", "data:")
    ):
        return None

    absolute = urljoin(base_url, href)
    parsed = urlsplit(absolute)

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None

    # The supplied page redirects inside-ws links to bgn.
    if hostname == "inside-ws.bosch.com":
        hostname = "bgn.bosch.com"

    netloc = hostname
    if parsed.port:
        netloc = f"{hostname}:{parsed.port}"

    query_items = sorted(
        (
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in IGNORED_QUERY_KEYS
        ),
        key=lambda pair: (pair[0], pair[1]),
    )

    path = ZERO_WIDTH_RE.sub("", parsed.path).strip()
    path = re.sub(r"/{2,}", "/", path)
    if path != "/":
        path = path.rstrip("/")

    return urlunsplit(
        (
            "https",
            netloc,
            path or "/",
            urlencode(query_items, doseq=True),
            "",
        )
    )


def is_internal_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname in ALLOWED_HOSTS or hostname == "bgn.bosch.com"


def is_crawlable_url(url: str) -> bool:
    if not is_internal_url(url):
        return False

    parsed = urlsplit(url)
    path = parsed.path.lower()

    if any(part in path for part in BLOCKED_PATH_PARTS):
        return False

    if Path(path).suffix.lower() in BLOCKED_EXTENSIONS:
        return False

    return "/firstspiritweb/" in path or path.startswith("/alias/")


def class_xpath(class_name: str) -> str:
    return CLASS_XPATH.format(class_name=class_name)


def hidden_css_classes(document: HtmlElement) -> set[str]:
    hidden: set[str] = set()
    for style_text in document.xpath("//style/text()"):
        hidden.update(HIDDEN_CLASS_RE.findall(style_text))
    return hidden


def node_is_hidden(node: HtmlElement, hidden_classes: set[str]) -> bool:
    current: HtmlElement | None = node
    while current is not None:
        if "hidden" in current.attrib:
            return True
        if current.attrib.get("aria-hidden", "").lower() == "true":
            return True

        style = current.attrib.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True

        classes = set(current.attrib.get("class", "").split())
        if classes.intersection(hidden_classes):
            return True

        parent = current.getparent()
        current = parent if isinstance(parent, HtmlElement) else None

    return False


def element_text(node: HtmlElement | None) -> str:
    if node is None:
        return ""
    return clean_text(" ".join(node.itertext()))


def unique_links(records: Iterable[LinkRecord]) -> list[LinkRecord]:
    """Deduplicate left-navigation targets while preserving the strongest relation."""
    priorities = {
        "root_department": 0,
        "navigation_child": 1,
        "navigation_section": 2,
        "navigation_fallback": 3,
    }
    best: dict[str, LinkRecord] = {}
    for record in records:
        existing = best.get(record.url)
        if existing is None:
            best[record.url] = record
            continue

        existing_priority = priorities.get(existing.relation_type, 99)
        current_priority = priorities.get(record.relation_type, 99)
        if current_priority < existing_priority:
            best[record.url] = record
            continue

        if (
            current_priority == existing_priority
            and existing.text.strip().lower() in GENERIC_LABELS
            and record.text.strip().lower() not in GENERIC_LABELS
        ):
            best[record.url] = record

    return list(best.values())


def _first_direct_anchor(li: HtmlElement) -> HtmlElement | None:
    anchors = li.xpath("./a[@href] | ./*[not(self::ul)]/a[@href]")
    return first_node(anchors)


def _direct_list_links(
    list_node: HtmlElement,
    page_url: str,
    relation_type: str,
) -> list[LinkRecord]:
    records: list[LinkRecord] = []
    for li in list_node.xpath("./li"):
        if not isinstance(li, HtmlElement):
            continue
        anchor = _first_direct_anchor(li)
        if anchor is None:
            continue
        href = anchor.attrib.get("href", "")
        url = canonicalize_url(page_url, href)
        if not url:
            continue
        label = element_text(anchor) or Path(urlsplit(url).path).stem
        records.append(
            LinkRecord(
                url=url,
                text=label,
                source="left_navigation",
                relation_type=relation_type,
                crawlable=is_crawlable_url(url),
            )
        )
    return records


def extract_links(
    document: HtmlElement,
    page_url: str,
    *,
    is_start_page: bool,
) -> list[LinkRecord]:
    """
    Discover pages only from the left navigation and derive a stable hierarchy.

    Rules:
    1. The seed page uses the direct top-level entries as root departments.
    2. If the current URL is present and its ``li`` has a nested ``ul``, only
       direct entries in that nested list are treated as children.
    3. Otherwise, direct top-level entries are treated as section pages.  This
       supports landing pages whose sidebar exposes sibling section entries
       without explicit nested markup.
    4. All anchors are used only as a final compatibility fallback.

    Collapsed entries are retained because they are already present in HTML.
    """
    subnav = first_node(
        document.xpath(
            "//*[@id='sidebar']//*[@id='subNav'][1] | //*[@id='subNav'][1]"
        )
    )
    if subnav is None:
        return []

    top_list = first_node(subnav.xpath("./ul[1]"))
    if is_start_page and top_list is not None:
        return unique_links(
            _direct_list_links(top_list, page_url, "root_department")
        )

    canonical_page = canonicalize_url(page_url, page_url) or page_url
    current_anchor: HtmlElement | None = None
    for anchor in subnav.xpath(".//a[@href]"):
        if not isinstance(anchor, HtmlElement):
            continue
        href = anchor.attrib.get("href", "")
        candidate = canonicalize_url(page_url, href)
        if candidate == canonical_page:
            current_anchor = anchor
            break

    if current_anchor is not None:
        current_li = first_node(current_anchor.xpath("ancestor::li[1]"))
        if current_li is not None:
            child_list = first_node(current_li.xpath("./ul[1]"))
            if child_list is not None:
                children = _direct_list_links(
                    child_list,
                    page_url,
                    "navigation_child",
                )
                children = [item for item in children if item.url != canonical_page]
                if children:
                    return unique_links(children)

    if top_list is not None:
        section_links = _direct_list_links(
            top_list,
            page_url,
            "navigation_section",
        )
        section_links = [
            item for item in section_links if item.url != canonical_page
        ]
        if section_links:
            return unique_links(section_links)

    fallback: list[LinkRecord] = []
    for anchor in subnav.xpath(".//a[@href]"):
        if not isinstance(anchor, HtmlElement):
            continue
        url = canonicalize_url(page_url, anchor.attrib.get("href", ""))
        if not url or url == canonical_page:
            continue
        fallback.append(
            LinkRecord(
                url=url,
                text=element_text(anchor) or Path(urlsplit(url).path).stem,
                source="left_navigation",
                relation_type="navigation_fallback",
                crawlable=is_crawlable_url(url),
            )
        )
    return unique_links(fallback)


def normalize_extension(value: str) -> str:
    value = clean_text(value).lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def parse_file_extensions(value: str, include_images: bool) -> frozenset[str]:
    extensions = {
        normalize_extension(item)
        for item in value.split(",")
        if normalize_extension(item)
    }
    if not extensions:
        extensions = set(DEFAULT_DOWNLOAD_EXTENSIONS)
    if include_images:
        extensions.update(IMAGE_DOWNLOAD_EXTENSIONS)
    return frozenset(extensions)


def probable_file_extension(url: str) -> str:
    return Path(urlsplit(url).path).suffix.lower()


def unique_file_links(records: Iterable[FileLinkRecord]) -> list[FileLinkRecord]:
    best: dict[str, FileLinkRecord] = {}
    for record in records:
        existing = best.get(record.url)
        if existing is None or (not existing.text and record.text):
            best[record.url] = record
    return list(best.values())


def extract_file_links(
    document: HtmlElement,
    page_url: str,
    allowed_extensions: frozenset[str],
    *,
    discover_hinted_links: bool,
) -> list[FileLinkRecord]:
    """Find downloadable files without using content links as page crawl targets."""
    records: list[FileLinkRecord] = []
    anchors = document.xpath(
        "//*[@id='pageContent']//a[@href]"
        " | //*[@id='sidebar']//*[@id='subNav']//a[@href]"
        " | //*[@id='subNav']//a[@href]"
    )

    for anchor in anchors:
        if not isinstance(anchor, HtmlElement):
            continue
        href = anchor.attrib.get("href", "")
        url = canonicalize_url(page_url, href)
        if not url or not is_internal_url(url):
            continue

        text_value = element_text(anchor)
        extension = probable_file_extension(url)
        has_download_attribute = "download" in anchor.attrib
        hinted = bool(FILE_LINK_HINT_RE.search(f"{href} {text_value}"))

        is_left_navigation = bool(anchor.xpath("ancestor::*[@id='subNav']"))
        if extension not in allowed_extensions:
            # Extension-less links are probed only when they are explicit downloads
            # or when a page-content label strongly suggests a file.  We do not use
            # keyword hints for sidebar links because labels such as
            # "Employee Representation" can otherwise create false positives.
            hinted_page_file = (
                discover_hinted_links and hinted and not is_left_navigation
            )
            if not has_download_attribute and not hinted_page_file:
                continue
            extension = ""

        source = (
            "left_navigation_file"
            if is_left_navigation
            else "page_content_file"
        )
        records.append(
            FileLinkRecord(
                url=url,
                text=text_value or Path(urlsplit(url).path).name,
                source=source,
                expected_extension=extension,
            )
        )

    return unique_file_links(records)


def safe_filename(value: str, fallback: str) -> str:
    value = unquote(clean_text(value)).replace("/", "_").replace("\\", "_")
    value = SAFE_FILENAME_RE.sub("_", value).strip(" ._")
    if not value:
        value = fallback
    stem = Path(value).stem[:120].strip(" ._") or "file"
    suffix = Path(value).suffix[:16].lower()
    return f"{stem}{suffix}"


def filename_from_content_disposition(value: str) -> str:
    if not value:
        return ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", value, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip())
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def extension_for_response(
    content_type: str,
    response_url: str,
    expected_extension: str,
) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return (
        MIME_TO_EXTENSION.get(content_type)
        or probable_file_extension(response_url)
        or expected_extension
        or mimetypes.guess_extension(content_type, strict=False)
        or ""
    ).lower()


def is_allowed_file_response(
    content_type: str,
    extension: str,
    allowed_extensions: frozenset[str],
) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    if "text/html" in media_type or "application/xhtml" in media_type:
        return False
    if extension in allowed_extensions:
        return True
    if media_type == "application/octet-stream" and extension:
        return extension in allowed_extensions
    return MIME_TO_EXTENSION.get(media_type, "") in allowed_extensions


def file_task_from_record(record: dict[str, Any]) -> FileTask | None:
    try:
        return FileTask(
            url=str(record["url"]),
            source_page_url=str(record["source_page_url"]),
            source_page_title=str(record.get("source_page_title", "")),
            department_path=tuple(str(item) for item in record.get("department_path", [])),
            root_department=(
                str(record["root_department"])
                if record.get("root_department") is not None
                else None
            ),
            link_text=str(record.get("link_text", "")),
            expected_extension=str(record.get("expected_extension", "")),
            source=str(record.get("source", "resume_file")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def remove_node_preserving_tail(node: HtmlElement) -> None:
    """Remove an element without discarding visible text stored in its tail."""
    parent = node.getparent()
    if parent is None:
        return

    tail = node.tail or ""
    previous = node.getprevious()
    if tail:
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(node)


def remove_unwanted_nodes(root: HtmlElement, hidden_classes: set[str]) -> None:
    removable_tags = {"script", "style", "noscript", "svg", "img", "iframe"}
    removable_classes = {"somOptions", "menu_toggler"}

    for node in list(root.iterdescendants()):
        if not isinstance(node.tag, str):
            continue

        classes = set(node.attrib.get("class", "").split())
        should_remove = (
            node.tag.lower() in removable_tags
            or bool(classes.intersection(removable_classes))
            or node_is_hidden(node, hidden_classes)
        )
        if should_remove:
            remove_node_preserving_tail(node)


def first_node(nodes: list[Any]) -> HtmlElement | None:
    for node in nodes:
        if isinstance(node, HtmlElement):
            return node
    return None


def meta_content(document: HtmlElement, name: str) -> str:
    values = document.xpath(f"//meta[@name={json.dumps(name)}]/@content")
    return clean_text(values[0]) if values else ""


def parse_document(html_text: str) -> HtmlElement:
    parser = html.HTMLParser(encoding="utf-8", recover=True, remove_comments=True)
    document = html.fromstring(html_text.encode("utf-8", errors="replace"), parser=parser)
    if not isinstance(document, HtmlElement):
        raise ValueError("Unable to parse HTML document.")
    return document


def extract_page(
    html_text: str,
    task: CrawlTask,
    final_url: str,
    allowed_file_extensions: frozenset[str],
    discover_hinted_files: bool,
) -> tuple[dict[str, Any], list[LinkRecord], list[FileLinkRecord]]:
    document = parse_document(html_text)
    hidden_classes = hidden_css_classes(document)

    title_node = first_node(
        document.xpath(
            f"//*[{class_xpath('headerTitle')} and {class_xpath('bottomAbsolute')}]"
        )
    )
    if title_node is None:
        title_node = first_node(document.xpath("//title"))
    title = element_text(title_node)

    content_node = first_node(document.xpath("//*[@id='pageContent']"))
    if content_node is None:
        content_node = first_node(document.xpath("//*[@id='pageContainer']"))
    if content_node is None:
        raise ValueError(
            "Page content container was not found; authentication may have expired."
        )

    links = extract_links(document, final_url, is_start_page=task.depth == 0)
    file_links = extract_file_links(
        document,
        final_url,
        allowed_file_extensions,
        discover_hinted_links=discover_hinted_files,
    )
    # Deep-copy only the content subtree, not the very large global navigation.
    detached_root = etree.fromstring(etree.tostring(content_node, encoding="utf-8"))
    if not isinstance(detached_root, HtmlElement):
        detached_root = html.fromstring(etree.tostring(content_node, encoding="unicode"))
    remove_unwanted_nodes(detached_root, hidden_classes)

    content = clean_multiline_text(detached_root.itertext())
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    path = list(task.path)
    if not path and task.depth == 0:
        path = [title or "Organization"]
    elif path and path[-1].strip().lower() in GENERIC_LABELS and title:
        path[-1] = title

    record = {
        "title": title,
        "department_path": path,
        "short_id": meta_content(document, "DCSext.wtg_shortId"),
        "page_owner_department": meta_content(
            document, "DCSext.wtg_pageOwnerDepartment"
        ),
        "last_changed": meta_content(document, "DCSext.wtg_lastChangedDate"),
        "language": meta_content(document, "DCSext.wtg_lang"),
        "content": content,
        "content_hash": content_hash,
    }
    return record, links, file_links


async def launch_context(
    playwright: Any,
    profile_dir: Path,
    headless: bool,
    browser_channel: str,
) -> BrowserContext:
    kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "ignore_https_errors": True,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "en-US",
    }

    if browser_channel:
        try:
            return await playwright.chromium.launch_persistent_context(
                channel=browser_channel,
                **kwargs,
            )
        except PlaywrightError as exc:
            print(
                f"Unable to launch browser channel {browser_channel!r}: {exc}\n"
                "Falling back to Playwright Chromium.",
                file=sys.stderr,
            )

    return await playwright.chromium.launch_persistent_context(**kwargs)


async def ensure_authenticated(
    context: BrowserContext,
    start_url: str,
    headless: bool,
    timeout_seconds: int,
) -> None:
    page = await context.new_page()
    try:
        await page.goto(
            start_url,
            wait_until="domcontentloaded",
            timeout=timeout_seconds * 1000,
        )
        if await page.locator("#pageContent").count() > 0:
            return

        if headless:
            raise RuntimeError(
                "The authenticated page was not reached. Run once without "
                "--headless and complete Bosch SSO login."
            )

        print(
            "\nComplete Bosch SSO authentication in the opened browser window.\n"
            "After the Organization page is visible, return here and press Enter."
        )
        await asyncio.to_thread(input)

        await page.goto(
            start_url,
            wait_until="domcontentloaded",
            timeout=timeout_seconds * 1000,
        )
        if await page.locator("#pageContent").count() == 0:
            raise RuntimeError(
                "Authentication was not confirmed: #pageContent is still absent."
            )
    finally:
        await page.close()


async def response_text(response: APIResponse) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        raise ValueError(f"Unexpected content type: {content_type or 'unknown'}")
    return await response.text()


def html_has_page_content(html_text: str) -> bool:
    sample = html_text.lower()
    return 'id="pagecontent"' in sample or "id='pagecontent'" in sample


async def render_page_html(
    context: BrowserContext,
    url: str,
    *,
    timeout_seconds: int,
    render_semaphore: asyncio.Semaphore,
) -> tuple[str, str]:
    async with render_semaphore:
        page: Page = await context.new_page()
        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout_seconds * 1000,
            )
            try:
                await page.wait_for_selector(
                    "#pageContent",
                    timeout=min(timeout_seconds, 30) * 1000,
                )
            except PlaywrightTimeoutError:
                pass
            return await page.content(), page.url
        finally:
            await page.close()


async def fetch_html(
    context: BrowserContext,
    task: CrawlTask,
    args: argparse.Namespace,
    rate_limiter: AsyncRateLimiter,
    render_semaphore: asyncio.Semaphore,
    counters: CrawlCounters,
) -> tuple[str, str, int, bool]:
    last_error: Exception | None = None

    for attempt in range(args.max_retries + 1):
        try:
            await rate_limiter.wait()
            response = await context.request.get(
                task.url,
                timeout=args.timeout_seconds * 1000,
                fail_on_status_code=False,
            )
            status = response.status
            final_url = canonicalize_url(task.url, response.url) or task.url

            if status in RETRYABLE_STATUS_CODES:
                raise RetryableFetchError(f"Retryable HTTP {status}")
            if status != 200:
                raise PermanentFetchError(f"HTTP {status}")

            html_text = await response_text(response)
            if html_has_page_content(html_text):
                return html_text, final_url, status, False

            if not args.render_fallback:
                raise PermanentFetchError(
                    "Static response does not contain #pageContent"
                )

            rendered_html, rendered_url = await render_page_html(
                context,
                final_url,
                timeout_seconds=args.timeout_seconds,
                render_semaphore=render_semaphore,
            )
            rendered_final_url = canonicalize_url(final_url, rendered_url) or final_url
            counters.rendered_fallbacks += 1
            return rendered_html, rendered_final_url, status, True

        except PermanentFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_error = exc
            if attempt >= args.max_retries:
                break
            counters.retried_requests += 1
            base = args.retry_backoff_seconds * (2**attempt)
            jitter = random.uniform(0, max(0.05, base * 0.25))
            await asyncio.sleep(base + jitter)

    assert last_error is not None
    raise last_error


async def fetch_file_bytes(
    context: BrowserContext,
    task: FileTask,
    args: argparse.Namespace,
    rate_limiter: AsyncRateLimiter,
    counters: CrawlCounters,
) -> tuple[bytes, str, str, str, int]:
    last_error: Exception | None = None
    max_bytes = int(args.max_file_size_mb * 1024 * 1024)

    for attempt in range(args.max_retries + 1):
        try:
            await rate_limiter.wait()
            response = await context.request.get(
                task.url,
                timeout=args.file_timeout_seconds * 1000,
                fail_on_status_code=False,
            )
            status = response.status
            final_url = canonicalize_url(task.url, response.url) or task.url
            if status in RETRYABLE_STATUS_CODES:
                raise RetryableFetchError(f"Retryable HTTP {status}")
            if status != 200:
                raise PermanentFetchError(f"HTTP {status}")

            headers = {key.lower(): value for key, value in response.headers.items()}
            content_length = headers.get("content-length", "").strip()
            if content_length.isdigit() and int(content_length) > max_bytes:
                raise SkippableFileError(
                    f"File exceeds --max-file-size-mb ({content_length} bytes)"
                )

            content_type = headers.get("content-type", "application/octet-stream")
            extension = extension_for_response(
                content_type,
                final_url,
                task.expected_extension,
            )
            if not is_allowed_file_response(
                content_type,
                extension,
                args.file_extensions_set,
            ):
                raise SkippableFileError(
                    f"Response is not an allowed downloadable file: {content_type}"
                )

            body = await response.body()
            if len(body) > max_bytes:
                raise SkippableFileError(
                    f"File exceeds --max-file-size-mb ({len(body)} bytes)"
                )
            if not body:
                raise PermanentFetchError("Downloaded file is empty")

            disposition_name = filename_from_content_disposition(
                headers.get("content-disposition", "")
            )
            url_name = Path(urlsplit(final_url).path).name
            filename = disposition_name or url_name or f"download{extension}"
            filename = safe_filename(filename, f"download{extension}")
            if extension and Path(filename).suffix.lower() != extension:
                filename = f"{Path(filename).stem}{extension}"

            return body, final_url, filename, content_type, status

        except (PermanentFetchError, SkippableFileError):
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= args.max_retries:
                break
            counters.retried_requests += 1
            base = args.retry_backoff_seconds * (2**attempt)
            await asyncio.sleep(base + random.uniform(0, max(0.05, base * 0.25)))

    assert last_error is not None
    raise last_error


def file_storage_path(
    files_dir: Path,
    digest: str,
    filename: str,
    root_department: str | None,
) -> Path:
    root = safe_filename(root_department or "unassigned", "unassigned")
    destination_dir = files_dir / root / digest[:2]
    destination_dir.mkdir(parents=True, exist_ok=True)
    return destination_dir / f"{digest[:16]}_{filename}"


def load_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []

    def iterator() -> Iterable[dict[str, Any]]:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"Skipping malformed JSONL line {path}:{line_number}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if isinstance(value, dict):
                    yield value

    return iterator()


def task_from_record(record: dict[str, Any]) -> CrawlTask | None:
    try:
        return CrawlTask(
            url=str(record["url"]),
            parent_url=(
                str(record["parent_url"])
                if record.get("parent_url") is not None
                else None
            ),
            path=tuple(str(item) for item in record.get("path", [])),
            depth=int(record.get("depth", 0)),
            source=str(record.get("source", "resume")),
            link_text=str(record.get("link_text", "")),
            root_department=(
                str(record["root_department"])
                if record.get("root_department") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def depth_allows_children(depth: int, max_depth: int) -> bool:
    # max_depth <= 0 means unlimited depth.
    return max_depth <= 0 or depth < max_depth


def page_limit_reached(enqueued: int, max_pages: int) -> bool:
    # max_pages <= 0 means unlimited pages.
    return max_pages > 0 and enqueued >= max_pages


async def crawl(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    if not args.resume:
        for filename in (*OUTPUT_FILES.values(), "summary.json"):
            path = output_dir / filename
            if path.exists():
                path.unlink()
        shutil.rmtree(output_dir / "files", ignore_errors=True)
        shutil.rmtree(output_dir / ".state", ignore_errors=True)

    start_url = canonicalize_url(args.start_url, args.start_url)
    if not start_url:
        raise ValueError("Invalid start URL.")

    counters = CrawlCounters()
    previous_page_count = 0
    previous_relation_count = 0
    previous_file_count = 0
    previous_error_count = 0
    previous_max_depth = 0
    seen_urls: set[str] = set()
    completed_urls: set[str] = set()
    failed_urls: set[str] = set()
    content_hash_to_url: dict[str, str] = {}
    queue: asyncio.Queue[CrawlTask | None] = asyncio.Queue()
    file_queue: asyncio.Queue[FileTask | None] = asyncio.Queue(
        maxsize=max(100, args.file_concurrency * 50)
    )
    seen_file_urls: set[str] = set()
    completed_file_urls: set[str] = set()
    file_hash_to_path: dict[str, str] = {}
    files_dir = output_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    state_lock = asyncio.Lock()

    if args.resume:
        for record in load_jsonl(output_dir / OUTPUT_FILES["page"]):
            url = canonicalize_url(start_url, str(record.get("url", "")))
            if url:
                completed_urls.add(url)
                seen_urls.add(url)
            content = str(record.get("content", ""))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if content and url:
                content_hash_to_url.setdefault(content_hash, url)
            previous_page_count += 1
            previous_max_depth = max(
                previous_max_depth, int(record.get("depth", 0))
            )

        for record in load_jsonl(output_dir / OUTPUT_FILES["error"]):
            previous_error_count += 1
            if record.get("type") != "page":
                continue
            url = canonicalize_url(start_url, str(record.get("url", "")))
            if url:
                failed_urls.add(url)

        for _record in load_jsonl(output_dir / OUTPUT_FILES["relation"]):
            previous_relation_count += 1

        for record in load_jsonl(output_dir / OUTPUT_FILES["file"]):
            previous_file_count += 1
            url = canonicalize_url(start_url, str(record.get("url", "")))
            if url:
                completed_file_urls.add(url)
                seen_file_urls.add(url)
            digest = str(record.get("sha256", ""))
            stored_path = str(record.get("stored_path", ""))
            if digest and stored_path:
                file_hash_to_path.setdefault(digest, stored_path)

    writer = StreamingJSONLWriter(
        output_dir,
        append=args.resume,
        batch_size=args.write_batch_size,
    )
    await writer.start()

    async def enqueue(task: CrawlTask, *, persist: bool = True) -> bool:
        canonical = canonicalize_url(task.parent_url or task.url, task.url)
        if not canonical:
            return False

        normalized = CrawlTask(
            url=canonical,
            parent_url=task.parent_url,
            path=task.path,
            depth=task.depth,
            source=task.source,
            link_text=task.link_text,
            root_department=task.root_department,
        )

        async with state_lock:
            if canonical in seen_urls or canonical in completed_urls:
                return False
            if (
                canonical in failed_urls
                and args.resume
                and not args.retry_failures
            ):
                seen_urls.add(canonical)
                return False
            total_known_pages = previous_page_count + counters.enqueued
            if page_limit_reached(total_known_pages, args.max_pages):
                return False
            seen_urls.add(canonical)
            counters.enqueued += 1

        if persist:
            await writer.write("frontier", asdict(normalized))
        await queue.put(normalized)
        return True

    async def enqueue_file(task: FileTask, *, persist: bool = True) -> bool:
        canonical = canonicalize_url(task.source_page_url, task.url)
        if not canonical:
            return False
        normalized = FileTask(
            url=canonical,
            source_page_url=task.source_page_url,
            source_page_title=task.source_page_title,
            department_path=task.department_path,
            root_department=task.root_department,
            link_text=task.link_text,
            expected_extension=task.expected_extension,
            source=task.source,
        )
        async with state_lock:
            if canonical in seen_file_urls or canonical in completed_file_urls:
                return False
            total_known_files = previous_file_count + counters.files_enqueued
            if args.max_files > 0 and total_known_files >= args.max_files:
                return False
            seen_file_urls.add(canonical)
            counters.files_enqueued += 1
        if persist:
            await writer.write("file_frontier", asdict(normalized))
        await file_queue.put(normalized)
        return True

    if args.resume:
        pending: dict[str, CrawlTask] = {}
        for record in load_jsonl(output_dir / OUTPUT_FILES["frontier"]):
            task = task_from_record(record)
            if task is None:
                continue
            canonical = canonicalize_url(task.parent_url or task.url, task.url)
            if not canonical or canonical in completed_urls:
                continue
            if canonical in failed_urls and not args.retry_failures:
                continue
            pending.setdefault(canonical, task)

        for task in pending.values():
            await enqueue(task, persist=False)

        if args.download_files:
            pending_files: dict[str, FileTask] = {}
            for record in load_jsonl(output_dir / OUTPUT_FILES["file_frontier"]):
                task = file_task_from_record(record)
                if task is None:
                    continue
                canonical = canonicalize_url(task.source_page_url, task.url)
                if not canonical or canonical in completed_file_urls:
                    continue
                pending_files.setdefault(canonical, task)
            for task in pending_files.values():
                await enqueue_file(task, persist=False)

    if queue.empty():
        await enqueue(
            CrawlTask(
                url=start_url,
                parent_url=None,
                path=(),
                depth=0,
                source="seed",
                link_text="Organization",
                root_department=None,
            )
        )

    page_rate_limiter = AsyncRateLimiter(args.requests_per_second)
    file_rate_limiter = AsyncRateLimiter(args.file_requests_per_second)
    render_semaphore = asyncio.Semaphore(max(1, args.render_concurrency))
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=max(1, args.parse_workers),
        thread_name_prefix="bgn-html-parser",
    )
    loop.set_default_executor(executor)

    started_at = time.monotonic()
    crawl_completed = False

    try:
        async with async_playwright() as playwright:
            context = await launch_context(
                playwright,
                profile_dir,
                args.headless,
                args.browser_channel,
            )

            try:
                await ensure_authenticated(
                    context,
                    start_url,
                    args.headless,
                    args.timeout_seconds,
                )

                async def worker(worker_id: int) -> None:
                    while True:
                        task = await queue.get()
                        if task is None:
                            queue.task_done()
                            return

                        try:
                            html_text, final_url, _status, _rendered = await fetch_html(
                                context,
                                task,
                                args,
                                page_rate_limiter,
                                render_semaphore,
                                counters,
                            )

                            page_record, links, file_links = await asyncio.to_thread(
                                extract_page,
                                html_text,
                                task,
                                final_url,
                                args.file_extensions_set,
                                args.discover_hinted_files,
                            )

                            content_hash = page_record["content_hash"]
                            duplicate_of: str | None = None
                            async with state_lock:
                                if page_record["content"]:
                                    existing = content_hash_to_url.get(content_hash)
                                    if existing and existing != final_url:
                                        duplicate_of = existing
                                        counters.duplicates += 1
                                    else:
                                        content_hash_to_url[content_hash] = final_url

                                counters.completed += 1
                                counters.max_depth_seen = max(
                                    counters.max_depth_seen, task.depth
                                )
                                completed_urls.add(task.url)
                                completed_urls.add(final_url)

                            if duplicate_of and args.omit_duplicate_content:
                                page_record["content"] = ""

                            compact_page = {
                                "url": final_url,
                                "parent_url": task.parent_url,
                                "title": page_record["title"],
                                "depth": task.depth,
                                "root_department": task.root_department,
                                "department_path": page_record["department_path"],
                                "short_id": page_record["short_id"],
                                "page_owner_department": page_record[
                                    "page_owner_department"
                                ],
                                "last_changed": page_record["last_changed"],
                                "language": page_record["language"],
                                "content": page_record["content"],
                            }
                            if duplicate_of:
                                compact_page["duplicate_of"] = duplicate_of

                            await writer.write("page", compact_page)

                            for link in links:
                                if (
                                    not link.crawlable
                                    or not depth_allows_children(
                                        task.depth, args.max_depth
                                    )
                                ):
                                    continue

                                root_department = task.root_department
                                if task.depth == 0:
                                    root_department = link.text

                                child_task = CrawlTask(
                                    url=link.url,
                                    parent_url=final_url,
                                    path=task.path + (link.text,),
                                    depth=task.depth + 1,
                                    source=link.source,
                                    link_text=link.text,
                                    root_department=root_department,
                                )
                                accepted = await enqueue(child_task)
                                if not accepted:
                                    continue

                                await writer.write(
                                    "relation",
                                    {
                                        "parent_url": final_url,
                                        "parent_title": page_record["title"],
                                        "child_url": link.url,
                                        "child_title": link.text,
                                        "parent_depth": task.depth,
                                        "child_depth": task.depth + 1,
                                        "root_department": root_department,
                                        "relation_type": link.relation_type,
                                    },
                                )
                                counters.edges += 1

                            if args.download_files:
                                for file_link in file_links:
                                    await enqueue_file(
                                        FileTask(
                                            url=file_link.url,
                                            source_page_url=final_url,
                                            source_page_title=page_record["title"],
                                            department_path=tuple(
                                                page_record["department_path"]
                                            ),
                                            root_department=task.root_department,
                                            link_text=file_link.text,
                                            expected_extension=file_link.expected_extension,
                                            source=file_link.source,
                                        )
                                    )

                            if (
                                args.verbose
                                and counters.completed % args.progress_every == 0
                            ):
                                elapsed = max(0.001, time.monotonic() - started_at)
                                rate = counters.completed / elapsed
                                print(
                                    f"pages={counters.completed} "
                                    f"pending={queue.qsize()} "
                                    f"files={counters.files_downloaded} "
                                    f"errors={counters.failed + counters.files_failed} "
                                    f"rate={rate:.2f}/s"
                                )

                        except Exception as exc:  # noqa: BLE001 - worker boundary
                            async with state_lock:
                                counters.failed += 1
                                failed_urls.add(task.url)
                            await writer.write(
                                "error",
                                {
                                    "type": "page",
                                    "url": task.url,
                                    "parent_url": task.parent_url,
                                    "depth": task.depth,
                                    "error": f"{type(exc).__name__}: {exc}",
                                },
                            )
                            if args.verbose:
                                print(
                                    f"Page failed: {task.url}: {exc}",
                                    file=sys.stderr,
                                )
                        finally:
                            queue.task_done()

                async def file_worker(worker_id: int) -> None:
                    while True:
                        task = await file_queue.get()
                        if task is None:
                            file_queue.task_done()
                            return
                        try:
                            body, final_url, filename, content_type, status = (
                                await fetch_file_bytes(
                                    context,
                                    task,
                                    args,
                                    file_rate_limiter,
                                    counters,
                                )
                            )
                            digest = hashlib.sha256(body).hexdigest()
                            duplicate_of: str | None = None
                            async with state_lock:
                                existing_path = file_hash_to_path.get(digest)
                                if existing_path:
                                    duplicate_of = existing_path
                                    counters.file_duplicates += 1
                                else:
                                    destination = file_storage_path(
                                        files_dir,
                                        digest,
                                        filename,
                                        task.root_department,
                                    )
                                    await asyncio.to_thread(destination.write_bytes, body)
                                    existing_path = str(
                                        destination.relative_to(output_dir)
                                    )
                                    file_hash_to_path[digest] = existing_path
                                completed_file_urls.add(task.url)
                                completed_file_urls.add(final_url)
                                counters.files_downloaded += 1
                                counters.file_bytes_downloaded += len(body)

                            file_record = {
                                "url": final_url,
                                "source_page_url": task.source_page_url,
                                "source_page_title": task.source_page_title,
                                "root_department": task.root_department,
                                "department_path": list(task.department_path),
                                "filename": filename,
                                "stored_path": existing_path,
                                "content_type": content_type,
                                "size_bytes": len(body),
                                "sha256": digest,
                            }
                            if duplicate_of:
                                file_record["duplicate_of"] = duplicate_of
                            await writer.write("file", file_record)
                        except SkippableFileError as exc:
                            async with state_lock:
                                counters.files_skipped += 1
                            if args.verbose:
                                print(f"File skipped: {task.url}: {exc}")
                        except Exception as exc:  # noqa: BLE001
                            async with state_lock:
                                counters.files_failed += 1
                            await writer.write(
                                "error",
                                {
                                    "type": "file",
                                    "url": task.url,
                                    "source_page_url": task.source_page_url,
                                    "error": f"{type(exc).__name__}: {exc}",
                                },
                            )
                            if args.verbose:
                                print(
                                    f"File failed: {task.url}: {exc}",
                                    file=sys.stderr,
                                )
                        finally:
                            file_queue.task_done()

                workers = [
                    asyncio.create_task(worker(index + 1))
                    for index in range(args.concurrency)
                ]
                file_workers = [
                    asyncio.create_task(file_worker(index + 1))
                    for index in range(args.file_concurrency)
                ]

                await queue.join()
                for _ in workers:
                    await queue.put(None)
                await queue.join()
                await asyncio.gather(*workers)

                await file_queue.join()
                for _ in file_workers:
                    await file_queue.put(None)
                await file_queue.join()
                await asyncio.gather(*file_workers)
                crawl_completed = True

            finally:
                await context.close()
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        await writer.close()


    if crawl_completed and not args.keep_state:
        for kind in ("frontier", "file_frontier"):
            state_path = output_dir / OUTPUT_FILES[kind]
            if state_path.exists():
                state_path.unlink()
        state_dir = output_dir / ".state"
        if state_dir.exists() and not any(state_dir.iterdir()):
            state_dir.rmdir()

    elapsed = max(0.001, time.monotonic() - started_at)
    summary = {
        "pages": previous_page_count + counters.completed,
        "relations": previous_relation_count + counters.edges,
        "files": previous_file_count + counters.files_downloaded,
        "errors": previous_error_count + counters.failed + counters.files_failed,
        "max_depth": max(previous_max_depth, counters.max_depth_seen),
        "elapsed_seconds": round(elapsed, 3),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        "Crawl complete: "
        f"pages={summary['pages']}, "
        f"relations={summary['relations']}, "
        f"files={summary['files']}, "
        f"errors={summary['errors']}"
    )



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively crawl only links found in the Bosch GlobalNet left "
            "navigation. Depth is unlimited by default."
        )
    )
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--output-dir", default="bosch_org_sidebar_v2")
    parser.add_argument(
        "--profile-dir",
        default=".playwright-bgn-profile",
        help="Dedicated persistent browser profile used for Bosch SSO.",
    )
    parser.add_argument(
        "--browser-channel",
        default="msedge",
        help="Playwright browser channel; use an empty string for bundled Chromium.",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=0,
        help="Maximum link depth. 0 means unlimited (default).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20000,
        help="Maximum unique pages. 0 means unlimited; default: 20000.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="Concurrent HTTP workers; default: 6.",
    )
    parser.add_argument(
        "--parse-workers",
        type=int,
        default=4,
        help="HTML parser threads; default: 4.",
    )
    parser.add_argument(
        "--render-concurrency",
        type=int,
        default=1,
        help="Maximum concurrent browser-render fallbacks; default: 1.",
    )
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=8.0,
        help="Global request rate. 0 disables throttling; default: 8.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument(
        "--render-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use browser rendering only when static HTML lacks #pageContent.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing frontier/pages output.",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="When resuming, retry page URLs recorded in errors.jsonl.",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Keep hidden frontier files after a fully successful crawl.",
    )
    parser.add_argument(
        "--omit-duplicate-content",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="For duplicate content hashes, retain metadata but omit repeated text.",
    )
    parser.add_argument("--write-batch-size", type=int, default=100)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show periodic progress and individual request errors.",
    )
    parser.add_argument(
        "--download-files",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download supported files found on visited pages; default: enabled.",
    )
    parser.add_argument(
        "--file-extensions",
        default=",".join(sorted(DEFAULT_DOWNLOAD_EXTENSIONS)),
        help="Comma-separated downloadable file extensions.",
    )
    parser.add_argument(
        "--include-images",
        action="store_true",
        help="Also download image files linked from page content.",
    )
    parser.add_argument(
        "--discover-hinted-files",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Probe links whose text/URL suggests a download even without an extension.",
    )
    parser.add_argument(
        "--file-concurrency",
        type=int,
        default=3,
        help="Concurrent file downloads; default: 3.",
    )
    parser.add_argument(
        "--file-requests-per-second",
        type=float,
        default=2.0,
        help="Independent file-download request rate; 0 disables throttling.",
    )
    parser.add_argument(
        "--max-file-size-mb",
        type=float,
        default=100.0,
        help="Maximum size per downloaded file; default: 100 MB.",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=0,
        help="Maximum unique files. 0 means unlimited.",
    )
    parser.add_argument(
        "--file-timeout-seconds",
        type=int,
        default=300,
        help="Timeout for each file download; default: 300 seconds.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.max_depth < 0:
        raise SystemExit("--max-depth must be 0 (unlimited) or a positive integer.")
    if args.max_pages < 0:
        raise SystemExit("--max-pages must be 0 (unlimited) or a positive integer.")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1.")
    if args.parse_workers < 1:
        raise SystemExit("--parse-workers must be at least 1.")
    if args.render_concurrency < 1:
        raise SystemExit("--render-concurrency must be at least 1.")
    if args.timeout_seconds < 1:
        raise SystemExit("--timeout-seconds must be at least 1.")
    if args.max_retries < 0:
        raise SystemExit("--max-retries must be non-negative.")
    if args.write_batch_size < 1:
        raise SystemExit("--write-batch-size must be at least 1.")
    if args.progress_every < 1:
        raise SystemExit("--progress-every must be at least 1.")
    if args.file_concurrency < 1:
        raise SystemExit("--file-concurrency must be at least 1.")
    if args.file_timeout_seconds < 1:
        raise SystemExit("--file-timeout-seconds must be at least 1.")
    if args.max_files < 0:
        raise SystemExit("--max-files must be 0 (unlimited) or positive.")
    if not math.isfinite(args.max_file_size_mb) or args.max_file_size_mb <= 0:
        raise SystemExit("--max-file-size-mb must be finite and positive.")
    args.file_extensions_set = parse_file_extensions(
        args.file_extensions,
        args.include_images,
    )
    for name in (
        "requests_per_second",
        "file_requests_per_second",
        "retry_backoff_seconds",
    ):
        value = float(getattr(args, name))
        if not math.isfinite(value) or value < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be finite and non-negative.")


def main() -> None:
    args = build_parser().parse_args()
    validate_args(args)
    asyncio.run(crawl(args))


if __name__ == "__main__":
    main()
