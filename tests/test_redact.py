"""Redactor unit tests, using the patterns shipped in config/policy.yaml."""

import pytest

from cua.policy.gate import PolicyConfig
from cua.policy.redact import Redactor


@pytest.fixture
def redactor():
    return PolicyConfig.load().redactor({"password": "Tr0ub4dor&3", "member_pin": "4821"})


@pytest.mark.parametrize("text,expected", [
    ("SSN 123-45-6789 on file", "SSN [REDACTED:ssn] on file"),
    ("SSN: ***-**-0001", "SSN: [REDACTED:ssn_masked]"),
    ("acct 000123456789 closed", "acct [REDACTED:account_number] closed"),
    ("Savings $5,230.17, Checking $812.40", "Savings [REDACTED:money], Checking [REDACTED:money]"),
    ("deposit $75 today", "deposit [REDACTED:money] today"),
    ("mail jane.q+ops@example.co.uk now", "mail [REDACTED:email] now"),
    ("call (555) 010-4477 or +1 555.010.4477", "call [REDACTED:phone] or [REDACTED:phone]"),
    ("key sk-ant-api03-AbCdEfGhIjKlMnOp", "key [REDACTED:api_key]"),
    ("Authorization: Bearer abcdefghijklmnop0123", "Authorization: [REDACTED:bearer_token]"),
    ("jwt eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "jwt [REDACTED:jwt]"),
    ("session 0123456789abcdef0123456789abcdef", "session [REDACTED:secret_hex]"),
    ("login password=letmein then", "login [REDACTED:password_assignment] then"),
    ("typed Tr0ub4dor&3 and PIN 4821", "typed [REDACTED:password] and PIN [REDACTED:member_pin]"),
])
def test_patterns_and_sensitive_params(redactor, text, expected):
    assert redactor.text(text) == expected


@pytest.mark.parametrize("text", [
    "member 100234",                         # member ids stay readable (6 digits)
    "run 20260928T013206-look-up-member",    # run ids / timestamps
    "state 40ca409d32e8f530",                # 16-hex state hash
    "/app/member?m=100234",
    "clicked at 640,380 in 1280x800",
    "Member Name: Jane Q. Testmember",
])
def test_operational_text_is_left_alone(redactor, text):
    assert redactor.text(text) == text


def test_structures_are_redacted_recursively_and_sensitive_keys_go_whole(redactor):
    data = {
        "note": "SSN 123-45-6789",
        "nested": [{"balance": "$1,000.00"}],
        "password": "anything at all",
        "token": "short",
        "count": 3,
        "flag": True,
        "Password": None,
    }
    assert redactor.obj(data) == {
        "note": "SSN [REDACTED:ssn]",
        "nested": [{"balance": "[REDACTED:money]"}],
        "password": "[REDACTED:password]",
        "token": "[REDACTED:token]",
        "count": 3,
        "flag": True,
        "Password": None,
    }


def test_patterns_run_before_secret_values():
    """A short secret must not break a longer pattern apart (it used to leak '123-[pin]5-6789')."""
    r = Redactor({"pin": "4"}, {"ssn": r"\b\d{3}-\d{2}-\d{4}\b"})
    assert r.text("ssn 123-45-6789") == "ssn [REDACTED:ssn]"


def test_longest_secret_wins_and_secrets_can_be_added():
    r = Redactor({"short": "abc", "long": "abcdef"})
    assert r.text("abcdef abc") == "[REDACTED:long] [REDACTED:short]"
    assert r.with_secrets({"otp": "999111"}).text("otp 999111 abc") == "otp [REDACTED:otp] [REDACTED:short]"
