import pytest

from cua.policy.gate import PolicyConfig, PolicyGate, Verdict
from cua.policy.redact import Redactor
from cua.policy.risk import Risk, classify
from cua.surface.base import Click, Extract, Fill, LocatorCandidate, Navigate, Press, Strategy, Target

APP = "http://127.0.0.1:5055/desk"


def button(name: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=Strategy.ROLE_NAME, role="button", value=name)])


@pytest.fixture
def config():
    return PolicyConfig.load()


@pytest.mark.parametrize("action,risk", [
    (Extract(target=button("x")), Risk.READ),
    (Navigate(url="/app/search"), Risk.NAVIGATE),
    (Fill(target=button("x"), value="1"), Risk.INPUT),
    (Click(target=button("Search")), Risk.INPUT),
    (Click(target=button("Submit")), Risk.COMMIT),
    (Click(target=button("Confirm Transfer")), Risk.COMMIT),
    (Click(target=button("Submitted items")), Risk.INPUT),  # whole words only
    (Press(key="Enter"), Risk.COMMIT),
    (Press(key="Tab"), Risk.INPUT),
])
def test_risk_classification(config, action, risk):
    assert classify(action, config.commit_keywords) is risk


def test_gate_host_allowlist(config):
    gate = PolicyGate(config, base_url="http://127.0.0.1:5055")
    assert gate.check(Navigate(url="/login"), "").verdict is Verdict.ALLOW
    assert gate.check(Navigate(url="https://evil.example/"), APP).verdict is Verdict.DENY
    assert gate.check(Click(target=button("Search")), "https://evil.example/").verdict is Verdict.DENY
    assert gate.check(Click(target=button("Search")), "about:blank").verdict is Verdict.DENY


def test_gate_commit_needs_approval(config):
    submit = Click(target=button("Submit"))
    assert PolicyGate(config).check(submit, APP).verdict is Verdict.DENY
    assert PolicyGate(config, approver=lambda a, r: False).check(submit, APP).verdict is Verdict.DENY
    approved = PolicyGate(config, approver=lambda a, r: True).check(submit, APP)
    assert approved.verdict is Verdict.ALLOW and approved.approved


def test_denied_action_never_reaches_the_surface(config):
    class ExplodingSurface:
        def act(self, action):
            raise AssertionError("surface must not be touched")

    gated = PolicyGate(config).execute(ExplodingSurface(), Click(target=button("Submit")), APP)
    assert gated.result is None and gated.decision.verdict is Verdict.DENY


def test_redactor_replaces_secrets_and_patterns_recursively():
    r = Redactor({"password": "hunter2", "pin": "4"}, {"ssn": r"\b\d{3}-\d{2}-\d{4}\b"})
    data = {"msg": "pw hunter2, ssn 123-45-6789", "nested": [{"v": "hunter2"}], "n": 5, "hunter2": "key kept"}
    assert r.obj(data) == {
        "msg": "pw [REDACTED:password], ssn [REDACTED:ssn]",
        "nested": [{"v": "[REDACTED:password]"}],
        "n": 5,
        "hunter2": "key kept",
    }
    assert r.with_secrets({"token": "abc123"}).text("abc123 hunter2") == "[REDACTED:token] [REDACTED:password]"
