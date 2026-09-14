from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import WebSocketException
from starlette.websockets import WebSocketDisconnect

from backend.api.dependencies import (
    get_session_service,
    get_speech_websocket_hub,
)
from backend.api.routes import rehearsal as rehearsal_route
from backend.core.auth_dependency import get_current_user
from backend.core.session_context import (
    reset_current_auth_user_id,
    set_current_auth_user_id,
)
from backend.schemas.api import RehearsalMessageRequest
from backend.schemas.state import SessionState
from backend.services.speech_websocket_service import SpeechWebSocketHub


class _WebSocket:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        incoming: list[dict[str, Any]] | None = None,
    ) -> None:
        self.query_params = {"session_id": session_id} if session_id else {}
        self.incoming = list(incoming or [])
        self.accepted = False
        self.sent_json: list[dict[str, Any]] = []
        self.sent_bytes: list[bytes] = []
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        if self.incoming:
            return self.incoming.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent_json.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class _SessionService:
    def __init__(self, session_id: str = "workflow-session-a") -> None:
        self.session_id = session_id

    def get_session(self, session_id: str) -> SessionState:
        if session_id != self.session_id:
            raise KeyError(session_id)
        return SessionState(session_id=session_id)


@pytest.mark.asyncio
async def test_speech_websocket_audio_uses_binary_pcm_frame() -> None:
    hub = SpeechWebSocketHub()
    websocket = _WebSocket()
    await hub.register("workflow-session-a", websocket)
    pcm = b"\x00\x01\x02\x03"

    sent = await hub.publish(
        "workflow-session-a",
        {
            "event": "speech_audio",
            "_pcm": pcm,
            "byte_length": len(pcm),
            "sample_rate": 44_100,
            "session_id": "workflow-session-a",
            "speech_stream_id": "speech-stream-0001",
        },
    )

    assert sent is True
    assert websocket.sent_bytes == [pcm]
    assert websocket.sent_json == [
        {
            "event": "speech_audio",
            "byte_length": len(pcm),
            "sample_rate": 44_100,
            "session_id": "workflow-session-a",
            "speech_stream_id": "speech-stream-0001",
            "transport": "binary",
            "encoding": "pcm_s16le",
        }
    ]


@pytest.mark.asyncio
async def test_speech_websocket_accepts_legacy_base64_audio_payload() -> None:
    hub = SpeechWebSocketHub()
    websocket = _WebSocket()
    await hub.register("workflow-session-a", websocket)
    pcm = b"\x00\x01\x02\x03"

    sent = await hub.publish(
        "workflow-session-a",
        {
            "event": "speech_audio",
            "audio": base64.b64encode(pcm).decode("ascii"),
            "sample_rate": 44_100,
        },
    )

    assert sent is True
    assert websocket.sent_bytes == [pcm]


@pytest.mark.asyncio
async def test_speech_websocket_isolates_sessions_and_replaces_same_session() -> None:
    hub = SpeechWebSocketHub()
    first = _WebSocket()
    replacement = _WebSocket()
    other = _WebSocket()

    first_id = await hub.register("workflow-session-a", first)
    other_id = await hub.register("workflow-session-b", other)
    replacement_id = await hub.register("workflow-session-a", replacement)

    assert first.closed == [(1000, "Replaced by a newer speech connection")]
    assert await hub.publish(
        "workflow-session-a",
        {"event": "speech_done", "session_id": "workflow-session-a"},
    )
    assert replacement.sent_json[-1]["session_id"] == "workflow-session-a"
    assert not other.sent_json

    await hub.unregister("workflow-session-a", first_id)
    assert await hub.is_connected("workflow-session-a") is True
    await hub.unregister("workflow-session-a", replacement_id)
    await hub.unregister("workflow-session-b", other_id)
    assert await hub.is_connected("workflow-session-a") is False
    assert await hub.is_connected("workflow-session-b") is False


