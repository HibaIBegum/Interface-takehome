"""Human handoff on the same live session: control state, capture, intervention requests, resume.

A scripted operator stands in for the console. When it "takes" control, test code drives the same
page with Playwright's own click/fill, which fire real DOM events, so capture.js sees them exactly
as it would see a person.
"""

import json

import pytest

from conftest import MOCK_PASSWORD, MOCK_USER, post_json
from cua.agent.loop import DiscoveryAgent, Limits, Outcome
from cua.handoff.control import ControlState, Controller, HandoffSession, InvalidTransition
from cua.handoff.operator_cli import UnattendedOperator
from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyConfig, PolicyGate, Verdict
from cua.replay import BusinessOutcome, Failure, FailureKind, ReplayConfig, Success, replay
from cua.replay.engine import approved_artifact_approver
from cua.surface.base import Click, LocatorCandidate, Strategy, Target
from test_agent_loop import LOGIN, PARAMS, ScriptedDecider, call, click, extract, fill
from test_replay import CREDS, SUBACCOUNT, _as_draft, lookup_artifact, open_subaccount_artifact  # noqa: F401

FAST = ReplayConfig(checkpoint_timeout_ms=1500, precondition_timeout_ms=1500)


class ScriptedOperator:
    """Commands in order. A callable item is the human working in the window; it runs, then events pump."""

    name = "test-operator"

    def __init__(self, *items):
        self.items = list(items)
        self.shown = []
        self.said = []

    def show(self, request):
        self.shown.append(request)

    def say(self, message):
        self.said.append(message)

    def next_command(self, pump):
        while self.items:
            item = self.items.pop(0)
            if callable(item):
                item()
                pump()
                continue
            return item
        return "abort"


def main_frame(surface):
    return surface._frame(["main"])


def human_searches(surface, member_id):
    """What a teller would do after a 500: go back to Member Search and look the member up by hand."""
    def act():
        surface._frame(["hdr"]).get_by_role("link", name="Member Search").click()
        main_frame(surface).get_by_role("button", name="Search").wait_for()
        main_frame(surface).locator("input[name=mid]").fill(member_id)
        main_frame(surface).get_by_role("button", name="Search").click()
        main_frame(surface).get_by_text("Member Detail").wait_for()
    return act


@pytest.fixture
def handoff_replay(surface, mock_server, tmp_path):
    def run(artifact, operator, params=None, *, approver=None, config=FAST):
        params = params if params is not None else {"member_id": "100234", **CREDS}
        policy = PolicyConfig.load()
        log = RunLog(tmp_path / "run", policy.redactor({"password": MOCK_PASSWORD}), echo=False)
        session = HandoffSession(surface=surface, log=log, operator=operator, subject=artifact.capability.id)
        gate = PolicyGate(policy, base_url=mock_server,
                          approver=approver or approved_artifact_approver(artifact, fallback=session.approver))
        gate.may_act = session.controller.automation_may_act
        result = replay(artifact, params, surface=surface, gate=gate, log=log, config=config, handoff=session)
        events = [json.loads(x) for x in (log.dir / "steps.jsonl").read_text().splitlines()]
        return result, events, log.dir, session

    return run


def of(events, kind):
    return [e for e in events if e.get("event") == kind]


# ---------------------------------------------------------------- acceptance demo

