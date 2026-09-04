"""Semi-automated Solio CSV fetcher.

Standalone script, separate from sklw_lineup.py/draft_lineup.py -- this
one drives a real browser via Playwright instead of the requests-based
FPL API calls the other two use.

Solio's login is Google Sign-In, and Google actively detects and blocks
scripted OAuth logins ("This browser or app may not be secure") -- so
this deliberately does NOT script the login. It follows the same pattern
already proven out in the sibling fpl-model project
(scripts/scrape_solio_pen_shares.py): a one-time manual login in a real,
visible browser window (you log in yourself, same as any normal browser
session).

Uses a PERSISTENT browser profile (a real Chrome user-data directory
saved to disk, not just a cookie/localStorage snapshot) rather than
Playwright's storage_state -- an earlier version used storage_state and
had the login not stick between runs, most likely because Google's own
session validation checks more browser state (IndexedDB, etc.) than
storage_state captures. A persistent profile is much closer to "the same
real browser" across runs and should hold the login far more reliably.
No credentials ever touch disk either way -- only the browser's own
normal profile data, same as any browser you use day to day.

Because the exact CSV export button/page isn't known here (this can't
reach fpl.solioanalytics.com to inspect it), the actual "click export"
step is also manual on first use of each session -- but the button click
ITSELF is automated (matched by its aria-label). Playwright watches for
the resulting download and saves it straight into this folder as
solio.csv, which sklw_lineup.py/draft_lineup.py already auto-detect.

Setup (one-time):
    pip install playwright
    playwright install chromium
    python fetch_solio.py --login
        (a real browser window opens -- log in with Google yourself,
        confirm you can see the actual logged-in Solio page (not still
        mid-redirect), then press Enter in the terminal)

Normal use:
    python fetch_solio.py
        (opens a browser using the saved profile, already logged in --
        the download button is found and clicked automatically; the
        resulting download is captured and saved as solio.csv)

If the saved profile ever stops being logged in (Solio logs you out
server-side), just run --login again -- it reuses and refreshes the same
profile rather than starting over.
"""
from __future__ import annotations

import argparse
from pathlib import Path

PROFILE_DIR = Path(__file__).parent / "browser_profile"
OUT_PATH = Path(__file__).parent / "solio.csv"
SOLIO_URL = "https://fpl.solioanalytics.com/"
DOWNLOAD_BUTTON_LABEL = "Download points projections"

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch_context(p, headless: bool):
    """Launches (or reopens) the persistent Chrome profile at PROFILE_DIR.
    Uses your real installed Chrome (channel="chrome", not Playwright's
    bundled Chromium) with automation fingerprinting disabled -- Google's
    own anti-automation checks otherwise refuse the sign-in even for a
    human doing the actual clicking, same fix the sibling fpl-model
    project already had to make for this exact site."""
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        channel="chrome",
        args=_LAUNCH_ARGS,
    )


def do_login():
    """Opens a real, visible browser window (the persistent profile) for
    you to log in with Google by hand. The profile itself IS the saved
    session -- there's nothing separate to export/save, closing the
    browser after logging in is enough."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        page.goto(SOLIO_URL)
        print("A browser window has opened. Log in with Google yourself, "
              "same as normal, and confirm you can actually see the "
              "logged-in Solio page (not a blank page or still "
              "mid-redirect) before continuing.")
        input("Once you're logged in, press Enter here...")
        context.close()
    print(f"Session saved to the browser profile at {PROFILE_DIR}. "
          f"Future runs (without --login) will reuse it automatically.")


def do_fetch():
    """Opens a browser using the saved profile (already logged in), waits
    for the real 'Download points projections' button (matched by its
    aria-label, a stable accessibility attribute -- not the auto-generated
    id next to it, which looks like it'd change across page loads) to
    appear, and clicks it automatically once found. If the button isn't
    on the landing page, navigate there yourself in the opened window --
    the script keeps waiting and will click it the moment it appears.
    Whatever file downloads gets captured and saved as solio.csv here."""
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        print(f"No saved session at {PROFILE_DIR} -- run with --login first.")
        return

    with sync_playwright() as p:
        context = _launch_context(p, headless=False)
        page = context.new_page()
        page.goto(SOLIO_URL)
        print(f"Browser opened using your saved profile. Waiting for the "
              f"'{DOWNLOAD_BUTTON_LABEL}' button to appear (navigate there "
              f"yourself if it doesn't load automatically) -- it'll be "
              f"clicked automatically the moment it's found, no action "
              f"needed from you otherwise.")
        try:
            button = page.get_by_role("button", name=DOWNLOAD_BUTTON_LABEL)
            button.wait_for(state="visible", timeout=120_000)
            with page.expect_download(timeout=60_000) as download_info:
                button.click()
            download = download_info.value
            download.save_as(str(OUT_PATH))
            print(f"Saved to {OUT_PATH}")
        except Exception as e:
            print(f"Could not find/click the download button automatically "
                  f"({e}). If a Google/Solio login page appeared instead of "
                  f"the projections page, the saved session has expired -- "
                  f"run 'fetch_solio.bat --login' again. Nothing saved.")
        context.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true",
                     help="one-time (or session-expired) manual login, saves the session")
    args = ap.parse_args()

    if args.login:
        do_login()
    else:
        do_fetch()


if __name__ == "__main__":
    main()
