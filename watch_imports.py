import argparse
import csv
import sqlite3
import time
from pathlib import Path

import app


def process_csv(path, source, account_label):
    if path.stat().st_size > app.MAX_UPLOAD_BYTES:
        raise ValueError("CSV file exceeds the 5 MB import limit.")
    file_bytes = path.read_bytes()
    if len(file_bytes) > app.MAX_UPLOAD_BYTES:
        raise ValueError("CSV file exceeds the 5 MB import limit.")

    rows = app.expense_rows(file_bytes)
    inserted = app.save_transactions(source, account_label, rows)
    return inserted, len(rows) - inserted


def move_without_overwriting(path, directory):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / path.name
    suffix = 1
    while destination.exists():
        destination = directory / f"{path.stem}-{suffix}{path.suffix}"
        suffix += 1
    return path.replace(destination)


def watch_imports(inbox, source, account_label, poll_seconds):
    processed = inbox.parent / "processed"
    failed = inbox.parent / "failed"
    inbox.mkdir(parents=True, exist_ok=True)
    observed = {}

    print(f"Watching {inbox} for CSV exports. Press Ctrl+C to stop.")
    while True:
        files = sorted(
            path
            for path in inbox.iterdir()
            if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".csv"
        )
        current_paths = set(files)
        observed = {path: stamp for path, stamp in observed.items() if path in current_paths}

        for path in files:
            try:
                stat = path.stat()
            except OSError as error:
                print(f"Cannot inspect {path}: {error}")
                continue
            stamp = (stat.st_size, stat.st_mtime_ns)
            if observed.get(path) != stamp:
                observed[path] = stamp
                continue

            try:
                inserted, duplicates = process_csv(path, source, account_label)
            except (OSError, UnicodeError, ValueError, csv.Error, sqlite3.Error) as error:
                try:
                    destination = move_without_overwriting(path, failed)
                    print(f"Could not import {path.name}: {error}. Moved to {destination}.")
                    observed.pop(path, None)
                except OSError as move_error:
                    print(f"Could not import {path.name}: {error}; could not move it to failed: {move_error}")
                continue

            try:
                destination = move_without_overwriting(path, processed)
            except OSError as error:
                print(f"Imported {path.name}, but could not move it to processed: {error}")
                continue
            print(
                f"Imported {destination.name}: {inserted} new transaction(s), "
                f"{duplicates} duplicate(s) skipped."
            )
            observed.pop(path, None)

        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(
        description="Watch a local folder and automatically import bank CSV exports."
    )
    parser.add_argument("--source", required=True, choices=sorted(app.ALLOWED_SOURCES))
    parser.add_argument("--account-label", required=True, help="Account label to attach to imported transactions")
    parser.add_argument("--inbox", type=Path, default=app.IMPORT_INBOX)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if not args.account_label.strip() or len(args.account_label) > 80:
        parser.error("--account-label must contain 1 to 80 characters.")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be greater than zero.")

    try:
        watch_imports(args.inbox.expanduser().resolve(), args.source, args.account_label.strip(), args.poll_seconds)
    except KeyboardInterrupt:
        print("\nStopped watching for CSV exports.")


if __name__ == "__main__":
    main()
