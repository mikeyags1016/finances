import tempfile
import hashlib
import unittest
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
        self.assertIn(b"Show all transactions", response.data)
        self.assertIn(b"/static/app.js", response.data)

    def test_categorizes_transactions_into_requested_buckets(self):
        examples = {
            "Chase rent payment": "Home",
            "Electric utility bill": "Home",
            "Shell gas station": "Transportation",
            "Trader Joe's grocery": "Daily Living",
            "Coffee shop": "Daily Living",
            "Netflix subscription": "Subscriptions",
            "Payment to Chase card": "Loans/Debts",
            "United Airlines flight": "Vacation/Flights",
            "Online Transfer to SAV": "Savings",
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
