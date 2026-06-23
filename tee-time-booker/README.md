# ⛳ tee-time-booker

A small, self-contained automation that logs into your golf club's member
portal and books a tee time the instant it's released — useful when desirable
times open at a fixed moment (e.g. **2 weeks out at 12:01 AM**) and get snapped
up fast.

This project is **completely standalone**. It shares no code with any other
project.

> **Use responsibly.** This logs in with *your own* membership credentials to
> book *your own* tee times. Check that automated booking is permitted by your
> club's terms of use before relying on it.

## How it works

1. You describe your club's portal once in `config.yaml` — the login URL, the
   tee-sheet URL, and the CSS selectors for the login form and tee-time slots.
2. Credentials are read from a `.env` file (never committed).
3. `schedule` computes the exact release instant from your play date
   (`play_date − days_ahead` at `release_time`, in your timezone, DST-aware),
   waits for it with sub-second precision, then races to grab the first
   acceptable time from your `preferred_times` list, retrying for a configurable
   window.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # one-time browser download

cp .env.example .env                 # add your username/password
cp config.example.yaml config.yaml   # add your club's URL + selectors
```

### Capture your portal's selectors

The selectors in `config.yaml` are placeholders — they must match *your* portal.
The easiest way to find them:

```bash
python book.py inspect
```

This opens a visible browser at your login page. Right-click each element
(username field, password field, login button, a tee-time slot, the book
button), choose **Inspect**, and copy a CSS selector into `config.yaml`. Set
`runtime.headless: false` while you test so you can watch the flow.

## Usage

```bash
# Test the login + slot detection without booking anything:
python book.py book --date 2026-07-07 --dry-run

# Book right now (times already open):
python book.py book --date 2026-07-07

# Wait for the 12:01 AM release two weeks out, then race to book:
python book.py schedule --date 2026-07-07
```

For an unattended race, run `schedule` under `cron`/`systemd`/Task Scheduler on
a machine that's awake at release time, or just start it before midnight — it
sleeps until the release instant on its own.

## Configuration reference

See `config.example.yaml` — every field is commented. Key sections:

- `club` — login + tee-sheet URLs and the date format for the URL.
- `release` — `days_ahead`, `release_time`, `timezone`, and retry behavior.
- `booking` — `date`, ordered `preferred_times`, `players`.
- `selectors` — the club-specific CSS selectors.
- `runtime` — headless on/off, screenshots, debug slow-mo.

## Security

- Credentials live only in `.env`, which is gitignored. Nothing secret is
  committed.
- `config.yaml` is also gitignored (it may hold a club-specific URL).
- On error the tool can save a screenshot to `screenshots/` (also gitignored)
  to help you fix selectors.

## Tests

```bash
pip install pytest
pytest -q
```

The tests cover config/credential handling and the release-time math
(including EST↔EDT), and don't require a browser.
