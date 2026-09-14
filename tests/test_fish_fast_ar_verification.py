from __future__ import annotations

import json
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_fish_fast_ar as verification


def _evidence(**overrides):
    return {"numerical_passed": True, "rng_passed": True, "sampling_passed": True, "compiled_calls": 120,
            "captured_graphs": 10, "graphless_calls": 0, "compile_failed": False,
            "timings": [{"speedup": 1.5}], "full_fast_ar_timings": [{"speedup": 1.2}], **overrides}


def test_default_checks_production_batch_changes_and_repeated_reuse(tmp_path):
    args = verification.parse_args(["--output", str(tmp_path / "report.json")])
    assert args.batches == [1, 10, 3, 1]
    assert args.passes == 2
    assert args.iterations == 10
    assert args.baseline_source_path is None


def test_optional_baseline_source_is_independent_of_candidate(tmp_path):
    baseline_path = tmp_path / "baseline.py"
    candidate_path = tmp_path / "candidate.py"
    args = verification.parse_args(["--output", str(tmp_path / "report.json"),
                                    "--baseline-source-path", str(baseline_path),
                                    "--source-path", str(candidate_path)])
    assert args.baseline_source_path == baseline_path
    assert args.source_path == candidate_path
    baseline_path.write_text("class FishSpeechFastAR:\n    marker = 'baseline'\n")
    candidate_path.write_text("class FishSpeechFastAR:\n    marker = 'candidate'\n")
    installed = object()
    try:
        baseline = verification._load_source_class(baseline_path, "baseline", installed)
        candidate = verification._load_source_class(candidate_path, "candidate", installed)
        assert baseline.marker == "baseline"
        assert candidate.marker == "candidate"
        assert baseline.__module__ != candidate.__module__
        assert verification._load_source_class(None, "baseline", installed) is installed
    finally:
        for role in ("baseline", "candidate"):
            sys.modules.pop(f"vllm_omni.model_executor.models.fish_speech._verification_{role}", None)


@pytest.mark.parametrize(("batches", "warm_batches", "capacity"), [
    ([1, 10, 3, 1], (1, 4), 10), ([1], (1,), 1), ([1, 2, 1], (1, 2), 2),
])
def test_warmup_prepares_production_capacity_before_reuse_checks(batches, warm_batches, capacity):
    calls = []
    torch = SimpleNamespace(device=lambda value: value, bfloat16="bf16")
    eager = SimpleNamespace(_ensure_buffers=lambda *args: calls.append(("eager_buffers", args)))
    candidate = SimpleNamespace(warmup_compile=lambda **kwargs: calls.append(("warmup", kwargs)))
    verification._warmup_models(torch, eager, candidate, SimpleNamespace(batches=batches, device="cuda:0"))
    assert calls == [("warmup", {"device": "cuda:0", "dtype": "bf16", "batch_sizes": warm_batches}),
                     ("eager_buffers", (capacity, "cuda:0", "bf16"))]


@pytest.mark.parametrize("arguments", [
    ["--batches", ""], ["--batches", "x"], ["--batches", "0,10"],
    ["--batches", "33"], ["--passes", "1"], ["--iterations", "-1"],
    ["--atol", "nan"], ["--rtol", "inf"], ["--rtol", "-0.1"],
])
def test_invalid_verification_arguments_are_rejected(arguments, tmp_path):
    with pytest.raises(SystemExit):
        verification.parse_args(["--output", str(tmp_path / "report.json"), *arguments])


@pytest.mark.parametrize("override", [
    {"compiled_calls": 0}, {"captured_graphs": 0}, {"compile_failed": True},
    {"graphless_calls": 1},
    {"numerical_passed": False}, {"rng_passed": False}, {"sampling_passed": False}, {"timings": []},
    {"timings": [{"speedup": 1.2}, {"speedup": 0.95}]},
    {"timings": [{"speedup": 1.0}]},
    {"full_fast_ar_timings": []},
    {"full_fast_ar_timings": [{"speedup": 1.2}, {"speedup": 0.95}]},
    {"full_fast_ar_timings": [{"speedup": 1.0}]},
])
def test_fallback_unexecuted_compilation_and_missing_speedup_cannot_pass(override):
    assert not verification._acceleration_passed(**_evidence(**override))


def test_acceleration_needs_correctness_execution_and_measured_improvement():
    assert verification._acceleration_passed(**_evidence())


