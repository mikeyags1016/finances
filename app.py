import csv
import hashlib
import io
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("FINANCES_DB", BASE_DIR / "data" / "expenses.sqlite3"))
IMPORT_INBOX = BASE_DIR / "data" / "imports" / "inbox"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
ALLOWED_SOURCES = {"Chase", "Discover", "Bank of America"}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-development-only-change-me")

CATEGORY_RULES = [
    ("Savings", ("transfer to sav", "transfer to savings", "savings", "brokerage", "investment")),
    ("Home", ("rent", "mortgage", "property management", "electric", "utility", "water bill", "internet", "comcast", "verizon", "at&t")),
    ("Transportation", ("gas", "fuel", "uber", "lyft", "transit", "parking", "shell", "chevron", "garage", "toll")),
    ("Daily Living", ("grocery", "supermarket", "whole foods", "trader joe", "kroger", "aldi", "safeway", "restaurant", "cafe", "coffee", "doordash", "ubereats", "grubhub", "starbucks", "chipotle", "dining", "food")),
    ("Subscriptions", ("subscription", "netflix", "spotify", "hulu", "disney+", "streaming", "membership", "icloud", "apple.com/bill")),
    ("Loans/Debts", ("loan", "credit card", "card payment", "payment to chase card", "affirm", "klarna", "afterpay", "student debt", "auto loan", "discover e-payment")),
    ("Vacation/Flights", ("airline", "hotel", "airbnb", "flight", "vacation", "travel", "rental car")),
]
CATEGORY_COLORS = {
    "Income": "#20a879",
    "Savings": "#3988d6",
    "Home": "#6d5ce8",
    "Transportation": "#dfbf3b",
    "Daily Living": "#f28c45",
    "Subscriptions": "#aa62ca",
    "Loans/Debts": "#df627a",
    "Vacation/Flights": "#32a6b0",
    "Other": "#b9c1cc",
}

DATE_HEADERS = ("date", "transaction date", "trans date", "posting date", "post date", "posted date")
DESCRIPTION_HEADERS = ("description", "merchant", "details", "transaction", "payee")
AMOUNT_HEADERS = ("amount", "transaction amount", "purchase amount")
DEBIT_HEADERS = ("debit", "withdrawal", "withdrawals", "charge")
INCOME_AMOUNT_HEADERS = ("credit", "deposit", "deposits")
TRANSACTION_DIRECTION_HEADERS = ("debit credit", "transaction direction", "transaction type", "details")


@contextmanager
def get_db():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_db():
    with get_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                account_label TEXT NOT NULL,
                transaction_date TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL CHECK (amount >= 0),
                bucket TEXT NOT NULL,
                flow_type TEXT NOT NULL DEFAULT 'spending'
            )
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(transactions)")
        }
        if "flow_type" not in columns:
            connection.execute(
                "ALTER TABLE transactions ADD COLUMN flow_type TEXT NOT NULL DEFAULT 'spending'"
            )
            legacy_rows = connection.execute(
                """
                SELECT id, source, account_label, transaction_date, description, amount
                FROM transactions
                """
            ).fetchall()
            for row in legacy_rows:
                identity = "\x1f".join(
                    (
                        row["source"],
                        row["account_label"],
                        row["transaction_date"],
                        row["description"],
                        f'{row["amount"]:.2f}',
                        "spending",
                    )
                )
                fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                connection.execute(
                    "UPDATE transactions SET fingerprint = ? WHERE id = ?",
                    (fingerprint, row["id"]),
                )
        for row in connection.execute(
            "SELECT id, description, flow_type FROM transactions"
        ):
            bucket = categorize(row["description"], row["flow_type"])
            connection.execute(
                "UPDATE transactions SET bucket = ? WHERE id = ?",
                (bucket, row["id"]),
            )


def normalize_header(value):
    value = re.sub(r"[^\w\s]", " ", (value or "").strip().lower())
    return re.sub(r"\s+", " ", value)


def find_header(headers, choices):
    normalized_headers = {normalize_header(header): header for header in headers}
    return next((normalized_headers[choice] for choice in choices if choice in normalized_headers), None)


def parse_date(value):
    value = (value or "").strip()
    for date_format in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized transaction date: {value!r}")


def parse_amount(value):
    cleaned = (value or "").strip().replace("$", "").replace(",", "")
    if not cleaned:
        return 0.0
    is_parenthesized = cleaned.startswith("(") and cleaned.endswith(")")
    if is_parenthesized:
        cleaned = cleaned[1:-1]
    amount = float(cleaned)
    return -amount if is_parenthesized else amount


def categorize(description, flow_type="spending"):
    if flow_type == "income":
        return "Income"
    normalized = (description or "").lower()
    for bucket, keywords in CATEGORY_RULES:
        if any(keyword in normalized for keyword in keywords):
            return bucket
    return "Other"


