from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_kb_sync_is_a_dedicated_healthcheck_free_maintenance_service() -> None:
    compose = yaml.safe_load(
        (
            PROJECT_ROOT
            / "deployment"
            / "compose"
            / "compose.services.yml"
        ).read_text(encoding="utf-8")
    )

    service = compose["services"]["kb_sync"]
    assert service["profiles"] == ["maintenance"]
    assert service["restart"] == "no"
    assert service["entrypoint"] == ["python", "scripts/sync_vectorstore.py"]
    assert service["command"] == []
    assert service["healthcheck"] == {"disable": True}
    assert service["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert "./backend:/app/backend:ro" in service["volumes"]
    assert "./scripts:/app/scripts:ro" in service["volumes"]
    assert "./data:/app/data" in service["volumes"]
    assert "ports" not in service


def test_sync_kb_wrapper_targets_only_the_dedicated_service() -> None:
    wrapper = (PROJECT_ROOT / "scripts" / "sync_kb.sh").read_text(
        encoding="utf-8"
    )

    assert 'compose.sh" run --rm kb_sync "$@"' in wrapper
    assert " backend " not in wrapper