def test_human_fixes_an_unrecoverable_state_and_replay_completes(handoff_replay, lookup_artifact, mock_server,
                                                                   surface):
    post_json(f"{mock_server}/__faults", {"fault": "server_error"})  # a hard failure at s04
    operator = ScriptedOperator("take", human_searches(surface, "100234"), "resume")
    result, events, run_dir, session = handoff_replay(lookup_artifact, operator)

    assert isinstance(result, Success), result
    assert result.outputs == {"savings_balance": "$5,230.17"}
    [handoff] = [r for r in result.recoveries if r.kind == "human_handoff"]
    assert "continuing at s07" in handoff.detail   # the human already did s05/s06: verified, not repeated

    request = json.loads((run_dir / "intervention.json").read_text())
    assert (request["trigger"], request["step_id"]) == ("replay_failure", "s04")
    assert "hard_failure" in request["reason"] and (run_dir / request["screenshot"]).exists()
    assert operator.shown[0].reason == request["reason"]

    moves = [(t["from_state"], t["to_state"], t["by"]) for t in of(events, "control_transition")]
    assert moves == [("agent", "awaiting_human", "automation"), ("awaiting_human", "human", "test-operator"),
                     ("human", "agent", "test-operator")]

    human = [(a["kind"], a.get("name") or a.get("label"), a.get("value")) for a in of(events, "human_action")
             if a["kind"] != "navigate"]
    assert human == [("click", "Member Search", None), ("fill", "Member ID", "100234"), ("click", "Search", None)]
    assert any(a["kind"] == "navigate" and "/app/member" in a["url"] for a in of(events, "human_action"))
    # automation's own clicks (Sign On before, nothing after) are not attributed to the human
    took, handed_back = (t["at"] for t in of(events, "control_transition")[1:3])
    assert all(took <= a["at"] <= handed_back for a in of(events, "human_action"))
    assert not any(a.get("name") == "Sign On" for a in of(events, "human_action"))


# ---------------------------------------------------------------- resume must be verified

def test_resume_without_fixing_asks_again_then_abort_fails(handoff_replay, lookup_artifact, mock_server):
    post_json(f"{mock_server}/__faults", {"fault": "server_error"})
    operator = ScriptedOperator("resume", "abort")
    result, events, _, session = handoff_replay(lookup_artifact, operator)
    assert isinstance(result, Failure) and result.kind is FailureKind.HARD_FAILURE
    assert "operator aborted" in result.message and "SERVER_ERROR is still showing" in result.message
    assert len(operator.shown) == 2 and session.controller.state is ControlState.ABORTED


def test_resume_on_the_wrong_member_is_not_accepted(handoff_replay, lookup_artifact, mock_server, surface):
    """Checkpoint routes are bound to this run's params: member 100236's page is not member 100234's."""
    post_json(f"{mock_server}/__faults", {"fault": "server_error"})
    operator = ScriptedOperator("take", human_searches(surface, "100236"), "resume", "abort")
    result, _, _, _ = handoff_replay(lookup_artifact, operator)
    assert isinstance(result, Failure) and "no step's checkpoint matches" in result.message
    assert "savings_balance" not in result.model_dump_json()


def test_business_outcomes_do_not_hand_off(handoff_replay, lookup_artifact):
    operator = ScriptedOperator()
    result, _, _, _ = handoff_replay(lookup_artifact, operator, {"member_id": "999999", **CREDS})
    assert isinstance(result, BusinessOutcome) and operator.shown == []


# ---------------------------------------------------------------- approvals

@pytest.mark.parametrize("command,approved", [("approve", True), ("deny", False)])
def test_irreversible_approval_goes_through_the_operator(handoff_replay, open_subaccount_artifact, command, approved):
    operator = ScriptedOperator(command)
    config = ReplayConfig(checkpoint_timeout_ms=1500, allow_draft=True)
    result, events, _, _ = handoff_replay(_as_draft(open_subaccount_artifact), operator, SUBACCOUNT, config=config)
    [request] = operator.shown
    assert request.trigger == "approval" and "Submit" in request.reason and "/app/subacct/review" in request.reason
    assert request.allowed_commands == ["approve", "deny", "abort"]
    if approved:
        assert isinstance(result, Success) and result.outputs["reference"].startswith("SA-")
    else:
        assert isinstance(result, Failure) and result.kind is FailureKind.APPROVAL_DENIED
    assert of(events, "control_transition")[-1]["why"] == f"{command}d the action"


# ---------------------------------------------------------------- discovery

