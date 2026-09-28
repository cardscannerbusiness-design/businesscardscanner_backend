"""Prepaid checkout is locked until PAYMENTS_ENABLED=true."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-billing-payments-lock")

from api.routes.billing_routes import payments_enabled


class TestPaymentsEnabled(unittest.TestCase):
    def test_disabled_when_unset(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PAYMENTS_ENABLED"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(payments_enabled())

    def test_disabled_when_false(self) -> None:
        with patch.dict(os.environ, {"PAYMENTS_ENABLED": "false"}):
            self.assertFalse(payments_enabled())

    def test_enabled_when_true(self) -> None:
        with patch.dict(os.environ, {"PAYMENTS_ENABLED": "true"}):
            self.assertTrue(payments_enabled())
