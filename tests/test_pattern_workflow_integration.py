from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.simulation import MotivationState, PsychologicalPatternState
from backend.schemas.state import SessionState
from backend.services.employee_state_transition_service import (
    EmployeeStateTransitionResult,
)
from backend.services.rehearsal_service import RehearsalService
from backend.workflows.nodes import RehearsalNodes


@pytest.mark.asyncio
async def test_non_stream_analysis_runs_motivation_transition_and_rag_in_parallel():
    started: set[str] = set()
    release = asyncio.Event()
    captured: dict[str, object] = {}
    guidance = (
        '{"main_psychological_activity":"保持当前判断",'
        '"response_tendency":"谨慎回应"}'
    )
    chunks = [
        RetrievedChunk(
            chunk_id="chunk-1",
            source_id="general/example.md",
            title="example",
            scope="general",
            text="employee-visible evidence",
            score=1.0,
        )
    ]

    async def rendezvous(name: str) -> None:
        started.add(name)
        if len(started) == 3:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=1)

    class MotivationScoring:
        async def update_after_manager_message(
            self,
            state: SessionState,
            manager_message: str,
        ) -> SessionState:
            captured["motivation_input"] = state.motivation.primary_score
            await rendezvous("motivation")
            result = state.model_copy(deep=True)
            result.motivation.primary_score = 80.0
            return result

    class StateTransition:
        async def update_after_manager_message(
            self,
            state: SessionState,
            manager_message: str,
        ) -> EmployeeStateTransitionResult:
            captured["transition_input"] = state.motivation.primary_score
            await rendezvous("transition")
            transitioned = state.model_copy(deep=True)
            transitioned.psychological_pattern_state = PsychologicalPatternState(
                last_processed_manager_turn=1,
            )
            return EmployeeStateTransitionResult(
                state=transitioned,
                pattern_response_guidance=guidance,
                pattern_dynamics_applied=True,
            )

    class Employee:
        async def aretrieve_reply_context(
            self,
            state: SessionState,
            manager_message: str,
        ) -> list[RetrievedChunk]:
            captured["retrieval_input"] = state.motivation.primary_score
            await rendezvous("rag")
            return chunks

        async def reply(
            self,
            state: SessionState,
            manager_message: str,
            pattern_response_guidance: str = "",
            *,
            retrieved_chunks: list[RetrievedChunk] | None = None,
        ) -> str:
            captured["reply_motivation"] = state.motivation.primary_score
            captured["reply_chunks"] = retrieved_chunks
            captured["reply_guidance"] = pattern_response_guidance
            return "我想先确认这些具体依据。"

    nodes = RehearsalNodes(
        motivation_scoring=MotivationScoring(),
        employee_state_transition=StateTransition(),
    )
    nodes.employee_agent = Employee()
    state = SessionState(
        session_id="non-stream-parallel",
        motivation=MotivationState(
            primary_motive_id="fairness",
            primary_score=10.0,
        ),
    )

    result = await nodes.employee_reply_node(state, "我们来讨论本轮表现。")

    assert started == {"motivation", "transition", "rag"}
    assert captured["motivation_input"] == 10.0
    assert captured["transition_input"] == 10.0
    assert captured["retrieval_input"] == 10.0
    assert captured["reply_motivation"] == 80.0
    assert captured["reply_chunks"] == chunks
    assert captured["reply_guidance"] == guidance
    assert result.psychological_pattern_state == PsychologicalPatternState(
        last_processed_manager_turn=1,
    )
    assert result.conversation[-1].text == "我想先确认这些具体依据。"


@pytest.mark.asyncio
async def test_feature_flag_off_keeps_the_original_sequential_node_flow():
    order: list[str] = []

    class MotivationScoring:
        async def update_after_manager_message(
            self,
            state: SessionState,
            manager_message: str,
        ) -> SessionState:
            order.append("motivation")
            return state

    class EmotionTransition:
        async def update_after_manager_message(
            self,
            state: SessionState,
            manager_message: str,
        ) -> SessionState:
            order.append("emotion")
            return state

    class DisabledSettings:
        psychological_pattern_dynamics_enabled = False

    class DisabledStateTransition:
        settings = DisabledSettings()

        async def update_after_manager_message(
            self,
            state: SessionState,
            manager_message: str,
        ) -> EmployeeStateTransitionResult:
            raise AssertionError("joint transition must not run when disabled")

    class LegacyEmployee:
        async def reply(
            self,
            state: SessionState,
            manager_message: str,
        ) -> str:
            order.append("reply")
            return "收到。"

    nodes = RehearsalNodes(
        motivation_scoring=MotivationScoring(),
        emotion_transition=EmotionTransition(),
        employee_state_transition=DisabledStateTransition(),
    )
    nodes.employee_agent = LegacyEmployee()

    result = await nodes.employee_reply_node(
        SessionState(session_id="feature-off"),
        "我们继续。",
    )

    assert order == ["motivation", "emotion", "reply"]
    assert [turn.speaker for turn in result.conversation] == [
        "manager",
        "employee",
    ]


