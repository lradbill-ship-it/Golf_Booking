# Nightly auto-booking setup (Pennsauken CC / TeeItUp)

This machine is set up to book tee times automatically at the 12:01 AM release.

## What runs

- **`nightly.py`** — each night it computes the play date 14 days out, picks
  that weekday's times from `config.yaml` → `weekly_schedule`, waits for the
  12:01 AM Eastern release, then books `players` golfers at the first available
  preferred time. An empty list for a weekday = skip (e.g. Mondays).

  Current schedule: **6:30 AM Tue–Fri**, **7:00/7:10 AM Sat–Sun**, **2 golfers**,
  **Mondays skipped**. Edit `weekly_schedule` in `config.yaml` to change it.

  **If none of that weekday's times is bookable** — which is what happens once
  sunrise pushes the sheet past 6:30 AM — it books the **closest available
  time** instead of nothing. The standing rule here is *always book*:
  `booking.fallback.max_minutes_later` is `null`, so it takes the nearest time
  however late it lands. "Nearest" is measured from that day's **target** — the
  first time in its `weekly_schedule` list, i.e. **6:30 AM Tue-Thu** and
  **7:00 AM Fri-Sun**. `python nightly.py --plan` prints the target and window
  it would use tonight. See README → *Falling back to the closest time*.

  **Watch the drift:** `python nightly.py --history` now has a **`1st tee`**
  column — the earliest time the sheet published each night. When that has moved
  past 6:30 AM for a few nights, edit `weekly_schedule` to match rather than
  relying on the fallback nightly.

- **launchd job** `~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist`
  fires at **23:58** nightly and runs `nightly.py` under `caffeinate -i` (so the
  Mac won't idle-sleep during the booking). `nightly.py` logs in ~60s before
  release and then **keeps polling from 12:01 until ~1:00 AM** (or until it
  books), to catch a release that lands after midnight. Polling is deliberately
  gentle (~15s jittered, images/fonts/analytics blocked, back-off on rate-limit
  blocks) to stay under Cloudflare's limiter. Tighten the window once the
  release pattern is known.

- **Power schedule (you must set this once, needs admin):**
  ```bash
  sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00 sleep MTWRFSU 01:15:00
  ```
  Wakes the Mac at 11:55 PM and sleeps it at 1:15 AM — the late sleep keeps it
  awake for the full polling window (`release.retry_window_seconds`, 59 min) plus
  margin. If you change that window, push the sleep time out to match.
  Verify with `pmset -g sched`. Clear with `sudo pmset repeat cancel`.

## Safety — one booking per run

Each run books **at most one** tee time:

- The booker stops the moment one booking succeeds.
- It submits a purchase at most once and **never retries after that point** — if
  it can't confirm the result, it stops and reports rather than risk a double.
- Before checkout it verifies the cart holds only the one item it just added
  (a leftover from an interrupted run makes it abort, not over-book).
- The nightly job fires once per night (no KeepAlive), and the dashboard never
  books — it only views/cancels reservations and arms/skips.

To book more than one day, that's what the weekly schedule is for: one booking
per night for each upcoming play date. (Covered by `test_booking_safety.py`.)

## Earlier-time watcher (optional, read-only)

`watch_earlier.py` re-checks dates where the nightly booker had to settle for a
late time (e.g. **Tuesdays at 9:40 AM** — PCC's Tuesday sheet is empty from the
first tee until 9:40 at release) and flags an **earlier** time on the dashboard's
*Earlier-time watch* card if one opens. It **never books or cancels** — moving
to the better time is yours to do (book it on the portal, then cancel the later
one from the dashboard).

- Only watches tee times the nightly booker booked (matched against
  `state/release_history.jsonl`), later than that day's target, on a scheduled,
  un-skipped day. `python watch_earlier.py --plan` lists every reservation and
  why it is or isn't watched.
- Refuses to run between **23:30 and 01:30** (`earlier_watch.quiet_start/end`)
  so it can never log in during the midnight race, and honours the dashboard's
  kill switch.
- History: `state/earlier_watch.jsonl`; `python watch_earlier.py --history`.
- The dashboard card warns if no check has run for over a day.

**Scheduling it** (not installed by default):

```bash
cp launchd/com.laneradbill.teebooker.watch.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.laneradbill.teebooker.watch.plist
tail -f logs/watch.log
# remove:
launchctl unload -w ~/Library/LaunchAgents/com.laneradbill.teebooker.watch.plist
```

It runs at 7:10, 11:10, 15:10 and 19:10; a time the Mac slept through runs on the
next wake.

## Requirements / caveats

- **Stay logged in** (screen may be **locked**, but don't fully log out) — a
  LaunchAgent only runs in an active user session. **The browser now runs
  VISIBLE (`runtime.headless: false`)**, because since 2026-09-23 the portal
  stalls a headless browser forever at the final purchase step. A visible window
  needs a real GUI login session; if a night ever fails to open one, that is the
  first thing to check.
- **Keep it plugged into AC** — scheduled wake is reliable on power; on battery
  it may not wake.
- **Prefer Sleep over Shutdown** at night. If the disk uses FileVault and the
  Mac is fully powered off, scheduled power-on stops at the pre-boot unlock
  screen and the job won't run. Sleeping avoids that.
- Credentials live in `.env`; the club config + selectors in `config.yaml`
  (both gitignored).

## Operate it

```bash
# See what it would do tonight (no browser, no booking):
.venv/bin/python nightly.py --plan

# Watch the logs (written here each night):
tail -f logs/nightly.log

# Pause / resume the nightly job:
launchctl unload -w ~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist
launchctl load   -w ~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist

# Manually book a specific date now (bypasses the wait), e.g. for testing:
.venv/bin/python nightly.py --date 2026-07-08 --no-wait
```

## After it runs

Confirm bookings on the portal under **Reservations**, or watch `logs/nightly.log`
for a `✅`/`❌` line. Each successful booking also saves a confirmation screenshot
under `screenshots/`.
