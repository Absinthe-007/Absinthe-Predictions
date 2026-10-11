"""Absinthe Predictions — main pipeline.

python -m pipeline.build --mode predict  # refresh predictions for games that haven't started
python -m pipeline.build --mode grade    # grade finished games only (predictions untouched)
python -m pipeline.build                 # manual: both
python -m pipeline.build --backfill 1-4  # also build backtest weeks 1-4 of the current season
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import sources as src
from .grade import LABELS, grade_game, grade_player
from .model import (MARGIN_SD, OUT_STATUSES, VEGAS_WEIGHT, build_context, fix_team, model_score,
                    norm_cdf, prep_stats, project_team, round_scores, stat_group, STAT_KEYS)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
WEEKS = DATA / "weeks"
PLAYERS = DATA / "players"
for d in (DATA, WEEKS, PLAYERS):
    d.mkdir(parents=True, exist_ok=True)

SLEEPER_STATUS = {"IR": "Injured Reserve", "Out": "Out", "Doubtful": "Doubtful", "Questionable": "Questionable",
                  "PUP": "PUP", "Sus": "Suspension", "NA": "Out", "DNR": "Out", "COV": "Out"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def r1(x):
    return None if x is None else round(float(x), 1)


def week_label(stype, week):
    if stype == 3:
        return {1: "Wild Card", 2: "Divisional", 3: "Conference Championships", 4: "Pro Bowl", 5: "Super Bowl"}.get(week, f"Playoffs {week}")
    return f"Week {week}"


def wfile(season, stype, week):
    return WEEKS / f"{season}-{stype}-{week:02d}.json"


# ---------------------------------------------------------------- data loading
class World:
    def __init__(self, season: int, cur_week: int):
        self.season = season
        print("• loading nflverse player stats", src.HIST_START, "->", season)
        self.stats = prep_stats(src.player_weeks(list(range(src.HIST_START, season + 1))))
        print("  rows:", len(self.stats))
        print("• loading results")
        self.results = pd.concat([src.season_results(season - 1), src.season_results(season, upto_week=cur_week)],
                                 ignore_index=True)
        print("• loading player crosswalk / Sleeper / ESPN injuries")
        cw = src.players_crosswalk()
        self.cw = cw.set_index("gsis_id")
        self.espn_of = {g: str(int(e)) for g, e in zip(cw["gsis_id"], cw["espn_id"]) if pd.notna(e) and pd.notna(g)}
        sl = src.sleeper_players()
        self.sleeper_by_espn = {str(v["espn_id"]): v for v in sl.values() if v.get("espn_id")}
        self.sleeper_by_gsis = {str(v["gsis_id"]).strip(): v for v in sl.values() if v.get("gsis_id")}
        self.espn_inj = {}
        try:
            for t in src.injuries().get("injuries", []):
                for i in t.get("injuries", []):
                    ath = i.get("athlete", {})
                    m = None
                    for l in ath.get("links", []):
                        m = re.search(r"/id/(\d+)", l.get("href", ""))
                        if m:
                            break
                    if m:
                        self.espn_inj[m.group(1)] = {"status": i.get("status"), "detail": i.get("shortComment") or i.get("longComment"),
                                                     "date": i.get("date")}
        except RuntimeError as e:
            print("  ! ESPN injuries unavailable:", e)
        print(f"  injuries tracked: {len(self.espn_inj)}")

    def sleeper(self, pid):
        e = self.espn_of.get(pid)
        return self.sleeper_by_espn.get(e) if e else self.sleeper_by_gsis.get(pid)

    def status(self, pid):
        e = self.espn_of.get(pid)
        if e and e in self.espn_inj:
            return self.espn_inj[e]["status"] or "Active", self.espn_inj[e].get("detail")
        s = self.sleeper(pid)
        if s and s.get("injury_status"):
            return SLEEPER_STATUS.get(s["injury_status"], s["injury_status"]), s.get("injury_body_part")
        return "Active", None


def pos_of(row):
    p = row["position"] if isinstance(row["position"], str) else ""
    if p in ("QB", "RB", "WR", "TE", "K"):
        return p
    if p == "FB":
        return "RB"
    g = row["position_group"] if isinstance(row["position_group"], str) else ""
    return g if g in ("DL", "LB", "DB") else None


def build_rosters(world: World, season: int, week: int, teams: set[str], backtest: bool):
    """Return team -> list of candidate players (with status)."""
    st = world.stats
    rosters = {t: [] for t in teams}
    if backtest:
        this = st[(st["season"] == season) & (st["week"] == week)]
        played = set(this["player_id"])
        recent = st[(st["season"] == season) & (st["week"] < week) & (st["week"] >= week - 3)]
        pool = pd.concat([this, recent]).sort_values(["week"]).drop_duplicates("player_id", keep="last")
        for row in pool.itertuples():
            team = row.team  # last row = this week's team if he played
            if team not in rosters or not isinstance(row.player_id, str):
                continue
            pos = pos_of(row._asdict())
            if not pos:
                continue
            rosters[team].append({"pid": row.player_id, "name": row.player_display_name, "pos": pos,
                                  "grp": row.position_group, "status": "Active" if row.player_id in played else "Out",
                                  "detail": None if row.player_id in played else "Did not play (inactive)",
                                  "depth": None, "headshot": row.headshot_url})
        return rosters

    pool = st[(st["season"] >= season - 1)].sort_values(["season", "week"])
    last = pool.drop_duplicates("player_id", keep="last")
    for row in last.itertuples():
        d = row._asdict()
        pos = pos_of(d)
        if not pos:
            continue
        s = world.sleeper(row.player_id)
        team = row.team
        if s is not None:
            if s.get("team") is None or s.get("status") in ("Inactive", "Retired"):
                continue
            team = fix_team(s["team"])
        elif row.season < season:
            continue
        if team not in rosters:
            continue
        status, detail = world.status(row.player_id)
        rosters[team].append({"pid": row.player_id, "name": row.player_display_name, "pos": pos,
                              "grp": row.position_group, "status": status, "detail": detail,
                              "depth": (s or {}).get("depth_chart_order"), "headshot": row.headshot_url})
    return rosters


def primary_qb_out(world, ctx, team, roster):
    h = ctx.hist[(ctx.hist["team"] == team) & (ctx.hist["season"] == ctx.season) & (ctx.hist["attempts"] > 0)]
    if h.empty:
        return False
    top = h.groupby("player_id")["attempts"].sum().idxmax()
    for p in roster:
        if p["pid"] == top:
            return p["status"] in OUT_STATUSES
    return True  # top passer no longer on roster


def fill_odds(g):
    if g["spread_home"] is not None and g["total"] is not None:
        return
    try:
        d = src.summary(g["id"])
        for pc in d.get("pickcenter", []) or []:
            if pc.get("overUnder") is not None and pc.get("spread") is not None:
                g["spread_home"] = float(pc["spread"])
                g["total"] = float(pc["overUnder"])
                g["odds_text"] = pc.get("details")
                return
    except RuntimeError:
        pass


# ---------------------------------------------------------------- predictions
def predict_game(world, ctx, g, rosters, backtest):
    home, away = g["home"], g["away"]
    fill_odds(g)
    mh, ma = model_score(ctx.ratings, home, away, g["neutral"])
    qb_h = primary_qb_out(world, ctx, home, rosters.get(home, []))
    qb_a = primary_qb_out(world, ctx, away, rosters.get(away, []))
    if qb_h: mh -= 3.0
    if qb_a: ma -= 3.0
    if g["spread_home"] is not None and g["total"] is not None:
        vh = (g["total"] - g["spread_home"]) / 2
        va = (g["total"] + g["spread_home"]) / 2
        h = VEGAS_WEIGHT * vh + (1 - VEGAS_WEIGHT) * mh
        a = VEGAS_WEIGHT * va + (1 - VEGAS_WEIGHT) * ma
        vegas = {"spread_home": g["spread_home"], "total": g["total"], "home": r1(vh), "away": r1(va), "text": g.get("odds_text")}
    else:
        h, a, vegas = mh, ma, None
    H, A = round_scores(h, a)
    wp = norm_cdf((h - a) / MARGIN_SD)

    team_players = {}
    for side, team, opp, tp, op in (("home", home, away, h, a), ("away", away, home, a, h)):
        res, info = project_team(ctx, world.stats, team, opp, rosters.get(team, []), tp, op, ctx.week)
        out = []
        for r in res:
            p, pr = r["p"], r["proj"]
            grp = stat_group(p["pos"])
            keys = STAT_KEYS[grp]
            out.append({"pid": p["pid"], "name": p["name"], "pos": p["pos"], "status": p["status"],
                        "detail": p.get("detail"), "espn_id": world.espn_of.get(p["pid"]),
                        "headshot": p.get("headshot") if isinstance(p.get("headshot"), str) else None,
                        "proj": {k: round(float(pr.get(k, 0.0)), 2) for k in keys if k in pr},
                        "vs_games": r["n_vs"]})
        # injured players worth listing (no projection) so the site shows who is out
        listed = {o["pid"] for o in out}
        for p in rosters.get(team, []):
            if p["status"] in OUT_STATUSES and p["pid"] not in listed and p["pos"] in ("QB", "RB", "WR", "TE"):
                u = ctx.by_pid.get(p["pid"])
                u = u[u["season"] == ctx.season] if u is not None else None
                if u is not None and len(u) and (u["targets"].sum() + u["carries"].sum() + u["attempts"].sum()) / max(len(u), 1) >= 3:
                    out.append({"pid": p["pid"], "name": p["name"], "pos": p["pos"], "status": p["status"],
                                "detail": p.get("detail"), "espn_id": world.espn_of.get(p["pid"]),
                                "headshot": p.get("headshot") if isinstance(p.get("headshot"), str) else None,
                                "proj": {}, "vs_games": 0, "out": True})
        team_players[side] = out
        team_players[side + "_info"] = {"pass_att": r1(info["pass_att"]), "rush_att": r1(info["rush_att"]),
                                        "qb_out": info["qb_out"]}

    return {
        "pred": {"home": H, "away": A, "home_raw": r1(h), "away_raw": r1(a), "spread_home": r1(a - h),
                 "total": r1(h + a), "win_prob_home": round(wp, 3),
                 "pick": home if h > a else away},
        "model": {"home": r1(mh), "away": r1(ma), "qb_out_home": qb_h, "qb_out_away": qb_a},
        "vegas": vegas,
        "players": {"home": team_players["home"], "away": team_players["away"]},
        "team_info": {"home": team_players["home_info"], "away": team_players["away_info"]},
        "predicted_at": now_iso(),
    }


def grade_completed(game):
    if not game.get("pred"):
        return
    act = game["actual"]
    game["grade"] = grade_game(game["pred"]["home"], game["pred"]["away"], act["home"], act["away"])
    v = game.get("vegas")
    if v:
        vm = v["home"] - v["away"]
        am = act["home"] - act["away"]
        ours = abs(game["pred"]["home_raw"] - game["pred"]["away_raw"] - am)
        game["grade"]["beat_vegas_margin"] = ours < abs(vm - am)
        # pick against the spread: did our side cover?
        our_margin = game["pred"]["home_raw"] - game["pred"]["away_raw"]
        line = -v["spread_home"]
        if abs(am - line) > 1e-9 and abs(our_margin - line) > 0.25:
            game["grade"]["ats_correct"] = (our_margin > line) == (am > line)
    try:
        box = src.box_score(game["id"])
    except RuntimeError as e:
        print("  ! box score failed", game["id"], e)
        return
    by_name = {(v["name"].lower(), v["team"]): v for v in box.values()}
    for side in ("home", "away"):
        team = game[side]
        for p in game["players"][side]:
            if p.get("out") or not p["proj"]:
                continue
            a = box.get(str(p.get("espn_id"))) if p.get("espn_id") else None
            if a is None:
                a = by_name.get((p["name"].lower(), team))
            g = grade_player(p["proj"], a)
            p["grade"] = g
            if a:
                p["actual"] = {k: a.get(k, 0.0) for k in p["proj"]}
    game["graded_at"] = now_iso()


def confidence_score(g):
    """How comfortable we are with the pick: win probability of the side we picked,
    docked when the Vegas line and our team model disagree on the winner."""
    p = g["pred"]
    wp = p["win_prob_home"]
    score = max(wp, 1 - wp)
    v, m = g.get("vegas"), g.get("model") or {}
    if v and m.get("home") is not None:
        vegas_home = v["home"] > v["away"]
        model_home = m["home"] > m["away"]
        if vegas_home != model_home:
            score -= 0.04
    return score


def assign_confidence(games):
    """Grade each game's pick A-F, most probable to least, across the week.
    A pick keeps its grade once the game has kicked off."""
    picks = [g for g in games if g.get("pred")]
    if not picks:
        return
    ranked = sorted(picks, key=confidence_score, reverse=True)
    n = len(ranked)
    for i, g in enumerate(ranked):
        if g.get("locked") and g.get("confidence"):
            continue
        g["confidence"] = {"grade": "ABCDF"[min(4, i * 5 // n)], "rank": i + 1, "of": n,
                           "score": round(confidence_score(g), 3),
                           "pick_prob": round(max(g["pred"]["win_prob_home"], 1 - g["pred"]["win_prob_home"]), 3)}


def process_week(world, season, stype, week, backtest=False, allow_predict=True):
    print(f"\n=== {season} {week_label(stype, week)} {'(backtest)' if backtest else ''}")
    games = src.parse_events(src.scoreboard(season, week, stype))
    path = wfile(season, stype, week)
    existing = json.loads(path.read_text()) if path.exists() else {"games": []}
    ex_by_id = {g["id"]: g for g in existing.get("games", [])}
    model_week = week if stype == 2 else 19
    ctx = build_context(world.stats, world.results, season, model_week)
    teams = {g["home"] for g in games} | {g["away"] for g in games}
    rosters = None
    out_games = []
    for g in games:
        ex = ex_by_id.get(g["id"], {})
        rec = dict(ex)
        live = {k: g[k] for k in ("id", "date", "name", "short", "status", "state", "completed", "home", "away",
                                  "home_name", "away_name", "home_logo", "away_logo", "home_record", "away_record",
                                  "venue", "indoor", "neutral", "temp", "weather", "broadcast")}
        rec.update(live)
        has_pred = bool(ex.get("pred"))
        locked = ex.get("locked", False) or (has_pred and g["state"] != "pre")
        if allow_predict and not locked and (g["state"] == "pre" or not has_pred):
            if rosters is None:
                rosters = build_rosters(world, season, week, teams, backtest)
            rec.update(predict_game(world, ctx, g, rosters, backtest))
            rec["backtest"] = backtest
            rec["late"] = (g["state"] != "pre") and not backtest
            rec.pop("grade", None)
        if has_pred or rec.get("pred"):
            rec["locked"] = locked or g["state"] != "pre"
        if g["completed"]:
            rec["actual"] = {"home": g["home_score"], "away": g["away_score"]}
            if not rec.get("grade") and rec.get("pred"):
                grade_completed(rec)
        elif g["state"] == "in":
            rec["live_score"] = {"home": g["home_score"], "away": g["away_score"]}
        out_games.append(rec)
        p = rec.get("pred") or {}
        print(f"  {g['away']:>3} @ {g['home']:<3}  pred {p.get('away')}-{p.get('home')}  "
              f"{'final ' + str(g['away_score']) + '-' + str(g['home_score']) if g['completed'] else g['state']}"
              f"  {rec.get('grade', {}).get('grade', '')}")
    assign_confidence(out_games)
    data = {"season": season, "seasontype": stype, "week": week, "label": week_label(stype, week),
            "updated": now_iso(), "backtest": backtest, "games": sorted(out_games, key=lambda x: x["date"])}
    path.write_text(json.dumps(data, separators=(",", ":")))
    return data


# ---------------------------------------------------------------- aggregates
def all_weeks():
    out = []
    for p in sorted(WEEKS.glob("*.json")):
        out.append(json.loads(p.read_text()))
    return out


def build_accuracy(weeks):
    rows = []
    run = {"official": {"g": 0, "w": 0, "sc": 0.0, "h": 0, "n": 0, "pg": 0},
           "all": {"g": 0, "w": 0, "sc": 0.0, "h": 0, "n": 0, "pg": 0}}
    by_stat = {}
    by_pos = {}
    by_conf = {}
    for wk in weeks:
        r = {"key": f"{wk['season']}-{wk['seasontype']}-{wk['week']:02d}", "label": wk["label"], "season": wk["season"],
             "games": 0, "winners": 0, "game_score": 0.0, "beat_vegas": 0, "ats": 0, "ats_n": 0,
             "stat_hits": 0, "stats": 0, "players": 0, "player_score": 0.0, "backtest": False, "late": 0}
        for g in wk["games"]:
            gr = g.get("grade")
            if not gr:
                continue
            official = not g.get("backtest") and not g.get("late")
            r["backtest"] |= bool(g.get("backtest"))
            r["late"] += bool(g.get("late"))
            r["games"] += 1
            r["winners"] += bool(gr.get("winner_correct"))
            r["game_score"] += gr["score"]
            r["beat_vegas"] += bool(gr.get("beat_vegas_margin"))
            cg = (g.get("confidence") or {}).get("grade")
            if cg and gr.get("winner_correct") is not None:
                bc = by_conf.setdefault(cg, {"g": 0, "w": 0, "og": 0, "ow": 0})
                bc["g"] += 1; bc["w"] += bool(gr["winner_correct"])
                if official:
                    bc["og"] += 1; bc["ow"] += bool(gr["winner_correct"])
            if "ats_correct" in gr:
                r["ats_n"] += 1
                r["ats"] += bool(gr["ats_correct"])
            gh = gn = gp = 0
            for side in ("home", "away"):
                for p in g["players"][side]:
                    pg = p.get("grade")
                    if not pg or pg.get("dnp"):
                        continue
                    gh += pg["hits"]; gn += pg["n"]; gp += 1
                    r["player_score"] += pg["score"]
                    pos = p["pos"] if p["pos"] in ("QB", "RB", "WR", "TE", "K") else "DEF"
                    bp = by_pos.setdefault(pos, {"h": 0, "n": 0})
                    bp["h"] += pg["hits"]; bp["n"] += pg["n"]
                    for k, s in pg["stats"].items():
                        b = by_stat.setdefault(k, {"h": 0, "n": 0})
                        b["h"] += s["hit"]; b["n"] += 1
            r["stat_hits"] += gh; r["stats"] += gn; r["players"] += gp
            for bucket in (["all", "official"] if official else ["all"]):
                t = run[bucket]
                t["g"] += 1; t["w"] += bool(gr.get("winner_correct")); t["sc"] += gr["score"]
                t["h"] += gh; t["n"] += gn; t["pg"] += gp
        if r["games"] == 0:
            continue
        r["winner_pct"] = round(100 * r["winners"] / r["games"], 1)
        r["avg_game_score"] = round(r["game_score"] / r["games"], 1)
        r["stat_hit_pct"] = round(100 * r["stat_hits"] / max(r["stats"], 1), 1)
        r["avg_player_score"] = round(r["player_score"] / max(r["players"], 1), 1)
        for b in ("all", "official"):
            t = run[b]
            r[f"run_{b}"] = {"games": t["g"], "winner_pct": round(100 * t["w"] / max(t["g"], 1), 1),
                             "stat_hit_pct": round(100 * t["h"] / max(t["n"], 1), 1),
                             "stat_hits": t["h"], "stats": t["n"], "avg_game_score": round(t["sc"] / max(t["g"], 1), 1)}
        rows.append(r)
    stat_rows = [{"stat": k, "label": LABELS.get(k, k), "hits": v["h"], "n": v["n"],
                  "pct": round(100 * v["h"] / max(v["n"], 1), 1)} for k, v in by_stat.items()]
    stat_rows.sort(key=lambda x: -x["n"])
    pos_rows = [{"pos": k, "hits": v["h"], "n": v["n"], "pct": round(100 * v["h"] / max(v["n"], 1), 1)}
                for k, v in sorted(by_pos.items())]
    conf_rows = [{"grade": k, "games": v["g"], "wins": v["w"], "pct": round(100 * v["w"] / max(v["g"], 1), 1),
                  "official_games": v["og"], "official_wins": v["ow"]} for k, v in sorted(by_conf.items())]
    return {"updated": now_iso(), "weeks": rows, "by_stat": stat_rows, "by_pos": pos_rows, "by_confidence": conf_rows,
            "totals": {b: rows[-1][f"run_{b}"] if rows else None for b in ("all", "official")}}


def player_records(weeks):
    rec = {}
    for wk in weeks:
        for g in wk["games"]:
            for side in ("home", "away"):
                opp = g["away"] if side == "home" else g["home"]
                for p in g["players"][side]:
                    pg = p.get("grade")
                    if not pg or pg.get("dnp"):
                        continue
                    r = rec.setdefault(p["pid"], {"games": 0, "hits": 0, "n": 0, "score": 0.0, "weeks": []})
                    r["games"] += 1; r["hits"] += pg["hits"]; r["n"] += pg["n"]; r["score"] += pg["score"]
                    r["weeks"].append({"label": wk["label"], "season": wk["season"], "opp": opp,
                                       "hits": pg["hits"], "n": pg["n"], "hit_pct": pg["hit_pct"],
                                       "grade": pg["grade"], "score": pg["score"],
                                       "backtest": bool(g.get("backtest") or g.get("late")),
                                       "stats": pg["stats"],
                                       "running_hit_pct": None})
    for r in rec.values():
        h = n = 0
        for w in r["weeks"]:
            h += w["hits"]; n += w["n"]
            w["running_hit_pct"] = round(100 * h / max(n, 1), 1)
        r["hit_pct"] = round(100 * r["hits"] / max(r["n"], 1), 1)
        r["avg_score"] = round(r["score"] / max(r["games"], 1), 1)
        del r["score"]
    return rec


LOG_COLS = {"cmp": "completions", "att": "attempts", "pass_yds": "passing_yards", "pass_td": "passing_tds",
            "int": "passing_interceptions", "rush_att": "carries", "rush_yds": "rushing_yards", "rush_td": "rushing_tds",
            "tgt": "targets", "rec": "receptions", "rec_yds": "receiving_yards", "rec_td": "receiving_tds",
            "tackles": "tackles", "sacks": "def_sacks", "def_int": "def_interceptions", "fg": "fg_made", "xp": "pat_made"}


def write_player_pages(world, weeks, records):
    st = world.stats
    seen = {}
    for wk in weeks:
        for g in wk["games"]:
            for side in ("home", "away"):
                team = g[side]
                opp = g["away"] if side == "home" else g["home"]
                for p in g["players"][side]:
                    seen[p["pid"]] = (p, team, opp, wk, g)
    byp = {pid: df for pid, df in st[st["player_id"].isin(list(seen))].groupby("player_id")}
    index = []
    for pid, (p, team, opp, wk, g) in seen.items():
        grp = stat_group(p["pos"])
        keys = STAT_KEYS[grp]
        df = byp.get(pid)
        log = []
        if df is not None:
            for r in df.sort_values(["season", "week"]).itertuples():
                d = r._asdict()
                log.append({"s": int(r.season), "w": int(r.week), "tm": r.team, "op": r.opponent_team,
                            **{k: (round(float(d[LOG_COLS[k]]), 1) if LOG_COLS[k] in d else 0) for k in keys}})
        cwr = world.cw.loc[pid] if pid in world.cw.index else None
        if isinstance(cwr, pd.DataFrame):
            cwr = cwr.iloc[0]
        bio = {}
        if cwr is not None:
            for k in ("college_name", "height", "weight", "birth_date", "years_of_experience", "draft_year",
                      "draft_round", "draft_pick", "jersey_number", "rookie_season"):
                v = cwr.get(k)
                if pd.notna(v):
                    bio[k] = v.item() if hasattr(v, "item") else v
        page = {"pid": pid, "name": p["name"], "pos": p["pos"], "group": grp, "team": team, "keys": keys,
                "labels": {k: LABELS.get(k, k) for k in keys}, "headshot": p.get("headshot"),
                "espn_id": p.get("espn_id"), "status": p.get("status"), "detail": p.get("detail"), "bio": bio,
                "next": {"opp": opp, "label": wk["label"], "season": wk["season"], "game_id": g["id"],
                         "proj": p.get("proj"), "home": side_is_home(g, team)},
                "log": log, "record": records.get(pid)}
        (PLAYERS / f"{pid}.json").write_text(json.dumps(page, separators=(",", ":"), default=str))
        index.append({"pid": pid, "name": p["name"], "pos": p["pos"], "team": team})
    (DATA / "players_index.json").write_text(json.dumps(sorted(index, key=lambda x: x["name"]), separators=(",", ":")))


def side_is_home(g, team):
    return g["home"] == team


def leaderboard(records, weeks, min_games=2):
    names = {}
    for wk in weeks:
        for g in wk["games"]:
            for side in ("home", "away"):
                for p in g["players"][side]:
                    names[p["pid"]] = (p["name"], p["pos"], g[side])
    rows = [{"pid": pid, "name": names[pid][0], "pos": names[pid][1], "team": names[pid][2], "games": r["games"],
             "hits": r["hits"], "n": r["n"], "hit_pct": r["hit_pct"], "avg_score": r["avg_score"]}
            for pid, r in records.items() if r["games"] >= min_games and r["n"] >= 3 * min_games and pid in names]
    rows.sort(key=lambda x: (-x["hit_pct"], -x["n"]))
    return rows


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", help="weeks of the current season to build as backtests, e.g. 1-4")
    ap.add_argument("--mode", default="manual", choices=["manual", "predict", "grade"],
                    help="predict = refresh upcoming predictions, grade = grade finished games only")
    args = ap.parse_args()
    allow_predict = args.mode in ("manual", "predict")

    sb = src.scoreboard()
    season = sb["season"]["year"]
    stype = sb["season"]["type"]
    week = sb["week"]["number"]
    print(f"ESPN says: season {season}, type {stype}, week {week}")
    if stype not in (2, 3):
        (DATA / "meta.json").write_text(json.dumps({"updated": now_iso(), "offseason": True, "season": season}))
        print("Not regular season / playoffs — nothing to predict.")
        return

    world = World(season, week if stype == 2 else 18)

    if args.backfill:
        a, b = (int(x) for x in args.backfill.split("-"))
        for w in range(a, b + 1):
            if stype == 3 or w < week:
                if not wfile(season, 2, w).exists():
                    process_week(world, season, 2, w, backtest=True)

    # previous week: finish grading anything left over (e.g. Monday night)
    prev = (season, stype, week - 1) if week > 1 else ((season, 2, 18) if stype == 3 else None)
    if prev and wfile(*prev).exists():
        d = json.loads(wfile(*prev).read_text())
        if any(g.get("pred") and not g.get("grade") for g in d["games"]):
            process_week(world, *prev, backtest=d.get("backtest", False), allow_predict=False)

    process_week(world, season, stype, week, allow_predict=allow_predict)

    # older week files written before confidence grades existed
    for pth in sorted(WEEKS.glob("*.json")):
        d = json.loads(pth.read_text())
        if any(g.get("pred") and not g.get("confidence") for g in d["games"]):
            assign_confidence(d["games"])
            pth.write_text(json.dumps(d, separators=(",", ":")))

    weeks = all_weeks()
    print("\n• aggregating accuracy + player pages")
    acc = build_accuracy(weeks)
    records = player_records(weeks)
    acc["leaders"] = leaderboard(records, weeks)
    (DATA / "accuracy.json").write_text(json.dumps(acc, separators=(",", ":")))
    write_player_pages(world, weeks, records)
    idx = [{"key": f"{w['season']}-{w['seasontype']}-{w['week']:02d}", "label": w["label"], "season": w["season"],
            "backtest": w.get("backtest", False)} for w in weeks]
    (DATA / "weeks.json").write_text(json.dumps(idx))
    prev_meta = json.loads((DATA / "meta.json").read_text()) if (DATA / "meta.json").exists() else {}
    (DATA / "meta.json").write_text(json.dumps({
        "last_predict": now_iso() if allow_predict else prev_meta.get("last_predict"),
        "last_grade": now_iso() if args.mode in ("manual", "grade") else prev_meta.get("last_grade"),
        "updated": now_iso(), "run": args.mode, "season": season, "seasontype": stype, "week": week,
        "current": f"{season}-{stype}-{week:02d}", "label": week_label(stype, week),
        "sources": ["ESPN (schedule, live scores, Vegas lines, weather, injuries, box scores)",
                    "nflverse (player game logs 2018-present, player IDs)",
                    "Sleeper (depth charts, injury designations)"],
    }))
    print("done.")


if __name__ == "__main__":
    main()
