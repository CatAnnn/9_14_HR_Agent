from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from project_config import (
    CRAWLER_OUTPUT_SCHEMA_VERSION,
    CRAWL_OUTPUT_DIR,
    EXCEL_OUTPUT_FILE,
    PAGES_FILE,
    PROJECT_DIR,
    TARGET_ORGANIZATIONS as SHARED_TARGET_ORGANIZATIONS,
    TARGET_ROOTS,
    configure_console_utf8,
)

# ============================================================
# 参数配置区：只修改这里
# ============================================================

SCRIPT_DIR = PROJECT_DIR

# 输入文件：相对路径以脚本所在目录为基准。
INPUT_PAGES_FILE = PAGES_FILE

# True：忽略 INPUT_PAGES_FILE，在项目和爬虫输出目录中寻找最新文件。
AUTO_FIND_LATEST_PAGES = False

# 输出文件。
OUTPUT_EXCEL_FILE = EXCEL_OUTPUT_FILE
OUTPUT_SHEET_NAME = "Pages"

# 目标一级组织。
TARGET_ORGANIZATIONS = list(SHARED_TARGET_ORGANIZATIONS)

# 可选过滤；空值表示不过滤。
ALLOWED_LANGUAGES: list[str] = []
ALLOWED_HTTP_STATUS: list[int] = []
ALLOWED_FETCH_MODES: set[str] = set()
MIN_DEPTH: int | None = None
MAX_DEPTH: int | None = None
ONLY_WITH_CONTENT = True
MIN_CONTENT_LENGTH = 0
ONLY_COMPLETE_SIDEBARS = False

# URL 去重。
DEDUPLICATE_BY_URL = True
DUPLICATE_KEEP = "best"  # best / first / last
# organization：同一组织内按 URL 去重；global：跨组织按 URL 去重。
URL_DEDUPLICATION_SCOPE = "global"

# 遇到损坏 JSON 行。
STRICT_JSON = False

# ---------------- 部门拆分参数 ----------------

# Organization 已单独输出，因此这里表示根组织以下的部门层级数量。
DEPARTMENT_LEVEL_COUNT = 3

# 如果未来数据超过 3 层，是否将剩余层级合并到最后一列。
MERGE_DEPARTMENT_OVERFLOW_TO_LAST = True

# 保留完整 Department Path，便于 RAG 进行层级检索与引用。
KEEP_FULL_DEPARTMENT_PATH = True

# False（推荐）：严格按原始 department_path 拆分，路径末级也保留；
# True：若路径末级明显等于页面标题，则不把该末级当作部门层级。
REMOVE_PAGE_LEAF_FROM_DEPARTMENT_LEVELS = False

# ---------------- 页面主题参数 ----------------

# concise：仅输出精简主题名称；
# descriptive：输出“主题名称 — 主题类别”，更适合 RAG 检索。
PAGE_TOPIC_MODE = "descriptive"

# 是否允许使用正文前部辅助判断主题类别。
TOPIC_USE_CONTENT = True

# 用于主题分类的最大正文字符数；0 表示使用完整正文。
TOPIC_CONTENT_SCAN_LENGTH = 0

# Page Topic 最大字符数。
PAGE_TOPIC_MAX_LENGTH = 180

# ---------------- 输出字段 ----------------

ESCAPE_EXCEL_FORMULAS = True

DEPARTMENT_HEADERS = [
    f"Department Level {index}"
    for index in range(1, DEPARTMENT_LEVEL_COUNT + 1)
]

def build_output_headers() -> list[str]:
    headers = [
        "Organization",
        "Discovered In Organizations",
        "Ownership Confidence",
        *DEPARTMENT_HEADERS,
        "Page Topic",
        "Title",
    ]
    if KEEP_FULL_DEPARTMENT_PATH:
        headers.append("Department Path")
    headers.append("Content")
    return headers


OUTPUT_HEADERS = build_output_headers()

# ---------------- Excel 样式 ----------------

HEADER_FILL_COLOR = "1F4E78"
HEADER_FONT_COLOR = "FFFFFF"
HEADER_FONT_SIZE = 11
HEADER_ROW_HEIGHT = 30
BODY_FONT_SIZE = 11
BODY_ROW_HEIGHT = 42

COLUMN_WIDTHS = {
    "Organization": 38,
    "Discovered In Organizations": 64,
    "Ownership Confidence": 26,
    "Department Level 1": 24,
    "Department Level 2": 28,
    "Department Level 3": 34,
    "Page Topic": 54,
    "Title": 34,
    "Department Path": 56,
    "Content": 90,
}

EXCEL_CELL_MAX_LENGTH = 32767
EXCEL_ILLEGAL_CHARACTERS_RE = re.compile(
    r"[\x00-\x08\x0B-\x0C\x0E-\x1F]"
)
TRUNCATION_MARKER = (
    "\n...[超过 Excel 单元格上限，已截断；完整内容保留在原始 JSONL]"
)


# ============================================================
# 以下逻辑一般不需要修改
# ============================================================


@dataclass
class Result:
    rows: list[dict[str, Any]]
    total: int
    valid: int
    invalid: int
    blank: int
    excluded: int
    duplicates: int
    incomplete_sidebars: int
    cleaned_toolbars: int
    inferred_owner_departments: int


@dataclass(frozen=True)
class ContentMetadata:
    content: str
    last_changed: str = ""
    page_owner_department: str = ""
    toolbar_removed: bool = False


