"""Builds clubs.json from the league's master list CSV.

Standalone script, separate from sklw_lineup.py/sklw_matchup.py. Reads a
CSV with columns (tab, team_group, handle, fpl_id, fpl_team_name,
manager_name) -- the shape of SKLW's own master roster export covering
every club in the league -- and writes clubs.json: {"Club Name":
{"@handle": fpl_id, ...}, ...}.

Keeps the handle (or, if a row has no usable handle, the FPL team name)
as each manager's label -- readable output, matches what's actually on
the sheet -- but drops manager_name (real/government name)
unconditionally, public repo or not; that's a hard line regardless of
who can see this data. IDs + handles/team names are fine to be public;
real names never are. Some rows on the real master list have no proper
@handle and just have the manager's real name typed into the handle
column instead -- guarded against explicitly (see _label_for): any
candidate that exactly matches that row's own manager_name is skipped,
falling through to fpl_team_name and then a generic "ManagerN"
placeholder if even that matches. clubs.json (and this script) are both
safe to commit.

Usage:
    python build_clubs_json.py SKLW_master_list.csv
    (writes clubs.json in the current folder; re-run any time the
    master list is updated to refresh it)

--opponent "Club Name" on sklw_matchup.py reads the resulting
clubs.json to auto-fill an opponent's roster instead of pasting 16 IDs
by hand each matchup.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def _label_for(row: dict, fallback_index: int) -> str:
    """Handle first, then FPL team name, then a generic placeholder as a
    last resort -- never manager_name (real/government name). Some rows
    on the real sheet have no proper @handle and someone just typed the
    manager's real name into that column instead (confirmed against a
    real master list: e.g. handle=='Tom Mitcham', manager_name=='Tom
    Mitcham') -- so any candidate that exactly matches manager_name
    (case/whitespace-insensitive) is skipped, not just the manager_name
    column itself."""
    real_name = (row.get("manager_name") or "").strip().casefold()
    for candidate in (row.get("handle"), row.get("fpl_team_name")):
        candidate = (candidate or "").strip()
        if candidate and candidate.casefold() != real_name:
            return candidate
    return f"Manager{fallback_index}"


def build_clubs(csv_path: Path) -> dict[str, dict[str, int]]:
    by_club: dict[str, list[tuple[str, int]]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"team_group", "fpl_id"}
        if not required.issubset(reader.fieldnames or []):
            print(f"ERROR: {csv_path} is missing required column(s): "
                  f"{required - set(reader.fieldnames or [])}. Expected a "
                  f"header with at least 'team_group' and 'fpl_id'.")
            sys.exit(1)
        for i, row in enumerate(reader, 1):
            club = row["team_group"].strip()
            try:
                fpl_id = int(row["fpl_id"].strip())
            except (ValueError, KeyError):
                continue
            if club:
                by_club[club].append((_label_for(row, i), fpl_id))

    clubs: dict[str, dict[str, int]] = {}
    for club, entries in by_club.items():
        managers: dict[str, int] = {}
        seen: dict[str, int] = defaultdict(int)
        for label, mid in entries:
            seen[label] += 1
            key = label if seen[label] == 1 else f"{label} ({seen[label]})"
            managers[key] = mid
        clubs[club] = managers
    return clubs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="path to the league master list CSV "
                                      "(columns: tab, team_group, handle, "
                                      "fpl_id, fpl_team_name, manager_name)")
    ap.add_argument("--out", default="clubs.json",
                     help="output path (default: clubs.json in the current folder)")
    args = ap.parse_args()

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"ERROR: no file at {csv_path}")
        sys.exit(1)

    clubs = build_clubs(csv_path)
    if not clubs:
        print(f"ERROR: no clubs found in {csv_path} -- check it has a "
              f"'team_group' column with real values.")
        sys.exit(1)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(clubs, indent=2))

    sizes = {club: len(managers) for club, managers in clubs.items()}
    odd_sized = {club: n for club, n in sizes.items() if n != 16}
    print(f"Wrote {len(clubs)} club(s) to {out_path} ({sum(sizes.values())} managers total).")
    if odd_sized:
        print(f"NOTE: {len(odd_sized)} club(s) don't have exactly 16 managers "
              f"(check the source CSV for missing/extra rows): "
              + ", ".join(f"{club} ({n})" for club, n in sorted(odd_sized.items())))


if __name__ == "__main__":
    main()
