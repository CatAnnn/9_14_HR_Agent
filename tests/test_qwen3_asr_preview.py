from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import numpy as np
import pytest


@pytest.fixture(scope="module")
def asr_module():
    torch_stub = ModuleType("torch")
    torch_stub.bfloat16 = object()
    huggingface_hub_stub = ModuleType("huggingface_hub")
    huggingface_hub_stub.snapshot_download = object()
    qwen_stub = ModuleType("qwen_asr")
    qwen_stub.Qwen3ASRModel = SimpleNamespace(LLM=object)
    previous_torch = sys.modules.get("torch")
    previous_huggingface_hub = sys.modules.get("huggingface_hub")
    previous_qwen = sys.modules.get("qwen_asr")
    sys.modules["torch"] = torch_stub
    sys.modules["huggingface_hub"] = huggingface_hub_stub
    sys.modules["qwen_asr"] = qwen_stub
    module_name = "_hr_agent_qwen3_asr_preview"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path("scripts/run_qwen3_asr.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(module_name, None)
        if previous_torch is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = previous_torch
        if previous_huggingface_hub is None:
            sys.modules.pop("huggingface_hub", None)
        else:
            sys.modules["huggingface_hub"] = previous_huggingface_hub
        if previous_qwen is None:
            sys.modules.pop("qwen_asr", None)
        else:
            sys.modules["qwen_asr"] = previous_qwen


def _session(asr_module):
    return asr_module.PreviewSession(
        window_samples=12 * asr_module.SAMPLE_RATE,
        overlap_samples=int(1.5 * asr_module.SAMPLE_RATE),
        silence_commit_ms=750,
        force_refresh_ms=1500,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
    )


def _result(asr_module, text: str):
    return asr_module.InferenceResult(
        language="Chinese",
        text=text,
        queue_ms=3.0,
        inference_ms=120.0,
    )


def test_resolve_model_path_completes_remote_snapshot(asr_module, monkeypatch):
    calls: list[str] = []

    def fake_snapshot_download(model_path: str) -> str:
        calls.append(model_path)
        return "/cache/complete-snapshot"

    monkeypatch.setattr(asr_module, "snapshot_download", fake_snapshot_download)

    resolved = asr_module.resolve_model_path("Qwen/Qwen3-ASR-1.7B")

    assert resolved == "/cache/complete-snapshot"
    assert calls == ["Qwen/Qwen3-ASR-1.7B"]


def test_resolve_model_path_preserves_existing_local_directory(
    asr_module,
    monkeypatch,
    tmp_path,
):
    def unexpected_download(_model_path: str) -> str:
        raise AssertionError("local model paths must not trigger a download")

    monkeypatch.setattr(asr_module, "snapshot_download", unexpected_download)

    assert asr_module.resolve_model_path(str(tmp_path)) == str(tmp_path)


def test_cli_defaults_to_bounded_sessions(asr_module, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_qwen3_asr.py"])

    args = asr_module.parse_args()

    assert args.max_num_seqs == 11
    assert args.max_sessions == 16


def test_rolling_window_never_exceeds_twelve_seconds(asr_module):
    session = _session(asr_module)
    long_audio = np.full(20 * asr_module.SAMPLE_RATE, 0.1, dtype=np.float32)

    prepared = session.prepare_inference(long_audio, now=1.0)

    assert prepared is not None
    assert prepared.audio.size == 12 * asr_module.SAMPLE_RATE
    assert session.snapshot()["window_seconds"] == 12.0


def test_silence_commits_stable_text_without_later_rollback(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, "第一句话"))

    commit = session.prepare_inference(silence, now=1.75)
    assert commit is not None and commit.commit_after is True
    assert commit.requires_decode is False
    committed = session.seal(commit)
    assert committed["stable_text"] == "第一句话"
    assert committed["unstable_text"] == ""

    next_part = session.prepare_inference(speech, now=2.5)
    assert next_part is not None
    updated = session.apply_result(next_part, _result(asr_module, "第二句话"))
    assert updated["stable_text"] == "第一句话"
    assert updated["text"].startswith("第一句话")
    assert "第二句话" in updated["text"]


def test_silence_commit_drops_overlap_and_preserves_intentional_repetition(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, "之前说过的话"))

    commit = session.prepare_inference(silence, now=1.75)
    assert commit is not None and commit.commit_after is True
    assert commit.requires_decode is False
    committed = session.seal(commit)

    assert commit.finish_stream is False
    assert committed["correction_state"] == "finalized"
    assert session.audio_window.size == 0

    repeated = session.prepare_inference(speech, now=2.5)
    assert repeated is not None
    snapshot = session.apply_result(
        repeated,
        _result(asr_module, "之前说过的话"),
    )

    assert snapshot["stable_text"] == "之前说过的话"
    assert snapshot["unstable_text"] == "之前说过的话"
    assert snapshot["text"] == "之前说过的话之前说过的话"


