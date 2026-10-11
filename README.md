# Absinthe Predictions

Weekly NFL predictions (Thursday night, every Sunday game and Monday night): a predicted final score for every game and a projected stat line for every relevant player, graded after each game.

**Site:** `https://absinthe-007.github.io/Absinthe-Predictions/` (GitHub Pages, served from `/docs`)

## What it does

- **Predicted scores.** 70% Vegas-implied score, 30% opponent-adjusted team-rating model, with an adjustment when a starting QB is out. Also shows win probability, spread and total.
- **Player projections.** QB, RB, WR, TE, K and top defenders. Team volume × the player's usage share × his efficiency, adjusted for the opposing defense against his position, game script, injuries (an injured player's targets and carries go to healthy teammates) and his history against this opponent.
- **Clickable players.** Every game since 2018 against any opponent, season-by-season averages, the last 10 games, injury status and Absinthe's hit rate on that player week by week.
- **Grading.** Game grades (A+ to F) and player grades. A stat is a "hit" when it lands inside a tolerance band around the projection. The Accuracy page keeps a running weekly tally broken down by stat and position.
- **Locked predictions.** Each projection freezes at kickoff and can't be changed after the game starts.

## Schedule

`.github/workflows/update.yml` runs at **11:30 PM** and **4:30 AM Central** every day. GitHub cron uses UTC, so the workflow fires at both possible UTC times and `pipeline/gate.py` lets one through per slot, which keeps the times correct across daylight saving time.

## Data sources

| Source | Used for |
|---|---|
| ESPN public API | schedule, live scores, Vegas lines, weather, injury report, box scores for grading |
| nflverse | every player's weekly stats back to 2018, player ID crosswalk, bios |
| Sleeper | depth charts, injury designations |

## Run it yourself

```bash
pip install -r requirements.txt
python -m pipeline.build                 # predict + grade current week
python -m pipeline.build --backfill 1-4  # also build backtest weeks
cd docs && python -m http.server         # view the site at localhost:8000
```

You can also start a run manually from **Actions → Update predictions → Run workflow**.

*For entertainment. Not betting advice.*
