from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
from typing import Any

import pytest

from backend.config.settings import Settings
from backend.services.fish_tts_service import FishTtsService
from backend.services.speech_playback_service import SpeechPlaybackBuffer


def _settings(**overrides: Any) -> Settings:
    values = {
        "tts_enabled": True,
        "tts_provider": "fish_vllm_omni",
        "tts_default_voice": "employee",
        "tts_available_voices": "employee",
        "tts_require_registered_voice": False,
        "tts_max_concurrency": 1,
        "tts_global_max_concurrency": 1,
        "tts_queue_timeout_seconds": 0.02,
        "tts_sentence_max_chars": 180,
        "tts_first_segment_min_chars": 16,
        "tts_first_segment_max_chars": 64,
        "tts_first_segment_wait_ms": 20,
        "tts_sentence_merge_max_chars": 80,
    }
    values.update(overrides)
    return Settings(**values)


def _queue(*chunks: str | None) -> asyncio.Queue[str | None]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    for chunk in chunks:
        queue.put_nowait(chunk)
    return queue


@pytest.mark.asyncio
async def test_first_segment_flushes_at_a_soft_boundary_after_its_wait_deadline() -> None:
    service = FishTtsService(_settings(), client=object())
    first = "我们先认真确认这次工作中每项具体安排，"
    tail = "然后再继续讨论后续进度"
    queue = _queue(first + tail)
    async with aclosing(service._iter_sentences(queue)) as segments:
        pending = asyncio.create_task(anext(segments))
        try:
            await asyncio.sleep(0)
            assert not pending.done()
            assert await asyncio.wait_for(pending, 0.5) == first
            queue.put_nowait(None)
            assert [segment async for segment in segments] == [tail]
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_segment_uses_a_soft_boundary_before_its_maximum_target() -> None:
    service = FishTtsService(
        _settings(tts_first_segment_wait_ms=1000), client=object()
    )
    first, tail = "甲" * 20 + "，", "乙" * 50
    queue = _queue(first + tail)
    async with aclosing(service._iter_sentences(queue)) as segments:
        assert await asyncio.wait_for(anext(segments), 0.2) == first
        queue.put_nowait(None)
        assert [segment async for segment in segments] == [tail]


@pytest.mark.asyncio
async def test_first_segment_deadline_never_splits_a_word_without_a_boundary() -> None:
    service = FishTtsService(_settings(), client=object())
    word = "uninterruptedword" * 3
    queue = _queue(word)
    async with aclosing(service._iter_sentences(queue)) as segments:
        pending = asyncio.create_task(anext(segments))
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            queue.put_nowait(" followed by more text")
            first = await asyncio.wait_for(pending, 0.5)
            assert first.startswith(word + " ")
            queue.put_nowait(None)
            rest = [segment async for segment in segments]
            assert first + "".join(rest) == word + " followed by more text"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_soft_segment_respects_the_minimum_length() -> None:
    service = FishTtsService(_settings(), client=object())
    queue = _queue("好，继续")
    async with aclosing(service._iter_sentences(queue)) as segments:
        pending = asyncio.create_task(anext(segments))
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            queue.put_nowait("讨论下一步的具体安排。")
            assert await asyncio.wait_for(pending, 0.5) == "好，继续讨论下一步的具体安排。"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_decimal_split_across_deltas_is_kept_in_one_segment() -> None:
    service = FishTtsService(_settings(), client=object())
    queue = _queue("本季度利润率为3.")
    async with aclosing(service._iter_sentences(queue)) as segments:
        pending = asyncio.create_task(anext(segments))
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()
            queue.put_nowait("14%，我们继续讨论。")
            assert await asyncio.wait_for(pending, 0.5) == "本季度利润率为3.14%，我们继续讨论。"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_later_short_sentences_merge_only_text_already_in_the_queue() -> None:
    service = FishTtsService(_settings(), client=object())
    queue = _queue("第一句。", "第二句。", "第三句。", None)
    assert [segment async for segment in service._iter_sentences(queue)] == [
        "第一句。",
        "第二句。第三句。",
    ]


@pytest.mark.asyncio
async def test_later_sentence_does_not_wait_for_an_unfinished_neighbour() -> None:
    service = FishTtsService(_settings(), client=object())
    queue = _queue("第一句。")
    async with aclosing(service._iter_sentences(queue)) as segments:
        assert await anext(segments) == "第一句。"
        queue.put_nowait("第二句。第三句仍未写完")
        assert await asyncio.wait_for(anext(segments), 0.2) == "第二句。"
        queue.put_nowait("。")
        assert await asyncio.wait_for(anext(segments), 0.2) == "第三句仍未写完。"


@pytest.mark.asyncio
async def test_merged_segments_stay_bounded_and_preserve_all_text() -> None:
    service = FishTtsService(_settings(), client=object())
    text = "首句。" + ("甲" * 29 + "。") * 8
    segments = [
        segment async for segment in service._iter_sentences(_queue(text, None))
    ]
    assert segments[0] == "首句。"
    assert "".join(segments) == text
    assert [len(segment) for segment in segments[1:]] == [60, 60, 60, 60]


@pytest.mark.asyncio
async def test_hard_limit_bounds_long_sentences_even_with_a_distant_full_stop() -> None:
    service = FishTtsService(_settings(), client=object())
    text = "甲" * 400 + "。"
    segments = [
        segment async for segment in service._iter_sentences(_queue(text, None))
    ]
    assert "".join(segments) == text
    assert [len(segment) for segment in segments] == [180, 180, 41]


