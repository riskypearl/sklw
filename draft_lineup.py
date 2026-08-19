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
import unicodedata
from pathlib import Path

import requests

FPL_BASE = "https://fantasy.premierleague.com/api"

# --- Transcribed from the league's draft board screenshot (2026-08) ---
DRAFT_SQUADS: dict[str, list[str]] = {
    "Axiom Analytics": [
        "Haaland", "Rice", "O'Reilly", "Lammens", "Enzo", "Tavernier",
        "Dewsbury-Hall", "J.Timber", "Canvot", "Garner", "Richards",
        "Bijol", "McBurnie", "Awoniyi", "Henderson",
    ],
    "AZFC": [
        "B.Fernandes", "Anderson", "Havertz", "Buendía", "Gonzalo", "Hill",
        "Calafiori", "Foden", "Palestra", "Collins", "Hinshelwood",
        "Branthwaite", "Barry", "Sánchez", "Petrović",
    ],
    "Margem d'Erro": [
        "Palmer", "Szoboszlai", "Mateta", "Šeško", "Tarkowski", "Schade",
        "Sangaré", "Groß", "Van Hecke", "Vuskovic", "Diomande", "Pickford",
        "Konsa", "N.Jackson", "Verbruggen",
    ],
    "AnB Heisenteam": [
        "Isak", "Gibbs-White", "Wirtz", "Wissa", "Pedro Porro", "Osula",
        "Truffert", "Cherki", "N.Williams", "Scott", "Thiaw", "Jacquet",
        "A.Becker", "Bruno G.", "Trafford",
    ],
    "Harven FC": [
        "Gabriel", "Calvert-Lewin", "Virgil", "Raya", "Sarr", "Brobbey",
        "Muñoz", "Marmoush", "Wilson", "Fernandes", "Colwill", "Roefs",
        "Kluivert", "Van de Ven", "Ødegaard",
    ],
    "Wan Trick Pony": [
        "João Pedro", "Thiago", "Mbeumo", "Ndiaye", "O.Dango", "Lacroix",
        "Gakpo", "Tzolis", "Muharemović", "Kerkez", "Watkins", "Ballard",
        "Donnarumma", "Martinez", "Mosquera",
    ],
    "Cyclones FC": [
        "Saka", "Semenyo", "Rogers", "Gyökeres", "Evanilson", "Igor Jesus",
        "Cunha", "Gvardiol", "E.Le Fée", "Guéhi", "Mukiele", "Murillo",
        "Wieffer", "Kelleher", "Sels",
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


def load_solio_projections(csv_path: Path, bootstrap: dict) -> dict[int, float]:
    """Same matching approach as sklw_lineup.py's --projections: join by
    name (folded for accents/case), team only as a tiebreaker for a
    genuine shared-surname collision."""
    team_names = {t["id"]: t["name"] for t in bootstrap["teams"]}
    by_name: dict[str, list[dict]] = {}
    for p in bootstrap["elements"]:
        by_name.setdefault(_fold(p["web_name"]), []).append(p)

    points: dict[int, float] = {}
    unmatched = []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            candidates = by_name.get(_fold(row["Name"]), [])
            if len(candidates) == 1:
                points[candidates[0]["id"]] = float(row["1_Pts"])
                continue
            if len(candidates) > 1:
                csv_team = _fold(row["Team"])
                narrowed = [p for p in candidates
                            if csv_team in _fold(team_names.get(p["team"], ""))
                            or _fold(team_names.get(p["team"], "")) in csv_team]
                if len(narrowed) == 1:
                    points[narrowed[0]["id"]] = float(row["1_Pts"])
                    continue
            unmatched.append(row["Name"])
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" ..." if len(unmatched) > 10 else "")
        print(f"WARNING: {len(unmatched)} CSV row(s) didn't match a unique "
              f"FPL player, skipped: {shown}")
    return points


def resolve_player(name: str, bootstrap: dict) -> int | None:
    """Matches a screenshot-derived player name to an FPL element ID: exact
    web_name match first (folded for accents/case), falling back to a
    substring match against web_name or full name if that's ambiguous or
    empty. Returns None (not raises) on no/ambiguous match -- callers warn
    and skip rather than crash, since this is manually-transcribed data."""
    folded = _fold(name)
    exact = [p for p in bootstrap["elements"] if _fold(p["web_name"]) == folded]
    if len(exact) == 1:
        return exact[0]["id"]
    if len(exact) > 1:
        return None
    candidates = [p for p in bootstrap["elements"]
                  if folded in _fold(p["web_name"])
                  or folded in _fold(f"{p['first_name']} {p['second_name']}")]
    return candidates[0]["id"] if len(candidates) == 1 else None


def pick_best_eleven(element_ids: list[int], players: dict[int, dict],
                      points: dict[int, float]) -> tuple[list[int], int | None]:
    """Picks the highest-projected VALID starting XI (1 GK, 3-5 DEF, 2-5
    MID, 1-3 FWD) from a team's full squad. Returns (starter_ids, captain_id)
    -- captain is whichever starter has the highest projection."""
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
    captain = max(starters)[1] if starters else None
    return [pid for _, pid in starters], captain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projections", metavar="CSV_PATH",
                     help="path to a Solio-style projections CSV to score "
                          "with instead of FPL's ep_next. Falls back to "
                          "ep_next for anyone not matched in the CSV.")
    args = ap.parse_args()

    print("=== Draft League Best-XI Comparison ===")
    print(f"{len(DRAFT_SQUADS)} teams:")
    for team in DRAFT_SQUADS:
        print(f"  {team}")
    print()

    print("Fetching bootstrap-static...")
    bootstrap = load_bootstrap()
    players = player_lookup(bootstrap)

    points = ep_next_points(players)
    if args.projections:
        solio_points = load_solio_projections(Path(args.projections), bootstrap)
        print(f"Loaded {len(solio_points)} player projection(s) from "
              f"{args.projections} (falling back to ep_next for anyone not "
              f"matched)")
        points.update(solio_points)

    results = []
    for team, names in DRAFT_SQUADS.items():
        element_ids = []
        unresolved = []
        for name in names:
            eid = resolve_player(name, bootstrap)
            if eid is None:
                unresolved.append(name)
            else:
                element_ids.append(eid)
        if unresolved:
            print(f"WARNING: {team} -- couldn't resolve: {', '.join(unresolved)}")

        starters, captain = pick_best_eleven(element_ids, players, points)
        total = sum(points.get(pid, 0.0) for pid in starters)
        if captain is not None:
            total += points.get(captain, 0.0)  # captain doubled
        results.append((team, round(total, 2), captain))

    results.sort(key=lambda x: -x[1])
    print("\n=== Ranked by projected best-XI score ===")
    for team, total, captain in results:
        cap_name = players[captain]["web_name"] if captain else "?"
        print(f"  {team}: {total}  (captain: {cap_name})")


if __name__ == "__main__":
    main()
