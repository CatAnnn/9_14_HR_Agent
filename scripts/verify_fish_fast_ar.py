"""Numerically verify the installed FastAR decode compiler with real S2-Pro weights.

Run inside the Fish image, with its checkpoint volume and an explicitly selected
GPU. This is a standalone process: it does not call or change the running server.
Synthetic hidden states test decoder equivalence, not speech quality. Compilation
fallback, an unexecuted compiled function, and a missing speed measurement cannot
count as an acceleration pass.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import statistics
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=Path("/app/checkpoints/s2-pro"))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--source-path", type=Path,
                        help="Candidate FastAR module; leaves the installed server module untouched.")
    parser.add_argument("--baseline-source-path", type=Path,
                        help="Optional reference FastAR module; its decoder is still evaluated eagerly.")
    parser.add_argument("--batches", default="1,10,3,1")
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=10,
                        help="Measured iterations per batch for decoder and full FastAR; 0 skips both timings.")
    parser.add_argument("--atol", type=float, default=0.02)
    parser.add_argument("--rtol", type=float, default=0.02)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        args.batches = [int(value) for value in args.batches.split(",")]
    except ValueError:
        parser.error("--batches must be comma-separated positive integers")
    if not args.batches or min(args.batches) < 1 or max(args.batches) > 32:
        parser.error("--batches values must be in 1..32")
    if args.passes < 2 or args.iterations < 0:
        parser.error("--passes must be at least 2; --iterations cannot be negative")
    if any(not math.isfinite(x) or x < 0 for x in (args.atol, args.rtol)):
        parser.error("tolerances must be finite and nonnegative")
    return args


def _acceleration_passed(*, numerical_passed, rng_passed, sampling_passed, compiled_calls,
                         captured_graphs, graphless_calls, compile_failed, timings, full_fast_ar_timings):
    return bool(numerical_passed and rng_passed and sampling_passed and compiled_calls > 0
                and captured_graphs > 0 and graphless_calls == 0 and not compile_failed and timings
                and full_fast_ar_timings
                and all(item["speedup"] > 1.0 for item in (*timings, *full_fast_ar_timings)))


def _load_source_class(source_path, role, installed_class):
    if source_path is None:
        return installed_class
    module_name = f"vllm_omni.model_executor.models.fish_speech._verification_{role}"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import {role} source: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.FishSpeechFastAR


@contextmanager
def _single_rank(torch, config):
    from vllm.config.vllm import set_current_vllm_config
    from vllm.distributed.parallel_state import (
        destroy_distributed_environment, destroy_model_parallel,
        init_distributed_environment, initialize_model_parallel,
    )

    if torch.distributed.is_initialized():
        raise RuntimeError("Verification must run in its own process, not an existing distributed worker")
    # A new file store avoids every server's MASTER_PORT and rendezvous. Only
    # this standalone process owns these groups and may destroy them afterward.
    with tempfile.TemporaryDirectory(prefix="fish-fast-ar-verification-") as directory:
        with set_current_vllm_config(config):
            try:
                init_distributed_environment(
                    world_size=1, rank=0, local_rank=torch.cuda.current_device(),
                    distributed_init_method=(Path(directory) / "rendezvous").as_uri(), backend="nccl")
                initialize_model_parallel(tensor_model_parallel_size=1, pipeline_model_parallel_size=1)
                yield
            finally:
                try:
                    destroy_model_parallel()
                finally:
                    destroy_distributed_environment()


def _load_models(torch, args, vllm_config):
    from safetensors import safe_open
    from vllm.config.vllm import set_current_vllm_config
    from vllm_omni.model_executor.models.fish_speech.configuration_fish_speech import FishSpeechConfig
    from vllm_omni.model_executor.models.fish_speech.fish_speech_fast_ar import FishSpeechFastAR
    from vllm_omni.model_executor.models.fish_speech.fish_speech_slow_ar import _remap_fish_speech_weights

    baseline_class = _load_source_class(args.baseline_source_path, "baseline", FishSpeechFastAR)
    candidate_class = _load_source_class(args.source_path, "candidate", FishSpeechFastAR)
    config = FishSpeechConfig(**json.loads((args.checkpoint / "config.json").read_text()))
    index = json.loads((args.checkpoint / "model.safetensors.index.json").read_text())["weight_map"]
    shards = defaultdict(list)
    for name, shard in index.items():
        if name.startswith("audio_decoder.") and name != "audio_decoder.codebook_embeddings.weight":
            shards[shard].append(name)
    if not shards:
        raise ValueError("Checkpoint contains no FastAR weights")

    def weights():
        for shard, names in sorted(shards.items()):
            with safe_open(str(args.checkpoint / shard), framework="pt", device="cpu") as handle:
                for name in sorted(names):
                    yield name, handle.get_tensor(name)

    slow, fast = config.text_config, config.audio_decoder_config
    original_dtype = torch.get_default_dtype()
    try:
        torch.set_default_dtype(torch.bfloat16)
        with set_current_vllm_config(vllm_config), torch.device(args.device):
            eager = baseline_class(vllm_config=vllm_config, config=fast, slow_ar_config=slow).eval()
            candidate = candidate_class(vllm_config=vllm_config, config=fast, slow_ar_config=slow).eval()
        remapped = _remap_fish_speech_weights(
            weights(), slow.num_attention_heads, slow.num_key_value_heads, slow.head_dim,
            fast.num_attention_heads, fast.num_key_value_heads, fast.head_dim,
        )
        loaded = eager.load_weights((name.removeprefix("fast_ar."), tensor)
                                    for name, tensor in remapped if name.startswith("fast_ar."))
        missing = sorted(set(dict(eager.named_parameters())) - loaded)
        if missing:
            raise ValueError(f"FastAR weights were not fully loaded: {missing}")
        candidate.load_state_dict(eager.state_dict(), strict=True)
        for model in (eager, candidate):
            for module in model.modules():
                if hasattr(module, "cos_sin_cache"):
                    cache = module.cos_sin_cache
                    module.cos_sin_cache = cache.to(torch.bfloat16).to(cache.dtype)
    finally:
        torch.set_default_dtype(original_dtype)
    # Keep the baseline independent of any newly installed dispatch wrapper.
    eager._run_model_one = lambda embed, positions, pos: eager.model.forward_one(
        embed, positions, eager._k_cache, eager._v_cache, pos)
    return eager, candidate, vllm_config, len(loaded)


def _difference(torch, actual, expected, args):
    a, b = actual.float(), expected.float()
    finite = bool(torch.isfinite(a).all() and torch.isfinite(b).all())
    delta = (a - b).abs()
    return {"allclose": finite and bool(torch.allclose(a, b, atol=args.atol, rtol=args.rtol)),
            "max_abs_error": float(delta.max()) if finite else None,
            "rms_error": float(delta.square().mean().sqrt()) if finite else None}


def _warmup_models(torch, eager, candidate, args):
    maximum = max(args.batches)
    device = torch.device(args.device)
    candidate.warmup_compile(device=device, dtype=torch.bfloat16,
                             batch_sizes=tuple(sorted({1, min(4, maximum)})))
    eager._ensure_buffers(maximum, device, torch.bfloat16)


def _numerics(torch, eager, candidate, args):
    rows = []
    generator = torch.Generator(device=args.device).manual_seed(230906)
    for pass_index in range(args.passes):
        for batch in args.batches:
            for model in (eager, candidate):
                model._ensure_buffers(batch, torch.device(args.device), torch.bfloat16)
            # Same teacher-forced embeddings prevent a small sampling difference
            # from contaminating comparison of all subsequent codebook positions.
            # Real decoding slices [capacity, codebooks + 1, hidden], including
            # the final spare slot. Preserve those strides in compiler guards.
            embeds = torch.randn(batch, eager._num_codebooks + 1, eager._fast_dim,
                                 device=args.device, dtype=torch.bfloat16, generator=generator)
            for model in (eager, candidate):
                model._embed_buf[:batch].copy_(embeds)
            for pos in range(eager._num_codebooks):
                expected = eager._run_model_one(eager._embed_buf[:batch, pos], eager._pos_ids[:batch, pos], pos)
                actual = candidate._run_model_one(candidate._embed_buf[:batch, pos], candidate._pos_ids[:batch, pos], pos)
                row = {"pass": pass_index, "batch": batch, "cache_pos": pos,
                       "cache_capacity": candidate._k_cache.shape[1],
                       "hidden": _difference(torch, actual, expected, args),
                       "logits": _difference(torch, candidate.fast_output(candidate.fast_norm(actual)),
                                              eager.fast_output(eager.fast_norm(expected)), args)}
                for field in ("k", "v"):
                    row[field] = _difference(
                        torch, getattr(candidate, f"_{field}_cache")[:, :batch, :, :pos + 1],
                        getattr(eager, f"_{field}_cache")[:, :batch, :, :pos + 1], args)
                rows.append(row)
    return rows


def _sampling(torch, eager, candidate, args):
    batch = max(args.batches)
    generator = torch.Generator(device=args.device).manual_seed(4701)
    hidden = torch.randn(batch, eager.slow_ar_config.hidden_size, device=args.device,
                         dtype=torch.bfloat16, generator=generator)
    semantic = torch.arange(batch, device=args.device) + eager.slow_ar_config.semantic_begin_id
    permutation = list(reversed(range(batch)))
    order = torch.tensor(permutation, device=args.device)

    def generators():
        return [torch.Generator(device=args.device).manual_seed(61000 + row) for row in range(batch)]

    eager_rng, candidate_rng, permuted_rng = generators(), generators(), generators()
    rows = []
    for pass_index in range(args.passes):
        before = [rng.get_state().clone() for rng in candidate_rng]
        reference = eager(hidden, semantic, generators=eager_rng)
        actual = candidate(hidden, semantic, generators=candidate_rng)
        permuted = candidate(hidden[order], semantic[order],
                             generators=[permuted_rng[row] for row in permutation])
        rows.append({
            "pass": pass_index,
            "rng_states_match_eager": all(torch.equal(a.get_state(), b.get_state())
                                          for a, b in zip(eager_rng, candidate_rng)),
            "all_rng_states_advanced": all(not torch.equal(old, rng.get_state())
                                           for old, rng in zip(before, candidate_rng)),
            "permutation_rng_states_match": all(torch.equal(a.get_state(), b.get_state())
                                                for a, b in zip(candidate_rng, permuted_rng)),
            "permutation_codes_match": bool(torch.equal(actual[order], permuted)),
            "eager_compiled_codes_match": bool(torch.equal(reference, actual)),
            "eager_compiled_code_agreement": float((reference == actual).float().mean()),
        })
    return rows


def _timings(torch, eager, candidate, args, *, full_forward=False):
    rows = []
    if not args.iterations:
        return rows
    for batch in sorted(set(args.batches)):
        if full_forward:
            hidden_rng = torch.Generator(device=args.device).manual_seed(4701)
            hidden = torch.randn(batch, eager.slow_ar_config.hidden_size, device=args.device,
                                 dtype=torch.bfloat16, generator=hidden_rng)
            semantic = torch.arange(batch, device=args.device) + eager.slow_ar_config.semantic_begin_id
            # Separate, equally seeded streams persist through both warmup and
            # measurement. Allocation and reseeding stay outside the timed call.
            generators = {
                label: [torch.Generator(device=args.device).manual_seed(82000 + row) for row in range(batch)]
                for label in ("eager", "compiled")
            }
        else:
            for model in (eager, candidate):
                model._embed_buf[:batch].zero_()
        samples = {"eager": [], "compiled": []}
        # Warm both paths, then alternate timed order to reduce order bias.
        for iteration in range(args.iterations + 2):
            order = (("eager", eager), ("compiled", candidate))
            for label, model in (order if iteration % 2 == 0 else reversed(order)):
                torch.cuda.synchronize(args.device)
                started = time.perf_counter()
                if full_forward:
                    model(hidden, semantic, do_sample=True, temperature=0.8, top_k=30, top_p=0.9,
                          generators=generators[label])
                else:
                    for pos in range(eager._num_codebooks):
                        model._run_model_one(model._embed_buf[:batch, pos], model._pos_ids[:batch, pos], pos)
                torch.cuda.synchronize(args.device)
                elapsed = (time.perf_counter() - started) * 1000
                if iteration >= 2:
                    samples[label].append(elapsed)
        eager_ms, compiled_ms = (statistics.median(samples[key]) for key in ("eager", "compiled"))
        rows.append({"batch": batch, "positions_per_sweep": eager._num_codebooks,
                     "iterations": args.iterations, "eager_median_ms": eager_ms,
                     "compiled_median_ms": compiled_ms, "speedup": eager_ms / compiled_ms})
    return rows


def verify(args):
    import torch
    from vllm.config import VllmConfig

    if not args.device.startswith("cuda") or not torch.cuda.is_available():
        raise RuntimeError("Real verification requires the explicitly selected CUDA device")
    torch.cuda.set_device(args.device)
    config = VllmConfig()
    config.scheduler_config.max_num_seqs = max(args.batches)
    with _single_rank(torch, config):
        return _verify_model(torch, config, args)


def _verify_model(torch, config, args):
    from torch._dynamo.utils import counters
    from vllm.config.vllm import set_current_vllm_config

    before_graphs = counters["stats"]["unique_graphs"]
    eager, candidate, config, loaded_count = _load_models(torch, args, config)
    compiled_calls = 0
    graph_executions = 0
    graphless_calls = 0
    decoded_shapes = defaultdict(lambda: {"calls": 0, "graphless_calls": 0})
    inductor = None

    def counting_backend(graph, inputs):
        compiled_graph = inductor(graph, inputs)

        def counted_graph(*values):
            nonlocal graph_executions
            graph_executions += 1
            return compiled_graph(*values)

        # Inductor may return a boxed callable; preserve the calling convention.
        if hasattr(compiled_graph, "_boxed_call"):
            counted_graph._boxed_call = compiled_graph._boxed_call
        return counted_graph

    with set_current_vllm_config(config), torch.inference_mode(), torch._dynamo.config.patch(
        fail_on_recompile_limit_hit=True
    ):
        original_compile = torch.compile

        def tracked_compile(*values, **kwargs):
            nonlocal inductor
            if kwargs.get("backend", "inductor") != "inductor":
                raise ValueError("Verification requires the real Inductor backend")
            # Use PyTorch's actual option/mode adapter, not compile_fx(options=...),
            # which is not its API and would create a false compilation failure.
            inductor = torch._TorchCompileInductorWrapper(
                kwargs.pop("mode", None), kwargs.pop("options", None), kwargs.get("dynamic"))
            return original_compile(*values, **{**kwargs, "backend": counting_backend})

        # Only install instrumentation while creating the lazy compiled callable.
        # Numerical execution still uses the actual Inductor-generated kernels.
        torch.compile = tracked_compile
        try:
            candidate._setup_compile()
        finally:
            torch.compile = original_compile
        compiled = candidate._compiled_model_fwd
        if compiled is None:
            raise RuntimeError("FastAR did not create its compiled decode function")

        def observed(*values, **kwargs):
            nonlocal compiled_calls, graphless_calls
            compiled_calls += 1
            before = graph_executions
            result = compiled(*values, **kwargs)
            key = f"batch={values[0].shape[0]},pos={values[4]}"
            decoded_shapes[key]["calls"] += 1
            if graph_executions == before:
                graphless_calls += 1
                decoded_shapes[key]["graphless_calls"] += 1
            return result

        candidate._compiled_model_fwd = observed
        _warmup_models(torch, eager, candidate, args)
        numerics = _numerics(torch, eager, candidate, args)
        sampling = _sampling(torch, eager, candidate, args)
        timings = _timings(torch, eager, candidate, args)
        full_fast_ar_timings = _timings(torch, eager, candidate, args, full_forward=True)
    captured_graphs = counters["stats"]["unique_graphs"] - before_graphs
    numerical_passed = all(row[field]["allclose"] for row in numerics for field in ("hidden", "logits", "k", "v"))
    rng_passed = all(row[key] for row in sampling for key in (
        "rng_states_match_eager", "all_rng_states_advanced", "permutation_rng_states_match", "permutation_codes_match"))
    sampling_passed = bool(sampling) and all(row["eager_compiled_codes_match"] for row in sampling)
    evidence = dict(numerical_passed=numerical_passed, rng_passed=rng_passed, sampling_passed=sampling_passed,
                    compiled_calls=compiled_calls, captured_graphs=captured_graphs,
                    graphless_calls=graphless_calls,
                    compile_failed=bool(candidate._compile_failed), timings=timings,
                    full_fast_ar_timings=full_fast_ar_timings)
    return {
        "checkpoint": str(args.checkpoint), "torch_version": torch.__version__,
        "gpu": torch.cuda.get_device_name(args.device), "dtype": "bfloat16",
        "weights_loaded": loaded_count, "batches": args.batches, "passes": args.passes,
        "baseline_source": str(args.baseline_source_path) if args.baseline_source_path else "installed",
        "candidate_source": str(args.source_path) if args.source_path else "installed",
        "compiled_graph_executions": graph_executions, "decode_shapes": dict(decoded_shapes),
        "fail_on_recompile_limit_hit": True,
        "atol": args.atol, "rtol": args.rtol, **evidence,
        "verification_passed": numerical_passed and rng_passed and sampling_passed and compiled_calls > 0
                               and captured_graphs > 0 and graphless_calls == 0 and not candidate._compile_failed,
        "acceleration_passed": _acceleration_passed(**evidence),
        "numerics": numerics, "sampling": sampling,
        "dynamo_counters": {key: dict(value) for key, value in counters.items()},
        "limitations": ["Synthetic hidden states with real weights; not a speech-quality assessment.",
                        "Teacher-forced decoder timing excludes the output head and sampling.",
                        "Full FastAR timing includes the output head and per-request sampling, but excludes SlowAR, DAC and HTTP.",
                        "Both timings compare an eager reference decoder with the compiled candidate, not two compiled production versions.",
                        "Inductor graph execution counters add small Python instrumentation overhead.",
                        "Exact sampled code agreement is required independently of RNG-state agreement; sampling uses the maximum batch and default parameters.",
                        "Run on an otherwise idle GPU; live server activity would bias timings."],
    }


def main(argv=None):
    args = parse_args(argv)
    started = datetime.now(timezone.utc).isoformat()
    try:
        report = verify(args)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        report = {"verification_passed": False, "acceleration_passed": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    report.update(started_at=started, completed_at=datetime.now(timezone.utc).isoformat())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
    args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if report["verification_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
