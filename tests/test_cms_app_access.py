"""CMS channel locks and payment snapshot helpers."""

from __future__ import annotations

import unittest

from services.cms_app_access import effective_channel_locks, normalize_channel_locks, payment_snapshot
from services import entitlement_service as ent


class TestChannelLocks(unittest.TestCase):
    def test_default_unlocked(self) -> None:
        locks = normalize_channel_locks(None)
        self.assertFalse(locks["whatsapp"])
        self.assertFalse(locks["email"])
        self.assertFalse(locks["google_sheets"])

    def test_unpaid_defaults_unlocked(self) -> None:
        locks = effective_channel_locks(None, False)
        self.assertFalse(locks["whatsapp"])
        self.assertFalse(locks["email"])
        self.assertFalse(locks["google_sheets"])

    def test_cms_lock_overrides_unpaid(self) -> None:
        locks = effective_channel_locks({"whatsapp": True}, False)
        self.assertTrue(locks["whatsapp"])
        self.assertFalse(locks["email"])
        self.assertFalse(locks["google_sheets"])
        locks = normalize_channel_locks({"whatsapp": True, "email": 0})
        self.assertTrue(locks["whatsapp"])
        self.assertFalse(locks["email"])
        self.assertFalse(locks["google_sheets"])

    def test_json_string_locks_are_honored(self) -> None:
        locks = effective_channel_locks('{"whatsapp": true, "email": false}', False)
        self.assertTrue(locks["whatsapp"])
        self.assertFalse(locks["email"])
        self.assertFalse(locks["google_sheets"])


class TestPaymentSnapshot(unittest.TestCase):
    def test_freemium_not_done(self) -> None:
        snap = payment_snapshot(plan_name="FREEMIUM")
        self.assertFalse(snap["payment_done"])
        self.assertEqual(snap["payment_label"], "payment not paid")

    def test_prepaid_plan_done(self) -> None:
        snap = payment_snapshot(plan_name="PREPAID_STARTER")
        self.assertTrue(snap["payment_done"])
        self.assertEqual(snap["payment_label"], "payment paid")

    def test_paid_intent_done(self) -> None:
        snap = payment_snapshot(plan_name="FREEMIUM", intent_status="captured")
        self.assertTrue(snap["payment_done"])


class TestEntitlementCmsLocks(unittest.TestCase):
    def test_cms_lock_blocks_outside_channel(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "FREEMIUM",
                "card_limit": 25,
                "cards_used": 1,
                "cms_channel_locks": {"whatsapp": True, "email": False},
            },
            "c1",
        )
        self.assertTrue(info["can_process_card"])
        self.assertFalse(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["google_sheets_allowed"])

    def test_unpaid_freemium_allows_channels_while_cards_remain(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "FREEMIUM",
                "card_limit": 25,
                "cards_used": 2,
            },
            "c1",
        )
        self.assertTrue(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["google_sheets_allowed"])

    def test_paid_plan_unlocked_until_cms_locks(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "PREPAID_STARTER",
                "card_limit": 1000,
                "cards_used": 1,
            },
            "c1",
        )
        self.assertTrue(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["google_sheets_allowed"])

    def test_json_string_cms_lock_blocks_whatsapp(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "FREEMIUM",
                "card_limit": 25,
                "cards_used": 1,
                "cms_channel_locks": '{"whatsapp": true, "email": false}',
            },
            "c1",
        )
        self.assertFalse(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["cms_channel_locks"]["whatsapp"])


if __name__ == "__main__":
    unittest.main()
