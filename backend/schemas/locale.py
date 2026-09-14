from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeVar, cast


SessionLocale = Literal["zh-CN", "en", "de", "ja"]
SUPPORTED_SESSION_LOCALES: tuple[SessionLocale, ...] = (
    "zh-CN",
    "en",
    "de",
    "ja",
)

_T = TypeVar("_T")


def _supported_session_locale(value: str | None) -> SessionLocale | None:
    language = str(value or "").strip().lower().replace("_", "-")
    if language == "zh" or language.startswith("zh-"):
        return "zh-CN"
    if language == "jp":
        return "ja"
    for locale in ("en", "de", "ja"):
        if language == locale or language.startswith(f"{locale}-"):
            return cast(SessionLocale, locale)
    return None


def normalize_session_locale(
    value: str | None,
    *,
    default: SessionLocale = "zh-CN",
) -> SessionLocale:
    """Normalize a browser/client locale to one of the persisted locale keys."""

    return _supported_session_locale(value) or default


def localized_value(locale: str, values: Mapping[str, _T]) -> _T:
    """Return a localized fixed value, with the legacy Chinese value as fallback."""

    if locale in values:
        return values[locale]
    return values["zh-CN"]


def locale_from_accept_language(value: str | None) -> SessionLocale:
    """Resolve the preferred supported language; legacy/no header is Chinese."""

    candidates: list[tuple[float, int, SessionLocale]] = []
    for index, entry in enumerate(str(value or "").split(",")):
        parts = [part.strip() for part in entry.split(";")]
        locale = _supported_session_locale(parts[0])
        if locale is None:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, raw_quality = parameter.partition("=")
            if separator and name.strip().lower() == "q":
                try:
                    quality = float(raw_quality.strip())
                except ValueError:
                    quality = 0.0
                break
        if not 0.0 < quality <= 1.0:
            continue
        candidates.append((quality, -index, locale))
    if candidates:
        return max(candidates)[2]
    return "zh-CN"
