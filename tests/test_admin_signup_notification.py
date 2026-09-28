"""Tests for the Admin signup notification email."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-admin-signup-notification")

from auth.email_service import (  # noqa: E402
    admin_signup_notification_recipients,
    parse_notification_emails,
    send_admin_signup_notification_email,
)
from auth.registration_service import create_admin_registration  # noqa: E402

FIVE_RECIPIENTS = [
    "onboarding@namecardscan.com",
    "sugitha.ulavi@gmail.com",
    "nanduja.ulacab@gmail.com",
    "dhana@ulavitech.com",
    "shwethars.ulavi@gmail.com",
]
FIVE_ENV = ",".join(FIVE_RECIPIENTS)


class TestAdminSignupNotificationEmail(unittest.TestCase):
    def test_empty_recipient_entries_are_ignored(self) -> None:
        parsed = parse_notification_emails(
            " onboarding@namecardscan.com, , sugitha.ulavi@gmail.com, ,"
        )
        self.assertEqual(
            parsed,
            ["onboarding@namecardscan.com", "sugitha.ulavi@gmail.com"],
        )

    @patch.dict(
        os.environ,
        {"ADMIN_SIGNUP_NOTIFICATION_EMAILS": f"{FIVE_ENV}, , "},
        clear=False,
    )
    def test_configured_list_is_the_five_recipients(self) -> None:
        self.assertEqual(admin_signup_notification_recipients(), FIVE_RECIPIENTS)

    @patch.dict(
        os.environ,
        {"ADMIN_SIGNUP_NOTIFICATION_EMAILS": "", "BUSINESS_EMAIL": "owner@example.com"},
        clear=False,
    )
    def test_empty_list_falls_back_to_business_email(self) -> None:
        self.assertEqual(admin_signup_notification_recipients(), ["owner@example.com"])

    @patch("auth.email_service._send_email", return_value={"sent": True})
    def test_subject_recipient_and_fields(self, mock_send: MagicMock) -> None:
        result = send_admin_signup_notification_email(
            recipients=FIVE_RECIPIENTS,
            admin_name="John Doe",
            company_name="ABC Technologies",
            designation="Sales Manager",
            admin_email="john@example.com",
            phone="+919876543210",
            signup_at="2026-09-19 06:20:00 UTC",
        )
        self.assertTrue(result.get("sent"))
        mock_send.assert_called_once()
        _to_email, subject, html = mock_send.call_args[0]
        self.assertEqual(mock_send.call_args.kwargs["bcc"], FIVE_RECIPIENTS)
        self.assertEqual(subject, "New Admin Registration - NameCardScan")
        self.assertIn("Administrator Details", html)
        self.assertIn("THE NAMECARDSCAN WORKFLOW", html)
        self.assertIn("New Admin", html)
        self.assertIn("John Doe", html)
        self.assertIn("ABC Technologies", html)
        self.assertIn("Sales Manager", html)
        self.assertIn("john@example.com", html)
        self.assertIn("+919876543210", html)
        self.assertIn("https://api.namecardscan.com/assets/logo-mark.png", html)
        self.assertNotIn("Company Code", html)
        self.assertNotIn("{{company_code}}", html)
        self.assertNotIn("abc-tech", html)
        self.assertIn("2026-09-19 06:20:00 UTC", html)
        self.assertNotIn("{{", html)
        self.assertIn("can sign in immediately", html)
        self.assertNotIn("pending", html.lower())
        self.assertNotIn("password", html.lower())

    @patch("auth.email_service._send_email", return_value={"sent": False, "reason": "SMTP not configured"})
    def test_send_failure_returns_result_without_raising(self, mock_send: MagicMock) -> None:
        result = send_admin_signup_notification_email(
            recipients=["onboarding@namecardscan.com"],
            admin_name="Ada",
            company_name="Co",
            designation="CEO",
            admin_email="ada@example.com",
            phone="+10000000000",
            signup_at="2026-09-19 00:00:00 UTC",
        )
        self.assertFalse(result.get("sent"))
        mock_send.assert_called_once()


class TestAdminSignupNotificationCallSite(unittest.TestCase):
    """Verify create_admin_registration notifies BUSINESS_EMAIL after commit."""

    def _mock_db_cursor(self) -> MagicMock:
        cur = MagicMock()
        # users lookup, invitations lookup
        cur.fetchone.side_effect = [None, None]
        cur.fetchall.return_value = []
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cur
        cursor_cm.__exit__.return_value = False
        return cursor_cm

    @patch("services.google_sheets_service.fire_ensure_company_sheet", MagicMock())
    @patch("auth.registration_service.audit_service")
    @patch("auth.registration_service.send_admin_signup_notification_email")
    @patch("auth.registration_service.send_welcome_email", return_value={"sent": True})
    @patch(
        "auth.registration_service._create_admin_and_company",
        return_value=("user-1", "co-1", "abc-code"),
    )
    @patch("auth.registration_service.db_cursor")
    @patch("auth.registration_service.hash_password", return_value="hashed")
    @patch("auth.phone_otp_service.assert_phone_available")
    @patch("auth.phone_otp_service.normalize_phone", return_value="919876543210")
    @patch.dict(os.environ, {"ADMIN_SIGNUP_NOTIFICATION_EMAILS": FIVE_ENV}, clear=False)
    def test_notification_sent_to_business_email_after_signup(
        self,
        _norm: MagicMock,
        _avail: MagicMock,
        _hash: MagicMock,
        mock_db: MagicMock,
        _create: MagicMock,
        mock_welcome: MagicMock,
        mock_notify: MagicMock,
        _audit: MagicMock,
    ) -> None:
        mock_db.return_value = self._mock_db_cursor()
        mock_notify.return_value = {"sent": True}

        result = create_admin_registration(
            full_name="John Doe",
            email="john.notify@example.com",
            password="ValidPass1!",
            phone="+919876543210",
            designation="Sales Manager",
            company_name="ABC Technologies",
        )

        self.assertTrue(result["success"])
        mock_welcome.assert_called_once()
        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs["recipients"], FIVE_RECIPIENTS)
        self.assertEqual(kwargs["admin_name"], "John Doe")
        self.assertEqual(kwargs["company_name"], "ABC Technologies")
        self.assertEqual(kwargs["designation"], "Sales Manager")
        self.assertEqual(kwargs["admin_email"], "john.notify@example.com")
        self.assertEqual(kwargs["phone"], "+919876543210")
        self.assertNotIn("company_code", kwargs)
        self.assertIn("UTC", kwargs["signup_at"])

    @patch("services.google_sheets_service.fire_ensure_company_sheet", MagicMock())
    @patch("auth.registration_service.audit_service")
    @patch(
        "auth.registration_service.send_admin_signup_notification_email",
        side_effect=RuntimeError("smtp down"),
    )
    @patch("auth.registration_service.send_welcome_email", return_value={"sent": True})
    @patch(
        "auth.registration_service._create_admin_and_company",
        return_value=("user-1", "co-1", "abc-code"),
    )
    @patch("auth.registration_service.db_cursor")
    @patch("auth.registration_service.hash_password", return_value="hashed")
    @patch("auth.phone_otp_service.assert_phone_available")
    @patch("auth.phone_otp_service.normalize_phone", return_value="919876543210")
    @patch.dict(os.environ, {"ADMIN_SIGNUP_NOTIFICATION_EMAILS": FIVE_ENV}, clear=False)
    def test_notification_failure_does_not_break_signup(
        self,
        _norm: MagicMock,
        _avail: MagicMock,
        _hash: MagicMock,
        mock_db: MagicMock,
        _create: MagicMock,
        mock_welcome: MagicMock,
        mock_notify: MagicMock,
        _audit: MagicMock,
    ) -> None:
        mock_db.return_value = self._mock_db_cursor()

        result = create_admin_registration(
            full_name="John Doe",
            email="jane.notify@example.com",
            password="ValidPass1!",
            phone="+919876543210",
            designation="Sales Manager",
            company_name="ABC Technologies",
        )

        self.assertTrue(result["success"])
        mock_welcome.assert_called_once()
        mock_notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
