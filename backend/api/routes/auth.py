from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from backend.config.settings import Settings, get_settings
from backend.core.auth_dependency import get_current_session, require_admin_session
from backend.schemas.auth import (
    AdminAccountCreateRequest,
    AdminAccountResponse,
    AdminAccountsResponse,
    AdminPasswordResetRequest,
    AdminWhitelistUpdateRequest,
    AuthMeResponse,
    AuthSuccessResponse,
    LoginRequest,
    RegisterRequest,
)
from backend.services.auth_service import (
    AuthService,
    InvalidCredentials,
    RegistrationFailed,
    SessionLimitReached,
)
from backend.services.auth_session_service import AuthSession

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    return AuthService()


@router.post("/register", response_model=AuthSuccessResponse)
async def register(
    payload: RegisterRequest,
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthSuccessResponse:
    try:
        await auth_service.register(payload, request)
    except RegistrationFailed:
        return AuthSuccessResponse(
            success=False,
            message="注册失败，请检查信息或联系管理员。",
        )
    return AuthSuccessResponse(success=True, message="账号创建成功，请登录。")


@router.post("/login", response_model=AuthSuccessResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthSuccessResponse:
    try:
        result = await auth_service.login(payload, request)
    except SessionLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="登录服务暂时不可用，请稍后重试。",
        ) from exc
    except InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误，或账号暂不可用。",
        ) from exc
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=result.session_id,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        max_age=settings.auth_session_absolute_timeout_seconds,
        path="/",
    )
    return AuthSuccessResponse(success=True, user=result.user)


@router.get("/me", response_model=AuthMeResponse)
async def me(
    current_session: AuthSession = Depends(get_current_session),
) -> AuthMeResponse:
    return AuthMeResponse(authenticated=True, user=current_session.user)


@router.post("/logout", response_model=AuthSuccessResponse)
async def logout(
    response: Response,
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
    current_session: AuthSession = Depends(get_current_session),
) -> AuthSuccessResponse:
    auth_service.logout(current_session.session_id)
    response.delete_cookie(
        settings.auth_cookie_name,
        path="/",
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
    )
    return AuthSuccessResponse(success=True)


@router.get("/admin/accounts", response_model=AdminAccountsResponse)
async def admin_accounts(
    _admin: AuthSession = Depends(require_admin_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> AdminAccountsResponse:
    return AdminAccountsResponse(items=auth_service.list_admin_accounts())


@router.post("/admin/accounts", response_model=AdminAccountResponse)
async def admin_create_account(
    payload: AdminAccountCreateRequest,
    request: Request,
    _admin: AuthSession = Depends(require_admin_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> AdminAccountResponse:
    try:
        return await auth_service.admin_create_account(payload, request)
    except RegistrationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法创建账号，请检查邮箱、白名单和密码规则。",
        ) from exc


@router.patch(
    "/admin/accounts/{email}/password",
    response_model=AdminAccountResponse,
)
async def admin_reset_password(
    email: str,
    payload: AdminPasswordResetRequest,
    request: Request,
    _admin: AuthSession = Depends(require_admin_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> AdminAccountResponse:
    try:
        return await auth_service.admin_reset_password(email, payload, request)
    except RegistrationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法重置密码，请检查账号和密码规则。",
        ) from exc


@router.delete(
    "/admin/accounts/{email}",
    response_model=AuthSuccessResponse,
)
async def admin_delete_account(
    email: str,
    request: Request,
    _admin: AuthSession = Depends(require_admin_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthSuccessResponse:
    try:
        auth_service.admin_delete_account(email, request)
    except RegistrationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法删除该账号。",
        ) from exc
    return AuthSuccessResponse(success=True, message="白名单授权已删除，账号已停用。")


@router.put("/admin/whitelist", response_model=AdminAccountResponse)
async def admin_update_whitelist(
    payload: AdminWhitelistUpdateRequest,
    request: Request,
    _admin: AuthSession = Depends(require_admin_session),
    auth_service: AuthService = Depends(get_auth_service),
) -> AdminAccountResponse:
    try:
        return auth_service.admin_set_whitelist(
            payload.email,
            payload.enabled,
            request,
        )
    except RegistrationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法更新该白名单账号。",
        ) from exc
