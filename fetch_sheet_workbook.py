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
first (same path a human takes); that still got refused at the document
step itself. Confirmed the actual distinction live: a genuine CLICK into
the document gets through, but an automated page.goto() straight to its
edit URL does not, even in the exact same logged-in session -- Google's
suspicion here keys off how the navigation happened, not just prior
history. So, same fix category as fetch_solio.py's manual login/click
steps: the home page is opened automatically, but clicking into the
actual document is a manual one-time action per run (the browser stays
open and waits for you), and only the export request afterwards is
automated.

Setup (one-time):
    pip install playwright openpyxl
    playwright install chromium
    python fetch_sheet_workbook.py --login
        (a real browser window opens -- log in with Google yourself,
        confirm you can see an actual logged-in Google page (not a
        sign-in prompt), then press Enter in the terminal)

Normal use:
    python fetch_sheet_workbook.py --sheet-id <SHEET_ID>
        (opens the Sheets home page -- click into the SKLW sheet
        yourself, press Enter once you can see it open, and the rest
        downloads automatically as sheet_workbook.xlsx)

<SHEET_ID> is the long ID in the sheet's URL
(.../spreadsheets/d/<SHEET_ID>/...) -- same sheet fetch_master_list.py
already points at.

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


def do_fetch(sheet_id: str, out_path: Path):
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        print(f"No saved session at {PROFILE_DIR} -- run with --login first.")
        return

    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        try:
            # Confirmed live: even an automated page.goto() straight to
            # the document's own edit URL (not just the export URL) gets
            # a real "You need access" refusal, for an account that DOES
            # have access and can open the exact same document with a
            # real click. Google's suspicion here seems to key off HOW
            # the navigation happened (a genuine click vs. a scripted
            # goto), not just the account or prior page history -- so
            # unlike the export step, this one can't be automated away.
            # Same fix category as fetch_solio.py's manual login/click
            # steps: make the sensitive step manual, automate the rest.
            page.goto("https://docs.google.com/spreadsheets/", wait_until="load")
            print("A browser window has opened on the Google Sheets home "
                  "page. Click into the SKLW sheet yourself here (search "
                  "for it if it's not in Recent) -- a direct automated "
                  "jump to the document gets refused even for an account "
                  "that has access, only a real click gets through.")
            input("Once you can actually see the document open (tabs, cells "
                  "visible), press Enter here...")
            print("Fetching the whole workbook (every tab, colors intact) as xlsx...")
            with page.expect_download(timeout=60_000) as download_info:
                page.goto(export_url)
            download = download_info.value
            download.save_as(str(out_path))
            print(f"Saved to {out_path}")
        except Exception as e:
            print(f"Could not download the workbook ({e}). If a Google "
                  f"sign-in or access-denied page appeared instead of a "
                  f"direct download, the saved session may have expired "
                  f"-- run --login again. Nothing saved.")
        context.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                     help="one-time (or session-expired) manual login, saves the session")
    ap.add_argument("--sheet-id", help="the sheet's ID from its URL "
                                        "(.../spreadsheets/d/<SHEET_ID>/...) -- required "
                                        "for a normal fetch, not for --login")
    ap.add_argument("--out", default=str(OUT_PATH),
                     help=f"output path (default: {OUT_PATH})")
    args = ap.parse_args()

    if args.login:
        do_login()
        return

    if not args.sheet_id:
        print("ERROR: --sheet-id is required (unless using --login)")
        return
    do_fetch(args.sheet_id, Path(args.out))


if __name__ == "__main__":
    main()
