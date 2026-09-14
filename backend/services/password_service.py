from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from backend.config.settings import get_settings


class PasswordPolicyError(ValueError):
    pass


class PasswordService:
    def __init__(self) -> None:
        settings = get_settings()
        self._hasher = PasswordHasher(
            time_cost=settings.auth_argon2_time_cost,
            memory_cost=settings.auth_argon2_memory_cost,
            parallelism=settings.auth_argon2_parallelism,
            hash_len=settings.auth_argon2_hash_len,
            salt_len=settings.auth_argon2_salt_len,
        )

    def hash_password(self, plain_password: str) -> str:
        self.validate_password_policy(plain_password)
        return self._hasher.hash(plain_password)

    def verify_password(self, plain_password: str, password_hash: str | None) -> bool:
        if not password_hash:
            return False
        try:
            return self._hasher.verify(password_hash, plain_password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str | None) -> bool:
        if not password_hash:
            return True
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (VerificationError, InvalidHashError):
            return True

    @staticmethod
    def validate_password_policy(password: str) -> None:
        minimum_length = 8 if password.isdigit() else 15
        if len(password) < minimum_length:
            raise PasswordPolicyError("PASSWORD_TOO_SHORT")
        if len(password) > 128:
            raise PasswordPolicyError("PASSWORD_TOO_LONG")
