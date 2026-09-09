"""Per-user profile identity (display name + picture path)."""

from __future__ import annotations

import unittest

from services.user_profile_identity import (
    effective_display_name,
    normalize_display_name,
    user_profile_relative_path,
)


class TestUserProfilePaths(unittest.TestCase):
    def test_path_is_user_scoped(self) -> None:
        path = user_profile_relative_path("user-abc-123", "image/jpeg")
        self.assertEqual(path, "profile-pictures/user-abc-123/profile.jpg")
        self.assertIn("user-abc-123", path)
        self.assertNotEqual(path, "profile-pictures/profile.jpg")

    def test_different_users_different_paths(self) -> None:
        a = user_profile_relative_path("aaaa", "image/png")
        b = user_profile_relative_path("bbbb", "image/png")
        self.assertNotEqual(a, b)

    def test_effective_display_name_prefers_custom(self) -> None:
        self.assertEqual(
            effective_display_name(
                display_name="John",
                first_name="A",
                last_name="B",
            ),
            "John",
        )

    def test_effective_falls_back_to_first_last(self) -> None:
        self.assertEqual(
            effective_display_name(display_name="", first_name="David", last_name="Lee"),
            "David Lee",
        )

    def test_normalize(self) -> None:
        self.assertEqual(normalize_display_name("  John   "), "John")


if __name__ == "__main__":
    unittest.main()
