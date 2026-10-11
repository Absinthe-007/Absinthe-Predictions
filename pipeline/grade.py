"""Grading: compare locked predictions with final results.

Game grade (0-100 -> A..F): penalizes error in the margin and total, plus a
penalty for picking the wrong winner.

Player stat "hit": the actual number landed inside a tolerance band around the
projection (bands below). A player's hit rate is hits / stats graded. Player
grade (A..F) is based on the average size of the miss across their stats.
"""
from __future__ import annotations

# stat -> (absolute band, relative band). Hit if |actual - proj| <= max(abs, rel * proj)
BANDS = {
    "pass_yds": (30, 0.15), "att": (4, 0.12), "cmp": (3, 0.12), "pass_td": (0.75, 0), "int": (0.75, 0),
    "rush_att": (3, 0.2), "rush_yds": (15, 0.25), "rush_td": (0.75, 0),
    "tgt": (2, 0.25), "rec": (1.5, 0.2), "rec_yds": (15, 0.25), "rec_td": (0.75, 0),
    "fg": (1, 0), "xp": (1.5, 0),
    "tackles": (1.5, 0.2), "sacks": (0.75, 0), "def_int": (0.75, 0),
}
LABELS = {
    "pass_yds": "Pass Yds", "att": "Att", "cmp": "Cmp", "pass_td": "Pass TD", "int": "INT",
    "rush_att": "Carries", "rush_yds": "Rush Yds", "rush_td": "Rush TD", "tgt": "Targets", "rec": "Rec",
    "rec_yds": "Rec Yds", "rec_td": "Rec TD", "fg": "FG", "xp": "XP", "tackles": "Tackles",
    "sacks": "Sacks", "def_int": "INT (def)",
}


# Stats that count toward the headline hit %. Touchdowns, interceptions and sacks are
# still graded and shown, but they're near-automatic "hits" when the projection is
# small (most players score 0), so counting them would inflate the hit rate.
KEY_STATS = {"pass_yds", "cmp", "att", "rush_att", "rush_yds", "tgt", "rec", "rec_yds", "fg", "xp", "tackles"}
# ...and only when the projection is big enough to be a real call (a backup projected
# for 1 carry "hitting" isn't meaningful).
MIN_PROJ = {"rush_yds": 15, "rec_yds": 15, "rush_att": 4, "tgt": 2.5, "rec": 2, "tackles": 2.5}


def letter(score: float) -> str:
    if score >= 93: return "A+"
    if score >= 90: return "A"
    if score >= 87: return "A-"
    if score >= 83: return "B+"
    if score >= 80: return "B"
    if score >= 77: return "B-"
    if score >= 73: return "C+"
    if score >= 70: return "C"
    if score >= 67: return "C-"
    if score >= 60: return "D"
    return "F"


def grade_game(pred_home, pred_away, act_home, act_away):
    pm, am = pred_home - pred_away, act_home - act_away
    margin_err = abs(pm - am)
    total_err = abs((pred_home + pred_away) - (act_home + act_away))
    winner_ok = (pm > 0) == (am > 0) if am != 0 else None
    score = 100 - 1.4 * margin_err - 0.9 * total_err - (12 if winner_ok is False else 0)
    score = max(0.0, min(100.0, score))
    return {"score": round(score, 1), "grade": letter(score), "winner_correct": winner_ok,
            "margin_err": round(margin_err, 1), "total_err": round(total_err, 1)}


def stat_hit(stat, proj, actual):
    a, r = BANDS.get(stat, (1, 0.2))
    return abs(actual - proj) <= max(a, r * proj)


def grade_player(proj: dict, actual: dict | None):
    if actual is None:
        return {"dnp": True}
    stats, hits, n, errs = {}, 0, 0, []
    for k, p in proj.items():
        act = float(actual.get(k, 0.0))
        hit = stat_hit(k, p, act)
        a, r = BANDS.get(k, (1, 0.2))
        band = max(a, r * p)
        key = k in KEY_STATS and p >= MIN_PROJ.get(k, 0)
        if key:
            errs.append(min(abs(act - p) / band, 4.0))
            hits += hit
            n += 1
        stats[k] = {"proj": round(p, 1), "actual": act, "hit": bool(hit), "key": key}
    if n == 0:
        return {"dnp": True}
    avg = sum(errs) / n
    score = max(0.0, min(100.0, 100 - 18 * avg))
    return {"dnp": False, "stats": stats, "hits": hits, "n": n, "hit_pct": round(100 * hits / n, 1),
            "score": round(score, 1), "grade": letter(score)}
