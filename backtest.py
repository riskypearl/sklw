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
    """Returns element_id -> {'position': str, 'team': str, 'by_gw': {gw:
    (total_points, xP)}}, restricted to the lowest `pool_size` distinct
    element IDs found."""
    by_player: dict[int, dict] = {}
    for row in rows:
        try:
            eid = int(row["element"])
            gw = int(row["GW"])
            pts = float(row["total_points"])
            xp = float(row["xP"])
        except (ValueError, KeyError):
            continue
        entry = by_player.setdefault(eid, {"position": row["position"],
                                            "team": row["team"], "by_gw": {}})
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


def _finalize_member(squad_ids: list[int], eligible: dict[int, dict], target_gw: int) -> dict:
    """Shared scoring step: captain = highest mean_proj (decision-time,
    not hindsight), sums actual/mean/std with the captain doubled."""
    signals = [(eid, *player_signals(eligible[eid], target_gw)) for eid in squad_ids]
    captain_id = max(signals, key=lambda s: s[2])[0]  # highest mean_proj

    actual_total = 0.0
    mean_total = 0.0
    std_total = 0.0
    for eid, actual, mean_proj, ceiling_std in signals:
        mult = 2 if eid == captain_id else 1
        actual_total += actual * mult
        mean_total += mean_proj * mult
        std_total += ceiling_std * mult
    return {"actual": actual_total, "mean_proj": mean_total, "std_proj": std_total}


def build_member(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
                  target_gw: int) -> dict:
    """One simulated club member = 11 real players drawn from the pool."""
    return _finalize_member(rng.sample(pool_ids, 11), eligible, target_gw)


def build_member_stacked(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
                          target_gw: int, stack_size: int = 3) -> dict:
    """Like build_member, but deliberately stacks `stack_size` defenders
    from the SAME real team (a classic FPL strategy -- a strong defensive
    team's defenders' clean-sheet points are correlated, so this raises
    the member's own score variance without necessarily sacrificing mean,
    unlike swapping to individually weaker/streakier players)."""
    by_team: dict[str, list[int]] = defaultdict(list)
    for eid in pool_ids:
        if eligible[eid]["position"] == "DEF":
            by_team[eligible[eid]["team"]].append(eid)
    candidate_teams = [t for t, defs in by_team.items() if len(defs) >= stack_size]
    if not candidate_teams:
        return build_member(rng, pool_ids, eligible, target_gw)

    team = rng.choice(candidate_teams)
    stacked_defs = rng.sample(by_team[team], stack_size)
    rest_pool = [eid for eid in pool_ids if eid not in stacked_defs]
    squad_ids = stacked_defs + rng.sample(rest_pool, 11 - stack_size)
    return _finalize_member(squad_ids, eligible, target_gw)


def build_member_diversified(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
                              target_gw: int) -> dict:
    """Like build_member, but deliberately avoids picking more than one
    defender from the same real team -- the opposite bet from stacking,
    minimizing correlation between the member's own players."""
    shuffled = pool_ids[:]
    rng.shuffle(shuffled)
    squad_ids: list[int] = []
    used_def_teams: set[str] = set()
    for eid in shuffled:
        if len(squad_ids) == 11:
            break
        if eligible[eid]["position"] == "DEF":
            team = eligible[eid]["team"]
            if team in used_def_teams:
                continue
            used_def_teams.add(team)
        squad_ids.append(eid)
    if len(squad_ids) < 11:  # pool too thin to satisfy the constraint, top up
        remaining = [eid for eid in pool_ids if eid not in squad_ids]
        squad_ids += remaining[: 11 - len(squad_ids)]
    return _finalize_member(squad_ids, eligible, target_gw)


