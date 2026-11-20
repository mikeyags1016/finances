import tempfile
import hashlib
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import app
from watch_imports import process_csv


class ProcessCsvTests(unittest.TestCase):
    def test_parses_chase_debits_as_spending_and_credits_as_income(self):
        csv_bytes = (
            b"Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
            b'DEBIT,10/28/2026,"Payment to card",-50.00,LOAN_PMT,100.00,\n'
            b'CREDIT,10/27/2026,"Payroll",300.00,ACH_CREDIT,150.00,\n'
        )

        rows = app.expense_rows(csv_bytes)

        self.assertEqual(
            rows,
            [
                {
                    "date": "2026-10-28",
                    "description": "Payment to card",
                    "amount": 50.0,
                    "bucket": "Other",
                    "flow_type": "spending",
                },
                {
                    "date": "2026-10-27",
                    "description": "Payroll",
                    "amount": 300.0,
                    "bucket": "Income",
                    "flow_type": "income",
                },
            ],
        )

    def test_dashboard_shows_separate_spending_and_income_charts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                app.save_transactions(
                    "Chase",
                    "Checking",
                    [
                        {
                            "date": "2026-10-28",
                            "description": "Coffee shop",
                            "amount": 5.0,
                            "bucket": "Daily Living",
                            "flow_type": "spending",
                        },
                        {
                            "date": "2026-10-28",
                            "description": "Payroll",
                            "amount": 100.0,
                            "bucket": "Income",
                            "flow_type": "income",
                        },
                        {
                            "date": "2026-10-27",
                            "description": "IBM Payroll",
                            "amount": 3472.89,
                            "bucket": app.categorize("IBM Payroll", "income", 3472.89),
                            "flow_type": "income",
                        },
                        {
                            "date": "2026-10-26",
                            "description": "Zelle payment from friend",
                            "amount": 25.0,
                            "bucket": app.categorize(
                                "Zelle payment from friend", "income", 25.0
                            ),
                            "flow_type": "income",
                        },
                        {
                            "date": "2026-10-25",
                            "description": "IBM Payroll",
                            "amount": 5628.0,
                            "bucket": app.categorize("IBM Payroll", "income", 5628.0),
                            "flow_type": "income",
                        },
                    ],
                )
                response = app.app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Spending by bucket", response.data)
        self.assertIn(b"Income by bucket", response.data)
        self.assertIn(b"$5.00", response.data)
        self.assertIn(b"$100.00", response.data)
        self.assertIn(b"<th>Type</th><th>Bucket</th>", response.data)
        self.assertIn(b"Bucket: Daily Living", response.data)
        self.assertIn(b"--bucket-color: #f28c45", response.data)
        self.assertIn(b'class="pie-slice"', response.data)
        self.assertIn(b'data-flow-type="spending"', response.data)
        self.assertIn(b'data-flow-type="income"', response.data)
        self.assertIn(b'data-bucket="Daily Living"', response.data)
        self.assertIn(b'data-bucket="Base Pay"', response.data)
        self.assertIn(b'data-bucket="Zelle Payments"', response.data)
        self.assertIn(b'data-bucket="Bonuses/Stipends"', response.data)
        self.assertIn(b'fill="#16845d"', response.data)
        self.assertIn(b'fill="#39a96b"', response.data)
        self.assertIn(b'fill="#7bc99c"', response.data)
        self.assertIn(b"Show all transactions", response.data)
        self.assertIn(b"/static/app.js", response.data)
        self.assertLess(response.data.index(b"Estimated net worth"), response.data.index(b"Spending by bucket"))
        self.assertIn(b'class="net-worth-line"', response.data)
        self.assertIn(b'class="net-worth-point"', response.data)

    def test_estimated_net_worth_starts_with_post_move_savings_balance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                app.save_transactions(
                    "Chase",
                    "Checking",
                    [
                        {
                            "date": "2025-12-31",
                            "description": "Prior year payroll",
                            "amount": 500.0,
                            "bucket": "Income",
                            "flow_type": "income",
                        },
                        {
                            "date": "2026-08-03",
                            "description": "Moving expenses already reflected in opening balance",
                            "amount": 2000.0,
                            "bucket": "Other",
                            "flow_type": "spending",
                        },
                        {
                            "date": "2026-08-04",
                            "description": "Coffee shop",
                            "amount": 40.0,
                            "bucket": "Daily Living",
                            "flow_type": "spending",
                        },
                        {
                            "date": "2026-09-02",
                            "description": "Payroll",
                            "amount": 100.0,
                            "bucket": "Income",
                            "flow_type": "income",
                        },
                    ],
                )
                with app.get_db() as connection:
                    chart = app.estimated_net_worth_chart(connection, today=date(2026, 9, 16))

        self.assertEqual(chart["opening_balance"], 4666.00)
        self.assertAlmostEqual(chart["latest"], 11671.78, places=2)
        self.assertEqual(chart["latest_label"], "$11,671.78")
        self.assertEqual(chart["start_label"], "Aug 3, 2026")
        self.assertEqual(chart["end_label"], "Sep 16, 2026")
        self.assertIn("Aug 3", [tick["label"] for tick in chart["x_ticks"]])
        self.assertTrue(chart["line"].startswith("72.00,"))

    def test_projected_base_pay_is_not_added_when_month_has_imported_base_pay(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                app.save_transactions(
                    "Chase",
                    "Checking",
                    [
                        {
                            "date": "2026-03-15",
                            "description": "IBM Payroll",
                            "amount": app.BASE_PAY_MONTHLY,
                            "bucket": app.categorize(
                                "IBM Payroll", "income", app.BASE_PAY_MONTHLY
                            ),
                            "flow_type": "income",
                        },
                    ],
                )
                with app.get_db() as connection:
                    chart = app.estimated_net_worth_chart(
                        connection, today=date(2026, 9, 16)
                    )

        self.assertEqual(
            chart["latest"],
            app.NET_WORTH_OPENING_BALANCE + app.BASE_PAY_MONTHLY * 2,
        )

    def test_categorizes_income_types_and_base_pay_amount(self):
        self.assertEqual(
            app.categorize("IBM Payroll", "income", 3472.89), "Base Pay"
        )
        self.assertEqual(
            app.categorize("IBM Payroll", "income", 5628.00), "Bonuses/Stipends"
        )
        self.assertEqual(
            app.categorize("Zelle payment from friend", "income", 100.00),
            "Zelle Payments",
        )
        self.assertEqual(
            app.categorize("Annual bonus", "income", 500.00), "Bonuses/Stipends"
        )
        self.assertEqual(app.categorize("Payroll deposit", "income", 100.00), "Income")

    def test_categorizes_transactions_into_requested_buckets(self):
        examples = {
            "GREYSTAR services rent": "Rent/Utilities/Bills",
            "DRIVEWAY Finance payment": "Rent/Utilities/Bills",
            "Chase rent payment": "Home",
            "Electric utility bill": "Home",
            "Shell gas station": "Transportation",
            "Trader Joe's grocery": "Daily Living",
            "Coffee shop": "Daily Living",
            "Netflix subscription": "Subscriptions",
            "Payment to Chase card": "Loans/Debts",
            "United Airlines flight": "Vacation/Flights",
            "Online Transfer to SAV": "Savings",
            "Fid Bkg transfer": "Savings",
        }

        for description, expected_bucket in examples.items():
            with self.subTest(description=description):
                self.assertEqual(app.categorize(description), expected_bucket)
        self.assertEqual(app.categorize("Payroll deposit", "income"), "Income")
        self.assertEqual(app.categorize("Unknown merchant"), "Other")

    def test_migrates_existing_transactions_without_reimporting_them(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "expenses.sqlite3"
            legacy_identity = "\x1f".join(
                ("Chase", "Checking", "2026-10-28", "Coffee shop", "5.00")
            )
            legacy_fingerprint = hashlib.sha256(legacy_identity.encode("utf-8")).hexdigest()
            with app.sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE transactions (
                        id INTEGER PRIMARY KEY,
                        fingerprint TEXT NOT NULL UNIQUE,
                        source TEXT NOT NULL,
                        account_label TEXT NOT NULL,
                        transaction_date TEXT NOT NULL,
                        description TEXT NOT NULL,
                        amount REAL NOT NULL CHECK (amount >= 0),
                        bucket TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO transactions
                        (fingerprint, source, account_label, transaction_date, description, amount, bucket)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy_fingerprint,
                        "Chase",
                        "Checking",
                        "2026-10-28",
                        "Coffee shop",
                        5.0,
                        "Dining",
                    ),
                )

            with patch.object(app, "DATABASE", database):
                app.initialize_db()
                with app.get_db() as connection:
                    transaction = connection.execute(
                        "SELECT flow_type, bucket FROM transactions"
                    ).fetchone()
                self.assertEqual(transaction["flow_type"], "spending")
                self.assertEqual(transaction["bucket"], "Daily Living")
                self.assertEqual(
                    app.save_transactions(
                        "Chase",
                        "Checking",
                        [
                            {
                                "date": "2026-10-28",
                                "description": "Coffee shop",
                                "amount": 5.0,
                                "bucket": "Dining",
                                "flow_type": "spending",
                            }
                        ],
                    ),
                    0,
                )

    def test_dashboard_has_no_manual_csv_upload_form(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                response = app.app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'form action="/import"', response.data)
        self.assertIn(b"data/imports/inbox", response.data)

    def test_dashboard_shows_balance_charts_with_placeholders(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                response = app.app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b">Assets</h2>", response.data)
        self.assertIn(b"Liabilities &amp; equity", response.data)
        self.assertIn(b"Student debt", response.data)
        self.assertIn(b"Car debt", response.data)
        self.assertIn(b"HSA", response.data)
        self.assertIn(b"Chase Savings", response.data)
        self.assertIn(b"HYSA", response.data)
        self.assertIn(b"Checking", response.data)
        self.assertIn(b"Roth IRA", response.data)
        self.assertIn(b"Robinhood", response.data)
        self.assertIn(b"401K", response.data)
        self.assertIn(b"Account balances are placeholders", response.data)
        self.assertIn(b'class="balance-fill" style="width: 0%', response.data)
        self.assertLess(
            response.data.index(b"Income by bucket"),
            response.data.index(b">Assets</h2>"),
        )
        self.assertLess(
            response.data.index(b">Assets</h2>"),
            response.data.index(b">Transactions</h2>"),
        )

    def test_balance_bars_scale_to_largest_account(self):
        bars = app.make_balance_bars((("Savings", 100.0), ("Checking", 50.0)))

        self.assertEqual([bar["width"] for bar in bars], [100.0, 50.0])

    def test_imports_rows_and_skips_duplicate_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "finances-watch-import-test.csv"
            csv_path.write_text(
                "Date,Description,Amount\n01/02/2025,Example Market,-12.50\n",
                encoding="utf-8",
            )
            with patch.object(app, "DATABASE", Path(temporary_directory) / "expenses.sqlite3"):
                app.initialize_db()
                self.assertEqual(process_csv(csv_path, "Chase", "Test account"), (1, 0))
                self.assertEqual(process_csv(csv_path, "Chase", "Test account"), (0, 1))

    def test_rejects_csv_over_import_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            csv_path = Path(temporary_directory) / "large.csv"
            csv_path.write_text("Date,Description,Amount\n", encoding="utf-8")
            with patch.object(app, "MAX_UPLOAD_BYTES", 5):
                with self.assertRaisesRegex(ValueError, "5 MB import limit"):
                    process_csv(csv_path, "Chase", "Test account")


if __name__ == "__main__":
    unittest.main()
