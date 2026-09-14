from __future__ import annotations

import os
from pathlib import Path

import yaml

from backend.business_config.loader import (
    COACH_DIMENSION_CONFIG_FILES,
    COACH_PROMPT_FILES,
    GUIDANCE_PROMPT_FILES,
    BusinessConfigLoader,
)
from backend.config.settings import Settings
from backend.services.prompt_service import PromptService


ROOT = Path(__file__).resolve().parents[1]


def _rewrite_with_new_mtime(path: Path, text: str) -> None:
    """Rewrite a fixture without relying on a wall-clock sleep for reload checks."""

    previous_mtime_ns = path.stat().st_mtime_ns if path.exists() else None
    path.write_text(text, encoding="utf-8")
    if previous_mtime_ns is None:
        return
    stat = path.stat()
    updated_mtime_ns = max(
        stat.st_mtime_ns,
        previous_mtime_ns + 1_000_000_000,
    )
    os.utime(
        path,
        ns=(stat.st_atime_ns, updated_mtime_ns),
    )


def test_prompt_service_hot_reloads_changed_template_on_same_instance(tmp_path: Path):
    template = tmp_path / "message.jinja2"
    template.write_text("before: {{ value }}", encoding="utf-8")
    prompts = PromptService(prompt_dir=tmp_path)

    assert prompts.render("message.jinja2", value="one") == "before: one"

    _rewrite_with_new_mtime(template, "after: {{ value }}")

    assert prompts.render("message.jinja2", value="two") == "after: two"


def test_prompt_service_hot_reloads_changed_include_on_same_instance(tmp_path: Path):
    entry = tmp_path / "entry.jinja2"
    included = tmp_path / "_included.jinja2"
    entry.write_text('result: {% include "_included.jinja2" %}', encoding="utf-8")
    included.write_text("included-v1", encoding="utf-8")
    prompts = PromptService(prompt_dir=tmp_path)

    assert prompts.render("entry.jinja2") == "result: included-v1"

    _rewrite_with_new_mtime(included, "included-v2")

    assert prompts.render("entry.jinja2") == "result: included-v2"


def test_prompt_versions_change_after_file_updates_without_manual_cache_clear(
    tmp_path: Path,
):
    config_dir = tmp_path / "business_config"
    coach_config_dir = config_dir / "coach"
    prompt_dir = tmp_path / "prompts"
    coach_prompt_dir = prompt_dir / "coach"
    guidance_prompt_dir = prompt_dir / "guidance"
    coach_config_dir.mkdir(parents=True)
    coach_prompt_dir.mkdir(parents=True)
    guidance_prompt_dir.mkdir(parents=True)

    for filename in COACH_DIMENSION_CONFIG_FILES:
        (coach_config_dir / filename).write_text("version: test-v1\n", encoding="utf-8")
    (config_dir / "query.yaml").write_text(
        "version: test-v1\ndefaults: {}\nqueries: {}\n",
        encoding="utf-8",
    )
    (config_dir / "personality_behavior_map.yaml").write_text(
        (ROOT / "backend/business_config/personality_behavior_map.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    for relative_path in {*COACH_PROMPT_FILES, *GUIDANCE_PROMPT_FILES}:
        path = prompt_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"initial prompt: {relative_path}\n", encoding="utf-8")

    loader = BusinessConfigLoader(
        config_dir=config_dir,
        prompt_dir=prompt_dir,
        auto_reload=True,
    )
    coach_v1 = loader.coach_version()
    guidance_v1 = loader.guidance_version()

    _rewrite_with_new_mtime(
        coach_prompt_dir / "start.jinja2",
        "updated coach prompt\n",
    )
    _rewrite_with_new_mtime(
        guidance_prompt_dir / "start.jinja2",
        "updated guidance prompt\n",
    )

    assert loader.coach_version() != coach_v1
    assert loader.guidance_version() != guidance_v1


def test_backend_mounts_prompts_read_only_and_enables_reload_by_default():
    compose = yaml.safe_load(
        (ROOT / "deployment/compose/compose.services.yml").read_text(encoding="utf-8")
    )
    backend = compose["services"]["backend"]

    assert "./backend/prompts:/app/backend/prompts:ro" in backend["volumes"]
    assert backend["environment"]["PROMPT_AUTO_RELOAD_ENABLED"] == (
        "${PROMPT_AUTO_RELOAD_ENABLED:-true}"
    )
    assert Settings.model_fields["prompt_auto_reload_enabled"].default is True
    assert "PROMPT_AUTO_RELOAD_ENABLED=true" in (
        ROOT / "backend/config/.env.example"
    ).read_text(encoding="utf-8")