def test_silence_commit_seals_without_redecoding_owned_speech(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)
    sentence = "我们需要制定清晰的改进计划"

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, sentence))

    commit = session.prepare_inference(silence, now=1.75)
    assert commit is not None and commit.commit_reason == "silence"
    assert commit.requires_decode is False
    snapshot = session.apply_result(
        commit,
        _result(asr_module, f"{sentence}， {sentence}。"),
    )

    assert snapshot["stable_text"] == sentence
    assert snapshot["text"] == sentence


def test_stop_seals_already_committed_speech_without_decode(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)
    sentence = "下一步需要明确目标和完成时间"

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, sentence))
    commit = session.prepare_inference(silence, now=1.75)
    assert commit is not None and commit.commit_reason == "silence"
    assert commit.requires_decode is False
    session.seal(commit)

    finish = session.prepare_finish()
    assert finish.requires_decode is False
    snapshot = session.seal(finish)

    assert snapshot["stable_text"] == sentence
    assert snapshot["text"] == sentence


def test_stop_preserves_repetition_when_new_speech_is_buffered(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    sentence = "这项工作需要重新确认交付范围"

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, sentence))

    repeated_audio = session.prepare_inference(speech, now=1.75)
    assert repeated_audio is not None
    session.apply_result(
        repeated_audio,
        asr_module.InferenceResult(
            language="Chinese",
            text="",
            queue_ms=1.0,
            inference_ms=0.0,
            decoded_chunks=0,
        ),
    )

    finish = session.prepare_finish()
    assert finish.requires_decode is True
    snapshot = session.apply_result(
        finish,
        _result(asr_module, f"{sentence}，{sentence}"),
    )

    assert snapshot["stable_text"] == f"{sentence},{sentence}"


def test_stop_does_not_redecode_short_hypothesis_without_new_speech(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, "是的"))

    finish = session.prepare_finish()
    assert finish.requires_decode is False
    snapshot = session.apply_result(finish, _result(asr_module, "是的，是的"))

    assert snapshot["stable_text"] == "是的"
    assert snapshot["text"] == "是的"


def test_legitimate_repetition_inside_one_hypothesis_is_preserved(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, "是的，是的"))

    finish = session.prepare_finish()
    assert finish.requires_decode is False
    snapshot = session.seal(finish)

    assert snapshot["stable_text"] == "是的,是的"


def test_prepared_sequence_can_only_mutate_transcript_once(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)

    prepared = session.prepare_inference(speech, now=1.0)
    assert prepared is not None
    first = session.apply_result(prepared, _result(asr_module, "一次结果"))
    first_revision = first["revision"]
    first_segment = first["segment_id"]

    replayed = session.apply_result(
        prepared,
        _result(asr_module, "一次结果一次结果"),
    )

    assert replayed["text"] == "一次结果"
    assert replayed["revision"] == first_revision
    assert replayed["segment_id"] == first_segment

def test_seal_sequence_can_only_advance_commit_once(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)

    prepared = session.prepare_inference(speech, now=1.0)
    assert prepared is not None
    session.apply_result(prepared, _result(asr_module, "只提交一次"))
    commit = session.prepare_inference(silence, now=1.75)
    assert commit is not None
    assert commit.requires_decode is False

    session.seal(commit)
    committed_revision = session.revision
    committed_segment = session.segment_id
    session.seal(commit)

    assert session.stable_text == "只提交一次"
    assert session.revision == committed_revision
    assert session.segment_id == committed_segment



def test_soft_window_commits_complete_sentence_before_hard_window(asr_module):
    session = asr_module.PreviewSession(
        window_samples=20 * asr_module.SAMPLE_RATE,
        soft_window_samples=12 * asr_module.SAMPLE_RATE,
        overlap_samples=int(1.5 * asr_module.SAMPLE_RATE),
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
    )
    first = session.prepare_inference(
        np.full(11 * asr_module.SAMPLE_RATE, 0.1, dtype=np.float32),
        now=1.0,
    )
    assert first is not None
    session.apply_result(first, _result(asr_module, "一句还没有结束的话"))

    boundary = session.prepare_inference(
        np.full(asr_module.SAMPLE_RATE, 0.1, dtype=np.float32),
        now=2.0,
    )
    assert boundary is not None
    live = session.apply_result(boundary, _result(asr_module, "一句完整的话。"))
    assert live["correction_state"] == "live"

    flush = session.prepare_inference(
        np.zeros(asr_module.SAMPLE_RATE // 2, dtype=np.float32),
        now=2.5,
    )
    assert flush is not None and flush.commit_after is True
    snapshot = session.apply_result(flush, _result(asr_module, "一句完整的话。"))

    assert snapshot["correction_state"] == "finalized"
    assert snapshot["stable_text"] == "一句完整的话。"
    assert snapshot["window_seconds"] == 0


def test_pure_silence_never_enters_inference_or_stream_state(asr_module):
    session = _session(asr_module)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)

    assert session.prepare_inference(silence, now=1.0) is None
    assert session.prepare_inference(silence, now=1.75) is None
    assert session.prepare_inference(silence, now=2.5) is None

    snapshot = session.snapshot()
    assert session.audio_window.size == 0
    assert snapshot["speech_detected"] is False
    assert snapshot["silence_skip_count"] == 3


