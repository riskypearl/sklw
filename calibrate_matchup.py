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
from collections import Counter

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
    """A goal for beating the opponent at all, +1 more per full 30-point
    margin beyond that -- same base+bonus shape as h2h_goals, just a
    wider band. This is the rules doc's literal wording; confirmed
    against 4 independent real SKLW results once the GK-scoring bug in
    match_goals (below) was fixed -- that bug, not this formula, was
    what made an earlier version look like it needed no base goal."""
    margin = a - b
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals(scores: list[float], roles: dict[str, list[int]],
                 opp_scores: list[float], opp_roles: dict[str, list[int]]) -> int:
    """Goals FOR 'scores', indices instead of names (members here have no
    natural name). The GK does NOT independently score goals of its own
    -- only Strikers score, against the opposing GK. A GK's role is
    purely to be a high-scoring target that's hard for the opponent's
    Strikers to beat; that's already fully captured by the Strikers' own
    H2H calculation on the OTHER side. Confirmed against 4 independent
    real SKLW results that giving the GK a mirrored scoring mechanic
    against the opposing Strikers (an earlier version of this function)
    is wrong -- removing it (and only it) was what made all 4 reproduce
    exactly."""
    goals = 0
    for i in roles["strikers"]:
        goals += h2h_goals(scores[i], opp_scores[opp_roles["gk"][0]])
    own_squad = sum(scores[i] for i in roles["squad"])
    opp_squad = sum(opp_scores[i] for i in opp_roles["squad"])
    goals += squad_goals(own_squad, opp_squad)
    return goals


def assign_roles(projected: list[float]) -> dict[str, list[int]]:
    """Top -> GK, next 2 -> Strikers (GK gets priority: its score is
    compared against BOTH opposing Strikers, denying 2 H2H battles at
    once, vs. a Striker's score only winning its own single battle --
    see sklw_matchup.py's assign_roles for the full reasoning, validated
    in backtest.py's net-goal-differential comparison)."""
    ranked = sorted(range(16), key=lambda i: -projected[i])
    return {"gk": ranked[0:1], "strikers": ranked[1:3], "squad": ranked[3:14], "bench": ranked[14:16]}


VALID_FORMATIONS = [(d, m, 10 - d - m) for d in range(3, 6) for m in range(2, 6)
                     if 1 <= 10 - d - m <= 3]


def build_member(rng: random.Random, eligible_by_pos: dict[str, list[int]],
                  eligible: dict[int, dict], target_gw: int) -> dict:
    """One synthetic club member = 11 real players in a VALID FPL
    formation (1 GK + a real DEF/MID/FWD split), drawn from real
    position-specific pools -- not 11 players pulled uniformly from every
    position mixed together (the earlier version). That matters for
    testing the team-correlation model: a formation-realistic squad
    naturally has some chance of clustering 2+ players from the same real
    club (as real squads do, sometimes deliberately), which a fully mixed
    random blob of 11 across all positions rarely does by chance -- so
    THIS version is what actually gives the team-correlation fix a fair
    test. Returns per-player xp/actual/position/team lists plus which
    index is captain (highest xp, decision-time) and the totals (captain
    doubled)."""
    d, m, f = rng.choice(VALID_FORMATIONS)
    ids = (rng.sample(eligible_by_pos["GK"], 1) + rng.sample(eligible_by_pos["DEF"], d)
           + rng.sample(eligible_by_pos["MID"], m) + rng.sample(eligible_by_pos["FWD"], f))
    xps = [eligible[eid]["by_gw"][target_gw][1] for eid in ids]
    actuals = [eligible[eid]["by_gw"][target_gw][0] for eid in ids]
    positions = [eligible[eid]["position"] for eid in ids]
    teams = [eligible[eid]["team"] for eid in ids]
    captain_idx = max(range(11), key=lambda i: xps[i])
    projected = sum(xps) + xps[captain_idx]
    actual = sum(actuals) + actuals[captain_idx]
    return {"xps": xps, "positions": positions, "teams": teams, "captain_idx": captain_idx,
            "projected": projected, "actual": actual}


