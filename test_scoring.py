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
import random
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
import fetch_master_list
import resolve_matchup_roles
import sklw_lineup
import sklw_matchup

try:
    import openpyxl
    from openpyxl.styles import PatternFill
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False


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


class MatchupPinsPersistenceTests(unittest.TestCase):
    """Reported live: having to retype the same scouted GK/Strikers IDs
    on every run within the same gameweek was real, avoidable friction.
    load_pins/save_pins/resolve_and_remember persist them locally
    (matchup_pins.json, gitignored) so they're remembered across runs,
    always visibly (never a silent surprise), and always overridable by
    an explicit CLI flag."""

    def test_missing_file_yields_empty_structure(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pins.json")
            self.assertEqual(sklw_matchup.load_pins(path), {"us": {}, "them": {}})

    def test_save_then_load_round_trips_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pins.json")
            pins = {"us": {"gk_id": "123", "strikers_ids": "456,789"},
                    "them": {"Fried Rice Eater": {"gk_id": "1", "strikers_ids": "2,3"}}}
            sklw_matchup.save_pins(path, pins)
            self.assertEqual(sklw_matchup.load_pins(path), pins)

    def test_malformed_json_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pins.json")
            with open(path, "w") as f:
                f.write("not valid json{{{")
            self.assertEqual(sklw_matchup.load_pins(path), {"us": {}, "them": {}})

    def test_cli_value_always_wins_over_saved(self):
        result = sklw_matchup.resolve_and_remember("999", "123", "prompt", no_prompt=True, field_label="x")
        self.assertEqual(result, "999")

    def test_saved_value_used_without_reprompting(self):
        with mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = sklw_matchup.resolve_and_remember(None, "123", "prompt", no_prompt=True, field_label="x")
        self.assertEqual(result, "123")

    def test_neither_cli_nor_saved_falls_through_to_prompt(self):
        result = sklw_matchup.resolve_and_remember(None, None, "prompt", no_prompt=True, field_label="x")
        self.assertIsNone(result)  # no_prompt=True -> prompt_for_role_ids skips and returns None


class PendingStarterNamesTests(unittest.TestCase):
    """Requested live: 'Still to play: 104 of our starters, 104 of
    theirs' as a bare count didn't say WHO -- pending_starter_names()
    returns the actual player names behind that count."""

    def setUp(self):
        self.players = {
            1: {"first_name": "A", "second_name": "Player1"},
            2: {"first_name": "B", "second_name": "Player2"},
            3: {"first_name": "C", "second_name": "Player3"},
        }

    def test_returns_names_of_not_yet_started_starters_only(self):
        club = {"Alice": {"starters": [1, 2, 3], "captain": 1}}
        live_locked = {1: 5.0}  # only player 1 has started
        result = sklw_matchup.pending_starter_names(club, self.players, live_locked)
        self.assertEqual(result["Alice"], ["B Player2", "C Player3"])

    def test_manual_score_manager_has_no_pending(self):
        club = {"Bob": {"manual_score": 50.0}}
        result = sklw_matchup.pending_starter_names(club, self.players, {})
        self.assertEqual(result["Bob"], [])

    def test_all_started_means_empty_list(self):
        club = {"Alice": {"starters": [1, 2], "captain": 1}}
        live_locked = {1: 5.0, 2: 3.0}
        result = sklw_matchup.pending_starter_names(club, self.players, live_locked)
        self.assertEqual(result["Alice"], [])


class SklwMatchupFreeHitTests(unittest.TestCase):
    """sklw_matchup.py's assign_roles had NO Free Hit handling at all --
    an FH manager's often-unrepresentative projection could silently
    land them anywhere including Bench, unlike sklw_lineup.py's
    suggested lineup for the same real matchup. Mirrors sklw_lineup's
    validated --fh priority: GK first, then Strikers."""

    def setUp(self):
        # m0 highest projected ... m15 lowest
        self.club = {f"m{i}": {"projected": 100.0 - i} for i in range(16)}

    def test_one_fh_manager_becomes_gk(self):
        roles = sklw_matchup.assign_roles(self.club, fh_names={"m10"})
        self.assertEqual(roles["gk"], ["m10"])
        self.assertEqual(set(roles["strikers"]), {"m0", "m1"})

    def test_three_fh_managers_gk_then_both_strikers(self):
        roles = sklw_matchup.assign_roles(self.club, fh_names={"m10", "m11", "m12"})
        self.assertEqual(roles["gk"], ["m10"])
        self.assertEqual(set(roles["strikers"]), {"m11", "m12"})

    def test_explicit_forced_gk_beats_fh(self):
        """Real scouted knowledge (forced_gk) wins over an FH-based
        guess -- the FH manager still gets priority for a remaining
        Striker slot instead."""
        roles = sklw_matchup.assign_roles(self.club, forced_gk="m5", fh_names={"m10"})
        self.assertEqual(roles["gk"], ["m5"])
        self.assertIn("m10", roles["strikers"])

    def test_no_fh_is_unchanged_from_baseline(self):
        roles = sklw_matchup.assign_roles(self.club)
        self.assertEqual(roles["gk"], ["m0"])
        self.assertEqual(set(roles["strikers"]), {"m1", "m2"})


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


class PromptForRoleIdsTests(unittest.TestCase):
    """sklw_matchup.py's interactive fallback for --them-gk-id/--them-
    strikers-ids/--us-gk-id/--us-strikers-ids -- skippable with Enter,
    never hangs on a non-interactive stdin, and never overrides a value
    already given on the command line."""

    def test_existing_value_bypasses_prompt_entirely(self):
        with mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = sklw_matchup.prompt_for_role_ids("123", "prompt: ", no_prompt=False)
        self.assertEqual(result, "123")

    def test_no_prompt_flag_skips(self):
        with mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = sklw_matchup.prompt_for_role_ids(None, "prompt: ", no_prompt=True)
        self.assertIsNone(result)

    def test_non_interactive_stdin_skips_without_hanging(self):
        with mock.patch("sys.stdin.isatty", return_value=False), \
             mock.patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = sklw_matchup.prompt_for_role_ids(None, "prompt: ", no_prompt=False)
        self.assertIsNone(result)

    def test_typed_answer_is_returned(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value="456"):
            result = sklw_matchup.prompt_for_role_ids(None, "prompt: ", no_prompt=False)
        self.assertEqual(result, "456")

    def test_empty_answer_means_skip(self):
        with mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", return_value=""):
            result = sklw_matchup.prompt_for_role_ids(None, "prompt: ", no_prompt=False)
        self.assertIsNone(result)


class FetchMasterListTests(unittest.TestCase):
    """fetch_master_list.py pulls a Google Sheet tab via its public CSV
    export URL. If the sheet isn't actually shared publicly, Google
    serves an HTML sign-in page instead (usually still HTTP 200, not an
    error) -- this must be caught explicitly rather than silently
    writing garbage HTML into what's supposed to be a CSV file."""

    def test_html_response_is_detected_and_rejected(self):
        fake_response = mock.Mock()
        fake_response.text = "<!DOCTYPE html><html><body>Sign in</body></html>"
        fake_response.raise_for_status = mock.Mock()
        with mock.patch("fetch_master_list.requests.get", return_value=fake_response):
            with self.assertRaises(SystemExit):
                fetch_master_list.fetch_tab_csv("sheet123", "456")

    def test_real_csv_response_passes_through(self):
        fake_response = mock.Mock()
        fake_response.text = "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\nM1,Club A,@x,1,Team,Name\n"
        fake_response.raise_for_status = mock.Mock()
        with mock.patch("fetch_master_list.requests.get", return_value=fake_response):
            result = fetch_master_list.fetch_tab_csv("sheet123", "456")
        self.assertIn("Club A", result)


class ClubsJsonTests(unittest.TestCase):
    """build_clubs_json.py converts the league master list CSV into
    clubs.json (club name -> handle/team-name-labeled 16-manager roster
    -- real/government names always stripped, see that script's
    docstring for why), and sklw_matchup.py's --opponent flag looks a
    club up from it. All synthetic data here -- no real manager IDs."""

    def _write_csv(self, path):
        path.write_text(
            "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
            "M1,Club Alpha,@a1,111,Team A1,Alice\n"
            "M1,Club Alpha,@a2,222,Team A2,Bob\n"
            "M1,Club Beta,@b1,333,Team B1,Carol\n"
        )

    def test_build_clubs_keeps_handles_strips_real_names(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            self._write_csv(csv_path)
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {
            "Club Alpha": {"@a1": 111, "@a2": 222},
            "Club Beta": {"@b1": 333},
        })
        # real names never leak into the output; handles are kept
        dumped = str(clubs)
        for leaked in ("Alice", "Bob", "Carol"):
            self.assertNotIn(leaked, dumped)
        for kept in ("@a1", "@a2", "@b1"):
            self.assertIn(kept, dumped)

    def test_build_clubs_falls_back_to_team_name_when_handle_missing(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            csv_path.write_text(
                "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
                "M1,Club Alpha,,111,Team A1,Alice\n"
            )
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {"Club Alpha": {"Team A1": 111}})

    def test_build_clubs_skips_handle_that_is_actually_the_real_name(self):
        """Found against a real master list: some rows have no proper
        @handle and the sheet just has the manager's real name typed
        into the handle column instead (handle == manager_name exactly).
        Must fall back to fpl_team_name rather than leak it."""
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            csv_path.write_text(
                "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
                "M1,Club Alpha,Tom Mitcham,111,Milambo No. 5,Tom Mitcham\n"
            )
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {"Club Alpha": {"Milambo No. 5": 111}})

    def test_build_clubs_falls_back_to_generic_when_everything_is_the_real_name(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            csv_path.write_text(
                "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
                "M1,Club Alpha,Tom Mitcham,111,Tom Mitcham,Tom Mitcham\n"
            )
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {"Club Alpha": {"Manager1": 111}})

    def test_build_clubs_dedupes_repeated_labels(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "master.csv"
            csv_path.write_text(
                "tab,team_group,handle,fpl_id,fpl_team_name,manager_name\n"
                "M1,Club Alpha,@dupe,111,Team A1,Alice\n"
                "M1,Club Alpha,@dupe,222,Team A2,Bob\n"
            )
            clubs = build_clubs_json.build_clubs(csv_path)
        self.assertEqual(clubs, {"Club Alpha": {"@dupe": 111, "@dupe (2)": 222}})

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


class ScorelineDistributionCalibrationTests(unittest.TestCase):
    """calibrate_matchup.py's scoreline-level calibration (added instead
    of a Dixon-Coles correction, which is a fix for a different kind of
    model -- see the README/module docstring). Checks the core math
    directly: the multiclass Brier shortcut formula must match its
    literal definition, and the pooled-bucket data points must be
    derivable from a predicted distribution correctly."""

    def test_multiclass_brier_shortcut_matches_literal_definition(self):
        pred_dist = {(1, 0): 0.5, (0, 0): 0.3, (2, 1): 0.2}
        real = (1, 0)
        shortcut = sum(p * p for p in pred_dist.values()) - 2 * pred_dist.get(real, 0.0) + 1.0
        # literal: sum((p_i - y_i)^2) over every scoreline with p_i != 0 union {real}
        all_scores = set(pred_dist) | {real}
        literal = sum((pred_dist.get(s, 0.0) - (1.0 if s == real else 0.0)) ** 2 for s in all_scores)
        self.assertAlmostEqual(shortcut, literal, places=9)

    def test_multiclass_brier_shortcut_when_real_outcome_unseen_in_sim(self):
        """The real scoreline never showing up in pred_dist (probability
        implicitly 0) must still compute correctly via the shortcut."""
        pred_dist = {(1, 0): 0.6, (0, 0): 0.4}
        real = (3, 1)  # never simulated
        shortcut = sum(p * p for p in pred_dist.values()) - 2 * pred_dist.get(real, 0.0) + 1.0
        all_scores = set(pred_dist) | {real}
        literal = sum((pred_dist.get(s, 0.0) - (1.0 if s == real else 0.0)) ** 2 for s in all_scores)
        self.assertAlmostEqual(shortcut, literal, places=9)

    def test_perfect_prediction_gives_zero_multiclass_brier(self):
        pred_dist = {(2, 1): 1.0}
        real = (2, 1)
        brier = sum(p * p for p in pred_dist.values()) - 2 * pred_dist.get(real, 0.0) + 1.0
        self.assertAlmostEqual(brier, 0.0, places=9)


class PredictedScoreDistributionTests(unittest.TestCase):
    """predicted_score_distribution must always return probabilities
    that sum to 1.0 across whatever scorelines occurred in-sim."""

    def _member(self):
        return {"xps": [5.0] * 11, "positions": ["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2,
                "teams": ["TeamA"] * 11, "captain_idx": 0}

    def test_probabilities_sum_to_one(self):
        club1 = [self._member() for _ in range(16)]
        club2 = [self._member() for _ in range(16)]
        roles1 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club1])
        roles2 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club2])
        fallback = {"GK": [0.0, 1.0, -1.0], "DEF": [0.0, 1.0, -1.0],
                    "MID": [0.0, 1.0, -1.0], "FWD": [0.0, 1.0, -1.0]}
        team_gw_residuals = {("TeamA", 1): fallback}
        team_gw_index = {"TeamA": [1]}
        rng = random.Random(1)
        dist = calibrate_matchup.predicted_score_distribution(
            rng, club1, roles1, club2, roles2, team_gw_residuals, team_gw_index, fallback, 200)
        self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)
        self.assertGreater(len(dist), 0)


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl not installed")
class ResolveMatchupRolesTests(unittest.TestCase):
    """resolve_matchup_roles.py parses the SKLW master sheet's per-
    matchup tabs -- GK vs Strikers is only distinguishable by cell fill
    color (2 alike = Strikers, the odd one out = GK), not by any text
    label, confirmed against a real example (see README). Validated
    end-to-end against a synthetic workbook built here before being
    tried against the real sheet, same rigor as everything else in this
    project after several costly wrong-guess incidents."""

    BLUE = "FFADD8E6"
    GREEN = "FFC6E0B4"
    GRAY = "FFD9D9D9"
    WHITE = "FFFFFFFF"

    def _fill(self, hexcode):
        return PatternFill(start_color=hexcode, end_color=hexcode, fill_type="solid")

    def _build_workbook(self):
        wb = openpyxl.Workbook()
        live = wb.active
        live.title = "Live Scores"
        rows = [
            ("M1", "Netflix & Chilwell", 0, 4, "El Sin Nombre"),
            ("M20", "The Galacticos", 3, 0, "Algorithm & Blues"),
        ]
        for i, (m, a, sa, sb, b) in enumerate(rows, 1):
            live.cell(row=i, column=1, value=m)
            live.cell(row=i, column=2, value=a)
            live.cell(row=i, column=3, value=sa)
            live.cell(row=i, column=4, value=sb)
            live.cell(row=i, column=5, value=b)

        m1 = wb.create_sheet("M1")
        m1.cell(row=1, column=1, value="Netflix & Chilwell").fill = self._fill("FFFFD9B3")
        m1.cell(row=18, column=1, value="El Sin Nombre").fill = self._fill("FFE6B3D9")

        left = (["@LewisW_FF", "@fpl_flair", "@Ad_1net"] + [f"@LSquad{i}" for i in range(11)]
                + ["@LBench1", "@LBench2"])
        left_fills = [self.BLUE, self.BLUE, self.WHITE] + [self.GREEN] * 11 + [self.GRAY, self.GRAY]
        right = (["@Fplmode", "@Frankwalsh82", "@Bobbylovefpl"] + [f"@RSquad{i}" for i in range(11)]
                 + ["@RBench1", "@RBench2"])
        right_fills = [self.BLUE, self.BLUE, self.WHITE] + [self.GREEN] * 11 + [self.GRAY, self.GRAY]
        for i in range(16):
            r = 2 + i
            m1.cell(row=r, column=1, value=left[i]).fill = self._fill(left_fills[i])
            m1.cell(row=r, column=6, value=right[i]).fill = self._fill(right_fills[i])
        return wb

    def test_find_fixture_matches_our_club_either_row(self):
        wb = self._build_workbook()
        self.assertEqual(resolve_matchup_roles.find_fixture(wb, "Netflix"),
                          ("M1", "Netflix & Chilwell", "El Sin Nombre"))
        self.assertEqual(resolve_matchup_roles.find_fixture(wb, "algorithm"),
                          ("M20", "Algorithm & Blues", "The Galacticos"))

    def test_find_fixture_no_match_errors(self):
        wb = self._build_workbook()
        with self.assertRaises(SystemExit):
            resolve_matchup_roles.find_fixture(wb, "Nonexistent Club")

    def test_classify_top_three_two_alike_one_different(self):
        entries = [("@a", self.BLUE), ("@b", self.WHITE), ("@c", self.BLUE)]
        gk, strikers = resolve_matchup_roles._classify_top_three(entries)
        self.assertEqual(gk, "@b")
        self.assertEqual(sorted(strikers), ["@a", "@c"])

    def test_classify_top_three_all_same_color_errors(self):
        entries = [("@a", self.BLUE), ("@b", self.BLUE), ("@c", self.BLUE)]
        with self.assertRaises(SystemExit):
            resolve_matchup_roles._classify_top_three(entries)

    def test_classify_top_three_all_different_colors_errors(self):
        entries = [("@a", self.BLUE), ("@b", self.WHITE), ("@c", self.GREEN)]
        with self.assertRaises(SystemExit):
            resolve_matchup_roles._classify_top_three(entries)

    def test_parse_matchup_sheet_left_is_top_banner_club(self):
        wb = self._build_workbook()
        result = resolve_matchup_roles.parse_matchup_sheet(wb, "M1")
        self.assertEqual(result["left_club"], "Netflix & Chilwell")
        self.assertEqual(result["right_club"], "El Sin Nombre")
        self.assertEqual(result["left"]["gk"], "@Ad_1net")
        self.assertEqual(sorted(result["left"]["strikers"]), ["@LewisW_FF", "@fpl_flair"])
        self.assertEqual(len(result["left"]["squad"]), 11)
        self.assertEqual(result["left"]["bench"], ["@LBench1", "@LBench2"])
        self.assertEqual(result["right"]["gk"], "@Bobbylovefpl")
        self.assertEqual(sorted(result["right"]["strikers"]), ["@Fplmode", "@Frankwalsh82"])

    def test_resolve_ids_case_insensitive(self):
        roster = {"@Ad_1net": 12}
        self.assertEqual(resolve_matchup_roles.resolve_ids(["@ad_1net"], roster, "GK"), [12])

    def test_resolve_ids_warns_and_skips_unmatched(self):
        roster = {"@Ad_1net": 12}
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = resolve_matchup_roles.resolve_ids(["@Nobody"], roster, "GK")
        self.assertEqual(result, [])
        self.assertIn("not found", buf.getvalue())

    def test_end_to_end_writes_expected_pins_shape(self):
        with tempfile.TemporaryDirectory() as d:
            wb = self._build_workbook()
            wb_path = os.path.join(d, "wb.xlsx")
            wb.save(wb_path)
            clubs = {
                "Algorithm & Blues": {"a": 1},
                "The Galacticos": {"x": 1},
                "Netflix & Chilwell": ({f"@LSquad{i}": 2000 + i for i in range(11)}
                                       | {"@LewisW_FF": 10, "@fpl_flair": 11, "@Ad_1net": 12,
                                          "@LBench1": 13, "@LBench2": 14}),
                "El Sin Nombre": ({f"@RSquad{i}": 3000 + i for i in range(11)}
                                  | {"@Fplmode": 1, "@Frankwalsh82": 2, "@Bobbylovefpl": 3,
                                     "@RBench1": 4, "@RBench2": 5}),
            }
            clubs_path = os.path.join(d, "clubs.json")
            Path(clubs_path).write_text(json.dumps(clubs))
            pins_path = os.path.join(d, "pins.json")

            import sys
            old_argv = sys.argv
            sys.argv = ["resolve_matchup_roles.py", "--workbook", wb_path,
                        "--clubs-file", clubs_path, "--our-club", "Netflix",
                        "--pins-file", pins_path]
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    resolve_matchup_roles.main()
            finally:
                sys.argv = old_argv

            pins = json.loads(Path(pins_path).read_text())
        self.assertEqual(pins["us"], {"gk_id": "12", "strikers_ids": "10,11"})
        self.assertEqual(pins["them"]["El Sin Nombre"], {"gk_id": "3", "strikers_ids": "1,2"})