def test_zero_threshold_still_rejects_digital_silence(asr_module):
    session = _session(asr_module)
    session.silence_rms_threshold = 0.0

    prepared = session.prepare_inference(
        np.zeros(asr_module.SAMPLE_RATE, dtype=np.float32),
        now=1.0,
    )

    assert prepared is None
    assert session.snapshot()["speech_detected"] is False


def test_short_speech_is_detected_by_frame_energy(asr_module):
    session = _session(asr_module)
    audio = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)
    audio[: int(0.08 * asr_module.SAMPLE_RATE)] = 0.02

    prepared = session.prepare_inference(audio, now=1.0)

    assert prepared is not None
    assert session.snapshot()["speech_detected"] is True


def test_startup_impulse_does_not_trigger_inference(asr_module):
    session = _session(asr_module)
    audio = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)
    audio[: int(0.06 * asr_module.SAMPLE_RATE)] = 0.1

    prepared = session.prepare_inference(audio, now=1.0)

    assert prepared is None
    assert session.audio_window.size == 0
    assert session.snapshot()["speech_detected"] is False


def test_steady_background_noise_does_not_trigger_inference(asr_module):
    session = _session(asr_module)
    noise = np.full(
        int(0.75 * asr_module.SAMPLE_RATE),
        0.01,
        dtype=np.float32,
    )

    prepared = session.prepare_inference(noise, now=1.0)

    assert prepared is None
    assert session.audio_window.size == 0
    assert session.snapshot()["speech_detected"] is False


def test_pre_speech_roll_preserves_speech_across_chunk_boundary(asr_module):
    session = _session(asr_module)
    first = np.zeros(int(0.5 * asr_module.SAMPLE_RATE), dtype=np.float32)
    first[-int(0.04 * asr_module.SAMPLE_RATE) :] = 0.02
    second = np.zeros(int(0.5 * asr_module.SAMPLE_RATE), dtype=np.float32)
    second[: int(0.08 * asr_module.SAMPLE_RATE)] = 0.02

    assert session.prepare_inference(first, now=1.0) is None
    prepared = session.prepare_inference(second, now=1.5)

    assert prepared is not None
    assert prepared.audio.size == asr_module.SAMPLE_RATE
    assert np.count_nonzero(prepared.audio) == int(0.12 * asr_module.SAMPLE_RATE)
    assert session.pre_speech_audio.size == 0
    assert session.snapshot()["speech_detected"] is True


def test_silence_after_short_speech_flushes_once(asr_module):
    session = _session(asr_module)
    speech = np.full(int(0.25 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32)
    silence = np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32)

    first = session.prepare_inference(speech, now=1.0)
    assert first is not None
    flush = session.prepare_inference(silence, now=1.75)

    assert flush is not None
    assert flush.commit_after is True
    session.apply_result(flush, _result(asr_module, "短句"))
    assert session.prepare_inference(silence, now=2.5) is None
    assert session.snapshot()["stable_text"] == "短句"


def test_transcript_overlap_is_normalized_and_non_destructive(asr_module):
    assert asr_module.merge_transcripts("这是第一句话。继续讨论", "继续讨论下一项") == "这是第一句话。继续讨论下一项"
    assert asr_module.merge_transcripts("之前说过的话", "之前说过的话之前说过的话") == "之前说过的话"
    assert asr_module.merge_transcripts("没有重叠", "完整保留") == "没有重叠完整保留"


def test_registry_admits_more_sessions_than_the_gpu_batch_size(asr_module):
    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1500,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )

    assert all(registry.create() is not None for _ in range(32))
    assert registry.count() == 32


def test_registry_retains_an_optional_operator_cap(asr_module):
    registry = asr_module.SessionRegistry(
        max_sessions=2,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1500,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )

    assert registry.create() is not None
    assert registry.create() is not None
    assert registry.create() is None


