"""Acceptance: a real Claude run against the mock app. Costs money; skipped without credentials."""

import os

import pytest

from conftest import MOCK_PASSWORD, MOCK_USER
from cua.agent.loop import DiscoveryAgent, Limits, Outcome, Param
from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyConfig, PolicyGate

pytestmark = pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_MODEL")),
    reason="live LLM test: set ANTHROPIC_API_KEY and ANTHROPIC_MODEL",
)


def test_agent_reads_savings_balance(surface, mock_server, tmp_path):
    from cua.agent.llm import ClaudeDecider

    policy = PolicyConfig.load()
    surface.timeout_ms = 5000
    log = RunLog(tmp_path / "live", policy.redactor({"password": MOCK_PASSWORD}))
    agent = DiscoveryAgent(surface=surface, gate=PolicyGate(policy, base_url=mock_server),
                           decider=ClaudeDecider(), log=log)
    result = agent.run(
        goal="look up member {member_id} and read their savings balance",
        entry_url=f"{mock_server}/login",
        params=[Param(name="member_id", value="100234"),
                Param(name="username", value=MOCK_USER, sensitive=True),
                Param(name="password", value=MOCK_PASSWORD, sensitive=True)],
        limits=Limits(max_steps=15, timeout_s=240),
    )
    assert result.outcome is Outcome.DONE, result
    assert "$5,230.17" in result.extracted.values()
