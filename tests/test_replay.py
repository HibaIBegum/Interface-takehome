"""Deterministic replay against the live mock app, with faults injected through /__faults.

Artifacts are recorded once per module from scripted discovery runs (no LLM), then approved.
"""

import json

import pytest

from conftest import MOCK_PASSWORD, MOCK_USER, RecordingApprover, post_json
from cua.agent.loop import DiscoveryAgent, Limits, Outcome, Param
from cua.artifact.recorder import AppCatalog, record_run
from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyConfig, PolicyGate
from cua.replay import BusinessOutcome, Failure, FailureKind, ReplayConfig, Success, replay
from cua.replay.engine import approved_artifact_approver
from cua.surface.base import Navigate
from cua.surface.playwright_surface import PlaywrightSurface
from test_agent_loop import LOGIN, LOOKUP, PARAMS, ScriptedDecider, call, click, extract, fill, index_of

FAST = ReplayConfig(checkpoint_timeout_ms=1500, precondition_timeout_ms=1500)
CREDS = {"username": MOCK_USER, "password": MOCK_PASSWORD}


def _record(browser, mock_server, tmp_dir, script, params, *, approver=None, capability_id, name):
    post_json(f"{mock_server}/__reset")
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    try:
        surface = PlaywrightSurface(context.new_page(), base_url=mock_server, timeout_ms=3000)
        policy = PolicyConfig.load()
        log = RunLog(tmp_dir / capability_id, policy.redactor(), echo=False)
        agent = DiscoveryAgent(surface=surface, gate=PolicyGate(policy, approver=approver, base_url=mock_server),
                               decider=ScriptedDecider(script), log=log)
        result = agent.run(goal=name, entry_url=f"{mock_server}/login", params=params,
                           limits=Limits(max_steps=20, timeout_s=60))
        assert result.outcome is Outcome.DONE, result
        artifact = record_run(log.dir, capability_id=capability_id, name=name, catalog=AppCatalog.load())
    finally:
        context.close()
    approved = artifact.capability.model_copy(update={"status": "approved"})
    return artifact.model_copy(update={"capability": approved})


@pytest.fixture(scope="module")
def lookup_artifact(browser, mock_server, tmp_path_factory):
    script = LOGIN + LOOKUP + [extract("savings_balance", r"cell row 'Savings', column 'Balance'"),
                               call("done", summary="read")]
    return _record(browser, mock_server, tmp_path_factory.mktemp("rec"), script, PARAMS,
                   capability_id="lookup-savings-balance", name="look up member {member_id} savings balance")


@pytest.fixture(scope="module")
def open_subaccount_artifact(browser, mock_server, tmp_path_factory):
    params = PARAMS + [Param(name="nickname", value="Rainy Day"), Param(name="amount", value="75.00")]
    script = LOGIN + LOOKUP + [
        click(r"button 'Open Sub-Account'"),
        lambda s: ("select", {"element_index": index_of(s, r"label='Sub-Account Type'"),
                              "option": "Money Market", "reason": "type"}),
        fill(r"label='Nickname'", "{{nickname}}"),
        fill(r"label='Initial Deposit \(\$\)'", "{{amount}}"),
        click(r"button 'Continue'"),
        click(r"button 'Submit'"),
        extract("reference", r"cell row 'Reference Number'"),
        call("done", summary="opened"),
    ]
    return _record(browser, mock_server, tmp_path_factory.mktemp("rec"), script, params,
                   approver=RecordingApprover(True), capability_id="open-sub-account",
                   name="open a money market sub-account for member {member_id}")


@pytest.fixture
def run_replay(surface, mock_server, tmp_path):
    def run(artifact, params=None, *, approver=None, config=FAST):
        params = params if params is not None else {"member_id": "100234", **CREDS}
        approver = approver or approved_artifact_approver(artifact, fallback=RecordingApprover(False))
        policy = PolicyConfig.load()
        log = RunLog(tmp_path / "replay", policy.redactor({"password": MOCK_PASSWORD}), echo=False)
        result = replay(artifact, params, surface=surface, gate=PolicyGate(policy, approver=approver,
                                                                            base_url=mock_server),
                        log=log, config=config)
        lines = [json.loads(x) for x in (log.dir / "steps.jsonl").read_text().splitlines()] \
            if (log.dir / "steps.jsonl").exists() else []
        return result, lines, log.dir

    return run


def arm(mock_server, fault, **extra):
    post_json(f"{mock_server}/__faults", {"fault": fault, **extra})


# ---------------------------------------------------------------- acceptance

