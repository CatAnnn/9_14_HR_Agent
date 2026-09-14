from __future__ import annotations

import asyncio
import io
import logging
import socket
import threading
import wave
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.config.settings import Settings, get_settings
from backend.observability.metrics import elapsed_ms, log_metric, now_ms
from backend.services.asr_activity import (
    bosch_http_asr_idle_seconds,
    mark_bosch_http_asr_activity,
)
from backend.services.embedding_service import EmbeddingService
from backend.services.fish_tts_service import (
    FishTtsService,
    TtsConfigurationError,
    TtsError,
)
from backend.services.langchain_llm_service import LangChainLLMService
from backend.services.http_client import get_shared_async_client
from backend.services.model_api_auth import ModelAPIAuth
from backend.services.rerank_service import RerankService

logger = logging.getLogger(__name__)

_STARTUP_RETRY_INITIAL_SECONDS = 0.5
_STARTUP_RETRY_MAX_SECONDS = 5.0
_RETRYABLE_HTTP_STATUSES = (408, 425, 429, 500, 502, 503, 504)

_CHAT_TASK_NAMES = (
    None,
    "profile",
    "intent",
    "intent_performance",
    "employee_reply",
    "conversation_summary",
    "motivation_scoring",
    "emotion_transition",
    "rehearsal_dimensions",
    "guidance",
    "coach_evaluator",
    "coach_redline",
)


@dataclass(frozen=True)
class _WarmupTarget:
    key: str
    kind: str
    model: str
    runner: Callable[[], Awaitable[None]]
    retry_startup: bool = False


@dataclass
class _WarmupResult:
    key: str
    kind: str
    model: str
    status: str = "pending"
    duration_ms: int | None = None
    error: str | None = None


