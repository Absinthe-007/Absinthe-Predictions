"""Decide whether this scheduled run should go ahead, and in which mode.

Rules (all times Central):
  predict -> Thursday, Sunday and Monday only, 5 hours before that day's first kickoff
             (e.g. 2:15 PM for a 7:15 PM Thursday game, 7:00 AM for noon Sunday games).
  grade   -> 11:30 PM on Thursday, Sunday and Monday.

GitHub cron can't follow a game schedule, so the workflow wakes up every 15
minutes on those days and this script decides. It only uses the standard
library so it can run before anything is installed. Each slot runs once; the
slot id is saved in docs/data/runs.json.

Prints `run=predict`, `run=grade`, `run=manual` or `run=skip` for GitHub Actions.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs" / "data" / "runs.json"
CT = ZoneInfo("America/Chicago")
GAME_DAYS = {3: "Thursday", 6: "Sunday", 0: "Monday"}  # datetime.weekday()
LEAD = timedelta(hours=5)
GRADE_AT = (23, 30)
GRADE_WINDOW = timedelta(hours=2, minutes=30)  # GitHub can start scheduled runs late
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


def kickoffs():
    """Kickoff times (Central) for the current ESPN week."""
    with urllib.request.urlopen(ESPN, timeout=30) as r:
        sb = json.load(r)
    out = []
    for e in sb.get("events", []):
        try:
            out.append(datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(CT))
        except (KeyError, ValueError):
            pass
    return out


def decide(now, kicks):
    """Return (mode, slot_id) or (None, reason)."""
    # grading: 11:30 PM on a game day (window runs past midnight)
    for back in (0, 1):
        day = (now - timedelta(days=back)).date()
        if day.weekday() not in GAME_DAYS:
            continue
        start = datetime(day.year, day.month, day.day, *GRADE_AT, tzinfo=CT)
        if start <= now < start + GRADE_WINDOW:
            return "grade", f"{day}-grade"
    # predictions: 5 hours before today's first kickoff
    if now.weekday() in GAME_DAYS:
        today = [k for k in kicks if k.date() == now.date()]
        if not today:
            return None, f"no games today ({GAME_DAYS[now.weekday()]})"
        first = min(today)
        if first - LEAD <= now < first:
            return "predict", f"{now.date()}-predict"
        return None, f"predict window opens {first - LEAD:%a %I:%M %p} (first kickoff {first:%I:%M %p})"
    return None, "not a game day"


def main():
    now = datetime.now(CT)
    runs = json.loads(LOG.read_text()) if LOG.exists() else []
    if os.environ.get("FORCE", "").lower() in ("1", "true", "yes"):
        mode, sid = "manual", f"{now:%Y-%m-%dT%H:%M}-manual"
    else:
        try:
            kicks = kickoffs()
        except Exception as e:  # noqa: BLE001
            print(f"could not read ESPN schedule: {e}", file=sys.stderr)
            kicks = []
        mode, sid = decide(now, kicks)
        if mode is None:
            print(f"{now:%a %Y-%m-%d %H:%M %Z}: skip — {sid}", file=sys.stderr)
            print("run=skip")
            return
        if sid in runs:
            print(f"{sid} already done, skipping", file=sys.stderr)
            print("run=skip")
            return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps((runs + [sid])[-80:]))
    print(f"{now:%a %Y-%m-%d %H:%M %Z}: running {sid}", file=sys.stderr)
    print(f"run={mode}")


if __name__ == "__main__":
    main()
