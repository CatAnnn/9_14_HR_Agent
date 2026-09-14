from __future__ import annotations

from datetime import datetime, timezone

from backend.business_config.loader import get_config_loader
from backend.repositories.session_repository import SessionRepository
from backend.schemas.api import EmployeeLatestSetupSettingsResponse
from backend.schemas.state import (
    REHEARSAL_DIMENSION_COVERAGE_VERSION,
    SessionState,
)
from backend.schemas.locale import SessionLocale
from backend.services.local_model_runtime import LocalModelRuntimeRecycler, get_local_model_runtime_recycler


class SessionService:
    def __init__(
        self,
        repo: SessionRepository | None = None,
        runtime_recycler: LocalModelRuntimeRecycler | None = None,
    ):
        self.repo = repo or SessionRepository()
        self.runtime_recycler = runtime_recycler or get_local_model_runtime_recycler()

    def create_session(self, *, locale: SessionLocale = "zh-CN") -> SessionState:
        return self.repo.create(locale=locale)

    def update_locale(
        self,
        session_id: str,
        *,
        locale: SessionLocale,
    ) -> SessionState:
        state = self.get_session(session_id)
        if state.locale == locale:
            return state
        state.locale = locale
        # Reports contain generated prose and must never be reused across locales.
        state.guidance_report_id = None
        state.coach_report_id = None
        if state.stage == "report_ready":
            state.stage = "rehearsal" if state.conversation else "setup_ready"
        elif state.stage == "guidance_ready":
            state.stage = "setup_ready"
        return self.save_session(state)

    def get_session(self, session_id: str) -> SessionState:
        return self._normalize_state(self.repo.get(session_id))

    def save_session(self, state: SessionState) -> SessionState:
        return self.repo.save(self._normalize_state(state))

    def get_latest_employee_setup_settings(
        self,
        employee_id: str,
    ) -> EmployeeLatestSetupSettingsResponse:
        normalized_employee_id = str(employee_id or "").strip()
        settings = self.repo.get_latest_employee_setup(normalized_employee_id)
        if settings is None:
            return EmployeeLatestSetupSettingsResponse(
                employee_id=normalized_employee_id,
            )

        primary_motive_id = (
            settings.motivation.primary_motive_id
            if settings.motivation
            else None
        )
        secondary_motive_ids = []
        if settings.motivation:
            secondary_motive_ids = [
                motive_id
                for motive_id in settings.motivation.secondary_motive_ids
                if motive_id and motive_id != primary_motive_id
            ][:2]

        return EmployeeLatestSetupSettingsResponse(
            employee_id=normalized_employee_id,
            found=True,
            supplemental_info=self._profile_supplemental_input(
                settings.supplemental_info
            ),
            personality=(
                settings.personality.model_copy(deep=True)
                if settings.personality
                else None
            ),
            primary_motive_id=primary_motive_id,
            secondary_motive_ids=secondary_motive_ids,
            updated_at=settings.updated_at,
        )

    @staticmethod
    def _profile_supplemental_input(value: str | None) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        if not text:
            return ""
        markers = (
            "session_supplemental_info.txt：\n",
            "session_supplemental_info.txt:\n",
            "手动补充信息：\n",
            "手动补充信息:\n",
        )
        latest = max(
            ((text.rfind(marker), marker) for marker in markers),
            key=lambda item: item[0],
        )
        if latest[0] < 0:
            return text
        return text[latest[0] + len(latest[1]):].strip()

    def end_session(self, session_id: str) -> SessionState:
        state = self._normalize_state(self.repo.get(session_id))
        state.stage = "ended"
        state.ended_at = datetime.now(timezone.utc)
        saved = self.repo.save(state)
        self.runtime_recycler.release_session_lease(session_id)
        return saved

    @staticmethod
    def _normalize_state(state: SessionState) -> SessionState:
        if state.emotion_state and state.emotion_state.current_anchor_id:
            loader = get_config_loader()
            state.emotion_state.current_anchor_id = loader.resolve_emotion_anchor_id(
                state.emotion_state.current_anchor_id
            )
        context = state.rehearsal_context
        if (
            context.dimension_coverage_version
            < REHEARSAL_DIMENSION_COVERAGE_VERSION
        ):
            context.covered_dimensions = []
        return state
