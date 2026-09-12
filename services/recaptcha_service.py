"""Google reCAPTCHA v2 verification service."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


class RecaptchaError(Exception):
    """Raised when reCAPTCHA verification fails."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def verify_recaptcha_v2(token: str | None, remote_ip: str = "") -> dict[str, Any]:
    """Verify Google reCAPTCHA v2 checkbox response token.

    Args:
        token: The reCAPTCHA response token submitted by the client.
        remote_ip: Optional client IP address.

    Returns:
        dict: The response from Google siteverify on success.

    Raises:
        RecaptchaError: If the token is missing, invalid, expired, or verification fails.
    """
    if not token or not str(token).strip():
        raise RecaptchaError(
            code="CAPTCHA_REQUIRED",
            message="Please complete the CAPTCHA verification.",
            status_code=400,
        )

    secret_key = (os.getenv("RECAPTCHA_SECRET_KEY") or "").strip()
    if not secret_key:
        logger.error("RECAPTCHA_SECRET_KEY is not configured in backend environment.")
        raise RecaptchaError(
            code="CAPTCHA_CONFIGURATION_ERROR",
            message="CAPTCHA service is temporarily misconfigured.",
            status_code=500,
        )

    data = {
        "secret": secret_key,
        "response": str(token).strip(),
    }
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(RECAPTCHA_VERIFY_URL, data=data)
            res.raise_for_status()
            payload = res.json()
    except Exception as exc:
        logger.error("Error communicating with Google reCAPTCHA endpoint: %s", exc)
        raise RecaptchaError(
            code="CAPTCHA_SERVICE_UNAVAILABLE",
            message="Unable to verify CAPTCHA with Google. Please try again.",
            status_code=502,
        ) from exc

    if not payload.get("success"):
        error_codes = payload.get("error-codes", [])
        logger.warning("Google reCAPTCHA verification failed: %s", error_codes)
        if "timeout-or-duplicate" in error_codes:
            msg = "CAPTCHA has expired or was already used. Please verify again."
        elif "invalid-input-response" in error_codes or "missing-input-response" in error_codes:
            msg = "Invalid CAPTCHA token. Please check the checkbox again."
        else:
            msg = "CAPTCHA verification failed. Please try again."

        raise RecaptchaError(
            code="CAPTCHA_VERIFICATION_FAILED",
            message=msg,
            status_code=400,
        )

    return payload
