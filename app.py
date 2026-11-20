import csv
import hashlib
import io
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("FINANCES_DB", BASE_DIR / "data" / "expenses.sqlite3"))
IMPORT_INBOX = BASE_DIR / "data" / "imports" / "inbox"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 10_000
BASE_PAY_MONTHLY = 3472.89
NET_WORTH_START_DATE = date(2026, 8, 3)
NET_WORTH_OPENING_BALANCE = 4666.00
ASSET_BALANCES = (
    ("Chase Savings", 0.0),
    ("HYSA", 0.0),
    ("Checking", 0.0),
)
LIABILITY_BALANCES = (
    ("Student debt", 0.0),
    ("Car debt", 0.0),
)
EQUITY_BALANCES = (
    ("Roth IRA", 0.0),
    ("HSA", 0.0),
    ("Robinhood", 0.0),
    ("401K", 0.0),
)
ALLOWED_SOURCES = {"Chase", "Discover", "Bank of America"}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "local-development-only-change-me")

CATEGORY_RULES = [
    ("Rent/Utilities/Bills", ("greystar", "driveway finance")),
    ("Savings", ("transfer to sav", "transfer to savings", "savings", "brokerage", "investment", "fid bkg")),
    ("Home", ("rent", "mortgage", "property management", "electric", "utility", "water bill", "internet", "comcast", "verizon", "at&t")),
    ("Transportation", ("gas", "fuel", "uber", "lyft", "transit", "parking", "shell", "chevron", "garage", "toll")),
    ("Daily Living", ("grocery", "supermarket", "whole foods", "trader joe", "kroger", "aldi", "safeway", "restaurant", "cafe", "coffee", "doordash", "ubereats", "grubhub", "starbucks", "chipotle", "dining", "food")),
    ("Subscriptions", ("subscription", "netflix", "spotify", "hulu", "disney+", "streaming", "membership", "icloud", "apple.com/bill")),
    ("Loans/Debts", ("loan", "credit card", "card payment", "payment to chase card", "affirm", "klarna", "afterpay", "student debt", "auto loan", "discover e-payment")),
    ("Vacation/Flights", ("airline", "hotel", "airbnb", "flight", "vacation", "travel", "rental car")),
]
CATEGORY_COLORS = {
    "Income": "#20a879",
    "Base Pay": "#16845d",
    "Zelle Payments": "#39a96b",
    "Bonuses/Stipends": "#7bc99c",
    "Rent/Utilities/Bills": "#e38b5b",
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
            "SELECT id, description, amount, flow_type FROM transactions"
        ):
            bucket = categorize(row["description"], row["flow_type"], row["amount"])
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


def categorize(description, flow_type="spending", amount=None):
    normalized = (description or "").lower()
    if flow_type == "income":
        if "zelle" in normalized:
            return "Zelle Payments"
        if any(keyword in normalized for keyword in ("bonus", "stipend")):
            return "Bonuses/Stipends"
        if "base pay" in normalized:
            return "Base Pay"
        if (
            "payroll" in normalized
            and amount is not None
            and round(amount, 2) == BASE_PAY_MONTHLY
        ):
            return "Base Pay"
        if "payroll" in normalized and "ibm" in normalized:
            return "Bonuses/Stipends"
        return "Income"
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
                "bucket": categorize(description, flow_type, amount),
                "flow_type": flow_type,
            }
        )
    return results


def format_money(value):
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def make_balance_bars(balances):
    maximum = max((amount for _, amount in balances), default=0.0)
    return [
        {
            "name": name,
            "amount": amount,
            "width": amount / maximum * 100 if maximum > 0 else 0,
        }
        for name, amount in balances
    ]


def format_axis_money(value):
    rounded = round(value)
    sign = "-" if rounded < 0 else ""
    return f"{sign}${abs(rounded):,}"


