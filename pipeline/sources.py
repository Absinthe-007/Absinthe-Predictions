"""Data sources for Absinthe Predictions.

ESPN public site API  -> schedule, live scores, Vegas lines, weather, injuries, box scores
nflverse              -> every player's weekly stats back to 2018, player ID crosswalk
Sleeper               -> depth charts and injury designations
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SLEEPER = "https://api.sleeper.app/v1/players/nfl"
HIST_START = 2018

S = requests.Session()
# keep the default requests User-Agent: ESPN rejects unusual ones


def _get(url, params=None, tries=3, as_json=True, timeout=60):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json() if as_json else r.content
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url} {params}: {last}")


def _cached(name: str, max_age_hours: float):
    p = CACHE / name
    if p.exists() and (time.time() - p.stat().st_mtime) < max_age_hours * 3600:
        return p
    return None


# ---------------------------------------------------------------- ESPN
def scoreboard(season=None, week=None, seasontype=None) -> dict:
    params = {}
    if season:
        params["dates"] = season
    if week:
        params["week"] = week
    if seasontype:
        params["seasontype"] = seasontype
    return _get(f"{ESPN}/scoreboard", params or None)


def summary(event_id: str) -> dict:
    return _get(f"{ESPN}/summary", {"event": event_id})


def injuries() -> dict:
    return _get(f"{ESPN}/injuries")


def parse_events(sb: dict) -> list[dict]:
    """Flatten an ESPN scoreboard into simple game dicts."""
    games = []
    for e in sb.get("events", []):
        c = e["competitions"][0]
        teams = {x["homeAway"]: x for x in c["competitors"]}
        h, a = teams["home"], teams["away"]
        odds = (c.get("odds") or [{}])[0] or {}
        spread_home = None
        details = odds.get("details")
        if odds.get("spread") is not None:
            # ESPN 'spread' is from the home team's perspective
            try:
                spread_home = float(odds["spread"])
            except (TypeError, ValueError):
                spread_home = None
        if spread_home is None and details and details not in ("EVEN", "OFF"):
            try:
                ab, num = details.rsplit(" ", 1)
                num = float(num)
                spread_home = num if ab == h["team"]["abbreviation"] else -num
            except ValueError:
                pass
        elif details == "EVEN":
            spread_home = 0.0
        ou = odds.get("overUnder")
        status = c["status"]["type"]
        w = e.get("weather") or {}
        games.append({
            "id": e["id"],
            "date": e["date"],
            "name": e.get("name"),
            "short": e.get("shortName"),
            "week": (e.get("week") or sb.get("week") or {}).get("number"),
            "season": (e.get("season") or sb.get("season") or {}).get("year"),
            "seasontype": (e.get("season") or sb.get("season") or {}).get("type"),
            "status": status.get("name"),
            "state": status.get("state"),  # pre / in / post
            "completed": bool(status.get("completed")),
            "home": h["team"]["abbreviation"],
            "away": a["team"]["abbreviation"],
            "home_name": h["team"].get("displayName"),
            "away_name": a["team"].get("displayName"),
            "home_logo": h["team"].get("logo"),
            "away_logo": a["team"].get("logo"),
            "home_record": (h.get("records") or [{}])[0].get("summary"),
            "away_record": (a.get("records") or [{}])[0].get("summary"),
            "home_score": int(h.get("score") or 0),
            "away_score": int(a.get("score") or 0),
            "spread_home": spread_home,   # negative = home favored
            "total": float(ou) if ou not in (None, "") else None,
            "odds_text": details,
            "venue": (c.get("venue") or {}).get("fullName"),
            "indoor": bool((c.get("venue") or {}).get("indoor")),
            "neutral": bool(c.get("neutralSite")),
            "temp": w.get("temperature"),
            "weather": w.get("displayValue"),
            "broadcast": ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", [])),
        })
    return games


def season_results(season: int, upto_week: int | None = None) -> pd.DataFrame:
    """Final scores for regular-season games of a season (cached for past seasons)."""
    fname = f"results_{season}.json"
    past = upto_week is None
    p = _cached(fname, 24 * 30 if past else 0)
    if p:
        return pd.DataFrame(json.loads(p.read_text()))
    rows = []
    last = 18 if upto_week is None else upto_week
    for wk in range(1, last + 1):
        try:
            for g in parse_events(scoreboard(season, wk, 2)):
                if g["completed"]:
                    rows.append({"season": season, "week": wk, "home": g["home"], "away": g["away"],
                                 "home_score": g["home_score"], "away_score": g["away_score"],
                                 "spread_home": g["spread_home"], "total": g["total"], "id": g["id"]})
        except RuntimeError:
            continue
    if past:
        (CACHE / fname).write_text(json.dumps(rows))
    return pd.DataFrame(rows)


ESPN_BOX_MAP = {
    "passing": {"completions/passingAttempts": ("cmp", "att"), "passingYards": "pass_yds",
                "passingTouchdowns": "pass_td", "interceptions": "int"},
    "rushing": {"rushingAttempts": "rush_att", "rushingYards": "rush_yds", "rushingTouchdowns": "rush_td"},
    "receiving": {"receptions": "rec", "receivingYards": "rec_yds", "receivingTouchdowns": "rec_td",
                  "receivingTargets": "tgt"},
    "defensive": {"totalTackles": "tackles", "sacks": "sacks"},
    "interceptions": {"interceptions": "def_int"},
    "kicking": {"fieldGoalsMade/fieldGoalAttempts": ("fg", "fga"), "extraPointsMade/extraPointAttempts": ("xp", "xpa")},
}


def box_score(event_id: str) -> dict[str, dict]:
    """ESPN athlete id -> actual stat dict for a finished game."""
    d = summary(event_id)
    out: dict[str, dict] = {}
    for team in d.get("boxscore", {}).get("players", []):
        abbr = team["team"]["abbreviation"]
        for cat in team.get("statistics", []):
            m = ESPN_BOX_MAP.get(cat["name"])
            if not m:
                continue
            keys = cat["keys"]
            for a in cat.get("athletes", []):
                aid = str(a["athlete"]["id"])
                rec = out.setdefault(aid, {"name": a["athlete"]["displayName"], "team": abbr})
                for k, v in zip(keys, a.get("stats", [])):
                    tgt = m.get(k)
                    if not tgt:
                        continue
                    try:
                        if isinstance(tgt, tuple):
                            x, y = str(v).split("/")
                            rec[tgt[0]] = float(x)
                            rec[tgt[1]] = float(y)
                        else:
                            rec[tgt] = float(str(v).split("-")[0])
                    except (ValueError, IndexError):
                        pass
    return out


# ---------------------------------------------------------------- nflverse
def player_weeks(seasons: list[int]) -> pd.DataFrame:
    frames = []
    current = max(seasons)
    for y in seasons:
        fname = f"stats_player_week_{y}.csv"
        p = _cached(fname, 6 if y == current else 24 * 60)
        if not p:
            try:
                raw = _get(f"{NFLVERSE}/stats_player/{fname}", as_json=False, timeout=180)
                (CACHE / fname).write_bytes(raw)
                p = CACHE / fname
            except RuntimeError:
                if (CACHE / fname).exists():
                    p = CACHE / fname
                else:
                    print(f"  ! no nflverse stats for {y}")
                    continue
        frames.append(pd.read_csv(p, low_memory=False))
    df = pd.concat(frames, ignore_index=True)
    df = df[df["season_type"] == "REG"].copy()
    return df


def players_crosswalk() -> pd.DataFrame:
    p = _cached("players.csv", 24)
    if not p:
        try:
            (CACHE / "players.csv").write_bytes(_get(f"{NFLVERSE}/players/players.csv", as_json=False, timeout=180))
        except RuntimeError:
            if not (CACHE / "players.csv").exists():
                raise
        p = CACHE / "players.csv"
    return pd.read_csv(p, low_memory=False)


# ---------------------------------------------------------------- Sleeper
def sleeper_players() -> dict:
    p = _cached("sleeper.json", 12)
    if not p:
        try:
            (CACHE / "sleeper.json").write_text(json.dumps(_get(SLEEPER, timeout=120)))
        except RuntimeError:
            if not (CACHE / "sleeper.json").exists():
                return {}
        p = CACHE / "sleeper.json"
    return json.loads(p.read_text())
