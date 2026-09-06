# SKLW Lineup Tool

Standalone helper for **Strikers, Keepers, Losers, Weepers (SKLW)** — a
community FPL tournament (hosted/covered by Fantasy Football Scout,
@OfficialSKLW). This repo is a small side tool for one club in that
tournament, fully independent of any other project — no shared code,
no shared dependencies.

## What SKLW is

Each club = 16 real FPL managers (1 captain + 15 members), each running
their own separate FPL team. Every SKLW matchweek (MW), the club captain
must submit a lineup drawn from those 16 members' real GW scores:

- **2 Strikers** — go into a H2H battle vs the opponent club's nominated
  Goalkeeper. A striker scores a goal if their FPL score beats the
  opposing GK's score; +1 more goal per full 20 points they outscore by.
- **1 Goalkeeper** — H2H battle vs the opponent's 2 Strikers (same
  scoring, mirrored). Both Strikers and GK reward being HIGH-scoring —
  there's no benefit to hiding a weak member here.
- **11 Squad** — summed FPL score vs the opponent's 11. +1 goal per 30
  points the combined total beats the opponent by.
- **2 Bench** — don't count.

Full rules: see `docs/rules.md` (paste of the original rules doc) if
present, or ask the user — they have the source Google Doc.

Key scoring wrinkles: Bench Boost drops the bench from a member's score
(only the 11 starters count); Triple Captain has 1/3 of the (already
tripled) captain score deducted. Wildcard/Free Hit scores are NOT
adjusted.

**MW deadline = 15 minutes after the real FPL GW deadline.** This matters
a lot for the tool's design (see below).

## The tool (`sklw_lineup.py`)

Standalone script, `requests` is the only dependency. Uses FPL's own
public API — no login needed, no separate projection model (uses FPL's
own `ep_next` "expected points" field per player).

**Why it's built around the deadline, not just "run whenever"**: FPL's
public picks endpoint (`/api/entry/{manager_id}/event/{gw}/picks/`) only
returns a manager's picks for a GW *after that GW's deadline has passed*
— FPL hides picks pre-deadline so managers can't copy each other. That
happens to line up well with SKLW's 15-minute submission window: the
real deadline hits, everyone's picks become public, and there's a real
15 minutes to auto-pull + project + submit.

Two modes:
- `--mode final` (default): pulls everyone's actual locked-in picks for
  the CURRENT gameweek — authoritative, no manual input. Falls back to
  each manager's last-finished-GW squad automatically if the deadline
  hasn't passed yet (so it's always safe to run early, it just won't be
  the live squad).
- `--mode preview`: pre-deadline planning only. Uses last-finished-GW
  squads as a baseline, then applies manual overrides from
  `overrides.json` (`{"manager_id": {"out": [elementId,...], "in":
  [elementId,...]}}`) for any known/expected transfers people have told
  the captain about ahead of time. NOT authoritative — always re-run
  `--mode final` once the real deadline passes to confirm.

Output: projects each of the 16 members' GW score (sum of projected points
over their 11 starters, captain doubled, chip-adjusted per SKLW's rule
above), then suggests a lineup — **top scorer → GK**, next 2 → Strikers,
next 11 → Squad, bottom 2 → Bench. (Both Strikers and GK want HIGH
projected scorers, since both roles are rewarded for outscoring the
opponent, not for hiding a weak link — GK gets the single best because it
faces BOTH opposing Strikers individually, double the H2H exposure of a
Striker slot. See `backtest.py` for how this was validated against real
historical data — putting the best output in GK helped; additionally
weighting by variance/ceiling on top of that was tested and found to
hurt, not help, so the ranking here is by plain projected mean.)

