from __future__ import annotations

import csv
import logging
import os
import subprocess
from collections.abc import Iterable
from typing import Any

from opentelemetry import metrics as otel_metrics

logger = logging.getLogger(__name__)

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_GPU_UTILIZATION_GAUGE: Any | None = None


def _parse_nvidia_smi_output(output: str) -> tuple[otel_metrics.Observation, ...]:
    observations: list[otel_metrics.Observation] = []
    for row in csv.reader(output.splitlines()):
        if len(row) != 4:
            continue
        index, uuid, name, utilization = (item.strip() for item in row)
        try:
            utilization_ratio = min(1.0, max(0.0, float(utilization) / 100.0))
        except ValueError:
            continue
        observations.append(
            otel_metrics.Observation(
                utilization_ratio,
                {
                    "gpu.index": index,
                    "gpu.uuid": uuid,
                    "gpu.name": name,
                },
            )
        )
    return tuple(observations)


class NvidiaGpuObserver:
    """Read all visible NVIDIA devices without retaining process-level data."""

    def __init__(self, *, command: str = "nvidia-smi", timeout_seconds: float = 5.0):
        self.command = command
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._warning_emitted = False

    def observations(self, _options: Any) -> Iterable[otel_metrics.Observation]:
        try:
            result = subprocess.run(
                [
                    self.command,
                    "--query-gpu=index,uuid,name,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if not self._warning_emitted:
                logger.warning("GPU utilization collection is unavailable: %s", exc)
                self._warning_emitted = True
            return ()

        self._warning_emitted = False
        return _parse_nvidia_smi_output(result.stdout)


def initialize_gpu_metrics() -> None:
    """Register one utilization series per visible GPU when explicitly enabled."""

    global _GPU_UTILIZATION_GAUGE
    if _GPU_UTILIZATION_GAUGE is not None:
        return
    enabled = os.getenv("HR_AGENT_GPU_METRICS_ENABLED", "false").strip().lower()
    if enabled not in _TRUTHY_VALUES:
        return

    observer = NvidiaGpuObserver()
    meter = otel_metrics.get_meter("hr_agent.gpu_observability")
    _GPU_UTILIZATION_GAUGE = meter.create_observable_gauge(
        "hr_agent.system.gpu.utilization",
        callbacks=[observer.observations],
        unit="1",
        description=(
            "NVIDIA GPU utilization ratio per visible device; average devices for total capacity."
        ),
    )
