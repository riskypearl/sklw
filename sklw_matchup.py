"""SKLW match win-probability estimator.

Standalone script (same convention as draft_lineup.py/backtest.py -- small
helpers duplicated from sklw_lineup.py rather than imported, so this runs
on its own). Estimates the probability of one SKLW club beating another
in a given matchweek, given both clubs' 16 real FPL manager IDs and a
Solio-style (or ep_next) projection.

Why this needs more than just comparing two projected totals: a point
PROJECTION only gives you a mean, not a probability -- to get a genuine
win probability you need to know how much a real score typically varies
around that mean too. This uses real historical FPL data (the public
vaastav/Fantasy-Premier-League GitHub archive, the same source
backtest.py already uses and validated) to build an empirical
distribution of (actual - projected) per position -- GK/DEF/MID/FWD have
very different real volatility, attackers far more than defenders. Each
simulated matchweek redraws every starter's outcome from that
distribution around their CURRENT projection, applies SKLW's real H2H/
Squad goal rules (same formulas as backtest.py, kept identical), and
tallies which club wins across thousands of simulated draws.

Known limitation (v1): doesn't apply sklw_lineup.py's chip overrides
(--wildcard/--tc/etc from overrides.json) -- uses each manager's real/
fallback picks and best-xi as-is. A reasonable next step once this core
simulation is validated against a real matchup.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import statistics
import sys
import unicodedata
from pathlib import Path

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"
ARCHIVE_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"

# Same 16 club members as sklw_lineup.py's MANAGER_IDS (duplicated -- see
# module docstring on why this file doesn't import from it).
US_MANAGER_IDS: dict[str, int] = {
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


def get_json(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    return r.json()


def load_bootstrap() -> dict:
    return get_json(f"{FPL_BASE}/bootstrap-static/")


def player_lookup(bootstrap: dict) -> dict[int, dict]:
    return {p["id"]: p for p in bootstrap["elements"]}


def current_and_next_gw(bootstrap: dict) -> tuple[int, int]:
    """See sklw_lineup.py for the full reasoning -- identical logic."""
    events = bootstrap["events"]
    finished = [e for e in events if e["finished"]]
    current_ev = next((e for e in events if e["is_current"] and not e["finished"]), None)
    next_ev = next((e for e in events if e["is_next"]), None)
    last_finished_id = finished[-1]["id"] if finished else 0
    target_ev = current_ev or next_ev
    target_id = target_ev["id"] if target_ev else last_finished_id + 1
    return last_finished_id, target_id


def get_manager_picks(manager_id: int, gw: int) -> dict | None:
    try:
        return get_json(f"{FPL_BASE}/entry/{manager_id}/event/{gw}/picks/")
    except requests.HTTPError:
        return None


def fetch_picks_with_fallback(mid: int, last_finished_gw: int, next_gw: int) -> tuple[dict | None, int | None]:
    picks_data = get_manager_picks(mid, next_gw)
    gw_used = next_gw
    if picks_data is None and last_finished_gw > 0:
        picks_data = get_manager_picks(mid, last_finished_gw)
        gw_used = last_finished_gw
    return picks_data, gw_used


def fetch_live_stats(gw: int) -> dict[int, dict]:
    """Each player's REAL total_points AND minutes so far in gameweek
    `gw` -- 0 if their fixture hasn't started yet, running values while
    in progress, final once finished. Public endpoint, no login needed.
    'minutes' is needed (not just points) to predict automatic
    substitutions -- see predict_effective_lineup."""
    data = get_json(f"{FPL_BASE}/event/{gw}/live/")
    return {el["id"]: {"points": float(el["stats"]["total_points"]),
                        "minutes": int(el["stats"]["minutes"])}
            for el in data["elements"]}


def fetch_fixture_status(gw: int) -> dict[int, str]:
    """real team_id -> 'not_started' | 'in_progress' | 'finished' for
    gameweek `gw`. A team with no fixture that GW (blank gameweek)
    simply has no entry -- callers should default a missing key to
    'not_started'.

    Treats 'finished_provisional' as 'finished' -- FPL's real 'finished'
    flag doesn't flip true until bonus points are OFFICIALLY locked in,
    which can lag hours behind the match actually ending (confirmed via
    a real fixtures response: a match at minute 90 showed
    finished_provisional=true but finished=false). Waiting for the
    strict flag meant nothing was ever treated as genuinely over, which
    silently broke automatic-substitution prediction entirely (it
    requires a CONFIRMED finished fixture to treat a 0-minute player as
    a real blank rather than 'hasn't played yet') -- 'finished' still
    wins if somehow set without provisional having been set first."""
    fixtures = get_json(f"{FPL_BASE}/fixtures/?event={gw}")
    status: dict[int, str] = {}
    for f in fixtures:
        if f.get("finished") or f.get("finished_provisional"):
            s = "finished"
        elif f.get("started"):
            s = "in_progress"
        else:
            s = "not_started"
        status[f["team_h"]] = s
        status[f["team_a"]] = s
    return status


def build_live_locked(players: dict[int, dict], live_stats: dict[int, dict],
                       team_status: dict[int, str]) -> dict[int, float]:
    """Element IDs whose real outcome for this GW is already partly or
    fully known (their fixture has started or finished) -> their real
    points so far, for simulate_manager_score to use as a FIXED value
    instead of a projection + simulated residual -- once a match has
    happened, there's no reason to keep guessing at it. A player
    mid-match is locked to their CURRENT live score, not their eventual
    final one -- a deliberate simplification (doesn't model the
    remaining upside/downside left in that specific match), but still
    strictly more accurate than treating an in-progress player as if
    nothing had happened yet. Returns {} harmlessly if run well before
    the GW starts (every fixture 'not_started', nothing to lock)."""
    locked: dict[int, float] = {}
    for pid, el in players.items():
        status = team_status.get(el["team"], "not_started")
        if status in ("in_progress", "finished") and pid in live_stats:
            locked[pid] = live_stats[pid]["points"]
    return locked


def _outfield_formation_ok(type_counts: dict[int, int]) -> bool:
    d, mi, f = type_counts.get(2, 0), type_counts.get(3, 0), type_counts.get(4, 0)
    return 3 <= d <= 5 and 2 <= mi <= 5 and 1 <= f <= 3 and d + mi + f == 10


def predict_effective_lineup(picks_data: dict, live_stats: dict[int, dict],
                              team_status: dict[int, str],
                              players: dict[int, dict]) -> tuple[list[int], int | None]:
    """Predicts the EFFECTIVE starting-11 and captain FPL will settle on
    once this gameweek fully concludes. FPL only finalizes real
    automatic substitutions -- and a captain -> vice-captain transfer, if
    the real captain blanks -- once the ENTIRE gameweek is over, not
    progressively as individual matches finish. That means 'multiplier'/
    'is_captain' in live picks_data still reflect the ORIGINALLY declared
    lineup throughout a live gameweek, even once some players' own
    fixtures have already concluded with 0 minutes -- using them directly
    mid-gameweek silently keeps a confirmed blank and drops whoever
    should already be subbed in for them.

    This simulates FPL's own real auto-sub algorithm instead (same
    approach a live-tracking site like livefpl.net uses, but computed
    here directly from the public FPL API rather than depending on a
    third-party site): a player only counts as a CONFIRMED blank once
    their own fixture has actually finished with 0 minutes -- if their
    fixture is still in progress or hasn't started, they just haven't
    played yet, which isn't the same thing and shouldn't trigger a sub.
    GK blanks are replaced by the reserve GK if the reserve played;
    outfield blanks are replaced by the next eligible (already played)
    bench player in priority order, only if doing so keeps a legal
    formation (3-5 DEF, 2-5 MID, 1-3 FWD)."""
    picks_by_pos = {p["position"]: p for p in picks_data["picks"]}
    starters = [picks_by_pos[i]["element"] for i in range(1, 12) if i in picks_by_pos]
    bench = [picks_by_pos[i]["element"] for i in range(12, 16) if i in picks_by_pos]
    orig_captain = next((p["element"] for p in picks_data["picks"] if p["is_captain"]), None)
    orig_vice = next((p["element"] for p in picks_data["picks"] if p["is_vice_captain"]), None)

    def team_of(pid: int) -> int | None:
        return players[pid]["team"] if pid in players else None

    def confirmed_blank(pid: int) -> bool:
        status = team_status.get(team_of(pid), "not_started")
        return status == "finished" and live_stats.get(pid, {"minutes": 0})["minutes"] == 0

    def played(pid: int) -> bool:
        return live_stats.get(pid, {"minutes": 0})["minutes"] > 0

    effective = list(starters)
    used_bench: set[int] = set()

    gk = next((pid for pid in starters if players.get(pid, {}).get("element_type") == 1), None)
    reserve_gk = next((pid for pid in bench if players.get(pid, {}).get("element_type") == 1), None)
    if gk and confirmed_blank(gk) and reserve_gk and played(reserve_gk):
        effective[effective.index(gk)] = reserve_gk
        used_bench.add(reserve_gk)

    outfield_bench = [pid for pid in bench if players.get(pid, {}).get("element_type") != 1]
    for i, pid in enumerate(effective):
        if players.get(pid, {}).get("element_type") == 1:
            continue  # GK slot already handled above
        if not confirmed_blank(pid):
            continue
        for sub in outfield_bench:
            if sub in used_bench or not played(sub):
                continue
            candidate = effective.copy()
            candidate[i] = sub
            counts: dict[int, int] = {}
            for cid in candidate:
                t = players.get(cid, {}).get("element_type")
                if t and t != 1:
                    counts[t] = counts.get(t, 0) + 1
            if _outfield_formation_ok(counts):
                effective[i] = sub
                used_bench.add(sub)
                break

    captain = orig_captain
    if orig_captain is not None and confirmed_blank(orig_captain) and orig_vice and played(orig_vice):
        captain = orig_vice

    return effective, captain


def ep_next_points(players: dict[int, dict]) -> dict[int, float]:
    return {pid: float(p.get("ep_next") or 0.0) for pid, p in players.items()}


_EXTRA_FOLDS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
})


def _fold(s: str) -> str:
    s = s.translate(_EXTRA_FOLDS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def load_solio_projections(csv_path: Path, bootstrap: dict) -> dict[int, float]:
    """See sklw_lineup.py for full reasoning -- identical matching logic,
    single-GW column only (this tool scores one specific matchweek)."""
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}
    by_name: dict[str, list[dict]] = {}
    for p in bootstrap["elements"]:
        by_name.setdefault(_fold(p["web_name"]), []).append(p)

    points: dict[int, float] = {}
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
    """Considers BOTH 'solio.csv' in the current folder (if present and
    valid) AND the most recently downloaded matching CSV in Downloads,
    and returns whichever is NEWER by modification time -- a stale local
    solio.csv must not silently block a freshly downloaded one (see
    sklw_lineup.py's copy of this function for the full reasoning)."""
    candidates: list[Path] = []

    here = Path("solio.csv")
    if here.exists() and _is_solio_csv(here):
        candidates.append(here)

    downloads = Path.home() / "Downloads"
    if downloads.is_dir():
        for p in sorted(downloads.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True):
            if _is_solio_csv(p):
                candidates.append(p)
                break

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def pick_best_eleven(picks_data: dict, players: dict[int, dict],
                      points: dict[int, float],
                      forced_captain: int | None = None) -> tuple[list[int], int | None]:
    """See sklw_lineup.py for full reasoning -- identical formation-valid
    best-XI logic, including forced_captain support for --tc: a real
    Triple Captain pick may not be the squad's single highest-projected
    player, so it can't just be inferred from the formation optimization.
    If the forced pick isn't already in the computed XI, it's swapped in
    for the weakest starter in the SAME position group. Ignored (with a
    warning) if the pick isn't even in this manager's squad at all."""
    squad_ids = {p["element"] for p in picks_data["picks"]}
    if forced_captain is not None and forced_captain not in squad_ids:
        print(f"WARNING: Triple Captain pick (element #{forced_captain}) isn't "
              f"in this manager's squad -- ignoring forced captain")
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


def apply_overrides(picks_data: dict, override: dict) -> dict:
    """See sklw_lineup.py for full reasoning -- identical out/in swap."""
    picks = [dict(p) for p in picks_data["picks"]]
    swap = dict(zip(override.get("out", []), override.get("in", [])))
    for p in picks:
        if p["element"] in swap:
            p["element"] = swap[p["element"]]
    return {**picks_data, "picks": picks}


def apply_wildcard(picks_data: dict, wildcard_ids: list[int]) -> dict:
    """See sklw_lineup.py for full reasoning -- replaces the entire squad."""
    picks = [{"element": eid} for eid in wildcard_ids]
    return {**picks_data, "picks": picks, "active_chip": "wildcard"}


POSITION_NAMES = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch_season_rows(season: str) -> list[dict]:
    url = f"{ARCHIVE_BASE}/{season}/gws/merged_gw.csv"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def build_position_residuals(rows: list[dict]) -> dict[str, list[float]]:
    """Real historical (actual total_points - FPL's own historical xP) per
    position, from the vaastav archive (same source and field backtest.py
    already validated) -- an empirical, data-grounded stand-in for 'how
    wrong does a point projection typically turn out to be for a player
    in this position'. Used as the FALLBACK when a player's real team has
    no usable data for the chosen shock gameweek (see
    build_team_gw_residuals) -- attacking positions have far fatter tails
    (a MID/FWD can blank OR haul; a DEF's ceiling is much more capped),
    so this must be split by position rather than pooled."""
    residuals: dict[str, list[float]] = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for row in rows:
        try:
            pos = row["position"]
            actual = float(row["total_points"])
            proj = float(row["xP"])
        except (ValueError, KeyError):
            continue
        if pos in residuals:
            residuals[pos].append(actual - proj)
    return residuals


def build_team_gw_residuals(rows: list[dict]) -> dict[tuple[str, int], dict[str, list[float]]]:
    """Real historical (actual - xP), grouped by (real team, GW) AND
    position -- e.g. Arsenal's GW14 defenders. Measured evidence (see
    calibrate_matchup.py / README): same-team same-GW residuals correlate
    at ~0.14 (a team has a good or bad day together -- shared clean
    sheet, shared goals, shared bonus points), vs ~0.03 pooled across the
    whole league that GW and ~0 between opposing teams in the same
    fixture -- so TEAM is the unit that actually needs to be correlated.
    Used for a team-scoped block bootstrap: for one simulated trial, draw
    ONE historical (team, GW) per real team involved, and every player
    from that team draws their residual from THAT SAME historical block.
    Verified via calibrate_matchup.py's out-of-sample calibration check
    to be a genuine improvement (not just a plausible-sounding idea) --
    see the README for the ablation result."""
    blocks: dict[tuple[str, int], dict[str, list[float]]] = {}
    for row in rows:
        try:
            team = row["team"]
            gw = int(row["GW"])
            pos = row["position"]
            actual = float(row["total_points"])
            proj = float(row["xP"])
        except (ValueError, KeyError):
            continue
        if pos not in ("GK", "DEF", "MID", "FWD"):
            continue
        key = (team, gw)
        blocks.setdefault(key, {"GK": [], "DEF": [], "MID": [], "FWD": []})[pos].append(actual - proj)
    return blocks


def build_team_gw_index(blocks: dict[tuple[str, int], dict[str, list[float]]]) -> dict[str, list[int]]:
    """team -> list of GWs that team has a block for."""
    index: dict[str, list[int]] = {}
    for team, gw in blocks:
        index.setdefault(team, []).append(gw)
    return index


def pick_team_shocks(rng: random.Random, teams_needed: set[str],
                      team_gw_index: dict[str, list[int]]) -> dict[str, int | None]:
    """One random historical GW per real team, shared by every player
    from that team within a single simulated trial."""
    return {team: (rng.choice(team_gw_index[team]) if team_gw_index.get(team) else None)
            for team in teams_needed}


def load_club_roster(clubs_path: str, club_name: str) -> dict[str, int]:
    """Looks up a club's 16-manager roster by name from a local clubs.json
    (built by build_clubs_json.py from the league's master list, kept
    OUT of git -- see that script and .gitignore -- since it maps real
    FPL IDs to hundreds of other real managers across the league, not
    just this club's own roster). Format: {"Club Name": {"Manager1":
    id1, ...}, ...} -- generic placeholder names, no real handles/names,
    same as an ad-hoc --them-file would use. Case-insensitive substring
    match on club name; errors clearly (not a silent guess) if it's
    missing, malformed, or the name doesn't match exactly one club."""
    path = Path(clubs_path)
    if not path.exists():
        print(f"ERROR: no clubs.json at {clubs_path} -- run build_clubs_json.py "
              f"against your league's master list CSV first (see README), "
              f"or use --them/--them-file instead of --opponent.")
        sys.exit(1)
    try:
        clubs = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {clubs_path} isn't valid JSON ({e}) -- rebuild it with "
              f"build_clubs_json.py.")
        sys.exit(1)
    folded = club_name.strip().lower()
    matches = [name for name in clubs if folded in name.lower()]
    if len(matches) == 0:
        print(f"ERROR: no club matching '{club_name}' found in {clubs_path}. "
              f"Known clubs: {', '.join(sorted(clubs))}")
        sys.exit(1)
    if len(matches) > 1:
        print(f"ERROR: '{club_name}' matched {len(matches)} clubs, need exactly 1: "
              f"{', '.join(sorted(matches))}")
        sys.exit(1)
    return {str(k): int(v) for k, v in clubs[matches[0]].items()}


