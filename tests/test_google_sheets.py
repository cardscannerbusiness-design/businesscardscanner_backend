"""Google Sheets secondary-sync tests — no network, no real credentials."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services import google_sheets_service as sheets

CONTACT = {
    "id": "11111111-1111-1111-1111-111111111111",
    "fullName": "Balaji Narayanan",
    "company": "Acme Corp",
    "designation": "CTO",
    "phone": "9884993074",
    "secondaryPhone": "",
    "countryCode": "+91",
    "countryName": "India",
    "email": "balaji@acme.com",
    "secondaryEmail": "",
    "website": "https://acme.com",
    "address": "12 MG Road, Bengaluru",
    "secondaryAddress": "",
    "notes": "Met at booth.",
    "eventName": "Mall Opening",
    "eventDay": "Day 1",
    "owner_company_id": "22222222-2222-2222-2222-222222222222",
    "admin_name": "Acme Corp",
    "user_name": "Admin User",
    "created_by_role": "ADMIN",
    "created_at": "2026-07-18T10:00:00",
    "updatedAt": "2026-07-18T10:05:00",
    "status": "synced",
    "syncStatus": "synced",
    "cardImageBase64": "data:image/jpeg;base64,abc",
}

EXTRAS = {"ocrEngine": "Textract", "ocrConfidence": 92.5, "captureSource": "Camera"}


class TestRowMapping(unittest.TestCase):
    def test_row_matches_header_length(self) -> None:
        row = sheets.contact_to_row(CONTACT, EXTRAS)
        self.assertEqual(len(row), len(sheets.HEADERS))

    def test_row_fields(self) -> None:
        row = sheets.contact_to_row(CONTACT, EXTRAS)
        as_dict = dict(zip(sheets.HEADERS, row))
        self.assertEqual(as_dict["Contact ID"], CONTACT["id"])
        self.assertEqual(as_dict["Full Name"], "Balaji Narayanan")
        self.assertEqual(as_dict["Event Name"], "Mall Opening")
        self.assertEqual(as_dict["Event Day"], "Day 1")
        self.assertEqual(as_dict["Country Code"], "+91")
        self.assertEqual(as_dict["Country Name"], "India")
        self.assertEqual(as_dict["Primary Phone"], "9884993074")
        self.assertEqual(as_dict["Company ID"], CONTACT["owner_company_id"])
        self.assertEqual(as_dict["Created By"], "Admin User")
        self.assertEqual(as_dict["Created By Role"], "ADMIN")
        self.assertEqual(as_dict["OCR Engine"], "Textract")
        self.assertEqual(as_dict["OCR Confidence"], "92.50")
        self.assertEqual(as_dict["Capture Source"], "Camera")
        self.assertEqual(as_dict["Contact Status"], "synced")
        self.assertEqual(as_dict["Created Date"], "2026-07-18")
        self.assertEqual(as_dict["Created Timestamp"], "2026-07-18T10:00:00")
        self.assertTrue(as_dict["Image File Name"].startswith("card-"))

    def test_no_confidence_is_blank(self) -> None:
        row = sheets.contact_to_row(CONTACT, {"ocrEngine": "PaddleOCR"})
        as_dict = dict(zip(sheets.HEADERS, row))
        self.assertEqual(as_dict["OCR Confidence"], "")


class TestConfiguration(unittest.TestCase):
    def test_not_configured_skips_without_error(self) -> None:
        with patch.dict("os.environ", {"GOOGLE_SHEET_ID": "", "GOOGLE_SERVICE_ACCOUNT_JSON": ""}):
            self.assertFalse(sheets.is_sheets_configured())
            self.assertFalse(sheets.sync_contact_to_sheet(CONTACT, EXTRAS))


class TestUpsert(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            "os.environ",
            {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        sheets._pending_retry.clear()
        sheets._workbook_cache.clear()

    def test_appends_when_contact_id_not_found(self) -> None:
        calls: dict[str, int] = {"append": 0, "update": 0}
        with (
            patch.object(sheets, "_auth_headers", return_value={"Authorization": "Bearer x"}),
            patch.object(sheets, "_resolve_workbook_id", return_value="sheet123"),
            patch.object(sheets, "_ensure_worksheet", return_value="Day 1"),
            patch.object(sheets, "_ensure_header_row", return_value=None),
            patch.object(sheets, "_find_row_by_contact_id", return_value=None),
            patch.object(
                sheets,
                "_values_update",
                side_effect=lambda *a, **k: calls.__setitem__("update", calls["update"] + 1),
            ),
            patch.object(
                sheets,
                "_values_append",
                side_effect=lambda *a, **k: calls.__setitem__("append", calls["append"] + 1),
            ),
        ):
            self.assertTrue(sheets.sync_contact_to_sheet(CONTACT, EXTRAS))
        self.assertEqual(calls["append"], 1)
        self.assertEqual(calls["update"], 0)

    def test_updates_existing_row_no_duplicate(self) -> None:
        calls: dict[str, list] = {"append": [], "update": []}
        with (
            patch.object(sheets, "_auth_headers", return_value={"Authorization": "Bearer x"}),
            patch.object(sheets, "_resolve_workbook_id", return_value="sheet123"),
            patch.object(sheets, "_ensure_worksheet", return_value="Day 1"),
            patch.object(sheets, "_ensure_header_row", return_value=None),
            patch.object(sheets, "_find_row_by_contact_id", return_value=2),
            patch.object(
                sheets,
                "_values_update",
                side_effect=lambda h, sid, r, v: calls["update"].append(r),
            ),
            patch.object(
                sheets,
                "_values_append",
                side_effect=lambda h, sid, r, v: calls["append"].append(r),
            ),
        ):
            self.assertTrue(sheets.sync_contact_to_sheet(CONTACT, EXTRAS))
        self.assertEqual(len(calls["append"]), 0)
        self.assertEqual(len(calls["update"]), 1)
        self.assertIn("!A2:", calls["update"][0])  # row 2 = existing contact row


class TestFailureScenario(unittest.TestCase):
    def test_sheets_down_never_raises_and_queues_retry(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {"GOOGLE_SHEET_ID": "sheet123", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}"},
            ),
            patch.object(sheets, "_upsert_row", side_effect=RuntimeError("Sheets API is down")),
            patch.object(sheets, "time") as mock_time,
        ):
            mock_time.sleep = lambda *_: None
            mock_time.time = lambda: 0.0
            ok = sheets.sync_contact_to_sheet(CONTACT, EXTRAS)
        self.assertFalse(ok)
        self.assertIn(CONTACT["id"], sheets._pending_retry)
        sheets._pending_retry.clear()


if __name__ == "__main__":
    unittest.main()
