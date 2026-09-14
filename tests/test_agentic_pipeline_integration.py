from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agents.guidance_agent import GUIDANCE_DIMENSION_SPECS
from backend.schemas.retrieval import RetrievedChunk
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult
from backend.services.coach_service import CoachService
from backend.services.guidance_service import GuidanceService


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=f"{chunk_id}.md",
        title=chunk_id,
        scope="general",
        text=f"text {chunk_id}",
        score=0.9,
    )


class _Retrieval:
    async def aretrieve(self, agent_name, context, top_k=None):
        return [_chunk(f"baseline-{agent_name}")]


class _Planner:
    def __init__(self):
        self.calls = []

    async def enhance(self, **kwargs):
        self.calls.append(kwargs)
        return [*kwargs["initial_chunks"], _chunk("adaptive-added")]


@pytest.mark.asyncio
async def test_guidance_uses_adaptive_evidence_after_baseline_retrieval():
    planner = _Planner()
    service = object.__new__(GuidanceService)
    service.retrieval = _Retrieval()
    service.agentic_evidence = planner
    service.config_loader = SimpleNamespace(company_value_terms=lambda: "")
    generated = {}

    async def generate_dimension(session_id, state, chunks, spec):
        generated["chunks"] = chunks
        return spec, {}, 1, None

    service._generate_dimension = generate_dimension
    state = SessionState(
        session_id="guidance-agentic",
        setup_ready=True,
        supplemental_info="完整补充信息",
    )
    spec = GUIDANCE_DIMENSION_SPECS[0]

    run = await service._run_dimension_pipeline(
        state.session_id,
        state,
        spec,
    )

    assert [chunk.chunk_id for chunk in generated["chunks"]] == [
        f"baseline-{spec.retrieval_name}",
        "adaptive-added",
    ]
    assert [chunk.chunk_id for chunk in run.chunks] == [
        f"baseline-{spec.retrieval_name}",
        "adaptive-added",
    ]
    call = planner.calls[0]
    assert call["flow"] == "guidance"
    assert call["retrieval_name"] == spec.retrieval_name
    assert call["context"]["supplemental_info"] == "完整补充信息"
    assert "conversation" not in call["context"]
    assert "emotion_state" not in call["context"]


@pytest.mark.asyncio
async def test_coach_uses_adaptive_evidence_before_unchanged_task_call():
    planner = _Planner()
    service = object.__new__(CoachService)
    service.retrieval = _Retrieval()
    service.agentic_evidence = planner
    generated = {}

    async def run_task(state, chunks, task_id, task_name):
        generated["chunks"] = chunks
        return (
            task_id,
            task_name,
            CoachTaskResult(
                task_id=task_id,
                task_name=task_name,
                summary="完成",
            ),
            None,
            1,
        )

    service._run_task = run_task
    state = SessionState(session_id="coach-agentic", setup_ready=True)
    context = {
        "conversation": [
            {"turn_index": 1, "speaker": "manager", "text": "绩效事实"}
        ],
        "profile": {"role": "Engineer"},
    }

    outcome = await service._run_task_pipeline(
        state.session_id,
        state,
        context,
        "opening_evaluation",
        "开场与目的",
    )

    assert outcome.error is None
    assert [chunk.chunk_id for chunk in generated["chunks"]] == [
        "baseline-opening_evaluation",
        "adaptive-added",
    ]
    call = planner.calls[0]
    assert call["flow"] == "coach"
    assert call["dimension"] == "opening_evaluation"
    assert call["context"] is context
    assert call["context"]["conversation"][0]["speaker"] == "manager"
