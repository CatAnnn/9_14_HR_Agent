from __future__ import annotations

import re
import unicodedata
from typing import Any


_RATING_LINE = re.compile(
    r"^\s*(?:Performance Rating \(A Group\)|当前绩效评级)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
_TCL_LINE = re.compile(
    r"^\s*TCL(?:\s*\(SLx\))?\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
_PERFORMANCE_TCL = re.compile(
    r"(?:^|[,;|/])\s*Performance\s*[:=]?\s*(#|\+{1,4})(?=\s*(?:[,;|/]|$))",
    re.IGNORECASE,
)


def normalize_performance_rating(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 5 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and 1 <= value <= 5 else None

    text = _normalize_text(value)
    match = re.fullmatch(r"([1-5])(?:\.0+)?", text)
    return int(match.group(1)) if match else None


def normalize_tcl(value: Any) -> str | None:
    if value is None:
        return None

    text = _normalize_text(value)
    compact = re.sub(r"\s+", "", text)
    if compact == "#" or re.fullmatch(r"\+{1,4}", compact):
        return compact

    match = _PERFORMANCE_TCL.search(text)
    return match.group(1) if match else None


def extract_profile_rating(source_profile_text: str | None) -> int | None:
    value = _extract_labeled_value(source_profile_text, _RATING_LINE)
    return normalize_performance_rating(value)


def extract_profile_tcl(source_profile_text: str | None) -> str | None:
    value = _extract_labeled_value(source_profile_text, _TCL_LINE)
    return normalize_tcl(value)


def _extract_labeled_value(source_profile_text: str | None, pattern: re.Pattern[str]) -> str | None:
    if not source_profile_text:
        return None
    for line in _normalize_text(source_profile_text).splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1).strip() or None
    return None


def _normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()
