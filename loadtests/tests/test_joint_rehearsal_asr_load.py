from __future__ import annotations

import json

import pytest

from loadtests.accounts import LoadTestAccount
from loadtests.joint_rehearsal_asr_load import (
    AsrRun,
    PreparedUser,
    RehearsalRun,
    SetupRun,
    _experience_metric,
    _parser,
    authorize_live_run,
    build_report,
)


def test_joint_parser_supports_only_staircase_sizes() -> None:
    parser = _parser()
    args = parser.parse_args(
        ["--base-url", "http://test", "--pcm-file", "sample.pcm"]
    )
    assert args.concurrency == 30
    assert args.model_mode == "stub"
    assert (
        parser.parse_args(
            [
                "--base-url",
                "http://test",
                "--pcm-file",
                "sample.pcm",
                "--model-mode",
                "live",
            ]
        ).model_mode
        == "live"
    )
    for concurrency in (1, 10, 30, 60):
        assert parser.parse_args(
            [
                "--base-url",
                "http://test",
                "--pcm-file",
                "sample.pcm",
                "--concurrency",
                str(concurrency),
            ]
        ).concurrency == concurrency
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--base-url",
                "http://test",
                "--pcm-file",
                "sample.pcm",
                "--concurrency",
                "61",
            ]
        )


def test_live_budget_gate_refuses_by_default_and_scales_with_concurrency() -> None:
    environment = {
        "LOADTEST_MODEL_PRICING": json.dumps(
            {
                "joint-model": {
                    "input_per_million": 10,
                    "output_per_million": 20,
                }
            }
        )
    }
    with pytest.raises(ValueError, match="refuses live requests"):
        authorize_live_run(
            concurrency=30,
            allow_live_models=False,
            maximum_cost_cny=10,
            model="joint-model",
            input_tokens_per_user=1000,
            output_tokens_per_user=100,
            env=environment,
        )
    ten = authorize_live_run(
        concurrency=10,
        allow_live_models=True,
        maximum_cost_cny=10,
        model="joint-model",
        input_tokens_per_user=1000,
        output_tokens_per_user=100,
        env=environment,
    )
    thirty = authorize_live_run(
        concurrency=30,
        allow_live_models=True,
        maximum_cost_cny=10,
        model="joint-model",
        input_tokens_per_user=1000,
        output_tokens_per_user=100,
        env=environment,
    )
    assert thirty["conservative_estimated_cost_cny"] == pytest.approx(
        ten["conservative_estimated_cost_cny"] * 3
    )
    with pytest.raises(ValueError, match="exceeds"):
        authorize_live_run(
            concurrency=30,
            allow_live_models=True,
            maximum_cost_cny=0.01,
            model="joint-model",
            input_tokens_per_user=1000,
            output_tokens_per_user=100,
            env=environment,
        )


