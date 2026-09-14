import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone

import pytest

from backend.config.settings import get_settings
from backend.repositories.conversation_summary_repository import ConversationSummaryRepository
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.conversation import ConversationTurn, EmotionTurnSnapshot
from backend.schemas.conversation_summary import (
    ConversationPromptContext,
    ConversationSummaryRecord,
)
from backend.schemas.state import SessionState
from backend.services.conversation_summary_service import ConversationSummaryService


def _state(turn_count: int, session_id: str = "summary-session") -> SessionState:
    state = SessionState(session_id=session_id)
    state.conversation = [
        ConversationTurn(
            turn_index=index,
            speaker="manager" if index % 2 else "employee",
            text=f"message-{index:02d}",
        )
        for index in range(1, turn_count + 1)
    ]
    return state


class InMemorySummaryRepository:
    def __init__(self, record: ConversationSummaryRecord | None = None):
        self.record = record
        self.save_attempts: list[dict] = []

    def get(self, session_id: str) -> ConversationSummaryRecord | None:
        if self.record is not None:
            assert self.record.session_id == session_id
        return self.record

    def save_if_newer(self, **payload) -> ConversationSummaryRecord | None:
        self.save_attempts.append(dict(payload))
        candidate = ConversationSummaryRecord(
            session_id=payload["session_id"],
            generation=payload["generation"],
            summary_text=payload["summary_text"],
            covered_through_turn_index=payload["covered_through_turn_index"],
            model_name=payload["model_name"],
            prompt_version=payload["prompt_version"],
        )
        current = self.record
        if current is not None and (
            current.generation > candidate.generation
            or (
                current.generation == candidate.generation
                and current.covered_through_turn_index
                >= candidate.covered_through_turn_index
            )
        ):
            return None
        self.record = candidate
        return candidate

    def reset_generation(self, session_id: str) -> ConversationSummaryRecord:
        generation = (self.record.generation if self.record else 0) + 1
        self.record = ConversationSummaryRecord(
            session_id=session_id,
            generation=generation,
        )
        return self.record


class FailingSummaryRepository:
    def get(self, session_id: str) -> ConversationSummaryRecord | None:
        raise RuntimeError("database unavailable")


