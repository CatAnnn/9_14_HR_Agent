from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import json
import time
from typing import Any, Iterable, Iterator


class SSEProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: Any


class SSEParser:
    """Incrementally parse UTF-8 SSE without assuming network chunk boundaries."""

    def __init__(self) -> None:
        self._buffer = ""
        self._pending_cr = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()

    def feed(self, chunk: bytes | str) -> list[SSEEvent]:
        text = self._decoder.decode(chunk, final=False) if isinstance(chunk, bytes) else chunk
        return self._append_text(text, final=False)

    def finish(self) -> list[SSEEvent]:
        events = self._append_text(self._decoder.decode(b"", final=True), final=True)
        if not self._buffer.strip():
            self._buffer = ""
            return events
        block, self._buffer = self._buffer, ""
        parsed = self._parse_block(block)
        if parsed is not None:
            events.append(parsed)
        return events

    def _append_text(self, text: str, *, final: bool) -> list[SSEEvent]:
        combined = f"{self._pending_cr}{text}"
        self._pending_cr = ""
        if not final and combined.endswith("\r"):
            combined, self._pending_cr = combined[:-1], "\r"
        self._buffer += combined.replace("\r\n", "\n").replace("\r", "\n")
        events: list[SSEEvent] = []
        while "\n\n" in self._buffer:
            block, self._buffer = self._buffer.split("\n\n", 1)
            parsed = self._parse_block(block)
            if parsed is not None:
                events.append(parsed)
        return events

    @staticmethod
    def _parse_block(block: str) -> SSEEvent | None:
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field_name, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field_name == "event":
                event_name = value or "message"
            elif field_name == "data":
                data_lines.append(value)
        if not data_lines and event_name == "message":
            return None
        raw_data = "\n".join(data_lines)
        try:
            data: Any = json.loads(raw_data)
        except json.JSONDecodeError:
            data = raw_data
        return SSEEvent(event=event_name, data=data)


@dataclass(slots=True)
class StreamObservation:
    flow: str
    started_at: float = field(default_factory=time.perf_counter)
    event_names: list[str] = field(default_factory=list)
    first_event_ms: float | None = None
    first_content_ms: float | None = None
    completed_ms: float | None = None
    response_bytes: int = 0
    coach_tasks: set[str] = field(default_factory=set)
    terminal_payload: dict[str, Any] | None = None

    def observe(self, event: SSEEvent, *, now: float | None = None) -> None:
        observed_at = now if now is not None else time.perf_counter()
        elapsed_ms = max(0.0, (observed_at - self.started_at) * 1000.0)
        if self.first_event_ms is None:
            self.first_event_ms = elapsed_ms
        self.event_names.append(event.event)
        if event.event in self._content_events and self.first_content_ms is None:
            self.first_content_ms = elapsed_ms
        if event.event == "task_done" and isinstance(event.data, dict):
            task_id = str(event.data.get("task_id") or event.data.get("dimension") or "")
            if task_id:
                self.coach_tasks.add(task_id)
        if event.event == "error":
            detail = event.data if isinstance(event.data, str) else json.dumps(event.data, ensure_ascii=False)
            raise SSEProtocolError(f"{self.flow} stream returned error: {detail[:300]}")
        if event.event == "done":
            self.completed_ms = elapsed_ms
            self.terminal_payload = event.data if isinstance(event.data, dict) else {}

    @property
    def _content_events(self) -> set[str]:
        if self.flow == "guidance":
            return {"delta", "section_done"}
        if self.flow == "coach":
            return {"task_done"}
        return {"delta"}

    def validate(self) -> None:
        if not self.event_names:
            raise SSEProtocolError(f"{self.flow} stream returned no events")
        if self.event_names[0] != "start":
            raise SSEProtocolError(
                f"{self.flow} stream must begin with start, got {self.event_names[0]}"
            )
        if self.event_names.count("done") != 1 or self.event_names[-1] != "done":
            raise SSEProtocolError(f"{self.flow} stream did not end with exactly one done event")
        if self.first_content_ms is None:
            raise SSEProtocolError(f"{self.flow} stream returned no useful content")
        if self.flow in {"guidance", "coach"} and not bool(
            (self.terminal_payload or {}).get("complete")
        ):
            raise SSEProtocolError(f"{self.flow} terminal event is incomplete")
        if self.flow == "coach":
            if self.event_names.count("task_done") != 4 or "task_error" in self.event_names:
                raise SSEProtocolError(
                    "coach stream must contain four successful task_done events"
                )


def observe_sse_chunks(chunks: Iterable[bytes], *, flow: str) -> StreamObservation:
    parser = SSEParser()
    observation = StreamObservation(flow=flow)
    for chunk in chunks:
        observation.response_bytes += len(chunk)
        for event in parser.feed(chunk):
            observation.observe(event)
    for event in parser.finish():
        observation.observe(event)
    observation.validate()
    return observation


def iter_response_chunks(response: Any, *, chunk_size: int = 4096) -> Iterator[bytes]:
    yield from response.iter_content(chunk_size=chunk_size)

