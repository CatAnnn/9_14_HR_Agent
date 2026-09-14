from __future__ import annotations

import asyncio
import json

import pytest

from backend.config.settings import Settings
from backend.services.bosch_native_realtime_asr_service import (
    BoschNativeRealtimeAsrProxy,
    _realtime_url,
)
from backend.services.realtime_asr_service import (
    AsrConfigurationError,
    _fetch_asr_readiness,
    create_realtime_asr_provider,
)


class _Auth:
    async def async_headers(self, api_key: str) -> dict[str, str]:
        assert api_key == "test-key"
        return {"Authorization": "Bearer test-key"}


class _FakeConnection:
    def __init__(self, *, auto_session_ready: bool = True) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.auto_session_ready = auto_session_ready

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        if payload["type"] == "session.update" and self.auto_session_ready:
            await self.emit({"type": "session.updated"})
        elif payload["type"] == "session.finish":
            await self.emit({"type": "session.finished"})

    async def emit(self, payload: dict) -> None:
        await self.incoming.put(json.dumps(payload, ensure_ascii=False))

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


def _settings(**overrides) -> Settings:
    values = {
        "asr_provider_mode": "bosch",
        "asr_http_url": "https://model.example/audio/transcriptions",
        "asr_http_model": "qwen3-asr-flash",
        "asr_api_key": "test-key",
        "asr_bosch_realtime_url": "wss://model.example/realtime",
        "asr_bosch_realtime_model": "qwen3-asr-flash-realtime",
        "asr_bosch_prompt": "TCL, G9, Career Elements",
        "asr_connect_timeout_seconds": 1,
        "asr_local_final_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(**values)


def test_realtime_url_adds_model_once_and_preserves_existing_query():
    assert _realtime_url("wss://model.example/realtime?tenant=hr", "qwen") == (
        "wss://model.example/realtime?tenant=hr&model=qwen"
    )
    assert _realtime_url(
        "wss://model.example/realtime?model=deployed", "ignored"
    ).endswith("model=deployed")


def test_factory_selects_native_bosch_only_when_websocket_url_is_configured():
    native = create_realtime_asr_provider(_settings())
    assert isinstance(native, BoschNativeRealtimeAsrProxy)


@pytest.mark.asyncio
async def test_native_bosch_stream_uses_stable_and_revisable_text(monkeypatch):
    connection = _FakeConnection()
    captured_connect: dict = {}

    async def fake_connect(url: str, **kwargs):
        captured_connect["url"] = url
        captured_connect.update(kwargs)
        return connection

    monkeypatch.setattr(
        "backend.services.bosch_native_realtime_asr_service.websockets.connect",
        fake_connect,
    )
    proxy = BoschNativeRealtimeAsrProxy(
        _settings(asr_language="zh"),
        auth=_Auth(),
        language="en",
    )
    events: list[dict] = []

    async def collect(payload: dict) -> None:
        events.append(payload)

    receive_task = asyncio.create_task(proxy.receive_loop(collect))
    await proxy.connect()
    await proxy.append_audio(b"\x01\x00" * 1600)

    await connection.emit(
        {
            "type": "conversation.item.input_audio_transcription.text",
            "item_id": "item-1",
            "text": "今天讨论",
            "stash": "绩效",
        }
    )
    await asyncio.sleep(0)
    await connection.emit(
        {
            "type": "conversation.item.input_audio_transcription.text",
            "item_id": "item-1",
            "text": "今天讨论绩效",
            "stash": "目标",
        }
    )
    await asyncio.sleep(0)
    await connection.emit(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "item-1",
            "transcript": "今天讨论绩效目标。",
        }
    )
    await asyncio.sleep(0)

    result = await proxy.finish()

    update = connection.sent[0]
    assert update["type"] == "session.update"
    assert update["session"]["input_audio_transcription"]["language"] == "en"
    assert update["session"]["input_audio_transcription"]["corpus"]["text"] == (
        "TCL, G9, Career Elements"
    )
    assert captured_connect["url"].endswith("model=qwen3-asr-flash-realtime")
    assert captured_connect["additional_headers"]["OpenAI-Beta"] == "realtime=v1"
    assert any(item["type"] == "input_audio_buffer.append" for item in connection.sent)
    assert [event["text"] for event in events if event["type"] == "partial"] == [
        "今天讨论绩效",
        "今天讨论绩效目标",
        "今天讨论绩效目标。",
    ]
    assert result["text"] == "今天讨论绩效目标。"
    assert result["speech_detected"] is True
    assert proxy.authoritative_final is False

    await proxy.close()
    await receive_task
    assert connection.closed is True


