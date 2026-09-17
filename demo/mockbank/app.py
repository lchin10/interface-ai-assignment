"""Mock legacy core-banking teller app used as the automation target.

Deliberately hostile to automation, like the real long tail: a frameset, table
layouts, labels in neighbouring <td>s, no ids or test ids, server-rendered forms.
All members, balances and tax ids are fake.

Faults (comma list via --faults / MOCKBANK_FAULTS, or create_app(faults=...)):
  slow            member + review pages take ~1.5s
  notice          one "System Notice" interstitial per session before member detail
  notice_always   the interstitial comes back every time (recovery never succeeds)
  session_expiry  the first member lookup finds the session expired
  server_error    member detail returns HTTP 500
  relabel         "Member ID:" is relabelled "Member #:" (a tenant/version variant)
"""
from __future__ import annotations

import argparse
import os
import random
import secrets
import time
from decimal import Decimal, InvalidOperation

from flask import Flask, redirect, request, session
from markupsafe import escape

from automation import load_env

FAULTS = {"slow", "notice", "notice_always", "session_expiry", "server_error", "relabel"}

MEMBERS = {
    "10001": {
        "name": "Jane Q. Sample",
        "since": "2014-03-02",
        "tax_id": "123-45-6789",
        "accounts": [("S01", "Savings", "12,450.33"), ("C01", "Checking", "1,203.10")],
    },
    "10002": {
        "name": "John R. Example",
        "since": "2019-11-20",
        "tax_id": "987-65-4320",
        "accounts": [("S01", "Savings", "8,000.00"), ("C01", "Checking", "310.75")],
    },
}
RESTRICTED = {"40300"}
DEPOSIT_MAX = Decimal("10000.00")
DEPOSIT_MIN = Decimal("0.01")


def _page(title: str, body: str) -> str:
    return (
        f"<html><head><title>{title}</title></head>"
        "<body bgcolor='#e8e8d0'><font face='Arial' size='2'>"
        f"{body}</font></body></html>"
    )


def _panel(heading: str, inner: str) -> str:
    return (
        "<table width='100%' cellpadding='3' cellspacing='0' border='0'>"
        f"<tr><td bgcolor='#336699'><font color='white' size='3'><b>{heading}</b></font></td></tr>"
        f"<tr><td>{inner}</td></tr></table>"
    )


def _error(text: str) -> str:
    return f"<br><font color='red'>{text}</font>" if text else ""


