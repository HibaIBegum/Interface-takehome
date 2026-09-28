"""ClaudeDecider request shape and response handling, against a fake client (no network)."""

from types import SimpleNamespace

import pytest

from cua.agent.llm import ClaudeDecider, LLMError
from cua.agent.prompts import tool_definitions


def response(*blocks, stop_reason="tool_use", model="claude-test", stop_details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, model=model, stop_details=stop_details,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def tool_use(name, **args):
    return SimpleNamespace(type="tool_use", name=name, input=args)


def text(t):
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=self)

    def with_options(self, **kwargs):
        return self

    def create(self, **request):
        self.requests.append(request)
        return self.responses.pop(0)


def decide(client):
    return ClaudeDecider(model="claude-test", client=client).decide(
        "system", [{"type": "text", "text": "screen"}], tool_definitions(), timeout_s=30)


def test_one_tool_call_per_step_without_forcing():
    client = FakeClient(response(tool_use("click", element_index=3, reason="go")))
    decision = decide(client)
    assert (decision.tool, decision.input) == ("click", {"element_index": 3, "reason": "go"})
    request = client.requests[0]
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert all(t["strict"] for t in request["tools"])
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["fallbacks"] == "default" and request["betas"] == ["server-side-fallback-2026-07-01"]
    assert decision.fell_back is False


def test_records_when_a_fallback_model_answered():
    fallback = SimpleNamespace(type="fallback")
    client = FakeClient(response(fallback, tool_use("click", element_index=1, reason="x"), model="claude-fallback"))
    decision = decide(client)
    assert decision.fell_back and decision.model == "claude-fallback"


def test_refusal_reports_category_and_explanation():
    details = SimpleNamespace(category="cyber", explanation="looks like credential abuse")
    with pytest.raises(LLMError, match=r"category: cyber\): looks like credential abuse"):
        decide(FakeClient(response(stop_reason="refusal", stop_details=details)))


def test_nudges_once_when_no_tool_is_called():
    client = FakeClient(response(text("thinking out loud"), stop_reason="end_turn"),
                        response(tool_use("done", summary="ok")))
    assert decide(client).tool == "done"
    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]


def test_gives_up_after_the_nudge():
    client = FakeClient(response(text("a"), stop_reason="end_turn"), response(text("b"), stop_reason="end_turn"))
    with pytest.raises(LLMError, match="did not call a tool"):
        decide(client)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_are_errors(stop_reason):
    with pytest.raises(LLMError):
        decide(FakeClient(response(tool_use("click", element_index=1, reason="x"), stop_reason=stop_reason)))


def test_model_comes_from_environment(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_MODEL"):
        ClaudeDecider(client=FakeClient())
