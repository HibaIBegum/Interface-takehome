"""Discovery loop mechanics against the live mock app, with a scripted stand-in for the LLM."""

import json
import re

import pytest

from conftest import MOCK_PASSWORD, MOCK_USER
from cua.agent.llm import LLMDecision, LLMError
from cua.agent.loop import DiscoveryAgent, Limits, Outcome, Param
from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyConfig, PolicyGate

PARAMS = [
    Param(name="member_id", value="100234"),
    Param(name="username", value=MOCK_USER, sensitive=True),
    Param(name="password", value=MOCK_PASSWORD, sensitive=True),
]


def index_of(screen: str, pattern: str) -> int:
    """Index of the first ELEMENTS line matching `pattern` (a regex) in the rendered step text."""
    for line in screen.split("ELEMENTS:")[1].split("VISIBLE TEXT:")[0].splitlines():
        if re.search(pattern, line):
            return int(re.search(r"\[(\d+)\]", line).group(1))
    raise AssertionError(f"no element matching {pattern!r} in:\n{screen}")


class ScriptedDecider:
    """Plays a list of steps. Each step maps the rendered screen text to (tool, input)."""

    def __init__(self, script):
        self.script = list(script)
        self.seen: list[str] = []

    def decide(self, system, content, tools, timeout_s):
        screen = next(b["text"] for b in content if b["type"] == "text")
        self.seen.append(screen)
        if not self.script:
            raise AssertionError("script exhausted")
        tool, args = self.script.pop(0)(screen)
        return LLMDecision(tool=tool, input=args, model="scripted", input_tokens=0, output_tokens=0)


def click(pattern):
    return lambda s: ("click", {"element_index": index_of(s, pattern), "reason": f"click {pattern}"})


def fill(pattern, value):
    return lambda s: ("fill", {"element_index": index_of(s, pattern), "value": value, "reason": "type"})


def extract(name, pattern):
    return lambda s: ("extract", {"name": name, "element_index": index_of(s, pattern), "reason": "read"})


def call(tool, **args):
    return lambda s: (tool, args)


LOGIN = [
    fill(r"label='User ID'", "{{username}}"),
    fill(r"label='Password'", "{{password}}"),
    click(r"button 'Sign On'"),
]
LOOKUP = [
    fill(r"label='Member ID'", "{{member_id}}"),
    click(r"button 'Search'"),
]


@pytest.fixture
def run_agent(surface, mock_server, tmp_path):
    policy = PolicyConfig.load()

    def run(script, *, limits=Limits(max_steps=20, timeout_s=60), approver=None, decider=None):
        decider = decider or ScriptedDecider(script)
        log = RunLog(tmp_path / "run", policy.redactor({"password": MOCK_PASSWORD, "username": MOCK_USER}), echo=False)
        agent = DiscoveryAgent(surface=surface, gate=PolicyGate(policy, approver=approver, base_url=mock_server),
                               decider=decider, log=log)
        result = agent.run(goal="look up member {member_id} and read their savings balance",
                           entry_url=f"{mock_server}/login", params=PARAMS, limits=limits)
        steps = [json.loads(line) for line in (log.dir / "steps.jsonl").read_text().splitlines()]
        return result, steps, decider, log.dir

    return run


def test_reads_savings_balance_and_logs_every_step(run_agent):
    script = LOGIN + LOOKUP + [
        extract("savings_balance", r"cell row 'Savings', column 'Balance'"),
        call("done", summary="Savings balance is $5,230.17"),
    ]
    result, steps, decider, run_dir = run_agent(script)

    assert result.outcome is Outcome.DONE
    assert result.extracted == {"savings_balance": "$5,230.17"}
    assert [s["tool"] for s in steps] == ["navigate", "fill", "fill", "click", "fill", "click", "extract", "done"]
    assert all(s["policy"]["verdict"] == "allow" for s in steps[:-1])
    assert steps[4]["action"]["value"] == "{{member_id}}"          # logged by reference, not value
    assert steps[6]["action"]["target"]["candidates"][0]["column"] == "Balance"
    assert steps[6]["element"]["text"] == "$5,230.17"
    assert (run_dir / "step_001.png").exists() and (run_dir / "result.json").exists()