@pytest.mark.parametrize("codes_match", [True, False, None])
def test_verification_requires_sampled_codes_even_when_rng_and_logits_match(monkeypatch, tmp_path, codes_match):
    counters = {"stats": {"unique_graphs": 0}}

    def compile_model(_target, *, backend, **_kwargs):
        counters["stats"]["unique_graphs"] += 1
        return backend(lambda *_args: None, [])

    torch = SimpleNamespace(
        __version__="test", compile=compile_model, inference_mode=nullcontext,
        _dynamo=SimpleNamespace(config=SimpleNamespace(patch=lambda **_kwargs: nullcontext())),
        _TorchCompileInductorWrapper=lambda *_args: lambda graph, _inputs: graph,
        cuda=SimpleNamespace(get_device_name=lambda _device: "test GPU; no device execution"),
    )
    candidate = SimpleNamespace(_compile_failed=False)

    def setup():
        candidate._compiled_model_fwd = torch.compile(lambda: None)

    candidate._setup_compile = setup
    monkeypatch.setitem(sys.modules, "torch._dynamo.utils", SimpleNamespace(counters=counters))
    monkeypatch.setitem(sys.modules, "vllm.config.vllm", SimpleNamespace(set_current_vllm_config=lambda _: nullcontext()))
    monkeypatch.setattr(verification, "_load_models", lambda *_args: (object(), candidate, object(), 27))
    monkeypatch.setattr(verification, "_warmup_models", lambda *_args:
                        candidate._compiled_model_fwd(SimpleNamespace(shape=(1,)), None, None, None, 0))
    monkeypatch.setattr(verification, "_numerics", lambda *_args:
                        [{field: {"allclose": True} for field in ("hidden", "logits", "k", "v")}])
    monkeypatch.setattr(verification, "_sampling", lambda *_args: [] if codes_match is None else [{
        "rng_states_match_eager": True, "all_rng_states_advanced": True,
        "permutation_rng_states_match": True, "permutation_codes_match": True,
        "eager_compiled_codes_match": codes_match,
    }])
    monkeypatch.setattr(verification, "_timings", lambda *_args, **_kwargs: [{"speedup": 1.2}])
    args = verification.parse_args(["--output", str(tmp_path / "report.json")])
    report = verification._verify_model(torch, object(), args)
    assert report["numerical_passed"] and report["rng_passed"]
    assert report["compiled_calls"] == report["compiled_graph_executions"] == 1
    assert report["graphless_calls"] == 0
    assert report["sampling_passed"] is (codes_match is True)
    assert report["verification_passed"] is (codes_match is True)
    assert report["acceleration_passed"] is (codes_match is True)


@pytest.mark.parametrize("full_forward", [False, True])
def test_zero_iterations_skips_all_timing_setup(full_forward):
    assert verification._timings(None, None, None, SimpleNamespace(iterations=0),
                                 full_forward=full_forward) == []


def test_full_fast_ar_timing_includes_sampling_with_persistent_independent_streams(monkeypatch):
    calls, synchronized = [], []

    class Generator:
        def __init__(self, *, device):
            self.device = device
            self.draws = 0

        def manual_seed(self, seed):
            self.seed = seed
            return self

    class SemanticRange(list):
        def __add__(self, offset):
            return [item + offset for item in self]

    class Model:
        _num_codebooks = 10
        slow_ar_config = SimpleNamespace(hidden_size=2560, semantic_begin_id=100)

        def __init__(self, label):
            self.label = label

        def __call__(self, hidden, semantic, *, do_sample, temperature, top_k, top_p, generators):
            assert (do_sample, temperature, top_k, top_p) == (True, 0.8, 30, 0.9)
            assert semantic == [100, 101]
            calls.append((self.label, hidden, generators, [rng.draws for rng in generators]))
            for rng in generators:
                rng.draws += 1

    # The two warmups are deliberately slower; measured medians must exclude them.
    durations_ms = (100, 100, 200, 200, 3, 3, 5, 5, 9, 9)
    clock = iter(value for started, duration in enumerate(durations_ms)
                 for value in (started, started + duration / 1000))
    monkeypatch.setattr(verification.time, "perf_counter", lambda: next(clock))
    torch = SimpleNamespace(
        Generator=Generator, bfloat16="bf16",
        randn=lambda *_args, **_kwargs: object(),
        arange=lambda batch, **_kwargs: SemanticRange(range(batch)),
        cuda=SimpleNamespace(synchronize=lambda device: synchronized.append(device)),
    )
    rows = verification._timings(torch, Model("eager"), Model("compiled"),
                                SimpleNamespace(batches=[2, 2], device="cuda:0", iterations=3), full_forward=True)
    assert len(rows) == 1
    assert rows[0]["iterations"] == 3
    assert rows[0]["eager_median_ms"] == pytest.approx(5)
    assert rows[0]["compiled_median_ms"] == pytest.approx(5)
    # Two untimed warmups plus three measurements, alternating execution order.
    assert [call[0] for call in calls] == ["eager", "compiled", "compiled", "eager", "eager", "compiled",
                                          "compiled", "eager", "eager", "compiled"]
    assert len(synchronized) == 20
    assert all(call[1] is calls[0][1] for call in calls)
    first_streams = {label: next(call[2] for call in calls if call[0] == label) for label in ("eager", "compiled")}
    for label, streams in first_streams.items():
        own_calls = [call for call in calls if call[0] == label]
        assert all(call[2] is streams for call in own_calls)
        assert [call[3] for call in own_calls] == [[0, 0], [1, 1], [2, 2], [3, 3], [4, 4]]
        assert [rng.seed for rng in streams] == [82000, 82001]
    assert all(a is not b for a, b in zip(first_streams["eager"], first_streams["compiled"]))


