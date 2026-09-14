from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.api.routes import rehearsal as rehearsal_route
from backend.schemas.api import RehearsalMessageRequest
from backend.schemas.state import SessionState
from backend.services.rehearsal_service import RehearsalService
from backend.services.speech_websocket_service import SpeechWebSocketHub


class _Scheduler:
    @asynccontextmanager
    async def slot(self, **_kwargs):
        yield SimpleNamespace(queue_ms=0)


class _Agent:
    def __init__(self):
        self.proceed = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream_reply(self, *_args, **_kwargs):
        try:
            yield "前半句。"
            await self.proceed.wait()
            yield "后半句。"
        finally:
            self.closed.set()


class _Tts:
    def __init__(self, *, delay_cleanup=False):
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.closed = asyncio.Event()
        self.queue = None
        if not delay_cleanup:
            self.allow_cleanup.set()

    async def stream(self, queue, **_kwargs):
        self.queue = queue
        try:
            yield {"event": "speech_start"}
            await asyncio.Event().wait()
        finally:
            self.cleanup_started.set()
            await self.allow_cleanup.wait()
            self.closed.set()


def _reply_stream(tts, agent, cancelled):
    service = object.__new__(RehearsalService)
    service.settings = SimpleNamespace(
        tts_enabled=True,
        psychological_pattern_dynamics_enabled=False,
        chat_url="http://unused.invalid",
        model_for_task=lambda _task: "test-model",
    )
    service.model_scheduler = _Scheduler()
    service.tts_service = tts
    service._session_speech_voice = lambda *_args: None
    service._session_speech_seed = lambda *_args: 1
    return service._stream_employee_reply(
        agent,
        SessionState(session_id="session-a"),
        "经理输入",
        [],
        speech_enabled=True,
        speech_voice=None,
        speech_stream_id="stream-a",
        speech_cancel_event=cancelled,
    )


async def _until(stream, name):
    async with asyncio.timeout(1):
        async for event in stream:
            if event["event"] == name:
                return event
    raise AssertionError(f"No {name} event")


@pytest.mark.asyncio
async def test_stop_speech_preserves_complete_text_and_releases_synthesis():
    cancelled = asyncio.Event()
    tts, agent = _Tts(), _Agent()
    stream = _reply_stream(tts, agent, cancelled)
    await _until(stream, "speech_start")

    cancelled.set()
    await asyncio.wait_for(tts.closed.wait(), 1)
    agent.proceed.set()
    async with asyncio.timeout(1):
        remaining = [event async for event in stream]

    assert remaining[-1] == {"event": "_reply_complete", "reply": "前半句。后半句。"}
    assert not any(event["event"].startswith("speech_") for event in remaining)
    assert tts.queue.empty()
    assert agent.closed.is_set()


@pytest.mark.asyncio
async def test_sse_close_does_not_cancel_speech_cleanup_twice():
    cancelled = asyncio.Event()
    tts, agent = _Tts(delay_cleanup=True), _Agent()
    stream = _reply_stream(tts, agent, cancelled)
    await _until(stream, "speech_start")
    cancelled.set()
    await asyncio.wait_for(tts.cleanup_started.wait(), 1)

    closing = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    tts.allow_cleanup.set()
    await asyncio.wait_for(closing, 1)

    assert tts.closed.is_set(), "Repeated cancellation interrupted lease cleanup"
    assert agent.closed.is_set()


@pytest.mark.asyncio
async def test_pre_cancelled_speech_never_starts_synthesis():
    cancelled = asyncio.Event()
    cancelled.set()
    tts, agent = _Tts(), _Agent()
    agent.proceed.set()
    async with asyncio.timeout(1):
        events = [event async for event in _reply_stream(tts, agent, cancelled)]

    assert tts.queue is None
    assert events[-1]["reply"] == "前半句。后半句。"
    assert not any(event["event"].startswith("speech_") for event in events)


@pytest.mark.asyncio
async def test_stop_speech_during_model_readiness_does_not_stop_text():
    class _LoadingTts(_Tts):
        async def ensure_ready(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.closed.set()

    cancelled = asyncio.Event()
    tts, agent = _LoadingTts(), _Agent()
    stream = _reply_stream(tts, agent, cancelled)
    await _until(stream, "delta")
    cancelled.set()
    agent.proceed.set()
    async with asyncio.timeout(1):
        events = [event async for event in stream]

    assert tts.closed.is_set()
    assert tts.queue is None
    assert events[-1]["reply"] == "前半句。后半句。"


class _Socket:
    def __init__(self):
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def send_json(self, _payload):
        pass

    async def send_bytes(self, _payload):
        self.blocked.set()
        await self.release.wait()

    async def close(self, **_kwargs):
        self.release.set()


class _StreamingService:
    settings = SimpleNamespace(tts_enabled=True)

    def __init__(self):
        self.closed = False
        self.arguments = None

    async def stream_manager_message(self, _session_id, _message, **kwargs):
        self.arguments = kwargs
        try:
            yield {"event": "speech_audio", "_pcm": b"\x01\x00"}
            yield {"event": "delta", "text": "文字无需等待音频发送。"}
            yield {"event": "done"}
        finally:
            self.closed = True


def _payload():
    return RehearsalMessageRequest(
        message="请说明你的想法。",
        speech={"enabled": True, "stream_id": "speech-stream-0001"},
    )


@pytest.mark.asyncio
async def test_slow_audio_socket_does_not_block_sse_text():
    hub = SpeechWebSocketHub(send_timeout_seconds=1)
    socket, service = _Socket(), _StreamingService()
    await hub.register("session-a", socket)
    response = await rehearsal_route.stream_message(
        "session-a", _payload(), service=service, speech_hub=hub
    )
    try:
        async with asyncio.timeout(0.5):
            first = await anext(response.body_iterator)
            second = await anext(response.body_iterator)
        assert "event: delta" in first
        assert "event: done" in second
        assert socket.blocked.is_set()
        socket.release.set()
        assert [part async for part in response.body_iterator] == []
        assert service.closed
    finally:
        await response.body_iterator.aclose()
        await hub.shutdown()


@pytest.mark.asyncio
async def test_sse_close_closes_source_and_cancels_only_bound_speech():
    hub = SpeechWebSocketHub(send_timeout_seconds=0.05)
    socket, service = _Socket(), _StreamingService()
    await hub.register("session-a", socket)
    response = await rehearsal_route.stream_message(
        "session-a", _payload(), service=service, speech_hub=hub
    )
    await anext(response.body_iterator)
    async with asyncio.timeout(1):
        await response.body_iterator.aclose()
    assert service.closed
    assert service.arguments["speech_cancel_event"].is_set()
    await hub.shutdown()


@pytest.mark.asyncio
async def test_cancel_arriving_before_http_disables_synthesis_but_keeps_text():
    hub = SpeechWebSocketHub()
    socket, service = _Socket(), _StreamingService()
    connection_id = await hub.register("session-a", socket)
    await hub.cancel_stream("session-a", connection_id, "speech-stream-0001")
    response = await rehearsal_route.stream_message(
        "session-a", _payload(), service=service, speech_hub=hub
    )
    parts = [part async for part in response.body_iterator]
    assert service.arguments["speech_enabled"] is False
    assert service.arguments["speech_cancel_event"] is None
    assert any("event: done" in part for part in parts)
    await hub.shutdown()
