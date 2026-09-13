"""Screenshots every relevant tab of the SKLW master Google Sheet
workbook (the "Live Scores" tab plus every "M#" fixture tab), for
parse_sheet_screenshots.py to OCR afterwards.

Standalone script, separate from fetch_sheet_workbook.py -- that one
downloads the actual .xlsx file, which needs DOWNLOAD permission (Drive
export). Confirmed live: this account's access has downloading
disabled by the sheet owner, so that path is blocked regardless of how
carefully the request is made (scripted or manual, it's the same
restriction). A screenshot only needs VIEWING access, which works fine
-- it's just a picture of what's already rendered on screen, same as
looking at it yourself, not a file export.

The tradeoff: xlsx export gives exact cell text and exact fill colors
for free. A screenshot gives neither directly -- parse_sheet_
screenshots.py has to OCR the text (pytesseract, same engine and same
lessons already learned the hard way in sklw_lineup.py's
--from-screenshot: isolated/upscaled crops read far more reliably than
a whole busy image at once) and sample pixel colors to tell GK from
Strikers. This is genuinely less reliable than reading a real xlsx cell
-- ALWAYS spot-check the first real run's output against what you can
see in the sheet yourself before trusting it, same caution as
--from-screenshot already carries.

Captures at 2x device scale factor (effectively doubles the pixel
density of everything without changing the layout) specifically because
small OCR'd text benefits from more real pixels to work with -- same
reasoning as sklw_lineup.py's _prep_image upscaling, just done at
capture time instead of after the fact since a higher-res screenshot
beats upscaling a low-res one.

Same login pattern as fetch_sheet_workbook.py (see that file for the
full rationale on why the document itself has to be opened by hand):
one-time --login, then the browser waits for you to click into the
SKLW sheet yourself each normal run. Switching BETWEEN tabs within an
already-open document, unlike jumping to a new document or hitting an
export URL, is just a same-page UI click (Google Sheets doesn't
navigate away), so that part IS automated once you're in.

Setup (one-time):
    pip install playwright
    playwright install chromium
    python fetch_sheet_workbook.py --login
        (shares the same browser_profile/ as fetch_sheet_workbook.py --
        no separate login needed here if you've already done that)

Normal use:
    python capture_sheet_screenshots.py
        (opens the Sheets home page -- click into the SKLW sheet
        yourself, press Enter once you can see it open, and every
        "Live Scores"/"M#" tab gets clicked through and screenshotted
        automatically into sheet_screenshots/)

Feed the resulting sheet_screenshots/ folder into
parse_sheet_screenshots.py.
"""
from __future__ import annotations

import re
from pathlib import Path

PROFILE_DIR = Path(__file__).parent / "browser_profile"
OUT_DIR = Path(__file__).parent / "sheet_screenshots"

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

TAB_NAME_RE = re.compile(r"^(Live Scores|M\d+)$")


def _launch_context(p):
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        channel="chrome",
        args=_LAUNCH_ARGS,
        device_scale_factor=2,  # more real pixels per bit of text -- helps OCR
        viewport={"width": 2200, "height": 1300},
    )


def discover_tabs(page) -> list[str]:
    """Reads the sheet tab bar at the bottom of the window for every tab
    matching 'Live Scores' or 'M<number>' -- doesn't hardcode which
    M-numbers exist, since the league's tab count can change season to
    season."""
    names = page.eval_on_selector_all(
        ".docs-sheet-tab-name",
        "els => els.map(e => e.textContent.trim())",
    )
    return [n for n in dict.fromkeys(names) if TAB_NAME_RE.match(n)]


def capture_tab(page, tab_name: str, out_dir: Path) -> bool:
    """Clicks the named tab (a real click, not a URL jump -- switching
    tabs within an already-open document doesn't trigger the
    goto-vs-click suspicion fetch_sheet_workbook.py hit, since it's a
    same-page UI interaction) and screenshots the grid area. Returns
    whether the tab was found and clicked.

    NOTE on completeness: Google Sheets virtualizes its grid (only
    renders rows/columns near the current viewport for performance), and
    a plain screenshot only captures what's actually rendered -- there
    is no reliable "full page" capture for it the way there is for a
    normal scrollable webpage, since the grid manages its own internal
    scroll rather than growing the page's scroll height. This zooms the
    browser out first (more content fits in the same viewport, offset
    by the higher device_scale_factor for OCR pixel density) as a
    best-effort fit, but a tab wider/taller than that still won't be
    fully captured -- confirmed only against the general approach, NOT
    verified end-to-end against a real M# tab's actual content extent.
    If parse_sheet_screenshots.py reports missing rows for a real tab,
    the fix is either zooming out further or a real scroll-and-stitch
    capture, neither implemented here yet."""
    tab = page.locator(".docs-sheet-tab-name", has_text=tab_name).first
    try:
        tab.click(timeout=10_000)
    except Exception:
        return False
    page.wait_for_timeout(1_500)  # let the grid finish rendering/scrolling into view
    for _ in range(3):
        page.keyboard.press("Control+Minus")
    page.wait_for_timeout(500)
    safe_name = tab_name.replace(" ", "_")
    page.screenshot(path=str(out_dir / f"{safe_name}.png"))
    page.keyboard.press("Control+0")  # reset zoom to 100% before the next tab
    return True


def main():
    from playwright.sync_api import sync_playwright

    if not PROFILE_DIR.exists():
        print(f"No saved session at {PROFILE_DIR} -- run "
              f"'python fetch_sheet_workbook.py --login' first (shared profile).")
        return

    OUT_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        context = _launch_context(p)
        page = context.new_page()
        try:
            page.goto("https://docs.google.com/spreadsheets/", wait_until="load")
            print("A browser window has opened on the Google Sheets home "
                  "page. Click into the SKLW sheet yourself here (search "
                  "for it if it's not in Recent).")
            input("Once you can actually see the document open (tabs, cells "
                  "visible), press Enter here...")
            # Confirmed live: reading the tab bar immediately after Enter
            # can race an in-progress page navigation from the click into
            # the document (however many seconds ago that was -- Google
            # Sheets can still be settling), destroying the JS execution
            # context mid-read. Wait for the page to actually finish
            # loading first, same fix as fetch_sheet_workbook.py's
            # earlier networkidle/timing issue.
            try:
                page.wait_for_load_state("load", timeout=15_000)
            except Exception:
                pass
            page.wait_for_timeout(2_000)

            tabs = discover_tabs(page)
            if not tabs:
                print("ERROR: couldn't find any 'Live Scores' or 'M#' tabs "
                      "in the tab bar -- is this actually the SKLW sheet? "
                      "Nothing captured.")
                context.close()
                return
            print(f"Found {len(tabs)} tab(s) to capture: {', '.join(tabs)}")

            captured, failed = [], []
            for name in tabs:
                print(f"  Capturing '{name}'...")
                if capture_tab(page, name, OUT_DIR):
                    captured.append(name)
                else:
                    failed.append(name)

            print(f"\nSaved {len(captured)} screenshot(s) to {OUT_DIR}/")
            if failed:
                print(f"Could not click/capture: {', '.join(failed)}")
        except Exception as e:
            print(f"Something went wrong ({e}). Whatever was captured "
                  f"before the error is still saved in {OUT_DIR}/.")
        context.close()


if __name__ == "__main__":
    main()