def test_success(run_replay, lookup_artifact):
    result, lines, run_dir = run_replay(lookup_artifact)
    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "$5,230.17"} and result.recoveries == []
    assert [x["step_id"] for x in lines if x["event"] == "step"] == [f"s0{i}" for i in range(1, 8)]
    assert json.loads((run_dir / "result.json").read_text())["status"] == "success"


def test_member_not_found_is_a_business_outcome(run_replay, lookup_artifact):
    result, _, _ = run_replay(lookup_artifact, {"member_id": "999999", **CREDS})
    assert isinstance(result, BusinessOutcome), result
    assert (result.code, result.step_id) == ("MEMBER_NOT_FOUND", "s06")


def test_recovers_from_session_timeout(run_replay, lookup_artifact, mock_server):
    arm(mock_server, "session_timeout")
    result, _, _ = run_replay(lookup_artifact)
    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "$5,230.17"}
    assert [(r.kind, r.signature) for r in result.recoveries] == [("reauthenticate", "session-expired")]
    assert "resuming at s05" in result.recoveries[0].detail


def test_recovers_from_interstitial(run_replay, lookup_artifact, mock_server):
    arm(mock_server, "interstitial")
    result, _, _ = run_replay(lookup_artifact)
    assert isinstance(result, Success), result
    assert [(r.kind, r.signature) for r in result.recoveries] == [("dismiss", "system-notice")]


def test_server_error_is_a_hard_failure_with_evidence(run_replay, lookup_artifact, mock_server):
    arm(mock_server, "server_error")
    result, _, _ = run_replay(lookup_artifact)
    assert isinstance(result, Failure), result
    assert (result.kind, result.code, result.step_id) == (FailureKind.HARD_FAILURE, "SERVER_ERROR", "s04")
    assert "`main` at `/app/search`" in result.expected and "Internal Server Error" in result.observed
    assert sorted(p.rsplit("-", 1)[-1] for p in result.evidence_paths) == ["aria.txt", "dom.html", "screen.png"]
    for path in result.evidence_paths:
        if not path.endswith(".png"):
            text = open(path).read()
            assert "Internal Server Error" in text and MOCK_PASSWORD not in text


# ---------------------------------------------------------------- more behaviour

def test_parameterized_for_another_member(run_replay, lookup_artifact):
    result, _, _ = run_replay(lookup_artifact, {"member_id": "100236", **CREDS})
    assert isinstance(result, Success) and result.outputs == {"savings_balance": "$18,400.00"}


def test_permission_denied_is_a_business_outcome(run_replay, lookup_artifact):
    result, _, _ = run_replay(lookup_artifact, {"member_id": "100237", **CREDS})
    assert isinstance(result, BusinessOutcome) and result.code == "PERMISSION_DENIED"


def test_slow_load_is_waited_out_with_backoff(run_replay, lookup_artifact, mock_server):
    arm(mock_server, "slow_load", delay_ms=4000)
    result, _, _ = run_replay(lookup_artifact)
    assert isinstance(result, Success), result
    assert [r.kind for r in result.recoveries] == ["slow_load_wait"]


def test_falls_back_to_the_next_locator_when_the_first_drifts(run_replay, lookup_artifact):
    step = lookup_artifact.steps[4]  # fill Member ID: label first
    ranked = step.target.candidates
    broken = ranked[0].model_copy(update={"candidate": ranked[0].candidate.model_copy(update={"value": "Member No."})})
    target = step.target.model_copy(update={"candidates": [broken] + ranked[1:]})
    steps = list(lookup_artifact.steps)
    steps[4] = step.model_copy(update={"target": target})
    result, lines, _ = run_replay(lookup_artifact.model_copy(update={"steps": steps}))
    assert isinstance(result, Success), result
    s05 = next(x for x in lines if x.get("step_id") == "s05")
    assert s05["locator_fallback"] and s05["result"]["resolution"]["matched_index"] == 1


def test_drafts_and_bad_params_are_refused_before_touching_the_app(run_replay, lookup_artifact, surface):
    draft = lookup_artifact.model_copy(update={"capability": lookup_artifact.capability.model_copy(
        update={"status": "draft"})})
    result, _, _ = run_replay(draft)
    assert isinstance(result, Failure) and result.kind is FailureKind.NOT_APPROVED
    result, _, _ = run_replay(lookup_artifact, {"member_id": "12ab", "username": MOCK_USER})
    assert isinstance(result, Failure) and result.kind is FailureKind.INVALID_PARAMS
    assert "member_id" in result.message and "missing parameter 'password'" in result.message
    assert "12ab" not in result.message
    assert surface.current_url() == "about:blank"


SUBACCOUNT = {"member_id": "100234", "nickname": "Rainy Day", "amount": "75.00", **CREDS}


def _as_draft(artifact):
    return artifact.model_copy(update={"capability": artifact.capability.model_copy(update={"status": "draft"})})


