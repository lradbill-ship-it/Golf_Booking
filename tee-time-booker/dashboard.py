#!/usr/bin/env python3
"""Phone-friendly dashboard for the tee-time auto-booker.

View upcoming reservations, cancel one, arm/disarm the nightly auto-booker, and
type plain-English commands ("cancel June 30", "don't book this Thursday").
Command parsing is fully local (no API, no cost) and every command is previewed
before it runs. Password-gated; intended to be reached over Tailscale.

Secrets are read from `.dashboard.env` (gitignored):
    DASHBOARD_PASSWORD=...   # what you type to log in
    DASHBOARD_SECRET=...     # random key used to sign the session cookie

Run:  .venv/bin/python dashboard.py        (listens on 0.0.0.0:8787)
"""

from __future__ import annotations

import datetime as _dt
import hmac
import os
import threading
import time
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, flash, redirect, render_template_string, request, session, url_for,
)

from tee_booker import state_store
from tee_booker.commands import parse_command
from tee_booker.config import Config, Credentials
from tee_booker.reservations import cancel_reservation, fetch_reservations

HERE = os.path.dirname(os.path.abspath(__file__))
NIGHTLY_LOG = os.path.join(HERE, "logs", "nightly.log")
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
CACHE_TTL = 120  # seconds

load_dotenv(os.path.join(HERE, ".dashboard.env"))
load_dotenv(os.path.join(HERE, ".env"))

PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SECRET = os.environ.get("DASHBOARD_SECRET", "")

app = Flask(__name__)
app.secret_key = SECRET or os.urandom(32)

_pw_lock = threading.Lock()
_cache: dict = {"reservations": None, "ts": 0.0, "error": None}


# --------------------------------------------------------------------------- #

def _load_cfg():
    cfg = Config.load(os.path.join(HERE, "config.yaml"))
    creds = Credentials.from_env(os.path.join(HERE, ".env"))
    return cfg, creds


def _refresh_reservations(force: bool = False):
    if not force and _cache["reservations"] is not None and (time.time() - _cache["ts"] < CACHE_TTL):
        return
    with _pw_lock:
        if not force and _cache["reservations"] is not None and (time.time() - _cache["ts"] < CACHE_TTL):
            return
        try:
            cfg, creds = _load_cfg()
            _cache["reservations"] = fetch_reservations(cfg, creds, log=lambda *_: None)
            _cache["error"] = None
        except Exception as exc:  # noqa: BLE001
            _cache["error"] = str(exc)
        _cache["ts"] = time.time()


def _reservations():
    _refresh_reservations(force=False)
    return _cache["reservations"] or []


def _tail_log(n: int = 40) -> str:
    try:
        with open(NIGHTLY_LOG, "r") as fh:
            return "".join(fh.readlines()[-n:]) or "(log is empty)"
    except FileNotFoundError:
        return "(no nightly log yet)"


def _pretty(iso_date: str) -> str:
    try:
        return _dt.date.fromisoformat(iso_date).strftime("%a, %b %-d")
    except ValueError:
        return iso_date


