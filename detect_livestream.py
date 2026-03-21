#!/usr/bin/env python3
"""
detect_livestream.py

Runs Sunday morning from 10:00–10:20 AM PST.
Checks the Facebook page every 2 minutes for an active livestream.
As soon as one is detected, it writes the live info to row 2 of the sheet:
  A: Facebook Live URL  |  B: Title  |  C: Date  |  D: Speaker  |  E: Scripture

Exits immediately after writing. If no stream is found by 10:20 AM, exits cleanly.
Row 2 is left blank until a stream is found — the 2 PM archive script clears it afterwards.
"""

import os
import re
import sys
import json
import time
import datetime
import subprocess

import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

FACEBOOK_PAGE_URL = os.environ["FACEBOOK_PAGE_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

CHECK_INTERVAL_SECONDS = 120   # 2 minutes between checks
MAX_DURATION_SECONDS   = 5400  # Stop after 90 minutes total (covers DST shift)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_sheet_worksheet():
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


def fetch_latest_video_info() -> dict | None:
    """Use yt-dlp to get metadata for the most recent/live video on the page."""
    result = subprocess.run(
        [
            "yt-dlp",
            "--dump-json",
            "--playlist-items", "1",
            "--no-download",
            FACEBOOK_PAGE_URL,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    first_line = result.stdout.strip().splitlines()[0]
    try:
        return json.loads(first_line)
    except json.JSONDecodeError:
        return None


def is_live(info: dict) -> bool:
    """Return True if yt-dlp reports this video as currently live."""
    return bool(info.get("is_live") or info.get("live_status") == "is_live")


def extract_title(text: str, scripture_fallback: str) -> str:
    """
    Pull the sermon title from the description — expected to be in double quotes.
    e.g. 'Pastor Jeff will be teaching "The Way of the Cross" out of Acts 9'
    If no quoted title is found, fall back to the scripture reference.
    """
    match = re.search(r'"([^"]+)"', text)
    if match:
        return match.group(1).strip()
    return scripture_fallback


def extract_scripture(text: str) -> str:
    pattern = (
        r"\b"
        r"(?:\d\s+)?"
        r"[A-Z][a-zA-Z]+(?:\s+[A-Za-z]+)*"
        r"\s+"
        r"\d+(?::\d+)?"
        r"(?:[-–]\d+)?"
        r"\b"
    )
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


def extract_speaker(text: str) -> str:
    titles = ["Pastor", "Brother", "Elder", "Deacon", "Rev", "Reverend", "Minister"]
    pattern = r"\b(" + "|".join(titles) + r")\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


def write_row_2(worksheet, live_url: str, title: str, date_str: str, speaker: str, scripture: str) -> None:
    """Write live info directly into row 2 (never append — always overwrite row 2)."""
    worksheet.update(
        range_name="A2:E2",
        values=[[live_url, title, date_str, speaker, scripture]],
        value_input_option="USER_ENTERED",
    )
    print("  Row 2 updated with live stream info.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  SRHF Livestream Detector")
    print("=" * 52)

    worksheet    = get_sheet_worksheet()
    start_time   = time.time()
    check_number = 0

    while True:
        elapsed = time.time() - start_time
        check_number += 1

        print(f"\n[Check #{check_number} — {int(elapsed // 60)}m {int(elapsed % 60)}s elapsed]")

        if elapsed > MAX_DURATION_SECONDS:
            print("90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        print("  Fetching page metadata...")
        info = fetch_latest_video_info()

        if info is None:
            print("  Could not fetch metadata. Will retry.")
        elif is_live(info):
            description = info.get("description", "")
            scripture   = extract_scripture(description)
            title       = extract_title(description, scripture or info.get("title", "Sunday Service"))
            live_url    = info.get("webpage_url", FACEBOOK_PAGE_URL)
            today       = datetime.date.today()
            date_str    = f"{today.month}-{today.day}-{today.year}"
            speaker     = extract_speaker(description)

            print(f"  🔴 Livestream detected!")
            print(f"  Title:     {title}")
            print(f"  Speaker:   {speaker or '(not in description yet)'}")
            print(f"  Scripture: {scripture or '(not in description yet)'}")
            print(f"  URL:       {live_url}")

            write_row_2(worksheet, live_url, title, date_str, speaker, scripture)
            print("\n✓ Row 2 populated. Done!")
            sys.exit(0)
        else:
            upload_date = info.get("upload_date", "unknown")
            print(f"  No active livestream. Most recent video is from {upload_date}.")

        # Wait before next check (unless we're about to exceed the time limit)
        remaining = MAX_DURATION_SECONDS - (time.time() - start_time)
        if remaining <= 0:
            print("\n90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        sleep_for = min(CHECK_INTERVAL_SECONDS, remaining)
        print(f"  Waiting {int(sleep_for)}s before next check...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
