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
  opposing GK's score; +1 more goal per full 20 points they outscore by
  (1-19 → 1 goal, 20-39 → 2 goals, and so on; a tie or the GK
  outscoring them means the striker blanks — 0 goals).
- **1 Goalkeeper** — nominated purely as the Strikers' opposing target
  above. **The GK does NOT independently score goals of its own** — it
  has no separate H2H battle "against" the opponent's Strikers the way
  the Strikers do against the GK. A GK's only function is being a
  high-scoring target that's hard for the opponent's Strikers to beat;
  that value is already fully captured by how it affects the Strikers'
  H2H result above, not as an extra source of goals. (An earlier version
  of every scoring file in this repo — `sklw_lineup.py`'s reasoning
  comments, `sklw_matchup.py`, `calibrate_matchup.py`, `backtest.py` —
  assumed a mirrored GK-scores-too mechanic; this was proven wrong by
  reproducing 4 independent real SKLW match results exactly only once
  it was removed — see the git history around when this was found for
  the full reasoning and worked examples.)
- **11 Squad** — summed FPL score vs the opponent's 11. A goal for
  beating the opponent's total at all, +1 more per full 30-point margin
  beyond that (1-29 → 1 goal, 30-59 → 2 goals, and so on — same
  base+bonus shape as Strikers/GK, just a wider band). A tie means
  neither side scores.
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
next 11 → Squad, bottom 2 → Bench.

An earlier version of this tool briefly flipped this to top-2→Strikers,
next→GK, reasoning that since the GK doesn't independently score goals
(see the rules section above), only Strikers convert a high score into
goals at all. That reasoning missed the GK's actual value: a strong GK
score is compared against **both** of the opponent's Strikers (2
separate H2H battles denied by 1 slot), while a strong Striker score
only wins its own single battle. `backtest.py` was re-run measuring NET
goal differential (goals for minus goals conceded — a "goals for only"
metric, which is what the flip was originally based on, misses the GK's
defensive contribution entirely by construction) and confirmed
best-to-GK is correct: +0.206 avg net goal diff vs -0.005 for the
flipped order, winning head-to-head 34.5% to 20.8% at plain-mean
ranking.

**`--ceiling-weight K` (default 0.5)** ranks the top-3 GK/Strikers pool
by `projected score + K × stdev of that manager's own real week-to-week
GW score history` instead of plain projected score — `backtest.py`
found mean + 0.5×stdev beats plain mean at every K tested,
**unconditionally**, not just when the club's an underdog: SKLW's own
goal formula is convex (downside capped at 0 goals, upside an unbounded
stepped ladder), so higher variance raises *expected* goals regardless
of favourite/underdog status. Ceiling comes from
`fetch_manager_ceiling()`, which pulls each manager's own real
week-to-week score history from FPL's public `entry/{id}/history/`
endpoint (their own personal streakiness, not a synthetic per-player
model) — recalculated on every run, so a manager's ceiling naturally
updates as their season goes on. A manager missing from the ceiling map
(brand new team, request failure) just falls back to plain score, no
boost. Pass `--ceiling-weight 0` to disable it and fall back to the
pre-ceiling-weighting default of plain top-3-by-mean (also skips the
history fetch, faster). Only affects GK/Strikers — Squad/Bench selection
always uses plain score, since `backtest.py` found ceiling-weighting
dilutes/doesn't help once pooled into the Squad sum.

Whether this is the right general-purpose default depends on your
club's actual position: for a club leading in EV most weeks, the
"protect a lead" framing from classic favourite/underdog theory
initially seemed to argue against variance — but that framing is about
*win probability*, and SKLW is a *goals* format with a convex payoff
shape, so the math (and the backtest) says ceiling-weighting the H2H
slots helps regardless of standing.

