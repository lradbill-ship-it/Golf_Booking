# Golf Booking App — Session Handoff

Handoff for the next working session. Read this first, then `AUTOMATION.md`,
`DASHBOARD.md`, and `README.md`. Live operational facts also live in the
assistant memory at
`~/.claude/projects/-Users-Lane-DDABBER-Golf-Booking/memory/` (loaded
automatically each session).

_Last updated: 2026-09-30 (Session 6 — the headless checkout block, then a
WRONG-DAY booking: the booker bought a tee time for the current day while
reporting the play date. Both fixed.)_

> **If the booker stops booking, check `runtime.headless` first** — see gotcha #9.

---

## 0c. Session 5 (2026-09-15/16) — what's released, the Tuesday block, the watcher

**Numbering:** the user opened this as "session 4"; the repo's Session 4 is the
Aug 24–25 fallback work below. This is **Session 5**. Trust the repo's count.

### Measured findings (read these before changing the schedule)

1. **First tee = sunrise rounded UP to the next :10.** Fits all 7 checked play
   dates (offset +1..+10 min after sunrise at Pennsauken, never before):

   | Play dates | First tee published |
   |---|---|
   | Sep 9–13 | 6:40 AM |
   | Sep 15–24 | 6:50 AM |
   | Sep 25–29 | 7:00 AM |

   Projection: **7:10 ≈ Oct 6, 7:20 ≈ Oct 20, 7:30 ≈ Oct 27, back to 6:40 on
   Nov 3** (DST ends Nov 1). It moves every day of the week together — nothing
   is Tue–Fri specific.
2. **Published ≠ gettable.** `release_history.earliest_time` is the earliest
   card seen that night (at release = earliest published). The live sheet only
   renders UNSOLD slots — sold ones vanish — so a daytime look shows what's
   LEFT, never what was published. Don't use a daytime scrape to measure drift.
3. **The Tuesday block.** Play dates Tue Sep 22 and Tue Sep 29 each lost **17 and
   16 consecutive checkouts** ("no longer available"), walking 6:50/7:00 → 9:30
   one slot per ~50s, and booked **9:40 AM** after ~35 min / 64 checks. Same
   boundary both weeks; and Wed Sep 23 (released one day later) still had 7:00
   open six days on. A standing Tuesday-morning block (league/outing), not a race
   lost by milliseconds — polling faster won't beat it. It began when the first
   tee passed 6:50: Tue Sep 8 booked 6:40; Sep 15 booked nothing; Sep 22/29 = 9:40.
   "Taken at checkout" over 12 nights each: **Tue 36**, Sun 24, Fri 15, Sat 10,
   Wed 4, Thu 4.
4. **The user cancelled Tue Sep 22 9:40** (status 0). **Tue Sep 29 9:40** and
   **Sun Sep 27 7:40** are held (as of 2026-09-16).
5. **`NOTIFY_WEBHOOK_URL` is blank** in `.env` — every `notify()` goes only to the
   log. The dashboard is the only place a result reaches the user.
6. **pmset repeating sleep is still 01:00**, not the 01:15 §3/§7 recommend. The
   last two Tuesday races ended at 00:35 — 25 min of margin.

### What shipped

- **`watch_earlier.py` + `tee_booker/earlier_watch.py` — READ-ONLY earlier-time
  watcher.** Re-checks dates the nightly booker settled for and flags an EARLIER
  2-golfer time. Never books, cancels, or touches the cart (a test fake fails if
  Book or the cart is touched). Watches a reservation only if: sole confirmed one
  that date; booked by the automation (exact date+time in release_history); a
  scheduled, un-skipped day; the configured party size; cancellable; ≥
  `min_days_ahead`; later than the target by ≥ `min_improvement_minutes`; later
  than that sheet's published first tee. `--plan` explains every reservation.
  Refuses to run 23:30–01:30 (`quiet_start/end`) — launchd replays a missed job
  on wake and the Mac wakes at 23:55. Lock file prevents overlap. Announces each
  opening once. History `state/earlier_watch.jsonl`, `--history`.
