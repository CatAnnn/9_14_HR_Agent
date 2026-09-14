from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, TextIO
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import html
from lxml.html import HtmlElement
from playwright.async_api import (
    APIResponse,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from project_config import (
    BROWSER_PROFILE_DIR,
    CRAWLER_OUTPUT_SCHEMA_VERSION,
    CRAWL_OUTPUT_DIR,
    DEFAULT_BROWSER_ARGS,
    DEFAULT_BROWSER_CHANNEL,
    DEFAULT_BROWSER_EXECUTABLE_PATH,
    DEFAULT_BROWSER_FALLBACK_CHANNELS,
    DEFAULT_HEADLESS,
    DEFAULT_IGNORE_HTTPS_ERRORS,
    DEFAULT_START_URL,
    TARGET_ROOTS,
    configure_console_utf8,
    env_bool,
    env_float,
    env_optional_int,
    runtime_path_problems,
)

CRAWLER_VERSION = "11.5-linux-server-runtime"
OUTPUT_SCHEMA_VERSION = CRAWLER_OUTPUT_SCHEMA_VERSION

TARGET_ROOT_LABELS = frozenset(item["label"] for item in TARGET_ROOTS)
TARGET_ROOT_LABEL_KEYS = frozenset(label.casefold() for label in TARGET_ROOT_LABELS)
TARGET_ROOT_BY_LABEL_KEY = {item["label"].casefold(): item for item in TARGET_ROOTS}
TARGET_ROOT_URLS = frozenset(item["url"] for item in TARGET_ROOTS)
TARGET_ROOT_FSIDS = frozenset(item["fsid"] for item in TARGET_ROOTS)

# Other known first-level entries in the global Organization sidebar. They are
# never crawled, but help distinguish a global sidebar from a project-local one.
GLOBAL_ORGANIZATION_SIBLING_LABEL_KEYS = frozenset(
    label.casefold()
    for label in (
        "Board of Management",
        "Supervisory Board",
        "Honorary chairman",
        "Industrietreuhand KG",
        "International Advisory Committee",
        "Countries & Regions",
        "Subsidiaries (TOGE)",
        "Business Units",
        "Employee Representation",
    )
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
    "organization",
    "home",
    "homepage",
    "overview",
    "start page",
    "startpage",
}

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
AUTHENTICATION_STATUS_CODES = {401, 403}
MAX_META_REFRESH_REDIRECTS = 5
MAX_META_REFRESH_DELAY_SECONDS = 5.0
PAGE_CONTENT_SELECTOR = (
    "#pageContent, main, [role='main'], #pageContainer, "
    ".main-content, .page-content"
)
AUTHENTICATION_FORM_SELECTOR = (
    "input[type='password'], form[action*='login' i], "
    "form[action*='signin' i]"
)
AUTHENTICATION_URL_MARKERS = ("/login", "/signin", "saml", "oauth", "adfs")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
SPACE_RE = re.compile(r"\s+")
META_REFRESH_CONTENT_RE = re.compile(
    r"^\s*(?P<delay>\d+(?:\.\d+)?)\s*;\s*url\s*=\s*(?P<target>.+?)\s*$",
    re.IGNORECASE,
)
HTML_START_TAG_RE = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9:_-]*)"
    r"(?P<attributes>(?:\s[^<>]*?)?)\s*/?>",
    re.DOTALL,
)
HTML_CONTENT_ATTRIBUTE_RE = re.compile(
    r'''\b(?P<name>id|role|class)\s*=\s*(?:
        "(?P<double>[^"]*)"
        |'(?P<single>[^']*)'
        |(?P<bare>[^\s"'=<>`]+)
    )''',
    re.IGNORECASE | re.VERBOSE,
)
HIDDEN_CLASS_RE = re.compile(
    r"\.([A-Za-z0-9_-]+)\s*\{[^{}]*display\s*:\s*none",
    re.IGNORECASE,
)
CLASS_XPATH = (
    "contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')"
)

