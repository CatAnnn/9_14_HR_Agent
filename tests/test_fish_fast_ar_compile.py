"""CPU-only contract tests for the actual vendored FastAR compile patch.

Extract the patched method bodies, not a second implementation.  Tiny Torch
and model stand-ins verify dispatch and failure handling without importing
Torch/vLLM, downloading weights, or reserving a GPU. Numerical parity and
compiler graph coverage are intentionally left to the real-model verifier.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


PATCH = (
    Path(__file__).resolve().parents[1]
    / "fish_tts/patches/vllm_omni_0_24_fish_fast_ar_compile.patch"
)


def _post_image_lines() -> list[str]:
    """Reconstruct context and new lines by their actual post-patch offsets."""
    lines: dict[int, str] = {}
    position = None
    in_fast_ar_file = False
    for line in PATCH.read_text(encoding="utf-8").splitlines():
        if line.startswith("+++ "):
            in_fast_ar_file = line.endswith("/fish_speech_fast_ar.py")
            position = None
            continue
        if not in_fast_ar_file:
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            position = int(hunk.group(1))
        elif position is not None and line.startswith((" ", "+")):
            value = line[1:]
            if position in lines:
                assert lines[position] == value, "Conflicting patch hunk context"
            lines[position] = value
            position += 1
        elif line.startswith("--- "):
            position = None
    assert lines, "FastAR patch must contain an implementation hunk"
    return [
        lines.get(index, "        # <omitted patch context>")
        for index in range(min(lines), max(lines) + 1)
    ]


def _method_body(lines: list[str], name: str) -> str:
    # A unified diff can omit the unchanged signature, but must include every
    # implementation line under test. Only the unchanged test shell is supplied.
    definition = next(
        (i for i, line in enumerate(lines) if line.startswith(f"    def {name}(")),
        None,
    )
    if definition is None:
        assert name == "_setup_compile"
        start = lines.index("        if self._compile_attempted:")
    else:
        signature_end = next(
            i
            for i in range(definition, len(lines))
            if lines[i].rstrip().endswith(":")
        )
        start = signature_end + 1
    end = start
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.startswith("        "):
            break
        end += 1
    body = "\n".join(lines[start:end]).rstrip()
    assert body.strip(), f"Missing patched method body: {name}"
    assert "# <omitted patch context>" not in body, (
        f"Patch must retain the whole {name} body; regenerate with more unified context"
    )
    return body


class _BackendCompilerFailed(Exception):
    def __init__(self, inner_exception: Exception | None = None):
        super().__init__(str(inner_exception or "compiler failed"))
        self.inner_exception = inner_exception


class _Unsupported(Exception):
    pass


class _OutOfMemoryError(RuntimeError):
    pass


@pytest.fixture
def harness():
    compiled = Mock(name="compiled_forward_one", return_value=object())
    torch = SimpleNamespace(
        compile=Mock(return_value=compiled),
        _dynamo=SimpleNamespace(
            exc=SimpleNamespace(
                BackendCompilerFailed=_BackendCompilerFailed,
                Unsupported=_Unsupported,
            )
        ),
        OutOfMemoryError=_OutOfMemoryError,
        long="int64",
        zeros=Mock(side_effect=lambda shape, **kw: SimpleNamespace(shape=shape, **kw)),
        full=Mock(
            side_effect=lambda shape, fill_value, **kw: SimpleNamespace(
                shape=shape, fill_value=fill_value, **kw
            )
        ),
        accelerator=SimpleNamespace(synchronize=Mock()),
    )
    logger = Mock()
    lines = _post_image_lines()
    signatures = {
        "_setup_compile": "self",
        "warmup_compile": "self, device, dtype, batch_sizes=(1,)",
        "_run_model_one": "self, input_embed, step_pos_ids, cache_pos",
    }
    source = "class PatchedFastAR:\n" + "\n".join(
        f"    def {name}({signature}):\n{_method_body(lines, name)}\n"
        for name, signature in signatures.items()
    )
    namespace = {"torch": torch, "logger": logger}
    exec(compile(source, str(PATCH), "exec"), namespace)
    cls = namespace["PatchedFastAR"]
    cls.__call__ = lambda self, *args, **kwargs: self.forward(*args, **kwargs)
    instance = cls()
    model = SimpleNamespace(
        forward_one=Mock(name="eager_forward_one", return_value=object()),
        forward=Mock(name="unused_full_sequence_forward"),
    )
    instance.model = model
    instance._compile_attempted = False
    instance._compile_failed = False
    instance._disable_compile_for_graph = False
    instance._compiled_model_fwd = None
    instance._k_cache = object()
    instance._v_cache = object()
    instance._vllm_config = SimpleNamespace(
        scheduler_config=SimpleNamespace(max_num_seqs=10)
    )
    instance.slow_ar_config = SimpleNamespace(hidden_size=2560, semantic_begin_id=1024)
    instance._ensure_buffers = Mock()
    instance.forward = Mock()
    return SimpleNamespace(
        instance=instance, model=model, torch=torch, logger=logger, compiled=compiled
    )


@pytest.mark.parametrize("legacy_graph_flag", [False, True])
def test_compile_targets_the_real_one_token_path_with_no_cuda_graphs(harness, legacy_graph_flag):
    subject = harness.instance
    subject._disable_compile_for_graph = legacy_graph_flag
    subject._setup_compile()
    harness.torch.compile.assert_called_once_with(
        harness.model.forward_one,
        dynamic=True,
        fullgraph=False,
        options={"triton.cudagraphs": False, "emulate_precision_casts": True, "pattern_matcher": False},
    )
    assert subject._compiled_model_fwd is harness.compiled
    assert subject._compile_attempted
    harness.model.forward.assert_not_called()


def test_setup_compile_is_not_repeated(harness):
    harness.instance._setup_compile()
    harness.instance._setup_compile()
    harness.torch.compile.assert_called_once()


def test_run_routes_all_positions_and_live_kv_objects_to_compiled_callable(harness):
    subject = harness.instance
    embedding, positions = object(), object()
    for cache_pos in range(10):
        output = subject._run_model_one(embedding, positions, cache_pos)
        assert output is harness.compiled.return_value
        harness.compiled.assert_called_with(
            embedding, positions, subject._k_cache, subject._v_cache, cache_pos
        )
    assert harness.compiled.call_count == 10
    harness.torch.compile.assert_called_once()
    harness.model.forward_one.assert_not_called()
    harness.model.forward.assert_not_called()


@pytest.mark.parametrize("missing_cache", ["_k_cache", "_v_cache"])
def test_missing_kv_cache_fails_before_compilation(harness, missing_cache):
    setattr(harness.instance, missing_cache, None)
    with pytest.raises(AssertionError):
        harness.instance._run_model_one(object(), object(), 0)
    harness.torch.compile.assert_not_called()


@pytest.mark.parametrize("error", [_BackendCompilerFailed(), _Unsupported("unsupported")])
def test_lazy_compile_failure_retries_same_step_eager_and_permanently_disables_compile(harness, error):
    subject = harness.instance
    harness.compiled.side_effect = error
    embedding, positions = object(), object()
    output = subject._run_model_one(embedding, positions, 3)
    assert output is harness.model.forward_one.return_value
    harness.model.forward_one.assert_called_once_with(
        embedding, positions, subject._k_cache, subject._v_cache, 3
    )
    assert subject._compile_failed
    assert subject._compiled_model_fwd is None
    subject._run_model_one(embedding, positions, 4)
    harness.torch.compile.assert_called_once()
    harness.compiled.assert_called_once()
    assert harness.model.forward_one.call_count == 2
    harness.logger.warning.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        _OutOfMemoryError("CUDA out of memory"),
        RuntimeError("CUDA error: an illegal memory access was encountered"),
        RuntimeError("CUDA error: device-side assert triggered"),
        ValueError("invalid tensor shape"),
    ],
)
def test_non_compiler_runtime_failures_are_not_retried_or_hidden(harness, error):
    harness.compiled.side_effect = error
    with pytest.raises(type(error)) as caught:
        harness.instance._run_model_one(object(), object(), 2)
    assert caught.value is error
    harness.model.forward_one.assert_not_called()
    harness.logger.warning.assert_not_called()
    assert not harness.instance._compile_failed


def test_compiler_wrapped_oom_is_not_retried(harness):
    error = _BackendCompilerFailed(_OutOfMemoryError("CUDA out of memory"))
    harness.compiled.side_effect = error
    with pytest.raises(_BackendCompilerFailed) as caught:
        harness.instance._run_model_one(object(), object(), 2)
    assert caught.value is error
    harness.model.forward_one.assert_not_called()
    assert not harness.instance._compile_failed


@pytest.mark.parametrize(
    "message",
    [
        "CUDA error: an illegal memory access was encountered",
        "CUDA error: device-side assert triggered",
    ],
)
def test_compiler_wrapped_cuda_execution_failure_is_not_retried(harness, message):
    error = _BackendCompilerFailed(RuntimeError(message))
    harness.compiled.side_effect = error
    with pytest.raises(_BackendCompilerFailed) as caught:
        harness.instance._run_model_one(object(), object(), 2)
    assert caught.value is error
    harness.model.forward_one.assert_not_called()
    assert not harness.instance._compile_failed


def test_immediate_compile_factory_failure_is_not_masked(harness):
    error = RuntimeError("compile initialization failed")
    harness.torch.compile.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        harness.instance._run_model_one(object(), object(), 0)
    assert caught.value is error
    harness.model.forward_one.assert_not_called()
    assert harness.instance._compile_attempted
    harness.instance._run_model_one(object(), object(), 0)
    harness.torch.compile.assert_called_once()
    harness.model.forward_one.assert_called_once()


def test_already_failed_compile_skips_torch_and_runs_eager(harness):
    subject = harness.instance
    subject._compile_attempted = True
    subject._compile_failed = True
    subject._compiled_model_fwd = None
    subject._run_model_one(object(), object(), 1)
    harness.torch.compile.assert_not_called()
    harness.compiled.assert_not_called()
    harness.model.forward_one.assert_called_once()


@pytest.mark.parametrize("capacity,expected_batches", [(1, [1]), (2, [1, 2]), (10, [1, 2, 4, 10])])
def test_warmup_allocates_max_capacity_first_and_exercises_real_forward_without_sampling(
    harness, capacity, expected_batches
):
    subject = harness.instance
    subject._vllm_config.scheduler_config.max_num_seqs = capacity
    order = []
    subject._ensure_buffers.side_effect = lambda *args: order.append(("allocate", args[0]))
    subject.forward.side_effect = lambda hidden, semantic, **kwargs: order.append(
        ("forward", hidden.shape[0], kwargs)
    )
    subject.warmup_compile("cuda:0", "bfloat16", batch_sizes=(4, 4, 0, -1, 99))
    subject._ensure_buffers.assert_called_once_with(capacity, "cuda:0", "bfloat16")
    assert order == [("allocate", capacity)] + [
        ("forward", batch, {"do_sample": False}) for batch in expected_batches
    ]
    for call, batch in zip(subject.forward.call_args_list, expected_batches, strict=True):
        hidden, semantic = call.args
        assert hidden.shape == (batch, 2560)
        assert hidden.dtype == "bfloat16"
        assert semantic.shape == (batch,)
        assert semantic.fill_value == 1024
        assert semantic.dtype == "int64"
        assert hidden.device == semantic.device == "cuda:0"
    harness.torch.accelerator.synchronize.assert_called_once_with("cuda:0")


def test_warmup_stops_after_fallback_and_does_not_claim_compilation_success(harness):
    subject = harness.instance

    def failed_forward(*args, **kwargs):
        subject._compile_failed = True

    subject.forward.side_effect = failed_forward
    subject.warmup_compile("cuda:0", "bfloat16", batch_sizes=(4,))
    subject.forward.assert_called_once()
    harness.torch.accelerator.synchronize.assert_called_once()
    assert not any("warmup completed" in str(call) for call in harness.logger.info.call_args_list)


def test_warmup_does_no_work_after_prior_compile_failure(harness):
    subject = harness.instance
    subject._compile_attempted = True
    subject._compile_failed = True
    subject.warmup_compile("cuda:0", "bfloat16")
    harness.torch.compile.assert_not_called()
    subject._ensure_buffers.assert_not_called()
    subject.forward.assert_not_called()
    harness.torch.accelerator.synchronize.assert_not_called()


def test_warmup_propagates_device_failure_without_success_message(harness):
    error = RuntimeError("CUDA error: device-side assert triggered")
    harness.instance.forward.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        harness.instance.warmup_compile("cuda:0", "bfloat16")
    assert caught.value is error
    assert not any("warmup completed" in str(call) for call in harness.logger.info.call_args_list)
