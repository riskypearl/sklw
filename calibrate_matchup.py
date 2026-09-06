"""Calibration check for sklw_matchup.py's win-probability model.

Standalone script (same convention as backtest.py/sklw_matchup.py -- no
imports between them, small helpers duplicated).

The question this answers: when sklw_matchup.py says "73% to win", does
the favoured side actually win about 73% of the time in reality? We have
no real historical SKLW match data (opponent lineups are private,
tournament-only -- see backtest.py's docstring for the same constraint),
so this uses the same trick backtest.py does: synthetic "matches" built
from real historical FPL players (real per-GW xP projections AND real
per-GW actual outcomes, from the public vaastav/Fantasy-Premier-League
archive), scored with SKLW's own real goal rules.

Critical design point -- avoiding lookahead bias: the position-based
score-variance model (what sklw_matchup.py calls "residuals") is trained
on one season (--train-season) and every prediction is TESTED against a
DIFFERENT season's real outcomes (--test-season). If the same season's
data were used for both, the variance model would effectively know
something about the specific players/outcomes it's being tested against
-- an out-of-sample split is the only way this calibration check means
anything.

Method per trial:
  - Pick a random target GW in the test season. Build two "clubs" of 16
    synthetic members, each = 11 real players randomly drawn from a pool
    of established (low element ID) players with real data at that GW,
    captained by whichever has the highest real historical xP among them
    (mirrors real captaincy -- picked on projection, not hindsight).
  - Roles (GK/Strikers/Squad/Bench) assigned by projected total (xP sum),
    same rule as sklw_lineup.py/sklw_matchup.py.
  - PREDICTED win probability: run sklw_matchup.py's exact Monte Carlo
    logic, drawing each player's simulated outcome as (their real xP for
    this GW) + (a resampled residual from the TRAINING season, by
    position), same as a live run would.
  - REAL outcome: apply the same SKLW goal rules directly to real
    historical total_points (no simulation, no lookahead) to get the
    actual winner.
  - Across many trials, bucket by predicted win probability and check
    whether the realized win rate in each bucket matches -- plus an
    overall Brier score (mean squared error between predicted probability
    and the real 0/1 outcome; 0 = perfect, 0.25 = no better than a coin
    flip) as a single summary number.
"""
from __future__ import annotations

import argparse
import csv
import io
import random
import statistics

import requests

ARCHIVE_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
POSITIONS = ("GK", "DEF", "MID", "FWD")


