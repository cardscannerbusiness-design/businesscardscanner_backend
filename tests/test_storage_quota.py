"""Unit tests for company storage quota (StorageService)."""

from __future__ import annotations

import base64
import unittest
from unittest.mock import MagicMock, patch

from services import storage_service as svc
from services.local_db_service import soft_delete_contact


def _tiny_jpeg_b64(nbytes_hint: int = 100) -> str:
    """Build a data-URL whose decoded size is at least nbytes_hint (approx)."""
    # Minimal valid-ish JPEG header + padding
    raw = b"\xff\xd8\xff\xe0" + (b"\x00" * max(0, nbytes_hint - 4))
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


class TestRowAsDict(unittest.TestCase):
    def test_mapping_row(self) -> None:
        row = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 100,
            "used_storage_bytes": 10,
        }
        self.assertEqual(svc._row_as_dict(row)["id"], "c1")

    def test_tuple_row_from_plain_cursor(self) -> None:
        # Regression: create_contact used plain cursor; dict(tuple) crashed.
        row = ("c1", "FREEMIUM", 1048576, 1000)
        parsed = svc._row_as_dict(row)
        self.assertEqual(parsed["id"], "c1")
        self.assertEqual(parsed["plan_name"], "FREEMIUM")
        self.assertEqual(parsed["storage_limit_bytes"], 1048576)
        self.assertEqual(parsed["used_storage_bytes"], 1000)


class TestCalculateImageSize(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(svc.calculate_image_size_bytes(None), 0)
        self.assertEqual(svc.calculate_image_size_bytes(""), 0)

    def test_data_url_size(self) -> None:
        payload = b"hello-storage-bytes-12345"
        data_url = "data:image/jpeg;base64," + base64.b64encode(payload).decode()
        self.assertEqual(svc.calculate_image_size_bytes(data_url), len(payload))


class TestCanUpload(unittest.TestCase):
    @patch.object(svc, "get_company_storage")
    def test_within_limit(self, get_storage: MagicMock) -> None:
        get_storage.return_value = {
            "storage_limit_bytes": 1 * 1024 * 1024,
            "used_storage_bytes": 100_000,
        }
        self.assertTrue(svc.can_upload("c1", 50_000))

    @patch.object(svc, "get_company_storage")
    def test_exceeding_limit(self, get_storage: MagicMock) -> None:
        get_storage.return_value = {
            "storage_limit_bytes": 1 * 1024 * 1024,
            "used_storage_bytes": 1 * 1024 * 1024 - 100,
        }
        self.assertFalse(svc.can_upload("c1", 200))

    @patch.object(svc, "get_company_storage")
    def test_zero_size_always_ok(self, get_storage: MagicMock) -> None:
        get_storage.return_value = {
            "storage_limit_bytes": 100,
            "used_storage_bytes": 100,
        }
        self.assertTrue(svc.can_upload("c1", 0))
        get_storage.assert_not_called()


class TestAssertCanUploadLocked(unittest.TestCase):
    def test_upload_within_limit(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1048576,
            "used_storage_bytes": 100_000,
        }
        svc.assert_can_upload_locked(cur, "c1", 100_000)

    def test_upload_exceeding_limit(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1048576,
            "used_storage_bytes": 1048576,
        }
        with self.assertRaises(svc.StorageLimitExceededError) as ctx:
            svc.assert_can_upload_locked(cur, "c1", 1)
        self.assertEqual(ctx.exception.code, "STORAGE_LIMIT_EXCEEDED")
        body = ctx.exception.to_response()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "STORAGE_LIMIT_EXCEEDED")
        self.assertEqual(body["used_storage_bytes"], 1048576)
        self.assertEqual(body["storage_limit_bytes"], 1048576)
        self.assertEqual(body["image_size_bytes"], 1)

    def test_no_company_skips_check(self) -> None:
        cur = MagicMock()
        svc.assert_can_upload_locked(cur, None, 999_999)
        cur.execute.assert_not_called()


