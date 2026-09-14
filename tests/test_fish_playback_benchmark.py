from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import itertools
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from scripts import benchmark_fish_tts as benchmark


def _sample(packets, *, index=0, started_at=0.0):
    byte_count = sum(size for _arrival, size in packets)
    completed_at = packets[-1][0] + 0.1 if packets else started_at + 0.1
    audio_seconds = byte_count / 20.0
    return benchmark.Sample(
        request_index=index,
        seed=41_000 + index,
        status=200,
        content_type="audio/pcm",
        first_audio_seconds=packets[0][0] - started_at if packets else None,
        completion_seconds=completed_at - started_at,
        audio_bytes=byte_count,
        audio_seconds=audio_seconds,
        realtime_factor=(completed_at - started_at) / audio_seconds if audio_seconds else None,
        sha256="simulated-pcm",
        error=None,
        started_at=started_at,
        completed_at=completed_at,
        playback=benchmark._simulate_playback(packets, sample_rate=10),
    )


def _four_continuous_samples():
    return [_sample([(index * 0.2, 100)], index=index) for index in range(4)]


def test_playback_simulation_joins_audio_arriving_before_the_buffer_runs_out():
    playback = benchmark._simulate_playback(
        [(0.0, 20), (0.5, 20), (1.0, 20)], sample_rate=10
    )
    assert playback.packet_count == 3
    assert len(playback.intervals) == 1
    assert playback.intervals[0] == pytest.approx((1.4, 4.4))
    assert playback.gap_count == 0
    assert playback.total_gap_seconds == 0
    assert playback.max_gap_seconds == 0


@pytest.mark.parametrize(("next_arrival", "expected_gap"), [(2.4, 1.4), (3.0, 2.0)])
def test_underrun_includes_the_frontend_rebuffer_delay(next_arrival, expected_gap):
    playback = benchmark._simulate_playback(
        [(0.0, 20), (next_arrival, 20)], sample_rate=10
    )
    assert playback.intervals[0] == pytest.approx((1.4, 2.4))
    assert playback.intervals[1] == pytest.approx((next_arrival + 1.4, next_arrival + 2.4))
    assert playback.gap_count == 1
    assert playback.total_gap_seconds == pytest.approx(expected_gap)
    assert playback.max_gap_seconds == pytest.approx(expected_gap)


def test_pcm_sample_split_between_http_reads_is_not_lost():
    playback = benchmark._simulate_playback(
        [(0.0, 1), (0.1, 1), (0.2, 2)], sample_rate=10
    )
    assert playback.packet_count == 3
    assert len(playback.intervals) == 1
    assert playback.intervals[0] == pytest.approx((1.5, 1.7))
    assert playback.gap_count == 0


@pytest.mark.parametrize("packets", [[], [(0.0, 0)], [(0.0, 1)]])
def test_empty_or_incomplete_pcm_samples_have_no_playback_interval(packets):
    playback = benchmark._simulate_playback(packets, sample_rate=10)
    assert playback.intervals == ()
    assert playback.started_at is None
    assert playback.ended_at is None
    assert not benchmark._playback_passed(_sample(packets), 0.1)


@pytest.mark.parametrize(
    "packets",
    [
        [(1.0, 2), (0.0, 2)],
        [(float("nan"), 2)],
        [(float("inf"), 2)],
        [(0.0, -1)],
        [(0.0, 1.5)],
    ],
)
def test_invalid_packet_observations_are_rejected(packets):
    with pytest.raises(ValueError):
        benchmark._simulate_playback(packets, sample_rate=10)


def test_four_streams_must_share_actual_playback_time():
    samples = _four_continuous_samples()
    summary = benchmark._summarize(samples)
    assert summary["requests"] == summary["successful"] == 4
    assert summary["playback_continuity_passed"] == 4
    assert summary["continuous_playback_passed"] is True
    assert summary["simulated_all_streams_playing_seconds"] == pytest.approx(4.4)
    assert summary["simulated_max_all_streams_playing_seconds"] == pytest.approx(4.4)
    assert summary["nominal_playback_overlap_seconds"] == pytest.approx(4.4)


