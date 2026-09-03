"""SKLW (Strikers, Keepers, Losers, Weepers) club lineup helper.

Standalone script -- no dependency on the fpl-model/Axiom project, run it
directly with `python sklw_lineup.py`. Uses only the public FPL API plus
FPL's own `ep_next` (expected points next GW) field for projections, so it
needs no separate model.

Two modes:

  --mode preview   Pre-deadline. Pulls each manager's LAST COMPLETED GW
                    squad (the only thing publicly visible before the
                    deadline) and lets you apply manual overrides for any
                    known/expected transfers via overrides.json, before
                    projecting. Good for an early decision when you can't
                    wait for the real deadline.

  --mode final      Post-deadline. Pulls everyone's actual, now-public,
                    locked-in picks for the CURRENT gameweek straight from
                    the API -- no manual input, fully authoritative. Run
                    this once the real FPL deadline passes.

Setup:
  1. Fill in MANAGER_IDS below (or pass --ids-file with one ID per line).
  2. (Optional, preview mode only) create overrides.json:
       {
         "1234567": {"out": [123, 456], "in": [789, 101]}
       }
     where the numbers are FPL player element IDs (not names) -- out/in
     lists must be the same length. Look player IDs up in bootstrap-static
     ("elements") if needed; the script prints a small player-name lookup
     helper at the bottom to make this easier interactively.

Output: a suggested SKLW lineup -- 2 Strikers, 1 GK, 11 Squad, 2 Bench --
ranked by projected GW score (both Strikers and GK reward being HIGH:
strikers win by outscoring the opponent's GK, your GK wins by outscoring/
tying the opponent's strikers -- so your top scorers go to those roles,
not your weakest).
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import sys
import unicodedata
from pathlib import Path

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"

# --- Fill in once the club has picked a name ---
CLUB_NAME = "Algorithm and Blues"

# --- Fill these in with your 16 club members' real FPL manager/entry IDs ---
# (the number in https://fantasy.premierleague.com/entry/<ID>/ ...)
MANAGER_IDS: dict[str, int] = {
    "az": 26099,
    "Classiic": 10,
    "Cyclones": 137079,
    "farhan": 631,
    "Harv": 4190,
    "Heisen": 1230,
    "kb2": 7878,
    "Mahmoud": 6227,
    "Mordo": 32082,
    "neB": 1231,
    "nokah": 62,
    "riskypearl": 8052,
    "smooth": 653,
    "Stxddy": 625,
    "tyh": 650,
    "vrawn": 4,
}


def print_banner() -> None:
    """Printed on every run so it's always obvious which club/roster this
    copy of the script is wired to before it does anything else."""
    name = CLUB_NAME or "(name TBD)"
    print(f"=== SKLW Lineup Tool -- {name} ===")
    print(f"{len(MANAGER_IDS)} club members:")
    for member_name, mid in MANAGER_IDS.items():
        print(f"  {mid:>7}  {member_name}")
    print()


def get_json(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    return r.json()


def load_bootstrap() -> dict:
    return get_json(f"{FPL_BASE}/bootstrap-static/")


def current_and_next_gw(bootstrap: dict) -> tuple[int, int]:
    """Returns (last_finished_gw, gw_to_project). The GW to project is the
    LIVE one (is_current AND NOT finished) when there is one -- mid-GW,
    FPL flags that GW as is_current (not is_next) even though its deadline
    has already passed and picks are public, so is_next alone would
    incorrectly skip ahead to the GW AFTER the one that's actually in
    progress. The 'not finished' check matters too: FPL can leave
    is_current=True on a GW for a while after finished flips to True (the
    live-to-next-GW transition lags), so trusting is_current alone would
    keep targeting a GW that's already over instead of moving on. Falls
    back to is_next between gameweeks (nothing currently live)."""
    events = bootstrap["events"]
    finished = [e for e in events if e["finished"]]
    current_ev = next((e for e in events if e["is_current"] and not e["finished"]), None)
    next_ev = next((e for e in events if e["is_next"]), None)
    last_finished_id = finished[-1]["id"] if finished else 0
    target_ev = current_ev or next_ev
    target_id = target_ev["id"] if target_ev else last_finished_id + 1
    return last_finished_id, target_id


def player_lookup(bootstrap: dict) -> dict[int, dict]:
    return {p["id"]: p for p in bootstrap["elements"]}


def get_manager_picks(manager_id: int, gw: int) -> dict | None:
    """Public API only returns picks for a GW once its deadline has passed.
    Returns None (not raises) if not available yet -- callers fall back to
    the last completed GW instead."""
    try:
        return get_json(f"{FPL_BASE}/entry/{manager_id}/event/{gw}/picks/")
    except requests.HTTPError:
        return None


def ep_next_points(players: dict[int, dict]) -> dict[int, float]:
    """Default projection source: FPL's own ep_next field per element ID."""
    return {pid: float(p.get("ep_next") or 0.0) for pid, p in players.items()}


# Letters like o/ø aren't accented variants of each other in Unicode (no
# NFKD decomposition exists), just visually/phonetically similar -- so they
# need an explicit translation table rather than accent-stripping alone.
_EXTRA_FOLDS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
})


