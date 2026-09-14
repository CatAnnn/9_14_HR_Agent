from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import threading
from typing import Any, Final

from backend.config.settings import get_settings


logger = logging.getLogger(__name__)

PATTERN_RENDER_FIELDS: Final[tuple[str, ...]] = (
    "construct_name",
    "description",
    "core_mechanisms",
    "real_world_manifestation",
)

_UNSEEN_REVISION = object()
_MISSING_REVISION: Final[tuple[str]] = ("missing",)


@dataclass(frozen=True, slots=True)
class PsychologicalPatternCatalogSnapshot:
    """An immutable, prompt-ready view of the last successfully loaded catalog."""

    available: bool = False
    source_mtime_ns: int | None = None
    catalog_version: str | None = None
    # This is deliberately a JSON string: it can be embedded in a prompt without
    # exposing mutable process-owned dictionaries to concurrent request handlers.
    render_patterns: str = "[]"
    valid_pattern_ids: frozenset[str] = field(default_factory=frozenset)

    def render_payload(self) -> list[dict[str, Any]]:
        """Return a fresh Python structure for renderers that do not accept JSON."""

        payload = json.loads(self.render_patterns)
        return payload


class PsychologicalPatternCatalogLoader:
    """Read-only, hot-reloading access to the business-config pattern catalog."""

    def __init__(self, path: Path | None = None):
        self.path = (
            path
            if path is not None
            else get_settings().business_config_dir / "patterns_data.json"
        )
        self._lock = threading.RLock()
        self._last_attempted_revision: object = _UNSEEN_REVISION
        self._snapshot = PsychologicalPatternCatalogSnapshot()

    def snapshot(self) -> PsychologicalPatternCatalogSnapshot:
        """Return the current catalog, retaining the last known good load on failure."""

        with self._lock:
            revision = self._source_revision()
            if revision == self._last_attempted_revision:
                return self._snapshot

            self._last_attempted_revision = revision
            try:
                snapshot = self._load_snapshot(revision)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                logger.warning(
                    "Unable to load psychological pattern catalog from %s; "
                    "using the last known good snapshot",
                    self.path,
                    exc_info=True,
                )
                return self._snapshot

            self._snapshot = snapshot
            return snapshot

    async def asnapshot(self) -> PsychologicalPatternCatalogSnapshot:
        """Load without blocking an async request's event-loop thread."""

        return await asyncio.to_thread(self.snapshot)

    def _source_revision(self) -> tuple[int, int, int, int] | tuple[str]:
        try:
            stat = self.path.stat()
        except OSError:
            return _MISSING_REVISION
        return (
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    def _load_snapshot(
        self,
        revision: tuple[int, int, int, int] | tuple[str],
    ) -> PsychologicalPatternCatalogSnapshot:
        source_bytes = self.path.read_bytes()
        raw_catalog = json.loads(source_bytes)
        if not isinstance(raw_catalog, dict):
            raise ValueError("Psychological pattern catalog root must be a JSON object")

        render_items: list[dict[str, Any]] = []
        for pattern_id, raw_pattern in raw_catalog.items():
            # The source file owns content quality. The loader only projects the
            # four prompt fields and never classifies or normalizes their values.
            pattern = raw_pattern if isinstance(raw_pattern, dict) else {}
            render_items.append(
                {
                    "pattern_id": pattern_id,
                    **{
                        field_name: pattern.get(field_name)
                        for field_name in PATTERN_RENDER_FIELDS
                    },
                }
            )

        return PsychologicalPatternCatalogSnapshot(
            available=True,
            source_mtime_ns=revision[2] if len(revision) == 4 else None,
            catalog_version=hashlib.sha256(source_bytes).hexdigest(),
            render_patterns=json.dumps(
                render_items,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            valid_pattern_ids=frozenset(raw_catalog),
        )
