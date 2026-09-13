"""Runs win-probability simulations for EVERY fixture in the league for
the current gameweek, not just your own club's.

Standalone in spirit, but DELIBERATELY IMPORTS sklw_matchup.py directly
rather than duplicating it -- a real exception to this project's usual
"no shared imports between the tools" convention. That convention
exists to keep small, independently-tweakable formula-level helpers
(match_goals, squad_goals, etc.) from becoming unintentionally coupled.
The simulation engine reused here is a different scale of thing
entirely: ~1000 lines of correctness-sensitive logic (live-pick
fetching, real auto-sub prediction, the Monte Carlo sim itself) that
would be far riskier to hand-duplicate once per fixture (~23 times)
than to import once and call repeatedly. A fix to sklw_matchup.py's
engine reaches this automatically instead of needing to be repeated by
hand in a second copy.

What makes this possible at all: previously, simulating another club's
outcome would have needed guessing/solving their GK+Strikers choice --
genuinely hard. It doesn't need guessing -- every club already records
its own real GK/Strikers picks in the same sheet (see
parse_sheet_screenshots.py), so this just reads what every club
actually declared instead of solving anything.

Requires, in order:
    python capture_sheet_screenshots.py   (grabs LiveScores + every M# tab)
    python league_wide_sim.py

For each fixture found in the LiveScores screenshot: reads both clubs'
real GK/Strikers from that fixture's M# tab screenshot (OCR + color,
see parse_sheet_screenshots.py -- same genuinely-less-reliable-than-a-
real-cell caveate applies here, doubled, since a bad read on EITHER
club silently skips that whole fixture rather than guessing), looks up
both rosters in clubs.json, fetches all 32 managers' real live FPL
data, and runs the same Monte Carlo simulation sklw_matchup.py runs for
a single matchup.

SCALE WARNING: league-wide means fetching live picks for every manager
in every club (47 clubs x 16 managers = 752 people here, vs. 32 for a
single matchup) plus the same once-per-run historical variance model
sklw_matchup.py already fetches. Expect this to take several minutes,
and it's far more likely to hit FPL's own rate limits than a normal
single-matchup run -- a small delay between each club's fetch is built
in to reduce that risk, not eliminate it.

Usage:
    python league_wide_sim.py
    python league_wide_sim.py --sims 2000 --clubs-file clubs.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

import sklw_matchup as sm
import parse_sheet_screenshots as ps

SCREENSHOTS_DIR = "sheet_screenshots"


def extract_roles_for_fixture(shots_dir: Path, m_label: str, club_a: str, club_b: str,
                               clubs: dict[str, dict[str, int]]
                               ) -> tuple[dict, dict] | None:
    """Reads both clubs' real GK/Strikers off the m_label tab screenshot.
    Returns (result_a, result_b) -- each parse_matchup_tab()'s result
    dict (with 'gk'/'strikers' if determined) -- or None if the tab
    screenshot is missing entirely. Doesn't itself decide whether a
    fixture is usable; callers check 'gk' in each result, same as
    parse_sheet_screenshots.py's own main()."""
    tab_path = shots_dir / f"{m_label}.png"
    if not tab_path.exists():
        return None
    result_a = ps.parse_matchup_tab(tab_path, clubs[club_a], clubs[club_b])
    result_b = ps.parse_matchup_tab(tab_path, clubs[club_b], clubs[club_a])
    return result_a, result_b


