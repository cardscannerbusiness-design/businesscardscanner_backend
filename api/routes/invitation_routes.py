"""Invitation routes — create, list, resend, revoke, validate, accept."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

from auth.constants import ROLE_ADMIN, ROLE_SUPER_ADMIN
from auth.dependencies import get_current_user, require_role
from auth.invitation_service import (
    InvitationError,
    accept_invitation,
    create_invitation,
    list_invitations,
    resend_invitation,
    revoke_invitation,
    validate_invitation_token,
)

router = APIRouter(prefix="/api/invitations", tags=["Invitations"])
logger = logging.getLogger(__name__)


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = Field(..., description="ADMIN or USER (SuperAdmin); USER only (Admin)")
    company_id: str | None = None
    company_name: str = ""
    company_code: str = ""
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_website: str = ""


class AcceptInviteRequest(BaseModel):
    token: str = Field(..., min_length=16)
    full_name: str = Field(default="", max_length=257)
    # Legacy fields remain accepted for older clients.
    first_name: str = Field(default="", max_length=128)
    last_name: str = Field(default="", max_length=128)
    password: str = Field(..., min_length=8)
    confirm_password: str = Field(..., min_length=8)
    phone: str = ""
    username: str = ""
    designation: str = Field(default="", max_length=255, description="Job title / designation")
    department: str = Field(default="", max_length=255, description="Department (optional)")
    company_name: str = ""
    company_code: str = ""
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_website: str = ""


class ValidateInviteRequest(BaseModel):
    token: str = Field(..., min_length=16)


def _meta(request: Request) -> dict[str, str]:
    return {
        "ip": request.client.host if request.client else "",
        "user_agent": request.headers.get("user-agent", ""),
    }


def _raise(exc: InvitationError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


@router.post(
    "",
    summary="Send invitation",
    description="SuperAdmin invites Admins or Users; Admin invites Users. Password is never set by the inviter.",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def invite_user(body: InviteRequest, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return create_invitation(
            email=str(body.email),
            role=body.role,
            invited_by=actor,
            company_id=body.company_id,
            company_name=body.company_name,
            company_code=body.company_code,
            company_address=body.company_address,
            company_phone=body.company_phone,
            company_email=body.company_email,
            company_website=body.company_website,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        _raise(exc)


@router.get(
    "",
    summary="List invitations",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def get_invitations(
    request: Request,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=200),
):
    actor = get_current_user(request)
    try:
        return list_invitations(actor, status=status, page=page, limit=limit)
    except InvitationError as exc:
        _raise(exc)


@router.post(
    "/{invitation_id}/resend",
    summary="Resend invitation",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def resend(invitation_id: str, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return resend_invitation(
            invitation_id, actor, ip=meta["ip"], user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        _raise(exc)


@router.post(
    "/{invitation_id}/revoke",
    summary="Revoke pending invitation",
    dependencies=[Depends(require_role(ROLE_SUPER_ADMIN, ROLE_ADMIN))],
)
def revoke(invitation_id: str, request: Request):
    actor = get_current_user(request)
    meta = _meta(request)
    try:
        return revoke_invitation(
            invitation_id, actor, ip=meta["ip"], user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        _raise(exc)


@router.post(
    "/validate",
    summary="Validate invitation token (public)",
)
def validate_token(body: ValidateInviteRequest):
    try:
        return validate_invitation_token(body.token)
    except InvitationError as exc:
        _raise(exc)


@router.post(
    "/accept",
    summary="Accept invitation and create account (public)",
)
def accept(body: AcceptInviteRequest, request: Request):
    if body.password != body.confirm_password:
        raise HTTPException(status_code=400, detail={"code": "PASSWORD_MISMATCH", "message": "Passwords do not match."})
    meta = _meta(request)
    try:
        return accept_invitation(
            raw_token=body.token,
            full_name=body.full_name,
            first_name=body.first_name,
            last_name=body.last_name,
            password=body.password,
            phone=body.phone,
            username=body.username or None,
            designation=body.designation,
            department=body.department,
            company_name=body.company_name,
            company_code=body.company_code,
            company_address=body.company_address,
            company_phone=body.company_phone,
            company_email=body.company_email,
            company_website=body.company_website,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
        )
    except InvitationError as exc:
        _raise(exc)
