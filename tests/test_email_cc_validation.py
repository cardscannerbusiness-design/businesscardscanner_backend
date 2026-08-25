"""Tests for CC email validation and send preparation."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.email_cc_validation import validate_cc_address_list


class TestCcAddressValidation(unittest.TestCase):
    def test_single_valid_cc(self) -> None:
        result = validate_cc_address_list(
            ["owner@example.com"],
            to_address="contact@example.com",
        )
        self.assertEqual(result.valid, ["owner@example.com"])
        self.assertEqual(result.invalid, [])

    def test_multiple_valid_cc(self) -> None:
        result = validate_cc_address_list(
            ["owner1@example.com", "owner2@example.com"],
            to_address="contact@example.com",
        )
        self.assertEqual(
            result.valid,
            ["owner1@example.com", "owner2@example.com"],
        )
        self.assertEqual(result.invalid, [])

    def test_no_cc_recipients(self) -> None:
        result = validate_cc_address_list(None, to_address="contact@example.com")
        self.assertEqual(result.valid, [])
        self.assertEqual(result.invalid, [])

    def test_invalid_cc_address(self) -> None:
        result = validate_cc_address_list(
            ["not-an-email"],
            to_address="contact@example.com",
        )
        self.assertEqual(result.valid, [])
        self.assertEqual(len(result.invalid), 1)
        self.assertIn("Invalid email format", result.invalid[0]["reason"])

    def test_mixed_valid_and_invalid_cc(self) -> None:
        result = validate_cc_address_list(
            ["owner@example.com", "bad@", "owner2@example.com"],
            to_address="contact@example.com",
        )
        self.assertEqual(
            result.valid,
            ["owner@example.com", "owner2@example.com"],
        )
        self.assertEqual(len(result.invalid), 1)

    def test_cc_cannot_match_to(self) -> None:
        result = validate_cc_address_list(
            ["contact@example.com"],
            to_address="contact@example.com",
        )
        self.assertEqual(result.valid, [])
        self.assertEqual(len(result.invalid), 1)
        self.assertIn("cannot match the To recipient", result.invalid[0]["reason"])


class TestEmailSendWithCc(unittest.TestCase):
    @patch("services.email_service._send_cc_scanned_details_emails", return_value=[{"email": "owner@example.com", "success": True}])
    @patch("services.email_service._deliver_email")
    @patch("services.email_service.is_email_configured", return_value=True)
    @patch("services.email_service.get_email_provider", return_value="smtp")
    def test_successful_send_with_cc(
        self,
        _provider: MagicMock,
        _configured: MagicMock,
        deliver: MagicMock,
        _cc_send: MagicMock,
    ) -> None:
        from services.email_service import send_business_thank_you_email

        deliver.return_value = {
            "success": True,
            "recipient_email": "contact@example.com",
            "error": None,
        }

        contact = {"fullName": "Jane", "email": "contact@example.com", "phone": "123"}
        result = send_business_thank_you_email(
            "contact@example.com",
            cc_addresses=["owner@example.com"],
            contact=contact,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["cc_emails"], ["owner@example.com"])
        deliver.assert_called_once()
        call_kwargs = deliver.call_args.kwargs
        self.assertIsNone(call_kwargs["cc_addresses"])
        from unittest.mock import ANY

        _cc_send.assert_called_once_with(
            ["owner@example.com"],
            contact=contact,
            sender_role=ANY,
            smtp_lane=ANY,
        )

    @patch("services.email_service._deliver_email")
    @patch("services.email_service.is_email_configured", return_value=True)
    @patch("services.email_service.get_email_provider", return_value="smtp")
    def test_email_service_failure(
        self,
        _provider: MagicMock,
        _configured: MagicMock,
        deliver: MagicMock,
    ) -> None:
        from services.email_service import send_business_thank_you_email

        deliver.return_value = {
            "success": False,
            "recipient_email": "contact@example.com",
            "cc_emails": [],
            "error": "SMTP error while sending to contact@example.com: connection refused",
        }

        result = send_business_thank_you_email("contact@example.com")

        self.assertFalse(result["success"])
        self.assertIn("SMTP error", str(result["error"]))


if __name__ == "__main__":
    unittest.main()
