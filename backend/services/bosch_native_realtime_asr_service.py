from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed

from backend.config.settings import Settings
from backend.services.model_api_auth import ModelAPIAuth
from backend.services.recording_asr_service import join_segment_transcripts
from backend.services.realtime_asr_service import AsrConfigurationError


SendFrontend = Callable[[dict[str, Any]], Awaitable[None]]


def _realtime_url(base_url: str, model: str) -> str:
    parsed = urlsplit(base_url.strip())
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "model" for key, _ in query):
        query.append(("model", model.strip()))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class BoschNativeRealtimeAsrProxy:
    """Proxy one Qwen ASR realtime WebSocket without treating preview as final."""

    authoritative_final = False

    def __init__(
        self,
        settings: Settings,
        *,
        auth: ModelAPIAuth | None = None,
        language: str | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or ModelAPIAuth()
        self._language = str(
            language
            if language is not None
            else getattr(settings, "asr_language", "zh")
        ).strip()
        self._connection: Any | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._audio_lock = asyncio.Lock()
        self._pending_audio = bytearray()
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._connect_done = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._session_finished = asyncio.Event()
        self._ever_connected = False
        self._closed = False
        self._connected = False
        self._failed = False
        self._speech_detected = False
        self._events_closed = False
        self._failure_notified = False
        self._error_message = ""
        self._committed_text = ""
        self._current_stable = ""
        self._current_unstable = ""
        self._current_item_id = ""
        self._completed_items: set[str] = set()
        self._last_preview = ""
        self._revision = 0

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def speech_detected(self) -> bool:
        return self._speech_detected

    def validate(self) -> None:
        if not self.settings.asr_enabled or not self.settings.asr_realtime_enabled:
            raise AsrConfigurationError("实时语音预览当前未启用。")
        url = str(self.settings.asr_bosch_realtime_url or "").strip()
        if not url.lower().startswith("wss://"):
            raise AsrConfigurationError("Bosch 原生实时语音地址必须使用 wss://。")
        if not str(self.settings.asr_bosch_realtime_model or "").strip():
            raise AsrConfigurationError("Bosch 原生实时语音模型未配置。")

    def _event_id(self) -> str:
        return f"event_{uuid4().hex}"

    async def _send_json(self, payload: dict[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            raise AsrConfigurationError("Bosch 原生实时语音连接尚未建立。")
        async with self._send_lock:
            await connection.send(json.dumps(payload, ensure_ascii=False))

    async def _send_audio_payload(self, pcm_bytes: bytes) -> None:
        await self._send_json(
            {
                "event_id": self._event_id(),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm_bytes).decode("ascii"),
            }
        )

    async def _notify_failure(self) -> None:
        if self._failure_notified or self._closed:
            return
        self._failure_notified = True
        await self._events.put(
            {
                "type": "preview_unavailable",
                "code": "bosch_native_asr_failed",
                "message": "Bosch 实时语音预览已暂停，完整录音仍在继续。",
                "recoverable": True,
            }
        )

    async def connect(self) -> None:
        self.validate()
        try:
            headers = await self.auth.async_headers(
                self.settings.effective_asr_api_key
            )
            headers = {**headers, "OpenAI-Beta": "realtime=v1"}
            url = _realtime_url(
                self.settings.asr_bosch_realtime_url,
                self.settings.asr_bosch_realtime_model,
            )
            self._connection = await websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=float(self.settings.asr_connect_timeout_seconds),
                close_timeout=5,
                ping_interval=15,
                ping_timeout=15,
                compression=None,
                max_size=4 * 1024 * 1024,
            )
            self._receiver_task = asyncio.create_task(
                self._receiver_loop(),
                name=f"bosch-native-asr-{uuid4().hex[:8]}",
            )
            transcription: dict[str, Any] = {
                "language": self._language,
            }
            corpus = str(self.settings.asr_bosch_prompt or "").strip()
            if corpus:
                transcription["corpus"] = {"text": corpus}
            await self._send_json(
                {
                    "event_id": self._event_id(),
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "input_audio_format": "pcm",
                        "sample_rate": int(self.settings.asr_sample_rate),
                        "input_audio_transcription": transcription,
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": float(
                                self.settings.asr_bosch_realtime_vad_threshold
                            ),
                            "silence_duration_ms": int(
                                self.settings.asr_bosch_realtime_silence_ms
                            ),
                        },
                    },
                }
            )
            await asyncio.wait_for(
                self._session_ready.wait(),
                timeout=float(self.settings.asr_connect_timeout_seconds),
            )
            if self._failed:
                raise AsrConfigurationError(
                    self._error_message or "Bosch 原生实时语音初始化失败。"
                )
            async with self._audio_lock:
                self._connected = True
                self._ever_connected = True
                pending_audio = bytes(self._pending_audio)
                self._pending_audio.clear()
                if pending_audio:
                    await self._send_audio_payload(pending_audio)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            self._failed = True
            await self.close()
            if isinstance(exc, AsrConfigurationError):
                raise
            raise AsrConfigurationError("无法连接 Bosch 原生实时语音服务。") from exc
        finally:
            self._connect_done.set()

    async def append_audio(self, pcm_bytes: bytes) -> None:
        if self._closed or self._failed or not pcm_bytes:
            return
        aligned = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)]
        if not aligned:
            return
        async with self._audio_lock:
            if self._closed or self._failed:
                return
            if not self._connected:
                self._pending_audio.extend(aligned)
                return
            await self._send_audio_payload(aligned)

    async def _emit_partial(self, *, item_id: str, committed: bool) -> None:
        active = f"{self._current_stable}{self._current_unstable}"
        preview = join_segment_transcripts((self._committed_text, active))
        if preview == self._last_preview:
            return
        self._last_preview = preview
        self._revision += 1
        await self._events.put(
            {
                "type": "partial",
                "text": preview,
                "preview": preview,
                "stable_text": join_segment_transcripts(
                    (self._committed_text, self._current_stable)
                ),
                "unstable_text": self._current_unstable,
                "revision": self._revision,
                "segment_id": item_id,
                "correction_state": "committed" if committed else "correcting",
                "speech_detected": self._speech_detected,
            }
        )

    async def _handle_server_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type") or "")
        if event_type == "session.updated":
            self._session_ready.set()
            return
        if event_type == "input_audio_buffer.speech_started":
            self._speech_detected = True
            return
        if event_type == "conversation.item.input_audio_transcription.text":
            self._speech_detected = True
            self._current_item_id = str(payload.get("item_id") or "")
            self._current_stable = str(payload.get("text") or "")
            self._current_unstable = str(payload.get("stash") or "")
            await self._emit_partial(
                item_id=self._current_item_id,
                committed=False,
            )
            return
        if event_type == "conversation.item.input_audio_transcription.completed":
            self._speech_detected = True
            item_id = str(payload.get("item_id") or "")
            transcript = str(payload.get("transcript") or "").strip()
            if transcript and item_id not in self._completed_items:
                self._committed_text = join_segment_transcripts(
                    (self._committed_text, transcript)
                )
                if item_id:
                    self._completed_items.add(item_id)
            if not item_id or item_id == self._current_item_id:
                self._current_item_id = ""
                self._current_stable = ""
                self._current_unstable = ""
            await self._emit_partial(item_id=item_id, committed=True)
            return
        if event_type in {
            "error",
            "conversation.item.input_audio_transcription.failed",
        }:
            error = payload.get("error")
            if isinstance(error, dict):
                self._error_message = str(error.get("message") or "")
            self._failed = True
            self._session_ready.set()
            if self._connected:
                await self._notify_failure()
            return
        if event_type == "session.finished":
            final_text = str(payload.get("transcript") or "").strip()
            if final_text and not self._committed_text:
                self._committed_text = final_text
            self._session_finished.set()

    async def _receiver_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    continue
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    await self._handle_server_event(payload)
                if self._session_finished.is_set():
                    return
            if not self._session_finished.is_set() and not self._closed:
                self._failed = True
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            if not self._session_finished.is_set() and not self._closed:
                self._failed = True
        except Exception:
            self._failed = True
        finally:
            self._connected = False
            self._connect_done.set()
            self._session_ready.set()
            if (
                self._failed
                and self._ever_connected
                and not self._session_finished.is_set()
            ):
                await self._notify_failure()
                self._session_finished.set()

    async def finish(self) -> dict[str, Any]:
        await self._connect_done.wait()
        if self._failed or self._closed or self._connection is None:
            raise AsrConfigurationError("Bosch 原生实时语音流不可用于尾处理。")
        await self._send_json(
            {"event_id": self._event_id(), "type": "session.finish"}
        )
        try:
            await asyncio.wait_for(
                self._session_finished.wait(),
                timeout=float(self.settings.asr_local_final_timeout_seconds),
            )
        except TimeoutError as exc:
            self._failed = True
            raise AsrConfigurationError("Bosch 原生实时语音流尾处理超时。") from exc
        if self._failed:
            raise AsrConfigurationError(
                self._error_message or "Bosch 原生实时语音流处理失败。"
            )
        return {
            "text": self._committed_text,
            "stable_text": self._committed_text,
            "unstable_text": "",
            "revision": self._revision,
            "speech_detected": self._speech_detected,
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        async with self._audio_lock:
            self._pending_audio.clear()
        self._connect_done.set()
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass
        receiver = self._receiver_task
        self._receiver_task = None
        current = asyncio.current_task()
        if receiver is not None and receiver is not current and not receiver.done():
            receiver.cancel()
        if receiver is not None and receiver is not current:
            await asyncio.gather(receiver, return_exceptions=True)
        if not self._events_closed:
            self._events_closed = True
            await self._events.put(None)

    async def receive_loop(self, send_frontend: SendFrontend) -> None:
        while True:
            event = await self._events.get()
            if event is None:
                return
            await send_frontend(event)