- **Dashboard "Earlier-time watch" card** (live; dashboard restarted 2026-09-16):
  per watched date "holding X · earliest open Y", a highlighted banner when an
  earlier time is open, and a warning if no check has run in >26h.
- `TeeBooker._closest_slot` — the fallback's ranking extracted so the nightly
  fallback and the watcher share ONE implementation; `_earliest_bookable`.
- `earlier_watch:` config block (all optional); `launchd/com.laneradbill.teebooker.watch.plist`
  (7:10/11:10/15:10/19:10) — **written, NOT installed.**
- Tests **214** (was 162). The 8 safety rules were mutation-tested: each
  deliberate break was caught by the test named for it.
- Verified live: `--plan` and a full scan against the portal (4 of 4 dates:
  Wed 23 earliest open 8:50, Thu 24 7:40, Sun 27 11:10, Tue 29 9:40 — our own slot).

### Blocked — auto-upgrade (book the earlier time, cancel the later one)

The user said "we should continue to try to get earlier tee times". Writing an
unattended book-then-cancel script was **denied by the auto-mode safety
classifier (Real-World Transactions)** — it needs the user's explicit OK, and was
NOT worked around. If approved, the design (already reasoned through):

1. At most ONE purchase attempt per run; set status "stop" BEFORE clicking so an
   exception mid-checkout reads "may have bought — verify", never "nothing".
2. **Book the new time FIRST; never cancel first** (cancel-first can lose both).
3. Verify the new reservation on the Kenna reservation list (retry ~4×10s) —
   a URL change / banner is not proof. Not found ⇒ flag, don't cancel.
4. Cancel the old via `reservations.cancel_reservation`.
5. Verify with POSITIVE evidence (old listed Cancelled AND new Confirmed). An
   empty read is NOT success. Otherwise flag "holding both" on the dashboard.
6. **Unknown that decides whether book-first is even possible:** does PCC let a
   member hold two tee times on the same day? Settle it with one supervised live
   attempt before arming anything.

### Open decisions (the user's)

1. Install the watcher's launchd job? (read-only; logs in ~4×/day)
2. Authorize the auto-upgrade above?
3. `sudo pmset repeat wakeorpoweron MTWRFSU 23:55:00 sleep MTWRFSU 01:15:00`
   (needs admin — the user runs it).
4. Retarget `weekly_schedule`: Tue–Thu still target 6:30, which hasn't been
   published since before Sep 9. Bookings still land right (the fallback takes
   the earliest), but logs read "190 min later than 6:30". Options: auto-follow
   the sunrise rule, or edit by hand monthly.
5. Tuesday strategy if the watcher never sees the block release: skip Tuesdays,
   target ~9:40, or ask the pro shop what the block is and whether it clears.
6. A date whose fallback the user CANCELLED (Sep 22) is not watched. Should an
   early slot opening there be flagged too?
7. Set `NOTIFY_WEBHOOK_URL` so openings reach the phone without opening the dashboard.

---

## 0b. The standing rule (set by the user, 2026-08-25)

> **You have to book a tee time.** If the desired time isn't open, take the next
> closest time — **even if it is hours later.**

Encoded as `booking.fallback.max_minutes_later: null` (no cap on the late side),
which is now the default. Do not reintroduce a late-side cap without the user
saying so. The early side stays capped at 60 min, since a slot earlier than the
sheet's first tee time doesn't occur in practice.

**"Closest" is measured from a single target, not from the whole list.** The
target is the FIRST entry in that day's `preferred_times`. The user chose this
explicitly over measuring to the nearest listed time: they want it to orient on
the time they actually want and work outward from there. Targets:

| Days | Target | List |
|---|---|---|
| Tue, Wed, Thu | **6:30 AM** | 6:30, 6:40, 6:50, 7:00, 7:10, 7:20 |
| Fri, Sat, Sun | **7:00 AM** | 7:00, 7:10, 7:20, 7:30, 6:50 |
| Mon | — | skipped |

Friday moved from the weekday group to the weekend group in this session — the
data showed it landing 7:10 three weeks running, so 6:30 was never its real
target. Because the target is just the list's first element, changing a day's
orientation means reordering that day's list; there is no separate setting.
`nightly.py --plan` prints the target it would use tonight.

