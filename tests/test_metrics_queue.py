from __future__ import annotations

import io

from backend.observability import metrics


def test_metrics_queue_flushes_every_record_on_shutdown(monkeypatch) -> None:
    metrics.shutdown_metrics_logger()
    stream = io.StringIO()
    monkeypatch.setattr(metrics.sys, "stdout", stream)
    try:
        for index in range(25):
            metrics.log_metric("queue_test", sequence=index)
        metrics.shutdown_metrics_logger()
    finally:
        metrics.shutdown_metrics_logger()

    lines = [line for line in stream.getvalue().splitlines() if "queue_test" in line]
    assert len(lines) == 25
    assert '"sequence": 0' in lines[0]
    assert '"sequence": 24' in lines[-1]
