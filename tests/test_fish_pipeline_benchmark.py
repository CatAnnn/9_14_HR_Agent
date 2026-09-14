from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest
from pydantic import ValidationError

from backend.config.settings import Settings
from scripts import benchmark_fish_pipeline as benchmark
from scripts.benchmark_fish_pipeline import MeasuredFish, PcmReceiver, run_scenario


class _PcmStream(httpx.AsyncByteStream):
    def __init__(self, seed):
        self.pcm = int(seed).to_bytes(2, "little") * 800

    async def __aiter__(self):
        for _ in range(4):
            yield self.pcm
            await asyncio.sleep(0.005)


def _settings(slots=4):
    return Settings(
        tts_enabled=True, distributed_coordination_enabled=False,
        tts_provider="fish_vllm_omni", tts_http_url="http://unused.invalid/v1/audio/speech",
        tts_default_voice="employee-natural", tts_available_voices="employee-natural",
        tts_max_concurrency=slots, tts_global_max_concurrency=0,
        tts_sample_rate=8000, tts_queue_timeout_seconds=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("users", "slots", "cancel_one"),
    [(4, 4, False), (4, 4, True), (6, 4, False), (8, 4, False),
     (12, 8, False), (12, 12, False), (12, 12, True)],
)
async def test_pipeline_audit_exercises_real_services_with_mocked_model_http(users, slots, cancel_one):
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200, headers={"Content-Type": "audio/pcm"}, stream=_PcmStream(payload["seed"]),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MeasuredFish(_settings(slots), client)
        result = await run_scenario(
            service, users=users, text="请确认本轮工作的具体安排。随后讨论下一步的支持。",
            seed_base=51_000, timeout_seconds=3, probe_hold_seconds=0.02,
            feedback_interval=0.01, cancel_one=cancel_one, require_continuous=True,
        )
    assert result["passed"], result
    assert result["feedback_admission_probe_passed"]
    assert result["capacity_released"]
    assert result["peak_simultaneous_http_inference_requests"] == slots
    assert len({request["seed"] for request in requests}) == users
    assert all(sample["pcm_isolation_passed"] for sample in result["samples"])
    assert len({sample["sha256"] for sample in result["samples"]}) == users
    if cancel_one:
        assert result["cancellation_passed"]
        assert result["samples"][0]["cancelled"]
        assert all(sample["completed"] for sample in result["samples"][1:])
        assert all(item["in_progress"] for item in result["cancellation_snapshot"])
        assert all(sample["audio_bytes_after_peer_cancellation"] > 0 for sample in result["samples"][1:])
        assert result["simulated_continuous_playback_passed"] is None
    else:
        assert result["simulated_continuous_playback_passed"]
        assert result["simulated_all_users_playing_seconds"] > 0


