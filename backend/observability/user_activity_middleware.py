from __future__ import annotations

import logging
import threading
import time
from typing import Any

from backend.core.session_context import (
    get_current_auth_user_id,
    reset_current_auth_user_id,
    reset_current_observability_user_id,
    set_current_auth_user_id,
    set_current_observability_user_email,
)
from backend.observability.metrics import record_authenticated_user_request


logger = logging.getLogger(__name__)

HEALTH_PATH_SUFFIX = "/health"
AUTOSCALING_PROBE_PATH_SUFFIX = "/health/autoscaling"


class ActiveWorkloadTracker:
    """Keep a process-local count of work that is still inside the ASGI app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_http_requests = 0
        self._active_websockets = 0
        self._idle_since = time.monotonic()

    def enter(self, protocol: str) -> None:
        with self._lock:
            if protocol not in {"http", "websocket"}:
                return
            if self._active_http_requests + self._active_websockets == 0:
                self._idle_since = None
            if protocol == "http":
                self._active_http_requests += 1
            else:
                self._active_websockets += 1

    def exit(self, protocol: str) -> None:
        with self._lock:
            if protocol == "http":
                self._active_http_requests = max(0, self._active_http_requests - 1)
            elif protocol == "websocket":
                self._active_websockets = max(0, self._active_websockets - 1)
            else:
                return
            if self._active_http_requests + self._active_websockets == 0:
                self._idle_since = time.monotonic()

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            idle_for_seconds = (
                max(0.0, time.monotonic() - self._idle_since)
                if self._idle_since is not None
                else 0.0
            )
            return {
                "active_http_requests": self._active_http_requests,
                "active_websockets": self._active_websockets,
                "idle_for_seconds": idle_for_seconds,
            }


active_workload_tracker = ActiveWorkloadTracker()


def _is_health_probe(scope: dict[str, Any]) -> bool:
    if scope.get("type") != "http":
        return False
    path = str(scope.get("path") or "").rstrip("/")
    return path.endswith(AUTOSCALING_PROBE_PATH_SUFFIX) or path.endswith(
        HEALTH_PATH_SUFFIX
    )


class UserActivityMiddleware:
    """Attach authenticated request usage to an email local-part dimension."""

    def __init__(
        self,
        app: Any,
        workload_tracker: ActiveWorkloadTracker | None = None,
    ) -> None:
        self.app = app
        self.workload_tracker = workload_tracker or active_workload_tracker

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        protocol = scope.get("type")
        if protocol not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        track_workload = not _is_health_probe(scope)
        if track_workload:
            self.workload_tracker.enter(protocol)
        try:
            auth_token = set_current_auth_user_id(None)
            observability_token = set_current_observability_user_email(None)
            started_at = time.perf_counter()
            status_code = 500

            async def send_with_status(message: dict[str, Any]) -> None:
                nonlocal status_code
                message_type = message.get("type")
                if message_type == "http.response.start":
                    status_code = int(message.get("status", 500))
                elif message_type == "websocket.accept":
                    status_code = 101
                await send(message)

            try:
                await self.app(scope, receive, send_with_status)
            finally:
                user_id = get_current_auth_user_id()
                if user_id:
                    route_object = scope.get("route")
                    route = getattr(route_object, "path", None) or "unknown"
                    method = scope.get("method") or (
                        "CONNECT" if protocol == "websocket" else "unknown"
                    )
                    try:
                        record_authenticated_user_request(
                            method=method,
                            route=route,
                            status_code=status_code,
                            duration_ms=int(
                                round((time.perf_counter() - started_at) * 1000)
                            ),
                            protocol=protocol,
                        )
                    except Exception:
                        logger.exception("Failed to record authenticated user activity")
                reset_current_observability_user_id(observability_token)
                reset_current_auth_user_id(auth_token)
        finally:
            if track_workload:
                self.workload_tracker.exit(protocol)