def build_club(rng: random.Random, eligible_by_pos: dict[str, list[int]],
               eligible: dict[int, dict], target_gw: int) -> list[dict]:
    return [build_member(rng, eligible_by_pos, eligible, target_gw) for _ in range(16)]


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
                           fallback_residuals: dict[str, list[float]],
                           variance_scale: float = 1.0) -> float:
    """variance_scale: multiplies every resampled residual before adding
    it to xp -- 1.0 (default) reproduces the plain bootstrap exactly.
    Exists to test a specific diagnosis directly: does the SIMULATED
    outcome distribution have too little spread overall (in which case
    a single inflation factor should improve BOTH the win-probability
    overconfidence-at-extremes pattern AND the scoreline under-spread
    pattern at once), rather than a structural correlation gap (which a
    single scalar couldn't fix). See --variance-scale-sweep in main()
    and the README's calibration section for the result."""
    total = 0.0
    for i, (xp, pos, team) in enumerate(zip(member["xps"], member["positions"], member["teams"])):
        shock_gw = team_shocks.get(team)
        pool = None
        if shock_gw is not None:
            pool = team_gw_residuals.get((team, shock_gw), {}).get(pos)
        if not pool:  # team unknown this season, or no player at this position that GW
            pool = fallback_residuals[pos]
        score = xp + rng.choice(pool) * variance_scale
        if i == member["captain_idx"]:
            score *= 2
        total += score
    return total


def predicted_score_distribution(rng: random.Random, club1: list[dict], roles1: dict,
                                  club2: list[dict], roles2: dict,
                                  team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                                  team_gw_index: dict[str, list[int]],
                                  fallback_residuals: dict[str, list[float]],
                                  inner_sims: int, variance_scale: float = 1.0) -> dict[tuple[int, int], float]:
    """The full simulated joint distribution over exact (g1, g2)
    scorelines for one matchup -- win probability is just this summed
    over the g1 > g2 cells, but the full distribution is what actually
    needs checking for scoreline-level calibration (see module
    docstring's Dixon-Coles discussion in the README): does sklw_matchup.py
    only get the WIN/LOSS call right, or the exact scoreline
    probabilities too? Returns {(g1, g2): probability, ...} over
    whichever scorelines actually occurred across inner_sims trials
    (probabilities sum to 1.0; an untouched scoreline is implicitly 0,
    not listed)."""
    teams_needed = {t for m in club1 + club2 for t in m["teams"]}
    counts: Counter[tuple[int, int]] = Counter()
    for _ in range(inner_sims):
        team_shocks = pick_team_shocks(rng, teams_needed, team_gw_index)
        scores1 = [simulate_member_score(rng, m, team_shocks, team_gw_residuals, fallback_residuals, variance_scale)
                   for m in club1]
        scores2 = [simulate_member_score(rng, m, team_shocks, team_gw_residuals, fallback_residuals, variance_scale)
                   for m in club2]
        g1 = match_goals(scores1, roles1, scores2, roles2)
        g2 = match_goals(scores2, roles2, scores1, roles1)
        counts[(g1, g2)] += 1
    return {score: n / inner_sims for score, n in counts.items()}


def real_outcome(club1: list[dict], roles1: dict, club2: list[dict], roles2: dict) -> tuple[int, int]:
    actual1 = [m["actual"] for m in club1]
    actual2 = [m["actual"] for m in club2]
    g1 = match_goals(actual1, roles1, actual2, roles2)
    g2 = match_goals(actual2, roles2, actual1, roles1)
    return g1, g2


