"""Prediction model: team scores + per-player stat lines.

Game score = blend of the Vegas-implied score (when a line exists) and an
opponent-adjusted team rating model (ridge regression on points scored).
Player stats = projected team volume x player's usage share x player's
efficiency (regressed toward league norms), adjusted for the opposing
defense vs. that position, game script, injuries (with usage redistributed
to healthy teammates), and the player's history against this opponent.
Touchdowns are scaled so player TDs add up to the team's projected points.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# nflverse -> ESPN team codes
TEAM_FIX = {"LA": "LAR", "WAS": "WSH", "OAK": "LV", "SD": "LAC", "STL": "LAR"}
HFA = 1.6           # home-field advantage in points
VEGAS_WEIGHT = 0.70
MARGIN_SD = 13.5

SKILL = ("QB", "RB", "WR", "TE")
DEF_GROUPS = ("LB", "DB", "DL")


def fix_team(t):
    return TEAM_FIX.get(t, t)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def season_weight(season, cur):
    d = cur - season
    return {0: 1.0, 1: 0.45, 2: 0.2}.get(d, 0.08)


def prep_stats(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["team"] = df["team"].map(fix_team)
    df["opponent_team"] = df["opponent_team"].map(fix_team)
    for c in ["attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
              "carries", "rushing_yards", "rushing_tds", "targets", "receptions", "receiving_yards",
              "receiving_tds", "def_tackles_solo", "def_tackle_assists", "def_sacks", "def_interceptions",
              "fg_made", "fg_att", "pat_made", "pat_att", "fantasy_points_ppr"]:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        else:
            df[c] = 0.0
    df["tackles"] = df["def_tackles_solo"] + df["def_tackle_assists"]
    df["pos"] = df["position"].fillna(df["position_group"])
    df["grp"] = df["position_group"].fillna(df["pos"])
    return df


# ------------------------------------------------------------------ team model
def team_ratings(results: pd.DataFrame, cur_season: int, ridge=3.0):
    """Ridge regression: pts = lg + HFA*home + off[team] + def[opp]."""
    teams = sorted(set(results["home"]) | set(results["away"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    rows, y, w = [], [], []
    for r in results.itertuples():
        sw = season_weight(r.season, cur_season)
        for team, opp, pts, home in ((r.home, r.away, r.home_score, 1), (r.away, r.home, r.away_score, 0)):
            x = np.zeros(2 * n + 1)
            x[idx[team]] = 1
            x[n + idx[opp]] = 1
            x[2 * n] = home - 0.5
            rows.append(x); y.append(pts); w.append(sw)
    X, y, w = np.array(rows), np.array(y, float), np.array(w)
    lg = float(np.average(y, weights=w))
    W = np.sqrt(w)[:, None]
    A = X * W
    b = (y - lg) * W[:, 0]
    reg = np.eye(2 * n + 1) * ridge
    reg[-1, -1] = 0.01
    beta = np.linalg.solve(A.T @ A + reg, A.T @ b)
    off = {t: float(beta[idx[t]]) for t in teams}
    dfn = {t: float(beta[n + idx[t]]) for t in teams}
    return {"lg": lg, "off": off, "def": dfn, "hfa": float(beta[-1])}


def model_score(ratings, home, away, neutral=False):
    lg, off, dfn = ratings["lg"], ratings["off"], ratings["def"]
    h = lg + off.get(home, 0) + dfn.get(away, 0) + (0 if neutral else HFA / 2)
    a = lg + off.get(away, 0) + dfn.get(home, 0) - (0 if neutral else HFA / 2)
    return h, a


def round_scores(h, a):
    """Round to realistic integer scores; never predict a tie."""
    H, A = int(round(h)), int(round(a))
    if H == A:
        if h >= a:
            H += 1
        else:
            A += 1
    return H, A


# ------------------------------------------------------------------ context
@dataclass
class Context:
    season: int
    week: int
    hist: pd.DataFrame                    # all player-weeks strictly before the cutoff
    ratings: dict
    team_games: pd.DataFrame = None       # per team-game volume
    by_pid: dict = field(default_factory=dict)
    tg_index: dict = field(default_factory=dict)
    def_factor: dict = field(default_factory=dict)
    league: dict = field(default_factory=dict)


def build_context(stats: pd.DataFrame, results: pd.DataFrame, season: int, week: int) -> Context:
    hist = stats[(stats["season"] < season) | ((stats["season"] == season) & (stats["week"] < week))].copy()
    hist = hist[hist["season"] >= season - 3]
    res = results[(results["season"] < season) | ((results["season"] == season) & (results["week"] < week))]
    res = res[res["season"] >= season - 1]
    ratings = team_ratings(res, season)
    ctx = Context(season, week, hist, ratings)
    ctx.by_pid = {pid: g for pid, g in hist.groupby("player_id")}

    # team volume per game
    tg = hist.groupby(["season", "week", "game_id", "team", "opponent_team"]).agg(
        pass_att=("attempts", "sum"), rush_att=("carries", "sum"), targets=("targets", "sum"),
        pass_yds=("passing_yards", "sum"), pass_td=("passing_tds", "sum"), rush_td=("rushing_tds", "sum"),
        fg=("fg_made", "sum"),
    ).reset_index()
    tg["plays"] = tg["pass_att"] + tg["rush_att"]
    tg["w"] = tg["season"].map(lambda s: season_weight(s, season))
    ctx.team_games = tg
    ctx.tg_index = {(r.game_id, r.team): (r.targets, r.rush_att, r.pass_att) for r in tg.itertuples()}

    # league baselines
    cur = hist[hist["season"] >= season - 1]
    def rate(num, den, d):
        sub = cur[cur[den] > 0]
        return float(sub[num].sum() / max(sub[den].sum(), 1)) if len(sub) else d
    ctx.league = {
        "cmp_pct": rate("completions", "attempts", 0.645), "ypa": rate("passing_yards", "attempts", 6.9),
        "ptd_rate": rate("passing_tds", "attempts", 0.043), "int_rate": rate("passing_interceptions", "attempts", 0.023),
        "ypc": rate("rushing_yards", "carries", 4.3), "rtd_rate": rate("rushing_tds", "carries", 0.03),
        "catch": {}, "ypt": {}, "rectd": {},
        "plays": float(np.average(tg["plays"], weights=tg["w"])) if len(tg) else 62.0,
        "pass_att": float(np.average(tg["pass_att"], weights=tg["w"])) if len(tg) else 33.0,
        "rush_att": float(np.average(tg["rush_att"], weights=tg["w"])) if len(tg) else 26.0,
        "pass_td_share": float(tg["pass_td"].sum() / max((tg["pass_td"] + tg["rush_td"]).sum(), 1)) if len(tg) else 0.62,
    }
    for g in ("RB", "WR", "TE"):
        sub = cur[(cur["pos"] == g) & (cur["targets"] > 0)]
        t = max(sub["targets"].sum(), 1)
        ctx.league["catch"][g] = float(sub["receptions"].sum() / t) if len(sub) else 0.65
        ctx.league["ypt"][g] = float(sub["receiving_yards"].sum() / t) if len(sub) else 7.5
        ctx.league["rectd"][g] = float(sub["receiving_tds"].sum() / t) if len(sub) else 0.045

    # defense vs position: stats allowed per game, opponent-perspective, relative to league
    allowed = hist[hist["season"] >= season - 1].copy()
    allowed["w"] = allowed["season"].map(lambda s: season_weight(s, season))
    keys = {
        "QB": ["passing_yards", "passing_tds", "passing_interceptions", "rushing_yards"],
        "RB": ["rushing_yards", "rushing_tds", "receiving_yards", "receptions"],
        "WR": ["receiving_yards", "receptions", "receiving_tds"],
        "TE": ["receiving_yards", "receptions", "receiving_tds"],
    }
    for g, cols in keys.items():
        sub = allowed[allowed["pos"] == g]
        per = sub.groupby(["season", "game_id", "opponent_team"])[cols].sum().reset_index()
        per["w"] = per["season"].map(lambda s: season_weight(s, season))
        lgm = {c: np.average(per[c], weights=per["w"]) if len(per) else 1 for c in cols}
        for team, grp in per.groupby("opponent_team"):
            n = grp["w"].sum()
            for c in cols:
                raw = np.average(grp[c], weights=grp["w"]) / max(lgm[c], 1e-6)
                shrink = n / (n + 5.0)
                ctx.def_factor[(team, g, c)] = float(1 + (raw - 1) * shrink)
    # opponent offensive volume / sacks allowed (for defender projections)
    for team, grp in tg[tg["season"] >= season - 1].groupby("team"):
        n = grp["w"].sum()
        sh = n / (n + 4.0)
        ctx.def_factor[(team, "OFF", "plays")] = float(1 + (np.average(grp["plays"], weights=grp["w"]) / ctx.league["plays"] - 1) * sh)
    return ctx


def dfac(ctx, opp, g, c):
    return ctx.def_factor.get((opp, g, c), 1.0)


# ------------------------------------------------------------------ helpers
def team_volume(ctx: Context, team: str):
    tg = ctx.team_games[(ctx.team_games["team"] == team)]
    tg = tg[tg["season"] >= ctx.season - 1]
    lg = ctx.league
    if len(tg) == 0:
        return {"pass_att": lg["pass_att"], "rush_att": lg["rush_att"], "targets": lg["pass_att"] * 0.95,
                "pass_td_share": lg["pass_td_share"], "fg": 1.7}
    w = tg["w"].values
    k = 3.0  # shrink toward league with 3 phantom games
    def sh(col, prior):
        return float((np.sum(tg[col].values * w) + prior * k) / (w.sum() + k))
    tds = (tg["pass_td"] + tg["rush_td"]).values
    ptd_share = float((np.sum(tg["pass_td"].values * w) + lg["pass_td_share"] * 5) / (np.sum(tds * w) + 5 + 1e-9))
    return {"pass_att": sh("pass_att", lg["pass_att"]), "rush_att": sh("rush_att", lg["rush_att"]),
            "targets": sh("targets", lg["pass_att"] * 0.95), "pass_td_share": min(max(ptd_share, 0.4), 0.8),
            "fg": sh("fg", 1.7)}


def player_usage(ctx: Context, pid: str, team: str):
    """Weighted target / carry / pass-attempt share in recent games with this team."""
    h = ctx.by_pid.get(pid)
    if h is None:
        return None
    rows = h[(h["team"] == team) & (h["season"] >= ctx.season - 1)]
    if rows.empty:
        return None
    out = {"tgt": [], "car": [], "att": [], "w": []}
    rows = rows.sort_values(["season", "week"])
    n = len(rows)
    for i, r in enumerate(rows.itertuples()):
        t = ctx.tg_index.get((r.game_id, team))
        if t is None:
            continue
        t = {"targets": t[0], "rush_att": t[1], "pass_att": t[2]}
        rec = 1.0 if r.season == ctx.season else 0.35
        rec *= 1.0 + 0.15 * max(0, i - (n - 4))  # last 3-4 games weigh a bit more
        out["tgt"].append(r.targets / max(t["targets"], 1))
        out["car"].append(r.carries / max(t["rush_att"], 1))
        out["att"].append(r.attempts / max(t["pass_att"], 1))
        out["w"].append(rec)
    if not out["w"]:
        return None
    w = np.array(out["w"])
    return {k: float(np.average(out[k], weights=w)) for k in ("tgt", "car", "att")} | {"games": len(w)}


def career_rates(ctx: Context, pid: str):
    h = ctx.by_pid.get(pid)
    if h is None or h.empty:
        return None
    w = h["season"].map(lambda s: season_weight(s, ctx.season)).values
    s = lambda c: float(np.sum(h[c].values * w))  # noqa: E731
    return {c: s(c) for c in ["attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
                               "carries", "rushing_yards", "rushing_tds", "targets", "receptions",
                               "receiving_yards", "receiving_tds", "tackles", "def_sacks", "def_interceptions",
                               "fg_made", "pat_made"]} | {"games_w": float(w.sum())}


def shrink(num, den, prior, k):
    return (num + prior * k) / (den + k)


_PID_CACHE = {}


def vs_opponent(stats_all: pd.DataFrame, pid: str, opp: str, before_season: int, before_week: int):
    key = id(stats_all)
    if key not in _PID_CACHE:
        _PID_CACHE.clear()
        _PID_CACHE[key] = {p: g for p, g in stats_all.groupby("player_id")}
    h = _PID_CACHE[key].get(pid)
    if h is None:
        return stats_all.iloc[0:0]
    h = h[h["opponent_team"] == opp]
    h = h[(h["season"] < before_season) | ((h["season"] == before_season) & (h["week"] < before_week))]
    return h


# ------------------------------------------------------------------ projections
STAT_KEYS = {
    "QB": ["cmp", "att", "pass_yds", "pass_td", "int", "rush_att", "rush_yds", "rush_td"],
    "RB": ["rush_att", "rush_yds", "rush_td", "tgt", "rec", "rec_yds", "rec_td"],
    "WR": ["tgt", "rec", "rec_yds", "rec_td", "rush_yds"],
    "TE": ["tgt", "rec", "rec_yds", "rec_td"],
    "K": ["fg", "xp"],
    "DEF": ["tackles", "sacks", "def_int"],
}

INJ_MULT = {"Questionable": 0.85, "Doubtful": 0.3}
OUT_STATUSES = {"Out", "Injured Reserve", "IR", "PUP", "Suspension", "Suspended", "Physically Unable to Perform",
                "Non Football Injury", "NFI", "Reserve/COVID-19", "Doubtful-Out"}


def project_team(ctx: Context, stats_all: pd.DataFrame, team: str, opp: str, roster: list[dict],
                 team_pts: float, opp_pts: float, game_week: int):
    """roster: list of {pid, name, pos, status, espn_id, depth} candidates for this team."""
    lg = ctx.league
    vol = team_volume(ctx, team)
    margin = team_pts - opp_pts
    pts_avg = ctx.ratings["lg"] + ctx.ratings["off"].get(team, 0)
    pace = 1 + 0.006 * (team_pts - pts_avg)
    P = max(22.0, (vol["pass_att"] - 0.30 * margin) * pace)
    R = max(16.0, (vol["rush_att"] + 0.40 * margin) * pace)
    T = P * vol["targets"] / max(vol["pass_att"], 1)

    players = []
    for c in roster:
        u = player_usage(ctx, c["pid"], team)
        cr = career_rates(ctx, c["pid"])
        if cr is None:
            continue
        mult = 0.0 if c["status"] in OUT_STATUSES else INJ_MULT.get(c["status"], 1.0)
        players.append(c | {"u": u or {"tgt": 0, "car": 0, "att": 0, "games": 0}, "cr": cr, "mult": mult})

    # ---- redistribute usage of OUT players to healthy teammates
    for key in ("tgt", "car"):
        out_share = sum(p["u"][key] for p in players if p["mult"] == 0)
        act = [p for p in players if p["mult"] > 0]
        act_share = sum(p["u"][key] for p in act)
        if act_share > 0 and out_share > 0:
            boost = 1 + 0.75 * out_share / act_share
            for p in act:
                p["u"][key] *= boost
        # never let projected shares exceed ~100%
        tot = sum(p["u"][key] * p["mult"] for p in act)
        if tot > 0.97:
            for p in act:
                p["u"][key] *= 0.97 / tot

    # ---- quarterback: starter = healthy QB with biggest recent pass share (depth chart breaks ties)
    qbs = [p for p in players if p["pos"] == "QB" and p["mult"] > 0]
    qbs.sort(key=lambda p: (p["u"]["att"] if p["u"]["games"] else 0, -(p.get("depth") or 9)), reverse=True)
    if qbs and qbs[0]["u"]["att"] < 0.3:
        dc = sorted(qbs, key=lambda p: p.get("depth") or 9)
        if dc and (dc[0].get("depth") or 9) == 1:
            qbs.insert(0, qbs.pop(qbs.index(dc[0])))
    starter = qbs[0] if qbs else None
    qb_out_primary = any(p["pos"] == "QB" and p["mult"] == 0 and p["u"]["att"] > 0.6 for p in players)

    results = []
    covered = {"tgt": 0.0, "car": 0.0}
    for p in players:
        pos = p["pos"]
        if pos not in SKILL or p["mult"] == 0 or (pos == "QB" and p is not starter):
            continue
        cr, u, m = p["cr"], p["u"], p["mult"]
        proj = {}
        opp_hist = vs_opponent(stats_all, p["pid"], opp, ctx.season, game_week)
        n_vs = len(opp_hist)
        vs_w = min(0.2, 0.05 * n_vs) if n_vs >= 2 else 0.0
        if pos in SKILL:
            # rushing
            car = R * u["car"] * m
            if pos == "QB":
                car = max(car, R * 0.08 * m) if u["car"] else car
            ypc = shrink(cr["rushing_yards"], cr["carries"], lg["ypc"], 60)
            ypc *= dfac(ctx, opp, "RB", "rushing_yards") ** (0.8 if pos == "RB" else 0.4)
            rtd = shrink(cr["rushing_tds"], cr["carries"], lg["rtd_rate"], 80)
            proj["rush_att"] = car
            proj["rush_yds"] = car * ypc
            proj["rush_td"] = car * rtd
            covered["car"] += u["car"] * m
        if pos in ("RB", "WR", "TE"):
            tg = T * u["tgt"] * m
            g = pos
            catch = shrink(cr["receptions"], cr["targets"], lg["catch"][g], 40)
            ypt = shrink(cr["receiving_yards"], cr["targets"], lg["ypt"][g], 50)
            ypt *= dfac(ctx, opp, g, "receiving_yards") / max(dfac(ctx, opp, g, "receptions"), 0.5) ** 0.5
            catch *= dfac(ctx, opp, g, "receptions") ** 0.35
            rtd = shrink(cr["receiving_tds"], cr["targets"], lg["rectd"][g], 60) * dfac(ctx, opp, g, "receiving_tds") ** 0.5
            proj["tgt"] = tg
            proj["rec"] = min(tg * catch, tg)
            proj["rec_yds"] = tg * ypt
            proj["rec_td"] = tg * rtd
            covered["tgt"] += u["tgt"] * m
        if pos == "QB":
            att = P * 0.97 * m
            cmp_ = shrink(cr["completions"], cr["attempts"], lg["cmp_pct"], 150)
            ypa = shrink(cr["passing_yards"], cr["attempts"], lg["ypa"], 200) * dfac(ctx, opp, "QB", "passing_yards")
            ptd = shrink(cr["passing_tds"], cr["attempts"], lg["ptd_rate"], 300)
            ir = shrink(cr["passing_interceptions"], cr["attempts"], lg["int_rate"], 400) * dfac(ctx, opp, "QB", "passing_interceptions") ** 0.6
            proj.update({"att": att, "cmp": att * cmp_, "pass_yds": att * ypa, "pass_td": att * ptd, "int": att * ir})
        if vs_w and pos in SKILL:
            ng = max(n_vs, 1)
            for k, col in (("rec_yds", "receiving_yards"), ("rush_yds", "rushing_yards"), ("pass_yds", "passing_yards")):
                if k in proj and proj[k] > 0:
                    proj[k] = (1 - vs_w) * proj[k] + vs_w * float(opp_hist[col].sum() / ng) * m
        results.append({"p": p, "proj": proj, "n_vs": n_vs})

    # ---- touchdown + yardage consistency with the team score
    fg_exp = vol["fg"] * math.sqrt(max(team_pts, 3) / max(pts_avg, 10))
    team_tds = max(0.3, (team_pts - 3 * fg_exp) / 6.95) * 0.93  # ~7% of TDs are D/ST
    pass_tds = team_tds * vol["pass_td_share"]
    rush_tds = team_tds - pass_tds
    for key, total, cov in (("rec_td", pass_tds, covered["tgt"]), ("rush_td", rush_tds, covered["car"] + 0.05)):
        init = sum(r["proj"].get(key, 0) for r in results)
        if init > 0:
            target = total * min(cov, 1.0)
            f = target / init
            for r in results:
                if key in r["proj"]:
                    r["proj"][key] *= f
    rec_yds_sum = sum(r["proj"].get("rec_yds", 0) for r in results)
    for r in results:
        if r["p"]["pos"] == "QB":
            pr = r["proj"]
            if covered["tgt"] > 0.3:
                pr["pass_yds"] = 0.5 * pr["pass_yds"] + 0.5 * rec_yds_sum / covered["tgt"]
            pr["pass_td"] = pass_tds * 0.97 * r["p"]["mult"]

    # ---- kicker
    ks = [p for p in players if p["pos"] == "K" and p["mult"] > 0]
    if ks:
        k = max(ks, key=lambda p: p["cr"]["games_w"])
        results.append({"p": k, "proj": {"fg": fg_exp, "xp": team_tds / 0.93 * 0.95}, "n_vs": 0})

    # ---- defenders
    opp_plays = dfac(ctx, opp, "OFF", "plays")
    defs = []
    for p in players:
        if p["pos"] not in ("LB", "DB", "DL", "CB", "S", "SAF", "DE", "DT", "OLB", "ILB", "MLB", "NT", "FS", "SS") or p["mult"] == 0:
            continue
        h = ctx.by_pid.get(p["pid"])
        if h is None:
            continue
        h = h[h["season"] >= ctx.season - 1]
        if h.empty:
            continue
        w = h["season"].map(lambda s: 1.0 if s == ctx.season else 0.4).values
        grp = p.get("grp") or "LB"
        prior = {"LB": 5.0, "DB": 4.0, "DL": 2.2}.get(grp, 3.5)
        tk = shrink(float(np.sum(h["tackles"].values * w)), float(w.sum()), prior, 2.5)
        sk = shrink(float(np.sum(h["def_sacks"].values * w)), float(w.sum()), {"DL": 0.3, "LB": 0.15}.get(grp, 0.03), 4)
        it = shrink(float(np.sum(h["def_interceptions"].values * w)), float(w.sum()), {"DB": 0.06}.get(grp, 0.01), 8)
        tk *= opp_plays
        defs.append({"p": p, "proj": {"tackles": tk * p["mult"], "sacks": sk * p["mult"], "def_int": it * p["mult"]}, "n_vs": 0})
    defs.sort(key=lambda r: r["proj"]["tackles"] + 6 * r["proj"]["sacks"], reverse=True)
    results.extend(defs[:7])

    # ---- tidy: drop negligible offensive players
    out = []
    for r in results:
        pr = r["proj"]
        pos = r["p"]["pos"]
        if pos in ("RB", "WR", "TE"):
            if pr.get("tgt", 0) + pr.get("rush_att", 0) < 1.5 and r["p"]["mult"] > 0:
                continue
        out.append(r)
    return out, {"qb_out": qb_out_primary, "pass_att": P, "rush_att": R}


def stat_group(pos):
    if pos in ("QB", "RB", "WR", "TE", "K"):
        return pos
    return "DEF"
