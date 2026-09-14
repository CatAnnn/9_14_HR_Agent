from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from array import array
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any

import httpx

from backend.config.settings import Settings
from backend.observability.metrics import (
    record_asr_duration,
    record_asr_preview_window,
    record_asr_silence_skip,
)
from backend.services.local_model_runtime import (
    LocalModelRuntimeRecycler,
    get_local_model_runtime_recycler,
)
from backend.services.http_client import get_shared_async_client

SendFrontend = Callable[[dict[str, Any]], Awaitable[None]]
_READINESS_CACHE_SECONDS = 0.5
_readiness_lock = threading.Lock()
_readiness_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_readiness_inflight: dict[tuple[Any, ...], asyncio.Task[dict[str, Any]]] = {}


class AsrConfigurationError(RuntimeError):
    """Raised when realtime ASR cannot be started with current settings."""


class AsrPreviewCapacityError(AsrConfigurationError):
    """Raised when all local preview slots are occupied."""


_MAX_LOCAL_ASR_BATCH_SIZE = 11


def _local_streaming_urls(settings: Settings) -> tuple[str, ...]:
    configured_property = getattr(settings, "asr_local_streaming_endpoint_urls", None)
    if configured_property is not None:
        urls = [
            str(item).strip().rstrip("/")
            for item in configured_property
            if str(item).strip()
        ]
    else:
        configured_urls = str(
            getattr(settings, "asr_local_streaming_urls", "") or ""
        )
        urls = [
            item.strip().rstrip("/")
            for item in configured_urls.replace("\n", ",").split(",")
            if item.strip()
        ]
        primary = str(getattr(settings, "asr_local_streaming_url", "") or "").strip().rstrip("/")
        if primary:
            urls.insert(0, primary)
    endpoints = tuple(dict.fromkeys(urls))
    if len(endpoints) > 2:
        raise AsrConfigurationError(
            "本地 ASR 最多允许配置两个模型实例，更多用户应在现有实例中等待。"
        )
    return endpoints


def _readiness_key(settings: Settings) -> tuple[Any, ...]:
    return (
        id(settings),
        bool(settings.asr_enabled),
        bool(settings.asr_realtime_enabled),
        str(settings.asr_provider_mode),
        _local_streaming_urls(settings),
        str(settings.asr_realtime_model or ""),
        str(settings.asr_http_url or ""),
        str(settings.asr_http_model or ""),
        str(settings.asr_bosch_realtime_url or ""),
        str(settings.asr_bosch_realtime_model or ""),
        int(settings.asr_bosch_global_max_concurrency),
        int(settings.asr_bosch_preview_max_concurrency),
        int(settings.asr_local_preview_max_concurrency),
    )


def clear_asr_readiness_cache() -> None:
    with _readiness_lock:
        _readiness_cache.clear()


def _store_readiness_result(
    key: tuple[Any, ...],
    task: asyncio.Task[dict[str, Any]],
) -> None:
    try:
        result = task.result()
    except BaseException:
        result = None
    with _readiness_lock:
        if _readiness_inflight.get(key) is task:
            _readiness_inflight.pop(key, None)
        if result is not None:
            _readiness_cache[key] = (
                time.monotonic() + _READINESS_CACHE_SECONDS,
                dict(result),
            )


