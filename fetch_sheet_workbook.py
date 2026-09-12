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

Hitting the export URL cold (no prior normal navigation in the browser
session) got a real "You need access" refusal even for an account that
DOES have access -- confirmed live. Google's Sheets access checks seem
to want to see the document opened normally first, not jumped straight
to via its export endpoint. So the normal fetch opens Google Sheets'
home page, then the actual document, and only requests the export
afterwards, in that same session -- the same path a human takes,
automated. No UI button-clicking needed beyond that (unlike Solio's
page, which required finding a specific download button).

Setup (one-time):
    pip install playwright openpyxl
    playwright install chromium
    python fetch_sheet_workbook.py --login
        (a real browser window opens -- log in with Google yourself,
        confirm you can see an actual logged-in Google page (not a
        sign-in prompt), then press Enter in the terminal)

Normal use:
    python fetch_sheet_workbook.py --sheet-id <SHEET_ID>
        (downloads the whole workbook, saves as sheet_workbook.xlsx)

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

    edit_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        try:
            # Hitting the export URL cold (no prior normal navigation in
            # this session) got flagged and refused with a real "You
            # need access" page even for an account that DOES have
            # access -- confirmed live. Google's Sheets access checks
            # seem to want to see the doc opened normally first. So:
            # home page -> the actual doc, same path a human takes,
            # THEN request the export from within that same session.
            print("Opening Google Sheets, then the document, before requesting the export...")
            # "networkidle" never actually fires on Google's own apps --
            # they keep background connections (polling, websockets)
            # open indefinitely, so waiting for network idle just hangs
            # until Playwright's timeout, aborting before ever reaching
            # the document. Use "load" (the page's own load event) plus
            # a fixed pause instead -- confirmed live this was the bug.
            page.goto("https://docs.google.com/spreadsheets/", wait_until="load")
            page.wait_for_timeout(3_000)
            page.goto(edit_url, wait_until="load")
            page.wait_for_timeout(5_000)  # let the doc actually finish rendering
            if "You need access" in page.content() or "you need access" in page.title().lower():
                print("Opening the document itself already shows an access-denied "
                      "page -- this account genuinely isn't shared on it yet. "
                      "Nothing to do here until access is granted.")
                context.close()
                return
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
