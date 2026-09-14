from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from backend.services import psychological_pattern_catalog as catalog_module
from backend.services.psychological_pattern_catalog import (
    PsychologicalPatternCatalogLoader,
)


def _write_catalog(path: Path, payload: object) -> None:
    previous_mtime_ns = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stat = path.stat()
    changed_mtime_ns = max(time.time_ns(), previous_mtime_ns + 1, stat.st_mtime_ns + 1)
    os.utime(path, ns=(stat.st_atime_ns, changed_mtime_ns))


def _pattern(name: object, *, description: object = "描述") -> dict[str, object]:
    return {
        "construct_name": name,
        "description": description,
        "core_mechanisms": "机制",
        "real_world_manifestation": "表现",
    }


def test_catalog_uses_business_config_file_and_preserves_four_fields_without_classification(
    tmp_path,
    monkeypatch,
):
    business_config_dir = tmp_path / "backend" / "business_config"
    business_config_dir.mkdir(parents=True)
    source = business_config_dir / "patterns_data.json"
    raw_pattern = _pattern("  Unchanged Name  ", description=["原始", {"层级": 1}])
    raw_pattern["unrelated_extra_field"] = "不进入渲染结构"
    _write_catalog(source, {"Mixed CASE id": raw_pattern})
    _write_catalog(
        tmp_path / "patterns_data.json",
        {"legacy-root-pattern": _pattern("legacy-root-pattern")},
    )
    monkeypatch.setattr(
        catalog_module,
        "get_settings",
        lambda: SimpleNamespace(business_config_dir=business_config_dir),
    )

    snapshot = PsychologicalPatternCatalogLoader().snapshot()

    assert snapshot.available is True
    assert snapshot.source_mtime_ns == source.stat().st_mtime_ns
    assert snapshot.catalog_version is not None
    assert snapshot.valid_pattern_ids == frozenset({"Mixed CASE id"})
    assert snapshot.render_payload() == [
        {
            "pattern_id": "Mixed CASE id",
            "construct_name": "  Unchanged Name  ",
            "description": ["原始", {"层级": 1}],
            "core_mechanisms": "机制",
            "real_world_manifestation": "表现",
        }
    ]


def test_catalog_does_not_reject_missing_fields_or_non_mapping_entries(tmp_path):
    source = tmp_path / "patterns_data.json"
    _write_catalog(
        source,
        {
            "partial": {"construct_name": "partial"},
            "source_owned": "未分类的源内容",
        },
    )

    snapshot = PsychologicalPatternCatalogLoader(source).snapshot()

    assert snapshot.valid_pattern_ids == frozenset({"partial", "source_owned"})
    assert snapshot.render_payload() == [
        {
            "pattern_id": "partial",
            "construct_name": "partial",
            "description": None,
            "core_mechanisms": None,
            "real_world_manifestation": None,
        },
        {
            "pattern_id": "source_owned",
            "construct_name": None,
            "description": None,
            "core_mechanisms": None,
            "real_world_manifestation": None,
        },
    ]


def test_catalog_hot_reloads_and_keeps_last_known_good_snapshot(tmp_path):
    source = tmp_path / "patterns_data.json"
    _write_catalog(source, {"first": _pattern("first")})
    loader = PsychologicalPatternCatalogLoader(source)
    first = loader.snapshot()

    source.write_text("{broken", encoding="utf-8")
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    assert loader.snapshot() is first

    _write_catalog(source, {"second": _pattern("second")})
    second = loader.snapshot()

    assert second is not first
    assert second.available is True
    assert second.catalog_version != first.catalog_version
    assert second.valid_pattern_ids == frozenset({"second"})
    assert second.render_payload()[0]["construct_name"] == "second"


def test_catalog_first_load_failure_is_empty_and_later_creation_recovers(tmp_path):
    source = tmp_path / "patterns_data.json"
    loader = PsychologicalPatternCatalogLoader(source)

    missing = loader.snapshot()

    assert missing.available is False
    assert missing.source_mtime_ns is None
    assert missing.catalog_version is None
    assert missing.render_patterns == "[]"
    assert missing.valid_pattern_ids == frozenset()

    _write_catalog(source, {"created": _pattern("created")})

    assert loader.snapshot().valid_pattern_ids == frozenset({"created"})


def test_successfully_loaded_empty_catalog_is_available(tmp_path):
    source = tmp_path / "patterns_data.json"
    _write_catalog(source, {})

    snapshot = PsychologicalPatternCatalogLoader(source).snapshot()

    assert snapshot.available is True
    assert snapshot.source_mtime_ns == source.stat().st_mtime_ns
    assert snapshot.catalog_version is not None
    assert snapshot.render_patterns == "[]"
    assert snapshot.valid_pattern_ids == frozenset()


def test_render_payload_is_a_fresh_copy(tmp_path):
    source = tmp_path / "patterns_data.json"
    _write_catalog(source, {"stable": _pattern("stable", description=["original"])})
    snapshot = PsychologicalPatternCatalogLoader(source).snapshot()

    first_payload = snapshot.render_payload()
    first_payload[0]["description"].append("mutated")

    assert snapshot.render_payload()[0]["description"] == ["original"]