@pytest.mark.asyncio
async def test_bounded_session_admission_is_independent_from_gpu_batch(asr_module):
    class Scheduler:
        max_batch_size = 10
        stream_chunk_size_sec = 1.0

        @staticmethod
        def health():
            return {"queue_depth": 0}

    registry = asr_module.SessionRegistry(
        max_sessions=16,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        admitted = [await client.post("/api/start") for _ in range(16)]
        rejected = await client.post("/api/start")
        health = (await client.get("/health")).json()

    assert all(response.status_code == 200 for response in admitted)
    assert rejected.status_code == 429
    assert health["active_sessions"] == 16
    assert health["max_sessions"] == 16
    assert health["max_batch_size"] == 10
    assert health["admission_mode"] == "bounded"


@pytest.mark.asyncio
async def test_scheduler_batches_fifo_requests_without_blocking_loop(asr_module):
    class FakeModel:
        def __init__(self):
            self.calls = []

        def transcribe(self, *, audio):
            values = [int(item[0][0]) for item in audio]
            self.calls.append(values)
            return [
                SimpleNamespace(language="Chinese", text=str(value))
                for value in values
            ]

    model = FakeModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=8,
        batch_wait_ms=30,
    )
    try:
        results = await asyncio.gather(
            *(
                scheduler.submit(np.asarray([value], dtype=np.float32))
                for value in range(1, 9)
            )
        )
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert [result.text for result in results] == [str(value) for value in range(1, 9)]
    assert model.calls == [list(range(1, 9))]
    assert scheduler.health()["last_batch_size"] == 8


@pytest.mark.asyncio
async def test_scheduler_prioritizes_preview_over_queued_final_work(asr_module):
    class BlockingModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.batches: list[list[int]] = []

        def transcribe(self, *, audio):
            values = [int(item[0][0]) for item in audio]
            self.batches.append(values)
            if len(self.batches) == 1:
                self.started.set()
                assert self.release.wait(timeout=1)
            return [
                SimpleNamespace(language="Chinese", text=str(value))
                for value in values
            ]

    model = BlockingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=8,
        batch_wait_ms=5,
    )
    first_final = asyncio.create_task(
        scheduler.submit(np.asarray([1], dtype=np.float32), priority=1)
    )
    assert await asyncio.to_thread(model.started.wait, 1)
    queued_final = asyncio.create_task(
        scheduler.submit(np.asarray([2], dtype=np.float32), priority=1)
    )
    preview = asyncio.create_task(
        scheduler.submit(np.asarray([3], dtype=np.float32), priority=0)
    )
    await asyncio.sleep(0)
    model.release.set()
    try:
        results = await asyncio.gather(first_final, queued_final, preview)
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert [result.text for result in results] == ["1", "2", "3"]
    assert model.batches[0] == [1]
    assert model.batches[1][0] == 3


@pytest.mark.asyncio
async def test_scheduler_ages_final_work_to_prevent_preview_starvation(asr_module):
    class BlockingModel:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.batches: list[list[int]] = []

        def transcribe(self, *, audio):
            values = [int(item[0][0]) for item in audio]
            self.batches.append(values)
            if len(self.batches) == 1:
                self.started.set()
                assert self.release.wait(timeout=2)
            return [
                SimpleNamespace(language="Chinese", text=str(value))
                for value in values
            ]

    model = BlockingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=1,
        batch_wait_ms=5,
    )
    first_preview = asyncio.create_task(
        scheduler.submit(np.asarray([1], dtype=np.float32), priority=0)
    )
    assert await asyncio.to_thread(model.started.wait, 1)
    final = asyncio.create_task(
        scheduler.submit(np.asarray([2], dtype=np.float32), priority=1)
    )
    await asyncio.sleep(scheduler._LOW_PRIORITY_GRACE_SECONDS + 0.05)
    later_previews = [
        asyncio.create_task(
            scheduler.submit(np.asarray([value], dtype=np.float32), priority=0)
        )
        for value in range(3, 7)
    ]
    model.release.set()
    try:
        await asyncio.gather(first_preview, final, *later_previews)
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert model.batches[:2] == [[1], [2]]


@pytest.mark.asyncio
async def test_same_session_never_has_two_inferences_in_flight(asr_module):
    class Scheduler:
        active = 0
        max_active = 0

        @staticmethod
        def health():
            return {}

        async def submit(self, _audio, *, language=None, priority=0):
            assert language is None
            assert priority == 0
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return _result(asr_module, "实时文本")

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1500,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )
    scheduler = Scheduler()
    app = asr_module.create_app(
        registry,
        scheduler,
        max_request_audio_seconds=12,
    )
    transport = httpx.ASGITransport(app=app)
    audio = np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32).tobytes()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/api/start")).json()["session_id"]
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/chunk",
                    params={"session_id": session_id},
                    content=audio,
                    headers={"Content-Type": "application/octet-stream"},
                )
                for _ in range(3)
            )
        )

    assert all(response.status_code == 200 for response in responses)
    assert scheduler.max_active == 1


