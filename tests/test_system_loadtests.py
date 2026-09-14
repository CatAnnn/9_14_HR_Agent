from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from loadtests.acceptance import evaluate_system_metrics
from loadtests.accounts import generate_accounts, load_accounts, write_accounts
from loadtests.aggregate_results import aggregate
from loadtests.aiperf_matrix import (
    MATRICES,
    build_aiperf_command,
    redact_url,
    run_matrix,
)
from loadtests.asr_load import AsrSessionResult, evaluate_asr_results
from loadtests.bosch_adapter import _configured_auth_headers, _unwrap
from loadtests.cost import estimate_cost_cny, reserve_cost
from loadtests.model_stub.app import _response_text
from loadtests.reporting import evaluate_locust_summary
from loadtests.shape_config import build_shape, required_workers
from loadtests.sse import SSEProtocolError, observe_sse_chunks


def test_account_pool_has_480_unique_isolated_accounts(tmp_path: Path) -> None:
    accounts = generate_accounts(password="Loadtest-Account-Password")
    assert len(accounts) == 480
    assert len({account.email for account in accounts}) == 480
    path = tmp_path / "accounts.jsonl"
    write_accounts(path, accounts)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_accounts(path) == accounts


def test_400_user_shapes_and_worker_ceiling() -> None:
    staircase = build_shape("staircase", {})
    assert [stage.users for stage in staircase] == [1, 10, 30, 60, 100, 200, 300, 400]
    assert staircase[-1].duration_seconds == 1800
    spike = build_shape("spike", {})
    assert [stage.users for stage in spike] == [50, 400]
    assert spike[-1].duration_seconds == 900
    assert build_shape("soak", {})[0].duration_seconds == 3600
    assert required_workers(400) == 8


def test_sse_parser_accepts_fragmented_stream_and_rejects_wrong_order() -> None:
    observation = observe_sse_chunks(
        [
            b"event: sta",
            b"rt\ndata: {}\n\nevent: delta\ndata: {\"text\":\"ok\"}\n\n",
            b"event: done\ndata: {\"complete\":true}\n\n",
        ],
        flow="guidance",
    )
    assert observation.event_names == ["start", "delta", "done"]
    with pytest.raises(SSEProtocolError, match="must begin with start"):
        observe_sse_chunks(
            [b"event: delta\ndata: {}\n\nevent: done\ndata: {\"complete\":true}\n\n"],
            flow="guidance",
        )


def test_sse_parser_accepts_utf8_and_crlf_split_across_chunks() -> None:
    stream = (
        "event: start\r\ndata: {}\r\n\r\n"
        "event: delta\r\ndata: {\"text\":\"谈前指导\"}\r\n\r\n"
        "event: done\r\ndata: {\"complete\":true}\r\n\r\n"
    ).encode("utf-8")
    observation = observe_sse_chunks(
        [stream[index : index + 1] for index in range(len(stream))],
        flow="guidance",
    )
    assert observation.event_names == ["start", "delta", "done"]


def test_coach_sse_requires_four_completed_dimensions() -> None:
    blocks = [b"event: start\ndata: {}\n\n"]
    blocks.extend(
        f'event: task_done\ndata: {{"task_id":"task-{index}"}}\n\n'.encode()
        for index in range(4)
    )
    blocks.append(b"event: done\ndata: {\"complete\":true}\n\n")
    assert len(observe_sse_chunks(blocks, flow="coach").coach_tasks) == 4


class _CostRedis:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def eval(self, _script: str, _numkeys: int, *_values: object):
        return [1 if self.allowed else 0, "12.5"]