async def get_asr_readiness(
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Read live local-ASR health instead of relying on a startup warmup snapshot."""

    key = _readiness_key(settings)
    now = time.monotonic()
    with _readiness_lock:
        cached = _readiness_cache.get(key)
        if not force_refresh and cached is not None and cached[0] > now:
            return dict(cached[1])
        task = _readiness_inflight.get(key)
        if task is None:
            task = asyncio.create_task(_fetch_asr_readiness(settings))
            _readiness_inflight[key] = task
            task.add_done_callback(
                lambda completed, cache_key=key: _store_readiness_result(
                    cache_key, completed
                )
            )
    return dict(await asyncio.shield(task))


async def _fetch_asr_readiness(settings: Settings) -> dict[str, Any]:
    provider = settings.asr_provider_mode
    bosch_native = provider == "bosch" and bool(
        str(settings.asr_bosch_realtime_url or "").strip()
    )
    if provider == "local":
        model = settings.asr_realtime_model
    elif bosch_native:
        model = settings.asr_bosch_realtime_model
    else:
        model = settings.asr_http_model
    max_batch_size = max(
        1,
        min(_MAX_LOCAL_ASR_BATCH_SIZE, int(settings.asr_local_preview_max_concurrency)),
    )
    bases = _local_streaming_urls(settings)
    base_payload: dict[str, Any] = {
        "status": "degraded",
        "model": model,
        "provider": provider,
        "active_preview_sessions": 0,
        "max_preview_sessions": 0,
        "max_inference_batch_size": max_batch_size,
        "total_inference_capacity": max_batch_size * max(1, len(bases)),
        "model_instances": len(bases) if provider == "local" else 0,
        "ready_model_instances": 0,
        "admission_mode": "unbounded",
        "streaming_mode": "unknown",
        "stream_chunk_seconds": 0.0,
    }
    if not settings.asr_enabled or not settings.asr_realtime_enabled:
        return {**base_payload, "detail": "实时语音预览未启用。"}
    if provider == "browser":
        return {
            **base_payload,
            "status": "ready",
            "model": "browser-web-speech",
            "max_inference_batch_size": 1,
            "total_inference_capacity": 1,
            "model_instances": 0,
            "ready_model_instances": 0,
            "streaming_mode": "browser_native",
            "detail": "使用浏览器内置语音识别。",
        }
    if provider == "bosch":
        http_capacity = int(settings.asr_bosch_global_max_concurrency) or int(
            settings.asr_bosch_preview_max_concurrency
        )
        if (
            not settings.asr_http_url.strip()
            or not settings.asr_http_model.strip()
            or not settings.effective_asr_api_key
        ):
            return {
                **base_payload,
                "detail": "Bosch 语音转写地址、模型或凭据未配置。",
            }
        if bosch_native:
            return {
                **base_payload,
                "status": "ready",
                "max_inference_batch_size": 0,
                "total_inference_capacity": 0,
                "model_instances": 0,
                "ready_model_instances": 0,
                "streaming_mode": "native_websocket",
                "stream_chunk_seconds": 0.1,
                "final_correction_model": settings.asr_http_model,
                "detail": (
                    "使用 Bosch 原生 Qwen WebSocket 实时预览，"
                    "停止后使用完整录音 HTTP 转写校正。"
                ),
            }
        return {
            **base_payload,
            "status": "ready",
            "max_inference_batch_size": http_capacity,
            "total_inference_capacity": http_capacity,
            "model_instances": 0,
            "ready_model_instances": 0,
            "streaming_mode": "cumulative_segment",
            "stream_chunk_seconds": float(settings.asr_bosch_preview_chunk_seconds),
            "first_stream_chunk_seconds": float(
                settings.asr_bosch_first_preview_chunk_seconds
            ),
            "accumulation_max_seconds": float(
                settings.asr_bosch_accumulation_max_seconds
            ),
            "per_session_preview_concurrency": int(
                settings.asr_bosch_preview_per_session_concurrency
            ),
            "detail": "使用 Bosch 累积语义段预览和完整录音校正。",
        }
    if not bases:
        return {**base_payload, "detail": "本地语音服务地址未配置。"}

    async def probe(base: str) -> dict[str, Any]:
        if not base.lower().startswith(("http://", "https://")):
            return {"status": "degraded", "detail": "本地语音服务地址无效。"}
        timeout = httpx.Timeout(2.0, connect=1.0)
        try:
            response = await get_shared_async_client("local_inference").get(
                f"{base}/health",
                timeout=timeout,
            )
            if response.status_code >= 500:
                return {"status": "degraded", "detail": "本地语音模型运行异常。"}
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                return {"status": "degraded", "detail": "本地语音模型健康响应无效。"}
            session_limit = max(0, int(payload.get("max_sessions") or 0))
            inference_batch_size = max(
                1,
                min(
                    _MAX_LOCAL_ASR_BATCH_SIZE,
                    int(payload.get("max_batch_size") or max_batch_size),
                ),
            )
            admission_mode = str(payload.get("admission_mode") or "").strip()
            if admission_mode not in {"bounded", "unbounded"}:
                admission_mode = "bounded" if session_limit else "unbounded"
            return {
                "status": "ready",
                "active_preview_sessions": max(0, int(payload.get("active_sessions") or 0)),
                "max_preview_sessions": session_limit,
                "max_inference_batch_size": inference_batch_size,
                "admission_mode": admission_mode,
                "queue_depth": max(0, int(payload.get("queue_depth") or 0)),
                "window_seconds": float(payload.get("window_seconds") or 0),
                "streaming_mode": str(payload.get("streaming_mode") or "unknown"),
                "stream_chunk_seconds": float(payload.get("stream_chunk_seconds") or 0),
            }
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return {"status": "warming", "detail": "实时语音准备中。"}
        except Exception:
            return {"status": "degraded", "detail": "本地语音模型暂不可用。"}

    results = await asyncio.gather(*(probe(base) for base in bases))
    ready = [result for result in results if result.get("status") == "ready"]
    if not ready:
        status = (
            "warming"
            if any(result.get("status") == "warming" for result in results)
            else "degraded"
        )
        return {
            **base_payload,
            "status": status,
            "detail": "实时语音准备中。" if status == "warming" else "本地语音模型暂不可用。",
        }

    all_unbounded = all(
        result.get("admission_mode") == "unbounded" for result in ready
    )
    status = "ready" if len(ready) == len(bases) else "degraded"
    result = {
        **base_payload,
        "status": status,
        "active_preview_sessions": sum(
            int(item.get("active_preview_sessions") or 0) for item in ready
        ),
        "max_preview_sessions": (
            0
            if all_unbounded
            else sum(int(item.get("max_preview_sessions") or 0) for item in ready)
        ),
        "max_inference_batch_size": max(
            int(item.get("max_inference_batch_size") or max_batch_size)
            for item in ready
        ),
        "total_inference_capacity": sum(
            int(item.get("max_inference_batch_size") or max_batch_size)
            for item in ready
        ),
        "ready_model_instances": len(ready),
        "admission_mode": "unbounded" if all_unbounded else "bounded",
        "queue_depth": sum(
            int(item.get("queue_depth") or 0) for item in ready
        ),
        "window_seconds": max(
            float(item.get("window_seconds") or 0) for item in ready
        ),
        "streaming_mode": str(ready[0].get("streaming_mode") or "unknown"),
        "stream_chunk_seconds": max(
            float(item.get("stream_chunk_seconds") or 0) for item in ready
        ),
    }
    if status != "ready":
        result["detail"] = f"{len(ready)}/{len(bases)} 个实时语音模型已就绪。"
    return result


class RealtimeAsrProxy:
    """Proxy one isolated native Qwen3-ASR stream for preview and final text."""

    authoritative_final = True

    def __init__(
        self,
        settings: Settings,
        runtime_recycler: LocalModelRuntimeRecycler | None = None,
        client: httpx.AsyncClient | None = None,
        routing_key: str = "",
        language: str | None = None,
    ):
        self.settings = settings
        self._routing_key = str(routing_key or "")
        self._language = str(
            language
            if language is not None
            else getattr(settings, "asr_language", "")
        ).strip()
        self._local_streaming_urls = _local_streaming_urls(settings)
        self._local_base_url = ""
        self.runtime_recycler = runtime_recycler or get_local_model_runtime_recycler()
        self._closed = False
        self._preview_connected = False
        self._preview_stopping = False
        self._local_client = client
        self._owns_local_client = False
        self._local_timeout: httpx.Timeout | None = None
        self._local_audio_format = "float32"
        self._local_session_id = ""
        self._local_pcm_buffer = bytearray()
        self._local_last_text = ""
        self._local_last_revision = -1
        self._local_speech_detected = False
        self._finish_payload: dict[str, Any] | None = None
        self._preview_failed = False
        self._audio_dropped = False
        self._local_events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._local_buffer_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None
        self._lag_warning_emitted = False
        self._runtime_lease: AbstractContextManager[None] | None = None
        sample_rate = max(1, int(getattr(settings, "asr_sample_rate", 16_000)))
        chunk_ms = max(100, int(getattr(settings, "asr_local_chunk_ms", 500)))
        stream_window_seconds = max(
            0.1,
            float(getattr(settings, "asr_local_stream_window_seconds", 12.0)),
        )
        self._chunk_bytes = max(2, sample_rate * 2 * chunk_ms // 1000)
        self._max_pending_bytes = max(2, int(sample_rate * 2 * stream_window_seconds))

    @property
    def is_connected(self) -> bool:
        return self._preview_connected and not self._closed

    @property
    def last_text(self) -> str:
        return self._local_last_text

    @property
    def failed(self) -> bool:
        return self._preview_failed or self._audio_dropped

    @property
    def speech_detected(self) -> bool:
        return self._local_speech_detected

    @property
    def _local_streaming_url(self) -> str:
        if self._local_base_url:
            return self._local_base_url
        return self._local_streaming_urls[0] if self._local_streaming_urls else ""

    def _ordered_local_streaming_urls(self) -> tuple[str, ...]:
        urls = self._local_streaming_urls
        if len(urls) < 2:
            return urls
        digest = hashlib.sha256(self._routing_key.encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big") % len(urls)
        return urls[start:] + urls[:start]

    async def _load_ordered_local_streaming_urls(
        self,
        client: Any,
        fallback_order: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Prefer the least-loaded healthy instance; keep hash order as a tie-break."""

        if len(fallback_order) < 2 or not callable(getattr(client, "get", None)):
            return fallback_order
        probe_timeout_seconds = max(
            0.05,
            min(
                2.0,
                float(
                    getattr(
                        self.settings,
                        "asr_local_routing_probe_timeout_seconds",
                        0.25,
                    )
                ),
            ),
        )
        timeout = httpx.Timeout(probe_timeout_seconds)

        async def probe(base: str, hash_rank: int) -> tuple[str, tuple[Any, ...] | None]:
            try:
                response = await client.get(f"{base}/health", timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("status") != "ok":
                    return base, None
                active_sessions = max(0, int(payload.get("active_sessions") or 0))
                max_sessions = max(0, int(payload.get("max_sessions") or 0))
                queue_depth = max(0, int(payload.get("queue_depth") or 0))
                max_batch_size = max(1, int(payload.get("max_batch_size") or 1))
                is_full = max_sessions > 0 and active_sessions >= max_sessions
                session_denominator = max_sessions or max_batch_size
                pressure = (
                    active_sessions / max(1, session_denominator)
                    + queue_depth / max_batch_size
                )
                return base, (is_full, pressure, queue_depth, hash_rank)
            except asyncio.CancelledError:
                raise
            except Exception:
                return base, None

        probed = await asyncio.gather(
            *(probe(base, rank) for rank, base in enumerate(fallback_order))
        )
        healthy = [(base, score) for base, score in probed if score is not None]
        if not healthy:
            return fallback_order
        healthy_not_full = sorted(
            (
                (base, score)
                for base, score in healthy
                if score is not None and not bool(score[0])
            ),
            key=lambda item: item[1],
        )
        unknown = [base for base, score in probed if score is None]
        healthy_full = sorted(
            (
                (base, score)
                for base, score in healthy
                if score is not None and bool(score[0])
            ),
            key=lambda item: item[1],
        )
        return tuple(
            [base for base, _score in healthy_not_full]
            + unknown
            + [base for base, _score in healthy_full]
        )

    def _local_endpoint(self, path: str) -> str:
        if self._owns_local_client:
            return path
        return f"{self._local_streaming_url}{path}"

    def validate(self) -> None:
        if not self.settings.asr_enabled or not self.settings.asr_realtime_enabled:
            raise AsrConfigurationError("实时语音预览当前未启用。")
        if not self._local_streaming_urls:
            raise AsrConfigurationError(
                "ASR_LOCAL_STREAMING_URL 或 ASR_LOCAL_STREAMING_URLS 未配置。"
            )
        if len(self._local_streaming_urls) > 2:
            raise AsrConfigurationError("本地 ASR 最多允许两个模型实例。")
        if any(
            not url.lower().startswith(("http://", "https://"))
            for url in self._local_streaming_urls
        ):
            raise AsrConfigurationError(
                "本地 ASR 服务地址必须是 http:// 或 https:// 地址。"
            )

    async def connect(self) -> None:
        self.validate()
        await self._acquire_runtime_lease()
        try:
            await self._connect_local()
        except BaseException:
            # Capture is owned by the route and continues independently. Once
            # preview admission fails, stop retaining duplicate PCM or
            # emitting late lag-recovery events from this proxy.
            self._preview_failed = True
            self._local_pcm_buffer.clear()
            self._local_buffer_event.clear()
            self._local_events.put_nowait(None)
            self._release_runtime_lease()
            raise
        self._preview_connected = True
        self._worker_task = asyncio.create_task(
            self._preview_worker(),
            name=f"asr-preview-{self._local_session_id[:8]}",
        )

    async def _acquire_runtime_lease(self) -> None:
        if self._runtime_lease is not None:
            return
        lease = self.runtime_recycler.lease("qwen3_asr")
        enter_task = asyncio.create_task(asyncio.to_thread(lease.__enter__))
        try:
            await asyncio.shield(enter_task)
        except asyncio.CancelledError:
            try:
                await enter_task
            except Exception:
                pass
            else:
                lease.__exit__(None, None, None)
            raise
        except Exception as exc:
            raise AsrConfigurationError(
                "无法启动本地 Qwen3-ASR 服务，请稍后重试。"
            ) from exc
        self._runtime_lease = lease

    def _release_runtime_lease(self) -> None:
        lease = self._runtime_lease
        self._runtime_lease = None
        if lease is not None:
            lease.__exit__(None, None, None)

    async def _connect_local(self) -> None:
        configured_request_timeout = float(
            getattr(self.settings, "asr_local_request_timeout_seconds", 0.0)
        )
        request_timeout = (
            None if configured_request_timeout <= 0 else max(1.0, configured_request_timeout)
        )
        connect_timeout = max(
            1.0,
            float(getattr(self.settings, "asr_connect_timeout_seconds", 15.0)),
        )
        timeout = httpx.Timeout(request_timeout, connect=connect_timeout)
        client = self._local_client
        if client is None:
            client = get_shared_async_client("local_inference")
        request_options: dict[str, Any] = {"timeout": timeout}
        if self._language:
            request_options["params"] = {"language": self._language}

        fallback_order = self._ordered_local_streaming_urls()
        admission_timeout_seconds = max(
            0.1,
            min(
                30.0,
                float(
                    getattr(
                        self.settings,
                        "asr_local_admission_timeout_seconds",
                        2.0,
                    )
                ),
            ),
        )
        loop = asyncio.get_running_loop()
        admission_started_at = loop.time()
        admission_deadline = admission_started_at + admission_timeout_seconds
        routing_probe_timeout_seconds = max(
            0.05,
            min(
                2.0,
                float(
                    getattr(
                        self.settings,
                        "asr_local_routing_probe_timeout_seconds",
                        0.25,
                    )
                ),
            ),
        )

        def raise_admission_timeout() -> None:
            self._local_base_url = ""
            record_asr_duration(
                "preview_admission_wait",
                (loop.time() - admission_started_at) * 1000,
                outcome="timeout",
            )
            raise AsrPreviewCapacityError(
                "本地实时语音预览并发已满，已切换到完整录音转写。"
            )

        async def load_candidates() -> tuple[str, ...]:
            remaining = admission_deadline - loop.time()
            if remaining <= 0:
                raise_admission_timeout()
            try:
                return await asyncio.wait_for(
                    self._load_ordered_local_streaming_urls(
                        client,
                        fallback_order,
                    ),
                    timeout=min(remaining, routing_probe_timeout_seconds),
                )
            except TimeoutError:
                # Health is only a routing hint. A slow probe falls back to
                # deterministic ordering without extending admission time.
                return fallback_order

        candidates = await load_candidates()
        next_load_probe_at = loop.time() + 0.5
        payload: dict[str, Any] | None = None
        session_id = ""
        while payload is None:
            if loop.time() >= admission_deadline:
                raise_admission_timeout()
            capacity_wait = False
            last_error: Exception | None = None
            for candidate_index, base in enumerate(candidates):
                self._local_base_url = base
                remaining = admission_deadline - loop.time()
                if remaining <= 0:
                    raise_admission_timeout()
                # A stalled first instance must not consume the entire pool
                # deadline and prevent the second instance from being tried.
                remaining_candidates = max(1, len(candidates) - candidate_index)
                attempt_timeout = max(0.001, remaining / remaining_candidates)
                try:
                    response = await asyncio.wait_for(
                        client.post(
                            self._local_endpoint("/api/start"),
                            **request_options,
                        ),
                        timeout=attempt_timeout,
                    )
                    if getattr(response, "status_code", 200) == 429:
                        capacity_wait = True
                        continue
                    response.raise_for_status()
                    candidate_payload = response.json()
                    session_id = str(candidate_payload.get("session_id") or "").strip()
                    if not session_id:
                        raise ValueError("missing session_id")
                    payload = candidate_payload
                    break
                except asyncio.CancelledError:
                    raise
                except TimeoutError as exc:
                    capacity_wait = True
                    last_error = exc
                except Exception as exc:
                    last_error = exc
            if payload is not None:
                break
            if capacity_wait:
                remaining = admission_deadline - loop.time()
                if remaining <= 0:
                    raise_admission_timeout()
                await asyncio.sleep(min(0.1, remaining))
                if loop.time() >= next_load_probe_at:
                    candidates = await load_candidates()
                    next_load_probe_at = loop.time() + 0.5
                continue
            self._local_base_url = ""
            if self._owns_local_client:
                await client.aclose()
            raise AsrConfigurationError(
                "无法连接本地 Qwen3-ASR 实例，请确认模型容器已经就绪。"
            ) from last_error

        record_asr_duration(
            "preview_admission_wait",
            (loop.time() - admission_started_at) * 1000,
            outcome="success",
        )

        self._local_client = client
        self._local_timeout = timeout
        self._local_session_id = session_id
        advertised_audio_format = str(payload.get("audio_format") or "").strip().lower()
        if advertised_audio_format in {"pcm_s16le", "pcm16", "audio/pcm"}:
            self._local_audio_format = "pcm_s16le"

    @staticmethod
    def _pcm16_to_float32(pcm_bytes: bytes) -> bytes:
        if len(pcm_bytes) % 2:
            pcm_bytes = pcm_bytes[:-1]
        samples = array("h")
        samples.frombytes(pcm_bytes)
        if sys.byteorder != "little":
            samples.byteswap()
        normalized = array("f", (sample / 32768.0 for sample in samples))
        if sys.byteorder != "little":
            normalized.byteswap()
        return normalized.tobytes()

    @staticmethod
    def _local_response_payload(response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        result = dict(payload)
        result["text"] = str(payload.get("text") or "").strip()
        for key in ("stable_text", "unstable_text"):
            if key in payload:
                result[key] = str(payload.get(key) or "").strip()
        return result

    async def _send_local_chunk(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        if not self._local_client or not self._local_session_id:
            raise AsrConfigurationError("本地 Qwen3-ASR 会话尚未建立。")
        pcm_bytes = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)]
        if not pcm_bytes:
            return
        if self._local_audio_format == "pcm_s16le":
            content = pcm_bytes
            content_type = "audio/pcm"
        else:
            content = self._pcm16_to_float32(pcm_bytes)
            content_type = "application/octet-stream"
        response = await self._local_client.post(
            self._local_endpoint("/api/chunk"),
            params={"session_id": self._local_session_id},
            content=content,
            headers={"Content-Type": content_type},
            timeout=self._local_timeout,
        )
        response.raise_for_status()
        payload = self._local_response_payload(response)
        if payload.get("queue_ms") is not None:
            record_asr_duration("preview_batch_wait", payload["queue_ms"], outcome="success")
        if payload.get("inference_ms") is not None:
            record_asr_duration("preview_inference", payload["inference_ms"], outcome="success")
        if payload.get("window_seconds") is not None:
            record_asr_preview_window(payload["window_seconds"])
        if payload.get("silence_skipped"):
            record_asr_silence_skip()

        reported_speech = payload.get("speech_detected")
        text = str(payload.get("text") or "")
        if reported_speech is False:
            text = ""
            payload["stable_text"] = ""
            payload["unstable_text"] = ""
        try:
            revision = int(payload.get("revision", -1))
        except (TypeError, ValueError):
            revision = -1
        if reported_speech is True:
            self._local_speech_detected = True
        if (
            revision >= 0
            and self._local_last_revision >= 0
            and revision < self._local_last_revision
        ):
            return

        previous_text = self._local_last_text
        changed = text != previous_text or revision > self._local_last_revision
        self._local_last_text = text
        self._local_last_revision = max(self._local_last_revision, revision)
        if changed and bool(text or previous_text):
            event: dict[str, Any] = {
                "type": "partial",
                "text": text,
                "preview": text,
            }
            for key in (
                "stable_text",
                "unstable_text",
                "revision",
                "segment_id",
                "correction_state",
                "window_seconds",
                "speech_detected",
            ):
                if payload.get(key) is not None:
                    event[key] = payload[key]
            await self._local_events.put(event)

    async def append_audio(self, pcm_bytes: bytes) -> None:
        if self._closed or self._preview_failed or not pcm_bytes:
            return
        self._local_pcm_buffer.extend(pcm_bytes)
        if len(self._local_pcm_buffer) > self._max_pending_bytes:
            self._audio_dropped = True
            overflow = len(self._local_pcm_buffer) - self._max_pending_bytes
            overflow += overflow % 2
            del self._local_pcm_buffer[:overflow]
            if not self._lag_warning_emitted:
                self._lag_warning_emitted = True
                await self._local_events.put(
                    {
                        "type": "status",
                        "code": "preview_lag_recovered",
                        "message": "实时预览已跳到当前语音，完整录音不受影响。",
                    }
                )
        self._local_buffer_event.set()

    async def _preview_worker(self) -> None:
        chunk_bytes = self._chunk_bytes
        max_request_bytes = max(chunk_bytes, self._max_pending_bytes)
        try:
            while True:
                await self._local_buffer_event.wait()
                self._local_buffer_event.clear()
                while (
                    len(self._local_pcm_buffer) >= chunk_bytes
                    or (self._preview_stopping and self._local_pcm_buffer)
                ):
                    if len(self._local_pcm_buffer) > max_request_bytes:
                        overflow = len(self._local_pcm_buffer) - max_request_bytes
                        overflow += overflow % 2
                        del self._local_pcm_buffer[:overflow]
                    take = len(self._local_pcm_buffer)
                    take -= take % 2
                    chunk = bytes(self._local_pcm_buffer[:take])
                    del self._local_pcm_buffer[:take]
                    await self._send_local_chunk(chunk)
                if self._preview_stopping and not self._local_pcm_buffer:
                    return
                if len(self._local_pcm_buffer) >= chunk_bytes:
                    self._local_buffer_event.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._preview_failed = True
            self._preview_connected = False
            self._local_pcm_buffer.clear()
            await self._local_events.put(
                {
                    "type": "preview_unavailable",
                    "code": "local_asr_chunk_failed",
                    "message": "本地实时预览已暂停，完整录音仍在继续。",
                    "recoverable": True,
                }
            )

    async def _stop_worker(self, *, cancel: bool) -> None:
        self._preview_stopping = True
        self._local_buffer_event.set()
        worker = self._worker_task
        if cancel and worker is not None and not worker.done():
            worker.cancel()
        try:
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
        finally:
            if self._worker_task is worker:
                self._worker_task = None

    async def _release_resources(self, *, cancel_session: bool) -> None:
        self._closed = True
        self._preview_connected = False
        client = self._local_client
        self._local_client = None
        try:
            if client is not None:
                if cancel_session and self._local_session_id:
                    try:
                        await client.post(
                            self._local_endpoint("/api/cancel"),
                            params={"session_id": self._local_session_id},
                            timeout=self._local_timeout,
                        )
                    except Exception:
                        pass
                if self._owns_local_client:
                    await client.aclose()
        finally:
            self._local_session_id = ""
            self._local_pcm_buffer.clear()
            await self._local_events.put(None)
            self._release_runtime_lease()

    async def finish(self) -> dict[str, Any]:
        """Drain pending PCM and flush the native stream into its corrected final text."""
        if self._finish_payload is not None:
            return dict(self._finish_payload)
        if (
            self._closed
            or self._preview_failed
            or self._audio_dropped
            or not self._preview_connected
        ):
            raise AsrConfigurationError("本地实时语音流不可用于最终定稿。")

        finalization_timeout = max(
            1.0,
            float(getattr(self.settings, "asr_local_final_timeout_seconds", 20.0)),
        )
        try:
            await asyncio.wait_for(
                self._stop_worker(cancel=False),
                timeout=finalization_timeout,
            )
        except TimeoutError as exc:
            self._preview_failed = True
            await self._stop_worker(cancel=True)
            await self._release_resources(cancel_session=True)
            raise AsrConfigurationError("本地实时语音流尾处理超时。") from exc
        if self._preview_failed:
            await self._release_resources(cancel_session=True)
            raise AsrConfigurationError("本地实时语音流处理失败。")

        client = self._local_client
        session_id = self._local_session_id
        if client is None or not session_id:
            await self._release_resources(cancel_session=True)
            raise AsrConfigurationError("本地实时语音会话已丢失。")
        try:
            response = await asyncio.wait_for(
                client.post(
                    self._local_endpoint("/api/finish"),
                    params={"session_id": session_id},
                    timeout=self._local_timeout,
                ),
                timeout=finalization_timeout,
            )
            response.raise_for_status()
            payload = self._local_response_payload(response)
            text = str(payload.get("text") or "").strip()
            speech_detected = payload.get("speech_detected")
            if speech_detected is True:
                self._local_speech_detected = True
            if not text and speech_detected is not False:
                raise AsrConfigurationError("本地实时语音流未生成最终文字。")
            self._local_last_text = text
            try:
                self._local_last_revision = max(
                    self._local_last_revision,
                    int(payload.get("revision", -1)),
                )
            except (TypeError, ValueError):
                pass
            self._finish_payload = dict(payload)
            self._local_session_id = ""
        except Exception:
            self._preview_failed = True
            await self._release_resources(cancel_session=True)
            raise

        await self._release_resources(cancel_session=False)
        return dict(self._finish_payload)

    async def close(self) -> None:
        if self._closed:
            return
        await self._stop_worker(cancel=True)
        await self._release_resources(cancel_session=True)

    async def receive_loop(self, send_frontend: SendFrontend) -> None:
        while True:
            event = await self._local_events.get()
            if event is None:
                return
            await send_frontend(event)


def create_realtime_asr_provider(
    settings: Settings,
    *,
    local_client: httpx.AsyncClient | None = None,
    speech_client: httpx.AsyncClient | None = None,
    routing_key: str = "",
    language: str | None = None,
    coordinator: Any | None = None,
) -> Any:
    """Build the configured server-side realtime ASR adapter."""

    if settings.asr_provider_mode == "local":
        return RealtimeAsrProxy(
            settings,
            client=local_client,
            routing_key=routing_key,
            language=language,
        )
    if settings.asr_provider_mode == "bosch":
        if str(settings.asr_bosch_realtime_url or "").strip():
            from backend.services.bosch_native_realtime_asr_service import (
                BoschNativeRealtimeAsrProxy,
            )

            return BoschNativeRealtimeAsrProxy(settings, language=language)
        from backend.services.bosch_realtime_asr_service import BoschChunkedAsrProxy

        if speech_client is None:
            speech_client = get_shared_async_client("speech")
        return BoschChunkedAsrProxy(
            settings,
            client=speech_client,
            language=language,
            coordinator=coordinator,
        )
    raise AsrConfigurationError(
        "浏览器语音模式应由前端 SpeechRecognition 直接处理。"
    )
