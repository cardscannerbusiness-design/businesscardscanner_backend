"""Email structured outcome status for API responses."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from api.outreach import email_response, resolve_email_outcome_status
from services import email_service as es


class EmailOutcomeStatusResolveTests(unittest.TestCase):
    def test_sent(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status({"sent": True, "status": "sent"}),
            "sent",
        )

    def test_skipped(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {
                    "skipped": True,
                    "status": "skipped",
                    "error": "Skipped by request (skipEmail=true).",
                }
            ),
            "skipped",
        )

    def test_duplicate(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {
                    "status": "duplicate",
                    "error": "Duplicate email send skipped for this address.",
                }
            ),
            "duplicate",
        )

    def test_already_sent(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {"sent": True, "skipped": True, "error": "already sent"}
            ),
            "already_sent",
        )

    def test_disabled(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {"status": "disabled", "error": "Email auto-send is disabled."}
            ),
            "disabled",
        )

    def test_offline(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {
                    "status": "offline",
                    "error": "Offline mode — email will send after the contact syncs.",
                }
            ),
            "offline",
        )

    def test_locked(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status({"status": "locked", "skipped": True}),
            "locked",
        )

    def test_failed_missing_email(self) -> None:
        self.assertEqual(
            resolve_email_outcome_status(
                {
                    "status": "failed",
                    "error": "No primary email address found on the contact.",
                }
            ),
            "failed",
        )


class EmailResponseApiTests(unittest.TestCase):
    def test_hides_non_failure_errors(self) -> None:
        payload = email_response(
            {
                "attempted": False,
                "sent": False,
                "skipped": True,
                "status": "skipped",
                "error": "Skipped by request (skipEmail=true).",
            }
        )
        self.assertEqual(payload["email_status"], "skipped")
        self.assertTrue(payload["email_skipped"])
        self.assertIsNone(payload["email_error"])

    def test_duplicate_hides_error(self) -> None:
        payload = email_response(
            {
                "status": "duplicate",
                "skipped": True,
                "error": "Duplicate email send skipped for this address.",
            }
        )
        self.assertEqual(payload["email_status"], "duplicate")
        self.assertIsNone(payload["email_error"])

    def test_already_sent_hides_error(self) -> None:
        payload = email_response(
            {
                "attempted": True,
                "sent": True,
                "skipped": True,
                "status": "already_sent",
                "error": "already sent",
                "extracted_email": "a@example.com",
            }
        )
        self.assertEqual(payload["email_status"], "already_sent")
        self.assertTrue(payload["email_sent"])
        self.assertIsNone(payload["email_error"])

    def test_keeps_failure_error(self) -> None:
        payload = email_response(
            {
                "attempted": True,
                "sent": False,
                "status": "failed",
                "error": "No primary email address found on the contact.",
            }
        )
        self.assertEqual(payload["email_status"], "failed")
        self.assertEqual(
            payload["email_error"],
            "No primary email address found on the contact.",
        )

    def test_sent_success(self) -> None:
        payload = email_response(
            {
                "attempted": True,
                "sent": True,
                "status": "sent",
                "recipient_email": "a@example.com",
                "extracted_email": "a@example.com",
            }
        )
        self.assertEqual(payload["email_status"], "sent")
        self.assertTrue(payload["email_sent"])
        self.assertIsNone(payload["email_error"])


class ScheduleEmailStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        es._RECENT_SENDS.clear()

    def tearDown(self) -> None:
        es._RECENT_SENDS.clear()

    async def test_disabled_status(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=False):
            result = await es.schedule_email_for_contact({"email": "a@example.com"})
        self.assertEqual(result["status"], "disabled")
        self.assertTrue(result["skipped"])

    async def test_offline_status(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True):
            result = await es.schedule_email_for_contact(
                {"email": "a@example.com"},
                online_mode=False,
                on_zoho_sync=False,
            )
        self.assertEqual(result["status"], "offline")

    async def test_not_configured_failed(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=False
        ):
            result = await es.schedule_email_for_contact(
                {"email": "a@example.com"},
                online_mode=True,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("not configured", str(result["error"]).lower())

    async def test_missing_email_failed(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ):
            result = await es.schedule_email_for_contact({}, online_mode=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("No primary email", result["error"])

    async def test_invalid_email_failed(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ):
            result = await es.schedule_email_for_contact(
                {"email": "not-an-email"},
                online_mode=True,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("Invalid email", result["error"])

    async def test_duplicate_status(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ), patch.object(es, "_should_send_to_email", return_value=False), patch.object(
            es, "is_test_recipient_mode", return_value=False
        ):
            result = await es.schedule_email_for_contact(
                {"email": "dup@example.com"},
                online_mode=True,
            )
        self.assertEqual(result["status"], "duplicate")
        self.assertTrue(result["skipped"])

    async def test_already_sent_status(self) -> None:
        existing = {"notes": "[email:sent]", "email": "a@example.com"}
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ), patch(
            "services.contact_storage.get_contact", return_value=existing
        ), patch(
            "services.contact_storage.has_email_sent", return_value=True
        ):
            result = await es.schedule_email_for_contact(
                {"email": "a@example.com"},
                online_mode=True,
                contact_id="c1",
                skip_if_already_sent=True,
            )
        self.assertEqual(result["status"], "already_sent")
        self.assertTrue(result["sent"])
        self.assertTrue(result["skipped"])

    async def test_smtp_failure_status(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ), patch.object(es, "_should_send_to_email", return_value=True), patch.object(
            es, "is_test_recipient_mode", return_value=True
        ), patch.object(
            es,
            "send_business_thank_you_email",
            return_value={
                "success": False,
                "error": "SMTP authentication failed: 535",
                "recipient_email": "a@example.com",
                "cc_emails": [],
            },
        ):
            result = await es.schedule_email_for_contact(
                {"email": "a@example.com", "fullName": "Ada"},
                online_mode=True,
            )
        self.assertEqual(result["status"], "failed")
        self.assertIn("535", result["error"])

    async def test_success_sent_status(self) -> None:
        with patch.object(es, "_auto_send_enabled", return_value=True), patch.object(
            es, "is_email_configured", return_value=True
        ), patch.object(es, "_should_send_to_email", return_value=True), patch.object(
            es, "is_test_recipient_mode", return_value=True
        ), patch.object(
            es,
            "send_business_thank_you_email",
            return_value={
                "success": True,
                "recipient_email": "a@example.com",
                "cc_emails": [],
            },
        ):
            result = await es.schedule_email_for_contact(
                {"email": "a@example.com", "fullName": "Ada"},
                online_mode=True,
            )
        self.assertEqual(result["status"], "sent")
        self.assertTrue(result["sent"])
        self.assertIsNone(result["error"])


class EmailLockSkipStatusTests(unittest.TestCase):
    def test_freemium_lock_in_response(self) -> None:
        payload = email_response(
            {
                "attempted": False,
                "sent": False,
                "skipped": True,
                "status": "locked",
                "error": "WhatsApp and Email are locked because your Freemium limit has been reached.",
            }
        )
        self.assertEqual(payload["email_status"], "locked")
        self.assertIsNone(payload["email_error"])

    def test_cms_lock_in_response(self) -> None:
        payload = email_response(
            {
                "attempted": False,
                "sent": False,
                "skipped": True,
                "status": "locked",
                "error": "Email is locked by Super Admin in CMS for your company.",
            }
        )
        self.assertEqual(payload["email_status"], "locked")
        self.assertIsNone(payload["email_error"])


if __name__ == "__main__":
    unittest.main()
