"""Unit tests for role-based Amazon SES / SMTP email transport."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-ses-email-unit-tests")

from auth import email_service as auth_email  # noqa: E402
from services import email_service as outreach  # noqa: E402


class TestRoleBasedOutreachTransport(unittest.TestCase):
    def setUp(self) -> None:
        self.patches = [
            patch.object(outreach, "SMTP_HOST", "email-smtp.ap-south-1.amazonaws.com"),
            patch.object(outreach, "SMTP_PORT", 587),
            patch.object(outreach, "SMTP_FROM", "onboarding@example.com"),
            patch.object(outreach, "SMTP_INTERNAL_HOST", "email-smtp.ap-south-1.amazonaws.com"),
            patch.object(outreach, "SMTP_INTERNAL_PORT", 587),
            patch.object(outreach, "SMTP_INTERNAL_USER", "AKIAINTERNALKEY12345"),
            patch.object(outreach, "SMTP_INTERNAL_PASSWORD", "internal-password"),
            patch.object(outreach, "SMTP_INTERNAL_FROM", "internal@example.com"),
            patch.object(outreach, "SMTP_EXTERNAL_HOST", "email-smtp.ap-south-1.amazonaws.com"),
            patch.object(outreach, "SMTP_EXTERNAL_PORT", 587),
            patch.object(outreach, "SMTP_EXTERNAL_USER", "AKIAEXTERNALKEY12345"),
            patch.object(outreach, "SMTP_EXTERNAL_PASSWORD", "external-password"),
            patch.object(outreach, "SMTP_EXTERNAL_FROM", "external@example.com"),
            patch.object(outreach, "BUSINESS_EMAIL", "reply@example.com"),
            patch.object(outreach, "_cms_smtp_override_configured", return_value=False),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_resolve_lane_super_admin_internal(self) -> None:
        self.assertEqual(outreach.resolve_smtp_lane(sender_role="SUPER_ADMIN"), "internal")

    def test_resolve_lane_admin_external(self) -> None:
        self.assertEqual(outreach.resolve_smtp_lane(sender_role="ADMIN"), "external")
        self.assertEqual(outreach.resolve_smtp_lane(sender_role="USER"), "external")

    def test_sender_email_follows_role(self) -> None:
        self.assertEqual(
            outreach.smtp_sender_email(sender_role="SUPER_ADMIN"),
            "internal@example.com",
        )
        self.assertEqual(
            outreach.smtp_sender_email(sender_role="ADMIN"),
            "external@example.com",
        )

    @patch("services.email_service._send_via_smtp_relay")
    @patch("services.email_service._check_recipient_mx", return_value=(True, ""))
    def test_deliver_email_uses_internal_for_super_admin(
        self, _mx: MagicMock, relay: MagicMock
    ) -> None:
        relay.return_value = {"success": True, "recipient_email": "to@example.com"}
        result = outreach._deliver_email(
            "to@example.com",
            subject="Hello",
            plain_body="hi",
            html_body="<p>hi</p>",
            sender_role="SUPER_ADMIN",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result.get("smtp_lane"), "internal")
        kwargs = relay.call_args.kwargs
        self.assertEqual(kwargs["smtp_user"], "AKIAINTERNALKEY12345")
        self.assertEqual(kwargs["from_address"], "internal@example.com")

    @patch("services.email_service._send_via_smtp_relay")
    @patch("services.email_service._check_recipient_mx", return_value=(True, ""))
    def test_deliver_email_uses_external_for_admin(
        self, _mx: MagicMock, relay: MagicMock
    ) -> None:
        relay.return_value = {"success": True, "recipient_email": "to@example.com"}
        result = outreach._deliver_email(
            "to@example.com",
            subject="Hello",
            plain_body="hi",
            html_body="<p>hi</p>",
            sender_role="ADMIN",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result.get("smtp_lane"), "external")
        kwargs = relay.call_args.kwargs
        self.assertEqual(kwargs["smtp_user"], "AKIAEXTERNALKEY12345")
        self.assertEqual(kwargs["from_address"], "external@example.com")

    @patch("services.email_service._send_via_smtp_relay")
    @patch("services.email_service._check_recipient_mx", return_value=(True, ""))
    def test_external_falls_back_to_internal_when_primary_fails(
        self, _mx: MagicMock, relay: MagicMock
    ) -> None:
        relay.side_effect = [
            {
                "success": False,
                "recipient_email": "to@example.com",
                "error": "Amazon SES (external) authentication failed: (535, b'Invalid')",
            },
            {
                "success": True,
                "recipient_email": "to@example.com",
                "error": None,
            },
        ]
        result = outreach._deliver_email(
            "to@example.com",
            subject="Hello",
            plain_body="hi",
            html_body="<p>hi</p>",
            sender_role="ADMIN",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result.get("smtp_profile"), "internal")
        self.assertEqual(result.get("smtp_lane"), "external")
        self.assertEqual(relay.call_count, 2)
        second = relay.call_args_list[1].kwargs
        self.assertEqual(second["smtp_user"], "AKIAINTERNALKEY12345")
        self.assertEqual(second["from_address"], "internal@example.com")

    @patch("services.email_service._send_via_smtp_relay")
    @patch("services.email_service._check_recipient_mx", return_value=(True, ""))
    def test_internal_falls_back_to_external_when_primary_fails(
        self, _mx: MagicMock, relay: MagicMock
    ) -> None:
        relay.side_effect = [
            {
                "success": False,
                "recipient_email": "to@example.com",
                "error": "Amazon SES (internal) authentication failed: (535, b'Invalid')",
            },
            {
                "success": True,
                "recipient_email": "to@example.com",
                "error": None,
            },
        ]
        result = outreach._deliver_email(
            "to@example.com",
            subject="Hello",
            plain_body="hi",
            html_body="<p>hi</p>",
            sender_role="SUPER_ADMIN",
        )
        self.assertTrue(result["success"])
        self.assertEqual(result.get("smtp_profile"), "external")
        self.assertEqual(result.get("smtp_lane"), "internal")
        self.assertEqual(relay.call_count, 2)
        first = relay.call_args_list[0].kwargs
        second = relay.call_args_list[1].kwargs
        self.assertEqual(first["smtp_user"], "AKIAINTERNALKEY12345")
        self.assertEqual(second["smtp_user"], "AKIAEXTERNALKEY12345")
        self.assertEqual(second["from_address"], "external@example.com")

    def test_gmail_cms_user_does_not_use_ses_host(self) -> None:
        with patch(
            "services.admin_runtime_config.runtime_email",
            return_value={
                "smtp_user": "tenant@gmail.com",
                "smtp_password": "gmail-app-password",
                "smtp_host": "",
                "smtp_from": "tenant@gmail.com",
            },
        ):
            profile = outreach._cms_smtp_profile()
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile["host"], "smtp.gmail.com")
        self.assertEqual(profile["user"], "tenant@gmail.com")
        self.assertEqual(profile["label"], "cms")

    @patch("services.email_service._send_via_smtp_relay")
    @patch("services.email_service._check_recipient_mx", return_value=(True, ""))
    def test_cms_gmail_auth_failure_falls_back_to_ses(
        self, _mx: MagicMock, relay: MagicMock
    ) -> None:
        relay.side_effect = [
            {
                "success": False,
                "recipient_email": "to@example.com",
                "error": "SMTP (cms) authentication failed: (535, b'Invalid')",
            },
            {
                "success": True,
                "recipient_email": "to@example.com",
                "error": None,
            },
        ]
        cms_profile = {
            "host": "smtp.gmail.com",
            "port": 587,
            "user": "tenant@gmail.com",
            "password": "bad-gmail-password",
            "from": "tenant@gmail.com",
            "reply": "tenant@gmail.com",
            "name": "CMS",
            "label": "cms",
        }
        with patch.object(outreach, "_cms_smtp_profile", return_value=cms_profile):
            result = outreach._deliver_email(
                "to@example.com",
                subject="Hello",
                plain_body="hi",
                html_body="<p>hi</p>",
                sender_role="ADMIN",
            )
        self.assertTrue(result["success"])
        self.assertEqual(result.get("smtp_profile"), "external")
        self.assertEqual(relay.call_count, 2)
        self.assertEqual(relay.call_args_list[0].kwargs["smtp_user"], "tenant@gmail.com")
        self.assertEqual(relay.call_args_list[1].kwargs["smtp_user"], "AKIAEXTERNALKEY12345")
        self.assertEqual(
            relay.call_args_list[1].kwargs["smtp_host"],
            "email-smtp.ap-south-1.amazonaws.com",
        )


class TestRoleBasedAuthEmail(unittest.TestCase):
    @patch("auth.email_service.smtplib.SMTP")
    def test_auth_internal_for_super_admin(self, smtp_cls: MagicMock) -> None:
        server = MagicMock()
        smtp_cls.return_value.__enter__.return_value = server
        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "email-smtp.ap-south-1.amazonaws.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "onboarding@example.com",
                "SMTP_INTERNAL_USER": "AKIAINTERNALKEY12345",
                "SMTP_INTERNAL_PASSWORD": "internal-password",
                "SMTP_INTERNAL_FROM": "internal@example.com",
                "SMTP_EXTERNAL_USER": "AKIAEXTERNALKEY12345",
                "SMTP_EXTERNAL_PASSWORD": "external-password",
                "SMTP_EXTERNAL_FROM": "external@example.com",
                "BUSINESS_EMAIL": "reply@example.com",
            },
            clear=False,
        ):
            result = auth_email._send_email(
                "user@example.com",
                "Subj",
                "<p>x</p>",
                sender_role="SUPER_ADMIN",
            )
        self.assertTrue(result.get("sent"))
        self.assertEqual(result.get("smtp_lane"), "internal")
        server.login.assert_called_once_with("AKIAINTERNALKEY12345", "internal-password")

    @patch("auth.email_service.smtplib.SMTP")
    def test_auth_external_for_admin(self, smtp_cls: MagicMock) -> None:
        server = MagicMock()
        smtp_cls.return_value.__enter__.return_value = server
        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "email-smtp.ap-south-1.amazonaws.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "onboarding@example.com",
                "SMTP_INTERNAL_USER": "AKIAINTERNALKEY12345",
                "SMTP_INTERNAL_PASSWORD": "internal-password",
                "SMTP_INTERNAL_FROM": "internal@example.com",
                "SMTP_EXTERNAL_USER": "AKIAEXTERNALKEY12345",
                "SMTP_EXTERNAL_PASSWORD": "external-password",
                "SMTP_EXTERNAL_FROM": "external@example.com",
                "BUSINESS_EMAIL": "reply@example.com",
            },
            clear=False,
        ):
            result = auth_email._send_email(
                "user@example.com",
                "Subj",
                "<p>x</p>",
                sender_role="ADMIN",
            )
        self.assertTrue(result.get("sent"))
        self.assertEqual(result.get("smtp_lane"), "external")
        server.login.assert_called_once_with("AKIAEXTERNALKEY12345", "external-password")

    @patch("auth.email_service.smtplib.SMTP")
    def test_auth_internal_falls_back_to_external(self, smtp_cls: MagicMock) -> None:
        primary = MagicMock()
        primary.login.side_effect = __import__("smtplib").SMTPAuthenticationError(
            535, b"Authentication Credentials Invalid"
        )
        fallback = MagicMock()
        smtp_cls.return_value.__enter__.side_effect = [primary, fallback]
        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "email-smtp.ap-south-1.amazonaws.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "onboarding@example.com",
                "SMTP_INTERNAL_USER": "AKIAINTERNALKEY12345",
                "SMTP_INTERNAL_PASSWORD": "bad-internal-password",
                "SMTP_INTERNAL_FROM": "internal@example.com",
                "SMTP_EXTERNAL_USER": "AKIAEXTERNALKEY12345",
                "SMTP_EXTERNAL_PASSWORD": "external-password",
                "SMTP_EXTERNAL_FROM": "external@example.com",
                "BUSINESS_EMAIL": "reply@example.com",
            },
            clear=False,
        ):
            result = auth_email._send_email(
                "user@example.com",
                "Subj",
                "<p>x</p>",
                sender_role="SUPER_ADMIN",
            )
        self.assertTrue(result.get("sent"))
        self.assertEqual(result.get("smtp_lane"), "internal")
        self.assertEqual(result.get("smtp_profile"), "external")
        self.assertEqual(smtp_cls.call_count, 2)
        fallback.login.assert_called_once_with("AKIAEXTERNALKEY12345", "external-password")

    @patch("auth.email_service.smtplib.SMTP")
    def test_auth_external_falls_back_to_internal(self, smtp_cls: MagicMock) -> None:
        primary = MagicMock()
        primary.login.side_effect = __import__("smtplib").SMTPAuthenticationError(
            535, b"Authentication Credentials Invalid"
        )
        fallback = MagicMock()
        smtp_cls.return_value.__enter__.side_effect = [primary, fallback]
        with patch.dict(
            os.environ,
            {
                "SMTP_HOST": "email-smtp.ap-south-1.amazonaws.com",
                "SMTP_PORT": "587",
                "SMTP_FROM": "onboarding@example.com",
                "SMTP_INTERNAL_USER": "AKIAINTERNALKEY12345",
                "SMTP_INTERNAL_PASSWORD": "internal-password",
                "SMTP_INTERNAL_FROM": "internal@example.com",
                "SMTP_EXTERNAL_USER": "AKIAEXTERNALKEY12345",
                "SMTP_EXTERNAL_PASSWORD": "bad-external-password",
                "SMTP_EXTERNAL_FROM": "external@example.com",
                "BUSINESS_EMAIL": "reply@example.com",
            },
            clear=False,
        ):
            result = auth_email._send_email(
                "user@example.com",
                "Subj",
                "<p>x</p>",
                sender_role="ADMIN",
            )
        self.assertTrue(result.get("sent"))
        self.assertEqual(result.get("smtp_lane"), "external")
        self.assertEqual(result.get("smtp_profile"), "internal")
        self.assertEqual(smtp_cls.call_count, 2)
        fallback.login.assert_called_once_with("AKIAINTERNALKEY12345", "internal-password")


if __name__ == "__main__":
    unittest.main()
