"""Backtest: does ceiling-weighting Strikers/GK beat plain greedy-by-mean?

Standalone script, separate from sklw_lineup.py/draft_lineup.py -- tests
the ROLE-ASSIGNMENT method itself, not any specific real club or season.

We have no real historical SKLW match data (opponent lineups are private,
tournament-only) and no historical pre-GW projections for the current
season (FPL only exposes the CURRENT ep_next, not what it was before a
past GW). So this uses a LAST-SEASON archive instead of the live FPL API:
real per-player per-GW data (including FPL's own historical 'xP' field --
literally what FPL projected before that GW, the direct historical
equivalent of ep_next) from the public vaastav/Fantasy-Premier-League
GitHub archive. This is a deliberate scope exception to this project's
"public FPL API only" rule, made because it's the only way to get real
projections AND real outcomes for gameweeks that have actually finished.

Method:
  - Player pool restricted to low FPL element IDs (--pool-size) -- early
    IDs tend to be established, frequently-selected players, closer to
    what an engaged SKLW member would actually have owned than a uniform
    draw across the full ~700-player list (which includes a lot of
    fringe/rarely-picked squad depth).
  - For a target GW, each simulated "member" = 11 real players randomly
    drawn from the pool, captained by whichever has the highest
    pre-GW projection (xP) among them (mirrors real captaincy -- picked
    on projection, not hindsight). A "ceiling" signal per player is the
    standard deviation of their own realized scores in GWs BEFORE the
    target GW (no lookahead).
  - Two clubs of 16 such members are drawn per trial. Method A (plain
    greedy) ranks members by summed xP and fills Strikers/GK/Squad/Bench
    top-down. Method B (ceiling-weighted) picks GK + both Strikers by
    (mean + k*std) instead of mean alone, then ranks the rest by mean for
    Squad/Bench -- per the reasoning that H2H roles are capped-downside/
    uncapped-upside (blank at zero either way, unbounded goal bands), so
    variance helps there but gets diluted in the pooled Squad sum.
  - Each trial's REAL outcome scores (total_points, not projections)
    then get scored with SKLW's actual H2H/Squad goal-band rules. Club 1
    is assigned via method A in one run and method B in a paired re-run,
    with Club 2's assignment and both clubs' random draws held IDENTICAL
    across the pair -- isolating the effect of the method itself rather
    than random squad variation between trials.
"""
from __future__ import annotations

import argparse
import csv
import io
import random
import statistics
from collections import defaultdict

import requests

ARCHIVE_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"


def fetch_season_rows(season: str) -> list[dict]:
    url = f"{ARCHIVE_BASE}/{season}/gws/merged_gw.csv"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def build_player_history(rows: list[dict], pool_size: int) -> dict[int, dict]:
    """Returns element_id -> {'position': str, 'by_gw': {gw: (total_points, xP)}},
    restricted to the lowest `pool_size` distinct element IDs found."""
    by_player: dict[int, dict] = {}
    for row in rows:
        try:
            eid = int(row["element"])
            gw = int(row["GW"])
            pts = float(row["total_points"])
            xp = float(row["xP"])
        except (ValueError, KeyError):
            continue
        entry = by_player.setdefault(eid, {"position": row["position"], "by_gw": {}})
        entry["by_gw"][gw] = (pts, xp)

    pool_ids = sorted(by_player)[:pool_size]
    return {eid: by_player[eid] for eid in pool_ids}


def eligible_for_target(history: dict[int, dict], target_gw: int, min_prior: int) -> dict[int, dict]:
    """Players with real data at target_gw AND at least min_prior earlier GWs
    (needed for a meaningful pre-target 'ceiling'/std estimate)."""
    out = {}
    for eid, entry in history.items():
        if target_gw not in entry["by_gw"]:
            continue
        prior = [gw for gw in entry["by_gw"] if gw < target_gw]
        if len(prior) < min_prior:
            continue
        out[eid] = entry
    return out


def player_signals(entry: dict, target_gw: int) -> tuple[float, float, float]:
    """(actual, mean_proj, ceiling_std) for one player at target_gw. mean_proj
    is FPL's own real historical xP for that GW (a genuine decision-time
    signal, not hindsight). ceiling_std uses only GWs strictly before
    target_gw."""
    actual, mean_proj = entry["by_gw"][target_gw]
    prior_scores = [pts for gw, (pts, _) in entry["by_gw"].items() if gw < target_gw]
    ceiling_std = statistics.pstdev(prior_scores) if len(prior_scores) > 1 else 0.0
    return actual, mean_proj, ceiling_std


