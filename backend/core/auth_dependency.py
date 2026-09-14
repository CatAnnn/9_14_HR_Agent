from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, HTTPException, WebSocketException, status
from starlette.requests import HTTPConnection

from backend.config.settings import Settings, get_settings
from backend.core.session_context import (
    set_current_auth_is_guest,
    set_current_auth_user_id,
    set_current_observability_user_email,
)
from backend.observability.metrics import mark_authenticated_user_active
from backend.schemas.auth import AuthUserResponse
from backend.services.auth_session_service import AuthSession, AuthSessionService


@lru_cache(maxsize=1)
def get_auth_session_service() -> AuthSessionService:
    return AuthSessionService()


async def get_current_session(
    connection: HTTPConnection,
    settings: Settings = Depends(get_settings),
    session_service: AuthSessionService = Depends(get_auth_session_service),
) -> AuthSession:
    if not settings.auth_enabled:
        set_current_auth_is_guest(False)
        set_current_auth_user_id("auth-disabled")
        set_current_observability_user_email("local@bosch.com")
        mark_authenticated_user_active()
        return AuthSession(
            session_id="auth-disabled",
            user=AuthUserResponse(
                id="auth-disabled",
                email="local@bosch.com",
                display_name="Local administrator",
                role="admin",
            ),
            user_id="auth-disabled",
            role="admin",
            created_at=0,
            last_seen_at=0,
        )
    has_session_cookie = settings.auth_cookie_name in connection.cookies
    session_id = connection.cookies.get(settings.auth_cookie_name)
    # Reset request-local guest state before touching the session store.  This
    # also prevents a reused task context from retaining a previous guest flag
    # if the store lookup raises unexpectedly.
    set_current_auth_is_guest(False)
    session = session_service.get_session(session_id)
    if not session:
        if (
            not has_session_cookie
            and getattr(settings, "auth_guest_access_enabled", False)
        ):
            # Guest sessions are intentionally unowned.  Workflow sessions
            # remain addressable by their random session id, while no fake
            # UUID is written to the sessions.owner_user_id foreign key.
            set_current_auth_is_guest(True)
            set_current_auth_user_id(None)
            set_current_observability_user_email("guest@local")
            return AuthSession(
                session_id="guest",
                user=AuthUserResponse(
                    id=None,
                    email="guest@local",
                    display_name="Guest",
                    role="guest",
                ),
                user_id="",
                role="guest",
                created_at=0,
                last_seen_at=0,
            )
        set_current_auth_is_guest(False)
        set_current_auth_user_id(None)
        set_current_observability_user_email(None)
        if connection.scope.get("type") == "websocket":
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="Not authenticated",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    set_current_auth_is_guest(False)
    set_current_auth_user_id(session.user_id)
    set_current_observability_user_email(session.user.email)
    mark_authenticated_user_active()
    return session


def get_current_user(
    session: AuthSession = Depends(get_current_session),
) -> AuthUserResponse:
    return session.user


def require_admin_session(
    session: AuthSession = Depends(get_current_session),
) -> AuthSession:
    if session.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return session