class VarianceScaleTests(unittest.TestCase):
    """variance_scale was added to test one specific diagnosis for the
    scoreline under-spread found by ScorelineDistributionCalibrationTests:
    is the simulation simply under-dispersed overall? A sweep across real
    2022-23/2023-24 data (see README) found NO scale that minimizes both
    win-probability Brier and scoreline-spread metrics at once -- win Brier
    is best at scale=1.0 and gets worse above it, while scoreline-spread
    keeps improving well past 1.0. That rules out plain uniform
    under-dispersion; the gap is something more structural. --variance-scale
    stays available (default 1.0, unchanged behavior) as a documented
    diagnostic, not a live fix. These tests just lock in the plumbing."""

    def _member(self, seed_xp=5.0):
        return {"xps": [seed_xp] * 11, "positions": ["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2,
                "teams": ["TeamA"] * 11, "captain_idx": 0}

    def test_scale_one_matches_default_behavior(self):
        rng1 = random.Random(7)
        rng2 = random.Random(7)
        member = self._member()
        team_shocks = {"TeamA": 1}
        team_gw_residuals = {("TeamA", 1): {"GK": [1.0, -1.0], "DEF": [1.0, -1.0],
                                             "MID": [1.0, -1.0], "FWD": [1.0, -1.0]}}
        fallback = team_gw_residuals[("TeamA", 1)]
        default_score = calibrate_matchup.simulate_member_score(rng1, member, team_shocks,
                                                                  team_gw_residuals, fallback)
        explicit_score = calibrate_matchup.simulate_member_score(rng2, member, team_shocks,
                                                                   team_gw_residuals, fallback,
                                                                   variance_scale=1.0)
        self.assertEqual(default_score, explicit_score)

    def test_larger_scale_widens_the_residual_contribution(self):
        # Single-element residual pools make rng.choice deterministic (always
        # 2.0), so the effect of variance_scale is exact and checkable:
        # each of the 11 positions gets xp + 2.0*scale, and the captain's
        # slot (index 0) is doubled again -- total = 12 * (xp + 2.0*scale).
        member = self._member()
        team_shocks = {"TeamA": 1}
        team_gw_residuals = {("TeamA", 1): {"GK": [2.0], "DEF": [2.0], "MID": [2.0], "FWD": [2.0]}}
        fallback = team_gw_residuals[("TeamA", 1)]
        base = calibrate_matchup.simulate_member_score(random.Random(1), member, team_shocks,
                                                         team_gw_residuals, fallback, variance_scale=1.0)
        scaled = calibrate_matchup.simulate_member_score(random.Random(1), member, team_shocks,
                                                           team_gw_residuals, fallback, variance_scale=2.0)
        self.assertAlmostEqual(scaled - base, 12 * 2.0 * (2.0 - 1.0), places=6)

    def test_predicted_score_distribution_accepts_variance_scale(self):
        club1 = [self._member() for _ in range(16)]
        club2 = [self._member() for _ in range(16)]
        roles1 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club1])
        roles2 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club2])
        fallback = {"GK": [0.0, 1.0, -1.0], "DEF": [0.0, 1.0, -1.0],
                    "MID": [0.0, 1.0, -1.0], "FWD": [0.0, 1.0, -1.0]}
        team_gw_residuals = {("TeamA", 1): fallback}
        team_gw_index = {"TeamA": [1]}
        rng = random.Random(1)
        dist = calibrate_matchup.predicted_score_distribution(
            rng, club1, roles1, club2, roles2, team_gw_residuals, team_gw_index, fallback,
            200, variance_scale=1.5)
        self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)

    def test_run_variance_scale_sweep_prints_one_row_per_scale(self):
        member = self._member()
        club1 = [member for _ in range(16)]
        club2 = [member for _ in range(16)]
        roles1 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club1])
        roles2 = calibrate_matchup.assign_roles([sum(m["xps"]) for m in club2])
        real = calibrate_matchup.real_outcome(
            [{**m, "actual": sum(m["xps"])} for m in club1], roles1,
            [{**m, "actual": sum(m["xps"])} for m in club2], roles2)
        built_trials = [(club1, roles1, club2, roles2, real)]
        fallback = {"GK": [0.0, 1.0, -1.0], "DEF": [0.0, 1.0, -1.0],
                    "MID": [0.0, 1.0, -1.0], "FWD": [0.0, 1.0, -1.0]}
        team_gw_residuals = {("TeamA", 1): fallback}
        team_gw_index = {"TeamA": [1]}
        rng = random.Random(1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            calibrate_matchup.run_variance_scale_sweep(
                rng, built_trials, team_gw_residuals, team_gw_index, fallback, 50, "0.8,1.0,1.2")
        output = buf.getvalue()
        for scale in ("0.80", "1.00", "1.20"):
            self.assertIn(scale, output)


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
