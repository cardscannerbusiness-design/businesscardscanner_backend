"""RBAC authentication middleware — replaces Neon Auth middleware."""

from __future__ import annotations

import asyncio
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from auth.constants import is_public_path
from auth.jwt_utils import verify_access_token
from auth.session_service import validate_session
from config.settings import cors_headers_for_origin
from db.pool import db_cursor

logger = logging.getLogger(__name__)


class RBACMiddleware(BaseHTTPMiddleware):
    """Validates JWT access tokens, sessions, and attaches full user info to the request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if is_public_path(path):
            return await call_next(request)

        cors = cors_headers_for_origin(request.headers.get("origin"))

        # ── Extract Bearer token ──────────────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid authorization header. Expected: Bearer <token>"},
                headers=cors,
            )

        token = auth_header[7:].strip()
        payload = verify_access_token(token)
        if not payload:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or expired access token."},
                headers=cors,
            )

        user_id = payload.get("sub")
        if not user_id:
            return JSONResponse(
                status_code=401,
                content={"detail": "Token missing user identifier."},
                headers=cors,
            )

        session_id = payload.get("session_id")
        if not session_id:
            return JSONResponse(
                status_code=401,
                content={"detail": "Access token missing session_id. Please log in again."},
                headers=cors,
            )

        # ── Validate session (idle + absolute timeout) ────────────────────
        try:
            session_ok = await asyncio.to_thread(
                lambda: validate_session(session_id, str(user_id), touch=True)
            )
        except Exception as exc:
            logger.error("Middleware session validation failed: %s", exc)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error during session validation."},
                headers=cors,
            )

        if not session_ok:
            return JSONResponse(
                status_code=401,
                content={"detail": "Session expired or invalid. Please log in again."},
                headers=cors,
            )

        # ── Lookup user in database ───────────────────────────────────────
        try:
            user_info = await _lookup_user(user_id)
        except Exception as exc:
            logger.error("Middleware user lookup failed: %s", exc)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error during authentication."},
                headers=cors,
            )

        if not user_info:
            return JSONResponse(
                status_code=401,
                content={"detail": "User not found or has been deleted."},
                headers=cors,
            )

        if not user_info.get("is_active"):
            return JSONResponse(
                status_code=403,
                content={"detail": "Account is deactivated. Contact your admin."},
                headers=cors,
            )

        # ── Check company active (if applicable) ─────────────────────────
        company_id = user_info.get("company_id")
        if company_id:
            company_status = user_info.get("company_status")
            if company_status and company_status != "active":
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Your company account is inactive."},
                    headers=cors,
                )

        # ── Attach to request state ──────────────────────────────────────
        user_info["session_id"] = str(session_id)
        request.state.auth_user = user_info
        return await call_next(request)


async def _lookup_user(user_id: str) -> dict | None:
    """Fetch user + role + company + permissions from the database."""

    def _query():
        with db_cursor(commit=False) as cur:
            # User + role
            cur.execute(
                """
                SELECT u.id, u.email, u.first_name, u.last_name, u.username,
                       u.phone, u.is_active, u.is_verified, u.company_id, u.admin_id,
                       u.profile_image, COALESCE(u.scans_unlimited, FALSE) AS scans_unlimited,
                       u.user_card_limit, COALESCE(u.user_cards_used, 0) AS user_cards_used,
                       r.name AS role, c.status AS company_status
                FROM users u
                JOIN roles r ON r.id = u.role_id
                LEFT JOIN companies c ON c.id = u.company_id
                WHERE u.id = %s AND u.deleted_at IS NULL
                """,
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                return None

            user = dict(user)
            role_name = user["role"]
            user["scans_unlimited"] = bool(user.get("scans_unlimited"))
            raw_limit = user.get("user_card_limit")
            user["user_card_limit"] = int(raw_limit) if raw_limit is not None else None
            user["user_cards_used"] = max(0, int(user.get("user_cards_used") or 0))

            # Fetch permissions
            cur.execute(
                """
                SELECT p.name
                FROM role_permissions rp
                JOIN permissions p ON p.id = rp.permission_id
                JOIN roles r ON r.id = rp.role_id
                WHERE r.name = %s
                """,
                (role_name,),
            )
            perms = [row["name"] for row in cur.fetchall()]
            user["permissions"] = perms

            # Serialize UUIDs
            for key in ("id", "company_id", "admin_id"):
                if user.get(key) is not None:
                    user[key] = str(user[key])

            return user

    return await asyncio.to_thread(_query)