def _fold(s: str) -> str:
    """Lowercase and strip accents/special letters, for lenient name/team
    matching (handles 'Nørgaard' vs 'Norgaard'-style spelling differences
    between sources)."""
    s = s.translate(_EXTRA_FOLDS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def load_solio_projections(csv_path: Path, bootstrap: dict) -> dict[int, float]:
    """Maps a Solio-style projections CSV (Pos,ID,Name,BV,SV,Team,1_xMins...,
    N_Pts...) onto FPL element IDs, keyed by whichever '<N>_Pts' column has
    the LOWEST number (the soonest upcoming GW) -- not hardcoded to
    '1_Pts', since Solio numbers these relative to the current GW rather
    than always resetting to 1 (e.g. '2_Pts' once GW1 has passed). Solio's
    own 'ID' column is its own internal numbering, not the FPL element ID,
    so matching is by name (FPL's short web_name) first -- team is only
    used to disambiguate the rare case of two players sharing a web_name,
    not required to match, since a promoted club's name or a recent
    real-life transfer can make the two sources' 'Team' values disagree
    even for an unambiguous, correctly-matched player."""
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}
    by_name: dict[str, list[dict]] = {}
    for p in bootstrap["elements"]:
        by_name.setdefault(_fold(p["web_name"]), []).append(p)

    points: dict[int, float] = {}
    unmatched = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        pts_cols = sorted((c for c in reader.fieldnames if c.endswith("_Pts")),
                           key=lambda c: int(c.split("_")[0]))
        if not pts_cols:
            print(f"ERROR: no '<N>_Pts' column found in {csv_path}")
            sys.exit(1)
        next_gw_col = pts_cols[0]
        for row in reader:
            candidates = by_name.get(_fold(row["Name"]), [])
            if len(candidates) == 1:
                points[candidates[0]["id"]] = float(row[next_gw_col])
                continue
            if len(candidates) > 1:
                csv_team = _fold(row["Team"])
                narrowed = [p for p in candidates
                            if csv_team in _fold(team_names.get(p["team"], ""))
                            or _fold(team_names.get(p["team"], "")) in csv_team]
                if len(narrowed) == 1:
                    points[narrowed[0]["id"]] = float(row[next_gw_col])
                    continue
            unmatched.append(row["Name"])
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        print(f"WARNING: {len(unmatched)} CSV row(s) didn't match a unique "
              f"FPL player by name+team, skipped: {shown}")
    return points