**A tested and rejected refinement:** an independent model (given only
the raw SKLW rules, no access to this project's own findings) argued
GK's payoff is actually *concave*, not convex — it's purely short two
of the opponent's option positions, gets no reward for scoring higher
than "enough to clear the bar," so it should be picked for a reliable
*floor* (mean − k×std) rather than ceiling, while only Strikers should
chase ceiling. Theoretically sound reasoning about the payoff shapes in
isolation — but `backtest.py`'s `assign_method_c` tests it directly
(GK-by-floor, Strikers-by-ceiling, as a split rather than method B's
uniform ceiling-weighting) and it loses at every floor-weight tested,
going net-negative past `k_gk≈1.5`. The reason: mean and ceiling are
strongly correlated in real FPL scores (r≈0.70 measured on the archive)
— chasing a lower-variance floor means systematically picking a
*lower-mean* player, and GK's absolute score still has to clear the bar
against two opposing Strikers, so the mean sacrifice costs more than
the safety buys. Kept in `backtest.py` as a documented negative result
(with a correctness-only test in `test_scoring.py`, not a performance
claim) rather than deleted — the uniform ceiling-weighting in method B
remains the validated approach, live as `--ceiling-weight` above.

`--effective-ownership` (below) is the complementary tool for the
captaincy-level version of the same
question.

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
a CSV: considers both `solio.csv` in this folder (if present and it
actually looks like a Solio export) and the most recently downloaded
matching CSV in your Downloads folder, and picks whichever is NEWER by
modification time — so a fresh weekly export dropped in Downloads is
picked up automatically even if there's an older `solio.csv` still
sitting in this folder from a previous week (an earlier version always
preferred the local file unconditionally, which silently blocked a
freshly downloaded one from ever being used — fixed after hitting this
in practice).

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

`--effective-ownership` prints each real player's Effective Ownership
(EO) across the club's own 16 managers, instead of the normal lineup
suggestion: `EO% = (# managers starting them + # managers CAPTAINING
them) / 16` — captaincy counts as an extra full share, the same
convention as FPL's own global EO stat, just scoped to this specific
club's 16 rather than the whole game (the public API doesn't expose
global captaincy%, and the whole FPL player base isn't the relevant
comparison anyway — the only "field" that matters for an SKLW matchup is
the other 31 real managers involved in it). A manager captaining the
club's template pick has a tight, low-variance score distribution; a
manager captaining a genuine low-EO differential has a wide one.

Classic favourite/underdog theory (favourite minimizes variance, only
the underdog chases it) turns out to be the wrong lens here — that
framing is about *win probability* in a symmetric-payoff game. SKLW
isn't symmetric: the goal formula is convex (downside capped at 0,
upside an unbounded stepped ladder — see `--ceiling-weight` above), so
widening a manager's outcome distribution raises *expected goals*
whether the club is ahead or behind on projected EV. So the main use of
this is the same regardless of standing: use it alongside
`--ceiling-weight` to see WHY a given manager scored high on ceiling —
if it's coming from a genuine differential captaincy, that's the signal
to actively put them in Strikers/GK rather than avoid it.

Reads `--mode`/`--overrides` the same as a normal run, so it reflects
recorded transfers/wildcards/TC picks in `--mode preview` — rerun after
every `--transfer` to keep it current, same as the normal lineup
suggestion.

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
and forces them into the suggested lineup's GK slot first, then the
Striker slots — GK is the scarcer, more valuable individual-role slot
(see above), so the highest-priority FH pick claims it first. Free Hit
scores get no chip adjustment (unlike Bench Boost or Triple Captain), so
they count at full value regardless of which role they land in. Repeat
the flag for multiple managers on FH the same week — the first takes
GK, up to 2 more take the Striker slots, any beyond that stay in the
normal pool with a note printed.

`--never-bench "Manager Name"` guarantees that club member a Squad slot
even if their projected score would otherwise land them in Bench —
swapped in for whoever's currently the weakest Squad member. This is a
one-off human preference override, not an EV-maximizing suggestion (a
captain not wanting to bench themselves regardless of the numbers, say)
— it doesn't touch GK/Strikers, only rescues someone out of Bench.
Repeat for multiple managers; if both current Bench slots are
protected, both get rescued (the second rescue is guaranteed not to
re-bench the first, even if the first-rescued manager is now the
weakest Squad member). A name that doesn't match any of the 16 scores
just prints a warning and is otherwise ignored, rather than erroring
the whole run over a typo.

