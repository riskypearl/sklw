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


def fetch_live_points(gw: int) -> dict[int, float]:
    """Each player's REAL total_points so far in gameweek `gw` -- 0 if
    their fixture hasn't started yet, their running total while it's in
    progress, the final real score once it's finished. Public endpoint,
    no login needed."""
    data = get_json(f"{FPL_BASE}/event/{gw}/live/")
    return {el["id"]: float(el["stats"]["total_points"]) for el in data["elements"]}


def fetch_fixture_status(gw: int) -> dict[int, str]:
    """real team_id -> 'not_started' | 'in_progress' | 'finished' for
    gameweek `gw`. A team with no fixture that GW (blank gameweek)
    simply has no entry -- callers should default a missing key to
    'not_started'."""
    fixtures = get_json(f"{FPL_BASE}/fixtures/?event={gw}")
    status: dict[int, str] = {}
    for f in fixtures:
        if f.get("finished"):
            s = "finished"
        elif f.get("started"):
            s = "in_progress"
        else:
            s = "not_started"
        status[f["team_h"]] = s
        status[f["team_a"]] = s
    return status


def build_live_locked(players: dict[int, dict], gw: int) -> dict[int, float]:
    """Element IDs whose real outcome for `gw` is already partly or fully
    known (their fixture has started or finished) -> their real points
    so far, for simulate_manager_score to use as a FIXED value instead of
    a projection + simulated residual -- once a match has happened,
    there's no reason to keep guessing at it. A player mid-match is
    locked to their CURRENT live score, not their eventual final one --
    a deliberate simplification (doesn't model the remaining upside/
    downside left in that specific match), but still strictly more
    accurate than treating an in-progress player as if nothing had
    happened yet. Returns {} harmlessly if run well before the GW starts
    (every fixture 'not_started', nothing to lock)."""
    live_points = fetch_live_points(gw)
    team_status = fetch_fixture_status(gw)
    locked: dict[int, float] = {}
    for pid, el in players.items():
        status = team_status.get(el["team"], "not_started")
        if status in ("in_progress", "finished") and pid in live_points:
            locked[pid] = live_points[pid]
    return locked


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


def find_solio_csv() -> Path | None:
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


def resolve_bench_ids(spec: str | None, roster: dict[str, int], label: str) -> set[str]:
    """Resolves a comma-separated list of FPL manager IDs (--them-bench-ids/
    --us-bench-ids) to the matching names in `roster`, for assign_roles'
    forced_bench. IDs rather than names since --them-file/--us-file names
    may just be arbitrary placeholder labels (no known real names yet) --
    matching by ID is unambiguous either way."""
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


