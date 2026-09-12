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
import statistics
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


def fetch_manager_ceiling(manager_id: int) -> float:
    """Stdev of this real manager's own week-to-week total GW score this
    season, via FPL's public entry-history endpoint -- how streaky/
    inconsistent THIS SPECIFIC manager personally tends to be, not a
    synthetic per-player figure. Used to ceiling-weight GK/Strikers
    selection: backtest.py found mean + k*std beats plain mean at every
    k tested, UNCONDITIONALLY -- not just when the club's an underdog.
    That's because SKLW's own goal formula is convex (downside capped
    at 0 goals, upside an unbounded stepped ladder), so higher variance
    raises EXPECTED goals regardless of favourite/underdog status; see
    the README's strategy section. Returns 0.0 (no ceiling boost) if the
    manager has fewer than 2 GWs of history yet (new team) or the
    request fails."""
    try:
        data = get_json(f"{FPL_BASE}/entry/{manager_id}/history/")
    except requests.HTTPError:
        return 0.0
    history = [gw["points"] for gw in data.get("current", [])]
    if len(history) < 2:
        return 0.0
    return statistics.pstdev(history)


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


def load_solio_projections(csv_path: Path, bootstrap: dict, horizon: int = 1) -> dict[int, float]:
    """Maps a Solio-style projections CSV (Pos,ID,Name,BV,SV,Team,1_xMins...,
    N_Pts...) onto FPL element IDs. Solio's own 'ID' column is its own
    internal numbering, not the FPL element ID, so matching is by name
    (FPL's short web_name) first -- team is only used to disambiguate the
    rare case of two players sharing a web_name, not required to match,
    since a promoted club's name or a recent real-life transfer can make
    the two sources' 'Team' values disagree even for an unambiguous,
    correctly-matched player.

    horizon: by default (1) uses just whichever '<N>_Pts' column has the
    LOWEST number (the soonest upcoming GW) -- not hardcoded to '1_Pts',
    since Solio numbers these relative to the current GW rather than
    always resetting to 1 (e.g. '2_Pts' once GW1 has passed). A horizon
    above 1 instead AVERAGES across that many of the nearest '<N>_Pts'
    columns (clamped to however many the CSV actually has) -- used by
    --wildcard-auto, where the squad needs to hold up well over several
    gameweeks, not just score well once; the normal per-GW scoring flow
    always uses the default horizon=1."""
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
        use_cols = pts_cols[:max(1, horizon)]

        def avg_pts(row: dict) -> float:
            vals = [float(row[c]) for c in use_cols if row.get(c, "").strip() != ""]
            return sum(vals) / len(vals) if vals else 0.0

        for row in reader:
            candidates = by_name.get(_fold(row["Name"]), [])
            if len(candidates) == 1:
                points[candidates[0]["id"]] = avg_pts(row)
                continue
            if len(candidates) > 1:
                csv_team = _fold(row["Team"])
                narrowed = [p for p in candidates
                            if csv_team in _fold(team_names.get(p["team"], ""))
                            or _fold(team_names.get(p["team"], "")) in csv_team]
                if len(narrowed) == 1:
                    points[narrowed[0]["id"]] = avg_pts(row)
                    continue
            unmatched.append(row["Name"])
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        print(f"WARNING: {len(unmatched)} CSV row(s) didn't match a unique "
              f"FPL player by name+team, skipped: {shown}")
    return points


