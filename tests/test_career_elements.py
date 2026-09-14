from __future__ import annotations

import pytest

from backend.schemas.profile import EmployeeProfile
from backend.services.career_elements import (
    career_elements_applicable,
    current_career_elements,
    is_g9_or_above,
)
from scripts.import_employees import (
    EMPLOYEE_HEADERS,
    build_profile,
    validate_headers,
)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("G8", False),
        ("G9", True),
        ("G 10", True),
        ("SL1", True),
        ("Senior Manager", False),
        (None, False),
    ],
)
def test_is_g9_or_above_uses_normalized_bosch_grades(
    level: str | None,
    expected: bool,
):
    assert is_g9_or_above(level) is expected


def test_career_elements_apply_only_to_g9_development_intents():
    g9_profile = EmployeeProfile(level="G9")
    g8_profile = EmployeeProfile(level="G8")

    assert career_elements_applicable(g9_profile, "development")
    assert career_elements_applicable(g9_profile, "development_improvement")
    assert not career_elements_applicable(g9_profile, "improvement")
    assert not career_elements_applicable(g8_profile, "development")


def test_current_career_elements_prefers_structured_values_and_deduplicates():
    profile = EmployeeProfile(
        current_career_elements=[
            "Cross Function",
            " Cross Function ",
            "",
            "International",
        ],
        source_profile_text="Current Career Elements: Project Leadership",
    )

    assert current_career_elements(profile) == [
        "Cross Function",
        "International",
    ]


def test_current_career_elements_supports_legacy_source_profile_text():
    profile = EmployeeProfile(
        source_profile_text=(
            "Name: Example Employee\n"
            "Current Career Elements: Cross Function; International"
        )
    )

    assert current_career_elements(profile) == [
        "Cross Function",
        "International",
    ]


def test_employee_import_maps_current_career_elements():
    profile = build_profile(
        {
            "Name": "Example Employee",
            "Current Career Elements": "Cross Function; International",
        }
    )

    assert profile.current_career_elements == [
        "Cross Function",
        "International",
    ]


def test_employee_import_accepts_legacy_headers_without_career_elements():
    headers = [
        header for header in EMPLOYEE_HEADERS
        if header != "Current Career Elements"
    ]

    validate_headers(headers)


def test_employee_import_normalizes_exact_career_elements_header_alias():
    headers = [
        "Career Elements" if header == "Current Career Elements" else header
        for header in EMPLOYEE_HEADERS
    ]

    assert validate_headers(headers) == EMPLOYEE_HEADERS


def test_employee_import_normalizes_singular_career_element_header_alias():
    headers = [
        "Career Element" if header == "Current Career Elements" else header
        for header in EMPLOYEE_HEADERS
    ]

    assert validate_headers(headers) == EMPLOYEE_HEADERS


def test_employee_import_rejects_unknown_header():
    with pytest.raises(SystemExit, match="unexpected headers: Unknown Employee Field"):
        validate_headers([*EMPLOYEE_HEADERS, "Unknown Employee Field"])


def test_employee_import_rejects_alias_and_canonical_header_together():
    with pytest.raises(
        SystemExit,
        match="duplicate headers after normalization: Current Career Elements",
    ):
        validate_headers([*EMPLOYEE_HEADERS, "Career Elements"])
