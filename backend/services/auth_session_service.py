from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from backend.config.settings import get_settings
from backend.schemas.auth import AuthUser, AuthUserResponse


class AuthSessionError(RuntimeError):
    pass


class MaxActiveSessionsReached(AuthSessionError):
    pass


@dataclass(frozen=True)
class AuthSession:
    session_id: str
    user: AuthUserResponse
    user_id: str
    role: str
    created_at: float
    last_seen_at: float


class AuthSessionService:
    active_sessions_key = "hragent:auth:active_sessions"
    session_key_prefix = "hragent:auth:session:"

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
        self._redis: Redis | None = None

    def create_session(self, user: AuthUser) -> str:
        client = self._client()
        if client is None:
            raise AuthSessionError("REDIS_UNAVAILABLE")
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        payload = {
            "user_id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "auth_provider": user.auth_provider,
            "created_at": now,
            "last_seen_at": now,
        }
        if not self._atomic_create_session(client, session_id, now, payload):
            raise MaxActiveSessionsReached("MAX_ACTIVE_SESSIONS_REACHED")
        return session_id

    def _atomic_create_session(self, client: Redis, session_id: str, now: float, payload: dict) -> bool:
        cutoff = now - self.settings.auth_session_idle_timeout_seconds
        script = """
        redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[4])
        local max_sessions = tonumber(ARGV[1])
        if max_sessions > 0 and tonumber(redis.call('ZCARD', KEYS[1])) >= max_sessions then
            return 0
        end
        redis.call('SETEX', KEYS[2], tonumber(ARGV[2]), ARGV[3])
        redis.call('ZADD', KEYS[1], ARGV[5], ARGV[6])
        return 1
        """
        try:
            result = client.eval(
                script,
                2,
                self.active_sessions_key,
                self._key(session_id),
                str(self.settings.auth_max_active_sessions),
                str(self.settings.auth_session_idle_timeout_seconds),
                json.dumps(payload, ensure_ascii=False),
                str(cutoff),
                str(now),
                session_id,
            )
        except RedisError as exc:
            raise AuthSessionError("REDIS_UNAVAILABLE") from exc
        return int(result or 0) == 1

    def get_session(self, session_id: str | None, *, refresh: bool = True) -> AuthSession | None:
        if not session_id:
            return None
        client = self._client()
        if client is None:
            return None
        try:
            raw = client.get(self._key(session_id))
        except RedisError:
            return None
        if not raw:
            self.delete_session(session_id)
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            self.delete_session(session_id)
            return None

        now = time.time()
        created_at = float(payload.get("created_at") or now)
        last_seen_at = float(payload.get("last_seen_at") or created_at)
        if now - created_at > self.settings.auth_session_absolute_timeout_seconds:
            self.delete_session(session_id)
            return None
        if now - last_seen_at > self.settings.auth_session_idle_timeout_seconds:
            self.delete_session(session_id)
            return None

        if refresh:
            payload["last_seen_at"] = now
            try:
                client.setex(
                    self._key(session_id),
                    self.settings.auth_session_idle_timeout_seconds,
                    json.dumps(payload, ensure_ascii=False),
                )
                client.zadd(self.active_sessions_key, {session_id: now})
            except RedisError:
                return None

        user = AuthUserResponse(
            id=str(payload.get("user_id") or "") or None,
            email=str(payload.get("email") or ""),
            display_name=payload.get("display_name"),
            role=str(payload.get("role") or "user"),
        )
        return AuthSession(
            session_id=session_id,
            user=user,
            user_id=str(payload.get("user_id") or ""),
            role=user.role,
            created_at=created_at,
            last_seen_at=last_seen_at,
        )

    def delete_session(self, session_id: str) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.delete(self._key(session_id))
            client.zrem(self.active_sessions_key, session_id)
        except RedisError:
            return

    def delete_user_sessions(self, user_id: str) -> None:
        client = self._client()
        if client is None:
            return
        try:
            session_ids: list[str] = []
            for key in client.scan_iter(match=f"{self.session_key_prefix}*", count=100):
                raw = client.get(key)
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if str(payload.get("user_id") or "") == user_id:
                    session_ids.append(str(key).removeprefix(self.session_key_prefix))
            if not session_ids:
                return
            pipeline = client.pipeline()
            for session_id in session_ids:
                pipeline.delete(self._key(session_id))
                pipeline.zrem(self.active_sessions_key, session_id)
            pipeline.execute()
        except RedisError:
            return

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

    @classmethod
    def _key(cls, session_id: str) -> str:
        return f"{cls.session_key_prefix}{session_id}"