def find_solio_csv() -> Path | None:
    """Finds a Solio projections CSV automatically -- no need to rename or
    move a fresh weekly export by hand. Checks 'solio.csv' in the current
    folder first (an explicit, stable override if you want one), then
    falls back to the most recently downloaded CSV in the user's Downloads
    folder whose header actually looks like a Solio export (checked by
    content, not just filename, so an unrelated CSV isn't picked up by
    mistake)."""
    here = Path("solio.csv")
    if here.exists():
        return here

    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return None
    candidates = sorted(downloads.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            with p.open(encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
        except (OSError, StopIteration):
            continue
        if {"Pos", "ID", "Name", "Team"}.issubset(header) and any(c.endswith("_Pts") for c in header):
            return p
    return None


def find_latest_screenshot() -> Path | None:
    """Finds the most recently added image in the user's Downloads folder
    -- lets --from-screenshot (no path given) work as "drop a screenshot
    in Downloads and run", same auto-detect pattern as find_solio_csv()."""
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return None
    candidates = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp"):
        candidates.extend(downloads.glob(ext))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def pick_best_eleven(picks_data: dict, players: dict[int, dict],
                      points: dict[int, float]) -> tuple[list[int], int | None]:
    """Given a manager's full 15-man squad, picks the highest-projected
    VALID starting XI (real FPL formation rules: 1 GK, 3-5 DEF, 2-5 MID,
    1-3 FWD) rather than trusting their actual submitted starting-11 --
    used by --best-xi to estimate each manager's best-possible score from
    their real squad. Returns (starter_element_ids, captain_element_id)."""
    by_pos: dict[int, list[tuple[float, int]]] = {1: [], 2: [], 3: [], 4: []}
    for p in picks_data["picks"]:
        el = players.get(p["element"])
        if not el:
            continue
        by_pos[el["element_type"]].append((points.get(p["element"], 0.0), p["element"]))
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    gk = by_pos[1][0] if by_pos[1] else None
    best_total = -1.0
    best_outfield: list[tuple[float, int]] = []
    for d in range(3, 6):
        for m in range(2, 6):
            f = 10 - d - m
            if not (1 <= f <= 3):
                continue
            if d > len(by_pos[2]) or m > len(by_pos[3]) or f > len(by_pos[4]):
                continue
            combo = by_pos[2][:d] + by_pos[3][:m] + by_pos[4][:f]
            total = sum(pts for pts, _ in combo)
            if total > best_total:
                best_total = total
                best_outfield = combo

    starters = ([gk] if gk else []) + best_outfield
    captain = max(starters)[1] if starters else None
    return [pid for _, pid in starters], captain


def project_best_xi_score(picks_data: dict, players: dict[int, dict],
                           points: dict[int, float]) -> float:
    """Like project_manager_score, but ignores the manager's actual
    submitted starting-11/captain and instead uses the highest-projected
    valid XI from their real 15-man squad (see pick_best_eleven). A one-off
    'what's their best possible score' estimate, not the authoritative
    actual-picks score -- so chip adjustments aren't applied here."""
    starter_ids, captain_id = pick_best_eleven(picks_data, players, points)
    total = sum(points.get(pid, 0.0) for pid in starter_ids)
    if captain_id is not None:
        total += points.get(captain_id, 0.0)  # captain doubled
    return round(total, 2)


def project_manager_score(picks_data: dict, points: dict[int, float]) -> float:
    """Sum projected points over the 11 starters, captain doubled, adjusted
    for chips per SKLW's own rule: BB drops the bench, TC deducts a third of
    the captain's score (since SKLW doesn't want chip effects skewing the
    inter-club scoring).

    Starters are picks with squad slot 'position' <= 11, NOT 'multiplier' >
    0 -- under Bench Boost, FPL's own API sets the bench's multiplier to 1
    too (since their real points count that week), so filtering on
    multiplier would silently include the bench exactly when SKLW's rule
    says not to. 'position' (1-11 = starting XI, 12-15 = bench) reflects
    the manager's actual starting-11 choice regardless of chip."""
    picks = picks_data["picks"]
    chip = picks_data.get("active_chip")  # "bboost", "3xc", "wildcard", "freehit", or None

    starters = [p for p in picks if p["position"] <= 11]
    total = 0.0
    captain_pts = 0.0
    for p in starters:
        pts = points.get(p["element"])
        if pts is None:
            continue
        mult = p["multiplier"] if p["multiplier"] > 0 else 1
        total += pts * mult
        if p["is_captain"]:
            captain_pts = pts * mult

    if chip == "3xc":
        # TC already applied x3 via multiplier above; SKLW rule: deduct a
        # third of the (already-tripled) captain score, leaving the
        # equivalent of a normal x2 captain for scoring purposes.
        total -= captain_pts / 3
    return round(total, 2)


def apply_overrides(picks_data: dict, override: dict) -> dict:
    """Swap 'out' element IDs for 'in' element IDs, same slot/multiplier --
    a simple like-for-like patch for preview mode. Captaincy/multiplier of
    the outgoing player carries over to the incoming one at the same slot."""
    picks = [dict(p) for p in picks_data["picks"]]
    out_ids = override.get("out", [])
    in_ids = override.get("in", [])
    if len(out_ids) != len(in_ids):
        raise ValueError("overrides.json: 'out' and 'in' lists must be the same length")
    swap = dict(zip(out_ids, in_ids))
    for p in picks:
        if p["element"] in swap:
            p["element"] = swap[p["element"]]
    return {**picks_data, "picks": picks}


def apply_wildcard(picks_data: dict, wildcard_ids: list[int]) -> dict:
    """Replaces the ENTIRE squad with wildcard_ids (15 element IDs) --
    unlike apply_overrides' like-for-like out/in swap, a Wildcard rebuilds
    most/all of the squad at once, so there's no meaningful old-slot to
    carry captaincy/multiplier from. Assigns arbitrary squad positions
    1-15 and multiplier 1 throughout, is meant to be paired with best-xi
    scoring (which recomputes the actual best formation/captain from
    scratch), not the real submitted-picks scoring."""
    picks = [{"element": eid, "position": i + 1, "multiplier": 1,
              "is_captain": False, "is_vice_captain": False}
             for i, eid in enumerate(wildcard_ids)]
    return {**picks_data, "picks": picks, "active_chip": "wildcard"}


def record_wildcard(overrides_path: Path, bootstrap: dict, manager_name: str,
                     manager_id: int, squad_fragments: str) -> None:
    """Resolves a full 15-name squad and saves it as that manager's
    'wildcard' entry in overrides.json (creating/overwriting it) --
    replaces the whole squad rather than an incremental out/in swap,
    since a Wildcard rebuilds most/all of it at once."""
    ids = resolve_players(bootstrap, squad_fragments)
    if len(ids) != 15:
        print(f"ERROR: --squad must list exactly 15 players, got {len(ids)}")
        sys.exit(1)

    all_overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    all_overrides[str(manager_id)] = {"wildcard": ids}
    overrides_path.write_text(json.dumps(all_overrides, indent=2))

    players = player_lookup(bootstrap)
    names = ", ".join(f"{players[i]['first_name']} {players[i]['second_name']}" for i in ids)
    print(f"Recorded Wildcard squad for {manager_name}: {names}")
    print(f"Saved to {overrides_path}\n")


def resolve_manager(name: str) -> tuple[str, int]:
    """Case-insensitive exact match against MANAGER_IDS' keys."""
    for member_name, mid in MANAGER_IDS.items():
        if member_name.lower() == name.lower():
            return member_name, mid
    print(f"ERROR: '{name}' isn't a known club member. Known names: "
          f"{', '.join(MANAGER_IDS)}")
    sys.exit(1)


def resolve_players(bootstrap: dict, fragments: str) -> list[int]:
    """Resolve a comma-separated list of name fragments to element IDs.
    Each fragment must match exactly one player -- ambiguous or missing
    matches abort with the candidate list so the user can be more specific,
    rather than silently guessing which player was meant."""
    ids = []
    for frag in [f.strip() for f in fragments.split(",") if f.strip()]:
        matches = [p for p in bootstrap["elements"]
                   if frag.lower() in f"{p['first_name']} {p['second_name']}".lower()]
        if len(matches) != 1:
            print(f"ERROR: '{frag}' matched {len(matches)} players, need exactly 1:")
            for p in matches[:10]:
                print(f"  {p['id']:>6}  {p['first_name']} {p['second_name']}")
            sys.exit(1)
        ids.append(matches[0]["id"])
    return ids


def _setup_tesseract() -> None:
    import shutil
    import pytesseract

    if not shutil.which("tesseract"):
        # Tesseract not on PATH -- try the standard Windows install
        # location before giving up, since forgetting to add it to PATH
        # (separate from the pip packages) is a common gotcha.
        for candidate in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ):
            if Path(candidate).exists():
                pytesseract.pytesseract.tesseract_cmd = candidate
                break


def _match_words_to_players(words: list[str], bootstrap: dict) -> tuple[dict[int, str], list[str]]:
    """Shared matching step: given a flat list of OCR'd word tokens, tries
    exact/substring matching first (contiguous 1-3 word spans, since
    player surnames are usually 1-3 tokens), then a fuzzy near-match
    fallback per single word for minor OCR misreads. Returns
    ({element_id: matched_text}, unmatched_words)."""
    # Longest web_name first, so a longer/more specific match wins over a
    # short substring coincidence (e.g. "Rice" inside an unrelated word).
    # Multiple real players can share a web_name (e.g. more than one real
    # "Palmer") -- name_to_ids maps to a LIST, and an ambiguous hit (more
    # than one id) is treated as no match rather than silently guessing
    # which one, since there's no team/club context available here to
    # disambiguate (unlike the Solio CSV loader, which has a Team column).
    by_name = sorted(
        ((_fold(p["web_name"]), p["id"]) for p in bootstrap["elements"] if len(p["web_name"]) >= 3),
        key=lambda x: -len(x[0]),
    )
    all_folded_names = list(dict.fromkeys(n for n, _ in by_name))  # de-duped, longest first
    name_to_ids: dict[str, list[int]] = {}
    for n, pid in by_name:
        name_to_ids.setdefault(n, []).append(pid)

    found: dict[int, str] = {}
    ambiguous: list[str] = []
    unmatched: list[str] = []
    for start in range(len(words)):
        matched = False
        for span in (1, 2, 3):
            chunk = " ".join(words[start:start + span])
            # Strip stray digits/symbols (price tags, remove-button
            # glyphs, etc. that OCR can merge into an adjacent word)
            # BEFORE the substring check -- without this, leftover noise
            # could form an accidental substring match against an
            # unrelated short real name (found in testing: this was
            # letting the exact pass match names it shouldn't have).
            cleaned_chunk = _fold("".join(c for c in chunk if c.isalpha() or c in " -'."))
            exact = next((n for n in all_folded_names if n in cleaned_chunk), None)
            # Also require the matched name to be a substantial fraction
            # of the cleaned chunk, not a short name buried in a much
            # longer garbled string -- same false-positive-vs-miss
            # tradeoff as the fuzzy cutoff below.
            if exact and len(exact) >= 0.6 * len(cleaned_chunk):
                ids = name_to_ids[exact]
                if len(ids) > 1:
                    ambiguous.append(f"{chunk} (matches {len(ids)} real players, skipped)")
                else:
                    found.setdefault(ids[0], chunk)
                matched = True
                break
        if matched:
            continue

        # Fuzzy fallback for minor OCR misreads. Cutoff set high (0.88)
        # specifically because a looser one (0.82) produced a real false
        # positive in testing -- garbled noise fuzzy-matched a real but
        # entirely unrelated player, which is worse than just missing a
        # name outright (the "type in missing names" prompt covers a miss).
        candidate = "".join(c for c in words[start] if c.isalpha() or c in "-'.").strip()
        if len(candidate) >= 3:
            close = difflib.get_close_matches(_fold(candidate), all_folded_names, n=1, cutoff=0.88)
            if close:
                ids = name_to_ids[close[0]]
                if len(ids) > 1:
                    ambiguous.append(f"{words[start]} (fuzzy, matches {len(ids)} real players, skipped)")
                else:
                    found.setdefault(ids[0], f"{words[start]} (fuzzy match)")
                continue
        unmatched.append(words[start])

    return found, ambiguous + unmatched


def _prep_image(img):
    """Grayscale + upscale -- small pitch-view name-tag text OCRs much
    better this way than the raw screenshot. Deliberately does NOT
    binarize (hard black/white threshold): tested against a small isolated
    crop and it actively destroyed the text -- upscaling a small font
    leaves anti-aliased gray edges, and a hard threshold breaks thin
    strokes rather than cleaning them up. Grayscale alone reads correctly
    where binarized failed outright."""
    from PIL import Image
    img = img.convert("L")
    scale = 3 if max(img.size) < 1600 else 1
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return img


def _ocr_words(img) -> list[str]:
    import pytesseract
    # PSM 11 = "sparse text: find as much text as possible in no
    # particular order" -- the default mode assumes a normal document
    # layout and can find NOTHING in an image with text scattered across
    # jersey icons/badges/a pitch graphic (pitch view) rather than a
    # single readable block, since it misjudges the whole thing as
    # non-text. Sparse mode searches everywhere instead of giving up.
    words = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)["text"]
    words = [w.strip() for w in words if w.strip()]
    if not words:
        # PSM 11 found nothing either -- fall back to the default mode in
        # case this particular image DOES have a normal-document layout
        # (e.g. list view) that PSM 11 handles worse than PSM 3 does.
        words = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)["text"]
        words = [w.strip() for w in words if w.strip()]
    return words


