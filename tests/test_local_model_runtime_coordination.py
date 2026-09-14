from __future__ import annotations

import pytest

from backend.config.settings import Settings
from backend.services.local_model_runtime import LocalModelRuntimeRecycler
from backend.services.local_model_runtime_controller import (
    DockerModelRuntimeController,
    DockerRuntimeControllerBusyError,
)


def test_shared_controller_blocks_stale_replica_during_activity_and_global_ttl(
    monkeypatch,
):
    controller = DockerModelRuntimeController(activity_lease_ttl_seconds=90)
    service_name = "qwen_embedding"
    running = {"value": True}
    stop_requests: list[str] = []

    monkeypatch.setattr(
        controller,
        "_container_for_action",
        lambda _service: ("container-id", {"running": running["value"]}),
    )
    monkeypatch.setattr(
        controller,
        "state",
        lambda _service: {
            "exists": True,
            "running": running["value"],
            "status": "running" if running["value"] else "exited",
            "health": "healthy" if running["value"] else None,
        },
    )

    def docker_request(_method, path, **_kwargs):
        stop_requests.append(path)
        running["value"] = False

    monkeypatch.setattr(controller, "_docker_request", docker_request)

    lease = controller.acquire_activity_lease(service_name)
    with pytest.raises(DockerRuntimeControllerBusyError):
        controller.stop(service_name, idle_ttl_seconds=3600)
    assert stop_requests == []

    controller.release_activity_lease(service_name, lease["token"])
    with pytest.raises(DockerRuntimeControllerBusyError):
        controller.stop(service_name, idle_ttl_seconds=3600)
    assert stop_requests == []

    controller._last_activity_at[service_name] -= 3601
    controller.stop(service_name, idle_ttl_seconds=3600)
    assert stop_requests == ["/containers/container-id/stop"]


def test_busy_controller_result_does_not_reset_replica_local_idle_timer(
    monkeypatch,
):
    recycler = LocalModelRuntimeRecycler(
        Settings(
            model_provider_mode="local",
            local_model_auto_recycle_enabled=True,
            local_model_pinned_services="qwen3_asr",
            local_model_controller_url="http://model-controller:7120",
        )
    )
    recycler._last_used_at["qwen_embedding"] = 0
    monkeypatch.setattr(recycler, "_stop_service", lambda _service: None)

    recycler._stop_if_idle("qwen_embedding")

    assert recycler._last_used_at["qwen_embedding"] == 0
    assert "qwen_embedding" not in recycler._stopped_after_use
