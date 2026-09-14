from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    local, separator, domain = email.partition("@")
    if not separator or not local or not domain or "@" in domain:
        raise ValueError("INVALID_EMAIL")
    return email


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        clean = (value or "").strip()
        return clean or None


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class AuthUser(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    role: str = "user"
    auth_provider: str = "local"


class AuthUserResponse(BaseModel):
    id: str | None = None
    email: str
    display_name: str | None = None
    role: str = "user"


class AuthSuccessResponse(BaseModel):
    success: bool = True
    message: str | None = None
    user: AuthUserResponse | None = None


class AuthMeResponse(BaseModel):
    authenticated: bool
    user: AuthUserResponse | None = None


class AdminAccountCreateRequest(RegisterRequest):
    pass


class AdminPasswordResetRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class AdminWhitelistUpdateRequest(BaseModel):
    email: str
    enabled: bool = True

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class AdminAccountResponse(BaseModel):
    email: str
    display_name: str | None = None
    role: str = "user"
    whitelist_enabled: bool
    registered: bool
    is_active: bool


class AdminAccountsResponse(BaseModel):
    items: list[AdminAccountResponse] = Field(default_factory=list)
