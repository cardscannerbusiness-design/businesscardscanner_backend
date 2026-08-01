"""Google OAuth helper tests — no network."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services import google_oauth_service as oauth


class TestOAuthConfig(unittest.TestCase):
    def test_not_configured(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_OAUTH_CLIENT_ID": "",
                    "GOOGLE_OAUTH_CLIENT_SECRET": "",
                    "GOOGLE_OAUTH_REDIRECT_URI": "",
                    "GOOGLE_OAUTH_CLIENT_JSON": "",
                },
                clear=False,
            ),
            patch.object(oauth, "_find_downloaded_client_secret", return_value=None),
        ):
            oauth._oauth_client_cache = None
            self.assertFalse(oauth.is_oauth_configured())
            self.assertIsNone(
                oauth.build_authorize_url(user_id="u1", role="ADMIN")
            )

    def test_build_authorize_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_OAUTH_CLIENT_ID": "cid",
                "GOOGLE_OAUTH_CLIENT_SECRET": "sec",
                "GOOGLE_OAUTH_REDIRECT_URI": "http://localhost/callback",
                "JWT_SECRET_KEY": "test-secret-key-at-least-32-bytes!!",
            },
        ):
            oauth._oauth_client_cache = None
            url = oauth.build_authorize_url(user_id="u1", role="ADMIN")
            self.assertIn("accounts.google.com", url)
            self.assertIn("client_id=cid", url)
            self.assertIn("access_type=offline", url)


class TestOAuthCreateUsesUserToken(unittest.TestCase):
    def test_ensure_company_requires_oauth_when_no_sheet(self) -> None:
        from services import google_sheets_service as sheets

        with (
            patch.object(sheets, "_auth_headers", return_value={"Authorization": "Bearer sa"}),
            patch.object(
                sheets,
                "_load_company_sheet_meta",
                return_value={
                    "company_name": "Acme",
                    "google_sheet_id": None,
                    "admin_email": "admin@acme.com",
                    "user_emails": [],
                },
            ),
            patch(
                "services.google_oauth_service.load_company_admin_oauth",
                return_value={"admin_id": "a1", "refresh_token": None},
            ),
        ):
            sheets._workbook_cache.clear()
            with self.assertRaises(RuntimeError) as ctx:
                sheets.ensure_company_sheet("company-1")
            self.assertIn("Connect Google Drive", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
