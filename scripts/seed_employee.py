from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.schemas.employee_database import EmployeeDatabaseUpsertRequest  # noqa: E402
from backend.schemas.profile import EmployeeProfile  # noqa: E402
from backend.services.employee_database_service import EmployeeDatabaseService  # noqa: E402


if __name__ == "__main__":
    record = EmployeeDatabaseService().upsert(
        EmployeeDatabaseUpsertRequest(
            employee_id="E001",
            employee_alias="Alex",
            name="张伟",
            department="产品部",
            role="产品经理",
            manager="李明",
            profile=EmployeeProfile(
                employee_alias="Alex",
                role="产品经理",
                department="产品部",
                level="P3",
                reporting_line="汇报给 李明",
                performance_rating="M-",
                review_cycle="2024 H1",
                conversation_topic="PIP / 绩效不达预期",
                key_goals=["项目交付", "跨团队协作"],
                employee_status_summary="有挫败感，关注发展机会。",
            ),
        )
    )
    print(record.model_dump_json(indent=2, ensure_ascii=False))
