from __future__ import annotations
import asyncio

from backend.agents.employee_agent import EmployeeAgent
from backend.schemas.conversation import (
    CONVERSATION_TURN_LOCALE_METADATA_KEY,
    ConversationTurn,
    attach_emotion_snapshot,
)
from backend.schemas.state import SessionState
from backend.services.conversation_summary_service import ConversationSummaryService
from backend.services.emotion_transition_service import EmotionTransitionService
from backend.services.employee_state_transition_service import EmployeeStateTransitionService
from backend.services.motivation_scoring_service import MotivationScoringService
from backend.services.retrieval_service import RetrievalService


class RehearsalNodes:
    def __init__(
        self,
        summary_service: ConversationSummaryService | None = None,
        retrieval: RetrievalService | None = None,
        motivation_scoring: MotivationScoringService | None = None,
        emotion_transition: EmotionTransitionService | None = None,
        employee_state_transition: EmployeeStateTransitionService | None = None,
    ):
        self.employee_agent = EmployeeAgent(
            retrieval=retrieval,
            summary_service=summary_service,
        )
        self.motivation_scoring = motivation_scoring or MotivationScoringService()
        self.emotion_transition = emotion_transition or EmotionTransitionService()
        self.employee_state_transition = (
            employee_state_transition
            or EmployeeStateTransitionService(
                emotion_transition=self.emotion_transition,
                summary_service=summary_service,
            )
        )

    async def employee_reply_node(
        self,
        state: SessionState,
        manager_message: str,
    ) -> SessionState:
        next_index = len(state.conversation) + 1
        state.conversation.append(
            ConversationTurn(
                turn_index=next_index,
                speaker="manager",
                text=manager_message,
                metadata={CONVERSATION_TURN_LOCALE_METADATA_KEY: state.locale},
            )
        )
        state.user_turn_count += 1
        pattern_settings = getattr(
            self.employee_state_transition,
            "settings",
            None,
        )
        if not getattr(
            pattern_settings,
            "psychological_pattern_dynamics_enabled",
            True,
        ):
            state = await self.motivation_scoring.update_after_manager_message(
                state,
                manager_message,
            )
            state = await self.emotion_transition.update_after_manager_message(
                state,
                manager_message,
            )
            attach_emotion_snapshot(state.conversation[-1], state.emotion_state)
            reply = await self.employee_agent.reply(state, manager_message)
            state.conversation.append(
                ConversationTurn(
                    turn_index=next_index + 1,
                    speaker="employee",
                    text=reply,
                    metadata={CONVERSATION_TURN_LOCALE_METADATA_KEY: state.locale},
                )
            )
            state.stage = "rehearsal"
            return state


        motivation_input = state.model_copy(deep=True)
        transition_input = state.model_copy(deep=True)
        reply_accepts_preloaded_chunks = EmployeeAgent._accepts_keyword_argument(
            self.employee_agent.reply,
            "retrieved_chunks",
        )
        retrieval_request = (
            self.employee_agent.aretrieve_reply_context(
                state,
                manager_message,
            )
            if reply_accepts_preloaded_chunks
            else asyncio.sleep(0, result=None)
        )
        motivation_state, transition_result, retrieved_chunks = await asyncio.gather(
            self.motivation_scoring.update_after_manager_message(
                motivation_input,
                manager_message,
            ),
            self.employee_state_transition.update_after_manager_message(
                transition_input,
                manager_message,
            ),
            retrieval_request,
        )
        if motivation_state.motivation is not None:
            state.motivation = motivation_state.motivation
        state.emotion_state = transition_result.state.emotion_state
        state.psychological_pattern_state = (
            transition_result.state.psychological_pattern_state
        )
        state.warnings = list(
            dict.fromkeys(
                [
                    *state.warnings,
                    *motivation_state.warnings,
                    *transition_result.state.warnings,
                ]
            )
        )
        attach_emotion_snapshot(state.conversation[-1], state.emotion_state)
        reply_kwargs: dict[str, object] = {}
        if retrieved_chunks is not None:
            reply_kwargs["retrieved_chunks"] = retrieved_chunks
        if (
            transition_result.pattern_response_guidance
            and EmployeeAgent._accepts_pattern_response_guidance(
                self.employee_agent.reply
            )
        ):
            reply_kwargs["pattern_response_guidance"] = (
                transition_result.pattern_response_guidance
            )
        reply = await self.employee_agent.reply(
            state,
            manager_message,
            **reply_kwargs,
        )
        state.conversation.append(
            ConversationTurn(
                turn_index=next_index + 1,
                speaker="employee",
                text=reply,
                metadata={CONVERSATION_TURN_LOCALE_METADATA_KEY: state.locale},
            )
        )
        state.stage = "rehearsal"
        return state
