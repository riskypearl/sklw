"""Reads the screenshots captured by capture_sheet_screenshots.py and
extracts GK/Strikers roles for a matchup -- OCR for the text, pixel
colors for GK-vs-Strikers (see capture_sheet_screenshots.py for why a
screenshot is needed at all here instead of the cleaner xlsx-based
resolve_matchup_roles.py: this account's Drive download permission is
disabled).

Standalone, separate from resolve_matchup_roles.py -- same end goal
(find this week's fixture, extract both clubs' GK/Strikers, resolve to
FPL IDs, write matchup_pins.json) but a completely different, far less
reliable extraction mechanism, so kept as its own file rather than
sharing internals.

KEY DESIGN CHOICE: rather than trying to reconstruct exact spreadsheet
cells/columns from raw OCR word positions (fragile -- club names and
some names are multi-word, and generic layout parsing has no ground
truth to check itself against), everything here matches OCR'd text
against a KNOWN candidate list instead -- every club name and every
manager handle already exists in clubs.json. That mirrors the one
extraction approach this project has already validated works
(sklw_lineup.py's screenshot OCR matches fragments against the real
bootstrap player list the same way, not by trying to parse generic
layout). A token that doesn't cleanly match a known candidate is
skipped and reported, never silently guessed.

THIS IS GENUINELY LESS RELIABLE than reading a real spreadsheet cell.
OCR misreads characters, pixel-color sampling can land on the wrong
spot, and screenshot capture (see capture_sheet_screenshots.py) may not
have gotten the whole tab if it didn't fit in the browser viewport.
Every extraction step below reports what it's confident about and what
it isn't -- ALWAYS check the first real run's output against the actual
sheet before trusting it for a matchup decision, same caution
--from-screenshot already carries in sklw_lineup.py.

Setup (one-time): pip install pytesseract pillow, plus the Tesseract
OCR binary itself (https://github.com/UB-Mannheim/tesseract/wiki on
Windows) -- not a pip package.

Usage:
    python capture_sheet_screenshots.py     (produces sheet_screenshots/)
    python parse_sheet_screenshots.py --our-club "Algorithm"
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import unicodedata
from pathlib import Path

SCREENSHOTS_DIR = "sheet_screenshots"
FUZZY_CUTOFF = 0.88  # same cutoff sklw_lineup.py's OCR matching settled on
# after testing found a looser one (0.82) produced a real false-positive
# match against unrelated text.


# Confirmed live against a real OCR pass (not just theorized): Tesseract
# actually found every single handle token in a test capture, but
# several failed matching anyway purely from classic OCR digit/letter
# ambiguity -- "@Ad_1net" read as "@Ad_linet" (1 -> "li"), "@LBench1"
# read as "@LBenchl" (1 -> l), "@LSquad0" read as "@LSquadO" (0 -> O).
# Normalizing these before comparing turns those into exact matches
# instead of relying on fuzzy distance (which missed at least one of
# them -- the "li" insertion was too different a string to clear the
# 0.88 cutoff). Tradeoff: two DIFFERENT real handles that differ only in
# one of these characters (e.g. one with an "l", another with a "1" in
# the same position) would collide after normalization -- accepted as
# unlikely given handles are unique strings, not flagged separately.
_OCR_DIGIT_LETTER_CONFUSIONS = str.maketrans({"l": "1", "i": "1", "o": "0"})


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    return s.translate(_OCR_DIGIT_LETTER_CONFUSIONS)


def _prep_image(img):
    """Grayscale + upscale. Borrowed this from sklw_lineup.py's
    _prep_image on the assumption it'd help the same way -- confirmed
    LIVE that assumption was WRONG for this kind of image: tested
    against a real rendered spreadsheet-style screenshot (clean text on
    a solid color background) and grayscale+3x-upscale made Tesseract
    find almost NOTHING (most rows silently vanished), while the RAW
    unprocessed image OCR'd the same content perfectly, 16/16 correct.
    sklw_lineup.py's win case was different: tiny name-tag text
    overlapping a busy graphical pitch background, where zooming in
    genuinely helps. Kept here only as a fallback (see ocr_tokens) for
    whatever the raw pass doesn't handle well, not the default."""
    img = img.convert("L")
    scale = 3 if max(img.size) < 2400 else 1
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale))
    return img


