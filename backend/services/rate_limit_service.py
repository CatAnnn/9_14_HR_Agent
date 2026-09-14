from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from backend.config.settings import get_settings


@dataclass(frozen=True)
class RateLimitRule:
    limit: int
    window_seconds: int


class RateLimitExceeded(RuntimeError):
    pass


class RateLimitService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis: Redis | None = None

    def check_login(self, *, email: str, ip_address: str) -> None:
        self._check(
            f"hragent:rate:login:email:{self._digest(email)}",
            self._parse_rule(self.settings.auth_login_rate_limit_per_email, 5, 60),
        )
        self._check(
            f"hragent:rate:login:ip:{self._digest(ip_address)}",
            self._parse_rule(self.settings.auth_login_rate_limit_per_ip, 20, 60),
        )

    def check_register(self, *, ip_address: str) -> None:
        self._check(
            f"hragent:rate:register:ip:{self._digest(ip_address)}",
            self._parse_rule(self.settings.auth_register_rate_limit_per_ip, 3, 3600),
        )

    def _client(self) -> Redis | None:
        if not self.settings.redis_url:
            return None
        if self._redis is None:
            self._redis = Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                max_connections=self.settings.effective_redis_max_connections,
                decode_responses=True,
            )
        return self._redis

    def _check(self, key: str, rule: RateLimitRule) -> None:
        client = self._client()
        if client is None:
            return
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, rule.window_seconds)
            if count > rule.limit:
                raise RateLimitExceeded("RATE_LIMITED")
        except RedisError:
            return

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _parse_rule(value: str, default_limit: int, default_window: int) -> RateLimitRule:
        match = re.match(r"^\s*(\d+)\s*/\s*(minute|hour|second|m|h|s)\s*$", value or "")
        if not match:
            return RateLimitRule(default_limit, default_window)
        limit = max(1, int(match.group(1)))
        unit = match.group(2)
        window = 3600 if unit in {"hour", "h"} else 60 if unit in {"minute", "m"} else 1
        return RateLimitRule(limit, window)
