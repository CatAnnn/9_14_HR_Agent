from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

try:
    from langchain.agents import create_agent
    from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.prompts import ChatPromptTemplate
except ImportError as exc:  # pragma: no cover - runtime dependency guard
    raise RuntimeError(
        "LangChain is required for all Agent LLM execution. "
        "Install dependencies from backend/requirements.txt before running the backend."
    ) from exc

from backend.config.settings import Settings, get_settings
from backend.exceptions.llm_errors import (
    LLMError,
    ModelInvocationError,
    StructuredOutputError,
    is_retryable_http_status,
)
from backend.observability.metrics import (
    cached_input_token_count_from_usage,
    elapsed_ms,
    estimate_tokens,
    log_metric,
    now_ms,
    record_llm_request,
    record_llm_stream_timing,
    record_llm_token_usage,
    record_structured_output,
    token_count_from_usage,
)
from backend.services.http_client import get_shared_async_client, get_shared_sync_client
from backend.services.model_api_auth import ModelAPIAuth
from backend.services.model_scheduler import (
    current_model_endpoint,
    model_endpoint_candidates,
    record_model_endpoint_attempt,
)

StructuredModelT = TypeVar("StructuredModelT", bound=BaseModel)
StructuredPayloadNormalizer = Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]
StructuredStreamContentCallback = Callable[[str], Any]


def _http_model_error(status_code: int, message: str) -> ModelInvocationError:
    return ModelInvocationError(
        f"http_{status_code}",
        message,
        retryable=is_retryable_http_status(status_code),
        status_code=status_code,
    )


def _classify_transport_error(error: Exception) -> Exception:
    if isinstance(error, (ModelInvocationError, StructuredOutputError)):
        return error
    if isinstance(error, httpx.TimeoutException):
        return ModelInvocationError("timeout", str(error), retryable=True)
    if isinstance(error, httpx.TransportError):
        return ModelInvocationError("network", str(error), retryable=True)
    return error


def _merge_structured_stream_content(
    current: str,
    fragment: str,
    *,
    replace: bool,
) -> str:
    """Merge true deltas and provider-specific cumulative JSON snapshots."""
    if replace:
        return fragment
    if not fragment:
        return current
    if not current:
        return fragment

    structured_prefix = current.lstrip("\ufeff \t\r\n").lower()
    looks_structured = structured_prefix.startswith(
        ("{", "[", "```", "json", "<json>", "<think")
    )
    if looks_structured:
        if fragment.startswith(current):
            return fragment
        if current.startswith(fragment):
            return current
    return current + fragment


@dataclass(frozen=True, slots=True)
class _StreamObservation:
    thinking_fragment: str = ""
    content_fragment: tuple[str, bool] | None = None
    tool_fragments: tuple[dict[str, Any], ...] = ()
    finish_reasons: tuple[str, ...] = ()

    @property
    def has_thinking(self) -> bool:
        return bool(self.thinking_fragment)

    @property
    def has_content(self) -> bool:
        return bool(self.content_fragment and self.content_fragment[0])

    @property
    def has_tool_call(self) -> bool:
        return any(
            fragment.get("id")
            or fragment.get("name")
            or fragment.get("arguments")
            for fragment in self.tool_fragments
        )


@dataclass(slots=True)
class _StructuredStreamAccumulator:
    """Incrementally assemble one structured stream without retaining raw chunks."""

    content: str = ""
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)
    usage: dict[str, Any] | None = None
    finish_reasons: list[str] = field(default_factory=list)
    stream_done: bool = False

    def consume(
        self,
        adapter: type[Any],
        data: Any,
        *,
        invocation_error: bool = False,
    ) -> _StreamObservation:
        if isinstance(data, dict) and data.get("_stream_done") is True:
            self.stream_done = True
            return _StreamObservation()

        error = adapter._extract_stream_error(data)
        if error:
            message = f"Chat API stream error: {error}"
            if invocation_error:
                raise ModelInvocationError(
                    "stream_error",
                    message,
                    retryable=True,
                )
            raise LLMError(message)

        thinking_fragment = adapter._extract_stream_thinking_fragment(data)
        content_fragment = adapter._extract_stream_content_fragment(data)
        tool_fragments = tuple(adapter._extract_stream_tool_call_fragments(data))
        usage = adapter._extract_stream_usage(data)
        finish_reasons = tuple(adapter._extract_stream_finish_reasons(data))

        if content_fragment is not None:
            text, replace = content_fragment
            self.content = _merge_structured_stream_content(
                self.content,
                text,
                replace=replace,
            )
        for fragment in tool_fragments:
            index = int(fragment["index"])
            current = self.tool_calls.setdefault(
                index,
                {"id": "", "name": "", "arguments": ""},
            )
            if fragment["replace"]:
                current["id"] = fragment["id"] or current["id"]
                current["name"] = fragment["name"] or current["name"]
                current["arguments"] = fragment["arguments"]
                continue
            if fragment["id"]:
                current["id"] = fragment["id"]
            if fragment["name"] and fragment["name"] != current["name"]:
                current["name"] += fragment["name"]
            current["arguments"] += fragment["arguments"]
        if usage:
            self.usage = usage
        for reason in finish_reasons:
            if reason not in self.finish_reasons:
                self.finish_reasons.append(reason)

        return _StreamObservation(
            thinking_fragment=thinking_fragment,
            content_fragment=content_fragment,
            tool_fragments=tool_fragments,
            finish_reasons=finish_reasons,
        )

    def response(
        self,
        adapter: type[Any],
        *,
        require_complete: bool,
    ) -> dict[str, Any]:
        incomplete_reason = next(
            (
                reason
                for reason in self.finish_reasons
                if adapter._is_incomplete_finish_reason(reason)
            ),
            None,
        )
        if incomplete_reason is not None:
            raise StructuredOutputError(
                "truncated_stream",
                "Chat API stream ended before the structured output was complete "
                f"(finish_reason={incomplete_reason}).",
            )
        if require_complete and not self.stream_done and not self.finish_reasons:
            raise StructuredOutputError(
                "truncated_stream",
                "Chat API stream closed without [DONE] or a finish reason.",
            )

        raw_tool_calls: list[dict[str, Any]] = []
        for index, tool_call in sorted(self.tool_calls.items()):
            if not tool_call["name"]:
                raise StructuredOutputError(
                    "truncated_stream",
                    f"Streamed tool call {index} ended without a function name.",
                )
            raw_tool_calls.append(
                {
                    "id": tool_call["id"] or f"tool_call_{index}",
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": tool_call["arguments"] or "{}",
                    },
                }
            )
        if not self.content and not raw_tool_calls:
            raise StructuredOutputError(
                "missing_output",
                "Chat API stream ended without content or tool calls.",
            )
        message: dict[str, Any] = {"content": self.content}
        if raw_tool_calls:
            message["tool_calls"] = raw_tool_calls
        choice: dict[str, Any] = {"message": message}
        if self.finish_reasons:
            choice["finish_reason"] = self.finish_reasons[-1]
        response: dict[str, Any] = {"choices": [choice]}
        if self.usage is not None:
            response["usage"] = self.usage
        return response


