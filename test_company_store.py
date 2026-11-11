import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import core


class CompanyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "companies.sqlite3"
        self.path_patch = patch.object(core, "COMPANY_DATABASE_PATH", self.database)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_company_status_and_tax_result_are_independent(self):
        core.set_company_active(1, 10, "Up-n-Atom", True)
        core.record_tax_result(
            1, 10, "Up-n-Atom", True, "2026-07-20", "$894,410.00"
        )
        core.set_company_active(1, 10, "Up-n-Atom", False)

        [company] = core.list_companies(1)
        self.assertFalse(company.active)
        self.assertTrue(company.tax_paid)
        self.assertEqual(company.tax_date, "2026-07-20")
        self.assertEqual(company.balance, "$894,410.00")

    def test_new_company_tax_state_is_unverified_and_board_is_persistent(self):
        core.record_tax_result(
            2, 20, "Company B", False, "2026-07-21", "$100.00"
        )
        core.set_board_channel(2, 30)
        core.set_board_message(2, 40)

        core.set_board_channel(2, 31)
        [company] = core.list_companies(2)
        self.assertFalse(company.active)
        self.assertFalse(company.tax_paid)
        self.assertEqual(core.get_board(2), (31, None))

    def test_company_reminders_skip_inactive_paid_and_exempt_companies(self):
        core.set_company_active(3, 30, "Active unpaid", True, channel_id=300)
        core.set_company_active(3, 31, "Inactive", False, channel_id=310)
        core.record_tax_result(3, 32, "Paid", True, "2026-07-20", "$100.00")
        core.set_company_active(3, 32, "Paid", True, channel_id=320)
        core.set_company_active(3, 33, "Relieved", True, channel_id=330)
        core.grant_company_tax_relief(3, 33, "Relieved")

        [company] = core.list_unpaid_active_companies(3)

        self.assertEqual(company.name, "Active unpaid")
        self.assertEqual(company.channel_id, 300)

    def test_company_can_be_removed_without_deleting_penalty_history(self):
        core.set_company_active(6, 60, "Company", True, channel_id=600)
        penalty = core.add_financial_penalty(
            6,
            60,
            "Company",
            "$1 tys. $",
            "Test",
            "2026-08-01",
            123,
            "Admin",
            datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertTrue(core.delete_company(6, 60))
        self.assertFalse(core.delete_company(6, 60))
        self.assertEqual(core.list_companies(6), [])
        self.assertEqual(core.get_financial_penalty(6, penalty.id), penalty)

    def test_penalty_removal_is_scoped_to_guild(self):
        first = core.add_financial_penalty(
            7, 70, "Company A", "$1 tys. $", "Test", "2026-08-01",
            123, "Admin", datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
        second = core.add_financial_penalty(
            8, 80, "Company B", "$2 tys. $", "Test", "2026-08-01",
            456, "Other admin", datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertFalse(core.delete_financial_penalty(8, first.id))
        self.assertTrue(core.delete_financial_penalty(7, first.id))
        self.assertIsNone(core.get_financial_penalty(7, first.id))
        self.assertEqual(core.get_financial_penalty(8, second.id), second)

    def test_active_penalty_list_hides_paid_items_after_24_hours(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
        recent_paid = core.add_financial_penalty(
            9, 90, "Recent", "1 tys. $", "Reason", "2026-11-20",
            1, "Admin", now - core.timedelta(days=2),
        )
        old_paid = core.add_financial_penalty(
            9, 91, "Old", "2 tys. $", "Reason", "2026-11-20",
            1, "Admin", now - core.timedelta(days=3),
        )
        unpaid = core.add_financial_penalty(
            9, 92, "Unpaid", "3 tys. $", "Reason", "2026-11-20",
            1, "Admin", now - core.timedelta(days=30),
        )
        core.mark_financial_penalty_paid(
            9,
            recent_paid.id,
            2,
            "Treasurer",
            now - core.timedelta(hours=23),
        )
        core.mark_financial_penalty_paid(
            9,
            old_paid.id,
            2,
            "Treasurer",
            now - core.timedelta(hours=25),
        )

        active = core.list_active_financial_penalties(9, now=now)

        self.assertEqual(
            {penalty.id for penalty in active},
            {recent_paid.id, unpaid.id},
        )
        self.assertEqual(len(core.list_financial_penalties(9)), 3)

    def test_company_board_keeps_both_message_ids_only_for_same_channel(self):
        core.set_board_channel(14, 140)
        core.set_board_message(14, 141)
        core.set_penalty_board_message(14, 142)

        self.assertEqual(core.get_board(14), (140, 141))
        self.assertEqual(core.get_penalty_board_message(14), (140, 142))

        core.set_board_channel(14, 143)

        self.assertEqual(core.get_board(14), (143, None))
        self.assertEqual(core.get_penalty_board_message(14), (143, None))

    def test_clearing_board_messages_preserves_newer_message_ids(self):
        core.set_board_channel(16, 160)
        core.set_board_message(16, 161)
        core.set_penalty_board_message(16, 162)

        self.assertFalse(core.clear_board_message(16, 999))
        self.assertEqual(core.get_board(16), (160, 161))
        self.assertTrue(core.clear_board_message(16, 161))
        self.assertEqual(core.get_board(16), (160, None))

        self.assertTrue(core.clear_penalty_board_message(16, 162))
        self.assertEqual(core.get_penalty_board_message(16), (160, None))

    def test_penalty_payment_and_overdue_status_are_persistent(self):
        penalty = core.add_financial_penalty(
            10, 100, "Company", "$2 tys. $", "Late", "2026-07-20",
            1, "Admin", datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            core.financial_penalty_status(penalty, date(2026, 7, 20)),
            "pending",
        )
        self.assertEqual(
            core.financial_penalty_status(penalty, date(2026, 7, 21)),
            "overdue",
        )

        paid_at = datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc)
        saved, changed = core.mark_financial_penalty_paid(
            10, penalty.id, 2, "Treasurer", paid_at,
        )
        self.assertTrue(changed)
        if saved is None:
            raise AssertionError("Paid penalty was not returned")
        self.assertEqual(core.financial_penalty_status(saved), "paid")
        self.assertEqual(saved.paid_by_name, "Treasurer")
        self.assertEqual(saved.paid_at, paid_at.isoformat(timespec="seconds"))

        duplicate, changed_again = core.mark_financial_penalty_paid(
            10, penalty.id, 3, "Another admin", paid_at,
        )
        self.assertFalse(changed_again)
        if duplicate is None:
            raise AssertionError("Existing penalty was not returned")
        self.assertEqual(duplicate.paid_by_name, "Treasurer")

    def test_overdue_penalty_notifications_are_tracked_independently(self):
        penalty = core.add_financial_penalty(
            15,
            150,
            "Company",
            "$2 tys. $",
            "Late",
            "2026-07-20",
            1,
            "Admin",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        [overdue] = core.list_unnotified_overdue_penalties(15, date(2026, 7, 21))
        self.assertEqual(overdue.id, penalty.id)

        notified_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(
            core.mark_penalty_overdue_notified(
                15, penalty.id, "company", notified_at
            )
        )
        self.assertFalse(
            core.mark_penalty_overdue_notified(
                15, penalty.id, "company", notified_at
            )
        )
        self.assertTrue(core.mark_penalty_overdue_logged(15, penalty.id, notified_at))
        self.assertFalse(core.mark_penalty_overdue_logged(15, penalty.id, notified_at))

        saved = core.get_financial_penalty(15, penalty.id)
        if saved is None:
            raise AssertionError("Penalty was not returned")
        self.assertEqual(saved.company_overdue_notified_at, notified_at.isoformat(timespec="seconds"))
        self.assertEqual(saved.overdue_logged_at, notified_at.isoformat(timespec="seconds"))
        self.assertEqual(core.list_unnotified_overdue_penalties(15, date(2026, 7, 21)), [saved])

        self.assertTrue(
            core.mark_penalty_overdue_notified(15, penalty.id, "audit", notified_at)
        )
        self.assertEqual(core.list_unnotified_overdue_penalties(15, date(2026, 7, 21)), [])

    def test_due_reminders_are_scheduled_one_day_before_and_persist_per_channel(self):
        penalty = core.add_financial_penalty(
            16,
            160,
            "Company",
            "$2 tys. $",
            "Payment due soon",
            "2026-07-22",
            1,
            "Admin",
            datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        [due] = core.list_unnotified_due_reminders(16, date(2026, 7, 21))
        self.assertEqual(due.id, penalty.id)
        self.assertEqual(core.list_unnotified_due_reminders(16, date(2026, 7, 20)), [])

        notified_at = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(
            core.mark_penalty_due_reminder_notified(
                16, penalty.id, "company", notified_at
            )
        )
        self.assertFalse(
            core.mark_penalty_due_reminder_notified(
                16, penalty.id, "company", notified_at
            )
        )
        [still_due] = core.list_unnotified_due_reminders(16, date(2026, 7, 21))
        self.assertTrue(
            core.mark_penalty_due_reminder_notified(
                16, penalty.id, "audit", notified_at
            )
        )
        self.assertEqual(core.list_unnotified_due_reminders(16, date(2026, 7, 21)), [])
        self.assertIsNone(still_due.due_reminder_audit_notified_at)

    def test_weekly_penalty_summary_counts_payments_and_current_arrears(self):
        paid = core.add_financial_penalty(
            17, 170, "Paid", "2 tys. $", "Reason", "2026-07-20",
            1, "Admin", datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        pending = core.add_financial_penalty(
            17, 171, "Pending", "1 mln $", "Reason", "2026-07-30",
            1, "Admin", datetime(2026, 7, 7, tzinfo=timezone.utc),
        )
        core.add_financial_penalty(
            17, 172, "Overdue", "500 $", "Reason", "2026-07-09",
            1, "Admin", datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        core.mark_financial_penalty_paid(
            17,
            paid.id,
            2,
            "Treasurer",
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),
        )

        summary = core.summarize_financial_penalties(
            17,
            date(2026, 7, 1),
            date(2026, 7, 8),
            as_of=date(2026, 7, 10),
        )

        self.assertEqual((summary.imposed_count, summary.imposed_total), (2, Decimal("1002000")))
        self.assertEqual((summary.paid_count, summary.paid_total), (1, Decimal("2000")))
        self.assertEqual((summary.pending_count, summary.pending_total), (1, Decimal("1000000")))
        self.assertEqual((summary.overdue_count, summary.overdue_total), (1, Decimal("500")))
        self.assertEqual(
            core.summarize_financial_penalties(
                17, date(2026, 7, 1), date(2026, 7, 8), as_of=date(2026, 7, 10)
            ),
            summary,
        )

    def test_weekly_penalty_report_delivery_is_idempotent(self):
        week_start = date(2026, 7, 5)
        sent_at = datetime(2026, 7, 12, tzinfo=timezone.utc)

        self.assertFalse(core.weekly_penalty_report_sent(18, week_start))
        self.assertTrue(core.mark_weekly_penalty_report_sent(18, week_start, sent_at))
        self.assertFalse(core.mark_weekly_penalty_report_sent(18, week_start, sent_at))
        self.assertTrue(core.weekly_penalty_report_sent(18, week_start))

    def test_presentation_resources_and_progress_are_persistent_and_guild_scoped(self):
        self.assertTrue(core.register_presentation_session(19, 190, 191, 192))
        self.assertFalse(core.register_presentation_session(19, 193, 194, 195))
        core.track_presentation_resource(19, 196, "channel")

        session = core.get_presentation_session(19)
        if session is None:
            raise AssertionError("Presentation session was not stored")
        self.assertEqual((session.category_id, session.requester_id, session.stage), (190, 191, 0))
        self.assertEqual(
            core.list_presentation_resources(19),
            [(192, "channel"), (196, "channel"), (190, "category")],
        )
        self.assertTrue(core.is_presentation_category(19, 190))
        self.assertFalse(core.is_presentation_category(20, 190))
        self.assertTrue(core.update_presentation_progress(19, 2, 196, 197))
        updated = core.get_presentation_session(19)
        self.assertIsNotNone(updated)
        self.assertEqual((updated.stage, updated.current_channel_id, updated.current_message_id), (2, 196, 197))

        self.assertTrue(core.untrack_presentation_resource(19, 196))
        self.assertTrue(core.delete_presentation_session(19))
        self.assertIsNone(core.get_presentation_session(19))
        self.assertEqual(core.list_presentation_resources(19), [])

    def test_admin_audit_is_persistent_and_scoped_to_guild(self):
        created_at = datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc)
        core.record_admin_audit(
            11, 42, "Admin", "status_firmy", "nieaktywna → aktywna",
            "Company", created_at,
        )
        core.record_admin_audit(
            11, 44, "Admin 2", "ulga_podatkowa", "Zwolniono", "Other Company",
        )
        core.record_admin_audit(12, 43, "Other", "reset", "Reset")

        entries = core.list_admin_audit(11)
        self.assertEqual(len(entries), 2)
        entry = next(item for item in entries if item.company_name == "Company")

        self.assertEqual(entry.actor_id, 42)
        self.assertEqual(entry.company_name, "Company")
        self.assertEqual(entry.details, "nieaktywna → aktywna")
        self.assertEqual(entry.created_at, created_at.isoformat(timespec="seconds"))

    def test_existing_penalty_schema_is_migrated_without_losing_records(self):
        import sqlite3

        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE financial_penalties (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        category_id INTEGER NOT NULL,
                        company_name TEXT NOT NULL,
                        amount TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        due_date TEXT NOT NULL,
                        imposed_by_id INTEGER NOT NULL,
                        imposed_by_name TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO financial_penalties (
                        guild_id, category_id, company_name, amount, reason,
                        due_date, imposed_by_id, imposed_by_name, created_at
                    ) VALUES (13, 130, 'Legacy', '$100', 'Reason', '2026-08-01',
                              1, 'Admin', '2026-07-20T00:00:00+00:00')
                    """
                )
        finally:
            connection.close()

        [penalty] = core.list_financial_penalties(13)

        self.assertEqual(penalty.company_name, "Legacy")
        self.assertIsNone(penalty.paid_at)
        self.assertIsNone(penalty.paid_by_id)
        self.assertIsNone(penalty.paid_by_name)

    def test_weekly_reset_preserves_activity_channels_and_penalty_register(self):
        core.set_company_active(4, 40, "Company", True, channel_id=400)
        core.record_tax_result(4, 40, "Company", True, "2026-07-20", "$100.00")
        core.grant_company_tax_relief(4, 41, "Relieved")
        penalty = core.add_financial_penalty(
            4,
            40,
            "Company",
            "$250.00",
            "Late payment",
            "2026-08-01",
            123,
            "Admin",
            datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(core.reset_weekly_taxes(4), 2)

        companies = {company.category_id: company for company in core.list_companies(4)}
        self.assertTrue(companies[40].active)
        self.assertEqual(companies[40].channel_id, 400)
        self.assertIsNone(companies[40].tax_paid)
        self.assertFalse(companies[41].tax_exempt)
        [saved_penalty] = core.list_financial_penalties(4)
        self.assertEqual(saved_penalty, penalty)

    def test_existing_company_schema_is_migrated(self):
        import sqlite3

        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE companies (
                        guild_id INTEGER NOT NULL,
                        category_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 0,
                        tax_paid INTEGER,
                        tax_date TEXT,
                        balance TEXT,
                        PRIMARY KEY (guild_id, category_id)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO companies (guild_id, category_id, name) VALUES (5, 50, 'Legacy')"
                )
        finally:
            connection.close()

        [company] = core.list_companies(5)

        self.assertIsNone(company.channel_id)
        self.assertFalse(company.tax_exempt)

    def test_existing_company_board_schema_is_migrated(self):
        import sqlite3

        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE company_boards (
                        guild_id INTEGER PRIMARY KEY,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO company_boards VALUES (15, 150, 151)"
                )
        finally:
            connection.close()

        self.assertEqual(core.get_board(15), (150, 151))
        self.assertEqual(core.get_penalty_board_message(15), (150, None))


if __name__ == "__main__":
    unittest.main()