@pytest.mark.asyncio
async def test_native_bosch_buffers_startup_audio_without_blocking(monkeypatch):
    connection = _FakeConnection(auto_session_ready=False)

    async def fake_connect(url: str, **kwargs):
        return connection

    monkeypatch.setattr(
        "backend.services.bosch_native_realtime_asr_service.websockets.connect",
        fake_connect,
    )
    proxy = BoschNativeRealtimeAsrProxy(_settings(), auth=_Auth())
    connect_task = asyncio.create_task(proxy.connect())

    async def wait_for_session_update() -> None:
        for _ in range(100):
            if connection.sent:
                return
            await asyncio.sleep(0)
        raise AssertionError("session.update was not sent")

    await wait_for_session_update()
    pcm = b"\x01\x00" * 1600
    await asyncio.wait_for(proxy.append_audio(pcm), timeout=0.05)
    assert [item["type"] for item in connection.sent] == ["session.update"]

    await connection.emit({"type": "session.updated"})
    await connect_task
    appended = [
        item for item in connection.sent if item["type"] == "input_audio_buffer.append"
    ]
    assert len(appended) == 1

    await proxy.close()


@pytest.mark.asyncio
async def test_native_bosch_unexpected_clean_close_reports_failure_once(monkeypatch):
    connection = _FakeConnection()

    async def fake_connect(url: str, **kwargs):
        return connection

    monkeypatch.setattr(
        "backend.services.bosch_native_realtime_asr_service.websockets.connect",
        fake_connect,
    )
    proxy = BoschNativeRealtimeAsrProxy(_settings(), auth=_Auth())
    events: list[dict] = []

    async def collect(payload: dict) -> None:
        events.append(payload)

    receive_task = asyncio.create_task(proxy.receive_loop(collect))
    await proxy.connect()
    await connection.incoming.put(None)
    assert proxy._receiver_task is not None
    await proxy._receiver_task
    await asyncio.sleep(0)

    assert proxy.failed is True
    failures = [event for event in events if event["type"] == "preview_unavailable"]
    assert len(failures) == 1

    await proxy.close()
    await receive_task


@pytest.mark.asyncio
async def test_native_bosch_startup_error_is_not_reported_twice(monkeypatch):
    connection = _FakeConnection(auto_session_ready=False)

    async def fake_connect(url: str, **kwargs):
        return connection

    monkeypatch.setattr(
        "backend.services.bosch_native_realtime_asr_service.websockets.connect",
        fake_connect,
    )
    proxy = BoschNativeRealtimeAsrProxy(_settings(), auth=_Auth())
    events: list[dict] = []

    async def collect(payload: dict) -> None:
        events.append(payload)

    receive_task = asyncio.create_task(proxy.receive_loop(collect))
    connect_task = asyncio.create_task(proxy.connect())
    for _ in range(100):
        if connection.sent:
            break
        await asyncio.sleep(0)
    await connection.emit(
        {"type": "error", "error": {"message": "invalid session"}}
    )

    with pytest.raises(AsrConfigurationError, match="invalid session"):
        await connect_task
    await receive_task
    assert events == []


@pytest.mark.asyncio
async def test_native_bosch_readiness_reports_http_final_correction():
    readiness = await _fetch_asr_readiness(_settings())

    assert readiness["status"] == "ready"
    assert readiness["streaming_mode"] == "native_websocket"
    assert readiness["model"] == "qwen3-asr-flash-realtime"
    assert readiness["final_correction_model"] == "qwen3-asr-flash"
