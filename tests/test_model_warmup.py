from __future__ import annotations

import asyncio
import io
import wave

import httpx
import pytest

from backend.config.settings import Settings
from backend.services import model_warmup_service
from backend.services.model_warmup_service import ModelWarmupService


def _settings(**overrides) -> Settings:
    values = {
        "model_warmup_enabled": True,
        "model_warmup_timeout_seconds": 2,
        "model_warmup_max_concurrency": 8,
        "chat_model": "",
        "chat_api_endpoint": "http://chat.test/v1/chat/completions",
        "default_chat_model": "chat-a",
        "profile_model": "chat-a",
        "intent_model": "chat-a",
        "intent_performance_model": "chat-a",
        "employee_model": "chat-b",
        "conversation_summary_model": "chat-c",
        "motivation_scoring_model": "chat-c",
        "emotion_transition_model": "chat-c",
        "rehearsal_dimension_model": "chat-c",
        "guidance_model": "chat-a",
        "coach_evaluator_model": "chat-a",
        "model_retry_race_model": "retry-a",
        "coach_redline_model": "chat-a",
        "document_vision_enabled": True,
        "document_vision_model": "vision-a",
        "embedding_api_endpoint": "http://embedding.test/v1/embeddings",
        "embedding_model": "embedding-a",
        "rerank_api_endpoint": "http://reranker.test/v1/completions",
        "rerank_model": "reranker-a",
        "asr_enabled": True,
        "asr_realtime_enabled": True,
        "asr_realtime_model": "asr-local-a",
        "asr_local_streaming_url": "http://asr-local.test",
        "asr_local_streaming_urls": "",
        "asr_http_url": "http://asr-http.test/audio/transcriptions",
        "asr_http_model": "asr-http-a",
        "tts_enabled": True,
        "tts_http_url": "http://fish-tts.test/v1/tts",
        "tts_model": "tts-a",
    }
    values.update(overrides)
    return Settings(**values)


class _FakeLLM:
    def __init__(self, calls: list[tuple[str, dict]]):
        self.calls = calls

    async def ainvoke_text(self, **kwargs):
        self.calls.append(("chat", kwargs))
        return "OK"


class _FakeEmbedding:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls

    async def aembed(self, texts, *, ensure_local_runtime=True):
        assert ensure_local_runtime is False
        self.calls.append(("embedding", texts))
        return [[0.1, 0.2]]


class _FakeReranker:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls

    async def arerank(self, query, documents, top_n, *, ensure_local_runtime=True):
        assert ensure_local_runtime is False
        self.calls.append(("reranker", (query, documents, top_n)))
        return [(0, 1.0)]


class _InitiallyUnavailableReranker(_FakeReranker):
    def __init__(self, calls: list[tuple[str, object]]):
        super().__init__(calls)
        self.attempts = 0

    async def arerank(self, query, documents, top_n, *, ensure_local_runtime=True):
        self.attempts += 1
        if self.attempts == 1:
            request = httpx.Request("POST", "http://reranker.test/v1/completions")
            raise httpx.ConnectError("service is still starting", request=request)
        return await super().arerank(
            query,
            documents,
            top_n,
            ensure_local_runtime=ensure_local_runtime,
        )


class _DelayedReadyTts:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls
        self.readiness_calls = 0

    async def is_ready(self):
        self.readiness_calls += 1
        return self.readiness_calls >= 3

    async def stream(self, text_queue, *, emotion_state, voice):
        while await text_queue.get() is not None:
            pass
        self.calls.append(("tts_synthesis", None))
        yield {"event": "speech_audio", "byte_length": 2}
        yield {"event": "speech_done"}


class _FakeTts:
    def __init__(self, calls: list[tuple[str, object]]):
        self.calls = calls

    async def stream(self, text_queue, *, emotion_state, voice):
        texts: list[str] = []
        while True:
            item = await text_queue.get()
            if item is None:
                break
            texts.append(item)
        self.calls.append(
            (
                "tts",
                {
                    "text": "".join(texts),
                    "emotion_state": emotion_state,
                    "voice": voice,
                },
            )
        )
        yield {"event": "speech_audio", "byte_length": 2}
        yield {"event": "speech_done"}


@pytest.mark.asyncio
async def test_tts_warmup_probes_health_with_backoff_before_one_synthesis(monkeypatch):
    calls: list[tuple[str, object]] = []
    tts = _DelayedReadyTts(calls)
    service = ModelWarmupService(
        _settings(model_warmup_timeout_seconds=30),
        llm_service=_FakeLLM([]),
        embedding_service=_FakeEmbedding([]),
        rerank_service=_FakeReranker([]),
        tts_service=tts,
    )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    await service._warm_tts()

    assert tts.readiness_calls == 3
    assert calls == [("tts_synthesis", None)]