def require_auth(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


# --------------------------------------------------------------------------- #

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if PASSWORD and hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            session["auth"] = True
            return redirect(url_for("home"))
        flash("Wrong password.")
    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_auth
def home():
    return render_template_string(
        HOME_HTML,
        signout=True,
        reservations=_reservations(),
        error=_cache["error"],
        paused=state_store.is_paused(),
        skips=[(d, _pretty(d)) for d in state_store.load_skip_dates()],
        updated=time.strftime("%-I:%M %p", time.localtime(_cache["ts"])) if _cache["ts"] else "never",
        log=_tail_log(),
    )


@app.route("/refresh", methods=["POST"])
@require_auth
def refresh():
    _refresh_reservations(force=True)
    if _cache["error"]:
        flash(f"Refresh failed: {_cache['error']}")
    return redirect(url_for("home"))


@app.route("/cancel/<int:res_id>", methods=["GET", "POST"])
@require_auth
def cancel(res_id):
    r = _find_res(res_id)
    if request.method == "GET":
        if r is None:
            flash("Couldn't find that reservation — try Refresh.")
            return redirect(url_for("home"))
        total = (r.players or 1)
        return render_template_string(CANCEL_HTML, r=r, total=total,
                                      options=list(range(1, total + 1)))
    # POST — perform the cancellation.
    total = (r.players if r else 1) or 1
    try:
        n = int(request.form.get("players_to_cancel", total))
    except ValueError:
        n = total
    _cancel_one(res_id, max(1, min(n, total)))
    return redirect(url_for("home"))


@app.route("/toggle-pause", methods=["POST"])
@require_auth
def toggle_pause():
    state_store.set_paused(not state_store.is_paused())
    flash("Auto-booker PAUSED." if state_store.is_paused() else "Auto-booker ARMED.")
    return redirect(url_for("home"))


@app.route("/command", methods=["POST"])
@require_auth
def command():
    text = (request.form.get("text") or "").strip()
    cmd = parse_command(text, _dt.date.today())

    matched, unmatched = [], []
    if cmd.action == "cancel":
        res = _reservations()
        for iso in cmd.iso_dates:
            hits = [r for r in res if r.when and r.when.date().isoformat() == iso and not r.cancelled]
            matched.extend(hits)
            if not hits:
                unmatched.append(iso)
        # A single match goes straight to the per-player cancel page (so it can
        # ask how many golfers to cancel). Multiple matches use the list below.
        if len(matched) == 1 and not unmatched:
            return redirect(url_for("cancel", res_id=matched[0].id))

    return render_template_string(
        CONFIRM_HTML,
        text=text,
        cmd=cmd,
        dates_pretty=[_pretty(d) for d in cmd.iso_dates],
        matched=matched,
        unmatched=[_pretty(d) for d in unmatched],
        ids_csv=",".join(str(r.id) for r in matched),
        dates_csv=",".join(cmd.iso_dates),
    )


@app.route("/command/apply", methods=["POST"])
@require_auth
def command_apply():
    action = request.form.get("action", "")
    dates = [d for d in (request.form.get("dates_csv") or "").split(",") if d]
    ids = [int(x) for x in (request.form.get("ids_csv") or "").split(",") if x]

    if action == "cancel":
        # Bulk cancel (multiple dates matched) — cancel each booking in full.
        for rid in ids:
            r = _find_res(rid)
            _cancel_one(rid, (r.players if r else 1) or 1)
    elif action == "skip":
        state_store.add_skip_dates(dates)
        flash(f"Won't book: {', '.join(_pretty(d) for d in dates)}.")
    elif action == "unskip":
        state_store.remove_skip_dates(dates)
        flash(f"Re-enabled booking for: {', '.join(_pretty(d) for d in dates)}.")
    elif action == "pause":
        state_store.set_paused(True)
        flash("Auto-booker PAUSED.")
    elif action == "arm":
        state_store.set_paused(False)
        flash("Auto-booker ARMED.")
    return redirect(url_for("home"))


def _find_res(res_id):
    for r in _reservations():
        if r.id == res_id:
            return r
    return None


def _cancel_one(res_id, players_to_cancel):
    cfg, creds = _load_cfg()
    try:
        with _pw_lock:
            ok = cancel_reservation(cfg, creds, res_id, players_to_cancel, log=lambda *_: None)
        flash(
            f"Cancelled {players_to_cancel} player(s)." if ok
            else "Couldn't confirm the cancellation — please check the portal."
        )
    except Exception as exc:  # noqa: BLE001
        flash(f"Cancel error: {exc}")
    _refresh_reservations(force=True)


# --------------------------------------------------------------------------- #
# Presentation: a classic clubhouse theme — scorecard cream, Masters green,
# brass accents, engraved-serif wordmark. Built for quick one-thumb use.

CREST = """
<svg class=crest viewBox="0 0 44 44" width=42 height=42 aria-hidden=true>
  <circle cx=22 cy=22 r=20 fill="#0e4430" stroke="#c7a957" stroke-width=2/>
  <path d="M5 31 Q22 23 39 31 L39 38 Q22 31 5 38 Z" fill="#1c6b46"/>
  <line x1=20 y1=11 x2=20 y2=32 stroke="#f4efe1" stroke-width=1.7 stroke-linecap=round/>
  <path d="M20 11 L32 14.5 L20 18 Z" fill="#c7a957"/>
  <ellipse cx=20 cy=32 rx=3 ry=1.2 fill="#08281d"/>
</svg>
"""

BASE_CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:#f1ead7;
         background-image: radial-gradient(120% 80% at 50% -10%, #f7f1e2 0%, #ebe2cb 100%);
         color:#23362b; font-family:-apple-system, system-ui, "Helvetica Neue", Arial, sans-serif;
         padding-bottom: 40px; }
  .serif { font-family:"Hoefler Text","Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif; }
  .wrap { max-width: 600px; margin: 0 auto; padding: 0 16px; }

  header { background:#0e4430; color:#f4efe1; border-bottom:3px solid #c7a957;
           box-shadow:0 2px 10px rgba(0,0,0,.18); position:sticky; top:0; z-index:5; }
  .head { display:flex; align-items:center; gap:12px; padding:12px 16px; max-width:600px; margin:0 auto; }
  .crest { flex:0 0 auto; display:block; }
  .brand { flex:1 1 auto; line-height:1.1; }
  .brand .name { font-size:1.45rem; letter-spacing:.5px; }
  .brand .sub { font-size:.62rem; letter-spacing:.22em; text-transform:uppercase; color:#d9c79a; margin-top:3px; }
  .signout { color:#d9c79a; text-decoration:none; font-size:.72rem; letter-spacing:.12em;
             text-transform:uppercase; border:1px solid #3c6552; border-radius:999px; padding:7px 12px; }

  h2.section { font-size:.72rem; letter-spacing:.2em; text-transform:uppercase; color:#7a6a3f;
               margin:22px 4px 10px; font-weight:700; }
  .label { font-size:.66rem; letter-spacing:.16em; text-transform:uppercase; color:#8a7a4d; }

  .card { background:#fcf9f0; border:1px solid #ddd0ac; border-radius:14px; padding:16px;
          margin-bottom:12px; box-shadow:0 1px 2px rgba(40,30,0,.06); }
  .row { display:flex; justify-content:space-between; align-items:center; gap:12px; }

  /* buttons */
  button, .btn { font:inherit; font-size:1rem; border:0; border-radius:11px; padding:13px 16px;
          min-height:48px; background:#0e4430; color:#f6f1e2; cursor:pointer; font-weight:600;
          text-decoration:none; display:inline-flex; align-items:center; justify-content:center; }
  button:active, .btn:active { transform:translateY(1px); }
  button.brass { background:#b3933f; color:#22210f; }
  button.danger { background:#7c2a28; }
  .btn.ghost, button.ghost { background:transparent; color:#0e4430; border:1.5px solid #0e4430; }
  .full { width:100%; }

  input[type=password], input[type=text] { font:inherit; font-size:16px; padding:14px; width:100%;
          border-radius:11px; border:1.5px solid #cdbf99; background:#fffdf7; color:#23362b; }
  input::placeholder { color:#a89c79; }

  .flash { background:#0e4430; color:#f4efe1; padding:11px 14px; border-radius:11px; margin:12px 0;
           font-size:.92rem; }

  /* quick command chips */
  .quick { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
  .quick form { margin:0; }
  .quick button { min-height:0; padding:8px 13px; font-size:.84rem; font-weight:600; border-radius:999px;
          background:#ede3c6; color:#0e4430; border:1px solid #d8c89c; }

  /* skip chips */
  .chips { margin-top:8px; }
  .chips form { display:inline; }
  .chip { min-height:0; padding:7px 12px; margin:4px 6px 0 0; font-size:.82rem; border-radius:999px;
          background:#fff; border:1px solid #c9a96a; color:#6e5a25; font-weight:600; }

  /* scorecard reservation row */
  .tee { border-left:4px solid #c7a957; }
  .tee.is-cancelled { border-left-color:#b08; opacity:.7; border-left-color:#9a5b58; }
  .when { font-size:1.22rem; font-weight:600; }
  .meta { color:#6d6450; font-size:.85rem; margin-top:3px; }
  .pill { font-size:.66rem; letter-spacing:.1em; text-transform:uppercase; padding:4px 10px;
          border-radius:999px; font-weight:700; white-space:nowrap; }
  .pill.ok { background:#e0eede; color:#1b5b3a; border:1px solid #bcd9bf; }
  .pill.cancelled { background:#f3e0df; color:#8a3330; border:1px solid #e2bdbb; }

  .status-on { color:#1b5b3a; } .status-off { color:#8a3330; }
  .big { font-size:1.15rem; font-weight:600; }
  .muted { color:#8a7f63; font-size:.78rem; }

  details { margin-top:8px; } summary { cursor:pointer; color:#7a6a3f; font-size:.8rem;
          letter-spacing:.16em; text-transform:uppercase; font-weight:700; }
  pre { white-space:pre-wrap; word-break:break-word; font-size:.72rem; background:#11201a; color:#cfe0d4;
        padding:12px; border-radius:11px; overflow:auto; max-height:240px; margin-top:10px; }
"""

HEADER = """
<header><div class=head>
  """ + CREST + """
  <div class=brand>
    <div class="name serif">Tee Booker</div>
    <div class=sub>Pennsauken Country Club</div>
  </div>
  {% if signout %}<a class=signout href="{{ url_for('logout') }}">Sign out</a>{% endif %}
</div></header>
"""

LOGIN_HTML = """
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta name=color-scheme content=light>
<title>Tee Booker</title><style>""" + BASE_CSS + """</style></head>
<body>""" + HEADER.replace("{% if signout %}", "{% if False %}") + """
<div class=wrap>
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class=flash>{{m}}</div>{% endfor %}{% endwith %}
  <h2 class=section>Members entrance</h2>
  <form method=post class=card>
    <div class=label style="margin-bottom:8px">Password</div>
    <input type=password name=password autofocus autocomplete=current-password>
    <p style="margin:14px 0 0"><button class="full" type=submit>Enter the clubhouse</button></p>
  </form>
</div></body></html>
"""


def _quick_chip(label, text):
    return (
        '<form method=post action="/command"><input type=hidden name=text value="'
        + text + '"><button type=submit>' + label + "</button></form>"
    )


QUICK_CHIPS = "".join(_quick_chip(l, t) for l, t in [
    ("Skip Thursday", "skip this Thursday"),
    ("Skip Friday", "skip this Friday"),
    ("Skip weekend", "skip this weekend"),
    ("Skip this week", "don't book this week"),
])

HOME_HTML = ("""
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta name=color-scheme content=light>
<title>Tee Booker</title><style>""" + BASE_CSS + """</style></head>
<body>""" + HEADER + """
<div class=wrap>
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class=flash>{{m}}</div>{% endfor %}{% endwith %}

  <h2 class=section>The Starter</h2>
  <form method=post action="{{ url_for('command') }}" class=card>
    <input type=text name=text placeholder="Tell me what to do…" autocomplete=off autocapitalize=off>
    <p style="margin:12px 0 0"><button class="full" type=submit>Go</button></p>
    <div class=quick>""" + QUICK_CHIPS + """</div>
    <div class=muted style="margin-top:10px">Try “cancel June 30”, “skip this Thursday”, “pause everything”, “resume”.</div>
  </form>

  <h2 class=section>Auto-booker</h2>
  <div class="card row">
    <div>
      <div class=big><span class="{{ 'status-off' if paused else 'status-on' }}">{{ 'Paused' if paused else 'Armed' }}</span></div>
      <div class=meta>{{ 'Will NOT book until re-armed.' if paused else 'Books the date 14 days out at 12:01 AM.' }}</div>
    </div>
    <form method=post action="{{ url_for('toggle_pause') }}">
      <button class="{{ 'brass' if paused else 'danger' }}" type=submit>{{ 'Arm' if paused else 'Pause' }}</button>
    </form>
  </div>

  {% if skips %}
  <div class=card>
    <div class=label>Skipping these dates</div>
    <div class=chips>
      {% for iso, pretty in skips %}
        <form method=post action="{{ url_for('command_apply') }}">
          <input type=hidden name=action value=unskip><input type=hidden name=dates_csv value="{{ iso }}">
          <button class=chip type=submit title="Tap to re-enable">{{ pretty }} ✕</button>
        </form>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class=row>
    <h2 class=section>Upcoming tee times</h2>
    <form method=post action="{{ url_for('refresh') }}"><button class=ghost style="min-height:0;padding:8px 14px" type=submit>Refresh</button></form>
  </div>
  <div class=muted style="margin:0 4px 8px">As of {{ updated }}</div>
  {% if error %}<div class=flash>Couldn't load reservations: {{ error }}</div>{% endif %}
  {% if not reservations %}<div class=card>No upcoming reservations.</div>{% endif %}

  {% for r in reservations %}
  <div class="card tee {{ 'is-cancelled' if r.cancelled else '' }}">
    <div class=row>
      <div>
        <div class="when serif">{{ r.date_label }}</div>
        <div class=meta>{{ r.time_label }} · {{ r.players }} golfer{{ '' if r.players == 1 else 's' }} · {{ r.holes }} holes</div>
        <div class=muted>#{{ r.confirmation }}</div>
      </div>
      <span class="pill {{ 'cancelled' if r.cancelled else 'ok' }}">{{ r.status }}</span>
    </div>
    {% if r.eligible_cancel and not r.cancelled %}
    <p style="margin:12px 0 0"><a class="btn danger full" href="{{ url_for('cancel', res_id=r.id) }}">Cancel this tee time</a></p>
    {% endif %}
  </div>
  {% endfor %}

  <details>
    <summary>Recent activity</summary>
    <pre>{{ log }}</pre>
  </details>
</div></body></html>
""")

CONFIRM_HTML = """
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta name=color-scheme content=light>
<title>Confirm</title><style>""" + BASE_CSS + """</style></head>
<body>""" + HEADER.replace("{% if signout %}", "{% if False %}") + """
<div class=wrap>
  <div class=card>
    <div class=label>You said</div>
    <div class="big serif" style="margin-top:4px">“{{ text }}”</div>
  </div>

  {% if cmd.action == 'unknown' %}
    <div class=flash>{{ cmd.note }}</div>
    <a class="btn ghost full" href="{{ url_for('home') }}">Back</a>

  {% elif cmd.action == 'cancel' %}
    {% if matched %}
      <div class=card>
        <div class=label>This will cancel — cannot be undone</div>
        {% for r in matched %}<div class="when serif" style="margin-top:6px">{{ r.date_label }}</div>
          <div class=meta>{{ r.time_label }} · {{ r.players }} golfer{{ '' if r.players == 1 else 's' }}</div>{% endfor %}
      </div>
    {% endif %}
    {% if unmatched %}<div class=flash>No active reservation found on: {{ unmatched|join(', ') }}.</div>{% endif %}
    {% if matched %}
      <form method=post action="{{ url_for('command_apply') }}">
        <input type=hidden name=action value=cancel><input type=hidden name=ids_csv value="{{ ids_csv }}">
        <button class="danger full" type=submit>Yes, cancel</button>
        <p style="margin:10px 0 0"><a class="btn ghost full" href="{{ url_for('home') }}">No, go back</a></p>
      </form>
    {% else %}
      <a class="btn ghost full" href="{{ url_for('home') }}">Back</a>
    {% endif %}

  {% elif cmd.action in ['skip', 'unskip'] %}
    <div class=card>
      <div class=label>{{ 'Stop auto-booking' if cmd.action=='skip' else 'Resume auto-booking' }}</div>
      <div class=chips style="margin-top:6px">{% for p in dates_pretty %}<span class=chip style="background:#eee">{{ p }}</span>{% endfor %}</div>
    </div>
    <form method=post action="{{ url_for('command_apply') }}">
      <input type=hidden name=action value="{{ cmd.action }}"><input type=hidden name=dates_csv value="{{ dates_csv }}">
      <button class="full" type=submit>Confirm</button>
      <p style="margin:10px 0 0"><a class="btn ghost full" href="{{ url_for('home') }}">Cancel</a></p>
    </form>

  {% else %}{# pause / arm #}
    <div class=card><div class="big serif">{{ 'Pause the auto-booker entirely?' if cmd.action=='pause' else 'Resume / arm the auto-booker?' }}</div></div>
    <form method=post action="{{ url_for('command_apply') }}">
      <input type=hidden name=action value="{{ cmd.action }}">
      <button class="full {{ 'danger' if cmd.action=='pause' else 'brass' }}" type=submit>Confirm</button>
      <p style="margin:10px 0 0"><a class="btn ghost full" href="{{ url_for('home') }}">Cancel</a></p>
    </form>
  {% endif %}
</div></body></html>
"""


CANCEL_HTML = """
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><meta name=color-scheme content=light>
<title>Cancel tee time</title><style>""" + BASE_CSS + """
  select { font:inherit; font-size:16px; padding:13px; width:100%; border-radius:11px;
           border:1.5px solid #cdbf99; background:#fffdf7; color:#23362b; }
</style></head>
<body>""" + HEADER.replace("{% if signout %}", "{% if False %}") + """
<div class=wrap>
  <div class="card tee">
    <div class=label>Cancel this tee time?</div>
    <div class="when serif" style="margin-top:4px">{{ r.date_label }} · {{ r.time_label }}</div>
    <div class=meta>{{ r.players }} golfer{{ '' if r.players == 1 else 's' }} · #{{ r.confirmation }}</div>
  </div>

  <form method=post action="{{ url_for('cancel', res_id=r.id) }}">
    {% if total > 1 %}
      <div class=card>
        <div class=label style="margin-bottom:8px">How many players to cancel?</div>
        <select name=players_to_cancel>
          {% for n in options %}
            <option value="{{ n }}" {{ 'selected' if n == total else '' }}>{{ n }}{{ ' (whole booking)' if n == total else '' }}</option>
          {% endfor %}
        </select>
      </div>
    {% else %}
      <input type=hidden name=players_to_cancel value=1>
    {% endif %}
    <button class="danger full" type=submit>Confirm cancellation</button>
    <p style="margin:10px 0 0"><a class="btn ghost full" href="{{ url_for('home') }}">Keep it</a></p>
  </form>

  <div class=muted style="margin-top:14px">Can't be undone. PCC charges the full amount if you cancel within 4 hours of the tee time.</div>
</div></body></html>
"""


if __name__ == "__main__":
    if not PASSWORD:
        raise SystemExit("DASHBOARD_PASSWORD not set. Create .dashboard.env (see header).")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