class _StreamSessionStore:
    def __init__(self, state: SessionState) -> None:
        self.state = state

    def get_session(self, session_id: str) -> SessionState:
        assert session_id == self.state.session_id
        return self.state

    def save_session(self, state: SessionState) -> SessionState:
        self.state = state
        return state


class _StreamScheduler:
    @asynccontextmanager
    async def slot(self, **_kwargs):
        yield type("QueueSlot", (), {"queue_ms": 0.0})()


class _StreamTts:
    def __init__(self) -> None:
        self.text_chunks: list[str] = []

    async def ensure_ready(self) -> None:
        return None

    async def stream(self, text_queue, **_kwargs):
        while True:
            chunk = await text_queue.get()
            if chunk is None:
                break
            self.text_chunks.append(chunk)
        yield {"event": "speech_start"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern_enabled",
    (True, False),
    ids=("pattern-on", "pattern-off"),
)
async def test_streaming_path_respects_pattern_switch(
    monkeypatch,
    pattern_enabled: bool,
):
    state = SessionState(
        session_id="pattern-stream",
        setup_ready=True,
        run_mode="rehearsal_report",
    )
    model_session_leases: list[tuple[str, tuple[str, ...]]] = []
    runtime_recycler = SimpleNamespace(
        acquire_session_lease=lambda session_id, service_names: model_session_leases.append(
            (session_id, tuple(service_names))
        ),
        release_session_lease=lambda _session_id: None,
    )
    service = RehearsalService(
        model_scheduler=_StreamScheduler(),
        runtime_recycler=runtime_recycler,
    )
    service.session_service = _StreamSessionStore(state)
    tts = _StreamTts()
    service.tts_service = tts
    monkeypatch.setattr(service.settings, "tts_enabled", True)
    monkeypatch.setattr(
        service.settings,
        "psychological_pattern_dynamics_enabled",
        pattern_enabled,
    )
    captured: dict[str, object] = {}
    guidance = (
        '{"main_psychological_activity":"谨慎核对新信息",'
        '"response_tendency":"clarify"}'
    )

    async def passthrough(next_state: SessionState, _message: str) -> SessionState:
        return next_state

    async def transition(
        next_state: SessionState,
        _message: str,
    ) -> EmployeeStateTransitionResult:
        transitioned = next_state.model_copy(deep=True)
        transitioned.psychological_pattern_state = PsychologicalPatternState(
            last_processed_manager_turn=1,
        )
        return EmployeeStateTransitionResult(
            state=transitioned,
            pattern_response_guidance=guidance,
            pattern_dynamics_applied=True,
        )

    async def retrieve(*_args, **_kwargs) -> list[RetrievedChunk]:
        return []

    async def stream_reply(
        _state: SessionState,
        _message: str,
        retrieved_chunks: list[RetrievedChunk] | None = None,
        **kwargs,
    ):
        captured["chunks"] = retrieved_chunks
        captured["reply_kwargs"] = kwargs
        yield "我想先核对一下具体依据。"

    async def no_summary(_state: SessionState) -> None:
        return None

    monkeypatch.setattr(service, "_start_dimension_evaluation", lambda *_a, **_k: None)
    monkeypatch.setattr(
        service.motivation_scoring,
        "update_after_manager_message",
        passthrough,
    )
    monkeypatch.setattr(
        service.employee_state_transition,
        "update_after_manager_message",
        transition,
    )
    monkeypatch.setattr(
        service.workflow.nodes.employee_agent,
        "aretrieve_reply_context",
        retrieve,
    )
    monkeypatch.setattr(
        service.workflow.nodes.employee_agent,
        "stream_reply",
        stream_reply,
    )
    monkeypatch.setattr(service.summary_service, "ensure_scheduled", no_summary)

    events = [
        event
        async for event in service.stream_manager_message(
            state.session_id,
            "我们继续讨论。",
            speech_enabled=True,
        )
    ]

    assert events[-1]["event"] == "done"
    assert any(event["event"] == "speech_start" for event in events)
    assert tts.text_chunks == ["我想先核对一下具体依据。"]
    assert captured["chunks"] == []
    assert captured["reply_kwargs"] == (
        {"pattern_response_guidance": guidance}
        if pattern_enabled
        else {}
    )
    assert model_session_leases == []
    assert service.session_service.state.psychological_pattern_state == (
        PsychologicalPatternState(last_processed_manager_turn=1)
    )
    assert service.session_service.state.conversation[-1].text == (
        "我想先核对一下具体依据。"
    )
