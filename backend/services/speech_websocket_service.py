from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from backend.core.session_context import get_current_auth_user_id
from backend.services.speech_playback_service import SpeechPlaybackBuffer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SpeechConnection:
    connection_id: str
    session_id: str
    websocket: WebSocket
    owner_user_id: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: bool = True
    stream: SpeechStreamDelivery | None = None
    cancelled_stream_ids: dict[str, None] = field(default_factory=dict)
    opened_stream_ids: dict[str, None] = field(default_factory=dict)


class SpeechStreamDelivery:
    """A bounded outbound stream tied to the socket that accepted the turn."""

    def __init__(
        self,
        hub: SpeechWebSocketHub,
        connection: _SpeechConnection,
        stream_id: str,
    ) -> None:
        self.cancelled = asyncio.Event()
        self.playback = SpeechPlaybackBuffer(
            high_water_seconds=hub.playback_high_water_seconds,
            low_water_seconds=hub.playback_low_water_seconds,
            feedback_timeout_seconds=hub.playback_feedback_timeout_seconds,
        )
        self._hub = hub
        self._connection = connection
        self.stream_id = stream_id
        self._queue: deque[tuple[dict[str, Any], int]] = deque()
        self._buffered_bytes = 0
        self._buffered_events = 0
        self._wake = asyncio.Event()
        self._closing = False
        self._terminal_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    def enqueue(self, event: dict[str, Any]) -> bool:
        if self._closing or self.cancelled.is_set() or not self._connection.active:
            return False
        if (
            event.get("session_id", self._connection.session_id)
            != self._connection.session_id
            or event.get("speech_stream_id", self.stream_id) != self.stream_id
        ):
            return False
        payload = dict(event)
        payload.setdefault("session_id", self._connection.session_id)
        payload.setdefault("speech_stream_id", self.stream_id)
        raw_pcm = payload.get("_pcm")
        metadata = {key: value for key, value in payload.items() if key != "_pcm"}
        try:
            size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            self._cancel("语音数据格式错误，本轮文字回复不受影响。")
            return False
        if isinstance(raw_pcm, (bytes, bytearray, memoryview)):
            size += memoryview(raw_pcm).nbytes
        if (
            self._buffered_events >= self._hub.max_queue_events
            or self._buffered_bytes + size > self._hub.max_queue_bytes
        ):
            self._cancel("语音播放通道积压，本轮语音已停止，文字回复不受影响。")
            return False
        self._queue.append((payload, size))
        self._buffered_events += 1
        self._buffered_bytes += size
        self._wake.set()
        return True

    def _cancel(self, error_message: str | None = None) -> None:
        if error_message is not None:
            self._terminal_error = error_message
        self.cancelled.set()
        self.playback.cancel()
        self._closing = True
        self._queue.clear()
        self._buffered_bytes = 0
        self._buffered_events = 0
        self._wake.set()
        # Finish an already-started metadata/PCM pair under the send deadline.
        # Interrupting between those frames would make this socket unusable.