def _ocr_single_line(img) -> list[str]:
    """For a small crop expected to contain exactly one line of text (one
    player's name tag). Tested against real crops: no single PSM mode
    reliably won -- PSM 7 ("single line") caught some names PSM 8 missed
    and vice versa, seemingly depending on exact text length/kerning
    within the crop, so this tries several modes and keeps whatever any
    of them found rather than betting on one."""
    import pytesseract
    words: list[str] = []
    for psm in (7, 8, 6):
        text = pytesseract.image_to_string(img, config=f"--psm {psm}").strip()
        if text:
            words.extend(text.split())
    return words


# Known layout of the "15-man pitch view" screenshot template: 4 rows
# (GK, DEF, MID, FWD) with a fixed player count each, name tag roughly at
# this fractional (x, y) position within each row -- estimated visually
# from a real example, not pixel-measured, so treated as a rough starting
# point rather than exact truth (see extract_squad_grid_crop's caller for
# how this gets combined with whole-image OCR rather than trusted alone).
PITCH_VIEW_ROWS = [
    (0.26, [0.355, 0.665]),               # GK: 2 players
    (0.50, [0.19, 0.355, 0.51, 0.665, 0.82]),  # DEF: 5 players
    (0.74, [0.19, 0.355, 0.51, 0.665, 0.82]),  # MID: 5 players
    (0.94, [0.355, 0.51, 0.665]),         # FWD: 3 players
]


