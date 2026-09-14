from __future__ import annotations

import subprocess

from backend.observability.gpu_metrics import NvidiaGpuObserver, _parse_nvidia_smi_output


def test_parse_nvidia_smi_output_normalizes_all_devices_to_ratios() -> None:
    observations = _parse_nvidia_smi_output(
        "0, GPU-a, NVIDIA RTX A6000, 25\n"
        "1, GPU-b, NVIDIA RTX A6000, 75\n"
    )

    assert [observation.value for observation in observations] == [0.25, 0.75]
    assert [observation.attributes["gpu.index"] for observation in observations] == [
        "0",
        "1",
    ]


def test_gpu_observer_returns_no_samples_when_nvidia_smi_fails(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "nvidia-smi")

    monkeypatch.setattr(subprocess, "run", fail)

    assert tuple(NvidiaGpuObserver().observations(None)) == ()