def test_approved_artifact_runs_its_irreversible_step(run_replay, open_subaccount_artifact):
    result, lines, _ = run_replay(open_subaccount_artifact, SUBACCOUNT)
    assert isinstance(result, Success), result
    assert result.outputs["reference"].startswith("SA-")
    submit = next(x for x in lines if x.get("event") == "step" and x["policy"]["risk"] == "commit")
    assert submit["policy"]["approval"]["by"] == "artifact open-sub-account v1.0.0 (approved)"


def test_irreversible_submit_is_held_for_human_approval(run_replay, open_subaccount_artifact, surface):
    """Acceptance: replaying a draft stops at Submit, asks a human, and does nothing when denied."""
    seen_while_held = []
    operator = RecordingApprover(False, on_request=lambda request: seen_while_held.append(
        surface.frame_url(["main"])))
    result, _, _ = run_replay(_as_draft(open_subaccount_artifact), SUBACCOUNT, approver=operator,
                              config=ReplayConfig(checkpoint_timeout_ms=1500, allow_draft=True))

    assert isinstance(result, Failure) and result.kind is FailureKind.APPROVAL_DENIED, result
    [request] = operator.requests
    assert (request.action, request.page) == ("click", "/app/subacct/review")
    assert request.reason == "marked irreversible: Submit on /app/subacct/review"
    assert "draft" in request.context and "Submit" in request.target
    assert seen_while_held[0].split("?")[0].endswith("/app/subacct/review")  # held on the review page, unsubmitted

    surface.act(Navigate(url="/app/member?m=100234"))  # test-only look at the app, outside the gate
    assert "Rainy Day" not in surface.observe().visible_text


def test_draft_needs_a_human_not_an_auto_approver(run_replay, open_subaccount_artifact):
    auto = RecordingApprover(True, human=False)
    result, _, _ = run_replay(_as_draft(open_subaccount_artifact), SUBACCOUNT, approver=auto,
                              config=ReplayConfig(checkpoint_timeout_ms=1500, allow_draft=True))
    assert isinstance(result, Failure) and result.kind is FailureKind.APPROVAL_DENIED
    assert "needs a human decision" in result.message


def test_draft_runs_once_a_human_approves(run_replay, open_subaccount_artifact):
    result, _, _ = run_replay(_as_draft(open_subaccount_artifact), SUBACCOUNT, approver=RecordingApprover(True),
                              config=ReplayConfig(checkpoint_timeout_ms=1500, allow_draft=True))
    assert isinstance(result, Success) and result.outputs["reference"].startswith("SA-")


@pytest.mark.parametrize("url,reason", [
    ("https://example.com/login", "origin https://example.com is not allowlisted"),
    ("/__reset", "route /__reset is not allowlisted"),
])
def test_off_allowlist_navigation_is_blocked(run_replay, lookup_artifact, surface, url, reason):
    """Acceptance: navigation outside the allowlist never happens and is reported as policy_blocked."""
    steps = list(lookup_artifact.steps)
    steps[0] = steps[0].model_copy(update={"value_template": url})
    result, lines, _ = run_replay(lookup_artifact.model_copy(update={"steps": steps}))
    assert isinstance(result, Failure) and result.kind is FailureKind.POLICY_BLOCKED, result
    assert result.step_id == "s01" and reason in result.message
    assert lines[0]["policy"]["verdict"] == "block" and reason in lines[0]["policy"]["reason"]
    assert surface.current_url() == "about:blank"


def test_business_rule_on_irreversible_flow(run_replay, open_subaccount_artifact):
    params = {"member_id": "100235", "nickname": "Big", "amount": "900.00", **CREDS}  # savings is $250
    result, _, _ = run_replay(open_subaccount_artifact, params)
    assert isinstance(result, BusinessOutcome) and result.code == "INSUFFICIENT_FUNDS"


def test_coordinates_are_never_used_to_type(run_replay, lookup_artifact):
    """If every structural candidate of a fill drifts, replay fails loudly instead of typing at a position."""
    step = lookup_artifact.steps[4]
    ranked = [r if r.candidate.strategy.value == "coords" else r.model_copy(update={
        "candidate": r.candidate.model_copy(update={"value": "Nothing Here", "column": None})})
        for r in step.target.candidates]
    steps = list(lookup_artifact.steps)
    steps[4] = step.model_copy(update={"target": step.target.model_copy(update={"candidates": ranked})})
    result, _, _ = run_replay(lookup_artifact.model_copy(update={"steps": steps}))
    assert isinstance(result, Failure) and result.kind is FailureKind.TARGET_NOT_FOUND and result.step_id == "s05"
