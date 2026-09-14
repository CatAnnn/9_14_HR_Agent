from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import sync_vectorstore


class FakeResumableEmbeddingService:
    instances: list["FakeResumableEmbeddingService"] = []

    def __init__(
        self,
        delegate,
        *,
        cache_path: Path,
        profile_id: str,
        dimensions: int,
        progress_callback=None,
    ) -> None:
        self.delegate = delegate
        self.cache_path = Path(cache_path)
        self.profile_id = profile_id
        self.dimensions = dimensions
        self.progress_callback = progress_callback
        self.cache_existed_at_init = self.cache_path.exists()
        self.stats = {
            "cache_hit_count": 2,
            "cache_miss_count": 1,
            "cache_write_count": 1,
        }
        self.closed = False
        self.instances.append(self)

    def embed(self, texts):
        return self.delegate.embed(texts)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class FakeEmbeddingDelegate:
    def embed(self, texts):
        return [[1.0, 2.0] for _ in texts]


class FakeIndexManager:
    instances: list["FakeIndexManager"] = []

    def __init__(
        self,
        *,
        settings,
        embedding_service=None,
        initialize_repository=True,
    ) -> None:
        self.settings = settings
        self.initialize_repository = initialize_repository
        self.constructor_embedding_service = embedding_service
        self.original_embedding_service = FakeEmbeddingDelegate()
        self.embedding_service = (
            embedding_service or self.original_embedding_service
        )
        self.sync_calls: list[dict[str, object]] = []
        self.check_calls: list[dict[str, object]] = []
        self.embedding_service_during_sync = None
        self.instances.append(self)

    def sync_changed(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
    ) -> dict:
        self.sync_calls.append({"raw_dir": raw_dir, "dataset": dataset})
        self.embedding_service_during_sync = self.embedding_service
        return {
            "changed_document_count": 1,
            "unchanged_document_count": 2,
            "vector_count": 3,
        }

    def check_sources(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
    ) -> dict:
        self.check_calls.append({"raw_dir": raw_dir, "dataset": dataset})
        configured_roots = {
            "core": self.settings.kb_core_dir,
            "organization_unit": self.settings.kb_organization_unit_dir,
        }
        datasets = (
            ["core", "organization_unit"] if dataset == "all" else [dataset]
        )
        source_roots = (
            {dataset: str(raw_dir)}
            if raw_dir is not None
            else {name: str(configured_roots[name]) for name in datasets}
        )
        return {
            "dataset": dataset,
            "datasets": datasets,
            "source_roots": source_roots,
            "source_count": 0,
            "sources": [],
        }


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_dir=tmp_path / "runtime",
        data_dir=tmp_path / "data",
        kb_core_dir=tmp_path / "data" / "kb_raw",
        kb_organization_unit_dir=tmp_path / "data" / "kb_large",
        embedding_profile_id="profile-a",
        effective_embedding_dimensions=2,
        embedding_dimensions=2,
        effective_embedding_model="embedding-model",
        kb_ingest_batch_size=2,
    )


def _install_fakes(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    settings = _settings(tmp_path)
    FakeIndexManager.instances.clear()
    FakeResumableEmbeddingService.instances.clear()
    monkeypatch.setattr(sync_vectorstore, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_vectorstore, "IndexManager", FakeIndexManager)
    monkeypatch.setattr(
        sync_vectorstore,
        "EmbeddingService",
        lambda *, settings: FakeEmbeddingDelegate(),
    )
    monkeypatch.setattr(
        sync_vectorstore,
        "ResumableEmbeddingService",
        FakeResumableEmbeddingService,
    )
    return settings


def _json_lines(capsys) -> list[dict]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]


