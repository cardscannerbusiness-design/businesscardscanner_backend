"""Regression tests for contact ownership, sync status, and auth logout path."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from auth.constants import is_public_path
from auth.ownership import user_can_access_contact
from services.local_db_service import _payload_to_local_body, _row_to_contact


class TestAuthPublicPaths(unittest.TestCase):
    def test_logout_is_public(self) -> None:
        self.assertTrue(is_public_path("/api/auth/logout"))
        self.assertTrue(is_public_path("/api/auth/refresh"))
        self.assertFalse(is_public_path("/api/contacts"))


class TestContactOwnership(unittest.TestCase):
    def test_super_admin_sees_own_not_tenant(self) -> None:
        self.assertTrue(
            user_can_access_contact(
                {"id": "sa", "role": "SUPER_ADMIN"},
                {"created_by_user_id": "sa", "owner_company_id": None, "created_by_role": "SUPER_ADMIN"},
            )
        )
        self.assertFalse(
            user_can_access_contact(
                {"id": "sa", "role": "SUPER_ADMIN"},
                {"created_by_user_id": "other", "owner_company_id": "c1", "created_by_role": "ADMIN"},
            )
        )

    def test_admin_same_company(self) -> None:
        self.assertTrue(
            user_can_access_contact(
                {"id": "a1", "role": "ADMIN", "company_id": "c1"},
                {"created_by_user_id": "u2", "owner_company_id": "c1"},
            )
        )

    def test_admin_other_company_blocked(self) -> None:
        self.assertFalse(
            user_can_access_contact(
                {"id": "a1", "role": "ADMIN", "company_id": "c1"},
                {"created_by_user_id": "u2", "owner_company_id": "c2"},
            )
        )

    def test_user_own_contact_only(self) -> None:
        self.assertTrue(
            user_can_access_contact(
                {"id": "u1", "role": "USER", "company_id": "c1"},
                {"created_by_user_id": "u1", "owner_company_id": "c1"},
            )
        )
        self.assertFalse(
            user_can_access_contact(
                {"id": "u1", "role": "USER", "company_id": "c1"},
                {"created_by_user_id": "u2", "owner_company_id": "c1"},
            )
        )


class TestContactPayloadNormalization(unittest.TestCase):
    def test_online_save_marks_synced_and_keeps_event_image(self) -> None:
        body = _payload_to_local_body(
            {
                "fullName": "Balaji Narayanan",
                "eventName": "Mall Opening",
                "eventId": "evt-1",
                "socialLinks": "linkedin.com/in/balaji",
                "gstNumber": "29ABCDE1234F1Z5",
                "connectionMode": "online",
                "cardImageBase64": "data:image/jpeg;base64,abc",
            }
        )
        self.assertEqual(body["eventName"], "Mall Opening")
        self.assertEqual(body["eventId"], "evt-1")
        self.assertEqual(body["socialLinks"], "linkedin.com/in/balaji")
        self.assertEqual(body["gstNumber"], "29ABCDE1234F1Z5")
        self.assertEqual(body["cardImageBase64"], "data:image/jpeg;base64,abc")
        self.assertEqual(body["syncStatus"], "synced")

    def test_legacy_local_only_payload_becomes_synced(self) -> None:
        body = _payload_to_local_body({"fullName": "Test", "syncStatus": "local_only"})
        self.assertEqual(body["syncStatus"], "synced")

    def test_row_maps_event_and_ownership(self) -> None:
        contact = _row_to_contact(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "fullName": "Balaji Narayanan",
                "eventName": "Mall Opening",
                "eventId": "evt-1",
                "syncStatus": "local_only",
                "created_by_user_id": "22222222-2222-2222-2222-222222222222",
                "joined_owner_company_id": "33333333-3333-3333-3333-333333333333",
                "created_by_role": "ADMIN",
                "admin_name": "Acme",
                "user_name": "Admin User",
            }
        )
        self.assertEqual(contact["eventName"], "Mall Opening")
        self.assertEqual(contact["status"], "synced")
        self.assertEqual(contact["syncStatus"], "synced")
        self.assertEqual(contact["owner_company_id"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(contact["created_by_role"], "ADMIN")
        self.assertNotIn("firebaseId", contact)
        self.assertNotIn("zohoLeadId", contact)


if __name__ == "__main__":
    unittest.main()
