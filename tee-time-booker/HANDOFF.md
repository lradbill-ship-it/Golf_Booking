# Golf Booking App — Session Handoff

Handoff for the next working session. Read this first, then `AUTOMATION.md`,
`DASHBOARD.md`, and `README.md`. Live operational facts also live in the
assistant memory at
`~/.claude/projects/-Users-Lane-DDABBER-Golf-Booking/memory/` (loaded
automatically each session).

_Last updated: 2026-06-27._

---

## 1. What this is

A standalone Python automation that books tee times for **Pennsauken Country
Club (PCC)** on the **TeeItUp / Kenna** member portal, plus a phone dashboard to
watch/cancel reservations and control the automation.

- Repo: `github.com/lradbill-ship-it/Golf_Booking`, code under `tee-time-booker/`.
- Runs on the user's Mac (`big-ls-office-mac`). Python 3.9 venv at
  `tee-time-booker/.venv`, Playwright (Chromium), Flask.

## 2. Status — WORKING ✅ (as of 2026-06-27)

- **Nightly auto-booker: confirmed working.** On 2026-06-27 it auto-booked
  **Sat Jul 11, 7:00 AM, 2 players** (the first fully successful unattended run).
- **Dashboard: live** via launchd, reachable over Tailscale.
- **Cancel + per-player cancel: working** (fixed and validated).
- **64 tests pass** (`.venv/bin/python -m pytest -q`).

### The big lesson from the first successful night
PCC's nominal release is "12:01 AM" but the sheet **actually released ~12:14
AM** that night (0 cards from 12:01→12:14, then slots appeared and it booked
7:00 within ~6s). The first three nights failed because the old 90-second window
gave up ~12 minutes too early. The booker now polls **12:01–1:00 AM**. No
rate-limiting occurred over 30 gentle checks.

## 3. How it runs in production

Two launchd LaunchAgents (in `~/Library/LaunchAgents/`):

- **`com.laneradbill.teebooker.nightly.plist`** — fires nightly at **23:58**
  under `caffeinate -i`, runs `nightly.py`. Flow:
  1. Compute the play date 14 days out; pick that weekday's times from
     `config.yaml` → `weekly_schedule` (empty list = skip, e.g. Mondays).
  2. Wait until ~60s before release, **log in** (before the surge), then hold to
     12:01.
  3. **Poll** the tee sheet gently from 12:01 until it books or ~1:00 AM.
  4. Book exactly one slot; notify; save a confirmation screenshot.
- **`com.laneradbill.teebooker.dashboard.plist`** — keeps `dashboard.py` running
  (Flask, port 8787), RunAtLoad + KeepAlive.

**Power schedule (pmset, set by user, needs admin):**
`wakeorpoweron 23:55`, `sleep 01:00`. NOTE: docs recommend bumping sleep to
**01:15** so the Mac stays awake for the whole polling window; currently 01:00,
which is fine while the release is ~12:14 but tighten/extend if the release
drifts later.

## 4. File map (`tee-time-booker/`)

| File | Role |
|---|---|
| `nightly.py` | The unattended orchestrator (weekday schedule, prelogin, run). |
| `book.py` / `tee_booker/cli.py` | Manual CLI (`book`, `schedule`, `inspect`). |
| `tee_booker/booker.py` | The Playwright booking flow: login, open sheet, **poll/race loop**, cart checkout, one-booking-per-run safety, rate-limit backoff, resource blocking. |
| `tee_booker/session.py` | Shared login + URL helpers. |
| `tee_booker/reservations.py` | List reservations (Kenna JSON API) and **cancel** (detail → Cancel or Modify → form). |
| `tee_booker/commands.py` | Local rule-based NL command parser (cancel/skip/pause/etc). No API. |
| `tee_booker/state_store.py` | Kill-switch flag + per-date skip list (`state/`). |
| `tee_booker/config.py` | Config dataclasses + validation. |
| `tee_booker/scheduler.py` | Precise `wait_until` for the release instant. |
| `tee_booker/notify.py` | Optional webhook notification. |
| `dashboard.py` | Flask phone dashboard (reservations, cancel, kill switch, NL commands, clubhouse theme). |
| `tests/` | `test_config.py`, `test_scheduler.py`, `test_commands.py`, `test_booking_safety.py`. |
| `AUTOMATION.md` / `DASHBOARD.md` / `README.md` | Setup + ops docs. |

Gitignored (local only): `config.yaml` (real URLs + selectors), `.env`
(credentials), `.dashboard.env` (dashboard password + cookie key), `state/`,
`logs/`, `screenshots/`, `.venv/`.