def simulate_fixture(rng: random.Random, roster_a: dict[str, int], roles_a_pins: dict,
                      roster_b: dict[str, int], roles_b_pins: dict,
                      players: dict[int, dict], points: dict[int, float],
                      last_finished_gw: int, next_gw: int, overrides: dict,
                      live_stats: dict[int, dict], team_status: dict[int, str],
                      team_names: dict[int, str],
                      team_gw_residuals: dict, team_gw_index: dict, fallback_residuals: dict,
                      live_locked: dict[int, float], sims: int) -> dict | None:
    """Fetches both clubs' real picks and runs the simulation -- same
    steps sklw_matchup.py's main() does for one matchup, reused here per
    fixture. Returns None (with a printed reason) if either side comes
    up short of usable managers, same 'need 15' floor sklw_matchup.py
    itself uses."""
    club_a, failed_a = sm.build_club_scores(roster_a, players, points, last_finished_gw,
                                             next_gw, overrides, live_stats, team_status)
    club_b, failed_b = sm.build_club_scores(roster_b, players, points, last_finished_gw,
                                             next_gw, overrides, live_stats, team_status)
    if len(club_a) < 15 or len(club_b) < 15:
        print(f"    SKIPPED: too few managers fetched (need 15) -- "
              f"{len(club_a)}/{len(roster_a)} and {len(club_b)}/{len(roster_b)}")
        return None

    forced_gk_a = roles_a_pins["gk"] if roles_a_pins["gk"] in club_a else None
    forced_strikers_a = {n for n in roles_a_pins["strikers"] if n in club_a}
    forced_gk_b = roles_b_pins["gk"] if roles_b_pins["gk"] in club_b else None
    forced_strikers_b = {n for n in roles_b_pins["strikers"] if n in club_b}
    roles_a = sm.assign_roles(club_a, forced_gk_a, forced_strikers_a)
    roles_b = sm.assign_roles(club_b, forced_gk_b, forced_strikers_b)

    result = sm.run_simulation(rng, club_a, roles_a, club_b, roles_b, players, points,
                                team_names, team_gw_residuals, team_gw_index,
                                fallback_residuals, sims, live_locked)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screenshots-dir", default=SCREENSHOTS_DIR)
    ap.add_argument("--clubs-file", default="clubs.json")
    ap.add_argument("--season", default="2023-24",
                     help="historical season used to build the score-variance model")
    ap.add_argument("--sims", type=int, default=2000,
                     help="Monte Carlo trials PER FIXTURE -- kept lower than "
                          "sklw_matchup.py's single-matchup default (5000) since "
                          "this runs it ~23 times in one go")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--overrides", default="overrides.json")
    ap.add_argument("--delay", type=float, default=0.3,
                     help="seconds to pause between each club's picks fetch, "
                          "a basic rate-limit safety margin (default: 0.3)")
    args = ap.parse_args()

    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: pytesseract/pillow aren't installed -- run: pip install pytesseract pillow "
              "(and install the Tesseract OCR binary itself, see parse_sheet_screenshots.py's "
              "module docstring)")
        sys.exit(1)
    ps._setup_tesseract()
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        print("ERROR: the Tesseract OCR binary itself isn't installed (or not on PATH, "
              "and not in the usual Windows install location either) -- this is separate "
              "from the pytesseract pip package. Install it: "
              "https://github.com/UB-Mannheim/tesseract/wiki")
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

    print("Reading fixtures from LiveScores...")
    fixtures = ps.find_all_fixtures(live_scores_path, list(clubs))
    if not fixtures:
        print("ERROR: found no recognizable fixtures in the LiveScores screenshot.")
        sys.exit(1)
    print(f"Found {len(fixtures)} fixture(s).")

    print("\nFetching bootstrap-static...")
    bootstrap = sm.load_bootstrap()
    players = sm.player_lookup(bootstrap)
    points = sm.ep_next_points(players)
    last_finished_gw, next_gw = sm.current_and_next_gw(bootstrap)
    print(f"Last finished GW: {last_finished_gw}, projecting for GW: {next_gw}")

    live_stats = sm.fetch_live_stats(next_gw)
    team_status = sm.fetch_fixture_status(next_gw)
    live_locked = sm.build_live_locked(players, live_stats, team_status)
    if live_locked:
        print(f"GW{next_gw} already in progress -- {len(live_locked)} player(s) locked "
              f"to their real live score")

    print(f"Fetching {args.season} historical data to model score variance...")
    rows = sm.fetch_season_rows(args.season)
    fallback_residuals = sm.build_position_residuals(rows)
    team_gw_residuals = sm.build_team_gw_residuals(rows)
    team_gw_index = sm.build_team_gw_index(team_gw_residuals)
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}

    ov_path = Path(args.overrides)
    overrides = json.loads(ov_path.read_text()) if ov_path.exists() else {}

    rng = random.Random(args.seed)
    results = []
    print(f"\nSimulating {len(fixtures)} fixture(s), {args.sims} trials each "
          f"(this fetches live data for {len(fixtures) * 32} managers total -- "
          f"expect several minutes)...\n")

    for i, (m_label, club_a, club_b) in enumerate(fixtures, 1):
        print(f"[{i}/{len(fixtures)}] {m_label}: {club_a} vs {club_b}")
        roles = extract_roles_for_fixture(shots_dir, m_label, club_a, club_b, clubs)
        if roles is None:
            print(f"    SKIPPED: no {m_label}.png screenshot found.")
            continue
        result_a, result_b = roles
        if "gk" not in result_a or "gk" not in result_b:
            reason_a = result_a.get("warning", "couldn't determine GK/Strikers")
            reason_b = result_b.get("warning", "couldn't determine GK/Strikers")
            print(f"    SKIPPED: {club_a}: {reason_a if 'gk' not in result_a else 'ok'} | "
                  f"{club_b}: {reason_b if 'gk' not in result_b else 'ok'}")
            continue

        sim = simulate_fixture(rng, clubs[club_a], result_a, clubs[club_b], result_b,
                                players, points, last_finished_gw, next_gw, overrides,
                                live_stats, team_status, team_names,
                                team_gw_residuals, team_gw_index, fallback_residuals,
                                live_locked, args.sims)
        if sim is None:
            continue
        print(f"    {club_a}: {sim['win_pct']:.1f}% win  |  {club_b}: {sim['loss_pct']:.1f}% win  "
              f"|  draw {sim['draw_pct']:.1f}%  |  avg {sim['avg_goals_for']:.2f} - "
              f"{sim['avg_goals_against']:.2f}")
        results.append((m_label, club_a, club_b, sim))
        time.sleep(args.delay)

    print(f"\n=== League-wide results: {len(results)}/{len(fixtures)} fixture(s) simulated ===")
    for m_label, club_a, club_b, sim in results:
        print(f"{m_label}: {club_a} {sim['win_pct']:.1f}% - {sim['loss_pct']:.1f}% {club_b} "
              f"(draw {sim['draw_pct']:.1f}%, avg {sim['avg_goals_for']:.2f}-"
              f"{sim['avg_goals_against']:.2f})")


if __name__ == "__main__":
    main()