By default the projected points come from FPL's own `ep_next` field.
`--projections path/to/solio.csv` swaps that out for a Solio-style
projections CSV instead (`Pos,ID,Name,BV,SV,Team,1_xMins,...,N_xMins,
1_Pts,...,N_Pts`) — it matches players by name (Solio's `Name` column is
FPL's short "web_name", e.g. `Saka`, `J.Timber`) + team, since Solio's own
`ID` column is its own internal numbering, not the FPL element ID. Uses
whichever `<N>_Pts` column has the lowest number (the soonest upcoming
GW — Solio numbers these relative to the current GW, not always starting
at `1`). Anyone in the squad that the CSV doesn't match falls back to
`ep_next` automatically, with a warning listing what didn't match so you
can sanity-check it. If no `--projections` path is given, it auto-detects
a CSV: checks for `solio.csv` in this folder first, then falls back to
the most recently downloaded CSV in your Downloads folder that actually
looks like a Solio export (checked by header content, not filename).

If you don't have a Solio CSV, just drop one you were given as
`solio.csv` in this folder (or anywhere in your Downloads) and it'll be
picked up automatically, no `--projections` flag needed. `run_nofetch.bat`
(or `run_draft_nofetch.bat`) runs the tool without touching Solio at
all — useful if you're sharing this with someone who doesn't have (or
doesn't need) a Solio CSV; they'll just get `ep_next`-based projections
instead, or their own CSV via `--projections`.

### Reading a squad from a screenshot (`--from-screenshot`) — ⚠️ doesn't work well right now

**Status: not currently reliable enough to use.** Tested against real
pitch-view screenshots and topped out around 10-12/15 players correctly
matched (the rest need to be typed in manually via the fill-in-the-gaps
prompt it shows) — good enough that it won't silently get a player
*wrong* (ambiguous/spurious matches are deliberately rejected rather than
guessed at), but not good enough to trust for a full automatic 15/15
read. Left in the codebase in case a real coordinate-measured calibration
pass improves it later, but the recommended path for now is
`--transfer`/`--wildcard` (both fully reliable, tested against live data)
rather than screenshots.

If you want to try it anyway: for a manager whose squad you only have as
a screenshot (pitch view or list view) rather than pulling live via the
API, `--from-screenshot path/to/image.png` OCRs it, matches whatever text
it finds against the real FPL player list, and prints the predicted
best-XI/captain from that squad using current projections. It does NOT
try to detect who was actually captained/benched from badges or icons in
the image — it just extracts the (partial) squad and lets the existing
best-XI logic (same as `--best-xi`) work out the predicted starting 11 +
captain. Requires `pip install pytesseract pillow` plus the
[Tesseract OCR binary](https://github.com/UB-Mannheim/tesseract/wiki)
installed separately (not a pip package). List view OCRs meaningfully
better than pitch view if you have a choice.

`--best-xi` is a one-off comparison: instead of trusting each manager's
actual submitted starting-11/captain, it scores them using the
highest-projected VALID XI (real FPL formation rules — 1 GK, 3–5 DEF,
2–5 MID, 1–3 FWD) picked from their real 15-man squad, captain = the
highest-projected starter. Useful for a "what's everyone's best possible
score right now" comparison across all 16 members. Not the authoritative
actual-picks score, so chip adjustments aren't applied under this flag.
Combine with `--projections` to base it on Solio's numbers instead of
`ep_next`.

`--lookup "name fragment"` searches FPL's bootstrap player list and
prints element IDs — for building `overrides.json` without having to
know player IDs by heart.

`--transfer "Manager Name" --out "player" --in "player"` is the normal
way to record a transfer once a club member tells you about it (the API
doesn't reveal real transfers until the deadline passes, which is too
late for pre-deadline planning). It resolves both names, appends the
swap to that member's entry in `overrides.json`, and stops there — it
does NOT re-run the full lineup (running the whole 16-manager fetch
after every single transfer entered is wasteful if you're logging
several back to back). Run `run.bat --mode preview` separately once
you're done recording transfers to see the updated suggested lineup.
Multiple players in one transfer: comma-separate them, e.g.
`--out "Haaland,Saka" --in "Watkins,Salah"` (same order on both sides).
Run `--transfer` again for each new transfer as members report them in
— it accumulates in `overrides.json` rather than overwriting.

`--wildcard "Manager Name" --squad "player1,player2,...,player15"` is for
when a member plays Wildcard (or you're planning their WC squad ahead of
time) — unlike `--transfer`, this replaces their ENTIRE 15-man squad
rather than swapping individual players, so it needs the full list, not
just what changed. Requires exactly 15 names, comma-separated. It resolves
each name, saves the squad under a `"wildcard"` key in that member's
`overrides.json` entry (distinct from `"out"/"in"` — running `--transfer`
afterwards for the same member doesn't clash with it), and automatically
scores that member off the best valid XI from the new squad (same
formation-valid logic as `--best-xi`) rather than trusting a submitted
starting-11, since a fresh Wildcard squad often hasn't had a lineup set
yet. Like `--transfer`, it just records and stops — run `run.bat --mode
preview` separately afterwards to see the updated lineup. Running
`--wildcard` again for the same manager overwrites their previous WC
squad entry (it's a full replacement, not cumulative like transfers).

`--wildcard-auto "Manager1,Manager2,..."` is for when several members are
playing Wildcard the same week and are likely to land on close to the
same squad anyway (e.g. a group of analytics-based managers all chasing
the same projections) — rather than typing out `--wildcard`/`--squad` by
hand for each one, this computes ONE genuinely optimal, budget-legal
15-man squad (max 3 players from any one real club, £100.0m budget by
default — override with `--budget 99.5` etc., total cost must stay under
that) that maximizes the best valid starting-XI + captain score
achievable from it, using current projections (`ep_next` or Solio,
whichever's loaded) — and applies that identical squad to every manager
name listed in one go. Real solver, not a top-15-by-points shortcut (that
would blow the budget/formation rules) — it's a MILP solved via
`pip install pulp` (bundles its own solver, no separate binary needed).
Same `overrides.json` `"wildcard"` key and best-xi auto-scoring as
`--wildcard`; running it again (for the same or different managers)
overwrites each listed manager's previous WC squad entry. If a handful of
members' Wildcard squads genuinely differ from each other, use
`--wildcard`/`--squad` per manager instead — `--wildcard-auto` assumes
they're all fine with the same squad.

Setup (one-time): `pip install pulp`. Then, e.g. for 8 members all on
Wildcard this week:
```
python sklw_lineup.py --wildcard-auto "az,Classiic,Cyclones,farhan,Harv,Heisen,kb2,Mahmoud"
```
Add `--budget 99.5` (or whatever bank they've actually got) if it's not
the standard £100.0m. Then, same as `--wildcard`: run `run.bat --mode
preview` separately to see it reflected in the suggested lineup.

By default the squad is optimized on points averaged across the next 5
GWs (`--wc-horizon 5`), not just the single next one — a Wildcard squad
has to hold up over several gameweeks, so a player who's brilliant for
one GW (a single soft fixture) but nothing after shouldn't outrank
someone solid across the whole horizon. This needs a Solio CSV loaded
(its `<N>_Pts` columns for GW+1, GW+2, ... — ep_next only ever covers the
single next GW, so without a CSV the optimizer falls back to next-GW-only
automatically). Override with `--wc-horizon 3` etc. if you want a shorter
or longer window.

If you don't trust the auto projection for a specific player (back from
injury, a new signing with no track record, or just a hunch), set their
expected-points NUMBER by hand for the optimizer with `--xpoints
"Haaland=15,Salah=12"` (comma-separated `name=value` pairs) — overrides
whatever ep_next/Solio would have given that player for this squad-
building step only, doesn't touch the CSV or affect anyone else's score
elsewhere.

If everyone in the `--wildcard-auto` batch is on the exact same squad
anyway, their real best-xi scores will end up near-identical regardless
of which of them technically has the marginally-better bench/captain —
so rather than sweat that noise, `--set-score 65` just calls it one flat
number for every manager in that batch:
```
python sklw_lineup.py --wildcard-auto "az,Classiic,Cyclones" --set-score 65
```
This saves a `"manual_score"` entry for each of them in `overrides.json`
— the main scoring loop then uses that number directly for that manager
(skipping picks/points entirely for them, so it works even without a
real squad fetched), regardless of mode. Doesn't touch their `"wildcard"`
squad entry — both are saved together and stack normally.

`--tc "Manager Name" --captain "player"` records a Triple Captain pick —
which specific player they're captaining this GW. SKLW's own rule nets
TC down to exactly a normal x2 captain (a third of the tripled score is
deducted), so this doesn't change a manager's projected score by itself —
it exists purely so the RIGHT player gets doubled: a real TC pick is
often a deliberate call (a favourable fixture, a nailed-on penalty
taker), not necessarily whoever the squad's single highest-projected
player is, which is what would otherwise get auto-captained. Forces that
player into the starting XI too if they wouldn't otherwise make it
(swapped in for the weakest starter in the same position). Stacks with
`--transfer` and `--wildcard` for the same manager (e.g. wildcard AND
triple-captain someone from the new squad) — saved as a separate
`"tc_captain"` key in `overrides.json`, doesn't overwrite either. Like
`--transfer`/`--wildcard`, it just records and stops — run `run.bat
--mode preview` separately afterwards.

`--fh "Manager Name"` marks a club member as playing Free Hit this GW
and forces them into the suggested lineup's GK slot, regardless of their
computed score. Reasoning: a GK faces BOTH opposing Strikers individually
(two separate H2H battles), while each Striker only faces the one
opposing GK — so a GK slot gets double the H2H exposure of a Striker
slot. Free Hit scores also get no chip adjustment (unlike Bench Boost or
Triple Captain), so they count at full value, and FH scores are often
high and hard to project accurately from someone's normal squad — so
that upside is worth more landing in the slot with double exposure.
Repeat the flag for multiple managers on FH the same week; only one can
actually take the GK slot (the higher-projected of them), the rest stay
in the normal pool with a note printed.

Every run also prints a banner up front with the club name ("Algorithm
and Blues" — edit `CLUB_NAME` at the top of the script if this changes)
and the full roster of member names → FPL manager IDs, so it's always
obvious which club/roster a given run is using.

## Setup

```
pip install -r requirements.txt
python sklw_lineup.py --mode final
```

## The 16 club members (name → FPL manager/entry ID)

```
az: 26099
Classiic: 10
Cyclones: 137079
farhan: 631
Harv: 4190
Heisen: 1230
kb2: 7878
Mahmoud: 6227
Mordo: 32082
neB: 1231
nokah: 62
riskypearl: 8052
smooth: 653
Stxddy: 625
tyh: 650
vrawn: 4
```

Already filled into `MANAGER_IDS` at the top of `sklw_lineup.py`.

## Match win-probability estimator (`sklw_matchup.py`)

Standalone (separate from `sklw_lineup.py`, same convention as
`draft_lineup.py`/`backtest.py`) — estimates the probability of beating
another SKLW club in a given matchweek, given both clubs' 16 real FPL
manager IDs.

A plain point projection only gives you a mean, not a probability — to
actually answer "what's our win chance" you need to know how much a real
score typically varies around that mean too. This builds that variance
model from real historical FPL data (same public archive `backtest.py`
already uses, same goal-scoring formulas), split by position since
attackers are far more volatile than defenders, then Monte Carlo
simulates several thousand matchweeks and reports what fraction your
club wins.

```
python sklw_matchup.py --them-file opponent.json
```
`opponent.json` is just `{"Name": id, ...}` for their 16 managers — get
these from the manager's own FPL page URL (`fantasy.premierleague.com/
entry/<ID>/...`), same as any FPL manager ID. Our own roster defaults to
`sklw_lineup.py`'s `MANAGER_IDS`; override with `--us-file` for a
different matchup. Add `--projections solio.csv` for Solio-based
projections instead of `ep_next` (same CSV format as `sklw_lineup.py`).

Picks up the SAME `overrides.json` `sklw_lineup.py` writes to — any
`--wildcard`/`--transfer`/`--tc`/`--set-score` already recorded there
applies automatically (matched by manager ID, works for either roster,
not just our own). `--overrides path.json` points at a different file if
needed. A `manual_score` override is treated as a fixed, zero-variance
number for that manager in every simulated trial, same as it is in
`sklw_lineup.py`.

Output also breaks down where the goals are expected to come from —
Strikers-vs-their-GK, our-GK-vs-their-Strikers, Squad-vs-Squad — so you
can see which part of the match is actually deciding the result, not
just the final win/draw/loss split.

**How accurate is it?** Run `python calibrate_matchup.py` to check —
it builds synthetic matchups from real historical FPL data (two seasons:
one purely to train the score-variance model, a DIFFERENT one to check
predictions against, so there's no lookahead), and reports whether
"predicted 70% to win" actually wins about 70% of the time in reality,
plus an overall Brier score (0 = perfect, 0.25 = no better than a coin
flip).

First pass was **Brier 0.206** (a real but modest improvement over
guessing) with the model measurably **overconfident at the extremes** —
a "95% to win" call only actually won about 84% of the time, a "5%"
call actually won about 11%, while 30-60% was well calibrated.
Investigated why: measuring real historical residuals directly shows a
random pair of players anywhere in the league barely correlates (0.033)
and opposing teams in the same match don't correlate at all (0.002), but
players on the SAME real team, same gameweek, correlate at 0.138 (a team
has a good or bad day together — shared clean sheet, shared goals,
shared bonus points) — so a team-scoped block-bootstrap (draw one
historical team-gameweek per real club per simulated trial, so same-team
players share it) should help.

First attempt at that showed **no improvement** (Brier 0.2059, same
overconfident tails) — because the synthetic test squads were 11
randomly drawn players with no formation constraints, so they rarely
clustered same-team players the way a real FPL squad does, giving the
fix nothing to grab onto. Fixed the test squads to draw a proper
formation (1 GK + a real DEF/MID/FWD split, from real position pools)
instead of a mixed blob, then re-ran the SAME comparison as a clean
ablation:

| squad model | correlation model | Brier |
|---|---|---|
| random 11-player blob | independent | 0.206 |
| formation-realistic | independent | 0.214 (worse) |
| formation-realistic | **team-scoped correlation** | **0.194 (best)** |

The middle row confirms the formation fix alone isn't what helped — it's
the team correlation specifically, and it only shows up once the test
squads are realistic enough to expose it. **This is now live in
`sklw_matchup.py`** (`build_team_gw_residuals`/`pick_team_shocks`) since
real FPL squads are already formation-realistic by construction
(`pick_best_eleven`), so this result transfers. Calibration is
meaningfully better but still not perfect — treat "clearly favoured" as
meaningful, exact numbers at the extremes with a bit more caution than
the middle of the range.

## Known gaps / next steps

- No `docs/rules.md` yet — this README doubles as the rules reference for
  now. Paste in the original rules doc there if/when available.
- `ep_next` (FPL's own expected-points field) is a decent free signal but
  much simpler than what the Axiom/fpl-model project's own projection
  engine does. If sharper projections are wanted later, the natural
  upgrade is pointing this script at a CSV export from that project
  instead of `ep_next` — but that's a separate project (`fpl-model`,
  different repo, different session scope) and deliberately NOT wired in
  here to keep this tool fully standalone. Don't import from it directly.
- Script has not yet been run against live data / verified against a
  real GW — first real run is still pending. Treat output with normal
  skepticism until confirmed once.
- Error handling for manager IDs that 404 entirely (typo'd ID), a
  manager who hasn't set a team for the GW yet, or partial squads (< 15
  players, e.g. a brand new team) is best-effort: it skips/warns rather
  than crashing, but hasn't been exercised against real edge cases yet.
- `overrides.json` format is a first draft (out/in element ID lists) —
  untested for usability; may want a name-based format instead once used
  for real (the `--lookup` helper exists as a stopgap for this).