def test_catalog_serializes_concurrent_sync_and_async_reload(tmp_path, monkeypatch):
    source = tmp_path / "patterns_data.json"
    _write_catalog(source, {"shared": _pattern("shared")})
    loader = PsychologicalPatternCatalogLoader(source)
    original_load = loader._load_snapshot
    counter_lock = threading.Lock()
    load_count = 0

    def counted_load(revision):
        nonlocal load_count
        with counter_lock:
            load_count += 1
        time.sleep(0.02)
        return original_load(revision)

    monkeypatch.setattr(loader, "_load_snapshot", counted_load)

    with ThreadPoolExecutor(max_workers=12) as executor:
        snapshots = list(executor.map(lambda _: loader.snapshot(), range(24)))

    assert load_count == 1
    assert all(snapshot is snapshots[0] for snapshot in snapshots)

    _write_catalog(source, {"async": _pattern("async")})

    async def load_concurrently():
        return await asyncio.gather(*(loader.asnapshot() for _ in range(24)))

    async_snapshots = asyncio.run(load_concurrently())

    assert load_count == 2
    assert all(snapshot is async_snapshots[0] for snapshot in async_snapshots)
    assert async_snapshots[0].valid_pattern_ids == frozenset({"async"})


def _project_catalog() -> dict[str, dict[str, str]]:
    source = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "business_config"
        / "patterns_data.json"
    )
    return json.loads(source.read_text(encoding="utf-8"))


def _combined_pattern_text(pattern: dict[str, str]) -> str:
    return "\n".join(
        pattern[field]
        for field in (
            "description",
            "core_mechanisms",
            "real_world_manifestation",
        )
    )


def test_project_catalog_has_no_repeated_long_policy_paragraphs():
    catalog = _project_catalog()
    paragraphs = [
        normalized
        for pattern in catalog.values()
        for field in (
            "description",
            "core_mechanisms",
            "real_world_manifestation",
        )
        for paragraph in pattern[field].split("\n\n")
        if len(normalized := " ".join(paragraph.split())) >= 160
    ]

    duplicates = {
        paragraph: count
        for paragraph, count in Counter(paragraphs).items()
        if count > 1
    }

    assert duplicates == {}


def test_project_catalog_corrects_known_type_and_evidence_mismatches():
    catalog = _project_catalog()
    banned_phrases = {
        "withdrawn": ("相对稳定的行为倾向",),
        "decision fatigue": (
            "更容易出现的认知偏差",
            "核心是有限的注意与自我调节资源被持续消耗",
        ),
        "groupthink": ("更容易出现的认知偏差",),
        "employee voice": (
            "主要描述员工与经理、团队或组织之间形成的关系状态",
        ),
        "job crafting": ("主要描述岗位、任务或工作系统的结构特征",),
        "meaningful work": (
            "主要描述长期工作压力、情绪调节或工作体验形成的状态与过程",
        ),
        "organizational citizenship behavior": (
            "至少两名成员或一个团队系统才能完整观察",
        ),
        "counterproductive work behavior": (
            "至少两名成员或一个团队系统才能完整观察",
        ),
        "antagonistic": ("自我利益权重", "默认善意假设"),
        "careless": ("该特质表现为较少核对细节",),
        "cold": ("低关系温度与低情感性表达",),
        "irresponsible": ("责任内化",),
        "quarrelsome": ("言语对抗倾向",),
        "rash": ("即时行动/收益权重",),
        "timid": ("对社会评价和潜在负面后果更敏感",),
        "touchy": ("更容易被解释为针对自身或不尊重",),
        "undependable": ("该特质表现为承诺兑现和行为一致性较低",),
    }

    for pattern_id, phrases in banned_phrases.items():
        text = _combined_pattern_text(catalog[pattern_id])
        assert all(phrase not in text for phrase in phrases)

    actor_observer = _combined_pattern_text(catalog["actor observer asymmetry"])
    assert "情境依赖" in actor_observer
    assert "研究并不支持它是普遍、固定" in actor_observer

    decision_fatigue = _combined_pattern_text(catalog["decision fatigue"])
    assert "定义和机制仍存在研究争议" in decision_fatigue
    assert "只是可能解释之一" in decision_fatigue

    flow = _combined_pattern_text(catalog["flow principle"])
    assert "不是进入心流的充分保证" in flow

    post_traumatic_growth = _combined_pattern_text(catalog["post-traumatic growth"])
    assert "真正具有创伤性的重大事件" in post_traumatic_growth
    assert "不是一般项目失败、普通职业挫折的通用标签" in post_traumatic_growth

    growth_mindset = _combined_pattern_text(catalog["growth mindset at work"])
    assert "干预效果较小且差异较大" in growth_mindset

    burnout = _combined_pattern_text(catalog["burnout"])
    assert "知识库不用于医学诊断" in burnout
    assert "诊断时应" not in burnout


def test_project_catalog_keeps_ids_and_four_field_interface():
    catalog = _project_catalog()
    expected_fields = {
        "construct_name",
        "description",
        "core_mechanisms",
        "real_world_manifestation",
    }

    assert {"irresponsible", "careless", "cold"} <= catalog.keys()
    assert all(set(pattern) == expected_fields for pattern in catalog.values())