def _ocr_pass(img, scale: float) -> list[dict]:
    """One OCR attempt over `img`, returning tokens rescaled back to the
    ORIGINAL image's pixel coordinates (scale = img.width / original
    image's width) so callers can sample colors from the original color
    image regardless of which pass produced the token."""
    import pytesseract
    data = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
    tokens = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        tokens.append({
            "text": text,
            "left": int(data["left"][i] / scale),
            "top": int(data["top"][i] / scale),
            "width": int(data["width"][i] / scale),
            "height": int(data["height"][i] / scale),
        })
    return tokens


def ocr_tokens(image_path: Path, min_tokens: int = 5) -> list[dict]:
    """Returns [{"text", "left", "top", "width", "height"}, ...] in the
    ORIGINAL image's pixel coordinates. Tries the raw image first
    (confirmed live: reliable for clean spreadsheet-style screenshots),
    only falling back to the grayscale+upscale prep (see _prep_image) if
    the raw pass suspiciously finds fewer than `min_tokens` -- e.g. a
    genuinely low-resolution capture, where the sklw_lineup.py-style
    upscale might actually help rather than hurt. Uses whichever pass
    found more, since a real screenshot's exact behavior isn't fully
    known ahead of time (this account's real captures haven't been
    validated end-to-end yet -- see module docstring)."""
    from PIL import Image

    original = Image.open(image_path)
    raw_tokens = _ocr_pass(original, scale=1.0)
    if len(raw_tokens) >= min_tokens:
        return raw_tokens

    prepped = _prep_image(original)
    scale = prepped.width / original.width
    prepped_tokens = _ocr_pass(prepped, scale)
    return prepped_tokens if len(prepped_tokens) > len(raw_tokens) else raw_tokens


def group_into_rows(tokens: list[dict], y_tolerance: int = 12) -> list[list[dict]]:
    """Clusters tokens into rows by vertical center position -- sorted
    top to bottom, tokens within y_tolerance px of each other's center
    are the same row. Coarse but sufficient for finding which tokens
    sit near a given handle for color-sampling purposes."""
    if not tokens:
        return []
    by_y = sorted(tokens, key=lambda t: t["top"] + t["height"] / 2)
    rows: list[list[dict]] = [[by_y[0]]]
    for t in by_y[1:]:
        center = t["top"] + t["height"] / 2
        row_center = sum(r["top"] + r["height"] / 2 for r in rows[-1]) / len(rows[-1])
        if abs(center - row_center) <= y_tolerance:
            rows[-1].append(t)
        else:
            rows.append([t])
    return rows


def sample_color(img, token: dict) -> tuple[int, int, int]:
    """Samples the average RGB a few pixels below the token's text
    baseline, within its own row -- avoids the token's own (usually
    dark) text pixels while staying inside the same cell's fill color.
    Rough heuristic, not pixel-measured against a real example -- same
    honesty as sklw_lineup.py's _PITCH_LAYOUT comment. If this
    misclassifies GK/Strikers on a real screenshot, the fix is
    recalibrating the offset here against real capture output."""
    x = token["left"] + token["width"] // 2
    y = token["top"] + token["height"] + 3
    x = max(0, min(img.width - 1, x))
    y = max(0, min(img.height - 1, y))
    px = img.convert("RGB").getpixel((x, y))
    return px


def fuzzy_match_one(text: str, candidates: dict[str, int]) -> str | None:
    """Matches OCR'd text against a KNOWN set of candidate strings
    (handles or club names already in clubs.json) -- exact fold match
    first, then a high-cutoff fuzzy fallback for OCR misreads. Returns
    None (never a silent guess) if nothing clears the bar."""
    folded = _fold(text)
    by_fold = {_fold(c): c for c in candidates}
    if folded in by_fold:
        return by_fold[folded]
    close = difflib.get_close_matches(folded, list(by_fold), n=1, cutoff=FUZZY_CUTOFF)
    return by_fold[close[0]] if close else None


