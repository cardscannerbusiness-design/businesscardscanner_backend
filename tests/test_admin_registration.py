"""Validation tests for Admin self-registration (no invitation changes)."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-admin-registration-unit-tests")

from auth.registration_service import (  # noqa: E402
    MSG_PENDING_APPROVAL,
    MSG_SIGNUP_CREATED,
    RegistrationError,
    _split_name,
    create_admin_registration,
)


class TestAdminRegistrationValidation(unittest.TestCase):
    def test_split_name(self) -> None:
        self.assertEqual(_split_name("Ada Lovelace"), ("Ada", "Lovelace"))
        self.assertEqual(_split_name("Ada"), ("Ada", ""))

    def test_invalid_email(self) -> None:
        with self.assertRaises(RegistrationError) as ctx:
            create_admin_registration(
                full_name="Ada Lovelace",
                email="not-an-email",
                password="ValidPass1!",
                company_name="Ulavi",
            )
        self.assertEqual(ctx.exception.code, "INVALID_EMAIL")

    def test_missing_company(self) -> None:
        with self.assertRaises(RegistrationError) as ctx:
            create_admin_registration(
                full_name="Ada Lovelace",
                email="ada@example.com",
                password="ValidPass1!",
                company_name="  ",
            )
        self.assertEqual(ctx.exception.code, "INVALID_COMPANY")

    def test_weak_password(self) -> None:
        with self.assertRaises(RegistrationError) as ctx:
            create_admin_registration(
                full_name="Ada Lovelace",
                email="ada@example.com",
                password="password",
                company_name="Ulavi",
            )
        self.assertEqual(ctx.exception.code, "WEAK_PASSWORD")

    def test_user_missing_company(self) -> None:
        from auth.registration_service import create_user_registration

        with self.assertRaises(RegistrationError) as ctx:
            create_user_registration(
                full_name="Casey User",
                email="casey@example.com",
                password="ValidPass1!",
                company_name="",
                company_code="",
            )
        self.assertEqual(ctx.exception.code, "SIGNUP_CLOSED")

    def test_pending_message(self) -> None:
        self.assertIn("pending SuperAdmin approval", MSG_PENDING_APPROVAL)

    def test_signup_created_message(self) -> None:
        self.assertIn("You can now sign in", MSG_SIGNUP_CREATED)
        self.assertNotIn("pending", MSG_SIGNUP_CREATED.lower())

    def test_user_self_registration_closed(self) -> None:
        from auth.registration_service import create_user_registration

        with self.assertRaises(RegistrationError) as ctx:
            create_user_registration(
                full_name="Casey User",
                email="casey@example.com",
                password="ValidPass1!",
                company_name="Acme",
                company_code="acme",
            )
        self.assertEqual(ctx.exception.code, "SIGNUP_CLOSED")


if __name__ == "__main__":
    unittest.main()