@pytest.mark.asyncio
async def test_preview_endpoint_advertises_and_accepts_lossless_pcm16(asr_module):
    calls: list[np.ndarray] = []

    class Scheduler:
        max_batch_size = 8

        @staticmethod
        def health():
            return {}

        async def submit(self, audio, *, language=None, priority=0):
            assert language == "Chinese"
            assert priority == 0
            calls.append(audio)
            return _result(asr_module, "实时文本")

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    pcm_samples = np.asarray([-32768, -1024, 0, 1024, 32767] * 2400, dtype="<i2")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/start", params={"language": "zh"})
        session_id = start.json()["session_id"]
        response = await client.post(
            "/api/chunk",
            params={"session_id": session_id},
            content=pcm_samples.tobytes(),
            headers={"Content-Type": "audio/pcm"},
        )

    assert start.status_code == 200
    assert start.json()["audio_format"] == "pcm_s16le"
    assert start.json()["language"] == "Chinese"
    assert response.status_code == 200
    assert len(calls) == 1
    np.testing.assert_array_equal(
        calls[0],
        pcm_samples.astype(np.float32) / 32768.0,
    )


@pytest.mark.asyncio
async def test_preview_endpoint_never_submits_pure_silence(asr_module):
    class Scheduler:
        max_batch_size = 8

        @staticmethod
        def health():
            return {}

        async def submit(self, *_args, **_kwargs):
            raise AssertionError("digital silence must not reach ASR inference")

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    silence = np.zeros(asr_module.SAMPLE_RATE, dtype="<i2").tobytes()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        session_id = (await client.post("/api/start")).json()["session_id"]
        partial = await client.post(
            "/api/chunk",
            params={"session_id": session_id},
            content=silence,
            headers={"Content-Type": "audio/pcm"},
        )
        final = await client.post(
            "/api/finish",
            params={"session_id": session_id},
        )

    assert partial.status_code == 200
    assert partial.json()["text"] == ""
    assert partial.json()["speech_detected"] is False
    assert final.status_code == 200
    assert final.json()["text"] == ""
    assert final.json()["speech_detected"] is False


@pytest.mark.asyncio
async def test_full_transcribe_endpoint_accepts_pcm16_and_forces_language(asr_module):
    calls: list[tuple[np.ndarray, str | None]] = []

    class Scheduler:
        max_batch_size = 8

        @staticmethod
        def health():
            return {}

        async def submit(self, audio, *, language=None, priority=0):
            assert priority == 1
            calls.append((audio, language))
            return asr_module.InferenceResult(
                language="Chinese",
                text="高质量完整文本",
                queue_ms=2.0,
                inference_ms=80.0,
            )

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    pcm = np.full(asr_module.SAMPLE_RATE, 1024, dtype="<i2").tobytes()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/transcribe",
            params={"language": "zh"},
            content=pcm,
            headers={"Content-Type": "audio/pcm"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "高质量完整文本"
    assert response.json()["audio_seconds"] == 1.0
    assert len(calls) == 1
    assert calls[0][1] == "Chinese"
    assert calls[0][0].dtype == np.float32


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (None, None),
        ("auto", None),
        ("zh-CN", "Chinese"),
        ("en-US", "English"),
        ("de-DE", "German"),
        ("de_AT", "German"),
        ("ja-JP", "Japanese"),
    ],
)
def test_qwen_language_normalization_supports_auto_and_ui_locales(
    asr_module,
    language,
    expected,
):
    assert asr_module.normalize_language(language) == expected


