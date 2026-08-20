"""Unit tests for Freemium card-count entitlement."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services import entitlement_service as ent


class TestNormalize(unittest.TestCase):
    def test_two_card_freemium_remaining(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "FREEMIUM",
                "card_limit": 2,
                "cards_used": 1,
                "entitlement_started_at": None,
                "entitlement_exhausted_at": None,
            },
            "c1",
        )
        self.assertEqual(info["card_limit"], 2)
        self.assertEqual(info["cards_used"], 1)
        self.assertEqual(info["cards_remaining"], 1)
        self.assertTrue(info["can_process_card"])
        self.assertTrue(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["contacts_allowed"])
        self.assertFalse(info["freemium_exhausted"])

    def test_exhausted_after_two_cards(self) -> None:
        info = ent._normalize(
            {
                "id": "c1",
                "plan_name": "FREEMIUM",
                "card_limit": 2,
                "cards_used": 2,
                "entitlement_started_at": None,
                "entitlement_exhausted_at": None,
            },
            "c1",
        )
        self.assertEqual(info["cards_remaining"], 0)
        self.assertFalse(info["can_process_card"])
        self.assertFalse(info["whatsapp_allowed"])
        self.assertFalse(info["email_allowed"])
        self.assertFalse(info["contacts_allowed"])
        self.assertTrue(info["freemium_exhausted"])


class TestAssertLocked(unittest.TestCase):
    def test_within_limit(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "card_limit": 2,
            "cards_used": 1,
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
        }
        info = ent.assert_can_process_card_locked(cur, "c1")
        self.assertTrue(info["can_process_card"])

    def test_exceeding_limit(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "card_limit": 2,
            "cards_used": 2,
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
        }
        with self.assertRaises(ent.CardLimitExceededError) as ctx:
            ent.assert_can_process_card_locked(cur, "c1")
        self.assertEqual(ctx.exception.code, "CONTACTS_FROZEN")
        body = ctx.exception.to_response()
        self.assertEqual(body["cards_used"], 2)
        self.assertEqual(body["card_limit"], 2)

    def test_no_company_skips_check(self) -> None:
        cur = MagicMock()
        info = ent.assert_can_process_card_locked(cur, None)
        self.assertTrue(info["can_process_card"])
        cur.execute.assert_not_called()


class TestConsume(unittest.TestCase):
    def test_consume_increments(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "card_limit": 2,
            "cards_used": 1,
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
        }
        info = ent.consume_card_locked(cur, "c1", contact_id="contact-1", user_id="u1")
        self.assertEqual(info["cards_used"], 1)
        self.assertEqual(cur.execute.call_count, 2)
        sql = cur.execute.call_args_list[0][0][0]
        self.assertIn("cards_used = cards_used + 1", sql)

    def test_consume_rejected_when_update_matches_nothing(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = None
        with patch.object(ent, "get_entitlement") as get_ent:
            get_ent.return_value = {"cards_used": 2, "card_limit": 2}
            with self.assertRaises(ent.CardLimitExceededError):
                ent.consume_card_locked(cur, "c1", contact_id="x")


class TestOutreachGate(unittest.TestCase):
    @patch.object(ent, "get_entitlement")
    def test_initial_save_allowed_on_final_card(self, get_ent: MagicMock) -> None:
        get_ent.return_value = {
            "card_quota_enforced": True,
            "cards_remaining": 0,
            "cards_used": 2,
            "card_limit": 2,
        }
        self.assertTrue(ent.can_send_outreach("c1", initial_save=True))
        self.assertFalse(ent.can_send_outreach("c1", initial_save=False))

    @patch.object(ent, "get_entitlement")
    def test_remaining_allows_send(self, get_ent: MagicMock) -> None:
        get_ent.return_value = {
            "card_quota_enforced": True,
            "cards_remaining": 1,
            "cards_used": 1,
            "card_limit": 2,
        }
        self.assertTrue(ent.can_send_outreach("c1", initial_save=False))

    def test_no_company_allows_send(self) -> None:
        self.assertTrue(ent.can_send_outreach(None))


class TestContactsAccess(unittest.TestCase):
    @patch.object(ent, "get_entitlement")
    def test_exhausted_blocks_contacts(self, get_ent: MagicMock) -> None:
        get_ent.return_value = {
            "contacts_allowed": False,
            "cards_used": 2,
            "card_limit": 2,
        }
        with self.assertRaises(ent.ContactsFrozenError) as ctx:
            ent.assert_can_access_contacts("c1")
        self.assertEqual(ctx.exception.code, "CONTACTS_FROZEN")

    def test_no_company_allows_contacts(self) -> None:
        ent.assert_can_access_contacts(None)

    def test_missing_company_is_unlimited(self) -> None:
        info = ent.get_entitlement(None)
        self.assertEqual(info["plan"], "UNLIMITED")
        self.assertEqual(info["plan_name"], "Unlimited")
        self.assertIsNone(info["card_limit"])
        self.assertFalse(info["card_quota_enforced"])
        self.assertTrue(info["can_process_card"])


class TestOutreachAssert(unittest.TestCase):
    @patch.object(ent, "can_send_outreach", return_value=False)
    @patch.object(ent, "get_entitlement")
    def test_frozen_raises(self, get_ent: MagicMock, _can: MagicMock) -> None:
        get_ent.return_value = {"cards_used": 2, "card_limit": 2}
        with self.assertRaises(ent.OutreachFrozenError) as ctx:
            ent.assert_can_send_outreach("c1", channel="whatsapp")
        self.assertEqual(ctx.exception.code, "OUTREACH_FROZEN")
        self.assertEqual(ctx.exception.channel, "whatsapp")


if __name__ == "__main__":
    unittest.main()
