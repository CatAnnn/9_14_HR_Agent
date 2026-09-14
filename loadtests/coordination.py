from __future__ import annotations

import json
import socket
import time
from typing import Any, Iterable

from loadtests.accounts import LoadTestAccount


_CLAIM_ACCOUNT_LUA = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1])
for _, account in ipairs(expired) do
  redis.call('ZREM', KEYS[2], account)
  redis.call('RPUSH', KEYS[1], account)
end
local account = redis.call('LPOP', KEYS[1])
if not account then return nil end
redis.call('ZADD', KEYS[2], ARGV[2], account)
return account
"""

_RELEASE_ACCOUNT_LUA = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 then
  redis.call('RPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""

_MARK_STARTED_LUA = """
redis.call('HINCRBY', KEYS[1], 'started', 1)
local active = redis.call('HINCRBY', KEYS[1], 'active', 1)
local maximum = tonumber(redis.call('HGET', KEYS[1], 'max_active') or '0')
if active > maximum then
  redis.call('HSET', KEYS[1], 'max_active', active)
end
return active
"""

_RECORD_WORKER_COUNT_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local candidate = tonumber(ARGV[1])
if candidate > current then
  redis.call('SET', KEYS[1], candidate, 'EX', ARGV[2])
  return candidate
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return current
"""


class RunCoordinator:
    def __init__(self, redis_client: Any, run_id: str, *, lease_seconds: int = 900) -> None:
        if not run_id.strip():
            raise ValueError("run_id is required")
        self.redis = redis_client
        self.run_id = run_id.strip()
        self.lease_seconds = max(60, int(lease_seconds))
        self.prefix = f"hr_agent:loadtest:{self.run_id}"

    @property
    def available_key(self) -> str:
        return f"{self.prefix}:accounts:available"

    @property
    def leased_key(self) -> str:
        return f"{self.prefix}:accounts:leased"

    def seed_accounts(self, accounts: Iterable[LoadTestAccount]) -> bool:
        marker = f"{self.prefix}:accounts:seeded"
        ready = f"{self.prefix}:accounts:ready"
        if not self.redis.set(marker, "initializing", nx=True, ex=86_400):
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if self.redis.exists(ready):
                    return False
                time.sleep(0.05)
            raise RuntimeError("timed out waiting for load-test account pool initialization")
        serialized = [account.to_json() for account in accounts]
        if not serialized:
            self.redis.delete(marker, ready)
            raise ValueError("cannot seed an empty account pool")
        try:
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(self.available_key, self.leased_key)
            pipeline.rpush(self.available_key, *serialized)
            pipeline.set(ready, "1", ex=86_400)
            pipeline.set(marker, "ready", ex=86_400)
            pipeline.execute()
            return True
        except BaseException:
            self.redis.delete(marker, ready)
            raise

    def claim_account(self) -> LoadTestAccount:
        now = time.time()
        value = self.redis.eval(
            _CLAIM_ACCOUNT_LUA,
            2,
            self.available_key,
            self.leased_key,
            now,
            now + self.lease_seconds,
        )
        if value is None:
            raise RuntimeError("load-test account pool is exhausted")
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        return LoadTestAccount.from_json(str(value))

    def heartbeat_account(self, account: LoadTestAccount) -> None:
        self.redis.zadd(
            self.leased_key,
            {account.to_json(): time.time() + self.lease_seconds},
            xx=True,
        )

    def release_account(self, account: LoadTestAccount) -> None:
        self.redis.eval(
            _RELEASE_ACCOUNT_LUA,
            2,
            self.leased_key,
            self.available_key,
            account.to_json(),
        )

    def mark_attempted(self) -> None:
        self.redis.hincrby(f"{self.prefix}:progress", "attempted", 1)

    def mark_started(self, *, account: LoadTestAccount, session_id: str) -> None:
        if not self.redis.set(
            f"{self.prefix}:session:{session_id}",
            account.email,
            nx=True,
            ex=86_400,
        ):
            raise RuntimeError(f"duplicate workflow session detected: {session_id}")
        self.redis.eval(
            _MARK_STARTED_LUA,
            1,
            f"{self.prefix}:progress",
        )

    def record_worker_count(self, worker_count: int) -> None:
        if worker_count < 0:
            raise ValueError("worker_count cannot be negative")
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.set(f"{self.prefix}:worker_count", int(worker_count), ex=86_400)
        pipeline.execute_command(
            "EVAL",
            _RECORD_WORKER_COUNT_LUA,
            1,
            f"{self.prefix}:peak_worker_count",
            int(worker_count),
            86_400,
        )
        pipeline.execute()

    def peak_worker_count(self) -> int:
        value = self.redis.get(f"{self.prefix}:peak_worker_count")
        return int(value or 0)

    def mark_completed(self) -> None:
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.hincrby(f"{self.prefix}:progress", "completed", 1)
        pipeline.hincrby(f"{self.prefix}:progress", "active", -1)
        pipeline.execute()

    def mark_failed(self, failure_type: str, *, decrement_active: bool = True) -> None:
        safe_type = "".join(ch for ch in failure_type.lower() if ch.isalnum() or ch in "_-")[:80]
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.hincrby(f"{self.prefix}:progress", "failed", 1)
        if decrement_active:
            pipeline.hincrby(f"{self.prefix}:progress", "active", -1)
        pipeline.hincrby(f"{self.prefix}:failures", safe_type or "unknown", 1)
        pipeline.execute()

    def record_failure_sample(
        self,
        failure_type: str,
        *,
        account_index: int,
        message: str,
    ) -> None:
        payload = json.dumps(
            {
                "type": failure_type[:80],
                "account_index": int(account_index),
                "message": " ".join(message.split())[:500],
                "recorded_at": time.time(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        key = f"{self.prefix}:failure_samples"
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.rpush(key, payload)
        pipeline.ltrim(key, -200, -1)
        pipeline.expire(key, 86_400)
        pipeline.execute()

    def failure_samples(self) -> list[dict[str, object]]:
        values = self.redis.lrange(f"{self.prefix}:failure_samples", 0, -1)
        return [
            json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
            for value in values
        ]

    def progress(self) -> dict[str, int]:
        raw = self.redis.hgetall(f"{self.prefix}:progress")
        return {
            (key.decode() if isinstance(key, bytes) else str(key)): int(value)
            for key, value in raw.items()
        }

    def set_draining(self) -> None:
        self.redis.set(f"{self.prefix}:draining", "1", ex=86_400)

    def is_draining(self) -> bool:
        return bool(self.redis.exists(f"{self.prefix}:draining"))

    def record_worker_sample(self, *, cpu_percent: float, process_rss_bytes: int) -> None:
        payload = json.dumps(
            {
                "worker": socket.gethostname(),
                "cpu_percent": round(float(cpu_percent), 3),
                "process_rss_bytes": int(process_rss_bytes),
                "sampled_at": time.time(),
            },
            sort_keys=True,
        )
        key = f"{self.prefix}:worker_samples"
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.rpush(key, payload)
        pipeline.ltrim(key, -20_000, -1)
        pipeline.expire(key, 86_400)
        pipeline.execute()

    def worker_samples(self) -> list[dict[str, object]]:
        values = self.redis.lrange(f"{self.prefix}:worker_samples", 0, -1)
        return [
            json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
            for value in values
        ]

    def failure_counts(self) -> dict[str, int]:
        raw = self.redis.hgetall(f"{self.prefix}:failures")
        return {
            (key.decode() if isinstance(key, bytes) else str(key)): int(value)
            for key, value in raw.items()
        }