def test_model_warmup_executes_real_inference_before_server_readiness(asr_module):
    calls: list[list[tuple[np.ndarray, int]]] = []

    class Model:
        def transcribe(self, *, audio):
            calls.append(audio)
            return [SimpleNamespace(language="", text="")]

    elapsed_ms = asr_module.warmup_model(Model(), audio_seconds=0.5)

    assert elapsed_ms >= 0
    assert len(calls) == 1
    assert calls[0][0][0].shape == (asr_module.SAMPLE_RATE // 2,)
    assert calls[0][0][1] == asr_module.SAMPLE_RATE


class _StreamingTokenizer:
    @staticmethod
    def encode(value):
        return list(str(value))

    @staticmethod
    def decode(values):
        return "".join(values)


class _StreamingInnerModel:
    def __init__(self):
        self.batch_sizes = []
        self.audio_sizes = []

    def generate(self, inputs, *, sampling_params, use_tqdm):
        assert sampling_params == "sampling"
        assert use_tqdm is False
        self.batch_sizes.append(len(inputs))
        outputs = []
        for item in inputs:
            audio = item["multi_modal_data"]["audio"][0]
            self.audio_sizes.append(audio.size)
            value = int(round(float(audio[-1])))
            outputs.append(
                SimpleNamespace(outputs=[SimpleNamespace(text=f"stream-{value}")])
            )
        return outputs


class _StreamingModel:
    def __init__(self):
        self.model = _StreamingInnerModel()
        self.processor = SimpleNamespace(tokenizer=_StreamingTokenizer())
        self.sampling_params = "sampling"

    def init_streaming_state(
        self,
        *,
        context,
        language,
        unfixed_chunk_num,
        unfixed_token_num,
        chunk_size_sec,
    ):
        return SimpleNamespace(
            unfixed_chunk_num=unfixed_chunk_num,
            unfixed_token_num=unfixed_token_num,
            chunk_size_sec=chunk_size_sec,
            chunk_size_samples=4,
            chunk_id=0,
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            prompt_raw="prompt:",
            context=context,
            force_language=language,
            language="",
            text="",
            _raw_decoded="",
        )


def _stream_parser(raw, language):
    return language or "", raw


def test_native_streaming_state_receives_previous_text_context(asr_module):
    model = _StreamingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=1,
        batch_wait_ms=1,
    )
    try:
        state = scheduler.create_streaming_state(
            "Chinese",
            "上一段已经确认的文本",
        )
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert state.context == "上一段已经确认的文本"


@pytest.mark.asyncio
async def test_native_streaming_batches_eleven_isolated_states(asr_module):
    model = _StreamingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=11,
        batch_wait_ms=30,
        stream_chunk_size_sec=1.0,
        parse_stream_output=_stream_parser,
    )
    states = [scheduler.create_streaming_state("Chinese") for _ in range(11)]
    try:
        results = await asyncio.gather(
            *(
                scheduler.submit_stream(
                    state,
                    np.full(4, index, dtype=np.float32),
                )
                for index, state in enumerate(states, 1)
            )
        )
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert model.model.batch_sizes == [11]
    assert [result.text for result in results] == [
        f"stream-{index}" for index in range(1, 12)
    ]
    assert all(result.decoded_chunks == 1 for result in results)
    assert all(state.chunk_id == 1 for state in states)
    assert all(state.audio_accum.size == 4 for state in states)
    health = scheduler.health()
    assert health["last_stream_batch_size"] == 11
    assert health["stream_decode_batches"] == 1
    assert health["stream_decoded_chunks"] == 11


@pytest.mark.asyncio
async def test_native_streaming_buffers_partial_chunk_then_decodes(asr_module):
    model = _StreamingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=8,
        batch_wait_ms=1,
        stream_chunk_size_sec=1.0,
        parse_stream_output=_stream_parser,
    )
    state = scheduler.create_streaming_state("Chinese")
    try:
        first = await scheduler.submit_stream(
            state,
            np.full(2, 7, dtype=np.float32),
        )
        second = await scheduler.submit_stream(
            state,
            np.full(2, 7, dtype=np.float32),
        )
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert first.decoded_chunks == 0
    assert first.text == ""
    assert second.decoded_chunks == 1
    assert second.text == "stream-7"
    assert model.model.batch_sizes == [1]
    assert state.buffer.size == 0
    assert state.audio_accum.size == 4


@pytest.mark.asyncio
async def test_native_streaming_finish_flushes_short_tail(asr_module):
    model = _StreamingModel()
    scheduler = asr_module.BatchInferenceScheduler(
        model,
        max_batch_size=8,
        batch_wait_ms=1,
        stream_chunk_size_sec=1.0,
        parse_stream_output=_stream_parser,
    )
    state = scheduler.create_streaming_state("Chinese")
    try:
        first = await scheduler.submit_stream(
            state,
            np.full(4, 7, dtype=np.float32),
        )
        buffered = await scheduler.submit_stream(
            state,
            np.full(2, 9, dtype=np.float32),
        )
        finished = await scheduler.submit_stream(
            state,
            np.zeros((0,), dtype=np.float32),
            finish=True,
        )
    finally:
        scheduler.close()
        scheduler._worker.join(timeout=1)

    assert first.decoded_chunks == 1
    assert first.text == "stream-7"
    assert buffered.decoded_chunks == 0
    assert finished.decoded_chunks == 1
    assert finished.text == "stream-9"
    assert model.model.audio_sizes == [4, 6]
    assert state.buffer.size == 0
    np.testing.assert_array_equal(
        state.audio_accum,
        np.asarray([7, 7, 7, 7, 9, 9], dtype=np.float32),
    )