def create_app(
    faults: tuple[str, ...] | list[str] = (),
    seed: int = 0,
    user: str | None = None,
    password: str | None = None,
    slow_seconds: float = 1.5,
) -> Flask:
    faults = set(faults)
    unknown = faults - FAULTS
    if unknown:
        raise ValueError(f"unknown faults: {sorted(unknown)}")

    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    user = user or os.environ.get("MOCKBANK_USER", "teller01")
    password = password or os.environ.get("MOCKBANK_PASS", "mock-only-password")
    state = {"expired_once": False, "rng": random.Random(seed)}
    member_label = "Member #:" if "relabel" in faults else "Member ID:"

    def signed_in() -> bool:
        return session.get("user") == user

    def login_page(message: str = "") -> str:
        form = (
            "<form method='post' action='/login' target='_top'>"
            "<table cellpadding='3'>"
            "<tr><td>User ID:</td><td><input type='text' name='u' size='16'></td></tr>"
            "<tr><td>Password:</td><td><input type='password' name='p' size='16'></td></tr>"
            "<tr><td></td><td><input type='submit' value='Sign On'></td></tr>"
            "</table></form>"
        )
        return _page("CoreOne Teller - Sign On", _panel("Sign On", _error(message) + form))

    def expired() -> str:
        return login_page("Your session has expired or you are not signed on. Please sign on again.")

    def search_page(error: str = "") -> str:
        form = (
            "<form method='get' action='/member'><table cellpadding='3'>"
            f"<tr><td>{member_label}</td><td><input type='text' name='mid' size='10'></td></tr>"
            "<tr><td></td><td><input type='submit' value='Search'></td></tr>"
            "</table></form>"
        )
        return _page("Member Search", _panel("Member Search", form + _error(error)))

    def open_form(mid: str, error: str = "") -> str:
        form = (
            "<form method='post' action='/open/review'>"
            f"<input type='hidden' name='mid' value='{escape(mid)}'><table cellpadding='3'>"
            "<tr><td>Account Type:</td><td><select name='type'>"
            "<option value='savings'>Savings</option><option value='checking'>Checking</option>"
            "</select></td></tr>"
            "<tr><td>Initial Deposit:</td><td><input type='text' name='amt' size='12'></td></tr>"
            "<tr><td>Nickname:</td><td><input type='text' name='nick' size='20'></td></tr>"
            "<tr><td></td><td><input type='submit' value='Continue'></td></tr>"
            "</table></form>"
        )
        return _page("Open Sub-Account", _panel("Open Sub-Account", _error(error) + form))

    @app.before_request
    def maybe_slow():
        if "slow" in faults and request.path in ("/member", "/open/review"):
            time.sleep(slow_seconds)

    @app.get("/login")
    def login_get():
        return login_page()

    @app.post("/login")
    def login_post():
        if request.form.get("u") == user and request.form.get("p") == password:
            session.clear()
            session["user"] = user
            return redirect("/")
        return login_page("Invalid user ID or password.")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/")
    def home():
        if not signed_in():
            return redirect("/login")
        return (
            "<html><head><title>CoreOne Teller</title></head>"
            "<frameset cols='190,*'><frame name='nav' src='/nav'><frame name='main' src='/search'></frameset>"
            "</html>"
        )

    @app.get("/nav")
    def nav():
        if not signed_in():
            return expired()
        links = (
            "<table cellpadding='4'>"
            "<tr><td><a href='/search' target='main'>Member Search</a></td></tr>"
            "<tr><td><a href='/logout' target='_top'>Sign Off</a></td></tr></table>"
        )
        return _page("Menu", _panel("CoreOne", links))

    @app.get("/search")
    def search():
        return search_page() if signed_in() else expired()

    @app.get("/member")
    def member():
        if not signed_in():
            return expired()
        if "session_expiry" in faults and not state["expired_once"]:
            state["expired_once"] = True
            session.clear()
            return expired()
        if "notice_always" in faults or ("notice" in faults and not session.get("notice_seen")):
            body = (
                "<p>Scheduled maintenance will occur tonight from 11:00 PM to 1:00 AM.</p>"
                "<form method='post' action='/notice/ack'>"
                f"<input type='hidden' name='next' value='{escape(request.full_path)}'>"
                "<input type='submit' value='OK'></form>"
            )
            return _page("System Notice", _panel("System Notice", body))
        if "server_error" in faults:
            body = "<p>The server encountered an unexpected condition. Reference ERR-5XX.</p>"
            return _page("Error", _panel("HTTP 500 - Internal Server Error", body)), 500
        mid = request.args.get("mid", "").strip()
        if mid in RESTRICTED:
            body = "<p>Insufficient privileges to view this member record.</p>"
            return _page("Access Denied", _panel("Access Denied", body))
        m = MEMBERS.get(mid)
        if m is None:
            return search_page("No member found matching the search criteria.")
        rows = "".join(
            f"<tr><td>{acct}</td><td>{kind}</td><td align='right'>{bal}</td></tr>"
            for acct, kind, bal in m["accounts"]
        )
        inner = (
            "<table cellpadding='3'>"
            f"<tr><td>Name:</td><td>{m['name']}</td></tr>"
            f"<tr><td>Member Since:</td><td>{m['since']}</td></tr>"
            f"<tr><td>Tax ID:</td><td>{m['tax_id']}</td></tr></table>"
            "<br><table border='1' cellpadding='3' cellspacing='0'>"
            f"<tr><th>Account</th><th>Type</th><th>Balance</th></tr>{rows}</table><br>"
            f"<form method='get' action='/open'><input type='hidden' name='mid' value='{escape(mid)}'>"
            "<input type='submit' value='Open Sub-Account'></form>"
        )
        return _page("Member Detail", _panel("Member Detail", inner))

    @app.post("/notice/ack")
    def notice_ack():
        if not signed_in():
            return expired()
        session["notice_seen"] = True
        nxt = request.form.get("next", "/search")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/search")

    @app.get("/open")
    def open_get():
        if not signed_in():
            return expired()
        mid = request.args.get("mid", "")
        if mid not in MEMBERS:
            return search_page("No member found matching the search criteria.")
        return open_form(mid)

    @app.post("/open/review")
    def open_review():
        if not signed_in():
            return expired()
        mid, kind = request.form.get("mid", ""), request.form.get("type", "")
        if mid not in MEMBERS:
            return search_page("No member found matching the search criteria.")
        try:
            amount = Decimal(request.form.get("amt", "").replace(",", ""))
        except InvalidOperation:
            amount = Decimal("-1")
        if not DEPOSIT_MIN <= amount <= DEPOSIT_MAX or kind not in ("savings", "checking"):
            return open_form(mid, "Validation error: Initial deposit must be between 0.01 and 10,000.00.")
        nick = escape(request.form.get("nick", ""))
        hidden = "".join(
            f"<input type='hidden' name='{k}' value='{escape(v)}'>"
            for k, v in (("mid", mid), ("type", kind), ("amt", f"{amount:.2f}"), ("nick", nick))
        )
        inner = (
            "<table cellpadding='3'>"
            f"<tr><td>Account Type:</td><td>{kind.title()}</td></tr>"
            f"<tr><td>Initial Deposit:</td><td>{amount:,.2f}</td></tr>"
            f"<tr><td>Nickname:</td><td>{nick}</td></tr></table>"
            f"<form method='post' action='/open/confirm'>{hidden}"
            "<input type='submit' value='Confirm'> <a href='/search'>Cancel</a></form>"
        )
        return _page("Review Sub-Account", _panel("Review Sub-Account", inner))

    @app.post("/open/confirm")
    def open_confirm():
        if not signed_in():
            return expired()
        if request.form.get("mid", "") not in MEMBERS:
            return search_page("No member found matching the search criteria.")
        number = str(state["rng"].randrange(10**9, 10**10))
        inner = (
            "<p>Account opened successfully.</p><table cellpadding='3'>"
            f"<tr><td>New Account Number:</td><td>{number}</td></tr>"
            f"<tr><td>Account Type:</td><td>{escape(request.form.get('type', '')).title()}</td></tr></table>"
        )
        return _page("Account Opened", _panel("Account Opened", inner))

    return app


def main() -> None:
    load_env()  # same MOCKBANK_USER / MOCKBANK_PASS the automation reads
    p = argparse.ArgumentParser(description="Run the mock legacy core-banking app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5001)
    p.add_argument("--faults", default=os.environ.get("MOCKBANK_FAULTS", ""))
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()
    faults = [f for f in args.faults.split(",") if f]
    create_app(faults=faults, seed=args.seed).run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
