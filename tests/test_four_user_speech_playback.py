"""Four isolated playback clients over real routing/pacing with simulated inference.

These tests verify scheduling and isolation, not the physical GPU's audio rate.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.api.routes import rehearsal as route
from backend.config.settings import Settings
from backend.core.session_context import reset_current_auth_user_id, set_current_auth_user_id
from backend.schemas.api import RehearsalMessageRequest
from backend.schemas.state import SessionState
from backend.services.fish_tts_service import FishTtsService
from backend.services.rehearsal_service import RehearsalService
from backend.services.speech_websocket_service import SpeechWebSocketHub


@pytest.mark.parametrize("overrides", [
    {"tts_playback_high_water_seconds": 4, "tts_playback_low_water_seconds": 8},
    {"tts_playback_high_water_seconds": 8, "tts_playback_low_water_seconds": 8},
    {"tts_first_segment_min_chars": 65, "tts_first_segment_max_chars": 64},
])
def test_playback_and_segment_config_reject_inverted_limits(overrides):
    with pytest.raises(ValidationError, match="TTS"):
        Settings(**overrides)


class _GlobalAdmission:
    def __init__(self):
        self.slots = asyncio.BoundedSemaphore(4)
        self.active = 0
        self.peak = 0
        self.all_four_started = asyncio.Event()

    @asynccontextmanager
    async def lease(self, _resource, *, capacity, **_kwargs):
        assert capacity == 4
        async with self.slots:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 4:
                self.all_four_started.set()
            try:
                yield SimpleNamespace(distributed=True)
            finally:
                self.active -= 1


class _Fish(FishTtsService):
    async def ensure_ready(self):
        pass

    async def _stream_sentence(self, _sentence, *, sentence_index, seed, **_kwargs):
        # Prove all four streams enter synthesis simultaneously across replicas.
        if sentence_index == 0:
            await self.coordinator.all_four_started.wait()
        yield {"event": "speech_start", "sample_rate": 8000}
        pcm = int(seed).to_bytes(2, "little") * 800
        for chunk_index in range(3):
            yield self._audio_event(pcm, sentence_index=sentence_index, sample_rate=8000)
            if sentence_index == 0 and chunk_index == 0:
                # Ensure feedback has crossed the outbound queue before this
                # simulated sentence completes; pacing acts on fresh reports.
                await self.coordinator.players[seed - 1].first_audio.wait()
            await asyncio.sleep(0)
        yield {"event": "speech_sentence_done", "sentence_index": sentence_index}


class _Scheduler:
    @asynccontextmanager
    async def slot(self, **_kwargs):
        yield SimpleNamespace(queue_ms=0)


class _Employee:
    text = "我先说一下自己的判断。我们可以核对这些工作的实际结果。然后讨论下一步需要的支持。"

    async def stream_reply(self, *_args, **_kwargs):
        yield self.text


class _Service(RehearsalService):
    def __init__(self, settings, tts):
        self.settings = settings
        self.tts_service = tts
        self.model_scheduler = _Scheduler()
        self.text_finished = {}
        self.buffers = {}

    def _session_speech_seed(self, state):
        return int(state.session_id.rsplit("-", 1)[1]) + 1

    def _session_speech_voice(self, *_args):
        return None

    async def stream_manager_message(self, session_id, message, **kwargs):
        self.buffers[session_id] = kwargs["speech_playback_buffer"]
        async with aclosing(self._stream_employee_reply(
            _Employee(), SessionState(session_id=session_id), message, [],
            speech_enabled=kwargs["speech_enabled"],
            speech_voice=kwargs["speech_voice"],
            speech_stream_id=kwargs["speech_stream_id"],
            speech_cancel_event=kwargs["speech_cancel_event"],
            speech_playback_buffer=kwargs["speech_playback_buffer"],
        )) as stream:
            async for event in stream:
                if event["event"] == "_reply_complete":
                    self.text_finished[session_id] = event["reply"]
                else:
                    yield event
        yield {"event": "done"}


class _Player:
    def __init__(self, hub, session_id, stream_id):
        self.hub = hub
        self.session_id = session_id
        self.stream_id = stream_id
        self.connection_id = None
        self.pcm = []
        self.events = []
        self.first_audio = asyncio.Event()
        self.refill = asyncio.Event()
        self.sequence = 0

    async def send_json(self, event):
        assert event["session_id"] == self.session_id
        assert event["speech_stream_id"] == self.stream_id
        self.events.append(event)

    async def send_bytes(self, data):
        self.pcm.append(data)
        # Simulate a player with a comfortable backlog. Hold the next sentence
        # until the test reports that this particular player needs a refill.
        await self.progress(10.0 if not self.refill.is_set() else 0.0)
        self.first_audio.set()

    async def progress(self, buffered_seconds):
        self.sequence += 1
        assert await self.hub.report_playback(
            self.session_id, self.connection_id, self.stream_id,
            buffered_seconds=buffered_seconds, sequence=self.sequence,
        )

    async def close(self, **_kwargs):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_one", [False, True])
async def test_four_users_play_independently_without_pacing_holding_inference_slots(cancel_one):
    settings = Settings(
        tts_enabled=True, tts_max_concurrency=4, tts_global_max_concurrency=4,
        tts_require_registered_voice=False, tts_provider="fish_vllm_omni",
        tts_sample_rate=8000, tts_queue_timeout_seconds=1,
        psychological_pattern_dynamics_enabled=False,
    )
    coordinator = _GlobalAdmission()
    hubs = [SpeechWebSocketHub(), SpeechWebSocketHub()]
    services = [
        _Service(settings, _Fish(settings, client=object(), coordinator=coordinator))
        for _ in range(2)
    ]
    players = [
        _Player(hubs[i % 2], f"session-{i}", f"speech-four-user-{i}")
        for i in range(4)
    ]
    coordinator.players = players
    results = {}

    async def run_user(index):
        player = players[index]
        token = set_current_auth_user_id(f"user-{index}")
        try:
            player.connection_id = await player.hub.register(player.session_id, player)
            response = await route.stream_message(
                player.session_id,
                RehearsalMessageRequest(
                    message="请说明你的判断。",
                    speech={"enabled": True, "stream_id": player.stream_id},
                ),
                service=services[index % 2], speech_hub=player.hub,
            )
            async with aclosing(response.body_iterator):
                results[index] = [part async for part in response.body_iterator]
        finally:
            reset_current_auth_user_id(token)

    tasks = [asyncio.create_task(run_user(i)) for i in range(4)]
    try:
        async with asyncio.timeout(3):
            await asyncio.gather(*(player.first_audio.wait() for player in players))
            # Allow the current sentence to finish and every stream to reach
            # its playback gate before inspecting shared admission capacity.
            while coordinator.active or any(len(player.pcm) < 3 for player in players):
                await asyncio.sleep(0)
            for _ in range(10):
                await asyncio.sleep(0)

            assert coordinator.peak == 4
            assert coordinator.active == 0
            assert not any(task.done() for task in tasks)
            assert all(
                services[i % 2].text_finished[players[i].session_id] == _Employee.text
                for i in range(4)
            )
            before_refill = [len(player.pcm) for player in players]
            assert before_refill == [3, 3, 3, 3]

            for index, player in enumerate(players):
                token = set_current_auth_user_id(f"user-{index}")
                try:
                    if cancel_one and index == 0:
                        assert await player.hub.cancel_stream(
                            player.session_id, player.connection_id, player.stream_id
                        )
                    else:
                        player.refill.set()
                        await player.progress(0)
                finally:
                    reset_current_auth_user_id(token)
            await asyncio.gather(*tasks)

        assert coordinator.active == 0
        assert coordinator.peak == 4
        for index, player in enumerate(players):
            marker = (index + 1).to_bytes(2, "little") * 800
            assert all(pcm == marker for pcm in player.pcm), "Audio leaked across users"
            assert any("event: done" in event for event in results[index])
            assert not any("speech_error" in event for event in results[index])
            if cancel_one and index == 0:
                assert len(player.pcm) == before_refill[index]
            else:
                assert len(player.pcm) > before_refill[index]
                assert player.events[-1]["event"] == "speech_done"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(hub.shutdown() for hub in hubs))
