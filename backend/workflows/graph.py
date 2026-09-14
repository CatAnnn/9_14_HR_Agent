from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.schemas.state import SessionState
from backend.workflows.guards import ensure_can_add_user_turn, ensure_setup_ready
from backend.services.conversation_summary_service import ConversationSummaryService
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.employee_state_transition_service import EmployeeStateTransitionService
from backend.services.motivation_scoring_service import MotivationScoringService
from backend.services.retrieval_service import RetrievalService
from backend.workflows.nodes import RehearsalNodes


class RehearsalGraphState(TypedDict):
    """LangGraph state for one manager-message rehearsal turn."""

    state: SessionState
    manager_message: str


class RehearsalWorkflow:
    """LangGraph-backed workflow for one rehearsal turn."""

    def __init__(
        self,
        summary_service: ConversationSummaryService | None = None,
        retrieval: RetrievalService | None = None,
        motivation_scoring: MotivationScoringService | None = None,
        emotion_transition: EmotionTransitionService | None = None,
        employee_state_transition: EmployeeStateTransitionService | None = None,
    ):
        self.nodes = RehearsalNodes(
            summary_service=summary_service,
            retrieval=retrieval,
            motivation_scoring=motivation_scoring,
            emotion_transition=emotion_transition,
            employee_state_transition=employee_state_transition,
        )
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(RehearsalGraphState)
        builder.add_node("validate_turn", self._validate_turn_node)
        builder.add_node("employee_reply", self._employee_reply_node)
        builder.add_edge(START, "validate_turn")
        builder.add_edge("validate_turn", "employee_reply")
        builder.add_edge("employee_reply", END)
        return builder.compile()

    async def invoke(self, state: SessionState, inputs: dict[str, Any]) -> SessionState:
        message = str(inputs.get("manager_message") or "").strip()
        if not message:
            raise ValueError("manager_message is required")
        result = await self.graph.ainvoke({"state": state, "manager_message": message})
        return result["state"]

    async def _validate_turn_node(self, graph_state: RehearsalGraphState) -> dict[str, SessionState]:
        state = graph_state["state"]
        ensure_setup_ready(state)
        ensure_can_add_user_turn(state)
        return {"state": state}

    async def _employee_reply_node(self, graph_state: RehearsalGraphState) -> dict[str, SessionState]:
        state = await self.nodes.employee_reply_node(
            graph_state["state"],
            graph_state["manager_message"],
        )
        return {"state": state}
