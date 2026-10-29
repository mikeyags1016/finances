# Spendboard

A local-first starter app for importing bank CSV exports, storing transactions in SQLite, and viewing spending and income separately.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. SQLite data is stored in `data/expenses.sqlite3`. Set `FINANCES_DB` to use a different database path.

## Importing transactions

Download a CSV export for the desired account from Chase, Discover, or Bank of America, then save it into the watcher's inbox folder. The dashboard does not provide manual CSV uploads. The app does not collect bank passwords, sign in to bank websites, or download statements automatically. Keep the CSV files private and delete downloaded copies when they are no longer needed.

### Automatically import local CSV exports

The watcher automatically imports CSV files that you save into `data/imports/inbox`. Start it in a second terminal, using the correct bank and account label:

```bash
source .venv/bin/activate
python watch_imports.py --source Chase --account-label "Travel card"
```

Supported `--source` values are `Chase`, `Discover`, and `Bank of America`. The watcher waits until a file stops changing, imports it, and moves successful files to `data/imports/processed`. Files it cannot import are moved to `data/imports/failed`; the terminal reports the reason. This watches a local folder only; you must export/download the CSV from your bank yourself and save it into the inbox. It does not automate bank sign-in or fetch bank data.

The importer recognizes common date, description, and amount headers, including Chase activity exports with `Posting Date`, `Description`, `Amount`, and a `Details` column containing `DEBIT` or `CREDIT`. Debits appear as spending and credits appear as income. It also supports separate `Debit`/`Withdrawal` and `Credit`/`Deposit` amount columns. For a single signed `Amount` column without an explicit direction, negative entries are spending and positive entries are income.

An account label (such as `Travel card`) is stored with each transaction to keep imports from separate accounts identifiable. Re-importing the same account's rows is safe: exact duplicate rows are ignored.

## Categorization

Transactions are categorized into Income, Savings, Home, Transportation, Daily Living, Subscriptions, Loans/Debts, or Vacation/Flights using merchant and description keywords in `CATEGORY_RULES` in `app.py`. Unrecognized spending goes into `Other`. Existing transactions are recategorized automatically when the app starts, so edits to these rules update the dashboard without needing to re-import CSVs.

This is a local development template, not a production-hardened financial application. Before exposing it to a network, add authentication, CSRF protection, secure secret management, and a deployment configuration.