def extract_squad_grid_crop(image_path: Path, bootstrap: dict) -> tuple[dict[int, str], list[str]]:
    """Crops a small isolated region around each expected name-tag
    position in the known 15-man pitch-view layout (PITCH_VIEW_ROWS) and
    OCRs each crop separately -- isolated small crops of clean text are
    far easier for Tesseract than the whole busy graphic-heavy
    screenshot at once. Returns ({element_id: matched_text}, unmatched)."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    words: list[str] = []
    for y_frac, x_fracs in PITCH_VIEW_ROWS:
        for x_frac in x_fracs:
            cx, cy = x_frac * w, y_frac * h
            box_w, box_h = 0.22 * w, 0.09 * h
            crop = img.crop((cx - box_w / 2, cy - box_h / 2, cx + box_w / 2, cy + box_h / 2))
            crop = _prep_image(crop)
            words.extend(_ocr_single_line(crop))
    return _match_words_to_players(words, bootstrap)


def extract_squad_from_screenshot(image_path: Path, bootstrap: dict) -> tuple[list[int], list[str]]:
    """OCR's an FPL squad screenshot (pitch or list view) and matches
    detected text against the real bootstrap player list to recover a
    squad's element IDs. Runs two passes and merges the results: whole-
    image OCR (works for any layout, including list view) and, since the
    pitch-view screenshot is always the same known template, a grid-crop
    pass that OCRs each expected name-tag position in isolation (more
    reliable per-name, but only applies to that one template -- if this
    isn't a pitch-view screenshot the grid crops just won't find real
    names there and contribute nothing). Doesn't require a perfect text
    read either way -- exact/substring match first, fuzzy fallback for
    minor misreads, anything that doesn't match closely enough is
    reported as unmatched rather than guessed at. Returns (element_ids,
    unmatched_words -- deduplicated across both passes)."""
    _setup_tesseract()

    from PIL import Image
    whole_img = _prep_image(Image.open(image_path).convert("L"))
    whole_words = _ocr_words(whole_img)
    found_whole, unmatched_whole = _match_words_to_players(whole_words, bootstrap)

    found_grid, unmatched_grid = extract_squad_grid_crop(image_path, bootstrap)

    found = {**found_whole, **found_grid}
    unmatched = sorted(set(unmatched_whole) & set(unmatched_grid))  # only if BOTH passes failed on it
    return list(found.keys()), unmatched


def record_transfer(overrides_path: Path, bootstrap: dict, manager_name: str,
                     manager_id: int, out_fragments: str, in_fragments: str) -> None:
    """Resolves player names, appends the out/in pair to that manager's
    entry in overrides.json (creating the file/entry if needed), and saves.
    Skips any (out, in) pair that's already recorded for this manager --
    re-running the same --transfer command (e.g. by accident) shouldn't
    double it up."""
    out_ids = resolve_players(bootstrap, out_fragments)
    in_ids = resolve_players(bootstrap, in_fragments)
    if len(out_ids) != len(in_ids):
        print("ERROR: --out and --in must list the same number of players")
        sys.exit(1)

    all_overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    entry = all_overrides.setdefault(str(manager_id), {"out": [], "in": []})
    existing_pairs = list(zip(entry["out"], entry["in"]))

    players = player_lookup(bootstrap)
    added, skipped = [], []
    for pair in zip(out_ids, in_ids):
        if pair in existing_pairs:
            skipped.append(pair)
        else:
            existing_pairs.append(pair)
            added.append(pair)

    entry["out"] = [p[0] for p in existing_pairs]
    entry["in"] = [p[1] for p in existing_pairs]
    overrides_path.write_text(json.dumps(all_overrides, indent=2))

    def name(pid: int) -> str:
        return f"{players[pid]['first_name']} {players[pid]['second_name']}"

    if added:
        added_str = ", ".join(f"{name(o)} -> {name(i)}" for o, i in added)
        print(f"Recorded transfer for {manager_name}: {added_str}")
    if skipped:
        skipped_str = ", ".join(f"{name(o)} -> {name(i)}" for o, i in skipped)
        print(f"Already recorded (skipped duplicate): {skipped_str}")
    print(f"Saved to {overrides_path}\n")


def fetch_picks_with_fallback(mid: int, mode: str, last_finished_gw: int,
                               next_gw: int) -> tuple[dict | None, int | None]:
    """Shared fallback chain used by both the main scoring loop and
    --explain, so they can never drift apart on which GW's picks get used.
    Returns (picks_data, gw_used); picks_data is None if nothing worked."""
    picks_data = None
    gw_used = None
    if mode == "final":
        picks_data = get_manager_picks(mid, next_gw)
        gw_used = next_gw
        if picks_data is None:
            print(f"  GW{next_gw} picks not public yet (deadline hasn't "
                  f"passed) -- falling back to GW{last_finished_gw}")
    if picks_data is None and last_finished_gw > 0:
        picks_data = get_manager_picks(mid, last_finished_gw)
        gw_used = last_finished_gw
    if picks_data is None and mode == "preview":
        picks_data = get_manager_picks(mid, next_gw)
        gw_used = next_gw
    return picks_data, gw_used


def explain_manager(picks_data: dict, gw_used: int, players: dict[int, dict],
                     points: dict[int, float]) -> None:
    """Prints every pick used to compute one manager's score -- player,
    squad slot, points value used, captain/chip markers -- for debugging a
    score that doesn't look right."""
    chip = picks_data.get("active_chip")
    print(f"GW{gw_used} squad (active_chip={chip}):")
    for p in sorted(picks_data["picks"], key=lambda x: x["position"]):
        el = players.get(p["element"])
        name = f"{el['first_name']} {el['second_name']}" if el else f"element #{p['element']}"
        pts = points.get(p["element"])
        starter = "starter" if p["position"] <= 11 else "BENCH"
        cap = " (C)" if p["is_captain"] else (" (VC)" if p["is_vice_captain"] else "")
        pts_str = f"{pts:.2f}" if pts is not None else "NO PROJECTION FOUND"
        print(f"  slot {p['position']:>2}  {starter:>7}  {name}{cap}: {pts_str}")
    score = project_manager_score(picks_data, points)
    print(f"\nComputed score: {score}")