def build_member(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
                  target_gw: int) -> dict:
    """One simulated club member = 11 real players drawn from the pool,
    captained by whichever has the highest mean_proj (decision-time, not
    hindsight)."""
    squad_ids = rng.sample(pool_ids, 11)
    signals = [(eid, *player_signals(eligible[eid], target_gw)) for eid in squad_ids]
    captain_id = max(signals, key=lambda s: s[2])[0]  # highest mean_proj

    actual_total = 0.0
    mean_total = 0.0
    ceiling_total = 0.0
    for eid, actual, mean_proj, ceiling_std in signals:
        mult = 2 if eid == captain_id else 1
        actual_total += actual * mult
        mean_total += mean_proj * mult
        ceiling_total += (mean_proj + ceiling_std) * mult
    return {"actual": actual_total, "mean_proj": mean_total, "ceiling_proj": ceiling_total}


def build_club(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
               target_gw: int) -> list[dict]:
    return [build_member(rng, pool_ids, eligible, target_gw) for _ in range(16)]


def assign_method_a(members: list[dict]) -> dict:
    ranked = sorted(range(16), key=lambda i: -members[i]["mean_proj"])
    return {"strikers": ranked[0:2], "gk": ranked[2:3], "squad": ranked[3:14], "bench": ranked[14:16]}


def assign_method_b(members: list[dict]) -> dict:
    by_ceiling = sorted(range(16), key=lambda i: -members[i]["ceiling_proj"])
    top3 = by_ceiling[0:3]
    gk = [top3[0]]
    strikers = top3[1:3]
    remaining = [i for i in range(16) if i not in top3]
    by_mean = sorted(remaining, key=lambda i: -members[i]["mean_proj"])
    return {"strikers": strikers, "gk": gk, "squad": by_mean[0:11], "bench": by_mean[11:13]}


def h2h_goals(a_actual: float, b_actual: float) -> int:
    """Goals for the 'a' side of one individual H2H battle (SKLW's own
    rule: 1 goal per full 20-point margin, zero if b equals/outscores a)."""
    margin = a_actual - b_actual
    return int(margin // 20) + 1 if margin >= 1 else 0


def squad_goals(a_total: float, b_total: float) -> int:
    """SKLW's own rule: 1 goal per full 30-point margin, tie = 0 both sides."""
    margin = a_total - b_total
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals(club: list[dict], roles: dict, opp: list[dict], opp_roles: dict) -> int:
    goals = 0
    for si in roles["strikers"]:
        goals += h2h_goals(club[si]["actual"], opp[opp_roles["gk"][0]]["actual"])
    for oi in opp_roles["strikers"]:
        goals += h2h_goals(club[roles["gk"][0]]["actual"], opp[oi]["actual"])
    own_squad = sum(club[i]["actual"] for i in roles["squad"])
    opp_squad = sum(opp[i]["actual"] for i in opp_roles["squad"])
    goals += squad_goals(own_squad, opp_squad)
    return goals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2023-24", help="archive season folder, e.g. 2023-24")
    ap.add_argument("--pool-size", type=int, default=300,
                     help="restrict to the lowest N element IDs (established/engaged-manager proxy)")
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--min-gw", type=int, default=10, help="earliest target GW (needs prior history)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print(f"Fetching {args.season} season data from GitHub archive...")
    rows = fetch_season_rows(args.season)
    history = build_player_history(rows, args.pool_size)
    print(f"Player pool: {len(history)} players (lowest element IDs)")

    rng = random.Random(args.seed)
    target_gws = list(range(args.min_gw, 39))

    results_a, results_b = [], []
    wins_a = draws = wins_b = 0

    for _ in range(args.trials):
        target_gw = rng.choice(target_gws)
        eligible = eligible_for_target(history, target_gw, min_prior=5)
        pool_ids = list(eligible)
        if len(pool_ids) < 22:  # need at least 2*11 distinct players available
            continue

        club1 = build_club(rng, pool_ids, eligible, target_gw)
        club2 = build_club(rng, pool_ids, eligible, target_gw)

        roles2_a = assign_method_a(club2)
        roles1_a = assign_method_a(club1)
        roles1_b = assign_method_b(club1)

        goals1_a = match_goals(club1, roles1_a, club2, roles2_a)
        goals1_b = match_goals(club1, roles1_b, club2, roles2_a)

        results_a.append(goals1_a)
        results_b.append(goals1_b)
        if goals1_b > goals1_a:
            wins_b += 1
        elif goals1_b < goals1_a:
            wins_a += 1
        else:
            draws += 1

    n = len(results_a)
    print(f"\n=== {n} valid trials ===")
    print(f"Method A (plain greedy-by-mean):      avg goals = {statistics.mean(results_a):.3f}")
    print(f"Method B (ceiling-weighted H2H roles): avg goals = {statistics.mean(results_b):.3f}")
    print(f"\nHead-to-head (same random draw, method B vs method A):")
    print(f"  B scored MORE goals: {wins_b} ({100*wins_b/n:.1f}%)")
    print(f"  A scored MORE goals: {wins_a} ({100*wins_a/n:.1f}%)")
    print(f"  Tied:                {draws} ({100*draws/n:.1f}%)")


if __name__ == "__main__":
    main()
