"""Universal international phone normalization tests — full metadata coverage."""

from __future__ import annotations

import unittest

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, example_number_for_type

from utils.international_phone import (
    get_all_countries,
    get_supported_country_count,
    normalize_international_phone,
)
from utils.international_phone.whatsapp_adapter import (
    apply_international_phone_to_contact,
    prepare_whatsapp_recipient,
)


class TestCountryMetadataCoverage(unittest.TestCase):
    def test_all_supported_regions_have_calling_code(self) -> None:
        countries = get_all_countries()
        self.assertGreater(len(countries), 200)
        self.assertEqual(len(countries), get_supported_country_count())
        for country in countries:
            self.assertTrue(country["iso"])
            self.assertTrue(country["calling_code"].startswith("+"))
            self.assertGreater(len(country["calling_code"]), 1)

    def test_iterate_phonenumbers_supported_regions(self) -> None:
        """Verify architecture against entire phonenumbers metadata set."""
        missing_calling: list[str] = []
        for iso in phonenumbers.SUPPORTED_REGIONS:
            if iso == "001":
                continue
            code = phonenumbers.country_code_for_region(iso)
            if code == 0:
                missing_calling.append(iso)
        self.assertEqual(missing_calling, [])

    def test_no_hardcoded_small_country_subset(self) -> None:
        isos = {c["iso"] for c in get_all_countries()}
        self.assertIn("IN", isos)
        self.assertIn("SG", isos)
        self.assertIn("US", isos)
        self.assertIn("NG", isos)
        self.assertIn("BR", isos)
        self.assertGreater(len(isos), 240)


class TestNormalizeInternationalPhone(unittest.TestCase):
    def test_international_india_with_formatting(self) -> None:
        result = normalize_international_phone("+91 98765 43210")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["e164"], "+919876543210")
        self.assertEqual(result["country"], "IN")

    def test_international_singapore_ignores_selected_country(self) -> None:
        result = normalize_international_phone("+65 8748 4012", country="IN")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["country"], "SG")
        self.assertTrue(result["e164"].startswith("+65"))

    def test_national_requires_country(self) -> None:
        result = normalize_international_phone("9876543210")
        self.assertFalse(result["is_valid"])
        self.assertIn("select a country", result["error"].lower())

    def test_national_with_country(self) -> None:
        result = normalize_international_phone("9876543210", country="IN")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["e164"], "+919876543210")

    def test_invalid_number(self) -> None:
        result = normalize_international_phone("+91 123", country="IN")
        self.assertFalse(result["is_valid"])

    def test_international_double_zero_prefix(self) -> None:
        result = normalize_international_phone("0091 98765 43210")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["e164"], "+919876543210")

    def test_whatsapp_recipient_digits_only(self) -> None:
        result = normalize_international_phone("+1 415 555 2671")
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["whatsapp_recipient"], "14155552671")
        self.assertNotIn("+", result["whatsapp_recipient"])


class TestExampleNumbersAcrossRegions(unittest.TestCase):
    """Use library example numbers — not invented digits for every country."""

    def test_example_numbers_normalize_for_mobile_regions(self) -> None:
        tested = 0
        failed: list[str] = []
        for iso in phonenumbers.SUPPORTED_REGIONS:
            if iso in ("001", "ZZ"):
                continue
            try:
                ex = example_number_for_type(iso, PhoneNumberType.MOBILE)
            except Exception:
                ex = None
            if ex is None:
                try:
                    ex = example_number_for_type(iso, PhoneNumberType.FIXED_LINE)
                except Exception:
                    ex = None
            if ex is None:
                continue
            e164 = phonenumbers.format_number(ex, PhoneNumberFormat.E164)
            result = normalize_international_phone(e164)
            tested += 1
            if not result.get("is_valid"):
                failed.append(iso)
        self.assertGreater(tested, 100, "Expected example numbers for many regions")
        self.assertEqual(failed, [], f"Failed regions: {failed[:10]}")


class TestWhatsAppAdapter(unittest.TestCase):
    def test_adapter_rejects_invalid_before_whatsapp(self) -> None:
        out = prepare_whatsapp_recipient("not-a-phone")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["whatsapp_recipient"])

    def test_adapter_returns_e164(self) -> None:
        out = prepare_whatsapp_recipient("+44 7911 123456")
        self.assertTrue(out["ok"])
        self.assertEqual(out["e164"], "+447911123456")
        self.assertEqual(out["whatsapp_recipient"], "447911123456")

    def test_apply_to_contact_legacy_local_plus_dial(self) -> None:
        contact = {
            "phone": "9876543210",
            "countryCode": "+91",
            "countryName": "India",
        }
        updated = apply_international_phone_to_contact(contact)
        self.assertEqual(updated["phone"], "919876543210")
        self.assertEqual(updated["countryIso"], "IN")
        self.assertEqual(updated["countryCode"], "+91")

    def test_apply_to_contact_international_ignores_wrong_country(self) -> None:
        contact = {
            "phone": "+65 8748 4012",
            "countryCode": "+91",
            "countryIso": "IN",
        }
        updated = apply_international_phone_to_contact(contact)
        self.assertTrue(updated["phone"].startswith("65"))
        self.assertEqual(updated["countryIso"], "SG")


if __name__ == "__main__":
    unittest.main()
