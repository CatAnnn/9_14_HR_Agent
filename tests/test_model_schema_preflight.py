from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.preflight_model_schema import run_schema_preflight


@pytest.mark.asyncio
async def test_schema_preflight_checks_primary_and_retry_without_weak_fallback() -> None:
    calls: list[dict] = []

    class Service:
        async def ainvoke_structured_single(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(status="ok")

    report = await run_schema_preflight(
        service=Service(),
        models=[("primary", "primary-model"), ("retry", "deepseek-v4-pro")],
        attempts=3,
        timeout_seconds=15,
    )

    assert report["passed"] is True
    assert len(calls) == 6
    assert {call["model"] for call in calls} == {
        "primary-model",
        "deepseek-v4-pro",
    }
    assert all(call["structured_transport"] == "json_schema" for call in calls)
    assert all(call["json_schema_strict"] is False for call in calls)
    assert all(call["stream"] is False for call in calls)


@pytest.mark.asyncio
async def test_schema_preflight_blocks_release_after_any_failure() -> None:
    call_count = 0

    class Error(RuntimeError):
        code = "response_format_unavailable"

    class Service:
        async def ainvoke_structured_single(self, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Error("unsupported")
            return SimpleNamespace(status="ok")

    report = await run_schema_preflight(
        service=Service(),
        models=[("primary", "primary-model"), ("retry", "deepseek-v4-pro")],
        attempts=2,
        timeout_seconds=15,
    )

    assert report["passed"] is False
    assert report["results"][0]["failed"] == 1
    assert report["results"][0]["failures"] == [
        {"attempt": 2, "error_code": "response_format_unavailable"}
    ]
    assert report["results"][1]["failed"] == 0
