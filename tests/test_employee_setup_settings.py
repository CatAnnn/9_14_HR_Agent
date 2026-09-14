from __future__ import annotations

from backend.repositories.session_repository import EmployeeReusableSettings
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.services.session_service import SessionService


class _SessionRepository:
    def __init__(self, settings: EmployeeReusableSettings | None):
        self.settings = settings
        self.employee_ids: list[str] = []

    def get_latest_employee_setup(
        self,
        employee_id: str,
    ) -> EmployeeReusableSettings | None:
        self.employee_ids.append(employee_id)
        return self.settings


class _RuntimeRecycler:
    def release_session_lease(self, _session_id: str) -> None:
        return None


def _service(
    settings: EmployeeReusableSettings | None,
) -> tuple[SessionService, _SessionRepository]:
    repository = _SessionRepository(settings)
    return (
        SessionService(
            repo=repository,  # type: ignore[arg-type]
            runtime_recycler=_RuntimeRecycler(),  # type: ignore[arg-type]
        ),
        repository,
    )


def test_latest_employee_settings_returns_only_reusable_page_values() -> None:
    settings = EmployeeReusableSettings(
        supplemental_info=(
            "background.pdf：\n文档内容\n\n"
            "session_supplemental_info.txt：\n上次填写的员工背景"
        ),
        personality=BigFivePersonality(
            openness=61,
            conscientiousness=72,
            extraversion=43,
            agreeableness=58,
            neuroticism=37,
        ),
        motivation=MotivationState(
            primary_motive_id="growth",
            secondary_motive_ids=["recognition", "autonomy"],
        ),
    )
    service, repository = _service(settings)

    result = service.get_latest_employee_setup_settings(" E001 ")

    assert repository.employee_ids == ["E001"]
    assert result.found is True
    assert result.employee_id == "E001"
    assert result.supplemental_info == "上次填写的员工背景"
    assert result.personality == settings.personality
    assert result.primary_motive_id == "growth"
    assert result.secondary_motive_ids == ["recognition", "autonomy"]


def test_latest_employee_settings_returns_empty_result_without_history() -> None:
    service, repository = _service(None)

    result = service.get_latest_employee_setup_settings("E404")

    assert repository.employee_ids == ["E404"]
    assert result.found is False
    assert result.supplemental_info == ""
    assert result.personality is None
    assert result.secondary_motive_ids == []


def test_latest_employee_settings_exposes_no_intent_or_performance_draft() -> None:
    service, _ = _service(
        EmployeeReusableSettings(supplemental_info="手动填写")
    )

    result = service.get_latest_employee_setup_settings("E001")

    assert result.found is True
    assert result.supplemental_info == "手动填写"
    assert not hasattr(result, "intent_id")
    assert not hasattr(result, "intent_draft")
