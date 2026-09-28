"""Feature-control merge and payment status helpers."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from services.cms_app_access import (
    classify_payment_status,
    effective_channel_locks,
    normalize_action_locks,
    normalize_channel_locks,
    persistable_feature_control,
    payment_snapshot,
)
from services.feature_control import FeatureBlockedError


class TestFeatureLocks(unittest.TestCase):
    def test_new_modules_default_unlocked(self) -> None:
        locks = normalize_channel_locks(None)
        self.assertFalse(locks["email_templates"])
        self.assertFalse(locks["events"])
        self.assertFalse(locks["capture"])
        self.assertFalse(locks["subscription"])

    def test_partial_update_preserves_other_keys(self) -> None:
        stored = {"whatsapp": True, "email": False, "google_sheets": False}
        merged = persistable_feature_control(stored, {"email_templates": True})
        self.assertTrue(merged["whatsapp"])
        self.assertTrue(merged["email_templates"])
        self.assertFalse(merged["email"])
        self.assertFalse(merged["actions"]["events.create"])

    def test_action_lock_roundtrip(self) -> None:
        merged = persistable_feature_control(
            {},
            {"actions": {"email_templates.delete": True}},
        )
        actions = normalize_action_locks(merged)
        self.assertTrue(actions["email_templates.delete"])
        self.assertFalse(actions["email_templates.create"])

    def test_email_module_lock_implies_send(self) -> None:
        actions = normalize_action_locks({"email": True})
        self.assertTrue(actions["email.send"])

    def test_effective_locks_are_account_json_only(self) -> None:
        locks = effective_channel_locks({"email_templates": True}, False)
        self.assertTrue(locks["email_templates"])
        self.assertFalse(locks["events"])


class TestPaymentStatus(unittest.TestCase):
    def test_pending_intent(self) -> None:
        self.assertEqual(
            classify_payment_status(plan_name="FREEMIUM", intent_status="pending"),
            "pending",
        )

    def test_failed_intent(self) -> None:
        self.assertEqual(
            classify_payment_status(plan_name="FREEMIUM", intent_status="failed"),
            "failed",
        )

    def test_prepaid_plan_is_paid(self) -> None:
        snap = payment_snapshot(plan_name="PREPAID_STARTER")
        self.assertTrue(snap["payment_done"])
        self.assertEqual(snap["status_label"], "Paid")
        self.assertEqual(snap["status_mark"], "✓")

    def test_feature_blocked_payload(self) -> None:
        err = FeatureBlockedError("email_templates", "create")
        body = err.to_response()
        self.assertEqual(body["code"], "FEATURE_BLOCKED")
        self.assertEqual(body["feature"], "email_templates")
        self.assertIn("administrator", body["message"].lower())


if __name__ == "__main__":
    unittest.main()