class ModelFarmLangChainChatModel(BaseChatModel):
    """LangChain ChatModel adapter for the configured OpenAI-compatible/Bosch API.

    Agent modules must call this model through LangChain. The HTTP transport is
    encapsulated here so Agent modules never call raw APIs directly.
    """

    settings: Settings = Field(default_factory=get_settings)
    task_name: str | None = None
    explicit_model: str | None = None
    response_format: str | dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    enable_thinking: bool | None = None
    max_retries: int | None = None
    profile: dict[str, Any] = Field(default_factory=lambda: {"structured_output": True, "tool_calling": True})

    _auth: ModelAPIAuth = PrivateAttr(default_factory=ModelAPIAuth)
    _bound_tools: list[Any] = PrivateAttr(default_factory=list)
    _tool_choice: Any | None = PrivateAttr(default=None)

    @property
    def _llm_type(self) -> str:
        return "model_farm_langchain_chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "endpoint": current_model_endpoint(self.settings.chat_url),
            "provider": self.settings.llm_provider,
            "model": self.settings.model_for_task(self.task_name, self.explicit_model),
            "task_name": self.task_name,
        }

    def _effective_max_retries(self) -> int:
        return max(0, self.settings.llm_max_retries if self.max_retries is None else self.max_retries)

    def _effective_timeout(self) -> float:
        return self.settings.timeout_for_task(self.task_name)

    def bind_tools(self, tools: Iterable[Any], *, tool_choice: Any | None = None, **kwargs: Any) -> "ModelFarmLangChainChatModel":
        """Bind tool definitions for LangChain ToolStrategy structured output.

        LangChain's ToolStrategy converts a schema into an artificial function/tool.
        The model adapter forwards that tool schema to the configured API instead of
        bypassing LangChain.
        """
        clone = self.model_copy(deep=True)
        clone._bound_tools = list(tools or [])
        clone._tool_choice = tool_choice if tool_choice is not None else kwargs.get("tool_choice") or kwargs.get("toolChoice")
        return clone

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        urls = model_endpoint_candidates(self.settings)
        if not urls:
            raise LLMError("Chat API endpoint is not configured.")
        started = now_ms()
        payload = self._build_payload(messages, stop=stop)
        headers = self._auth.sync_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        last_error: Exception | None = None
        error_count = 0
        for attempt in range(self._effective_max_retries() + 1):
            url = urls[attempt % len(urls)]
            attempt_started = time.perf_counter()
            try:
                client = get_shared_sync_client()
                resp = client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._effective_timeout(),
                )
                if resp.status_code >= 400:
                    raise LLMError(self._format_http_error(resp))
                data = resp.json()
                result = self._chat_result_from_response(data)
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=True,
                )
                self._log_llm_metric(
                    payload,
                    usage=self._extract_usage(data),
                    duration_ms=elapsed_ms(started),
                    error_count=error_count,
                    output_text=self._metric_output_text(data),
                )
                return result
            except Exception as exc:  # noqa: BLE001
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=False,
                )
                last_error = exc
                error_count += 1
                if attempt >= self.settings.llm_max_retries:
                    break
        self._log_llm_metric(
            payload,
            usage=None,
            duration_ms=elapsed_ms(started),
            error_count=error_count,
            error=self._format_exception(last_error),
        )
        raise LLMError(f"LangChain chat model invocation failed: {self._format_exception(last_error)}")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        urls = model_endpoint_candidates(self.settings)
        if not urls:
            raise LLMError("Chat API endpoint is not configured.")
        started = now_ms()
        payload = self._build_payload(messages, stop=stop)
        headers = await self._auth.async_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        last_error: Exception | None = None
        error_count = 0
        max_retries = self._effective_max_retries()
        for attempt in range(max_retries + 1):
            url = urls[attempt % len(urls)]
            attempt_started = time.perf_counter()
            try:
                client = get_shared_async_client()
                resp = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._effective_timeout(),
                )
                if resp.status_code >= 400:
                    raise _http_model_error(
                        resp.status_code,
                        self._format_http_error(resp),
                    )
                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ModelInvocationError(
                        "invalid_response",
                        "Chat API returned a non-JSON response.",
                        retryable=True,
                    ) from exc
                result = self._chat_result_from_response(data)
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=True,
                )
                self._log_llm_metric(
                    payload,
                    usage=self._extract_usage(data),
                    duration_ms=elapsed_ms(started),
                    error_count=error_count,
                    output_text=self._metric_output_text(data),
                )
                return result
            except Exception as exc:  # noqa: BLE001
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=False,
                )
                last_error = _classify_transport_error(exc)
                error_count += 1
                if attempt >= max_retries:
                    break
        self._log_llm_metric(
            payload,
            usage=None,
            duration_ms=elapsed_ms(started),
            error_count=error_count,
            error=self._format_exception(last_error),
        )
        if isinstance(last_error, (ModelInvocationError, StructuredOutputError)):
            raise last_error
        raise LLMError(
            f"LangChain chat model invocation failed: {self._format_exception(last_error)}"
        )

    def _chat_result_from_response(self, data: dict[str, Any]) -> ChatResult:
        content = self._extract_content(data)
        tool_calls = self._extract_tool_calls(data)
        if content is None and not tool_calls:
            raise LLMError(f"Unable to extract chat content/tool calls from response keys: {list(data.keys())}")
        message_kwargs: dict[str, Any] = {"content": content or "", "response_metadata": {"raw": data, "usage": self._extract_usage(data)}}
        if tool_calls:
            message_kwargs["tool_calls"] = tool_calls
        message = AIMessage(**message_kwargs)
        generation = ChatGeneration(
            message=message,
            generation_info={"raw": data, "usage": self._extract_usage(data)},
        )
        return ChatResult(generations=[generation], llm_output={"raw": data, "usage": self._extract_usage(data)})

    async def ainvoke_stream_message(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        *,
        record_stream_timing: bool = False,
        on_content_update: StructuredStreamContentCallback | None = None,
    ) -> AIMessage:
        """Collect one streamed response, including fragmented tool calls."""
        urls = model_endpoint_candidates(self.settings)
        if not urls:
            raise LLMError("Chat API endpoint is not configured.")
        started = now_ms()
        payload = self._build_payload(messages, stop=stop, stream=True)
        headers = await self._auth.async_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        last_error: Exception | None = None
        error_count = 0
        max_retries = self._effective_max_retries()
        for attempt in range(max_retries + 1):
            url = urls[attempt % len(urls)]
            attempt_started = time.perf_counter()
            accumulator = _StructuredStreamAccumulator()
            timing_started = now_ms()
            ttft_ms: int | None = None
            thinking_complete_ms: int | None = None
            tool_call_complete_ms: int | None = None
            finish_reason: str | None = None
            thinking_observed = False
            tool_call_observed = False
            try:
                client = get_shared_async_client()
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._effective_timeout(),
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:1000]
                        raise _http_model_error(
                            resp.status_code,
                            f"Chat API HTTP {resp.status_code}: {body}",
                        )
                    async for data in self._iter_stream_payloads(resp):
                        observation = accumulator.consume(
                            type(self),
                            data,
                            invocation_error=True,
                        )
                        if (
                            observation.content_fragment is not None
                            and on_content_update is not None
                        ):
                            callback_result = on_content_update(accumulator.content)
                            if inspect.isawaitable(callback_result):
                                await callback_result
                        if observation.finish_reasons:
                            finish_reason = observation.finish_reasons[-1]
                        has_thinking = observation.has_thinking
                        has_content = observation.has_content
                        has_tool_call = observation.has_tool_call
                        if has_thinking or has_content or has_tool_call:
                            milestone_ms = elapsed_ms(timing_started)
                            if ttft_ms is None:
                                ttft_ms = milestone_ms
                            if has_thinking:
                                thinking_observed = True
                            if (
                                thinking_observed
                                and thinking_complete_ms is None
                                and (has_content or has_tool_call)
                            ):
                                thinking_complete_ms = milestone_ms
                            if has_tool_call:
                                tool_call_observed = True
                if tool_call_observed:
                    tool_call_complete_ms = elapsed_ms(timing_started)
                data = accumulator.response(
                    type(self),
                    require_complete=True,
                )
                result = self._chat_result_from_response(data)
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=True,
                )
                message = result.generations[0].message
                if not isinstance(message, AIMessage):
                    raise StructuredOutputError(
                        "missing_output",
                        "Streamed chat response did not produce an AI message.",
                    )
                self._log_llm_metric(
                    payload,
                    usage=self._extract_usage(data),
                    duration_ms=elapsed_ms(started),
                    error_count=error_count,
                    output_text=self._metric_output_text(data),
                )
                if record_stream_timing:
                    self._record_stream_timing(
                        payload=payload,
                        ttft_ms=ttft_ms,
                        thinking_complete_ms=thinking_complete_ms,
                        tool_call_complete_ms=tool_call_complete_ms,
                        finish_reason=finish_reason,
                        thinking_observed=thinking_observed,
                        tool_call_observed=tool_call_observed,
                        complete=True,
                    )
                return message
            except Exception as exc:  # noqa: BLE001
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=False,
                )
                last_error = _classify_transport_error(exc)
                error_count += 1
                if attempt >= max_retries:
                    if record_stream_timing:
                        self._record_stream_timing(
                            payload=payload,
                            ttft_ms=ttft_ms,
                            thinking_complete_ms=thinking_complete_ms,
                            tool_call_complete_ms=tool_call_complete_ms,
                            finish_reason=finish_reason,
                            thinking_observed=thinking_observed,
                            tool_call_observed=tool_call_observed,
                            complete=False,
                        )
                    break
        self._log_llm_metric(
            payload,
            usage=None,
            duration_ms=elapsed_ms(started),
            error_count=error_count,
            error=self._format_exception(last_error),
        )
        if isinstance(last_error, (ModelInvocationError, StructuredOutputError)):
            raise last_error
        raise LLMError(
            f"LangChain chat model streaming failed: {self._format_exception(last_error)}"
        )

    def _record_stream_timing(
        self,
        *,
        payload: dict[str, Any],
        ttft_ms: int | None,
        thinking_complete_ms: int | None,
        tool_call_complete_ms: int | None,
        finish_reason: str | None,
        thinking_observed: bool,
        tool_call_observed: bool,
        complete: bool,
    ) -> None:
        fields = {
            "task_name": self.task_name or "default",
            "llm_model_name": str(payload.get("model") or "unknown"),
            "llm_stream_ttft_ms": ttft_ms,
            "llm_stream_thinking_complete_ms": thinking_complete_ms,
            "llm_stream_tool_call_complete_ms": tool_call_complete_ms,
            "llm_stream_finish_reason": finish_reason or "unknown",
            "llm_stream_thinking_observed": thinking_observed,
            "llm_stream_tool_call_observed": tool_call_observed,
            "complete": complete,
        }
        log_metric("llm.stream_timing", **fields)
        record_llm_stream_timing(
            task_name=str(fields["task_name"]),
            model_name=str(fields["llm_model_name"]),
            provider_name=self.settings.llm_provider,
            ttft_ms=ttft_ms,
            thinking_complete_ms=thinking_complete_ms,
            tool_call_complete_ms=tool_call_complete_ms,
            complete=complete,
        )

    async def astream_text(self, messages: list[BaseMessage], stop: list[str] | None = None) -> AsyncIterator[str]:
        urls = model_endpoint_candidates(self.settings)
        if not urls:
            raise LLMError("Chat API endpoint is not configured.")
        started = now_ms()
        payload = self._build_payload(messages, stop=stop, stream=True)
        headers = await self._auth.async_headers(self.settings.api_key)
        headers.setdefault("Content-Type", "application/json")
        last_error: Exception | None = None
        error_count = 0
        for attempt in range(self._effective_max_retries() + 1):
            url = urls[attempt % len(urls)]
            attempt_started = time.perf_counter()
            output_parts: list[str] = []
            try:
                client = get_shared_async_client()
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self._effective_timeout(),
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", errors="replace")[:1000]
                        raise LLMError(f"Chat API HTTP {resp.status_code}: {body}")
                    async for delta in self._iter_stream_deltas(resp):
                        if delta:
                            output_parts.append(delta)
                            yield delta
                self._log_llm_metric(
                    payload,
                    usage=None,
                    duration_ms=elapsed_ms(started),
                    error_count=error_count,
                    output_text="".join(output_parts),
                )
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=True,
                )
                return
            except Exception as exc:  # noqa: BLE001
                record_model_endpoint_attempt(
                    url,
                    duration_ms=(time.perf_counter() - attempt_started) * 1000,
                    success=False,
                )
                last_error = exc
                error_count += 1
                # A second stream would restart from the beginning and duplicate
                # text already delivered to the caller.
                if output_parts:
                    break
                if attempt >= self.settings.llm_max_retries:
                    break
        self._log_llm_metric(
            payload,
            usage=None,
            duration_ms=elapsed_ms(started),
            error_count=error_count,
            error=self._format_exception(last_error),
        )
        raise LLMError(f"LangChain chat model streaming failed: {self._format_exception(last_error)}")

    async def _iter_stream_payloads(self, resp: httpx.Response) -> AsyncIterator[Any]:
        async for line in resp.aiter_lines():
            payload = self._stream_line_payload(line)
            if payload is None:
                continue
            if payload == "[DONE]":
                yield {"_stream_done": True}
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                preview = payload[:500].replace("\r", " ").replace("\n", " ")
                raise LLMError(f"Chat API returned non-JSON SSE data: {preview}") from exc
            yield data

    async def _iter_stream_deltas(self, resp: httpx.Response) -> AsyncIterator[str]:
        async for data in self._iter_stream_payloads(resp):
            error = self._extract_stream_error(data)
            if error:
                raise LLMError(f"Chat API stream error: {error}")
            delta = self._extract_stream_content_delta(data)
            if delta:
                yield delta

    @staticmethod
    def _stream_line_payload(line: str) -> str | None:
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            return None
        if stripped.startswith("data:"):
            return stripped[5:].strip()
        if stripped.startswith("{") or stripped.startswith("["):
            return stripped
        return None

    def _build_payload(self, messages: list[BaseMessage], stop: list[str] | None = None, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model_for_task(task_name=self.task_name, explicit_model=self.explicit_model),
            "messages": self._normalize_messages(messages),
            "temperature": self.temperature if self.temperature is not None else self.settings.temperature_for_task(self.task_name),
            "stream": stream,
        }
        if stop:
            payload["stop"] = stop[:4]
        effective_max_tokens = self.max_tokens if self.max_tokens is not None else self.settings.max_tokens_for_task(self.task_name)
        effective_enable_thinking = (
            self.enable_thinking
            if self.enable_thinking is not None
            else self.settings.enable_thinking_for_task(self.task_name)
        )
        optional_values = {
            "top_p": self.settings.llm_top_p,
            "max_tokens": effective_max_tokens,
            "enable_thinking": effective_enable_thinking,
            "thinking_budget": (
                self.settings.llm_thinking_budget if effective_enable_thinking is True else None
            ),
        }
        for key, value in optional_values.items():
            if value is not None:
                payload[key] = value
        if self.settings.llm_web_search:
            payload["web_search"] = True
        formatted = self._format_response_format(self.response_format)
        if formatted is not None:
            payload["response_format"] = formatted
        if self._bound_tools:
            payload["tools"] = [self._normalize_tool(tool) for tool in self._bound_tools]
            tool_choice = self._normalize_tool_choice(self._tool_choice)
            if tool_choice is not None:
                if self.settings.llm_provider in {"openai_compatible", "bosch_openai_compatible"}:
                    payload["tool_choice"] = tool_choice
                else:
                    payload["toolChoice"] = tool_choice
        return payload

    def _normalize_messages(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                role = "system"
            elif isinstance(message, HumanMessage):
                role = "user"
            elif isinstance(message, AIMessage):
                role = "assistant"
            else:
                role = getattr(message, "type", "user") or "user"
                if role == "human":
                    role = "user"
                if role == "ai":
                    role = "assistant"
            content = message.content
            if role == "system" and self.settings.llm_provider == "bosch_messages":
                system_parts.append(str(content))
                continue
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            row: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant" and isinstance(message, AIMessage):
                tool_calls = []
                for tool_call in message.tool_calls:
                    name = tool_call.get("name")
                    if not name:
                        continue
                    arguments = tool_call.get("args") or {}
                    if not isinstance(arguments, str):
                        arguments = json.dumps(
                            arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    tool_calls.append(
                        {
                            "id": str(tool_call.get("id") or "tool_call"),
                            "type": "function",
                            "function": {
                                "name": str(name),
                                "arguments": arguments,
                            },
                        }
                    )
                if tool_calls:
                    row["tool_calls"] = tool_calls
            tool_call_id = getattr(message, "tool_call_id", None)
            if role == "tool" and tool_call_id:
                row["tool_call_id"] = tool_call_id
                tool_name = getattr(message, "name", None)
                if tool_name:
                    row["name"] = tool_name
            normalized.append(row)
        if system_parts:
            prefix = "\n\n".join(system_parts)
            if normalized and normalized[0]["role"] == "user":
                normalized[0]["content"] = f"{prefix}\n\n{normalized[0]['content']}"
            else:
                normalized.insert(0, {"role": "user", "content": prefix})
        return normalized

    def _format_response_format(self, response_format: str | None) -> str | dict[str, str] | None:
        if not response_format or self.settings.llm_response_format_style == "none":
            return None
        if isinstance(response_format, dict):
            return response_format
        if response_format == "text":
            return "text" if self.settings.llm_response_format_style in {"auto", "bosch"} else None
        style = self.settings.llm_response_format_style
        if style == "auto":
            style = "bosch" if self.settings.llm_provider == "bosch_messages" else "openai"
        if style == "bosch":
            return response_format
        return {"type": response_format}

    def _normalize_tool_choice(self, tool_choice: Any | None) -> Any | None:
        if tool_choice is None or tool_choice is False:
            return None
        if tool_choice is True:
            normalized: Any = "required"
        elif isinstance(tool_choice, str) and tool_choice.lower() == "any":
            normalized = "required"
        else:
            normalized = tool_choice

        if self.settings.llm_provider == "bosch_openai_compatible":
            if isinstance(normalized, str) and normalized.lower() in {
                "auto",
                "none",
                "required",
            }:
                return normalized.lower()
            return "required"
        return normalized

    @staticmethod
    def _normalize_tool(tool: Any) -> dict[str, Any]:
        if isinstance(tool, dict):
            if "type" in tool and "function" in tool:
                return tool
            if "name" in tool and "parameters" in tool:
                return {"type": "function", "function": tool}
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or "structured_output"
        description = getattr(tool, "description", None) or getattr(tool, "__doc__", None) or name
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            parameters = args_schema.model_json_schema()
        elif isinstance(tool, type) and issubclass(tool, BaseModel):
            parameters = tool.model_json_schema()
            description = parameters.get("description") or description
        else:
            parameters = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": str(name),
                "description": str(description),
                "parameters": parameters,
            },
        }

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> dict[str, Any] | None:
        usage = data.get("usage")
        if usage is None and isinstance(data.get("data"), dict):
            usage = data["data"].get("usage")
        return usage if isinstance(usage, dict) else None

    @classmethod
    def _extract_tool_calls(cls, data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []

        candidates: list[Any] = []

        def append_calls(container: Any) -> None:
            if not isinstance(container, dict):
                return
            for key in ("tool_calls", "toolCalls"):
                if container.get(key) is not None:
                    candidates.append(container.get(key))
            legacy_call = container.get("function_call") or container.get("functionCall")
            if isinstance(legacy_call, dict):
                candidates.append([legacy_call])

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            append_calls(choice.get("message"))
            append_calls(choice.get("delta"))

        inner = data.get("data")
        if isinstance(inner, dict):
            nested = cls._extract_tool_calls(inner)
            if nested:
                return nested
        elif isinstance(inner, list):
            for item in reversed(inner):
                nested = cls._extract_tool_calls(item)
                if nested:
                    return nested

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                append_calls(message)
        append_calls(data)

        for raw_calls in candidates:
            if not isinstance(raw_calls, list):
                continue
            normalized: list[dict[str, Any]] = []
            for index, raw_call in enumerate(raw_calls):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
                name = raw_call.get("name") or function.get("name")
                argument_sources = (
                    raw_call.get("args"),
                    raw_call.get("arguments"),
                    raw_call.get("input"),
                    raw_call.get("data"),
                    function.get("args"),
                    function.get("arguments"),
                    function.get("input"),
                    function.get("data"),
                )
                args_raw = next((value for value in argument_sources if value is not None), None)
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw or "{}")
                    except json.JSONDecodeError:
                        args = {"raw_arguments": args_raw}
                elif isinstance(args_raw, dict):
                    args = args_raw
                else:
                    args = {"raw_arguments": args_raw}
                if name:
                    normalized.append(
                        {
                            "name": str(name),
                            "args": args,
                            "id": str(raw_call.get("id") or f"tool_call_{index}"),
                            "type": "tool_call",
                        }
                    )
            if normalized:
                return normalized
        return []

    @staticmethod
    def _extract_content(data: Any) -> str | None:
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message")
            if isinstance(message, dict) and message.get("content") is not None:
                return ModelFarmLangChainChatModel._stringify_content(message.get("content"))
            delta = choice.get("delta")
            if isinstance(delta, dict) and delta.get("content") is not None:
                return ModelFarmLangChainChatModel._stringify_content(delta.get("content"))
        inner = data.get("data")
        if isinstance(inner, dict):
            nested = ModelFarmLangChainChatModel._extract_content(inner)
            if nested is not None:
                return nested
        if isinstance(inner, list):
            for item in reversed(inner):
                nested = ModelFarmLangChainChatModel._extract_content(item)
                if nested is not None:
                    return nested
        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("content") is not None:
                    return ModelFarmLangChainChatModel._stringify_content(message.get("content"))
        message = data.get("message")
        if isinstance(message, dict) and message.get("content") is not None:
            return ModelFarmLangChainChatModel._stringify_content(message.get("content"))
        if data.get("content") is not None:
            return ModelFarmLangChainChatModel._stringify_content(data.get("content"))
        if data.get("text") is not None:
            return str(data.get("text"))
        if data.get("msg") and not isinstance(data.get("msg"), (dict, list)):
            return str(data.get("msg"))
        return None

    @classmethod
    def _extract_stream_content_delta(cls, data: Any) -> str | None:
        if isinstance(data, str):
            return data
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                for key in ("content", "text"):
                    if delta.get(key) is not None:
                        return cls._stringify_content(delta.get(key))
            if isinstance(choice, dict) and choice.get("text") is not None:
                return cls._stringify_content(choice.get("text"))
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and message.get("content") is not None:
                return cls._stringify_content(message.get("content"))
        inner = data.get("data")
        if isinstance(inner, dict):
            nested = cls._extract_stream_content_delta(inner)
            if nested is not None:
                return nested
        if isinstance(inner, list):
            parts = [part for item in inner if (part := cls._extract_stream_content_delta(item))]
            return "".join(parts) if parts else None
        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict):
                    for key in ("delta", "content", "text"):
                        if message.get(key) is not None:
                            return cls._stringify_content(message.get(key))
        for key in ("delta", "content", "text"):
            if data.get(key) is not None:
                return cls._stringify_content(data.get(key))
        return None

    @classmethod
    def _extract_stream_error(cls, data: Any) -> str | None:
        if isinstance(data, list):
            return next((error for item in data if (error := cls._extract_stream_error(item))), None)
        if not isinstance(data, dict):
            return None
        error = data.get("error")
        if error:
            return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            return cls._extract_stream_error(inner)
        return None

    @classmethod
    def _extract_stream_finish_reasons(cls, data: Any) -> list[str]:
        if isinstance(data, list):
            reasons = [
                reason
                for item in data
                for reason in cls._extract_stream_finish_reasons(item)
            ]
            return list(dict.fromkeys(reasons))
        if not isinstance(data, dict):
            return []

        reasons: list[str] = []
        for key in ("finish_reason", "finishReason", "stop_reason", "stopReason"):
            value = data.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                reasons.append(str(value))
        for key in ("data", "choices", "messages", "message", "result", "response", "output"):
            nested = data.get(key)
            if isinstance(nested, (dict, list)):
                reasons.extend(cls._extract_stream_finish_reasons(nested))
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _is_incomplete_finish_reason(reason: str) -> bool:
        normalized = re.sub(r"[\s-]+", "_", reason.strip().lower())
        if normalized in {
            "length",
            "content_filter",
            "content_filtered",
            "safety",
            "cancelled",
            "canceled",
            "error",
        }:
            return True
        return normalized.startswith(
            ("max_token", "max_output", "token_limit", "context_length")
        )

    @classmethod
    def _extract_stream_usage(cls, data: Any) -> dict[str, Any] | None:
        if isinstance(data, list):
            for item in reversed(data):
                usage = cls._extract_stream_usage(item)
                if usage is not None:
                    return usage
            return None
        if not isinstance(data, dict):
            return None
        usage = cls._extract_usage(data)
        if usage is not None:
            return usage
        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            return cls._extract_stream_usage(inner)
        return None

    @classmethod
    def _extract_stream_content_fragment(cls, data: Any) -> tuple[str, bool] | None:
        if isinstance(data, list):
            fragments = [
                fragment
                for item in data
                if (fragment := cls._extract_stream_content_fragment(item)) is not None
            ]
            if not fragments:
                return None
            if any(replace for _, replace in fragments):
                return next(fragment for fragment in reversed(fragments) if fragment[1])
            return "".join(text for text, _ in fragments), False
        if not isinstance(data, dict):
            return (data, False) if isinstance(data, str) else None
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                for key in ("content", "text"):
                    if delta.get(key) is not None:
                        return cls._stringify_content(delta.get(key)), False
            if isinstance(choice, dict) and choice.get("text") is not None:
                return cls._stringify_content(choice.get("text")), False
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and message.get("content") is not None:
                return cls._stringify_content(message.get("content")), True
        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            nested = cls._extract_stream_content_fragment(inner)
            if nested is not None:
                return nested
        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict):
                    for key in ("content", "text"):
                        if message.get(key) is not None:
                            return cls._stringify_content(message.get(key)), True
        for key in ("content", "text"):
            if data.get(key) is not None:
                return cls._stringify_content(data.get(key)), False
        if isinstance(data.get("delta"), str):
            return str(data["delta"]), False
        return None

    @classmethod
    def _extract_stream_thinking_fragment(cls, data: Any) -> str | None:
        keys = (
            "reasoning_content",
            "reasoningContent",
            "reasoning",
            "thinking_content",
            "thinkingContent",
            "thinking",
        )
        if isinstance(data, list):
            parts = [
                fragment
                for item in data
                if (fragment := cls._extract_stream_thinking_fragment(item))
            ]
            return "".join(parts) or None
        if not isinstance(data, dict):
            return None

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            for container_name in ("delta", "message"):
                container = choice.get(container_name)
                if isinstance(container, dict):
                    for key in keys:
                        value = container.get(key)
                        if value not in (None, "", []):
                            return cls._stringify_content(value)

        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            nested = cls._extract_stream_thinking_fragment(inner)
            if nested:
                return nested

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                for key in keys:
                    value = message.get(key)
                    if value not in (None, "", []):
                        return cls._stringify_content(value)

        for key in keys:
            value = data.get(key)
            if value not in (None, "", []):
                return cls._stringify_content(value)
        return None

    @classmethod
    def _extract_stream_tool_call_fragments(cls, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [
                fragment
                for item in data
                for fragment in cls._extract_stream_tool_call_fragments(item)
            ]
        if not isinstance(data, dict):
            return []

        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            for key, replace in (("delta", False), ("message", True)):
                fragments = cls._stream_tool_fragments_from_container(
                    choice.get(key),
                    replace=replace,
                )
                if fragments:
                    return fragments

        inner = data.get("data")
        if isinstance(inner, (dict, list)):
            nested = cls._extract_stream_tool_call_fragments(inner)
            if nested:
                return nested

        messages = data.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                fragments = cls._stream_tool_fragments_from_container(message, replace=True)
                if fragments:
                    return fragments
        return cls._stream_tool_fragments_from_container(data, replace=False)

    @staticmethod
    def _stream_tool_fragments_from_container(
        container: Any,
        *,
        replace: bool,
    ) -> list[dict[str, Any]]:
        if not isinstance(container, dict):
            return []
        raw_calls = container.get("tool_calls") or container.get("toolCalls")
        legacy_call = container.get("function_call") or container.get("functionCall")
        if raw_calls is None and isinstance(legacy_call, dict):
            raw_calls = [legacy_call]
        if not isinstance(raw_calls, list):
            return []
        fragments: list[dict[str, Any]] = []
        for fallback_index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            arguments = next(
                (
                    value
                    for value in (
                        raw_call.get("args"),
                        raw_call.get("arguments"),
                        raw_call.get("input"),
                        function.get("args"),
                        function.get("arguments"),
                        function.get("input"),
                    )
                    if value is not None
                ),
                "",
            )
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            try:
                index = int(raw_call.get("index", fallback_index))
            except (TypeError, ValueError):
                index = fallback_index
            fragments.append(
                {
                    "index": index,
                    "id": str(raw_call.get("id") or ""),
                    "name": str(raw_call.get("name") or function.get("name") or ""),
                    "arguments": str(arguments or ""),
                    "replace": replace,
                }
            )
        return fragments

    @classmethod
    def _aggregate_stream_payloads(
        cls,
        payloads: Iterable[Any],
        *,
        require_complete: bool = False,
    ) -> dict[str, Any]:
        accumulator = _StructuredStreamAccumulator()
        for data in payloads:
            accumulator.consume(cls, data)
        return accumulator.response(cls, require_complete=require_complete)

    @staticmethod
    def _stringify_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False)


    def _log_llm_metric(
        self,
        payload: dict[str, Any],
        *,
        usage: dict[str, Any] | None,
        duration_ms: int,
        error_count: int,
        output_text: str | None = None,
        error: str | None = None,
    ) -> None:
        input_tokens = token_count_from_usage(usage, "prompt_tokens", "input_tokens")
        input_estimated = input_tokens is None
        if input_tokens is None:
            input_tokens = estimate_tokens(payload.get("messages", []))
        cached_input_tokens = cached_input_token_count_from_usage(usage) or 0
        cached_input_tokens = min(input_tokens, max(0, cached_input_tokens))
        output_tokens = token_count_from_usage(usage, "completion_tokens", "output_tokens")
        output_estimated = output_tokens is None
        if output_tokens is None:
            output_tokens = estimate_tokens(output_text or "")
        provider_total_tokens = token_count_from_usage(usage, "total_tokens")
        total_tokens = (
            provider_total_tokens
            if provider_total_tokens is not None
            else input_tokens + output_tokens
        )
        usage_source = (
            "estimated"
            if input_estimated and output_estimated
            else "mixed"
            if input_estimated or output_estimated
            else "provider"
        )
        model_name = str(payload.get("model") or "unknown")
        provider_name = self.settings.llm_provider
        pricing = self.settings.pricing_for_model(
            provider_name=provider_name,
            model_name=model_name,
        )
        rates = pricing.rates_for_input_tokens(input_tokens) if pricing else None
        regular_input_tokens = input_tokens - cached_input_tokens
        input_cost_cny = (
            round(regular_input_tokens * rates.input_per_million / 1_000_000, 12)
            if rates is not None
            else None
        )
        cached_input_cost_cny = (
            round(
                cached_input_tokens
                * (
                    rates.cached_input_per_million
                    if rates.cached_input_per_million is not None
                    else rates.input_per_million
                )
                / 1_000_000,
                12,
            )
            if rates is not None and cached_input_tokens > 0
            else None
        )
        output_cost_cny = (
            round(output_tokens * rates.output_per_million / 1_000_000, 12)
            if rates is not None
            else None
        )
        request_outcome = "error" if error else "success"
        record_llm_token_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_name=self.task_name or "default",
            model_name=model_name,
            provider_name=provider_name,
            stream=bool(payload.get("stream")),
            request_outcome=request_outcome,
            input_estimated=input_estimated,
            output_estimated=output_estimated,
        )
        record_llm_request(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            input_cost_cny=input_cost_cny,
            cached_input_cost_cny=cached_input_cost_cny,
            output_cost_cny=output_cost_cny,
            task_name=self.task_name or "default",
            model_name=model_name,
            provider_name=provider_name,
            stream=bool(payload.get("stream")),
            request_outcome=request_outcome,
            duration_ms=duration_ms,
            input_estimated=input_estimated,
            output_estimated=output_estimated,
            pricing_version=self.settings.llm_pricing_version,
            pricing_configured=pricing is not None,
        )
        log_metric(
            "llm.invoke",
            task_name=self.task_name or "default",
            llm_model_name=payload.get("model"),
            llm_input_tokens=input_tokens,
            llm_cached_input_tokens=cached_input_tokens,
            llm_output_tokens=output_tokens,
            llm_total_tokens=total_tokens,
            llm_token_usage_source=usage_source,
            llm_input_tokens_estimated=input_estimated,
            llm_output_tokens_estimated=output_estimated,
            llm_total_tokens_provider_reported=provider_total_tokens is not None,
            llm_cost_cny=(
                round(
                    (input_cost_cny or 0.0)
                    + (cached_input_cost_cny or 0.0)
                    + (output_cost_cny or 0.0),
                    12,
                )
                if pricing is not None
                else None
            ),
            llm_cost_currency="CNY" if pricing is not None else None,
            llm_cost_estimated=input_estimated or output_estimated,
            llm_pricing_configured=pricing is not None,
            llm_pricing_version=self.settings.llm_pricing_version,
            llm_error_count=error_count,
            llm_ms=duration_ms,
            stream=bool(payload.get("stream")),
            error=error,
        )

    @classmethod
    def _metric_output_text(cls, data: dict[str, Any]) -> str:
        content = cls._extract_content(data)
        if content is not None:
            return content
        tool_calls = cls._extract_tool_calls(data)
        if tool_calls:
            return json.dumps(tool_calls, ensure_ascii=False, default=str)
        return ""

    @staticmethod
    def _format_exception(exc: Exception | None) -> str:
        if exc is None:
            return "unknown error"
        message = str(exc).strip()
        return f"{type(exc).__name__}: {message}" if message else type(exc).__name__

    @staticmethod
    def _format_http_error(resp: httpx.Response) -> str:
        body = resp.text[:1000]
        if resp.status_code == 400 and "response_format" in body:
            body += " Hint: Model Farm China chat/completions requires OpenAI-compatible response_format payloads; set LLM_RESPONSE_FORMAT_STYLE=openai."
        return f"Chat API HTTP {resp.status_code}: {body}"


