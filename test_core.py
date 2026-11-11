import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import core


class AccountTaxTests(unittest.TestCase):
    def test_tesseract_discovery_prefers_path_executable(self):
        with patch("core.shutil.which", return_value=r"C:\tools\tesseract.exe"):
            self.assertEqual(
                core._find_tesseract_executable(),
                r"C:\tools\tesseract.exe",
            )

    def test_tesseract_discovery_finds_standard_windows_install(self):
        with (
            patch("core.shutil.which", return_value=None),
            patch.dict(
                core.os.environ,
                {
                    "ProgramFiles": r"C:\TestApps",
                    "ProgramFiles(x86)": r"C:\TestApps32",
                },
            ),
            patch.object(
                Path,
                "is_file",
                autospec=True,
                side_effect=lambda path: str(path)
                == r"C:\TestApps\Tesseract-OCR\tesseract.exe",
            ),
        ):
            self.assertEqual(
                core._find_tesseract_executable(),
                r"C:\TestApps\Tesseract-OCR\tesseract.exe",
            )

    def test_configure_tesseract_uses_discovered_path_when_setting_is_empty(self):
        import pytesseract

        original_command = pytesseract.pytesseract.tesseract_cmd
        try:
            with patch(
                "core._find_tesseract_executable",
                return_value=r"C:\TestApps\Tesseract-OCR\tesseract.exe",
            ):
                core.configure_tesseract("")
                self.assertEqual(
                    pytesseract.pytesseract.tesseract_cmd,
                    r"C:\TestApps\Tesseract-OCR\tesseract.exe",
                )
        finally:
            pytesseract.pytesseract.tesseract_cmd = original_command

    def test_twelve_percent_is_checked_against_balance(self):
        assessment = core.assess_account_tax(
            Decimal("894410"),
            [
                core.AccountTransaction(Decimal("-121965"), "2026-07-20", "14:39"),
                core.AccountTransaction(Decimal("2400")),
            ],
        )

        self.assertEqual(assessment.required_tax, Decimal("107329.20"))
        paid_transaction = assessment.paid_transaction
        self.assertIsNotNone(paid_transaction)
        assert paid_transaction is not None
        self.assertEqual(paid_transaction.amount, Decimal("-121965"))
        self.assertIn("2026-07-20 14:39", core.format_account_result(assessment))

    def test_smaller_negative_transaction_does_not_count_as_paid(self):
        assessment = core.assess_account_tax(
            Decimal("894410"),
            [core.AccountTransaction(Decimal("-100000"))],
        )

        self.assertIsNone(assessment.paid_transaction)
        self.assertIn("Nie wykryto zapłaty", core.format_account_result(assessment))

    def test_screen_amount_accepts_spaced_balance_and_signed_transfer(self):
        self.assertEqual(core._parse_screen_amount("894 410$"), Decimal("894410"))
        self.assertEqual(core._parse_screen_amount("-121965$"), Decimal("-121965"))
        self.assertEqual(core._parse_screen_amount("+2,400$"), Decimal("2400"))
        self.assertEqual(core._parse_screen_amount("1.234.567,89 $"), Decimal("1234567.89"))
        self.assertEqual(core._parse_screen_amount("($1,200.00)"), Decimal("-1200.00"))
        self.assertIsNone(core._parse_screen_amount("no amount"))

    def test_account_screen_text_parser_handles_labeled_wide_layout(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Account balance: $ 894,410.00",
                    "Transactions",
                    "2026-07-20 14:39 | Tax payment -$107,329.20",
                    "2026-07-20 14:40 | Deposit +$2,400.00",
                )
            )
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.balance, Decimal("894410.00"))
        self.assertEqual(assessment.required_tax, Decimal("107329.20"))
        paid_transaction = assessment.paid_transaction
        self.assertIsNotNone(paid_transaction)
        assert paid_transaction is not None
        self.assertEqual(paid_transaction.amount, Decimal("-107329.20"))
        self.assertEqual(paid_transaction.date, "2026-07-20")
        self.assertEqual(paid_transaction.time, "14:39")
        self.assertEqual(len(assessment.transactions), 1)

    def test_account_screen_parser_recognizes_balans_and_strips_merged_table_headers(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Balans",
                    "$1,904,745",
                    "Data Kwota Przelew z firmy Up-n-Atom przez",
                    "2026-11-02",
                    "-121,965$",
                )
            )
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertTrue(assessment.balance_observed)
        self.assertEqual(assessment.balance, Decimal("1904745"))
        self.assertEqual(assessment.required_tax, Decimal("228569.40"))
        self.assertEqual(len(assessment.transactions), 1)
        transaction = assessment.transactions[0]
        self.assertEqual(transaction.amount, Decimal("-121965"))
        self.assertIn("Przelew z firmy Up-n-Atom", transaction.description)
        self.assertNotIn("Data Kwota", transaction.description)

    def test_balance_extraction_uses_first_value_after_label(self):
        self.assertEqual(
            core._extract_labeled_balance(
                "Saldo konta [24\n"
                "1 904 745$ $3\n"
                "2163 2275 2388 4331 Przychody"
            ),
            Decimal("1904745"),
        )

        self.assertEqual(
            core._extract_labeled_balance(
                "STAN KONTA\n"
                "894 410$"
            ),
            Decimal("894410"),
        )

    def test_account_parser_selects_balance_not_adjacent_income_and_expense_totals(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Saldo konta wv & A",
                    "1904 745$ $3243858 $2115705 U",
                    "2103 2275 2388 4331 Przychody",
                    "Ostatnie czynnosci Wszystkie",
                    "Przelew z firmy Up-n-Atom przez Theo Oliveira",
                    "2026-11-02 19:16 -296720$",
                )
            ),
            allow_zero_balance=True,
            require_balance=False,
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertTrue(assessment.balance_observed)
        self.assertEqual(assessment.balance, Decimal("1904745"))
        self.assertEqual(assessment.required_tax, Decimal("228569.40"))

    def test_missing_balance_does_not_create_zero_tax_candidate(self):
        assessment = core._parse_account_screen_text(
            "2026-11-02 Przelew podatku -$121,965",
            allow_zero_balance=True,
            require_balance=False,
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertFalse(assessment.balance_observed)
        self.assertIsNone(core.find_tax_payment_candidate(assessment))
        diagnostics = core.format_ocr_diagnostics(assessment)
        self.assertIn("nie można obliczyć bez odczytanego salda", diagnostics)
        self.assertNotIn("12% salda: 0 $", diagnostics)

    def test_account_screen_uses_full_image_ocr_variants(self):
        from PIL import Image

        image_buffer = io.BytesIO()
        Image.new("RGB", (80, 40), "white").save(image_buffer, format="PNG")
        recognized_screen = "\n".join(
            (
                "Current balance $1,000,000",
                "2026-07-20 14:39 Tax payment -$120,000",
            )
        )

        with patch(
            "pytesseract.get_languages",
            return_value=["eng"],
        ), patch(
            "pytesseract.image_to_data",
            side_effect=[
                {
                    "text": ["", ""],
                    "conf": ["-1", "-1"],
                    "block_num": [0, 0],
                    "par_num": [0, 0],
                    "line_num": [0, 0],
                    "word_num": [0, 0],
                },
                {
                    "text": ["Current", "balance", "$1,000,000", "2026-07-20", "14:39", "Tax", "payment", "-$120,000"],
                    "conf": ["94", "92", "90", "91", "90", "92", "91", "90"],
                    "block_num": [1] * 8,
                    "par_num": [1] * 8,
                    "line_num": [1, 1, 1, 2, 2, 2, 2, 2],
                    "word_num": [1, 2, 3, 1, 2, 3, 4, 5],
                },
            ],
        ) as image_to_string:
            assessment = core.analyze_account_screen(image_buffer.getvalue())

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.balance, Decimal("1000000"))
        paid_transaction = assessment.paid_transaction
        self.assertIsNotNone(paid_transaction)
        assert paid_transaction is not None
        self.assertEqual(paid_transaction.amount, Decimal("-120000"))
        self.assertEqual(assessment.ocr_confidence, 91.25)
        self.assertEqual(paid_transaction.description, "Tax payment")

    def test_account_screen_text_parser_handles_european_dates_and_numbers(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Stan konta",
                    "1 234 567,89 $",
                    "Historia transakcji",
                    "21.07.2026 15:10 Podatek -200 000,00 $",
                )
            )
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.balance, Decimal("1234567.89"))
        paid_transaction = assessment.paid_transaction
        self.assertIsNotNone(paid_transaction)
        assert paid_transaction is not None
        self.assertEqual(paid_transaction.date, "2026-07-21")
        self.assertEqual(paid_transaction.amount, Decimal("-200000.00"))

    def test_transaction_card_screenshot_layout_extracts_amount_and_time(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Konto Firmowe - Department of Justice",
                    "6463 9592 9290 1339",
                    "-7060$",
                    "Dziś o 14:30",
                )
            ),
            allow_zero_balance=True,
            require_balance=False,
            ocr_confidence=88,
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertFalse(assessment.balance_observed)
        self.assertEqual(len(assessment.transactions), 1)
        transaction = assessment.transactions[0]
        self.assertEqual(transaction.amount, Decimal("-7060"))
        self.assertEqual(transaction.time, "14:30")
        self.assertIn("Department of Justice", transaction.description)
        self.assertNotIn("6463", transaction.description)
        self.assertEqual(transaction.ocr_confidence, 88)

    def test_finance_dashboard_screenshot_matches_description_amount_and_date(self):
        assessment = core._parse_account_screen_text(
            "\n".join(
                (
                    "Saldo",
                    "1 904 745 $",
                    "Transakcje",
                    "2026-11-02 19:16",
                    "Przelew z firmy Up-n-Atom przez Theo Oliveira",
                    "-296720$",
                )
            ),
            ocr_confidence=92,
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.balance, Decimal("1904745"))
        self.assertEqual(len(assessment.transactions), 1)
        transaction = assessment.transactions[0]
        self.assertEqual(transaction.amount, Decimal("-296720"))
        self.assertEqual(transaction.date, "2026-11-02")
        self.assertEqual(transaction.time, "19:16")
        self.assertIn("Up-n-Atom", transaction.description)
        match = core.find_tax_payment_candidate(assessment)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertTrue(match.requires_confirmation)

    def test_tax_candidate_requires_confirmation_when_transfer_is_not_labeled_tax(self):
        assessment = core.assess_account_tax(
            Decimal("1904745"),
            [
                core.AccountTransaction(
                    Decimal("-296720"),
                    "2026-11-02",
                    "19:16",
                    "Przelew z firmy Up-n-Atom przez Theo Oliveira",
                    92,
                )
            ],
            ocr_confidence=92,
        )

        match = core.find_tax_payment_candidate(assessment)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertTrue(match.requires_confirmation)
        self.assertIn("nie potwierdza jednoznacznie", match.reason)

    def test_tax_candidate_prefers_a_dated_transaction_when_amounts_tie(self):
        assessment = core.assess_account_tax(
            Decimal("1000"),
            [
                core.AccountTransaction(Decimal("-150"), description="Transfer"),
                core.AccountTransaction(
                    Decimal("-150"),
                    (date.today() - timedelta(days=30)).isoformat(),
                    description="Transfer",
                ),
                core.AccountTransaction(
                    Decimal("-150"),
                    date.today().isoformat(),
                    description="Transfer",
                ),
            ],
        )

        match = core.find_tax_payment_candidate(assessment)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.transaction.date, date.today().isoformat())
        self.assertTrue(match.requires_confirmation)

    def test_explicit_tax_payment_with_high_confidence_can_be_auto_matched(self):
        assessment = core.assess_account_tax(
            Decimal("1904745"),
            [
                core.AccountTransaction(
                    Decimal("-296720"),
                    "2026-11-02",
                    "19:16",
                    "Tax payment",
                    92,
                )
            ],
            ocr_confidence=92,
        )

        match = core.find_tax_payment_candidate(assessment)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertFalse(match.requires_confirmation)


    def test_penalty_screen_text_can_be_read_without_account_balance(self):
        assessment = core._parse_account_screen_text(
            "2026-07-20 14:39 | Fine payment $500",
            allow_zero_balance=True,
            require_balance=False,
        )

        self.assertIsNotNone(assessment)
        assert assessment is not None
        self.assertEqual(assessment.balance, Decimal("0"))
        self.assertFalse(assessment.balance_observed)
        self.assertEqual(assessment.transactions[0].amount, Decimal("-500"))

    def test_money_uses_polish_compact_currency_units(self):
        examples = (
            (Decimal("1000"), "1 tys. $"),
            (Decimal("1234.50"), "1,23 tys. $"),
            (Decimal("1000000"), "1 mln $"),
            (Decimal("1000000000"), "1 mld $"),
            (Decimal("-1000000000"), "-1 mld $"),
            (Decimal("999.50"), "999,5 $"),
            (Decimal("999.999"), "1 tys. $"),
        )
        for amount, expected in examples:
            with self.subTest(amount=amount):
                self.assertEqual(core.money(amount), expected)

    def test_stored_legacy_money_is_reformatted(self):
        self.assertEqual(
            core.format_stored_money("$1,000,000.00"),
            "1 mln $",
        )
        self.assertEqual(core.parse_stored_money("1 tys. $"), Decimal("1000"))
        self.assertEqual(core.parse_stored_money("1,23 tys. $"), Decimal("1230.00"))
        self.assertEqual(core.parse_stored_money("123,45 $"), Decimal("123.45"))
        self.assertEqual(core.parse_stored_money("$1,000"), Decimal("1000"))
        self.assertEqual(
            core.parse_stored_money("1000000.99"),
            Decimal("1000000.99"),
        )

    def test_tax_week_runs_from_sunday_to_next_sunday(self):
        week_start, week_end = core.tax_week_range(date(2026, 11, 11))

        self.assertEqual(week_start, date(2026, 11, 8))
        self.assertEqual(week_end, date(2026, 11, 15))
        self.assertEqual(
            core.tax_week_range(date(2026, 11, 8)),
            (date(2026, 11, 8), date(2026, 11, 15)),
        )

    def test_company_names_ignore_common_descriptors_and_legal_suffixes(self):
        examples = {
            "Up-n-Atom - Firma": "Up-n-Atom",
            "Nevada_Poles_Firma": "Nevada Poles",
            "Starlight Company": "Starlight",
            "Blue Moon sp. z o.o.": "Blue Moon",
            "North Inc.": "North",
            "Company Name": "Company Name",
        }
        for raw_name, expected in examples.items():
            with self.subTest(raw_name=raw_name):
                self.assertEqual(core.normalize_company_name(raw_name), expected)

    def test_saving_gui_settings_preserves_audit_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"token": "old-token", "audit_channel_id": "123456789"}),
                encoding="utf-8",
            )
            with patch.object(core, "CONFIG_PATH", config_path):
                core.save_config({"token": "new-token", "guild_id": "987"})
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["token"], "new-token")
        self.assertEqual(saved["audit_channel_id"], "123456789")

    def test_saving_gui_language_preserves_existing_bot_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "token": "existing-token",
                        "audit_channel_id": "123456789",
                        "language": "pl",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(core, "CONFIG_PATH", config_path):
                core.save_config({"language": "en"})
                saved = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["token"], "existing-token")
        self.assertEqual(saved["audit_channel_id"], "123456789")
        self.assertEqual(saved["language"], "en")

    def test_ocr_profiles_preserve_separate_language_and_layout_choices(self):
        configured = {
            "auto": {"language": "pol", "layout": "auto"},
            "dashboard": {"language": "eng+pol", "layout": "dashboard"},
            "transaction-card": {"language": "eng", "layout": "transaction-card"},
        }

        self.assertEqual(
            core.get_ocr_profile(configured, "dashboard"),
            ("eng+pol", "dashboard"),
        )
        self.assertEqual(
            core.get_ocr_profile(configured, "transaction-card"),
            ("eng", "transaction-card"),
        )

    def test_legacy_ocr_config_migrates_to_profiles_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ocr_language": "pol",
                        "ocr_layout": "dashboard",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(core, "CONFIG_PATH", config_path):
                loaded = core.load_config(use_env=False)
                self.assertEqual(
                    loaded["ocr_profiles"]["dashboard"],
                    {"language": "pol", "layout": "dashboard"},
                )
                loaded["ocr_profiles"]["transaction-card"]["language"] = "eng+pol"
                core.save_config(loaded)
                reloaded = core.load_config(use_env=False)

        self.assertEqual(
            reloaded["ocr_profiles"]["transaction-card"],
            {"language": "eng+pol", "layout": "transaction-card"},
        )

    def test_guild_role_settings_are_persisted_per_server_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                core,
                "COMPANY_DATABASE_PATH",
                Path(directory) / "companies.sqlite3",
            ):
                core.set_guild_role_settings(1, 100, 200)
                core.set_guild_role_settings(2, 300, None)

                self.assertEqual(core.get_guild_role_settings(1), (100, 200))
                self.assertEqual(core.get_guild_role_settings(2), (300, None))

                core.set_guild_role_settings(1, None, None)

                self.assertEqual(core.get_guild_role_settings(1), (None, None))


if __name__ == "__main__":
    unittest.main()