def fetch_season_rows(season: str) -> list[dict]:
    url = f"{ARCHIVE_BASE}/{season}/gws/merged_gw.csv"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def build_position_residuals(rows: list[dict]) -> dict[str, list[float]]:
    """Identical to sklw_matchup.py's function of the same name -- real
    historical (actual - xP) per position, used as a FALLBACK when a
    player's real team has no usable data for the chosen shock gameweek
    (see build_team_gw_residuals)."""
    residuals: dict[str, list[float]] = {p: [] for p in POSITIONS}
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
    module docstring / README): same-team same-GW residuals correlate at
    ~0.14 (a team has a good or bad day together -- shared clean sheet,
    shared goals, shared bonus points), vs ~0.03 pooled across the whole
    league that GW and ~0 between opposing teams in the same fixture --
    so TEAM is the unit that actually needs to be correlated, not 'the
    whole gameweek' or 'the match'. Used for a team-scoped block
    bootstrap: for one simulated trial, draw ONE historical (team, GW)
    per real team involved, and every player from that team draws their
    residual from THAT SAME historical block -- reproducing the real
    correlation without needing to assume any particular distribution
    shape for it."""
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
        if pos not in POSITIONS:
            continue
        key = (team, gw)
        blocks.setdefault(key, {p: [] for p in POSITIONS})[pos].append(actual - proj)
    return blocks


def build_team_gw_index(blocks: dict[tuple[str, int], dict[str, list[float]]]) -> dict[str, list[int]]:
    """team -> list of GWs that team has a block for, so a random shock
    GW can be drawn per team without hardcoding the season's GW range
    (handles promoted/relegated clubs missing from one season cleanly)."""
    index: dict[str, list[int]] = {}
    for team, gw in blocks:
        index.setdefault(team, []).append(gw)
    return index


def build_player_history(rows: list[dict], pool_size: int) -> dict[int, dict]:
    """Same as backtest.py's function of the same name: element_id ->
    {'position': str, 'team': str, 'by_gw': {gw: (actual, xp)}},
    restricted to the lowest `pool_size` distinct element IDs
    (established/engaged-manager proxy, same reasoning as backtest.py)."""
    by_player: dict[int, dict] = {}
    for row in rows:
        try:
            eid = int(row["element"])
            gw = int(row["GW"])
            actual = float(row["total_points"])
            xp = float(row["xP"])
        except (ValueError, KeyError):
            continue
        entry = by_player.setdefault(eid, {"position": row["position"], "team": row["team"], "by_gw": {}})
        entry["by_gw"][gw] = (actual, xp)

    pool_ids = sorted(by_player)[:pool_size]
    return {eid: by_player[eid] for eid in pool_ids}


def h2h_goals(a: float, b: float) -> int:
    margin = a - b
    return int(margin // 20) + 1 if margin >= 1 else 0


def squad_goals(a: float, b: float) -> int:
    margin = a - b
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals(scores: list[float], roles: dict[str, list[int]],
                 opp_scores: list[float], opp_roles: dict[str, list[int]]) -> int:
    """Goals FOR 'scores' -- identical formula to backtest.py/sklw_matchup.py,
    indices instead of names (members here have no natural name)."""
    goals = 0
    for i in roles["strikers"]:
        goals += h2h_goals(scores[i], opp_scores[opp_roles["gk"][0]])
    for i in opp_roles["strikers"]:
        goals += h2h_goals(scores[roles["gk"][0]], opp_scores[i])
    own_squad = sum(scores[i] for i in roles["squad"])
    opp_squad = sum(opp_scores[i] for i in opp_roles["squad"])
    goals += squad_goals(own_squad, opp_squad)
    return goals


def assign_roles(projected: list[float]) -> dict[str, list[int]]:
    ranked = sorted(range(16), key=lambda i: -projected[i])
    return {"gk": ranked[0:1], "strikers": ranked[1:3], "squad": ranked[3:14], "bench": ranked[14:16]}


def build_member(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
                  target_gw: int) -> dict:
    """One synthetic club member = 11 real players. Returns per-player
    xp/actual/position/team lists plus which index is captain (highest
    xp, decision-time, not hindsight) and the totals (captain doubled)."""
    ids = rng.sample(pool_ids, 11)
    xps = [eligible[eid]["by_gw"][target_gw][1] for eid in ids]
    actuals = [eligible[eid]["by_gw"][target_gw][0] for eid in ids]
    positions = [eligible[eid]["position"] for eid in ids]
    teams = [eligible[eid]["team"] for eid in ids]
    captain_idx = max(range(11), key=lambda i: xps[i])
    projected = sum(xps) + xps[captain_idx]
    actual = sum(actuals) + actuals[captain_idx]
    return {"xps": xps, "positions": positions, "teams": teams, "captain_idx": captain_idx,
            "projected": projected, "actual": actual}


def build_club(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
               target_gw: int) -> list[dict]:
    return [build_member(rng, pool_ids, eligible, target_gw) for _ in range(16)]


def pick_team_shocks(rng: random.Random, teams_needed: set[str],
                      team_gw_index: dict[str, list[int]]) -> dict[str, int | None]:
    """One random historical GW per real team, shared by every player
    from that team within a single simulated trial -- the mechanism that
    reproduces the measured same-team correlation. None for a team with
    no training-season data at all (e.g. promoted/relegated between
    seasons) -- callers fall back to the pooled position distribution for
    those players instead."""
    shocks: dict[str, int | None] = {}
    for team in teams_needed:
        gws = team_gw_index.get(team)
        shocks[team] = rng.choice(gws) if gws else None
    return shocks


def simulate_member_score(rng: random.Random, member: dict, team_shocks: dict[str, int | None],
                           team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                           fallback_residuals: dict[str, list[float]]) -> float:
    total = 0.0
    for i, (xp, pos, team) in enumerate(zip(member["xps"], member["positions"], member["teams"])):
        shock_gw = team_shocks.get(team)
        pool = None
        if shock_gw is not None:
            pool = team_gw_residuals.get((team, shock_gw), {}).get(pos)
        if not pool:  # team unknown this season, or no player at this position that GW
            pool = fallback_residuals[pos]
        score = xp + rng.choice(pool)
        if i == member["captain_idx"]:
            score *= 2
        total += score
    return total


def predicted_win_probability(rng: random.Random, club1: list[dict], roles1: dict,
                               club2: list[dict], roles2: dict,
                               team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                               team_gw_index: dict[str, list[int]],
                               fallback_residuals: dict[str, list[float]], inner_sims: int) -> float:
    teams_needed = {t for m in club1 + club2 for t in m["teams"]}
    wins = 0
    for _ in range(inner_sims):
        team_shocks = pick_team_shocks(rng, teams_needed, team_gw_index)
        scores1 = [simulate_member_score(rng, m, team_shocks, team_gw_residuals, fallback_residuals)
                   for m in club1]
        scores2 = [simulate_member_score(rng, m, team_shocks, team_gw_residuals, fallback_residuals)
                   for m in club2]
        g1 = match_goals(scores1, roles1, scores2, roles2)
        g2 = match_goals(scores2, roles2, scores1, roles1)
        if g1 > g2:
            wins += 1
    return wins / inner_sims


def real_outcome(club1: list[dict], roles1: dict, club2: list[dict], roles2: dict) -> tuple[int, int]:
    actual1 = [m["actual"] for m in club1]
    actual2 = [m["actual"] for m in club2]
    g1 = match_goals(actual1, roles1, actual2, roles2)
    g2 = match_goals(actual2, roles2, actual1, roles1)
    return g1, g2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-season", default="2022-23",
                     help="season used to build the score-variance model (must differ "
                          "from --test-season, otherwise this test is meaningless)")
    ap.add_argument("--test-season", default="2023-24",
                     help="season used for real outcomes to check predictions against")
    ap.add_argument("--pool-size", type=int, default=300)
    ap.add_argument("--trials", type=int, default=1200, help="number of synthetic matchups")
    ap.add_argument("--inner-sims", type=int, default=300,
                     help="Monte Carlo simulations per matchup for the predicted probability "
                          "(same role sklw_matchup.py's --sims plays live)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.train_season == args.test_season:
        print("ERROR: --train-season and --test-season must differ, otherwise the "
              "variance model would have lookahead knowledge of what it's tested against.")
        return

    print(f"Fetching {args.train_season} (training) data for the variance model...")
    train_rows = fetch_season_rows(args.train_season)
    fallback_residuals = build_position_residuals(train_rows)
    for pos in POSITIONS:
        print(f"  {pos}: {len(fallback_residuals[pos])} samples, "
              f"stdev(actual - xP) = {statistics.pstdev(fallback_residuals[pos]):.2f}")
    team_gw_residuals = build_team_gw_residuals(train_rows)
    team_gw_index = build_team_gw_index(team_gw_residuals)
    print(f"  {len(team_gw_index)} real teams, {len(team_gw_residuals)} (team, GW) blocks "
          f"for the team-scoped correlation model")

    print(f"\nFetching {args.test_season} (test) data for real outcomes...")
    test_rows = fetch_season_rows(args.test_season)
    history = build_player_history(test_rows, args.pool_size)
    print(f"Player pool: {len(history)} players (lowest element IDs)")

    rng = random.Random(args.seed)
    target_gws = list(range(1, 39))

    records: list[tuple[float, bool, bool]] = []  # (predicted_win_prob, real_win, real_draw)
    for _ in range(args.trials):
        target_gw = rng.choice(target_gws)
        eligible = {eid: e for eid, e in history.items() if target_gw in e["by_gw"]}
        pool_ids = list(eligible)
        if len(pool_ids) < 22:
            continue

        club1 = build_club(rng, pool_ids, eligible, target_gw)
        club2 = build_club(rng, pool_ids, eligible, target_gw)
        roles1 = assign_roles([m["projected"] for m in club1])
        roles2 = assign_roles([m["projected"] for m in club2])

        pred = predicted_win_probability(rng, club1, roles1, club2, roles2,
                                          team_gw_residuals, team_gw_index,
                                          fallback_residuals, args.inner_sims)
        g1, g2 = real_outcome(club1, roles1, club2, roles2)
        records.append((pred, g1 > g2, g1 == g2))

    n = len(records)
    print(f"\n{n} valid trials.\n")

    print("=== Calibration: predicted win probability vs realized win rate ===")
    print(f"{'bucket':>12}  {'n':>6}  {'realized win%':>14}  {'avg predicted%':>15}")
    for lo in range(0, 100, 10):
        hi = lo + 10
        bucket = [(p, w) for p, w, _ in records if lo / 100 <= p < hi / 100 or (hi == 100 and p == 1.0)]
        if not bucket:
            continue
        realized = 100 * sum(1 for _, w in bucket if w) / len(bucket)
        avg_pred = 100 * statistics.mean(p for p, _ in bucket)
        print(f"{lo:>4}-{hi:<3}%    {len(bucket):>6}  {realized:>13.1f}%  {avg_pred:>14.1f}%")

    brier = statistics.mean((p - (1.0 if w else 0.0)) ** 2 for p, w, _ in records)
    draw_rate = 100 * sum(1 for _, _, d in records if d) / n
    print(f"\nBrier score: {brier:.4f} (0 = perfect, 0.25 = no better than always guessing 50%)")
    print(f"Real draw rate in these trials: {draw_rate:.1f}% "
          f"(draws are excluded from the win-rate calibration buckets above, "
          f"included as a 'not a win' in the Brier score)")


if __name__ == "__main__":
    main()