def test_native_streaming_segment_reset_preserves_stable_text_and_overlap(asr_module):
    states = []

    def factory(language, context):
        state = SimpleNamespace(
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            language=language or "",
            context=context,
            text="",
        )
        states.append(state)
        return state

    session = asr_module.PreviewSession(
        window_samples=8,
        overlap_samples=2,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
        requested_language="Chinese",
        streaming_state_factory=factory,
        stream_context_chars=6,
    )
    first = session.prepare_inference(np.ones(8, dtype=np.float32), now=1.0)
    assert first is not None
    session.apply_result(first, _result(asr_module, "第一段稳定文本"))
    first_state = session.streaming_state

    next_part = session.prepare_inference(np.full(4, 2, dtype=np.float32), now=2.0)

    assert next_part is not None
    assert session.stable_text == "第一段稳定文本"
    assert session.streaming_state is not first_state
    assert len(states) == 2
    assert states[0].context == ""
    assert states[1].context == "一段稳定文本"
    assert next_part.audio.size == 6
    np.testing.assert_array_equal(next_part.audio[:2], np.ones(2, dtype=np.float32))
    np.testing.assert_array_equal(next_part.audio[2:], np.full(4, 2, dtype=np.float32))
    assert session.audio_window.size == 6



def test_native_streaming_silence_seals_already_decoded_audio(asr_module):
    created_states = []

    def factory(_language, context):
        state = SimpleNamespace(
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            context=context,
        )
        created_states.append(state)
        return state

    session = asr_module.PreviewSession(
        window_samples=12 * asr_module.SAMPLE_RATE,
        overlap_samples=0,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
        streaming_state_factory=factory,
    )
    speech = session.prepare_inference(
        np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32),
        now=1.0,
    )
    assert speech is not None
    session.apply_result(speech, _result(asr_module, "已经识别的完整句子"))

    commit = session.prepare_inference(
        np.zeros(int(0.75 * asr_module.SAMPLE_RATE), dtype=np.float32),
        now=1.75,
    )
    assert commit is not None and commit.commit_after is True
    assert commit.requires_decode is False
    assert commit.finish_stream is False

    session.seal(commit)

    assert session.stable_text == "已经识别的完整句子"
    assert session.unstable_text == ""
    assert session.audio_window.size == 0
    assert len(created_states) == 2
    assert created_states[0].context == ""
    assert created_states[1].context == ""


def test_native_finish_decodes_even_one_active_unconsumed_frame(asr_module):
    def factory(_language, _context):
        return SimpleNamespace(
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
        )

    session = asr_module.PreviewSession(
        window_samples=12 * asr_module.SAMPLE_RATE,
        overlap_samples=0,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
        streaming_state_factory=factory,
    )
    speech = session.prepare_inference(
        np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32),
        now=1.0,
    )
    assert speech is not None
    session.apply_result(speech, _result(asr_module, "已经识别"))
    streaming_state = session.streaming_state
    tail = np.full(
        asr_module._SPEECH_FRAME_SAMPLES,
        0.02,
        dtype=np.float32,
    )
    streaming_state.buffer = tail.copy()

    finish = session.prepare_finish()

    assert finish.requires_decode is True
    assert finish.finish_stream is True
    assert finish.audio.size == 0
    assert session.streaming_state is streaming_state


def test_native_finish_fails_open_for_unknown_stream_buffer(asr_module):
    def factory(_language, _context):
        return SimpleNamespace(audio_accum=np.zeros((0,), dtype=np.float32))

    session = asr_module.PreviewSession(
        window_samples=12 * asr_module.SAMPLE_RATE,
        overlap_samples=0,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        created_at=0,
        last_seen=0,
        streaming_state_factory=factory,
    )
    speech = session.prepare_inference(
        np.full(int(0.75 * asr_module.SAMPLE_RATE), 0.1, dtype=np.float32),
        now=1.0,
    )
    assert speech is not None
    session.apply_result(speech, _result(asr_module, "协议不完整"))

    finish = session.prepare_finish()

    assert finish.requires_decode is True
    assert finish.finish_stream is True