def config_path(path: Path) -> Path:
    path = path.expanduser()
    if path.is_absolute():
        return path.resolve()
    return (SCRIPT_DIR / path).resolve()


def normalize_inline(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


EDITORIAL_TOOLBAR_SIGNATURES: dict[str, tuple[str | None, ...]] = {
    "editorial tools": (
        "editorial tools",
        "explicit permissions necessary",
        "show prep areas",
        "hide prep areas",
        "generate report",
        "wcms help",
        "sitearchitect",
        "edit page",
        None,
        "copy short id",
        None,
        "recommend page",
        None,
    ),
    "redaktionswerkzeuge": (
        "redaktionswerkzeuge",
        "zugriffsrechte sind erforderlich",
        "prep areas einblenden",
        "prep areas ausblenden",
        "report generieren",
        "wcms hilfe",
        "sitearchitect",
        "seite bearbeiten",
        None,
        "kurz-id kopieren",
        None,
        "seite empfehlen",
        None,
    ),
}
LAST_CHANGED_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
PERMALINK_PREFIX = "https://bgn.bosch.com/firstspiritweb/permlink/"


def _normalized_content_lines(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value).replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    return [line.rstrip() for line in text.splitlines()]


def parse_content_metadata(value: Any) -> ContentMetadata:
    """Remove only a fully recognized WCMS editorial suffix.

    The footer is validated by a complete sequence of independent markers near
    the end of the page. A normal body sentence containing "Editorial tools"
    is therefore never removed. The source JSONL remains unchanged.
    """
    lines = _normalized_content_lines(value)
    meaningful = [
        (line_index, line.strip())
        for line_index, line in enumerate(lines)
        if line.strip()
    ]

    for position in range(len(meaningful) - 1, -1, -1):
        marker = meaningful[position][1].casefold()
        signature = EDITORIAL_TOOLBAR_SIGNATURES.get(marker)
        if signature is None:
            continue
        if position < len(meaningful) - 20:
            continue
        if position + len(signature) > len(meaningful):
            continue

        candidate = meaningful[position : position + len(signature)]
        candidate_values = [item[1].casefold() for item in candidate]
        if any(
            expected is not None and candidate_values[offset] != expected
            for offset, expected in enumerate(signature)
        ):
            continue
        if not candidate_values[10].startswith(PERMALINK_PREFIX):
            continue

        owner_line = candidate[12][1]
        if "," not in owner_line:
            continue
        owner_department = normalize_inline(owner_line.rsplit(",", 1)[1])
        if not owner_department:
            continue

        toolbar_line_index = meaningful[position][0]
        content_end = toolbar_line_index
        last_changed = ""
        if position > 0:
            date_line_index, possible_date = meaningful[position - 1]
            if (
                len(possible_date) <= 64
                and LAST_CHANGED_YEAR_RE.search(possible_date)
            ):
                last_changed = normalize_inline(possible_date)
                content_end = date_line_index

        content = "\n".join(lines[:content_end]).strip()
        return ContentMetadata(
            content=content,
            last_changed=last_changed,
            page_owner_department=owner_department,
            toolbar_removed=True,
        )

    return ContentMetadata(content="\n".join(lines).strip())


def normalize_content(value: Any) -> str:
    return parse_content_metadata(value).content


def prepare_record_for_export(
    record: dict[str, Any],
) -> tuple[dict[str, Any], ContentMetadata]:
    metadata = parse_content_metadata(record.get("content"))
    prepared = dict(record)
    prepared["content"] = metadata.content
    if not normalize_inline(prepared.get("last_changed")):
        prepared["last_changed"] = metadata.last_changed
    if not normalize_inline(prepared.get("page_owner_department")):
        prepared["page_owner_department"] = metadata.page_owner_department
    prepared["_export_content_prepared"] = True
    return prepared, metadata


def record_content(record: dict[str, Any]) -> str:
    if record.get("_export_content_prepared") is True:
        return str(record.get("content") or "")
    return normalize_content(record.get("content"))


def normalize_for_compare(value: Any) -> str:
    """
    用于比较标题和路径末级：
    - & 统一为 and；
    - 去掉 Welcome to / About 等前缀；
    - 去掉常见符号；
    - 忽略大小写与多余空格。
    """
    text = normalize_inline(value).casefold()
    text = text.replace("&", " and ")
    text = re.sub(
        r"^\s*(welcome\s+to|about|overview\s+of|home\s+of)\s+",
        "",
        text,
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_wcms_recommendation_query(query: str) -> bool:
    """Recognize only the exact WCMS recommendation-link query shape."""
    pairs = parse_qsl(query, keep_blank_values=True)
    if len(pairs) != 2:
        return False
    values = {name.casefold(): value for name, value in pairs}
    return (
        set(values) == {"cmcall", "perm_link"}
        and values["cmcall"].casefold() == "true"
        and bool(values["perm_link"])
    )


def page_identity_url(value: Any) -> str:
    """Build a conservative page identity without rewriting the display URL.

    URL paths and arbitrary business query parameters remain byte-for-byte
    distinct. Only the known WCMS recommendation alias is folded into the
    query-free landing URL.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    query = "" if is_wcms_recommendation_query(parsed.query) else parsed.query
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            query,
            "",
        )
    )


def is_wcms_recommendation_url(value: Any) -> bool:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return False
    try:
        return is_wcms_recommendation_query(urlsplit(raw).query)
    except ValueError:
        return False


ORGANIZATION_URL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Corporate Headquarters and Service Areas": (
        re.compile(r"/wcms/wcms_corpfunc/"),
        re.compile(r"/(?:02-organization/)?corporate-functions/"),
    ),
    "Mobility (BBM)": (
        re.compile(r"/wcms/wcms_(?:m|eb)/"),
        re.compile(
            r"/(?:mobility-bbm|organization-bbm|"
            r"automotive-technology-ubk)(?:/|$)"
        ),
    ),
    "Industrial Technology (BBI)": (
        re.compile(r"/wcms/wcms_dc/"),
        re.compile(r"/(?:industrial-technology-bbi|ubi)(?:/|$)"),
    ),
    "Consumer Goods (BBG)": (
        re.compile(r"/wcms/wcms_pt/"),
        re.compile(r"/(?:consumer-goods-bbg|ubg)(?:/|$)"),
    ),
    "Energy and Building Technology (BBE)": (
        re.compile(r"/wcms/wcms_st/"),
        re.compile(
            r"/(?:energy-and-building-technology-bbe|ube)(?:/|$)"
        ),
    ),
    "Value Accelerator & Portfolio Companies (VP)": (
        re.compile(r"/organization/vp(?:/|$)"),
    ),
}

ROOT_URL_PREFIXES = tuple(
    (
        item["label"],
        urlsplit(item["url"]).path.casefold().rsplit("/", 1)[0] + "/",
    )
    for item in TARGET_ROOTS
)


def infer_organization_from_url(value: Any) -> str:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return ""
    try:
        path = urlsplit(raw).path.casefold()
    except ValueError:
        return ""

    matches: set[str] = {
        organization
        for organization, prefix in ROOT_URL_PREFIXES
        if path.startswith(prefix)
    }
    for organization, patterns in ORGANIZATION_URL_PATTERNS.items():
        if organization not in TARGET_ORGANIZATIONS:
            continue
        if any(pattern.search(path) for pattern in patterns):
            matches.add(organization)
    return next(iter(matches)) if len(matches) == 1 else ""


def department_path_parts(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_parts = value
    else:
        text = normalize_inline(value)
        if not text:
            return []
        # 兼容已经被拼接成字符串的路径。
        raw_parts = re.split(r"\s*>\s*", text)

    parts: list[str] = []
    for item in raw_parts:
        normalized = normalize_inline(item)
        if normalized:
            parts.append(normalized)
    return parts


def is_code_like(value: str) -> bool:
    """
    判断路径末级是否更像部门代码，而不是页面主题。
    例如：C/FII、CR/RTC-AP、DSM/BDP、C/FIO1.1-AP。
    """
    text = normalize_inline(value)
    if not text:
        return False

    if re.fullmatch(
        r"[A-Z0-9]+(?:[/.-][A-Z0-9]+)+(?:-[A-Z0-9]+)*",
        text,
        flags=re.IGNORECASE,
    ):
        return True

    # 较短且同时包含斜杠、数字或多个大写代码片段。
    return (
        len(text) <= 24
        and bool(re.search(r"[/]", text))
        and bool(re.search(r"[A-Z]", text))
    )


def is_structural_label(value: str) -> bool:
    text = normalize_for_compare(value)
    structural = {
        "organization",
        "organisation",
        "topics",
        "our topics",
        "locations",
        "products and services",
        "products services",
        "guidelines and standards",
        "guidelines standards",
        "information services",
        "business support functions",
        "consulting services",
        "board management",
    }
    return text in structural


def is_generic_title(value: str) -> bool:
    text = normalize_for_compare(value)
    if not text:
        return True

    generic_exact = {
        "organization",
        "organisation",
        "our topics",
        "topics",
        "welcome",
        "home",
        "overview",
        "locations",
        "guidelines and standards",
        "guidelines standards",
        "products and services",
        "products services",
        "page has been deleted",
    }
    if text in generic_exact:
        return True

    # 例如 C/LS Topics、C/LS - Topics、C/CA Organization。
    if re.fullmatch(r"[a-z0-9 ]{0,20}(topics|organization|organisation)", text):
        return True

    return False


def leaf_matches_title(leaf: str, title: str) -> bool:
    """
    判断路径末级是否其实就是当前页面主题。

    示例：
    - Board of Management == Board of Management
    - Compliance ~= Compliance @ BD
    - Security ~= BD Security
    - Guidelines and standards ~= Guidelines & Standards for BD
    - C/AUC ~= C/AUC | Corporate Internal Auditing...
    """
    left = normalize_for_compare(leaf)
    right = normalize_for_compare(title)

    if not left or not right:
        return False

    if left == right:
        return True

    if len(left) >= 3 and (left in right or right in left):
        return True

    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens) / min(
            len(left_tokens), len(right_tokens)
        )
        if overlap >= 0.80:
            return True

    return SequenceMatcher(None, left, right).ratio() >= 0.78


def clean_topic_name(value: str) -> str:
    text = normalize_inline(value)
    text = re.sub(
        r"^\s*(Welcome\s+to|About|Overview\s+of|Home\s+of)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text.strip(" -–—:|")


def split_departments_and_topic_seed(
    organization: str,
    title: str,
    raw_path: Any,
    *,
    normalized_path: list[str] | None = None,
) -> tuple[list[str], str]:
    """
    拆分 department_path 并确定页面主题名称。

    默认规则：
    1. 去掉路径中的根 Organization，因为它已经单独输出；
    2. 其后的所有路径层级原样保留到 Department Level 1～3；
    3. Page Topic 独立生成，不会因为生成主题而删除部门层级；
    4. 对于 C/LS Topics 这类泛化标题，可使用路径末级
       Anti-Money Laundering 作为更具体的页面主题。

    若 REMOVE_PAGE_LEAF_FROM_DEPARTMENT_LEVELS=True：
    - 路径末级与标题相同或高度相似时，会从部门层级中移除；
    - 适用于只想保留“纯部门”而不保留页面节点的场景。
    """
    path_parts = (
        department_path_parts(raw_path)
        if normalized_path is None
        else list(normalized_path)
    )

    if (
        path_parts
        and normalize_for_compare(path_parts[0])
        == normalize_for_compare(organization)
    ):
        path_parts = path_parts[1:]

    topic_seed = clean_topic_name(title)

    if path_parts:
        leaf = path_parts[-1]

        # 泛化标题无法说明具体主题时，优先采用路径末级。
        if (
            is_generic_title(title)
            and not is_code_like(leaf)
            and not is_structural_label(leaf)
        ):
            topic_seed = clean_topic_name(leaf)

        if REMOVE_PAGE_LEAF_FROM_DEPARTMENT_LEVELS:
            if leaf_matches_title(leaf, title):
                path_parts = path_parts[:-1]
            elif (
                is_generic_title(title)
                and not is_code_like(leaf)
                and not is_structural_label(leaf)
            ):
                path_parts = path_parts[:-1]

    return path_parts, topic_seed

TOPIC_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("page has been deleted", "not found", "error 404", "expired"),
        "page status and availability",
    ),
    (
        ("compliance", "anti money laundering", "gratuity", "speak up"),
        "compliance requirements and regulatory guidance",
    ),
    (
        (
            "cyber security",
            "cybersecurity",
            "information security",
            "data protection",
            "physical security",
        ),
        "security, cybersecurity and data protection",
    ),
    (
        (
            "guideline",
            "standard",
            "regulation",
            "policy",
            "management manual",
        ),
        "guidelines, standards and regulations",
    ),
    (
        (
            "organization",
            "organisation",
            "board of management",
            "leadership",
            "management of",
            "president roles",
        ),
        "organization structure and leadership",
    ),
    (
        ("location", "regional", "region ", "country", "sites"),
        "locations and regional presence",
    ),
    (
        (
            "legal",
            "law",
            "patent",
            "intellectual property",
            "tax",
            "customs",
        ),
        "legal, tax and intellectual property",
    ),
    (
        (
            "product",
            "service",
            "solution",
            "portfolio",
            "business application",
        ),
        "products, services and solutions",
    ),
    (
        ("consulting", "consultancy", "advisory"),
        "consulting services and expertise",
    ),
    (
        ("strategy", "priorities", "roadmap", "strategic portfolio"),
        "strategy, priorities and roadmap",
    ),
    (
        (
            "research",
            "technology",
            "innovation",
            "advance engineering",
            "advanced technology",
            "artificial intelligence",
            " ai ",
        ),
        "research, technology and innovation",
    ),
    (
        ("digital", "enterprise it", "it infrastructure", "workplace"),
        "digital business, IT and transformation",
    ),
    (
        ("internal auditing", "audit", "assurance"),
        "internal auditing, controls and assurance",
    ),
    (
        (
            "finance",
            "controlling",
            "risk management",
            "internal control",
            "reporting",
            "kpi",
        ),
        "finance, controlling and risk management",
    ),
    (
        ("quality", "product compliance", "performance excellence"),
        "quality management and continuous improvement",
    ),
    (
        ("supply chain", "purchasing", "procurement", "logistics"),
        "supply chain, purchasing and logistics",
    ),
    (
        (
            "human resources",
            "people",
            "associate",
            "talent",
            "career",
            "works council",
            "trainee",
        ),
        "people, organization and human resources",
    ),
    (
        (
            "sustainability",
            "environment",
            "health and safety",
            "ehs",
        ),
        "sustainability, environment, health and safety",
    ),
    (
        (
            "communication",
            "marketing",
            "press",
            "news",
            "moving images",
            "media",
        ),
        "communications, marketing and news",
    ),
    (
        ("training", "learning", "academy"),
        "training and learning resources",
    ),
    (
        ("process", "project management", "operations", "workflow"),
        "processes, projects and operations",
    ),
    (
        ("contact", "support", "help", "service desk"),
        "contacts and support",
    ),
    (
        ("topics", "index a z", "a-z", "our topics"),
        "topic index and reference navigation",
    ),
]


def topic_category(
    topic_seed: str,
    title: str,
    path_parts: list[str],
    content: str,
) -> str:
    """
    主题分类优先级：
    1. 页面主题名称和标题；
    2. Department Path；
    3. 正文前部。

    这样可避免路径中的“Products and Services”覆盖
    “Cross Division Consulting”“Digital Workplace”等更具体的标题主题。
    """
    primary_text = " ".join([
        topic_seed,
        title,
    ]).casefold()

    path_text = " ".join(path_parts).casefold()

    content_text = ""
    if TOPIC_USE_CONTENT:
        content_sample = (
            content
            if TOPIC_CONTENT_SCAN_LENGTH <= 0
            else content[:TOPIC_CONTENT_SCAN_LENGTH]
        )
        content_text = content_sample.casefold()

    for keywords, category in TOPIC_RULES:
        if any(keyword in primary_text for keyword in keywords):
            return category

    for keywords, category in TOPIC_RULES:
        if any(keyword in path_text for keyword in keywords):
            return category

    for keywords, category in TOPIC_RULES:
        if any(keyword in content_text for keyword in keywords):
            return category

    return "overview and key information"

def nearest_department_context(
    organization: str,
    department_parts: list[str],
) -> str:
    for item in reversed(department_parts):
        if item and not is_structural_label(item):
            return item
    return organization


def build_page_topic(
    organization: str,
    title: str,
    department_parts: list[str],
    topic_seed: str,
    content: str,
) -> str:
    context = nearest_department_context(
        organization,
        department_parts,
    )
    clean_title = clean_topic_name(title)
    clean_seed = clean_topic_name(topic_seed) or clean_title

    # 根页面和 Welcome 页面统一写成“组织概览”。
    if (
        normalize_for_compare(clean_seed)
        == normalize_for_compare(organization)
        or normalize_for_compare(clean_title)
        == normalize_for_compare(organization)
    ):
        topic = f"{organization} overview"

    # 泛化标题需要结合部门上下文，避免 Topic 只写成 Organization/Topics。
    elif is_generic_title(title) and clean_seed == clean_title:
        generic = normalize_for_compare(title)

        if "organization" in generic or "organisation" in generic:
            topic = f"{context} organization structure and responsibilities"
        elif "topic" in generic:
            topic = f"{context} topics and reference information"
        elif "guideline" in generic or "standard" in generic:
            topic = f"{context} guidelines, standards and regulations"
        elif "location" in generic:
            topic = f"{context} locations and regional presence"
        else:
            topic = f"{context} overview and key information"

    else:
        if PAGE_TOPIC_MODE == "concise":
            topic = clean_seed
        else:
            category = topic_category(
                clean_seed,
                title,
                department_parts,
                content,
            )
            topic = f"{clean_seed} — {category}"

    topic = normalize_inline(topic)

    if len(topic) > PAGE_TOPIC_MAX_LENGTH:
        topic = topic[: PAGE_TOPIC_MAX_LENGTH - 1].rstrip() + "…"

    return topic


def department_level_values(
    department_parts: list[str],
) -> dict[str, str]:
    parts = list(department_parts)

    if (
        MERGE_DEPARTMENT_OVERFLOW_TO_LAST
        and len(parts) > DEPARTMENT_LEVEL_COUNT
    ):
        fixed = parts[: DEPARTMENT_LEVEL_COUNT - 1]
        overflow = " > ".join(
            parts[DEPARTMENT_LEVEL_COUNT - 1 :]
        )
        parts = fixed + [overflow]

    output: dict[str, str] = {}
    for index, header in enumerate(DEPARTMENT_HEADERS):
        output[header] = (
            parts[index] if index < len(parts) else ""
        )

    return output


def locate_pages_file() -> Path:
    if not AUTO_FIND_LATEST_PAGES:
        path = config_path(INPUT_PAGES_FILE)
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到输入文件：{path}\n"
                "请先运行爬虫，或修改 INPUT_PAGES_FILE。"
            )
        return path

    search_directories = {SCRIPT_DIR, CRAWL_OUTPUT_DIR}
    candidates = {
        path
        for directory in search_directories
        if directory.is_dir()
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".jsonl"
        and path.name.lower().startswith("pages")
    }
    if not candidates:
        raise FileNotFoundError(
            "项目目录和爬虫输出目录中都没有找到 pages*.jsonl。"
        )
    return max(
        candidates,
        key=lambda path: (
            path.stat().st_mtime,
            path.stat().st_size,
            path.name,
        ),
    )


def iter_jsonl(
    path: Path,
) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                yield line_no, None, "blank"
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                message = f"第 {line_no} 行 JSON 无效：{exc}"
                if STRICT_JSON:
                    raise ValueError(message) from exc
                yield line_no, None, message
                continue
            if not isinstance(obj, dict):
                message = f"第 {line_no} 行不是 JSON object"
                if STRICT_JSON:
                    raise ValueError(message)
                yield line_no, None, message
                continue
            yield line_no, obj, None


def passes_filters(
    record: dict[str, Any],
    target_map: dict[str, str],
) -> tuple[bool, str | None]:
    organization = normalize_inline(
        record.get("root_department")
    )
    selected_org = target_map.get(organization.casefold())
    if selected_org is None:
        return False, None

    language = normalize_inline(record.get("language"))
    if ALLOWED_LANGUAGES:
        allowed = {
            normalize_inline(item).casefold()
            for item in ALLOWED_LANGUAGES
        }
        if language.casefold() not in allowed:
            return False, selected_org

    status = record.get("http_status")
    if ALLOWED_HTTP_STATUS and status not in ALLOWED_HTTP_STATUS:
        return False, selected_org

    mode = normalize_inline(record.get("fetch_mode"))
    if ALLOWED_FETCH_MODES:
        modes = {
            normalize_inline(item).casefold()
            for item in ALLOWED_FETCH_MODES
        }
        if mode.casefold() not in modes:
            return False, selected_org

    depth = record.get("depth")
    if (
        MIN_DEPTH is not None
        and (not isinstance(depth, int) or depth < MIN_DEPTH)
    ):
        return False, selected_org
    if (
        MAX_DEPTH is not None
        and (not isinstance(depth, int) or depth > MAX_DEPTH)
    ):
        return False, selected_org

    content = record_content(record)
    if ONLY_WITH_CONTENT and not content:
        return False, selected_org
    if len(content) < MIN_CONTENT_LENGTH:
        return False, selected_org
    if ONLY_COMPLETE_SIDEBARS and record.get("sidebar_complete") is not True:
        return False, selected_org

    return True, selected_org


def validate_active_filter_fields(
    record: dict[str, Any],
    *,
    line_no: int,
) -> None:
    required: list[str] = []
    if ALLOWED_HTTP_STATUS:
        required.append("http_status")
    if ALLOWED_FETCH_MODES:
        required.append("fetch_mode")
    if MIN_DEPTH is not None or MAX_DEPTH is not None:
        required.append("depth")
    missing = [field for field in required if field not in record]
    if missing:
        raise ValueError(
            f"第 {line_no} 行缺少已启用过滤器所需字段："
            + ", ".join(missing)
            + "。当前爬虫输出结构不提供这些字段，请关闭对应过滤器。"
        )


def build_row(
    record: dict[str, Any],
    organization: str,
    order: int,
    source_index: int,
) -> dict[str, Any]:
    title = normalize_inline(record.get("title"))
    content = record_content(record)
    raw_path = record.get("department_path")
    full_path_parts = department_path_parts(raw_path)
    full_path_text = " > ".join(full_path_parts)
    input_url = "" if record.get("url") is None else str(record["url"]).strip()
    requested_url = (
        ""
        if record.get("requested_url") is None
        else str(record["requested_url"]).strip()
    )

    department_parts, topic_seed = (
        split_departments_and_topic_seed(
            organization,
            title,
            raw_path,
            normalized_path=full_path_parts,
        )
    )

    row: dict[str, Any] = {
        "organization_order": order,
        "Organization": organization,
        "Discovered In Organizations": organization,
        "Ownership Confidence": "High (single organization)",
        **department_level_values(department_parts),
        "Page Topic": build_page_topic(
            organization,
            title,
            department_parts,
            topic_seed,
            content,
        ),
        "Title": title,
        "_page_owner_department": normalize_inline(
            record.get("page_owner_department")
        ),
        "_last_changed": normalize_inline(record.get("last_changed")),
        "Content": content,
        "_source_index": source_index,
        "_input_url": input_url,
        "_requested_url": requested_url,
        "_language": normalize_inline(record.get("language")).casefold(),
        "_department_path": full_path_text,
        "_department_depth": len(full_path_parts),
        "_sidebar_complete": record.get("sidebar_complete"),
        "_url_owner": infer_organization_from_url(input_url),
        "_recommendation_alias": is_wcms_recommendation_url(input_url),
    }

    if KEEP_FULL_DEPARTMENT_PATH:
        row["Department Path"] = full_path_text

    return row


def row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["organization_order"],
        str(row.get("Department Level 1", "")).casefold(),
        str(row.get("Department Level 2", "")).casefold(),
        str(row.get("Department Level 3", "")).casefold(),
        str(row.get("Page Topic", "")).casefold(),
        str(row.get("Title", "")).casefold(),
        str(row.get("_input_url", "")).casefold(),
    )


def deduplication_key(row: dict[str, Any]) -> tuple[str, ...]:
    identity_url = page_identity_url(row.get("_input_url"))
    language = str(row.get("_language") or "")
    organization = str(row.get("Organization") or "").casefold()
    if identity_url:
        if URL_DEDUPLICATION_SCOPE == "organization":
            return ("url", organization, identity_url, language)
        return ("url", identity_url, language)

    # Missing URLs are not safe to merge across organizations.
    return (
        "fallback",
        organization,
        str(row.get("Title") or "").casefold(),
        str(row.get("_department_path") or "").casefold(),
        language,
    )


def row_preference_rank(row: dict[str, Any]) -> tuple[int, ...]:
    organization = str(row.get("Organization") or "")
    inferred_owner = str(row.get("_url_owner") or "")
    if inferred_owner:
        owner_tier = 2 if inferred_owner == organization else 0
    else:
        owner_tier = 1

    input_url = str(row.get("_input_url") or "")
    requested_url = str(row.get("_requested_url") or "")
    recommendation_alias = row.get("_recommendation_alias") is True

    return (
        owner_tier,
        -int(row.get("_department_depth") or 0),
        int(row.get("_sidebar_complete") is True),
        int(bool(requested_url) and requested_url == input_url),
        int(bool(row.get("Title"))),
        len(str(row.get("Content") or "")),
        int(not recommendation_alias),
    )


def select_preferred_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if DUPLICATE_KEEP == "first":
        return min(rows, key=lambda row: int(row.get("_source_index") or 0))
    if DUPLICATE_KEEP == "last":
        return max(rows, key=lambda row: int(row.get("_source_index") or 0))

    ranked_rows = [(row_preference_rank(row), row) for row in rows]
    best_rank = max(rank for rank, _row in ranked_rows)
    tied = [row for rank, row in ranked_rows if rank == best_rank]
    return min(
        tied,
        key=lambda row: (
            str(row.get("Organization") or "").casefold(),
            str(row.get("_department_path") or "").casefold(),
            str(row.get("_requested_url") or ""),
            str(row.get("_input_url") or ""),
            int(row.get("_source_index") or 0),
        ),
    )


def ownership_confidence(
    chosen: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    organization = str(chosen.get("Organization") or "")
    inferred_owner = str(chosen.get("_url_owner") or "")
    if inferred_owner == organization:
        return "High (URL match)"

    minimum_depth_by_organization: dict[str, int] = {}
    for row in candidates:
        candidate_organization = str(row.get("Organization") or "")
        depth = int(row.get("_department_depth") or 0)
        previous = minimum_depth_by_organization.get(candidate_organization)
        if previous is None or depth < previous:
            minimum_depth_by_organization[candidate_organization] = depth

    if len(minimum_depth_by_organization) == 1:
        return "High (single organization)"
    chosen_depth = minimum_depth_by_organization.get(organization, 0)
    other_depths = [
        depth
        for candidate_organization, depth in minimum_depth_by_organization.items()
        if candidate_organization != organization
    ]
    gap = min(other_depths) - chosen_depth
    if gap >= 2:
        return "Medium (clearer path)"
    if gap == 1:
        return "Medium-Low (shorter path)"
    return "Low (ambiguous)"


def deduplicate_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate rows already normalized by :func:`build_row`."""
    if not DEDUPLICATE_BY_URL:
        return rows, 0

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(deduplication_key(row), []).append(row)

    organization_order = {
        organization: index
        for index, organization in enumerate(TARGET_ORGANIZATIONS)
    }
    selected: list[dict[str, Any]] = []
    for candidates in groups.values():
        chosen = dict(select_preferred_row(candidates))
        discovered = sorted(
            {
                str(row.get("Organization") or "")
                for row in candidates
                if row.get("Organization")
            },
            key=organization_order.__getitem__,
        )
        chosen["Discovered In Organizations"] = " | ".join(discovered)
        chosen["Ownership Confidence"] = ownership_confidence(
            chosen,
            candidates,
        )

        plain_url = min(
            (
                str(row.get("_input_url") or "")
                for row in candidates
                if row.get("_input_url")
                and row.get("_recommendation_alias") is not True
            ),
            default="",
        )
        if plain_url:
            chosen["_input_url"] = plain_url
        selected.append(chosen)

    return selected, len(rows) - len(selected)


def read_data(path: Path) -> Result:
    target_map = {
        normalize_inline(item).casefold(): item
        for item in TARGET_ORGANIZATIONS
    }
    target_order = {
        item: index
        for index, item in enumerate(TARGET_ORGANIZATIONS)
    }

    rows: list[dict[str, Any]] = []
    total = valid = invalid = blank = excluded = 0
    cleaned_toolbars = 0

    for line_no, record, error in iter_jsonl(path):
        total += 1
        if error == "blank":
            blank += 1
            continue
        if error:
            invalid += 1
            print(
                f"警告：{path.name} {error}",
                file=sys.stderr,
            )
            continue

        assert record is not None
        valid += 1
        validate_active_filter_fields(record, line_no=line_no)
        prepared, content_metadata = prepare_record_for_export(record)
        if content_metadata.toolbar_removed:
            cleaned_toolbars += 1

        ok, organization = passes_filters(
            prepared,
            target_map,
        )
        if not ok or organization is None:
            excluded += 1
            continue

        rows.append(
            build_row(
                prepared,
                organization,
                target_order[organization],
                line_no,
            )
        )

    rows, duplicates = deduplicate_rows(rows)
    rows.sort(key=row_sort_key)
    incomplete_sidebars = sum(
        row.get("_sidebar_complete") is False for row in rows
    )
    inferred_owner_departments = sum(
        bool(normalize_inline(row.get("_page_owner_department")))
        for row in rows
    )

    return Result(
        rows,
        total,
        valid,
        invalid,
        blank,
        excluded,
        duplicates,
        incomplete_sidebars,
        cleaned_toolbars,
        inferred_owner_departments,
    )


def excel_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value

    text = EXCEL_ILLEGAL_CHARACTERS_RE.sub("", str(value))
    if ESCAPE_EXCEL_FORMULAS and text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    if len(text) > EXCEL_CELL_MAX_LENGTH:
        keep = EXCEL_CELL_MAX_LENGTH - len(TRUNCATION_MARKER)
        return text[: max(0, keep)] + TRUNCATION_MARKER
    return text


def load_crawl_summary(pages_path: Path) -> dict[str, Any] | None:
    summary_path = pages_path.with_name("summary.json")
    if not summary_path.is_file():
        return None
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取爬虫摘要 {summary_path}: {exc}") from exc
    if not isinstance(summary, dict):
        raise ValueError(f"爬虫摘要不是 JSON object：{summary_path}")
    schema_version = summary.get("output_schema_version")
    if schema_version != CRAWLER_OUTPUT_SCHEMA_VERSION:
        raise ValueError(
            "pages.jsonl 的输出结构版本不兼容："
            f"{schema_version!r}，当前要求 {CRAWLER_OUTPUT_SCHEMA_VERSION}。"
        )
    return summary


def validate_config() -> None:
    if DEPARTMENT_LEVEL_COUNT < 1:
        raise ValueError(
            "DEPARTMENT_LEVEL_COUNT 必须大于或等于 1"
        )

    if PAGE_TOPIC_MODE not in {"concise", "descriptive"}:
        raise ValueError(
            'PAGE_TOPIC_MODE 只能是 "concise" 或 "descriptive"'
        )

    if DUPLICATE_KEEP not in {"best", "first", "last"}:
        raise ValueError(
            'DUPLICATE_KEEP 只能是 "best"、"first" 或 "last"'
        )

    if URL_DEDUPLICATION_SCOPE not in {"organization", "global"}:
        raise ValueError(
            'URL_DEDUPLICATION_SCOPE 只能是 "organization" 或 "global"'
        )

    if TOPIC_CONTENT_SCAN_LENGTH < 0:
        raise ValueError("TOPIC_CONTENT_SCAN_LENGTH 不能小于 0")

    if (
        MIN_DEPTH is not None
        and MAX_DEPTH is not None
        and MIN_DEPTH > MAX_DEPTH
    ):
        raise ValueError("MIN_DEPTH 不能大于 MAX_DEPTH")

    missing_widths = [
        header
        for header in OUTPUT_HEADERS
        if header not in COLUMN_WIDTHS
    ]
    if missing_widths:
        raise ValueError(
            "COLUMN_WIDTHS 缺少以下输出列："
            + ", ".join(missing_widths)
        )

    duplicate_headers = {
        header for header in OUTPUT_HEADERS if OUTPUT_HEADERS.count(header) > 1
    }
    if duplicate_headers:
        raise ValueError(
            "OUTPUT_HEADERS 包含重复列：" + ", ".join(sorted(duplicate_headers))
        )


def write_excel(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 openpyxl。请执行：\n"
            f'  "{sys.executable}" -m pip install -r '
            f'"{SCRIPT_DIR / "requirements.txt"}"'
        ) from exc

    wb = Workbook()
    ws = wb.active
    ws.title = OUTPUT_SHEET_NAME
    ws.sheet_view.showGridLines = False

    header_fill = PatternFill(
        "solid",
        fgColor=HEADER_FILL_COLOR,
    )
    header_font = Font(
        bold=True,
        color=HEADER_FONT_COLOR,
        size=HEADER_FONT_SIZE,
    )
    body_font = Font(size=BODY_FONT_SIZE)
    header_alignment = Alignment(
        horizontal="center",
        vertical="center",
        wrap_text=True,
    )
    body_alignment = Alignment(
        vertical="top",
        wrap_text=True,
    )

    for column, header in enumerate(OUTPUT_HEADERS, 1):
        cell = ws.cell(1, column, header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        ws.column_dimensions[
            get_column_letter(column)
        ].width = COLUMN_WIDTHS[header]

    ws.row_dimensions[1].height = HEADER_ROW_HEIGHT
    # A worksheet default avoids creating one RowDimension object per result.
    ws.sheet_format.defaultRowHeight = BODY_ROW_HEIGHT

    for row_number, row in enumerate(rows, 2):
        for column, header in enumerate(OUTPUT_HEADERS, 1):
            cell = ws.cell(
                row_number,
                column,
                excel_value(row.get(header)),
            )
            cell.font = body_font
            cell.alignment = body_alignment

    last_column = get_column_letter(len(OUTPUT_HEADERS))
    last_row = max(1, len(rows) + 1)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = (
        f"A1:{last_column}{last_row}"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=path.suffix,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        wb.save(temporary)
        os.replace(temporary, path)
    finally:
        wb.close()
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    configure_console_utf8()
    validate_config()

    input_path = locate_pages_file()
    output_path = config_path(OUTPUT_EXCEL_FILE)
    crawl_summary = load_crawl_summary(input_path)

    print(f"Python：{sys.executable}")
    print(f"脚本目录：{SCRIPT_DIR}")
    print(f"输入文件：{input_path}")
    print(f"输出文件：{output_path}")
    print(f"部门拆分层数：{DEPARTMENT_LEVEL_COUNT}")
    print(f"页面主题模式：{PAGE_TOPIC_MODE}")
    if crawl_summary is not None:
        print(f"爬虫状态：{crawl_summary.get('status', 'unknown')}")
        if crawl_summary.get("status") != "completed":
            print(
                "警告：爬虫摘要不是完全完成状态，请检查 summary.json。",
                file=sys.stderr,
            )

    result = read_data(input_path)
    write_excel(output_path, result.rows)

    print("\n处理完成")
    print(f"原始行数：{result.total}")
    print(f"有效 JSON：{result.valid}")
    print(f"无效行：{result.invalid}")
    print(f"空行：{result.blank}")
    print(f"过滤记录：{result.excluded}")
    print(f"重复记录：{result.duplicates}")
    print(f"已清理 WCMS 工具栏：{result.cleaned_toolbars}")
    print(f"已回填页面负责部门：{result.inferred_owner_departments}")
    print(f"导出后侧边栏不完整记录：{result.incomplete_sidebars}")
    print(f"提取页面：{len(result.rows)}")

    counts = {
        organization: 0
        for organization in TARGET_ORGANIZATIONS
    }
    for row in result.rows:
        counts[row["Organization"]] += 1

    for organization in TARGET_ORGANIZATIONS:
        print(f"  {organization}: {counts[organization]}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