def _is_solio_csv(path: Path) -> bool:
    """Content check (not just filename) so an unrelated CSV isn't picked
    up by mistake."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
    except (OSError, StopIteration):
        return False
    return {"Pos", "ID", "Name", "Team"}.issubset(header) and any(c.endswith("_Pts") for c in header)


def find_solio_csv() -> Path | None:
    """Finds a Solio projections CSV automatically -- no need to rename or
    move a fresh weekly export by hand. Considers BOTH 'solio.csv' in the
    current folder (if present and it actually looks like a Solio export)
    AND the most recently downloaded CSV in the user's Downloads folder
    that looks like one, and returns whichever is NEWER by modification
    time -- a stale local solio.csv left over from a previous week must
    not silently block a freshly downloaded one from ever being picked up
    (an earlier version always preferred the local file unconditionally,
    which is exactly backwards for the weekly drop-a-fresh-export-and-run
    workflow this is meant to support)."""
    candidates: list[Path] = []

    here = Path("solio.csv")
    if here.exists() and _is_solio_csv(here):
        candidates.append(here)

    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        for p in sorted(downloads.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
            if _is_solio_csv(p):
                candidates.append(p)
                break  # only need the single freshest matching one from Downloads

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


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
                      points: dict[int, float],
                      forced_captain: int | None = None) -> tuple[list[int], int | None]:
    """Given a manager's full 15-man squad, picks the highest-projected
    VALID starting XI (real FPL formation rules: 1 GK, 3-5 DEF, 2-5 MID,
    1-3 FWD) rather than trusting their actual submitted starting-11 --
    used by --best-xi to estimate each manager's best-possible score from
    their real squad. Returns (starter_element_ids, captain_element_id).

    forced_captain: for a manager's own deliberate Triple Captain pick
    (--tc), which may not be their single highest-projected player, so it
    can't just be inferred from the formation optimization above. If the
    forced pick isn't already in the computed starting XI, it's swapped in
    for the weakest starter in the SAME position group (same slot count,
    so the formation stays valid) -- real TC picks are always someone the
    manager actually intends to start. Ignored (with a warning) if the
    pick isn't even in this manager's 15-man squad at all."""
    squad_ids = {p["element"] for p in picks_data["picks"]}
    if forced_captain is not None and forced_captain not in squad_ids:
        fc_el = players.get(forced_captain)
        name = f"{fc_el['first_name']} {fc_el['second_name']}" if fc_el else f"element #{forced_captain}"
        print(f"WARNING: Triple Captain pick {name} isn't in this manager's "
              f"squad -- ignoring forced captain, using best-xi's own pick instead.")
        forced_captain = None

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

    if forced_captain is not None:
        starter_ids = [pid for _, pid in starters]
        if forced_captain not in starter_ids:
            fc_pos = players[forced_captain]["element_type"]
            fc_pts = points.get(forced_captain, 0.0)
            group_idx = [i for i, (_, pid) in enumerate(starters)
                         if players[pid]["element_type"] == fc_pos]
            weakest_idx = min(group_idx, key=lambda i: starters[i][0])
            starters[weakest_idx] = (fc_pts, forced_captain)
        captain = forced_captain
    else:
        captain = max(starters)[1] if starters else None

    return [pid for _, pid in starters], captain


def starters_and_captain(picks_data: dict, players: dict[int, dict],
                          points: dict[int, float], use_best_xi: bool,
                          forced_captain: int | None) -> tuple[list[int], int | None]:
    """Same starter/captain choice used for scoring, factored out so
    --effective-ownership can tally it without duplicating the branch
    logic from the main scoring loop below."""
    if use_best_xi:
        return pick_best_eleven(picks_data, players, points, forced_captain)
    starters = [p["element"] for p in picks_data["picks"] if p["position"] <= 11]
    captain = next((p["element"] for p in picks_data["picks"] if p["is_captain"]), None)
    return starters, captain


def project_best_xi_score(picks_data: dict, players: dict[int, dict],
                           points: dict[int, float],
                           forced_captain: int | None = None) -> float:
    """Like project_manager_score, but ignores the manager's actual
    submitted starting-11/captain and instead uses the highest-projected
    valid XI from their real 15-man squad (see pick_best_eleven). A one-off
    'what's their best possible score' estimate, not the authoritative
    actual-picks score -- so chip adjustments aren't applied here (except
    forced_captain -- see pick_best_eleven -- which is just a normal x2
    double, matching SKLW's own net effect for Triple Captain)."""
    starter_ids, captain_id = pick_best_eleven(picks_data, players, points, forced_captain)
    total = sum(points.get(pid, 0.0) for pid in starter_ids)
    if captain_id is not None:
        total += points.get(captain_id, 0.0)  # captain doubled
    return round(total, 2)


def project_manager_score(picks_data: dict, points: dict[int, float]) -> float:
    """Sum projected points over the 11 starters, captain doubled, adjusted
    for chips per SKLW's own rule: BB drops the bench, TC deducts a third of
    the captain's score (since SKLW doesn't want chip effects skewing the
    inter-club scoring).

    Starters are picks with 'multiplier' > 0, NOT squad slot 'position' <=
    11 -- 'position' is frozen at the manager's ORIGINALLY declared lineup
    and never updates, but FPL applies AUTOMATIC SUBSTITUTIONS mid/post-
    gameweek (a starter who blanked gets swapped for a bench player who
    played) by updating 'multiplier' instead (0 for subbed-out, 1 for
    subbed-in) -- filtering on 'position' alone silently keeps crediting a
    blanked starter's zero and drops the real substitute's points
    entirely. The one exception is Bench Boost: SKLW overrides real FPL's
    own BB rule (bench still doesn't count here), and BB sets EVERY pick's
    multiplier to 1 including the bench, so 'position' is the only
    reliable signal specifically for that one chip.

    The real captain is identified the same way, via whichever pick has
    'multiplier' > 1 -- not the static 'is_captain' label, which never
    moves even when FPL transfers the multiplier to the vice-captain
    because the real captain blanked."""
    picks = picks_data["picks"]
    chip = picks_data.get("active_chip")  # "bboost", "3xc", "wildcard", "freehit", or None

    if chip == "bboost":
        starters = [p for p in picks if p["position"] <= 11]
    else:
        starters = [p for p in picks if p["multiplier"] > 0]
    total = 0.0
    captain_pts = 0.0
    for p in starters:
        pts = points.get(p["element"])
        if pts is None:
            continue
        mult = p["multiplier"] if p["multiplier"] > 0 else 1
        total += pts * mult
        if p["multiplier"] > 1:
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


