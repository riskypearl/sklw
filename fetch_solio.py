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
session), after which Playwright saves that session's cookies to a local
file and reuses it on every future run -- no credentials ever touch disk.

Because the exact CSV export button/page isn't known here (this can't
reach fpl.solioanalytics.com to inspect it), the actual "click export"
step is also manual -- a real browser window opens, already logged in
via the saved session, and you click whatever export/download button
gets you the CSV same as you do today. Playwright watches for the
resulting download and saves it straight into this folder as solio.csv,
which sklw_lineup.py/draft_lineup.py already auto-detect.

Setup (one-time):
    pip install playwright
    playwright install chromium
    python fetch_solio.py --login
        (a real browser window opens -- log in with Google yourself,
        navigate wherever you'd normally go, then press Enter in the
        terminal once you're logged in and can see the projections page)

Normal use:
    python fetch_solio.py
        (opens a browser using the saved session, already logged in --
        click through to wherever the CSV export button is and click it,
        same as you do manually today; the download is captured
        automatically and saved as solio.csv in this folder)

If the saved session expires (Solio logs you out), just run --login
again.
"""
from __future__ import annotations

import argparse
from pathlib import Path

AUTH_STATE_PATH = Path(__file__).parent / "solio_auth_state.json"
OUT_PATH = Path(__file__).parent / "solio.csv"
SOLIO_URL = "https://fpl.solioanalytics.com/"


def do_login():
    """Opens a real, visible browser window for you to log in with Google
    by hand, then saves that session to disk for future runs to reuse.

    Uses your real installed Chrome (channel="chrome", not Playwright's
    bundled Chromium) with automation fingerprinting disabled -- Google's
    own anti-automation checks otherwise refuse the sign-in even for a
    human doing the actual clicking, same fix the sibling fpl-model
    project already had to make for this exact site."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(SOLIO_URL)
        print("A browser window has opened. Log in with Google yourself, "
              "same as normal, and navigate to wherever you'd normally go "
              "to export the projections CSV.")
        input("Once you're logged in, press Enter here to save the session...")
        context.storage_state(path=str(AUTH_STATE_PATH))
        browser.close()
    print(f"Session saved to {AUTH_STATE_PATH}. Future runs (without "
          f"--login) will reuse it automatically.")


def do_fetch():
    """Opens a browser using the saved session (already logged in) and
    waits for you to click through to the CSV export/download button
    yourself -- the exact page/button isn't known here, so this part
    stays manual. Whatever file downloads gets captured and saved as
    solio.csv in this folder automatically."""
    from playwright.sync_api import sync_playwright

    if not AUTH_STATE_PATH.exists():
        print(f"No saved session at {AUTH_STATE_PATH} -- run with --login first.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(storage_state=str(AUTH_STATE_PATH))
        page = context.new_page()
        page.goto(SOLIO_URL)
        print("Browser opened using your saved session. Click through to "
              "the CSV export/download button the same way you normally "
              "do, then come back here and press Enter -- the download "
              "is being captured in the background as soon as you click "
              "it, this Enter just lets the script know to check for it.")
        try:
            with page.expect_download(timeout=300_000) as download_info:
                input("Press Enter AFTER you've clicked the export/download "
                      "button in the browser...")
            download = download_info.value
            download.save_as(str(OUT_PATH))
            print(f"Saved to {OUT_PATH}")
        except Exception as e:
            print(f"No download captured ({e}). Nothing saved.")
        browser.close()


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
