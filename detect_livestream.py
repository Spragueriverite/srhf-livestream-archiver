#!/usr/bin/env python3
"""
detect_livestream.py

Runs Sunday morning starting at 9:30 AM PDT (16:30 UTC).
Polls the Facebook Graph API every 2 minutes for an active livestream created today.
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

FACEBOOK_PAGE_URL  = os.environ["FACEBOOK_PAGE_URL"]
GOOGLE_SHEET_ID    = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
META_PAGE_TOKEN    = os.environ["META_PAGE_ACCESS_TOKEN"]

GRAPH_API_VERSION      = "v19.0"
CHECK_INTERVAL_SECONDS = 120   # 2 minutes between checks
MAX_DURATION_SECONDS   = 5400  # 90 minutes total

PAGE_NAME = [p for p in FACEBOOK_PAGE_URL.rstrip("/").split("/") if p][-2]


# ── Graph API ─────────────────────────────────────────────────────────────────

def get_live_video() -> dict | None:
    """
    Check the page's /live_videos endpoint for a stream with LIVE status
    that was created today. Filters out stale ghost entries from past weeks.
    """
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_NAME}/live_videos"
    params = {
        "fields": "id,title,description,status,created_time,permalink_url",
        "access_token": META_PAGE_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        print(f"  Graph API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    videos = resp.json().get("data", [])
    today_utc = datetime.datetime.now(datetime.timezone.utc).date()

    candidates = []
    for v in videos:
        if v.get("status") != "LIVE":
            continue

        # Reject stale entries not created today
        created_raw = v.get("created_time", "")
        if not created_raw:
            continue
        created = datetime.datetime.fromisoformat(
            created_raw.replace("+0000", "+00:00")
        )
        if created.date() != today_utc:
            print(f"  Skipping stale LIVE entry from {created.date()} (not today)")
            continue

        candidates.append((created, v))

    if not candidates:
        return None

    # Pick the most recently created live video in case there are multiple
    candidates.sort(key=lambda x: x[0], reverse=True)
    if len(candidates) > 1:
        print(f"  Found {len(candidates)} LIVE videos today — using most recent")
    return candidates[0][1]


# ── URL verification ─────────────────────────────────────────────────────────

def is_url_accessible(url: str) -> bool:
    """
    Check if a Facebook video URL is actually accessible.
    Ghost/inaccessible videos return a redirect to an error page or a
    non-200 status. We follow redirects and check the final URL — if
    Facebook bounces us to /watch/ or /video/unavailable/ it is a ghost.
    """
    try:
        resp = requests.get(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        final_url = resp.url.lower()
        # Facebook redirects inaccessible videos to these paths
        if any(p in final_url for p in ["unavailable", "login", "/watch", "checkpoint"]):
            print(f"  URL redirected to inaccessible page: {resp.url}")
            return False
        return resp.status_code == 200
    except requests.RequestException as e:
        print(f"  URL check failed: {e}")
        return False


# ── Extraction helpers ────────────────────────────────────────────────────────

BIBLE_BOOKS_PATTERN = (
    r"(?:Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|"
    r"(?:1|2)\s*Samuel|(?:1|2)\s*Kings|(?:1|2)\s*Chronicles|Ezra|Nehemiah|"
    r"Esther|Job|Psalms?|Proverbs|Ecclesiastes|Song\s+of\s+(?:Solomon|Songs)|"
    r"Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|"
    r"Jonah|Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|"
    r"Matthew|Mark|Luke|John|Acts|Romans|(?:1|2)\s*Corinthians|Galatians|"
    r"Ephesians|Philippians|Colossians|(?:1|2)\s*Thessalonians|(?:1|2)\s*Timothy|"
    r"Titus|Philemon|Hebrews|James|(?:1|2)\s*Peter|(?:1|2|3)\s*John|Jude|Revelation)"
)


def extract_scripture(text: str) -> str:
    pattern = BIBLE_BOOKS_PATTERN + r"\s+\d+(?::\d+)?(?:[-\u2013]\d+)?"
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


def extract_title(text: str, scripture_fallback: str) -> str:
    match = re.search(r'"([^"]+)"', text)
    if match:
        return match.group(1).strip()
    return scripture_fallback


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
    # Use a requests session with an explicit timeout so we never hang indefinitely
    import requests as req_module
    session = req_module.Session()
    session.timeout = 30
    gc = gspread.Client(auth=creds, session=session)
    gc.login()
    return gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)


def write_row_2(worksheet, live_url, title, date_str, speaker, scripture):
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

            # Build the live URL — normalise relative paths from the API
            raw_url  = live.get("permalink_url", "")
            if raw_url.startswith("http"):
                live_url = raw_url
            else:
                live_url = f"https://www.facebook.com/{PAGE_NAME}/videos/{video_id}/"

            # Verify the URL is actually accessible before trusting it
            print(f"  Verifying URL is accessible...")
            if not is_url_accessible(live_url):
                print(f"  URL is inaccessible — treating as ghost entry, skipping.")
                live = None
            else:
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
