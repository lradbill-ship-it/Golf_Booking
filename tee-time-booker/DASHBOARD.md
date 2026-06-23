# Phone dashboard

A small Flask web app (`dashboard.py`) to view/cancel reservations and arm or
disarm the nightly auto-booker from your phone over Tailscale.

## Access

- **URL:** `http://100.122.139.14:8787` (the Mac's Tailscale IP), or
  `http://big-ls-office-mac:8787` if MagicDNS is on.
- **Password:** stored in `.dashboard.env` (gitignored) as `DASHBOARD_PASSWORD`.
- Your phone must have **Tailscale turned on**, and the **Mac must be awake**
  (the app is unreachable while the Mac sleeps).

## What it does

- **Type-a-command box** — plain English, parsed locally (no API, no cost),
  with a preview + confirm before anything happens. Examples:
  - `cancel June 30` / `cancel my reservation on 7/2`
  - `don't book this Thursday` / `skip this weekend` / `don't make any reservations this week`
  - `pause everything` / `resume`
  - Skips are stored per-date in `state/skips.json`; `nightly.py` skips those
    dates. Parsing is rule-based (`tee_booker/commands.py`) — it understands
    dates, weekdays, "this/next week", "this weekend", today/tomorrow.
- **Upcoming reservations** — pulled live from the portal (date, time, golfers,
  confirmation #, status). Cached ~2 min; "Refresh" forces a re-fetch.
- **Cancel** — each eligible reservation has a Cancel button with a confirm
  prompt. (The portal cancels immediately once confirmed — there's no undo.)
- **Kill switch** — pauses the nightly auto-booker (writes `state/paused.flag`,
  which `nightly.py` checks and obeys). "Arm" re-enables it.
- **Skip chips** — dates you've told it to skip show as chips; tap one to undo.
- **Recent activity** — tail of `logs/nightly.log`.

## Run / manage

Installed as a launchd service (starts at login, restarts on crash):
`~/Library/LaunchAgents/com.laneradbill.teebooker.dashboard.plist`

```bash
# Stop / start:
launchctl unload -w ~/Library/LaunchAgents/com.laneradbill.teebooker.dashboard.plist
launchctl load   -w ~/Library/LaunchAgents/com.laneradbill.teebooker.dashboard.plist

# Logs:
tail -f logs/dashboard.log

# Change the password: edit .dashboard.env, then reload the service.
```

## Notes / limits

- It serves a Flask development server — fine for one private user over
  Tailscale, not hardened for the public internet.
- Reachable only while the Mac is awake. With the nightly power schedule the Mac
  sleeps 12:15 AM–11:55 PM unless you're using it; wake it (or disable idle
  sleep) if you need the dashboard at a specific time.
- Secrets (`.dashboard.env`, `.env`, `config.yaml`) and runtime `state/` are
  gitignored.
