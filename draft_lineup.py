"""Draft league best-XI comparison tool.

Standalone script for a 7-manager FPL Draft mini-league -- separate from
the SKLW club tool in sklw_lineup.py (different game, different squads,
no shared code between them). FPL Draft's own API is private/session-
gated, so instead of pulling squads live, each team's 15-man squad below
was transcribed from a screenshot of the league's draft board and is
matched against the public Classic FPL bootstrap-static player list to
get projections. If squads change (transfers/waivers), update
DRAFT_SQUADS below.

Prints each team's best possible VALID starting XI (1 GK, 3-5 DEF, 2-5
MID, 1-3 FWD) for the upcoming GW, using FPL's ep_next by default or a
Solio-style projections CSV via --projections (same format/matching as
sklw_lineup.py's --projections), ranked by total projected score.
"""
from __future__ import annotations

import argparse
import csv
import sys
import unicodedata
from pathlib import Path

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"

# --- Transcribed from the league's draft board screenshot (2026-08) ---
# Each entry is (player_name, team_short_code) -- the team code disambiguates
# players who share a surname (e.g. there's more than one "Henderson" and
# "Wilson" in the FPL player pool), same short codes shown in the screenshot.
DRAFT_SQUADS: dict[str, list[tuple[str, str]]] = {
    "Axiom Analytics": [
        ("Haaland", "MCI"), ("Rice", "ARS"), ("O'Reilly", "MCI"),
        ("Lammens", "MUN"), ("Enzo", "CHE"), ("Tavernier", "BOU"),
        ("Dewsbury-Hall", "EVE"), ("J.Timber", "ARS"), ("Canvot", "CRY"),
        ("Garner", "EVE"), ("Richards", "CRY"), ("Bijol", "LEE"),
        ("McBurnie", "HUL"), ("Awoniyi", "COV"), ("Henderson", "CRY"),
    ],
    "AZFC": [
        ("B.Fernandes", "MUN"), ("Anderson", "MCI"), ("Havertz", "ARS"),
        ("Buendía", "AVL"), ("Gonzalo", "FUL"), ("Hill", "BOU"),
        ("Calafiori", "ARS"), ("Foden", "MCI"), ("Palestra", "CHE"),
        ("Collins", "BRE"), ("Hinshelwood", "BHA"), ("Branthwaite", "EVE"),
        ("Barry", "EVE"), ("Sánchez", "CHE"), ("Petrović", "BOU"),
    ],
    "Margem d'Erro": [
        ("Palmer", "CHE"), ("Szoboszlai", "LIV"), ("Mateta", "CRY"),
        ("Šeško", "MUN"), ("Tarkowski", "EVE"), ("Schade", "BRE"),
        ("Sangaré", "BRE"), ("Groß", "BHA"), ("Van Hecke", "TOT"),
        ("Vuskovic", "BHA"), ("Diomande", "NFO"), ("Pickford", "EVE"),
        ("Konsa", "AVL"), ("N.Jackson", "CHE"), ("Verbruggen", "BHA"),
    ],
    "AnB Heisenteam": [
        ("Isak", "LIV"), ("Gibbs-White", "NFO"), ("Wirtz", "LIV"),
        ("Wissa", "NEW"), ("Pedro Porro", "TOT"), ("Osula", "NEW"),
        ("Truffert", "BOU"), ("Cherki", "MCI"), ("N.Williams", "NFO"),
        ("Scott", "BOU"), ("Thiaw", "NEW"), ("Jacquet", "LIV"),
        ("A.Becker", "LIV"), ("Bruno G.", "ARS"), ("Trafford", "LEE"),
    ],
    "Harven FC": [
        ("Gabriel", "ARS"), ("Calvert-Lewin", "LEE"), ("Virgil", "LIV"),
        ("Raya", "ARS"), ("Sarr", "CRY"), ("Brobbey", "SUN"),
        ("Muñoz", "CRY"), ("Marmoush", "MCI"), ("Wilson", "LEE"),
        ("Fernandes", "TOT"), ("Colwill", "CHE"), ("Roefs", "SUN"),
        ("Kluivert", "BOU"), ("Van de Ven", "TOT"), ("Ødegaard", "ARS"),
    ],
    "Wan Trick Pony": [
        ("João Pedro", "CHE"), ("Thiago", "BRE"), ("Mbeumo", "MUN"),
        ("Ndiaye", "EVE"), ("O.Dango", "BRE"), ("Lacroix", "CHE"),
        ("Gakpo", "LIV"), ("Tzolis", "ARS"), ("Muharemović", "LEE"),
        ("Kerkez", "LIV"), ("Watkins", "AVL"), ("Ballard", "SUN"),
        ("Donnarumma", "MCI"), ("Martinez", "AVL"), ("Mosquera", "ARS"),
    ],
    "Cyclones FC": [
        ("Saka", "ARS"), ("Semenyo", "MCI"), ("Rogers", "CHE"),
        ("Gyökeres", "ARS"), ("Evanilson", "BOU"), ("Igor Jesus", "NFO"),
        ("Cunha", "MUN"), ("Gvardiol", "MCI"), ("E.Le Fée", "SUN"),
        ("Guéhi", "MCI"), ("Mukiele", "SUN"), ("Murillo", "NFO"),
        ("Wieffer", "BHA"), ("Kelleher", "BRE"), ("Sels", "NFO"),
    ],
}


