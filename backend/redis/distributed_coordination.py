from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import logging
import threading
import time
from typing import AsyncIterator
import uuid

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import log_metric


logger = logging.getLogger(__name__)


_ACQUIRE_SCRIPT = """
local lease_key = KEYS[1]
local queue_key = KEYS[2]
local heartbeat_key = KEYS[3]
local sequence_key = KEYS[4]
local token = ARGV[1]
local now_ms = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local lease_ttl_ms = tonumber(ARGV[4])
local queue_stale_ms = tonumber(ARGV[5])
local priority = tonumber(ARGV[6])

redis.call('ZREMRANGEBYSCORE', lease_key, '-inf', now_ms)
local stale = redis.call(
    'ZRANGEBYSCORE', heartbeat_key, '-inf', now_ms - queue_stale_ms
)
for _, member in ipairs(stale) do
    redis.call('ZREM', queue_key, member)
    redis.call('ZREM', heartbeat_key, member)
end

if not redis.call('ZSCORE', queue_key, token) then
    local sequence = redis.call('INCR', sequence_key)
    redis.call('ZADD', queue_key, priority * 1000000000000 + sequence, token)
end
redis.call('ZADD', heartbeat_key, now_ms, token)
local active = redis.call('ZCARD', lease_key)
local rank = redis.call('ZRANK', queue_key, token)
local available = capacity - active
if rank and available > 0 and rank < available then
    redis.call('ZREM', queue_key, token)
    redis.call('ZREM', heartbeat_key, token)
    redis.call('ZADD', lease_key, now_ms + lease_ttl_ms, token)
    local key_ttl = math.ceil((queue_stale_ms + lease_ttl_ms) / 1000)
    redis.call('EXPIRE', lease_key, key_ttl)
    redis.call('EXPIRE', queue_key, key_ttl)
    redis.call('EXPIRE', heartbeat_key, key_ttl)
    redis.call('EXPIRE', sequence_key, key_ttl)
    return {1, active + 1, redis.call('ZCARD', queue_key)}
end
return {0, active, redis.call('ZCARD', queue_key)}
"""


_HEARTBEAT_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
    redis.call('ZADD', KEYS[1], 'XX', tonumber(ARGV[2]), ARGV[1])
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
    return 1
end
return 0
"""


_RELEASE_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""


@dataclass(frozen=True, slots=True)
class DistributedLeaseSlot:
    wait_ms: float
    active_count: int
    queue_depth: int
    distributed: bool
    priority: int = 0


class AsyncKeyedLockPool:
    """Loop-aware local locks that are removed when the final waiter leaves."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        loop_key = (id(asyncio.get_running_loop()), str(key))
        with self._guard:
            lock, references = self._locks.get(
                loop_key,
                (asyncio.Lock(), 0),
            )
            self._locks[loop_key] = (lock, references + 1)
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()
            with self._guard:
                current = self._locks.get(loop_key)
                if current is None:
                    return
                current_lock, references = current
                if references <= 1:
                    self._locks.pop(loop_key, None)
                else:
                    self._locks[loop_key] = (current_lock, references - 1)

    @property
    def entry_count(self) -> int:
        with self._guard:
            return len(self._locks)