**Why it mattered:** 47 nights of history showed **Sundays had never booked —
0 for 8** since July 12, and Sat Sep 5 became the first Saturday miss. The
weekend list was only `7:00 AM` + `7:10 AM`; the Sep 6 run watched **46 bookable
cards** for an hour and took none of them. Weekdays are drifting too — Fridays
landed 7:10 three weeks running (Aug 21/28, Sep 4), one step from falling off a
list that ends at 7:20.

Fri/Sat/Sun `weekly_schedule` was widened at the same time to
`["7:00 AM", "7:10 AM", "7:20 AM", "7:30 AM", "6:50 AM"]` — that list controls
*preference order* near the target; the fallback handles everything past it.
Note 6:50 sits last (least preferred) but does not affect the target, which is
the first element.

---

## 0a. Session 4 (2026-08-24) — book the closest time when 6:30 AM is gone

**Why:** sunrise is sliding later, so PCC's sheet is starting later and the
6:30 AM slots in `weekly_schedule` will soon stop being published at all. An
exact-match-only booker books *nothing* on exactly those nights.

**What changed:**
- `booking.fallback` (new config block, **on by default**). When none of the
  night's `preferred_times` is bookable, the booker takes the bookable slot
  **closest** to them — earlier wins a tie — bounded to `max_minutes_earlier`
  (60) before the first preferred time and `max_minutes_later` after the last.
  In the sunrise case that is just "the earliest time on the sheet". Either
  bound accepts `null` for "no bound"; the late side ships as `null` per the
  standing rule in §0b.
- `recheck_seconds` (3s): before settling for second best it pauses and re-scans
  the preferred times once, so a half-rendered sheet can't cost a good time.
  `after_seconds` (0) can hold the fallback back to give preferred a head start.
- **Drift tracking:** every run now records the earliest tee time the sheet
  published that night. `nightly.py --history` has a new **`1st tee`** column
  plus a first-vs-most-recent line. **This is the number to watch** — when it
  passes 6:30 for a few nights, move `weekly_schedule` deliberately.
- `nightly.py --plan` prints tonight's fallback window.
- Logs and notifications name the compromise:
  `Booked 7:00 AM for 2 players (closest available — 30 min later than preferred)`.

**No action needed on the Mac's `config.yaml`** — the fallback defaults to on,
so it works with the existing file. Add a `booking.fallback:` block only to tune
the bounds or turn it off. Tests: **162 passing** (was 90).

**Still to watch:** the first night the fallback actually fires. Look for
`closest available` in `logs/nightly.log`, and confirm the booked time is sane.

---

## 0. Syncing this back to the Mac (read first if resuming locally)

Session 2 ran in a **cloud container** (fresh clone, no local files). All its
work is on branch `claude/golf-booking-app-session-2-r3i7zt`, which is just
`main` **+ 1 commit** (a clean fast-forward — no conflicts). To pull it onto the
Mac and consolidate everything in one local place:

```bash
cd ~/Golf_Booking
git fetch origin
git checkout main
git merge --ff-only origin/claude/golf-booking-app-session-2-r3i7zt
git push origin main          # optional: keep the remote main current
```

Your gitignored local files (`config.yaml`, `.env`, `.dashboard.env`, `.venv/`,
`logs/`, `state/`, `screenshots/`) are untouched by this — they live only on the
Mac and aren't in any branch. After the merge, future sessions can just work on
`main` locally. Then verify: `cd tee-time-booker && .venv/bin/python -m pytest -q`
(expect 214 passing).

### What Session 2 changed (no behavior change to the nightly race)
- Removed a leaked-browser path in `booker.run()` (guarded both closes).
- `_players_allowed` now accepts `"up to N"` / `"max N"` labels (a twosome is no
  longer skipped from a slot that allows it).
