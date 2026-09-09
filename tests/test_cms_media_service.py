"""CMS media library helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import cms_media_service as media


class TestValidateCmsMedia(unittest.TestCase):
    def test_accepts_png(self) -> None:
        self.assertEqual(
            media.validate_cms_media(
                filename="banner.png",
                content_type="image/png",
                size=1024,
            ),
            "image/png",
        )

    def test_accepts_pdf(self) -> None:
        self.assertEqual(
            media.validate_cms_media(
                filename="brochure.pdf",
                content_type="application/pdf",
                size=2048,
            ),
            "application/pdf",
        )

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            media.validate_cms_media(
                filename="x.png",
                content_type="image/png",
                size=0,
            )

    def test_rejects_exe(self) -> None:
        with self.assertRaises(ValueError):
            media.validate_cms_media(
                filename="x.exe",
                content_type="application/octet-stream",
                size=100,
            )


class TestCmsMediaRoundTrip(unittest.TestCase):
    def test_save_list_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cms-media"
            with mock.patch.object(media, "CMS_MEDIA_ROOT", root):
                with mock.patch.object(media, "_BACKEND_ROOT", Path(tmp)):
                    saved = media.save_cms_media(
                        "admin-1",
                        file_bytes=b"\x89PNG\r\n\x1a\nfake",
                        filename="Hero Banner.png",
                        content_type="image/png",
                    )
                    self.assertEqual(saved["kind"], "image")
                    self.assertTrue(saved["url"].endswith(saved["relative_path"]) or "/assets/" in saved["url"])
                    items = media.list_cms_media("admin-1")
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["filename"], saved["filename"])
                    deleted = media.delete_cms_media("admin-1", saved["filename"])
                    self.assertEqual(deleted["filename"], saved["filename"])
                    self.assertEqual(media.list_cms_media("admin-1"), [])


if __name__ == "__main__":
    unittest.main()
