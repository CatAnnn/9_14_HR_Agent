from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request

from backend.config.settings import get_settings
from backend.models.user import AppUser
from backend.repositories.postgres_repository import PostgresRepository
from backend.schemas.auth import (
    AdminAccountCreateRequest,
    AdminAccountResponse,
    AdminPasswordResetRequest,
    AuthUser,
    AuthUserResponse,
    LoginRequest,
    RegisterRequest,
)
from backend.services.auth_session_service import (
    AuthSessionError,
    AuthSessionService,
    MaxActiveSessionsReached,
)
from backend.services.password_service import PasswordPolicyError, PasswordService
from backend.services.rate_limit_service import RateLimitExceeded, RateLimitService
from backend.services.whitelist_service import WhitelistService


class InvalidCredentials(RuntimeError):
    pass


class RegistrationFailed(RuntimeError):
    pass


class SessionLimitReached(RuntimeError):
    pass


@dataclass(frozen=True)
class LoginResult:
    session_id: str
    user: AuthUserResponse


class AuthService:
    def __init__(
        self,
        repo: PostgresRepository | None = None,
        password_service: PasswordService | None = None,
        whitelist_service: WhitelistService | None = None,
        session_service: AuthSessionService | None = None,
        rate_limit_service: RateLimitService | None = None,
    ) -> None:
        self.settings = get_settings()
        self.repo = repo or PostgresRepository()
        self.password_service = password_service or PasswordService()
        self.whitelist_service = whitelist_service or WhitelistService(self.repo)
        self.session_service = session_service or AuthSessionService()
        self.rate_limit_service = rate_limit_service or RateLimitService()
        self._hash_semaphore = asyncio.Semaphore(max(1, self.settings.auth_login_hash_max_concurrency))
        self._dummy_password_hash = self.password_service.hash_password("00000000")

    async def register(self, payload: RegisterRequest, request: Request) -> None:
        try:
            self.rate_limit_service.check_register(ip_address=self._ip_address(request))
            if not self._is_allowed_domain(payload.email):
                raise RegistrationFailed("EMAIL_DOMAIN_NOT_ALLOWED")
            if not self.whitelist_service.is_allowed(payload.email):
                raise RegistrationFailed("NOT_WHITELISTED")
            password_hash = await self._hash_password(payload.password)
            role = "admin" if payload.email in self.settings.auth_admin_email_set else "user"
            with self.repo.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM app_users WHERE lower(email::text) = %s",
                    (payload.email,),
                ).fetchone()
                if row:
                    raise RegistrationFailed("ACCOUNT_EXISTS")
                conn.execute(
                    """
                    INSERT INTO app_users (
                        email, display_name, password_hash, auth_provider,
                        role, is_active, is_email_verified
                    )
                    VALUES (%s, %s, %s, 'local', %s, TRUE, TRUE)
                    """,
                    (payload.email, payload.display_name, password_hash, role),
                )
        except (PasswordPolicyError, RateLimitExceeded, RegistrationFailed) as exc:
            self._audit(payload.email, "register", False, str(exc), request)
            raise RegistrationFailed("REGISTRATION_FAILED") from exc
        self._audit(payload.email, "register", True, None, request)

    async def login(self, payload: LoginRequest, request: Request) -> LoginResult:
        try:
            self.rate_limit_service.check_login(
                email=payload.email,
                ip_address=self._ip_address(request),
            )
        except RateLimitExceeded as exc:
            self._audit(payload.email, "login", False, "rate_limited", request)
            raise InvalidCredentials("INVALID_CREDENTIALS") from exc

        user = self._get_user_by_email(payload.email)
        if (
            not self._is_allowed_domain(payload.email)
            or not user
            or not user.is_active
            or not self.whitelist_service.is_allowed(payload.email)
        ):
            await self._verify_password(payload.password, self._dummy_password_hash)
            self._audit(payload.email, "login", False, "not_allowed", request)
            raise InvalidCredentials("INVALID_CREDENTIALS")

        if not await self._verify_password(payload.password, user.password_hash):
            self._audit(payload.email, "login", False, "wrong_password", request)
            raise InvalidCredentials("INVALID_CREDENTIALS")

        if self.password_service.needs_rehash(user.password_hash):
            self._update_password_hash(user.id, await self._hash_password(payload.password))

        role = "admin" if user.email in self.settings.auth_admin_email_set else user.role
        if role != user.role:
            self._update_role(user.id, role)
        auth_user = AuthUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=role,
            auth_provider=user.auth_provider,
        )
        try:
            session_id = self.session_service.create_session(auth_user)
        except MaxActiveSessionsReached as exc:
            self._audit(payload.email, "login", False, "session_limit_reached", request)
            raise SessionLimitReached("MAX_ACTIVE_SESSIONS_REACHED") from exc
        except AuthSessionError as exc:
            self._audit(payload.email, "login", False, "session_store_unavailable", request)
            raise SessionLimitReached("SESSION_STORE_UNAVAILABLE") from exc

        self._mark_login(user.id)
        self._audit(payload.email, "login", True, None, request)
        return LoginResult(
            session_id=session_id,
            user=AuthUserResponse(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=role,
            ),
        )

    def logout(self, session_id: str) -> None:
        self.session_service.delete_session(session_id)

    def list_admin_accounts(self) -> list[AdminAccountResponse]:
        return [
            AdminAccountResponse.model_validate(row)
            for row in self.whitelist_service.list_accounts()
        ]

    async def admin_create_account(
        self,
        payload: AdminAccountCreateRequest,
        request: Request,
    ) -> AdminAccountResponse:
        if not self._is_allowed_domain(payload.email):
            raise RegistrationFailed("EMAIL_DOMAIN_NOT_ALLOWED")
        try:
            password_hash = await self._hash_password(payload.password)
        except PasswordPolicyError as exc:
            raise RegistrationFailed("INVALID_PASSWORD") from exc
        role = "admin" if payload.email in self.settings.auth_admin_email_set else "user"
        self.whitelist_service.set_allowed(payload.email, True)
        with self.repo.connection() as conn:
            conn.execute(
                """
                INSERT INTO app_users (
                    email, display_name, password_hash, auth_provider,
                    role, is_active, is_email_verified
                )
                VALUES (%s, %s, %s, 'local', %s, TRUE, TRUE)
                ON CONFLICT (email) DO UPDATE SET
                    display_name = COALESCE(EXCLUDED.display_name, app_users.display_name),
                    password_hash = EXCLUDED.password_hash,
                    role = EXCLUDED.role,
                    is_active = TRUE,
                    is_email_verified = TRUE,
                    updated_at = NOW()
                """,
                (payload.email, payload.display_name, password_hash, role),
            )
        self._audit(payload.email, "admin_create_account", True, None, request)
        return self._admin_account(payload.email)

    async def admin_reset_password(
        self,
        email: str,
        payload: AdminPasswordResetRequest,
        request: Request,
    ) -> AdminAccountResponse:
        normalized = email.strip().lower()
        try:
            password_hash = await self._hash_password(payload.password)
        except PasswordPolicyError as exc:
            raise RegistrationFailed("INVALID_PASSWORD") from exc
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                UPDATE app_users
                SET password_hash = %s, updated_at = NOW()
                WHERE lower(email::text) = %s
                RETURNING id::text
                """,
                (password_hash, normalized),
            ).fetchone()
        if row is None:
            raise RegistrationFailed("ACCOUNT_NOT_FOUND")
        self.session_service.delete_user_sessions(row["id"])
        self._audit(normalized, "admin_reset_password", True, None, request)
        return self._admin_account(normalized)

    def admin_set_whitelist(
        self,
        email: str,
        enabled: bool,
        request: Request,
    ) -> AdminAccountResponse:
        normalized = email.strip().lower()
        if not self._is_allowed_domain(normalized):
            raise RegistrationFailed("EMAIL_DOMAIN_NOT_ALLOWED")
        user = self._get_user_by_email(normalized)
        if not enabled and self._is_protected_admin(normalized, user):
            raise RegistrationFailed("ADMIN_WHITELIST_REQUIRED")
        self.whitelist_service.set_allowed(normalized, enabled)
        if not enabled and user:
            self.session_service.delete_user_sessions(user.id)
        self._audit(normalized, "admin_update_whitelist", True, None, request)
        return self._admin_account(normalized)

    def admin_delete_account(self, email: str, request: Request) -> None:
        normalized = email.strip().lower()
        user = self._get_user_by_email(normalized)
        if self._is_protected_admin(normalized, user):
            raise RegistrationFailed("ADMIN_ACCOUNT_REQUIRED")
        with self.repo.connection() as conn:
            whitelist = conn.execute(
                "SELECT 1 FROM auth_whitelist WHERE lower(email::text) = %s",
                (normalized,),
            ).fetchone()
            if whitelist is None and user is None:
                raise RegistrationFailed("ACCOUNT_NOT_FOUND")
            conn.execute(
                "DELETE FROM auth_whitelist WHERE lower(email::text) = %s",
                (normalized,),
            )
            conn.execute(
                """
                UPDATE app_users
                SET is_active = FALSE, updated_at = NOW()
                WHERE lower(email::text) = %s
                """,
                (normalized,),
            )
        if user:
            self.session_service.delete_user_sessions(user.id)
        self._audit(normalized, "admin_delete_account", True, None, request)

    def _admin_account(self, email: str) -> AdminAccountResponse:
        normalized = email.strip().lower()
        row = next(
            (
                item
                for item in self.whitelist_service.list_accounts()
                if item["email"].lower() == normalized
            ),
            None,
        )
        if row is None:
            raise RegistrationFailed("ACCOUNT_NOT_FOUND")
        return AdminAccountResponse.model_validate(row)

    def _get_user_by_email(self, email: str) -> AppUser | None:
        with self.repo.connection() as conn:
            row = conn.execute(
                """
                SELECT id::text, email::text, display_name, password_hash,
                       auth_provider, provider_subject, role, is_active,
                       is_email_verified
                FROM app_users
                WHERE lower(email::text) = %s
                """,
                (email.strip().lower(),),
            ).fetchone()
        return AppUser(**row) if row else None

    def _update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self.repo.connection() as conn:
            conn.execute(
                "UPDATE app_users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                (password_hash, user_id),
            )

    def _update_role(self, user_id: str, role: str) -> None:
        with self.repo.connection() as conn:
            conn.execute(
                "UPDATE app_users SET role = %s, updated_at = NOW() WHERE id = %s",
                (role, user_id),
            )

    def _mark_login(self, user_id: str) -> None:
        with self.repo.connection() as conn:
            conn.execute(
                "UPDATE app_users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s",
                (user_id,),
            )

    async def _hash_password(self, password: str) -> str:
        async with self._hash_semaphore:
            return await asyncio.to_thread(self.password_service.hash_password, password)

    async def _verify_password(self, password: str, password_hash: str | None) -> bool:
        async with self._hash_semaphore:
            return await asyncio.to_thread(
                self.password_service.verify_password,
                password,
                password_hash,
            )

    def _is_allowed_domain(self, email: str) -> bool:
        domain = email.strip().lower().partition("@")[2]
        allowed = self.settings.auth_allowed_domain_set
        # Match the configured mailbox domains exactly.  An empty allowlist
        # must fail closed instead of accidentally accepting every domain.
        return bool(domain) and domain in allowed

    def _is_protected_admin(self, email: str, user: AppUser | None) -> bool:
        return email in self.settings.auth_admin_email_set or bool(user and user.role == "admin")

    def _audit(
        self,
        email: str | None,
        event_type: str,
        success: bool,
        reason: str | None,
        request: Request,
    ) -> None:
        try:
            with self.repo.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO auth_audit_log (
                        email, event_type, success, reason,
                        ip_address, user_agent, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        email.strip().lower() if email else None,
                        event_type,
                        success,
                        reason,
                        self._ip_address(request),
                        request.headers.get("user-agent"),
                        datetime.now(timezone.utc),
                    ),
                )
        except Exception:
            return

    @staticmethod
    def _ip_address(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
        return request.client.host if request.client else "0.0.0.0"
