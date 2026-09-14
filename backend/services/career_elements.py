from __future__ import annotations

import re

from backend.schemas.profile import EmployeeProfile


CAREER_ELEMENTS_INTENT_IDS = frozenset(
    {"development", "development_improvement"}
)
_JOB_GRADE_PATTERN = re.compile(
    r"(?<![A-Z0-9])(G|SL)\s*-?\s*(\d+)",
    re.IGNORECASE,
)
_SOURCE_CAREER_ELEMENTS_PATTERN = re.compile(
    r"^Current Career Elements\s*[：:]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_EMPTY_CAREER_ELEMENT_VALUES = frozenset(
    {"", "n/a", "na", "none", "null", "-"}
)


def is_g9_or_above(level: str | None) -> bool:
    """Return whether the Bosch grade is in the Career Elements population."""
    for grade_family, raw_number in _JOB_GRADE_PATTERN.findall(str(level or "")):
        number = int(raw_number)
        if grade_family.upper() == "SL" or number >= 9:
            return True
    return False


def current_career_elements(profile: EmployeeProfile | None) -> list[str]:
    if profile is None:
        return []

    raw_values = list(profile.current_career_elements)
    if not raw_values:
        match = _SOURCE_CAREER_ELEMENTS_PATTERN.search(
            profile.source_profile_text or ""
        )
        if match:
            raw_values = re.split(r"[,，;；、|]+", match.group(1))

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        value = " ".join(str(raw_value).split()).strip()
        identity = value.casefold()
        if identity in _EMPTY_CAREER_ELEMENT_VALUES or identity in seen:
            continue
        seen.add(identity)
        normalized.append(value)
    return normalized


def career_elements_applicable(
    profile: EmployeeProfile | None,
    intent_id: str | None,
) -> bool:
    return bool(
        profile
        and is_g9_or_above(profile.level)
        and str(intent_id or "").strip() in CAREER_ELEMENTS_INTENT_IDS
    )
