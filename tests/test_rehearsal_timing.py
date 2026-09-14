from __future__ import annotations

from backend.observability import rehearsal_timing as timing_module


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_rehearsal_turn_timing_preserves_parallel_waterfall(monkeypatch) -> None:
    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        timing_module,
        "get_current_observability_user_id",
        lambda: "captured-user",
    )
    monkeypatch.setattr(
        timing_module,
        "record_rehearsal_turn_timing",
        lambda **fields: recorded.update(fields),
    )
    clock = FakeClock()
    timing = timing_module.RehearsalTurnTiming(
        session_id="private-session-id",
        transport="stream",
        speech_enabled=False,
        explicit_thinking_enabled=False,
        attempt_id="attempt-1",
        clock=clock,
    )
    timing.set_turn_indexes(manager_turn_index=3, employee_turn_index=4)

    motivation = timing.start_stage(
        "motivation_scoring",
        parallel_group="analysis",
    )
    retrieval = timing.start_stage(
        "knowledge_retrieval",
        parallel_group="analysis",
    )
    clock.advance(0.1)
    timing.finish_stage(motivation)
    clock.advance(0.1)
    timing.finish_stage(retrieval)
    timing.mark_milestone("generating")
    clock.advance(0.05)
    timing.mark_milestone("first_visible_delta")
    clock.advance(0.1)
    timing.mark_milestone("text_complete")
    clock.advance(0.05)

    monkeypatch.setattr(
        timing_module,
        "get_current_observability_user_id",
        lambda: "changed-user",
    )
    payload = timing.finalize("success")
    summary = payload["summary_ms"]
    stages = {stage["name"]: stage for stage in payload["stages"]}

    assert summary == {
        "total_ms": 400,
        "analysis_and_preparation_ms": 200,
        "wait_for_first_visible_reply_ms": 250,
        "generation_to_first_visible_ms": 50,
        "text_total_ms": 350,
        "postprocess_ms": 50,
        "visible_reply_stream_ms": 100,
    }
    assert stages["motivation_scoring"]["start_offset_ms"] == 0
    assert stages["knowledge_retrieval"]["start_offset_ms"] == 0
    assert stages["motivation_scoring"]["duration_ms"] == 100
    assert stages["knowledge_retrieval"]["duration_ms"] == 200
    assert sum(stage["duration_ms"] for stage in stages.values()) == 300
    assert summary["analysis_and_preparation_ms"] == 200
    assert payload["explicit_thinking_ms"] is None
    assert recorded["user_id"] == "captured-user"
    assert recorded["session_hash"] != "private-session-id"
    assert len(str(recorded["session_hash"])) == 16


def test_rehearsal_turn_timing_finalizes_only_once(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        timing_module,
        "record_rehearsal_turn_timing",
        lambda **fields: calls.append(fields),
    )
    clock = FakeClock()
    timing = timing_module.RehearsalTurnTiming(
        session_id="s1",
        transport="stream",
        speech_enabled=True,
        explicit_thinking_enabled=False,
        clock=clock,
    )
    clock.advance(0.1)

    first = timing.finalize("error")
    second = timing.finalize("success")

    assert first is second
    assert first["outcome"] == "error"
    assert len(calls) == 1
