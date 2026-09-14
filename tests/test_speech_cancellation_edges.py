from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.api.routes import rehearsal as rehearsal_route
from backend.schemas.state import SessionState
from backend.services.rehearsal_service import RehearsalService
from backend.services.speech_websocket_service import SpeechWebSocketHub


class _Scheduler:
    @asynccontextmanager
    async def slot(self, **_kwargs):
        yield SimpleNamespace(queue_ms=0)


class _FloodingAgent:
    def __init__(self) -> None:
        self.fill_queue = asyncio.Event()
        self.queue_at_capacity = asyncio.Event()
        self.closed = asyncio.Event()
        self.chunks = [f"{index:03d}。" for index in range(400)]

    async def stream_reply(self, *_args, **_kwargs):
        try:
            yield "开始。"
            await self.fill_queue.wait()
            for index, chunk in enumerate(self.chunks):
                if index == 256:
                    self.queue_at_capacity.set()
                yield chunk
        finally:
            self.closed.set()


class _WaitingTts:
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.queue = None

    async def stream(self, queue, **_kwargs):
        self.queue = queue
        try:
            yield {"event": "speech_start"}
            await asyncio.Event().wait()
        finally:
            self.closed.set()


@pytest.mark.asyncio
async def test_speech_cancel_with_full_pipeline_queue_preserves_every_text_delta() -> None:
    cancelled = asyncio.Event()
    completion_attempted = asyncio.Event()
    completion_queue_sizes: list[tuple[int, int]] = []
    tts, agent = _WaitingTts(), _FloodingAgent()
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

    async def observe_completion(queue, item, *, nonblocking):
        if item[0] == "speech_done" and nonblocking:
            completion_queue_sizes.append((queue.qsize(), queue.maxsize))
            completion_attempted.set()
        await RehearsalService._signal_pipeline_completion(
            queue, item, nonblocking=nonblocking
        )

    service._signal_pipeline_completion = observe_completion
    stream = service._stream_employee_reply(
        agent,
        SessionState(session_id="session-full-queue"),
        "请继续。",
        [],
        speech_enabled=True,
        speech_voice=None,
        speech_stream_id="speech-full-queue",
        speech_cancel_event=cancelled,
    )
    prefix: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(1):
            async for event in stream:
                prefix.append(event)
                if event["event"] == "speech_start":
                    break
            agent.fill_queue.set()
            await agent.queue_at_capacity.wait()
            cancelled.set()
            await completion_attempted.wait()
            await tts.closed.wait()

        # The cancelled producer cannot insert its terminal signal. The watcher
        # must retry that signal once the SSE consumer resumes draining the queue.
        assert completion_queue_sizes == [(256, 256)]
        async with asyncio.timeout(1):
            remaining = [event async for event in stream]
    finally:
        await stream.aclose()

    expected = "开始。" + "".join(agent.chunks)
    assert "".join(
        event["text"] for event in prefix + remaining if event["event"] == "delta"
    ) == expected
    assert remaining[-1] == {"event": "_reply_complete", "reply": expected}
    assert not any(event["event"].startswith("speech_") for event in remaining)
    assert agent.closed.is_set()
    assert tts.queue.empty()


class _CancelTrackingHub(SpeechWebSocketHub):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls: list[tuple[str, str, str, bool]] = []
        self.connection_ids: dict[str, str] = {}

    async def register(self, session_id, websocket):
        connection_id = await super().register(session_id, websocket)
        self.connection_ids[session_id] = connection_id
        return connection_id

    async def cancel_stream(self, session_id, connection_id, stream_id):
        result = await super().cancel_stream(session_id, connection_id, stream_id)
        self.cancel_calls.append((session_id, connection_id, stream_id, result))
        return result


class _Socket:
    def __init__(self, hub, session_id, *, incoming=None, stream_id=None) -> None:
        self.hub = hub
        self.query_params = {"session_id": session_id}
        self.incoming = list(incoming or [])
        self.stream_id = stream_id
        self.delivery = None
        self.accepted = False
        self.sent: list[dict[str, Any]] = []
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                runtime=SimpleNamespace(settings=SimpleNamespace(tts_enabled=True))
            )
        )

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, event):
        self.sent.append(event)
        if event["event"] == "speech_socket_ready" and self.stream_id is not None:
            self.delivery = await self.hub.open_stream(
                self.query_params["session_id"], self.stream_id
            )

    async def close(self, **_kwargs):
        pass


class _OwnedSession:
    def get_session(self, session_id):
        assert session_id == "session-cancel-endpoint"
        return SessionState(session_id=session_id)


@pytest.mark.asyncio
async def test_websocket_cancel_uses_bound_identity_and_ignores_invalid_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_owned_session_check(_executor, _operation, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        rehearsal_route, "run_db_with_context", run_owned_session_check
    )
    session_id = "session-cancel-endpoint"
    stream_id = "speech-cancel-endpoint"
    hub = _CancelTrackingHub()
    other_socket = _Socket(hub, "other-session")
    await hub.register("other-session", other_socket)
    other_delivery = await hub.open_stream("other-session", "other-speech-stream")
    socket = _Socket(
        hub,
        session_id,
        stream_id=stream_id,
        incoming=[
            None,
            [],
            "cancel",
            {},
            {"type": "cancel"},
            {"type": "cancel", "speech_stream_id": None},
            {"type": "cancel", "speech_stream_id": 123},
            {"type": "cancel", "speech_stream_id": ""},
            {"type": "cancel", "speech_stream_id": "x" * 129},
            {"type": "unknown", "speech_stream_id": stream_id},
            {
                "type": "cancel",
                "speech_stream_id": stream_id,
                "session_id": "other-session",
                "connection_id": hub.connection_ids["other-session"],
            },
            {"type": "ping"},
        ],
    )
    try:
        async with asyncio.timeout(1):
            await rehearsal_route.speech_realtime(
                websocket=socket,
                _current_user=SimpleNamespace(email="user@bosch.com"),
                session_service=_OwnedSession(),
                speech_hub=hub,
            )

        assert hub.cancel_calls == [
            (session_id, hub.connection_ids[session_id], stream_id, True)
        ]
        assert socket.accepted
        assert socket.delivery.cancelled.is_set()
        assert not other_delivery.cancelled.is_set()
        assert [event["event"] for event in socket.sent] == [
            "speech_socket_ready",
            "speech_pong",
        ]
        assert not await hub.is_connected(session_id)
        assert await hub.is_connected("other-session")
    finally:
        await hub.shutdown()
