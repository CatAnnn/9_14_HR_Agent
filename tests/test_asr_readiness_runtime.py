from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from backend.api.routes import asr as asr_route
from backend.config.settings import Settings
from backend.core.auth_dependency import get_current_user
from backend.services.local_model_runtime import LocalModelRuntimeRecycler
from backend.services.realtime_asr_service import (
    clear_asr_readiness_cache,
    get_asr_readiness,
)


def test_qwen3_asr_is_pinned_while_other_local_models_can_recycle(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="local",
            local_model_auto_recycle_enabled=True,
            local_model_pinned_services="qwen3_asr",
            local_model_idle_ttl_seconds=1,
        )
    )
    stopped: list[str] = []
    monkeypatch.setattr(recycler, "_stop_service", lambda service: stopped.append(service) or True)
    recycler._last_used_at["qwen3_asr"] = 0
    recycler._last_used_at["qwen_embedding"] = 0

    recycler._stop_if_idle("qwen3_asr")
    recycler._stop_if_idle("qwen_embedding")

    assert stopped == ["qwen_embedding"]


def test_realtime_asr_is_required_resident_even_if_pinned_env_omits_it():
    recycler = LocalModelRuntimeRecycler(
        Settings(
            asr_enabled=True,
            asr_realtime_enabled=True,
            local_model_auto_recycle_enabled=True,
            local_model_pinned_services="",
        )
    )

    assert recycler.required_resident_services == {"qwen3_asr"}
    assert "qwen3_asr" in recycler.pinned_services


def test_realtime_asr_warms_when_generic_auto_start_is_disabled(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            asr_enabled=True,
            asr_realtime_enabled=True,
            local_model_auto_recycle_enabled=False,
            local_model_auto_start_enabled=False,
            local_model_pinned_services="",
        )
    )
    scheduled: list[str] = []
    monkeypatch.setattr(recycler, "ensure_resident", scheduled.append)

    recycler.start()

    assert scheduled == ["qwen3_asr"]


def test_disabled_realtime_asr_does_not_force_residency():
    recycler = LocalModelRuntimeRecycler(
        Settings(
            asr_enabled=True,
            asr_realtime_enabled=False,
            local_model_pinned_services="",
        )
    )

    assert recycler.required_resident_services == set()
    assert "qwen3_asr" not in recycler.pinned_services


def test_resident_warmup_is_single_flight(monkeypatch):
    recycler = LocalModelRuntimeRecycler(
        Settings(asr_enabled=True, asr_realtime_enabled=True)
    )
    starts: list[str] = []

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            assert target == recycler._warm_pinned_service
            assert args == ("qwen3_asr",)
            assert daemon is True
            self.name = name

        def start(self):
            starts.append(self.name)

    monkeypatch.setattr(
        "backend.services.local_model_runtime.threading.Thread",
        Thread,
    )

    assert recycler.ensure_resident("qwen3_asr") is True
    assert recycler.ensure_resident("qwen3_asr") is False
    assert starts == ["local-model-pinned-qwen3_asr"]


def test_asr_performance_defaults_are_bounded():
    settings = Settings(
        local_model_pinned_services="qwen3_asr",
        tts_max_concurrency=4,
    )

    assert settings.local_model_pinned_service_set == {"qwen3_asr"}
    assert settings.asr_local_preview_max_concurrency == 10
    assert settings.asr_local_chunk_ms == 500
    assert settings.asr_local_stream_soft_window_seconds == 12
    assert settings.asr_local_stream_window_seconds == 20
    assert settings.asr_local_stream_overlap_seconds == 1.5
    assert settings.asr_local_silence_commit_ms == 750
    assert settings.asr_local_force_refresh_ms == 1000
    assert settings.asr_local_silence_rms_threshold == 0.006
    assert settings.asr_local_request_timeout_seconds == 0
    assert settings.asr_local_admission_timeout_seconds == 2
    assert settings.asr_local_routing_probe_timeout_seconds == 0.25
    assert settings.asr_local_language_auto_detect is True
    assert settings.asr_capture_max_connections == 0
    assert settings.asr_local_final_enabled is True
    assert settings.asr_local_final_segment_seconds == 10
    assert settings.asr_local_final_per_recording_concurrency == 2
    assert settings.asr_remote_fallback_on_stream_failure is True
    assert settings.asr_final_segment_seconds == 45
    assert settings.asr_final_per_recording_concurrency == 8
    assert settings.tts_max_concurrency == 4
    assert settings.tts_queue_timeout_seconds == 0