def defender_pairwise_correlation(history: dict[int, dict]) -> tuple[float, float, int, int]:
    """Real measured Pearson correlation of GW-by-GW total_points between
    pairs of defenders, split same-team vs different-team, using each
    pair's overlapping GWs this season. Returns (avg_same_team_corr,
    avg_diff_team_corr, n_same_pairs, n_diff_pairs)."""
    defenders = [eid for eid, e in history.items() if e["position"] == "DEF"]
    same_team, diff_team = [], []
    for i in range(len(defenders)):
        for j in range(i + 1, len(defenders)):
            a, b = defenders[i], defenders[j]
            gws_a, gws_b = history[a]["by_gw"], history[b]["by_gw"]
            common = sorted(set(gws_a) & set(gws_b))
            if len(common) < 10:
                continue
            xa = [gws_a[gw][0] for gw in common]
            xb = [gws_b[gw][0] for gw in common]
            try:
                corr = statistics.correlation(xa, xb)
            except statistics.StatisticsError:
                continue
            (same_team if history[a]["team"] == history[b]["team"] else diff_team).append(corr)
    avg_same = statistics.mean(same_team) if same_team else float("nan")
    avg_diff = statistics.mean(diff_team) if diff_team else float("nan")
    return avg_same, avg_diff, len(same_team), len(diff_team)


def individual_score(member: dict, k: float) -> float:
    """mean + k*std -- the H2H-role selection score. k=0 reduces to plain
    mean (method A); k>0 weights toward higher-variance/ceiling members."""
    return member["mean_proj"] + k * member["std_proj"]


def build_club(rng: random.Random, pool_ids: list[int], eligible: dict[int, dict],
               target_gw: int) -> list[dict]:
    return [build_member(rng, pool_ids, eligible, target_gw) for _ in range(16)]


def assign_method_a(members: list[dict]) -> dict:
    ranked = sorted(range(16), key=lambda i: -members[i]["mean_proj"])
    return {"strikers": ranked[0:2], "gk": ranked[2:3], "squad": ranked[3:14], "bench": ranked[14:16]}