def run_variance_scale_sweep(rng: random.Random,
                              built_trials: list[tuple[list[dict], dict, list[dict], dict, tuple[int, int]]],
                              team_gw_residuals: dict[tuple[str, int], dict[str, list[float]]],
                              team_gw_index: dict[str, list[int]],
                              fallback_residuals: dict[str, list[float]],
                              inner_sims: int, sweep_spec: str) -> None:
    """Tests whether a single variance_scale improves BOTH the win-
    probability overconfidence-at-extremes pattern AND the scoreline
    under-spread pattern at once -- evidence for "the simulation is
    generally under-dispersed" if so, since a structural correlation
    gap couldn't be fixed by one scalar applied uniformly everywhere."""
    scales = [float(s.strip()) for s in sweep_spec.split(",") if s.strip()]
    n = len(built_trials)
    print(f"\n=== Variance-scale sweep ({n} trials, same synthetic clubs "
          f"reused across every scale) ===")
    print(f"{'scale':>6}  {'win Brier':>10}  {'multiclass Brier':>17}  {'extremes gap':>13}  {'other-bucket gap':>17}")

    for scale in scales:
        records: list[tuple[float, bool]] = []
        score_records: list[tuple[dict[tuple[int, int], float], tuple[int, int]]] = []
        for club1, roles1, club2, roles2, (g1, g2) in built_trials:
            pred_dist = predicted_score_distribution(rng, club1, roles1, club2, roles2,
                                                       team_gw_residuals, team_gw_index,
                                                       fallback_residuals, inner_sims, scale)
            pred = sum(p for (pg1, pg2), p in pred_dist.items() if pg1 > pg2)
            records.append((pred, g1 > g2))
            score_records.append((pred_dist, (g1, g2)))

        win_brier = statistics.mean((p - (1.0 if w else 0.0)) ** 2 for p, w in records)
        multiclass_brier = statistics.mean(
            sum(p * p for p in pred_dist.values()) - 2 * pred_dist.get(real, 0.0) + 1.0
            for pred_dist, real in score_records)

        # "Extremes gap": overconfidence at the top/bottom win-probability
        # deciles, the SAME pattern noted throughout this project's
        # calibration history -- average |predicted - realized| across
        # the 0-10% and 90-100% buckets only.
        extreme_gaps = []
        for lo, hi in [(0.0, 0.1), (0.9, 1.0)]:
            bucket = [(p, w) for p, w in records if lo <= p < hi or (hi == 1.0 and p == 1.0)]
            if bucket:
                realized = sum(1 for _, w in bucket if w) / len(bucket)
                avg_pred = statistics.mean(p for p, _ in bucket)
                extreme_gaps.append(abs(avg_pred - realized))
        extremes_gap = statistics.mean(extreme_gaps) if extreme_gaps else float("nan")

        common = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 1), (1, 2), (2, 2)]
        other_pred = statistics.mean(
            1.0 - sum(pred_dist.get(s, 0.0) for s in common) for pred_dist, _ in score_records)
        other_real = sum(1 for _, real in score_records if real not in common) / n
        other_gap = abs(other_pred - other_real)

        print(f"{scale:>6.2f}  {win_brier:>10.4f}  {multiclass_brier:>17.4f}  "
              f"{100*extremes_gap:>12.1f}%  {100*other_gap:>16.1f}%")

    print(f"\nLower is better across all four columns. If one scale minimizes "
          f"(or comes close to minimizing) BOTH Brier scores AND both gap "
          f"columns together, that's real evidence the simulation is "
          f"generally under-dispersed rather than missing a specific "
          f"correlation structure -- inflating variance uniformly wouldn't "
          f"fix a structural gap, only a genuine overall shortfall.")


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
    ap.add_argument("--variance-scale", type=float, default=1.0,
                     help="multiplies every resampled residual before adding "
                          "it to xp -- 1.0 (default) is the plain bootstrap, "
                          "unchanged from before this flag existed.")
    ap.add_argument("--variance-scale-sweep", metavar="S1,S2,...",
                     help="instead of a single run, test several "
                          "--variance-scale values against the SAME "
                          "synthetic trials (fair comparison, same idea as "
                          "backtest.py's k-sweep) and report win-probability "
                          "Brier + multiclass scoreline Brier + the 'other' "
                          "bucket gap for each -- built to test one specific "
                          "diagnosis: is the simulated outcome distribution "
                          "under-dispersed overall (in which case a single "
                          "scale improves both metrics at once), rather than "
                          "a structural correlation gap (which it couldn't "
                          "fix). Overrides --variance-scale.")
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

    # Build the synthetic clubs/roles/real-outcomes ONCE, independent of
    # variance_scale -- so a --variance-scale-sweep compares different
    # scales against the EXACT SAME trials (same reasoning as
    # backtest.py's k-sweep: isolates the effect of the scale itself,
    # not random draw variation between trials).
    built_trials: list[tuple[list[dict], dict, list[dict], dict, tuple[int, int]]] = []
    for _ in range(args.trials):
        target_gw = rng.choice(target_gws)
        eligible = {eid: e for eid, e in history.items() if target_gw in e["by_gw"]}
        eligible_by_pos: dict[str, list[int]] = {p: [] for p in POSITIONS}
        for eid, e in eligible.items():
            eligible_by_pos[e["position"]].append(eid)
        # Need at least enough distinct players per position for the
        # widest valid formation (5 DEF, 5 MID, 3 FWD, 1 GK).
        if (len(eligible_by_pos["GK"]) < 1 or len(eligible_by_pos["DEF"]) < 5
                or len(eligible_by_pos["MID"]) < 5 or len(eligible_by_pos["FWD"]) < 3):
            continue

        club1 = build_club(rng, eligible_by_pos, eligible, target_gw)
        club2 = build_club(rng, eligible_by_pos, eligible, target_gw)
        roles1 = assign_roles([m["projected"] for m in club1])
        roles2 = assign_roles([m["projected"] for m in club2])
        real = real_outcome(club1, roles1, club2, roles2)
        built_trials.append((club1, roles1, club2, roles2, real))

    if args.variance_scale_sweep:
        run_variance_scale_sweep(rng, built_trials, team_gw_residuals, team_gw_index,
                                  fallback_residuals, args.inner_sims, args.variance_scale_sweep)
        return

    records: list[tuple[float, bool, bool]] = []  # (predicted_win_prob, real_win, real_draw)
    score_records: list[tuple[dict[tuple[int, int], float], tuple[int, int]]] = []  # (pred_dist, real_scoreline)
    for club1, roles1, club2, roles2, (g1, g2) in built_trials:
        pred_dist = predicted_score_distribution(rng, club1, roles1, club2, roles2,
                                                  team_gw_residuals, team_gw_index,
                                                  fallback_residuals, args.inner_sims, args.variance_scale)
        pred = sum(p for (pg1, pg2), p in pred_dist.items() if pg1 > pg2)
        records.append((pred, g1 > g2, g1 == g2))
        score_records.append((pred_dist, (g1, g2)))

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

    print_scoreline_calibration(score_records)


