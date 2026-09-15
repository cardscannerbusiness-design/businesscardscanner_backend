"""Meta template body/header param counts must match the approved definition."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# auth.constants requires a non-default JWT secret at import time.
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-jwt-secret-not-for-production")

from services.whatsapp_service import (
    _is_builtin_ula_video_template,
    _ordered_outbound_template_names,
    build_card_received_template_components,
)


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

    def test_journey_stack1_not_treated_as_ula_video_when_env_points_at_it(self) -> None:
        """If .env CARD_RECEIVED was set to journey_stack1, still use IMAGE/Meta path."""
        self.assertFalse(_is_builtin_ula_video_template("journey_stack1"))
        self.assertTrue(_is_builtin_ula_video_template("card_final_ula"))

        meta = {
            "name": "journey_stack1",
            "language": "en",
            "components": [
                {"type": "HEADER", "format": "IMAGE"},
                {"type": "BODY", "text": "Static partner offer — no variables."},
            ],
        }
        templates = {
            "whatsapp_header_format": "IMAGE",
            "whatsapp_header_media_url": "https://api.namecardscan.com/assets/Journey-Stack-Logo.png",
            "whatsapp_header_media": [
                {
                    "format": "IMAGE",
                    "media_url": "https://api.namecardscan.com/assets/Journey-Stack-Logo.png",
                }
            ],
            "whatsapp_body": "Static partner offer — no variables.",
            "token_map": {},
        }
        with patch(
            "services.whatsapp_service.CARD_RECEIVED_TEMPLATE_NAME",
            "journey_stack1",
        ), patch(
            "services.whatsapp_service.BUSINESS_CARD_TEMPLATE_NAME",
            "journey_stack1",
        ), patch(
            "services.whatsapp_service.SCAN_THANKS_TEMPLATE_NAME",
            "journey_stack1",
        ), patch(
            "services.admin_runtime_config.runtime_templates",
            return_value=templates,
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ):
            components = build_card_received_template_components(
                {"fullName": "Alex"},
                template_name="journey_stack1",
                meta_template=meta,
            )

        types = [c.get("type") for c in components]
        self.assertIn("header", types)
        self.assertNotIn("body", types)
        header = next(c for c in components if c.get("type") == "header")
        self.assertEqual(header["parameters"][0]["type"], "image")

    def test_cms_runtime_fallback_names_exclude_global_ula(self) -> None:
        cms_wa = {
            "enabled": True,
            "access_token": "tok",
            "phone_number_id": "pid",
            "card_received_template_name": "journey_stack1",
            "business_card_template_name": "journey_stack1",
            "scan_template_name": "journey_stack1",
        }
        with patch(
            "services.admin_runtime_config.runtime_whatsapp",
            return_value=cms_wa,
        ), patch(
            "services.whatsapp_service.CARD_RECEIVED_TEMPLATE_NAME",
            "card_final_ula",
        ):
            names = _ordered_outbound_template_names()
        self.assertEqual(names, ["journey_stack1"])
        self.assertNotIn("card_final_ula", names)

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

    def test_ncs_uten_four_vars_uses_meta_examples_not_emdash(self) -> None:
        meta = {
            "name": "ncs_uten_version_1",
            "language": "en",
            "components": [
                {
                    "type": "HEADER",
                    "format": "IMAGE",
                    "example": {
                        "header_handle": ["https://example.com/header.jpg"],
                    },
                },
                {
                    "type": "BODY",
                    "text": "Dear {{1}} at {{2}} from {{3}} to {{4}}.",
                    "example": {
                        "body_text": [["sugitha", "ATM 2026", "14th sep", "15th Sep"]]
                    },
                },
            ],
        }
        with patch(
            "services.admin_runtime_config.runtime_templates",
            return_value={"whatsapp_header_format": "NONE", "token_map": {}},
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ), patch(
            "services.whatsapp_service._header_media_from_url_or_upload",
            return_value={
                "type": "header",
                "parameters": [{"type": "image", "image": {"id": "media123"}}],
            },
        ):
            components = build_card_received_template_components(
                {"fullName": "Test Contact", "eventName": "CMS Test"},
                template_name="ncs_uten_version_1",
                meta_template=meta,
            )

        body = next(c for c in components if c.get("type") == "body")
        texts = [p["text"] for p in body["parameters"]]
        self.assertEqual(len(texts), 4)
        self.assertEqual(texts[0], "Test")
        self.assertEqual(texts[1], "CMS Test")
        self.assertEqual(texts[2], "14th sep")
        self.assertEqual(texts[3], "15th Sep")
        self.assertNotIn("—", texts)
        header = next(c for c in components if c.get("type") == "header")
        self.assertEqual(header["parameters"][0]["image"]["id"], "media123")

    def test_requires_header_media_when_meta_needs_image(self) -> None:
        meta = {
            "name": "needs_image",
            "language": "en",
            "components": [
                {"type": "HEADER", "format": "IMAGE"},
                {"type": "BODY", "text": "Hello"},
            ],
        }
        with patch(
            "services.admin_runtime_config.runtime_templates",
            return_value={"whatsapp_header_format": "NONE", "token_map": {}},
        ), patch(
            "services.admin_runtime_config.runtime_email",
            return_value={},
        ), patch(
            "services.whatsapp_service._build_cms_header_component",
            return_value=None,
        ), patch(
            "services.whatsapp_service._build_header_component",
            return_value=None,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                build_card_received_template_components(
                    {"fullName": "Alex"},
                    template_name="needs_image",
                    meta_template=meta,
                )
        self.assertIn("requires a IMAGE header", str(ctx.exception))

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
