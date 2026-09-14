from __future__ import annotations

import json
import math
import os
from urllib.parse import urlparse

from backend.config.settings import get_settings


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold()


def _is_https(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme.casefold() == "https" and bool(parsed.hostname)


def _positive_live_cost_ceiling(errors: list[str]) -> float:
    raw_value = os.getenv("LOADTEST_MAX_COST_CNY", "")
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        errors.append("live mode requires a positive LOADTEST_MAX_COST_CNY")
    return value


def validate_environment() -> dict[str, object]:
    settings = get_settings()
    database_name = urlparse(settings.admin_database_url).path.strip("/").casefold()
    model_mode = os.getenv("LOADTEST_MODEL_MODE", "stub").strip().casefold()
    errors: list[str] = []
    if settings.environment != "loadtest":
        errors.append("ENVIRONMENT must be loadtest")
    if "loadtest" not in database_name:
        errors.append("DATABASE_ADMIN_URL database name must contain loadtest")
    if _host(settings.admin_database_url) != "postgres":
        errors.append("DATABASE_ADMIN_URL must target isolated service postgres")
    if _host(settings.database_url) != "pgbouncer":
        errors.append("DATABASE_URL must target isolated service pgbouncer")
    if _host(settings.redis_url) != "redis":
        errors.append("REDIS_URL must target isolated service redis")
    if settings.model_provider_mode != "platform":
        errors.append("MODEL_PROVIDER_MODE must be platform")
    if settings.asr_enabled or settings.tts_enabled:
        errors.append("ASR and TTS must be disabled in the text workflow stack")
    if model_mode == "stub":
        if _host(settings.chat_api_endpoint) != "model_stub":
            errors.append("CHAT_API_ENDPOINT must target model_stub in stub mode")
    elif model_mode == "live":
        if os.getenv("LOADTEST_ALLOW_LIVE_MODELS", "").strip().casefold() != "true":
            errors.append("live mode requires LOADTEST_ALLOW_LIVE_MODELS=true")
        _positive_live_cost_ceiling(errors)
        live_endpoints = {
            "CHAT_API_ENDPOINT": settings.chat_api_endpoint,
            "EMBEDDING_API_ENDPOINT": settings.embedding_api_endpoint,
            "RERANK_API_ENDPOINT": settings.rerank_api_endpoint,
        }
        for name, endpoint in live_endpoints.items():
            if _host(endpoint) == "model_stub":
                errors.append(f"{name} must not target model_stub in live mode")
            if not _is_https(endpoint):
                errors.append(f"{name} must be an HTTPS endpoint in live mode")
    else:
        errors.append("LOADTEST_MODEL_MODE must be stub or live")
    if errors:
        raise RuntimeError("; ".join(errors))
    return {
        "event": "loadtest_environment_validated",
        "database": database_name,
        "model_mode": model_mode,
        "model_host": _host(settings.chat_api_endpoint),
        "asr_enabled": settings.asr_enabled,
        "tts_enabled": settings.tts_enabled,
    }


if __name__ == "__main__":
    print(json.dumps(validate_environment(), sort_keys=True))
