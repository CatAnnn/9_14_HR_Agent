from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
import threading
import time

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import log_metric


_PRIORITIES = {
    "interactive": 0,
    "guidance": 1,
    "coach": 2,
    "guidance_evidence": 3,
    "coach_evidence": 4,
    "background": 5,
}


@dataclass(frozen=True, slots=True)
class ModelQueueSlot:
    queue_ms: float
    active_count: int
    endpoint: str = ""


@dataclass(slots=True)
class _EndpointState:
    active: int = 0
    ewma_latency_ms: float = 0.0
    error_rate: float = 0.0
    attempts: int = 0


_CURRENT_MODEL_ENDPOINT: ContextVar[str | None] = ContextVar(
    "current_model_endpoint",
    default=None,
)
_CURRENT_MODEL_SCHEDULER: ContextVar[ModelScheduler | None] = ContextVar(
    "current_model_scheduler",
    default=None,
)


def current_model_endpoint(default: str = "") -> str:
    """Return the endpoint selected for the current scheduled invocation."""

    return _CURRENT_MODEL_ENDPOINT.get() or default


def model_endpoint_candidates(settings: Settings) -> tuple[str, ...]:
    """Return the selected endpoint first, followed by failover endpoints."""

    selected = current_model_endpoint()
    configured = tuple(getattr(settings, "chat_urls", ()) or ())
    if not configured:
        primary = str(getattr(settings, "chat_url", "") or "").strip()
        configured = (primary,) if primary else ()
    if selected:
        configured = (selected, *configured)
    return tuple(dict.fromkeys(endpoint for endpoint in configured if endpoint))


def record_model_endpoint_attempt(
    endpoint: str,
    *,
    duration_ms: float,
    success: bool,
) -> None:
    """Feed one transport attempt back into the active scheduler's EWMA."""

    scheduler = _CURRENT_MODEL_SCHEDULER.get()
    if scheduler is not None:
        scheduler.record_endpoint_attempt(
            endpoint,
            duration_ms=duration_ms,
            success=success,
        )


class FairAsyncLimiter:
    """Cancellation-safe semaphore with priority and per-session round robin."""

    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self._active = 0
        self._lock = asyncio.Lock()
        self._queues: dict[
            int,
            OrderedDict[str, deque[asyncio.Future[bool]]],
        ] = {}

    @property
    def active_count(self) -> int:
        return self._active

    async def acquire(
        self,
        *,
        session_id: str,
        priority: int,
        timeout_seconds: float,
    ) -> int:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[bool] = loop.create_future()
        normalized_session = str(session_id or "anonymous")
        async with self._lock:
            sessions = self._queues.setdefault(priority, OrderedDict())
            sessions.setdefault(normalized_session, deque()).append(waiter)
            self._dispatch_locked()
        try:
            await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout_seconds)
        except BaseException:
            async with self._lock:
                granted = waiter.done() and not waiter.cancelled() and waiter.result()
                if granted:
                    self._active = max(0, self._active - 1)
                else:
                    self._remove_waiter_locked(waiter)
                self._dispatch_locked()
            raise
        return self._active

    async def release(self) -> None:
        async with self._lock:
            if self._active <= 0:
                raise RuntimeError("FairAsyncLimiter released without an active slot.")
            self._active -= 1
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        while self._active < self.capacity:
            waiter = self._next_waiter_locked()
            if waiter is None:
                return
            self._active += 1
            waiter.set_result(True)

    def _next_waiter_locked(self) -> asyncio.Future[bool] | None:
        for priority in sorted(self._queues):
            sessions = self._queues[priority]
            while sessions:
                session_id, waiters = sessions.popitem(last=False)
                while waiters and waiters[0].done():
                    waiters.popleft()
                if not waiters:
                    continue
                waiter = waiters.popleft()
                if waiters:
                    sessions[session_id] = waiters
                if not sessions:
                    self._queues.pop(priority, None)
                return waiter
            self._queues.pop(priority, None)
        return None

    def _remove_waiter_locked(self, target: asyncio.Future[bool]) -> None:
        for priority, sessions in list(self._queues.items()):
            for session_id, waiters in list(sessions.items()):
                filtered = deque(waiter for waiter in waiters if waiter is not target)
                if filtered:
                    sessions[session_id] = filtered
                else:
                    sessions.pop(session_id, None)
            if not sessions:
                self._queues.pop(priority, None)