def test_live_cost_estimate_and_atomic_ceiling() -> None:
    assert estimate_cost_cny(
        model="glm-5.2",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == 36.0
    assert reserve_cost(
        _CostRedis(True),
        key="cost",
        estimated_cny=1,
        maximum_cny=10,
    ) == 12.5
    with pytest.raises(RuntimeError, match="cost ceiling"):
        reserve_cost(
            _CostRedis(False),
            key="cost",
            estimated_cny=1,
            maximum_cny=10,
        )


def test_aiperf_uses_declared_matrices_and_redacts_url(tmp_path: Path) -> None:
    assert MATRICES["llm"][-1] == 400
    assert MATRICES["embedding"][-1] == 400
    assert MATRICES["reranker"][-1] == 196
    command = build_aiperf_command(
        target="llm",
        base_url="https://models.test",
        model="glm-5.2",
        concurrency=400,
        artifact_dir=tmp_path / "artifact",
        run_id="run-1",
    )
    assert command[0:2] == ["aiperf", "profile"]
    assert command[command.index("--concurrency") + 1] == "400"
    assert redact_url("https://secret:token@models.test/v1?key=x") == "https://models.test/v1"
    assert run_matrix(
        target="embedding",
        base_url="http://stub:8099",
        model="loadtest-embedding",
        artifact_root=tmp_path,
        mode="stub",
        allow_live_models=False,
        max_cost_cny=0,
        levels=[1, 400],
        dry_run=True,
    ) == 0


def test_aiperf_live_non_llm_cost_is_explicit(tmp_path: Path) -> None:
    kwargs = {
        "target": "embedding",
        "base_url": "https://models.test",
        "model": "embedding-model",
        "artifact_root": tmp_path,
        "mode": "live",
        "allow_live_models": True,
        "max_cost_cny": 1.0,
        "levels": [1],
        "dry_run": True,
    }
    with pytest.raises(RuntimeError, match="estimated-cost-cny-per-request"):
        run_matrix(**kwargs)
    assert run_matrix(**kwargs, estimated_cost_cny_per_request=0.001) == 0
    manifest = json.loads(
        next(tmp_path.glob("aiperf-*/embedding/run-manifest.json")).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["reserved_cost_cny"] == 0.05


def _accepted_asr(index: int, *, partial: bool) -> AsrSessionResult:
    events = ["recording_ready"]
    if partial:
        events.append("partial")
    events.extend(["capture_stopped", "final"])
    return AsrSessionResult(
        account_index=index,
        session_id=f"session-{index}",
        recording_id=f"recording-{index}",
        accepted=True,
        preview_available=partial,
        partial_count=1 if partial else 0,
        terminal_type="final",
        terminal_code="final",
        event_types=events,
    )


def test_asr_preview_capacity_is_bounded_but_keeps_capture_available() -> None:
    assert evaluate_asr_results(
        [_accepted_asr(index, partial=True) for index in range(8)],
        concurrency=8,
        expect_preview=True,
    ) == []
    queued = [_accepted_asr(index, partial=index < 16) for index in range(40)]
    assert evaluate_asr_results(
        queued,
        concurrency=40,
        expect_preview=False,
    ) == []

    rejected = list(queued)
    rejected[-1] = AsrSessionResult(
        account_index=39,
        terminal_type="error",
        terminal_code="capture_capacity_exceeded",
        event_types=["error"],
    )
    failures = evaluate_asr_results(
        rejected,
        concurrency=40,
        expect_preview=False,
    )
    assert any("rejected instead of queued" in failure for failure in failures)


def test_local_qwen_asr_is_part_of_isolated_loadtest_stack() -> None:
    compose = yaml.safe_load(
        Path("loadtests/compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    primary = services["qwen3_asr"]
    secondary = services["qwen3_asr_secondary"]
    assert primary["profiles"] == ["asr-local"]
    assert secondary["profiles"] == ["asr-local"]
    assert primary["image"] == secondary["image"]
    assert "qwen3-asr" in primary["image"]
    assert "@sha256:" in primary["image"]
    assert primary["gpus"][0]["device_ids"] == ["0"]
    assert secondary["gpus"][0]["device_ids"] == ["1"]
    for service in (primary, secondary):
        command = service["command"]
        assert command[command.index("--max-num-seqs") + 1] == "11"
        assert command[command.index("--max-sessions") + 1] == (
            "${LOADTEST_ASR_MAX_SESSIONS:-16}"
        )
        assert service["environment"]["HF_HUB_OFFLINE"] == (
            "${LOADTEST_HF_HUB_OFFLINE:-1}"
        )

    backend_environment = compose["x-backend-environment"]
    assert backend_environment["ASR_PROVIDER_MODE"] == "local"
    assert backend_environment["ASR_LOCAL_STREAMING_URL"] == "http://qwen3_asr:7118"
    assert backend_environment["ASR_LOCAL_STREAMING_URLS"] == (
        "http://qwen3_asr:7118,http://qwen3_asr_secondary:7118"
    )
    assert backend_environment["ASR_LOCAL_PREVIEW_MAX_CONCURRENCY"] == "11"
    assert backend_environment["ASR_LOCAL_ADMISSION_TIMEOUT_SECONDS"] == "2"
    assert backend_environment["ASR_LOCAL_ROUTING_PROBE_TIMEOUT_SECONDS"] == "0.25"
    assert backend_environment["KB_SYNC_ON_STARTUP"] == "false"
    assert backend_environment["ASR_CAPTURE_MAX_CONNECTIONS"] == "0"
    assert backend_environment["ASR_REMOTE_FALLBACK_ON_STREAM_FAILURE"] == "false"
    assert backend_environment["AUTH_MAX_ACTIVE_SESSIONS"] == "0"
    assert backend_environment["REDIS_MAX_CONNECTIONS"] == "0"
    assert backend_environment["RAG_DB_SEARCH_MAX_CONCURRENCY"] == "0"
    assert backend_environment["RAG_GLOBAL_DB_SEARCH_MAX_CONCURRENCY"] == "0"
    assert backend_environment["RAG_RERANK_MAX_CONCURRENCY"] == "4"
    assert backend_environment["RAG_GLOBAL_RERANK_MAX_CONCURRENCY"] == "4"
    assert backend_environment["EMBEDDING_API_BATCH_SIZE"] == "46"
    assert services["backend"]["deploy"]["replicas"] == 8
    assert services["kb_seed"]["command"] == [
        "python",
        "scripts/rebuild_vectorstore.py",
        "--dataset",
        "core",
        "--exact",
    ]
    kb_sources = sorted(Path("loadtests/data/kb_raw").glob("**/*"))
    kb_sources = [path for path in kb_sources if path.is_file()]
    assert len(kb_sources) == 11
    assert all(path.suffix == ".md" for path in kb_sources)
    assert all("organization_unit" not in path.parts for path in kb_sources)

    load_generator = services["asr_load"]
    assert load_generator["profiles"] == ["asr-loadgen"]
    assert load_generator["depends_on"]["qwen3_asr"]["condition"] == "service_healthy"
    assert load_generator["depends_on"]["qwen3_asr_secondary"]["condition"] == "service_healthy"
    fixture_script = Path("loadtests/prepare_asr_fixture.sh")
    assert fixture_script.is_file()
    assert fixture_script.stat().st_mode & 0o111


def test_system_metric_acceptance_enforces_all_thresholds() -> None:
    passing = {
        "event_loop_lag_p95_ms": 99,
        "database_connection_wait_p95_ms": 99,
        "connection_pool_exhaustions": 0,
        "deadlocks": 0,
        "queue_depth_at_stop": 20,
        "queue_depth_after_drain": 0,
        "throughput_rps": 25,
        "single_backend_throughput_rps": 10,
        "single_user_p95_ms": 100,
        "thirty_user_p95_ms": 105,
    }
    assert evaluate_system_metrics(passing) == []
    failing = dict(passing, event_loop_lag_p95_ms=100, deadlocks=1)
    failures = evaluate_system_metrics(failing)
    assert any("event loop" in failure for failure in failures)
    assert any("deadlocks" in failure for failure in failures)


def test_locust_acceptance_does_not_dilute_failures_with_sse_metrics() -> None:
    summary = {
        "shape": "staircase",
        "total": {"requests": 1000, "failures": 1},
        "network_total": {"requests": 50, "failures": 1},
        "progress": {
            "attempted": 100,
            "completed": 99,
            "failed": 1,
            "active": 0,
            "max_active": 400,
        },
        "workflow_failures": {"workflow": 1},
        "load_generator": {"samples_over_70_percent": 0},
    }
    failures = evaluate_locust_summary(summary)
    assert any("network error rate" in failure for failure in failures)
    assert any("workflow failure rate" in failure for failure in failures)


def test_stub_supports_valid_schema_and_retry_race(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "guidance_plan_output",
                "schema": {"title": "GuidancePlanOutput", "type": "object"},
            },
        }
    }
    monkeypatch.setenv("MODEL_STUB_TRIGGER_RETRY_RACE", "true")
    assert _response_text(payload, model="loadtest-chat") == '{"loadtest_malformed":'
    result = json.loads(_response_text(payload, model="loadtest-retry"))
    assert result["points"][0]["title"]


def test_bosch_adapter_unwraps_nested_envelopes() -> None:
    payload = {"choices": [{"message": {"content": "ok"}}]}
    assert _unwrap({"data": {"data": payload}}) == payload


def test_bosch_adapter_supports_configurable_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOSCH_ADAPTER_API_KEY", "secret")
    assert _configured_auth_headers() == {"Authorization": "Bearer secret"}
    monkeypatch.setenv("BOSCH_ADAPTER_API_KEY_HEADER", "api-key")
    monkeypatch.setenv("BOSCH_ADAPTER_AUTH_SCHEME", "")
    assert _configured_auth_headers() == {"api-key": "secret"}


def test_unified_manifest_requires_requested_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "locust-summary.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "shape": "staircase",
                "model_mode": "stub",
                "worker_count": 8,
                "progress": {"max_active": 400, "active": 0},
                "total": {"requests": 1, "failures": 0},
                "acceptance": {"passed": True, "failures": []},
            }
        ),
        encoding="utf-8",
    )
    manifest = aggregate(run_dir=run_dir, require_aiperf=True, require_asr=True)
    assert not manifest["acceptance"]["passed"]
    assert any("missing AIPerf llm" in item for item in manifest["acceptance"]["failures"])
    assert any("concurrency-64" in item for item in manifest["acceptance"]["failures"])