class FakeSummaryLLM:
    def __init__(self, responses: list[object] | None = None):
        self.responses = list(responses or ["updated summary"])
        self.calls: list[dict] = []

    async def ainvoke_text(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return str(response)


class GatedSummaryLLM(FakeSummaryLLM):
    def __init__(self):
        super().__init__(["stale summary"])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke_text(self, **kwargs) -> str:
        self.calls.append(dict(kwargs))
        self.started.set()
        await self.release.wait()
        return "stale summary"


class RecordingModelScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    @asynccontextmanager
    async def slot(self, **payload):
        self.calls.append(dict(payload))
        yield


def _service(repository, llm=None, scheduler=None) -> ConversationSummaryService:
    settings = get_settings().model_copy(
        update={
            "conversation_raw_turn_window": 12,
            "conversation_summary_batch_turns": 6,
            "conversation_summary_llm_timeout_seconds": 20.0,
        }
    )
    return ConversationSummaryService(
        settings=settings,
        repository=repository,
        llm_service=llm or FakeSummaryLLM(),
        model_scheduler=scheduler,
    )


async def _drain(service: ConversationSummaryService) -> None:
    while service._tasks:
        await asyncio.gather(*list(service._tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_summary_model_uses_background_scheduler_slot():
    repository = InMemorySummaryRepository()
    llm = FakeSummaryLLM()
    scheduler = RecordingModelScheduler()
    service = _service(repository, llm, scheduler)

    assert await service.ensure_scheduled(_state(18)) is True
    await _drain(service)

    assert scheduler.calls == [
        {
            "session_id": "summary-session",
            "category": "background",
            "endpoint": service.settings.chat_url,
            "model": service.settings.model_for_task("conversation_summary"),
        }
    ]


def test_summary_becomes_due_only_after_six_turns_over_the_window():
    service = _service(InMemorySummaryRepository())
    record = ConversationSummaryRecord(session_id="summary-session")

    assert service._due_turns(_state(17), record) == []
    assert [turn.turn_index for turn in service._due_turns(_state(18), record)] == list(
        range(1, 7)
    )


def test_summary_prompt_projects_only_dialogue_semantics():
    turn = ConversationTurn(
        turn_index=1,
        speaker="manager",
        text="请说明这个季度的交付情况。",
        created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        metadata={
            "rehearsal_timing": {
                "attempt_id": "summary-attempt",
                "raw_operational_payload": "计" * 3_540,
            },
            "emotion_snapshot": {
                "anchor_id": "guarded",
                "reason_summary": "内部推断不应成为摘要事实。",
            },
        },
    )

    prompt = ConversationSummaryService._build_prompt("", [turn])

    assert "请说明这个季度的交付情况。" in prompt
    assert '"turn_index": 1' in prompt
    assert '"speaker": "manager"' in prompt
    assert "created_at" not in prompt
    assert "rehearsal_timing" not in prompt
    assert "raw_operational_payload" not in prompt
    assert "emotion_snapshot" not in prompt
    assert "内部推断不应成为摘要事实" not in prompt


def test_english_summary_prompt_and_context_do_not_reuse_chinese_cache():
    state = _state(20)
    state.locale = "en"
    old_chinese_record = ConversationSummaryRecord(
        session_id=state.session_id,
        generation=2,
        summary_text="这是中文旧摘要。",
        covered_through_turn_index=6,
        prompt_version="v1",
    )

    locale_record = ConversationSummaryService._record_for_locale(
        state,
        old_chinese_record,
    )
    context = ConversationSummaryService._build_context(state, locale_record)
    prompt = ConversationSummaryService._build_prompt(
        "",
        state.conversation[:2],
        output_locale="en",
    )

    assert locale_record.generation == 3
    assert locale_record.prompt_version == "v2:en"
    assert context.output_locale == "en"
    assert context.summary_text == ""
    assert [turn.turn_index for turn in context.raw_turns] == list(range(1, 21))
    assert "Use concise English plain text" in prompt
    assert "使用简洁中文" not in prompt


@pytest.mark.parametrize(
    ("locale", "language_marker", "summary_label"),
    [
        ("de", "knappen deutschen Klartext", "Bestehende Zusammenfassung"),
        ("ja", "簡潔な日本語のプレーンテキスト", "既存の要約"),
    ],
)
def test_german_and_japanese_summary_prompts_use_the_requested_language(
    locale: str,
    language_marker: str,
    summary_label: str,
):
    prompt = ConversationSummaryService._build_prompt(
        "",
        _state(2).conversation,
        output_locale=locale,
    )

    assert language_marker in prompt
    assert summary_label in prompt
    assert "使用简洁中文" not in prompt


@pytest.mark.asyncio
async def test_summary_context_keeps_pending_buffer_and_recent_twelve_turns():
    repository = InMemorySummaryRepository(
        ConversationSummaryRecord(
            session_id="summary-session",
            generation=0,
            summary_text="messages 1 through 6",
            covered_through_turn_index=6,
        )
    )
    service = _service(repository)

    context = await service.context_for_reply(_state(23))

    assert context.summary_text == "messages 1 through 6"
    assert [turn.turn_index for turn in context.raw_turns] == list(range(7, 24))
    assert len(context.raw_turns) == 17
    assert service._tasks == {}


@pytest.mark.asyncio
async def test_summary_context_exposes_a_safe_emotion_state_at_the_coverage_boundary():
    state = _state(20)
    boundary_snapshot = EmotionTurnSnapshot(
        valence=-0.3,
        arousal=0.5,
        dominance=-0.2,
        anchor_id="guarded",
        previous_anchor_id="neutral",
        vad_delta={"valence": -0.2, "arousal": 0.3, "dominance": -0.1},
        transition_intensity=0.3,
        reason_summary="COVERED_INTERNAL_REASON_MUST_NOT_REACH_REPLY",
    ).model_dump(exclude_none=True)
    state.conversation[4].metadata = {
        "emotion_snapshot": {
            **boundary_snapshot,
            "reply_emotion_guidance": "STALE_GUIDANCE_MUST_NOT_REACH_REPLY",
        },
        "rehearsal_timing": {
            "raw_operational_payload": "TIMING_MUST_NOT_REACH_REPLY",
        },
    }
    repository = InMemorySummaryRepository(
        ConversationSummaryRecord(
            session_id=state.session_id,
            summary_text="前六轮已完成事实核对。",
            covered_through_turn_index=6,
        )
    )
    service = _service(repository)

    context = await service.context_for_reply(state)

    assert context.summary_emotion_continuity == {
        "covered_through_turn_index": 6,
        "snapshot_turn_index": 5,
        "emotion_snapshot": {
            key: value
            for key, value in boundary_snapshot.items()
            if key != "reason_summary"
        },
    }
    assert [turn.turn_index for turn in context.raw_turns] == list(range(7, 21))

    from backend.agents.employee_agent import EmployeeAgent

    for test_prompt_enabled in (False, True):
        prompt = EmployeeAgent._build_reply_prompt(
            state,
            "请继续。",
            [],
            conversation_context=context,
            test_prompt_enabled=test_prompt_enabled,
        )
        assert '"covered_through_turn_index":6' in prompt
        assert '"snapshot_turn_index":5' in prompt
        assert '"anchor_id":"guarded"' in prompt
        assert "message-05" not in prompt
        assert "COVERED_INTERNAL_REASON_MUST_NOT_REACH_REPLY" not in prompt
        assert "STALE_GUIDANCE_MUST_NOT_REACH_REPLY" not in prompt
        assert "TIMING_MUST_NOT_REACH_REPLY" not in prompt
        assert "冲突时当前状态优先" in prompt or "始终优先" in prompt


@pytest.mark.asyncio
async def test_runtime_context_events_are_never_reintroduced_by_summary_context():
    state = SessionState(
        session_id="summary-session",
        conversation=[
            ConversationTurn(turn_index=1, speaker="manager", text="manager text"),
            ConversationTurn(
                turn_index=2,
                speaker="system",
                text="OLD_RUNTIME_CONTEXT_SHOULD_STAY_CLEARED",
                metadata={"type": "rehearsal_context_update"},
            ),
            ConversationTurn(turn_index=3, speaker="employee", text="employee text"),
        ],
    )
    repository = InMemorySummaryRepository(
        ConversationSummaryRecord(
            session_id=state.session_id,
            generation=1,
            summary_text="",
            covered_through_turn_index=0,
        )
    )
    service = _service(repository)

    context = await service.context_for_reply(state)

    assert [turn.turn_index for turn in context.raw_turns] == [1, 3]
    assert all(
        "OLD_RUNTIME_CONTEXT_SHOULD_STAY_CLEARED" not in turn.text
        for turn in context.raw_turns
    )

    overflow_state = _state(19)
    overflow_state.conversation[0] = state.conversation[1]
    due_turns = service._due_turns(
        overflow_state,
        ConversationSummaryRecord(session_id=overflow_state.session_id),
    )
    assert [turn.turn_index for turn in due_turns] == list(range(2, 8))
    assert all(
        turn.metadata.get("type") != "rehearsal_context_update"
        for turn in due_turns
    )


@pytest.mark.asyncio
async def test_summary_updates_in_six_turn_batches_with_task_specific_model_config(
    monkeypatch,
):
    metric_events: list[dict] = []
    monkeypatch.setattr(
        "backend.services.conversation_summary_service.record_conversation_summary",
        lambda **fields: metric_events.append(fields),
    )
    repository = InMemorySummaryRepository()
    llm = FakeSummaryLLM(["first rolling summary"])
    service = _service(repository, llm)

    context = await service.context_for_reply(_state(18))
    assert [turn.turn_index for turn in context.raw_turns] == list(range(1, 19))
    await _drain(service)

    assert repository.record is not None
    assert repository.record.covered_through_turn_index == 6
    assert repository.record.summary_text == "first rolling summary"
    assert len(llm.calls) == 1
    assert llm.calls[0]["task_name"] == "conversation_summary"
    assert llm.calls[0]["temperature"] == 0
    assert llm.calls[0]["max_tokens"] == service.settings.max_tokens_for_task("conversation_summary")
    assert llm.calls[0]["enable_thinking"] is service.settings.enable_thinking_for_task(
        "conversation_summary"
    )
    assert "message-06" in llm.calls[0]["prompt"]
    assert "message-07" not in llm.calls[0]["prompt"]
    assert metric_events[0]["status"] == "success"
    assert metric_events[0]["trigger"] == "before_employee_reply"
    assert metric_events[0]["is_compensation"] is True
    assert metric_events[0]["target_turn_index"] == 6
    assert metric_events[0]["summarized_turn_count"] == 6


@pytest.mark.asyncio
async def test_summary_advances_coverage_from_six_to_twelve_at_turn_twenty_four():
    repository = InMemorySummaryRepository(
        ConversationSummaryRecord(
            session_id="summary-session",
            summary_text="first batch",
            covered_through_turn_index=6,
        )
    )
    llm = FakeSummaryLLM(["first and second batches"])
    service = _service(repository, llm)

    assert await service.ensure_scheduled(_state(24)) is True
    await _drain(service)

    assert repository.record is not None
    assert repository.record.covered_through_turn_index == 12
    assert repository.record.summary_text == "first and second batches"
    assert "已有摘要：first batch" in llm.calls[0]["prompt"]
    assert "message-07" in llm.calls[0]["prompt"]
    assert "message-12" in llm.calls[0]["prompt"]
    assert "message-06" not in llm.calls[0]["prompt"]
    assert "message-13" not in llm.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_failed_summary_is_retried_on_the_next_check():
    repository = InMemorySummaryRepository()
    llm = FakeSummaryLLM([RuntimeError("temporary failure"), "recovered summary"])
    service = _service(repository, llm)
    state = _state(18)

    assert await service.ensure_scheduled(state) is True
    await _drain(service)
    assert repository.record is None

    assert await service.ensure_scheduled(state) is True
    await _drain(service)

    assert repository.record is not None
    assert repository.record.covered_through_turn_index == 6
    assert repository.record.summary_text == "recovered summary"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_summary_repository_failure_falls_back_to_full_raw_history():
    service = _service(FailingSummaryRepository())
    state = _state(18)

    context = await service.context_for_reply(state)

    assert context.summary_text == ""
    assert [turn.turn_index for turn in context.raw_turns] == list(range(1, 19))
    assert await service.ensure_scheduled(state) is False
    assert service._tasks == {}


@pytest.mark.asyncio
async def test_reset_generation_rejects_an_old_inflight_summary():
    repository = InMemorySummaryRepository()
    llm = GatedSummaryLLM()
    service = _service(repository, llm)

    assert await service.ensure_scheduled(_state(18)) is True
    await llm.started.wait()
    reset = service.reset_generation("summary-session")
    assert reset.generation == 1

    llm.release.set()
    await _drain(service)

    assert repository.record is not None
    assert repository.record.generation == 1
    assert repository.record.covered_through_turn_index == 0
    assert repository.record.summary_text == ""
    assert repository.save_attempts[0]["generation"] == 0


def test_employee_prompt_uses_summary_and_only_unsummarized_raw_turns():
    from backend.agents.employee_agent import EmployeeAgent

    state = _state(20)
    context = ConversationPromptContext(
        summary_text="前六条对话已经确认了奖金争议。",
        covered_through_turn_index=6,
        raw_turns=state.conversation[6:],
    )

    prompt = EmployeeAgent._build_reply_prompt(
        state,
        "请继续。",
        [],
        conversation_context=context,
    )

    assert "前六条对话已经确认了奖金争议。" in prompt
    assert "message-07" in prompt
    assert "message-20" in prompt
    assert "message-01" not in prompt
    assert len(state.conversation) == 20


def test_postgres_schema_and_repository_use_monotonic_summary_updates():
    statements: list[str] = []

    class Cursor:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Connection:
        def execute(self, sql, params=None):
            statements.append(str(sql))
            return Cursor()

    @contextmanager
    def connection():
        yield Connection()

    postgres = object.__new__(PostgresRepository)
    postgres.connection = connection
    postgres.init_schema()

    class Repository:
        @contextmanager
        def connection(self):
            yield Connection()

    summary_repository = ConversationSummaryRepository(repository=Repository())
    assert (
        summary_repository.save_if_newer(
            session_id="s1",
            generation=0,
            summary_text="summary",
            covered_through_turn_index=6,
            model_name="qwen3.6-flash",
            prompt_version="v1",
        )
        is None
    )

    sql = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS conversation_summaries" in sql
    assert "conversation_summaries.generation < EXCLUDED.generation" in sql
    assert "covered_through_turn_index" in sql
