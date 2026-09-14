from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from loadtests import validate_environment as environment_guard


LIVE_OVERRIDE = Path("loadtests/compose.live.yml")


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "environment": "loadtest",
        "admin_database_url": (
            "postgresql://hr_agent:hr_agent@postgres:7214/hr_agent_loadtest"
        ),
        "database_url": (
            "postgresql://hr_agent:hr_agent@pgbouncer:6432/hr_agent_loadtest"
        ),
        "redis_url": "redis://redis:6379/0",
        "chat_api_endpoint": "https://models.example.test/v1/chat/completions",
        "embedding_api_endpoint": "https://models.example.test/v1/embeddings",
        "rerank_api_endpoint": "https://models.example.test/v1/rerank",
        "model_provider_mode": "platform",
        "asr_enabled": False,
        "tts_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_live_compose_override_allowlists_only_model_configuration() -> None:
    compose = yaml.safe_load(LIVE_OVERRIDE.read_text(encoding="utf-8"))
    live_environment = compose["x-live-model-environment"]
    generator_environment = compose["x-live-load-generator-environment"]

    assert live_environment["LOADTEST_MODEL_MODE"] == "live"
    assert generator_environment["LOADTEST_MODEL_MODE"] == "live"
    assert live_environment["MODEL_PROVIDER_MODE"] == "platform"
    for required in ("LOADTEST_ALLOW_LIVE_MODELS", "LOADTEST_MAX_COST_CNY"):
        assert ":?" in live_environment[required]
        assert ":?" in generator_environment[required]

    for secret in ("API_KEY", "OAUTH2_CLIENT_SECRET"):
        value = live_environment[secret]
        assert value.startswith("${") and value.endswith("}")

    assert live_environment["LLM_PRICING_VERSION"] == (
        "${LLM_PRICING_VERSION:?backend/config/.env must define LLM_PRICING_VERSION}"
    )
    assert live_environment["LLM_MODEL_PRICING"] == (
        "${LLM_MODEL_PRICING:?backend/config/.env must define LLM_MODEL_PRICING}"
    )

    assert live_environment["HTTP_PROXY"] == (
        "${LOADTEST_LIVE_HTTP_PROXY:-http://host.docker.internal:3128}"
    )
    assert live_environment["HTTPS_PROXY"] == (
        "${LOADTEST_LIVE_HTTPS_PROXY:-http://host.docker.internal:3128}"
    )
    assert "qwen3_asr_secondary" in live_environment["NO_PROXY"]
    for service_name in ("migrate", "kb_seed", "backend"):
        assert compose["services"][service_name]["extra_hosts"] == [
            "host.docker.internal:host-gateway"
        ]

    forbidden = {
        "DATABASE_URL",
        "DATABASE_ADMIN_URL",
        "REDIS_URL",
        "RUNTIME_DATA_DIR",
        "ASR_LOCAL_STREAMING_URL",
        "ASR_LOCAL_STREAMING_URLS",
    }
    assert forbidden.isdisjoint(live_environment)
    assert set(compose["services"]) == {
        "migrate",
        "kb_seed",
        "backend",
        "locust_master",
        "locust_worker",
        "asr_load",
    }


def test_live_environment_guard_requires_authorization_and_positive_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_guard, "get_settings", _settings)
    monkeypatch.setenv("LOADTEST_MODEL_MODE", "live")
    monkeypatch.setenv("LOADTEST_ALLOW_LIVE_MODELS", "false")
    monkeypatch.setenv("LOADTEST_MAX_COST_CNY", "0")
    with pytest.raises(RuntimeError) as exc_info:
        environment_guard.validate_environment()
    message = str(exc_info.value)
    assert "LOADTEST_ALLOW_LIVE_MODELS=true" in message
    assert "positive LOADTEST_MAX_COST_CNY" in message


def test_live_environment_guard_preserves_data_isolation_and_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        environment_guard,
        "get_settings",
        lambda: _settings(
            database_url="postgresql://hr_agent:hr_agent@production-db:5432/hr_agent",
            embedding_api_endpoint="http://models.example.test/v1/embeddings",
        ),
    )
    monkeypatch.setenv("LOADTEST_MODEL_MODE", "live")
    monkeypatch.setenv("LOADTEST_ALLOW_LIVE_MODELS", "true")
    monkeypatch.setenv("LOADTEST_MAX_COST_CNY", "10")
    with pytest.raises(RuntimeError) as exc_info:
        environment_guard.validate_environment()
    message = str(exc_info.value)
    assert "DATABASE_URL must target isolated service pgbouncer" in message
    assert "EMBEDDING_API_ENDPOINT must be an HTTPS endpoint" in message


def test_live_environment_guard_accepts_safe_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment_guard, "get_settings", _settings)
    monkeypatch.setenv("LOADTEST_MODEL_MODE", "live")
    monkeypatch.setenv("LOADTEST_ALLOW_LIVE_MODELS", "true")
    monkeypatch.setenv("LOADTEST_MAX_COST_CNY", "10")
    result = environment_guard.validate_environment()
    assert result["model_mode"] == "live"
    assert result["database"] == "hr_agent_loadtest"
