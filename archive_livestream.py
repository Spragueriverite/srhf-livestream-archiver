#!/usr/bin/env python3
"""
archive_livestream.py

Every Sunday, this script:
  1. Checks whether the Facebook page posted a livestream today.
  2. If not, exits cleanly — nothing to do.
  3. If yes, downloads the video via yt-dlp.
  4. Uploads it to the Internet Archive.
  5. Appends a row to the Google Sheet:
       A: IA URL  |  B: Title  |  C: Date (M-D-YYYY)  |  D: Speaker  |  E: Scripture Location

Sheet notes:
  - Row 1 is the header row.
  - Row 2 is reserved for live stream links — never touched by this script.
  - New entries are appended after the last populated row.
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

FACEBOOK_PAGE_URL = os.environ["FACEBOOK_PAGE_URL"]            # e.g. https://www.facebook.com/spragueriverhomefellowship/videos/
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]              # Long ID from the Sheet URL
GOOGLE_SA_JSON    = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]  # Full contents of service account .json key
IA_ACCESS_KEY     = os.environ["IA_ACCESS_KEY"]                # archive.org S3 access key
IA_SECRET_KEY     = os.environ["IA_SECRET_KEY"]                # archive.org S3 secret key


# ── Step 1: Fetch video metadata (no download yet) ────────────────────────────

def get_latest_video_info() -> dict | None:
    """Ask yt-dlp for metadata on the most recent video on the page."""
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
        print("yt-dlp metadata fetch failed.")
        print("stderr:", result.stderr)
        return None
    first_line = result.stdout.strip().splitlines()[0]
    return json.loads(first_line)


# ── Step 2: Check if the video is from today ──────────────────────────────────

def uploaded_today(info: dict) -> bool:
    """Return True only if the video's upload_date is today."""
    raw = info.get("upload_date", "")
    if not raw:
        return False
    upload_date = datetime.datetime.strptime(raw, "%Y%m%d").date()
    return upload_date == datetime.date.today()


# ── Step 3: Extract scripture from the post description ──────────────────────

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
    """
    Find the first Bible reference in the description.
    Handles formats like: 'John 3:16', '1 Corinthians 13:4-7', 'Psalm 51', 'Acts 7'.
    """
    pattern = (
        r"\b"
        r"(?:\d\s+)?"                        # optional leading number: 1, 2, 3
        r"[A-Z][a-zA-Z]+(?:\s+[A-Za-z]+)*"  # book name (one or more capitalized words)
        r"\s+"
        r"\d+(?::\d+)?"                      # chapter, with optional :verse
        r"(?:[-–]\d+)?"                      # optional end verse
        r"\b"
    )
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


# ── Step 4: Extract speaker from the post description ────────────────────────

def extract_speaker(text: str) -> str:
    """
    Find a speaker name in the description.
    Matches patterns like:
      - 'Pastor Jeff Johnson'
      - 'Brother Joseph Bergstrom'
      - 'Pastor Steve Davis'
    Adjust the titles list below if your descriptions use other honorifics.
    """
    titles = ["Pastor", "Brother", "Elder", "Deacon", "Rev", "Reverend", "Minister"]
    # Last name is optional: matches 'Pastor Jeff' and 'Pastor Jeff Johnson'
    pattern = r"\b(" + "|".join(titles) + r")\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
    match = re.search(pattern, text)
    return match.group(0).strip() if match else ""


# ── Step 5: Download the video ────────────────────────────────────────────────

def download_video(video_url: str, output_path: str) -> bool:
    """Download the video to output_path. Returns True on success."""
    result = subprocess.run(
        [
            "yt-dlp",
            "--format", "best",
            "-o", output_path,
            video_url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("Download error:", result.stderr)
    return result.returncode == 0


# ── Step 6: Upload to Internet Archive ───────────────────────────────────────

def make_ia_identifier(title: str, date_str: str) -> str:
    """
    Build a unique, IA-safe identifier from the title + date.
    IA identifiers only allow letters, numbers, hyphens, underscores, dots.
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", title)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:60]
    return f"{slug}-{date_str}"


def upload_to_ia(filepath: str, title: str, date_str: str, identifier: str) -> str:
    """Upload to Internet Archive and return the item's public URL."""
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


# ── Step 7: Append archive row and clear row 2 ───────────────────────────────

def log_to_sheet(ia_url: str, title: str, date_str: str, speaker: str, scripture: str) -> None:
    """
    1. Appends the archive row: A=IA URL, B=Title, C=Date, D=Speaker, E=Scripture
    2. Clears row 2 (the live stream slot) so it is blank and ready for next week.

    gspread's append_row always writes after the last populated row, so it never
    accidentally overwrites row 2 when appending.
    """
    sa_info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    worksheet = gc.open_by_key(GOOGLE_SHEET_ID).get_worksheet(0)

    # Append the new archive row
    worksheet.append_row(
        [ia_url, title, date_str, speaker, scripture],
        value_input_option="USER_ENTERED",
    )
    print("  Archive row appended to Google Sheet.")

    # Clear row 2 so it is blank and ready for next week's livestream
    worksheet.update(range_name="A2:E2", values=[["", "", "", "", ""]])
    print("  Row 2 cleared.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 52)
    print("  SRHF Sunday Livestream Archiver")
    print("=" * 52)

    # 1. Fetch metadata
    print("\n[1/4] Fetching latest video metadata from Facebook...")
    info = get_latest_video_info()
    if not info:
        print("ERROR: Could not retrieve video metadata. Exiting.")
        sys.exit(1)

    # 2. Check if there was a livestream today
    print("\n[2/4] Checking if a livestream was posted today...")
    if not uploaded_today(info):
        upload_date = info.get("upload_date", "unknown")
        print(f"  Most recent video was posted on {upload_date}, not today ({datetime.date.today()}).")
        print("  No livestream today — nothing to archive. Exiting cleanly.")
        sys.exit(0)  # Exit 0 = success, nothing to do

    # Extract all needed metadata
    description = info.get("description", "")
    scripture   = extract_scripture(description)
    title       = extract_title(description, scripture or info.get("title", "Untitled Sermon"))
    video_url   = info.get("webpage_url", FACEBOOK_PAGE_URL)
    today       = datetime.date.today()
    date_str    = f"{today.month}-{today.day}-{today.year}"  # e.g. "3-22-2026"
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
        if not download_video(video_url, output_path):
            print("ERROR: Download failed. Exiting.")
            sys.exit(1)
        print("  Download complete.")

        # 4. Upload to IA (must happen inside the tempdir context)
        print("\n[4/4] Uploading to Internet Archive...")
        ia_url = upload_to_ia(output_path, title, date_str, identifier)
        print(f"  Live at: {ia_url}")

    # 5. Log to sheet (after temp dir cleans up — we only need the URL now)
    print("\n[+] Logging to Google Sheet...")
    log_to_sheet(ia_url, title, date_str, speaker, scripture)

    print("\n✓ All done!")
    print(f"  IA URL: {ia_url}")


if __name__ == "__main__":
    main()