def test_secrets_never_reach_the_llm_or_disk(run_agent):
    result, steps, decider, run_dir = run_agent(LOGIN + [call("done", summary="signed in")])
    assert result.outcome is Outcome.DONE
    assert all(MOCK_PASSWORD not in screen for screen in decider.seen)
    assert "password = <secret>" in decider.seen[0]
    for path in run_dir.iterdir():
        if path.suffix in (".jsonl", ".json"):
            assert MOCK_PASSWORD not in path.read_text()


def test_commit_action_is_denied_without_approval(run_agent):
    script = LOGIN + LOOKUP + [
        click(r"button 'Open Sub-Account'"),
        lambda s: ("select", {"element_index": index_of(s, r"label='Sub-Account Type'"), "option": "Share Savings",
                              "reason": "type"}),
        fill(r"label='Nickname'", "Test"),
        fill(r"label='Initial Deposit \(\$\)'", "10.00"),
        click(r"button 'Continue'"),
        click(r"button 'Submit'"),
        call("request_human", reason="submit needs approval"),
    ]
    result, steps, _, _ = run_agent(script)
    submit = steps[-2]
    assert submit["policy"] == {"verdict": "deny", "risk": "commit", "approved": False,
                                "reason": "commit action requires approval and no approver is configured"}
    assert submit["result"] is None
    assert result.outcome is Outcome.HUMAN_REQUESTED


def test_commit_action_runs_when_approved(run_agent):
    script = LOGIN + LOOKUP + [
        click(r"button 'Open Sub-Account'"),
        lambda s: ("select", {"element_index": index_of(s, r"label='Sub-Account Type'"), "option": "Share Savings",
                              "reason": "type"}),
        fill(r"label='Nickname'", "Test"),
        fill(r"label='Initial Deposit \(\$\)'", "10.00"),
        click(r"button 'Continue'"),
        click(r"button 'Submit'"),
        extract("reference", r"cell row 'Reference Number'"),
        call("done", summary="opened"),
    ]
    result, steps, _, _ = run_agent(script, approver=lambda action, risk: True)
    assert steps[-3]["policy"]["approved"] is True
    assert result.extracted["reference"].startswith("SA-")


def test_repeating_the_same_action_on_the_same_screen_is_stuck(run_agent):
    result, steps, _, _ = run_agent(LOGIN + [click(r"button 'Search'")] * 3)
    assert result.outcome is Outcome.STUCK
    assert steps[-1]["result"] is None and "3 times" in steps[-1]["error"]


def test_three_consecutive_failures_is_stuck(run_agent):
    bad = call("click", element_index=999, reason="nonsense")
    result, steps, _, _ = run_agent([bad, bad, bad])
    assert result.outcome is Outcome.STUCK
    assert result.summary.startswith("3 consecutive failed actions")
    assert all("no element [999]" in s["error"] for s in steps[1:])


def test_unknown_parameter_and_off_allowlist_navigation_are_rejected(run_agent):
    script = [
        fill(r"label='User ID'", "{{account_number}}"),
        call("navigate", url="https://example.com/", reason="leave"),
        call("done", summary="stop"),
    ]
    result, steps, _, _ = run_agent(script)
    assert "unknown parameter {{account_number}}" in steps[1]["error"]
    assert steps[2]["policy"]["verdict"] == "deny" and steps[2]["result"] is None
    assert result.outcome is Outcome.DONE


def test_max_steps_and_timeout(run_agent):
    result, _, _, _ = run_agent(LOGIN, limits=Limits(max_steps=2, timeout_s=60))
    assert result.outcome is Outcome.MAX_STEPS
    result, _, _, _ = run_agent([], limits=Limits(max_steps=5, timeout_s=0.001))
    assert result.outcome is Outcome.TIMEOUT


def test_llm_failure_ends_the_run(run_agent):
    class Broken:
        def decide(self, *args, **kwargs):
            raise LLMError("LLM unreachable or timed out (after SDK retries)")

    result, steps, _, _ = run_agent(None, decider=Broken())
    assert result.outcome is Outcome.LLM_ERROR
    assert steps[-1]["error"].startswith("LLM unreachable")
