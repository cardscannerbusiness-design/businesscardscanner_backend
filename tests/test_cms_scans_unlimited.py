"""CMS premium grant — per-user scans_unlimited via SuperAdmin."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-cms-scans-unlimited")

import services.admin_env_service as cms  # noqa: E402


class TestSetCmsTenantUserScansUnlimited(unittest.TestCase):
    @patch.object(cms, "list_cms_tenant_users")
    @patch.object(cms, "get_admin_env_settings")
    @patch.object(cms, "db_cursor")
    def test_grant_updates_flag(
        self,
        db_cursor: MagicMock,
        get_admin: MagicMock,
        list_users: MagicMock,
    ) -> None:
        get_admin.return_value = {"admin_id": "admin-1", "company_id": "co-1"}
        cur = MagicMock()
        db_cursor.return_value.__enter__.return_value = cur
        cur.fetchone.side_effect = [
            {"id": "user-1", "role": "ADMIN"},
            {"id": "user-1"},
        ]
        list_users.return_value = {
            "users": [{"id": "user-1", "email": "a@x.com", "scans_unlimited": True}],
        }

        result = cms.set_cms_tenant_user_scans_unlimited(
            "admin-1", "user-1", scans_unlimited=True
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["scans_unlimited"])
        cur.execute.assert_called()
        update_sql = cur.execute.call_args_list[1][0][0]
        self.assertIn("scans_unlimited", update_sql)

    @patch.object(cms, "get_admin_env_settings")
    def test_admin_not_found(self, get_admin: MagicMock) -> None:
        get_admin.return_value = None
        with self.assertRaises(ValueError):
            cms.set_cms_tenant_user_scans_unlimited(
                "missing", "user-1", scans_unlimited=True
            )


if __name__ == "__main__":
    unittest.main()