def test_zero_capture_limit_disables_user_admission_cap():
    asr_route._capture_slots = None
    asr_route._capture_slots_size = 0

    assert asr_route._get_capture_slots(0) is None
    assert asr_route._get_capture_slots(4) is not None

    asr_route._capture_slots = None
    asr_route._capture_slots_size = 0


def test_gpu_zero_service_budgets_leave_runtime_headroom():
    compose = Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )

    fish_config = Path("fish_tts/config/fish_s2_pro_dynamic_batch.yaml").read_text(
        encoding="utf-8"
    )
    assert '${QWEN_EMBEDDING_GPU_MEMORY_UTILIZATION:-0.20}' in compose
    assert '${QWEN3_ASR_GPU_MEMORY_UTILIZATION:-0.17}' in compose
    assert "gpu_memory_utilization: 0.45" in fish_config
    assert "gpu_memory_utilization: 0.05" in fish_config
    assert '${ASR_LOCAL_STREAM_SOFT_WINDOW_SECONDS:-12}' in compose
    assert '${ASR_LOCAL_STREAM_WINDOW_SECONDS:-20}' in compose
    assert '${ASR_LOCAL_PREVIEW_MAX_SESSIONS:-16}' in compose
    assert '${ASR_LOCAL_ADMISSION_TIMEOUT_SECONDS:-2}' in compose
    assert (
        '${ASR_LOCAL_LANGUAGE_AUTO_DETECT:-${ASR_LOCAL_ZH_EN_AUTO_DETECT:-true}}'
        in compose
    )
    assert '${ASR_STREAM_UNFIXED_TOKEN_NUM:-64}' in compose

@pytest.mark.asyncio
async def test_asr_readiness_reports_live_capacity(monkeypatch):
    primary = "http://qwen3_asr:7118"
    secondary = "http://qwen3_asr_secondary:7118"
    capacities = {
        f"{primary}/health": (3, 2),
        f"{secondary}/health": (1, 1),
    }

    class Response:
        status_code = 200

        def __init__(self, active_sessions: int, queue_depth: int):
            self.active_sessions = active_sessions
            self.queue_depth = queue_depth

        @staticmethod
        def raise_for_status():
            return None

        def json(self):
            return {
                "status": "ok",
                "active_sessions": self.active_sessions,
                "max_sessions": 0,
                "max_batch_size": 11,
                "admission_mode": "unbounded",
                "queue_depth": self.queue_depth,
                "window_seconds": 12,
                "streaming_mode": "native_incremental_batch",
                "stream_chunk_seconds": 1.0,
            }

    class Client:
        async def get(self, url, **kwargs):
            assert url in capacities
            assert kwargs["timeout"] is not None
            return Response(*capacities[url])

    monkeypatch.setattr(
        "backend.services.realtime_asr_service.get_shared_async_client",
        lambda pool: Client(),
    )
    settings = Settings(
        asr_local_streaming_url=primary,
        asr_local_streaming_urls=f"{primary},{secondary}",
    )
    clear_asr_readiness_cache()
    result = await get_asr_readiness(settings)

    assert result["status"] == "ready"
    assert result["active_preview_sessions"] == 4
    assert result["max_preview_sessions"] == 0
    assert result["max_inference_batch_size"] == 11
    assert result["total_inference_capacity"] == 22
    assert result["model_instances"] == 2
    assert result["ready_model_instances"] == 2
    assert result["admission_mode"] == "unbounded"
    assert result["queue_depth"] == 3
    assert result["window_seconds"] == 12
    assert result["streaming_mode"] == "native_incremental_batch"
    assert result["stream_chunk_seconds"] == 1.0


@pytest.mark.asyncio
async def test_asr_readiness_reports_warming_during_model_start(monkeypatch):
    class Client:
        async def get(self, _url, **_kwargs):
            request = httpx.Request("GET", "http://qwen3_asr:7118/health")
            raise httpx.ConnectError("warming", request=request)

    monkeypatch.setattr(
        "backend.services.realtime_asr_service.get_shared_async_client",
        lambda pool: Client(),
    )
    clear_asr_readiness_cache()
    result = await get_asr_readiness(Settings())

    assert result["status"] == "warming"
    assert result["active_preview_sessions"] == 0


@pytest.mark.asyncio
async def test_browser_asr_readiness_uses_no_server_model(monkeypatch):
    monkeypatch.setattr(
        "backend.services.realtime_asr_service.get_shared_async_client",
        lambda _pool: pytest.fail("browser ASR must not call a server model"),
    )
    clear_asr_readiness_cache()

    result = await get_asr_readiness(
        Settings(asr_provider_mode="browser"),
        force_refresh=True,
    )

    assert result["status"] == "ready"
    assert result["provider"] == "browser"
    assert result["model"] == "browser-web-speech"
    assert result["streaming_mode"] == "browser_native"


