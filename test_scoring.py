"""Regression tests for SKLW's core scoring rules.

This project deliberately duplicates small scoring helpers across
sklw_lineup.py / sklw_matchup.py / calibrate_matchup.py / backtest.py
(standalone-file convention -- no shared imports between the tools
themselves). That's a real drift risk: a fix applied to one copy can
silently fail to reach the others. These tests import all four files
directly (test-only, doesn't affect how the tools ship or run) and check
every copy against the same rules, so a future edit that breaks one
without the others gets caught here rather than by another 4-real-match
debugging session.

Locks in the two bugs that took real SKLW match results to find:
  - The GK does NOT independently score goals of its own (it's purely a
    defensive target for the opponent's Strikers -- see README).
  - Squad goals use a base-plus-bonus formula (1 goal just for winning
    the squad battle, +1 more per full 30-point margin beyond that), not
    a "bonus only" formula.
And the validated role-assignment priority: GK gets first pick (top
projected score), Strikers get the next 2 -- not the other way around
(see backtest.py's net-goal-differential comparison in the README).

Run: python3 -m unittest test_scoring.py -v
"""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import backtest
import build_clubs_json
import calibrate_matchup
import draft_lineup
import sklw_lineup
import sklw_matchup


class H2HGoalsTests(unittest.TestCase):
    """1 goal per full 20-point margin, 0 if the defender ties/wins.
    Same rule, three copies -- backtest.py, calibrate_matchup.py,
    sklw_matchup.py."""

    MODULES = (backtest, calibrate_matchup, sklw_matchup)

    def test_tie_is_zero(self):
        for mod in self.MODULES:
            self.assertEqual(mod.h2h_goals(50, 50), 0, mod.__name__)

    def test_defender_wins_is_zero(self):
        for mod in self.MODULES:
            self.assertEqual(mod.h2h_goals(30, 50), 0, mod.__name__)

    def test_margin_1_to_19_is_one_goal(self):
        for mod in self.MODULES:
            self.assertEqual(mod.h2h_goals(51, 50), 1, mod.__name__)
            self.assertEqual(mod.h2h_goals(69, 50), 1, mod.__name__)

    def test_margin_20_to_39_is_two_goals(self):
        for mod in self.MODULES:
            self.assertEqual(mod.h2h_goals(70, 50), 2, mod.__name__)
            self.assertEqual(mod.h2h_goals(89, 50), 2, mod.__name__)

    def test_margin_40_to_59_is_three_goals(self):
        for mod in self.MODULES:
            self.assertEqual(mod.h2h_goals(90, 50), 3, mod.__name__)


class SquadGoalsTests(unittest.TestCase):
    """1 goal for winning the squad battle at all, +1 more per full
    30-point margin beyond that. Same rule, three copies."""

    MODULES = (backtest, calibrate_matchup, sklw_matchup)

    def test_tie_is_zero(self):
        for mod in self.MODULES:
            self.assertEqual(mod.squad_goals(500, 500), 0, mod.__name__)

    def test_loser_is_zero(self):
        for mod in self.MODULES:
            self.assertEqual(mod.squad_goals(490, 500), 0, mod.__name__)

    def test_margin_1_to_29_is_one_goal(self):
        for mod in self.MODULES:
            self.assertEqual(mod.squad_goals(501, 500), 1, mod.__name__)
            self.assertEqual(mod.squad_goals(529, 500), 1, mod.__name__)

    def test_margin_30_to_59_is_two_goals(self):
        for mod in self.MODULES:
            self.assertEqual(mod.squad_goals(530, 500), 2, mod.__name__)
            self.assertEqual(mod.squad_goals(559, 500), 2, mod.__name__)