def suggest_lineup(scores: list[tuple[str, float]], fh_names: set[str] | None = None) -> None:
    """scores: [(manager_name, projected_score), ...]. Prints a suggested
    SKLW role assignment -- top scorers to GK+Strikers (both roles reward
    being high), rest fill the 11-a-side squad, bottom 2 benched.

    The single highest-projected manager goes to GK, not Strikers -- GK
    faces BOTH opposing Strikers individually (two H2H battles vs a
    Striker's one), so it has double the H2H exposure and is where the
    single best output belongs. Backtested in backtest.py against real
    historical FPL data: this GK-priority ordering alone was the entire
    source of improvement over a naive top-2-to-Strikers/3rd-to-GK split
    -- a further variance/ceiling-weighted selection was ALSO tested there
    and found to hurt, not help (real higher-volatility players tend to
    have lower means too, and that mean sacrifice outweighed any upside
    from the volatility), so this only reorders by plain projected mean.

    fh_names: managers on Free Hit this GW are prioritized into the
    highest-H2H-exposure individual roles, in order: GK first, then the 2
    Striker slots. FH gets no chip score adjustment so it counts at full
    value, and (per the same backtest reasoning) an FH score's inherent
    unpredictability makes it a GK/Striker candidate over a Squad one
    regardless of rank. Beyond GK + both Striker slots there's no more
    individual-battle room, so any further FH managers fall back into the
    normal pool with no special treatment."""
    fh_names = fh_names or set()
    ranked = sorted(scores, key=lambda x: -x[1])
    if len(ranked) < 15:
        print(f"WARNING: only {len(ranked)} managers with data (need 15) -- "
              f"suggestion below is incomplete.")

    fh_present = sorted((ns for ns in ranked if ns[0] in fh_names), key=lambda x: -x[1])
    fh_gk = fh_present[0] if fh_present else None
    fh_strikers = fh_present[1:3]
    fh_extra = fh_present[3:]
    fh_striker_names = {n for n, _ in fh_strikers}

    if fh_gk:
        if fh_extra:
            print(f"NOTE: also on Free Hit but GK + both Striker slots "
                  f"already taken by higher-priority FH picks, left in the "
                  f"normal pool: {', '.join(n for n, _ in fh_extra)}")
        for n, _ in fh_strikers:
            print(f"NOTE: {n} is on Free Hit this GW -- forced into "
                  f"Strikers (next-highest H2H exposure after GK).")
        print(f"NOTE: {fh_gk[0]} is on Free Hit this GW -- forced into GK "
              f"(GK faces both opponent Strikers, double the H2H exposure "
              f"of a Striker slot, so a big FH score is worth more there).")
        assigned = {fh_gk[0]} | fh_striker_names
        pool = [ns for ns in ranked if ns[0] not in assigned]
        remaining_striker_slots = 2 - len(fh_strikers)
        strikers = fh_strikers + pool[0:remaining_striker_slots]
        gk = [fh_gk]
        idx = remaining_striker_slots
        squad = pool[idx:idx + 11]
        bench = pool[idx + 11:idx + 13]
    else:
        gk = ranked[0:1]
        strikers = ranked[1:3]
        squad = ranked[3:14]
        bench = ranked[14:16]

    print("\n=== Suggested SKLW lineup ===")
    print("\nStrikers:")
    for name, sc in strikers:
        if name in fh_striker_names:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc}")
    print("\nGoalkeeper:")
    for name, sc in gk:
        if fh_gk and name == fh_gk[0]:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc}")
    print("\nSquad:")
    for name, sc in squad:
        print(f"  {name}: {sc}")
    print(f"  --> squad total: {sum(sc for _, sc in squad):.2f}")
    print("\nBench:")
    for name, sc in bench:
        print(f"  {name}: {sc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["preview", "final"], default="final")
    ap.add_argument("--overrides", default="overrides.json",
                     help="path to overrides.json (preview mode only)")
    ap.add_argument("--lookup", metavar="NAME_FRAGMENT",
                     help="instead of running, search bootstrap players by "
                          "name fragment and print their element IDs (for "
                          "building overrides.json)")
    ap.add_argument("--transfer", metavar="MANAGER_NAME",
                     help="record a transfer for this club member by name, "
                          "then re-run --mode preview with it applied. "
                          "Requires --out and --in.")
    ap.add_argument("--out", help="comma-separated player name fragment(s) "
                                   "being transferred out (with --transfer)")
    ap.add_argument("--in", dest="in_", help="comma-separated player name "
                                   "fragment(s) being transferred in, same "
                                   "order as --out (with --transfer)")
    ap.add_argument("--wildcard", metavar="MANAGER_NAME",
                     help="record a Wildcard squad for this club member --"
                          " replaces their ENTIRE squad (unlike --transfer's "
                          "incremental out/in swap), since Wildcard rebuilds "
                          "most/all of it at once. Requires --squad with "
                          "exactly 15 names. Scored via --best-xi logic "
                          "automatically, since the real starting-11/captain "
                          "choice for a wildcarded squad isn't known.")
    ap.add_argument("--squad", help="comma-separated list of exactly 15 "
                                     "player name fragments (with --wildcard)")
    ap.add_argument("--fh", action="append", metavar="MANAGER_NAME",
                     help="mark this club member as playing Free Hit this "
                          "GW -- forces them into the GK slot in the "
                          "suggested lineup (GK faces both opponent "
                          "Strikers, so a big/hard-to-project FH score is "
                          "worth more there). Repeat for multiple managers.")
    ap.add_argument("--projections", metavar="CSV_PATH",
                     help="path to a Solio-style projections CSV "
                          "(Pos,ID,Name,BV,SV,Team,1_xMins...,1_Pts...) to "
                          "score with instead of FPL's ep_next. Matched to "
                          "FPL players by name+team. Any player not found "
                          "in the CSV falls back to ep_next. Defaults to "
                          "solio.csv in the current folder if it exists.")
    ap.add_argument("--best-xi", action="store_true",
                     help="one-off comparison: ignore each manager's actual "
                          "submitted starting-11/captain and instead score "
                          "them using the highest-projected VALID XI (1 GK, "
                          "3-5 DEF, 2-5 MID, 1-3 FWD) picked from their real "
                          "15-man squad. Not the authoritative actual-picks "
                          "score -- chip adjustments aren't applied.")
    ap.add_argument("--list-transfers", action="store_true",
                     help="print all transfers recorded in overrides.json "
                          "this week (by player name), then exit.")
    ap.add_argument("--explain", metavar="MANAGER_NAME",
                     help="print the full player-by-player breakdown used "
                          "to compute one manager's score (squad slot, "
                          "points value, captain/chip), then exit -- for "
                          "debugging a score that doesn't look right.")
    ap.add_argument("--from-screenshot", metavar="IMAGE_PATH", nargs="?", const="AUTO",
                     help="OCR a squad screenshot (pitch or list view) "
                          "instead of pulling live picks, and print the "
                          "predicted best-XI/captain from it using current "
                          "projections. Omit the path to auto-detect the "
                          "most recently added image in your Downloads "
                          "folder (drop a screenshot there and just pass "
                          "--from-screenshot with nothing after it). "
                          "Requires 'pip install pytesseract pillow' plus "
                          "the Tesseract OCR binary installed separately "
                          "(not a pip package). List-view screenshots OCR "
                          "far more reliably than pitch view -- clean text "
                          "rows vs small text scattered over colored "
                          "jersey icons.")
    ap.add_argument("--for", dest="screenshot_for", metavar="MANAGER_NAME",
                     help="label the --from-screenshot output with this "
                          "club member's name (must be a known name from "
                          "MANAGER_IDS). Purely a label -- doesn't fetch "
                          "or override anything for that manager.")
    args = ap.parse_args()

    print_banner()

    print("Fetching bootstrap-static...")
    bootstrap = load_bootstrap()
    players = player_lookup(bootstrap)

    if args.lookup:
        frag = args.lookup.lower()
        matches = [p for p in bootstrap["elements"]
                   if frag in f"{p['first_name']} {p['second_name']}".lower()]
        for p in matches[:25]:
            print(f"  {p['id']:>6}  {p['first_name']} {p['second_name']} "
                  f"({p['team']}) ep_next={p.get('ep_next')}")
        return

    if args.transfer:
        if not args.out or not args.in_:
            print("ERROR: --transfer requires both --out and --in")
            sys.exit(1)
        member_name, mid = resolve_manager(args.transfer)
        record_transfer(Path(args.overrides), bootstrap, member_name, mid,
                         args.out, args.in_)
        print("Run 'run.bat --mode preview' separately to see the updated "
              "lineup once you're done recording transfers.")
        return

    if args.wildcard:
        if not args.squad:
            print("ERROR: --wildcard requires --squad with exactly 15 names")
            sys.exit(1)
        member_name, mid = resolve_manager(args.wildcard)
        record_wildcard(Path(args.overrides), bootstrap, member_name, mid, args.squad)
        print("Run 'run.bat --mode preview' separately to see the updated "
              "lineup once you're done recording Wildcard squads.")
        return

    if args.list_transfers:
        ov_path = Path(args.overrides)
        if not ov_path.exists():
            print(f"No transfers recorded yet ({ov_path} doesn't exist).")
            return
        overrides = json.loads(ov_path.read_text())
        if not overrides:
            print("No transfers recorded yet.")
            return
        id_to_name = {v: k for k, v in MANAGER_IDS.items()}
        print("=== Transfers recorded this week ===")
        for mid_str, entry in overrides.items():
            manager_name = id_to_name.get(int(mid_str), f"manager {mid_str}")
            if "wildcard" in entry:
                squad_names = ", ".join(
                    f"{players[i]['first_name']} {players[i]['second_name']}"
                    if i in players else f"#{i}" for i in entry["wildcard"])
                print(f"  {manager_name}: WILDCARD [{squad_names}]")
                continue
            out_names = ", ".join(
                f"{players[i]['first_name']} {players[i]['second_name']}"
                if i in players else f"#{i}" for i in entry.get("out", []))
            in_names = ", ".join(
                f"{players[i]['first_name']} {players[i]['second_name']}"
                if i in players else f"#{i}" for i in entry.get("in", []))
            print(f"  {manager_name}: OUT [{out_names}] -> IN [{in_names}]")
        return

    if not MANAGER_IDS:
        print("ERROR: fill in MANAGER_IDS at the top of this script first "
              "(name -> FPL manager ID for all 16 club members).")
        sys.exit(1)

    fh_names = set()
    for raw in args.fh or []:
        member_name, _ = resolve_manager(raw)
        fh_names.add(member_name)

    points = ep_next_points(players)
    projections_path = Path(args.projections) if args.projections else find_solio_csv()
    if projections_path and projections_path.exists():
        solio_points = load_solio_projections(projections_path, bootstrap)
        print(f"Loaded {len(solio_points)} player projection(s) from "
              f"{projections_path} (falling back to ep_next for anyone not "
              f"matched)")
        points.update(solio_points)
    elif args.projections:
        print(f"ERROR: no projections CSV found at {projections_path}")
        sys.exit(1)
    else:
        print("No Solio projections CSV found (checked solio.csv and "
              "Downloads) -- using ep_next only. Pass --projections <path> "
              "to use a specific file.")

    if args.from_screenshot:
        if args.from_screenshot == "AUTO":
            screenshot_path = find_latest_screenshot()
            if screenshot_path is None:
                print("ERROR: no image found in your Downloads folder. "
                      "Save/drop a screenshot there, or pass "
                      "--from-screenshot <path> explicitly.")
                return
            print(f"Using most recently added screenshot: {screenshot_path}")
        else:
            screenshot_path = Path(args.from_screenshot)
        element_ids, unmatched = extract_squad_from_screenshot(screenshot_path, bootstrap)
        print(f"\nMatched {len(element_ids)} player(s) from the screenshot:")
        for eid in element_ids:
            el = players.get(eid)
            name = f"{el['first_name']} {el['second_name']}" if el else f"element #{eid}"
            print(f"  {name}")
        if unmatched:
            shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
            print(f"  {len(unmatched)} word(s) didn't match any player "
                  f"(likely noise -- badges, point totals, headers): {shown}")

        if len(element_ids) < 15:
            print(f"\n{len(element_ids)}/15 matched. Type in any missing "
                  f"player name(s) to fill the gaps (comma-separated), or "
                  f"press Enter to continue with just what was found.")
            typed = input("Missing player name(s): ").strip()
            if typed:
                for eid in resolve_players(bootstrap, typed):
                    if eid not in element_ids:
                        element_ids.append(eid)
                print(f"Now have {len(element_ids)}/15 players.")

        if len(element_ids) < 11:
            print(f"ERROR: only matched {len(element_ids)} players, need at "
                  f"least 11 for a valid XI. Try a clearer screenshot -- "
                  f"list view tends to OCR far more reliably than pitch view.")
            return
        if len(element_ids) > 15:
            print(f"WARNING: matched {len(element_ids)} players, more than "
                  f"a 15-man squad -- some matches may be false positives.")

        if args.screenshot_for:
            label, _ = resolve_manager(args.screenshot_for)
        else:
            names = list(MANAGER_IDS.keys())
            print("\nWhich manager is this screenshot for?")
            for i, name in enumerate(names, 1):
                print(f"  {i}. {name}")
            choice = input("Enter a number (or press Enter to skip): ").strip()
            label = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else None

        picks_data = {"picks": [{"element": eid} for eid in element_ids]}
        starters, captain = pick_best_eleven(picks_data, players, points)
        score = project_best_xi_score(picks_data, players, points)
        header = f"Predicted best XI from screenshot: {label}" if label else "Predicted best XI from screenshot"
        print(f"\n=== {header} ===")
        for eid in starters:
            el = players.get(eid)
            name = f"{el['first_name']} {el['second_name']}" if el else f"element #{eid}"
            cap = " (C)" if eid == captain else ""
            print(f"  {name}{cap}: {points.get(eid, 0.0):.2f}")
        print(f"\nPredicted score: {score}")
        return

    last_finished_gw, next_gw = current_and_next_gw(bootstrap)
    print(f"Last finished GW: {last_finished_gw}, projecting for GW: {next_gw}")

    overrides = {}
    if args.mode == "preview":
        ov_path = Path(args.overrides)
        if ov_path.exists():
            overrides = json.loads(ov_path.read_text())
            print(f"Loaded {len(overrides)} manual override(s) from {ov_path}")
        else:
            print(f"No overrides file at {ov_path} -- using last-known squads as-is.")

    if args.explain:
        member_name, mid = resolve_manager(args.explain)
        picks_data, gw_used = fetch_picks_with_fallback(mid, args.mode, last_finished_gw, next_gw)
        if picks_data is None:
            print(f"  {member_name}: could not fetch picks at all (bad manager ID?)")
            return
        if args.mode == "preview" and str(mid) in overrides:
            entry = overrides[str(mid)]
            picks_data = apply_wildcard(picks_data, entry["wildcard"]) if "wildcard" in entry \
                else apply_overrides(picks_data, entry)
        print(f"\n=== {member_name} ===")
        explain_manager(picks_data, gw_used, players, points)
        return

    scores: list[tuple[str, float]] = []
    for name, mid in MANAGER_IDS.items():
        picks_data, gw_used = fetch_picks_with_fallback(mid, args.mode, last_finished_gw, next_gw)

        if picks_data is None:
            print(f"  {name}: could not fetch picks at all (bad manager ID?), skipping")
            continue

        # gw_used == next_gw means these are the manager's REAL, actually
        # locked-in picks for the GW being projected -- trust them as-is,
        # they're not a guess. Any other gw_used means we're using an
        # older/fallback squad as a proxy for what they'll field, since
        # their real picks for next_gw aren't public yet -- in that case,
        # optimize (best-xi) rather than assume they'll blindly repeat an
        # older week's exact selection. --best-xi forces optimization even
        # when real picks ARE available, for an explicit "what's the
        # ceiling" comparison.
        use_best_xi = args.best_xi or (gw_used != next_gw)

        if args.mode == "preview" and str(mid) in overrides:
            entry = overrides[str(mid)]
            if "wildcard" in entry:
                picks_data = apply_wildcard(picks_data, entry["wildcard"])
                use_best_xi = True  # real starting-11/captain for a wildcarded squad isn't known
            else:
                picks_data = apply_overrides(picks_data, entry)

        if use_best_xi:
            score = project_best_xi_score(picks_data, players, points)
        else:
            score = project_manager_score(picks_data, points)
        tag = "best-xi" if use_best_xi else "actual picks"
        print(f"  {name} (GW{gw_used} squad, {tag}): projected {score}")
        scores.append((name, score))

    suggest_lineup(scores, fh_names)


if __name__ == "__main__":
    main()
