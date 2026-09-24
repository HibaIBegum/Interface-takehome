import re
import time

import pytest

from mock_app import create_app


@pytest.fixture
def client():
    app = create_app(username="teller", password="pw")
    return app.test_client()


def login(client):
    resp = client.post("/login", data={"uid": "teller", "pwd": "pw"})
    assert resp.status_code == 302 and resp.location.endswith("/desk")


def arm(client, fault, count=1, **extra):
    resp = client.post("/__faults", json={"fault": fault, "count": count, **extra})
    assert resp.status_code == 200, resp.get_json()


def open_draft(client, mid="100234", typ="SHR", nick="Vacation", amt="250.00"):
    resp = client.post("/app/subacct/new", data={"m": mid, "typ": typ, "nick": nick, "amt": amt})
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return resp.location.split("t=")[1]


# ---- auth ----

def test_content_requires_login(client):
    resp = client.get("/app/search")
    assert resp.status_code == 302 and "/login" in resp.location


def test_bad_credentials(client):
    resp = client.post("/login", data={"uid": "teller", "pwd": "nope"})
    assert "Invalid User ID or Password." in resp.get_data(as_text=True)


def test_create_app_requires_credentials(monkeypatch):
    monkeypatch.delenv("MOCK_USERNAME", raising=False)
    monkeypatch.delenv("MOCK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        create_app()


# ---- happy path ----

def test_full_flow(client):
    login(client)
    assert b"<frameset" in client.get("/desk").data

    resp = client.post("/app/search", data={"mid": "100234"})
    assert resp.status_code == 302
    detail = client.get(resp.location).get_data(as_text=True)
    assert "Jane Q. Testmember" in detail and "***-**-0001" in detail and "$5,230.17" in detail

    token = open_draft(client)
    review = client.get(f"/app/subacct/review?t={token}").get_data(as_text=True)
    assert "Share Savings" in review and "$250.00" in review

    resp = client.post("/app/subacct/submit", data={"t": token})
    assert resp.status_code == 302
    confirm = client.get(resp.location).get_data(as_text=True)
    assert re.search(r"Reference Number:.*SA-\d{6}", confirm)

    detail = client.get("/app/member?m=100234").get_data(as_text=True)
    assert "Vacation" in detail and "$4,980.17" in detail


def test_resubmit_is_idempotent(client):
    login(client)
    token = open_draft(client)
    first = client.post("/app/subacct/submit", data={"t": token}).location
    second = client.post("/app/subacct/submit", data={"t": token}).location
    assert first.split("&")[0] == second.split("&")[0]
    assert "already been submitted" in client.get(second).get_data(as_text=True)
    assert client.get("/app/member?m=100234").get_data(as_text=True).count("Vacation") == 1


# ---- natural errors ----

def test_unknown_member(client):
    login(client)
    body = client.post("/app/search", data={"mid": "999999"}).get_data(as_text=True)
    assert "No member found for Member ID 999999." in body


def test_restricted_member(client):
    login(client)
    resp = client.get("/app/member?m=100237")
    assert resp.status_code == 403 and "Access Denied" in resp.get_data(as_text=True)
    assert client.get("/app/subacct/new?m=100237").status_code == 403


@pytest.mark.parametrize("typ,amt,message", [
    ("SHR", "abc", "must be a dollar amount"),
    ("SHR", "12.345", "must be a dollar amount"),
    ("SHR", "0", "greater than zero"),
    ("SHR", "999999", "Insufficient funds"),
    ("CD12", "500", "Minimum opening deposit"),
    ("", "10", "Please select a sub-account type."),
])
def test_invalid_deposit_inline_error(client, typ, amt, message):
    login(client)
    resp = client.post("/app/subacct/new", data={"m": "100234", "typ": typ, "nick": "X", "amt": amt})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Please correct the errors below." in body and message in body


# ---- faults ----

def test_server_error_fault_counts_down(client):
    login(client)
    arm(client, "server_error", count=2)
    assert client.get("/app/search").status_code == 500
    assert client.get("/app/search").status_code == 500
    assert client.get("/app/search").status_code == 200


def test_server_error_before_submit_commits_nothing(client):
    login(client)
    token = open_draft(client)
    arm(client, "server_error")
    assert client.post("/app/subacct/submit", data={"t": token}).status_code == 500
    assert "Vacation" not in client.get("/app/member?m=100234").get_data(as_text=True)


def test_session_timeout_fault(client):
    login(client)
    arm(client, "session_timeout")
    resp = client.get("/app/search")
    assert resp.status_code == 302 and "expired=1" in resp.location
    assert "Your session has expired" in client.get(resp.location).get_data(as_text=True)
    assert "/login" in client.get("/app/search").location  # session really gone


def test_interstitial_fault_only_on_get(client):
    login(client)
    arm(client, "interstitial")
    client.post("/app/search", data={"mid": "100234"})  # POST does not consume it
    assert "System Notice" in client.get("/app/search").get_data(as_text=True)
    assert "System Notice" not in client.get("/app/search").get_data(as_text=True)


def test_slow_load_fault(client):
    login(client)
    arm(client, "slow_load", delay_ms=300)
    start = time.monotonic()
    assert client.get("/app/search").status_code == 200
    assert time.monotonic() - start >= 0.3


def test_fault_endpoint_validates_and_is_local_only(client):
    assert client.post("/__faults", json={"fault": "meteor"}).status_code == 400
    assert client.post("/__faults", json={"fault": "slow_load", "bogus": 1}).status_code == 400
    remote = client.post("/__faults", json={"fault": "slow_load"}, environ_base={"REMOTE_ADDR": "10.1.2.3"})
    assert remote.status_code == 403
    arm(client, "server_error", count=3)
    assert client.get("/__faults").get_json() == {"armed": {"server_error": 3}}
    assert client.delete("/__faults").get_json() == {"armed": {}}


# ---- legacy markup contract ----

def test_markup_has_no_stable_ids(client):
    login(client)
    token = open_draft(client)
    pages = [
        client.get("/login"), client.get("/desk"), client.get("/hdr"), client.get("/app/search"),
        client.get("/app/member?m=100234"), client.get("/app/subacct/new?m=100234"),
        client.get(f"/app/subacct/review?t={token}"),
    ]
    for page in pages:
        html = page.get_data(as_text=True)
        assert not re.search(r"\sid\s*=", html) and "data-testid" not in html
