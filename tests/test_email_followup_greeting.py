"""Email follow-up greeting uses the contact's full name."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from services.email_service import build_thank_you_email_plain


class EmailFollowUpGreetingTests(unittest.TestCase):
    def test_plain_body_uses_full_name(self) -> None:
        body = build_thank_you_email_plain("Anand Singh")
        self.assertIn("Hi Anand Singh,", body)
        self.assertNotIn("Hi Anand,", body)

    def test_plain_body_keeps_empty_name_fallback(self) -> None:
        body = build_thank_you_email_plain("  ")
        self.assertIn("Hi Valued Customer,", body)


if __name__ == "__main__":
    unittest.main()