def resolve_roster(spec: str | None, file_path: str | None, label: str) -> dict[str, int]:
    """Builds a {name: manager_id} roster from either --them/--us inline
    ('Name:ID,Name:ID,...') or a JSON file ({'Name': id, ...}) -- exactly
    one of spec/file_path should be given."""
    if file_path:
        data = json.loads(Path(file_path).read_text())
        return {str(k): int(v) for k, v in data.items()}
    if spec:
        roster: dict[str, int] = {}
        for pair in [p.strip() for p in spec.split(",") if p.strip()]:
            if ":" not in pair:
                print(f"ERROR: --{label} entries must be 'Name:ID', got '{pair}'")
                sys.exit(1)
            name, mid = pair.rsplit(":", 1)
            try:
                roster[name.strip()] = int(mid.strip())
            except ValueError:
                print(f"ERROR: --{label} id for '{name.strip()}' isn't a number: '{mid.strip()}'")
                sys.exit(1)
        return roster
    return {}


def resolve_role_ids(spec: str | None, roster: dict[str, int], label: str) -> set[str]:
    """Resolves a comma-separated list of FPL manager IDs (--them-gk-id/
    --them-strikers-ids/--them-bench-ids, or the --us- equivalents) to
    the matching names in `roster`, for assign_roles' forced_gk/
    forced_strikers/forced_bench. IDs rather than names since --them-file/
    --us-file names may just be arbitrary placeholder labels (no known
    real names yet) -- matching by ID is unambiguous either way."""
    if not spec:
        return set()
    id_to_name = {v: k for k, v in roster.items()}
    names: set[str] = set()
    for raw in [p.strip() for p in spec.split(",") if p.strip()]:
        try:
            mid = int(raw)
        except ValueError:
            print(f"ERROR: --{label} entries must be numeric manager IDs, got '{raw}'")
            sys.exit(1)
        if mid not in id_to_name:
            print(f"WARNING: --{label} id {mid} isn't in this roster, ignored")
            continue
        names.add(id_to_name[mid])
    return names