class GKDoesNotIndependentlyScoreTests(unittest.TestCase):
    """The bug that took 4 real SKLW match results to find and fix: an
    earlier version of match_goals gave the GK a mirrored H2H mechanic
    against the opponent's Strikers, on top of the Strikers' own H2H
    battle. Construct a case where the GK's own score is enormous but
    every Striker on both sides blanks -- if match_goals ever returns a
    nonzero goal from that, the bug is back."""

    def test_backtest_match_goals(self):
        club = [{"actual": 0.0} for _ in range(16)]
        club[0]["actual"] = 1000.0  # "GK" -- must NOT independently score
        opp = [{"actual": 0.0} for _ in range(16)]
        roles = {"strikers": [1, 2], "gk": [0], "squad": list(range(3, 14)), "bench": [14, 15]}
        opp_roles = {"strikers": [1, 2], "gk": [0], "squad": list(range(3, 14)), "bench": [14, 15]}
        self.assertEqual(backtest.match_goals(club, roles, opp, opp_roles), 0)

    def test_calibrate_matchup_match_goals(self):
        scores = [0.0] * 16
        scores[0] = 1000.0
        opp_scores = [0.0] * 16
        roles = {"strikers": [1, 2], "gk": [0], "squad": list(range(3, 14)), "bench": [14, 15]}
        opp_roles = {"strikers": [1, 2], "gk": [0], "squad": list(range(3, 14)), "bench": [14, 15]}
        self.assertEqual(calibrate_matchup.match_goals(scores, roles, opp_scores, opp_roles), 0)

    def test_sklw_matchup_match_goals_breakdown_has_no_gk_key(self):
        names = [f"m{i}" for i in range(16)]
        scores = {n: 0.0 for n in names}
        scores["m0"] = 1000.0  # "GK" -- must NOT independently score
        opp_names = [f"o{i}" for i in range(16)]
        opp_scores = {n: 0.0 for n in opp_names}
        roles = {"strikers": ["m1", "m2"], "gk": ["m0"], "squad": names[3:14], "bench": names[14:16]}
        opp_roles = {"strikers": ["o1", "o2"], "gk": ["o0"], "squad": opp_names[3:14], "bench": opp_names[14:16]}
        breakdown = sklw_matchup.match_goals_breakdown(scores, roles, opp_scores, opp_roles)
        self.assertNotIn("gk", breakdown)
        self.assertEqual(set(breakdown), {"strikers", "squad"})
        self.assertEqual(sum(breakdown.values()), 0)
        self.assertEqual(sklw_matchup.match_goals(scores, roles, opp_scores, opp_roles), 0)

    def test_our_gk_score_still_helps_defensively_via_their_side(self):
        """A high GK score doesn't score FOR us, but it should still deny
        the OPPONENT's Strikers when THEIR goals are computed against
        our GK -- that's the GK's real (defensive-only) value."""
        names = [f"m{i}" for i in range(16)]
        scores = {n: 0.0 for n in names}
        scores["m0"] = 1000.0  # our GK
        opp_names = [f"o{i}" for i in range(16)]
        opp_scores = {n: 50.0 for n in opp_names}  # opponent Strikers score 50
        roles = {"strikers": ["m1", "m2"], "gk": ["m0"], "squad": names[3:14], "bench": names[14:16]}
        opp_roles = {"strikers": ["o1", "o2"], "gk": ["o0"], "squad": opp_names[3:14], "bench": opp_names[14:16]}
        # Opponent's goals FOR them = their Strikers (50 each) vs OUR GK (1000) -> denied.
        opp_breakdown = sklw_matchup.match_goals_breakdown(opp_scores, opp_roles, scores, roles)
        self.assertEqual(opp_breakdown["strikers"], 0)


class RoleAssignmentPriorityTests(unittest.TestCase):
    """Validated in backtest.py via net goal differential: GK gets first
    pick (denies 2 opposing H2H battles at once), Strikers get the next
    2. This was flipped and flipped back once already this project --
    lock in the direction so it can't silently flip again."""

    def test_calibrate_matchup_assign_roles(self):
        projected = list(range(16))  # index 15 is highest
        roles = calibrate_matchup.assign_roles(projected)
        self.assertEqual(roles["gk"], [15])
        self.assertEqual(sorted(roles["strikers"]), [13, 14])
        self.assertEqual(len(roles["squad"]), 11)
        self.assertEqual(len(roles["bench"]), 2)
        all_assigned = roles["gk"] + roles["strikers"] + roles["squad"] + roles["bench"]
        self.assertEqual(sorted(all_assigned), list(range(16)))

    def test_sklw_matchup_assign_roles(self):
        club = {f"m{i}": {"projected": float(i)} for i in range(16)}
        roles = sklw_matchup.assign_roles(club)
        self.assertEqual(roles["gk"], ["m15"])
        self.assertEqual(sorted(roles["strikers"]), ["m13", "m14"])
        self.assertEqual(len(roles["squad"]), 11)
        self.assertEqual(len(roles["bench"]), 2)
        all_assigned = roles["gk"] + roles["strikers"] + roles["squad"] + roles["bench"]
        self.assertEqual(sorted(all_assigned), sorted(club))

    def test_strikers_and_gk_excluded_from_squad(self):
        """The Squad battle is 11 DISTINCT members -- neither the GK nor
        either Striker also counts toward the Squad total."""
        club = {f"m{i}": {"projected": float(i)} for i in range(16)}
        roles = sklw_matchup.assign_roles(club)
        self.assertFalse(set(roles["gk"]) & set(roles["squad"]))
        self.assertFalse(set(roles["strikers"]) & set(roles["squad"]))
        self.assertFalse(set(roles["gk"]) & set(roles["strikers"]))