def get_json(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.raise_for_status()
    return r.json()


def load_bootstrap() -> dict:
    return get_json(f"{FPL_BASE}/bootstrap-static/")


def player_lookup(bootstrap: dict) -> dict[int, dict]:
    return {p["id"]: p for p in bootstrap["elements"]}


def ep_next_points(players: dict[int, dict]) -> dict[int, float]:
    return {pid: float(p.get("ep_next") or 0.0) for pid, p in players.items()}


_EXTRA_FOLDS = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
})


def _fold(s: str) -> str:
    s = s.translate(_EXTRA_FOLDS)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.strip().lower()


def load_solio_projections(csv_path: Path, bootstrap: dict) -> tuple[dict[int, list[float]], int]:
    """Same matching approach as sklw_lineup.py's --projections: join by
    name (folded for accents/case), team only as a tiebreaker for a
    genuine shared-surname collision. Unlike the SKLW tool (one score per
    week, since SKLW is a weekly submission), this returns each player's
    FULL list of per-GW projections (one value per '<N>_Pts' column found,
    in GW order) plus the horizon length -- callers pick a fresh best XI
    for each week rather than assuming one fixed lineup holds for all of
    them, since who's actually worth starting changes week to week
    (rotation, fixtures, injuries)."""
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}
    by_name: dict[str, list[dict]] = {}
    for p in bootstrap["elements"]:
        by_name.setdefault(_fold(p["web_name"]), []).append(p)

    per_gw: dict[int, list[float]] = {}
    unmatched = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        pts_cols = sorted((c for c in reader.fieldnames if c.endswith("_Pts")),
                           key=lambda c: int(c.split("_")[0]))
        for row in reader:
            values = [float(row[c]) for c in pts_cols]
            candidates = by_name.get(_fold(row["Name"]), [])
            if len(candidates) == 1:
                per_gw[candidates[0]["id"]] = values
                continue
            if len(candidates) > 1:
                csv_team = _fold(row["Team"])
                narrowed = [p for p in candidates
                            if csv_team in _fold(team_names.get(p["team"], ""))
                            or _fold(team_names.get(p["team"], "")) in csv_team]
                if len(narrowed) == 1:
                    per_gw[narrowed[0]["id"]] = values
                    continue
            unmatched.append(row["Name"])
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        print(f"WARNING: {len(unmatched)} CSV row(s) didn't match a unique "
              f"FPL player, skipped: {shown}")
    return per_gw, len(pts_cols)


