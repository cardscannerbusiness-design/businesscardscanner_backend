"""Company-scoped 120s WhatsApp phone dedupe + outcome status for API."""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from api.outreach import resolve_whatsapp_outcome_status, whatsapp_response
from services import whatsapp_service as ws


class CompanyScopedWhatsAppDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        ws._RECENT_SENDS.clear()

    def tearDown(self) -> None:
        ws._RECENT_SENDS.clear()

    def test_same_company_first_send_allowed(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))

    def test_same_company_second_send_within_120s_blocked(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))
        self.assertFalse(ws._should_send_to_phone("9876543210", company_id="company-a"))

    def test_different_company_same_phone_allowed(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-b"))

    def test_same_company_different_phone_allowed(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))
        self.assertTrue(ws._should_send_to_phone("9123456789", company_id="company-a"))

    def test_same_company_same_phone_after_120s_allowed(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))
        key = ws._whatsapp_dedupe_key("9876543210", "company-a")
        assert key is not None
        ws._RECENT_SENDS[key] = time.time() - (ws._SEND_DEDUPE_SECONDS + 1)
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))

    def test_company_none_uses_sender_scope_not_global_none_bucket(self) -> None:
        with patch.object(
            ws,
            "_active_whatsapp_credentials",
            return_value=("token", "phone-id-1", "v21.0"),
        ):
            key1 = ws._whatsapp_dedupe_key("9876543210", None)
            self.assertEqual(key1, "sender:phone-id-1:919876543210")
            self.assertTrue(ws._should_send_to_phone("9876543210", company_id=None))
            self.assertFalse(ws._should_send_to_phone("9876543210", company_id=None))

        ws._RECENT_SENDS.clear()
        with patch.object(
            ws,
            "_active_whatsapp_credentials",
            return_value=("token", "phone-id-2", "v21.0"),
        ):
            # Different sender id must not collide with phone-id-1 history (cleared)
            # and must not share a single "None:phone" bucket with other senders.
            key2 = ws._whatsapp_dedupe_key("9876543210", None)
            self.assertEqual(key2, "sender:phone-id-2:919876543210")
            self.assertTrue(ws._should_send_to_phone("9876543210", company_id=None))

    def test_company_none_does_not_collide_with_company_scope(self) -> None:
        self.assertTrue(ws._should_send_to_phone("9876543210", company_id="company-a"))
        with patch.object(
            ws,
            "_active_whatsapp_credentials",
            return_value=("token", "phone-id-1", "v21.0"),
        ):
            self.assertTrue(ws._should_send_to_phone("9876543210", company_id=None))

    def test_dedupe_seconds_unchanged(self) -> None:
        self.assertEqual(ws._SEND_DEDUPE_SECONDS, 120)


class WhatsAppOutcomeStatusTests(unittest.TestCase):
    def test_explicit_status_preferred(self) -> None:
        self.assertEqual(
            resolve_whatsapp_outcome_status({"status": "duplicate", "error": "x"}),
            "duplicate",
        )

    def test_skip_whatsapp_status(self) -> None:
        self.assertEqual(
            resolve_whatsapp_outcome_status(
                {"error": "Skipped by request (skipWhatsApp=true).", "skipped": True}
            ),
            "skipped",
        )

    def test_already_sent_status(self) -> None:
        self.assertEqual(
            resolve_whatsapp_outcome_status(
                {"sent": True, "skipped": True, "error": "already sent"}
            ),
            "already_sent",
        )

    def test_whatsapp_response_hides_non_failure_errors(self) -> None:
        payload = whatsapp_response(
            {
                "attempted": False,
                "sent": False,
                "skipped": True,
                "status": "skipped",
                "error": "Skipped by request (skipWhatsApp=true).",
            }
        )
        self.assertEqual(payload["whatsapp_status"], "skipped")
        self.assertIsNone(payload["whatsapp_error"])

    def test_whatsapp_response_keeps_failure_error(self) -> None:
        payload = whatsapp_response(
            {
                "attempted": True,
                "sent": False,
                "status": "failed",
                "error": "No primary phone number found on the contact.",
            }
        )
        self.assertEqual(payload["whatsapp_status"], "failed")
        self.assertEqual(
            payload["whatsapp_error"],
            "No primary phone number found on the contact.",
        )

    def test_whatsapp_response_sent(self) -> None:
        payload = whatsapp_response(
            {
                "attempted": True,
                "sent": True,
                "status": "sent",
                "message_id": "wamid.x",
                "recipient_phone": "919876543210",
            }
        )
        self.assertEqual(payload["whatsapp_status"], "sent")
        self.assertTrue(payload["whatsapp_sent"])
        self.assertIsNone(payload["whatsapp_error"])


if __name__ == "__main__":
    unittest.main()
