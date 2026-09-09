"""Company Display Name + Display Picture helpers."""

from __future__ import annotations

import unittest

from services.company_display_identity import (
    company_profile_relative_path,
    normalize_display_name,
    public_display_picture_url,
    validate_display_picture,
)


class TestNormalizeDisplayName(unittest.TestCase):
    def test_trims_and_collapses(self) -> None:
        self.assertEqual(normalize_display_name("  Yogesh's   Company  "), "Yogesh's Company")

    def test_empty(self) -> None:
        self.assertEqual(normalize_display_name(""), "")
        self.assertEqual(normalize_display_name(None), "")

    def test_caps_length(self) -> None:
        self.assertEqual(len(normalize_display_name("A" * 400)), 255)


class TestValidateDisplayPicture(unittest.TestCase):
    def test_accepts_jpeg(self) -> None:
        self.assertEqual(
            validate_display_picture(
                filename="logo.jpg",
                content_type="image/jpeg",
                size=1024,
            ),
            "image/jpeg",
        )

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            validate_display_picture(filename="x.png", content_type="image/png", size=0)

    def test_rejects_too_large(self) -> None:
        with self.assertRaises(ValueError):
            validate_display_picture(
                filename="x.png",
                content_type="image/png",
                size=6 * 1024 * 1024,
            )

    def test_rejects_pdf(self) -> None:
        with self.assertRaises(ValueError):
            validate_display_picture(
                filename="x.pdf",
                content_type="application/pdf",
                size=100,
            )


class TestProfilePaths(unittest.TestCase):
    def test_relative_path(self) -> None:
        path = company_profile_relative_path("abc-123", "image/png")
        self.assertEqual(path, "company-profiles/abc-123.png")

    def test_public_url_passthrough(self) -> None:
        self.assertEqual(
            public_display_picture_url("https://cdn.example/a.png"),
            "https://cdn.example/a.png",
        )

    def test_public_url_relative(self) -> None:
        url = public_display_picture_url("company-profiles/x.jpg")
        self.assertTrue(url.endswith("/assets/company-profiles/x.jpg") or url.startswith("/assets/"))


if __name__ == "__main__":
    unittest.main()