def test_report_requires_strict_overlap_and_never_serializes_credentials() -> None:
    concurrency = 30
    secret_account = LoadTestAccount(
        1,
        "secret-user@loadtest.invalid",
        "very-secret-password",
        "Secret User",
    )
    prepared = PreparedUser(
        account=secret_account,
        session_id="raw-session-secret",
        session_ref="session-ref-1",
        cookie="secret-cookie-value",
        http_session=object(),  # type: ignore[arg-type]
    )
    setup = [
        SetupRun(
            account_index=index,
            duration_ms=100,
            session_ref=f"session-ref-{index}",
            prepared=prepared if index == 1 else None,
        )
        for index in range(1, concurrency + 1)
    ]
    asr = [
        AsrRun(
            account_index=index,
            session_ref=f"session-ref-{index}",
            started_ms=10 + index / 100,
            finished_ms=10300 + index / 100,
            capture_started_ms=100 + index / 100,
            capture_finished_ms=10100 + index / 100,
            ready_ms=90,
            first_partial_ms=400,
            capture_stopped_ms=10120,
            first_partial_wait_ms=310,
            stop_ack_wait_ms=20,
            final_wait_after_stop_ms=100,
            final_ms=10200,
            accepted=True,
            partial_count=2,
            terminal_type="final",
            terminal_code="final",
            recording_ref=f"recording-ref-{index}",
            event_types=("recording_ready", "partial", "capture_stopped", "final"),
        )
        for index in range(1, concurrency + 1)
    ]
    rehearsal = [
        RehearsalRun(
            account_index=index,
            session_ref=f"session-ref-{index}",
            started_ms=10 + index / 100,
            finished_ms=1200 + index / 100,
            connect_ms=20,
            first_event_ms=30,
            first_content_ms=500,
            completed_ms=1100,
            startup_wait_ms=30,
            analysis_model_queue_ms=40,
            employee_model_queue_ms=30,
            model_queue_wait_ms=70,
            server_first_visible_ms=490,
            server_total_ms=1090,
            response_bytes=200,
            status_code=200,
            event_names=("start", "delta", "done"),
        )
        for index in range(1, concurrency + 1)
    ]
    payload = build_report(
        run_id="joint-run",
        base_url="https://user:password@test.invalid:7443/path?key=secret",
        concurrency=concurrency,
        pcm=b"\0" * 320000,
        sample_rate=16000,
        chunk_ms=100,
        budget={"authorized": True},
        setup_results=setup,
        asr_results=asr,
        rehearsal_results=rehearsal,
        barrier_release_ms=10,
        setup_wall_ms=500,
        max_start_skew_ms=1000,
    )
    assert payload["acceptance"]["passed"] is True
    assert payload["format_version"] == 2
    assert payload["asr"]["waits"]["final_after_stop"]["p99_ms"] == 100
    assert payload["rehearsal"]["waits"]["model_queue"]["p95_ms"] == 70
    experience = payload["user_experience"]
    assert experience["passed"] is True
    assert experience["status"] == "smooth"
    assert experience["successful_joint_users"] == concurrency
    assert experience["scope"]["speech_to_reply_chain"] is False
    joint_first_feedback = experience["metrics"][
        "joint_parallel_first_feedback_wait"
    ]
    assert 500 < joint_first_feedback["p95_ms"] < 501
    assert joint_first_feedback["count"] == concurrency
    assert payload["overlap"]["users_with_capture_rehearsal_overlap"] == 30
    assert payload["overlap"]["all_captures_and_rehearsals_overlap_ms"] > 0
    rendered = json.dumps(payload)
    for secret in (
        "secret-user@loadtest.invalid",
        "very-secret-password",
        "raw-session-secret",
        "secret-cookie-value",
        "user:password",
        "key=secret",
    ):
        assert secret not in rendered

    rehearsal[0].finished_ms = 50
    failed = build_report(
        run_id="joint-run",
        base_url="https://test.invalid",
        concurrency=concurrency,
        pcm=b"\0" * 320000,
        sample_rate=16000,
        chunk_ms=100,
        budget={"authorized": True},
        setup_results=setup,
        asr_results=asr,
        rehearsal_results=rehearsal,
        barrier_release_ms=10,
        setup_wall_ms=500,
        max_start_skew_ms=1000,
    )
    assert failed["acceptance"]["passed"] is False
    assert any(
        "no ASR-capture/rehearsal overlap" in failure
        for failure in failed["acceptance"]["failures"]
    )


def test_experience_gate_rejects_extreme_tail_and_missing_samples() -> None:
    tail = _experience_metric(
        [100.0] * 59 + [20001.0],
        expected_count=60,
        smooth_threshold_ms=3000,
        acceptable_threshold_ms=10000,
    )
    assert tail["p95_ms"] == 100
    assert tail["p99_ms"] == 20001
    assert tail["grade"] == "poor"
    assert tail["reason"] == "tail_exceeded"

    incomplete = _experience_metric(
        [100.0] * 59,
        expected_count=60,
        smooth_threshold_ms=3000,
        acceptable_threshold_ms=10000,
    )
    assert incomplete["grade"] == "poor"
    assert incomplete["reason"] == "incomplete_samples"
