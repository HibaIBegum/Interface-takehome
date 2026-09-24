"""Flask routes for the mock back office.

Layout: /login (top level) -> /desk (frameset) with frames /hdr and /app/* (main content).
Faults only fire on /app/* requests, i.e. inside the main content frame.
"""

from __future__ import annotations

import hmac
import os
import time
from decimal import Decimal

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from pydantic import ValidationError

from .data import SUBACCOUNT_TYPES, Bank, InsufficientFunds, Member, validate_subaccount
from .faults import Fault, FaultInjector, FaultRequest

_LOCAL_ADDRS = {"127.0.0.1", "::1"}
_AUTHED_PREFIXES = ("/app/", "/desk", "/hdr")


def create_app(*, username: str | None = None, password: str | None = None) -> Flask:
    username = username or os.environ.get("MOCK_USERNAME")
    password = password or os.environ.get("MOCK_PASSWORD")
    if not username or not password:
        raise RuntimeError("MOCK_USERNAME and MOCK_PASSWORD must be set in the environment")

    app = Flask(__name__)
    app.secret_key = os.urandom(32)
    bank = Bank()
    faults = FaultInjector()
    app.extensions["bank"] = bank
    app.extensions["faults"] = faults

    @app.template_filter("money")
    def money(value: Decimal) -> str:
        return f"${value:,.2f}"

    @app.before_request
    def gate():
        path = request.path
        if path.startswith(_AUTHED_PREFIXES) and "user" not in session:
            return redirect(url_for("login"))
        if not path.startswith("/app/"):
            return None
        fault = faults.consume(request.method)
        if fault is Fault.SLOW_LOAD:
            time.sleep(faults.delay_s)
        elif fault is Fault.SESSION_TIMEOUT:
            session.clear()
            return redirect(url_for("login", expired=1))
        elif fault is Fault.SERVER_ERROR:
            return render_template("error500.html"), 500
        elif fault is Fault.INTERSTITIAL:
            g.interstitial = True
        return None

    # ---- control endpoints (local only, no auth, never faulted) ----

    def _local_only():
        if request.remote_addr not in _LOCAL_ADDRS:
            return jsonify(error="control endpoints are local-only"), 403
        return None

    @app.route("/__faults", methods=["GET", "POST", "DELETE"])
    def control_faults():
        if (denied := _local_only()) is not None:
            return denied
        if request.method == "POST":
            try:
                faults.arm(FaultRequest.model_validate(request.get_json(silent=True) or {}))
            except ValidationError as exc:
                return jsonify(error=exc.errors(include_url=False, include_context=False)), 400
        elif request.method == "DELETE":
            faults.clear()
        return jsonify(armed=faults.snapshot())

    @app.post("/__reset")
    def control_reset():
        if (denied := _local_only()) is not None:
            return denied
        bank.reset()
        faults.clear()
        return jsonify(ok=True)

    # ---- sign on / shell ----

    @app.get("/")
    def root():
        return redirect(url_for("desk"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            uid_ok = hmac.compare_digest(request.form.get("uid", ""), username)
            pwd_ok = hmac.compare_digest(request.form.get("pwd", ""), password)
            if uid_ok and pwd_ok:
                session.clear()
                session["user"] = username
                return redirect(url_for("desk"))
            error = "Invalid User ID or Password."
        return render_template("login.html", error=error, expired=request.args.get("expired") == "1")

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/desk")
    def desk():
        return render_template("desk.html")

    @app.get("/hdr")
    def hdr():
        return render_template("hdr.html", user=session["user"])

    # ---- main content frame ----

    def _load_member(member_id: str) -> tuple[Member | None, object]:
        """Resolve a member or return the page to show instead."""
        member = bank.get(member_id)
        if member is None:
            return None, render_template("search.html", mid=member_id, not_found=True)
        if member.restricted:
            return None, (render_template(
                "message.html",
                heading="Access Denied",
                text="You are not authorized to access this member record. (Code R-17) "
                     "Contact your supervisor if you believe this is an error.",
            ), 403)
        return member, None

    @app.route("/app/search", methods=["GET", "POST"])
    def search():
        if request.method == "GET":
            return render_template("search.html", mid="", not_found=False)
        mid = request.form.get("mid", "").strip()
        if bank.get(mid) is None:
            return render_template("search.html", mid=mid, not_found=True)
        return redirect(url_for("member", m=mid))

    @app.get("/app/member")
    def member():
        m, alt = _load_member(request.args.get("m", ""))
        if m is None:
            return alt
        return render_template("member.html", m=m)

    @app.route("/app/subacct/new", methods=["GET", "POST"])
    def subacct_new():
        source = request.form if request.method == "POST" else request.args
        m, alt = _load_member(source.get("m", ""))
        if m is None:
            return alt
        values = {"typ": "", "nick": "", "amt": ""}
        errors: dict[str, str] = {}
        if request.method == "POST":
            values = {k: request.form.get(k, "").strip() for k in values}
            amount, errors = validate_subaccount(m, values["typ"], values["nick"], values["amt"])
            if not errors:
                draft = bank.create_draft(m.member_id, values["typ"], values["nick"], amount)
                return redirect(url_for("subacct_review", t=draft.token))
        return render_template("subacct_form.html", m=m, types=SUBACCOUNT_TYPES, v=values, errors=errors)

    def _stale_request():
        return render_template(
            "message.html",
            heading="Request Not Available",
            text="This request is no longer available. Please start again from Member Search.",
        ), 400

    @app.get("/app/subacct/review")
    def subacct_review():
        draft = bank.drafts.get(request.args.get("t", ""))
        if draft is None:
            return _stale_request()
        return render_template("review.html", d=draft, m=bank.members[draft.member_id], error=None)

    @app.post("/app/subacct/submit")
    def subacct_submit():
        token = request.form.get("t", "")
        try:
            receipt, duplicate = bank.submit(token)
        except InsufficientFunds:
            draft = bank.drafts[token]
            return render_template(
                "review.html", d=draft, m=bank.members[draft.member_id],
                error="Insufficient funds: Savings balance has changed. Please start again.",
            )
        if receipt is None:
            return _stale_request()
        return redirect(url_for("subacct_done", ref=receipt.reference, dup=int(duplicate)))

    @app.get("/app/subacct/done")
    def subacct_done():
        receipt = bank.receipts.get(request.args.get("ref", ""))
        if receipt is None:
            return _stale_request()
        return render_template("confirm.html", r=receipt, duplicate=request.args.get("dup") == "1")

    return app