def test_discovery_hands_off_when_stuck_and_continues(surface, mock_server, tmp_path):
    policy = PolicyConfig.load()
    log = RunLog(tmp_path / "disc", policy.redactor({"password": MOCK_PASSWORD}), echo=False)
    operator = ScriptedOperator("take", human_searches(surface, "100234"), "resume")
    session = HandoffSession(surface=surface, log=log, operator=operator, subject="discovery")
    gate = PolicyGate(policy, approver=session.approver, base_url=mock_server)
    gate.may_act = session.controller.automation_may_act
    script = LOGIN + [click(r"button 'Search'")] * 3 + [
        extract("savings_balance", r"cell row 'Savings', column 'Balance'"), call("done", summary="read")]
    agent = DiscoveryAgent(surface=surface, gate=gate, decider=ScriptedDecider(script), log=log,
                           handoff=lambda trigger, reason: session.request(trigger=trigger, reason=reason,
                                                                           step_id=None).command == "resume")
    result = agent.run(goal="look up member {member_id}", entry_url=f"{mock_server}/login", params=PARAMS,
                       limits=Limits(max_steps=15, timeout_s=60))
    assert result.outcome is Outcome.DONE and result.extracted == {"savings_balance": "$5,230.17"}
    assert operator.shown[0].trigger == "stuck"
    human = [e for e in map(json.loads, (log.dir / "steps.jsonl").read_text().splitlines())
             if e.get("event") == "human_action" and e["kind"] == "fill"]
    assert human[0]["value"] == "100234"


# ---------------------------------------------------------------- building blocks

def test_passwords_typed_by_a_human_are_never_captured(surface, mock_server, tmp_path):
    log = RunLog(tmp_path / "pw", PolicyConfig.load().redactor(), echo=False)

    def type_credentials():
        surface.page.locator("input[name=uid]").fill(MOCK_USER)
        surface.page.locator("input[name=pwd]").fill(MOCK_PASSWORD)

    surface.page.goto(f"{mock_server}/login")
    session = HandoffSession(surface=surface, log=log, operator=ScriptedOperator("take", type_credentials, "resume"),
                             subject="test")
    session.request(trigger="request_human", reason="please sign in", step_id=None)
    fills = [a for a in session.recorder.actions if a["kind"] == "fill"]
    assert [(a["label"], a["value"]) for a in fills] == [("User ID", MOCK_USER), ("Password", "[secret]")]
    assert MOCK_PASSWORD not in (log.dir / "steps.jsonl").read_text()


def test_control_transitions_are_checked_and_logged(tmp_path):
    controller = Controller(RunLog(tmp_path, PolicyConfig.load().redactor(), echo=False))
    with pytest.raises(InvalidTransition):
        controller.transition(ControlState.HUMAN, by="someone", why="skipping the request")
    controller.transition(ControlState.AWAITING_HUMAN, by="automation", why="stuck")
    controller.transition(ControlState.HUMAN, by="op", why="took control")
    assert not controller.automation_may_act()
    controller.transition(ControlState.ABORTED, by="op", why="enough")
    with pytest.raises(InvalidTransition):
        controller.transition(ControlState.AGENT, by="automation", why="try to continue")
    lines = [json.loads(x) for x in (tmp_path / "steps.jsonl").read_text().splitlines()]
    assert [(x["to_state"], x["by"], x["why"]) for x in lines] == [
        ("awaiting_human", "automation", "stuck"), ("human", "op", "took control"), ("aborted", "op", "enough")]


def test_the_gate_refuses_automation_while_a_human_has_control(tmp_path):
    controller = Controller(RunLog(tmp_path, PolicyConfig.load().redactor(), echo=False))
    gate = PolicyGate(PolicyConfig.load())
    gate.may_act = controller.automation_may_act
    controller.transition(ControlState.AWAITING_HUMAN, by="automation", why="stuck")

    class Untouchable:
        def act(self, action):
            raise AssertionError("automation acted while it did not own the session")

    button = Target(candidates=[LocatorCandidate(strategy=Strategy.ROLE_NAME, role="button", value="Search")])
    gated = gate.execute(Untouchable(), Click(target=button))
    assert gated.result is None and gated.decision.verdict is Verdict.BLOCK
    assert gated.decision.reason == "automation does not hold control of the session"


def test_unattended_runs_abort_handoffs(surface, tmp_path):
    session = HandoffSession(surface=surface, log=RunLog(tmp_path, PolicyConfig.load().redactor(), echo=False),
                             operator=UnattendedOperator(), subject="test")
    assert session.request(trigger="stuck", reason="nobody home", step_id=None).command == "abort"
    assert session.controller.state is ControlState.ABORTED