@pytest.mark.asyncio
async def test_startup_warmup_covers_unique_chat_and_specialized_models(monkeypatch):
    calls: list[tuple[str, object]] = []
    service = ModelWarmupService(
        _settings(),
        llm_service=_FakeLLM(calls),
        embedding_service=_FakeEmbedding(calls),
        rerank_service=_FakeReranker(calls),
        tts_service=_FakeTts(calls),
    )

    async def fake_realtime_asr():
        calls.append(("asr_realtime", None))

    async def fake_http_asr():
        calls.append(("asr_http", None))

    monkeypatch.setattr(service, "_warm_realtime_asr", fake_realtime_asr)
    monkeypatch.setattr(service, "_warm_http_asr", fake_http_asr)

    snapshot = await service.warmup()

    chat_calls = [payload for kind, payload in calls if kind == "chat"]
    assert {payload["model"] for payload in chat_calls} == {
        "chat-a",
        "chat-b",
        "chat-c",
        "vision-a",
        "retry-a",
    }
    assert all(payload["enable_thinking"] is False for payload in chat_calls)
    assert {result["kind"] for result in snapshot["targets"]} == {
        "chat",
        "embedding",
        "reranker",
        "asr_realtime",
        "asr_http",
        "tts",
    }
    assert snapshot["status"] == "completed"
    assert all(result["status"] == "completed" for result in snapshot["targets"])
    assert ("asr_realtime", None) in calls
    assert ("asr_http", None) in calls
    assert (
        "tts",
        {"text": "你好。", "emotion_state": None, "voice": None},
    ) in calls


@pytest.mark.asyncio
async def test_startup_warmup_is_concurrent_and_isolates_failures(monkeypatch):
    service = ModelWarmupService(
        _settings(
            document_vision_enabled=False,
            asr_enabled=False,
            model_provider_mode="platform",
            embedding_api_endpoint="",
            rerank_api_endpoint="",
            model_warmup_max_concurrency=2,
            tts_enabled=False,
        ),
        llm_service=_FakeLLM([]),
        embedding_service=_FakeEmbedding([]),
        rerank_service=_FakeReranker([]),
    )
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def fake_chat(model: str):
        started.add(model)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        if model == "chat-b":
            raise RuntimeError("expected warmup failure")

    monkeypatch.setattr(service, "_warm_chat_model", fake_chat)
    task = asyncio.create_task(service.warmup())
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    snapshot = await task

    statuses = {result["model"]: result["status"] for result in snapshot["targets"]}
    assert statuses == {
        "retry-a": "completed",
        "chat-a": "completed",
        "chat-b": "failed",
        "chat-c": "completed",
    }
    assert snapshot["status"] == "completed_with_errors"


@pytest.mark.asyncio
async def test_local_warmup_retries_transient_startup_failure(monkeypatch):
    calls: list[tuple[str, object]] = []
    reranker = _InitiallyUnavailableReranker(calls)
    service = ModelWarmupService(
        _settings(
            chat_api_endpoint="",
            embedding_api_endpoint="",
            asr_enabled=False,
            tts_enabled=False,
            model_provider_mode="local",
            embedding_local_model="",
            rerank_local_model="reranker-a",
        ),
        llm_service=_FakeLLM(calls),
        embedding_service=_FakeEmbedding(calls),
        rerank_service=reranker,
    )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    snapshot = await service.warmup()

    assert reranker.attempts == 2
    assert snapshot["status"] == "completed"
    assert len(snapshot["targets"]) == 1
    assert snapshot["targets"][0]["kind"] == "reranker"
    assert snapshot["targets"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_local_warmup_does_not_retry_non_transient_failure(monkeypatch):
    calls = 0
    service = ModelWarmupService(
        _settings(
            chat_api_endpoint="",
            embedding_api_endpoint="",
            asr_enabled=False,
            tts_enabled=False,
            model_provider_mode="local",
            embedding_local_model="",
            rerank_local_model="reranker-a",
        ),
        llm_service=_FakeLLM([]),
        embedding_service=_FakeEmbedding([]),
        rerank_service=_FakeReranker([]),
    )

    async def invalid_response():
        nonlocal calls
        calls += 1
        raise ValueError("invalid reranker response")

    monkeypatch.setattr(service, "_warm_reranker", invalid_response)
    snapshot = await service.warmup()

    assert calls == 1
    assert snapshot["status"] == "completed_with_errors"
    assert snapshot["targets"][0]["status"] == "failed"


def test_http_asr_warmup_uses_valid_pcm16_mono_wav():
    payload = ModelWarmupService._silent_wav()

    with wave.open(io.BytesIO(payload), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnframes() == 4_000


@pytest.mark.asyncio
async def test_http_asr_warmup_reuses_runtime_speech_pool(monkeypatch):
    calls: list[dict] = []

    class _Response:
        status_code = 200

    class _Client:
        async def post(self, _url, **kwargs):
            calls.append(kwargs)
            return _Response()

    async def fake_headers(_self, _api_key):
        return {"Authorization": "Bearer secret"}

    pools: list[str] = []
    client = _Client()

    def fake_shared_client(pool: str):
        pools.append(pool)
        return client

    monkeypatch.setattr(
        "backend.services.model_warmup_service.get_shared_async_client",
        fake_shared_client,
    )
    monkeypatch.setattr(
        "backend.services.model_warmup_service.ModelAPIAuth.async_headers",
        fake_headers,
    )

    await ModelWarmupService(_settings())._warm_http_asr()

    assert pools == ["speech"]
    assert calls[0]["timeout"] is not None


@pytest.mark.asyncio
async def test_http_asr_keepwarm_runs_after_idle_and_cancels_cleanly(monkeypatch):
    service = ModelWarmupService(
        _settings(asr_bosch_keepwarm_interval_seconds=15)
    )
    warm_calls = 0
    sleep_calls = 0

    async def fake_warm() -> None:
        nonlocal warm_calls
        warm_calls += 1

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "_warm_http_asr", fake_warm)
    monkeypatch.setattr(model_warmup_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        model_warmup_service,
        "bosch_http_asr_idle_seconds",
        lambda: 15.0,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.keep_http_asr_warm()

    assert warm_calls == 1