@pytest.mark.asyncio
async def test_preview_endpoint_seals_finish_without_second_stream_call(asr_module):
    calls = []

    def factory(language, context):
        return SimpleNamespace(
            buffer=np.zeros((0,), dtype=np.float32),
            language=language or "",
            context=context,
        )

    class Scheduler:
        max_batch_size = 8
        stream_chunk_size_sec = 1.0

        @staticmethod
        def health():
            return {}

        async def submit_stream(self, state, audio, *, finish=False, priority=0):
            assert priority == 0
            calls.append((state, audio.copy(), finish))
            return asr_module.InferenceResult(
                language="Chinese",
                text="本地增量文本",
                queue_ms=1.0,
                inference_ms=10.0,
                decoded_chunks=1,
            )

        async def submit(self, *args, **kwargs):
            raise AssertionError("preview must not use full-window transcribe")

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=1.5,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
        streaming_state_factory=factory,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    audio = np.full(asr_module.SAMPLE_RATE // 2, 1024, dtype="<i2").tobytes()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = await client.post("/api/start", params={"language": "zh"})
        session_id = started.json()["session_id"]
        partial = await client.post(
            "/api/chunk",
            params={"session_id": session_id},
            content=audio,
            headers={"Content-Type": "audio/pcm"},
        )
        finished = await client.post(
            "/api/finish",
            params={"session_id": session_id},
        )

    assert started.json()["streaming_mode"] == "native_incremental_batch"
    assert started.json()["chunk_size_seconds"] == 1.0
    assert partial.status_code == 200
    assert partial.json()["text"] == "本地增量文本"
    assert finished.status_code == 200
    assert finished.json()["correction_state"] == "finalized"
    assert len(calls) == 1
    assert calls[0][2] is False


@pytest.mark.parametrize(
    ("partial_text", "final_text"),
    [
        ("绩效反贵", "绩效反馈"),
        ("performance feed", "performance feedback"),
    ],
)
@pytest.mark.asyncio
async def test_preview_endpoint_native_finish_preserves_cross_chunk_revision(
    asr_module,
    partial_text,
    final_text,
):
    stream_calls = []
    tail_size = asr_module._SPEECH_FRAME_SAMPLES

    def factory(language, context):
        return SimpleNamespace(
            buffer=np.zeros((0,), dtype=np.float32),
            audio_accum=np.zeros((0,), dtype=np.float32),
            language=language or "",
            context=context,
        )

    class Scheduler:
        max_batch_size = 8
        stream_chunk_size_sec = 1.0

        @staticmethod
        def health():
            return {}

        async def submit_stream(self, state, audio, *, finish=False, priority=0):
            assert priority == 0
            stream_calls.append(
                (state, audio.copy(), finish, state.buffer.copy())
            )
            if not finish:
                state.audio_accum = audio[:-tail_size].copy()
                state.buffer = audio[-tail_size:].copy()
                text = partial_text
            else:
                assert audio.size == 0
                state.audio_accum = np.concatenate(
                    (state.audio_accum, state.buffer)
                )
                state.buffer = np.zeros((0,), dtype=np.float32)
                text = final_text
            return asr_module.InferenceResult(
                language="Chinese",
                text=text,
                queue_ms=1.0,
                inference_ms=10.0,
                decoded_chunks=1,
            )

        async def submit(self, *_args, **_kwargs):
            raise AssertionError(
                "native stream finalization must not transcribe an isolated tail"
            )

    registry = asr_module.SessionRegistry(
        max_sessions=0,
        window_seconds=12,
        overlap_seconds=0,
        silence_commit_ms=750,
        force_refresh_ms=1000,
        silence_rms_threshold=0.006,
        ttl_seconds=600,
        streaming_state_factory=factory,
    )
    app = asr_module.create_app(
        registry,
        Scheduler(),
        max_request_audio_seconds=12,
    )
    pcm = np.full(asr_module.SAMPLE_RATE // 2, 1024, dtype="<i2")
    expected_audio = pcm.astype(np.float32) / 32768.0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        session_id = (await client.post(
            "/api/start",
            params={"language": "zh"},
        )).json()["session_id"]
        partial = await client.post(
            "/api/chunk",
            params={"session_id": session_id},
            content=pcm.tobytes(),
            headers={"Content-Type": "audio/pcm"},
        )
        finished = await client.post(
            "/api/finish",
            params={"session_id": session_id},
        )

    assert partial.json()["text"] == partial_text
    assert finished.status_code == 200
    assert finished.json()["correction_state"] == "finalized"
    assert finished.json()["stable_text"] == final_text
    assert len(stream_calls) == 2
    assert stream_calls[0][0] is stream_calls[1][0]
    assert stream_calls[0][2] is False
    assert stream_calls[1][1].size == 0
    assert stream_calls[1][2] is True
    np.testing.assert_array_equal(
        stream_calls[1][3],
        expected_audio[-tail_size:],
    )
    np.testing.assert_array_equal(
        stream_calls[1][0].audio_accum,
        expected_audio,
    )
