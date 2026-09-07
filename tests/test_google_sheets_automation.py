"""Event workbook automation tests — no network, no real credentials."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from services import google_oauth_service as oauth
from services import google_sheets_automation as automation


EVENT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class TestUniqueDayTitles(unittest.TestCase):
    def test_sanitizes_and_defaults(self) -> None:
        self.assertEqual(automation._unique_day_titles([]), ["Day 1"])
        titles = automation._unique_day_titles(["Day 1", "Day 2", "Day 3"])
        self.assertEqual(titles[0], "Day 1")
        self.assertEqual(titles[1], "Day 2")
        self.assertEqual(titles[2], "Day 3")
        self.assertEqual(len(titles), 3)
        self.assertEqual(len(set(titles)), 3)


class TestOauthAccessToken(unittest.TestCase):
    def test_company_admin_tried_first(self) -> None:
        with (
            patch.object(
                oauth,
                "load_company_admin_oauth",
                return_value={"refresh_token": "admin-rt"},
            ) as load_admin,
            patch.object(oauth, "load_user_refresh_token") as load_user,
            patch.object(oauth, "refresh_access_token", return_value="access-token") as refresh,
        ):
            token = oauth._oauth_access_token(
                company_id="company-1", user_id="user-1"
            )
        self.assertEqual(token, "access-token")
        load_admin.assert_called_once_with("company-1")
        load_user.assert_not_called()
        refresh.assert_called_once_with("admin-rt")

    def test_falls_back_to_creating_user(self) -> None:
        with (
            patch.object(oauth, "load_company_admin_oauth", return_value={"refresh_token": None}),
            patch.object(oauth, "load_user_refresh_token", return_value="user-rt"),
            patch.object(oauth, "refresh_access_token", return_value="user-access"),
        ):
            token = oauth._oauth_access_token(
                company_id="company-1", user_id="user-1"
            )
        self.assertEqual(token, "user-access")


class TestCreateFreshWorkbook(unittest.TestCase):
    def test_calls_sheets_create_and_reads_response(self) -> None:
        service = MagicMock()
        service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "1abcExampleSheetId0001",
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/1abcExampleSheetId0001/edit",
        }
        with (
            patch.object(automation, "_sheets_v4_service", return_value=service),
            self.assertLogs(automation.logger, level="INFO") as captured,
        ):
            spreadsheet_id, spreadsheet_url, titles = automation._create_fresh_workbook(
                "access-token",
                "Tech Expo",
                ["Day 1", "Day 2"],
            )

        self.assertEqual(spreadsheet_id, "1abcExampleSheetId0001")
        self.assertIn("1abcExampleSheetId0001", spreadsheet_url)
        self.assertEqual(titles, ["Day 1", "Day 2"])
        create_kw = service.spreadsheets.return_value.create.call_args.kwargs
        body = create_kw["body"]
        self.assertEqual(body["properties"]["title"], "NCS-Tech Expo")
        self.assertEqual(len(body["sheets"]), 2)
        self.assertEqual(body["sheets"][0]["properties"]["title"], "Day 1")
        self.assertEqual(body["sheets"][1]["properties"]["index"], 1)
        logs = "\n".join(captured.output)
        self.assertIn("[GSHEET] calling fresh workbook creation", logs)
        self.assertIn("[GSHEET] Google response spreadsheet_id=1abcExampleSheetId0001", logs)


class TestAutomateEventSheets(unittest.TestCase):
    def test_new_event_creates_and_persists_on_event_id(self) -> None:
        captured_update: list[tuple] = []

        class PersistCursor:
            def execute(self, sql, params=None) -> None:
                captured_update.append((str(sql), params))

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        @contextmanager
        def fake_db_cursor(commit: bool = True, dict_cursor: bool = True):
            del commit, dict_cursor
            yield PersistCursor()

        with (
            patch.object(oauth, "_oauth_access_token", return_value="access-token") as token_fn,
            patch.object(
                automation,
                "_create_fresh_workbook",
                return_value=(
                    "1abcExampleSheetId0001",
                    "https://docs.google.com/spreadsheets/d/1abcExampleSheetId0001/edit",
                    ["Day 1", "Day 2"],
                ),
            ) as create_fn,
            patch.object(automation, "_ensure_worksheet", side_effect=lambda *_a, **_k: "Day 1"),
            patch.object(automation, "_ensure_header_row"),
            patch.object(automation, "_share_event_workbook"),
            patch.object(automation, "db_cursor", fake_db_cursor),
            self.assertLogs(automation.logger, level="INFO") as captured,
        ):
            result = automation.automate_event_sheets(
                event_id=EVENT_ID,
                event_name="Tech Expo",
                company_id="company-1",
                user_id="user-1",
                day_titles=["Day 1", "Day 2"],
                reuse_existing=False,
            )

        token_fn.assert_called_once_with(company_id="company-1", user_id="user-1")
        create_fn.assert_called_once()
        self.assertNotIn("spreadsheet_id", create_fn.call_args.kwargs)
        self.assertEqual(result["spreadsheet_id"], "1abcExampleSheetId0001")
        self.assertEqual(result["google_sheet_id"], "1abcExampleSheetId0001")
        self.assertEqual(len(captured_update), 1)
        sql, params = captured_update[0]
        self.assertIn("UPDATE managed_events", sql)
        self.assertIn("spreadsheet_id", sql)
        self.assertIn("spreadsheet_url", sql)
        self.assertIn("google_sheet_id", sql)
        self.assertIn("google_sheet_url", sql)
        self.assertEqual(params[-1], EVENT_ID)
        self.assertEqual(params[0], "1abcExampleSheetId0001")
        logs = "\n".join(captured.output)
        self.assertIn("[GSHEET] reuse_existing=False", logs)
        self.assertIn("[GSHEET] saving workbook metadata for event_id=", logs)
        self.assertIn("[GSHEET] workbook metadata saved", logs)

    def test_reuse_existing_true_is_rejected_for_new_event_path(self) -> None:
        with self.assertRaises(RuntimeError):
            automation.automate_event_sheets(
                event_id=EVENT_ID,
                event_name="Tech Expo",
                company_id=None,
                user_id="user-1",
                day_titles=["Day 1"],
                reuse_existing=True,
            )


if __name__ == "__main__":
    unittest.main()
