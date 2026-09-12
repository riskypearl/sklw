"""Fetches the ENTIRE SKLW master Google Sheet workbook (every tab, cell
colors intact) as a local .xlsx file.

Standalone script, separate from fetch_master_list.py -- that one uses
Google's plain unauthenticated CSV export endpoint, which works fine for
a sheet that's actually shared "Anyone with the link can view" but 401s
otherwise (confirmed against this exact sheet), and even when it works,
CSV export throws away all cell background formatting. This script
exists for the two tabs that need more than CSV can give:
  - "Live Scores": to look up which M# tab is your club's fixture this
    week (fixtures are redrawn every week, no fixed schedule).
  - The specific "M#" tab itself: GK vs Strikers is only distinguishable
    by cell background color (2 blue-filled rows = Strikers, the third
    differently-filled row = GK -- there's no text label), which xlsx
    export preserves and CSV export does not.

Same login pattern as fetch_solio.py (see that file for the full
rationale): a one-time manual Google login in a real, visible browser
window using a PERSISTENT profile, reused automatically afterwards.
Since this is the same Google account, running fetch_solio.py --login
first may already leave you logged in here too -- but this script
manages its own copy of the profile so it works standalone either way.

Hitting the export URL cold got a real "You need access" refusal, even
for an account that DOES have access -- confirmed live. Tried fixing it
by having the script itself navigate home page -> document -> export
first (same path a human takes); that still got refused, at BOTH the
document step and (separately, tried again after a genuine click into
the document) the export step itself. Confirmed the actual distinction
live: a genuine CLICK gets through, an automated page.goto() to the same
URL in the same logged-in session does not -- Google's suspicion here
keys off how the navigation happened, not just the account or prior
history. So neither sensitive step can be a plain goto(): the home page
opens automatically, but clicking into the actual document is manual
(the browser waits for you), and the download is driven by actually
clicking through File > Download > Microsoft Excel (.xlsx) -- attempted
automatically, falling back to "finish this click yourself" (still
captures the resulting download either way) if Google's exact menu
wording/structure ever changes. Same fix category as fetch_solio.py's
manual login/download-button steps throughout.

Setup (one-time):
    pip install playwright openpyxl
    playwright install chromium
    python fetch_sheet_workbook.py --login
        (a real browser window opens -- log in with Google yourself,
        confirm you can see an actual logged-in Google page (not a
        sign-in prompt), then press Enter in the terminal)

Normal use:
    python fetch_sheet_workbook.py
        (opens the Sheets home page -- click into the SKLW sheet
        yourself, press Enter once you can see it open, then it clicks
        File > Download > Microsoft Excel automatically, or asks you to
        finish that click yourself if the menu didn't match -- saves as
        sheet_workbook.xlsx either way)

Feed the resulting sheet_workbook.xlsx into resolve_matchup_roles.py.
"""
from __future__ import annotations

import argparse
from pathlib import Path

PROFILE_DIR = Path(__file__).parent / "browser_profile"
OUT_PATH = Path(__file__).parent / "sheet_workbook.xlsx"

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch_context(p, headless: bool):
    """Same persistent-profile, anti-fingerprinting setup as
    fetch_solio.py's _launch_context -- see that file for why (Google's
    own anti-automation checks otherwise refuse the sign-in even for a
    human doing the actual clicking)."""
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        channel="chrome",
        args=_LAUNCH_ARGS,
    )


def do_login():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        page.goto("https://docs.google.com/spreadsheets/")
        print("A browser window has opened. Log in with Google yourself, "
              "same as normal, and confirm you can actually see a "
              "logged-in Google Sheets page (not a sign-in prompt) "
              "before continuing.")
        input("Once you're logged in, press Enter here...")
        context.close()
    print(f"Session saved to the browser profile at {PROFILE_DIR}. "
          f"Future runs (without --login) will reuse it automatically.")


def do_fetch(out_path: Path):
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        print(f"No saved session at {PROFILE_DIR} -- run with --login first.")
        return

    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        try:
            # Confirmed live, twice: an automated page.goto() gets a real
            # "You need access" refusal both for the document's own edit
            # URL AND for the export URL directly -- even right after a
            # genuine click into the same document in the same session.
            # Google's suspicion here keys off HOW a navigation happened
            # (a real click event vs. a scripted goto), not the account
            # or prior page history. So both sensitive steps are driven
            # by actual UI clicks, same fix category as fetch_solio.py's
            # manual login/download-button steps: File > Download >
            # Microsoft Excel (.xlsx), attempted automatically, with a
            # manual fallback (finish the click yourself, still captured)
            # if the exact menu wording/structure has changed.
            page.goto("https://docs.google.com/spreadsheets/", wait_until="load")
            print("A browser window has opened on the Google Sheets home "
                  "page. Click into the SKLW sheet yourself here (search "
                  "for it if it's not in Recent) -- a direct automated "
                  "jump to the document gets refused even for an account "
                  "that has access, only a real click gets through.")
            input("Once you can actually see the document open (tabs, cells "
                  "visible), press Enter here...")
            print("Downloading via File > Download > Microsoft Excel (.xlsx) "
                  "-- even the export LINK gets refused when requested by a "
                  "script, so this clicks through the real menu instead.")
            try:
                with page.expect_download(timeout=90_000) as download_info:
                    page.get_by_text("File", exact=True).first.click(timeout=10_000)
                    page.wait_for_timeout(500)
                    page.get_by_text("Download", exact=True).first.click(timeout=10_000)
                    page.wait_for_timeout(500)
                    page.get_by_text("Microsoft Excel (.xlsx)", exact=True).first.click(timeout=10_000)
                download = download_info.value
            except Exception:
                print("Couldn't find/click through that menu automatically "
                      "(Google's exact wording/structure may differ from "
                      "what this script expects) -- click File > Download > "
                      "Microsoft Excel (.xlsx) yourself now in that browser "
                      "window. Still watching for the resulting download...")
                download = page.wait_for_event("download", timeout=120_000)
            download.save_as(str(out_path))
            print(f"Saved to {out_path}")
        except Exception as e:
            print(f"Could not download the workbook ({e}). Nothing saved.")
        context.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                     help="one-time (or session-expired) manual login, saves the session")
    ap.add_argument("--out", default=str(OUT_PATH),
                     help=f"output path (default: {OUT_PATH})")
    args = ap.parse_args()

    if args.login:
        do_login()
        return

    do_fetch(Path(args.out))


if __name__ == "__main__":
    main()