class RedisDistributedCoordinator:
    """Cross-process FIFO leases with local fallback during Redis outages."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: AsyncRedis | None = None
        self._client_lock = asyncio.Lock()
        self._local_locks = AsyncKeyedLockPool()
        self._redis_warning_emitted = False

    @asynccontextmanager
    async def lease(
        self,
        resource: str,
        *,
        capacity: int,
        timeout_seconds: float = 0.0,
        priority: int = 0,
    ) -> AsyncIterator[DistributedLeaseSlot]:
        if capacity < 1:
            raise ValueError("Distributed lease capacity must be positive.")
        started = time.perf_counter()
        token = uuid.uuid4().hex
        acquired = False
        heartbeat_task: asyncio.Task[None] | None = None
        distributed = False
        active_count = 0
        queue_depth = 0
        keys = self._keys(resource)
        normalized_priority = max(0, int(priority))
        try:
            if self.settings.distributed_coordination_enabled:
                acquired_result = await self._acquire(
                    keys,
                    token=token,
                    capacity=capacity,
                    timeout_seconds=timeout_seconds,
                    priority=normalized_priority,
                )
                if acquired_result is not None:
                    acquired, active_count, queue_depth = acquired_result
                    distributed = acquired
                    if acquired:
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat(keys[0], token),
                            name=f"redis-lease-heartbeat-{token[:8]}",
                        )
            wait_ms = (time.perf_counter() - started) * 1000
            log_metric(
                "coordination.lease",
                coordination_resource=resource,
                coordination_wait_ms=round(wait_ms, 2),
                coordination_active_count=active_count,
                coordination_queue_depth=queue_depth,
                coordination_distributed=distributed,
                coordination_priority=normalized_priority,
            )
            yield DistributedLeaseSlot(
                wait_ms=wait_ms,
                active_count=active_count,
                queue_depth=queue_depth,
                distributed=distributed,
                priority=normalized_priority,
            )
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if acquired:
                await self._release(keys, token)

    @asynccontextmanager
    async def session_lock(
        self,
        namespace: str,
        session_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        local_key = f"{namespace}:{digest}"
        timeout = (
            self.settings.distributed_session_lock_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        async with self._local_locks.lock(local_key):
            async with self.lease(
                f"session:{local_key}",
                capacity=1,
                timeout_seconds=timeout,
            ):
                yield

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    @property
    def local_lock_count(self) -> int:
        return self._local_locks.entry_count

    async def _acquire(
        self,
        keys: tuple[str, str, str, str],
        *,
        token: str,
        capacity: int,
        timeout_seconds: float,
        priority: int,
    ) -> tuple[bool, int, int] | None:
        client = await self._redis()
        if client is None:
            return None
        started = time.monotonic()
        ttl_ms = int(self.settings.distributed_lease_ttl_seconds * 1000)
        stale_ms = max(ttl_ms * 2, 60_000)
        poll_seconds = self.settings.distributed_lease_poll_interval_ms / 1000
        try:
            while True:
                now_ms = int(time.time() * 1000)
                result = await client.eval(
                    _ACQUIRE_SCRIPT,
                    4,
                    *keys,
                    token,
                    now_ms,
                    int(capacity),
                    ttl_ms,
                    stale_ms,
                    priority,
                )
                acquired = bool(int(result[0]))
                active_count = int(result[1])
                queue_depth = int(result[2])
                if acquired:
                    return True, active_count, queue_depth
                if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
                    await self._release(keys, token)
                    raise TimeoutError(
                        f"Timed out waiting for distributed lease {keys[0]!r}."
                    )
                await asyncio.sleep(poll_seconds)
        except asyncio.CancelledError:
            await self._release(keys, token)
            raise
        except RedisError as exc:
            await self._mark_redis_unavailable(exc)
            return None

    async def _heartbeat(self, lease_key: str, token: str) -> None:
        interval = max(1.0, self.settings.distributed_lease_ttl_seconds / 3)
        key_ttl = max(10, int(self.settings.distributed_lease_ttl_seconds * 3))
        while True:
            await asyncio.sleep(interval)
            client = await self._redis()
            if client is None:
                return
            try:
                renewed = await client.eval(
                    _HEARTBEAT_SCRIPT,
                    1,
                    lease_key,
                    token,
                    int(time.time() * 1000)
                    + int(self.settings.distributed_lease_ttl_seconds * 1000),
                    key_ttl,
                )
                if not renewed:
                    return
            except RedisError as exc:
                await self._mark_redis_unavailable(exc)
                return

    async def _release(self, keys: tuple[str, str, str, str], token: str) -> None:
        client = await self._redis()
        if client is None:
            return
        try:
            await client.eval(_RELEASE_SCRIPT, 4, *keys, token)
        except RedisError as exc:
            await self._mark_redis_unavailable(exc)

    async def _redis(self) -> AsyncRedis | None:
        if not self.settings.distributed_coordination_enabled:
            return None
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = AsyncRedis.from_url(
                    self.settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                    socket_timeout=self.settings.redis_socket_timeout_seconds,
                    max_connections=self.settings.effective_redis_max_connections,
                )
            return self._client

    async def _mark_redis_unavailable(self, exc: RedisError) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except RedisError:
                pass
        if not self._redis_warning_emitted:
            logger.warning(
                "Distributed coordination unavailable; using process-local limits: %s",
                exc,
            )
            self._redis_warning_emitted = True
        log_metric(
            "coordination.redis_unavailable",
            error_type=type(exc).__name__,
        )

    def _keys(self, resource: str) -> tuple[str, str, str, str]:
        safe = "".join(
            character if character.isalnum() or character in "-_:" else "_"
            for character in str(resource)
        )
        prefix = (
            self.settings.distributed_coordination_key_prefix.strip()
            or "hr_agent:coordination"
        )
        base = f"{prefix}:{safe}"
        return (
            f"{base}:leases",
            f"{base}:queue",
            f"{base}:heartbeat",
            f"{base}:sequence",
        )


_coordinator_lock = threading.Lock()
_coordinators: dict[tuple[str, str, bool], RedisDistributedCoordinator] = {}


def get_distributed_coordinator(
    settings: Settings | None = None,
) -> RedisDistributedCoordinator:
    resolved = settings or get_settings()
    key = (
        str(getattr(resolved, "redis_url", "")),
        str(
            getattr(
                resolved,
                "distributed_coordination_key_prefix",
                "hr_agent:coordination",
            )
        ),
        bool(getattr(resolved, "distributed_coordination_enabled", False)),
    )
    with _coordinator_lock:
        coordinator = _coordinators.get(key)
        if coordinator is None:
            coordinator = RedisDistributedCoordinator(resolved)
            _coordinators[key] = coordinator
        return coordinator
