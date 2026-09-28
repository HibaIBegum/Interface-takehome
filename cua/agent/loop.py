"""Discovery loop: observe -> decide (LLM) -> policy -> act -> log, until done or stopped."""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from cua.observability.runlog import RunLog
from cua.policy.gate import PolicyDecision, PolicyGate
from cua.surface.base import (
    Action, ActionResult, Click, ElementInfo, Extract, Fill, Navigate, Observation, Select, Surface, target_for,
)

from .llm import Decider, LLMError
from .prompts import SYSTEM_PROMPT, TOOL_INPUTS, describe_element, render_step, tool_definitions

_PARAM_REF = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_EXTRACT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
STUCK_REPEATS = 3
STUCK_FAILURES = 3


class Param(BaseModel):
    name: str
    value: str = Field(repr=False)
    sensitive: bool = False


class Limits(BaseModel):
    max_steps: int = Field(default=25, ge=1)
    timeout_s: float = Field(default=300, gt=0)


class Outcome(str, Enum):
    DONE = "done"
    HUMAN_REQUESTED = "human_requested"
    STUCK = "stuck"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    ENTRY_FAILED = "entry_failed"
    LLM_ERROR = "llm_error"


class ObservationSummary(BaseModel):
    url: str
    frames: dict[str, str]
    title: str
    state_hash: str
    element_count: int
    screenshot: str | None

    @classmethod
    def of(cls, obs: Observation) -> ObservationSummary:
        return cls(
            url=obs.url, title=obs.title, state_hash=obs.state_hash, element_count=len(obs.elements),
            frames={"/".join(f.path) or "top": f.url for f in obs.frames},
            screenshot=obs.screenshot_path.name if obs.screenshot_path else None,
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


class DiscoveryResult(BaseModel):
    outcome: Outcome
    summary: str
    extracted: dict[str, str]
    steps: int
    run_dir: str


class ToolInputError(Exception):
    """The model's tool call can't be turned into an action (bad index, unknown parameter, ...)."""


# Phase 6 replaces this with a real operator handoff. Returning True would mean "human fixed it, continue".
HandoffHandler = Callable[[str], bool]


def no_handoff(reason: str) -> bool:
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DiscoveryAgent:
    def __init__(self, *, surface: Surface, gate: PolicyGate, decider: Decider, log: RunLog,
                 handoff: HandoffHandler = no_handoff, screenshots: bool = True):
        self.surface = surface
        self.gate = gate
        self.decider = decider
        self.log = log
        self.handoff = handoff
        self.screenshots = screenshots

    # ------------------------------------------------------------ tool call -> action

    def _resolve_ref(self, value: str, params: dict[str, Param]) -> tuple[str, bool]:
        """Resolve a whole-string {{name}} reference. Returns (value, sensitive)."""
        match = _PARAM_REF.fullmatch(value.strip())
        if match is None:
            return value, False
        param = params.get(match.group(1))
        if param is None:
            raise ToolInputError(f"unknown parameter {{{{{match.group(1)}}}}}; known: {sorted(params)}")
        return param.value, param.sensitive

    @staticmethod
    def _element(obs: Observation, index: int, *, data_ok: bool) -> ElementInfo:
        if not 0 <= index < len(obs.elements):
            raise ToolInputError(f"there is no element [{index}] on this screen")
        element = obs.elements[index]
        if element.role == "cell" and not data_ok:
            raise ToolInputError(f"[{index}] is a read-only data cell; use extract to read it")
        if element.role != "cell" and data_ok:
            raise ToolInputError(f"[{index}] is not a data cell; extract reads cells")
        return element

    def _to_action(self, tool: str, args: BaseModel, obs: Observation,
                   params: dict[str, Param]) -> tuple[Action, Action, ElementInfo | None]:
        """Returns (action to execute, action as logged with references, element pointed at)."""
        if tool == "navigate":
            action = Navigate(url=args.url)
            return action, action, None
        element = self._element(obs, args.element_index, data_ok=(tool == "extract"))
        try:
            target = target_for(element)
        except ValueError as exc:
            raise ToolInputError(f"[{element.index}] has no stable name or label to target") from exc
        if tool == "click":
            action = Click(target=target)
            return action, action, element
        if tool == "extract":
            if not _EXTRACT_NAME.match(args.name):
                raise ToolInputError("extract name must be short snake_case, e.g. savings_balance")
            action = Extract(target=target)
            return action, action, element
        if tool == "fill":
            value, sensitive = self._resolve_ref(args.value, params)
            return (Fill(target=target, value=value, sensitive=sensitive),
                    Fill(target=target, value=args.value, sensitive=sensitive), element)
        if tool == "select":
            option, _ = self._resolve_ref(args.option, params)
            if element.options is not None and option not in element.options:
                raise ToolInputError(f"{option!r} is not an option of [{element.index}]: {element.options}")
            return Select(target=target, option=option), Select(target=target, option=args.option), element
        raise ToolInputError(f"unknown tool {tool!r}")

    # ------------------------------------------------------------ loop

    def run(self, *, goal: str, entry_url: str, params: list[Param], limits: Limits) -> DiscoveryResult:
        by_name = {p.name: p for p in params}
        shown_params = [(p.name, None if p.sensitive else p.value) for p in params]
        tools = tool_definitions()
        deadline = time.monotonic() + limits.timeout_s
        history: list[str] = []
        extracted: dict[str, str] = {}
        repeats: Counter[str] = Counter()
        failures = 0

        def finish(outcome: Outcome, summary: str, steps: int) -> DiscoveryResult:
            result = DiscoveryResult(outcome=outcome, summary=summary, extracted=extracted, steps=steps,
                                     run_dir=str(self.log.dir))
            self.log.write_json("result.json", result)
            self.log.echo(f"== {outcome.value}: {summary}")
            return result

        def escalate(outcome: Outcome, reason: str, step: int) -> DiscoveryResult | None:
            self.log.echo(f"   handoff requested: {reason}")
            if self.handoff(reason):
                return None
            return finish(outcome, f"{reason} (no human handoff available yet)", step)

        entry = self.gate.execute(self.surface, Navigate(url=entry_url), current_url="")
        self.log.step(StepRecord(step=0, at=_now(), tool="navigate", tool_input={"url": entry_url},
                                 reason="entry URL", action=Navigate(url=entry_url).model_dump(mode="json"),
                                 policy=entry.decision, result=entry.result))
        if entry.result is None or not entry.result.ok:
            why = entry.decision.reason if entry.result is None else entry.result.error
            return finish(Outcome.ENTRY_FAILED, f"could not open entry URL: {why}", 0)

        for step in range(1, limits.max_steps + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return finish(Outcome.TIMEOUT, f"timed out after {limits.timeout_s:.0f}s", step - 1)

            obs = self.surface.observe(self.log.screenshot_path(step) if self.screenshots else None)
            record = StepRecord(step=step, at=_now(), observation=ObservationSummary.of(obs), tool="", tool_input={},
                                reason="")
            content = render_step(goal=goal, params=shown_params, extracted=extracted, history=history, obs=obs,
                                  step=step, max_steps=limits.max_steps)
            try:
                decision = self.decider.decide(SYSTEM_PROMPT, content, tools, timeout_s=remaining)
            except LLMError as exc:
                record.error = str(exc)
                self.log.step(record)
                return finish(Outcome.LLM_ERROR, str(exc), step)

            record.tool, record.tool_input = decision.tool, decision.input
            record.llm = {"model": decision.model, "fell_back": decision.fell_back,
                          "input_tokens": decision.input_tokens, "output_tokens": decision.output_tokens}
            try:
                model = TOOL_INPUTS.get(decision.tool)
                if model is None:
                    raise ToolInputError(f"unknown tool {decision.tool!r}")
                args = model.model_validate(decision.input)
            except (ToolInputError, ValidationError) as exc:
                args, record.error = None, f"invalid tool call: {exc}"
            record.reason = getattr(args, "reason", None) or getattr(args, "summary", "") or ""

            if decision.tool == "done" and args is not None:
                self.log.step(record)
                return finish(Outcome.DONE, args.summary, step)
            if decision.tool == "request_human" and args is not None:
                self.log.step(record)
                if (stopped := escalate(Outcome.HUMAN_REQUESTED, args.reason, step)) is not None:
                    return stopped
                continue

            ok = False
            if args is not None:
                try:
                    action, logged, record.element = self._to_action(decision.tool, args, obs, by_name)
                    record.action = logged.model_dump(mode="json")
                except ToolInputError as exc:
                    record.error = str(exc)
                else:
                    key = obs.state_hash + json.dumps(record.action, sort_keys=True)
                    repeats[key] += 1
                    if repeats[key] >= STUCK_REPEATS:
                        record.error = f"same action on the same screen {STUCK_REPEATS} times; not repeating it"
                        self.log.step(record)
                        if (stopped := escalate(Outcome.STUCK, record.error, step)) is not None:
                            return stopped
                        continue
                    gated = self.gate.execute(self.surface, action, obs.url)
                    record.policy, record.result = gated.decision, gated.result
                    ok = gated.result is not None and gated.result.ok
                    if ok and isinstance(action, Extract):
                        extracted[args.name] = gated.result.extracted or ""

            self.log.step(record)
            history.append(self._history_line(step, record, args))
            self.log.echo(history[-1])
            failures = 0 if ok else failures + 1
            if failures >= STUCK_FAILURES:
                if (stopped := escalate(Outcome.STUCK, f"{failures} consecutive failed actions", step)) is not None:
                    return stopped
                failures = 0

        return finish(Outcome.MAX_STEPS, f"goal not reached within {limits.max_steps} steps", limits.max_steps)

    @staticmethod
    def _history_line(step: int, record: StepRecord, args: BaseModel | None) -> str:
        what = record.tool
        if record.element is not None:
            what += " " + describe_element(record.element)
        for key in ("value", "option", "url", "name"):
            if args is not None and hasattr(args, key):
                what += f" {key}={getattr(args, key)!r}"
        if record.error is not None:
            status = f"REJECTED: {record.error}"
        elif record.result is None:
            status = f"DENIED by policy: {record.policy.reason}"
        elif record.result.ok:
            status = "ok" + (f", read {record.result.extracted!r}" if record.result.extracted is not None else "")
        else:
            status = f"FAILED ({record.result.error_code.value}): {record.result.error}"
        return f"{step}. {what} -> {status}"