def test_four_stream_overlap_excludes_gaps_inside_a_nominal_playback_window():
    samples = _four_continuous_samples()
    samples[0] = _sample([(0.0, 20), (4.0, 20)])
    common = benchmark._common_playback_intervals(samples)
    assert len(common) == 2
    assert common[0] == pytest.approx((2.0, 2.4))
    assert common[1] == pytest.approx((5.4, 6.4))
    summary = benchmark._summarize(samples)
    assert summary["nominal_playback_overlap_seconds"] == pytest.approx(4.4)
    assert summary["simulated_all_streams_playing_seconds"] == pytest.approx(1.4)
    assert summary["simulated_max_all_streams_playing_seconds"] == pytest.approx(1.0)
    assert summary["playback_continuity_passed"] == 3
    assert summary["continuous_playback_passed"] is False


def test_individually_continuous_but_nonoverlapping_streams_do_not_pass():
    samples = [_sample([(index * 10.0, 20)], index=index) for index in range(4)]
    summary = benchmark._summarize(samples)
    assert summary["playback_continuity_passed"] == 4
    assert summary["simulated_all_streams_playing_seconds"] == 0
    assert summary["continuous_playback_passed"] is False


def test_brief_common_playback_does_not_pass_a_sustained_threshold():
    samples = _four_continuous_samples()
    summary = benchmark._summarize(samples, min_common_playback_seconds=10.0)
    assert summary["playback_continuity_passed"] == 4
    assert summary["simulated_max_all_streams_playing_seconds"] == pytest.approx(4.4)
    assert summary["continuous_playback_passed"] is False
    assert benchmark._summarize(samples, min_common_playback_seconds=4.0)[
        "continuous_playback_passed"
    ] is True


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_cli_rejects_invalid_common_playback_threshold(monkeypatch, value):
    monkeypatch.setattr(
        sys, "argv", ["benchmark_fish_tts.py", "--min-common-playback-seconds", value]
    )
    monkeypatch.setattr(benchmark, "_run_request", lambda **_kwargs: pytest.fail("request started"))
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 503},
        {"error": "upstream connection closed"},
        {"audio_bytes": 0},
        {"audio_bytes": 3},
        {"content_type": "application/json"},
    ],
)
def test_one_failed_or_invalid_response_prevents_four_stream_acceptance(failure):
    samples = _four_continuous_samples()
    samples[-1] = replace(samples[-1], **failure)
    summary = benchmark._summarize(samples)
    assert summary["successful"] == 3
    assert summary["failed"] == 1
    assert summary["playback_continuity_passed"] == 3
    assert summary["continuous_playback_passed"] is False
    assert summary["simulated_all_streams_playing_seconds"] == 0


def test_missing_playback_evidence_does_not_pass_even_if_http_succeeded():
    samples = _four_continuous_samples()
    samples[-1] = replace(samples[-1], playback=None)
    summary = benchmark._summarize(samples)
    assert summary["successful"] == 4
    assert summary["playback_continuity_passed"] == 3
    assert summary["continuous_playback_passed"] is False


def test_empty_summary_has_no_success_claim_or_division_by_zero():
    summary = benchmark._summarize([])
    assert summary["requests"] == summary["successful"] == summary["failed"] == 0
    assert summary["wall_seconds"] == 0
    assert summary["aggregate_audio_per_wall_second"] is None
    assert summary["first_audio_p95_seconds"] is None
    assert summary["response_headers_p50_seconds"] is None
    assert summary["response_headers_p95_seconds"] is None
    assert summary["headers_to_first_audio_p50_seconds"] is None
    assert summary["headers_to_first_audio_p95_seconds"] is None
    assert summary["continuous_playback_passed"] is False
    assert summary["simulated_all_streams_playing_seconds"] == 0