def test_speech_websocket_route_has_auth_and_session_dependencies() -> None:
    route = next(
        item
        for item in rehearsal_route.router.routes
        if getattr(item, "path", "") == "/rehearsal/speech/realtime"
    )
    dependency_calls = [dependency.call for dependency in route.dependant.dependencies]

    assert get_current_user in dependency_calls
    assert get_session_service in dependency_calls
    assert get_speech_websocket_hub in dependency_calls


def test_speech_websocket_rejects_missing_or_unowned_session() -> None:
    with pytest.raises(WebSocketException) as missing:
        rehearsal_route._require_owned_workflow_session(
            _WebSocket(),
            _SessionService(),
        )
    assert missing.value.code == 1008

    with pytest.raises(WebSocketException) as unowned:
        rehearsal_route._require_owned_workflow_session(
            _WebSocket(session_id="workflow-session-b"),
            _SessionService(),
        )
    assert unowned.value.code == 1008
    assert unowned.value.reason == "Workflow session not found"


@pytest.mark.asyncio
async def test_speech_websocket_endpoint_sends_ready_and_pong(monkeypatch) -> None:
    monkeypatch.setattr(
        rehearsal_route,
        "get_settings",
        lambda: SimpleNamespace(tts_enabled=True),
    )
    websocket = _WebSocket(
        session_id="workflow-session-a",
        incoming=[{"type": "ping"}],
    )
    hub = SpeechWebSocketHub()

    await rehearsal_route.speech_realtime(
        websocket=websocket,
        _current_user=SimpleNamespace(email="user@bosch.com"),
        session_service=_SessionService(),
        speech_hub=hub,
    )

    assert websocket.accepted is True
    assert [event["event"] for event in websocket.sent_json] == [
        "speech_socket_ready",
        "speech_pong",
    ]
    assert websocket.sent_json[0]["transport"] == "websocket"
    assert await hub.is_connected("workflow-session-a") is False


@pytest.mark.asyncio
async def test_speech_websocket_rejects_when_tts_is_globally_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        rehearsal_route,
        "get_settings",
        lambda: SimpleNamespace(tts_enabled=False),
    )
    websocket = _WebSocket(session_id="workflow-session-a")
    hub = SpeechWebSocketHub()

    await rehearsal_route.speech_realtime(
        websocket=websocket,
        _current_user=SimpleNamespace(email="user@bosch.com"),
        session_service=_SessionService(),
        speech_hub=hub,
    )

    assert websocket.accepted is False
    assert websocket.closed == [(1008, "Employee speech output is disabled")]
    assert await hub.is_connected("workflow-session-a") is False


class _StreamingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_manager_message(self, session_id: str, message: str, **kwargs):
        self.calls.append(
            {"session_id": session_id, "message": message, **kwargs}
        )
        yield {
            "event": "speech_stream_ready",
            "session_id": session_id,
            "speech_stream_id": kwargs["speech_stream_id"],
        }
        yield {"event": "delta", "text": "员工回复"}
        yield {
            "event": "speech_audio",
            "audio": "AAA=",
            "session_id": session_id,
            "speech_stream_id": kwargs["speech_stream_id"],
        }
        yield {
            "event": "speech_done",
            "session_id": session_id,
            "speech_stream_id": kwargs["speech_stream_id"],
        }
        yield {"event": "done", "state": {"session_id": session_id}}


