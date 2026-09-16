"""Country-agnostic international phone normalization tests.

India/UAE cases are regressions only. Multi-region coverage uses phonenumbers
example numbers across continents and calling-code lengths.
"""

from __future__ import annotations

import unittest

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, example_number_for_type

from utils.international_phone import (
    get_all_countries,
    get_supported_country_count,
    normalize_international_phone,
)
from utils.international_phone.cc_dedupe import strip_duplicate_calling_code_prefix
from utils.international_phone.whatsapp_adapter import (
    apply_international_phone_to_contact,
    prepare_whatsapp_recipient,
)


# Multi-continent sample — proves algorithm is not India/UAE-specific.
REGION_SAMPLE: list[tuple[str, str]] = [
    ("US", "North America"),
    ("CA", "North America"),
    ("BR", "South America"),
    ("AR", "South America"),
    ("GB", "Europe"),
    ("DE", "Europe"),
    ("FR", "Europe"),
    ("AE", "Middle East"),
    ("SA", "Middle East"),
    ("IN", "South Asia"),
    ("BD", "South Asia"),
    ("JP", "East Asia"),
    ("KR", "East Asia"),
    ("SG", "Southeast Asia"),
    ("MY", "Southeast Asia"),
    ("NG", "Africa"),
    ("ZA", "Africa"),
    ("AU", "Oceania"),
    ("NZ", "Oceania"),
]


def _example_e164(iso: str) -> str | None:
    for number_type in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE):
        try:
            ex = example_number_for_type(iso, number_type)
        except Exception:
            ex = None
        if ex is not None:
            return phonenumbers.format_number(ex, PhoneNumberFormat.E164)
    return None


def _cc_digits(iso: str) -> str:
    return str(phonenumbers.country_code_for_region(iso))


def _region_rows() -> list[dict]:
    rows: list[dict] = []
    for iso, region in REGION_SAMPLE:
        e164 = _example_e164(iso)
        if not e164:
            continue
        cc = _cc_digits(iso)
        rows.append({"iso": iso, "region": region, "e164": e164, "cc": cc})
    return rows


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


class TestStripHelperGeneric(unittest.TestCase):
    def test_strip_duplicate_prefix_table(self) -> None:
        cases = [
            ("971", "971557274174", "557274174"),
            ("91", "919820305322", "9820305322"),
            ("44", "447911123456", "7911123456"),
            ("65", "6587484012", "87484012"),
            ("61", "61412345678", "412345678"),
        ]
        for cc, national, expected in cases:
            with self.subTest(cc=cc):
                self.assertEqual(
                    strip_duplicate_calling_code_prefix(national, cc),
                    expected,
                )

    def test_no_strip_when_not_prefixed(self) -> None:
        self.assertEqual(
            strip_duplicate_calling_code_prefix("557274174", "971"),
            "557274174",
        )


class TestRegressionIndiaUae(unittest.TestCase):
    """Keep IN/AE OCR regressions — not special-cased in the algorithm."""

    def test_dedupe_ocr_duplicated_uae_calling_code(self) -> None:
        result = normalize_international_phone("+971 971557274174")
        self.assertTrue(result["is_valid"], result)
        self.assertEqual(result["e164"], "+971557274174")
        self.assertEqual(result["national_number"], "557274174")
        self.assertEqual(result["country"], "AE")

    def test_dedupe_ocr_duplicated_india_calling_code(self) -> None:
        result = normalize_international_phone("+91 919820305322")
        self.assertTrue(result["is_valid"], result)
        self.assertEqual(result["e164"], "+919820305322")
        self.assertEqual(result["national_number"], "9820305322")
        self.assertEqual(result["country"], "IN")

    def test_national_with_duplicated_cc_when_country_selected(self) -> None:
        ae = normalize_international_phone("971557274174", country="AE")
        self.assertTrue(ae["is_valid"], ae)
        self.assertEqual(ae["national_number"], "557274174")
        self.assertEqual(ae["e164"], "+971557274174")

        inn = normalize_international_phone("919820305322", country="IN")
        self.assertTrue(inn["is_valid"], inn)
        self.assertEqual(inn["national_number"], "9820305322")
        self.assertEqual(inn["e164"], "+919820305322")

    def test_adapter_strips_duplicated_cc_from_contact_phone(self) -> None:
        updated = apply_international_phone_to_contact(
            {
                "phone": "971971557274174",
                "countryCode": "+971",
                "countryIso": "AE",
            }
        )
        self.assertEqual(updated["phone"], "971557274174")
        self.assertEqual(updated["countryCode"], "+971")


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

    def test_bare_national_still_requires_country(self) -> None:
        result = normalize_international_phone("9820305322")
        self.assertFalse(result["is_valid"])
        self.assertIn("select a country", result["error"].lower())


