"""A fake golf-club portal, used only by the end-to-end tests.

It mimics the *shape* a real member portal presents — a login form, a tee sheet
with bookable slots (each advertising an allowed party size), and a one-click
booking that lands on a confirmation page — so the real `TeeBooker` automation
can be exercised against a live browser without touching any real club.

The CSS hooks here (#username, .teetime-slot, .book, .booking-confirmed, ...)
match the selectors the e2e config points at. Run standalone with:

    python mock_portal.py 8799
"""
from flask import Flask, request, redirect, session, render_template_string

app = Flask(__name__)
app.secret_key = "test-only-not-a-secret"

# In-memory record of bookings, so tests can assert the one-booking guarantee.
BOOKINGS = []

USERNAME = "member123"
PASSWORD = "hunter2"

# (start time, allowed party-size label) for each slot on the sheet.
SLOTS = [
    ("07:50 AM", "1 or 2"),
    ("08:00 AM", "1 - 4"),
    ("08:10 AM", "1 - 4"),
]

LOGIN_HTML = """
<!doctype html><title>Login</title>
<form method=post action="/login">
  <input id="username" name="username" placeholder="user">
  <input id="password" name="password" type="password" placeholder="pass">
  <button type="submit">Sign in</button>
</form>
"""

TEESHEET_HTML = """
<!doctype html><title>Tee Sheet</title>
<div class="member-dashboard">Logged in as {{ user }}</div>
<h1>Tee sheet for {{ date }}</h1>
{% for t, party in slots %}
  <div class="teetime-slot" data-time="{{ t }}">
    <span class="slot-time">{{ t }}</span>
    <span class="slot-party">{{ party }}</span>
    {% if t not in booked %}
      <form method=post action="/book" style="display:inline">
        <input type=hidden name=time value="{{ t }}">
        <button class="book" type="submit">Book</button>
      </form>
    {% else %}
      <span class="taken">BOOKED</span>
    {% endif %}
  </div>
{% endfor %}
"""

CONFIRM_HTML = """
<!doctype html><title>Confirmed</title>
<div class="booking-confirmed">
  Confirmation #{{ n }} — {{ time }} booked. Thanks, {{ user }}!
</div>
"""


@app.route("/count")
def count():
    """Test hook: how many bookings have been recorded so far."""
    return {"count": len(BOOKINGS), "bookings": BOOKINGS}


@app.route("/reset")
def reset():
    """Test hook: clear all bookings between test cases."""
    BOOKINGS.clear()
    return {"count": 0}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("username") == USERNAME and request.form.get("password") == PASSWORD:
            session["user"] = USERNAME
            return redirect("/teesheet?date=2026-07-09")
        return "Wrong credentials", 401
    return LOGIN_HTML


@app.route("/teesheet")
def teesheet():
    if "user" not in session:
        return redirect("/login")
    booked = {b["time"] for b in BOOKINGS}
    return render_template_string(
        TEESHEET_HTML, user=session["user"], date=request.args.get("date"),
        slots=SLOTS, booked=booked,
    )


@app.route("/book", methods=["POST"])
def book():
    if "user" not in session:
        return redirect("/login")
    t = request.form.get("time")
    BOOKINGS.append({"time": t, "user": session["user"]})
    return render_template_string(CONFIRM_HTML, n=len(BOOKINGS), time=t, user=session["user"])


if __name__ == "__main__":
    import sys
    app.run(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8799, debug=False)
