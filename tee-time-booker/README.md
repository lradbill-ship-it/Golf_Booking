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
4. If none of those times is bookable, it takes the closest available time
   instead (see **Falling back to the closest time** below) rather than coming
   away with nothing.

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
- `booking` — `date`, ordered `preferred_times`, `players`, and `fallback`.
- `selectors` — the club-specific CSS selectors.
- `checkout` — success detection for cart-based portals (see below).
- `runtime` — headless on/off, screenshots, debug slow-mo.

### Falling back to the closest time

Tee sheets move with sunrise. A 6:30 AM slot that exists all June simply stops
being published in late August — and an exact-match-only booker would then book
*nothing*, on precisely the nights the early times shifted.

So when none of `preferred_times` is bookable, the booker takes the bookable
slot **closest** to what you asked for, with the earlier slot winning a tie.
In the sunrise case — everything before your time is gone or was never
published — "closest" is simply the earliest time on the sheet.

```yaml
booking:
  preferred_times: ["6:30 AM", "6:40 AM", "6:50 AM"]
  fallback:
    enabled: true
    max_minutes_earlier: 60    # window starts at 5:30 AM (6:30 − 60)
    max_minutes_later: null    # no cap on the late side — always book something
    after_seconds: 0           # consider it as soon as the sheet is up
    recheck_seconds: 3         # re-scan preferred times once before settling
```

`max_minutes_later: null` is the **always-book rule**, and the default:
whatever happens to the morning, the booker comes away with the nearest
available time, even if that is hours later — an afternoon round beats no
round. Set a number instead (`120`, say) if you would rather end the night
unbooked than play far off target; then nothing outside the window is booked
and a night with nothing in range ends empty. `0` means no slack at all on
that side, and `enabled: false` restores exact-match-only behavior.

"Nearest" is unaffected by the bounds: with 7:00 AM preferred and 8:15 AM,
11:30 AM and 2:00 PM open, it books 8:15 AM. No cap never means "grab the
first thing on the sheet".

The log and the confirmation say when a fallback was used, e.g.
`Booked 7:00 AM for 2 players (closest available — 30 min later than preferred)`.

### Watching the sheet drift

Each night's run also records the **earliest tee time the sheet published**,
bookable or not. That's the number that tells you how far sunrise has pushed
things:

```bash
python nightly.py --history      # "1st tee" column, plus first vs. most recent
```

When that column has moved past your preferred times for a few nights running,
move `preferred_times` (or `weekly_schedule`) deliberately rather than leaning
on the fallback every night.

### Single-click vs. cart-based portals

Some portals book in one confirm click; set `confirm_button` and
`confirmation_marker` and leave the cart selectors blank.

Others (e.g. **TeeItUp**, which powers many member courses) use a multi-step
cart checkout. When `add_to_cart_button` is set, the booker runs:

> book → choose golfers (`golfer_radio`, with `{players}` substituted) → add to
> cart → checkout (`cart_checkout_button`) → agree to terms (`terms_checkbox`) →
> complete the purchase (`complete_purchase_button`)

and treats the booking as successful once the URL leaves the checkout route
(`checkout.success_when_url_leaves`). Playwright auto-waits for the final
button to become enabled (it stays disabled until the terms box is checked).

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