def assign_method_b(members: list[dict], k: float) -> dict:
    by_ceiling = sorted(range(16), key=lambda i: -individual_score(members[i], k))
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
    """SKLW's own rule: a goal for beating the opponent at all, +1 more
    per full 30-point margin beyond that (same base+bonus shape as
    h2h_goals, just a wider band). This is the rules doc's literal
    wording; confirmed against 4 independent real SKLW results once the
    GK-scoring bug in match_goals (below) was fixed -- that bug, not
    this formula, was what made an earlier version look like it needed
    no base goal."""
    margin = a_total - b_total
    return int(margin // 30) + 1 if margin >= 1 else 0


def match_goals(club: list[dict], roles: dict, opp: list[dict], opp_roles: dict) -> int:
    """Goals FOR 'club'. The GK does NOT independently score goals of its
    own -- only Strikers score, against the opposing GK. A GK's role is
    purely to be a high-scoring target that's hard for the opponent's
    Strikers to beat; that's already fully captured by the Strikers' own
    H2H calculation on the OTHER side. Confirmed against 4 independent
    real SKLW results that giving the GK a mirrored scoring mechanic
    against the opposing Strikers (an earlier version of this function)
    is wrong -- removing it (and only it) was what made all 4 reproduce
    exactly. Call twice with sides swapped to get both scorelines."""
    goals = 0
    for si in roles["strikers"]:
        goals += h2h_goals(club[si]["actual"], opp[opp_roles["gk"][0]]["actual"])
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
    ap.add_argument("--k-values", default="0,0.5,1,1.5,2,2.5,3,4",
                     help="comma-separated ceiling weights (mean + k*std) to sweep, "
                          "using the SAME random draws for each so only k changes")
    ap.add_argument("--covariance", action="store_true",
                     help="also run the same-team-defender-stacking comparison "
                          "(stacked vs diversified vs normal member construction)")
    args = ap.parse_args()

    print(f"Fetching {args.season} season data from GitHub archive...")
    rows = fetch_season_rows(args.season)
    history = build_player_history(rows, args.pool_size)
    print(f"Player pool: {len(history)} players (lowest element IDs)")

    rng = random.Random(args.seed)
    target_gws = list(range(args.min_gw, 39))

    trials = []  # (club1, club2, roles2_a, diff1_a) -- fixed once, independent of k
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
        # NET goal differential, not one-sided attacking output: club1's own
        # GK choice also determines how many goals club2's (fixed) Strikers
        # score AGAINST club1, so that has to be in the comparison too, or a
        # strong-GK strategy's entire defensive benefit is invisible to this
        # backtest by construction.
        for_a = match_goals(club1, roles1_a, club2, roles2_a)
        against_a = match_goals(club2, roles2_a, club1, roles1_a)
        diff_a = for_a - against_a
        trials.append((club1, club2, roles2_a, diff_a))

    n = len(trials)
    print(f"\n{n} valid trials. Method A (k=0, plain mean) baseline avg NET goal diff = "
          f"{statistics.mean(d for *_, d in trials):.3f}\n")
    print(f"{'k':>5}  {'avg net diff':>12}  {'B > A':>8}  {'A > B':>8}  {'tied':>8}")

    best_k, best_avg = None, float("-inf")
    for k_str in args.k_values.split(","):
        k = float(k_str)
        diffs_b = []
        wins_a = draws = wins_b = 0
        for club1, club2, roles2_a, diff_a in trials:
            roles1_b = assign_method_b(club1, k)
            for_b = match_goals(club1, roles1_b, club2, roles2_a)
            against_b = match_goals(club2, roles2_a, club1, roles1_b)
            diff_b = for_b - against_b
            diffs_b.append(diff_b)
            if diff_b > diff_a:
                wins_b += 1
            elif diff_b < diff_a:
                wins_a += 1
            else:
                draws += 1
        avg = statistics.mean(diffs_b)
        print(f"{k:>5.1f}  {avg:>12.3f}  {100*wins_b/n:>7.1f}%  {100*wins_a/n:>7.1f}%  {100*draws/n:>7.1f}%")
        if avg > best_avg:
            best_avg, best_k = avg, k

    print(f"\nBest k in this sweep: {best_k} (avg net diff {best_avg:.3f} vs "
          f"{statistics.mean(d for *_, d in trials):.3f} for plain mean)")
    print(f"Suggested rule: individual-role score = mean_projection + {best_k} * "
          f"stdev(player's own recent real scores)")

    if args.covariance:
        print(f"\n=== Same-team defender stacking ===")
        avg_same, avg_diff, n_same, n_diff = defender_pairwise_correlation(history)
        print(f"Real measured correlation of GW-by-GW scores this season:")
        print(f"  same-team defender pairs:      {avg_same:.3f}  (n={n_same} pairs)")
        print(f"  different-team defender pairs: {avg_diff:.3f}  (n={n_diff} pairs)")

        builders = {
            "normal (random 11)": build_member,
            "stacked (3 DEF same team)": lambda rng, pids, elig, gw: build_member_stacked(rng, pids, elig, gw, 3),
            "diversified (no 2 DEF same team)": build_member_diversified,
        }
        cov_results: dict[str, list[int]] = {name: [] for name in builders}
        cov_trials = 0
        for _ in range(args.trials):
            target_gw = rng.choice(target_gws)
            eligible = eligible_for_target(history, target_gw, min_prior=5)
            pool_ids = list(eligible)
            if len(pool_ids) < 22:
                continue
            club2 = build_club(rng, pool_ids, eligible, target_gw)
            roles2_a = assign_method_a(club2)
            for name, builder in builders.items():
                club1 = [builder(rng, pool_ids, eligible, target_gw) for _ in range(16)]
                roles1_a = assign_method_a(club1)
                # net diff, same reasoning as the k-sweep above -- one-sided
                # "goals for" alone hides any construction's effect on the
                # defensive side (club2's Strikers vs club1's own GK).
                gfor = match_goals(club1, roles1_a, club2, roles2_a)
                gagainst = match_goals(club2, roles2_a, club1, roles1_a)
                cov_results[name].append(gfor - gagainst)
            cov_trials += 1

        print(f"\n{cov_trials} valid trials (each construction faces the SAME "
              f"opponent draw per trial):")
        for name, diffs in cov_results.items():
            print(f"  {name:<34} avg net goal diff = {statistics.mean(diffs):.3f}")


if __name__ == "__main__":
    main()
