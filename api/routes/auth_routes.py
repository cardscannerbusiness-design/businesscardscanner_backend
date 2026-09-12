"""Auth routes — login, refresh, logout, forgot/reset password, verify email."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from api.schemas import (
    ForgotPasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from auth.dependencies import get_current_user
from auth.service import AuthError, forgot_password, login, logout, logout_all, refresh_tokens, reset_password, verify_email
from services.recaptcha_service import RecaptchaError, verify_recaptcha_v2

router = APIRouter(prefix="/api/auth", tags=["Auth"])
logger = logging.getLogger(__name__)


def _request_meta(request: Request) -> dict[str, str]:
    return {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }


@router.post(
    "/login",
    summary="Login with email/username + password",
    description="Returns JWT access token (15 min) and refresh token (7 days).",
)
def login_route(body: LoginRequest, request: Request):
    meta = _request_meta(request)
    try:
        verify_recaptcha_v2(body.recaptcha_token, remote_ip=meta["ip"])
    except RecaptchaError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    try:
        return login(
            body.identifier, body.password, ip=meta["ip"], user_agent=meta["user_agent"],
        )
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/refresh",
    summary="Rotate refresh token for new access token",
    description="Provide the refresh token from login. Returns new token pair (old refresh token is revoked).",
)
def refresh_route(body: RefreshTokenRequest, request: Request):
    meta = _request_meta(request)
    try:
        return refresh_tokens(body.refresh_token, ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/logout",
    summary="Logout current device",
    description="Revokes the refresh token and ends the current session.",
)
def logout_route(body: RefreshTokenRequest, request: Request):
    meta = _request_meta(request)
    try:
        return logout(body.refresh_token, ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/logout-all",
    summary="Logout all devices",
    description="Revokes all refresh tokens and ends all sessions for the authenticated user.",
)
def logout_all_route(request: Request):
    user = get_current_user(request)
    meta = _request_meta(request)
    try:
        return logout_all(user["id"], ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/forgot-password",
    summary="Send password reset OTP",
    description="Sends a 6-digit OTP to the email if the account exists. Does not reveal whether the email is registered.",
)
def forgot_password_route(body: ForgotPasswordRequest, request: Request):
    meta = _request_meta(request)
    try:
        return forgot_password(body.email, ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/reset-password",
    summary="Reset password with OTP",
    description="Verifies the 6-digit OTP and sets a new password. All existing sessions are revoked.",
)
def reset_password_route(body: ResetPasswordRequest, request: Request):
    meta = _request_meta(request)
    try:
        return reset_password(body.email, body.otp, body.password, ip=meta["ip"], user_agent=meta["user_agent"])
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc


@router.post(
    "/verify-email",
    summary="Verify email address",
    description="Confirms the email verification token sent during registration.",
)
def verify_email_route(body: VerifyEmailRequest):
    try:
        return verify_email(body.token)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
