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
    print("\nStrikers (want HIGH -- beat opponent's GK):")
    for name, sc in strikers:
        if name in fh_striker_names:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc}")
    print("\nGoalkeeper (want HIGH -- beat opponent's 2 strikers):")
    for name, sc in gk:
        if fh_gk and name == fh_gk[0]:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc}")
    print("\nSquad (11, sum vs opponent's 11):")
    for name, sc in squad:
        print(f"  {name}: {sc}")
    print(f"  --> squad total: {sum(sc for _, sc in squad):.2f}")
    print("\nBench (2, does not count):")
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
            picks_data = apply_overrides(picks_data, overrides[str(mid)])
        print(f"\n=== {member_name} ===")
        explain_manager(picks_data, gw_used, players, points)
        return

    scores: list[tuple[str, float]] = []
    for name, mid in MANAGER_IDS.items():
        picks_data, gw_used = fetch_picks_with_fallback(mid, args.mode, last_finished_gw, next_gw)

        if picks_data is None:
            print(f"  {name}: could not fetch picks at all (bad manager ID?), skipping")
            continue

        if args.mode == "preview" and str(mid) in overrides:
            picks_data = apply_overrides(picks_data, overrides[str(mid)])

        if args.best_xi:
            score = project_best_xi_score(picks_data, players, points)
        else:
            score = project_manager_score(picks_data, points)
        print(f"  {name} (GW{gw_used} squad): projected {score}")
        scores.append((name, score))

    suggest_lineup(scores, fh_names)


if __name__ == "__main__":
    main()
