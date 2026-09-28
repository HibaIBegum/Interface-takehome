"""Shapes of what a discovery run writes, shared by the agent (writer) and the recorder (reader)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cua.policy.gate import PolicyDecision
from cua.surface.base import ActionResult, ElementInfo, Observation

_TEXT_IN_LOG = 2000


class ParamRecord(BaseModel):
    name: str
    sensitive: bool
    example: str | None  # the run's value; always None for sensitive params


class RunManifest(BaseModel):
    """run.json: what the run was asked to do. Never holds a secret value."""

    run_id: str
    goal: str
    entry_url: str
    params: list[ParamRecord]
    started_at: str


class ObservationSummary(BaseModel):
    url: str
    frames: dict[str, str]  # "main" -> URL ("top" for the top-level document)
    title: str
    state_hash: str
    element_count: int
    screenshot: str | None
    visible_text: str = ""

    @classmethod
    def of(cls, obs: Observation) -> ObservationSummary:
        return cls(
            url=obs.url, title=obs.title, state_hash=obs.state_hash, element_count=len(obs.elements),
            frames={"/".join(f.path) or "top": f.url for f in obs.frames},
            screenshot=obs.screenshot_path.name if obs.screenshot_path else None,
            visible_text=obs.visible_text[:_TEXT_IN_LOG],
        )


class StepRecord(BaseModel):
    """One line of steps.jsonl. `action` carries parameter references ({{name}}), never resolved values."""

    step: int
    at: str
    observation: ObservationSummary | None = None
    tool: str
    tool_input: dict[str, Any]
    reason: str
    element: ElementInfo | None = None  # what the model pointed at; the recorder derives locators from it
    action: dict[str, Any] | None = None
    policy: PolicyDecision | None = None
    result: ActionResult | None = None
    error: str | None = None
    llm: dict[str, Any] | None = None
    performed_by: str = "agent"  # "human" for steps taken during a handoff