- Fixed the stale cancel-flow docstring in `reservations.py`.
- Dashboard cancel POST no longer caps players-to-cancel to 1 on a cache miss.
- Added tests for `compute_play_date`, `reservations._parse`, `state_store`, and
  the new party-size cases (42 → 64 passing).

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
- **214 tests pass** (`.venv/bin/python -m pytest -q`).

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
| `tee_booker/release_history.py` | Append-only log of each night's actual sheet-release time **and the earliest tee time published** (`state/release_history.jsonl`); `summarize()` powers `nightly.py --history`. |
| `tee_booker/config.py` | Config dataclasses + validation. |
| `tee_booker/scheduler.py` | Precise `wait_until` for the release instant. |
| `tee_booker/notify.py` | Optional webhook notification. |
| `watch_earlier.py` | READ-ONLY earlier-time watcher (Session 5): flags an earlier time on dates the nightly booker settled for. `--plan`, `--history`. |
| `tee_booker/earlier_watch.py` | The watcher's pure rules: which reservations to watch, quiet window, history, lock. |
| `launchd/com.laneradbill.teebooker.watch.plist` | Watcher schedule (4×/day) — not installed by default. |
| `dashboard.py` | Flask phone dashboard (reservations, cancel, kill switch, NL commands, earlier-time watch card, clubhouse theme). |
| `tests/` | `test_config.py`, `test_scheduler.py`, `test_commands.py`, `test_booking_safety.py`, `test_fallback.py` (closest-time fallback + drift tracking), `test_earlier_watch.py` (watcher rules, read-only scan, runner). |
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
  to include 6:30–7:20 on weekdays for better odds.) As of Session 4, a night
  where none of those is bookable falls back to the closest available time
  rather than booking nothing — see §0a.
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
7. **A slot can be taken out from under you at checkout** (Session 3, the
   2026-06-28 Jul-12 run: found 7:00 AM, but it was gone by the time checkout
   completed → "The selected inventory is no longer available"). This is a
   *safe* rejection (nothing bought), NOT a possible double-booking. The booker
   now detects that message, returns `"taken"`, and tries the **next** preferred
   time (e.g. 7:10) — resolving the lost race in ~1s instead of waiting out the
   full confirmation timeout. To clean up the dead cart item before the retry it
   reopens the tee sheet, opens the cart drawer (`cart_open_button`), and deletes
   the item (`cart_item` kebab → `cart_item_remove`). If any of those selectors
   is unset it skips clearing and the >1-item guard stops it (still safe).
   **Bug found & fixed in the Session-3 audit:** the first version cleared the
   cart while still on the *checkout* page, where the drawer controls don't
   render, so the dead item survived and the retry hit the 2-item guard (this is
   exactly what lost Jul 17 and Jul 19). Fix: reopen the sheet first, then open
   the drawer via `cart_open_button` (`[data-testid="core-shopping-cart"]`, which
   only renders when the cart is non-empty) before deleting.
8. **No internet at login time is survivable now** (Session-3 audit: the Jul 16
   run died at 00:00 with `net::ERR_INTERNET_DISCONNECTED` — the Mac's Wi-Fi
   hadn't reconnected). Login is now retried on network errors for
   `release.login_retry_seconds` (default 300s, gentle 10s gap) before giving up;
   non-network errors still fail fast. There's ~13 min of slack before the sheet
   releases, so a brief blip no longer loses the night. If the Mac's Wi-Fi drops
   nightly, address that at the OS level too.

9. **THE PORTAL BLOCKS HEADLESS BROWSERS AT CHECKOUT (from 2026-09-23).** Six
   consecutive nights (play dates Oct 7-13) booked NOTHING: the race was won,
   the slot found, the purchase submitted — then the page sat on "Processing
   cart items ( 0 of 1 )" forever and the run gave up. Proven 2026-09-29 by A/B
   on the SAME date and slot: headless hung 180s and bought nothing; a visible
   browser booked it in 4 seconds. Ruled out first: sleep (the Mac woke every
   night and ran), the cart (empty), price/rate (all $0.00), resource blocking
   (same hang with `block_resources: false`), and the Session-5 refactor (four
   nights booked fine after it). **`runtime.headless` must stay `false`.** The
   member's own browser was never affected — the user booked by hand throughout.

10. **NEVER TRUST THE PAGE TO BE THE DAY YOU ASKED FOR (2026-09-30).** The run
   for play date 2026-10-14 — a day the course is CLOSED — reopened the sheet,
   the reopen died mid-flight, `_reopen` swallowed the error, and the browser
   was left on the portal's DEFAULT sheet (that night's own day, 63 cards). The
   race read those as Oct 14's, bought **12:40 PM on 2026-09-30**, and logged
   "Booked 12:40 PM for 2026-10-14". The order receipt was the only place the
   real date appeared; the reservation list could not show it either, because a
   same-day booking drops off once the time passes.
   Fixes: `selectors.sheet_date_label` is read every cycle and compared with the
   requested play date — a sheet that is the wrong day (or whose date cannot be
   read) contributes NOTHING: not "released", not `earliest_time`, not a
   bookable slot — and `_book_slot` refuses at the click as a second layer. An
   unconfigured `sheet_date_label` keeps the old behaviour rather than refusing
   everything. `_reopen` now logs its failures.
   **A closed day is now its own outcome:** zero tee times all night on a
   POSITIVELY-read sheet for the right day reports "No tee times were published
   for <date> at all ... likely closed ... nothing was booked on any other day",
   distinct from "times were published, so they were taken". `_slot_count`
   returning -1 means COULDN'T COUNT and is never called a closure.