class _SpeechHub:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.cancelled = asyncio.Event()
        self.playback = object()
        self.drained: bool | None = None

    async def is_connected(self, _session_id: str) -> bool:
        return True

    async def open_stream(self, _session_id: str, _stream_id: str):
        return self

    def enqueue(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True

    async def close_stream(self, _delivery, drain: bool = True) -> None:
        self.drained = drain


@pytest.mark.asyncio
async def test_rehearsal_sse_contains_text_only_and_routes_speech_to_websocket() -> None:
    service = _StreamingService()
    hub = _SpeechHub()
    payload = RehearsalMessageRequest(
        message="请说明你的想法。",
        speech={
            "enabled": True,
            "voice": "Fish Audio 默认音色",
            "stream_id": "speech-stream-0001",
        },
    )

    response = await rehearsal_route.stream_message(
        "workflow-session-a",
        payload,
        service=service,
        speech_hub=hub,
    )
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    body = "".join(chunks)

    assert "event: delta" in body
    assert "event: done" in body
    assert "speech_audio" not in body
    assert "\"audio\"" not in body
    assert [event["event"] for event in hub.events] == [
        "speech_stream_ready",
        "speech_audio",
        "speech_done",
    ]
    assert service.calls == [
        {
            "session_id": "workflow-session-a",
            "message": "请说明你的想法。",
            "request_id": None,
            "speech_enabled": True,
            "speech_voice": "Fish Audio 默认音色",
            "speech_stream_id": "speech-stream-0001",
            "speech_cancel_event": hub.cancelled,
            "speech_playback_buffer": hub.playback,
        }
    ]
    assert hub.drained is True


def test_nginx_upgrades_tts_websocket_path() -> None:
    nginx = Path("frontend/nginx.conf").read_text(encoding="utf-8")
    start = nginx.index("location = /api/v1/rehearsal/speech/realtime")
    end = nginx.index("location /api/v1/", start)
    block = nginx[start:end]

    assert "proxy_set_header Upgrade $http_upgrade;" in block
    assert 'proxy_set_header Connection "upgrade";' in block
    assert "proxy_buffering off;" in block
    assert "map $cookie_hragent_session $backend_affinity_key" in nginx
    assert "hash $backend_affinity_key consistent;" in nginx


class _GatedSpeechSocket(_WebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, Any]] = []
        self.sending_audio = asyncio.Event()
        self.allow_audio = asyncio.Event()

    async def send_json(self, payload: dict[str, Any]) -> None:
        await super().send_json(payload)
        self.frames.append(("json", payload))

    async def send_bytes(self, payload: bytes) -> None:
        self.sending_audio.set()
        await self.allow_audio.wait()
        if self.closed:
            raise WebSocketDisconnect(code=1000)
        await super().send_bytes(payload)
        self.frames.append(("bytes", payload))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await super().close(code, reason)
        self.allow_audio.set()


@pytest.mark.asyncio
async def test_speech_delivery_drains_in_order_with_paired_pcm_frames() -> None:
    hub = SpeechWebSocketHub()
    websocket = _GatedSpeechSocket()
    websocket.allow_audio.set()
    await hub.register("session-a", websocket)
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    assert delivery.enqueue({"event": "speech_start"})
    assert delivery.enqueue({"event": "speech_audio", "_pcm": b"\x01\x00"})
    assert delivery.enqueue({"event": "speech_done"})

    await asyncio.wait_for(hub.close_stream(delivery), timeout=0.5)

    assert [kind for kind, _ in websocket.frames] == ["json", "json", "bytes", "json"]
    assert [event["event"] for event in websocket.sent_json] == [
        "speech_start", "speech_audio", "speech_done"
    ]
    assert websocket.sent_json[1]["speech_stream_id"] == "stream-a"
    assert websocket.sent_json[1]["session_id"] == "session-a"
    assert not delivery.cancelled.is_set()
    assert not delivery.enqueue({"event": "speech_done"})
    assert not hub._deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["bytes", "events"])
async def test_speech_delivery_overflow_cancels_and_discards_pending_audio(bound: str) -> None:
    hub = SpeechWebSocketHub(
        max_queue_bytes=512 if bound == "bytes" else 2_097_152,
        max_queue_events=1 if bound == "events" else 32,
    )
    websocket = _WebSocket()
    await hub.register("session-a", websocket)
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    assert delivery.enqueue({"event": "speech_audio", "_pcm": b"\x00\x00"})
    assert not delivery.enqueue({
        "event": "speech_audio", "_pcm": b"\x00" * (512 if bound == "bytes" else 2)
    })
    assert delivery.cancelled.is_set()

    await asyncio.wait_for(hub.close_stream(delivery), timeout=0.5)

    assert [event["event"] for event in websocket.sent_json] == ["speech_error"]
    assert websocket.sent_json[0]["speech_stream_id"] == "stream-a"
    assert not websocket.sent_bytes
    assert not hub._deliveries
    assert await hub.is_connected("session-a")