def find_solio_csv() -> Path | None:
    """Finds a Solio projections CSV automatically -- no need to rename or
    move a fresh weekly export by hand. Checks 'solio.csv' in the current
    folder first, then falls back to the most recently downloaded CSV in
    the user's Downloads folder whose header actually looks like a Solio
    export (checked by content, not just filename)."""
    here = Path("solio.csv")
    if here.exists():
        return here

    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return None
    candidates = sorted(downloads.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates:
        try:
            with p.open(encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
        except (OSError, StopIteration):
            continue
        if {"Pos", "ID", "Name", "Team"}.issubset(header) and any(c.endswith("_Pts") for c in header):
            return p
    return None


def resolve_player(name: str, team_code: str, bootstrap: dict) -> int | None:
    """Matches a screenshot-derived (name, team_code) pair to an FPL element
    ID: exact web_name match first (folded for accents/case), falling back
    to a substring match against web_name or full name. team_code (the
    3-letter short code shown on the draft board, e.g. 'CRY', 'LEE')
    disambiguates shared surnames -- there's more than one Henderson and
    Wilson in the FPL player pool. Returns None (not raises) on no/still-
    ambiguous match -- callers warn and skip rather than crash, since this
    is manually-transcribed data."""
    team_short = {t["id"]: t["short_name"] for t in bootstrap["teams"]}
    folded = _fold(name)
    candidates = [p for p in bootstrap["elements"] if _fold(p["web_name"]) == folded]
    if not candidates:
        candidates = [p for p in bootstrap["elements"]
                      if folded in _fold(p["web_name"])
                      or folded in _fold(f"{p['first_name']} {p['second_name']}")]
    if len(candidates) == 1:
        return candidates[0]["id"]
    if len(candidates) > 1:
        team_folded = _fold(team_code)
        narrowed = [p for p in candidates
                    if _fold(team_short.get(p["team"], "")) == team_folded]
        if len(narrowed) == 1:
            return narrowed[0]["id"]
    return None


def pick_best_eleven(element_ids: list[int], players: dict[int, dict],
                      points: dict[int, float]) -> list[int]:
    """Picks the highest-projected VALID starting XI (1 GK, 3-5 DEF, 2-5
    MID, 1-3 FWD) from a team's full squad. No captaincy in this league --
    every starter counts once, no doubling."""
    by_pos: dict[int, list[tuple[float, int]]] = {1: [], 2: [], 3: [], 4: []}
    for eid in element_ids:
        el = players.get(eid)
        if not el:
            continue
        by_pos[el["element_type"]].append((points.get(eid, 0.0), eid))
    for pos in by_pos:
        by_pos[pos].sort(reverse=True)

    gk = by_pos[1][0] if by_pos[1] else None
    best_total = -1.0
    best_outfield: list[tuple[float, int]] = []
    for d in range(3, 6):
        for m in range(2, 6):
            f = 10 - d - m
            if not (1 <= f <= 3):
                continue
            if d > len(by_pos[2]) or m > len(by_pos[3]) or f > len(by_pos[4]):
                continue
            combo = by_pos[2][:d] + by_pos[3][:m] + by_pos[4][:f]
            total = sum(pts for pts, _ in combo)
            if total > best_total:
                best_total = total
                best_outfield = combo

    starters = ([gk] if gk else []) + best_outfield
    return [pid for _, pid in starters]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projections", metavar="CSV_PATH",
                     help="path to a Solio-style projections CSV to score "
                          "with instead of FPL's ep_next. Falls back to "
                          "ep_next for anyone not matched in the CSV. "
                          "Defaults to solio.csv in the current folder if "
                          "it exists.")
    args = ap.parse_args()

    print("=== Draft League Best-XI Comparison ===")
    print(f"{len(DRAFT_SQUADS)} teams:")
    for team in DRAFT_SQUADS:
        print(f"  {team}")
    print()

    print("Fetching bootstrap-static...")
    bootstrap = load_bootstrap()
    players = player_lookup(bootstrap)

    ep_next = ep_next_points(players)
    solio_per_gw: dict[int, list[float]] = {}
    horizon = 1
    projections_path = Path(args.projections) if args.projections else find_solio_csv()
    if projections_path and projections_path.exists():
        solio_per_gw, horizon = load_solio_projections(projections_path, bootstrap)
        print(f"Loaded {len(solio_per_gw)} player projection(s) from "
              f"{projections_path}, covering {horizon} GW(s) (falling back "
              f"to a flat ep_next estimate each week for anyone not matched)")
    elif args.projections:
        print(f"ERROR: no projections CSV found at {projections_path}")
        sys.exit(1)
    else:
        print("No Solio projections CSV found (checked solio.csv and "
              "Downloads) -- using ep_next only. Pass --projections <path> "
              "to use a specific file.")

    def points_for_week(pid: int, week_idx: int) -> float:
        per_gw = solio_per_gw.get(pid)
        return per_gw[week_idx] if per_gw is not None else ep_next.get(pid, 0.0)

    results = []
    for team, squad in DRAFT_SQUADS.items():
        element_ids = []
        unresolved = []
        for name, team_code in squad:
            eid = resolve_player(name, team_code, bootstrap)
            if eid is None:
                unresolved.append(name)
            else:
                element_ids.append(eid)
        if unresolved:
            print(f"WARNING: {team} -- couldn't resolve: {', '.join(unresolved)}")

        # A fresh best XI is picked for EACH week rather than one fixed
        # lineup for the whole horizon, since who's worth starting changes
        # week to week (rotation, fixtures, injuries).
        season_total = 0.0
        for week in range(horizon):
            week_points = {pid: points_for_week(pid, week) for pid in element_ids}
            starters = pick_best_eleven(element_ids, players, week_points)
            season_total += sum(week_points.get(pid, 0.0) for pid in starters)
        results.append((team, round(season_total, 2)))

    results.sort(key=lambda x: -x[1])
    print(f"\n=== Ranked by projected total over {horizon} GW(s), best XI "
          f"picked fresh each week (no captain) ===")
    for team, total in results:
        print(f"  {team}: {total}")


if __name__ == "__main__":
    main()