## 5. Key facts

- **Platform:** TeeItUp / Kenna. Real URLs, course id, and all CSS/`data-testid`
  selectors are in the local `config.yaml` and documented in the memory file
  `pcc-booking-facts.md`. (Kept out of the repo on purpose.)
- **Booking window:** 14 days in advance. **Release ≈ 12:14 AM ET** (1 data
  point — needs more nights to confirm).
- **Desired schedule** (`weekly_schedule` in config.yaml): 6:30 AM Tue–Fri,
  7:00/7:10 AM Sat–Sun, **2 golfers**, Mondays skipped. (Times list was widened
  to include 6:30–7:20 on weekdays for better odds.)
- **Dashboard:** `http://100.122.139.14:8787` (Mac's Tailscale IP). Password in
  `.dashboard.env`. Phone needs Tailscale on; Mac must be awake.
- **Reservation status codes:** 1 = Confirmed, 0 = Cancelled.

## 6. Hard-won gotchas (don't rediscover these)

1. **Logged-out tee sheet looks fine but is fatal.** The date header
   (`teetimes-header-date`) renders even when logged out, so it's a bad
   login marker. Use `core-user-profile` (account button). A logged-out sheet
   shows ~56 cards during the day but **0 at the release instant** → nothing to
   book. Booker now logs in before the surge and re-logs-in if it sees
   "Login / Sign Up".
2. **The release is later than 12:01** (~12:14). Hence the long poll window.
3. **Cancelling:** the `/cancel` URL does NOT render/cancel on a cold load. Must
   open the reservation **detail** page → click "Cancel or Modify" → fill the
   form (Number of players + Reason + Submit). Reason is hardwired to "Other".
   Per-player cancel is supported.
4. **Cloudflare 1015 rate limiting** bit us early (73 reloads in 90s). The poll
   is now gentle: ~15s jittered interval, images/fonts/analytics blocked in the
   browser context, and **back off ~60s and resume** on any 1015. Keep it gentle
   if you change cadence.
5. **One booking per run is guaranteed** — never retries after submitting a
   purchase; aborts checkout if the cart holds >1 item. Covered by
   `test_booking_safety.py`. Don't loosen this.
6. **Cold deep-links to SPA sub-routes hang** (e.g. `/reservation/history/<id>/
   cancel`); navigate the app the way a user does instead.

## 7. Open items / next steps

- **TIGHTEN THE WINDOW.** Watch `logs/nightly.log` for **2–3 more nights** to
  confirm the release time (~12:14 so far). Then narrow `retry_window_seconds`
  (e.g. 12:01–12:30) so the Mac sleeps earlier, and consider polling a bit
  faster in the ~2-minute band around the known release to compete for popular
  times — while staying gentle the rest of the window.
- **pmset sleep** is at 01:00; bump to 01:15 if you keep the hour-long window,
  or pull it in once the window is tightened.
- **No full-hour / peak load test** of the gentle poll yet — only a 5-min live
  test plus the one real night. Watch the logs for any "Rate-limited" lines.
- **July 8 was intentionally left unbooked** (user couldn't make it).
- Dashboard is reachable only while the Mac is awake; user chose "Tailscale
  always-on" on the phone rather than subnet routing.

## 8. Operate it — cheat sheet

```bash
cd ~/Golf_Booking/tee-time-booker
.venv/bin/python -m pytest -q                 # tests (expect 64 passing)
tail -f logs/nightly.log                       # watch the nightly run
.venv/bin/python nightly.py --plan             # what it WOULD do tonight (no browser)
.venv/bin/python nightly.py --date 2026-07-12 --no-wait --dry-run  # safe dry run

# Manual booking / inspection (CLI):
.venv/bin/python book.py book --date YYYY-MM-DD --dry-run

# launchd (pause/resume a job):
launchctl unload -w ~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist
launchctl load   -w ~/Library/LaunchAgents/com.laneradbill.teebooker.nightly.plist
# (same pattern for ...teebooker.dashboard.plist; reload after editing dashboard.py)

# Power schedule:
pmset -g sched
sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00 sleep MTWRFSU 01:15:00

# Dashboard controls: kill switch (pause), per-date skips, and NL commands
# ("cancel June 30", "skip this Thursday") are all on the web UI. Skips/pause
# are stored in state/ and honored by nightly.py.
```

## 9. First moves for the next session

1. Read this + the memory files + `tail -60 logs/nightly.log`.
2. Confirm the latest night booked (and at what time the sheet released).
3. If 2–3 nights agree on the release time, tighten the window (see §7).
4. `git log --oneline` for recent history; working tree should be clean.