def prompt_for_role_ids(current: str | None, prompt_text: str, no_prompt: bool) -> str | None:
    """Falls back to an interactive input() for a GK/Strikers ID flag
    that wasn't given on the command line -- --them-gk-id/--them-
    strikers-ids/--us-gk-id/--us-strikers-ids are useful the moment
    you've actually scouted a real declared lineup, but remembering the
    exact flag name and re-typing the whole command each time is
    friction most captains won't bother with mid-week. Skipped
    entirely (returns `current` unchanged) if a value was already given
    via the flag, --no-prompt is set, or stdin isn't interactive (a
    redirected/scripted run shouldn't hang waiting for input)."""
    if current or no_prompt or not sys.stdin.isatty():
        return current
    answer = input(prompt_text).strip()
    return answer or None


def build_club_scores(roster: dict[str, int], players: dict[int, dict], points: dict[int, float],
                       last_finished_gw: int, next_gw: int, overrides: dict[str, dict],
                       live_stats: dict[int, dict], team_status: dict[int, str]
                       ) -> tuple[dict[str, dict], list[str]]:
    """Fetches each of the 16 managers' picks and returns per-manager
    {'starters': [...], 'captain': id, 'projected': float} plus a list of
    any manager names that failed to fetch (skipped).

    Whenever the manager's REAL picks for the target GW are actually
    confirmed (gw_used == next_gw, deadline already passed), their
    EFFECTIVE starting-11 and captain are predicted rather than guessed
    at via best-xi -- accounting for automatic substitutions and a
    possible captain -> vice transfer that FPL itself won't reflect in
    the raw picks data until the entire gameweek is over (see
    predict_effective_lineup). Bench Boost is the one exception, where
    the originally declared 'position'/'is_captain' are used directly
    (SKLW overrides real FPL's own BB rule so bench never counts, and
    there's no bench to sub in anyway once BB is active). This matters a
    lot once a real GW is underway: a manager's real captain (including a
    real Triple Captain) might not be whoever the highest-projected
    player in their squad happens to be, and once their game has partly
    or fully played out (see build_live_locked), doubling the WRONG
    player instead of their real one would silently produce a wrong
    'real score so far'. SKLW nets Triple Captain down to exactly a
    normal x2 captain regardless (see sklw_lineup.py) -- since this
    always applies a flat x2 to whichever player was REALLY captained,
    that net effect falls out automatically with no separate chip-
    specific adjustment needed.
    overrides.json's --wildcard/--transfer/--tc/--set-score only apply
    when real picks AREN'T available yet (an older/fallback squad is
    being used as a pre-deadline planning proxy) -- once real, confirmed
    picks exist, they're authoritative and an override recorded for
    earlier planning purposes is stale, so it's ignored (same real-vs-
    proxy split sklw_lineup.py's --mode final/preview already makes). A
    'manual_score' override always short-circuits everything regardless
    -- the manager told us the number directly, no guessing needed."""
    club: dict[str, dict] = {}
    failed: list[str] = []
    for name, mid in roster.items():
        entry = overrides.get(str(mid), {})

        if "manual_score" in entry:
            score = float(entry["manual_score"])
            club[name] = {"starters": [], "captain": None, "projected": score, "manual_score": score}
            continue

        picks_data, gw_used = fetch_picks_with_fallback(mid, last_finished_gw, next_gw)
        if picks_data is None:
            if "wildcard" in entry:
                picks_data = {"picks": []}  # wildcard replaces the squad entirely, no base needed
                gw_used = None
            else:
                failed.append(name)
                continue

        if gw_used == next_gw:
            chip = picks_data.get("active_chip")
            if chip == "bboost":
                # SKLW overrides real FPL's own BB rule -- bench still
                # doesn't count -- so 'position' (the originally declared
                # lineup) is the right signal here regardless of subs,
                # since BB sets EVERY pick's multiplier to 1 including
                # bench and there's no bench to ever sub in anyway.
                starters = [p["element"] for p in picks_data["picks"] if p["position"] <= 11]
                captain = next((p["element"] for p in picks_data["picks"] if p["is_captain"]), None)
            else:
                # FPL only finalizes real automatic substitutions (and a
                # captain -> vice transfer) once the ENTIRE gameweek is
                # over, not progressively -- so mid-gameweek, neither
                # 'multiplier' nor 'is_captain'/'position' yet reflect a
                # sub that's already effectively locked in (a starter's
                # own fixture already finished with 0 minutes). Predict
                # what FPL will settle on instead of waiting for it.
                starters, captain = predict_effective_lineup(picks_data, live_stats, team_status, players)
        else:
            if "wildcard" in entry:
                picks_data = apply_wildcard(picks_data, entry["wildcard"])
            elif entry.get("out") or entry.get("in"):
                picks_data = apply_overrides(picks_data, entry)
            starters, captain = pick_best_eleven(picks_data, players, points, entry.get("tc_captain"))

        if not starters:
            failed.append(name)
            continue
        projected = sum(points.get(pid, 0.0) for pid in starters)
        if captain is not None:
            projected += points.get(captain, 0.0)
        club[name] = {"starters": starters, "captain": captain, "projected": projected}
    return club, failed