def print_scoreline_calibration(score_records: list[tuple[dict[tuple[int, int], float], tuple[int, int]]]) -> None:
    """Checks whether the full simulated (g1, g2) scoreline distribution
    is calibrated, not just the win/loss call the section above checks.
    Considered and rejected a Dixon-Coles-style parametric correction
    for this (see the README) -- that's a fix for independent-Poisson
    models fit to marginal scoring rates, not for a Monte Carlo
    simulation that already produces whatever joint scoreline
    correlation the underlying player-score simulation has. What's
    actually worth checking is whether that simulated distribution
    matches reality, which is what this does directly against the same
    real historical outcomes used above -- no parametric model or
    correction layered on top."""
    n = len(score_records)

    # Multiclass Brier (quadratic score): for a predicted distribution p
    # over scorelines and a one-hot real outcome, sum((p_i - y_i)^2) over
    # every scoreline simplifies to sum(p_i^2) - 2*p_real + 1, since y is
    # one-hot (sum(y_i^2) = 1, sum(p_i*y_i) = p_real) -- exact even
    # though pred_dist only lists scorelines that actually occurred
    # in-sim (everything else is implicitly probability 0, contributing
    # nothing to sum(p_i^2)).
    multiclass_brier = statistics.mean(
        sum(p * p for p in pred_dist.values()) - 2 * pred_dist.get(real, 0.0) + 1.0
        for pred_dist, real in score_records)
    print(f"\n=== Full scoreline distribution calibration ===")
    print(f"Multiclass Brier score: {multiclass_brier:.4f} (0 = perfect; ranges higher than "
          f"the binary win/loss Brier above since there are many more possible outcomes "
          f"to get exactly right, not just 2)")

    # Pooled probability-bucket calibration: every (trial, scoreline)
    # pair the model assigned a nonzero probability to is one data
    # point -- "did THIS specific scoreline happen in THIS trial" --
    # exactly generalizing the win-probability bucket check above to
    # every possible outcome instead of just win/loss.
    pooled: list[tuple[float, bool]] = []
    for pred_dist, real in score_records:
        for score, p in pred_dist.items():
            pooled.append((p, score == real))

    print(f"\n{len(pooled)} (trial, predicted-scoreline) data points pooled across "
          f"{n} trials.")
    print(f"{'bucket':>12}  {'n':>7}  {'realized%':>10}  {'avg predicted%':>15}")
    for lo in range(0, 100, 10):
        hi = lo + 10
        bucket = [(p, hit) for p, hit in pooled if lo / 100 <= p < hi / 100 or (hi == 100 and p == 1.0)]
        if not bucket:
            continue
        realized = 100 * sum(1 for _, hit in bucket if hit) / len(bucket)
        avg_pred = 100 * statistics.mean(p for p, _ in bucket)
        print(f"{lo:>4}-{hi:<3}%    {len(bucket):>7}  {realized:>9.1f}%  {avg_pred:>14.1f}%")

    # Curated common/low-scoreline frequency check -- the specific thing
    # Dixon-Coles corrects for in a parametric model (0-0/1-0/0-1/1-1
    # systematically off under an independence assumption). Checked
    # directly here instead: for each of these scorelines, average
    # predicted probability across ALL trials vs the fraction of trials
    # where it was the actual outcome.
    common = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 1), (1, 2), (2, 2)]
    print(f"\nCommon-scoreline check (the Dixon-Coles low-score concern, "
          f"checked directly against real outcomes instead of assumed):")
    print(f"{'scoreline':>10}  {'avg predicted%':>15}  {'realized%':>10}")
    for score in common:
        avg_pred = 100 * statistics.mean(pred_dist.get(score, 0.0) for pred_dist, _ in score_records)
        realized = 100 * sum(1 for _, real in score_records if real == score) / n
        print(f"{score[0]}-{score[1]:>8}  {avg_pred:>14.1f}%  {realized:>9.1f}%")
    other_pred = 100 * statistics.mean(
        1.0 - sum(pred_dist.get(s, 0.0) for s in common) for pred_dist, _ in score_records)
    other_real = 100 * sum(1 for _, real in score_records if real not in common) / n
    print(f"{'other':>10}  {other_pred:>14.1f}%  {other_real:>9.1f}%")


if __name__ == "__main__":
    main()
