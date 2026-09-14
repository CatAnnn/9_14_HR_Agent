from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.core.session_context import (
    reset_current_auth_user_id,
    set_current_auth_user_id,
)
from backend.services.speech_playback_service import SpeechPlaybackBuffer
from backend.services.speech_websocket_service import SpeechWebSocketHub


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    async def send_json(self, _payload: dict[str, Any]) -> None:
        pass

    async def send_bytes(self, _payload: bytes) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


async def _start_paused_wait(buffer: SpeechPlaybackBuffer) -> asyncio.Task[None]:
    task = asyncio.create_task(buffer.wait_for_capacity())
    await asyncio.sleep(0)
    assert not task.done()
    return task


@pytest.mark.asyncio
async def test_playback_admits_legacy_clients_without_feedback() -> None:
    buffer = SpeechPlaybackBuffer()
    await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)
    assert buffer.report(7.0, 0)
    await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)


@pytest.mark.asyncio
async def test_playback_uses_high_low_hysteresis_and_rejects_stale_sequences() -> None:
    buffer = SpeechPlaybackBuffer()
    assert buffer.report(8.0, 1)
    waiting = await _start_paused_wait(buffer)
    try:
        assert buffer.report(6.0, 2)
        await asyncio.sleep(0)
        assert not waiting.done()
        assert not buffer.report(0.0, 2)
        assert not buffer.report(0.0, 1)
        await asyncio.sleep(0)
        assert not waiting.done()
        assert buffer.report(4.0, 3)
        await asyncio.wait_for(waiting, timeout=0.1)
        assert buffer.report(6.0, 4)
        await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)
    finally:
        buffer.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_playback_expired_high_feedback_stops_throttling() -> None:
    buffer = SpeechPlaybackBuffer(feedback_timeout_seconds=0.02)
    assert buffer.report(120.0, 1)
    waiting = await _start_paused_wait(buffer)

    await asyncio.wait_for(waiting, timeout=0.5)
    await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)
    assert buffer.report(6.0, 2)
    await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)


@pytest.mark.asyncio
async def test_playback_new_feedback_after_gap_does_not_preserve_old_pause() -> None:
    buffer = SpeechPlaybackBuffer(feedback_timeout_seconds=0.01)
    assert buffer.report(9.0, 1)
    await asyncio.sleep(0.02)
    assert buffer.report(6.0, 2)
    await asyncio.wait_for(buffer.wait_for_capacity(), timeout=0.1)


@pytest.mark.asyncio
async def test_playback_cancel_wakes_waiter_and_never_admits_more_sentences() -> None:
    buffer = SpeechPlaybackBuffer()
    assert buffer.report(9.0, 1)
    waiting = await _start_paused_wait(buffer)
    buffer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, timeout=0.1)
    assert not buffer.report(0.0, 2)
    with pytest.raises(asyncio.CancelledError):
        await buffer.wait_for_capacity()


@pytest.mark.parametrize(
    ("buffered", "sequence"),
    [
        (True, 1),
        ("8", 1),
        (None, 1),
        (-0.1, 1),
        (120.1, 1),
        (float("nan"), 1),
        (float("inf"), 1),
        (float("-inf"), 1),
        (10 ** 500, 1),
        (8.0, True),
        (8.0, False),
        (8.0, "1"),
        (8.0, 1.0),
        (8.0, None),
        (8.0, -1),
    ],
)
def test_playback_rejects_invalid_feedback_without_advancing_sequence(
    buffered: object, sequence: object
) -> None:
    buffer = SpeechPlaybackBuffer()
    assert not buffer.report(buffered, sequence)
    assert buffer.report(0.0, 0)


