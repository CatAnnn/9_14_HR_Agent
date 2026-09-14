from __future__ import annotations

from contextvars import ContextVar, Token

_current_auth_user_id: ContextVar[str | None] = ContextVar("current_auth_user_id", default=None)
_current_auth_is_guest: ContextVar[bool] = ContextVar("current_auth_is_guest", default=False)
_current_observability_user_id: ContextVar[str | None] = ContextVar(
    "current_observability_user_id",
    default=None,
)


def set_current_auth_user_id(user_id: str | None) -> Token:
    return _current_auth_user_id.set(user_id)


def reset_current_auth_user_id(token: Token) -> None:
    _current_auth_user_id.reset(token)


def get_current_auth_user_id() -> str | None:
    return _current_auth_user_id.get()


def set_current_auth_is_guest(is_guest: bool) -> Token:
    return _current_auth_is_guest.set(bool(is_guest))


def get_current_auth_is_guest() -> bool:
    return _current_auth_is_guest.get()


def observability_user_id_from_email(email: str | None) -> str:
    """Return the normalized email local-part used by observability labels."""

    normalized = (email or "").strip().lower()
    local_part, separator, _domain = normalized.partition("@")
    if not separator or not local_part:
        return "system"
    return local_part


def set_current_observability_user_email(email: str | None) -> Token:
    user_id = None if email is None else observability_user_id_from_email(email)
    return _current_observability_user_id.set(user_id)


def reset_current_observability_user_id(token: Token) -> None:
    _current_observability_user_id.reset(token)


def get_current_observability_user_id() -> str:
    return _current_observability_user_id.get() or "system"