@pytest.mark.asyncio
async def test_pipeline_audit_rejects_pcm_that_differs_from_the_assigned_user(monkeypatch):
    original = PcmReceiver.send_bytes
    async def corrupt(self, pcm):
        await original(self, bytes([pcm[0] ^ 1]) + pcm[1:])
    monkeypatch.setattr(PcmReceiver, "send_bytes", corrupt)

    async def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(
            200, headers={"Content-Type": "audio/pcm"}, stream=_PcmStream(payload["seed"]),
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MeasuredFish(_settings(), client)
        result = await run_scenario(
            service, users=4, text="测试音频隔离。", seed_base=51_000,
            timeout_seconds=3, probe_hold_seconds=0.02,
        )
    assert not result["passed"]
    assert not any(sample["pcm_isolation_passed"] for sample in result["samples"])
    assert result["capacity_released"]


@pytest.mark.asyncio
async def test_pipeline_audit_does_not_count_failed_http_as_a_successful_user():
    async def handler(request):
        payload = json.loads(request.content)
        if payload["seed"] == 51_000:
            return httpx.Response(503, json={"error": "synthetic upstream failure"})
        return httpx.Response(
            200, headers={"Content-Type": "audio/pcm"}, stream=_PcmStream(payload["seed"]),
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MeasuredFish(_settings(), client)
        result = await run_scenario(
            service, users=4, text="测试失败统计。", seed_base=51_000,
            timeout_seconds=3, probe_hold_seconds=0.02,
        )
    assert not result["passed"]
    assert result["samples"][0]["error"]
    assert not result["samples"][0]["completed"]
    assert result["capacity_released"]
    assert all(sample["completed"] for sample in result["samples"][1:])


@pytest.mark.asyncio
async def test_cancel_audit_fails_when_other_users_had_already_finished(monkeypatch):
    original = benchmark._first_audio_or_finished
    async def wait_until_peers_finish(player, worker):
        if player.index != 0:
            await worker
            return player.first_audio.is_set()
        return await original(player, worker)
    monkeypatch.setattr(benchmark, "_first_audio_or_finished", wait_until_peers_finish)

    class WaitingTarget(_PcmStream):
        async def __aiter__(self):
            yield self.pcm
            await asyncio.Event().wait()

    async def handler(request):
        seed = json.loads(request.content)["seed"]
        stream = WaitingTarget(seed) if seed == 51_000 else _PcmStream(seed)
        return httpx.Response(200, headers={"Content-Type": "audio/pcm"}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_scenario(
            MeasuredFish(_settings(), client), users=4, text="", seed_base=51_000,
            timeout_seconds=3, probe_hold_seconds=0.02, cancel_one=True,
        )
    assert result["samples"][0]["cancelled"]
    assert all(sample["completed"] for sample in result["samples"][1:])
    assert not result["passed"]
    assert not result["cancellation_passed"]
    assert not any(item["in_progress"] for item in result["cancellation_snapshot"])
    assert "not all still running" in result["cancellation_failure_reason"]
    assert result["capacity_released"]


@pytest.mark.asyncio
async def test_cancel_audit_requires_further_pcm_not_only_later_completion(monkeypatch):
    release_peers = asyncio.Event()
    original = benchmark.SpeechWebSocketHub.cancel_stream
    async def cancel_then_finish_peers(self, *args, **kwargs):
        accepted = await original(self, *args, **kwargs)
        release_peers.set()
        return accepted
    monkeypatch.setattr(benchmark.SpeechWebSocketHub, "cancel_stream", cancel_then_finish_peers)

    class FinishingStream(_PcmStream):
        def __init__(self, seed):
            super().__init__(seed)
            self.is_target = seed == 51_000

        async def __aiter__(self):
            yield self.pcm
            await (asyncio.Event() if self.is_target else release_peers).wait()

    async def handler(request):
        seed = json.loads(request.content)["seed"]
        return httpx.Response(
            200, headers={"Content-Type": "audio/pcm"}, stream=FinishingStream(seed),
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_scenario(
            MeasuredFish(_settings(), client), users=4, text="", seed_base=51_000,
            timeout_seconds=3, probe_hold_seconds=0.02, cancel_one=True,
        )
    assert all(item["in_progress"] for item in result["cancellation_snapshot"])
    assert all(sample["completed"] for sample in result["samples"][1:])
    assert all(sample["audio_bytes_after_peer_cancellation"] == 0 for sample in result["samples"][1:])
    assert not result["passed"]
    assert "receive further PCM" in result["cancellation_failure_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("minimum", "expected_pass"), [(0.1, True), (10.0, False)])
async def test_strict_playback_requires_the_minimum_longest_common_interval(minimum, expected_pass):
    async def handler(request):
        seed = json.loads(request.content)["seed"]
        return httpx.Response(200, headers={"Content-Type": "audio/pcm"}, stream=_PcmStream(seed))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_scenario(
            MeasuredFish(_settings(), client), users=4, text="", seed_base=51_000,
            timeout_seconds=3, probe_hold_seconds=0.02,
            min_common_playback_seconds=minimum, require_continuous=True,
        )
    assert all(sample["completed"] for sample in result["samples"])
    assert 0.1 < result["simulated_max_all_users_playing_seconds"] < 1
    assert result["min_common_playback_seconds"] == minimum
    assert result["passed"] is expected_pass
    assert result["simulated_continuous_playback_passed"] is expected_pass


@pytest.mark.parametrize(("users", "slots"), [(6, 4), (12, 8), (12, 12)])
def test_cli_keeps_user_count_separate_from_slots_and_writes_report(monkeypatch, tmp_path, capsys, users, slots):
    output = tmp_path / "reports" / "pipeline.json"
    report = {"passed": True, "validation_scope": "synthetic_test_only"}
    async def run(args):
        assert args.users == users
        assert args.slots == slots
        assert args.min_common_playback_seconds == 10
        return report
    monkeypatch.setattr(benchmark, "_run", run)
    monkeypatch.setattr(sys, "argv", [
        "benchmark_fish_pipeline.py", "--url", "http://unused.invalid/v1/audio/speech",
        "--concurrency", str(users), "--slots", str(slots), "--output", str(output),
        "--min-common-playback-seconds", "10",
    ])
    assert benchmark.main() == 0
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize("option", ["--concurrency", "--users", "--slots"])
@pytest.mark.parametrize("value", [0, 13])
def test_cli_rejects_capacity_outside_supported_range(monkeypatch, option, value):
    monkeypatch.setattr(sys, "argv", [
        "benchmark_fish_pipeline.py", "--url", "http://unused.invalid/v1/audio/speech",
        option, str(value),
    ])
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2


@pytest.mark.parametrize("field", [
    "tts_stage_0_max_num_seqs", "tts_stage_1_max_num_seqs", "tts_max_concurrency",
])
@pytest.mark.parametrize("value", [1, 12])
def test_settings_accept_supported_tts_capacity(field, value):
    settings = Settings(**{field: value})
    assert getattr(settings, field) == value


@pytest.mark.parametrize("field", [
    "tts_stage_0_max_num_seqs", "tts_stage_1_max_num_seqs", "tts_max_concurrency",
])
@pytest.mark.parametrize("value", [0, 13])
def test_settings_reject_tts_capacity_outside_supported_range(field, value):
    with pytest.raises(ValidationError) as failure:
        Settings(**{field: value})
    assert any(error["loc"] == (field,) for error in failure.value.errors())


def test_cli_rejects_a_negative_minimum_common_interval(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "benchmark_fish_pipeline.py", "--url", "http://unused.invalid/v1/audio/speech",
        "--min-common-playback-seconds", "-1",
    ])
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2