def test_public_playback_timing_is_relative_to_that_request():
    sample = _sample([(101.0, 20)], started_at=100.0)
    public = sample.public()
    assert public["simulated_playback_start_seconds"] == pytest.approx(2.4)
    assert public["simulated_playback_end_seconds"] == pytest.approx(3.4)
    assert public["playback_continuity_passed"] is True


def test_legacy_sample_without_header_timing_keeps_unknown_stages_unknown():
    sample = _sample([(101.0, 20)], started_at=100.0)
    assert sample.response_headers_seconds is None
    assert sample.headers_to_first_audio_seconds is None
    public = sample.public()
    assert public["first_audio_seconds"] == 1.0
    assert public["response_headers_seconds"] is None
    assert public["headers_to_first_audio_seconds"] is None
    summary = benchmark._summarize([sample])
    assert summary["successful"] == 1
    assert summary["first_audio_p95_seconds"] == 1.0
    assert summary["response_headers_p95_seconds"] is None
    assert summary["headers_to_first_audio_p95_seconds"] is None


@pytest.mark.parametrize(("headers", "audio", "expected"), [
    (0.25, 0.8, 0.55), (0.0, 0.8, 0.8), (0.0, 0.0, 0.0),
    (None, 0.8, None), (0.25, None, None), (None, None, None),
])
def test_header_to_audio_interval_requires_both_times_and_accepts_zero(headers, audio, expected):
    sample = replace(_sample([(1.0, 20)]), response_headers_seconds=headers, first_audio_seconds=audio)
    actual = sample.public()["headers_to_first_audio_seconds"]
    assert sample.public()["response_headers_seconds"] == headers
    if expected is None:
        assert actual is None
    else:
        assert actual == pytest.approx(expected)


def test_stage_percentiles_use_per_sample_differences_not_percentile_subtraction():
    samples = [replace(_sample([(1.0, 20)]), response_headers_seconds=headers,
                       first_audio_seconds=audio)
               for headers, audio in [(8.0, 9.0), (1.0, 8.0), (4.0, 5.0)]]
    summary = benchmark._summarize(samples)
    assert summary["response_headers_p50_seconds"] == 4.0
    assert summary["response_headers_p95_seconds"] == 8.0
    assert summary["headers_to_first_audio_p50_seconds"] == 1.0
    assert summary["headers_to_first_audio_p95_seconds"] == 7.0
    assert summary["headers_to_first_audio_p50_seconds"] != (
        summary["first_audio_p50_seconds"] - summary["response_headers_p50_seconds"])
    assert summary["headers_to_first_audio_p95_seconds"] != (
        summary["first_audio_p95_seconds"] - summary["response_headers_p95_seconds"])


def test_stage_summary_uses_only_successful_requests_with_known_corresponding_times():
    sample = _sample([(1.0, 20)])
    known = replace(sample, response_headers_seconds=0.2, first_audio_seconds=0.8)
    header_only = replace(sample, response_headers_seconds=0.4, first_audio_seconds=None)
    failed = replace(sample, response_headers_seconds=100.0, first_audio_seconds=200.0,
                     error="connection closed after partial PCM")
    empty = replace(failed, error=None, audio_bytes=0)
    http_error = replace(failed, error=None, status=503)
    summary = benchmark._summarize([known, header_only, sample, failed, empty, http_error])
    assert summary["successful"] == 3
    assert summary["failed"] == 3
    assert summary["response_headers_p50_seconds"] == 0.2
    assert summary["response_headers_p95_seconds"] == 0.4
    assert summary["headers_to_first_audio_p50_seconds"] == pytest.approx(0.6)
    assert summary["headers_to_first_audio_p95_seconds"] == pytest.approx(0.6)


