from __future__ import annotations

from pydantic import BaseModel, Field

from backend.schemas.profile import EmployeeProfile


class EmployeeDatabaseRecord(BaseModel):
    employee_id: str = Field(min_length=1)
    employee_alias: str | None = None
    name: str | None = None
    department: str | None = None
    role: str | None = None
    manager: str | None = None
    profile_text: str | None = None
    profile: EmployeeProfile | None = None
    updated_at: str | None = None


class EmployeeDatabaseUpsertRequest(BaseModel):
    employee_id: str = Field(min_length=1)
    employee_alias: str | None = None
    name: str | None = None
    department: str | None = None
    role: str | None = None
    manager: str | None = None
    profile_text: str | None = None
    profile: EmployeeProfile | None = None


class EmployeeDatabaseSearchResponse(BaseModel):
    database: str = "postgresql"
    items: list[EmployeeDatabaseRecord] = Field(default_factory=list)
