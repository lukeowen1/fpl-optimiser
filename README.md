# FPL Team Picker

A personal tool that:
1. Pulls live data from the official Fantasy Premier League API
2. Optimises your squad (budget-constrained, maximising expected points) using integer programming
3. Picks the best valid starting XI + captain from that squad
4. Emails you a reminder before each gameweek deadline

## Why a real backend

The FPL API blocks direct calls from browser JavaScript (CORS), so this
can't be a single static webpage — it needs a Python process making the
requests. That's what `app.py` (Streamlit) does.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip3 install -r requirements.txt
```

## Running the dashboard

```bash
streamlit run app.py
```

Opens at `http://localhost:8501`.

**To get transfer suggestions for your own team**, enter your FPL team ID in
the sidebar (the number in your Points page URL:
`fantasy.premierleague.com/entry/<ID>/event/...`). The app loads your squad
and bank, works out your budget from bank + selling prices, and suggests the
best transfers given your free transfers (anything beyond those costs -4
pts). Choose Wildcard / Free Hit to allow unlimited free changes, or cap the
number of transfers with the slider.

Leave the ID blank to pick a squad from scratch with a manual budget. Either
way you can lock in or exclude specific players.

## Deadline reminders

`reminder.py` sends one reminder per gameweek, `REMINDER_HOURS_BEFORE`
hours (default 24) before the deadline, with suggested transfers and a
captain for `FPL_TEAM_ID`. Configure channels in `.env`:

- **Phone push (ntfy):** install the ntfy app and subscribe to the topic in
  `NTFY_TOPIC`. Treat the topic name like a password.
- **Email (optional):** fill in `SMTP_USER`, `SMTP_PASSWORD` (for Gmail, an
  app password from https://myaccount.google.com/apppasswords) and `TO_EMAIL`.

Send a test now with `venv/bin/python3 reminder.py --test`.

It's scheduled with launchd (`~/Library/LaunchAgents/com.fpl-optimiser.reminder.plist`),
which checks hourly and catches up after sleep. Output goes to `reminder.log`.

```bash
# stop / start the schedule
launchctl bootout gui/$(id -u)/com.fpl-optimiser.reminder
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fpl-optimiser.reminder.plist
```

The project must stay outside `~/Downloads`, `~/Desktop` and `~/Documents`.
macOS blocks background jobs from reading those folders.

## How the optimiser works

- **Squad selection** (`optimiser.optimize_squad`): a linear program (via
  PuLP/CBC) that picks 15 players maximising the chosen metric (expected
  points next GW, form, or season total), subject to: total cost ≤ budget,
  exactly 2 GK / 5 DEF / 5 MID / 3 FWD, and no more than 3 players from any
  one real team. This finds the mathematically optimal squad for the
  objective — not a heuristic guess.
- **Starting XI + captain** (`optimiser.pick_starting_xi`): tries every
  valid formation (e.g. 3-4-3, 4-4-2, ...) and solves for the best XI +
  captain in each, keeping the best overall. Captain's points are doubled
  in the objective, matching real FPL scoring.

## Files

| File | Purpose |
|---|---|
| `fpl_api.py` | Thin client for the public FPL API |
| `optimiser.py` | Squad + starting XI optimisation logic |
| `app.py` | Streamlit dashboard |
| `reminder.py` | Email reminder, run on a schedule |
| `test_optimiser.py` | Sanity-check the optimiser against synthetic data |

## Notes / caveats

- `ep_next` is FPL's own "expected points next gameweek" projection — decent
  but not gospel. Swap the objective to `form` or `total_points` in the
  sidebar to compare.
- The squad optimiser scores the best starting XI (captain doubled); bench
  players count at 10%, so it won't take a hit just to upgrade the bench.
- Your squad is read from the last deadline. Transfers you've already made
  for the upcoming gameweek aren't visible through the public API, so load
  your team before making any.
- Free transfers and purchase prices aren't public either. Enter your free
  transfers yourself (check the Transfers page). Selling prices are
  reconstructed from your transfer history, so they may be £0.1m out in
  edge cases.
- Suggestions only look one gameweek ahead (`ep_next`). That's a short
  horizon to justify a -4 hit, so treat suggested hits with some caution.
- Injured/suspended players are excluded by default based on FPL's `status`
  flag; toggle this off in the sidebar if you want to see them anyway.