@pytest.mark.asyncio
async def test_speech_delivery_overflow_sends_error_after_completing_active_pcm_pair() -> None:
    hub = SpeechWebSocketHub(max_queue_events=1)
    websocket = _GatedSpeechSocket()
    await hub.register("session-a", websocket)
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    assert delivery.enqueue({"event": "speech_audio", "_pcm": b"\x01\x00"})
    await asyncio.wait_for(websocket.sending_audio.wait(), timeout=0.5)
    assert not delivery.enqueue({"event": "speech_done"})
    assert delivery.cancelled.is_set()
    websocket.allow_audio.set()

    await asyncio.wait_for(hub.close_stream(delivery), timeout=0.5)

    assert [kind for kind, _ in websocket.frames] == ["json", "bytes", "json"]
    assert websocket.sent_json[-1]["event"] == "speech_error"
    assert not websocket.closed


@pytest.mark.asyncio
async def test_speech_delivery_bad_metadata_reports_terminal_error() -> None:
    hub = SpeechWebSocketHub()
    websocket = _WebSocket()
    await hub.register("session-a", websocket)
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    assert not delivery.enqueue({"event": "speech_audio", "invalid": object()})

    await hub.close_stream(delivery)

    assert delivery.cancelled.is_set()
    assert [event["event"] for event in websocket.sent_json] == ["speech_error"]


@pytest.mark.asyncio
async def test_speech_delivery_timeout_closes_partial_frame_and_cancels_stream() -> None:
    hub = SpeechWebSocketHub(send_timeout_seconds=0.02)
    websocket = _GatedSpeechSocket()
    await hub.register("session-a", websocket)
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    assert delivery.enqueue({"event": "speech_audio", "_pcm": b"\x00\x00"})
    await asyncio.wait_for(websocket.sending_audio.wait(), timeout=0.5)
    assert delivery.enqueue({"event": "speech_done"})

    await asyncio.wait_for(hub.close_stream(delivery), timeout=0.5)

    assert delivery.cancelled.is_set()
    assert [event["event"] for event in websocket.sent_json] == ["speech_audio"]
    assert not websocket.sent_bytes
    assert websocket.closed
    assert not await hub.is_connected("session-a")
    assert not hub._deliveries


@pytest.mark.asyncio
async def test_speech_send_deadline_includes_waiting_for_connection_lock() -> None:
    hub = SpeechWebSocketHub(send_timeout_seconds=0.02)
    websocket = _WebSocket()
    connection_id = await hub.register("session-a", websocket)
    connection = hub._connections["session-a"]
    await connection.send_lock.acquire()
    try:
        assert not await asyncio.wait_for(
            hub.send_to_connection("session-a", connection_id, {"event": "speech_pong"}),
            timeout=0.5,
        )
    finally:
        connection.send_lock.release()
    assert not websocket.sent_json
    assert websocket.closed
    assert not await hub.is_connected("session-a")


@pytest.mark.asyncio
async def test_speech_cancel_finishes_active_frame_and_keeps_new_stream_usable() -> None:
    hub = SpeechWebSocketHub()
    websocket = _GatedSpeechSocket()
    connection_id = await hub.register("session-a", websocket)
    old = await hub.open_stream("session-a", "old-stream")
    assert old is not None
    assert old.enqueue({"event": "speech_audio", "_pcm": b"\x01\x00"})
    assert old.enqueue({"event": "speech_audio", "_pcm": b"\x02\x00"})
    await asyncio.wait_for(websocket.sending_audio.wait(), timeout=0.5)

    assert await hub.cancel_stream("session-a", connection_id, "old-stream")
    assert old.cancelled.is_set()
    current = await hub.open_stream("session-a", "new-stream")
    assert current is not None
    assert not await hub.cancel_stream("session-a", connection_id, "old-stream")
    assert not current.cancelled.is_set()
    assert current.enqueue({"event": "speech_done"})
    websocket.allow_audio.set()
    await asyncio.wait_for(hub.close_stream(old, drain=False), timeout=0.5)
    await asyncio.wait_for(hub.close_stream(current), timeout=0.5)

    assert websocket.sent_bytes == [b"\x01\x00"]
    assert websocket.sent_json[-1]["speech_stream_id"] == "new-stream"
    assert [kind for kind, _ in websocket.frames] == ["json", "bytes", "json"]
    assert not websocket.closed
    assert await hub.is_connected("session-a")


