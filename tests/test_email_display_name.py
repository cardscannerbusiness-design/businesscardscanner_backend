"""Company email display-name normalization (From header only)."""

from __future__ import annotations

import unittest
from email.utils import formataddr, parseaddr

from services.email_display_name import normalize_email_display_name


class TestNormalizeEmailDisplayName(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self) -> None:
        self.assertEqual(
            normalize_email_display_name("  ABC   Company  "),
            "ABC Company",
        )

    def test_empty_is_allowed(self) -> None:
        self.assertEqual(normalize_email_display_name(""), "")
        self.assertEqual(normalize_email_display_name("   "), "")
        self.assertEqual(normalize_email_display_name(None), "")

    def test_strips_angle_brackets_and_newlines(self) -> None:
        self.assertEqual(
            normalize_email_display_name("ABC <spoof@x.com>\nCompany"),
            "ABC spoof@x.com Company",
        )

    def test_caps_length(self) -> None:
        self.assertEqual(len(normalize_email_display_name("A" * 400)), 255)

    def test_formataddr_keeps_mailbox_unchanged(self) -> None:
        formatted = formataddr(("ABC Company", "onboarding@namecardscan.com"))
        name, addr = parseaddr(formatted)
        self.assertEqual(name, "ABC Company")
        self.assertEqual(addr, "onboarding@namecardscan.com")


if __name__ == "__main__":
    unittest.main()