class ModelScheduler:
    """Model Farm admission control keyed by endpoint/model.

    A zero total cap preserves unbounded remote admission. Coach calls retain
    their category cap, and enabled total caps share the same fair ordering.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._coach_limiters: dict[tuple[str, str], FairAsyncLimiter] = {}
        self._global_limiters: dict[tuple[str, str], FairAsyncLimiter] = {}
        self._endpoint_states: dict[str, _EndpointState] = {}
        self._endpoint_lock = threading.Lock()

    @asynccontextmanager
    async def slot(
        self,
        *,
        session_id: str,
        category: str,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[ModelQueueSlot]:
        category_name = category if category in _PRIORITIES else "background"
        endpoint_name = self._reserve_endpoint(endpoint or "")
        model_name = model or ""
        limiters = tuple(
            limiter
            for limiter in (
                self._limiter(category_name, endpoint_name, model_name),
                self._global_limiter(endpoint_name, model_name),
            )
            if limiter is not None
        )
        started = time.perf_counter()
        timeout_seconds = float(self.settings.model_queue_timeout_seconds)
        deadline = started + timeout_seconds
        active_count = 0
        acquired: list[FairAsyncLimiter] = []
        endpoint_token: Token[str | None] = _CURRENT_MODEL_ENDPOINT.set(
            endpoint_name or None
        )
        scheduler_token: Token[ModelScheduler | None] = _CURRENT_MODEL_SCHEDULER.set(
            self
        )
        try:
            if limiters:
                for limiter in limiters:
                    remaining = max(0.001, deadline - time.perf_counter())
                    active_count = await limiter.acquire(
                        session_id=session_id,
                        priority=_PRIORITIES[category_name],
                        timeout_seconds=remaining,
                    )
                    acquired.append(limiter)
            queue_ms = (time.perf_counter() - started) * 1000
            log_metric(
                "model.queue",
                session_id=session_id,
                model_category=category_name,
                model=model,
                model_endpoint=endpoint_name,
                model_queue_ms=round(queue_ms, 2),
                model_active_count=active_count,
            )
            yield ModelQueueSlot(
                queue_ms=queue_ms,
                active_count=active_count,
                endpoint=endpoint_name,
            )
        except BaseException as exc:
            if isinstance(exc, TimeoutError):
                log_metric(
                    "model.queue.timeout",
                    session_id=session_id,
                    model_category=category_name,
                    model=model,
                    model_endpoint=endpoint_name,
                    model_queue_ms=round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                )
            raise
        finally:
            if acquired:
                for acquired_limiter in reversed(acquired):
                    await acquired_limiter.release()
            _CURRENT_MODEL_SCHEDULER.reset(scheduler_token)
            _CURRENT_MODEL_ENDPOINT.reset(endpoint_token)
            self._release_endpoint(endpoint_name)

    def _reserve_endpoint(self, requested: str) -> str:
        configured = tuple(getattr(self.settings, "chat_urls", ()) or ())
        if requested and requested not in configured:
            configured = (requested,)
        elif requested:
            configured = tuple(dict.fromkeys((requested, *configured)))
        if not configured:
            return requested
        with self._endpoint_lock:
            candidates = [
                (endpoint, self._endpoint_states.setdefault(endpoint, _EndpointState()))
                for endpoint in configured
            ]
            endpoint, state = min(
                candidates,
                key=lambda item: (
                    item[1].active
                    + (item[1].ewma_latency_ms / 10_000.0)
                    + (item[1].error_rate * 4.0),
                    item[1].attempts,
                    configured.index(item[0]),
                ),
            )
            state.active += 1
            return endpoint

    def _release_endpoint(self, endpoint: str) -> None:
        if not endpoint:
            return
        with self._endpoint_lock:
            state = self._endpoint_states.get(endpoint)
            if state is not None:
                state.active = max(0, state.active - 1)

    def record_endpoint_attempt(
        self,
        endpoint: str,
        *,
        duration_ms: float,
        success: bool,
    ) -> None:
        if not endpoint:
            return
        alpha = 0.2
        with self._endpoint_lock:
            state = self._endpoint_states.setdefault(endpoint, _EndpointState())
            state.attempts += 1
            latency = max(0.0, float(duration_ms))
            state.ewma_latency_ms = (
                latency
                if state.attempts == 1
                else ((1.0 - alpha) * state.ewma_latency_ms) + (alpha * latency)
            )
            sample_error = 0.0 if success else 1.0
            state.error_rate = (
                sample_error
                if state.attempts == 1
                else ((1.0 - alpha) * state.error_rate) + (alpha * sample_error)
            )
            active = state.active
            ewma_latency_ms = state.ewma_latency_ms
            error_rate = state.error_rate
        log_metric(
            "model.endpoint",
            model_endpoint=endpoint,
            model_endpoint_active=active,
            model_endpoint_ewma_ms=round(ewma_latency_ms, 2),
            model_endpoint_error_rate=round(error_rate, 4),
            outcome="success" if success else "error",
        )

    def _limiter(
        self,
        category: str,
        endpoint: str,
        model: str,
    ) -> FairAsyncLimiter | None:
        if category not in {"coach", "coach_evidence"}:
            return None
        key = (endpoint, model)
        limiter = self._coach_limiters.get(key)
        if limiter is None:
            limiter = FairAsyncLimiter(self.settings.coach_model_max_concurrency)
            self._coach_limiters[key] = limiter
        return limiter

    def _global_limiter(
        self,
        endpoint: str,
        model: str,
    ) -> FairAsyncLimiter | None:
        capacity = max(0, int(getattr(self.settings, "model_total_max_concurrency", 0)))
        if capacity == 0:
            return None
        key = (endpoint, model)
        limiter = self._global_limiters.get(key)
        if limiter is None:
            limiter = FairAsyncLimiter(capacity)
            self._global_limiters[key] = limiter
        return limiter
