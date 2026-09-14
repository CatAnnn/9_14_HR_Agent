from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sqlite3
import struct
from threading import RLock
from typing import Any, Callable


class ResumableEmbeddingService:
    """Persist successful ingestion embeddings so interrupted syncs can resume.

    The cache stores only a SHA-256 digest of the exact embedding input and the
    float32 vector. It follows the repository's embedding profile identity
    (model name + dimensions), so changing an endpoint or provider does not
    invalidate an otherwise compatible vector space.
    """

    CACHE_VERSION = "kb-ingest-embedding-v1"
    _SQLITE_LOOKUP_BATCH_SIZE = 500

    def __init__(
        self,
        delegate: Any,
        *,
        cache_path: Path,
        profile_id: str,
        dimensions: int,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        normalized_profile_id = str(profile_id or "").strip()
        normalized_dimensions = int(dimensions)
        if not normalized_profile_id:
            raise ValueError("Embedding cache profile_id cannot be empty.")
        if normalized_dimensions <= 0:
            raise ValueError("Embedding cache dimensions must be positive.")

        self.delegate = delegate
        self.cache_path = Path(cache_path)
        self.profile_id = normalized_profile_id
        self.dimensions = normalized_dimensions
        self.progress_callback = progress_callback
        self._lock = RLock()
        self._closed = False
        self._stats: dict[str, int] = {
            "embed_call_count": 0,
            "requested_text_count": 0,
            "cache_hit_count": 0,
            "cache_miss_count": 0,
            "embedded_text_count": 0,
            "cache_write_count": 0,
            "invalid_cache_entry_count": 0,
        }

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.cache_path,
            timeout=30,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA wal_autocheckpoint = 1000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embedding_vectors (
                cache_key TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_accessed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS embedding_vectors_profile_idx
            ON embedding_vectors (profile_id)
            """
        )
        self._connection.commit()

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "cache_path": str(self.cache_path),
                "profile_id": self.profile_id,
                "dimensions": self.dimensions,
                **self._stats,
            }

    def embed(self, texts: list[str]) -> list[list[float]]:
        cleaned = [
            str(text)
            for text in texts
            if text is not None and str(text).strip()
        ]
        if not cleaned:
            return []

        keys_and_hashes = [self._cache_identity(text) for text in cleaned]
        keys = [cache_key for cache_key, _ in keys_and_hashes]
        with self._lock:
            self._ensure_open()
            cached = self._get_many(list(dict.fromkeys(keys)))

            missing_by_key: dict[str, tuple[str, str]] = {}
            for text, (cache_key, text_sha256) in zip(
                cleaned,
                keys_and_hashes,
                strict=True,
            ):
                if cache_key not in cached:
                    missing_by_key.setdefault(cache_key, (text, text_sha256))

            input_hit_count = sum(cache_key in cached for cache_key in keys)
            input_miss_count = len(keys) - input_hit_count
            self._stats["embed_call_count"] += 1
            self._stats["requested_text_count"] += len(cleaned)
            self._stats["cache_hit_count"] += input_hit_count
            self._stats["cache_miss_count"] += input_miss_count

            if missing_by_key:
                missing_items = list(missing_by_key.items())
                missing_texts = [item[1][0] for item in missing_items]
                generated = self.delegate.embed(missing_texts)
                if len(generated) != len(missing_items):
                    raise RuntimeError(
                        "Embedding API returned an unexpected vector count for "
                        "the resumable cache batch."
                    )

                encoded_rows: list[tuple[str, str, bytes]] = []
                for (cache_key, (_, text_sha256)), vector in zip(
                    missing_items,
                    generated,
                    strict=True,
                ):
                    validated = self._validate_vector(vector)
                    cached[cache_key] = validated
                    encoded_rows.append(
                        (cache_key, text_sha256, self._encode_vector(validated))
                    )
                self._put_many(encoded_rows)
                self._stats["embedded_text_count"] += len(missing_items)
                self._stats["cache_write_count"] += len(missing_items)

            result = [cached[cache_key] for cache_key in keys]
            if self.progress_callback is not None:
                self.progress_callback(self.stats)
            return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def __enter__(self) -> ResumableEmbeddingService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def _cache_identity(self, text: str) -> tuple[str, str]:
        text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return (
            f"{self.CACHE_VERSION}:{self.profile_id}:{text_sha256}",
            text_sha256,
        )

    def _get_many(self, keys: list[str]) -> dict[str, list[float]]:
        cached: dict[str, list[float]] = {}
        invalid_keys: list[str] = []
        for start in range(0, len(keys), self._SQLITE_LOOKUP_BATCH_SIZE):
            batch = keys[start : start + self._SQLITE_LOOKUP_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = self._connection.execute(
                f"""
                SELECT cache_key, dimensions, vector
                FROM embedding_vectors
                WHERE cache_key IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for cache_key, dimensions, payload in rows:
                try:
                    if int(dimensions) != self.dimensions:
                        raise ValueError(
                            "Cached embedding dimensions do not match."
                        )
                    cached[str(cache_key)] = self._decode_vector(payload)
                except (TypeError, ValueError, struct.error):
                    invalid_keys.append(str(cache_key))

        if invalid_keys:
            with self._connection:
                self._connection.executemany(
                    "DELETE FROM embedding_vectors WHERE cache_key = ?",
                    [(cache_key,) for cache_key in invalid_keys],
                )
            self._stats["invalid_cache_entry_count"] += len(invalid_keys)
        return cached

    def _put_many(self, rows: list[tuple[str, str, bytes]]) -> None:
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO embedding_vectors (
                    cache_key,
                    profile_id,
                    text_sha256,
                    dimensions,
                    vector
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (cache_key) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    text_sha256 = excluded.text_sha256,
                    dimensions = excluded.dimensions,
                    vector = excluded.vector,
                    last_accessed_at = CURRENT_TIMESTAMP
                """,
                [
                    (
                        cache_key,
                        self.profile_id,
                        text_sha256,
                        self.dimensions,
                        payload,
                    )
                    for cache_key, text_sha256, payload in rows
                ],
            )

    def _validate_vector(self, vector: Any) -> list[float]:
        try:
            normalized = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Embedding API returned a non-numeric vector."
            ) from exc
        if len(normalized) != self.dimensions:
            raise ValueError(
                "Embedding API returned a vector with unexpected dimensions: "
                f"expected {self.dimensions}, got {len(normalized)}."
            )
        if not all(math.isfinite(value) for value in normalized):
            raise ValueError("Embedding API returned a non-finite vector.")
        return normalized

    def _encode_vector(self, vector: list[float]) -> bytes:
        return struct.pack(f"<{self.dimensions}f", *vector)

    def _decode_vector(self, payload: Any) -> list[float]:
        raw = bytes(payload)
        expected_bytes = self.dimensions * 4
        if len(raw) != expected_bytes:
            raise ValueError(
                "Cached embedding payload has an unexpected byte length."
            )
        vector = list(struct.unpack(f"<{self.dimensions}f", raw))
        if not all(math.isfinite(value) for value in vector):
            raise ValueError("Cached embedding contains a non-finite value.")
        return vector

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Resumable embedding cache is closed.")