## 7. Open items / next steps

- **Checkout-race recovery — cart-clear selectors captured & bug fixed (Session
  3 audit).** Recovery (gotcha #7) clears a now-dead cart item before trying the
  next preferred time, using `cart_open_button`
  (`[data-testid="core-shopping-cart"]`), `cart_item`
  (`[data-testid^="shopping-cart-kebab-button-"]`), and `cart_item_remove`
  (`[data-testid^="delete-item-button-"]`) — all set in the local `config.yaml`.
  The drawer-open + delete controls are confirmed live (added a throwaway item
  and deleted it). **Still unverified end-to-end:** a *real* lost race at the
  release instant (can't be staged) — watch `logs/nightly.log` for the first
  night it logs a "taken" and then books a later time instead of a 2-item abort.
- **Release-time tracking is now automatic (Session 3).** Every genuine waited
  run appends a record to `state/release_history.jsonl` (when the sheet actually
  released vs. the nominal 00:01, whether it booked, how many checks). View it
  with `.venv/bin/python nightly.py --history`. The one known data point
  (Sat Jul 11, released 00:14:18 → +13m18s) is backfilled. Let a few more nights
  accumulate, then use the median to tighten the window.
- **TIGHTEN THE WINDOW.** Watch `nightly.py --history` for **2–3 more nights** to
  confirm the release time (~12:14 so far). Then narrow `retry_window_seconds`
  (e.g. 12:01–12:30) so the Mac sleeps earlier, and consider polling a bit
  faster in the ~2-minute band around the known release to compete for popular
  times — while staying gentle the rest of the window.
- **pmset sleep** is at 01:00; bump to 01:15 if you keep the hour-long window,
  or pull it in once the window is tightened.
- **No full-hour / peak load test** of the gentle poll yet — only a 5-min live
  test plus the one real night. Watch the logs for any "Rate-limited" lines.
- **Sunrise drift (Session 4).** Watch the `1st tee` column in
  `nightly.py --history`. Once the sheet consistently starts after 6:30 AM,
  update `weekly_schedule` to the real times — the fallback is a safety net, not
  a substitute for a correct schedule. Also confirm the first real fallback
  booking looks right in `logs/nightly.log` (`closest available`).
- **July 8 was intentionally left unbooked** (user couldn't make it).
- Dashboard is reachable only while the Mac is awake; user chose "Tailscale
  always-on" on the phone rather than subnet routing.

## 8. Operate it — cheat sheet

```bash
cd ~/Golf_Booking/tee-time-booker
.venv/bin/python -m pytest -q                 # tests (expect 214 passing)
tail -f logs/nightly.log                       # watch the nightly run
.venv/bin/python nightly.py --plan             # what it WOULD do tonight (no browser)
.venv/bin/python nightly.py --history          # when the sheet actually released each night
.venv/bin/python watch_earlier.py --plan        # which tee times the earlier-time watcher would watch
.venv/bin/python watch_earlier.py --history     # earliest open time per watched date
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
