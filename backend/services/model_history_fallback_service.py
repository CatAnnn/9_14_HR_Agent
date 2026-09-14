from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
import unicodedata

from backend.config.settings import Settings, get_settings
from backend.repositories.model_history_repository import (
    HistoricalSessionRecord,
    ModelHistoryRepository,
)
from backend.schemas.coach import CoachReport
from backend.schemas.conversation import (
    CONVERSATION_TURN_LOCALE_METADATA_KEY,
    ConversationTurn,
)
from backend.schemas.guidance import GuidanceReport
from backend.schemas.locale import SessionLocale
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.schemas.state import SessionState
from backend.schemas.task import CoachTaskResult


HISTORY_FALLBACK_METADATA_KEY = "model_history_fallback"
GUIDANCE_HISTORY_WARNING = (
    "谈前指导模型重试失败，已使用同一员工和沟通意图的历史相似记录兜底。"
)
COACH_HISTORY_WARNING = (
    "复盘报告模型重试失败，已使用同一员工、沟通意图且证据可映射的历史相似记录兜底。"
)
EMPLOYEE_REPLY_HISTORY_WARNING = (
    "员工回复模型重试失败，已使用同一员工和沟通意图的历史相似对话兜底。"
)
_COACH_TASK_IDS = {
    "opening_evaluation",
    "emotion_evaluation",
    "output_expectations_evaluation",
    "development_plan_evaluation",
}


@dataclass(frozen=True, slots=True)
class HistoricalGuidanceMatch:
    report: GuidanceReport
    source_session_id: str
    score: float
    personality_similarity: float
    motive_similarity: float
    context_similarity: float


@dataclass(frozen=True, slots=True)
class HistoricalCoachTaskMatch:
    result: CoachTaskResult
    source_session_id: str
    score: float
    conversation_similarity: float


@dataclass(frozen=True, slots=True)
class HistoricalEmployeeReplyMatch:
    reply: str
    source_session_id: str
    source_manager_turn_index: int
    source_employee_turn_index: int
    score: float
    manager_message_similarity: float


@dataclass(frozen=True, slots=True)
class _SessionSimilarity:
    score: float
    personality: float
    motive: float
    context: float


