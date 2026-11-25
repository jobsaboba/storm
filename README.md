# ⛈️ Storm

The **win-only** companion to [Sandstorm 1%](https://jobsaboba.github.io/sandstorm1/).
Same Sandstorm picks, same actual Sporting Life starting prices, but no
each-way and no place returns.

**Live app:** https://jobsaboba.github.io/storm/

## The model

- Bank starts at **£10.00**.
- Each horse carries **1% of the bank at the start of its race**, rounded
  to the penny.
- A **win** pays the SP.
- **Every other finish loses the stake** — second and third get nothing.
- Every Sandstorm qualifier is eligible. The 8+ runner rule was an each-way
  requirement, so it does not apply here.
- Non-runners, withdrawn and abandoned/void races are no bets.
- Dead heats for first split the stake correctly.
- If the bank falls below **50p**, 1% rounds below 1p and the book stops
  betting.

Picks come from Sandstorm's stored `drafts-28` selection sheets. Settlement
uses the actual starting price / final odds shown on Sporting Life's result
pages, never the morning pick price.

## 2026 season (to 25 November)

| | |
|---|---:|
| Bets | 1,496 |
| Wins | 171 |
| Losses | 1,325 |
| Place finishes (unpaid) | 438 |
| Staked | £81.82 |
| P/L | **−£9.51** |
| ROI | **−11.62%** |
| Peak bank | £14.27 |
| Low bank | **£0.49** |
| Max drawdown | £13.78 |
| Bank exhausted | **19 September 2026** |

The bank busts in September. Once it is below 50p, a 1% stake rounds below
1p, so no further bets are possible. The rest of the season is flat at 49p.

### Why it busts

The each-way book's place returns were doing the heavy lifting. On the same
stakes as Sandstorm 1%, removing the place returns turns the season into:

| | Sandstorm 1% (each-way) | Storm (wins only, same stakes) |
|---|---:|---:|
| Bets | 1,687 | 1,687 |
| Wins | 165 | 165 |
| Place finishes | 395 paid | 395 unpaid |
| Staked | £6,602.65 | £6,602.65 |
| Win returns | — | £3,231.34 |
| P/L | **+£1,087.51** | **−£3,371.31** |
| ROI | **+16.47%** | **−51.06%** |

A full-season level-stakes version (10p per pick, all qualifiers) is also
negative: 1,926 bets, 201 wins, £192.60 staked, **−£46.01 P/L, −23.89% ROI**.

## Commands

```bash
cd ~/projects/storm

python3 pipeline/storm.py --rebuild           # full season from the result cache
python3 pipeline/storm.py --settle --publish  # settle open races + push
python3 pipeline/storm.py --publish           # morning: publish today's card
python3 pipeline/storm.py --summary           # print the season
```

`--publish` commits and pushes only `data/`; the app pulls it on ⟳.
`--no-push` commits locally without pushing.

The compact result-page cache is shared with Sandstorm 1% at
`~/projects/sandstorm1/pipeline/cache` so both books settle from exactly the
same Sporting Life pages. Override it with `STORM_CACHE` if needed.

## Automation

- `~/.local/bin/sandstorm-morning.sh` at 05:00 publishes the new day.
- `~/.local/bin/results-refresh.sh` every 30 minutes from 13:00 settles races.

Both scripts run Sandstorm 1% first, then Storm.

## Data

- Picks: Sandstorm v5, stored `drafts-28` sheets.
- Results/SP: [sportinglife.com](https://www.sportinglife.com) racecards and
  results.
- This is a backtest, not a guarantee.