def find_fixture_from_live_scores(image_path: Path, our_club_substring: str,
                                   known_clubs: list[str]) -> tuple[str, str, str]:
    """OCRs the LiveScores screenshot, finds the row containing our
    club's name (fuzzy-matched against known_clubs, same style as
    sklw_matchup.py's --opponent), and returns (m_label, our_full_name,
    opponent_full_name) from that row. Errors clearly if our club isn't
    found in exactly one row."""
    tokens = ocr_tokens(image_path)
    rows = group_into_rows(tokens)
    needle = _fold(our_club_substring)

    matches: list[tuple[str, str, str]] = []
    for row in rows:
        row_text = " ".join(t["text"] for t in sorted(row, key=lambda t: t["left"]))
        m_label_tok = next((t["text"] for t in row if t["text"].upper().startswith("M")
                             and t["text"][1:].isdigit()), None)
        if not m_label_tok:
            continue
        # substring check per known club name against the row's full text
        present = [c for c in known_clubs if _fold(c) in _fold(row_text)]
        matched_ours = [c for c in present if needle in _fold(c)]
        if matched_ours and len(present) >= 2:
            opponent = next((c for c in present if c not in matched_ours), None)
            if opponent:
                matches.append((m_label_tok.upper(), matched_ours[0], opponent))

    if not matches:
        print(f"ERROR: '{our_club_substring}' didn't match any recognizable row in "
              f"the LiveScores screenshot (OCR may have missed it -- check the "
              f"image manually). Nothing found.")
        sys.exit(1)
    if len(matches) > 1:
        print(f"ERROR: '{our_club_substring}' matched more than one row: {matches}")
        sys.exit(1)
    return matches[0]


def resolve_handles_in_tab(image_path: Path, roster: dict[str, int]) -> list[dict]:
    """OCRs the M# tab screenshot and matches tokens against ONE club's
    known handle list (roster's keys) -- called once per side. Returns
    [{"handle", "left", "top", "width", "height"}, ...] for every
    cleanly-matched handle, sorted top to bottom. Handles OCR splitting
    a single handle across adjacent tokens (rare but possible) by also
    trying 2-token spans."""
    tokens = ocr_tokens(image_path)
    matched: dict[str, dict] = {}
    for i, tok in enumerate(tokens):
        for span in (1, 2):
            chunk_toks = tokens[i:i + span]
            chunk = "".join(t["text"] for t in chunk_toks)
            hit = fuzzy_match_one(chunk, roster)
            if hit and hit not in matched:
                matched[hit] = {"handle": hit, "left": chunk_toks[0]["left"],
                                 "top": chunk_toks[0]["top"], "width": sum(t["width"] for t in chunk_toks),
                                 "height": chunk_toks[0]["height"]}
                break
    return sorted(matched.values(), key=lambda m: m["top"])


def classify_gk_strikers(image_path: Path, top_entries: list[dict]) -> tuple[str, list[str]] | None:
    """Given the topmost 3 matched-handle entries for one club (by y
    position), samples each one's cell color and applies the same "2
    alike, 1 different" rule as resolve_matchup_roles.py's
    _classify_top_three -- the two sharing a color are Strikers, the
    odd one out is GK. Returns None (not a silent guess) if the colors
    don't split cleanly 2-1, e.g. because pixel-sampling landed
    somewhere unreliable."""
    from PIL import Image
    img = Image.open(image_path)
    colored = [(e["handle"], sample_color(img, e)) for e in top_entries]

    def close(c1, c2, tol=20):
        return all(abs(a - b) <= tol for a, b in zip(c1, c2))

    for i in range(3):
        others = [j for j in range(3) if j != i]
        if close(colored[others[0]][1], colored[others[1]][1]) and not close(colored[i][1], colored[others[0]][1]):
            return colored[i][0], [colored[others[0]][0], colored[others[1]][0]]
    return None


def parse_matchup_tab(image_path: Path, us_roster: dict[str, int], them_roster: dict[str, int]
                       ) -> dict:
    """Full extraction for one club's side of an M# tab screenshot:
    matches handles against `us_roster`, takes the topmost 3 as the
    GK+Strikers block, classifies them by color. Returns a dict with
    'gk', 'strikers', 'matched_count' (out of 16 -- low counts are a
    real signal the capture/OCR didn't get everything) and 'warning' if
    anything couldn't be determined cleanly.

    Requires a FULL 16/16 match before attempting GK/Strikers at all --
    confirmed live this matters: with even one handle missed by OCR
    (e.g. the real GK), "topmost 3 matched entries" silently shifts to
    include a Squad member instead, and the color check can still find
    an accidental 2-1 split among the WRONG 3 people, reporting a
    confident-looking but wrong GK. Refusing outright on a partial match
    is the safe failure mode -- better to say "check it yourself" than
    to silently write a wrong role into matchup_pins.json."""
    entries = resolve_handles_in_tab(image_path, us_roster)
    result: dict = {"matched_count": len(entries), "total_roster": len(us_roster)}
    if len(entries) < len(us_roster):
        result["warning"] = (f"only matched {len(entries)}/{len(us_roster)} handles -- "
                              f"refusing to guess GK/Strikers from a partial/possibly "
                              f"misaligned match. Missing OCR reads (even just one) can "
                              f"silently shift which 3 people look like the top block.")
        return result
    gk_strikers = classify_gk_strikers(image_path, entries[:3])
    if gk_strikers is None:
        result["warning"] = ("matched all 16 handles but couldn't split the top 3 into "
                              "a clean 2-1 by color -- pixel-sampling may have landed on "
                              f"the wrong spot. Candidates were: {[e['handle'] for e in entries[:3]]}")
        return result
    result["gk"], result["strikers"] = gk_strikers
    return result