def expense_rows(file_bytes):
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV must be encoded as UTF-8.") from error
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    date_header = find_header(headers, DATE_HEADERS)
    description_header = find_header(headers, DESCRIPTION_HEADERS)
    amount_header = find_header(headers, AMOUNT_HEADERS)
    debit_header = find_header(headers, DEBIT_HEADERS)
    income_amount_header = find_header(headers, INCOME_AMOUNT_HEADERS)
    direction_header = find_header(headers, TRANSACTION_DIRECTION_HEADERS)
    if not date_header or not description_header or not (
        amount_header or debit_header or income_amount_header
    ):
        raise ValueError(
            "CSV needs date and description columns, plus an amount, debit/withdrawal, or credit/deposit column."
        )

    results = []
    for row_number, row in enumerate(reader, start=2):
        if row_number > MAX_ROWS + 1:
            raise ValueError(f"CSV exceeds the {MAX_ROWS:,}-row import limit.")
        description = (row.get(description_header) or "").strip()
        if not description:
            continue

        direction = normalize_header(row.get(direction_header)) if direction_header else ""
        if debit_header or income_amount_header:
            debit_amount = parse_amount(row.get(debit_header)) if debit_header else 0
            income_amount = (
                parse_amount(row.get(income_amount_header)) if income_amount_header else 0
            )
            if debit_amount > 0:
                amount = debit_amount
                flow_type = "spending"
            elif income_amount > 0:
                amount = income_amount
                flow_type = "income"
            else:
                continue
        elif amount_header:
            raw_amount = parse_amount(row.get(amount_header))
            if raw_amount == 0:
                continue
            amount = abs(raw_amount)
            if direction in {"debit", "credit"}:
                flow_type = "spending" if direction == "debit" else "income"
            else:
                flow_type = "spending" if raw_amount < 0 else "income"
        else:
            continue

        results.append(
            {
                "date": parse_date(row.get(date_header)),
                "description": description,
                "amount": amount,
                "bucket": categorize(description, flow_type),
                "flow_type": flow_type,
            }
        )
    return results


def save_transactions(source, account_label, rows):
    inserted = 0
    with get_db() as connection:
        for row in rows:
            identity = "\x1f".join(
                (
                    source,
                    account_label,
                    row["date"],
                    row["description"],
                    f'{row["amount"]:.2f}',
                    row.get("flow_type", "spending"),
                )
            )
            fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO transactions
                    (fingerprint, source, account_label, transaction_date, description, amount, bucket, flow_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint,
                    source,
                    account_label,
                    row["date"],
                    row["description"],
                    row["amount"],
                    row["bucket"],
                    row.get("flow_type", "spending"),
                ),
            )
            inserted += cursor.rowcount
    return inserted


@app.get("/")
def dashboard():
    with get_db() as connection:
        totals = connection.execute(
            """
            SELECT flow_type, bucket, SUM(amount) AS total
            FROM transactions
            GROUP BY flow_type, bucket
            ORDER BY flow_type, total DESC
            """
        ).fetchall()
        transactions = connection.execute(
            """
            SELECT source, account_label, transaction_date, description, amount, bucket, flow_type
            FROM transactions
            ORDER BY transaction_date DESC, id DESC
            LIMIT 100
            """
        ).fetchall()

    spending_totals = [row for row in totals if row["flow_type"] == "spending"]
    income_totals = [row for row in totals if row["flow_type"] == "income"]

    def make_segments(rows):
        total = sum(row["total"] for row in rows)
        segments = []
        offset = 0.0
        for row in rows:
            percent = row["total"] / total * 100 if total else 0
            end = offset + percent
            start_angle = offset * 3.6 - 90
            end_angle = end * 3.6 - 90
            if percent >= 99.999:
                path = "M 100 5 A 95 95 0 1 0 100 195 A 95 95 0 1 0 100 5 Z"
            else:
                start_x = 100 + 95 * math.cos(math.radians(start_angle))
                start_y = 100 + 95 * math.sin(math.radians(start_angle))
                end_x = 100 + 95 * math.cos(math.radians(end_angle))
                end_y = 100 + 95 * math.sin(math.radians(end_angle))
                large_arc = 1 if percent > 50 else 0
                path = (
                    f"M 100 100 L {start_x:.4f} {start_y:.4f} "
                    f"A 95 95 0 {large_arc} 1 {end_x:.4f} {end_y:.4f} Z"
                )
            segments.append(
                {
                    "bucket": row["bucket"],
                    "total": row["total"],
                    "percent": percent,
                    "color": CATEGORY_COLORS.get(row["bucket"], CATEGORY_COLORS["Other"]),
                    "start": offset,
                    "end": end,
                    "path": path,
                }
            )
            offset = end
        return segments, total

    spending_segments, total_spending = make_segments(spending_totals)
    income_segments, total_income = make_segments(income_totals)

    return render_template(
        "index.html",
        spending_segments=spending_segments,
        income_segments=income_segments,
        transactions=transactions,
        total_spending=total_spending,
        total_income=total_income,
        category_colors=CATEGORY_COLORS,
        import_inbox=IMPORT_INBOX.relative_to(BASE_DIR).as_posix(),
    )


@app.post("/import")
def import_csv():
    source = request.form.get("source", "")
    account_label = request.form.get("account_label", "").strip()
    uploaded_file = request.files.get("file")
    if source not in ALLOWED_SOURCES:
        flash("Choose Chase, Discover, or Bank of America.", "error")
        return redirect(url_for("dashboard"))
    if not account_label or len(account_label) > 80:
        flash("Enter an account label (up to 80 characters).", "error")
        return redirect(url_for("dashboard"))
    if not uploaded_file or not secure_filename(uploaded_file.filename or "").lower().endswith(".csv"):
        flash("Choose a CSV export file.", "error")
        return redirect(url_for("dashboard"))

    file_bytes = uploaded_file.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        flash("CSV file exceeds the 5 MB upload limit.", "error")
        return redirect(url_for("dashboard"))

    try:
        rows = expense_rows(file_bytes)
    except (ValueError, csv.Error) as error:
        flash(str(error), "error")
        return redirect(url_for("dashboard"))

    inserted = save_transactions(source, account_label, rows)
    flash(f"Imported {inserted} new transaction(s); skipped {len(rows) - inserted} duplicate(s).", "success")
    return redirect(url_for("dashboard"))


initialize_db()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
