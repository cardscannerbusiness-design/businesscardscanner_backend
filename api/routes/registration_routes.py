"""Admin self-registration routes — public submit, SuperAdmin review."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from auth.constants import ROLE_SUPER_ADMIN
from auth.dependencies import get_current_user, require_role
from auth.phone_otp_service import PhoneOtpError, confirm_signup_otp, send_signup_otp
from auth.registration_service import (
    RegistrationError,
    approve_admin_registration,
    create_admin_registration,
    delete_admin_registration,
    list_admin_registrations,
    reject_admin_registration,
)
from services.recaptcha_service import RecaptchaError, verify_recaptcha_v2

router = APIRouter(prefix="/api/registrations", tags=["Registrations"])
logger = logging.getLogger(__name__)


class AdminRegistrationRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=257)
    email: EmailStr
    password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)
    phone: str = ""
    designation: str = Field(default="", max_length=255)
    department: str = Field(default="", max_length=255)
    company_name: str = Field(default="", max_length=255)
    company_code: str = Field(default="", max_length=64)
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_website: str = ""
    username: str = ""
    phone_verification_token: str = ""
    recaptcha_token: str = Field(..., description="Google reCAPTCHA v2 response token")


class PhoneOtpSendRequest(BaseModel):
    phone: str = Field(..., min_length=7, max_length=32)
    email: str = ""


class PhoneOtpConfirmRequest(BaseModel):
    phone: str = Field(..., min_length=7, max_length=32)
    otp: str = Field(..., min_length=6, max_length=6)


class RejectRegistrationRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


def _meta(request: Request) -> dict[str, str]:
    return {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }


def _raise(exc: RegistrationError | PhoneOtpError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


@router.post(
    "/phone/send-otp",
    summary="Send Admin signup phone OTP",
)
def send_admin_phone_otp(body: PhoneOtpSendRequest, request: Request):
    try:
        return send_signup_otp(
            phone=body.phone,
            email=body.email,
            ip=request.client.host if request.client else "",
        )
    except PhoneOtpError as exc:
        _raise(exc)


@router.post(
    "/phone/confirm",
    summary="Confirm Admin signup phone OTP",
)
def confirm_admin_phone_otp(body: PhoneOtpConfirmRequest):
    try:
        return confirm_signup_otp(phone=body.phone, otp=body.otp)
    except PhoneOtpError as exc:
        _raise(exc)


@router.post(
    "/admin",
    summary="Submit Admin self-registration (public)",
    description="Creates an Admin user and Freemium company immediately. The Admin can sign in after signup.",
)
def submit_admin_registration(body: AdminRegistrationRequest, request: Request):
    if body.password != body.confirm_password:
        raise HTTPException(
            status_code=400,
            detail={"code": "PASSWORD_MISMATCH", "message": "Passwords do not match."},
        )
    meta = _meta(request)
    try:
        verify_recaptcha_v2(body.recaptcha_token, remote_ip=meta["ip"])
    except RecaptchaError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    try:
        return create_admin_registration(
            full_name=body.full_name,
            email=str(body.email),
            password=body.password,
            phone=body.phone,
            designation=body.designation,
            department=body.department,
            company_name=body.company_name,
            company_code=body.company_code,
            company_address=body.company_address,
            company_phone=body.company_phone,
            company_email=body.company_email,
            company_website=body.company_website,
            username=body.username,
            phone_verification_token=body.phone_verification_token,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except RegistrationError as exc:
        _raise(exc)


@router.post(
    "/user",
    summary="User self-registration (closed)",
    description="User self-signup is frozen. Users join through an Admin invitation.",
)
def submit_user_registration():
    raise HTTPException(
        status_code=403,
        detail={
            "code": "SIGNUP_CLOSED",
            "message": "User self-registration is closed. Ask your Admin to send an invitation.",
        },
    )


@router.get(
    "",
    summary="List registration requests",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def get_admin_registrations(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=200),
):
    try:
        return list_admin_registrations(status=status, page=page, limit=limit)
    except RegistrationError as exc:
        _raise(exc)


@router.post(
    "/{request_id}/approve",
    summary="Approve Admin registration request",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def approve_registration(request_id: str, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return approve_admin_registration(
            request_id, actor, ip=meta["ip"], user_agent=meta["user_agent"]
        )
    except RegistrationError as exc:
        _raise(exc)


@router.post(
    "/{request_id}/reject",
    summary="Reject Admin registration request",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def reject_registration(request_id: str, body: RejectRegistrationRequest, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return reject_admin_registration(
            request_id,
            actor,
            reason=body.reason,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except RegistrationError as exc:
        _raise(exc)


@router.delete(
    "/{request_id}",
    summary="Delete a registration request row",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN))],
)
def delete_registration(request_id: str, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return delete_admin_registration(
            request_id, actor, ip=meta["ip"], user_agent=meta["user_agent"]
        )
    except RegistrationError as exc:
        _raise(exc)