def build_club_scores(roster: dict[str, int], players: dict[int, dict], points: dict[int, float],
                       last_finished_gw: int, next_gw: int,
                       overrides: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Fetches each of the 16 managers' picks, applies any chip overrides
    recorded via sklw_lineup.py's --wildcard/--transfer/--tc/--set-score
    (same overrides.json, same format -- see sklw_lineup.py for the full
    reasoning on each), computes their best-xi + captain (decision-time,
    from current projections), and returns per-manager {'starters': [...],
    'captain': id, 'projected': float} plus a list of any manager names
    that failed to fetch (skipped). A 'manual_score' override skips
    picks/points entirely -- starters is left empty and 'manual_score' is
    set, which simulate_manager_score treats as a fixed, zero-variance
    contribution (the manager told us the number directly, no guessing
    needed)."""
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
            else:
                failed.append(name)
                continue

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


def assign_roles(club: dict[str, dict], forced_bench: set[str] | None = None) -> dict[str, list[str]]:
    """Same rule as sklw_lineup.py's suggest_lineup: top projected -> GK,
    next 2 -> Strikers, next 11 -> Squad, rest -> Bench. Fixed ONCE from
    projections (decision-time), same as a real captain would submit --
    the Monte Carlo layer below only varies the OUTCOME given this fixed
    assignment, not the assignment itself.

    forced_bench: manager names KNOWN to actually be benched this week
    (e.g. scouted from the opponent's real declared lineup, rather than
    just assumed from projections) -- pinned to Bench regardless of
    projection. The remaining 14 are ranked as normal (top -> GK, next 2
    -> Strikers, rest -> Squad). Extra names beyond 2 are ignored (with a
    warning) since Bench only has 2 slots; fewer than 2 fills the rest of
    Bench from the bottom of the ranked pool as usual."""
    forced_bench = forced_bench or set()
    unknown = forced_bench - set(club)
    if unknown:
        print(f"WARNING: forced-bench name(s) not found in this club, ignored: {', '.join(unknown)}")
        forced_bench = forced_bench - unknown
    if len(forced_bench) > 2:
        extra = sorted(forced_bench)[2:]
        print(f"WARNING: more than 2 forced-bench names given, ignoring: {', '.join(extra)}")
        forced_bench = set(sorted(forced_bench)[:2])

    pool = [n for n in club if n not in forced_bench]
    ranked = sorted(pool, key=lambda n: -club[n]["projected"])
    need = 2 - len(forced_bench)
    bench = sorted(forced_bench) + (ranked[-need:] if need else [])
    rest = ranked[:-need] if need else ranked
    return {"gk": rest[0:1], "strikers": rest[1:3], "squad": rest[3:14], "bench": bench}


def h2h_goals(a_actual: float, b_actual: float) -> int:
    """SKLW's own rule: 1 goal per full 20-point margin, zero if b
    equals/outscores a. Identical to backtest.py's h2h_goals."""
    margin = a_actual - b_actual
    return int(margin // 20) + 1 if margin >= 1 else 0


def squad_goals(a_total: float, b_total: float) -> int:
    """SKLW's own rule: 1 goal per full 30-point margin, tie = 0 both
    sides. Identical to backtest.py's squad_goals."""
    margin = a_total - b_total
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals_breakdown(scores: dict[str, float], roles: dict[str, list[str]],
                           opp_scores: dict[str, float],
                           opp_roles: dict[str, list[str]]) -> dict[str, int]:
    """Goals FOR the 'scores' side, split by which battle they came from
    -- our Strikers vs their GK, our GK vs their Strikers, our Squad vs
    their Squad. Lets the final report show WHERE a match is likely to
    be won or lost, not just the final tally. match_goals() is just the
    sum of these three."""
    striker_goals = sum(h2h_goals(scores[name], opp_scores[opp_roles["gk"][0]])
                         for name in roles["strikers"])
    gk_goals = sum(h2h_goals(scores[roles["gk"][0]], opp_scores[name])
                   for name in opp_roles["strikers"])
    own_squad = sum(scores[n] for n in roles["squad"])
    opp_squad = sum(opp_scores[n] for n in opp_roles["squad"])
    squad_g = squad_goals(own_squad, opp_squad)
    return {"strikers": striker_goals, "gk": gk_goals, "squad": squad_g}


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
    our_breakdown_total = {"strikers": 0, "gk": 0, "squad": 0}
    their_breakdown_total = {"strikers": 0, "gk": 0, "squad": 0}
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
                          "--them-file is required.")
    ap.add_argument("--us", metavar="NAME:ID,...",
                     help="override our own 16 managers (defaults to the "
                          "same roster as sklw_lineup.py's MANAGER_IDS)")
    ap.add_argument("--us-file", metavar="PATH", help="like --them-file, for our own roster")
    ap.add_argument("--them-bench-ids", metavar="ID,ID",
                     help="if you know the opponent's REAL declared Bench "
                          "for this matchup (e.g. scouted from their "
                          "lineup), pin those 2 manager IDs to Bench "
                          "instead of letting the tool assume an optimal "
                          "assignment for them too. Comma-separated FPL "
                          "manager IDs, e.g. --them-bench-ids "
                          "\"1859490,41471\".")
    ap.add_argument("--us-bench-ids", metavar="ID,ID",
                     help="same as --them-bench-ids, for our own club")
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
    args = ap.parse_args()

    if not args.them and not args.them_file:
        print("ERROR: need --them \"Name:ID,...\" or --them-file path.json "
              "for the opponent club's 16 managers")
        sys.exit(1)

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

    live_locked = build_live_locked(players, next_gw)
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
    us_club, us_failed = build_club_scores(us_roster, players, points, last_finished_gw, next_gw, overrides)
    print("Fetching opponent's picks...")
    them_club, them_failed = build_club_scores(them_roster, players, points, last_finished_gw, next_gw, overrides)

    if us_failed:
        print(f"WARNING: could not fetch/score {len(us_failed)} of our managers, "
              f"skipped: {', '.join(us_failed)}")
    if them_failed:
        print(f"WARNING: could not fetch/score {len(them_failed)} opponent managers, "
              f"skipped: {', '.join(them_failed)}")
    if len(us_club) < 15 or len(them_club) < 15:
        print("ERROR: too few managers fetched on one side to form a valid lineup (need 15)")
        sys.exit(1)

    us_forced_bench = resolve_bench_ids(args.us_bench_ids, us_roster, "us-bench-ids")
    them_forced_bench = resolve_bench_ids(args.them_bench_ids, them_roster, "them-bench-ids")
    us_roles = assign_roles(us_club, us_forced_bench)
    them_roles = assign_roles(them_club, them_forced_bench)
    print_roles("Us", us_club, us_roles)
    print_roles("Them", them_club, them_roles)

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
    print(f"  Strikers vs their GK:  {for_['strikers']:.2f} - {against['gk']:.2f}")
    print(f"  Our GK vs their Strikers: {for_['gk']:.2f} - {against['strikers']:.2f}")
    print(f"  Squad vs Squad:        {for_['squad']:.2f} - {against['squad']:.2f}")


if __name__ == "__main__":
    main()
