#!/usr/bin/env python3
"""
detect_livestream.py

Runs Sunday morning starting at 9:00 AM PST (covers DST shift).
Polls the Facebook Graph API every 2 minutes for an active livestream.
Once detected, writes live info to row 2 of the Google Sheet and exits.
If nothing is found within 90 minutes, exits cleanly.
"""

import os
import re
import sys
import json
import time
import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────

FACEBOOK_PAGE_URL = os.environ["FACEBOOK_PAGE_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
META_PAGE_ACCESS_TOKEN = os.environ["META_PAGE_ACCESS_TOKEN"]

GRAPH_API_VERSION      = "v21.0"
CHECK_INTERVAL_SECONDS = 120   # 2 minutes between checks
MAX_DURATION_SECONDS   = 5400  # 90 minutes total (covers DST shift)

PAGE_NAME = [p for p in FACEBOOK_PAGE_URL.rstrip("/").split("/") if p][-2]


# ── Graph API helpers ─────────────────────────────────────────────────────────

def get_live_video() -> dict | None:
    """
    Check the page's /live_videos endpoint for an active stream created today.
    Filtering by today's date prevents stale ended livestreams with a lingering
    LIVE status from being mistakenly returned.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_NAME}/live_videos"
    params = {
        "fields": "id,title,description,status,created_time,permalink_url",
        "access_token": META_PAGE_ACCESS_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        print(f"  Graph API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    videos = resp.json().get("data", [])
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()
    for v in videos:
        if v.get("status") != "LIVE":
            continue
        # Reject any live video not created today — these are stale ghost entries
        created_raw = v.get("created_time", "")
        if created_raw:
            created = datetime.datetime.fromisoformat(created_raw.replace("+0000", "+00:00"))
            if created.date() != today_utc:
                print(f"  Skipping stale LIVE entry from {created.date()} (not today)")
                continue
        return v
    return None


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_title(text: str, scripture_fallback: str) -> str:
    match = re.search(r'"([^"]+)"', text)
    if match:
        return match.group(1).strip()
    return scripture_fallback


def extract_scripture(text: str) -> str:
    # Strip trailing punctuation from each word so "Acts 10!" still matches
    cleaned = re.sub(r"[!?.,:;]+", " ", text)
    pattern = (
        r"\b"
        r"(?:\d\s+)?"
        r"[A-Z][a-zA-Z]+(?:\s+[A-Za-z]+){0,2}"
        r"\s+"
        r"\d+(?::\d+)?"
        r"(?:[-–]\d+)?"
        r"\b"
    )
    match = re.search(pattern, cleaned)
    return match.group(0).strip() if match else ""


def extract_speaker(text: str) -> str:
    titles = ["Pastor", "Brother", "Elder", "Deacon", "Rev", "Reverend", "Minister"]
    pattern = r"\b(" + "|".join(titles) + r")\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


# ── Sheet helper ──────────────────────────────────────────────────────────────

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


def write_row_2(worksheet, live_url: str, title: str, date_str: str, speaker: str, scripture: str) -> None:
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
    print(f"  Page: {PAGE_NAME}")

    worksheet    = get_worksheet()
    start_time   = time.time()
    check_number = 0

    while True:
        elapsed = time.time() - start_time
        check_number += 1

        print(f"\n[Check #{check_number} — {int(elapsed // 60)}m {int(elapsed % 60)}s elapsed]")

        if elapsed > MAX_DURATION_SECONDS:
            print("90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        print("  Checking for active livestream via Graph API...")
        try:
            live = get_live_video()
        except requests.RequestException as e:
            print(f"  API request failed: {e} — will retry.")
            live = None

        if live:
            video_id    = live["id"]
            description = live.get("description", "")
            today       = datetime.date.today()
            date_str    = f"{today.month}-{today.day}-{today.year}"
            scripture   = extract_scripture(description)
            fb_title    = live.get("title", "")
            clean_title = fb_title if fb_title and fb_title != description else ""
            title       = extract_title(description, scripture or clean_title or "Sunday Service")
            speaker     = extract_speaker(description)
            raw_url     = live.get("permalink_url", "")
            # API sometimes returns a relative path — normalise to a full URL
            if raw_url.startswith("http"):
                live_url = raw_url
            else:
                live_url = f"https://www.facebook.com/{PAGE_NAME}/videos/{video_id}/"

            print(f"  🔴 Livestream detected!")
            print(f"  Title:     {title}")
            print(f"  Speaker:   {speaker or '(not in description yet)'}")
            print(f"  Scripture: {scripture or '(not in description yet)'}")
            print(f"  URL:       {live_url}")

            write_row_2(worksheet, live_url, title, date_str, speaker, scripture)
            print("\n✓ Row 2 populated. Done!")
            sys.exit(0)
        else:
            print("  No active livestream found.")

        remaining = MAX_DURATION_SECONDS - (time.time() - start_time)
        if remaining <= 0:
            print("\n90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        sleep_for = min(CHECK_INTERVAL_SECONDS, remaining)
        print(f"  Waiting {int(sleep_for)}s before next check...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()                print(f"  Skipping stale LIVE entry from {created.date()} (not today)")
                continue
        return v
    return None


# ── Extraction helpers ────────────────────────────────────────────────────────

def extract_title(text: str, scripture_fallback: str) -> str:
    match = re.search(r'"([^"]+)"', text)
    if match:
        return match.group(1).strip()
    return scripture_fallback


def extract_scripture(text: str) -> str:
    # Strip trailing punctuation from each word so "Acts 10!" still matches
    cleaned = re.sub(r"[!?.,:;]+", " ", text)
    pattern = (
        r"\b"
        r"(?:\d\s+)?"
        r"[A-Z][a-zA-Z]+(?:\s+[A-Za-z]+){0,2}"
        r"\s+"
        r"\d+(?::\d+)?"
        r"(?:[-–]\d+)?"
        r"\b"
    )
    match = re.search(pattern, cleaned)
    return match.group(0).strip() if match else ""


def extract_speaker(text: str) -> str:
    titles = ["Pastor", "Brother", "Elder", "Deacon", "Rev", "Reverend", "Minister"]
    pattern = r"\b(" + "|".join(titles) + r")\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?"
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


# ── Sheet helper ──────────────────────────────────────────────────────────────

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


def write_row_2(worksheet, live_url: str, title: str, date_str: str, speaker: str, scripture: str) -> None:
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
    print(f"  Page: {PAGE_NAME}")

    worksheet    = get_worksheet()
    start_time   = time.time()
    check_number = 0

    while True:
        elapsed = time.time() - start_time
        check_number += 1

        print(f"\n[Check #{check_number} — {int(elapsed // 60)}m {int(elapsed % 60)}s elapsed]")

        if elapsed > MAX_DURATION_SECONDS:
            print("90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        print("  Checking for active livestream via Graph API...")
        try:
            live = get_live_video()
        except requests.RequestException as e:
            print(f"  API request failed: {e} — will retry.")
            live = None

        if live:
            video_id    = live["id"]
            description = live.get("description", "")
            today       = datetime.date.today()
            date_str    = f"{today.month}-{today.day}-{today.year}"
            scripture   = extract_scripture(description)
            fb_title    = live.get("title", "")
            clean_title = fb_title if fb_title and fb_title != description else ""
            title       = extract_title(description, scripture or clean_title or "Sunday Service")
            speaker     = extract_speaker(description)
            raw_url     = live.get("permalink_url", "")
            # API sometimes returns a relative path — normalise to a full URL
            if raw_url.startswith("http"):
                live_url = raw_url
            else:
                live_url = f"https://www.facebook.com/{PAGE_NAME}/videos/{video_id}/"

            print(f"  🔴 Livestream detected!")
            print(f"  Title:     {title}")
            print(f"  Speaker:   {speaker or '(not in description yet)'}")
            print(f"  Scripture: {scripture or '(not in description yet)'}")
            print(f"  URL:       {live_url}")

            write_row_2(worksheet, live_url, title, date_str, speaker, scripture)
            print("\n✓ Row 2 populated. Done!")
            sys.exit(0)
        else:
            print("  No active livestream found.")

        remaining = MAX_DURATION_SECONDS - (time.time() - start_time)
        if remaining <= 0:
            print("\n90 minutes elapsed with no livestream detected. Exiting cleanly.")
            sys.exit(0)

        sleep_for = min(CHECK_INTERVAL_SECONDS, remaining)
        print(f"  Waiting {int(sleep_for)}s before next check...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
