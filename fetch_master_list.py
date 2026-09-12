"""Fetches the SKLW league master list directly from its public Google
Sheet, instead of manually exporting/downloading a CSV by hand each time
the league updates it.

Standalone script, separate from sklw_lineup.py/sklw_matchup.py/
build_clubs_json.py.

A Google Sheet shared as "Anyone with the link can view" supports a
plain, unauthenticated CSV export per-tab:
    https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv&gid=<GID>
-- no login needed, no Playwright/browser automation required (unlike
fetch_solio.py, which needs a real Google login Solio's own site
enforces). <SHEET_ID> is the long ID in the sheet's URL
(.../d/<SHEET_ID>/...); <GID> identifies one specific tab (the number
after '#gid=' in the URL when that tab is open).

If the sheet ISN'T shared publicly, Google serves an HTML sign-in page
instead of CSV (usually still HTTP 200, not an error) -- this script
checks for that and fails clearly rather than silently writing a
garbage "CSV" full of HTML.

The master list may be split across multiple tabs (this league's is,
per session history -- a bye-week team turned out to be on a separate
tab from the main M1-M23 list). Pass --gid more than once to fetch
several tabs and concatenate them into one CSV, keeping only the first
tab's header row.

Usage:
    python fetch_master_list.py --sheet-id 1E8TGqNufhI9YDaWLoet_SIhjYDoZQbKNQR7ivwUIXFQ --gid 813360199
    python fetch_master_list.py --sheet-id <ID> --gid <GID1> --gid <GID2> --out master.csv

Then feed the result into build_clubs_json.py as normal:
    python build_clubs_json.py SKLW_master_list.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import requests


def fetch_tab_csv(sheet_id: str, gid: str) -> str:
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    text = r.text
    stripped = text.lstrip().lower()
    if stripped.startswith("<!doctype") or stripped.startswith("<html"):
        print(f"ERROR: gid {gid} returned an HTML page instead of CSV -- the "
              f"sheet (or this specific tab) probably isn't shared as "
              f"'Anyone with the link can view'. Check the sharing settings "
              f"in Google Sheets, or fall back to File > Download > "
              f"Comma-separated values (.csv) by hand for this tab.")
        sys.exit(1)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", required=True,
                     help="the long ID from the sheet's URL "
                          "(.../spreadsheets/d/<SHEET_ID>/...)")
    ap.add_argument("--gid", action="append", required=True,
                     help="tab ID (the number after '#gid=' in the URL "
                          "when that tab is open). Repeat for multiple "
                          "tabs -- they're concatenated into one CSV, "
                          "keeping only the first tab's header row.")
    ap.add_argument("--out", default="SKLW_master_list.csv",
                     help="output path (default: SKLW_master_list.csv "
                          "in the current folder)")
    args = ap.parse_args()

    all_rows: list[list[str]] = []
    header: list[str] | None = None
    for i, gid in enumerate(args.gid):
        print(f"Fetching gid {gid}...")
        text = fetch_tab_csv(args.sheet_id, gid)
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            print(f"WARNING: gid {gid} returned an empty CSV, skipping.")
            continue
        if header is None:
            header = rows[0]
            all_rows.append(header)
            all_rows.extend(rows[1:])
        else:
            # skip this tab's own header row if it matches the first tab's
            body = rows[1:] if rows[0] == header else rows
            all_rows.extend(body)
        print(f"  {len(rows) - 1} row(s)")

    if not all_rows:
        print("ERROR: nothing fetched -- check the sheet ID/gid(s).")
        sys.exit(1)

    out_path = Path(args.out)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(all_rows)
    print(f"\nWrote {len(all_rows) - 1} total row(s) to {out_path}")
    print(f"Now run: python build_clubs_json.py {out_path}")


if __name__ == "__main__":
    main()
