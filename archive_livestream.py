#!/usr/bin/env python3
"""
archive_livestream.py

Every Sunday at 2:00 PM PST, this script:
  1. Uses the Facebook Graph API to check if a video was posted today.
  2. If not, exits cleanly — nothing to do.
  3. If yes, downloads the video via yt-dlp.
  4. Uploads it to the Internet Archive.
  5. Appends a row to the Google Sheet and clears row 2.

Sheet column order: A=IA URL | B=Title | C=Date (M-D-YYYY) | D=Speaker | E=Scripture
Row 1 = headers, Row 2 = reserved for live stream slot (cleared here after archiving).
"""

import os
import re
import sys
import json
import datetime
import subprocess
import tempfile

import requests
import gspread
import internetarchive
from google.oauth2.service_account import Credentials

# ── Config ────

FACEBOOK_PAGE_URL = os.environ["FACEBOOK_PAGE_URL"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SA_JSON    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
IA_ACCESS_KEY     = os.environ["IA_ACCESS_KEY"]
IA_SECRET_KEY     = os.environ["IA_SECRET_KEY"]
META_PAGE_TOKEN   = os.environ["META_PAGE_ACCESS_TOKEN"]

GRAPH_API_VERSION = "v19.0"
# URL ends with /videos/, so take [-2] to get the page name
PAGE_NAME = [p for p in FACEBOOK_PAGE_URL.rstrip("/").split("/") if p][-2]

# Allow videos posted within the last 36 hours to account for UTC/PST timezone
# differences. A video posted Sunday evening PST is already Monday in UTC.
SAME_DAY_TOLERANCE_HOURS = 36


# ── Graph API ────

def get_latest_video() -> dict | None:
    """Fetch the most recent video posted to the page via the Graph API."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PAGE_NAME}/videos"
    params = {
        "fields": "id,title,description,created_time",
        "limit": 5,
        "access_token": META_PAGE_TOKEN,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        print("  No videos found on page.")
        return None
    return data[0]


# ── Check date ────

def posted_today(video: dict) -> bool:
    """Return True if the video was created within the tolerance window."""
    raw = video.get("created_time", "")
    if not raw:
        return False
    created = datetime.datetime.fromisoformat(raw.replace("+0000", "+00:00"))
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    age = now_utc - created
    return age.total_seconds() <= SAME_DAY_TOLERANCE_HOURS * 3600


# ── Extraction helpers ────

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


# ── Download ────

def download_video(video_id: str, output_path: str) -> bool:
    video_url = f"https://www.facebook.com/{PAGE_NAME}/videos/{video_id}/"
    result = subprocess.run(
        ["yt-dlp", "--format", "best", "-o", output_path, video_url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Download error: {result.stderr}")
    return result.returncode == 0


# ── Internet Archive ────

def make_ia_identifier(title: str, date_str: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", title)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:60]
    return f"{slug}-{date_str}"


def upload_to_ia(filepath: str, title: str, date_str: str, identifier: str) -> str:
    print(f"  Uploading as IA identifier: {identifier}")
    responses = internetarchive.upload(
        identifier,
        files=[filepath],
        metadata={
            "title":     title,
            "date":      date_str,
            "mediatype": "movies",
            "subject":   "sermon; church; livestream; archive",
        },
        access_key=IA_ACCESS_KEY,
        secret_key=IA_SECRET_KEY,
        retries=3,
    )
    for r in responses:
        r.raise_for_status()
    return f"https://archive.org/details/{identifier}"


# ── Google Sheet ────

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


def log_to_sheet(ia_url: str, title: str, date_str: str, speaker: str, scripture: str) -> None:
    """Append archive row and clear row 2."""
    worksheet = get_worksheet()
    worksheet.append_row(
        [ia_url, title, date_str, speaker, scripture],
        value_input_option="USER_ENTERED",
    )
    print("  Archive row appended to Google Sheet.")
    worksheet.update(range_name="A2:E2", values=[["", "", "", "", ""]])
    print("  Row 2 cleared.")


# ── Main ────

def main():
    print("=" * 52)
    print("  SRHF Sunday Livestream Archiver")
    print("=" * 52)
    print(f"  Page: {PAGE_NAME}")

    # 1. Fetch latest video
    print("\n[1/4] Fetching latest video from Facebook Graph API...")
    video = get_latest_video()
    if not video:
        print("ERROR: Could not retrieve video. Exiting.")
        sys.exit(1)

    # 2. Check if posted within tolerance window
    print("\n[2/4] Checking if a video was posted within the tolerance window...")
    if not posted_today(video):
        print(f"  Most recent video is from {video.get('created_time', 'unknown')}, outside tolerance window.")
        print("  No recent livestream — nothing to archive. Exiting cleanly.")
        sys.exit(0)

    # Extract metadata
    video_id    = video["id"]
    description = video.get("description", "")
    today       = datetime.date.today()
    date_str    = f"{today.month}-{today.day}-{today.year}"
    scripture   = extract_scripture(description)
    fb_title    = video.get("title", "")
    clean_title = fb_title if fb_title and fb_title != description else ""
    title       = extract_title(description, scripture or clean_title or "Untitled Sermon")
    speaker     = extract_speaker(description)
    identifier  = make_ia_identifier(title, f"{today.year}-{today.month:02d}-{today.day:02d}")

    print(f"\n  Title:     {title}")
    print(f"  Date:      {date_str}")
    print(f"  Speaker:   {speaker or '(not detected — fill in manually)'}")
    print(f"  Scripture: {scripture or '(not detected — fill in manually)'}")

    # 3. Download
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "video.mp4")

        print("\n[3/4] Downloading video...")
        if not download_video(video_id, output_path):
            print("ERROR: Download failed. Exiting.")
            sys.exit(1)
        print("  Download complete.")

        # 4. Upload to IA
        print("\n[4/4] Uploading to Internet Archive...")
        ia_url = upload_to_ia(output_path, title, date_str, identifier)
        print(f"  Live at: {ia_url}")

    # 5. Update sheet
    print("\n[+] Updating Google Sheet...")
    log_to_sheet(ia_url, title, date_str, speaker, scripture)

    print("\n✓ All done!")
    print(f"  IA URL: {ia_url}")


if __name__ == "__main__":
    main()