Every run also prints a banner up front with the club name ("Algorithm
and Blues" — edit `CLUB_NAME` at the top of the script if this changes)
and the full roster of member names → FPL manager IDs, so it's always
obvious which club/roster a given run is using.

## Setup

```
pip install -r requirements.txt
python sklw_lineup.py --mode final
```

## Tests

`test_scoring.py` locks in the core scoring rules (stdlib `unittest`, no
extra dependency, no network calls) — run `python3 -m unittest
test_scoring.py -v`. This project deliberately duplicates small scoring
helpers across `sklw_lineup.py`/`sklw_matchup.py`/`calibrate_matchup.py`/
`backtest.py` rather than sharing code between them, which is a real
drift risk: a fix applied to one copy can silently fail to reach the
others. These tests import all four files and check every copy against
the same rules, and specifically pin down the two bugs that previously
took real match results to catch — the GK not independently scoring
goals, and the base-plus-bonus Squad formula — plus the validated
GK-gets-first-pick role-assignment priority, so a future edit can't
quietly reintroduce either without a test failing first.

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

Once a manager's real picks for the target GW are actually confirmed
(deadline passed), their REAL submitted starting-11 and REAL captain are
used as-is, not guessed at — this matters a lot mid-gameweek, since a
manager's real captain (including a real Triple Captain) might not be
whoever the highest-projected player in their squad happens to be, and
doubling the wrong player once their game's already partly or fully
played would silently give a wrong real score. Before the deadline
(only an older/fallback squad is available as a proxy), it still
estimates via best-xi as before.

**Automatic substitutions.** FPL only finalizes real auto-subs (a starter
who blanked getting swapped for a bench player who played) — and a
captain → vice-captain transfer, if the real captain blanks — once the
ENTIRE gameweek is over, not progressively as individual matches finish.
That means neither `position`, `multiplier`, nor `is_captain` in live
picks data reflect a sub that's already effectively locked in mid-
gameweek (a starter's own match already finished with 0 minutes) — using
any of them directly silently keeps a confirmed blank and drops whoever
should already be subbed in. Instead this predicts what FPL will settle
on, the same way a live-tracking site like livefpl.net does, but
computed directly from the public FPL API rather than depending on a
third-party site: a player only counts as a CONFIRMED blank once their
OWN fixture has actually finished with 0 minutes (still in progress or
not started yet just means "hasn't played", not "won't play" — never
guessed at). GK blanks are replaced by the reserve GK if the reserve
played; outfield blanks by the next eligible already-finished-and-played
bench player in priority order, only if it keeps a legal formation. The
one exception is Bench Boost, where the originally declared lineup is
used directly — SKLW overrides real FPL's own BB rule (bench still
doesn't count here) and there's no bench to sub in once BB is active
anyway. `sklw_lineup.py`'s scoring keeps the simpler `position`/
`is_captain` check (it doesn't yet have full auto-sub prediction) — a
known gap there for a future pass if it turns out to matter in practice.

A fixture is only treated as "confirmed over" (needed before a 0-minute
player counts as a real blank) once its `finished_provisional` flag is
true — NOT the stricter `finished` flag, which doesn't flip true until
bonus points are officially locked in, often hours after a match
actually ends. Waiting on the strict flag silently meant no
substitution was ever predicted at all, confirmed against a real
mismatch (the tool showed 43 for a manager whose real score was 47 —
a blanked player never got swapped for their real bench replacement,
who'd already played and scored). Fixed and reproduced the real 47
exactly using that manager's actual real picks and live data.

```
python sklw_matchup.py --them-file opponent.json
```
`opponent.json` is just `{"Name": id, ...}` for their 16 managers — get
these from the manager's own FPL page URL (`fantasy.premierleague.com/
entry/<ID>/...`), same as any FPL manager ID. Our own roster defaults to
`sklw_lineup.py`'s `MANAGER_IDS`; override with `--us-file` for a
different matchup. Add `--projections solio.csv` for Solio-based
projections instead of `ep_next` (same CSV format as `sklw_lineup.py`).

**`--opponent "Club Name"`** looks the opponent's 16 managers up by club
name from a local `clubs.json`, instead of pasting `--them-file
opponent.json` by hand every matchup. Build `clubs.json` once (and
re-run whenever the league's master list updates):
```
python build_clubs_json.py SKLW_master_list.csv
```
This reads the league's own master roster CSV (columns: `tab,
team_group, handle, fpl_id, fpl_team_name, manager_name` — one row per
manager, `team_group` is the club name) and writes `clubs.json`:
`{"Club Name": {"@handle": id, ...}, ...}` — IDs + handle (or, if a row
has no usable handle, the FPL team name) are considered fine to be
public; `manager_name` (real/government name) is dropped
unconditionally and never appears in the output, public repo or not.
Some rows on the real master list turned out to have no proper @handle
at all — just the manager's real name typed into the handle column
instead — so this is guarded explicitly, not just by dropping the
`manager_name` column: any candidate label that exactly matches that
row's own `manager_name` is skipped in favour of the next fallback
(`fpl_team_name`, then a generic `ManagerN` placeholder if even that
matches). `clubs.json` and `build_clubs_json.py` are both safe to
commit; the raw `SKLW_master_list*.csv` stays in `.gitignore` and
should never be committed itself, since `manager_name` sits right there
in plain columns for every manager league-wide. `--opponent` does a
case-insensitive substring match against club names in `clubs.json` and
errors clearly (listing all known clubs) if it's missing, matches more
than one, or matches none — never a silent guess. `--clubs-file path`
points at a different file if you keep it somewhere other than the
project folder.

If the master list lives in a Google Sheet shared as "Anyone with the
link can view", `fetch_master_list.py` grabs it directly instead of
exporting/downloading a CSV by hand each time:
```
python fetch_master_list.py --sheet-id <SHEET_ID> --gid <GID>
```
`<SHEET_ID>` is the long ID in the sheet's URL
(`.../spreadsheets/d/<SHEET_ID>/...`); `<GID>` is the tab ID (the number
after `#gid=` when that tab is open). Uses Google's plain unauthenticated
per-tab CSV export endpoint — no login, no browser automation (unlike
`fetch_solio.py`, which needs one because Solio's own site enforces a
real Google sign-in). If the sheet isn't actually shared publicly,
Google serves an HTML sign-in page instead of CSV — this is checked for
explicitly and fails clearly rather than silently writing garbage HTML
into the output file. The master list may be split across multiple
tabs (this league's is — a bye-week team turned out to live on a
separate tab from the main list); repeat `--gid` to fetch several and
concatenate them into one CSV. Chain straight into `build_clubs_json.py`
afterwards, same as a manually-exported CSV.

**Auto-filling GK/Strikers from the sheet (`fetch_sheet_workbook.py` +
`resolve_matchup_roles.py`).** The same master workbook also has, per
club, who's actually playing GK and Strikers each week — but that's
only readable from the FORMATTED sheet, not a plain CSV: it's shown by
cell background color (2 blue-filled rows = Strikers, the third,
differently-filled row = GK), not a text label, and CSV export throws
color away entirely. It's also on a tab ("Live Scores") that turned out
not to be shared publicly (401'd, same as the master list), and
fixtures are redrawn fresh every week rather than following a fixed
schedule, so there's no way to hardcode which club plays which tab in
advance either.

Two standalone scripts handle this:
```
python fetch_sheet_workbook.py --login
python fetch_sheet_workbook.py --sheet-id <SHEET_ID>
python resolve_matchup_roles.py --our-club "Algorithm"
```
`fetch_sheet_workbook.py` is network-only, same login pattern as
`fetch_solio.py` (a one-time manual Google sign-in in a real visible
browser window, using a persistent Chrome profile reused afterwards —
no credentials ever touch disk). Since colors matter here, it downloads
the ENTIRE workbook as `.xlsx` (`pip install openpyxl` first) via the
authenticated session hitting the export URL directly — xlsx keeps
cell fill colors, CSV doesn't.

`resolve_matchup_roles.py` is pure logic, no network, fully unit-tested
against a synthetic workbook (see `test_scoring.py`): it opens "Live
Scores" to find which `M#` tab is your club's fixture this week
(fixtures aren't a fixed schedule, so this has to be looked up fresh
each time), reads that tab's two clubs (whichever club's name is in the
TOP banner occupies the LEFT-hand columns, confirmed convention), and
within each club's 16-manager block classifies the top 3 rows by
comparing their fill colors PAIRWISE rather than matching a specific
hardcoded color — the two that match are Strikers, the odd one out is
GK — so a color-scheme tweak next season doesn't silently break it.
Handles are then resolved to FPL IDs via `clubs.json` and written
straight into `matchup_pins.json`, the same file `sklw_matchup.py`'s
interactive GK/Strikers prompts already save to — so a normal
`sklw_matchup.py --opponent "..."` run afterwards picks them up
automatically with no manual ID entry on either side.

This is automated color/position parsing of someone else's spreadsheet,
not an official export format — sanity-check the first real run's
printed GK/Strikers names against what you can see in the sheet
yourself before trusting it blindly, same caution as everything else
mid-gameweek in this project.

Picks up the SAME `overrides.json` `sklw_lineup.py` writes to — any
`--wildcard`/`--transfer`/`--tc`/`--set-score` already recorded there
applies automatically (matched by manager ID, works for either roster,
not just our own). `--overrides path.json` points at a different file if
needed. A `manual_score` override is treated as a fixed, zero-variance
number for that manager in every simulated trial, same as it is in
`sklw_lineup.py`.

Output also breaks down where the goals are expected to come from —
Strikers-vs-their-GK, Squad-vs-Squad — so you can see which part of the
match is actually deciding the result, not just the final win/draw/loss
split. (There's no separate "GK battle" category — see the rules section
above on why the GK doesn't independently score.)

If you run this mid-gameweek (some matches already kicked off or
finished), it automatically pulls FPL's live scores and locks in any
player whose fixture has started or finished to their REAL points
instead of still guessing at them with a projection — only players whose
match genuinely hasn't started yet still get simulated. A player
currently mid-match is locked to their CURRENT live score, not a
predicted final one (a deliberate simplification — doesn't model the
upside/downside still left in that specific match, but still strictly
more accurate than pretending nothing has happened yet). Prints how many
players got locked so you know it's using live data. Run well before the
gameweek starts and this is a no-op — nothing's live yet, so it behaves
exactly as before.

If anything's locked, it also prints a separate "Real score so far"
section — the actual current scoreline computed ONLY from already-known
real results (0 for anyone who hasn't played yet), plus exactly how many
starters on each side are still pending. This exists so a suspiciously
confident result isn't just a black box: if most of a gameweek has
already been played (e.g. only one late kickoff left), the simulated
win probability CAN legitimately collapse toward 100%/0% — check this
section to see the real numbers behind that and how many players are
actually still undecided, rather than trusting the percentage blind.
Right below the pending-count line, a "Who's still to play (per
manager)" section lists the actual player NAMES behind each manager's
pending count — a bare "104 of our starters, 104 of theirs" doesn't say
WHO, e.g. a club currently leading on already-known results just
because more of its players happened to have early kickoffs isn't
visible from a count alone.

By default both sides' GK/Strikers/Squad/Bench are assigned by assumed-
optimal projection ranking (same rule as `sklw_lineup.py`). If you
actually know a club's REAL declared roles for this matchup (scouted
from their lineup, rather than guessed), pin them by FPL manager ID
instead of letting the tool assume:
```
--them-gk-id "879" --them-strikers-ids "627589,111982" --them-bench-ids "1859490,41471"
```
(or the `--us-gk-id`/`--us-strikers-ids`/`--us-bench-ids` equivalents for
our own club). Pin as many or as few of these as you actually know —
whatever's left unpinned still gets ranked as normal to fill the
remaining slots. A name pinned to more than one role is resolved by
priority GK > Strikers > Bench rather than erroring.

`--us-fh-id "ID"` / `--them-fh-id "ID"` mark a manager as playing Free
Hit this GW (repeatable for multiple managers) — prioritized into GK
first, then Strikers, same rule and reasoning as `sklw_lineup.py`'s
`--fh`. An earlier version of this tool had NO Free Hit handling at
all, so an FH manager's often-unrepresentative projection could
silently land them in Bench instead of getting the individual-role
priority `sklw_lineup.py` already gives them for the same real matchup.
Beaten by an explicit `--us-gk-id`/`--us-strikers-ids`/`--us-bench-ids`
pin for the same manager — real scouted knowledge always wins over an
FH-based guess.

If you don't pass `--them-gk-id`/`--them-strikers-ids`/`--us-gk-id`/
`--us-strikers-ids` on the command line, the tool interactively prompts
for each one instead (skippable by just pressing Enter) — remembering
the exact flag names and retyping the whole command isn't something
most captains will bother with mid-week just to pin a scouted GK.
Doesn't apply to `--them-bench-ids`/`--us-bench-ids` (Bench doesn't
affect scoring, so there's nothing worth prompting for). Pass
`--no-prompt` to skip all of this for a non-interactive/scripted run —
unpinned managers just get the normal assumed-optimal assignment, same
as always. Also automatically skipped if stdin isn't a real interactive
terminal (e.g. output piped to a file), so it never hangs waiting for
input that isn't coming.

**Whatever you enter (or pass via flag) for GK/Strikers is remembered**
in `matchup_pins.json` (gitignored — local only) so you don't have to
retype the same scouted IDs on every run within the same gameweek. "Us"
pins apply regardless of opponent (your own declared lineup doesn't
change based on who you're facing); "them" pins are stored per opponent
(keyed by `--opponent` club name, or one shared slot for a raw
`--them`/`--them-file` run). Reusing a saved value always prints which
one it used, so it's never a silent surprise, and an explicit CLI flag
always overrides it for that run. `--reset-pins` clears saved pins for
the current opponent (or everything, `--opponent` included, if run
without one) before continuing, rather than needing to delete the file
by hand. `--pins-file path` points at a different file if needed. Free
Hit pins (`--us-fh-id`/`--them-fh-id`) are deliberately NOT persisted —
unlike GK/Strikers, FH is a one-time chip, not a stable weekly fact, so
silently re-forcing a manager into GK/Strikers on some future week long
after their real FH chip is gone would be a real footgun rather than a
convenience.

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
squads are realistic enough to expose it. **This is live in
`sklw_matchup.py`** (`build_team_gw_residuals`/`pick_team_shocks`) since
real FPL squads are already formation-realistic by construction
(`pick_best_eleven`), so this result transfers. Calibration is
meaningfully better but still not perfect — treat "clearly favoured" as
meaningful, exact numbers at the extremes with a bit more caution than
the middle of the range.

**Re-ran the full calibration after fixing the GK-scoring bug, reverting
the Squad formula, and correcting the role-assignment priority back to
best-player→GK** (see the rules section above — every number before this
point predates those fixes): **Brier 0.191**, slightly better than the
pre-fix 0.194 best result, so the team-correlation finding and overall
calibration quality both hold up — if anything a bit better — under the
fully corrected rules. Similar overconfident-at-the-extremes pattern as
before (a "0-10%" bucket realized 13%, a "40-50%" bucket realized only
35%, "70-80%" realized 62% — some buckets close, a few off by 10+
points), same overall shape as every previous run of this calibration.
Nothing here suggests the earlier findings were artifacts of any of the
three bugs — the calibration *methodology* was always independent of
them; only the exact number moves a little each time a rules bug gets
fixed.

**Does it get the exact scoreline right, not just win/loss?** Asked
whether the win-probability model should use a **Dixon-Coles**
correction for this. Looked into it and it doesn't actually apply here:
Dixon-Coles fixes a specific problem in *parametric* models — assume
each side's goals are independent Poisson processes with fixed
attack/defense rates, discover real low scores (0-0, 1-0, 0-1, 1-1)
don't follow that independence assumption, add a correction term for
exactly those four cells. `sklw_matchup.py` doesn't fit that shape at
all — it runs full Monte Carlo simulation (thousands of trials of both
clubs' actual player-level FPL scores, correlated via the team-scoped
block bootstrap above), then applies SKLW's exact goal rules to each
trial. Whatever joint correlation exists between the two sides'
scorelines is already whatever the simulation produces; there's no
independence assumption being smuggled in for Dixon-Coles to correct.
Bolting one on anyway would double-correct for something already
handled structurally.

The actually-relevant question — is the simulated distribution of
*exact scorelines* (not just who wins) calibrated — is real and now
checked directly, extending `calibrate_matchup.py` with the full
simulated (g1, g2) distribution per trial instead of just a win
probability:
- **Multiclass Brier score** (0 = perfect; naturally higher than the
  binary win/loss Brier above, since there are many more exact outcomes
  to get right, not just 2) — a single summary number for the whole
  scoreline distribution's accuracy.
- **Pooled probability-bucket calibration**: every (trial,
  predicted-scoreline) pair the model assigned a nonzero probability to
  is one data point — generalizes the win-probability bucket check
  above to every possible outcome instead of just win/loss.
- **Common-scoreline check** — the specific thing Dixon-Coles corrects
  for in a parametric model (0-0/1-0/0-1/1-1 systematically mismatched
  under an independence assumption) — checked directly against real
  historical outcomes instead of assumed: average predicted probability
  vs. realized frequency for each of the 9 lowest scorelines plus
  "other".

Full-size run (`--trials 1200 --inner-sims 300`, the default): win
Brier 0.1930 (consistent with the win/loss-only numbers above),
multiclass Brier 0.9501. The pooled bucket check showed real
overconfidence in the 10-30% predicted range (e.g. the 20-30% bucket:
610 data points, 23.1% predicted vs. 13.9% realized) — a genuine,
previously invisible miscalibration the win/loss-only check couldn't
have caught, since a scoreline can be wrong in a way that doesn't flip
who wins. The common-scoreline table showed the model under-predicting
"other" (41.7% predicted vs. 50.8% realized) and over-predicting each
of 1-0/0-1/2-0/0-2 by roughly 2-2.5 percentage points — real scorelines
are more spread out (more goals, more varied margins) than the
simulation produces.

**Is that just uniform under-dispersion?** Tested directly with a
`--variance-scale` multiplier (`simulate_member_score`'s
`variance_scale` parameter, default 1.0 = unchanged behavior) applied
to every resampled residual before it's added to a player's xP, and a
`--variance-scale-sweep S1,S2,...` flag that reruns the *same* built
trials at each scale (same trick as `backtest.py`'s k-sweep — isolates
the scale's effect from trial-to-trial randomness) and reports win
Brier, multiclass Brier, the win-probability "extremes gap" (average
miscalibration in the 0-10% / 90-100% predicted-probability buckets),
and the "other"-scoreline gap for each. If a single scale minimized
all four at once, that would be strong evidence the simulation is
simply under-dispersed everywhere. It doesn't:

```
 scale   win Brier   multiclass Brier   extremes gap   other-bucket gap
  0.80      0.1772             0.9735           6.0%              20.6%
  1.00      0.1730             0.9503           2.3%              14.6%
  1.20      0.1737             0.9499           4.2%               8.8%
  1.40      0.1737             0.9419           3.8%               3.4%
  1.60      0.1770             0.9424           5.4%               1.7%
  1.80      0.1777             0.9455           4.0%               6.1%
  2.00      0.1795             0.9500           7.8%              10.3%
```
(`--trials 500 --inner-sims 150 --variance-scale-sweep
"0.8,1.0,1.2,1.4,1.6,1.8,2.0"`; a smaller sweep at `--trials 200
--inner-sims 100` showed the identical pattern.)

Win/loss accuracy (win Brier, extremes gap) is best right at the
current default, scale=1.0, and gets steadily *worse* past it.
Scoreline-spread accuracy (the "other"-bucket gap) keeps improving well
past 1.0, bottoming out around 1.4-1.6, before getting worse again.
There's no scale that's good at both — ruling out plain uniform
under-dispersion as the explanation. Also ruled out: the test season
(2023-24) being intrinsically more volatile than the training season
(2022-23) — it's actually the opposite (lower per-position residual
stdev in every one of GK/DEF/MID/FWD), so the model already imports
*more* spread than the test season's own data would suggest and still
under-spreads scorelines. The real cause is something more structural
than a single knob can reach — most likely the strength of cross-player
correlation (same-team, same-manager-squad, or both) rather than each
player's individual variance. `--variance-scale` and
`--variance-scale-sweep` are kept as a documented diagnostic (default
behavior unchanged); this is a live known gap, not a fixed one — see
"Known gaps" below.

Bottom line for using this project day-to-day: **win/loss probabilities
are reliably calibrated; exact scorelines are directionally right but
mildly under-spread** (real matches land on rarer, higher-margin
scorelines slightly more often than the simulation predicts). Treat any
single scoreline probability as approximate.

## Known gaps / next steps

- Exact scoreline predictions are mildly under-spread (see the
  scoreline-calibration section above) — win/loss is reliably
  calibrated, but real matches land on rarer/higher-margin scorelines a
  bit more often than simulated. Ruled out plain uniform under-dispersion
  (a `--variance-scale` sweep found no single scale that's simultaneously
  best for win/loss accuracy and scoreline-spread accuracy) and ruled out
  the test season being unusually volatile (it's less volatile than
  training, if anything). Likely cause is under-modeled cross-player
  correlation strength rather than per-player variance; not yet fixed.
- No `docs/rules.md` yet — this README doubles as the rules reference for
  now. Paste in the original rules doc there if/when available.
- `ep_next` (FPL's own expected-points field) is a decent free signal but
  much simpler than what the Axiom/fpl-model project's own projection
  engine does. If sharper projections are wanted later, the natural
  upgrade is pointing this script at a CSV export from that project
  instead of `ep_next` — but that's a separate project (`fpl-model`,
  different repo, different session scope) and deliberately NOT wired in
  here to keep this tool fully standalone. Don't import from it directly.
- Error handling for manager IDs that 404 entirely (typo'd ID), a
  manager who hasn't set a team for the GW yet, or partial squads (< 15
  players, e.g. a brand new team) is best-effort: it skips/warns rather
  than crashing, but hasn't been exercised against real edge cases yet.
- `overrides.json` format is a first draft (out/in element ID lists) —
  untested for usability; may want a name-based format instead once used
  for real (the `--lookup` helper exists as a stopgap for this).
- **`backtest.py` has been re-run twice on the role-assignment question,
  and the metric itself needed a fix along the way.** After the
  GK-scoring bug was removed from `match_goals`, an initial re-run
  compared methods using "goals for" alone and concluded top-2→Strikers/
  3rd→GK was decisively better — but that metric only measures a club's
  own attacking output, and can never see a strategy's defensive effect
  (the opponent's Strikers scoring against *your* GK), so it structurally
  can't detect the GK's actual value. Fixing the comparison to use NET
  goal differential (goals for minus goals conceded, where "conceded"
  depends on the opponent's Strikers vs. your own GK choice) reversed
  that finding back: **best player → GK, next 2 → Strikers** wins,
  averaging +0.206 net goal diff per matchweek vs. -0.005 for the
  flipped order, and winning head-to-head 34.5% to 20.8% of the time at
  plain-mean ranking. This is the live default in `sklw_lineup.py`
  (`suggest_lineup`) and `sklw_matchup.py`/`calibrate_matchup.py`
  (`assign_roles`).
- `calibrate_matchup.py` has also been re-run with the fully corrected
  rules and role priority — see the accuracy section above (Brier 0.191,
  slightly better than the earlier team-correlation finding's 0.194,
  consistent with it).
- `sklw_lineup.py` still lacks the full auto-sub *prediction* that
  `sklw_matchup.py` has (`predict_effective_lineup`, driven by live
  minutes and `finished_provisional`). It only uses the real
  `multiplier`/`position` fields, which FPL doesn't update until the
  entire gameweek ends — so mid-gameweek its projections for a manager
  with a blank starter can be stale the same way `sklw_matchup.py`'s were
  before that fix. Porting `predict_effective_lineup` over is the
  natural next step if this becomes a problem in practice.
