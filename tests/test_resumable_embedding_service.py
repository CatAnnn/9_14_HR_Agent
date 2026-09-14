from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pytest

from backend.services.resumable_embedding_service import ResumableEmbeddingService


class RecordingEmbeddingDelegate:
    def __init__(
        self,
        vectors: dict[str, list[float]],
        *,
        fail_on: set[str] | None = None,
    ) -> None:
        self.vectors = vectors
        self.fail_on = fail_on or set()
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        if any(text in self.fail_on for text in batch):
            raise RuntimeError("embedding batch failed")
        return [list(self.vectors[text]) for text in batch]


def _stats_payload(service: ResumableEmbeddingService) -> dict:
    stats = service.stats
    if is_dataclass(stats):
        payload = asdict(stats)
    else:
        payload = dict(stats)
    assert json.loads(json.dumps(payload)) == payload
    return payload


def test_duplicate_inputs_are_generated_once_in_first_seen_order_and_persisted(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "embeddings.sqlite3"
    delegate = RecordingEmbeddingDelegate(
        {
            "beta": [2.0, 20.0],
            "alpha": [1.0, 10.0],
            "gamma": [3.0, 30.0],
        }
    )
    texts = ["beta", "alpha", "beta", "gamma", "alpha"]
    expected = [
        [2.0, 20.0],
        [1.0, 10.0],
        [2.0, 20.0],
        [3.0, 30.0],
        [1.0, 10.0],
    ]

    with ResumableEmbeddingService(
        delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    ) as service:
        assert service.embed(texts) == expected
        assert service.embed(texts) == expected
        _stats_payload(service)

    assert delegate.calls == [["beta", "alpha", "gamma"]]
    assert cache_path.exists()

    cached_only_delegate = RecordingEmbeddingDelegate({})
    reopened = ResumableEmbeddingService(
        cached_only_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    )
    try:
        assert reopened.embed(texts) == expected
    finally:
        reopened.close()

    assert cached_only_delegate.calls == []


def test_successful_batch_is_resumable_after_a_later_batch_fails(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "embeddings.sqlite3"
    initial_delegate = RecordingEmbeddingDelegate(
        {
            "first": [1.0, 0.0],
            "second": [0.0, 1.0],
            "third": [0.5, 0.5],
            "fourth": [0.25, 0.75],
        },
        fail_on={"third"},
    )
    interrupted = ResumableEmbeddingService(
        initial_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    )

    try:
        assert interrupted.embed(["first", "second"]) == [
            [1.0, 0.0],
            [0.0, 1.0],
        ]
        with pytest.raises(RuntimeError, match="embedding batch failed"):
            interrupted.embed(["third", "fourth"])

        resumed_delegate = RecordingEmbeddingDelegate(
            {
                "third": [0.5, 0.5],
                "fourth": [0.25, 0.75],
            }
        )
        resumed = ResumableEmbeddingService(
            resumed_delegate,
            cache_path=cache_path,
            profile_id="profile-a",
            dimensions=2,
        )
        try:
            assert resumed.embed(
                ["first", "second", "third", "fourth"]
            ) == [
                [1.0, 0.0],
                [0.0, 1.0],
                [0.5, 0.5],
                [0.25, 0.75],
            ]
        finally:
            resumed.close()

        assert resumed_delegate.calls == [["third", "fourth"]]
    finally:
        interrupted.close()


def test_cache_isolated_by_profile_and_recomputes_wrong_dimensions(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "embeddings.sqlite3"
    first_delegate = RecordingEmbeddingDelegate({"same": [1.0, 2.0]})
    with ResumableEmbeddingService(
        first_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    ) as first:
        assert first.embed(["same"]) == [[1.0, 2.0]]

    other_profile_delegate = RecordingEmbeddingDelegate({"same": [3.0, 4.0]})
    with ResumableEmbeddingService(
        other_profile_delegate,
        cache_path=cache_path,
        profile_id="profile-b",
        dimensions=2,
    ) as other_profile:
        assert other_profile.embed(["same"]) == [[3.0, 4.0]]

    cached_profile_delegate = RecordingEmbeddingDelegate({})
    with ResumableEmbeddingService(
        cached_profile_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    ) as cached_profile:
        assert cached_profile.embed(["same"]) == [[1.0, 2.0]]

    changed_dimensions_delegate = RecordingEmbeddingDelegate(
        {"same": [5.0, 6.0, 7.0]}
    )
    with ResumableEmbeddingService(
        changed_dimensions_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=3,
    ) as changed_dimensions:
        assert changed_dimensions.embed(["same"]) == [[5.0, 6.0, 7.0]]

    assert first_delegate.calls == [["same"]]
    assert other_profile_delegate.calls == [["same"]]
    assert cached_profile_delegate.calls == []
    assert changed_dimensions_delegate.calls == [["same"]]


@pytest.mark.parametrize(
    "bad_vector",
    [
        pytest.param([1.0], id="wrong-dimensions"),
        pytest.param([math.nan, 1.0], id="nan"),
        pytest.param([math.inf, 1.0], id="infinity"),
    ],
)
def test_invalid_vectors_are_not_reused(
    tmp_path: Path,
    bad_vector: list[float],
) -> None:
    cache_path = tmp_path / "embeddings.sqlite3"
    invalid_delegate = RecordingEmbeddingDelegate({"invalid": bad_vector})
    invalid = ResumableEmbeddingService(
        invalid_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    )
    try:
        with pytest.raises((RuntimeError, ValueError)):
            invalid.embed(["invalid"])
    finally:
        invalid.close()

    valid_delegate = RecordingEmbeddingDelegate({"invalid": [8.0, 9.0]})
    with ResumableEmbeddingService(
        valid_delegate,
        cache_path=cache_path,
        profile_id="profile-a",
        dimensions=2,
    ) as valid:
        assert valid.embed(["invalid"]) == [[8.0, 9.0]]

    assert valid_delegate.calls == [["invalid"]]