@pytest.mark.asyncio
async def test_four_users_playback_buffers_are_independent() -> None:
    hub = SpeechWebSocketHub()
    deliveries = []
    connection_ids = []
    waiting = []
    try:
        for index in range(4):
            token = set_current_auth_user_id(f"user-{index}")
            try:
                session_id = f"session-{index}"
                stream_id = f"stream-{index}"
                connection_id = await hub.register(session_id, _Socket())
                delivery = await hub.open_stream(session_id, stream_id)
                assert delivery is not None
                assert await hub.report_playback(
                    session_id, connection_id, stream_id, 10.0, 1
                )
                deliveries.append(delivery)
                connection_ids.append(connection_id)
            finally:
                reset_current_auth_user_id(token)
        for delivery in deliveries:
            waiting.append(await _start_paused_wait(delivery.playback))

        token = set_current_auth_user_id("user-0")
        try:
            assert await hub.report_playback(
                "session-0", connection_ids[0], "stream-0", 4.0, 2
            )
            assert not await hub.report_playback(
                "session-1", connection_ids[1], "stream-1", 0.0, 2
            )
        finally:
            reset_current_auth_user_id(token)
        await asyncio.wait_for(waiting[0], timeout=0.1)
        assert all(not task.done() for task in waiting[1:])
    finally:
        await hub.shutdown()
        results = await asyncio.gather(*waiting, return_exceptions=True)
    assert results[0] is None
    assert all(isinstance(result, asyncio.CancelledError) for result in results[1:])


@pytest.mark.asyncio
async def test_playback_feedback_validates_connection_stream_sequence_and_values() -> None:
    hub = SpeechWebSocketHub()
    connection_id = await hub.register("session-a", _Socket())
    other_id = await hub.register("session-b", _Socket())
    delivery = await hub.open_stream("session-a", "stream-a")
    assert delivery is not None
    try:
        assert not await hub.report_playback("session-a", other_id, "stream-a", 8.0, 1)
        assert not await hub.report_playback("session-a", connection_id, "unknown", 8.0, 1)
        assert not await hub.report_playback("session-a", connection_id, "stream-a", True, 1)
        assert not await hub.report_playback("session-a", connection_id, "stream-a", 8.0, True)
        assert await hub.report_playback("session-a", connection_id, "stream-a", 8.0, 1)
        waiting = await _start_paused_wait(delivery.playback)
        assert not await hub.report_playback("session-a", connection_id, "stream-a", 0.0, 1)
        assert not waiting.done()
        assert await hub.cancel_stream("session-a", connection_id, "stream-a")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=0.1)
        assert not await hub.report_playback("session-a", connection_id, "stream-a", 0.0, 2)
    finally:
        await hub.shutdown()


@pytest.mark.asyncio
async def test_playback_replacement_wakes_old_waiter_and_ignores_old_feedback() -> None:
    hub = SpeechWebSocketHub()
    old_id = await hub.register("session-a", _Socket())
    old = await hub.open_stream("session-a", "stream-old")
    assert old is not None
    assert await hub.report_playback("session-a", old_id, "stream-old", 10.0, 1)
    waiting = await _start_paused_wait(old.playback)
    try:
        new_id = await hub.register("session-a", _Socket())
        current = await hub.open_stream("session-a", "stream-new")
        assert current is not None
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=0.1)
        assert not await hub.report_playback("session-a", old_id, "stream-new", 120.0, 2)
        assert not await hub.report_playback("session-a", new_id, "stream-old", 120.0, 2)
        await asyncio.wait_for(current.playback.wait_for_capacity(), timeout=0.1)
    finally:
        await hub.shutdown()


@pytest.mark.asyncio
async def test_playback_new_stream_on_same_socket_does_not_inherit_old_pause() -> None:
    hub = SpeechWebSocketHub()
    connection_id = await hub.register("session-a", _Socket())
    old = await hub.open_stream("session-a", "stream-old")
    assert old is not None
    assert await hub.report_playback("session-a", connection_id, "stream-old", 10.0, 1)
    waiting = await _start_paused_wait(old.playback)
    try:
        current = await hub.open_stream("session-a", "stream-new")
        assert current is not None
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=0.1)
        assert not await hub.report_playback("session-a", connection_id, "stream-old", 120.0, 2)
        await asyncio.wait_for(current.playback.wait_for_capacity(), timeout=0.1)
    finally:
        await hub.shutdown()