class LangChainLLMService:
    """LangChain-only LLM facade for Agent modules."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def chat_model(
        self,
        *,
        task_name: str | None = None,
        model: str | None = None,
        response_format: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
        max_retries: int | None = None,
    ) -> ModelFarmLangChainChatModel:
        return ModelFarmLangChainChatModel(
            settings=self.settings,
            task_name=task_name,
            explicit_model=model,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            max_retries=max_retries,
        )

    async def ainvoke_text(
        self,
        *,
        prompt: str,
        task_name: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool | None = None,
    ) -> str:
        chain = (
            ChatPromptTemplate.from_messages([("user", "{prompt}")])
            | self.chat_model(
                task_name=task_name,
                model=model,
                response_format="text",
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
            )
            | StrOutputParser()
        )
        content = await chain.ainvoke({"prompt": prompt})
        cleaned = str(content).strip()
        if not cleaned:
            raise LLMError("LangChain chat model returned empty content.")
        return cleaned

    async def astream_text(
        self,
        *,
        prompt: str,
        task_name: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        chat_model = self.chat_model(
            task_name=task_name,
            model=model,
            response_format="text",
            temperature=temperature,
            max_tokens=max_tokens,
        )
        async for delta in chat_model.astream_text([HumanMessage(content=prompt)]):
            yield delta

    async def ainvoke_structured_single(
        self,
        *,
        prompt: str | list[dict[str, Any]],
        schema: type[StructuredModelT],
        task_name: str,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        enable_thinking: bool | None = None,
        payload_normalizer: StructuredPayloadNormalizer | None = None,
        stream: bool = False,
        record_stream_timing: bool = False,
        on_content_update: StructuredStreamContentCallback | None = None,
        structured_transport: Literal["tool", "json_schema"] = "tool",
        json_schema_strict: bool = True,
    ) -> StructuredModelT:
        """Run one structured inference with deterministic local recovery only."""
        started = now_ms()
        schema_name = schema.__name__
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.settings.timeout_for_task(task_name)
        )
        if structured_transport == "json_schema":
            response_schema_name = self._stable_json_schema_name(schema)
            chat_model = self.chat_model(
                task_name=task_name,
                model=model,
                response_format=self._json_schema_response_format(
                    schema,
                    schema_name=response_schema_name,
                    strict=json_schema_strict,
                ),
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                max_retries=0,
            )
            messages = [
                SystemMessage(
                    content=(
                        "Return exactly one JSON object matching the configured response schema. "
                        "Do not add prose, Markdown, or tool calls."
                    )
                ),
                HumanMessage(content=prompt),
            ]
        elif structured_transport == "tool":
            if self.settings.llm_provider in {"bosch_openai_compatible", "bosch_messages"}:
                explicit_tool_choice: Any = "required"
            else:
                explicit_tool_choice = {
                    "type": "function",
                    "function": {"name": schema_name},
                }
            chat_model = self.chat_model(
                task_name=task_name,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
                max_retries=0,
            ).bind_tools([schema], tool_choice=explicit_tool_choice)
            messages = [
                SystemMessage(
                    content=(
                        f"Call the {schema_name} tool exactly once. "
                        "Do not call any other tool and do not add prose. "
                        "If the gateway cannot emit a tool call, return only one JSON object "
                        "that matches the same schema."
                    )
                ),
                HumanMessage(content=prompt),
            ]
        else:
            raise ValueError(f"Unsupported structured transport: {structured_transport}")

        try:
            if stream:
                stream_arguments: dict[str, Any] = {
                    "record_stream_timing": record_stream_timing,
                }
                if on_content_update is not None:
                    stream_arguments["on_content_update"] = on_content_update
                invocation = chat_model.ainvoke_stream_message(
                    messages,
                    **stream_arguments,
                )
            else:
                invocation = chat_model.ainvoke(messages)
            if effective_timeout > 0:
                message = await asyncio.wait_for(invocation, timeout=effective_timeout)
            else:
                message = await invocation
            if not isinstance(message, AIMessage):
                raise StructuredOutputError(
                    "missing_output",
                    "The model did not return an AI message.",
                )
            if structured_transport == "json_schema":
                structured, repair_steps = self._parse_json_schema_message(
                    message=message,
                    schema=schema,
                    payload_normalizer=payload_normalizer,
                )
                source = "response_format_json_schema"
            else:
                structured, source, repair_steps = self._parse_single_structured_message(
                    message=message,
                    schema=schema,
                    expected_tool_name=schema_name,
                    payload_normalizer=payload_normalizer,
                )
            duration_ms = elapsed_ms(started)
            log_metric(
                "llm.structured_output",
                task_name=task_name,
                schema_name=schema_name,
                structured_output_source=source,
                structured_output_valid=True,
                structured_output_recovered=bool(repair_steps),
                structured_output_repair_count=len(repair_steps),
                structured_output_repair_steps=",".join(repair_steps) or "none",
                structured_output_ms=duration_ms,
            )
            record_structured_output(
                task_name=task_name,
                schema_name=schema_name,
                source=source,
                valid=True,
                recovered=bool(repair_steps),
                repair_steps=repair_steps,
                fallback_reason="",
                duration_ms=duration_ms,
            )
            return structured
        except TimeoutError as exc:
            error = StructuredOutputError(
                "timeout",
                f"Single structured inference exceeded {effective_timeout:.0f}s.",
            )
            duration_ms = elapsed_ms(started)
            log_metric(
                "llm.structured_output",
                task_name=task_name,
                schema_name=schema_name,
                structured_output_valid=False,
                structured_output_recovered=False,
                structured_output_fallback_reason=error.code,
                structured_output_error=error.code,
                structured_output_ms=duration_ms,
            )
            record_structured_output(
                task_name=task_name,
                schema_name=schema_name,
                source="none",
                valid=False,
                recovered=False,
                repair_steps=[],
                fallback_reason=error.code,
                duration_ms=duration_ms,
            )
            raise error from exc
        except StructuredOutputError as exc:
            duration_ms = elapsed_ms(started)
            log_metric(
                "llm.structured_output",
                task_name=task_name,
                schema_name=schema_name,
                structured_output_valid=False,
                structured_output_recovered=False,
                structured_output_fallback_reason=exc.code,
                structured_output_error=exc.code,
                structured_output_ms=duration_ms,
            )
            record_structured_output(
                task_name=task_name,
                schema_name=schema_name,
                source="none",
                valid=False,
                recovered=False,
                repair_steps=[],
                fallback_reason=exc.code,
                duration_ms=duration_ms,
            )
            raise
        except Exception as exc:
            duration_ms = elapsed_ms(started)
            log_metric(
                "llm.structured_output",
                task_name=task_name,
                schema_name=schema_name,
                structured_output_valid=False,
                structured_output_recovered=False,
                structured_output_fallback_reason="model_invocation",
                structured_output_error="model_invocation",
                structured_output_exception_type=type(exc).__name__,
                structured_output_ms=duration_ms,
            )
            record_structured_output(
                task_name=task_name,
                schema_name=schema_name,
                source="none",
                valid=False,
                recovered=False,
                repair_steps=[],
                fallback_reason="model_invocation",
                duration_ms=duration_ms,
            )
            raise

    @classmethod
    def _parse_json_schema_message(
        cls,
        *,
        message: AIMessage,
        schema: type[StructuredModelT],
        payload_normalizer: StructuredPayloadNormalizer | None = None,
    ) -> tuple[StructuredModelT, list[str]]:
        if message.tool_calls or getattr(message, "invalid_tool_calls", None):
            raise StructuredOutputError(
                "malformed_json",
                "JSON Schema response unexpectedly contained tool calls.",
            )

        content = message.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            fragments: list[str] = []
            for block in content:
                if isinstance(block, str):
                    fragments.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    fragments.append(block["text"])
                else:
                    raise StructuredOutputError(
                        "malformed_json",
                        "JSON Schema response contained a non-text content block.",
                    )
            text = "".join(fragments)
        else:
            raise StructuredOutputError(
                "missing_output",
                "JSON Schema response did not contain text content.",
            )

        text = text.strip()
        if not text:
            raise StructuredOutputError(
                "missing_output",
                "JSON Schema response was empty.",
            )

        text, repair_steps = cls._unwrap_json_schema_text(text)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            try:
                candidates = cls._json_object_candidates_from_text(text)
            except StructuredOutputError:
                raise StructuredOutputError(
                    "malformed_json",
                    "JSON Schema response was not one complete JSON object.",
                ) from exc

            validated_candidates: list[tuple[StructuredModelT, list[str]]] = []
            first_validation_error: StructuredOutputError | None = None
            for candidate_payload, candidate_steps in candidates:
                try:
                    structured, normalization_steps = cls._validate_structured_payload(
                        payload=candidate_payload,
                        schema=schema,
                        payload_normalizer=payload_normalizer,
                    )
                except StructuredOutputError as candidate_error:
                    if first_validation_error is None:
                        first_validation_error = candidate_error
                    continue
                validated_candidates.append(
                    (
                        structured,
                        cls._deduplicate_steps(
                            repair_steps
                            + candidate_steps
                            + ["selected_single_schema_object"]
                            + normalization_steps
                        ),
                    )
                )

            if len(validated_candidates) == 1:
                return validated_candidates[0]
            if len(validated_candidates) > 1:
                raise StructuredOutputError(
                    "malformed_json",
                    "JSON Schema response contained multiple valid JSON objects.",
                ) from exc
            if len(candidates) == 1 and first_validation_error is not None:
                raise first_validation_error from exc
            raise StructuredOutputError(
                "malformed_json",
                "JSON Schema response was not one unambiguous JSON object.",
            ) from exc
        if not isinstance(payload, dict):
            raise StructuredOutputError(
                "schema_validation",
                "JSON Schema response root must be an object.",
            )
        structured, normalization_steps = cls._validate_structured_payload(
            payload=payload,
            schema=schema,
            payload_normalizer=payload_normalizer,
        )
        return structured, cls._deduplicate_steps(repair_steps + normalization_steps)

    @staticmethod
    def _unwrap_json_schema_text(text: str) -> tuple[str, list[str]]:
        """Remove presentation-only wrappers while preserving one exact JSON value."""
        cleaned = text.strip()
        repair_steps: list[str] = []

        if cleaned.startswith("\ufeff"):
            cleaned = cleaned.lstrip("\ufeff").lstrip()
            repair_steps.append("removed_utf8_bom")

        lowered = cleaned.lower()
        if lowered.startswith("<think"):
            opening_end = cleaned.find(">")
            closing_start = lowered.find("</think>", opening_end + 1)
            if opening_end >= 0 and closing_start >= 0:
                cleaned = cleaned[closing_start + len("</think>") :].strip()
                repair_steps.append("removed_think_block")
                lowered = cleaned.lower()

        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
            lowered = cleaned.lower()
            if lowered.startswith("json") and (
                len(cleaned) == 4
                or cleaned[4].isspace()
                or cleaned[4] in "{["
            ):
                cleaned = cleaned[4:]
            cleaned = cleaned.lstrip()
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].rstrip()
            repair_steps.append("unwrapped_json_code_fence")
        elif lowered.startswith("<json>"):
            cleaned = cleaned[len("<json>") :].lstrip()
            if cleaned.lower().endswith("</json>"):
                cleaned = cleaned[: -len("</json>")].rstrip()
            repair_steps.append("unwrapped_json_tag")
        elif lowered.startswith("json") and len(cleaned) > 4 and (
            cleaned[4].isspace() or cleaned[4] in "{["
        ):
            cleaned = cleaned[4:].lstrip()
            repair_steps.append("removed_json_label")
        elif cleaned.endswith("```") and cleaned.lstrip().startswith(("{", "[")):
            cleaned = cleaned[:-3].rstrip()
            repair_steps.append("removed_trailing_json_fence")

        return cleaned, repair_steps

    @classmethod
    def _parse_single_structured_message(
        cls,
        *,
        message: AIMessage,
        schema: type[StructuredModelT],
        expected_tool_name: str,
        payload_normalizer: StructuredPayloadNormalizer | None = None,
    ) -> tuple[StructuredModelT, str, list[str]]:
        raw_tool_calls = list(message.tool_calls or [])
        raw_tool_calls.extend(list(getattr(message, "invalid_tool_calls", None) or []))
        valid_tool_results: list[tuple[StructuredModelT, list[str]]] = []
        tool_errors: list[StructuredOutputError] = []

        for tool_call in raw_tool_calls:
            try:
                payload, repair_steps = cls._payload_from_tool_call(tool_call)
                structured, normalization_steps = cls._validate_structured_payload(
                    payload=payload,
                    schema=schema,
                    payload_normalizer=payload_normalizer,
                )
                repair_steps.extend(normalization_steps)
                tool_name = str(tool_call.get("name") or "")
                if cls._normalized_tool_name(tool_name) != cls._normalized_tool_name(expected_tool_name):
                    repair_steps.append("normalized_tool_name")
                valid_tool_results.append((structured, cls._deduplicate_steps(repair_steps)))
            except StructuredOutputError as exc:
                tool_errors.append(exc)

        if valid_tool_results:
            structured, repair_steps = cls._select_consistent_result(
                results=valid_tool_results,
                conflict_code="conflicting_tool_calls",
            )
            if len(raw_tool_calls) > 1:
                repair_steps.append("deduplicated_tool_calls")
            if tool_errors:
                repair_steps.append("discarded_invalid_tool_calls")
            return structured, "tool_call", cls._deduplicate_steps(repair_steps)

        content_error: StructuredOutputError | None = None
        valid_content_results: list[tuple[StructuredModelT, list[str]]] = []
        try:
            for payload, repair_steps in cls._json_object_candidates_from_content(message.content):
                try:
                    structured, normalization_steps = cls._validate_structured_payload(
                        payload=payload,
                        schema=schema,
                        payload_normalizer=payload_normalizer,
                    )
                    valid_content_results.append(
                        (
                            structured,
                            cls._deduplicate_steps(repair_steps + normalization_steps),
                        )
                    )
                except StructuredOutputError as exc:
                    content_error = exc
        except StructuredOutputError as exc:
            content_error = exc

        if valid_content_results:
            structured, repair_steps = cls._select_consistent_result(
                results=valid_content_results,
                conflict_code="conflicting_content_objects",
            )
            if raw_tool_calls:
                repair_steps.append("recovered_from_content_after_tool_error")
            return structured, "content_json", cls._deduplicate_steps(repair_steps)

        validation_errors = [
            error
            for error in [*tool_errors, content_error]
            if error is not None and error.code == "schema_validation"
        ]
        if validation_errors:
            raise validation_errors[0]
        if tool_errors:
            raise tool_errors[0]
        if content_error is not None:
            raise content_error
        raise StructuredOutputError(
            "missing_output",
            "The model returned neither a usable tool call nor JSON content.",
        )

    @classmethod
    def _payload_from_tool_call(cls, tool_call: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        raw_args: Any = tool_call.get("args")
        if isinstance(raw_args, dict) and set(raw_args) == {"raw_arguments"}:
            raw_args = raw_args.get("raw_arguments")
        if raw_args is None:
            for key in ("arguments", "input", "data"):
                if tool_call.get(key) is not None:
                    raw_args = tool_call.get(key)
                    break

        repair_steps: list[str] = []
        if isinstance(raw_args, dict):
            payload = raw_args
        elif isinstance(raw_args, str):
            candidates = cls._json_object_candidates_from_text(raw_args)
            if not candidates:
                raise StructuredOutputError(
                    "malformed_json",
                    "The model returned malformed tool arguments.",
                )
            canonical = {
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                for payload, _ in candidates
            }
            if len(canonical) > 1:
                raise StructuredOutputError(
                    "conflicting_tool_arguments",
                    "The model returned conflicting JSON objects in one tool call.",
                )
            payload, parse_steps = candidates[0]
            repair_steps.extend(["parsed_raw_tool_arguments", *parse_steps])
        else:
            raise StructuredOutputError(
                "malformed_json",
                "The model returned missing or non-object tool arguments.",
            )

        payload, unwrap_steps = cls._unwrap_structured_payload(payload)
        repair_steps.extend(unwrap_steps)
        return payload, cls._deduplicate_steps(repair_steps)

    @staticmethod
    def _validate_structured_payload(
        *,
        payload: dict[str, Any],
        schema: type[StructuredModelT],
        payload_normalizer: StructuredPayloadNormalizer | None,
    ) -> tuple[StructuredModelT, list[str]]:
        repair_steps: list[str] = []
        normalized_payload = payload
        if payload_normalizer is not None:
            try:
                normalized_payload, repair_steps = payload_normalizer(dict(payload))
            except StructuredOutputError:
                raise
            except (TypeError, ValueError) as exc:
                raise StructuredOutputError(
                    "schema_validation",
                    f"{schema.__name__} payload normalization failed.",
                ) from exc
        if not isinstance(normalized_payload, dict):
            raise StructuredOutputError(
                "schema_validation",
                f"{schema.__name__} normalizer did not return an object.",
            )
        try:
            return schema.model_validate(normalized_payload), repair_steps
        except ValidationError as exc:
            safe_errors: list[str] = []
            validation_errors = exc.errors()
            for error in validation_errors[:8]:
                location = ".".join(
                    str(part) for part in (error.get("loc") or ("<root>",))
                )
                error_type = str(error.get("type") or "validation_error")
                safe_errors.append(f"{location}:{error_type}")
            if len(validation_errors) > len(safe_errors):
                safe_errors.append(
                    f"+{len(validation_errors) - len(safe_errors)}_more"
                )
            safe_summary = ", ".join(safe_errors) or "unknown_validation_error"
            raise StructuredOutputError(
                "schema_validation",
                (
                    f"{schema.__name__} failed {exc.error_count()} field validation(s): "
                    f"{safe_summary}."
                ),
            ) from exc

    @classmethod
    def _select_consistent_result(
        cls,
        *,
        results: list[tuple[StructuredModelT, list[str]]],
        conflict_code: str,
    ) -> tuple[StructuredModelT, list[str]]:
        canonical_results: dict[str, tuple[StructuredModelT, list[str]]] = {}
        for structured, repair_steps in results:
            canonical = json.dumps(
                structured.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            canonical_results.setdefault(canonical, (structured, repair_steps))
        if len(canonical_results) > 1:
            raise StructuredOutputError(
                conflict_code,
                "The model returned multiple conflicting structured results.",
            )
        return next(iter(canonical_results.values()))

    @classmethod
    def _json_object_candidates_from_content(cls, content: Any) -> list[tuple[dict[str, Any], list[str]]]:
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "".join(parts).strip()
        else:
            text = ""
        if not text:
            raise StructuredOutputError(
                "missing_output",
                "The model returned neither a tool call nor JSON content.",
            )
        return cls._json_object_candidates_from_text(text)

    @classmethod
    def _json_object_candidates_from_text(cls, text: str) -> list[tuple[dict[str, Any], list[str]]]:
        cleaned = text.strip()
        base_steps: list[str] = []
        without_thinking = re.sub(r"<think\b[^>]*>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        if without_thinking != cleaned:
            cleaned = without_thinking
            base_steps.append("removed_think_block")

        candidate_texts: list[tuple[str, list[str]]] = [(cleaned, list(base_steps))]
        fence_pattern = re.compile(r"```(?:json|python)?\s*(.*?)```", flags=re.IGNORECASE | re.DOTALL)
        for match in fence_pattern.finditer(cleaned):
            candidate_texts.append((match.group(1).strip(), [*base_steps, "removed_markdown_fence"]))
        for candidate in cls._balanced_object_texts(cleaned):
            if candidate.strip() != cleaned:
                candidate_texts.append((candidate, [*base_steps, "extracted_balanced_object"]))

        results: list[tuple[dict[str, Any], list[str]]] = []
        seen_payloads: set[str] = set()
        seen_texts: set[str] = set()
        for candidate_text, repair_steps in candidate_texts:
            candidate_text = candidate_text.strip()
            if not candidate_text or candidate_text in seen_texts:
                continue
            seen_texts.add(candidate_text)
            decoded = cls._decode_object_candidate(candidate_text)
            if decoded is None:
                continue
            payload, decode_steps = decoded
            payload, unwrap_steps = cls._unwrap_structured_payload(payload)
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            if canonical in seen_payloads:
                continue
            seen_payloads.add(canonical)
            results.append(
                (
                    payload,
                    cls._deduplicate_steps(repair_steps + decode_steps + unwrap_steps),
                )
            )
        if not results:
            raise StructuredOutputError(
                "malformed_json",
                "The model content does not contain a recoverable JSON object.",
            )
        return results

    @staticmethod
    def _decode_object_candidate(candidate: str) -> tuple[dict[str, Any], list[str]] | None:
        value: Any = candidate
        repair_steps: list[str] = []
        for _ in range(3):
            if isinstance(value, dict):
                return value, repair_steps
            if not isinstance(value, str):
                return None
            text = value.strip()
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                try:
                    value = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    return None
                repair_steps.append("parsed_python_literal")
            if isinstance(value, str):
                repair_steps.append("decoded_nested_json")
        return (value, repair_steps) if isinstance(value, dict) else None

    @staticmethod
    def _balanced_object_texts(text: str) -> list[str]:
        results: list[str] = []
        start: int | None = None
        depth = 0
        quote: str | None = None
        escaped = False
        for index, character in enumerate(text):
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if depth > 0 and character in {'"', "'"}:
                quote = character
                continue
            if character == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif character == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    results.append(text[start : index + 1])
                    start = None
        return results

    @classmethod
    def _unwrap_structured_payload(cls, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        current = payload
        repair_steps: list[str] = []
        metadata_keys = {"name", "type", "id", "tool_call_id"}
        for _ in range(4):
            wrapper_key = next(
                (
                    key
                    for key in ("arguments", "args", "input", "data")
                    if key in current and set(current).issubset(metadata_keys | {key})
                ),
                None,
            )
            if wrapper_key is None:
                break
            wrapped = current.get(wrapper_key)
            if isinstance(wrapped, dict):
                current = wrapped
            elif isinstance(wrapped, str):
                candidates = cls._json_object_candidates_from_text(wrapped)
                if len(candidates) != 1:
                    raise StructuredOutputError(
                        "conflicting_wrapped_objects",
                        "The model returned conflicting wrapped structured objects.",
                    )
                current = candidates[0][0]
                repair_steps.extend(candidates[0][1])
            else:
                raise StructuredOutputError(
                    "malformed_json",
                    "The model returned a non-object structured payload wrapper.",
                )
            repair_steps.append(f"unwrapped_{wrapper_key}")
        return current, cls._deduplicate_steps(repair_steps)

    @staticmethod
    def _normalized_tool_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    @staticmethod
    def _deduplicate_steps(steps: list[str]) -> list[str]:
        return list(dict.fromkeys(step for step in steps if step))

    async def ainvoke_structured(
        self,
        *,
        prompt: str | list[dict[str, Any]],
        schema: type[StructuredModelT],
        task_name: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        strategy: str | None = None,
        timeout_seconds: float | None = None,
        enable_thinking: bool | None = None,
    ) -> StructuredModelT:
        """Invoke a LangChain agent with structured output.

        The structured result must come from LangChain's ``structured_response``
        state key. Manual JSON extraction is not used for Agent structured tasks.
        """
        configured_strategy = (strategy or self.settings.langchain_structured_output_strategy).strip().lower()
        strategy_name = self._effective_structured_strategy(configured_strategy, explicit_strategy=strategy is not None)
        try:
            return await self._ainvoke_structured_once(
                prompt=prompt,
                schema=schema,
                strategy_name=strategy_name,
                task_name=task_name,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                enable_thinking=enable_thinking,
            )
        except Exception as exc:  # noqa: BLE001
            if strategy_name in {"auto", "provider"} and self._should_retry_structured_with_tool(exc):
                return await self._ainvoke_structured_once(
                    prompt=prompt,
                    schema=schema,
                    strategy_name="tool",
                    task_name=task_name,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                    enable_thinking=enable_thinking,
                )
            raise

    async def ainvoke_multimodal_structured(
        self,
        *,
        content: list[dict[str, Any]],
        schema: type[StructuredModelT],
        task_name: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        strategy: str | None = None,
        timeout_seconds: float | None = None,
        enable_thinking: bool | None = None,
    ) -> StructuredModelT:
        """Invoke structured output with OpenAI-compatible text/image content blocks."""
        if not content:
            raise ValueError("Multimodal content blocks cannot be empty.")
        effective_task_name = task_name or "document_vision"
        effective_thinking = (
            enable_thinking
            if enable_thinking is not None
            else self.settings.enable_thinking_for_task(effective_task_name)
        )
        if effective_thinking is True:
            return await self.ainvoke_structured_single(
                prompt=content,
                schema=schema,
                task_name=effective_task_name,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                enable_thinking=True,
                stream=True,
            )
        return await self.ainvoke_structured(
            prompt=content,
            schema=schema,
            task_name=effective_task_name,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
            enable_thinking=enable_thinking,
        )

    def _effective_structured_strategy(self, strategy_name: str, *, explicit_strategy: bool) -> str:
        if (
            not explicit_strategy
            and self.settings.llm_provider == "bosch_openai_compatible"
            and strategy_name in {"auto", "provider"}
        ):
            return "tool"
        return strategy_name

    @staticmethod
    def _should_retry_structured_with_tool(exc: Exception) -> bool:
        message = str(exc).lower()
        exc_name = type(exc).__name__.lower()
        return any(
            marker in message or marker in exc_name
            for marker in (
                "structuredoutputvalidationerror",
                "structured output",
                "structured_response",
                "json_schema must be provided",
                "native structured output expected valid json",
                "invalid control character",
                "response_format",
                "json_schema",
            )
        )

    async def _ainvoke_structured_once(
        self,
        *,
        prompt: str | list[dict[str, Any]],
        schema: type[StructuredModelT],
        strategy_name: str,
        task_name: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        enable_thinking: bool | None = None,
    ) -> StructuredModelT:
        response_format = self._structured_response_format(schema, strategy_name)
        model_response_format = None
        if strategy_name in {"auto", "provider"}:
            model_response_format = self._json_schema_response_format(schema)
        agent = create_agent(
            model=self.chat_model(
                task_name=task_name,
                model=model,
                response_format=model_response_format,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=enable_thinking,
            ),
            tools=[],
            response_format=response_format,
        )
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only the structured response required by the configured Pydantic schema. "
                        "Do not include prose outside structured fields."
                    ),
                },
                {"role": "user", "content": prompt},
            ]
        }
        if timeout_seconds is not None and timeout_seconds > 0:
            try:
                result = await asyncio.wait_for(agent.ainvoke(payload), timeout=timeout_seconds)
            except TimeoutError as exc:
                raise LLMError(
                    f"LangChain structured agent timed out after {timeout_seconds:.0f}s"
                    f" for task={task_name or 'default'}."
                ) from exc
        else:
            result = await agent.ainvoke(payload)
        if not isinstance(result, dict):
            raise LLMError("LangChain structured agent returned non-dict state.")
        structured = result.get("structured_response")
        if structured is None:
            raise LLMError("LangChain structured agent did not return structured_response.")
        if isinstance(structured, schema):
            return structured
        if isinstance(structured, dict):
            return schema.model_validate(structured)
        if hasattr(structured, "model_dump"):
            return schema.model_validate(structured.model_dump())
        raise LLMError(f"Unsupported structured_response type: {type(structured).__name__}")

    @staticmethod
    @lru_cache(maxsize=64)
    def _json_schema_response_format(
        schema: type[StructuredModelT],
        *,
        schema_name: str | None = None,
        strict: bool | None = False,
    ) -> dict[str, Any]:
        schema_config: dict[str, Any] = {
            "name": schema_name or schema.__name__,
            "schema": schema.model_json_schema(),
        }
        if strict is not None:
            schema_config["strict"] = strict
        return {
            "type": "json_schema",
            "json_schema": schema_config,
        }

    @staticmethod
    @lru_cache(maxsize=64)
    def _stable_json_schema_name(schema: type[StructuredModelT]) -> str:
        snake_case = re.sub(r"(?<!^)(?=[A-Z])", "_", schema.__name__).lower()
        safe_name = re.sub(r"[^a-z0-9_-]+", "_", snake_case).strip("_-")
        return (safe_name or "structured_output")[:64]

    @staticmethod
    def _structured_response_format(schema: type[StructuredModelT], strategy_name: str) -> Any:
        if strategy_name == "auto":
            return schema
        if strategy_name == "provider":
            return ProviderStrategy(schema)
        if strategy_name == "tool":
            return ToolStrategy(schema)
        raise LLMError("LANGCHAIN_STRUCTURED_OUTPUT_STRATEGY must be one of: auto, provider, tool.")
