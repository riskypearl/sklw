"""Builds clubs.json from the league's master list CSV.

Standalone script, separate from sklw_lineup.py/sklw_matchup.py. Reads a
CSV with columns (tab, team_group, handle, fpl_id, fpl_team_name,
manager_name) -- the shape of SKLW's own master roster export covering
every club in the league -- and writes clubs.json: {"Club Name":
{"Manager1": fpl_id, "Manager2": fpl_id, ...}, ...}.

Deliberately drops handle/fpl_team_name/manager_name entirely and
replaces each manager with a generic "ManagerN" label. clubs.json maps
real FPL entry IDs for potentially hundreds of real people across the
whole league (everyone else's club rosters, not just your own or a
single opponent's) -- that's exactly the kind of data this project has
been careful NOT to commit to this public repo even for a single
opponent, so this script's OUTPUT is listed in .gitignore. The script
itself (no personal data, just conversion logic) is safe to commit and
share.

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


def build_clubs(csv_path: Path) -> dict[str, dict[str, int]]:
    by_club: dict[str, list[int]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"team_group", "fpl_id"}
        if not required.issubset(reader.fieldnames or []):
            print(f"ERROR: {csv_path} is missing required column(s): "
                  f"{required - set(reader.fieldnames or [])}. Expected a "
                  f"header with at least 'team_group' and 'fpl_id'.")
            sys.exit(1)
        for row in reader:
            club = row["team_group"].strip()
            try:
                fpl_id = int(row["fpl_id"].strip())
            except (ValueError, KeyError):
                continue
            if club:
                by_club[club].append(fpl_id)

    clubs: dict[str, dict[str, int]] = {}
    for club, ids in by_club.items():
        clubs[club] = {f"Manager{i}": mid for i, mid in enumerate(ids, 1)}
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