def save_wildcard_squad(overrides_path: Path, players: dict[int, dict],
                         manager_name: str, manager_id: int, ids: list[int]) -> None:
    """Shared save step for a resolved 15-id Wildcard squad -- writes it
    into that manager's 'wildcard' entry in overrides.json (creating/
    overwriting it). Used by both --wildcard (one manager, hand-typed
    squad) and --wildcard-auto (many managers, one computed squad)."""
    all_overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    entry = all_overrides.setdefault(str(manager_id), {})
    entry["wildcard"] = ids
    overrides_path.write_text(json.dumps(all_overrides, indent=2))

    names = ", ".join(f"{players[i]['first_name']} {players[i]['second_name']}" for i in ids)
    print(f"Recorded Wildcard squad for {manager_name}: {names}")


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

    players = player_lookup(bootstrap)
    save_wildcard_squad(overrides_path, players, manager_name, manager_id, ids)
    print(f"Saved to {overrides_path}\n")


def save_manual_score(overrides_path: Path, manager_name: str,
                       manager_id: int, score: float) -> None:
    """Directly sets a manager's final SKLW score for this GW to an exact
    number, skipping the normal picks/points computation entirely for
    them (the main scoring loop short-circuits on this before even
    fetching their picks). For --wildcard-auto in particular: several
    managers on the exact same computed squad are going to end up with
    near-identical real scores anyway, so rather than trust slightly
    different best-xi numbers (which can differ GW to GW as each
    manager's own bench/captain history diverges even off an identical
    squad), just call it the same number for all of them."""
    all_overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    entry = all_overrides.setdefault(str(manager_id), {})
    entry["manual_score"] = score
    overrides_path.write_text(json.dumps(all_overrides, indent=2))
    print(f"Set manual score override for {manager_name}: {score}")


def parse_xpoints_overrides(bootstrap: dict, spec: str) -> dict[int, float]:
    """Parses --xpoints's value into {element_id: manual_points}. Format:
    comma-separated 'name=value' pairs, e.g. 'Haaland=15,Salah=12' -- lets
    you directly set the expected-points NUMBER used by --wildcard-auto's
    optimizer for specific players, overriding whatever ep_next/Solio
    would otherwise have projected, for whenever you don't trust the auto
    projection for someone (returning from injury, a new signing with no
    track record, a hunch, etc)."""
    overrides: dict[int, float] = {}
    for pair in [p.strip() for p in spec.split(",") if p.strip()]:
        if "=" not in pair:
            print(f"ERROR: --xpoints entries must be 'name=value', got '{pair}'")
            sys.exit(1)
        name_part, value_part = pair.rsplit("=", 1)
        try:
            value = float(value_part.strip())
        except ValueError:
            print(f"ERROR: --xpoints value for '{name_part.strip()}' isn't "
                  f"a number: '{value_part.strip()}'")
            sys.exit(1)
        ids = resolve_players(bootstrap, name_part.strip())
        overrides[ids[0]] = value
    return overrides


def build_optimal_wildcard_squad(bootstrap: dict, points: dict[int, float],
                                  budget: int = 1000) -> list[int]:
    """Builds ONE budget-legal 15-man squad (2 GK/5 DEF/5 MID/3 FWD, max 3
    players from any one real club, total cost <= budget in FPL's own
    now_cost units -- tenths of a million, so 1000 = GBP100.0m) that
    maximizes the best valid starting-XI + captain score achievable from
    it. Solved as a single MILP (squad selection and starting-XI/captain
    selection jointly, not as two separate steps) via PuLP + its bundled
    CBC solver -- no external binary needed, unlike Tesseract.

    Used by --wildcard-auto: several analytics-minded managers picking a
    Wildcard independently would likely converge on close to the same
    squad anyway (same projections, same budget/formation rules), so
    rather than typing out --wildcard/--squad by hand per manager, this
    computes ONE genuinely optimal squad once and lets it be applied to
    all of them in a single command."""
    import pulp

    elements = [p for p in bootstrap["elements"] if p["status"] != "u"]
    ids = [p["id"] for p in elements]
    pts = {p["id"]: points.get(p["id"], 0.0) for p in elements}
    cost = {p["id"]: p["now_cost"] for p in elements}
    pos = {p["id"]: p["element_type"] for p in elements}  # 1 GK, 2 DEF, 3 MID, 4 FWD
    team = {p["id"]: p["team"] for p in elements}

    prob = pulp.LpProblem("wildcard_squad", pulp.LpMaximize)
    x = pulp.LpVariable.dicts("squad", ids, cat="Binary")   # in the 15-man squad
    y = pulp.LpVariable.dicts("start", ids, cat="Binary")   # in the starting XI
    c = pulp.LpVariable.dicts("cap", ids, cat="Binary")     # captain

    prob += (pulp.lpSum(pts[i] * y[i] for i in ids)
             + pulp.lpSum(pts[i] * c[i] for i in ids))  # captain's score doubled

    prob += pulp.lpSum(x[i] for i in ids) == 15
    prob += pulp.lpSum(x[i] for i in ids if pos[i] == 1) == 2
    prob += pulp.lpSum(x[i] for i in ids if pos[i] == 2) == 5
    prob += pulp.lpSum(x[i] for i in ids if pos[i] == 3) == 5
    prob += pulp.lpSum(x[i] for i in ids if pos[i] == 4) == 3
    prob += pulp.lpSum(cost[i] * x[i] for i in ids) <= budget
    for t in {team[i] for i in ids}:
        prob += pulp.lpSum(x[i] for i in ids if team[i] == t) <= 3

    prob += pulp.lpSum(y[i] for i in ids) == 11
    prob += pulp.lpSum(y[i] for i in ids if pos[i] == 1) == 1
    def_start = pulp.lpSum(y[i] for i in ids if pos[i] == 2)
    mid_start = pulp.lpSum(y[i] for i in ids if pos[i] == 3)
    fwd_start = pulp.lpSum(y[i] for i in ids if pos[i] == 4)
    prob += def_start >= 3
    prob += def_start <= 5
    prob += mid_start >= 2
    prob += mid_start <= 5
    prob += fwd_start >= 1
    prob += fwd_start <= 3

    for i in ids:
        prob += y[i] <= x[i]
        prob += c[i] <= y[i]
    prob += pulp.lpSum(c[i] for i in ids) == 1

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        print(f"ERROR: wildcard squad optimizer failed to find a solution "
              f"(solver status: {pulp.LpStatus[prob.status]})")
        sys.exit(1)

    return [i for i in ids if x[i].value() > 0.5]


