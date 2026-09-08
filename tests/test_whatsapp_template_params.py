"""Meta template body/header param counts must match the approved definition."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# auth.constants requires a non-default JWT secret at import time.
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from services.whatsapp_service import build_card_received_template_components


class JourneyStackTemplateParamsTests(unittest.TestCase):
    def test_static_meta_body_sends_zero_body_params(self) -> None:
        """journey_stack1-style: IMAGE header + static body → no body parameters."""
        meta = {
            "name": "journey_stack1",
            "language": "en",
            "status": "APPROVED",
            "components": [
                {"type": "HEADER", "format": "IMAGE"},
                {
                    "type": "BODY",
                    "text": "ITB MUMBAI • PARTNER OFFER\n\nExclusive partner offers.",
                },
            ],
        }
        templates = {
            "whatsapp_header_format": "IMAGE",
            "whatsapp_header": "CardScan Message",
            "whatsapp_header_media_url": "https://api.namecardscan.com/assets/Journey-Stack-Logo.png",
            "whatsapp_header_media": [
                {
                    "format": "IMAGE",
                    "text": "CardScan Message",
                    "media_url": "https://api.namecardscan.com/assets/Journey-Stack-Logo.png",
                    "media_filename": "",
                }
            ],
            "whatsapp_body": "ITB MUMBAI • PARTNER OFFER\n\nExclusive partner offers.",
            "token_map": {},
        }

        with patch(
            "services.admin_runtime_config.runtime_templates",
            return_value=templates,
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ):
            components = build_card_received_template_components(
                {"fullName": "Alex", "eventName": "ITB"},
                template_name="journey_stack1",
                meta_template=meta,
            )

        types = [c.get("type") for c in components]
        self.assertIn("header", types)
        self.assertNotIn("body", types)
        header = next(c for c in components if c.get("type") == "header")
        self.assertEqual(header["parameters"][0]["type"], "image")

    def test_meta_two_vars_still_sends_two_body_params(self) -> None:
        meta = {
            "name": "card_final_ula",
            "language": "en",
            "components": [
                {"type": "HEADER", "format": "VIDEO"},
                {"type": "BODY", "text": "Hi {{1}}, great meeting you at {{2}}."},
            ],
        }
        with patch(
            "services.admin_runtime_config.runtime_templates",
            return_value={"whatsapp_header_format": "NONE", "token_map": {}},
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ), patch(
            "services.whatsapp_service._build_header_component",
            return_value={
                "type": "header",
                "parameters": [{"type": "video", "video": {"link": "https://example.com/v.mp4"}}],
            },
        ):
            components = build_card_received_template_components(
                {"fullName": "Alex Yogesh", "eventName": "ITB Mumbai"},
                template_name="card_final_ula",
                meta_template=meta,
            )

        body = next(c for c in components if c.get("type") == "body")
        self.assertEqual(len(body["parameters"]), 2)
        self.assertTrue(body["parameters"][0]["text"])
        self.assertEqual(body["parameters"][1]["text"], "ITB Mumbai")

    def test_does_not_send_cms_image_header_when_meta_has_none(self) -> None:
        meta = {
            "name": "plain_body",
            "language": "en",
            "components": [
                {"type": "BODY", "text": "Hello there — no variables."},
            ],
        }
        templates = {
            "whatsapp_header_format": "IMAGE",
            "whatsapp_header_media_url": "https://api.namecardscan.com/assets/logo.png",
            "whatsapp_body": "Hello there — no variables.",
            "token_map": {},
        }
        with patch(
            "services.admin_runtime_config.runtime_templates",
            return_value=templates,
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ):
            components = build_card_received_template_components(
                {"fullName": "Alex"},
                template_name="plain_body",
                meta_template=meta,
            )

        self.assertEqual(components, [])


if __name__ == "__main__":
    unittest.main()
