"""Tests for PostgreSQL soft-delete contacts."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.local_db_service import soft_delete_contact


class TestSoftDeleteContact(unittest.TestCase):
    @patch("services.local_db_service._connect")
    def test_soft_delete_updates_flags(self, connect: MagicMock) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "image_size_bytes": 0,
            "owner_company_id": None,
        }
        cur.rowcount = 1
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value.__enter__.return_value = conn

        result = soft_delete_contact("contact-1")
        self.assertTrue(result["success"])
        self.assertIn("soft-deleted", result["message"])
        self.assertTrue(cur.execute.called)

    @patch("services.local_db_service._connect")
    def test_soft_delete_missing_contact(self, connect: MagicMock) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        cur.rowcount = 0
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value.__enter__.return_value = conn

        result = soft_delete_contact("missing")
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