@pytest.mark.asyncio
async def test_speech_connection_replacement_cancels_old_stream_without_touching_new() -> None:
    hub = SpeechWebSocketHub()
    old_socket = _GatedSpeechSocket()
    old_id = await hub.register("session-a", old_socket)
    old = await hub.open_stream("session-a", "stream-a")
    assert old is not None
    assert old.enqueue({"event": "speech_audio", "_pcm": b"\x00\x00"})
    await asyncio.wait_for(old_socket.sending_audio.wait(), timeout=0.5)
    new_socket = _WebSocket()
    new_id = await hub.register("session-a", new_socket)
    current = await hub.open_stream("session-a", "stream-b")
    assert current is not None
    assert old.cancelled.is_set()
    await hub.unregister("session-a", old_id)
    assert not await hub.cancel_stream("session-a", old_id, "stream-b")
    assert not old.enqueue({"event": "speech_done"})
    assert current.enqueue({"event": "speech_done"})

    await asyncio.wait_for(hub.close_stream(old, drain=False), timeout=0.5)
    await asyncio.wait_for(hub.close_stream(current), timeout=0.5)

    assert not new_socket.closed
    assert not current.cancelled.is_set()
    assert [event["speech_stream_id"] for event in new_socket.sent_json] == ["stream-b"]
    assert await hub.is_connected("session-a")
    await hub.unregister("session-a", new_id)


@pytest.mark.asyncio
async def test_speech_cancel_is_scoped_and_remembers_early_stop_with_bounded_history() -> None:
    hub = SpeechWebSocketHub()
    first_id = await hub.register("session-a", _WebSocket())
    second_id = await hub.register("session-b", _WebSocket())
    second = await hub.open_stream("session-b", "stream-b")
    assert second is not None
    assert not await hub.cancel_stream("session-b", first_id, "stream-b")
    assert not second.cancelled.is_set()
    assert not second.enqueue({"event": "speech_done", "session_id": "session-a"})
    assert not second.enqueue({"event": "speech_done", "speech_stream_id": "wrong"})
    assert await hub.cancel_stream("session-a", first_id, "early-stream")
    assert await hub.open_stream("session-a", "early-stream") is None
    for index in range(100):
        assert await hub.cancel_stream("session-a", first_id, f"early-{index}")
    assert len(hub._connections["session-a"].cancelled_stream_ids) == 64
    assert await hub.open_stream("session-a", "early-99") is None

    await hub.unregister("session-b", second_id)
    assert second.cancelled.is_set()
    await hub.close_stream(second, drain=False)


@pytest.mark.asyncio
async def test_speech_open_stream_checks_socket_owner_before_replacing_delivery() -> None:
    hub = SpeechWebSocketHub()
    owner_token = set_current_auth_user_id("owner-a")
    try:
        connection_id = await hub.register("session-a", _WebSocket())
        delivery = await hub.open_stream("session-a", "owner-stream")
        assert delivery is not None
        attacker_token = set_current_auth_user_id("owner-b")
        try:
            assert not await hub.is_connected("session-a")
            assert await hub.open_stream("session-a", "attacker-stream") is None
            assert not await hub.cancel_stream("session-a", connection_id, "owner-stream")
        finally:
            reset_current_auth_user_id(attacker_token)
        assert not delivery.cancelled.is_set()
        assert await hub.is_connected("session-a")
        await hub.close_stream(delivery)
    finally:
        reset_current_auth_user_id(owner_token)