class SpeechWebSocketHub:
    """Routes synthesized speech only to the active socket for one workflow session."""

    def __init__(
        self,
        send_timeout_seconds: float = 5.0,
        max_queue_bytes: int = 2_097_152,
        max_queue_events: int = 32,
        playback_high_water_seconds: float = 8.0,
        playback_low_water_seconds: float = 4.0,
        playback_feedback_timeout_seconds: float = 3.0,
    ) -> None:
        if send_timeout_seconds <= 0 or max_queue_bytes < 1 or max_queue_events < 1:
            raise ValueError("Speech delivery limits must be positive.")
        self.send_timeout_seconds = send_timeout_seconds
        self.max_queue_bytes = max_queue_bytes
        self.max_queue_events = max_queue_events
        SpeechPlaybackBuffer.validate_limits(
            playback_high_water_seconds,
            playback_low_water_seconds,
            playback_feedback_timeout_seconds,
        )
        self.playback_high_water_seconds = playback_high_water_seconds
        self.playback_low_water_seconds = playback_low_water_seconds
        self.playback_feedback_timeout_seconds = playback_feedback_timeout_seconds
        self._connections: dict[str, _SpeechConnection] = {}
        self._lock = asyncio.Lock()
        self._deliveries: set[SpeechStreamDelivery] = set()

    async def register(self, session_id: str, websocket: WebSocket) -> str:
        connection = _SpeechConnection(
            connection_id=uuid.uuid4().hex,
            session_id=session_id,
            websocket=websocket,
            owner_user_id=get_current_auth_user_id(),
        )
        async with self._lock:
            previous = self._connections.get(session_id)
            self._connections[session_id] = connection
            if previous is not None:
                self._retire(previous)

        if previous is not None:
            await self._close_connection(
                previous,
                code=1000,
                reason="Replaced by a newer speech connection",
            )
        return connection.connection_id

    async def unregister(self, session_id: str, connection_id: str) -> None:
        async with self._lock:
            current = self._connections.get(session_id)
            if current is not None and current.connection_id == connection_id:
                self._connections.pop(session_id, None)
                self._retire(current)

    @staticmethod
    def _retire(connection: _SpeechConnection) -> None:
        connection.active = False
        if connection.stream is not None:
            connection.stream._cancel()

    @staticmethod
    def _remember_stream(records: dict[str, None], stream_id: str) -> None:
        records[stream_id] = None
        if len(records) > 64:
            records.pop(next(iter(records)))

    async def open_stream(
        self, session_id: str, stream_id: str
    ) -> SpeechStreamDelivery | None:
        if not stream_id or len(stream_id) > 128:
            return None
        async with self._lock:
            connection = self._connections.get(session_id)
            if (
                connection is None
                or connection.owner_user_id != get_current_auth_user_id()
                or stream_id in connection.cancelled_stream_ids
            ):
                return None
            if stream_id in connection.opened_stream_ids:
                return None
            self._remember_stream(connection.opened_stream_ids, stream_id)
            if connection.stream is not None:
                connection.stream._cancel()
            delivery = SpeechStreamDelivery(self, connection, stream_id)
            connection.stream = delivery
            self._deliveries.add(delivery)
            delivery._task = asyncio.create_task(self._deliver(delivery))
            return delivery

    async def cancel_stream(
        self, session_id: str, connection_id: str, stream_id: str
    ) -> bool:
        if not stream_id or len(stream_id) > 128:
            return False
        async with self._lock:
            connection = self._connections.get(session_id)
            if (
                connection is None
                or connection.connection_id != connection_id
                or connection.owner_user_id != get_current_auth_user_id()
            ):
                return False
            delivery = connection.stream
            if delivery is not None and delivery.stream_id == stream_id:
                self._remember_stream(connection.cancelled_stream_ids, stream_id)
                delivery._cancel()
                return True
            if stream_id in connection.opened_stream_ids:
                return False
            # The stop message may overtake the HTTP request starting this turn.
            self._remember_stream(connection.cancelled_stream_ids, stream_id)
            return True

    async def report_playback(
        self,
        session_id: str,
        connection_id: str,
        stream_id: str,
        buffered_seconds: object,
        sequence: object,
    ) -> bool:
        if not isinstance(stream_id, str) or not stream_id or len(stream_id) > 128:
            return False
        async with self._lock:
            connection = self._connections.get(session_id)
            if (
                connection is None
                or not connection.active
                or connection.connection_id != connection_id
                or connection.owner_user_id != get_current_auth_user_id()
            ):
                return False
            delivery = connection.stream
            if (
                delivery is None
                or delivery.stream_id != stream_id
                or delivery._closing
                or delivery.cancelled.is_set()
            ):
                return False
            return delivery.playback.report(buffered_seconds, sequence)

    async def close_stream(
        self, delivery: SpeechStreamDelivery, drain: bool = True
    ) -> None:
        if delivery._hub is not self:
            return
        delivery.playback.cancel()
        if drain:
            delivery._closing = True
            delivery._wake.set()
        else:
            delivery._cancel()
        if delivery._task is not None:
            try:
                await asyncio.shield(delivery._task)
            except asyncio.CancelledError:
                delivery._cancel()
                raise

    async def _deliver(self, delivery: SpeechStreamDelivery) -> None:
        try:
            while not delivery.cancelled.is_set():
                if delivery._queue:
                    event, size = delivery._queue.popleft()
                    sent = await self._send(delivery._connection, event, delivery=delivery)
                    delivery._buffered_events = max(0, delivery._buffered_events - 1)
                    delivery._buffered_bytes = max(0, delivery._buffered_bytes - size)
                    if not sent:
                        delivery._cancel()
                    continue
                if delivery._closing:
                    break
                delivery._wake.clear()
                await delivery._wake.wait()
        except asyncio.CancelledError:
            delivery._cancel()
            raise
        except Exception:
            delivery._cancel("语音播放通道异常，本轮文字回复不受影响。")
            logger.exception(
                "Speech delivery failed for session_id=%s", delivery._connection.session_id
            )
        finally:
            try:
                if delivery._terminal_error is not None:
                    await self._send(
                        delivery._connection,
                        {
                            "event": "speech_error",
                            "session_id": delivery._connection.session_id,
                            "speech_stream_id": delivery.stream_id,
                            "error_type": "SpeechDeliveryError",
                            "message": delivery._terminal_error,
                        },
                        delivery=delivery,
                        allow_cancelled=True,
                    )
            finally:
                delivery._closing = True
                delivery.playback.cancel()
                if delivery._connection.stream is delivery:
                    delivery._connection.stream = None
                self._deliveries.discard(delivery)

    async def is_connected(self, session_id: str) -> bool:
        async with self._lock:
            connection = self._connections.get(session_id)
            return (
                connection is not None
                and connection.owner_user_id == get_current_auth_user_id()
            )

    async def send_to_connection(
        self,
        session_id: str,
        connection_id: str,
        event: dict[str, Any],
    ) -> bool:
        connection = await self._current_connection(session_id)
        if connection is None or connection.connection_id != connection_id:
            return False
        return await self._send(connection, event)

    async def publish(self, session_id: str, event: dict[str, Any]) -> bool:
        connection = await self._current_connection(session_id)
        if connection is None:
            return False
        if await self._send(connection, event):
            return True
        await self.unregister(session_id, connection.connection_id)
        return False

    async def shutdown(self) -> None:
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
            for connection in connections:
                self._retire(connection)
        deliveries = list(self._deliveries)
        for delivery in deliveries:
            delivery._cancel()
        await asyncio.gather(
            *(
                self._close_connection(
                    connection,
                    code=1001,
                    reason="Server shutting down",
                )
                for connection in connections
            ),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(delivery._task for delivery in deliveries if delivery._task is not None),
            return_exceptions=True,
        )

    async def _current_connection(
        self,
        session_id: str,
    ) -> _SpeechConnection | None:
        async with self._lock:
            return self._connections.get(session_id)

    async def _send(
        self,
        connection: _SpeechConnection,
        event: dict[str, Any],
        *,
        delivery: SpeechStreamDelivery | None = None,
        allow_cancelled: bool = False,
    ) -> bool:
        payload = dict(event)
        pcm: bytes | None = None
        if payload.get("event") == "speech_audio":
            raw_pcm = payload.pop("_pcm", None)
            encoded = str(payload.pop("audio", "") or "")
            try:
                if isinstance(raw_pcm, (bytes, bytearray, memoryview)):
                    pcm = bytes(raw_pcm)
                    if not pcm:
                        raise ValueError("empty PCM payload")
                else:
                    pcm = base64.b64decode(encoded, validate=True)
                    if not pcm:
                        raise ValueError("empty PCM payload")
            except (binascii.Error, ValueError):
                pcm = None
                logger.warning(
                    "Invalid synthesized audio payload for session_id=%s",
                    connection.session_id,
                    exc_info=True,
                )
                payload = {
                    "event": "speech_error",
                    "message": "语音数据格式错误，本轮文字回复不受影响。",
                    "error_type": "InvalidAudioPayload",
                    "session_id": event.get("session_id") or connection.session_id,
                    "speech_stream_id": event.get("speech_stream_id"),
                }
            else:
                payload["byte_length"] = len(pcm)
                payload["transport"] = "binary"
                payload["encoding"] = "pcm_s16le"

        sending = False
        try:
            async with asyncio.timeout(self.send_timeout_seconds):
                async with connection.send_lock:
                    if not connection.active or (
                        delivery is not None
                        and (
                            connection.stream is not delivery
                            or (delivery.cancelled.is_set() and not allow_cancelled)
                        )
                    ):
                        return False
                    sending = True
                    await connection.websocket.send_json(payload)
                    if pcm is not None:
                        await connection.websocket.send_bytes(pcm)
            return True
        except asyncio.CancelledError:
            if sending:
                await self._disconnect(connection, reason="Speech frame send interrupted")
            raise
        except (TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
            if not sending and delivery is not None and (
                connection.stream is not delivery
                or (delivery.cancelled.is_set() and not allow_cancelled)
            ):
                return False
            logger.debug(
                "Speech WebSocket send failed for session_id=%s",
                connection.session_id,
                exc_info=True,
            )
            await self._disconnect(connection, reason="Speech connection is unavailable")
        return False

    async def _disconnect(self, connection: _SpeechConnection, *, reason: str) -> None:
        self._retire(connection)
        await self.unregister(connection.session_id, connection.connection_id)
        await self._close_connection(connection, code=1011, reason=reason)

    async def _close_connection(
        self,
        connection: _SpeechConnection,
        *,
        code: int,
        reason: str,
    ) -> None:
        with suppress(TimeoutError, WebSocketDisconnect, RuntimeError, OSError):
            async with asyncio.timeout(self.send_timeout_seconds):
                await connection.websocket.close(code=code, reason=reason)