class ModelHistoryFallbackService:
    """Select task-compatible historical results without another model call."""

    _BIG_FIVE_FIELDS = (
        "openness",
        "conscientiousness",
        "extraversion",
        "agreeableness",
        "neuroticism",
    )

    def __init__(
        self,
        *,
        repository: ModelHistoryRepository | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.repository = repository or ModelHistoryRepository()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.model_history_fallback_enabled)

    def find_guidance(
        self,
        state: SessionState,
        *,
        guidance_version: str,
        culture_version: str,
        dimension_key: str | None = None,
    ) -> HistoricalGuidanceMatch | None:
        if not self.enabled:
            return None
        matches: list[HistoricalGuidanceMatch] = []
        for record in self._records(
            state,
            guidance_version=guidance_version,
            culture_version=culture_version,
        ):
            report = record.guidance_report
            if report is None:
                continue
            if report.locale != state.locale:
                continue
            if dimension_key is not None and not self._has_guidance_dimension(
                report,
                dimension_key,
            ):
                continue
            if report.intent_id != self._intent_id(state):
                continue
            if report.guidance_version != guidance_version:
                continue
            if report.culture_version != culture_version:
                continue
            candidate_state = self._state_for_report_locale(
                record.guidance_state or record.state,
                report.locale,
            )
            if candidate_state is None:
                continue
            similarity = self._session_similarity(
                state,
                candidate_state,
                candidate_primary_motive_id=report.primary_motive_id,
                candidate_secondary_motive_ids=report.secondary_motive_ids,
            )
            if similarity is None:
                continue
            adapted = self._adapt_guidance_report(
                report,
                state,
                guidance_version=guidance_version,
                culture_version=culture_version,
            )
            matches.append(
                HistoricalGuidanceMatch(
                    report=adapted,
                    source_session_id=record.state.session_id,
                    score=similarity.score,
                    personality_similarity=similarity.personality,
                    motive_similarity=similarity.motive,
                    context_similarity=similarity.context,
                )
            )
        return max(
            matches,
            key=lambda item: item.score,
            default=None,
        )

    @staticmethod
    def _has_guidance_dimension(
        report: GuidanceReport,
        dimension_key: str,
    ) -> bool:
        if dimension_key not in {"start", "emotion", "requirement", "plan"}:
            return False
        dimension_points = report.dimension_points
        return bool(
            dimension_points is not None
            and getattr(dimension_points, dimension_key, None)
        )

    @staticmethod
    def _coach_task_result_complete(result: CoachTaskResult) -> bool:
        if result.status == "insufficient_information":
            return True
        if result.status != "success" or result.score not in {1, 2, 3, 4, 5}:
            return False
        return not (
            result.score in {1, 2, 3}
            and not result.improvement_points
        )

    def find_coach_task(
        self,
        state: SessionState,
        *,
        task_id: str,
        coach_version: str,
    ) -> HistoricalCoachTaskMatch | None:
        if not self.enabled:
            return None
        matches: list[HistoricalCoachTaskMatch] = []
        current_manager_text = self._speaker_text(state, "manager")
        current_employee_text = self._speaker_text(state, "employee")
        current_ordered_text = self._ordered_conversation_text(state)
        if not current_manager_text:
            return None

        for record in self._records(state, coach_version=coach_version):
            report = record.coach_report
            if report is None or report.coach_version != coach_version:
                continue
            if report.locale != state.locale:
                continue
            results_by_id = {
                item.task_id: item
                for item in report.task_results
            }
            if set(results_by_id) != _COACH_TASK_IDS or any(
                not self._coach_task_result_complete(item)
                for item in results_by_id.values()
            ):
                continue
            result = next(
                (
                    item
                    for item in report.task_results
                    if item.task_id == task_id
                    and self._coach_task_result_complete(item)
                ),
                None,
            )
            if result is None:
                continue
            candidate_state = self._state_for_report_locale(
                record.coach_state or record.state,
                report.locale,
            )
            if candidate_state is None:
                continue
            session_similarity = self._session_similarity(state, candidate_state)
            if session_similarity is None:
                continue
            manager_similarity = self.text_similarity(
                current_manager_text,
                self._speaker_text(candidate_state, "manager"),
            )
            ordered_similarity = self.text_similarity(
                current_ordered_text,
                self._ordered_conversation_text(candidate_state),
            )
            if (
                manager_similarity
                < self.settings.model_history_fallback_min_conversation_similarity
                or ordered_similarity < 0.72
            ):
                continue
            employee_similarity = self.text_similarity(
                current_employee_text,
                self._speaker_text(candidate_state, "employee"),
            )
            evidence_backed = self._coach_result_has_mappable_evidence(result)
            # A scored historical evaluation must carry at least one Manager
            # utterance that can be mapped onto the current transcript.  An
            # insufficient-information result has no positive claim to map,
            # but is reusable only for a near-identical (not byte-identical)
            # dialogue shape.
            if result.status == "success" and not evidence_backed:
                continue
            if result.status == "insufficient_information" and (
                manager_similarity < 0.97 or ordered_similarity < 0.95
            ):
                continue
            conversation_similarity = (
                0.50 * manager_similarity
                + 0.20 * employee_similarity
                + 0.20 * ordered_similarity
                + 0.10 * session_similarity.context
            )
            score = (
                0.70 * conversation_similarity
                + 0.18 * session_similarity.personality
                + 0.12 * session_similarity.motive
            )
            if score < 0.85:
                continue
            adapted = self._adapt_coach_task(result, state.conversation)
            if adapted is None:
                continue
            matches.append(
                HistoricalCoachTaskMatch(
                    result=adapted,
                    source_session_id=record.state.session_id,
                    score=score,
                    conversation_similarity=conversation_similarity,
                )
            )
        return max(
            matches,
            key=lambda item: item.score,
            default=None,
        )

    def find_employee_reply(
        self,
        state: SessionState,
        manager_message: str,
    ) -> HistoricalEmployeeReplyMatch | None:
        if not self.enabled:
            return None
        normalized_query = self._normalize_text(manager_message)
        if len(normalized_query) < 8:
            return None

        current_manager_position = self._latest_manager_position(
            state.conversation,
            manager_message,
        )
        current_local_context = self._local_context_text(
            state.conversation,
            current_manager_position,
        )
        current_employee_replies = [
            turn.text
            for turn in state.conversation
            if turn.speaker == "employee" and turn.text.strip()
        ]

        candidate_sessions: list[tuple[SessionState, float]] = [(state, 1.0)]
        for record in self._records(state):
            similarity = self._session_similarity(state, record.state)
            if similarity is not None:
                candidate_sessions.append((record.state, similarity.score))

        matches: list[HistoricalEmployeeReplyMatch] = []
        for candidate_state, session_score in candidate_sessions:
            for manager_position, manager_turn, employee_turn in self._successful_reply_pairs(
                candidate_state.conversation,
                locale=state.locale,
            ):
                message_similarity = self.text_similarity(
                    manager_message,
                    manager_turn.text,
                )
                if (
                    message_similarity
                    < self.settings.model_history_fallback_min_reply_similarity
                ):
                    continue
                candidate_local_context = self._local_context_text(
                    candidate_state.conversation,
                    manager_position,
                )
                local_context_similarity = self.text_similarity(
                    current_local_context,
                    candidate_local_context,
                )
                if (
                    current_local_context
                    and candidate_local_context
                    and local_context_similarity < 0.45
                ):
                    continue
                if any(
                    self.text_similarity(employee_turn.text, existing_reply) >= 0.96
                    for existing_reply in current_employee_replies
                ):
                    continue
                combined_score = (
                    0.60 * message_similarity
                    + 0.20 * local_context_similarity
                    + 0.20 * session_score
                )
                matches.append(
                    HistoricalEmployeeReplyMatch(
                        reply=employee_turn.text.strip(),
                        source_session_id=candidate_state.session_id,
                        source_manager_turn_index=manager_turn.turn_index,
                        source_employee_turn_index=employee_turn.turn_index,
                        score=combined_score,
                        manager_message_similarity=message_similarity,
                    )
                )
        return max(
            matches,
            key=lambda item: (
                item.score,
                item.source_session_id == state.session_id,
                item.source_employee_turn_index,
                item.source_session_id,
            ),
            default=None,
        )

    def _records(
        self,
        state: SessionState,
        *,
        guidance_version: str | None = None,
        culture_version: str | None = None,
        coach_version: str | None = None,
    ) -> list[HistoricalSessionRecord]:
        return self.repository.list_candidates(
            state,
            limit=self.settings.model_history_fallback_max_sessions,
            max_age_days=self.settings.model_history_fallback_max_age_days,
            guidance_version=guidance_version,
            culture_version=culture_version,
            coach_version=coach_version,
        )

    @staticmethod
    def _state_for_report_locale(
        candidate: SessionState,
        report_locale: SessionLocale,
    ) -> SessionState | None:
        """Bind legacy snapshots to their report locale without hiding conflicts."""
        if "locale" not in candidate.model_fields_set:
            return candidate.model_copy(update={"locale": report_locale})
        if candidate.locale != report_locale:
            return None
        return candidate

    def _session_similarity(
        self,
        current: SessionState,
        candidate: SessionState,
        *,
        candidate_primary_motive_id: str | None = None,
        candidate_secondary_motive_ids: list[str] | None = None,
    ) -> _SessionSimilarity | None:
        if current.locale != candidate.locale:
            return None
        if not self._same_employee_and_intent(current, candidate):
            return None
        personality = self.personality_similarity(
            current.personality,
            candidate.personality,
        )
        if personality is None:
            return None
        motive = self.motive_similarity(
            current.motivation,
            candidate.motivation,
            candidate_primary_motive_id=candidate_primary_motive_id,
            candidate_secondary_motive_ids=candidate_secondary_motive_ids,
        )
        if motive is None or motive < 0.50:
            return None
        context = self.context_similarity(current, candidate)
        score = 0.55 * context + 0.25 * personality + 0.20 * motive
        if score < self.settings.model_history_fallback_min_session_similarity:
            return None
        return _SessionSimilarity(
            score=score,
            personality=personality,
            motive=motive,
            context=context,
        )

    @classmethod
    def personality_similarity(
        cls,
        current: BigFivePersonality | None,
        candidate: BigFivePersonality | None,
    ) -> float | None:
        if current is None or candidate is None:
            return None
        differences = [
            abs(float(getattr(current, field)) - float(getattr(candidate, field)))
            for field in cls._BIG_FIVE_FIELDS
        ]
        if max(differences, default=0.0) > 35.0:
            return None
        similarity = 1.0 - sum(differences) / (100.0 * len(differences))
        return similarity if similarity >= 0.75 else None

    @staticmethod
    def motive_similarity(
        current: MotivationState | None,
        candidate: MotivationState | None,
        *,
        candidate_primary_motive_id: str | None = None,
        candidate_secondary_motive_ids: list[str] | None = None,
    ) -> float | None:
        if current is None:
            return None
        if candidate is None and not candidate_primary_motive_id:
            return None
        current_weights = ModelHistoryFallbackService._motive_weights(
            current.primary_motive_id,
            current.secondary_motive_ids,
        )
        primary_id = candidate_primary_motive_id or (
            candidate.primary_motive_id if candidate is not None else ""
        )
        secondary_ids = (
            candidate_secondary_motive_ids
            if candidate_secondary_motive_ids is not None
            else (candidate.secondary_motive_ids if candidate is not None else [])
        )
        candidate_weights = ModelHistoryFallbackService._motive_weights(
            primary_id,
            secondary_ids,
        )
        if not current_weights or not candidate_weights:
            return None
        motive_ids = set(current_weights) | set(candidate_weights)
        numerator = sum(
            min(current_weights.get(key, 0.0), candidate_weights.get(key, 0.0))
            for key in motive_ids
        )
        denominator = sum(
            max(current_weights.get(key, 0.0), candidate_weights.get(key, 0.0))
            for key in motive_ids
        )
        return numerator / denominator if denominator else None

    @staticmethod
    def _motive_weights(primary_id: str, secondary_ids: list[str]) -> dict[str, float]:
        primary = str(primary_id or "").strip()
        secondary = list(
            dict.fromkeys(
                str(item).strip()
                for item in secondary_ids
                if str(item).strip() and str(item).strip() != primary
            )
        )[:2]
        if not primary:
            return {}
        if not secondary:
            return {primary: 1.0}
        weights = {primary: 0.70}
        per_secondary = 0.30 / len(secondary)
        for motive_id in secondary:
            weights[motive_id] = per_secondary
        return weights

    @classmethod
    def context_similarity(
        cls,
        current: SessionState,
        candidate: SessionState,
    ) -> float:
        current_parts = cls._context_parts(current)
        candidate_parts = cls._context_parts(candidate)
        weighted = (
            (0.45, current_parts[0], candidate_parts[0]),
            (0.30, current_parts[1], candidate_parts[1]),
            (0.10, current_parts[2], candidate_parts[2]),
        )
        numerator = 0.0
        denominator = 0.0
        for weight, left, right in weighted:
            if not left and not right:
                continue
            numerator += weight * cls.text_similarity(left, right)
            denominator += weight
        categorical = cls._categorical_similarity(current, candidate)
        numerator += 0.15 * categorical
        denominator += 0.15
        context = numerator / denominator if denominator else 0.0
        performance_similarity = cls.text_similarity(
            current_parts[0],
            candidate_parts[0],
        )
        if current_parts[0] and candidate_parts[0] and performance_similarity < 0.60:
            return 0.0
        return context

    @staticmethod
    def _context_parts(state: SessionState) -> tuple[str, str, str]:
        profile = state.employee_profile
        performance_items = state.intent.performance_items if state.intent else []
        performance = "\n".join(
            f"{item.goal}\n{item.current_performance}"
            for item in performance_items
        )
        if not performance and state.intent is not None:
            performance = str(state.intent.performance_context or "")
        evidence_parts: list[str] = []
        if profile is not None:
            evidence_parts.extend(profile.key_goals)
            evidence_parts.extend(profile.historical_feedback)
            evidence_parts.extend(profile.management_actions)
            for fact in profile.facts:
                evidence_parts.extend(
                    value
                    for value in (
                        fact.description,
                        fact.impact,
                        fact.evidence_source,
                    )
                    if value
                )
        supplemental = str(state.supplemental_info or "")
        return performance, "\n".join(evidence_parts), supplemental

    @staticmethod
    def _categorical_similarity(
        current: SessionState,
        candidate: SessionState,
    ) -> float:
        current_profile = current.employee_profile
        candidate_profile = candidate.employee_profile
        if current_profile is None or candidate_profile is None:
            return 0.0
        fields = (
            "role",
            "department",
            "level",
            "performance_rating",
            "review_cycle",
        )
        scores: list[float] = []
        for field in fields:
            left = str(getattr(current_profile, field) or "").strip().casefold()
            right = str(getattr(candidate_profile, field) or "").strip().casefold()
            if not left and not right:
                continue
            scores.append(1.0 if left and left == right else 0.0)
        left_career = set(current_profile.current_career_elements)
        right_career = set(candidate_profile.current_career_elements)
        if left_career or right_career:
            scores.append(
                len(left_career & right_career) / len(left_career | right_career)
            )
        return sum(scores) / len(scores) if scores else 0.5

    @classmethod
    def text_similarity(cls, left: str, right: str) -> float:
        left_normalized = cls._normalize_text(left)
        right_normalized = cls._normalize_text(right)
        if not left_normalized and not right_normalized:
            return 1.0
        if not left_normalized or not right_normalized:
            return 0.0
        if left_normalized == right_normalized:
            return 1.0
        left_counts = Counter(cls._ngrams(left_normalized))
        right_counts = Counter(cls._ngrams(right_normalized))
        dot_product = sum(
            count * right_counts.get(token, 0)
            for token, count in left_counts.items()
        )
        left_norm = math.sqrt(sum(count * count for count in left_counts.values()))
        right_norm = math.sqrt(sum(count * count for count in right_counts.values()))
        if not left_norm or not right_norm:
            return 0.0
        return dot_product / (left_norm * right_norm)

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @staticmethod
    def _ngrams(value: str) -> list[str]:
        width = 2 if len(value) >= 2 else 1
        return [value[index:index + width] for index in range(len(value) - width + 1)]

    @staticmethod
    def _same_employee_and_intent(
        current: SessionState,
        candidate: SessionState,
    ) -> bool:
        current_employee = str(
            current.employee_profile.employee_id
            if current.employee_profile is not None
            else ""
        ).strip()
        candidate_employee = str(
            candidate.employee_profile.employee_id
            if candidate.employee_profile is not None
            else ""
        ).strip()
        return bool(
            current_employee
            and current_employee == candidate_employee
            and ModelHistoryFallbackService._intent_id(current)
            == ModelHistoryFallbackService._intent_id(candidate)
            and ModelHistoryFallbackService._intent_id(current)
        )

    @staticmethod
    def _intent_id(state: SessionState) -> str:
        return str(state.intent.intent_id if state.intent is not None else "").strip()

    @staticmethod
    def _speaker_text(state: SessionState, speaker: str) -> str:
        return "\n".join(
            turn.text.strip()
            for turn in state.conversation
            if turn.speaker == speaker and turn.text.strip()
        )

    @staticmethod
    def _ordered_conversation_text(state: SessionState) -> str:
        return "\n".join(
            f"{turn.speaker}:{turn.text.strip()}"
            for turn in state.conversation
            if turn.speaker in {"manager", "employee"} and turn.text.strip()
        )

    @staticmethod
    def _successful_reply_pairs(
        conversation: list[ConversationTurn],
        *,
        locale: SessionLocale,
    ) -> list[tuple[int, ConversationTurn, ConversationTurn]]:
        pairs: list[tuple[int, ConversationTurn, ConversationTurn]] = []
        for index, manager_turn in enumerate(conversation[:-1]):
            if manager_turn.speaker != "manager" or not manager_turn.text.strip():
                continue
            if (manager_turn.metadata or {}).get(HISTORY_FALLBACK_METADATA_KEY):
                continue
            employee_turn = conversation[index + 1]
            if employee_turn.speaker != "employee" or not employee_turn.text.strip():
                continue
            if (employee_turn.metadata or {}).get(HISTORY_FALLBACK_METADATA_KEY):
                continue
            if not all(
                ModelHistoryFallbackService._turn_locale_matches(
                    turn,
                    locale=locale,
                )
                for turn in (manager_turn, employee_turn)
            ):
                continue
            pairs.append((index, manager_turn, employee_turn))
        return pairs

    @staticmethod
    def _turn_locale_matches(
        turn: ConversationTurn,
        *,
        locale: SessionLocale,
    ) -> bool:
        turn_locale = (turn.metadata or {}).get(
            CONVERSATION_TURN_LOCALE_METADATA_KEY
        )
        if turn_locale is None:
            # Historical turns predate locale metadata and were generated under
            # the legacy Chinese-only contract. Never reinterpret them as a
            # German, Japanese, or English reply.
            return locale == "zh-CN"
        return turn_locale == locale

    @staticmethod
    def _latest_manager_position(
        conversation: list[ConversationTurn],
        manager_message: str,
    ) -> int:
        expected = manager_message.strip()
        for index in range(len(conversation) - 1, -1, -1):
            turn = conversation[index]
            if turn.speaker == "manager" and turn.text.strip() == expected:
                return index
        return len(conversation)

    @staticmethod
    def _local_context_text(
        conversation: list[ConversationTurn],
        manager_position: int,
        *,
        window: int = 4,
    ) -> str:
        start = max(0, manager_position - window)
        return "\n".join(
            f"{turn.speaker}:{turn.text.strip()}"
            for turn in conversation[start:manager_position]
            if turn.speaker in {"manager", "employee"} and turn.text.strip()
        )

    @classmethod
    def mark_employee_reply_fallback(
        cls,
        state: SessionState,
        manager_message: str,
        match: HistoricalEmployeeReplyMatch,
    ) -> None:
        for turn in reversed(state.conversation):
            if turn.speaker != "manager":
                continue
            if turn.text.strip() != manager_message.strip():
                continue
            turn.metadata = {
                **(turn.metadata or {}),
                HISTORY_FALLBACK_METADATA_KEY: {
                    "artifact": "employee_reply",
                    "score": round(match.score, 4),
                },
            }
            break
        if EMPLOYEE_REPLY_HISTORY_WARNING not in state.warnings:
            state.warnings.append(EMPLOYEE_REPLY_HISTORY_WARNING)

    @classmethod
    def _adapt_guidance_report(
        cls,
        report: GuidanceReport,
        state: SessionState,
        *,
        guidance_version: str,
        culture_version: str,
    ) -> GuidanceReport:
        payload = cls._strip_stale_references(report.model_dump(mode="json"))
        payload.update(
            {
                "session_id": state.session_id,
                "locale": state.locale,
                "intent_id": cls._intent_id(state),
                "guidance_version": guidance_version,
                "culture_version": culture_version,
                "primary_motive_id": (
                    state.motivation.primary_motive_id
                    if state.motivation is not None
                    else None
                ),
                "secondary_motive_ids": (
                    list(state.motivation.secondary_motive_ids)
                    if state.motivation is not None
                    else []
                ),
                "citations": [],
            }
        )
        return GuidanceReport.model_validate(payload)

    @classmethod
    def _adapt_coach_task(
        cls,
        result: CoachTaskResult,
        conversation: list[ConversationTurn],
    ) -> CoachTaskResult | None:
        manager_turns = [
            turn
            for turn in conversation
            if turn.speaker == "manager" and turn.text.strip()
        ]

        def mapped_turn(quote: str) -> ConversationTurn | None:
            normalized_quote = cls._normalize_quote(quote)
            if not normalized_quote:
                return None
            matches = [
                turn
                for turn in manager_turns
                if normalized_quote in cls._normalize_quote(turn.text)
            ]
            return matches[0] if len(matches) == 1 else None

        payload = result.model_dump(mode="json")
        for evidence in payload.get("evidence", []):
            turn = mapped_turn(str(evidence.get("quote") or ""))
            if turn is None:
                return None
            evidence["turn_index"] = turn.turn_index
            evidence["speaker"] = "manager"
        for dimension in payload.get("dimension_scores", []):
            for evidence in dimension.get("evidence", []):
                turn = mapped_turn(str(evidence.get("quote") or ""))
                if turn is None:
                    return None
                evidence["turn_index"] = turn.turn_index
                evidence["speaker"] = "manager"
        for phrase in payload.get("better_phrases", []):
            original = str(phrase.get("original") or "").strip()
            if original and mapped_turn(original) is None:
                return None
        for risk in payload.get("risks", []):
            matched_text = str(risk.get("matched_text") or "").strip()
            if matched_text and mapped_turn(matched_text) is None:
                return None
        payload = cls._strip_stale_references(payload)
        payload["citations"] = []
        # Career Elements advice is specific to the historical employee context
        # and knowledge version, so it is not carried into another session.
        payload["career_elements_advice"] = []
        return CoachTaskResult.model_validate(payload)

    @staticmethod
    def _coach_result_has_mappable_evidence(result: CoachTaskResult) -> bool:
        if result.evidence:
            return True
        if any(dimension.evidence for dimension in result.dimension_scores):
            return True
        if any(phrase.original for phrase in result.better_phrases):
            return True
        return any(risk.matched_text for risk in result.risks)

    @classmethod
    def _strip_stale_references(cls, value: object) -> object:
        if isinstance(value, list):
            return [cls._strip_stale_references(item) for item in value]
        if not isinstance(value, dict):
            return value
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if (
                key == "citations"
                or key == "knowledge_chunk_ids"
                or key.endswith("_knowledge_chunk_ids")
                or key.endswith("_citation_refs")
            ):
                cleaned[key] = []
            else:
                cleaned[key] = cls._strip_stale_references(item)
        return cleaned

    @staticmethod
    def _normalize_quote(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()
