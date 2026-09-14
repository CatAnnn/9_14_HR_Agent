from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppUser:
    id: str
    email: str
    display_name: str | None
    password_hash: str | None
    auth_provider: str
    provider_subject: str | None
    role: str
    is_active: bool
    is_email_verified: bool
