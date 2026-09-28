"""Authenticated, account-isolated email-template management APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from auth.constants import ROLE_ADMIN, ROLE_USER
from auth.dependencies import get_current_user
from services.admin_runtime_config import resolve_owner_admin_id
from services.email_template_store import (
    DEFAULT_TEMPLATE_KEY,
    EmailTemplateError,
    create_email_template,
    delete_email_template,
    get_admin_scope,
    get_email_template,
    list_email_templates,
    update_email_template,
)
from services.cms_media_service import (
    delete_account_media,
    list_account_media,
    save_account_media,
)
from services.feature_control import require_feature

router = APIRouter(prefix="/api/email-templates", tags=["Email Templates"])


class EmailTemplateCreateRequest(BaseModel):
    template_name: str = Field(min_length=1, max_length=160)
    template_key: str = Field(default=DEFAULT_TEMPLATE_KEY, min_length=2, max_length=64)
    subject: str = Field(min_length=1)
    body: str = ""
    components: list[dict[str, Any]] = Field(default_factory=list)
    event_id: str | None = None
    token_map: dict[str, str] = Field(default_factory=dict)
    status: str = Field(default="active")


class EmailTemplateUpdateRequest(BaseModel):
    template_name: str | None = Field(default=None, min_length=1, max_length=160)
    template_key: str | None = Field(default=None, min_length=2, max_length=64)
    subject: str | None = Field(default=None, min_length=1)
    body: str | None = None
    components: list[dict[str, Any]] | None = None
    event_id: str | None = None
    token_map: dict[str, str] | None = None
    status: str | None = None


class EmailTemplateStatusRequest(BaseModel):
    status: str


def _current_scope(request: Request, *, write: bool = False) -> tuple[dict[str, Any], str, str]:
    user = get_current_user(request)
    role = str(user.get("role") or "")
    if role not in {ROLE_ADMIN, ROLE_USER}:
        raise HTTPException(status_code=403, detail="Email templates require a customer account.")

    admin_user_id = resolve_owner_admin_id(user)
    if not admin_user_id:
        raise HTTPException(status_code=403, detail="No account owner is assigned.")
    try:
        account_id, owner_id = get_admin_scope(admin_user_id)
    except EmailTemplateError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    require_feature(user, "email_templates")
    return user, account_id, owner_id


def _bad_request(exc: EmailTemplateError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("", summary="List templates belonging to the authenticated account")
def list_templates(request: Request):
    _, account_id, _ = _current_scope(request)
    items = list_email_templates(account_id)
    return {"items": items, "total": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create an account template")
def create_template(body: EmailTemplateCreateRequest, request: Request):
    user, account_id, owner_id = _current_scope(request, write=True)
    require_feature(user, "email_templates", "create")
    try:
        return create_email_template(
            account_id=account_id,
            admin_user_id=owner_id,
            actor_id=str(user["id"]),
            **body.model_dump(),
        )
    except EmailTemplateError as exc:
        raise _bad_request(exc) from exc


@router.get("/media", summary="List media belonging to the authenticated account")
def list_template_media(request: Request):
    user, account_id, owner_id = _current_scope(request)
    require_feature(user, "media")
    return {"items": list_account_media(account_id, admin_user_id=owner_id)}


@router.post("/media", status_code=status.HTTP_201_CREATED, summary="Upload template media")
async def upload_template_media(request: Request, file: UploadFile = File(...)):
    user, account_id, owner_id = _current_scope(request, write=True)
    require_feature(user, "media", "upload")
    raw = await file.read()
    try:
        return save_account_media(
            account_id=account_id,
            admin_user_id=owner_id,
            actor_id=str(user["id"]),
            file_bytes=raw,
            filename=file.filename,
            content_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/media/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template_media(media_id: str, request: Request):
    user, account_id, _ = _current_scope(request, write=True)
    require_feature(user, "media")
    try:
        delete_account_media(account_id, media_id)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return None


@router.get("/{template_id}", summary="Get one template from the authenticated account")
def get_template(template_id: str, request: Request):
    _, account_id, _ = _current_scope(request)
    item = get_email_template(account_id, template_id)
    if not item:
        raise HTTPException(status_code=404, detail="Email template not found.")
    return item


@router.put("/{template_id}", summary="Update an account template")
def put_template(template_id: str, body: EmailTemplateUpdateRequest, request: Request):
    user, account_id, _ = _current_scope(request, write=True)
    require_feature(user, "email_templates", "edit")
    try:
        item = update_email_template(
            account_id,
            template_id,
            body.model_dump(exclude_none=True),
            actor_id=str(user["id"]),
        )
    except EmailTemplateError as exc:
        raise _bad_request(exc) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Email template not found.")
    return item


@router.patch("/{template_id}/status", summary="Activate or deactivate an account template")
def patch_template_status(
    template_id: str,
    body: EmailTemplateStatusRequest,
    request: Request,
):
    user, account_id, _ = _current_scope(request, write=True)
    require_feature(user, "email_templates", "edit")
    try:
        item = update_email_template(
            account_id,
            template_id,
            {"status": body.status},
            actor_id=str(user["id"]),
        )
    except EmailTemplateError as exc:
        raise _bad_request(exc) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Email template not found.")
    return item


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: str, request: Request):
    user, account_id, _ = _current_scope(request, write=True)
    require_feature(user, "email_templates", "delete")
    if not delete_email_template(account_id, template_id):
        raise HTTPException(status_code=404, detail="Email template not found.")
    return None
