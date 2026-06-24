# Nightly auto-booking setup (Pennsauken CC / TeeItUp)

This machine is set up to book tee times automatically at the 12:01 AM release.

## What runs

- **`nightly.py`** — each night it computes the play date 14 days out, picks
  that weekday's times from `config.yaml` → `weekly_schedule`, waits for the
  12:01 AM Eastern release, then books `players` golfers at the first available
  preferred time. An empty list for a weekday = skip (e.g. Mondays).

  Current schedule: **6:30 AM Tue–Fri**, **7:00/7:10 AM Sat–Sun**, **2 golfers**,
  **Mondays skipped**. Edit `weekly_schedule` in `config.yaml` to change it.

- **launchd job** `~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist`
  fires at **23:58** nightly and runs `nightly.py` under `caffeinate -i` (so the
  Mac won't idle-sleep during the booking). `nightly.py` itself waits until 00:01.

- **Power schedule (you must set this once, needs admin):**
  ```bash
  sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00 sleep MTWRFSU 00:15:00
  ```
  Wakes the Mac at 11:55 PM and sleeps it at 12:15 AM, every day.
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

## Requirements / caveats

- **Stay logged in** (screen may be **locked**, but don't fully log out) — a
  LaunchAgent only runs in an active user session. Headless Chromium doesn't
  need the screen unlocked.
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