def test_default_cache_path_wraps_sync_and_reports_serializable_stats(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    settings = _install_fakes(monkeypatch, tmp_path)

    exit_code = sync_vectorstore.main([])

    manager = FakeIndexManager.instances[-1]
    cache = FakeResumableEmbeddingService.instances[-1]
    lines = _json_lines(capsys)
    expected_cache_path = settings.runtime_dir / "kb_embedding_cache.sqlite3"

    assert exit_code == 0
    assert isinstance(cache.delegate, FakeEmbeddingDelegate)
    assert cache.cache_path == expected_cache_path
    assert cache.profile_id == "profile-a"
    assert cache.dimensions == 2
    assert manager.embedding_service_during_sync is cache
    assert manager.sync_calls == [{"raw_dir": None, "dataset": "all"}]
    assert cache.closed is True
    assert lines[0]["cache_path"] == str(expected_cache_path)
    assert lines[0]["dataset"] == "all"
    assert lines[0]["source_roots"] == {
        "core": str(settings.kb_core_dir),
        "organization_unit": str(settings.kb_organization_unit_dir),
    }
    assert lines[0]["resume_enabled"] is True
    assert lines[-1]["changed_document_count"] == 1
    assert lines[-1]["cache_stats"] == {
        "cache_hit_count": 2,
        "cache_miss_count": 1,
        "cache_write_count": 1,
    }


def test_explicit_raw_dir_and_no_resume_preserve_existing_cache(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    _install_fakes(monkeypatch, tmp_path)
    cache_path = tmp_path / "custom" / "cache.sqlite3"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("stale-cache", encoding="utf-8")
    raw_dir = tmp_path / "knowledge"

    exit_code = sync_vectorstore.main(
        [
            "--cache-path",
            str(cache_path),
            "--no-resume",
            "--raw-dir",
            str(raw_dir),
            "--dataset",
            "core",
        ]
    )

    manager = FakeIndexManager.instances[-1]
    lines = _json_lines(capsys)

    assert exit_code == 0
    assert FakeResumableEmbeddingService.instances == []
    assert cache_path.read_text(encoding="utf-8") == "stale-cache"
    assert manager.sync_calls == [{"raw_dir": raw_dir, "dataset": "core"}]
    assert lines[0]["cache_path"] == str(cache_path)
    assert lines[0]["raw_dir"] == str(raw_dir)
    assert lines[0]["resume_enabled"] is False
    assert lines[0]["dataset"] == "core"
    assert lines[0]["source_roots"] == {"core": str(raw_dir)}
    assert lines[-1]["cache_stats"]["enabled"] is False
    assert lines[-1]["cache_stats"]["cache_write_count"] == 0


def test_check_only_lists_sources_without_embedding_or_sync(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    settings = _install_fakes(monkeypatch, tmp_path)

    def unexpected_service(*args, **kwargs):
        del args, kwargs
        raise AssertionError("embedding services must not be created in check mode")

    monkeypatch.setattr(sync_vectorstore, "EmbeddingService", unexpected_service)
    monkeypatch.setattr(
        sync_vectorstore,
        "ResumableEmbeddingService",
        unexpected_service,
    )

    exit_code = sync_vectorstore.main(
        ["--check", "--dataset", "organization_unit"]
    )

    manager = FakeIndexManager.instances[-1]
    lines = _json_lines(capsys)
    assert exit_code == 0
    assert manager.sync_calls == []
    assert manager.check_calls == [
        {"raw_dir": None, "dataset": "organization_unit"}
    ]
    assert manager.initialize_repository is False
    assert isinstance(
        manager.constructor_embedding_service,
        sync_vectorstore._CheckOnlyEmbeddingService,
    )
    assert FakeResumableEmbeddingService.instances == []
    assert lines[0]["check_only"] is True
    assert lines[0]["resume_enabled"] is False
    assert lines[0]["cache_path"] is None
    assert lines[0]["source_roots"] == {
        "organization_unit": str(settings.kb_organization_unit_dir)
    }
    assert lines[-1]["event"] == "kb_source_check_complete"
    assert lines[-1]["dataset"] == "organization_unit"


def test_raw_dir_requires_a_single_dataset_before_services_are_created(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    _install_fakes(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        sync_vectorstore.main(["--raw-dir", str(tmp_path / "knowledge")])

    assert FakeIndexManager.instances == []
    assert FakeResumableEmbeddingService.instances == []
    assert "--raw-dir cannot be used with --dataset all" in capsys.readouterr().err