class ModelWarmupService:
    """Warm every configured model once without making application startup fragile."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm_service: LangChainLLMService | None = None,
        embedding_service: EmbeddingService | None = None,
        rerank_service: RerankService | None = None,
        tts_service: FishTtsService | None = None,
    ):
        self.settings = settings or get_settings()
        self.llm_service = llm_service or LangChainLLMService()
        self.embedding_service = embedding_service or EmbeddingService()
        self.rerank_service = rerank_service or RerankService()
        self.tts_service = tts_service or FishTtsService(self.settings)
        self._run_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._has_run = False
        self._status = "disabled" if not self.settings.model_warmup_enabled else "pending"
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._duration_ms: int | None = None
        self._results: dict[str, _WarmupResult] = {}

    async def warmup(self) -> dict[str, Any]:
        if not self.settings.model_warmup_enabled:
            return self.snapshot()

        async with self._run_lock:
            if self._has_run:
                return self.snapshot()
            self._has_run = True
            targets = self._build_targets()
            if not targets:
                self._finish(status="skipped", duration_ms=0)
                return self.snapshot()

            started = now_ms()
            with self._state_lock:
                self._status = "running"
                self._started_at = self._utc_now()
                self._results = {
                    target.key: _WarmupResult(
                        key=target.key,
                        kind=target.kind,
                        model=target.model,
                    )
                    for target in targets
                }

            semaphore = asyncio.Semaphore(max(1, int(self.settings.model_warmup_max_concurrency)))
            try:
                await asyncio.gather(
                    *(self._run_target(target, semaphore) for target in targets)
                )
            except asyncio.CancelledError:
                self._finish(status="cancelled", duration_ms=elapsed_ms(started))
                raise

            failed_count = sum(
                result.status == "failed" for result in self._results.values()
            )
            status = "completed" if failed_count == 0 else "completed_with_errors"
            duration = elapsed_ms(started)
            self._finish(status=status, duration_ms=duration)
            log_metric(
                "model.warmup.summary",
                warmup_status=status,
                warmup_target_count=len(targets),
                warmup_failed_count=failed_count,
                warmup_ms=duration,
            )
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "status": self._status,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "duration_ms": self._duration_ms,
                "targets": [asdict(result) for result in self._results.values()],
            }

    def _build_targets(self) -> list[_WarmupTarget]:
        targets: list[_WarmupTarget] = []
        if self.settings.chat_url.strip():
            for model in self._configured_chat_models():
                targets.append(
                    _WarmupTarget(
                        key=f"chat:{model}",
                        kind="chat",
                        model=model,
                        runner=lambda model=model: self._warm_chat_model(model),
                    )
                )

        embedding_model = self.settings.effective_embedding_model
        if self.settings.embedding_url.strip() and embedding_model:
            targets.append(
                _WarmupTarget(
                    key=f"embedding:{embedding_model}",
                    kind="embedding",
                    model=embedding_model,
                    runner=self._warm_embedding,
                    retry_startup=self.settings.embedding_uses_local_runtime,
                )
            )

        rerank_model = self.settings.effective_rerank_model
        if self.settings.rerank_url.strip() and rerank_model:
            targets.append(
                _WarmupTarget(
                    key=f"reranker:{rerank_model}",
                    kind="reranker",
                    model=rerank_model,
                    runner=self._warm_reranker,
                    retry_startup=self.settings.rerank_uses_local_runtime,
                )
            )

        realtime_asr_model = self.settings.asr_realtime_model.strip()
        if (
            self.settings.asr_enabled
            and self.settings.asr_realtime_enabled
            and self.settings.asr_local_streaming_url.strip()
            and self.settings.asr_provider_mode == "local"
            and realtime_asr_model
        ):
            targets.append(
                _WarmupTarget(
                    key=f"asr_realtime:{realtime_asr_model}",
                    kind="asr_realtime",
                    model=realtime_asr_model,
                    runner=self._warm_realtime_asr,
                    retry_startup=True,
                )
            )

        http_asr_model = self.settings.asr_http_model.strip()
        if (
            self.settings.asr_enabled
            and self.settings.asr_provider_mode != "browser"
            and self.settings.asr_http_url.strip()
            and http_asr_model
        ):
            targets.append(
                _WarmupTarget(
                    key=f"asr_http:{http_asr_model}",
                    kind="asr_http",
                    model=http_asr_model,
                    runner=self._warm_http_asr,
                )
            )

        tts_model = self.settings.tts_model.strip()
        if (
            self.settings.tts_enabled
            and self.settings.tts_http_url.strip()
            and tts_model
        ):
            targets.append(
                _WarmupTarget(
                    key=f"tts:{tts_model}",
                    kind="tts",
                    model=tts_model,
                    runner=self._warm_tts,
                )
            )
        return targets

    def _configured_chat_models(self) -> list[str]:
        task_names = list(_CHAT_TASK_NAMES)
        if self.settings.document_vision_enabled:
            task_names.append("document_vision")
        models: list[str] = []
        seen: set[str] = set()
        retry_model = self.settings.model_retry_race_model.strip()
        if retry_model:
            seen.add(retry_model)
            models.append(retry_model)
        for task_name in task_names:
            model = self.settings.model_for_task(task_name).strip()
            if model and model not in seen:
                seen.add(model)
                models.append(model)
        return models

    async def _run_target(
        self,
        target: _WarmupTarget,
        semaphore: asyncio.Semaphore,
    ) -> None:
        self._update_result(target.key, status="running")
        started = now_ms()
        timeout_seconds = max(
            1.0,
            float(self.settings.model_warmup_timeout_seconds),
        )
        try:
            await asyncio.wait_for(
                self._run_target_with_startup_retry(
                    target,
                    semaphore,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            self._update_result(
                target.key,
                status="cancelled",
                duration_ms=elapsed_ms(started),
            )
            raise
        except Exception as exc:  # noqa: BLE001
            duration = elapsed_ms(started)
            error = self._safe_error(exc)
            self._update_result(
                target.key,
                status="failed",
                duration_ms=duration,
                error=error,
            )
            logger.warning("Model warmup failed for %s: %s", target.key, error)
            log_metric(
                "model.warmup.target",
                warmup_target=target.key,
                warmup_kind=target.kind,
                warmup_model=target.model,
                warmup_status="failed",
                warmup_error=error,
                warmup_ms=duration,
            )
            return

        duration = elapsed_ms(started)
        self._update_result(
            target.key,
            status="completed",
            duration_ms=duration,
            error=None,
        )
        logger.info("Model warmup completed for %s in %sms", target.key, duration)
        log_metric(
            "model.warmup.target",
            warmup_target=target.key,
            warmup_kind=target.kind,
            warmup_model=target.model,
            warmup_status="completed",
            warmup_ms=duration,
        )

    async def _run_target_with_startup_retry(
        self,
        target: _WarmupTarget,
        semaphore: asyncio.Semaphore,
        *,
        timeout_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        retry_delay = _STARTUP_RETRY_INITIAL_SECONDS
        retry_count = 0
        while True:
            try:
                async with semaphore:
                    await target.runner()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                remaining = deadline - asyncio.get_running_loop().time()
                if (
                    not target.retry_startup
                    or remaining <= 0
                    or not self._is_retryable_startup_error(exc)
                ):
                    raise

                retry_count += 1
                delay = min(retry_delay, remaining)
                if retry_count <= 2 or retry_count in {4, 8} or retry_count % 12 == 0:
                    logger.info(
                        "Model warmup target %s is not ready (attempt %s); "
                        "retrying in %.1fs",
                        target.key,
                        retry_count,
                        delay,
                    )
                log_metric(
                    "model.warmup.target_retry",
                    warmup_target=target.key,
                    warmup_kind=target.kind,
                    warmup_model=target.model,
                    warmup_retry_count=retry_count,
                    warmup_retry_delay_ms=int(delay * 1000),
                    warmup_error=self._safe_error(exc),
                )
                await asyncio.sleep(delay)
                retry_delay = min(
                    _STARTUP_RETRY_MAX_SECONDS,
                    retry_delay * 2,
                )

    @staticmethod
    def _is_retryable_startup_error(exc: Exception) -> bool:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(
                current,
                (
                    httpx.NetworkError,
                    httpx.TimeoutException,
                    httpx.RemoteProtocolError,
                    ConnectionError,
                    TimeoutError,
                ),
            ):
                return True
            if isinstance(current, httpx.HTTPStatusError):
                status_code = current.response.status_code
                if status_code in _RETRYABLE_HTTP_STATUSES:
                    return True
            message = " ".join(str(current).split()).upper()
            if any(
                f"HTTP {status_code}" in message
                for status_code in _RETRYABLE_HTTP_STATUSES
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    async def _warm_chat_model(self, model: str) -> None:
        await self.llm_service.ainvoke_text(
            prompt="Reply with OK.",
            model=model,
            temperature=0.0,
            max_tokens=4,
            enable_thinking=False,
        )

    async def _warm_embedding(self) -> None:
        embeddings = await self.embedding_service.aembed(
            ["HR model startup warmup"],
            ensure_local_runtime=False,
        )
        if len(embeddings) != 1 or not embeddings[0]:
            raise RuntimeError("Embedding warmup did not return one vector.")

    async def _warm_reranker(self) -> None:
        ranked = await self.rerank_service.arerank(
            "performance feedback warmup",
            ["objective performance evidence and actionable feedback"],
            1,
            ensure_local_runtime=False,
        )
        if len(ranked) != 1:
            raise RuntimeError("Reranker warmup did not return one result.")

    async def _warm_realtime_asr(self) -> None:
        base_url = self.settings.asr_local_streaming_url.strip().rstrip("/")
        request_timeout = max(1.0, float(self.settings.asr_local_request_timeout_seconds))
        connect_timeout = max(1.0, float(self.settings.asr_connect_timeout_seconds))
        session_id = ""
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
        ) as client:
            response = await client.post("/api/start")
            response.raise_for_status()
            payload = response.json()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise RuntimeError("Realtime ASR warmup did not return a session id.")
            try:
                response = await client.post(
                    "/api/finish",
                    params={"session_id": session_id},
                )
                response.raise_for_status()
            finally:
                session_id = ""

    async def _warm_http_asr(self) -> None:
        headers = await ModelAPIAuth().async_headers(self.settings.effective_asr_api_key)
        files = {
            "file": (
                "warmup.wav",
                self._silent_wav(),
                "audio/wav",
            )
        }
        data = {
            "model": self.settings.asr_http_model,
            "language": self.settings.asr_language,
        }
        client = get_shared_async_client("speech")
        mark_bosch_http_asr_activity()
        try:
            response = await client.post(
                self.settings.asr_http_url,
                headers=headers,
                data=data,
                files=files,
                timeout=httpx.Timeout(self.settings.asr_timeout_seconds),
            )
        finally:
            mark_bosch_http_asr_activity()
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP ASR warmup returned status {response.status_code}.")

    async def keep_http_asr_warm(self) -> None:
        """Keep each Backend speech pool and the remote ASR deployment warm."""

        interval = float(self.settings.asr_bosch_keepwarm_interval_seconds)
        hostname_phase = sum(socket.gethostname().encode("utf-8")) % 101 / 100
        first_delay = interval * (1.0 + 0.5 * hostname_phase)
        await asyncio.sleep(first_delay)
        while True:
            if bosch_http_asr_idle_seconds() >= interval:
                started = now_ms()
                try:
                    await self._warm_http_asr()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Bosch ASR keepwarm failed: %s", self._safe_error(exc))
                    log_metric(
                        "asr.bosch.keepwarm",
                        asr_keepwarm_status="failed",
                        asr_keepwarm_ms=elapsed_ms(started),
                    )
                else:
                    log_metric(
                        "asr.bosch.keepwarm",
                        asr_keepwarm_status="completed",
                        asr_keepwarm_ms=elapsed_ms(started),
                    )
            await asyncio.sleep(interval)

    async def _warm_tts(self) -> None:
        retry_deadline = asyncio.get_running_loop().time() + max(
            1.0,
            float(self.settings.model_warmup_timeout_seconds) - 1.0,
        )
        readiness_probe = getattr(self.tts_service, "is_ready", None)
        if callable(readiness_probe):
            attempt = 0
            delay_seconds = 2.0
            while not await readiness_probe():
                attempt += 1
                remaining = retry_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TtsError("Fish Audio health endpoint did not become ready.")
                if attempt <= 2 or attempt in {4, 8} or attempt % 10 == 0:
                    logger.info(
                        "TTS health endpoint is not ready (attempt %s); retrying in %.1fs",
                        attempt,
                        min(delay_seconds, remaining),
                    )
                await asyncio.sleep(min(delay_seconds, remaining))
                delay_seconds = min(30.0, delay_seconds * 2)

        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        text_queue.put_nowait("你好。")
        text_queue.put_nowait(None)
        audio_bytes = 0
        try:
            async for event in self.tts_service.stream(
                text_queue,
                emotion_state=None,
                voice=None,
            ):
                if event.get("event") == "speech_audio":
                    audio_bytes += int(event.get("byte_length") or 0)
        except asyncio.CancelledError:
            raise
        except TtsConfigurationError:
            raise
        if audio_bytes <= 0:
            raise RuntimeError("TTS warmup did not return audio.")

    @staticmethod
    def _silent_wav(sample_rate: int = 16_000, duration_ms: int = 250) -> bytes:
        frame_count = sample_rate * duration_ms // 1000
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"\x00\x00" * frame_count)
        return buffer.getvalue()

    def _update_result(self, key: str, **updates: Any) -> None:
        with self._state_lock:
            result = self._results[key]
            for field, value in updates.items():
                setattr(result, field, value)

    def _finish(self, *, status: str, duration_ms: int) -> None:
        with self._state_lock:
            self._status = status
            self._finished_at = self._utc_now()
            self._duration_ms = duration_ms

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = " ".join(str(exc).split())[:300]
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()
