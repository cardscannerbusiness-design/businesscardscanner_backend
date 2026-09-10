"""Transactional SMS for Admin signup OTP.

Does not use WhatsApp or Email. Delivery is SMS only.

Provider selection (no secrets logged):
  1. MSG91 when MSG91_AUTH_KEY is set (India OTP API).
  2. Amazon SNS when AWS credentials already used by Textract are present.

Phone format sent to providers is E.164 (+91XXXXXXXXXX for Indian mobiles).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class SmsOtpError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def to_e164(digits: str) -> str:
    cleaned = (digits or "").strip()
    if cleaned.startswith("+"):
        raw = re.sub(r"\D", "", cleaned)
        if not raw:
            raise SmsOtpError("INVALID_PHONE", "Enter a valid mobile number.", 422)
        return f"+{raw}"
    raw = re.sub(r"\D", "", cleaned)
    if not raw:
        raise SmsOtpError("INVALID_PHONE", "Enter a valid mobile number.", 422)
    if len(raw) == 10 and raw[0] in "6789":
        raw = f"91{raw}"
    return f"+{raw}"



def mask_phone(e164: str) -> str:
    digits = re.sub(r"\D", "", e164)
    last4 = digits[-4:] if len(digits) >= 4 else digits
    prefix = digits[:-10] if len(digits) > 10 else ""
    return f"+{prefix}{'*' * 6}{last4}"


def is_sms_configured() -> bool:
    if (os.getenv("MSG91_AUTH_KEY") or "").strip():
        return True
    return bool(
        (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
        and (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    )


def send_signup_otp_sms(phone_digits: str, otp: str) -> str:
    """Send a 6-digit OTP over SMS. Returns provider name. Never logs OTP or keys."""
    e164 = to_e164(phone_digits)
    msg91 = (os.getenv("MSG91_AUTH_KEY") or "").strip()
    aws_key = (os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    aws_secret = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()

    if msg91:
        logger.info(
            "OTP provider configured: YES provider=msg91 Phone number format: %s OTP request initiated: YES",
            mask_phone(e164),
        )
        status = _send_msg91(e164, otp, msg91)
        logger.info("Provider response status: %s", status)
        return "msg91"

    if aws_key and aws_secret:
        logger.info(
            "OTP provider configured: YES provider=sns Phone number format: %s OTP request initiated: YES",
            mask_phone(e164),
        )
        status = _send_sns(e164, otp)
        logger.info("Provider response status: %s", status)
        return "sns"

    logger.info("OTP provider configured: NO")
    raise SmsOtpError(
        "SMS_NOT_CONFIGURED",
        "SMS OTP is not configured. Set MSG91_AUTH_KEY or AWS SMS credentials on the backend.",
        503,
    )


def _otp_body(otp: str) -> str:
    minutes = os.getenv("PHONE_OTP_EXPIRE_MINUTES", "10")
    return (
        f"Your NameCardScan verification code is {otp}. "
        f"It expires in {minutes} minutes. Do not share this code."
    )


def _send_sns(e164: str, otp: str) -> int:
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise SmsOtpError("SMS_NOT_CONFIGURED", "AWS SNS client is not installed.", 503) from exc

    region = (os.getenv("AWS_SNS_REGION") or os.getenv("AWS_REGION") or "ap-south-1").strip()
    try:
        client = boto3.client(
            "sns",
            region_name=region,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        )
        response = client.publish(
            PhoneNumber=e164,
            Message=_otp_body(otp),
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {
                    "DataType": "String",
                    "StringValue": "Transactional",
                }
            },
        )
    except ClientError as exc:
        status = int((exc.response or {}).get("ResponseMetadata", {}).get("HTTPStatusCode") or 400)
        logger.warning("SNS OTP publish rejected status=%s", status)
        raise SmsOtpError(
            "PROVIDER_REJECTED",
            "The SMS provider rejected this number. Check the mobile number and try again.",
            502,
        ) from exc
    except BotoCoreError as exc:
        logger.warning("SNS OTP publish failed (network/client)")
        raise SmsOtpError(
            "PROVIDER_UNREACHABLE",
            "Could not reach the SMS provider. Try again in a moment.",
            502,
        ) from exc

    message_id = str(response.get("MessageId") or "")
    if not message_id:
        raise SmsOtpError("PROVIDER_REJECTED", "The SMS provider did not accept the message.", 502)
    return 200


def _send_msg91(e164: str, otp: str, auth_key: str) -> int:
    template_id = (os.getenv("MSG91_TEMPLATE_ID") or "").strip()
    sender = (os.getenv("MSG91_SENDER_ID") or "NCSAPP").strip() or "NCSAPP"
    mobile = re.sub(r"\D", "", e164)
    payload: dict[str, Any] = {
        "template_id": template_id,
        "mobile": mobile,
        "otp": otp,
        "otp_expiry": int(os.getenv("PHONE_OTP_EXPIRE_MINUTES", "10") or 10),
    }
    if not template_id:
        # Flow API still requires a DLT template in production; without it, use sendhttp SMS.
        url = "https://api.msg91.com/api/sendhttp.php"
        params = {
            "authkey": auth_key,
            "mobiles": mobile,
            "message": _otp_body(otp),
            "sender": sender[:6],
            "route": "4",
            "country": "91",
        }
        try:
            with httpx.Client(timeout=20.0) as client:
                res = client.get(url, params=params)
        except Exception as exc:
            logger.warning("MSG91 OTP request failed (network)")
            raise SmsOtpError(
                "PROVIDER_UNREACHABLE",
                "Could not reach the SMS provider. Try again in a moment.",
                502,
            ) from exc
        if res.status_code >= 400:
            logger.warning("Provider response status: %s", res.status_code)
            raise SmsOtpError(
                "PROVIDER_REJECTED",
                "The SMS provider rejected this number. Check the mobile number and try again.",
                502,
            )
        return res.status_code or 200

    url = "https://control.msg91.com/api/v5/otp"
    try:
        with httpx.Client(timeout=20.0) as client:
            res = client.post(
                url,
                headers={
                    "authkey": auth_key,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:
        logger.warning("MSG91 OTP request failed (network)")
        raise SmsOtpError(
            "PROVIDER_UNREACHABLE",
            "Could not reach the SMS provider. Try again in a moment.",
            502,
        ) from exc

    if res.status_code >= 400:
        logger.warning("Provider response status: %s", res.status_code)
        raise SmsOtpError(
            "PROVIDER_REJECTED",
            "The SMS provider rejected this number. Check the mobile number and try again.",
            502,
        )
    try:
        body = res.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and str(body.get("type") or "").lower() == "error":
        logger.warning("Provider response status: rejected")
        raise SmsOtpError(
            "PROVIDER_REJECTED",
            "The SMS provider rejected this number. Check the mobile number and try again.",
            502,
        )
    return res.status_code or 200