def assign_roles(club: dict[str, dict], forced_gk: str | None = None,
                  forced_strikers: set[str] | None = None,
                  forced_bench: set[str] | None = None,
                  fh_names: set[str] | None = None) -> dict[str, list[str]]:
    """Same rule as sklw_lineup.py's suggest_lineup: top projected -> GK,
    next 2 -> Strikers, next 11 -> Squad, rest -> Bench. GK gets priority
    over Strikers: a strong GK score is compared against BOTH of the
    opponent's Strikers (denying goals in 2 H2H battles at once), while
    a strong Striker score only wins its own single battle -- see
    backtest.py's net-goal-differential re-validation and the README's
    rules section. Fixed ONCE from projections (decision-time), same as
    a real captain would submit -- the Monte Carlo layer below only
    varies the OUTCOME given this fixed assignment, not the assignment
    itself.

    forced_gk/forced_strikers/forced_bench: manager names KNOWN to
    actually hold that real role this week (e.g. scouted from a club's
    real declared lineup, rather than just assumed from projections) --
    pinned regardless of projection, and take priority over fh_names
    below (real knowledge beats an inference from chip status alone).
    Whichever of the 16 aren't pinned to anything are ranked as normal
    and fill the remaining GK (if unpinned)/Striker/Squad/Bench slots. A
    name pinned to more than one role is resolved by priority GK >
    Strikers > Bench (arbitrary but deterministic) rather than erroring.
    Extra names beyond a role's slot count are ignored (with a warning).

    fh_names: managers on Free Hit this GW, prioritized into whichever
    of GK/Strikers isn't already filled by forced_gk/forced_strikers,
    same order and reasoning as sklw_lineup.py's --fh (GK first, then
    Strikers -- GK is the scarcer/more valuable individual-role slot).
    An earlier version of this function had no FH handling at all,
    silently letting a Free Hit manager's often-unrepresentative
    projection land them anywhere including Bench, unlike
    sklw_lineup.py's suggested lineup for the same real matchup."""
    forced_strikers = set(forced_strikers or [])
    forced_bench = set(forced_bench or [])
    fh_names = set(fh_names or [])

    if forced_gk is not None and forced_gk not in club:
        print(f"WARNING: forced-GK name not found in this club, ignored: {forced_gk}")
        forced_gk = None
    unknown_s = forced_strikers - set(club)
    if unknown_s:
        print(f"WARNING: forced-striker name(s) not found in this club, ignored: {', '.join(sorted(unknown_s))}")
        forced_strikers -= unknown_s
    if len(forced_strikers) > 2:
        extra = sorted(forced_strikers)[2:]
        print(f"WARNING: more than 2 forced-striker names given, ignoring: {', '.join(extra)}")
        forced_strikers = set(sorted(forced_strikers)[:2])
    unknown_b = forced_bench - set(club)
    if unknown_b:
        print(f"WARNING: forced-bench name(s) not found in this club, ignored: {', '.join(sorted(unknown_b))}")
        forced_bench -= unknown_b
    if len(forced_bench) > 2:
        extra = sorted(forced_bench)[2:]
        print(f"WARNING: more than 2 forced-bench names given, ignoring: {', '.join(extra)}")
        forced_bench = set(sorted(forced_bench)[:2])

    forced_strikers -= {forced_gk} if forced_gk else set()
    forced_bench -= ({forced_gk} if forced_gk else set()) | forced_strikers

    unknown_fh = fh_names - set(club)
    if unknown_fh:
        print(f"WARNING: FH name(s) not found in this club, ignored: {', '.join(sorted(unknown_fh))}")
        fh_names -= unknown_fh
    fh_names -= forced_bench  # a real known Bench placement beats an FH guess entirely

    fh_gk = None
    fh_strikers: set[str] = set()
    if forced_gk is None:
        fh_gk_pool = sorted(fh_names - forced_strikers, key=lambda n: -club[n]["projected"])
        if fh_gk_pool:
            fh_gk = fh_gk_pool[0]
    striker_slots_left = 2 - len(forced_strikers)
    if striker_slots_left > 0:
        fh_striker_pool = sorted(fh_names - forced_strikers - ({fh_gk} if fh_gk else set()),
                                  key=lambda n: -club[n]["projected"])
        fh_strikers = set(fh_striker_pool[:striker_slots_left])

    forced_all = ({forced_gk} if forced_gk else set()) | forced_strikers | forced_bench
    ranked = sorted((n for n in club if n not in forced_all), key=lambda n: -club[n]["projected"])

    if forced_gk:
        gk = [forced_gk]
    elif fh_gk:
        gk = [fh_gk]
        ranked.remove(fh_gk)
    else:
        gk = [ranked.pop(0)]

    strikers = list(forced_strikers)
    if fh_strikers:
        strikers += list(fh_strikers)
        for n in fh_strikers:
            ranked.remove(n)
    need = 2 - len(strikers)
    if need > 0:
        strikers += ranked[:need]
        del ranked[:need]

    bench = list(forced_bench)
    need = 2 - len(bench)
    if need > 0:
        bench += ranked[-need:]
        del ranked[-need:]

    return {"gk": gk, "strikers": strikers, "squad": ranked, "bench": bench}


