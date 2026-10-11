"""Decide whether this scheduled run should go ahead.

GitHub cron runs in UTC and doesn't know about daylight saving time, so the
workflow fires at both possible UTC times for each Central-time slot. This
script lets exactly one of them through per slot:

  night   -> 11:30 PM Central (window 11:15 PM - 2:00 AM, in case GitHub is late)
  morning ->  4:30 AM Central (window 4:15 AM - 7:00 AM)

Prints `run=<slot>` (or `run=skip`) for GitHub Actions and records the slot in
docs/data/runs.json so a duplicate firing is skipped.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs" / "data" / "runs.json"


def slot_for(now):
    mins = now.hour * 60 + now.minute
    if mins >= 23 * 60 + 15 or mins < 2 * 60:
        day = (now - timedelta(hours=3)).date()  # after midnight still counts as the previous night
        return "night", f"{day}-night"
    if 4 * 60 + 15 <= mins < 7 * 60:
        return "morning", f"{now.date()}-morning"
    return None, None


def main():
    force = os.environ.get("FORCE", "").lower() in ("1", "true", "yes")
    now = datetime.now(ZoneInfo("America/Chicago"))
    slot, sid = slot_for(now)
    runs = json.loads(LOG.read_text()) if LOG.exists() else []
    if force:
        slot, sid = "manual", f"{now:%Y-%m-%dT%H:%M}-manual"
    elif slot is None:
        print(f"{now:%Y-%m-%d %H:%M %Z}: outside both windows, skipping", file=sys.stderr)
        print("run=skip")
        return
    elif sid in runs:
        print(f"{sid} already ran, skipping duplicate DST firing", file=sys.stderr)
        print("run=skip")
        return
    runs = (runs + [sid])[-60:]
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(runs))
    print(f"{now:%Y-%m-%d %H:%M %Z}: running {sid}", file=sys.stderr)
    print(f"run={slot}")


if __name__ == "__main__":
    main()
