"""SMS OTP delivery tests — no real provider calls."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.sms_otp_service import SmsOtpError, mask_phone, send_signup_otp_sms, to_e164


class TestPhoneFormat(unittest.TestCase):
    def test_india_ten_digit(self) -> None:
        self.assertEqual(to_e164("9876543210"), "+919876543210")

    def test_already_e164_digits(self) -> None:
        self.assertEqual(to_e164("919876543210"), "+919876543210")

    def test_mask_does_not_include_full_number(self) -> None:
        masked = mask_phone("+919876543210")
        self.assertNotIn("987654", masked)
        self.assertTrue(masked.endswith("3210"))


class TestSendSignupOtpSms(unittest.TestCase):
    def test_sns_success(self) -> None:
        client = MagicMock()
        client.publish.return_value = {"MessageId": "mid-1"}
        with (
            patch.dict(
                "os.environ",
                {
                    "MSG91_AUTH_KEY": "",
                    "AWS_ACCESS_KEY_ID": "ak",
                    "AWS_SECRET_ACCESS_KEY": "sk",
                    "AWS_REGION": "ap-south-1",
                },
                clear=False,
            ),
            patch("boto3.client", return_value=client),
        ):
            provider = send_signup_otp_sms("9876543210", "123456")
        self.assertEqual(provider, "sns")
        kwargs = client.publish.call_args.kwargs
        self.assertEqual(kwargs["PhoneNumber"], "+919876543210")
        self.assertIn("verification code", kwargs["Message"].lower())

    def test_not_configured(self) -> None:
        with patch.dict(
            "os.environ",
            {"MSG91_AUTH_KEY": "", "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""},
            clear=False,
        ):
            with self.assertRaises(SmsOtpError) as ctx:
                send_signup_otp_sms("9876543210", "123456")
        self.assertEqual(ctx.exception.code, "SMS_NOT_CONFIGURED")


class TestPhoneOtpDoesNotUseWhatsAppOrEmail(unittest.TestCase):
    def test_deliver_otp_calls_sms_only(self) -> None:
        from auth import phone_otp_service as otp

        with (
            patch("services.sms_otp_service.send_signup_otp_sms", return_value="sns") as sms,
            patch("services.whatsapp_service.send_whatsapp_text") as wa,
            patch("auth.email_service.send_mobile_verification_otp") as email,
        ):
            via = otp._deliver_otp("919876543210", "654321")
        self.assertEqual(via, "sns")
        sms.assert_called_once()
        wa.assert_not_called()
        email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