def _timed_mock_request(monkeypatch, *, times, reads=(), open_error=None, status=200):
    class Response:
        headers = {"Content-Type": "audio/pcm"}

        def __enter__(self):
            self.reads = iter(reads)
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            value = next(self.reads, b"")
            if isinstance(value, Exception):
                raise value
            return value

        read1 = read

    Response.status = status

    def open_response(*_args, **_kwargs):
        if open_error is not None:
            raise open_error
        return Response()

    clock = iter(times)
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    monkeypatch.setattr(benchmark.urllib.request, "build_opener",
                        lambda *_args: SimpleNamespace(open=open_response))
    sample = benchmark._run_request(
        request_index=0, barrier=threading.Barrier(1), url="http://unused.invalid/tts",
        model="test", voice="test", text="测试", seed=1, timeout_seconds=1,
        sample_rate=10, max_new_tokens=10, stagger_ms=0)
    assert list(clock) == [], "Expected exactly one timestamp for headers, not for each PCM chunk"
    return sample


def test_mock_clock_splits_request_headers_and_first_pcm_exactly(monkeypatch):
    sample = _timed_mock_request(monkeypatch, times=[100.0, 100.5, 101.25, 101.5, 101.75],
                                 reads=[b"\x00\x00", b"\x01\x00"])
    assert sample.error is None
    assert sample.response_headers_seconds == 0.5
    assert sample.first_audio_seconds == 1.25
    assert sample.headers_to_first_audio_seconds == 0.75
    assert sample.completion_seconds == 1.75
    assert sample.audio_bytes == 4


def test_network_failure_before_headers_does_not_invent_response_time(monkeypatch):
    sample = _timed_mock_request(monkeypatch, times=[100.0, 103.0],
                                 open_error=benchmark.urllib.error.URLError("offline"))
    assert "offline" in sample.error
    assert sample.response_headers_seconds is None
    assert sample.first_audio_seconds is None
    assert sample.headers_to_first_audio_seconds is None


def test_http_error_records_headers_before_reading_the_error_body(monkeypatch):
    error = benchmark.urllib.error.HTTPError(
        "http://unused.invalid/tts", 503, "busy", {"Content-Type": "text/plain"}, io.BytesIO(b"retry later"))
    sample = _timed_mock_request(monkeypatch, times=[100.0, 101.0, 103.0], open_error=error)
    assert sample.status == 503
    assert sample.error == "HTTP 503: retry later"
    assert sample.response_headers_seconds == 1.0
    assert sample.headers_to_first_audio_seconds is None
    assert benchmark._summarize([sample])["response_headers_p95_seconds"] is None


def test_http_error_body_network_failure_preserves_status_and_header_observation(monkeypatch):
    error = benchmark.urllib.error.HTTPError(
        "http://unused.invalid/tts", 503, "busy", {}, io.BytesIO())

    def broken_read(_size):
        raise TimeoutError("error body timed out")

    error.read = broken_read
    sample = _timed_mock_request(monkeypatch, times=[100.0, 101.0, 103.0], open_error=error)
    assert sample.status == 503
    assert sample.response_headers_seconds == 1.0
    assert sample.first_audio_seconds is None
    assert sample.headers_to_first_audio_seconds is None
    assert "error body unavailable (TimeoutError: error body timed out)" in sample.error


@pytest.mark.parametrize("failure", ["empty", "stream_error", "http_status"])
def test_failure_after_headers_preserves_observation_but_excludes_success_metrics(monkeypatch, failure):
    sample = _timed_mock_request(
        monkeypatch, times=[100.0, 100.5, 103.0],
        reads=[TimeoutError("read timed out")] if failure == "stream_error" else [],
        status=503 if failure == "http_status" else 200)
    assert sample.error is not None
    assert sample.response_headers_seconds == 0.5
    assert sample.first_audio_seconds is None
    assert sample.headers_to_first_audio_seconds is None
    summary = benchmark._summarize([sample])
    assert summary["successful"] == 0
    assert summary["response_headers_p95_seconds"] is None
    assert summary["headers_to_first_audio_p95_seconds"] is None


