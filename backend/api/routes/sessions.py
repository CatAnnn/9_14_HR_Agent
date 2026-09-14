from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from backend.api.dependencies import get_session_service
from backend.schemas.api import SessionLocaleRequest, SessionLocaleUpdateRequest
from backend.schemas.locale import locale_from_accept_language
from backend.schemas.state import SessionState
from backend.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionState)
def create_session(
    payload: SessionLocaleRequest | None = None,
    accept_language: Annotated[
        str | None,
        Header(alias="Accept-Language"),
    ] = None,
    service: SessionService = Depends(get_session_service),
):
    # Preserve both an empty HTTP body and existing direct Python calls.
    if payload is None:
        if accept_language is None:
            return service.create_session()
        return service.create_session(
            locale=locale_from_accept_language(accept_language)
        )
    return service.create_session(locale=payload.locale)


@router.get("/{session_id}", response_model=SessionState)
def get_session(session_id: str, service: SessionService = Depends(get_session_service)):
    return service.get_session(session_id)


@router.patch("/{session_id}/locale", response_model=SessionState)
def update_session_locale(
    session_id: str,
    payload: SessionLocaleUpdateRequest,
    service: SessionService = Depends(get_session_service),
):
    return service.update_locale(session_id, locale=payload.locale)


@router.delete("/{session_id}", response_model=SessionState)
def end_session(session_id: str, service: SessionService = Depends(get_session_service)):
    return service.end_session(session_id)
