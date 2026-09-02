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

    def test_default_freemium_allowance_is_ten(self) -> None:
        self.assertEqual(ent.DEFAULT_FREEMIUM_CARD_LIMIT, 10)

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
            "user_cards_used": 1,
            "user_card_limit": 10,
        }
        info = ent.consume_card_locked(cur, "c1", contact_id="contact-1", user_id="u1")
        self.assertEqual(info["cards_used"], 1)
        self.assertEqual(info["card_limit"], 10)
        sql = cur.execute.call_args_list[0][0][0]
        self.assertIn("user_cards_used", sql)

    def test_consume_rejected_when_update_matches_nothing(self) -> None:
        cur = MagicMock()
        cur.fetchone.side_effect = [None, {"user_cards_used": 10, "user_card_limit": 10}]
        with self.assertRaises(ent.CardLimitExceededError):
            ent.consume_card_locked(cur, "c1", contact_id="x", user_id="u1")


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


class TestUserScanUnlimited(unittest.TestCase):
    """Per-user scans_unlimited unlocks the complete Freemium workflow for that user."""

    _EXHAUSTED = {
        "plan": "FREEMIUM",
        "plan_name": "FREEMIUM",
        "card_limit": 10,
        "cards_used": 10,
        "cards_remaining": 0,
        "card_quota_enforced": True,
        "can_process_card": False,
        "whatsapp_allowed": False,
        "email_allowed": False,
        "contacts_allowed": False,
        "freemium_exhausted": True,
    }

    _NORMAL_EXHAUSTED_USER = {
        "id": "u-normal",
        "scans_unlimited": False,
        "user_card_limit": 10,
        "user_cards_used": 10,
    }

    def test_flag_false_uses_existing_company_limit(self) -> None:
        """1. Normal Freemium user: 10-card personal limit still applies."""
        with self.assertRaises(ent.CardLimitExceededError):
            ent.assert_can_process_card("c1", user=dict(self._NORMAL_EXHAUSTED_USER))

    def test_normal_user_whatsapp_email_contacts_freeze_at_cap(self) -> None:
        """1. Normal Freemium user: WhatsApp / Email / Contacts freeze at exhaustion."""
        with patch.object(ent, "get_entitlement", return_value=self._EXHAUSTED):
            with self.assertRaises(ent.ContactsFrozenError):
                ent.assert_can_access_contacts(
                    "c1", user=dict(self._NORMAL_EXHAUSTED_USER)
                )
            self.assertFalse(
                ent.can_send_outreach(
                    "c1",
                    initial_save=False,
                    user=dict(self._NORMAL_EXHAUSTED_USER),
                )
            )
            with self.assertRaises(ent.OutreachFrozenError):
                ent.assert_can_send_outreach(
                    "c1",
                    channel="whatsapp",
                    user=dict(self._NORMAL_EXHAUSTED_USER),
                )
            with self.assertRaises(ent.OutreachFrozenError):
                ent.assert_can_send_outreach(
                    "c1",
                    channel="email",
                    user=dict(self._NORMAL_EXHAUSTED_USER),
                )

    def test_flag_true_allows_save_when_company_at_limit(self) -> None:
        """2. Unlimited user: card #11+ can be saved."""
        with patch.object(ent, "get_entitlement") as get_ent:
            get_ent.return_value = {
                "can_process_card": False,
                "cards_used": 10,
                "card_limit": 10,
            }
            ent.assert_can_process_card(
                "c1", user={"id": "u-unlimited", "scans_unlimited": True}
            )
            get_ent.assert_not_called()

    def test_locked_assert_allows_unlimited_when_company_exhausted(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "card_limit": 10,
            "cards_used": 10,
            "entitlement_started_at": None,
            "entitlement_exhausted_at": None,
        }
        info = ent.assert_can_process_card_locked(cur, "c1", scans_unlimited=True)
        self.assertTrue(info["can_process_card"])
        self.assertTrue(info["scans_unlimited"])
        self.assertEqual(info["cards_used"], 10)
        self.assertEqual(info["card_limit"], 10)
        self.assertEqual(info["plan"], "FREEMIUM")
        self.assertTrue(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["contacts_allowed"])
        self.assertFalse(info["freemium_exhausted"])

    def test_unlimited_user_whatsapp_email_contacts_remain_available(self) -> None:
        """2. Unlimited user: WhatsApp / Email / Contacts stay available after 10 cards."""
        unlimited = {"id": "u-unlimited", "scans_unlimited": True}
        with patch.object(ent, "get_entitlement", return_value=self._EXHAUSTED):
            ent.assert_can_access_contacts("c1", user=unlimited)
            self.assertTrue(
                ent.can_send_outreach("c1", initial_save=False, user=unlimited)
            )
            ent.assert_can_send_outreach("c1", channel="whatsapp", user=unlimited)
            ent.assert_can_send_outreach("c1", channel="email", user=unlimited)

    def test_unlimited_consume_does_not_increment_cards_used(self) -> None:
        """Unlimited user extra cards do not increment companies.cards_used."""
        cur = MagicMock()
        info = ent.consume_card_locked(
            cur, "c1", contact_id="contact-1", user_id="u1", scans_unlimited=True
        )
        self.assertTrue(info["scans_unlimited"])
        cur.execute.assert_not_called()

    def test_same_company_unlimited_vs_normal_user(self) -> None:
        """3. Same company: unlimited and normal users have different entitlements."""
        unlimited = {
            "id": "manish",
            "company_id": "company-x",
            "scans_unlimited": True,
        }
        normal = {
            "id": "other",
            "company_id": "company-x",
            "scans_unlimited": False,
            "user_card_limit": 10,
            "user_cards_used": 10,
        }
        with patch.object(ent, "get_entitlement") as get_ent:
            get_ent.return_value = dict(self._EXHAUSTED)
            ent.assert_can_process_card("company-x", user=unlimited)
            with self.assertRaises(ent.CardLimitExceededError):
                ent.assert_can_process_card("company-x", user=normal)

            ent.assert_can_access_contacts("company-x", user=unlimited)
            with self.assertRaises(ent.ContactsFrozenError):
                ent.assert_can_access_contacts("company-x", user=normal)

            self.assertTrue(
                ent.can_send_outreach("company-x", initial_save=False, user=unlimited)
            )
            self.assertFalse(
                ent.can_send_outreach("company-x", initial_save=False, user=normal)
            )

    def test_other_company_unchanged(self) -> None:
        """4. Other users keep the default 10-card personal enforcement."""
        exhausted = dict(self._NORMAL_EXHAUSTED_USER)
        exhausted["id"] = "admin-2"
        with self.assertRaises(ent.CardLimitExceededError):
            ent.assert_can_process_card("company-other", user=exhausted)
        with self.assertRaises(ent.ContactsFrozenError):
            ent.assert_can_access_contacts("company-other", user=exhausted)
        self.assertFalse(
            ent.can_send_outreach(
                "company-other",
                initial_save=False,
                user=exhausted,
            )
        )

    def test_storage_quota_not_skipped_for_unlimited_scans(self) -> None:
        """5. Unlimited scans does not skip the storage-byte limit."""
        self.assertFalse(ent.skip_storage_quota_for_creator("ADMIN"))
        self.assertFalse(ent.skip_storage_quota_for_creator("USER"))
        self.assertTrue(ent.skip_card_quota_for_creator("ADMIN", True))
        self.assertFalse(ent.skip_card_quota_for_creator("ADMIN", False))
        self.assertTrue(ent.skip_storage_quota_for_creator("SUPER_ADMIN"))

    def test_overlay_unlocks_workflow_without_changing_company_plan(self) -> None:
        """Overlay unlocks workflow flags; plan_name and card_limit stay Freemium/10."""
        overlay = ent.apply_user_scan_overlay(
            self._EXHAUSTED, user={"scans_unlimited": True}
        )
        self.assertTrue(overlay["scans_unlimited"])
        self.assertTrue(overlay["can_process_card"])
        self.assertTrue(overlay["whatsapp_allowed"])
        self.assertTrue(overlay["email_allowed"])
        self.assertTrue(overlay["contacts_allowed"])
        self.assertFalse(overlay["freemium_exhausted"])
        self.assertEqual(overlay["plan"], "FREEMIUM")
        self.assertEqual(overlay["card_limit"], 10)
        self.assertEqual(overlay["cards_used"], 10)

        normal = ent.apply_user_scan_overlay(
            self._EXHAUSTED, user=dict(self._NORMAL_EXHAUSTED_USER)
        )
        self.assertFalse(normal["scans_unlimited"])
        self.assertFalse(normal["can_process_card"])
        self.assertFalse(normal["whatsapp_allowed"])
        self.assertFalse(normal["email_allowed"])
        self.assertFalse(normal["contacts_allowed"])
        self.assertTrue(normal["freemium_exhausted"])

    def test_company_snapshot_without_user_stays_frozen(self) -> None:
        """Unlimited status must not leak when the user is not passed."""
        with patch.object(ent, "get_entitlement", return_value=self._EXHAUSTED):
            with self.assertRaises(ent.ContactsFrozenError):
                ent.assert_can_access_contacts("c1")
            self.assertFalse(ent.can_send_outreach("c1", initial_save=False))

    def test_super_admin_unlimited_behavior_unchanged(self) -> None:
        """6. Super Admin remains unlimited via missing company, not the user flag."""
        info = ent.get_entitlement(None)
        self.assertEqual(info["plan"], "UNLIMITED")
        self.assertFalse(info["card_quota_enforced"])
        self.assertTrue(info["can_process_card"])
        self.assertTrue(info["whatsapp_allowed"])
        self.assertTrue(info["email_allowed"])
        self.assertTrue(info["contacts_allowed"])
        self.assertFalse(ent.user_has_unlimited_scans({"role": "SUPER_ADMIN"}))
        self.assertTrue(ent.skip_card_quota_for_creator("SUPER_ADMIN", False))
        self.assertTrue(ent.skip_storage_quota_for_creator("SUPER_ADMIN"))
        self.assertTrue(ent.can_send_outreach(None))
        ent.assert_can_access_contacts(None)

    def test_usage_fields_default_scans_unlimited_false(self) -> None:
        with patch.object(ent, "get_entitlement") as get_ent:
            get_ent.return_value = {
                "card_limit": 10,
                "cards_used": 3,
                "cards_remaining": 7,
                "freemium_exhausted": False,
                "card_quota_enforced": True,
                "can_process_card": True,
                "whatsapp_allowed": True,
                "email_allowed": True,
                "contacts_allowed": True,
                "entitlement_started_at": None,
                "entitlement_exhausted_at": None,
            }
            fields = ent.entitlement_fields_for_usage("c1")
            self.assertFalse(fields["scans_unlimited"])
            self.assertEqual(fields["card_limit"], 10)

    def test_does_not_trust_client_email(self) -> None:
        self.assertFalse(
            ent.user_has_unlimited_scans({"email": "manish@journeywithus.co"})
        )
        self.assertTrue(
            ent.user_has_unlimited_scans({"scans_unlimited": True, "email": "other@x.com"})
        )


if __name__ == "__main__":
    unittest.main()