@pytest.mark.asyncio
async def test_segmentation_preserves_whitespace_punctuation_and_text_order() -> None:
    service = FishTtsService(_settings(), client=object())
    chunks = ("  开始。\n", "Next sentence has 3.", "14 units.  ", "最后一句。\n ")
    segments = [
        segment async for segment in service._iter_sentences(_queue(*chunks, None))
    ]
    assert "".join(segments).rstrip() == "".join(chunks).rstrip()
    assert all(segment.strip() for segment in segments)
    assert any("3.14" in segment for segment in segments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ((" \n\t", None), []),
        (("内容。", "\n  \t", None), ["内容。"]),
    ],
)
async def test_whitespace_never_becomes_an_independent_segment_or_tts_request(
    chunks, expected
) -> None:
    service = _RecordingTts()
    segments = [segment async for segment in service._iter_sentences(_queue(*chunks))]
    assert segments == expected
    events = await asyncio.wait_for(_collect(service, _queue(*chunks)), 0.5)
    assert service.sentences == expected
    assert events[-1]["event"] == "speech_done"


@pytest.mark.asyncio
async def test_leading_whitespace_does_not_disable_first_segment_deadline() -> None:
    service = FishTtsService(_settings(), client=object())
    first = "我们先认真确认这次工作中每项具体安排，"
    tail = "然后再继续讨论后续进度"
    queue = _queue("\n \t", first + tail)
    async with aclosing(service._iter_sentences(queue)) as segments:
        assert await asyncio.wait_for(anext(segments), 0.5) == "\n \t" + first
        queue.put_nowait(None)
        assert [segment async for segment in segments] == [tail]


class _Coordinator:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0

    @asynccontextmanager
    async def lease(self, *_args, **_kwargs):
        self.calls += 1
        self.active += 1
        try:
            yield None
        finally:
            self.active -= 1


class _RecordingTts(FishTtsService):
    def __init__(self, coordinator=None, *, on_sentence=None) -> None:
        super().__init__(_settings(), client=object(), coordinator=coordinator)
        self.sentences: list[str] = []
        self.on_sentence = on_sentence

    async def _stream_sentence(self, sentence, **kwargs):
        self.sentences.append(sentence)
        if self.on_sentence is not None:
            self.on_sentence(len(self.sentences))
        yield {"event": "speech_sentence_done", "sentence_index": kwargs["sentence_index"]}


async def _collect(service, queue, playback_buffer=None):
    return [
        event
        async for event in service.stream(
            queue, emotion_state=None, playback_buffer=playback_buffer
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("with_buffer", [False, True])
async def test_clients_without_playback_feedback_are_admitted_immediately(with_buffer) -> None:
    service = _RecordingTts()
    playback = SpeechPlaybackBuffer() if with_buffer else None
    events = await asyncio.wait_for(
        _collect(service, _queue("正常播放。", " \n", None), playback), 0.5
    )
    assert events[-1]["event"] == "speech_done"
    assert service.sentences == ["正常播放。"]


@pytest.mark.asyncio
async def test_playback_wait_holds_no_inference_slot_or_admission_deadline() -> None:
    playback = SpeechPlaybackBuffer(feedback_timeout_seconds=1)
    playback.report(9, 0)
    coordinator = _Coordinator()
    service = _RecordingTts(coordinator)
    task = asyncio.create_task(_collect(service, _queue("等待播放容量。", None), playback))
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not service._slots.locked()
        assert coordinator.calls == 0
        assert service.sentences == []
        playback.report(2, 1)
        events = await asyncio.wait_for(task, 0.5)
        assert events[-1]["event"] == "speech_done"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not service._slots.locked()
    assert coordinator.active == 0


@pytest.mark.asyncio
async def test_cancel_during_playback_wait_does_not_block_later_requests() -> None:
    playback = SpeechPlaybackBuffer(feedback_timeout_seconds=1)
    playback.report(9, 0)
    coordinator = _Coordinator()
    service = _RecordingTts(coordinator)
    task = asyncio.create_task(_collect(service, _queue("取消等待。", None), playback))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._slots.locked()
    assert coordinator.calls == 0
    events = await asyncio.wait_for(_collect(service, _queue("下一轮。", None)), 0.5)
    assert events[-1]["event"] == "speech_done"
    assert service.sentences == ["下一轮。"]


@pytest.mark.asyncio
async def test_playback_capacity_is_checked_again_before_each_sentence() -> None:
    class ObservedBuffer(SpeechPlaybackBuffer):
        def __init__(self):
            super().__init__(feedback_timeout_seconds=1)
            self.waits = 0
            self.second_wait = asyncio.Event()

        async def wait_for_capacity(self):
            self.waits += 1
            if self.waits == 2:
                self.second_wait.set()
            await super().wait_for_capacity()

    playback = ObservedBuffer()
    coordinator = _Coordinator()
    service = _RecordingTts(
        coordinator,
        on_sentence=lambda count: playback.report(9, 0) if count == 1 else None,
    )
    task = asyncio.create_task(
        _collect(service, _queue("第一句。第二句。第三句。", None), playback)
    )
    try:
        await asyncio.wait_for(playback.second_wait.wait(), 0.5)
        assert service.sentences == ["第一句。"]
        assert coordinator.calls == 1
        assert coordinator.active == 0
        assert not service._slots.locked()
        playback.report(1, 1)
        assert (await asyncio.wait_for(task, 0.5))[-1]["event"] == "speech_done"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert service.sentences == ["第一句。", "第二句。第三句。"]
    assert coordinator.calls == 2
