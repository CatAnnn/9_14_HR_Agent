from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from backend.api.dependencies import get_employee_database_service
from backend.schemas.employee_database import (
    EmployeeDatabaseRecord,
    EmployeeDatabaseSearchResponse,
    EmployeeDatabaseUpsertRequest,
)
from backend.services.employee_database_service import EmployeeDatabaseService

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=EmployeeDatabaseSearchResponse)
def search_employees(
    q: str | None = Query(default=None, description="Search by employee id, alias, name, department, role, manager, or profile text."),
    employee_id: str | None = Query(default=None, description="Exact employee id / 工号 match."),
    name: str | None = Query(default=None, description="Employee name or alias / 姓名 match."),
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Optional maximum number of matches. Omit to return all matches.",
    ),
    service: EmployeeDatabaseService = Depends(get_employee_database_service),
):
    return EmployeeDatabaseSearchResponse(
        database="postgresql",
        items=service.search(query=q, employee_id=employee_id, name=name, limit=limit),
    )


@router.post("", response_model=EmployeeDatabaseRecord)
def upsert_employee(
    payload: EmployeeDatabaseUpsertRequest,
    service: EmployeeDatabaseService = Depends(get_employee_database_service),
):
    return service.upsert(payload)


@router.get("/{employee_id}", response_model=EmployeeDatabaseRecord)
def get_employee(employee_id: str, service: EmployeeDatabaseService = Depends(get_employee_database_service)):
    return service.get(employee_id)
