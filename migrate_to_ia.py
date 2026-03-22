#!/usr/bin/env python3
"""
migrate_to_ia.py

Runs once daily. Finds up to 6 rows in the Google Sheet where column A
is still a Facebook URL, downloads each video, uploads to Internet Archive,
swaps column A to the IA URL, and moves the original Facebook URL to column H.

When no Facebook URLs remain, exits cleanly — safe to leave scheduled forever.
"""

import os
import re
import sys
import json
import datetime
import subprocess
import tempfile

import gspread
import internetarchive
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON  = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
IA_ACCESS_KEY   = os.environ["IA_ACCESS_KEY"]
IA_SECRET_KEY   = os.environ["IA_SECRET_KEY"]

BATCH_SIZE = 6  # Max videos to process per day


# ── Sheet helpers ─────────────────────────────────────────────────────────────

def get_worksheet():
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)


def find_facebook_rows(worksheet) -> list[dict]:
    """
    Return up to BATCH_SIZE rows where column A contains a Facebook URL.
    Skips row 1 (headers) and row 2 (live stream slot).
    Each result is a dict with: row_number, fb_url, title, date, speaker, scripture
    """
    all_rows = worksheet.get_all_values()
    facebook_rows = []

    for i, row in enumerate(all_rows):
        row_number = i + 1
        if row_number <= 2:
            continue  # Skip header and live row

        # Pad short rows
        while len(row) < 6:
            row.append("")

        link = row[0].strip()
        if "facebook.com" in link:
            facebook_rows.append({
                "row_number": row_number,
                "fb_url":     link,
                "title":      row[1].strip(),
                "date":       row[2].strip(),
                "speaker":    row[3].strip(),
                "scripture":  row[4].strip(),
            })

        if len(facebook_rows) >= BATCH_SIZE:
            break

    return facebook_rows


# ── Download ──────────────────────────────────────────────────────────────────

def download_video(fb_url: str, output_path: str) -> bool:
    result = subprocess.run(
        ["yt-dlp", "--format", "best", "-o", output_path, fb_url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Download error: {result.stderr}")
    return result.returncode == 0


# ── Internet Archive upload ───────────────────────────────────────────────────

def make_ia_identifier(title: str, date_str: str) -> str:
    """
    Build a URL-safe IA identifier from title + date.
    date_str is expected in M-D-YYYY format (e.g. '3-1-2026').
    Converts to YYYY-MM-DD for the identifier.
    """
    # Normalise date to YYYY-MM-DD
    try:
        parts = date_str.split("-")
        normalised = f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"
    except (IndexError, ValueError):
        normalised = date_str  # Fall back to whatever we have

    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", title)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:60]
    return f"{slug}-{normalised}"


def upload_to_ia(filepath: str, title: str, date_str: str, identifier: str) -> str:
    print(f"  Uploading as IA identifier: {identifier}")
    responses = internetarchive.upload(
        identifier,
        files=[filepath],
        metadata={
            "title":     title,
            "date":      date_str,
            "mediatype": "movies",
            "subject":   "sermon; church; archive",
        },
        access_key=IA_ACCESS_KEY,
        secret_key=IA_SECRET_KEY,
        retries=3,
    )
    for r in responses:
        r.raise_for_status()
    return f"https://archive.org/details/{identifier}"


# ── Sheet update ──────────────────────────────────────────────────────────────

def update_row(worksheet, row_number: int, ia_url: str, fb_url: str) -> None:
    """Swap column A to IA URL and store the old Facebook URL in column H."""
    worksheet.update(
        range_name=f"A{row_number}",
        values=[[ia_url]],
        value_input_option="USER_ENTERED",
    )
    worksheet.update(
        range_name=f"H{row_number}",
        values=[[fb_url]],
        value_input_option="USER_ENTERED",
    )
    print(f"  Row {row_number} updated.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  SRHF Archive Migration")
    print("=" * 52)

    worksheet = get_worksheet()

    print("\n[1] Scanning sheet for Facebook URLs...")
    rows = find_facebook_rows(worksheet)

    if not rows:
        print("  No Facebook URLs remaining — migration complete. Exiting cleanly.")
        sys.exit(0)

    print(f"  Found {len(rows)} Facebook URL(s) to migrate today.")

    success_count = 0
    fail_count    = 0

    for idx, row in enumerate(rows, start=1):
        print(f"\n[Video {idx}/{len(rows)}] {row['title']} ({row['date']})")
        print(f"  Facebook URL: {row['fb_url']}")

        identifier = make_ia_identifier(
            row["title"] or "Untitled Sermon",
            row["date"]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = f"{tmpdir}/video.mp4"

            print("  Downloading...")
            if not download_video(row["fb_url"], output_path):
                print("  ERROR: Download failed — skipping this video.")
                fail_count += 1
                continue

            print("  Uploading to Internet Archive...")
            try:
                ia_url = upload_to_ia(
                    output_path,
                    row["title"] or "Untitled Sermon",
                    row["date"],
                    identifier,
                )
                print(f"  IA URL: {ia_url}")
            except Exception as e:
                print(f"  ERROR: IA upload failed — {e} — skipping this video.")
                fail_count += 1
                continue

        print("  Updating sheet...")
        update_row(worksheet, row["row_number"], ia_url, row["fb_url"])
        success_count += 1

    print(f"\n{'=' * 52}")
    print(f"  Done. {success_count} migrated, {fail_count} failed.")
    if fail_count > 0:
        print("  Failed videos will be retried tomorrow.")
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