def test_stream_failure_after_first_pcm_keeps_timings_without_counting_as_success(monkeypatch):
    sample = _timed_mock_request(monkeypatch, times=[100.0, 100.5, 101.25, 103.0],
                                 reads=[b"\x00\x00", TimeoutError("read timed out")])
    assert sample.response_headers_seconds == 0.5
    assert sample.first_audio_seconds == 1.25
    assert sample.headers_to_first_audio_seconds == 0.75
    assert "timed out" in sample.error
    assert benchmark._summarize([sample])["headers_to_first_audio_p95_seconds"] is None


@pytest.mark.parametrize(
    ("chunks", "content_type", "expected_error"),
    [
        ([], "audio/pcm", "no PCM audio"),
        ([b"\x00\x00\x00"], "audio/pcm", "odd byte length"),
        ([b"{}"], "application/json", "expected PCM content type"),
        ([b"\x00", b"\x00\x01", b"\x00"], "audio/pcm", None),
    ],
)
def test_request_validation_uses_mocked_http_reads_only(
    monkeypatch, chunks, content_type, expected_error
):
    class Response:
        status = 200
        headers = {"Content-Type": content_type}

        def __enter__(self):
            self.chunks = iter(chunks)
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return next(self.chunks, b"")

        read1 = read

    clock = itertools.count(100.0)
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    monkeypatch.setattr(
        benchmark.urllib.request,
        "build_opener",
        lambda *_args: SimpleNamespace(open=lambda *_args, **_kwargs: Response()),
    )
    sample = benchmark._run_request(
        request_index=0,
        barrier=threading.Barrier(1),
        url="http://unused.invalid/v1/audio/speech",
        model="test-model",
        voice="test-voice",
        text="测试。",
        seed=1,
        timeout_seconds=1,
        sample_rate=10,
        max_new_tokens=10,
        stagger_ms=0,
    )
    if expected_error is None:
        assert sample.error is None
        assert sample.audio_bytes == 4
        assert sample.audio_seconds == pytest.approx(0.2)
        assert benchmark._sample_succeeded(sample)
    else:
        assert expected_error in sample.error
        assert not benchmark._sample_succeeded(sample)


@pytest.mark.parametrize(
    ("mode", "strict", "expected_exit"),
    [("continuous", True, 0), ("gap", True, 1), ("gap", False, 0), ("failure", False, 1)],
)
def test_cli_strict_acceptance_and_reported_scope(monkeypatch, capsys, mode, strict, expected_exit):
    samples = _four_continuous_samples()
    if mode == "gap":
        samples[0] = _sample([(0.0, 20), (4.0, 20)])
    elif mode == "failure":
        samples[0] = replace(samples[0], error="simulated HTTP failure")
    monkeypatch.setattr(
        benchmark, "_run_request", lambda **kwargs: samples[kwargs["request_index"]]
    )
    arguments = ["benchmark_fish_tts.py", "--concurrency", "4"]
    if strict:
        arguments.append("--require-continuous-playback")
    monkeypatch.setattr(sys, "argv", arguments)

    assert benchmark.main() == expected_exit
    report = json.loads(capsys.readouterr().out)
    assert report["validation_scope"] == "direct_model_http_no_backend_admission"
    assert "excludes backend admission" in report["playback_simulation_notes"]
    assert "browser scheduling costs" in report["playback_simulation_notes"]
    timing_notes = report["timing_measurement_notes"]
    assert "request preparation" in timing_notes["response_headers_seconds"]
    assert "network" in timing_notes["response_headers_seconds"]
    assert "queueing" in timing_notes["headers_to_first_audio_seconds"]
    assert "audio encoding" in timing_notes["headers_to_first_audio_seconds"]
    assert "not isolate precise internal service stages" in timing_notes["limitations"]
    assert "not differences" in timing_notes["percentiles"]
    assert report["configuration"]["concurrency"] == 4
    assert len(report["configuration"]["text_sha256"]) == 64
    assert report["configuration"]["text_char_count"] > 0
    assert report["rounds"][0]["summary"]["continuous_playback_passed"] is (mode == "continuous")