def _run_suggest_lineup(scores, fh_names=None, ceiling=None, k=0.5, never_bench=None):
    """suggest_lineup only prints -- capture stdout and pull out which
    names landed under each section header, in order."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        sklw_lineup.suggest_lineup(scores, fh_names, ceiling, k, never_bench)
    sections: dict[str, list[str]] = {}
    current = None
    for line in buf.getvalue().splitlines():
        header = line.strip().rstrip(":")
        if header in ("Strikers", "Goalkeeper", "Squad", "Bench"):
            current = header
            sections[current] = []
        elif current and line.startswith("  ") and ":" in line and not line.strip().startswith("-->"):
            sections[current].append(line.strip().split(":")[0])
    return sections


class ResolvePlayersTests(unittest.TestCase):
    """resolve_players (used by --wildcard's --squad, --transfer's
    --out/--in) hit real false-positive substring collisions in
    practice: 'Egan' matched 'R-EGAN-Slater' too, 'Saka' matched
    'Wan-Bis-SAKA' and '-SAKA-moto' too. Fixed with tiered matching
    (exact surname first, falling back to substring only when nothing
    matches exactly) -- these lock in that fix and that genuine
    ambiguity (two real players who really do share a surname) still
    correctly errors rather than silently picking one."""

    BOOTSTRAP = {"elements": [
        {"id": 277, "first_name": "John", "second_name": "Egan"},
        {"id": 290, "first_name": "Regan", "second_name": "Slater"},
        {"id": 12, "first_name": "Bukayo", "second_name": "Saka"},
        {"id": 611, "first_name": "Aaron", "second_name": "Wan-Bissaka"},
        {"id": 185, "first_name": "Tatsuhiro", "second_name": "Sakamoto"},
        {"id": 154, "first_name": "Cole", "second_name": "Palmer"},
        {"id": 301, "first_name": "Alex", "second_name": "Palmer"},
        {"id": 567, "first_name": "Lukás", "second_name": "Hornícek"},
    ]}

    def test_surname_substring_of_another_first_name_resolves_uniquely(self):
        self.assertEqual(sklw_lineup.resolve_players(self.BOOTSTRAP, "Egan"), [277])

    def test_surname_substring_of_another_surname_resolves_uniquely(self):
        self.assertEqual(sklw_lineup.resolve_players(self.BOOTSTRAP, "Saka"), [12])

    def test_genuine_surname_ambiguity_still_errors(self):
        with self.assertRaises(SystemExit):
            sklw_lineup.resolve_players(self.BOOTSTRAP, "Palmer")

    def test_accent_folding_still_works(self):
        self.assertEqual(sklw_lineup.resolve_players(self.BOOTSTRAP, "Hornicek"), [567])


class FindSolioCsvFreshnessTests(unittest.TestCase):
    """find_solio_csv() is duplicated in sklw_lineup.py/sklw_matchup.py/
    draft_lineup.py. An earlier version always preferred a local
    'solio.csv' unconditionally over anything in Downloads -- meaning a
    stale local file left over from a previous week silently blocked a
    freshly downloaded export from ever being picked up, defeating the
    whole point of the weekly drop-a-fresh-export-and-run workflow. Now
    it compares modification times and picks whichever is actually
    newer. Runs against all three copies to catch drift."""

    MODULES = (sklw_lineup, sklw_matchup, draft_lineup)
    SOLIO_HEADER = "Pos,ID,Name,BV,SV,Team,1_xMins,1_Pts\n"
    SOLIO_ROW = "FWD,1,Haaland,15,15,MCI,90,10\n"

    def _make_csv(self, path: Path, mtime_offset: float) -> None:
        path.write_text(self.SOLIO_HEADER + self.SOLIO_ROW)
        t = time.time() + mtime_offset
        os.utime(path, (t, t))

    def _run_in(self, cwd_dir: str, home: Path, mod):
        real_cwd = os.getcwd()  # captured BEFORE any patching/chdir
        os.chdir(cwd_dir)
        try:
            with mock.patch.object(Path, "home", return_value=home):
                return mod.find_solio_csv()
        finally:
            os.chdir(real_cwd)

    def test_fresh_downloads_file_beats_stale_local_solio_csv(self):
        for mod in self.MODULES:
            with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as cwd_dir:
                home = Path(home_dir)
                (home / "Downloads").mkdir()
                fresh = home / "Downloads" / "export.csv"
                self._make_csv(fresh, mtime_offset=0)
                stale_local = Path(cwd_dir) / "solio.csv"
                self._make_csv(stale_local, mtime_offset=-100000)

                result = self._run_in(cwd_dir, home, mod)
                self.assertEqual(result, fresh, mod.__name__)

    def test_fresh_local_solio_csv_beats_stale_downloads_file(self):
        for mod in self.MODULES:
            with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as cwd_dir:
                home = Path(home_dir)
                (home / "Downloads").mkdir()
                stale_downloads = home / "Downloads" / "export.csv"
                self._make_csv(stale_downloads, mtime_offset=-100000)
                fresh_local = Path(cwd_dir) / "solio.csv"
                self._make_csv(fresh_local, mtime_offset=0)

                result = self._run_in(cwd_dir, home, mod)
                self.assertEqual(result, Path("solio.csv"), mod.__name__)


class ClubsJsonTests(unittest.TestCase):
    """build_clubs_json.py converts the league master list CSV into
    clubs.json (club name -> generic-labeled 16-manager roster, no real
    names/handles -- see that script's docstring for why), and
    sklw_matchup.py's --opponent flag looks a club up from it. All
    synthetic data here -- no real manager IDs -- since clubs.json
    itself is deliberately kept out of git."""

    def _write_csv(self, path):
        path.write_text(
            "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
            "M1,Club Alpha,@a1,111,Team A1,Alice\n"
            "M1,Club Alpha,@a2,222,Team A2,Bob\n"
            "M1,Club Beta,@b1,333,Team B1,Carol\n"
        )

    def test_build_clubs_strips_names_keeps_ids(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            self._write_csv(csv_path)
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {
            "Club Alpha": {"Manager1": 111, "Manager2": 222},
            "Club Beta": {"Manager1": 333},
        })
        # no real names, handles, or team names anywhere in the output
        dumped = str(clubs)
        for leaked in ("Alice", "Bob", "Carol", "@a1", "Team A1"):
            self.assertNotIn(leaked, dumped)

    def test_opponent_fuzzy_match_resolves_uniquely(self):
        with tempfile.TemporaryDirectory() as d:
            clubs_path = Path(d) / "clubs.json"
            clubs_path.write_text(json.dumps({
                "Fried Rice Eater": {"Manager1": 1, "Manager2": 2},
                "Algorithm & Blues": {"Manager1": 3, "Manager2": 4},
            }))
            roster = sklw_matchup.load_club_roster(str(clubs_path), "Fried Rice")
        self.assertEqual(roster, {"Manager1": 1, "Manager2": 2})

    def test_opponent_ambiguous_match_errors(self):
        with tempfile.TemporaryDirectory() as d:
            clubs_path = Path(d) / "clubs.json"
            clubs_path.write_text(json.dumps({
                "Set Piece Again OLE": {"Manager1": 1},
                "Kahn You Feel The Low Tonight": {"Manager1": 2},
            }))
            with self.assertRaises(SystemExit):
                sklw_matchup.load_club_roster(str(clubs_path), "o")

    def test_opponent_missing_file_errors_not_crashes(self):
        with self.assertRaises(SystemExit):
            sklw_matchup.load_club_roster("/nonexistent/clubs.json", "anything")


class NeverBenchTests(unittest.TestCase):
    """--never-bench guarantees a manager a Squad slot even if their
    score would otherwise land them in Bench, by swapping them with the
    current weakest Squad member. Requested live: a captain didn't want
    to be benched by pure EV ranking. Doesn't touch GK/Strikers."""

    def setUp(self):
        self.scores = [(f"m{i}", 100.0 - i) for i in range(16)]  # m14/m15 naturally benched

    def test_protected_manager_rescued_from_bench(self):
        sections = _run_suggest_lineup(self.scores, never_bench={"m15"})
        self.assertIn("m15", sections["Squad"])
        self.assertNotIn("m15", sections["Bench"])
        self.assertEqual(len(sections["Squad"]), 11)
        self.assertEqual(len(sections["Bench"]), 2)

    def test_two_protected_managers_both_rescued(self):
        """The bug this locks in: rescuing the 2nd protected manager must
        not re-bench the 1st one just because it's now the weakest
        Squad member."""
        sections = _run_suggest_lineup(self.scores, never_bench={"m14", "m15"})
        self.assertIn("m14", sections["Squad"])
        self.assertIn("m15", sections["Squad"])
        self.assertNotIn("m14", sections["Bench"])
        self.assertNotIn("m15", sections["Bench"])

    def test_unknown_name_warns_but_does_not_crash(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            sklw_lineup.suggest_lineup(self.scores, never_bench={"nonexistent"})
        self.assertIn("WARNING", buf.getvalue())

    def test_manager_already_in_squad_is_a_no_op(self):
        sections = _run_suggest_lineup(self.scores, never_bench={"m5"})
        self.assertIn("m5", sections["Squad"])
        self.assertEqual(len(sections["Squad"]), 11)
        self.assertEqual(len(sections["Bench"]), 2)


class WildcardScoringTests(unittest.TestCase):
    """apply_wildcard sets every pick's multiplier to 1, including the
    bench (a hand-typed squad has no real starter/bench distinction of
    its own). project_manager_score decides who's a "starter" by
    multiplier>0 -- correct for real FPL data (bench genuinely has
    multiplier 0), but for a wildcard-applied squad that means it
    silently sums all 15 players instead of 11. Hit in practice: --explain
    on a wildcarded manager showed an inflated score with all 4 bench
    players folded in, even though the printed labels correctly said
    which were bench. project_best_xi_score (what the main scoring loop
    ALWAYS uses for a wildcard entry) doesn't have this problem -- it
    computes the best valid XI from scratch, ignoring multiplier
    entirely. This is why --explain now uses best-xi for wildcard
    entries too instead of the plain (buggy, for this case)
    project_manager_score path."""

    def _build_squad(self):
        players, points, eid = {}, {}, 1
        for pos, count, base in [(1, 2, 10), (2, 5, 5), (3, 5, 5), (4, 3, 5)]:
            for i in range(count):
                players[eid] = {"first_name": "F", "second_name": f"P{eid}", "element_type": pos}
                points[eid] = float(base + i)
                eid += 1
        picks_data = sklw_lineup.apply_wildcard({"picks": [{"element": i} for i in range(1, eid)]},
                                                 list(range(1, eid)))
        return picks_data, players, points, eid

    def test_project_manager_score_wrongly_includes_bench_for_wildcard(self):
        picks_data, players, points, eid = self._build_squad()
        naive = sklw_lineup.project_manager_score(picks_data, points)
        correct = sklw_lineup.project_best_xi_score(picks_data, players, points)
        # documents the bug: naive sums ~all 15, correct only 11 + captain
        self.assertNotEqual(naive, correct)
        self.assertGreater(naive, correct)

    def test_project_best_xi_score_correctly_excludes_bench(self):
        picks_data, players, points, eid = self._build_squad()
        starters, captain = sklw_lineup.pick_best_eleven(picks_data, players, points)
        self.assertEqual(len(starters), 11)
        bench = [i for i in range(1, eid) if i not in starters]
        self.assertEqual(len(bench), 4)
        # the 2 weakest of each outfield position group must be benched
        self.assertIn(1, bench)  # weakest GK (point value 10, other GK is 11)
        self.assertIn(3, bench)  # weakest DEF


class BacktestMethodCTests(unittest.TestCase):
    """assign_method_c (GK-by-floor, Strikers-by-ceiling) was tested
    against real historical data and REJECTED -- it never beat method B
    (uniform ceiling-weighting) at any k_gk, and got worse than plain
    mean once k_gk went much above 0 (see README). Not used by any live
    tool. This just locks in that the function itself stays well-formed
    (a valid 16-way partition) so it doesn't bitrot silently -- it's
    kept in backtest.py as a documented negative result, not dead code
    to delete."""

    def test_valid_partition(self):
        members = [{"mean_proj": float(i), "std_proj": float(15 - i) % 7} for i in range(16)]
        roles = backtest.assign_method_c(members, k_gk=0.5, k_s=0.5)
        self.assertEqual(len(roles["gk"]), 1)
        self.assertEqual(len(roles["strikers"]), 2)
        self.assertEqual(len(roles["squad"]), 11)
        self.assertEqual(len(roles["bench"]), 2)
        all_assigned = roles["gk"] + roles["strikers"] + roles["squad"] + roles["bench"]
        self.assertEqual(sorted(all_assigned), list(range(16)))


class SuggestLineupPriorityTests(unittest.TestCase):
    """sklw_lineup.py's own copy of the GK-first/Strikers-next priority,
    plus the Free Hit override ordering (also GK-first)."""

    def setUp(self):
        self.scores = [(f"m{i}", float(15 - i)) for i in range(16)]  # m0 highest

    def test_no_fh_gk_gets_top_pick(self):
        sections = _run_suggest_lineup(self.scores)
        self.assertEqual(sections["Goalkeeper"], ["m0"])
        self.assertEqual(sections["Strikers"], ["m1", "m2"])
        self.assertEqual(len(sections["Squad"]), 11)
        self.assertEqual(len(sections["Bench"]), 2)

    def test_one_fh_manager_forced_into_gk(self):
        sections = _run_suggest_lineup(self.scores, fh_names={"m10"})
        self.assertEqual(sections["Goalkeeper"], ["m10"])
        self.assertEqual(sections["Strikers"], ["m0", "m1"])

    def test_three_fh_managers_gk_then_both_strikers(self):
        sections = _run_suggest_lineup(self.scores, fh_names={"m10", "m11", "m12"})
        self.assertEqual(sections["Goalkeeper"], ["m10"])
        self.assertEqual(sorted(sections["Strikers"]), ["m11", "m12"])

    def test_fourth_fh_manager_falls_back_to_normal_pool(self):
        sections = _run_suggest_lineup(self.scores, fh_names={"m10", "m11", "m12", "m13"})
        self.assertEqual(sections["Goalkeeper"], ["m10"])
        self.assertEqual(sorted(sections["Strikers"]), ["m11", "m12"])
        self.assertIn("m13", sections["Squad"])  # 4th FH manager, no role room left


class CeilingWeightingTests(unittest.TestCase):
    """backtest.py found mean + k*std beats plain mean for GK/Strikers
    selection at every k tested, unconditionally (SKLW's own goal
    formula is convex: capped downside, unbounded stepped upside, so
    variance raises expected goals regardless of favourite/underdog
    status). Locks in that a high-ceiling manager can outrank a
    higher-mean/zero-variance one for GK when k > 0, and that k=0 (or no
    ceiling map) falls back to plain top-3-by-mean exactly."""

    def setUp(self):
        self.scores = [(f"m{i}", 100.0 - i) for i in range(16)]  # m0 highest mean
        self.ceiling = {f"m{i}": 0.0 for i in range(16)}
        self.ceiling["m1"] = 50.0  # huge stdev, 2nd-highest mean

    def test_k_zero_is_plain_top3_by_mean(self):
        sections = _run_suggest_lineup(self.scores, ceiling=self.ceiling, k=0.0)
        self.assertEqual(sections["Goalkeeper"], ["m0"])
        self.assertEqual(sections["Strikers"], ["m1", "m2"])

    def test_no_ceiling_map_is_plain_top3_by_mean(self):
        sections = _run_suggest_lineup(self.scores, k=0.5)  # ceiling=None
        self.assertEqual(sections["Goalkeeper"], ["m0"])
        self.assertEqual(sections["Strikers"], ["m1", "m2"])

    def test_high_ceiling_can_outrank_higher_mean_for_gk(self):
        sections = _run_suggest_lineup(self.scores, ceiling=self.ceiling, k=0.5)
        self.assertEqual(sections["Goalkeeper"], ["m1"])
        self.assertEqual(sorted(sections["Strikers"]), ["m0", "m2"])

    def test_squad_selection_always_uses_plain_score(self):
        """Squad/Bench boundary must stay plain-score-ordered even when
        ceiling-weighting reorders who's in the GK/Strikers pool --
        backtest.py found ceiling-weighting dilutes/doesn't help once
        pooled into the Squad sum."""
        sections = _run_suggest_lineup(self.scores, ceiling=self.ceiling, k=0.5)
        self.assertEqual(sections["Squad"], [f"m{i}" for i in range(3, 14)])
        self.assertEqual(sections["Bench"], ["m14", "m15"])


if __name__ == "__main__":
    unittest.main()