def record_tc(overrides_path: Path, bootstrap: dict, manager_name: str,
              manager_id: int, captain_fragment: str) -> None:
    """Records a manual Triple Captain pick for this manager -- which
    specific player they're captaining, since a real TC choice is
    deliberate (a differential, a favourable fixture, a nailed-on
    penalty taker) and won't always be whoever best-xi's own logic would
    have doubled automatically. Saved as a 'tc_captain' key, distinct
    from (and stackable with) 'out'/'in' and 'wildcard' -- a manager can
    transfer AND triple-captain, or wildcard AND triple-captain, in the
    same week."""
    ids = resolve_players(bootstrap, captain_fragment)
    if len(ids) != 1:
        print(f"ERROR: --captain must resolve to exactly 1 player, got {len(ids)}")
        sys.exit(1)
    captain_id = ids[0]

    all_overrides = json.loads(overrides_path.read_text()) if overrides_path.exists() else {}
    entry = all_overrides.setdefault(str(manager_id), {})
    entry["tc_captain"] = captain_id
    overrides_path.write_text(json.dumps(all_overrides, indent=2))

    players = player_lookup(bootstrap)
    name = f"{players[captain_id]['first_name']} {players[captain_id]['second_name']}"
    print(f"Recorded Triple Captain for {manager_name}: captaining {name}")
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
    Matching folds accents/special characters on BOTH sides (a plain
    ASCII 'Joao Pedro' or 'Hornicek' fragment matches a stored 'João
    Pedro'/'Hornícek' just fine -- no need to type or copy-paste special
    characters, which is unreliable in a Windows terminal) -- same
    accent-folding as load_solio_projections' name matching, via _fold().

    Tiered matching, tightest first, to avoid false-positive substring
    collisions (hit repeatedly in practice: 'Egan' matching 'R-EGAN-
    Slater', 'Saka' matching 'Wan-Bis-SAKA' and '-SAKA-moto'):
      1. Exact match on second_name alone (surname) -- resolves the
         common case (typing just a surname) precisely, since a
         surname-internal substring like 'egan' inside 'Regan' isn't an
         EXACT surname match for anyone.
      2. Exact match on the full 'first_name second_name' string --
         covers a fragment that's someone's full name.
      3. Substring search across the full name (the original, loosest
         behavior) -- last resort, for a deliberately partial fragment
         that isn't an exact surname or full name (e.g. 'Ronald' for
         'Ronaldo').
    Moves to the next tier only when the current one finds zero matches;
    2+ exact surname matches (e.g. two real players both named exactly
    'Palmer') is reported as genuine ambiguity rather than falling
    through to substring, since substring could only ever widen it.

    Every fragment is checked before anything fails: ambiguous or
    missing matches are ALL collected and printed together in one pass,
    rather than aborting at the first bad one and making the user fix
    problems one at a time across repeated re-runs."""
    problems: list[str] = []
    ids: list[int] = []
    for frag in [f.strip() for f in fragments.split(",") if f.strip()]:
        folded = _fold(frag)
        surname_exact = [p for p in bootstrap["elements"] if _fold(p["second_name"]) == folded]
        if len(surname_exact) == 1:
            matches = surname_exact
        elif surname_exact:
            matches = surname_exact  # 2+ exact surname matches: real ambiguity, report as-is
        else:
            full_exact = [p for p in bootstrap["elements"]
                          if _fold(f"{p['first_name']} {p['second_name']}") == folded]
            if full_exact:
                matches = full_exact
            else:
                matches = [p for p in bootstrap["elements"]
                           if folded in _fold(f"{p['first_name']} {p['second_name']}")]
        if len(matches) != 1:
            lines = [f"'{frag}' matched {len(matches)} players, need exactly 1:"]
            for p in matches[:10]:
                lines.append(f"    {p['id']:>6}  {p['first_name']} {p['second_name']}")
            problems.append("\n".join(lines))
        else:
            ids.append(matches[0]["id"])

    if problems:
        print(f"ERROR: {len(problems)} name(s) out of {len(fragments.split(','))} "
              f"didn't resolve cleanly:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        sys.exit(1)
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


def suggest_lineup(scores: list[tuple[str, float]], fh_names: set[str] | None = None,
                    ceiling: dict[str, float] | None = None, k: float = 0.5) -> None:
    """scores: [(manager_name, projected_score), ...]. Prints a suggested
    SKLW role assignment -- top scorer to GK, next 2 to Strikers, rest
    fill the 11-a-side squad, bottom 2 benched.

    GK gets the top score, not Strikers -- a strong GK score is compared
    against BOTH of the opponent's Strikers (denying goals in 2 separate
    H2H battles at once), while a strong Striker score only wins its own
    single battle against the opposing GK. The GK does NOT independently
    score goals of its own (confirmed by reproducing 4 real SKLW match
    results exactly -- see the README's rules section); its value is
    purely this double defensive exposure. Backtested in backtest.py
    against real historical FPL data using NET goal differential (goals
    for minus goals conceded -- a one-sided "goals for only" metric
    misses the GK's whole defensive contribution by construction):
    best-to-GK beats top-2-to-Strikers/3rd-to-GK decisively (avg net
    diff +0.206 vs -0.005, 34.5% vs 20.8% head-to-head at plain-mean
    ranking).

    ceiling/k: the GK/Strikers pool (top 3 candidates) is picked by
    `score + k * ceiling[name]` instead of plain score when a ceiling
    map is given -- backtest.py found mean + 0.5*stdev beats plain mean
    at EVERY k tested, unconditionally (not just when the club's an
    underdog): SKLW's own goal formula is convex (downside capped at 0,
    upside an unbounded stepped ladder), so higher variance raises
    EXPECTED goals in the individual-role H2H slots regardless of
    favourite/underdog status -- see the README's strategy section.
    ceiling[name] = stdev of that manager's own real week-to-week GW
    score history (fetch_manager_ceiling); a manager missing from
    `ceiling` gets 0.0 (no boost, falls back to plain score for them).
    Pass ceiling=None (or k=0) for the pre-ceiling-weighting default of
    plain top-3-by-mean. Squad/Bench selection ALWAYS uses plain score
    -- ceiling-weighting only helps the individual-role H2H slots;
    backtest.py found it dilutes/doesn't help once pooled into the
    Squad sum.

    fh_names: managers on Free Hit this GW are prioritized into the
    scoring roles, in order: GK first, then both Striker slots (ceiling-
    weighting doesn't apply to them -- a chip forces them in regardless
    of any ranking). FH gets no chip score adjustment so it counts at
    full value, and GK is the scarcer/more valuable individual-role slot
    (see above), so the highest-scoring FH manager goes there first.
    Beyond GK + both Striker slots there's no more individual-role room,
    so any further FH managers fall back into the normal pool (still
    ceiling-weighted for the boundary, if any Striker slots remain)."""
    fh_names = fh_names or set()
    ceiling = ceiling or {}
    ranked = sorted(scores, key=lambda x: -x[1])
    if len(ranked) < 15:
        print(f"WARNING: only {len(ranked)} managers with data (need 15) -- "
              f"suggestion below is incomplete.")

    def by_ceiling(pool: list[tuple[str, float]]) -> list[tuple[str, float]]:
        return sorted(pool, key=lambda ns: -(ns[1] + k * ceiling.get(ns[0], 0.0)))

    fh_present = sorted((ns for ns in ranked if ns[0] in fh_names), key=lambda x: -x[1])
    fh_gk = fh_present[0:1]
    fh_strikers = fh_present[1:3]
    fh_extra = fh_present[3:]
    fh_striker_names = {n for n, _ in fh_strikers}

    if fh_gk:
        if fh_extra:
            print(f"NOTE: also on Free Hit but GK + both Striker slots "
                  f"already taken by higher-priority FH picks, left in the "
                  f"normal pool: {', '.join(n for n, _ in fh_extra)}")
        print(f"NOTE: {fh_gk[0][0]} is on Free Hit this GW -- forced into "
              f"GK (highest-priority individual-role slot).")
        for n, _ in fh_strikers:
            print(f"NOTE: {n} is on Free Hit this GW -- forced into "
                  f"Strikers (GK slot already taken by a higher-priority "
                  f"FH pick).")
        assigned = {fh_gk[0][0]} | fh_striker_names
        pool = [ns for ns in ranked if ns[0] not in assigned]
        remaining_striker_slots = 2 - len(fh_strikers)
        gk = fh_gk
        extra_strikers = by_ceiling(pool)[0:remaining_striker_slots]
        strikers = fh_strikers + extra_strikers
        assigned_extra = {n for n, _ in extra_strikers}
        remaining = [ns for ns in pool if ns[0] not in assigned_extra]  # still plain-score ordered
        squad = remaining[0:11]
        bench = remaining[11:13]
    else:
        top3 = by_ceiling(ranked)[0:3]
        gk = top3[0:1]
        strikers = top3[1:3]
        assigned = {n for n, _ in top3}
        remaining = [ns for ns in ranked if ns[0] not in assigned]  # still plain-score ordered
        squad = remaining[0:11]
        bench = remaining[11:13]

    print("\n=== Suggested SKLW lineup ===")
    print("\nStrikers:")
    for name, sc in strikers:
        if name in fh_striker_names:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc} (ceiling {ceiling.get(name, 0.0):.2f})")
    print("\nGoalkeeper:")
    for name, sc in gk:
        if fh_gk and name == fh_gk[0][0]:
            print(f"  {name}: FH")
        else:
            print(f"  {name}: {sc} (ceiling {ceiling.get(name, 0.0):.2f})")
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
    ap.add_argument("--wildcard-auto", metavar="MANAGER_NAMES",
                     help="build ONE optimal, budget-legal Wildcard squad "
                          "(max 3 players per real club, maximizing "
                          "projected best-XI+captain score via a MILP "
                          "solver) and apply it to ALL of these comma-"
                          "separated club members at once -- for when "
                          "several managers (e.g. all analytics-based) are "
                          "likely to land on close to the same squad "
                          "anyway, so there's no point typing out "
                          "--wildcard/--squad by hand for each one. "
                          "Requires 'pip install pulp'.")
    ap.add_argument("--budget", type=float, default=100.0,
                     help="total squad budget in millions for "
                          "--wildcard-auto (default 100.0, FPL's standard "
                          "starting budget)")
    ap.add_argument("--wc-horizon", type=int, default=5,
                     help="for --wildcard-auto: average projected points "
                          "across this many upcoming GWs (from the Solio "
                          "CSV's '<N>_Pts' columns, clamped to however "
                          "many it actually has) instead of just the next "
                          "single GW, since a Wildcard squad needs to hold "
                          "value over multiple weeks, not just once. "
                          "Default 5. Has no effect without a Solio CSV "
                          "loaded (ep_next only ever has next-GW data).")
    ap.add_argument("--xpoints", metavar="NAME=POINTS,...",
                     help="manually set the expected-points NUMBER used by "
                          "--wildcard-auto's optimizer for specific "
                          "players, overriding whatever ep_next/Solio "
                          "would have projected -- for when you don't "
                          "trust the auto projection for someone (back "
                          "from injury, a new signing with no track "
                          "record, a hunch). Comma-separated 'name=value' "
                          "pairs, e.g. --xpoints \"Haaland=15,Salah=12\".")
    ap.add_argument("--set-score", type=float, metavar="POINTS",
                     help="with --wildcard-auto: also set every listed "
                          "manager's final SKLW score for this GW to this "
                          "exact same number, instead of each getting "
                          "their own separately-computed best-xi score -- "
                          "for when they're all on an identical (or "
                          "near-identical) squad and you just want to "
                          "call it one number for all of them rather than "
                          "worry about small best-xi differences. e.g. "
                          "--wildcard-auto \"az,Classiic\" --set-score 65")
    ap.add_argument("--tc", metavar="MANAGER_NAME",
                     help="record a Triple Captain pick for this club "
                          "member -- which specific player they're "
                          "captaining (requires --captain). SKLW nets TC "
                          "to the same as a normal x2 captain (a third of "
                          "the tripled score is deducted), so this exists "
                          "purely so the RIGHT player gets doubled in the "
                          "projection -- a real TC pick is often a "
                          "deliberate differential, not necessarily the "
                          "squad's single highest-projected player, which "
                          "is what best-xi would otherwise auto-captain. "
                          "Stacks with --transfer/--wildcard for the same "
                          "manager.")
    ap.add_argument("--captain", metavar="PLAYER_NAME",
                     help="player name fragment being triple-captained "
                          "(with --tc)")
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
    ap.add_argument("--ceiling-weight", type=float, default=0.5, metavar="K",
                     help="ceiling-weight the GK/Strikers pick: rank the "
                          "top-3 pool by (projected score + K * stdev of "
                          "that manager's own real week-to-week GW score "
                          "history) instead of plain projected score. "
                          "Validated in backtest.py: mean + 0.5*stdev beats "
                          "plain mean at every K tested, UNCONDITIONALLY -- "
                          "not just when the club's an underdog, because "
                          "SKLW's own goal formula is convex (downside "
                          "capped at 0, upside an unbounded stepped "
                          "ladder), so higher variance raises EXPECTED "
                          "goals regardless of favourite/underdog status. "
                          "Default 0.5 (the backtested value). Pass 0 to "
                          "disable and fall back to plain top-3-by-mean "
                          "(also skips fetching score history, faster). "
                          "Squad/Bench selection is never affected -- only "
                          "the individual-role H2H slots.")
    ap.add_argument("--effective-ownership", action="store_true",
                     help="print each real player's Effective Ownership "
                          "(EO) across the club's 16 managers instead of "
                          "the normal lineup suggestion: EO% = (# managers "
                          "starting them + # managers CAPTAINING them) / "
                          "16 -- captaincy counts as an extra full share, "
                          "same convention as FPL's own EO stat, just "
                          "scoped to this club's 16 instead of the whole "
                          "game. A high-EO player is the club's template "
                          "pick (safe, low variance if you're relying on "
                          "them); a low-EO captain is a genuine "
                          "differential (risky, wide variance) -- useful "
                          "for spotting which manager's captain choice to "
                          "lean into for a Strikers slot when the matchup "
                          "calls for more variance, vs which is safest for "
                          "GK. Reads --mode/--overrides the same as the "
                          "normal run, so it reflects recorded transfers/ "
                          "wildcards/TC picks in preview mode, not just "
                          "live API data -- rerun after every --transfer "
                          "to keep it current.")
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

    if args.tc:
        if not args.captain:
            print("ERROR: --tc requires --captain \"player name\"")
            sys.exit(1)
        member_name, mid = resolve_manager(args.tc)
        record_tc(Path(args.overrides), bootstrap, member_name, mid, args.captain)
        print("Run 'run.bat --mode preview' separately to see the updated "
              "lineup once you're done recording chips/transfers.")
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
            parts = []
            if "wildcard" in entry:
                squad_names = ", ".join(
                    f"{players[i]['first_name']} {players[i]['second_name']}"
                    if i in players else f"#{i}" for i in entry["wildcard"])
                parts.append(f"WILDCARD [{squad_names}]")
            elif entry.get("out") or entry.get("in"):
                out_names = ", ".join(
                    f"{players[i]['first_name']} {players[i]['second_name']}"
                    if i in players else f"#{i}" for i in entry.get("out", []))
                in_names = ", ".join(
                    f"{players[i]['first_name']} {players[i]['second_name']}"
                    if i in players else f"#{i}" for i in entry.get("in", []))
                parts.append(f"OUT [{out_names}] -> IN [{in_names}]")
            if "tc_captain" in entry:
                cid = entry["tc_captain"]
                cname = (f"{players[cid]['first_name']} {players[cid]['second_name']}"
                          if cid in players else f"#{cid}")
                parts.append(f"TRIPLE CAPTAIN: {cname}")
            if "manual_score" in entry:
                parts.append(f"MANUAL SCORE: {entry['manual_score']}")
            if parts:
                print(f"  {manager_name}: " + "; ".join(parts))
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

    if args.wildcard_auto:
        member_pairs = [resolve_manager(n.strip())
                         for n in args.wildcard_auto.split(",") if n.strip()]
        if not member_pairs:
            print("ERROR: --wildcard-auto needs at least one manager name")
            sys.exit(1)
        opt_points = dict(points)
        if projections_path and projections_path.exists():
            horizon_points = load_solio_projections(projections_path, bootstrap, horizon=args.wc_horizon)
            opt_points.update(horizon_points)
            print(f"Building one optimal Wildcard squad (budget {args.budget}m, "
                  f"averaged over the next {args.wc_horizon} GW(s) of Solio "
                  f"projections where available, ep_next elsewhere)...")
        else:
            print(f"Building one optimal Wildcard squad (budget {args.budget}m) "
                  f"using single-GW ep_next only -- no Solio CSV loaded, so "
                  f"multi-GW averaging isn't available...")
        if args.xpoints:
            manual = parse_xpoints_overrides(bootstrap, args.xpoints)
            opt_points.update(manual)
            manual_str = ", ".join(
                f"{players[i]['first_name']} {players[i]['second_name']}={v}"
                for i, v in manual.items())
            print(f"Manual xPoints override(s) applied: {manual_str}")

        ids = build_optimal_wildcard_squad(bootstrap, opt_points, budget=round(args.budget * 10))
        squad_str = ", ".join(f"{players[i]['first_name']} {players[i]['second_name']}" for i in ids)
        print(f"Optimal squad: {squad_str}\n")
        ov_path = Path(args.overrides)
        for member_name, mid in member_pairs:
            save_wildcard_squad(ov_path, players, member_name, mid, ids)
            if args.set_score is not None:
                save_manual_score(ov_path, member_name, mid, args.set_score)
        print(f"\nSaved to {ov_path}")
        print("Run 'run.bat --mode preview' separately to see the updated "
              "lineup.")
        return

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

    if args.effective_ownership:
        ownership: dict[int, int] = {}
        captaincy: dict[int, int] = {}
        n = 0
        for name, mid in MANAGER_IDS.items():
            if args.mode == "preview" and str(mid) in overrides and "manual_score" in overrides[str(mid)]:
                print(f"  {name}: manual score override, no squad known -- skipped for EO")
                continue
            picks_data, gw_used = fetch_picks_with_fallback(mid, args.mode, last_finished_gw, next_gw)
            if picks_data is None:
                print(f"  {name}: could not fetch picks, skipped for EO")
                continue
            use_best_xi = args.best_xi or (gw_used != next_gw)
            forced_captain = None
            if args.mode == "preview" and str(mid) in overrides:
                entry = overrides[str(mid)]
                if "wildcard" in entry:
                    picks_data = apply_wildcard(picks_data, entry["wildcard"])
                    use_best_xi = True
                else:
                    picks_data = apply_overrides(picks_data, entry)
                if "tc_captain" in entry:
                    forced_captain = entry["tc_captain"]
                    use_best_xi = True
            starters, captain = starters_and_captain(picks_data, players, points, use_best_xi, forced_captain)
            n += 1
            for eid in starters:
                ownership[eid] = ownership.get(eid, 0) + 1
            if captain is not None:
                captaincy[captain] = captaincy.get(captain, 0) + 1

        if n == 0:
            print("ERROR: couldn't compute EO -- no manager's squad was fetchable.")
            return

        rows = []
        for eid in set(ownership) | set(captaincy):
            own = ownership.get(eid, 0)
            cap = captaincy.get(eid, 0)
            eo = (own + cap) / n * 100
            el = players.get(eid)
            pname = f"{el['first_name']} {el['second_name']}" if el else f"element #{eid}"
            rows.append((eo, own, cap, pname))
        rows.sort(reverse=True)

        print(f"\n=== Effective Ownership across {n} club managers ===")
        print(f"{'Player':<25} {'EO%':>7}   {'Started':>9}   {'Captained':>10}")
        for eo, own, cap, pname in rows:
            print(f"{pname:<25} {eo:>6.1f}%   {own:>6}/{n}   {cap:>7}/{n}")
        return

    if args.explain:
        member_name, mid = resolve_manager(args.explain)
        if args.mode == "preview" and str(mid) in overrides and "manual_score" in overrides[str(mid)]:
            print(f"\n=== {member_name} ===")
            print(f"Manual score override: {overrides[str(mid)]['manual_score']} "
                  f"(no picks fetched -- this bypasses the normal computation entirely)")
            return
        picks_data, gw_used = fetch_picks_with_fallback(mid, args.mode, last_finished_gw, next_gw)
        if picks_data is None:
            print(f"  {member_name}: could not fetch picks at all (bad manager ID?)")
            return
        forced_captain = None
        is_wildcard = False
        if args.mode == "preview" and str(mid) in overrides:
            entry = overrides[str(mid)]
            is_wildcard = "wildcard" in entry
            picks_data = apply_wildcard(picks_data, entry["wildcard"]) if is_wildcard \
                else apply_overrides(picks_data, entry)
            forced_captain = entry.get("tc_captain")
        print(f"\n=== {member_name} ===")
        if is_wildcard or forced_captain is not None:
            # apply_wildcard sets every pick's multiplier to 1 (bench
            # included, since a hand-typed squad has no real starter/
            # bench distinction of its own) -- explain_manager's plain
            # project_manager_score relies on multiplier>0 to find
            # starters, so it would silently sum all 15 instead of 11 for
            # a wildcard entry. Best-xi (what the main scoring loop
            # ALWAYS uses for a wildcard regardless of TC) is the correct
            # computation here, matching what actually feeds the lineup.
            starters, captain = pick_best_eleven(picks_data, players, points, forced_captain)
            score = project_best_xi_score(picks_data, players, points, forced_captain)
            cap_el = players.get(captain)
            cap_name = f"{cap_el['first_name']} {cap_el['second_name']}" if cap_el else "?"
            if forced_captain is not None:
                print(f"Triple Captain override -- captaining {cap_name}")
            else:
                print(f"Wildcard squad -- best-XI captain: {cap_name}")
            print("Best-XI squad:")
            for eid in starters:
                el = players.get(eid)
                pname = f"{el['first_name']} {el['second_name']}" if el else f"element #{eid}"
                cap = " (C, TC)" if eid == captain and forced_captain is not None else (" (C)" if eid == captain else "")
                print(f"  {pname}{cap}: {points.get(eid, 0.0):.2f}")
            bench = [p["element"] for p in picks_data["picks"] if p["element"] not in starters]
            print("Bench (not in best-XI, doesn't count):")
            for eid in bench:
                el = players.get(eid)
                pname = f"{el['first_name']} {el['second_name']}" if el else f"element #{eid}"
                print(f"  {pname}: {points.get(eid, 0.0):.2f}")
            tc_note = (" (SKLW nets Triple Captain to a normal x2 captain -- "
                       "no extra adjustment beyond doubling)") if forced_captain is not None else ""
            print(f"\nComputed score: {score}{tc_note}")
        else:
            explain_manager(picks_data, gw_used, players, points)
        return

    ceiling: dict[str, float] = {}
    if args.ceiling_weight != 0:
        print(f"Fetching each manager's own score history for ceiling-weighting "
              f"(k={args.ceiling_weight})...")
        for name, mid in MANAGER_IDS.items():
            ceiling[name] = fetch_manager_ceiling(mid)

    scores: list[tuple[str, float]] = []
    for name, mid in MANAGER_IDS.items():
        if args.mode == "preview" and str(mid) in overrides and "manual_score" in overrides[str(mid)]:
            score = overrides[str(mid)]["manual_score"]
            print(f"  {name}: manual score override, projected {score}")
            scores.append((name, score))
            continue

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

        forced_captain = None
        is_wildcard = False
        if args.mode == "preview" and str(mid) in overrides:
            entry = overrides[str(mid)]
            if "wildcard" in entry:
                picks_data = apply_wildcard(picks_data, entry["wildcard"])
                use_best_xi = True  # real starting-11/captain for a wildcarded squad isn't known
                is_wildcard = True
            else:
                picks_data = apply_overrides(picks_data, entry)
            if "tc_captain" in entry:
                forced_captain = entry["tc_captain"]
                use_best_xi = True  # need best-xi to actually force this specific captain

        if use_best_xi:
            score = project_best_xi_score(picks_data, players, points, forced_captain)
        else:
            score = project_manager_score(picks_data, points)
        tag = "best-xi" if use_best_xi else "actual picks"
        wc_tag = " WC" if is_wildcard else ""
        print(f"  {name} (GW{gw_used} squad, {tag}): projected {score}{wc_tag}")
        scores.append((name, score))

    suggest_lineup(scores, fh_names, ceiling, args.ceiling_weight)


if __name__ == "__main__":
    main()
