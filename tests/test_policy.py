"""Policy gate, allowlist and risk classification (no browser)."""

import pytest

from conftest import RecordingApprover
from cua.policy.allowlist import url_allowed
from cua.policy.gate import PolicyConfig, PolicyGate, Verdict
from cua.policy.risk import Risk, classify
from cua.surface.base import Click, Extract, Fill, LocatorCandidate, Navigate, Press, Strategy, Target

APP = "http://127.0.0.1:5055"
REVIEW = f"{APP}/app/subacct/review?t=abc"
SEARCH = f"{APP}/app/search"


def button(name: str, frame=("main",)) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=Strategy.ROLE_NAME, role="button", value=name,
                                               frame_path=list(frame))], description=f"button {name!r}")


@pytest.fixture
def config():
    return PolicyConfig.load()


def risk_of(config, action, frame_url=SEARCH):
    return classify(action, frame_url=frame_url, markers=config.irreversible_markers,
                    commit_keywords=config.commit_keywords)[0]


# ---------------------------------------------------------------- allowlist

@pytest.mark.parametrize("url,allowed", [
    (f"{APP}/app/member?m=1", True),
    ("http://localhost:8080/login", True),
    ("https://127.0.0.1:5055/login", False),     # scheme is part of the origin
    ("https://example.com/app/search", False),
    (f"{APP}/__faults", False),                   # same origin, route not allowlisted
    (f"{APP}/admin", False),
    ("javascript:alert(1)", False),
    ("file:///etc/passwd", False),
])
def test_url_allowlist(config, url, allowed):
    assert url_allowed(url, config.allowed_origins, config.allowed_routes)[0] is allowed


def test_relative_navigation_resolves_against_the_base_url(config):
    gate = PolicyGate(config, base_url=APP)
    assert gate.check(Navigate(url="/login"), page_url="", frame_url=None).verdict is Verdict.ALLOW
    blocked = gate.check(Navigate(url="/__reset"), page_url="", frame_url=None)
    assert blocked.verdict is Verdict.BLOCK and "route /__reset" in blocked.reason


def test_action_on_a_non_allowlisted_frame_is_blocked(config):
    gate = PolicyGate(config)
    decision = gate.check(Click(target=button("Search")), page_url=f"{APP}/desk",
                          frame_url="https://evil.example/phish")
    assert decision.verdict is Verdict.BLOCK and decision.reason.startswith("frame not allowlisted")


def test_disallowed_action_types_are_blocked(config):
    gate = PolicyGate(config.model_copy(update={"allowed_actions": ["click", "extract"]}))
    decision = gate.check(Press(key="Tab"), page_url=SEARCH, frame_url=None)
    assert decision.verdict is Verdict.BLOCK and "'press' is not allowed" in decision.reason


def test_nothing_but_navigation_before_a_page_is_loaded(config):
    assert PolicyGate(config).check(Click(target=button("Search")), page_url="about:blank",
                                    frame_url=None).verdict is Verdict.BLOCK


# ---------------------------------------------------------------- risk

@pytest.mark.parametrize("action,frame_url,risk", [
    (Extract(target=button("x")), SEARCH, Risk.READ),
    (Navigate(url="/app/search"), SEARCH, Risk.NAVIGATE),
    (Fill(target=button("x"), value="1"), SEARCH, Risk.INPUT),
    (Click(target=button("Search")), SEARCH, Risk.INPUT),
    (Click(target=button("Submit")), REVIEW, Risk.COMMIT),          # explicit marker
    (Click(target=button("Confirm Transfer")), SEARCH, Risk.COMMIT),  # keyword fallback
    (Click(target=button("Submitted items")), SEARCH, Risk.INPUT),    # whole words only
    (Press(key="Enter"), SEARCH, Risk.COMMIT),
    (Press(key="Tab"), SEARCH, Risk.INPUT),
])
def test_risk_classification(config, action, frame_url, risk):
    assert risk_of(config, action, frame_url) is risk


def test_markers_apply_even_without_keywords(config):
    no_keywords = config.model_copy(update={"commit_keywords": []})
    assert risk_of(no_keywords, Click(target=button("Submit")), REVIEW) is Risk.COMMIT
    assert risk_of(no_keywords, Click(target=button("Submit")), SEARCH) is Risk.INPUT


# ---------------------------------------------------------------- approval

def test_irreversible_actions_are_held_for_an_approver(config):
    submit = Click(target=button("Submit"))
    assert PolicyGate(config).check(submit, page_url=f"{APP}/desk", frame_url=REVIEW).verdict is Verdict.DENY

    operator = RecordingApprover(True)
    decision = PolicyGate(config, approver=operator).check(submit, page_url=f"{APP}/desk", frame_url=REVIEW,
                                                           context="replay of x, step s12")
    assert decision.verdict is Verdict.ALLOW and decision.approval.by_human
    [request] = operator.requests
    assert (request.page, request.context) == ("/app/subacct/review", "replay of x, step s12")


def test_a_human_is_required_unless_waived(config):
    auto = RecordingApprover(True, human=False)
    submit = Click(target=button("Submit"))
    gate = PolicyGate(config, approver=auto)
    assert gate.check(submit, page_url=SEARCH, frame_url=REVIEW).verdict is Verdict.DENY
    assert gate.check(submit, page_url=SEARCH, frame_url=REVIEW, require_human=False).verdict is Verdict.ALLOW


def test_the_artifact_can_only_raise_risk(config):
    gate = PolicyGate(config)  # deny-all approver
    decision = gate.check(Click(target=button("Search")), page_url=SEARCH, frame_url=SEARCH, irreversible=True)
    assert decision.verdict is Verdict.DENY and decision.risk is Risk.COMMIT


def test_approval_requests_are_redacted(config):
    operator = RecordingApprover(True)
    gate = PolicyGate(config, approver=operator, redactor=config.redactor({"password": "hunter2hunter2"}))
    gate.check(Fill(target=button("Confirm Transfer"), value="to 123-45-6789, pw hunter2hunter2"),
               page_url=SEARCH, frame_url=SEARCH, irreversible=True, context="acct 1234567890")
    [request] = operator.requests
    assert request.value == "to [REDACTED:ssn], pw [REDACTED:password]"
    assert request.context == "acct [REDACTED:account_number]"
    secret_fill = Fill(target=button("PIN"), value="4321", sensitive=True)
    gate.check(secret_fill, page_url=SEARCH, frame_url=SEARCH, irreversible=True)
    assert operator.requests[-1].value == "[secret]"


def test_denied_action_never_reaches_the_surface(config):
    class ExplodingSurface:
        def current_url(self):
            return f"{APP}/desk"

        def frame_url(self, frame_path):
            return REVIEW

        def act(self, action):
            raise AssertionError("surface must not be touched")

    for gate in (PolicyGate(config), PolicyGate(config, approver=RecordingApprover(True, human=False))):
        gated = gate.execute(ExplodingSurface(), Click(target=button("Submit")))
        assert gated.result is None and gated.decision.verdict is Verdict.DENY