def h2h_goals(a_actual: float, b_actual: float) -> int:
    """SKLW's own rule: 1 goal per full 20-point margin, zero if b
    equals/outscores a. Identical to backtest.py's h2h_goals."""
    margin = a_actual - b_actual
    return int(margin // 20) + 1 if margin >= 1 else 0


def squad_goals(a_total: float, b_total: float) -> int:
    """SKLW's own rule: a goal for beating the opponent at all, +1 more
    per full 30-point margin beyond that (same base+bonus shape as
    h2h_goals, just a wider band) -- margin // 30 + 1 if margin >= 1
    else 0. This IS the rules doc's literal wording; an earlier version
    of this code dropped the '+1' after one real result seemed to
    require it, but that was compensating for a DIFFERENT bug (see
    match_goals_breakdown) -- confirmed against 4 independent real SKLW
    results that this formula is correct once that other bug is fixed."""
    margin = a_total - b_total
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals_breakdown(scores: dict[str, float], roles: dict[str, list[str]],
                           opp_scores: dict[str, float],
                           opp_roles: dict[str, list[str]]) -> dict[str, int]:
    """Goals FOR the 'scores' side, split by which battle they came from
    -- our Strikers vs their GK, our Squad vs their Squad. Lets the final
    report show WHERE a match is likely to be won or lost, not just the
    final tally. match_goals() is just the sum of these two.

    The GK does NOT independently score goals of its own -- only
    Strikers score, against the opposing GK. A GK's role is purely to be
    a high-scoring target that's hard for the opponent's Strikers to
    beat; that's already fully captured by the Strikers' own H2H
    calculation on the OTHER side (their Strikers vs OUR GK counts as
    goals FOR THEM, not as a separate deduction or GK-goal on our side).
    An earlier version of this code gave the GK a mirrored scoring
    mechanic against the opposing Strikers too, which is confirmed WRONG
    against 4 independent real SKLW results -- removing it (and only it)
    was what made all 4 reproduce exactly; keeping it double-counts the
    same individual battle as goals for BOTH sides at once, which can
    silently make the computed total exceed what either side could
    actually have scored."""
    striker_goals = sum(h2h_goals(scores[name], opp_scores[opp_roles["gk"][0]])
                         for name in roles["strikers"])
    own_squad = sum(scores[n] for n in roles["squad"])
    opp_squad = sum(opp_scores[n] for n in opp_roles["squad"])
    squad_g = squad_goals(own_squad, opp_squad)
    return {"strikers": striker_goals, "squad": squad_g}


def match_goals(scores: dict[str, float], roles: dict[str, list[str]],
                 opp_scores: dict[str, float], opp_roles: dict[str, list[str]]) -> int:
    """Goals FOR the 'scores' side. Same formula as backtest.py's
    match_goals -- call twice with sides swapped to get both scorelines."""
    return sum(match_goals_breakdown(scores, roles, opp_scores, opp_roles).values())


def simulate_manager_score(rng: random.Random, info: dict, players: dict[int, dict],
                            points: dict[int, float], team_names: dict[int, str],
                            team_shocks: dict[str, int | None],
                            team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                            fallback_residuals: dict[str, list[float]],
                            live_locked: dict[int, float] | None = None) -> float:
    """One simulated real GW score for a manager: each starter's
    projected mean plus a resampled historical residual for their
    position, captain doubled AFTER adding the residual (matches how a
    real captain multiplier applies to whatever they actually score, not
    to the pre-match projection). The residual comes from that player's
    real team's shared shock block for this trial where available
    (team_shocks/team_gw_residuals -- see build_team_gw_residuals),
    falling back to the plain pooled position distribution for a team
    with no usable data that GW. A 'manual_score' override (--set-score
    in sklw_lineup.py) is a fixed, zero-variance number every trial --
    the manager told us the number directly, nothing to simulate.

    live_locked: element IDs whose real gameweek has already started or
    finished (see build_live_locked) use their REAL points instead of a
    projection+residual -- no need to keep guessing at a match that's
    already happened."""
    if "manual_score" in info:
        return info["manual_score"]

    live_locked = live_locked or {}
    total = 0.0
    for pid in info["starters"]:
        if pid in live_locked:
            score = live_locked[pid]
        else:
            el = players.get(pid)
            pos = POSITION_NAMES.get(el["element_type"], "MID") if el else "MID"
            team = team_names.get(el["team"]) if el else None
            shock_gw = team_shocks.get(team) if team else None
            pool = team_gw_residuals.get((team, shock_gw), {}).get(pos) if shock_gw is not None else None
            if not pool:
                pool = fallback_residuals[pos]
            projected = points.get(pid, 0.0)
            score = projected + rng.choice(pool)
        if pid == info["captain"]:
            score *= 2
        total += score
    return total


def run_simulation(rng: random.Random, us_club: dict[str, dict], us_roles: dict[str, list[str]],
                    them_club: dict[str, dict], them_roles: dict[str, list[str]],
                    players: dict[int, dict], points: dict[int, float], team_names: dict[int, str],
                    team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                    team_gw_index: dict[str, list[int]],
                    fallback_residuals: dict[str, list[float]], sims: int,
                    live_locked: dict[int, float] | None = None) -> dict:
    all_starter_ids = [pid for info in list(us_club.values()) + list(them_club.values())
                        for pid in info["starters"]]
    teams_needed = {team_names[players[pid]["team"]] for pid in all_starter_ids
                     if pid in players and players[pid]["team"] in team_names}

    wins = draws = losses = 0
    our_goals_total = their_goals_total = 0
    our_breakdown_total = {"strikers": 0, "squad": 0}
    their_breakdown_total = {"strikers": 0, "squad": 0}
    for _ in range(sims):
        team_shocks = pick_team_shocks(rng, teams_needed, team_gw_index)
        our_scores = {n: simulate_manager_score(rng, info, players, points, team_names,
                                                 team_shocks, team_gw_residuals, fallback_residuals,
                                                 live_locked)
                      for n, info in us_club.items()}
        their_scores = {n: simulate_manager_score(rng, info, players, points, team_names,
                                                    team_shocks, team_gw_residuals, fallback_residuals,
                                                    live_locked)
                         for n, info in them_club.items()}
        our_breakdown = match_goals_breakdown(our_scores, us_roles, their_scores, them_roles)
        their_breakdown = match_goals_breakdown(their_scores, them_roles, our_scores, us_roles)
        our_goals = sum(our_breakdown.values())
        their_goals = sum(their_breakdown.values())
        our_goals_total += our_goals
        their_goals_total += their_goals
        for k in our_breakdown_total:
            our_breakdown_total[k] += our_breakdown[k]
            their_breakdown_total[k] += their_breakdown[k]
        if our_goals > their_goals:
            wins += 1
        elif our_goals < their_goals:
            losses += 1
        else:
            draws += 1
    return {
        "win_pct": 100 * wins / sims,
        "draw_pct": 100 * draws / sims,
        "loss_pct": 100 * losses / sims,
        "avg_goals_for": our_goals_total / sims,
        "avg_goals_against": their_goals_total / sims,
        "avg_breakdown_for": {k: v / sims for k, v in our_breakdown_total.items()},
        "avg_breakdown_against": {k: v / sims for k, v in their_breakdown_total.items()},
    }


def current_known_scores(club: dict[str, dict],
                          live_locked: dict[int, float]) -> tuple[dict[str, float], dict[str, int]]:
    """Per-manager score using ONLY already-known real results (live or
    finished, via live_locked) -- 0 contribution for any starter whose
    match hasn't started yet, since that's genuinely unknown, not a
    guess. Also returns each manager's pending starter count, so it's
    clear how much of that manager's score isn't decided yet. A
    'manual_score' manager is fully known already (a human decision, not
    a projection) -- 0 pending."""
    known: dict[str, float] = {}
    pending: dict[str, int] = {}
    for name, info in club.items():
        if "manual_score" in info:
            known[name] = info["manual_score"]
            pending[name] = 0
            continue
        total = 0.0
        left = 0
        for pid in info["starters"]:
            if pid in live_locked:
                score = live_locked[pid]
                if pid == info["captain"]:
                    score *= 2
                total += score
            else:
                left += 1
        known[name] = total
        pending[name] = left
    return known, pending


def pending_starter_names(club: dict[str, dict], players: dict[int, dict],
                           live_locked: dict[int, float]) -> dict[str, list[str]]:
    """Per-manager list of starter NAMES whose match hasn't started yet
    (not in live_locked) -- the actual players behind
    current_known_scores' pending COUNT. Requested live: seeing "104 of
    our starters, 104 of theirs" pending doesn't say WHO -- e.g. a club
    leading on already-known results because more of its players
    happen to have early kickoffs isn't visible from a bare count."""
    out: dict[str, list[str]] = {}
    for name, info in club.items():
        if "manual_score" in info:
            out[name] = []
            continue
        names = []
        for pid in info["starters"]:
            if pid not in live_locked:
                el = players.get(pid)
                names.append(f"{el['first_name']} {el['second_name']}" if el else f"element #{pid}")
        out[name] = names
    return out


def print_roles(label: str, club: dict[str, dict], roles: dict[str, list[str]]) -> None:
    print(f"\n{label} suggested roles (by projection):")
    print(f"  GK:       {roles['gk'][0]} ({club[roles['gk'][0]]['projected']:.1f})")
    for n in roles["strikers"]:
        print(f"  Striker:  {n} ({club[n]['projected']:.1f})")
    squad_total = sum(club[n]["projected"] for n in roles["squad"])
    print(f"  Squad:    {', '.join(roles['squad'])} (total {squad_total:.1f})")
    print(f"  Bench:    {', '.join(roles['bench'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--them", metavar="NAME:ID,...",
                     help="opponent club's 16 manager names/IDs inline, "
                          "e.g. --them \"Alice:111,Bob:222,...\"")
    ap.add_argument("--them-file", metavar="PATH",
                     help="path to a JSON file {'Name': id, ...} with the "
                          "opponent club's 16 managers -- easier than "
                          "typing 16 pairs inline. Exactly one of --them/"
                          "--them-file/--opponent is required.")
    ap.add_argument("--opponent", metavar="CLUB_NAME",
                     help="look up the opponent's 16 managers by club name "
                          "from a local clubs.json instead of pasting IDs "
                          "by hand -- build clubs.json once with "
                          "build_clubs_json.py against your league's "
                          "master list CSV (see README; kept out of git, "
                          "not committed). Case-insensitive substring "
                          "match. Alternative to --them/--them-file.")
    ap.add_argument("--clubs-file", default="clubs.json",
                     help="path to clubs.json for --opponent (default: "
                          "clubs.json in the current folder)")
    ap.add_argument("--us", metavar="NAME:ID,...",
                     help="override our own 16 managers (defaults to the "
                          "same roster as sklw_lineup.py's MANAGER_IDS)")
    ap.add_argument("--us-file", metavar="PATH", help="like --them-file, for our own roster")
    ap.add_argument("--them-gk-id", metavar="ID",
                     help="if you know the opponent's REAL declared GK for "
                          "this matchup, pin that one manager ID to GK "
                          "instead of letting the tool assume an optimal "
                          "assignment for them too.")
    ap.add_argument("--them-strikers-ids", metavar="ID,ID",
                     help="same idea for the opponent's real 2 Strikers. "
                          "Comma-separated FPL manager IDs.")
    ap.add_argument("--them-bench-ids", metavar="ID,ID",
                     help="if you know the opponent's REAL declared Bench "
                          "for this matchup (e.g. scouted from their "
                          "lineup), pin those 2 manager IDs to Bench "
                          "instead of letting the tool assume an optimal "
                          "assignment for them too. Comma-separated FPL "
                          "manager IDs, e.g. --them-bench-ids "
                          "\"1859490,41471\".")
    ap.add_argument("--us-gk-id", metavar="ID", help="same as --them-gk-id, for our own club")
    ap.add_argument("--us-strikers-ids", metavar="ID,ID",
                     help="same as --them-strikers-ids, for our own club")
    ap.add_argument("--us-bench-ids", metavar="ID,ID",
                     help="same as --them-bench-ids, for our own club")
    ap.add_argument("--us-fh-id", action="append", metavar="ID",
                     help="mark this of our own club members (by FPL "
                          "manager ID, same convention as --us-gk-id "
                          "etc.) as playing Free Hit this GW -- "
                          "prioritized into GK first, then Strikers "
                          "(same rule as sklw_lineup.py's --fh), ahead "
                          "of plain projection ranking. Repeat for "
                          "multiple managers. Beaten by an explicit "
                          "--us-gk-id/--us-strikers-ids/--us-bench-ids "
                          "pin for the same manager -- real scouted "
                          "knowledge wins over an FH-based guess.")
    ap.add_argument("--them-fh-id", action="append", metavar="ID",
                     help="same as --us-fh-id, for an opponent manager. "
                          "Repeat for multiple managers.")
    ap.add_argument("--no-prompt", action="store_true",
                     help="skip the interactive GK/Strikers ID prompts "
                          "below (e.g. for a non-interactive/scripted "
                          "run) -- unpinned managers just get the "
                          "assumed-optimal projection-based assignment "
                          "as normal.")
    ap.add_argument("--projections", metavar="CSV_PATH",
                     help="Solio-style projections CSV (see sklw_lineup.py). "
                          "Defaults to auto-detecting solio.csv / Downloads, "
                          "falling back to ep_next for anyone not matched.")
    ap.add_argument("--season", default="2023-24",
                     help="historical season (vaastav archive folder) used "
                          "to build the position-based score-variance model")
    ap.add_argument("--sims", type=int, default=5000, help="number of Monte Carlo trials")
    ap.add_argument("--seed", type=int, default=None, help="fix the random seed for reproducible runs")
    ap.add_argument("--overrides", default="overrides.json",
                     help="path to sklw_lineup.py's overrides.json -- any "
                          "--wildcard/--transfer/--tc/--set-score recorded "
                          "there is applied here too (matched by manager "
                          "ID, works for either roster).")
    ap.add_argument("--explain-manager", metavar="NAME",
                     help="print a player-by-player breakdown for one "
                          "manager (their real starting-11, real captain, "
                          "and each player's live/locked points if their "
                          "match has started) instead of running the full "
                          "simulation -- for checking a score against "
                          "their actual FPL page when something looks "
                          "wrong. Name must match a label in --them/"
                          "--them-file/--us/--us-file.")
    args = ap.parse_args()

    if not args.them and not args.them_file and not args.opponent:
        print("ERROR: need --them \"Name:ID,...\", --them-file path.json, "
              "or --opponent \"Club Name\" for the opponent club's 16 managers")
        sys.exit(1)

    if args.opponent:
        them_roster = load_club_roster(args.clubs_file, args.opponent)
    else:
        them_roster = resolve_roster(args.them, args.them_file, "them")
    us_roster = resolve_roster(args.us, args.us_file, "us") or US_MANAGER_IDS
    if len(them_roster) != 16:
        print(f"WARNING: opponent roster has {len(them_roster)} managers, expected 16")
    if len(us_roster) != 16:
        print(f"WARNING: our roster has {len(us_roster)} managers, expected 16")

    print("Fetching bootstrap-static...")
    bootstrap = load_bootstrap()
    players = player_lookup(bootstrap)

    points = ep_next_points(players)
    projections_path = Path(args.projections) if args.projections else find_solio_csv()
    if projections_path and projections_path.exists():
        solio_points = load_solio_projections(projections_path, bootstrap)
        print(f"Loaded {len(solio_points)} player projection(s) from {projections_path}")
        points.update(solio_points)
    else:
        print("No Solio projections CSV found -- using ep_next only.")

    last_finished_gw, next_gw = current_and_next_gw(bootstrap)
    print(f"Last finished GW: {last_finished_gw}, projecting for GW: {next_gw}")

    live_stats = fetch_live_stats(next_gw)
    team_status = fetch_fixture_status(next_gw)
    live_locked = build_live_locked(players, live_stats, team_status)
    if live_locked:
        print(f"GW{next_gw} already in progress -- {len(live_locked)} player(s) locked "
              f"to their real live score instead of a projection")

    print(f"Fetching {args.season} historical data to model score variance...")
    rows = fetch_season_rows(args.season)
    fallback_residuals = build_position_residuals(rows)
    for pos, vals in fallback_residuals.items():
        print(f"  {pos}: {len(vals)} historical samples, "
              f"stdev(actual - projected) = {statistics.pstdev(vals):.2f}")
    team_gw_residuals = build_team_gw_residuals(rows)
    team_gw_index = build_team_gw_index(team_gw_residuals)
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}

    ov_path = Path(args.overrides)
    overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}
    if overrides:
        print(f"Loaded {len(overrides)} chip override(s) from {ov_path}")

    print("\nFetching our club's picks...")
    us_club, us_failed = build_club_scores(us_roster, players, points, last_finished_gw, next_gw,
                                            overrides, live_stats, team_status)
    print("Fetching opponent's picks...")
    them_club, them_failed = build_club_scores(them_roster, players, points, last_finished_gw, next_gw,
                                                overrides, live_stats, team_status)

    if args.explain_manager:
        name = args.explain_manager
        club = us_club if name in us_club else them_club if name in them_club else None
        if club is None:
            print(f"ERROR: '{name}' not found in either roster")
            sys.exit(1)
        info = club[name]
        print(f"\n=== {name} ===")
        if "manual_score" in info:
            print(f"Manual score override: {info['manual_score']}")
            return
        print(f"Real captain: {info['captain']}")
        total = 0.0
        for pid in info["starters"]:
            el = players.get(pid)
            pname = f"{el['first_name']} {el['second_name']}" if el else f"element #{pid}"
            cap = " (C)" if pid == info["captain"] else ""
            if pid in live_locked:
                raw = live_locked[pid]
                score = raw * 2 if pid == info["captain"] else raw
                total += score
                print(f"  {pname}{cap}: LIVE {raw:.1f}{' x2' if cap else ''} = {score:.1f}")
            else:
                proj = points.get(pid, 0.0)
                print(f"  {pname}{cap}: not started yet, projection {proj:.1f} "
                      f"(will be simulated, not shown here)")
        print(f"\nReal known total so far: {total:.1f}")
        return

    if us_failed:
        print(f"WARNING: could not fetch/score {len(us_failed)} of our managers, "
              f"skipped: {', '.join(us_failed)}")
    if them_failed:
        print(f"WARNING: could not fetch/score {len(them_failed)} opponent managers, "
              f"skipped: {', '.join(them_failed)}")
    if len(us_club) < 15 or len(them_club) < 15:
        print("ERROR: too few managers fetched on one side to form a valid lineup (need 15)")
        sys.exit(1)

    if not args.no_prompt and sys.stdin.isatty():
        print("\nIf you know either club's REAL declared GK/Strikers for this "
              "matchup (scouted from their lineup), enter the FPL manager "
              "ID(s) below to pin them -- otherwise just press Enter to skip "
              "and let the tool assume an optimal assignment. "
              "(--no-prompt skips all of this.)")
    args.them_gk_id = prompt_for_role_ids(
        args.them_gk_id, "Opponent's real GK manager ID (Enter to skip): ", args.no_prompt)
    args.them_strikers_ids = prompt_for_role_ids(
        args.them_strikers_ids, "Opponent's real Strikers manager IDs, comma-separated (Enter to skip): ", args.no_prompt)
    args.us_gk_id = prompt_for_role_ids(
        args.us_gk_id, "Our real GK manager ID (Enter to skip): ", args.no_prompt)
    args.us_strikers_ids = prompt_for_role_ids(
        args.us_strikers_ids, "Our real Strikers manager IDs, comma-separated (Enter to skip): ", args.no_prompt)

    us_forced_gk = next(iter(resolve_role_ids(args.us_gk_id, us_roster, "us-gk-id")), None)
    them_forced_gk = next(iter(resolve_role_ids(args.them_gk_id, them_roster, "them-gk-id")), None)
    us_forced_strikers = resolve_role_ids(args.us_strikers_ids, us_roster, "us-strikers-ids")
    them_forced_strikers = resolve_role_ids(args.them_strikers_ids, them_roster, "them-strikers-ids")
    us_forced_bench = resolve_role_ids(args.us_bench_ids, us_roster, "us-bench-ids")
    them_forced_bench = resolve_role_ids(args.them_bench_ids, them_roster, "them-bench-ids")
    us_fh_names = resolve_role_ids(",".join(args.us_fh_id or []), us_roster, "us-fh-id")
    them_fh_names = resolve_role_ids(",".join(args.them_fh_id or []), them_roster, "them-fh-id")
    us_roles = assign_roles(us_club, us_forced_gk, us_forced_strikers, us_forced_bench, us_fh_names)
    them_roles = assign_roles(them_club, them_forced_gk, them_forced_strikers, them_forced_bench, them_fh_names)
    print_roles("Us", us_club, us_roles)
    print_roles("Them", them_club, them_roles)

    if live_locked:
        us_known, us_pending = current_known_scores(us_club, live_locked)
        them_known, them_pending = current_known_scores(them_club, live_locked)
        us_left = sum(us_pending.values())
        them_left = sum(them_pending.values())
        current_us_goals = match_goals(us_known, us_roles, them_known, them_roles)
        current_them_goals = match_goals(them_known, them_roles, us_known, us_roles)
        breakdown_us = match_goals_breakdown(us_known, us_roles, them_known, them_roles)
        breakdown_them = match_goals_breakdown(them_known, them_roles, us_known, us_roles)
        print(f"\n=== Real score so far (already-known results only, 0 for anyone "
              f"who hasn't played yet) ===")
        print(f"Current scoreline: {current_us_goals} - {current_them_goals}")
        print(f"  Strikers vs their GK:  {breakdown_us['strikers']} - {breakdown_them['strikers']}")
        print(f"  Squad vs Squad:        {breakdown_us['squad']} - {breakdown_them['squad']}")
        print(f"Still to play: {us_left} of our starters, {them_left} of theirs "
              f"({'; '.join(f'{n}: {p} pending' for n, p in us_pending.items() if p) or 'none'} "
              f"| {'; '.join(f'{n}: {p} pending' for n, p in them_pending.items() if p) or 'none'})")

        us_pending_names = pending_starter_names(us_club, players, live_locked)
        them_pending_names = pending_starter_names(them_club, players, live_locked)
        print("\nWho's still to play (per manager):")
        print("  Us:")
        for n, names in us_pending_names.items():
            if names:
                print(f"    {n}: {', '.join(names)}")
        print("  Them:")
        for n, names in them_pending_names.items():
            if names:
                print(f"    {n}: {', '.join(names)}")

    rng = random.Random(args.seed)
    print(f"\nRunning {args.sims} simulated matchweeks...")
    result = run_simulation(rng, us_club, us_roles, them_club, them_roles,
                             players, points, team_names, team_gw_residuals,
                             team_gw_index, fallback_residuals, args.sims, live_locked)

    print(f"\n=== Result ===")
    print(f"Win:  {result['win_pct']:.1f}%")
    print(f"Draw: {result['draw_pct']:.1f}%")
    print(f"Loss: {result['loss_pct']:.1f}%")
    print(f"Average scoreline: {result['avg_goals_for']:.2f} - {result['avg_goals_against']:.2f}")

    for_ = result["avg_breakdown_for"]
    against = result["avg_breakdown_against"]
    print(f"\nWhere the goals come from (average per matchweek):")
    print(f"  Strikers vs their GK:  {for_['strikers']:.2f} - {against['strikers']:.2f}")
    print(f"  Squad vs Squad:        {for_['squad']:.2f} - {against['squad']:.2f}")


if __name__ == "__main__":
    main()
