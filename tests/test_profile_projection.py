"""Profile API projects company_name and designation for Preferences."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-profile-projection")

from api.routes.profile_routes import _serialize_profile_row  # noqa: E402


class TestProfileWorkFields(unittest.TestCase):
    def test_serialize_includes_company_name_and_designation(self) -> None:
        row = {
            "id": "user-1",
            "email": "john@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "display_name": "",
            "phone": "+919876543210",
            "designation": "Sales Manager",
            "company_name": "ABC Technologies",
            "company_id": "co-1",
            "profile_image": "",
            "role": "ADMIN",
            "updated_at": datetime.now(timezone.utc),
        }
        out = _serialize_profile_row(row)
        self.assertEqual(out["company_name"], "ABC Technologies")
        self.assertEqual(out["designation"], "Sales Manager")
        self.assertEqual(out["effective_display_name"], "John Doe")
        self.assertEqual(out["role"], "ADMIN")
        self.assertEqual(out["phone"], "+919876543210")

    def test_serialize_empty_work_fields_are_strings(self) -> None:
        out = _serialize_profile_row(
            {
                "id": "user-2",
                "first_name": "Ada",
                "last_name": "",
                "display_name": "",
                "designation": None,
                "company_name": None,
            }
        )
        self.assertEqual(out["company_name"], "")
        self.assertEqual(out["designation"], "")
        self.assertEqual(out["effective_display_name"], "Ada")