SIDEBAR_TARGET_XPATH = (
    ".//a[@href or @data-href or @data-url or @data-link or @data-target-url "
    "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url]"
    " | .//*[@role='link' and (@data-href or @data-url or @data-link or @data-target-url "
    "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url or @onclick)]"
    " | .//button[@data-href or @data-url or @data-link or @data-target-url "
    "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url or @onclick]"
    " | .//*[@data-destination-url or @data-navigation-url or @data-page-url or @data-link-url]"
)
ONCLICK_URL_RE = re.compile(
    r"(?:location(?:\.href|\.assign|\.replace)?|window\.open|"
    r"navigateTo|navigate|openPage|goTo|loadPage)\s*(?:=|\()\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
ONCLICK_QUOTED_URL_RE = re.compile(
    r"[\"']((?:https?://|/|\.\.?/)[^\"']+|[^\"']+\.(?:html?|shtml|xhtml|jsp|aspx)(?:[?#][^\"']*)?)[\"']",
    re.IGNORECASE,
)

OUTPUT_FILES = {
    # Only two visible files are produced: pages.jsonl and summary.json.
    "page": "pages.jsonl",
    # Resume and failure state stays inside the hidden .state directory.
    "error": ".state/page_errors.jsonl",
    "frontier": ".state/page_frontier.jsonl",
}

DURABLE_OUTPUT_KINDS = frozenset({"frontier", "error"})

# Files produced by older crawler versions. They are removed automatically so
# the output directory does not contain obsolete or misleading results.
OBSOLETE_OUTPUT_PATHS = (
    "navigation.jsonl",
    "relations.jsonl",
    "files.jsonl",
    "errors.jsonl",
    ".state/file_frontier.jsonl",
)

BROWSER_IDENTITY_FILENAME = ".crawler-browser-identity"


# ============================================================================
# 运行参数配置区
# 环境变量优先于此处默认值，便于 Linux 服务和容器运行。
# ============================================================================

CRAWLER_CONFIG: dict[str, Any] = {
    # 该页面只用于验证 Bosch SSO，不写入结果。
    "start_url": DEFAULT_START_URL,

    # 输出目录中正常完成后只保留 pages.jsonl 和 summary.json。
    "output_dir": str(CRAWL_OUTPUT_DIR),
    "profile_dir": str(BROWSER_PROFILE_DIR),
    "browser_channel": DEFAULT_BROWSER_CHANNEL,
    "browser_fallback_channels": list(DEFAULT_BROWSER_FALLBACK_CHANNELS),
    "browser_executable_path": DEFAULT_BROWSER_EXECUTABLE_PATH,
    "browser_args": list(DEFAULT_BROWSER_ARGS),
    "ignore_https_errors": DEFAULT_IGNORE_HTTPS_ERRORS,
    "headless": DEFAULT_HEADLESS,
    "auth_only": env_bool("SPIDER_AUTH_ONLY", False),
    "browser_check_only": env_bool("SPIDER_BROWSER_CHECK_ONLY", False),

    # 0 表示不限制。
    "max_depth": env_optional_int("SPIDER_MAX_DEPTH", 0) or 0,
    "max_pages": env_optional_int("SPIDER_MAX_PAGES", 0) or 0,

    # safe / balanced / fast。None 表示按配置档和 CPU 自动设置。
    "worker_profile": os.environ.get(
        "SPIDER_WORKER_PROFILE",
        "balanced" if DEFAULT_HEADLESS else "fast",
    ).strip().casefold(),
    "concurrency": env_optional_int("SPIDER_CONCURRENCY", None),
    "parse_workers": env_optional_int("SPIDER_PARSE_WORKERS", None),
    "render_concurrency": env_optional_int("SPIDER_RENDER_CONCURRENCY", None),

    # 请求和重试。
    "requests_per_second": env_float("SPIDER_REQUESTS_PER_SECOND", 8.0),
    "timeout_seconds": 46,
    "authentication_wait_seconds": 600,
    "max_retries": 3,
    "retry_backoff_seconds": 1.0,

    # 侧边栏完整展开。
    "render_fallback": True,
    "force_render_sidebar": False,
    "expand_all_sidebar": True,
    "min_sidebar_links": 2,
    "sidebar_expand_rounds": 24,
    "sidebar_click_batch_size": 40,
    "sidebar_max_clicks": 600,
    "sidebar_control_max_attempts": 2,
    "sidebar_expansion_timeout_seconds": 46,
    "sidebar_stable_passes": 2,
    "sidebar_settle_ms": 180,
    "sidebar_network_idle_seconds": 0,
    "render_content_wait_seconds": 46,
    "sidebar_link_wait_seconds": 46,
    "sidebar_stability_checks": 3,
    "sidebar_stability_interval_ms": 100,
    "sidebar_native_click_fallback": True,
    "sidebar_native_click_rounds": 3,
    "sidebar_native_max_clicks": 80,
    "fail_on_incomplete_sidebar": False,

    # 页面正文。
    "expand_page_content": True,
    "content_expand_rounds": 3,
    "content_expand_max_clicks": 80,
    "preserve_hidden_content": True,
    "block_nonessential_resources": True,
    "fast_mode": False,

    # 首次运行设为 False；使用同一 output_dir 断点继续时设为 True。
    "resume": env_bool("SPIDER_RESUME", True),
    "retry_failures": True,
    "fail_on_page_errors": env_bool("SPIDER_FAIL_ON_PAGE_ERRORS", False),

    # 正常且无失败地完成后，默认删除隐藏断点文件。
    # 异常中断或仍有失败页面时，断点文件会自动保留。
    "keep_state": False,
    "cleanup_obsolete_outputs": True,

    # 写入和日志。
    "write_batch_size": 50,
    "progress_every": 100,
    "verbose": True,
}


def build_args_from_code_config() -> argparse.Namespace:
    """Create the runtime namespace exclusively from CRAWLER_CONFIG."""
    return argparse.Namespace(**dict(CRAWLER_CONFIG))


class RetryableFetchError(RuntimeError):
    """A transient request error that may succeed after retrying."""


class PermanentFetchError(RuntimeError):
    """A non-transient request error that should not be retried."""


class AuthenticationExpiredError(RuntimeError):
    """The persisted browser session no longer reaches authenticated content."""


class PageContentNotFoundError(RetryableFetchError):
    """A fetched page does not currently expose a supported content container."""


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
    navigation_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FetchResult:
    html_text: str
    final_url: str
    sidebar_complete: bool


@dataclass(slots=True)
class CrawlCounters:
    enqueued: int = 0
    completed: int = 0
    failed: int = 0
    max_depth_seen: int = 0
    rendered_fallbacks: int = 0
    retried_requests: int = 0
    authentication_recoveries: int = 0
    sidebar_complete_pages: int = 0
    sidebar_incomplete_pages: int = 0


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
    """Write JSONL records with durable resume-state persistence."""

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
        self.queue: asyncio.Queue[
            tuple[str, dict[str, Any], asyncio.Future[None] | None] | None
        ] = asyncio.Queue(maxsize=self.batch_size * 20)
        self._task: asyncio.Task[None] | None = None
        self._files: dict[str, TextIO] = {}
        self._failure: BaseException | None = None
        self._closed = False

    async def start(self) -> None:
        if self._task is not None or self._closed:
            raise RuntimeError("JSONL writer cannot be started more than once.")
        mode = "a" if self.append else "w"
        try:
            for kind, filename in OUTPUT_FILES.items():
                path = self.output_dir / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                self._files[kind] = path.open(
                    mode,
                    encoding="utf-8",
                    buffering=1024 * 1024,
                )
        except BaseException:
            self._close_files(suppress_errors=True)
            raise
        self._task = asyncio.create_task(self._run())

    async def write(self, kind: str, record: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("JSONL writer is already closed.")
        if kind not in self._files:
            raise ValueError(f"Unknown output kind: {kind}")
        self._raise_if_failed()

        acknowledgement: asyncio.Future[None] | None = None
        if kind in DURABLE_OUTPUT_KINDS:
            acknowledgement = asyncio.get_running_loop().create_future()

        assert self._task is not None
        put_task = asyncio.create_task(
            self.queue.put((kind, record, acknowledgement))
        )
        try:
            # Queue backpressure must also be interruptible. If the writer dies
            # while the bounded queue is full, no consumer remains to wake a
            # producer blocked in queue.put().
            done, _pending = await asyncio.wait(
                (put_task, self._task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._task in done:
                if not put_task.done():
                    put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
                if acknowledgement is not None:
                    if acknowledgement.done():
                        # Retrieve a queued acknowledgement error instead of
                        # leaving an unobserved Future exception behind.
                        await acknowledgement
                    acknowledgement.cancel()
                self._raise_if_failed()
                raise RuntimeError("JSONL writer stopped before queuing a write.")

            await put_task
            if acknowledgement is not None:
                # A blocked producer can enqueue at the same moment the writer
                # fails. Race the durable acknowledgement against the writer
                # task so no producer waits forever on an orphaned future.
                done, _pending = await asyncio.wait(
                    (acknowledgement, self._task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if acknowledgement in done:
                    await acknowledgement
                    return
                acknowledgement.cancel()
                self._raise_if_failed()
                raise RuntimeError(
                    "JSONL writer stopped before acknowledging a write."
                )
            self._raise_if_failed()
        except BaseException:
            if not put_task.done():
                put_task.cancel()
                await asyncio.gather(put_task, return_exceptions=True)
            if acknowledgement is not None and not acknowledgement.done():
                acknowledgement.cancel()
            raise

    async def close(self) -> None:
        if self._closed:
            self._raise_if_failed()
            return
        self._closed = True
        close_after_failure = False
        try:
            if self._task is not None and not self._task.done():
                sentinel = asyncio.create_task(self.queue.put(None))
                done, _pending = await asyncio.wait(
                    (sentinel, self._task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if self._task in done and not sentinel.done():
                    sentinel.cancel()
                    await asyncio.gather(sentinel, return_exceptions=True)
                else:
                    await sentinel
            if self._task is not None:
                await self._task
        except BaseException:
            close_after_failure = True
            raise
        finally:
            self._close_files(suppress_errors=close_after_failure)

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise RuntimeError("JSONL writer failed.") from self._failure
        if self._task is None or not self._task.done():
            return
        if self._task.cancelled():
            raise RuntimeError("JSONL writer stopped unexpectedly.")
        failure = self._task.exception()
        if failure is not None:
            self._failure = failure
            raise RuntimeError("JSONL writer failed.") from failure

    def _close_files(self, *, suppress_errors: bool) -> None:
        first_error: OSError | ValueError | None = None
        for handle in self._files.values():
            try:
                handle.flush()
            except (OSError, ValueError) as exc:
                first_error = first_error or exc
            try:
                handle.close()
            except (OSError, ValueError) as exc:
                first_error = first_error or exc
        self._files.clear()
        if first_error is not None and not suppress_errors:
            raise first_error

    def _fail_pending_writes(self, failure: BaseException) -> None:
        self._failure = failure
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if item is None:
                    continue
                _kind, _record, acknowledgement = item
                if acknowledgement is None or acknowledgement.done():
                    continue
                if isinstance(failure, asyncio.CancelledError):
                    acknowledgement.cancel()
                else:
                    acknowledgement.set_exception(failure)
            finally:
                self.queue.task_done()

    async def _run(self) -> None:
        buffers: dict[str, list[str]] = {kind: [] for kind in self._files}

        def flush_kind(kind: str) -> None:
            buffer = buffers[kind]
            if not buffer:
                return
            handle = self._files[kind]
            handle.write("".join(buffer))
            handle.flush()
            buffer.clear()

        try:
            while True:
                item = await self.queue.get()
                acknowledgement: asyncio.Future[None] | None = None
                try:
                    if item is None:
                        for kind in buffers:
                            flush_kind(kind)
                        return

                    kind, record, acknowledgement = item
                    buffers[kind].append(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )

                    if (
                        kind in DURABLE_OUTPUT_KINDS
                        or len(buffers[kind]) >= self.batch_size
                    ):
                        flush_kind(kind)

                    if acknowledgement is not None and not acknowledgement.done():
                        acknowledgement.set_result(None)
                except BaseException as exc:
                    if acknowledgement is not None and not acknowledgement.done():
                        if isinstance(exc, asyncio.CancelledError):
                            acknowledgement.cancel()
                        else:
                            acknowledgement.set_exception(exc)
                    raise
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self._fail_pending_writes(exc)
            raise


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

    try:
        absolute = urljoin(base_url, href)
        parsed = urlsplit(absolute)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    if not hostname:
        return None

    # This browser carries an authenticated session. Do not let a malformed
    # sidebar link target arbitrary services on an otherwise allowed host.
    if port not in {None, 443}:
        return None

    # The supplied page redirects inside-ws links to bgn.
    if hostname == "inside-ws.bosch.com":
        hostname = "bgn.bosch.com"

    netloc = hostname

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


def target_root_definition(label: str | None) -> dict[str, str] | None:
    key = clean_text(label).casefold()
    return TARGET_ROOT_BY_LABEL_KEY.get(key)


def is_selected_root_label(label: str | None) -> bool:
    return clean_text(label).casefold() in TARGET_ROOT_LABEL_KEYS


def selected_root_page_key(url: str, root_department: str | None) -> tuple[str, str]:
    """Use URL + selected root as the page identity.

    A page exposed below two selected roots is intentionally retained once per
    root context so the resulting page record keeps the correct organization
    path for downstream exports.
    """
    return url, clean_text(root_department).casefold()


def selected_root_from_path(path: Iterable[str]) -> str | None:
    for raw_label in path:
        root = target_root_definition(str(raw_label))
        if root is not None:
            return root["label"]
    return None


def task_is_in_selected_scope(task: CrawlTask) -> bool:
    return is_selected_root_label(task.root_department)


def target_root_url(label: str | None) -> str | None:
    root = target_root_definition(label)
    return root["url"] if root is not None else None


def is_internal_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and port in {None, 443}
        and hostname in ALLOWED_HOSTS
    )


def is_crawlable_url(url: str) -> bool:
    """Return True for internal, page-like sidebar targets.

    Sidebar entries on Bosch GlobalNet are not limited to ``/FIRSTspiritWeb/``
    and ``/alias/`` paths. Because discovery remains restricted to the left
    sidebar, accepting other internal HTML-like paths is safe while downloads,
    assets, search and redirect endpoints remain excluded.
    """
    if not is_internal_url(url):
        return False

    parsed = urlsplit(url)
    path = parsed.path.lower()

    if any(part in path for part in BLOCKED_PATH_PARTS):
        return False

    suffix = Path(path).suffix.lower()
    if suffix in BLOCKED_EXTENSIONS:
        return False

    return not suffix or suffix in {
        ".html", ".htm", ".shtml", ".xhtml", ".jsp", ".aspx"
    }


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


def normalize_navigation_path(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize a label path while retaining meaningful hierarchy."""
    result: list[str] = []
    for value in values:
        label = clean_text(value)
        if not label:
            continue
        if result and result[-1].casefold() == label.casefold():
            continue
        result.append(label)
    return tuple(result)


def merge_navigation_path(
    parent_path: Iterable[str],
    local_path: Iterable[str],
    leaf_text: str,
) -> tuple[str, ...]:
    """Join a page-local sidebar branch to the inherited absolute path."""
    parent = list(normalize_navigation_path(parent_path))
    local = list(normalize_navigation_path(local_path))
    leaf = clean_text(leaf_text)

    if not local and leaf:
        local = [leaf]
    elif leaf and (not local or local[-1].casefold() != leaf.casefold()):
        local.append(leaf)

    if not parent:
        merged = list(local)
        while len(merged) > 1 and merged[0].casefold() in GENERIC_LABELS:
            merged.pop(0)
        return tuple(merged)
    if not local:
        merged = list(parent)
        while len(merged) > 1 and merged[0].casefold() in GENERIC_LABELS:
            merged.pop(0)
        return tuple(merged)

    overlap = 0
    for size in range(min(len(parent), len(local)), 0, -1):
        if [item.casefold() for item in parent[-size:]] == [
            item.casefold() for item in local[:size]
        ]:
            overlap = size
            break

    merged = list(normalize_navigation_path(parent + local[overlap:]))
    while len(merged) > 1 and merged[0].casefold() in GENERIC_LABELS:
        merged.pop(0)
    return tuple(merged)


def sidebar_link_key(
    target_url: str,
    root_department: str | None,
) -> tuple[str, str]:
    """Identify a sidebar target once per selected organization root."""
    return selected_root_page_key(target_url, root_department)


def unique_links(records: Iterable[LinkRecord]) -> list[LinkRecord]:
    """Deduplicate exact sidebar occurrences while retaining alternate paths."""
    priorities = {
        "root_department": 0,
        "navigation_child": 1,
        "navigation_section": 2,
        "sidebar_link": 3,
        "navigation_fallback": 4,
    }
    best: dict[tuple[str, tuple[str, ...]], LinkRecord] = {}
    for record in records:
        path_key = tuple(
            item.casefold()
            for item in normalize_navigation_path(record.navigation_path)
        )
        key = (record.url, path_key)
        existing = best.get(key)
        if existing is None:
            best[key] = record
            continue

        existing_priority = priorities.get(existing.relation_type, 99)
        current_priority = priorities.get(record.relation_type, 99)
        if current_priority < existing_priority:
            best[key] = record
            continue

        if current_priority == existing_priority:
            existing_generic = existing.text.casefold() in GENERIC_LABELS
            current_generic = record.text.casefold() in GENERIC_LABELS
            if existing_generic and not current_generic:
                best[key] = record

    return list(best.values())


def _node_navigation_target(node: HtmlElement) -> str:
    """Read a navigation target from href/data attributes or simple onclick."""
    for attribute in (
        "href",
        "data-href",
        "data-url",
        "data-link",
        "data-target-url",
        "data-destination-url",
        "data-navigation-url",
        "data-page-url",
        "data-link-url",
    ):
        value = clean_text(node.attrib.get(attribute, ""))
        if value:
            return value

    onclick = node.attrib.get("onclick", "")
    match = ONCLICK_URL_RE.search(onclick)
    if match:
        return clean_text(match.group(1))
    fallback = ONCLICK_QUOTED_URL_RE.search(onclick)
    return clean_text(fallback.group(1)) if fallback else ""


def _node_navigation_label(node: HtmlElement, url: str) -> str:
    return (
        element_text(node)
        or clean_text(node.attrib.get("aria-label", ""))
        or clean_text(node.attrib.get("title", ""))
        or Path(urlsplit(url).path).stem
        or url
    )


def _sidebar_navigation_path(
    node: HtmlElement,
    page_url: str,
    node_url: str,
    node_label: str,
) -> tuple[str, ...]:
    """Build a stable label path from nested sidebar ``li`` ancestors."""
    labels: list[str] = []
    ancestors = reversed(list(node.iterancestors("li")))
    for li in ancestors:
        if not isinstance(li, HtmlElement):
            continue
        direct_node = _first_direct_anchor(li)
        if direct_node is None:
            continue
        if direct_node is node:
            target = node_url
            label = node_label
        else:
            target = canonicalize_url(
                page_url,
                _node_navigation_target(direct_node),
            )
            if not target:
                continue
            label = _node_navigation_label(direct_node, target)
        if label and (not labels or labels[-1] != label):
            labels.append(label)

    if node_label and (not labels or labels[-1] != node_label):
        labels.append(node_label)
    return tuple(labels)


def _first_direct_anchor(li: HtmlElement) -> HtmlElement | None:
    candidates = li.xpath(
        "./a[@href or @data-href or @data-url or @data-link or @data-target-url "
        "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url]"
        " | ./*[not(self::ul)]/a[@href or @data-href or @data-url or @data-link or @data-target-url "
        "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url]"
        " | ./button[@data-href or @data-url or @data-link or @data-target-url "
        "or @data-destination-url or @data-navigation-url or @data-page-url or @data-link-url or @onclick]"
    )
    return first_node(candidates)


def _link_record_from_node(
    node: HtmlElement,
    page_url: str,
    relation_type: str,
) -> LinkRecord | None:
    raw_target = _node_navigation_target(node)
    url = canonicalize_url(page_url, raw_target)
    if not url:
        return None
    label = _node_navigation_label(node, url)
    return LinkRecord(
        url=url,
        text=label,
        source="left_navigation",
        relation_type=relation_type,
        crawlable=is_crawlable_url(url),
        navigation_path=_sidebar_navigation_path(
            node,
            page_url,
            url,
            label,
        ),
    )


def _direct_list_links(
    list_node: HtmlElement,
    page_url: str,
    relation_type: str,
) -> list[LinkRecord]:
    records: list[LinkRecord] = []
    for li in list_node.xpath("./li"):
        if not isinstance(li, HtmlElement):
            continue
        node = _first_direct_anchor(li)
        if node is None:
            continue
        record = _link_record_from_node(node, page_url, relation_type)
        if record is not None:
            records.append(record)
    return records


def _all_sidebar_links(
    sidebar: HtmlElement,
    page_url: str,
    relation_type: str,
) -> list[LinkRecord]:
    records: list[LinkRecord] = []
    for node in sidebar.xpath(SIDEBAR_TARGET_XPATH):
        if not isinstance(node, HtmlElement):
            continue
        record = _link_record_from_node(node, page_url, relation_type)
        if record is not None:
            records.append(record)
    return records


def _top_level_sidebar_items(sidebar: HtmlElement) -> list[HtmlElement]:
    """Return first-level ``li`` nodes for a global Organization sidebar."""
    if sidebar.attrib.get("id") == "subNav":
        nodes = sidebar.xpath("./ul[1]/li")
    else:
        nodes = sidebar.xpath(".//*[@id='subNav']/ul[1]/li")
        if not nodes:
            nodes = sidebar.xpath("./ul[1]/li")
    return [node for node in nodes if isinstance(node, HtmlElement)]


def _selected_root_scope(
    sidebar: HtmlElement,
    page_url: str,
    root_department: str,
) -> tuple[HtmlElement | None, str]:
    """Locate the exact first-level sidebar subtree for one selected root.

    The Bosch pages sometimes expose the complete Organization sidebar and
    sometimes a local project sidebar. If any of the six known root entries is
    present, the requested root must be found exactly; otherwise this container
    is treated as a local sidebar that already belongs to the current root.
    """
    desired = target_root_definition(root_department)
    if desired is None:
        return None, "invalid_root"

    desired_url = canonicalize_url(page_url, desired["url"]) or desired["url"]
    known_selected_root_present = False

    for li in _top_level_sidebar_items(sidebar):
        anchor = _first_direct_anchor(li)
        if anchor is None:
            continue
        target = canonicalize_url(page_url, _node_navigation_target(anchor))
        label = _node_navigation_label(anchor, target or page_url)
        fsid = clean_text(anchor.attrib.get("data-fsid", ""))

        label_key = label.casefold()
        is_any_selected_root = (
            label_key in TARGET_ROOT_LABEL_KEYS
            or fsid in TARGET_ROOT_FSIDS
            or bool(target and target in TARGET_ROOT_URLS)
        )
        if (
            is_any_selected_root
            or label_key in GLOBAL_ORGANIZATION_SIBLING_LABEL_KEYS
        ):
            known_selected_root_present = True

        if (
            label.casefold() == desired["label"].casefold()
            or fsid == desired["fsid"]
            or target == desired_url
        ):
            return li, "global_root_subtree"

    if known_selected_root_present:
        # A global Organization sidebar is present, but the selected root is
        # missing. Do not fall back to the entire sidebar because that would
        # enqueue sibling branches such as Countries, Subsidiaries or Boards.
        return None, "global_root_missing"

    return sidebar, "local_root_sidebar"


def _link_belongs_to_selected_root(
    record: LinkRecord,
    root_department: str,
) -> bool:
    """Reject links that explicitly belong to another selected root."""
    desired = target_root_definition(root_department)
    if desired is None:
        return False

    path_keys = {clean_text(item).casefold() for item in record.navigation_path}
    if path_keys.intersection(GLOBAL_ORGANIZATION_SIBLING_LABEL_KEYS):
        return False

    path_root = selected_root_from_path(record.navigation_path)
    if path_root is not None and path_root.casefold() != desired["label"].casefold():
        return False

    if clean_text(record.text).casefold() in GLOBAL_ORGANIZATION_SIBLING_LABEL_KEYS:
        return False

    canonical_start = canonicalize_url(DEFAULT_START_URL, DEFAULT_START_URL)
    if canonical_start and record.url == canonical_start:
        return False

    target_root = next(
        (
            item
            for item in TARGET_ROOTS
            if record.url == item["url"]
        ),
        None,
    )
    if target_root is not None and target_root["label"] != desired["label"]:
        return False

    text_root = target_root_definition(record.text)
    if text_root is not None and text_root["label"] != desired["label"]:
        return False

    return True


def extract_links(
    document: HtmlElement,
    page_url: str,
    *,
    is_start_page: bool,
    root_department: str | None = None,
) -> list[LinkRecord]:
    """Extract only links inside the active selected first-level root.

    When ``root_department`` is provided, links from all sibling Organization
    roots and from global navigation sections are excluded. The legacy
    unrestricted branch is retained only for compatibility with helper tests;
    normal V9 crawling always supplies one of the six selected roots.
    """
    preferred = [
        node
        for node in document.xpath("//*[@id='subNav']")
        if isinstance(node, HtmlElement)
    ]
    raw_sidebars = preferred or [
        node
        for node in document.xpath("//*[@id='sidebar']")
        if isinstance(node, HtmlElement)
    ]

    sidebars: list[HtmlElement] = []
    seen_nodes: set[int] = set()
    for sidebar in raw_sidebars:
        marker = id(sidebar)
        if marker in seen_nodes:
            continue
        seen_nodes.add(marker)
        sidebars.append(sidebar)

    if not sidebars:
        return []

    canonical_page = canonicalize_url(page_url, page_url) or page_url
    discovered: list[LinkRecord] = []

    if root_department is not None:
        if not is_selected_root_label(root_department):
            return []

        for sidebar in sidebars:
            scope, scope_mode = _selected_root_scope(
                sidebar,
                page_url,
                root_department,
            )
            if scope is None:
                continue

            relation_type = (
                "selected_root_descendant"
                if scope_mode == "global_root_subtree"
                else "selected_root_local_descendant"
            )
            for record in _all_sidebar_links(scope, page_url, relation_type):
                if _link_belongs_to_selected_root(record, root_department):
                    discovered.append(record)

        selected_root_url = target_root_url(root_department)
        excluded_urls = {canonical_page}
        if selected_root_url:
            excluded_urls.add(selected_root_url)
        return unique_links(
            record for record in discovered if record.url not in excluded_urls
        )

    # Compatibility fallback: unrestricted discovery when no root is supplied.
    for sidebar in sidebars:
        top_list = first_node(sidebar.xpath("./ul[1] | .//*[@id='subNav']/ul[1]"))
        if top_list is not None:
            discovered.extend(
                _direct_list_links(
                    top_list,
                    page_url,
                    "root_department" if is_start_page else "navigation_section",
                )
            )
        discovered.extend(_all_sidebar_links(sidebar, page_url, "sidebar_link"))

    return unique_links(
        record for record in discovered if record.url != canonical_page
    )

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


def append_node_marker(node: HtmlElement, marker: str) -> None:
    marker = clean_text(marker)
    if not marker:
        return
    prefix = f" [{marker}] "
    node.tail = prefix + (node.tail or "")


def remove_unwanted_nodes(
    root: HtmlElement,
    hidden_classes: set[str],
    *,
    preserve_hidden_content: bool,
) -> None:
    """Remove executable/decorative nodes without silently losing useful labels."""
    removable_classes = {"somOptions", "menu_toggler"}

    for node in list(root.iterdescendants()):
        if not isinstance(node.tag, str):
            continue

        tag = node.tag.lower()
        classes = set(node.attrib.get("class", "").split())

        if tag == "img":
            label = clean_text(
                node.attrib.get("alt", "")
                or node.attrib.get("title", "")
            )
            if label:
                append_node_marker(node, f"Image: {label}")
            remove_node_preserving_tail(node)
            continue

        if tag == "iframe":
            label = clean_text(
                node.attrib.get("title", "")
                or node.attrib.get("src", "")
            )
            if label:
                append_node_marker(node, f"Embedded content: {label}")
            remove_node_preserving_tail(node)
            continue

        if tag == "svg":
            # Keep SVG nodes that contain meaningful text, such as organization
            # charts; remove icon-only SVGs.
            if not element_text(node):
                remove_node_preserving_tail(node)
            continue

        should_remove = (
            tag in {"script", "style", "noscript"}
            or bool(classes.intersection(removable_classes))
            or (
                not preserve_hidden_content
                and node_is_hidden(node, hidden_classes)
            )
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


def meta_refresh_target(html_text: str, base_url: str) -> str | None:
    """Return a safe, immediate internal target from a meta-refresh shell."""
    document = parse_document(html_text)
    contents = document.xpath(
        "//meta[translate(normalize-space(@http-equiv), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='refresh']"
        "/@content"
    )
    for raw_content in contents:
        match = META_REFRESH_CONTENT_RE.match(str(raw_content))
        if match is None:
            continue

        delay = float(match.group("delay"))
        if not math.isfinite(delay) or delay > MAX_META_REFRESH_DELAY_SECONDS:
            continue

        raw_target = clean_text(match.group("target"))
        if (
            len(raw_target) >= 2
            and raw_target[0] == raw_target[-1]
            and raw_target[0] in {"'", '"'}
        ):
            raw_target = raw_target[1:-1].strip()

        try:
            target = canonicalize_url(base_url, raw_target)
        except ValueError as exc:
            raise PermanentFetchError(
                "Unsafe or invalid meta-refresh target was rejected."
            ) from exc
        if target is None or not is_internal_url(target) or not is_crawlable_url(target):
            raise PermanentFetchError(
                "Unsafe or invalid meta-refresh target was rejected."
            )
        return target
    return None


def extract_page(
    html_text: str,
    task: CrawlTask,
    final_url: str,
    preserve_hidden_content: bool,
) -> tuple[dict[str, Any], list[LinkRecord]]:
    """Extract page text and links inside the selected root sidebar only."""
    document = parse_document(html_text)
    hidden_classes = (
        set() if preserve_hidden_content else hidden_css_classes(document)
    )

    title_node = first_node(
        document.xpath(
            f"//*[{class_xpath('headerTitle')} and {class_xpath('bottomAbsolute')}]"
        )
    )
    if title_node is None:
        title_node = first_node(document.xpath("//title"))
    title = element_text(title_node)

    content_node = first_node(
        document.xpath(
            "//*[@id='pageContent']"
            " | //main"
            " | //*[@role='main']"
            " | //*[@id='pageContainer']"
            " | //*[contains(concat(' ', normalize-space(@class), ' '), ' main-content ')]"
            " | //*[contains(concat(' ', normalize-space(@class), ' '), ' page-content ')]"
        )
    )
    if content_node is None:
        raise PageContentNotFoundError(
            "Fetched HTML does not contain a supported page content container."
        )

    links = extract_links(
        document,
        final_url,
        is_start_page=task.depth == 0,
        root_department=task.root_department,
    )

    # All metadata and navigation records are already detached values. Detach
    # the content root too so hidden-node checks retain the old subtree boundary,
    # then mutate this one-use DOM instead of deep-copying large article trees.
    language = meta_content(document, "DCSext.wtg_lang")
    parent = content_node.getparent()
    if parent is not None:
        parent.remove(content_node)
    remove_unwanted_nodes(
        content_node,
        hidden_classes,
        preserve_hidden_content=preserve_hidden_content,
    )

    content = clean_multiline_text(content_node.itertext())
    path = list(task.path)
    if not path and task.depth == 0:
        path = [title or task.root_department or "Organization"]
    elif path and path[-1].strip().lower() in GENERIC_LABELS and title:
        path[-1] = title

    return (
        {
            "title": title,
            "department_path": path,
            "language": language,
            "content": content,
        },
        links,
    )


def expected_browser_identity(
    browser_channel: str,
    browser_executable_path: str,
) -> str:
    if browser_executable_path:
        executable = Path(browser_executable_path).expanduser().resolve()
        return f"executable:{executable}"
    channel = clean_text(browser_channel)
    return f"channel:{channel}" if channel else "playwright-chromium"


def verify_browser_identity(profile_dir: Path, expected: str) -> None:
    marker = profile_dir / BROWSER_IDENTITY_FILENAME
    if not marker.is_file():
        return
    recorded = marker.read_text(encoding="utf-8").strip()
    if recorded and recorded != expected:
        raise RuntimeError(
            "This browser profile is already bound to a different browser "
            f"({recorded!r}, requested {expected!r}). Use a new "
            "SPIDER_PROFILE_DIR and authenticate it separately."
        )


def persist_browser_identity(profile_dir: Path, identity: str) -> None:
    marker = profile_dir / BROWSER_IDENTITY_FILENAME
    temporary = profile_dir / f"{BROWSER_IDENTITY_FILENAME}.tmp"
    temporary.write_text(f"{identity}\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(marker)
    try:
        os.chmod(marker, 0o600)
    except OSError:
        pass


async def launch_context(
    playwright: Any,
    profile_dir: Path,
    headless: bool,
    browser_channel: str,
    browser_fallback_channels: Iterable[str] = (),
    browser_executable_path: str = "",
    browser_args: Iterable[str] = (),
    ignore_https_errors: bool = False,
    allow_fallbacks: bool = True,
) -> BrowserContext:
    persistent_identity: str | None = None
    if not allow_fallbacks:
        persistent_identity = expected_browser_identity(
            browser_channel,
            browser_executable_path,
        )
        verify_browser_identity(profile_dir, persistent_identity)

    async def finish_launch(context: BrowserContext) -> BrowserContext:
        if persistent_identity is not None:
            try:
                persist_browser_identity(profile_dir, persistent_identity)
            except OSError:
                await context.close()
                raise
        return context

    kwargs = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "ignore_https_errors": ignore_https_errors,
        "viewport": {"width": 1440, "height": 1000},
        "locale": "en-US",
        "args": list(browser_args),
    }

    failures: list[str] = []
    last_error: PlaywrightError | None = None
    if browser_executable_path:
        try:
            return await finish_launch(
                await playwright.chromium.launch_persistent_context(
                    executable_path=browser_executable_path,
                    **kwargs,
                )
            )
        except PlaywrightError as exc:
            last_error = exc
            failures.append(f"executable {browser_executable_path!r}: {exc}")
            print(
                f"Unable to launch browser executable "
                f"{browser_executable_path!r}: {exc}",
                file=sys.stderr,
            )

    channels: list[str] = []
    channel_candidates = (browser_channel,)
    if allow_fallbacks:
        channel_candidates = (browser_channel, *browser_fallback_channels)
    if browser_executable_path and not allow_fallbacks:
        channel_candidates = ()

    for channel in channel_candidates:
        normalized = clean_text(channel)
        if normalized and normalized not in channels:
            channels.append(normalized)

    for channel in channels:
        try:
            return await finish_launch(
                await playwright.chromium.launch_persistent_context(
                    channel=channel,
                    **kwargs,
                )
            )
        except PlaywrightError as exc:
            last_error = exc
            failures.append(f"channel {channel!r}: {exc}")
            print(
                f"Unable to launch browser channel {channel!r}: {exc}",
                file=sys.stderr,
            )

    configured_identity = bool(browser_executable_path or channels)
    if allow_fallbacks or not configured_identity:
        try:
            return await finish_launch(
                await playwright.chromium.launch_persistent_context(**kwargs)
            )
        except PlaywrightError as exc:
            last_error = exc
            failures.append(f"Playwright Chromium: {exc}")

    details = "\n".join(f"  - {item}" for item in failures)
    raise RuntimeError(
        "No configured Chromium browser could be launched. Run "
        "'./run_linux.sh check' and './run_linux.sh browser-check'. A persistent "
        "login profile is never opened with a different browser automatically; "
        "configure a separate SPIDER_PROFILE_DIR before changing browsers.\n"
        f"Launch attempts:\n{details}"
    ) from last_error


async def ensure_authenticated(
    context: BrowserContext,
    start_url: str,
    headless: bool,
    timeout_seconds: int,
    authentication_wait_seconds: int,
) -> bool:
    """Ensure the persistent context is authenticated; return True after login."""
    page = await context.new_page()
    try:
        await page.goto(
            start_url,
            wait_until="domcontentloaded",
            timeout=timeout_seconds * 1000,
        )
        try:
            await page.wait_for_selector(
                "#pageContent",
                state="attached",
                timeout=max(1, min(timeout_seconds, 15)) * 1000,
            )
        except PlaywrightTimeoutError:
            pass
        else:
            return False

        if headless:
            raise AuthenticationExpiredError(
                "The persisted browser profile is not authenticated or its SSO "
                "session has expired. From an SSH X11 or remote-desktop session, "
                "run './run_linux.sh auth' once, then restart the headless crawl."
            )

        print(
            "\nComplete Bosch SSO authentication in the opened browser window.\n"
            "The crawler will continue automatically after the Organization "
            "page becomes available."
        )
        try:
            await page.wait_for_selector(
                "#pageContent",
                state="attached",
                timeout=authentication_wait_seconds * 1000,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Authentication was not confirmed before the configured wait "
                "time expired."
            ) from exc
        return True
    finally:
        await page.close()


async def response_text(response: APIResponse) -> str:
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "text" not in content_type:
        raise ValueError(f"Unexpected content type: {content_type or 'unknown'}")
    return await response.text()


async def request_meta_refresh_target(
    context: BrowserContext,
    url: str,
    timeout_seconds: int,
) -> str | None:
    """Read a redirect shell through the authenticated request context."""
    response = await context.request.get(
        url,
        timeout=timeout_seconds * 1000,
        fail_on_status_code=False,
    )
    try:
        status = response.status
        if status in AUTHENTICATION_STATUS_CODES:
            raise AuthenticationExpiredError(f"HTTP {status}")
        if status in RETRYABLE_STATUS_CODES:
            raise RetryableFetchError(f"Retryable HTTP {status}")
        if status != 200:
            raise PermanentFetchError(f"HTTP {status}")
        return meta_refresh_target(
            await response_text(response),
            response.url,
        )
    finally:
        await response.dispose()


def html_has_page_content(html_text: str) -> bool:
    for tag_match in HTML_START_TAG_RE.finditer(html_text):
        if tag_match.group("tag").casefold() == "main":
            return True

        for attribute_match in HTML_CONTENT_ATTRIBUTE_RE.finditer(
            tag_match.group("attributes")
        ):
            name = attribute_match.group("name").casefold()
            value = next(
                value
                for value in (
                    attribute_match.group("double"),
                    attribute_match.group("single"),
                    attribute_match.group("bare"),
                )
                if value is not None
            ).casefold()
            if name == "id" and value in {"pagecontent", "pagecontainer"}:
                return True
            if name == "role" and value == "main":
                return True
            if name == "class" and {
                "main-content",
                "page-content",
            }.intersection(value.split()):
                return True
    return False


def count_sidebar_navigation_targets(
    html_text: str,
    page_url: str,
    *,
    is_start_page: bool,
    root_department: str | None = None,
) -> int:
    """Count links only inside the selected first-level root subtree."""
    try:
        document = parse_document(html_text)
        return len(
            extract_links(
                document,
                page_url,
                is_start_page=is_start_page,
                root_department=root_department,
            )
        )
    except Exception:
        return 0


async def _mark_selected_sidebar_scope(
    page: Page,
    root_department: str | None,
) -> dict[str, Any]:
    """Mark only the selected root subtree for live expansion and counting."""
    root = target_root_definition(root_department)
    if root is None:
        return {"found": False, "mode": "invalid_root"}

    result = await page.evaluate(
        r"""
        ({ desiredLabel, desiredUrl, desiredFsid, selectedLabels, siblingLabels }) => {
          for (const old of document.querySelectorAll('[data-bgn-crawler-selected-root]')) {
            old.removeAttribute('data-bgn-crawler-selected-root');
          }

          const subNav = document.querySelector('#subNav');
          if (!subNav) return { found: false, mode: 'no_subnav' };

          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const key = value => clean(value).toLocaleLowerCase();
          const canonical = value => {
            try {
              const url = new URL(value, location.href);
              const host = url.hostname.toLowerCase() === 'inside-ws.bosch.com'
                ? 'bgn.bosch.com'
                : url.hostname.toLowerCase();
              let path = url.pathname.replace(/\/{2,}/g, '/');
              if (path.length > 1) path = path.replace(/\/+$/, '');
              return `https://${host}${path}`;
            } catch (_) {
              return '';
            }
          };

          let topItems = [];
          try { topItems = Array.from(subNav.querySelectorAll(':scope > ul:first-of-type > li')); }
          catch (_) { topItems = Array.from(subNav.querySelectorAll('ul:first-of-type > li')); }

          let globalSidebarDetected = false;
          const desiredUrlKey = canonical(desiredUrl);
          for (const li of topItems) {
            let anchor = null;
            try { anchor = li.querySelector(':scope > a, :scope > *:not(ul) > a'); }
            catch (_) { anchor = li.querySelector('a'); }
            if (!anchor) continue;

            const label = key(anchor.textContent || anchor.getAttribute('aria-label') || anchor.title);
            const fsid = clean(anchor.getAttribute('data-fsid'));
            const href = canonical(
              anchor.getAttribute('href')
              || anchor.getAttribute('data-href')
              || anchor.getAttribute('data-url')
              || ''
            );

            if (selectedLabels.includes(label) || siblingLabels.includes(label)) {
              globalSidebarDetected = true;
            }
            if (
              label === key(desiredLabel)
              || fsid === desiredFsid
              || (href && href === desiredUrlKey)
            ) {
              li.setAttribute('data-bgn-crawler-selected-root', 'true');
              return { found: true, mode: 'global_root_subtree' };
            }
          }

          if (globalSidebarDetected) {
            return { found: false, mode: 'global_root_missing' };
          }

          subNav.setAttribute('data-bgn-crawler-selected-root', 'true');
          return { found: true, mode: 'local_root_sidebar' };
        }
        """,
        {
            "desiredLabel": root["label"],
            "desiredUrl": root["url"],
            "desiredFsid": root["fsid"],
            "selectedLabels": sorted(TARGET_ROOT_LABEL_KEYS),
            "siblingLabels": sorted(GLOBAL_ORGANIZATION_SIBLING_LABEL_KEYS),
        },
    )
    return {
        "found": bool(result.get("found", False)),
        "mode": str(result.get("mode", "unknown")),
    }


async def _sidebar_runtime_state(page: Page) -> dict[str, int]:
    """Return live counts across all desktop/mobile sidebar containers."""
    return await page.evaluate(
        """
        () => {
          const marked = document.querySelector('[data-bgn-crawler-selected-root="true"]');
          const candidates = marked
            ? [marked]
            : Array.from(document.querySelectorAll('#subNav, #sidebar'));
          const roots = candidates.filter(root => !candidates.some(
            other => other !== root && root.contains(other)
          ));
          if (!roots.length) return { targets: 0, collapsed: 0 };

          const targetNodes = new Set();
          for (const root of roots) {
            for (const node of root.querySelectorAll(
              'a[href], [data-href], [data-url], [data-link], [data-target-url], '
              + '[data-destination-url], [data-navigation-url], [data-page-url], '
              + '[data-link-url], [role="link"][onclick], button[onclick]'
            )) targetNodes.add(node);
          }

          const targets = new Set();
          for (const node of targetNodes) {
            const raw = (
              node.getAttribute?.('href')
              || node.getAttribute?.('data-href')
              || node.getAttribute?.('data-url')
              || node.getAttribute?.('data-link')
              || node.getAttribute?.('data-target-url')
              || node.getAttribute?.('data-destination-url')
              || node.getAttribute?.('data-navigation-url')
              || node.getAttribute?.('data-page-url')
              || node.getAttribute?.('data-link-url')
              || node.getAttribute?.('onclick')
              || ''
            ).trim();
            if (raw && raw !== '#' && !/^javascript:\\s*void/i.test(raw)) targets.add(raw);
          }

          const hidden = element => {
            if (!element) return false;
            const style = getComputedStyle(element);
            return Boolean(
              element.hidden
              || element.getAttribute('aria-hidden') === 'true'
              || style.display === 'none'
              || style.visibility === 'hidden'
              || Number(style.opacity) === 0
            );
          };

          const resolveControlled = node => {
            const ids = [
              node.getAttribute?.('aria-controls'),
              node.getAttribute?.('data-target'),
              node.getAttribute?.('data-bs-target'),
              node.getAttribute?.('href')?.startsWith('#')
                ? node.getAttribute('href').slice(1)
                : null,
            ].filter(Boolean);
            for (const rawId of ids) {
              const id = String(rawId).replace(/^#/, '');
              const controlled = document.getElementById(id);
              if (controlled) return controlled;
            }
            const li = node.closest?.('li');
            if (!li) return null;
            try {
              return li.querySelector(
                ':scope > ul, :scope > ol, :scope > .submenu, :scope > .sub-menu, '
                + ':scope > .collapse, :scope > div > ul, :scope > div > ol'
              );
            } catch (_) {
              return li.querySelector('ul, ol, .submenu, .sub-menu, .collapse');
            }
          };

          const selector = [
            '[aria-expanded="false"]', '.collapsed', '.is-collapsed',
            '.menu_toggler', '.menu-toggler', '.nav-toggle', '.submenu-toggle',
            '.sub-menu-toggle', '.accordion-toggle', '[data-toggle="collapse"]',
            '[data-bs-toggle="collapse"]', 'button[aria-controls]',
            '[role="button"][aria-controls]', 'li.has-children > button',
            'li.has-children > a', 'li.hasChildren > button',
            'li.hasChildren > a', 'li.parent > button', 'li.parent > a'
          ].join(',');

          const controls = new Set();
          for (const root of roots) {
            for (const node of root.querySelectorAll(selector)) controls.add(node);
          }

          let collapsed = 0;
          for (const node of controls) {
            const classes = String(node.className || '');
            const liClasses = String(node.closest?.('li')?.className || '');
            const controlled = resolveControlled(node);
            const aria = node.getAttribute?.('aria-expanded');
            const knownCollapsed = aria === 'false'
              || /(?:^|\\s)(?:collapsed|is-collapsed|closed)(?:\\s|$)/i.test(classes)
              || /(?:^|\\s)(?:collapsed|is-collapsed|closed)(?:\\s|$)/i.test(liClasses)
              || hidden(controlled);
            if (knownCollapsed) collapsed += 1;
          }
          return { targets: targets.size, collapsed };
        }
        """
    )


async def _expand_all_sidebar_sections(
    page: Page,
    args: argparse.Namespace,
    *,
    initial_state: dict[str, int] | None = None,
) -> dict[str, int | bool]:
    """Recursively expand collapsed controls across all sidebar containers."""
    if initial_state is None:
        initial_state = await _sidebar_runtime_state(page)
    if not args.expand_all_sidebar or args.sidebar_expand_rounds <= 0:
        return {
            "passes": 0,
            "clicks": 0,
            "targets_before": initial_state["targets"],
            "targets": initial_state["targets"],
            "collapsed_before": initial_state["collapsed"],
            "collapsed_remaining": initial_state["collapsed"],
            "complete": initial_state["collapsed"] == 0,
        }

    loop = asyncio.get_running_loop()
    deadline = loop.time() + args.sidebar_expansion_timeout_seconds
    total_clicks = 0
    stable_passes = 0
    previous_targets = initial_state["targets"]
    completed_passes = 0
    final_state: dict[str, int] | None = None

    for pass_index in range(args.sidebar_expand_rounds):
        if loop.time() >= deadline or total_clicks >= args.sidebar_max_clicks:
            break

        result = await page.evaluate(
            """
            ({ batchSize, maxAttempts }) => {
              const marked = document.querySelector('[data-bgn-crawler-selected-root="true"]');
          const candidates = marked
            ? [marked]
            : Array.from(document.querySelectorAll('#subNav, #sidebar'));
              const roots = candidates.filter(root => !candidates.some(
                other => other !== root && root.contains(other)
              ));
              if (!roots.length) return { clicked: 0, pending: 0 };

              const hidden = element => {
                if (!element) return false;
                const style = getComputedStyle(element);
                return Boolean(
                  element.hidden
                  || element.getAttribute('aria-hidden') === 'true'
                  || style.display === 'none'
                  || style.visibility === 'hidden'
                  || Number(style.opacity) === 0
                );
              };

              const resolveControlled = node => {
                const ids = [
                  node.getAttribute?.('aria-controls'),
                  node.getAttribute?.('data-target'),
                  node.getAttribute?.('data-bs-target'),
                  node.getAttribute?.('href')?.startsWith('#')
                    ? node.getAttribute('href').slice(1)
                    : null,
                ].filter(Boolean);
                for (const rawId of ids) {
                  const id = String(rawId).replace(/^#/, '');
                  const controlled = document.getElementById(id);
                  if (controlled) return controlled;
                }
                const li = node.closest?.('li');
                if (!li) return null;
                try {
                  return li.querySelector(
                    ':scope > ul, :scope > ol, :scope > .submenu, :scope > .sub-menu, '
                    + ':scope > .collapse, :scope > div > ul, :scope > div > ol'
                  );
                } catch (_) {
                  return li.querySelector('ul, ol, .submenu, .sub-menu, .collapse');
                }
              };

              const selector = [
                '[aria-expanded="false"]', '.collapsed', '.is-collapsed',
                '.menu_toggler', '.menu-toggler', '.nav-toggle', '.submenu-toggle',
                '.sub-menu-toggle', '.accordion-toggle', '[data-toggle="collapse"]',
                '[data-bs-toggle="collapse"]', 'button[aria-controls]',
                '[role="button"][aria-controls]', 'li.has-children > button',
                'li.has-children > a', 'li.hasChildren > button',
                'li.hasChildren > a', 'li.parent > button', 'li.parent > a'
              ].join(',');

              const nodes = new Set();
              for (const root of roots) {
                for (const node of root.querySelectorAll(selector)) nodes.add(node);
              }

              let clicked = 0;
              let pending = 0;
              for (const node of nodes) {
                if (!(node instanceof HTMLElement)) continue;

                const classes = String(node.className || '');
                const li = node.closest?.('li');
                const liClasses = String(li?.className || '');
                const controlled = resolveControlled(node);
                const aria = node.getAttribute?.('aria-expanded');
                const href = (node.getAttribute?.('href') || '').trim();
                const tag = node.tagName?.toLowerCase();
                const explicitToggle = tag === 'button'
                  || node.getAttribute?.('role') === 'button'
                  || /toggle|toggler|expand|collapse|accordion/i.test(classes)
                  || node.hasAttribute?.('data-toggle')
                  || node.hasAttribute?.('data-bs-toggle')
                  || node.hasAttribute?.('aria-controls')
                  || !href || href === '#' || /^javascript:/i.test(href);
                if (!explicitToggle) continue;

                const classCollapsed = /(?:^|\\s)(?:collapsed|is-collapsed|closed)(?:\\s|$)/i.test(classes);
                const liCollapsed = /(?:^|\\s)(?:collapsed|is-collapsed|closed)(?:\\s|$)/i.test(liClasses);
                const controlledHidden = hidden(controlled);
                const unknownToggle = !aria && !controlled
                  && /toggle|toggler|expand|collapse|accordion/i.test(classes);
                const shouldExpand = aria === 'false'
                  || classCollapsed || liCollapsed || controlledHidden || unknownToggle;
                if (!shouldExpand) continue;

                pending += 1;
                const attempts = Number(node.dataset.bgnCrawlerExpandAttempts || 0);
                if (attempts >= maxAttempts || clicked >= batchSize) continue;
                node.dataset.bgnCrawlerExpandAttempts = String(attempts + 1);

                try { node.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (_) {}
                const preventDefault = event => event.preventDefault();
                const navigableAnchor = tag === 'a' && href && href !== '#' && !/^javascript:/i.test(href);
                if (navigableAnchor) {
                  node.addEventListener('click', preventDefault, { capture: true, once: true });
                }
                node.dispatchEvent(new MouseEvent('click', {
                  bubbles: true,
                  cancelable: true,
                  composed: true,
                  view: window,
                }));
                clicked += 1;
              }
              return { clicked, pending };
            }
            """,
            {
                "batchSize": min(
                    args.sidebar_click_batch_size,
                    args.sidebar_max_clicks - total_clicks,
                ),
                "maxAttempts": args.sidebar_control_max_attempts,
            },
        )
        completed_passes = pass_index + 1
        clicked = int(result.get("clicked", 0))
        total_clicks += clicked

        if clicked > 0 and args.sidebar_settle_ms > 0:
            await page.wait_for_timeout(args.sidebar_settle_ms)

        state_after = await _sidebar_runtime_state(page)
        targets_changed = state_after["targets"] != previous_targets
        previous_targets = state_after["targets"]
        stable_passes = stable_passes + 1 if clicked == 0 and not targets_changed else 0

        if (
            state_after["collapsed"] == 0
            and stable_passes >= args.sidebar_stable_passes
        ):
            final_state = state_after
            break

        if clicked == 0 and state_after["collapsed"] > 0:
            if args.sidebar_stability_interval_ms > 0:
                await page.wait_for_timeout(args.sidebar_stability_interval_ms)

    if final_state is None:
        final_state = await _sidebar_runtime_state(page)
    return {
        "passes": completed_passes,
        "clicks": total_clicks,
        "targets_before": initial_state["targets"],
        "targets": final_state["targets"],
        "collapsed_before": initial_state["collapsed"],
        "collapsed_remaining": final_state["collapsed"],
        "complete": final_state["collapsed"] == 0,
    }


async def _native_click_sidebar_fallback(
    page: Page,
    args: argparse.Namespace,
) -> dict[str, int]:
    """Use Playwright trusted clicks when synthetic DOM events are ignored."""
    if not args.sidebar_native_click_fallback:
        return {"rounds": 0, "clicks": 0}

    selector = ", ".join(
        f"[data-bgn-crawler-selected-root='true'] {item}"
        for item in (
            '[aria-expanded="false"]',
            '.collapsed',
            '.is-collapsed',
            '.menu_toggler',
            '.menu-toggler',
            '.nav-toggle',
            '.submenu-toggle',
            '.sub-menu-toggle',
            '.accordion-toggle',
            '[data-toggle="collapse"]',
            '[data-bs-toggle="collapse"]',
        )
    )

    total_clicks = 0
    completed_rounds = 0
    for round_index in range(args.sidebar_native_click_rounds):
        if total_clicks >= args.sidebar_native_max_clicks:
            break
        state_before = await _sidebar_runtime_state(page)
        if state_before["collapsed"] == 0:
            break

        locator = page.locator(selector)
        count = min(
            await locator.count(),
            args.sidebar_native_max_clicks - total_clicks,
        )
        clicked_this_round = 0
        for index in range(count):
            control = locator.nth(index)
            try:
                aria = await control.get_attribute("aria-expanded")
                classes = (await control.get_attribute("class") or "").casefold()
                if aria not in {None, "false"} and not any(
                    token in classes for token in ("collapsed", "is-collapsed")
                ):
                    continue
                await control.scroll_into_view_if_needed(timeout=1500)
                await control.click(force=True, timeout=2500)
                clicked_this_round += 1
                total_clicks += 1
            except (PlaywrightError, PlaywrightTimeoutError):
                continue

        completed_rounds = round_index + 1
        if clicked_this_round == 0:
            break
        if args.sidebar_settle_ms > 0:
            await page.wait_for_timeout(max(args.sidebar_settle_ms, 250))

    return {"rounds": completed_rounds, "clicks": total_clicks}


async def _expand_page_content_sections(
    page: Page,
    args: argparse.Namespace,
) -> dict[str, int]:
    """Expand accordions/tabs inside the main content before HTML extraction."""
    if not args.expand_page_content:
        return {"rounds": 0, "clicks": 0}

    selector = ", ".join(
        f"{root} {item}"
        for root in ("#pageContent", "main", '[role="main"]')
        for item in (
            'button[aria-expanded="false"]',
            '[role="button"][aria-expanded="false"]',
            'button[data-toggle="collapse"]',
            'button[data-bs-toggle="collapse"]',
            '.accordion-toggle[aria-expanded="false"]',
        )
    )

    total_clicks = 0
    completed_rounds = 0
    for round_index in range(args.content_expand_rounds):
        if total_clicks >= args.content_expand_max_clicks:
            break
        locator = page.locator(selector)
        count = min(
            await locator.count(),
            args.content_expand_max_clicks - total_clicks,
        )
        clicked_this_round = 0
        for index in range(count):
            control = locator.nth(index)
            try:
                if await control.get_attribute("aria-expanded") == "true":
                    continue
                await control.scroll_into_view_if_needed(timeout=1200)
                await control.click(force=True, timeout=2000)
                clicked_this_round += 1
                total_clicks += 1
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
        completed_rounds = round_index + 1
        if clicked_this_round == 0:
            break
        await page.wait_for_timeout(max(150, args.sidebar_settle_ms))

    return {"rounds": completed_rounds, "clicks": total_clicks}


async def render_page_html(
    context: BrowserContext,
    url: str,
    *,
    root_department: str,
    args: argparse.Namespace,
    render_semaphore: asyncio.Semaphore,
) -> tuple[str, str, int, dict[str, Any]]:
    """Render a page, expand navigation/content, and return diagnostics."""
    async with render_semaphore:
        page: Page = await context.new_page()
        navigation_locked = False
        try:
            async def route_request(route: Any, request: Any) -> None:
                if (
                    navigation_locked
                    and request.is_navigation_request()
                    and request.frame == page.main_frame
                ):
                    await route.abort()
                    return
                if (
                    args.block_nonessential_resources
                    and request.resource_type in {"image", "media", "font"}
                ):
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/*", route_request)

            async def navigate(target_url: str) -> tuple[int, Any | None]:
                nonlocal navigation_locked
                navigation_locked = False
                try:
                    navigation_response = await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=args.timeout_seconds * 1000,
                    )
                finally:
                    # Sidebar and content expansion must never navigate away
                    # from the resolved content page.
                    navigation_locked = True

                response_status = (
                    navigation_response.status if navigation_response else 0
                )
                if response_status in AUTHENTICATION_STATUS_CODES:
                    raise AuthenticationExpiredError(f"HTTP {response_status}")
                if response_status in RETRYABLE_STATUS_CODES:
                    raise RetryableFetchError(
                        f"Retryable HTTP {response_status}"
                    )
                if response_status and response_status != 200:
                    raise PermanentFetchError(f"HTTP {response_status}")
                return response_status, navigation_response

            async def rendered_authentication_required() -> bool:
                current_url = page.url.casefold()
                login_marker = await page.locator(
                    AUTHENTICATION_FORM_SELECTOR
                ).count()
                return login_marker > 0 or any(
                    marker in current_url
                    for marker in AUTHENTICATION_URL_MARKERS
                )

            requested_navigation_url = url
            status, navigation_response = await navigate(requested_navigation_url)
            initial_url = canonicalize_url(url, url) or url
            response_url = (
                navigation_response.url if navigation_response else page.url
            )
            resolved_initial_url = canonicalize_url(url, response_url) or response_url
            visited_redirects = {initial_url, resolved_initial_url}
            for redirect_index in range(MAX_META_REFRESH_REDIRECTS + 1):
                if await page.locator(PAGE_CONTENT_SELECTOR).count() > 0:
                    break
                if await rendered_authentication_required():
                    break

                redirect_target = None
                if navigation_response is not None:
                    try:
                        response_html = await navigation_response.text()
                    except PlaywrightError:
                        response_html = ""
                    if response_html:
                        redirect_target = meta_refresh_target(
                            response_html,
                            navigation_response.url,
                        )
                if redirect_target is None:
                    redirect_target = meta_refresh_target(
                        await page.content(),
                        page.url,
                    )
                if redirect_target is None:
                    redirect_target = await request_meta_refresh_target(
                        context,
                        requested_navigation_url,
                        args.timeout_seconds,
                    )
                if redirect_target is None:
                    break
                if redirect_target in visited_redirects:
                    raise PermanentFetchError(
                        "Meta-refresh redirect loop was detected."
                    )
                if redirect_index >= MAX_META_REFRESH_REDIRECTS:
                    raise PermanentFetchError(
                        "Meta-refresh redirect limit was exceeded."
                    )

                visited_redirects.add(redirect_target)
                requested_navigation_url = redirect_target
                status, navigation_response = await navigate(redirect_target)

            try:
                await page.wait_for_selector(
                    PAGE_CONTENT_SELECTOR,
                    timeout=min(
                        args.timeout_seconds,
                        args.render_content_wait_seconds,
                    ) * 1000,
                )
            except PlaywrightTimeoutError:
                pass

            rendered_has_content = (
                await page.locator(PAGE_CONTENT_SELECTOR).count() > 0
            )
            if not rendered_has_content:
                if await rendered_authentication_required():
                    raise AuthenticationExpiredError(
                        "The rendered page redirected to Bosch authentication."
                    )
                raise PageContentNotFoundError(
                    "Rendered page did not expose a supported content container."
                )

            if args.sidebar_network_idle_seconds > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=min(
                            args.timeout_seconds,
                            args.sidebar_network_idle_seconds,
                        ) * 1000,
                    )
                except PlaywrightTimeoutError:
                    pass

            scope_diagnostics = await _mark_selected_sidebar_scope(
                page,
                root_department,
            )
            if not scope_diagnostics["found"]:
                if scope_diagnostics["mode"] == "invalid_root":
                    raise PermanentFetchError(
                        f"Unknown root department: {root_department!r}"
                    )

                # Some valid content pages do not render a department sidebar.
                # Save their content without touching unrelated global navigation.
                content_expansion = await _expand_page_content_sections(page, args)
                diagnostics: dict[str, Any] = {
                    "targets_before": 0,
                    "targets_after": 0,
                    "collapsed_before": 0,
                    "collapsed_remaining": 0,
                    "synthetic_passes": 0,
                    "synthetic_clicks": 0,
                    "native_rounds": 0,
                    "native_clicks": 0,
                    "content_expand_rounds": content_expansion["rounds"],
                    "content_expand_clicks": content_expansion["clicks"],
                    "complete": False,
                    "selected_root_scope_found": False,
                    "selected_root_scope_mode": scope_diagnostics["mode"],
                }
                if args.fail_on_incomplete_sidebar:
                    raise RetryableFetchError(
                        "Selected root sidebar scope was not found: "
                        f"{root_department!r} ({scope_diagnostics['mode']})"
                    )
                return await page.content(), page.url, status, diagnostics

            initial_state = await _sidebar_runtime_state(page)
            waited_for_sidebar = (
                initial_state["targets"] == 0
                and args.sidebar_link_wait_seconds > 0
            )
            if waited_for_sidebar:
                try:
                    await page.wait_for_function(
                        """
                        () => Boolean(document.querySelector(
                          '#subNav a[href], #sidebar a[href], '
                          + '#subNav [data-href], #sidebar [data-href], '
                          + '#subNav [data-url], #sidebar [data-url], '
                          + '#subNav [data-link], #sidebar [data-link], '
                          + '#subNav [data-target-url], #sidebar [data-target-url], '
                          + '#subNav [data-page-url], #sidebar [data-page-url]'
                        ))
                        """,
                        timeout=min(
                            args.timeout_seconds,
                            args.sidebar_link_wait_seconds,
                        ) * 1000,
                    )
                except PlaywrightTimeoutError:
                    pass

            expansion = await _expand_all_sidebar_sections(
                page,
                args,
                initial_state=None if waited_for_sidebar else initial_state,
            )
            native = {"rounds": 0, "clicks": 0}
            if not bool(expansion["complete"]):
                native = await _native_click_sidebar_fallback(page, args)
                if native["clicks"] > 0:
                    second = await _expand_all_sidebar_sections(page, args)
                    expansion["passes"] = int(expansion["passes"]) + int(second["passes"])
                    expansion["clicks"] = int(expansion["clicks"]) + int(second["clicks"])
                    expansion["targets"] = int(second["targets"])
                    expansion["collapsed_remaining"] = int(second["collapsed_remaining"])
                    expansion["complete"] = bool(second["complete"])

            content_expansion = await _expand_page_content_sections(page, args)

            previous_target_count = -1
            unchanged_samples = 0
            stable_final_state: dict[str, int] | None = None
            for _ in range(args.sidebar_stability_checks):
                state = await _sidebar_runtime_state(page)
                current_target_count = state["targets"]
                if current_target_count == previous_target_count:
                    unchanged_samples += 1
                    if unchanged_samples >= args.sidebar_stable_passes:
                        stable_final_state = state
                        break
                else:
                    previous_target_count = current_target_count
                    unchanged_samples = 0
                if args.sidebar_stability_interval_ms > 0:
                    await page.wait_for_timeout(args.sidebar_stability_interval_ms)

            final_state = stable_final_state
            if final_state is None:
                final_state = await _sidebar_runtime_state(page)
            diagnostics: dict[str, Any] = {
                "targets_before": int(expansion.get("targets_before", initial_state["targets"])),
                "targets_after": final_state["targets"],
                "collapsed_before": int(expansion.get("collapsed_before", initial_state["collapsed"])),
                "collapsed_remaining": final_state["collapsed"],
                "synthetic_passes": int(expansion.get("passes", 0)),
                "synthetic_clicks": int(expansion.get("clicks", 0)),
                "native_rounds": native["rounds"],
                "native_clicks": native["clicks"],
                "content_expand_rounds": content_expansion["rounds"],
                "content_expand_clicks": content_expansion["clicks"],
                "complete": final_state["collapsed"] == 0,
                "selected_root_scope_found": scope_diagnostics["found"],
                "selected_root_scope_mode": scope_diagnostics["mode"],
            }

            if args.fail_on_incomplete_sidebar and not diagnostics["complete"]:
                raise RetryableFetchError(
                    "Sidebar expansion incomplete: "
                    f"{diagnostics['collapsed_remaining']} collapsed controls remain"
                )

            return await page.content(), page.url, status, diagnostics
        finally:
            await page.close()


async def fetch_html(
    context: BrowserContext,
    task: CrawlTask,
    args: argparse.Namespace,
    rate_limiter: AsyncRateLimiter,
    render_semaphore: asyncio.Semaphore,
    counters: CrawlCounters,
) -> FetchResult:
    last_error: Exception | None = None

    for attempt in range(args.max_retries + 1):
        try:
            if args.force_render_sidebar:
                await rate_limiter.wait()
                (
                    rendered_html,
                    rendered_url,
                    _rendered_status,
                    expansion_diagnostics,
                ) = await render_page_html(
                    context,
                    task.url,
                    root_department=task.root_department or "",
                    args=args,
                    render_semaphore=render_semaphore,
                )
                rendered_final_url = (
                    canonicalize_url(task.url, rendered_url) or task.url
                )
                counters.rendered_fallbacks += 1
                return FetchResult(
                    html_text=rendered_html,
                    final_url=rendered_final_url,
                    sidebar_complete=bool(
                        expansion_diagnostics.get("complete", True)
                    ),
                )

            await rate_limiter.wait()
            response = await context.request.get(
                task.url,
                timeout=args.timeout_seconds * 1000,
                fail_on_status_code=False,
            )
            try:
                status = response.status
                final_url = canonicalize_url(task.url, response.url) or task.url

                if status in AUTHENTICATION_STATUS_CODES:
                    raise AuthenticationExpiredError(f"HTTP {status}")
                if status in RETRYABLE_STATUS_CODES:
                    raise RetryableFetchError(f"Retryable HTTP {status}")
                if status != 200:
                    raise PermanentFetchError(f"HTTP {status}")

                html_text = await response_text(response)
            finally:
                await response.dispose()

            static_has_content = html_has_page_content(html_text)
            if static_has_content and (
                not args.render_fallback or args.min_sidebar_links <= 0
            ):
                return FetchResult(
                    html_text=html_text,
                    final_url=final_url,
                    sidebar_complete=True,
                )
            if not args.render_fallback:
                raise AuthenticationExpiredError(
                    "Static response does not contain authenticated page content."
                )

            static_sidebar_count = count_sidebar_navigation_targets(
                html_text,
                final_url,
                is_start_page=task.depth == 0,
                root_department=task.root_department,
            )
            if (
                static_has_content
                and static_sidebar_count >= args.min_sidebar_links
            ):
                return FetchResult(
                    html_text=html_text,
                    final_url=final_url,
                    sidebar_complete=True,
                )

            # Render when the static response omits the dynamically populated
            # sidebar, even if #pageContent is already present.
            await rate_limiter.wait()
            (
                rendered_html,
                rendered_url,
                _rendered_status,
                expansion_diagnostics,
            ) = await render_page_html(
                context,
                final_url,
                root_department=task.root_department or "",
                args=args,
                render_semaphore=render_semaphore,
            )
            rendered_final_url = canonicalize_url(final_url, rendered_url) or final_url
            counters.rendered_fallbacks += 1
            return FetchResult(
                html_text=rendered_html,
                final_url=rendered_final_url,
                sidebar_complete=bool(
                    expansion_diagnostics.get("complete", True)
                ),
            )

        except AuthenticationExpiredError:
            raise
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


def load_unresolved_error_records(
    error_path: Path,
    start_url: str,
    completed_urls: set[tuple[str, str]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    """Load only the latest error for pages without a durable success record."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    stale_completed_records = 0
    for record in load_jsonl(error_path):
        root_department = clean_text(record.get("root_department", ""))
        root = target_root_definition(root_department)
        if root is None:
            continue
        url = canonicalize_url(start_url, str(record.get("url", "")))
        if not url:
            continue
        key = selected_root_page_key(url, root["label"])
        if key in completed_urls:
            stale_completed_records += 1
            continue
        latest[key] = record
    return latest, stale_completed_records


def replace_jsonl_records_atomically(
    path: Path,
    records: Iterable[dict[str, Any]],
) -> None:
    """Atomically compact a JSONL state file, removing it when empty."""
    compacted_records = list(records)
    if not compacted_records:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for record in compacted_records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}.")
    return value


def validate_resume_summary(summary: dict[str, Any]) -> None:
    schema_version = summary.get("output_schema_version")
    if schema_version != OUTPUT_SCHEMA_VERSION:
        raise RuntimeError(
            "Resume output schema does not match this crawler version: "
            f"found {schema_version!r}, expected {OUTPUT_SCHEMA_VERSION}."
        )

    expected_roots = [
        {"label": item["label"], "url": item["url"], "fsid": item["fsid"]}
        for item in TARGET_ROOTS
    ]
    if summary.get("selected_roots") != expected_roots:
        raise RuntimeError(
            "Resume output was created for a different set of selected roots. "
            "Use a new output directory."
        )


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


async def drain_queue_with_workers(
    queue: asyncio.Queue[Any],
    workers: list[asyncio.Task[None]],
) -> None:
    """Drain a dynamic queue without hanging when a worker terminates early."""
    queue_join = asyncio.create_task(queue.join())
    try:
        done, _pending = await asyncio.wait(
            (queue_join, *workers),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if queue_join not in done:
            stopped_workers = [task for task in done if task in workers]
            for stopped_worker in stopped_workers:
                if stopped_worker.cancelled():
                    continue
                failure = stopped_worker.exception()
                if failure is not None:
                    raise failure
            raise RuntimeError(
                "Crawler worker stopped before the queue was drained."
            )

        await queue_join
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)
    finally:
        if not queue_join.done():
            queue_join.cancel()
        for worker_task in workers:
            if not worker_task.done():
                worker_task.cancel()
        await asyncio.gather(
            queue_join,
            *workers,
            return_exceptions=True,
        )


async def crawl(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    for directory in (output_dir, profile_dir):
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    if args.cleanup_obsolete_outputs:
        for relative_path in OBSOLETE_OUTPUT_PATHS:
            obsolete = output_dir / relative_path
            if obsolete.is_file():
                obsolete.unlink()
        shutil.rmtree(output_dir / "files", ignore_errors=True)

    if not args.resume:
        for filename in (*OUTPUT_FILES.values(), "summary.json"):
            path = output_dir / filename
            if path.exists() and path.is_file():
                path.unlink()
        shutil.rmtree(output_dir / ".state", ignore_errors=True)

    start_url = canonicalize_url(args.start_url, args.start_url)
    if not start_url:
        raise ValueError("Invalid start URL.")

    print(f"Crawler version: {CRAWLER_VERSION}")
    print(f"Output directory: {output_dir}")
    print(
        "Workers: "
        f"page={args.concurrency}, "
        f"render={args.render_concurrency}, "
        f"parse={args.parse_workers}"
    )

    counters = CrawlCounters()
    previous_page_count = 0
    previous_max_depth = 0
    previous_sidebar_incomplete_pages = 0
    previous_root_counts = {root["label"]: 0 for root in TARGET_ROOTS}

    known_urls: set[tuple[str, str]] = set()
    latest_error_records: dict[tuple[str, str], dict[str, Any]] = {}
    seen_link_keys: set[tuple[str, str]] = set()

    queue: asyncio.Queue[CrawlTask | None] = asyncio.Queue()
    authentication_lock = asyncio.Lock()

    if args.resume:
        previous_summary = load_json_object(output_dir / "summary.json")
        if previous_summary is not None:
            validate_resume_summary(previous_summary)

        pages_path = output_dir / OUTPUT_FILES["page"]
        for record in load_jsonl(pages_path):
            if "requested_url" not in record or "sidebar_complete" not in record:
                raise RuntimeError(
                    "Resume pages do not match output schema version 5. "
                    "Use a new output directory."
                )

            root_department = clean_text(record.get("root_department", ""))
            root = target_root_definition(root_department)
            if root is None:
                raise RuntimeError(
                    "Resume output contains a page outside the six selected roots. "
                    "Use a new output directory."
                )

            for url_field in ("url", "requested_url"):
                url = canonicalize_url(
                    start_url,
                    str(record.get(url_field, "")),
                )
                if url:
                    key = selected_root_page_key(url, root["label"])
                    known_urls.add(key)

            path = normalize_navigation_path(record.get("department_path", []))
            previous_max_depth = max(previous_max_depth, max(0, len(path) - 1))
            previous_page_count += 1
            previous_root_counts[root["label"]] += 1
            if record.get("sidebar_complete") is False:
                previous_sidebar_incomplete_pages += 1

        error_path = output_dir / OUTPUT_FILES["error"]
        (
            latest_error_records,
            stale_completed_error_records,
        ) = load_unresolved_error_records(
            error_path,
            start_url,
            known_urls,
        )
        replace_jsonl_records_atomically(
            error_path,
            latest_error_records.values(),
        )
        if stale_completed_error_records and args.verbose:
            print(
                "Resume state reconciled: removed "
                f"{stale_completed_error_records} stale page error record(s) "
                "already present in pages.jsonl."
            )

    writer = StreamingJSONLWriter(
        output_dir,
        append=args.resume,
        batch_size=args.write_batch_size,
    )
    await writer.start()

    async def enqueue(task: CrawlTask, *, persist: bool = True) -> str:
        if not task_is_in_selected_scope(task):
            return "outside_selected_roots"

        canonical = canonicalize_url(task.parent_url or task.url, task.url)
        if not canonical:
            return "invalid_url"

        root = target_root_definition(task.root_department)
        assert root is not None
        normalized = CrawlTask(
            url=canonical,
            parent_url=task.parent_url,
            path=normalize_navigation_path(task.path),
            depth=task.depth,
            source=task.source,
            link_text=task.link_text,
            root_department=root["label"],
        )
        context_key = selected_root_page_key(canonical, root["label"])

        # No await occurs in this state transition, so it is atomic within the
        # asyncio event loop and does not need an asynchronous lock.
        if context_key in known_urls:
            return "already_seen"
        if (
            context_key in latest_error_records
            and args.resume
            and not args.retry_failures
        ):
            known_urls.add(context_key)
            return "failed_skipped"
        total_known_pages = previous_page_count + counters.enqueued
        if page_limit_reached(total_known_pages, args.max_pages):
            return "page_limit"
        known_urls.add(context_key)
        counters.enqueued += 1

        # Frontier records are flushed before the task is put on the queue.
        # Therefore every discovered child survives an unexpected interruption.
        if persist:
            await writer.write("frontier", asdict(normalized))
        await queue.put(normalized)
        return "accepted"

    if args.resume:
        pending: dict[tuple[str, str], CrawlTask] = {}
        frontier_path = output_dir / OUTPUT_FILES["frontier"]
        for record in load_jsonl(frontier_path):
            task = task_from_record(record)
            if task is None or not task_is_in_selected_scope(task):
                continue
            canonical = canonicalize_url(task.parent_url or task.url, task.url)
            if not canonical:
                continue
            key = selected_root_page_key(canonical, task.root_department)
            if key in known_urls:
                continue
            if key in latest_error_records and not args.retry_failures:
                continue
            pending.setdefault(key, task)

        for task in pending.values():
            await enqueue(task, persist=False)

    # Always reconcile all roots after loading a partial frontier. A process
    # can be interrupted while the initial roots are still being persisted;
    # seeding only an empty queue would permanently omit the remaining roots.
    for root in TARGET_ROOTS:
        status = await enqueue(
            CrawlTask(
                url=root["url"],
                parent_url=None,
                path=(root["label"],),
                depth=0,
                source="selected_root_seed",
                link_text=root["label"],
                root_department=root["label"],
            )
        )
        if status == "page_limit":
            await writer.close()
            raise ValueError(
                "SPIDER_MAX_PAGES is too small to include all six selected "
                "organization roots. Increase it or set it to 0 (unlimited)."
            )
        if status not in {"accepted", "already_seen", "failed_skipped"}:
            raise RuntimeError(
                f"Unable to enqueue selected root {root['label']!r}: {status}"
            )

    page_rate_limiter = AsyncRateLimiter(args.requests_per_second)
    render_semaphore = asyncio.Semaphore(max(1, args.render_concurrency))
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=max(1, args.parse_workers),
        thread_name_prefix="bgn-html-parser",
    )
    loop.set_default_executor(executor)

    started_at = time.monotonic()
    current_root_counts = {root["label"]: 0 for root in TARGET_ROOTS}

    try:
        async with async_playwright() as playwright:
            context = await launch_context(
                playwright,
                profile_dir,
                args.headless,
                args.browser_channel,
                args.browser_fallback_channels,
                args.browser_executable_path,
                args.browser_args,
                args.ignore_https_errors,
                False,
            )
            try:
                await ensure_authenticated(
                    context,
                    start_url,
                    args.headless,
                    args.timeout_seconds,
                    args.authentication_wait_seconds,
                )

                async def fetch_with_auth_recovery(task: CrawlTask) -> FetchResult:
                    authentication_confirmed = False
                    for authentication_attempt in range(2):
                        try:
                            return await fetch_html(
                                context,
                                task,
                                args,
                                page_rate_limiter,
                                render_semaphore,
                                counters,
                            )
                        except AuthenticationExpiredError as exc:
                            if authentication_attempt > 0:
                                if authentication_confirmed:
                                    raise PermanentFetchError(
                                        "The authenticated session is valid, but "
                                        f"this page remains inaccessible ({exc})."
                                    ) from exc
                                raise
                            async with authentication_lock:
                                login_completed = await ensure_authenticated(
                                    context,
                                    start_url,
                                    args.headless,
                                    args.timeout_seconds,
                                    args.authentication_wait_seconds,
                                )
                                authentication_confirmed = True
                                if login_completed:
                                    counters.authentication_recoveries += 1

                    raise RuntimeError("Authentication recovery loop exhausted.")

                async def worker() -> None:
                    while True:
                        task = await queue.get()
                        if task is None:
                            queue.task_done()
                            return

                        try:
                            fetch_result = await fetch_with_auth_recovery(task)
                            html_text = fetch_result.html_text
                            final_url = fetch_result.final_url

                            page_record, links = await asyncio.to_thread(
                                extract_page,
                                html_text,
                                task,
                                final_url,
                                args.preserve_hidden_content,
                            )

                            # Persist every discovered child before writing the
                            # parent page as completed. This ordering prevents a
                            # crash from losing an unpersisted subtree.
                            for link in links:
                                absolute_path = merge_navigation_path(
                                    task.path,
                                    link.navigation_path,
                                    link.text,
                                )
                                navigation_key = sidebar_link_key(
                                    link.url,
                                    task.root_department,
                                )

                                seen_link_keys.add(navigation_key)

                                if (
                                    not link.crawlable
                                    or not depth_allows_children(
                                        task.depth,
                                        args.max_depth,
                                    )
                                ):
                                    continue

                                await enqueue(
                                    CrawlTask(
                                        url=link.url,
                                        parent_url=final_url,
                                        path=absolute_path,
                                        depth=task.depth + 1,
                                        source=link.source,
                                        link_text=link.text,
                                        root_department=task.root_department,
                                    )
                                )

                            sidebar_complete = fetch_result.sidebar_complete
                            compact_page = {
                                "url": final_url,
                                "requested_url": task.url,
                                "title": page_record["title"],
                                "root_department": task.root_department,
                                "department_path": page_record["department_path"],
                                "language": page_record["language"],
                                "content": page_record["content"],
                                "sidebar_complete": sidebar_complete,
                            }
                            await writer.write("page", compact_page)

                            context_key = selected_root_page_key(
                                final_url,
                                task.root_department,
                            )
                            original_key = selected_root_page_key(
                                task.url,
                                task.root_department,
                            )
                            recovered_from_failure = (
                                context_key in latest_error_records
                                or original_key in latest_error_records
                            )
                            counters.completed += 1
                            counters.max_depth_seen = max(
                                counters.max_depth_seen,
                                task.depth,
                            )
                            known_urls.add(context_key)
                            known_urls.add(original_key)
                            latest_error_records.pop(context_key, None)
                            latest_error_records.pop(original_key, None)
                            current_root_counts[task.root_department] += 1
                            if sidebar_complete:
                                counters.sidebar_complete_pages += 1
                            else:
                                counters.sidebar_incomplete_pages += 1

                            if args.verbose and recovered_from_failure:
                                destination = (
                                    f" -> {final_url}"
                                    if final_url != task.url
                                    else ""
                                )
                                print(
                                    "Page recovered after previous failure: "
                                    f"{task.url}{destination}"
                                )

                            if (
                                args.verbose
                                and counters.completed % args.progress_every == 0
                            ):
                                elapsed = max(0.001, time.monotonic() - started_at)
                                print(
                                    f"pages={counters.completed} "
                                    f"pending={queue.qsize()} "
                                    f"links={len(seen_link_keys)} "
                                    f"rendered={counters.rendered_fallbacks} "
                                    f"errors={len(latest_error_records)} "
                                    f"rate={counters.completed / elapsed:.2f}/s"
                                )

                        except AuthenticationExpiredError:
                            # Authentication is shared by the whole browser
                            # context. Continuing would fail every queued page.
                            raise
                        except Exception as exc:  # noqa: BLE001
                            key = selected_root_page_key(
                                task.url,
                                task.root_department,
                            )
                            error_record = {
                                "url": task.url,
                                "parent_url": task.parent_url,
                                "depth": task.depth,
                                "root_department": task.root_department,
                                "path": list(task.path),
                                "source": task.source,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            counters.failed += 1
                            latest_error_records[key] = error_record
                            await writer.write("error", error_record)
                            if args.verbose:
                                failure_label = (
                                    "Page pending retry"
                                    if isinstance(exc, RetryableFetchError)
                                    else "Page failed"
                                )
                                print(
                                    f"{failure_label}: {task.url}: {exc}",
                                    file=sys.stderr,
                                )
                        finally:
                            queue.task_done()

                workers = [
                    asyncio.create_task(worker())
                    for _ in range(args.concurrency)
                ]
                await drain_queue_with_workers(queue, workers)
            finally:
                await context.close()
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        await writer.close()

    # Compact the hidden error state so successful retries remove stale entries.
    error_path = output_dir / OUTPUT_FILES["error"]
    replace_jsonl_records_atomically(
        error_path,
        latest_error_records.values(),
    )

    elapsed = max(0.001, time.monotonic() - started_at)
    total_root_counts = {
        root["label"]: previous_root_counts[root["label"]]
        + current_root_counts[root["label"]]
        for root in TARGET_ROOTS
    }
    total_sidebar_incomplete_pages = (
        previous_sidebar_incomplete_pages + counters.sidebar_incomplete_pages
    )
    if latest_error_records:
        completion_status = "completed_with_errors"
    elif total_sidebar_incomplete_pages:
        completion_status = "completed_with_incomplete_sidebars"
    else:
        completion_status = "completed"

    summary = {
        "crawler_version": CRAWLER_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "scope": "six_selected_organization_roots_only",
        "status": completion_status,
        "pages": previous_page_count + counters.completed,
        "pages_by_root": total_root_counts,
        "failed_pages": len(latest_error_records),
        "max_depth": max(previous_max_depth, counters.max_depth_seen),
        "sidebar_incomplete_pages": total_sidebar_incomplete_pages,
        "elapsed_seconds": round(elapsed, 3),
        "current_run": {
            "pages_completed": counters.completed,
            "pages_failed": counters.failed,
            "rendered_pages": counters.rendered_fallbacks,
            "retried_requests": counters.retried_requests,
            "authentication_recoveries": counters.authentication_recoveries,
            "sidebar_complete_pages": counters.sidebar_complete_pages,
            "sidebar_incomplete_pages": counters.sidebar_incomplete_pages,
            "unique_sidebar_links": len(seen_link_keys),
        },
        "selected_roots": [
            {"label": item["label"], "url": item["url"], "fsid": item["fsid"]}
            for item in TARGET_ROOTS
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_tmp = summary_path.with_suffix(".json.tmp")
    summary_tmp.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary_tmp.replace(summary_path)

    # A clean completion needs no resume files. Keep state after interruption,
    # remaining failures, or when explicitly requested.
    if not latest_error_records and not args.keep_state:
        state_dir = output_dir / ".state"
        shutil.rmtree(state_dir, ignore_errors=True)

    print(
        "Crawl complete: "
        f"pages={summary['pages']}, "
        f"failed={summary['failed_pages']}, "
        f"sidebar_incomplete={summary['sidebar_incomplete_pages']}"
    )
    if args.fail_on_page_errors and summary["failed_pages"]:
        raise RuntimeError(
            "Crawl completed with retryable page failures: "
            f"{summary['failed_pages']}. Resume state was preserved."
        )


def resolve_worker_configuration(args: argparse.Namespace) -> None:
    """Resolve a stable page-only worker configuration."""
    cpu_count = max(2, os.cpu_count() or 4)
    profiles = {
        "safe": {
            "concurrency": min(6, max(4, cpu_count)),
            "parse_workers": min(4, cpu_count),
            "render_concurrency": 2,
        },
        "balanced": {
            "concurrency": min(12, max(8, cpu_count * 2)),
            "parse_workers": min(8, max(4, cpu_count)),
            "render_concurrency": 4,
        },
        "fast": {
            "concurrency": min(20, max(12, cpu_count * 3)),
            "parse_workers": min(12, max(6, cpu_count)),
            "render_concurrency": 6,
        },
    }
    if args.worker_profile == "auto":
        args.worker_profile = "balanced" if args.headless else "fast"
    if args.worker_profile not in profiles:
        raise SystemExit("worker_profile must be auto, safe, balanced or fast.")
    selected = profiles[args.worker_profile]
    if args.concurrency is None:
        args.concurrency = selected["concurrency"]
    if args.parse_workers is None:
        args.parse_workers = selected["parse_workers"]
    if args.render_concurrency is None:
        args.render_concurrency = selected["render_concurrency"]

    args.concurrency = min(
        args.concurrency,
        max(4, args.render_concurrency * 3),
    )


def validate_args(args: argparse.Namespace) -> None:
    path_problems = runtime_path_problems(
        Path(args.output_dir),
        Path(args.profile_dir),
    )
    if path_problems:
        raise SystemExit("\n".join(path_problems))
    resolve_worker_configuration(args)

    if args.expand_all_sidebar or args.expand_page_content:
        args.force_render_sidebar = True
        args.render_fallback = True

    if args.fast_mode:
        args.render_concurrency = max(args.render_concurrency, 4)
        args.concurrency = min(
            max(args.concurrency, args.render_concurrency * 2),
            args.render_concurrency * 3,
        )
        args.sidebar_network_idle_seconds = 0
        args.render_content_wait_seconds = min(args.render_content_wait_seconds, 6)
        args.sidebar_link_wait_seconds = min(args.sidebar_link_wait_seconds, 1)
        args.sidebar_stability_interval_ms = min(
            args.sidebar_stability_interval_ms,
            100,
        )
        args.block_nonessential_resources = True

    integer_minimums = {
        "max_depth": 0,
        "max_pages": 0,
        "concurrency": 1,
        "parse_workers": 1,
        "render_concurrency": 1,
        "min_sidebar_links": 0,
        "sidebar_expand_rounds": 0,
        "sidebar_click_batch_size": 1,
        "sidebar_max_clicks": 1,
        "sidebar_control_max_attempts": 1,
        "sidebar_stable_passes": 1,
        "sidebar_settle_ms": 0,
        "sidebar_network_idle_seconds": 0,
        "render_content_wait_seconds": 1,
        "sidebar_link_wait_seconds": 0,
        "sidebar_stability_checks": 0,
        "sidebar_stability_interval_ms": 0,
        "sidebar_native_click_rounds": 0,
        "sidebar_native_max_clicks": 0,
        "content_expand_rounds": 0,
        "content_expand_max_clicks": 0,
        "timeout_seconds": 1,
        "authentication_wait_seconds": 30,
        "max_retries": 0,
        "write_batch_size": 1,
        "progress_every": 1,
    }
    for name, minimum in integer_minimums.items():
        value = int(getattr(args, name))
        if value < minimum:
            raise SystemExit(f"{name} must be at least {minimum}.")

    if (
        not math.isfinite(args.sidebar_expansion_timeout_seconds)
        or args.sidebar_expansion_timeout_seconds <= 0
    ):
        raise SystemExit(
            "sidebar_expansion_timeout_seconds must be finite and positive."
        )

    for name in ("requests_per_second", "retry_backoff_seconds"):
        value = float(getattr(args, name))
        if not math.isfinite(value) or value < 0:
            raise SystemExit(f"{name} must be finite and non-negative.")


async def authenticate_profile(args: argparse.Namespace) -> None:
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(profile_dir, 0o700)
    except OSError:
        pass
    start_url = canonicalize_url(args.start_url, args.start_url)
    if not start_url:
        raise ValueError("Invalid start URL.")

    async with async_playwright() as playwright:
        context = await launch_context(
            playwright,
            profile_dir,
            args.headless,
            args.browser_channel,
            args.browser_fallback_channels,
            args.browser_executable_path,
            args.browser_args,
            args.ignore_https_errors,
            False,
        )
        try:
            await ensure_authenticated(
                context,
                start_url,
                args.headless,
                args.timeout_seconds,
                args.authentication_wait_seconds,
            )
        finally:
            await context.close()
    print(f"Authenticated browser profile is ready: {profile_dir}")


async def check_browser_runtime(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="bosch-spider-browser-check-") as path:
        profile_dir = Path(path)
        async with async_playwright() as playwright:
            context = await launch_context(
                playwright,
                profile_dir,
                args.headless,
                args.browser_channel,
                args.browser_fallback_channels,
                args.browser_executable_path,
                args.browser_args,
                args.ignore_https_errors,
                False,
            )
            try:
                page = await context.new_page()
                await page.goto("about:blank", wait_until="domcontentloaded")
                await page.close()
            finally:
                await context.close()
    print(
        "Browser check passed: "
        f"mode={'headless' if args.headless else 'headed'}, "
        f"channel={args.browser_channel or 'bundled'}"
    )



def main() -> None:
    configure_console_utf8()
    args = build_args_from_code_config()
    validate_args(args)
    print("运行参数已从环境变量和 CRAWLER_CONFIG 读取。")
    if args.browser_check_only:
        asyncio.run(check_browser_runtime(args))
        return
    if args.auth_only:
        asyncio.run(authenticate_profile(args))
        return
    asyncio.run(crawl(args))


if __name__ == "__main__":
    main()
