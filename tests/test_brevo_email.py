"""Unit tests for Brevo REST email transport — commented out with Brevo email code."""

# from __future__ import annotations
#
# import os
# import unittest
# from unittest.mock import MagicMock, patch
#
# os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-brevo-email-unit-tests")
#
# from auth import email_service as auth_email  # noqa: E402
# from services import email_service as outreach  # noqa: E402
#
#
# def _brevo_ok_response(message_id: str = "<brevo-msg-1@smtp-relay.mailin.fr>") -> MagicMock:
#     response = MagicMock()
#     response.status_code = 201
#     response.reason = "Created"
#     response.text = '{"messageId": "%s"}' % message_id
#     response.json.return_value = {"messageId": message_id}
#     return response
#
#
# def _brevo_error_response(status: int, message: str) -> MagicMock:
#     response = MagicMock()
#     response.status_code = status
#     response.reason = "Bad Request"
#     response.text = '{"message": "%s"}' % message
#     response.json.return_value = {"message": message}
#     return response
#
#
# class TestBrevoOutreachTransport(unittest.TestCase):
#     def setUp(self) -> None:
#         self.brevo_key = patch.object(outreach, "BREVO_API_KEY", "xkeysib-test-key")
#         self.brevo_sender = patch.object(
#             outreach, "BREVO_SENDER_EMAIL", "cardscannerbusiness@gmail.com"
#         )
#         self.company = patch.object(outreach, "BUSINESS_COMPANY_NAME", "CardSync")
#         self.business = patch.object(
#             outreach, "BUSINESS_EMAIL", "cardscannerbusiness@gmail.com"
#         )
#         self.mx = patch.object(outreach, "_check_recipient_mx", return_value=(True, ""))
#         self.cms = patch.object(outreach, "_cms_smtp_override_configured", return_value=False)
#         for item in (
#             self.brevo_key,
#             self.brevo_sender,
#             self.company,
#             self.business,
#             self.mx,
#             self.cms,
#         ):
#             item.start()
#             self.addCleanup(item.stop)
#
#     def test_provider_is_brevo_when_api_key_is_set(self) -> None:
#         self.assertTrue(outreach.is_brevo_configured())
#         self.assertTrue(outreach.is_email_configured())
#         self.assertEqual(outreach.get_email_provider(), "brevo")
#         self.assertEqual(outreach.smtp_sender_email(), "cardscannerbusiness@gmail.com")
#
#     @patch.object(outreach, "_send_via_smtp")
#     @patch.object(outreach.requests, "post")
#     def test_deliver_email_uses_brevo_not_ses_smtp(
#         self,
#         post: MagicMock,
#         smtp: MagicMock,
#     ) -> None:
#         post.return_value = _brevo_ok_response()
#
#         result = outreach._deliver_email(
#             "inbox@example.com",
#             subject="Thank You for Meeting Us — Name Card Scan",
#             plain_body="Hello",
#             html_body="<p>Hello</p>",
#         )
#
#         self.assertTrue(result["success"])
#         self.assertEqual(result["recipient_email"], "inbox@example.com")
#         self.assertEqual(result["message_id"], "<brevo-msg-1@smtp-relay.mailin.fr>")
#         smtp.assert_not_called()
#         post.assert_called_once()
#         args, kwargs = post.call_args
#         self.assertEqual(args[0], outreach.BREVO_API_URL)
#         self.assertEqual(kwargs["headers"]["api-key"], "xkeysib-test-key")
#         payload = kwargs["json"]
#         self.assertEqual(payload["sender"]["email"], "cardscannerbusiness@gmail.com")
#         self.assertEqual(payload["sender"]["name"], "CardSync")
#         self.assertEqual(payload["to"], [{"email": "inbox@example.com"}])
#         self.assertEqual(payload["subject"], "Thank You for Meeting Us — Name Card Scan")
#         self.assertEqual(payload["htmlContent"], "<p>Hello</p>")
#         self.assertEqual(payload["textContent"], "Hello")
#
#     @patch.object(outreach.requests, "post")
#     def test_brevo_unverified_sender_is_reported(self, post: MagicMock) -> None:
#         post.return_value = _brevo_error_response(
#             400,
#             "sender email is not valid or not verified",
#         )
#
#         result = outreach._send_via_brevo(
#             "inbox@example.com",
#             subject="Test",
#             plain_body="plain",
#             html_body="<p>html</p>",
#         )
#
#         self.assertFalse(result["success"])
#         self.assertIn("Brevo rejected the send (400)", str(result["error"]))
#         self.assertIn("not verified", str(result["error"]))
#
#     @patch.object(outreach, "_send_via_smtp")
#     def test_cms_smtp_override_still_uses_smtp(self, smtp: MagicMock) -> None:
#         smtp.return_value = {
#             "success": True,
#             "recipient_email": "inbox@example.com",
#             "error": None,
#         }
#         with patch.object(outreach, "_cms_smtp_override_configured", return_value=True):
#             result = outreach._deliver_email(
#                 "inbox@example.com",
#                 subject="Test",
#                 plain_body="plain",
#                 html_body="<p>html</p>",
#             )
#
#         self.assertTrue(result["success"])
#         smtp.assert_called_once()
#
#
# class TestBrevoAuthEmail(unittest.TestCase):
#     @patch.object(auth_email.requests, "post")
#     def test_forgot_password_style_auth_email_uses_brevo(self, post: MagicMock) -> None:
#         post.return_value = _brevo_ok_response()
#         env = {
#             "BREVO_API_KEY": "xkeysib-test-key",
#             "BREVO_SENDER_EMAIL": "cardscannerbusiness@gmail.com",
#             "BUSINESS_EMAIL": "cardscannerbusiness@gmail.com",
#             "BUSINESS_COMPANY_NAME": "CardSync",
#         }
#         with patch.dict(os.environ, env, clear=False):
#             result = auth_email._send_email(
#                 "user@example.com",
#                 "NameCardScan — Password Reset Code",
#                 "<p>Your code is 123456</p>",
#             )
#
#         self.assertTrue(result["sent"])
#         post.assert_called_once()
#         args, kwargs = post.call_args
#         self.assertEqual(args[0], auth_email.BREVO_API_URL)
#         self.assertEqual(kwargs["headers"]["api-key"], "xkeysib-test-key")
#         payload = kwargs["json"]
#         self.assertEqual(payload["sender"]["email"], "cardscannerbusiness@gmail.com")
#         self.assertEqual(payload["to"], [{"email": "user@example.com"}])
#         self.assertIn("123456", payload["htmlContent"])
#
#     @patch.object(auth_email.requests, "post")
#     def test_auth_email_skips_when_brevo_missing(self, post: MagicMock) -> None:
#         with patch.dict(
#             os.environ,
#             {"BREVO_API_KEY": "", "BREVO_SENDER_EMAIL": "", "BUSINESS_EMAIL": ""},
#             clear=False,
#         ):
#             result = auth_email._send_email("user@example.com", "Hello", "<p>Hi</p>")
#
#         self.assertFalse(result["sent"])
#         self.assertIn("Brevo is not configured", str(result.get("reason")))
#         post.assert_not_called()
#
#
# if __name__ == "__main__":
#     unittest.main()