@pytest.mark.parametrize(
    "argument",
    [
        "--concurrency=0",
        "--sample-rate=0",
        "--timeout-seconds=0",
        "--max-new-tokens=0",
        "--playback-buffer-seconds=nan",
        "--max-playback-gap-seconds=-1",
    ],
)
def test_invalid_cli_configuration_fails_before_starting_any_request(monkeypatch, argument):
    monkeypatch.setattr(sys, "argv", ["benchmark_fish_tts.py", argument])
    monkeypatch.setattr(benchmark, "_run_request", lambda **_kwargs: pytest.fail("request started"))
    with pytest.raises(SystemExit) as failure:
        benchmark.main()
    assert failure.value.code == 2


def test_cli_text_file_and_output_keep_stdout_and_support_multiple_rounds(
    monkeypatch, capsys, tmp_path
):
    text_file = tmp_path / "职场 反馈.txt"
    workplace_text = "我们先核对已完成的工作。\n再一起明确下一阶段需要的支持。"
    text_file.write_text(f"  {workplace_text}\n", encoding="utf-8")
    output_file = tmp_path / "reports" / "four-users.json"
    samples = _four_continuous_samples()
    requested_texts = []

    def fake_request(**kwargs):
        requested_texts.append(kwargs["text"])
        return samples[kwargs["request_index"]]

    monkeypatch.setattr(benchmark, "_run_request", fake_request)
    monkeypatch.setattr(sys, "argv", [
        "benchmark_fish_tts.py",
        "--text-file", str(text_file),
        "--output", str(output_file),
        "--rounds", "2",
        "--require-continuous-playback",
    ])

    assert benchmark.main() == 0

    stdout = capsys.readouterr().out
    assert output_file.read_text(encoding="utf-8") == stdout
    report = json.loads(stdout)
    assert report["configuration"]["text_file"] == str(text_file)
    assert report["configuration"]["text_sha256"] == hashlib.sha256(
        workplace_text.encode("utf-8")
    ).hexdigest()
    assert report["configuration"]["text_char_count"] == len(workplace_text)
    assert report["configuration"]["concurrency"] == 4
    assert len(report["rounds"]) == 2
    assert requested_texts == [workplace_text] * 8
    assert all(item["summary"]["continuous_playback_passed"] for item in report["rounds"])


@pytest.mark.parametrize("mode", ["missing", "empty", "invalid_utf8", "conflicting_text"])
def test_cli_rejects_invalid_text_file_before_starting_requests(monkeypatch, tmp_path, mode):
    text_file = tmp_path / "input.txt"
    if mode == "empty":
        text_file.write_text(" \n\t", encoding="utf-8")
    elif mode == "invalid_utf8":
        text_file.write_bytes(b"\xff\xfe")
    elif mode == "conflicting_text":
        text_file.write_text("文件文本", encoding="utf-8")
    arguments = ["benchmark_fish_tts.py", "--text-file", str(text_file)]
    if mode == "conflicting_text":
        arguments.extend(["--text", "另一段文本"])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(benchmark, "_run_request", lambda **_kwargs: pytest.fail("request started"))

    with pytest.raises(SystemExit) as failure:
        benchmark.main()

    assert failure.value.code == 2


def test_cli_output_failure_preserves_report_on_stdout(monkeypatch, capsys, tmp_path):
    samples = _four_continuous_samples()
    monkeypatch.setattr(
        benchmark, "_run_request", lambda **kwargs: samples[kwargs["request_index"]]
    )
    monkeypatch.setattr(
        sys, "argv", ["benchmark_fish_tts.py", "--output", str(tmp_path)]
    )

    with pytest.raises(SystemExit) as failure:
        benchmark.main()

    captured = capsys.readouterr()
    assert failure.value.code == 2
    assert "cannot write output report" in captured.err
    assert json.loads(captured.out)["rounds"][0]["summary"]["successful"] == 4