def test_real_model_error_is_written_as_failure_not_a_skipped_pass(monkeypatch, tmp_path, capsys):
    def fail(_args):
        raise RuntimeError("compile failed")

    monkeypatch.setattr(verification, "verify", fail)
    path = tmp_path / "nested" / "report.json"
    assert verification.main(["--output", str(path)]) == 1
    report = json.loads(path.read_text())
    assert report["verification_passed"] is False
    assert report["acceleration_passed"] is False
    assert report["error"] == "RuntimeError: compile failed"
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_numerical_pass_does_not_claim_a_speedup_without_measurement(monkeypatch, tmp_path):
    monkeypatch.setattr(verification, "verify", lambda _args: {
        "verification_passed": True, "acceleration_passed": False, "timings": []})
    path = tmp_path / "report.json"
    assert verification.main(["--output", str(path), "--iterations", "0"]) == 0
    assert not json.loads(path.read_text())["acceleration_passed"]


@pytest.mark.parametrize("failure", [None, "init", "body"])
def test_single_rank_uses_private_file_store_and_always_destroys_owned_groups(monkeypatch, failure):
    events, stores = [], []

    def initialize(**kwargs):
        events.append("init")
        assert kwargs["world_size"] == 1
        assert kwargs["rank"] == 0
        assert kwargs["local_rank"] == 0
        assert kwargs["distributed_init_method"].startswith("file:///tmp/fish-fast-ar-verification-")
        stores.append(Path(kwargs["distributed_init_method"].removeprefix("file://")).parent)
        assert stores[-1].exists()
        if failure == "init":
            raise RuntimeError("init failure")

    monkeypatch.setitem(sys.modules, "vllm.config.vllm", SimpleNamespace(set_current_vllm_config=lambda _: nullcontext()))
    monkeypatch.setitem(sys.modules, "vllm.distributed.parallel_state", SimpleNamespace(
        init_distributed_environment=initialize,
        initialize_model_parallel=lambda **kwargs: events.append("model"),
        destroy_model_parallel=lambda: events.append("destroy_model"),
        destroy_distributed_environment=lambda: events.append("destroy_world")))
    torch = SimpleNamespace(distributed=SimpleNamespace(is_initialized=lambda: False),
                            cuda=SimpleNamespace(current_device=lambda: 0))
    with pytest.raises(RuntimeError) if failure else nullcontext():
        with verification._single_rank(torch, object()):
            events.append("body")
            if failure == "body":
                raise RuntimeError("body failure")
    assert events[-2:] == ["destroy_model", "destroy_world"]
    assert not stores[-1].exists()


def test_single_rank_refuses_to_take_over_existing_worker_groups(monkeypatch):
    monkeypatch.setitem(sys.modules, "vllm.config.vllm", SimpleNamespace(set_current_vllm_config=lambda _: nullcontext()))
    monkeypatch.setitem(sys.modules, "vllm.distributed.parallel_state", SimpleNamespace(
        init_distributed_environment=None, initialize_model_parallel=None,
        destroy_model_parallel=None, destroy_distributed_environment=None))
    torch = SimpleNamespace(distributed=SimpleNamespace(is_initialized=lambda: True))
    with pytest.raises(RuntimeError, match="own process"):
        with verification._single_rank(torch, object()):
            pytest.fail("must not enter an existing worker")


def test_recompile_limit_fail_closed_is_scoped_to_the_verifier(monkeypatch):
    events = []

    @contextmanager
    def patch(**kwargs):
        assert kwargs == {"fail_on_recompile_limit_hit": True}
        events.append("strict_on")
        try:
            yield
        finally:
            events.append("strict_off")

    def compile_failure():
        assert events == ["strict_on"]
        raise RuntimeError("compile limit")

    monkeypatch.setitem(sys.modules, "torch._dynamo.utils", SimpleNamespace(counters={"stats": {"unique_graphs": 0}}))
    monkeypatch.setitem(sys.modules, "vllm.config.vllm", SimpleNamespace(set_current_vllm_config=lambda _: nullcontext()))
    monkeypatch.setattr(verification, "_load_models", lambda *_args: (
        object(), SimpleNamespace(_setup_compile=compile_failure), object(), 31))
    torch = SimpleNamespace(compile=lambda: None, inference_mode=nullcontext,
                            _dynamo=SimpleNamespace(config=SimpleNamespace(patch=patch)))
    with pytest.raises(RuntimeError, match="compile limit"):
        verification._verify_model(torch, object(), object())
    assert events == ["strict_on", "strict_off"]
