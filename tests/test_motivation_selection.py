import pytest
from pydantic import ValidationError

from backend.schemas.api import ConfirmSimulationRequest
from backend.schemas.simulation import BigFivePersonality, MotivationState
from backend.services.setup_service import SetupService


class _MotiveLoader:
    @staticmethod
    def motives() -> dict[str, object]:
        return {
            "commerce": object(),
            "recognition": object(),
            "affiliation": object(),
            "security": object(),
        }


def _request(secondary_motive_ids: list[str]) -> ConfirmSimulationRequest:
    return ConfirmSimulationRequest(
        personality=BigFivePersonality(),
        primary_motive_id="commerce",
        secondary_motive_ids=secondary_motive_ids,
    )


@pytest.mark.parametrize(
    "secondary_motive_ids",
    [
        [],
        ["recognition"],
        ["recognition", "affiliation"],
    ],
)
def test_confirm_simulation_accepts_zero_to_two_secondary_motives(
    secondary_motive_ids: list[str],
):
    request = _request(secondary_motive_ids)

    assert request.secondary_motive_ids == secondary_motive_ids


@pytest.mark.parametrize(
    "secondary_motive_ids",
    [
        ["recognition", "affiliation", "security"],
    ],
)
def test_confirm_simulation_rejects_secondary_motive_count_outside_range(
    secondary_motive_ids: list[str],
):
    with pytest.raises(ValidationError):
        _request(secondary_motive_ids)


def test_setup_service_allows_optional_secondary_motives_and_rejects_invalid_values():
    service = object.__new__(SetupService)
    service.loader = _MotiveLoader()

    service._validate_motives("commerce", [])
    service._validate_motives("commerce", ["recognition"])
    service._validate_motives("commerce", ["recognition", "affiliation"])

    with pytest.raises(ValueError, match="duplicates"):
        service._validate_motives("commerce", ["recognition", "recognition"])
    with pytest.raises(ValueError, match="must be different"):
        service._validate_motives("commerce", ["commerce"])


def test_single_secondary_motive_uses_full_secondary_weight():
    initial = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition"],
    )
    changed = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=["recognition"],
        primary_score=40,
        secondary_scores={"recognition": 80},
    )

    assert initial.total_satisfaction == pytest.approx(50)
    assert changed.total_satisfaction == pytest.approx(52)


def test_no_secondary_motive_uses_the_primary_score_as_total_satisfaction():
    motivation = MotivationState(
        primary_motive_id="commerce",
        secondary_motive_ids=[],
        primary_score=40,
        secondary_scores={"stale": 80},
    )

    assert motivation.secondary_scores == {}
    assert motivation.total_satisfaction == pytest.approx(40)
