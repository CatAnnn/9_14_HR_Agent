from __future__ import annotations

import time
from dataclasses import dataclass

from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import LLMError
from backend.services.http_client import get_shared_async_client, get_shared_sync_client


@dataclass
class CachedToken:
    access_token: str
    expires_at: float


_TOKEN_CACHE: CachedToken | None = None


class ModelAPIAuth:
    """Build request headers for Bosch / OpenAI-compatible model APIs.

    Supported modes:
    - bearer: Authorization: Bearer <api_key>
    - api_key: configurable header, e.g. aigc-llm-api-key: <api_key>
    - client_credentials: OAuth2 client credential token, then Authorization: Bearer <access_token>
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def sync_headers(self, api_key: str | None = None) -> dict[str, str]:
        token_or_key = self._sync_token_or_key(api_key)
        return self._headers_from_token_or_key(token_or_key)

    async def async_headers(self, api_key: str | None = None) -> dict[str, str]:
        token_or_key = await self._async_token_or_key(api_key)
        return self._headers_from_token_or_key(token_or_key)

    def _headers_from_token_or_key(self, token_or_key: str) -> dict[str, str]:
        mode = self.settings.model_api_auth_mode
        header_name = (self.settings.model_api_key_header_name or "Authorization").strip()
        if mode == "api_key" and header_name.lower() != "authorization":
            return {header_name: token_or_key}
        return {"Authorization": f"Bearer {token_or_key}"}

    def _sync_token_or_key(self, api_key: str | None) -> str:
        if self.settings.model_api_auth_mode != "client_credentials":
            key = (api_key or "").strip()
            if not key:
                raise LLMError("Model API key is not configured.")
            return key
        return self._get_cached_token_sync()

    async def _async_token_or_key(self, api_key: str | None) -> str:
        if self.settings.model_api_auth_mode != "client_credentials":
            key = (api_key or "").strip()
            if not key:
                raise LLMError("Model API key is not configured.")
            return key
        return await self._get_cached_token_async()

    def _get_cached_token_sync(self) -> str:
        global _TOKEN_CACHE
        if _TOKEN_CACHE and _TOKEN_CACHE.expires_at > time.time() + 60:
            return _TOKEN_CACHE.access_token
        token = self._request_token_sync()
        _TOKEN_CACHE = token
        return token.access_token

    async def _get_cached_token_async(self) -> str:
        global _TOKEN_CACHE
        if _TOKEN_CACHE and _TOKEN_CACHE.expires_at > time.time() + 60:
            return _TOKEN_CACHE.access_token
        token = await self._request_token_async()
        _TOKEN_CACHE = token
        return token.access_token

    def _token_payload(self) -> dict[str, str]:
        s = self.settings
        if not s.oauth2_token_url or not s.oauth2_client_id or not s.oauth2_client_secret:
            raise LLMError("OAuth2 client credential settings are incomplete.")
        payload = {
            "grant_type": "client_credentials",
            "client_id": s.oauth2_client_id,
            "client_secret": s.oauth2_client_secret,
        }
        if s.oauth2_scope:
            payload["scope"] = s.oauth2_scope
        return payload

    def _request_token_sync(self) -> CachedToken:
        s = self.settings
        resp = get_shared_sync_client("model_farm").post(
            s.oauth2_token_url,
            data=self._token_payload(),
            timeout=s.llm_timeout_seconds,
        )
        resp.raise_for_status()
        return self._parse_token_response(resp.json())

    async def _request_token_async(self) -> CachedToken:
        s = self.settings
        client = get_shared_async_client()
        resp = await client.post(
            s.oauth2_token_url,
            data=self._token_payload(),
            timeout=s.llm_timeout_seconds,
        )
        resp.raise_for_status()
        return self._parse_token_response(resp.json())

    @staticmethod
    def _parse_token_response(data: dict) -> CachedToken:
        token = data.get("access_token")
        if not token:
            raise LLMError("OAuth2 token response does not contain access_token.")
        expires_in = int(data.get("expires_in") or 3600)
        return CachedToken(access_token=token, expires_at=time.time() + max(60, expires_in))