def estimated_net_worth_chart(connection, today=None):
    today = today or date.today()
    start = NET_WORTH_START_DATE
    if today < start:
        raise ValueError("Chart date cannot be earlier than the net worth start date.")
    rows = connection.execute(
        """
        SELECT transaction_date,
               SUM(CASE WHEN flow_type = 'income' THEN amount ELSE -amount END) AS net,
               SUM(CASE WHEN flow_type = 'income' AND bucket = 'Base Pay' THEN 1 ELSE 0 END) AS base_pay_count
        FROM transactions
        WHERE transaction_date > ? AND transaction_date <= ?
        GROUP BY transaction_date
        ORDER BY transaction_date
        """,
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    by_date = {row["transaction_date"]: row["net"] for row in rows}
    base_pay_months = {
        row["transaction_date"][:7]
        for row in rows
        if row["base_pay_count"]
    }

    month = start.replace(day=1)
    while month <= today:
        projected_pay_date = month.replace(day=15)
        if (
            projected_pay_date <= today
            and month.strftime("%Y-%m") not in base_pay_months
        ):
            key = projected_pay_date.isoformat()
            by_date[key] = by_date.get(key, 0.0) + BASE_PAY_MONTHLY
        next_month = month.replace(day=28) + timedelta(days=4)
        month = next_month.replace(day=1)

    points = []
    cumulative = NET_WORTH_OPENING_BALANCE
    current = start
    while current <= today:
        cumulative += by_date.get(current.isoformat(), 0.0)
        points.append({"date": current, "value": cumulative})
        current += timedelta(days=1)

    values = [point["value"] for point in points]
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        padding = max(abs(low) * 0.1, 1.0)
        low -= padding
        high += padding
    else:
        span = high - low
        low -= span * 0.08
        high += span * 0.08

    width = 960
    height = 280
    left, right, top, bottom = 72, 16, 16, 36
    plot_width = width - left - right
    plot_height = height - top - bottom
    last_index = len(points) - 1

    def x_at(index):
        if last_index == 0:
            return left + plot_width / 2
        return left + plot_width * index / last_index

    def y_at(value):
        return top + plot_height * (1 - (value - low) / (high - low))

    y_ticks = []
    for step in range(5):
        value = low + (high - low) * step / 4
        y_ticks.append({"y": f"{y_at(value):.2f}", "label": format_axis_money(value)})

    x_ticks = []
    previous_x = None
    for index, point in enumerate(points):
        is_start = index == 0
        is_month_start = point["date"].day == 1
        is_last_day = index == last_index
        if not is_start and not is_month_start and not is_last_day:
            continue
        x = x_at(index)
        if previous_x is not None and x - previous_x < plot_width * 0.08:
            continue
        label = (
            point["date"].strftime("%b %d").replace(" 0", " ")
            if is_start
            else point["date"].strftime("%b")
        )
        if is_last_day and not is_month_start:
            label = f"{label} {point['date'].day}"
        x_ticks.append({"x": f"{x:.2f}", "label": label})
        previous_x = x

    zero_y = f"{y_at(0):.2f}" if low <= 0 <= high else None
    latest_x = x_at(last_index)
    latest_y = y_at(points[-1]["value"])
    return {
        "line": " ".join(
            f"{x_at(index):.2f},{y_at(point['value']):.2f}"
            for index, point in enumerate(points)
        ),
        "latest_point_x": f"{latest_x:.2f}",
        "latest_point_y": f"{latest_y:.2f}",
        "y_ticks": y_ticks,
        "x_ticks": x_ticks,
        "zero_y": zero_y,
        "width": width,
        "height": height,
        "plot_left": left,
        "plot_right": width - right,
        "label_y": height - 10,
        "latest": points[-1]["value"],
        "opening_balance": NET_WORTH_OPENING_BALANCE,
        "base_pay_monthly": BASE_PAY_MONTHLY,
        "latest_label": format_money(points[-1]["value"]),
        "start_label": start.strftime("%b %d, %Y").replace(" 0", " "),
        "end_label": today.strftime("%b %d, %Y").replace(" 0", " "),
    }


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
        net_worth = estimated_net_worth_chart(connection)

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
        asset_bars=make_balance_bars(ASSET_BALANCES),
        liability_bars=make_balance_bars(LIABILITY_BALANCES),
        equity_bars=make_balance_bars(EQUITY_BALANCES),
        transactions=transactions,
        total_spending=total_spending,
        total_income=total_income,
        net_worth=net_worth,
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
