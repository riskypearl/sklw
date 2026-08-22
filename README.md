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
above), then suggests a lineup — top 2 scorers → Strikers, next → GK,
next 11 → Squad, bottom 2 → Bench. (Both Strikers and GK want HIGH
projected scorers, since both roles are rewarded for outscoring the
opponent, not for hiding a weak link.)

By default the projected points come from FPL's own `ep_next` field.
`--projections path/to/solio.csv` swaps that out for a Solio-style
projections CSV instead (`Pos,ID,Name,BV,SV,Team,1_xMins,...,10_xMins,
1_Pts,...,10_Pts`) — it matches players by name (Solio's `Name` column is
FPL's short "web_name", e.g. `Saka`, `J.Timber`) + team, since Solio's own
`ID` column is its own internal numbering, not the FPL element ID. Uses
the `1_Pts` column (projection for the next upcoming GW). Anyone in the
squad that the CSV doesn't match falls back to `ep_next` automatically,
with a warning listing what didn't match so you can sanity-check it.

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