class TestStorageUpdateAndRelease(unittest.TestCase):
    def test_update_storage_after_upload(self) -> None:
        cur = MagicMock()
        svc.update_storage_after_upload("c1", 12345, cur=cur)
        self.assertTrue(cur.execute.called)
        sql = cur.execute.call_args[0][0]
        self.assertIn("used_storage_bytes = used_storage_bytes + %s", sql)
        self.assertEqual(cur.execute.call_args[0][1], (12345, "c1"))

    def test_release_storage_never_negative(self) -> None:
        cur = MagicMock()
        svc.release_storage_after_delete("c1", 500, cur=cur)
        sql = cur.execute.call_args[0][0]
        self.assertIn("GREATEST(0, used_storage_bytes - %s)", sql)
        self.assertEqual(cur.execute.call_args[0][1], (500, "c1"))

    def test_multiple_uploads_accumulate_math(self) -> None:
        limit = 1048576
        used = 0
        sizes = [100_000, 250_000, 400_000]
        for size in sizes:
            self.assertLessEqual(used + size, limit)
            used += size
        self.assertEqual(used, sum(sizes))
        self.assertEqual(limit - used, limit - sum(sizes))


class TestGetStorageUsageShape(unittest.TestCase):
    @patch("db.pool.db_cursor")
    def test_usage_includes_mb_fields(self, db_cursor: MagicMock) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1048576,
            "used_storage_bytes": 422576,
        }
        db_cursor.return_value.__enter__.return_value = cur

        usage = svc.get_storage_usage("c1")
        self.assertEqual(usage["plan"], "FREEMIUM")
        self.assertEqual(usage["storage_limit_bytes"], 1048576)
        self.assertEqual(usage["used_storage_bytes"], 422576)
        self.assertEqual(usage["remaining_storage_bytes"], 1048576 - 422576)
        self.assertIn("used_mb", usage)
        self.assertIn("limit_mb", usage)
        self.assertIn("remaining_mb", usage)
        self.assertAlmostEqual(usage["used_percentage"], 40.3, places=1)
        self.assertTrue(usage["can_upload"])
        self.assertEqual(usage["warning_level"], "NORMAL")

    @patch("db.pool.db_cursor")
    def test_usage_warning_critical_blocked(self, db_cursor: MagicMock) -> None:
        cur = MagicMock()
        db_cursor.return_value.__enter__.return_value = cur

        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1000,
            "used_storage_bytes": 800,
        }
        self.assertEqual(svc.get_storage_usage("c1")["warning_level"], "WARNING")

        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1000,
            "used_storage_bytes": 950,
        }
        self.assertEqual(svc.get_storage_usage("c1")["warning_level"], "CRITICAL")

        cur.fetchone.return_value = {
            "id": "c1",
            "plan_name": "FREEMIUM",
            "storage_limit_bytes": 1000,
            "used_storage_bytes": 1000,
        }
        blocked = svc.get_storage_usage("c1")
        self.assertEqual(blocked["warning_level"], "BLOCKED")
        self.assertFalse(blocked["can_upload"])


class TestSoftDeleteReleasesStorage(unittest.TestCase):
    @patch("services.local_db_service._connect")
    def test_delete_contact_releases_bytes(self, connect: MagicMock) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {
            "image_size_bytes": 12_000,
            "owner_company_id": "company-1",
        }
        cur.rowcount = 1
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value.__enter__.return_value = conn

        with patch("services.storage_service.release_storage_after_delete") as release:
            result = soft_delete_contact("contact-1")
            self.assertTrue(result["success"])
            release.assert_called_once()
            args, kwargs = release.call_args
            self.assertEqual(args[0], "company-1")
            self.assertEqual(args[1], 12_000)


class TestSoftDeleteMissing(unittest.TestCase):
    @patch("services.local_db_service._connect")
    def test_soft_delete_missing_contact(self, connect: MagicMock) -> None:
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn.cursor.return_value.__enter__.return_value = cur
        connect.return_value.__enter__.return_value = conn

        result = soft_delete_contact("missing")
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