def load_pins(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"us": {}, "them": {}}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"us": {}, "them": {}}
    data.setdefault("us", {})
    data.setdefault("them", {})
    return data


def save_pins(path: str, pins: dict) -> None:
    Path(path).write_text(json.dumps(pins, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots-dir", default=SCREENSHOTS_DIR)
    ap.add_argument("--clubs-file", default="clubs.json")
    ap.add_argument("--our-club", required=True)
    ap.add_argument("--pins-file", default="matchup_pins.json")
    args = ap.parse_args()

    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: pytesseract/pillow aren't installed -- run: pip install pytesseract pillow "
              "(and install the Tesseract OCR binary itself, see module docstring)")
        sys.exit(1)

    shots_dir = Path(args.screenshots_dir)
    live_scores_path = shots_dir / "LiveScores.png"
    if not live_scores_path.exists():
        print(f"ERROR: no {live_scores_path} -- run capture_sheet_screenshots.py first.")
        sys.exit(1)
    if not Path(args.clubs_file).exists():
        print(f"ERROR: no clubs.json at {args.clubs_file} -- run build_clubs_json.py first.")
        sys.exit(1)

    clubs = json.loads(Path(args.clubs_file).read_text())

    m_label, our_name, opponent_name = find_fixture_from_live_scores(
        live_scores_path, args.our_club, list(clubs))
    print(f"This week's fixture: {our_name} vs {opponent_name} ({m_label})")

    tab_path = shots_dir / f"{m_label}.png"
    if not tab_path.exists():
        print(f"ERROR: no {tab_path} -- capture_sheet_screenshots.py should have "
              f"captured every M# tab, check it ran fully.")
        sys.exit(1)

    us_result = parse_matchup_tab(tab_path, clubs[our_name], clubs[opponent_name])
    them_result = parse_matchup_tab(tab_path, clubs[opponent_name], clubs[our_name])

    for label, result in (("Us", us_result), ("Them", them_result)):
        print(f"\n{label} ({our_name if label == 'Us' else opponent_name}): "
              f"matched {result['matched_count']}/{result['total_roster']} handles")
        if "warning" in result:
            print(f"  WARNING: {result['warning']}")
        if "gk" in result:
            print(f"  GK: {result['gk']}, Strikers: {result['strikers']}")

    if "gk" not in us_result or "gk" not in them_result:
        print("\nERROR: couldn't determine GK/Strikers for both sides -- see warnings "
              "above. Nothing written to matchup_pins.json. Check the screenshots "
              "manually (sheet_screenshots/) before retrying.")
        sys.exit(1)

    us_gk_id = clubs[our_name].get(us_result["gk"])
    us_striker_ids = [clubs[our_name].get(n) for n in us_result["strikers"]]
    them_gk_id = clubs[opponent_name].get(them_result["gk"])
    them_striker_ids = [clubs[opponent_name].get(n) for n in them_result["strikers"]]
    if None in [us_gk_id, them_gk_id, *us_striker_ids, *them_striker_ids]:
        print("\nERROR: a matched handle isn't actually in clubs.json's roster for "
              "that club -- shouldn't happen (matching was already scoped to that "
              "roster), please report. Nothing written.")
        sys.exit(1)

    pins = load_pins(args.pins_file)
    pins["us"] = {"gk_id": str(us_gk_id), "strikers_ids": ",".join(str(i) for i in us_striker_ids)}
    pins["them"][opponent_name] = {"gk_id": str(them_gk_id),
                                    "strikers_ids": ",".join(str(i) for i in them_striker_ids)}
    save_pins(args.pins_file, pins)
    print(f"\nSaved to {args.pins_file}. This was OCR'd from a screenshot, not read "
          f"from a real spreadsheet cell -- double-check the GK/Strikers names above "
          f"against the actual sheet before trusting it. Run: "
          f"python sklw_matchup.py --opponent \"{opponent_name}\"")


if __name__ == "__main__":
    main()