@pytest.mark.asyncio
async def test_bosch_asr_readiness_reports_cumulative_platform_mode():
    clear_asr_readiness_cache()

    result = await get_asr_readiness(
        Settings(
            asr_provider_mode="bosch",
            asr_http_url="https://model.example/audio/transcriptions",
            asr_http_model="qwen3-asr-flash",
            asr_api_key="test-key",
            asr_bosch_preview_chunk_seconds=1,
            asr_bosch_first_preview_chunk_seconds=0.8,
            asr_bosch_accumulation_max_seconds=20,
            asr_bosch_preview_max_concurrency=8,
            asr_bosch_preview_per_session_concurrency=2,
            asr_bosch_global_max_concurrency=16,
        ),
        force_refresh=True,
    )

    assert result["status"] == "ready"
    assert result["provider"] == "bosch"
    assert result["streaming_mode"] == "cumulative_segment"
    assert result["stream_chunk_seconds"] == 1
    assert result["first_stream_chunk_seconds"] == 0.8
    assert result["accumulation_max_seconds"] == 20
    assert result["max_inference_batch_size"] == 16
    assert result["per_session_preview_concurrency"] == 2


@pytest.mark.asyncio
async def test_asr_readiness_route_reasserts_residency(monkeypatch):
    settings = Settings(asr_enabled=True, asr_realtime_enabled=True)
    ensured: list[str] = []

    class Recycler:
        def ensure_resident(self, service_name: str) -> None:
            ensured.append(service_name)

    async def readiness(received_settings):
        assert received_settings is settings
        return {"status": "warming"}

    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        asr_route,
        "get_local_model_runtime_recycler",
        lambda: Recycler(),
    )
    monkeypatch.setattr(asr_route, "get_asr_readiness", readiness)

    result = await asr_route.asr_readiness()

    assert result == {"status": "warming"}
    assert ensured == ["qwen3_asr"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["bosch", "browser"])
async def test_non_local_readiness_never_starts_qwen3_asr(monkeypatch, provider):
    settings = Settings(
        asr_enabled=True,
        asr_realtime_enabled=True,
        asr_provider_mode=provider,
    )
    ensured: list[str] = []

    class Recycler:
        def ensure_resident(self, service_name: str) -> None:
            ensured.append(service_name)

    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        asr_route,
        "get_local_model_runtime_recycler",
        lambda: Recycler(),
    )
    monkeypatch.setattr(
        asr_route,
        "get_asr_readiness",
        lambda _settings: asyncio.sleep(0, result={"status": "degraded"}),
    )

    await asr_route.asr_readiness()

    assert ensured == []


@pytest.mark.asyncio
async def test_ready_asr_does_not_repeat_resident_warmup(monkeypatch):
    settings = Settings(asr_enabled=True, asr_realtime_enabled=True)
    ensured: list[str] = []

    class Recycler:
        def ensure_resident(self, service_name: str) -> None:
            ensured.append(service_name)

    monkeypatch.setattr(asr_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        asr_route,
        "get_local_model_runtime_recycler",
        lambda: Recycler(),
    )
    monkeypatch.setattr(
        asr_route,
        "get_asr_readiness",
        lambda _settings: asyncio.sleep(0, result={"status": "ready"}),
    )

    result = await asr_route.asr_readiness()

    assert result == {"status": "ready"}
    assert ensured == []


@pytest.mark.asyncio
async def test_asr_readiness_uses_short_cache_and_force_refresh(monkeypatch):
    calls = 0

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok", "active_sessions": 1, "max_batch_size": 8}

    class Client:
        async def get(self, _url, **_kwargs):
            nonlocal calls
            calls += 1
            return Response()

    monkeypatch.setattr(
        "backend.services.realtime_asr_service.get_shared_async_client",
        lambda pool: Client(),
    )
    settings = Settings(
        asr_local_streaming_url="http://qwen3_asr:7118",
        asr_local_streaming_urls="",
    )
    clear_asr_readiness_cache()

    first, second = await asyncio.gather(
        get_asr_readiness(settings),
        get_asr_readiness(settings),
    )
    refreshed = await get_asr_readiness(settings, force_refresh=True)

    assert first == second == refreshed
    assert calls == 2


def test_asr_readiness_route_requires_authentication():
    route = next(
        item for item in asr_route.router.routes
        if getattr(item, "path", "") == "/asr/readiness"
    )

    assert get_current_user in [dependency.call for dependency in route.dependant.dependencies]