class TestTableDrivenMultiRegion(unittest.TestCase):
    """Parameterized coverage across continents / CC lengths / national lengths."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _region_rows()

    def test_multi_region_coverage_breadth(self) -> None:
        regions = {r["region"] for r in self.rows}
        self.assertGreaterEqual(len(regions), 8)
        self.assertGreaterEqual(len(self.rows), 15)
        cc_lengths = {len(r["cc"]) for r in self.rows}
        self.assertGreaterEqual(len(cc_lengths), 2)

    def test_international_e164_normalize(self) -> None:
        for row in self.rows:
            with self.subTest(**row):
                result = normalize_international_phone(row["e164"])
                self.assertTrue(result["is_valid"], result)
                self.assertEqual(result["e164"], row["e164"])
                self.assertEqual(result["country"], row["iso"])
                self.assertEqual(result["country_calling_code"], row["cc"])
                self.assertEqual(
                    result["whatsapp_recipient"],
                    row["e164"].replace("+", ""),
                )
                self.assertTrue(result["whatsapp_recipient"].startswith(row["cc"]))
                self.assertGreater(len(result["national_number"]), 0)

    def test_spaces_hyphens_parentheses(self) -> None:
        for row in self.rows:
            cc = row["cc"]
            national = row["e164"][1 + len(cc) :]
            spaced = f"+{cc} {national}"
            hyphens = f"+{cc}-{national[:3]}-{national[3:]}"
            parens = f"+({cc}) {national}"
            for label, raw in (
                ("spaces", spaced),
                ("hyphens", hyphens),
                ("parens", parens),
            ):
                with self.subTest(iso=row["iso"], format=label):
                    result = normalize_international_phone(raw)
                    self.assertTrue(result["is_valid"], result)
                    self.assertEqual(result["e164"], row["e164"])
                    self.assertEqual(result["country"], row["iso"])

    def test_ocr_duplicated_calling_code(self) -> None:
        for row in self.rows:
            if len(row["cc"]) < 2:
                continue
            cc = row["cc"]
            national = row["e164"].replace("+", "")[len(cc) :]
            duplicated = f"+{cc}{cc}{national}"
            with self.subTest(**row):
                # Ambiguous if duplicated digits are themselves a valid number.
                try:
                    parsed_dup = phonenumbers.parse(duplicated, None)
                    if phonenumbers.is_valid_number(parsed_dup):
                        continue
                except phonenumbers.NumberParseException:
                    pass
                result = normalize_international_phone(duplicated)
                self.assertTrue(result["is_valid"], result)
                self.assertEqual(result["e164"], row["e164"])
                self.assertEqual(result["country"], row["iso"])
                self.assertEqual(result["national_number"], national)

    def test_national_with_selected_country(self) -> None:
        for row in self.rows:
            cc = row["cc"]
            national = row["e164"].replace("+", "")[len(cc) :]
            with self.subTest(**row):
                result = normalize_international_phone(national, country=row["iso"])
                self.assertTrue(result["is_valid"], result)
                self.assertEqual(result["e164"], row["e164"])
                self.assertEqual(result["country"], row["iso"])

    def test_country_selected_plus_cc_repeated_in_phone_field(self) -> None:
        for row in self.rows:
            if len(row["cc"]) < 2:
                continue
            cc = row["cc"]
            national = row["e164"].replace("+", "")[len(cc) :]
            with_cc = f"{cc}{national}"
            with self.subTest(**row):
                try:
                    parsed_dup = phonenumbers.parse(f"+{with_cc}", None)
                    if phonenumbers.is_valid_number(parsed_dup):
                        continue
                except phonenumbers.NumberParseException:
                    pass
                result = normalize_international_phone(with_cc, country=row["iso"])
                self.assertTrue(result["is_valid"], result)
                self.assertEqual(result["e164"], row["e164"])
                self.assertEqual(result["national_number"], national)

    def test_payload_whatsapp_reconstruction(self) -> None:
        for row in self.rows:
            with self.subTest(**row):
                contact = apply_international_phone_to_contact(
                    {
                        "phone": row["e164"],
                        "countryIso": row["iso"],
                        "countryCode": f"+{row['cc']}",
                    }
                )
                self.assertEqual(contact["phone"], row["e164"].replace("+", ""))
                self.assertEqual(contact["countryCode"], f"+{row['cc']}")
                self.assertEqual(contact["countryIso"], row["iso"])


class TestAmbiguousAndInvalid(unittest.TestCase):
    def test_does_not_guess_missing_country(self) -> None:
        result = normalize_international_phone("9820305322")
        self.assertFalse(result["is_valid"])

    def test_invalid_short_international(self) -> None:
        result = normalize_international_phone("+99 12")
        self.assertFalse(result["is_valid"])

    def test_valid_number_not_blindly_rewritten(self) -> None:
        e164 = _example_e164("SG")
        self.assertIsNotNone(e164)
        result = normalize_international_phone(e164)  # type: ignore[arg-type]
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["e164"], e164)

    def test_ambiguous_ocr_duplicate_left_alone_when_also_valid(self) -> None:
        """When +CC+CC+national is itself valid, do not guess a strip."""
        e164 = _example_e164("DE")
        self.assertIsNotNone(e164)
        cc = _cc_digits("DE")
        national = e164.replace("+", "")[len(cc) :]  # type: ignore[union-attr]
        duplicated = f"+{cc}{cc}{national}"
        parsed = phonenumbers.parse(duplicated, None)
        if not phonenumbers.is_valid_number(parsed):
            self.skipTest("DE duplicate form not valid in this metadata version")
        result = normalize_international_phone(duplicated)
        self.assertTrue(result["is_valid"], result)
        self.assertEqual(result["e164"], duplicated)


class TestExampleNumbersAcrossRegions(unittest.TestCase):
    """Use library example numbers — not invented digits for every country."""

    def test_example_numbers_normalize_for_mobile_regions(self) -> None:
        tested = 0
        failed: list[str] = []
        for iso in phonenumbers.SUPPORTED_REGIONS:
            if iso in ("001", "ZZ"):
                continue
            e164 = _example_e164(iso)
            if e164 is None:
                continue
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
