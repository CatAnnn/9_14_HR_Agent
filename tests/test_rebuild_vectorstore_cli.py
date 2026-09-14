from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.config.settings import Settings
from scripts import rebuild_vectorstore


class FakeIndexManager:
    instances: list["FakeIndexManager"] = []

    def __init__(self, *, settings):
        self.settings = settings
        self.last_summary = {"index_version": settings.kb_index_version}
        self.rebuild_calls: list[dict[str, object]] = []
        self.instances.append(self)

    def rebuild(
        self,
        raw_dir: Path | None = None,
        *,
        dataset: str = "all",
        exact: bool,
    ):
        self.rebuild_calls.append(
            {"raw_dir": raw_dir, "dataset": dataset, "exact": exact}
        )
        return [{"chunk_id": "chunk-1"}]


def _settings():
    return Settings(
        kb_chunk_size=2048,
        kb_chunk_overlap=160,
        kb_index_version="v3",
    )


@pytest.mark.parametrize(
    "flag",
    ["--vision-preflight-only", "--strict-images"],
)
def test_removed_document_vision_flags_fail_before_manager_creation(
    monkeypatch,
    flag,
):
    FakeIndexManager.instances.clear()
    monkeypatch.setattr(rebuild_vectorstore, "get_settings", _settings)
    monkeypatch.setattr(rebuild_vectorstore, "IndexManager", FakeIndexManager)

    with pytest.raises(SystemExit, match="processed Markdown"):
        rebuild_vectorstore.main([flag])

    assert FakeIndexManager.instances == []


def test_default_rebuild_reports_processed_pipeline(monkeypatch, capsys):
    FakeIndexManager.instances.clear()
    settings = _settings()
    monkeypatch.setattr(rebuild_vectorstore, "get_settings", lambda: settings)
    monkeypatch.setattr(rebuild_vectorstore, "IndexManager", FakeIndexManager)

    exit_code = rebuild_vectorstore.main([])

    manager = FakeIndexManager.instances[-1]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 0
    assert manager.rebuild_calls == [
        {"raw_dir": None, "dataset": "all", "exact": False}
    ]
    assert lines[0] == {
        "accepted_inputs": ["markdown", "organization_unit_xlsx"],
        "chunk_overlap": 160,
        "chunk_size": 2048,
        "dataset": "all",
        "event": "processed_kb_effective_config",
        "exact": False,
        "index_version": "v3",
        "raw_dir": str(settings.kb_core_dir),
        "source_roots": {
            "core": str(settings.kb_core_dir),
            "organization_unit": str(settings.kb_organization_unit_dir),
        },
    }
    assert lines[-1]["chunk_count"] == 1


def test_exact_flag_reaches_index_manager(monkeypatch, capsys):
    FakeIndexManager.instances.clear()
    monkeypatch.setattr(rebuild_vectorstore, "get_settings", _settings)
    monkeypatch.setattr(rebuild_vectorstore, "IndexManager", FakeIndexManager)

    exit_code = rebuild_vectorstore.main(["--exact"])

    manager = FakeIndexManager.instances[-1]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 0
    assert manager.rebuild_calls == [
        {"raw_dir": None, "dataset": "all", "exact": True}
    ]
    assert lines[0]["exact"] is True
    assert lines[-1]["chunk_count"] == 1


def test_organization_dataset_and_raw_dir_reach_index_manager(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    FakeIndexManager.instances.clear()
    monkeypatch.setattr(rebuild_vectorstore, "get_settings", _settings)
    monkeypatch.setattr(rebuild_vectorstore, "IndexManager", FakeIndexManager)
    raw_dir = tmp_path / "organization"

    exit_code = rebuild_vectorstore.main(
        [
            "--dataset",
            "organization_unit",
            "--raw-dir",
            str(raw_dir),
        ]
    )

    manager = FakeIndexManager.instances[-1]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 0
    assert manager.rebuild_calls == [
        {
            "raw_dir": raw_dir,
            "dataset": "organization_unit",
            "exact": False,
        }
    ]
    assert lines[0]["accepted_inputs"] == ["organization_unit_xlsx"]
    assert lines[0]["dataset"] == "organization_unit"
    assert lines[0]["raw_dir"] == str(raw_dir)
    assert lines[0]["source_roots"] == {
        "organization_unit": str(raw_dir)
    }


def test_rebuild_raw_dir_rejects_all_dataset_before_manager_creation(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    FakeIndexManager.instances.clear()
    monkeypatch.setattr(rebuild_vectorstore, "get_settings", _settings)
    monkeypatch.setattr(rebuild_vectorstore, "IndexManager", FakeIndexManager)

    with pytest.raises(SystemExit):
        rebuild_vectorstore.main(["--raw-dir", str(tmp_path / "knowledge")])

    assert FakeIndexManager.instances == []
    assert "--raw-dir cannot be used with --dataset all" in capsys.readouterr().err
