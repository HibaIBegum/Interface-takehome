"""Deterministic replay of a CapabilityArtifact. No LLM: every decision here is a rule in the artifact.

Per step: precondition -> resolve target -> policy gate -> act -> checkpoint (with slow-load backoff)
-> error-signature scan. Anything unexpected goes through `_on_trouble`, which classifies the screen
with the artifact's error signatures before anything is called a failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from pathlib import Path

from cua.artifact.schema import (
    CapabilityArtifact, Condition, ErrorSignature, ParamSpec, Step, describe_condition, render_template,
    template_fields,
)
from cua.observability.runlog import RunLog
from cua.policy.gate import ApprovalDecision, ApprovalRequest, Approver, PolicyGate, Verdict
from cua.surface.base import (
    Action, ActionResult, Click, ErrorCode, Extract, Fill, Navigate, Observation, Press, Select, Strategy, Surface,
    WaitFor,
)

from . import recovery
from .conditions import ConditionChecker, describe_observation
from .results import BusinessOutcome, Failure, FailureKind, RecoveryRecord, ReplayResult, Success


@dataclass(frozen=True)
class ReplayConfig:
    checkpoint_timeout_ms: float = 3000
    backoff_attempts: int = 3         # checkpoint waits of 1x, 2x, 4x the timeout before giving up
    precondition_timeout_ms: float = 3000
    allow_draft: bool = False         # draft artifacts have not been reviewed; refuse them by default


class Handoff(Protocol):
    """A human who can take over the live session (cua.handoff.control.HandoffSession)."""

    def request(self, *, trigger: str, reason: str, step_id: str | None): ...  # -> outcome with .command, .human_actions


# Failures a human can plausibly fix in the live window. Business outcomes are answers, not failures;
# policy blocks and denied approvals are decisions; bad params / drafts are the caller's to fix.
_HANDOFF_KINDS = {
    FailureKind.PRECONDITION_FAILED, FailureKind.TARGET_NOT_FOUND, FailureKind.TARGET_AMBIGUOUS,
    FailureKind.ACTION_FAILED, FailureKind.CHECKPOINT_TIMEOUT, FailureKind.HARD_FAILURE,
    FailureKind.RECOVERY_EXHAUSTED, FailureKind.UNSAFE_TO_RECOVER,
}
MAX_HANDOFFS = 3


class _Retry:
    """Sentinel: recovery succeeded, redo the current phase of the current step."""


RETRY = _Retry()


@dataclass(frozen=True)
class _Jump:
    index: int  # continue from this step index (after re-authentication)


_ACT_FAILURES = {
    ErrorCode.TARGET_NOT_FOUND: FailureKind.TARGET_NOT_FOUND,
    ErrorCode.TARGET_AMBIGUOUS: FailureKind.TARGET_AMBIGUOUS,
}


def validate_params(inputs: list[ParamSpec], params: dict[str, str]) -> list[str]:
    """Problems with the caller's params. Messages never include a value."""
    problems = [f"unknown parameter {name!r}" for name in sorted(set(params) - {p.name for p in inputs})]
    for spec in inputs:
        value = params.get(spec.name)
        if value is None or value == "":
            problems.append(f"missing parameter {spec.name!r}")
        elif spec.pattern and not re.fullmatch(spec.pattern, value):
            problems.append(f"parameter {spec.name!r} does not match {spec.pattern!r}")
        elif spec.type == "integer" and not re.fullmatch(r"-?\d+", value):
            problems.append(f"parameter {spec.name!r} must be an integer")
        elif spec.type == "decimal" and not re.fullmatch(r"-?\d+(\.\d+)?", value):
            problems.append(f"parameter {spec.name!r} must be a decimal number")
    return problems


class ReplayEngine:
    def __init__(self, artifact: CapabilityArtifact, *, surface: Surface, gate: PolicyGate, log: RunLog,
                 config: ReplayConfig = ReplayConfig(), handoff: Handoff | None = None):
        self.artifact = artifact
        self.handoff = handoff
        self.handoffs = 0
        self.surface = surface
        self.gate = gate
        self.log = log
        self.config = config
        self.steps = artifact.steps

    # ------------------------------------------------------------ public

    def run(self, params: dict[str, str]) -> ReplayResult:
        cap = self.artifact.capability
        self.log.echo(f"replay {cap.id} v{cap.version} ({cap.status})")
        if cap.status != "approved" and not self.config.allow_draft:
            return self._finish(Failure(kind=FailureKind.NOT_APPROVED, step_id=None, message=(
                f"{cap.id} v{cap.version} is {cap.status}; approve it or allow drafts explicitly"),
                expected="status approved", observed=f"status {cap.status}"))
        problems = validate_params(self.artifact.inputs, params)
        if problems:
            return self._finish(Failure(kind=FailureKind.INVALID_PARAMS, step_id=None, message="; ".join(problems),
                                        expected="params matching the artifact's inputs", observed="see message"))

        sensitive = {p.name for p in self.artifact.inputs if p.sensitive}
        self.params = params
        self.sensitive = sensitive
        self.checker = ConditionChecker(self.surface, {k: v for k, v in params.items() if k not in sensitive})
        self.outputs: dict[str, str] = {}
        self.verified: list[int] = []
        self.irreversible_done: set[int] = set()
        self.recoveries: list[RecoveryRecord] = []
        self.budget = recovery.RecoveryBudget()
        self.log.write_json("replay.json", {
            "capability": cap.id, "version": cap.version,
            "params": {k: ("[secret]" if k in sensitive else v) for k, v in params.items()},
        })

        index = 0
        while index < len(self.steps):
            outcome = self._run_step(index)
            if isinstance(outcome, _Jump):
                index = outcome.index
            elif outcome is None:
                index += 1
            elif isinstance(outcome, Failure) and self.handoff is not None and outcome.kind in _HANDOFF_KINDS:
                resumed = self._hand_off(index, outcome)
                if isinstance(resumed, int):
                    index = resumed
                else:
                    return self._finish(resumed)
            else:
                return self._finish(outcome)

        met, obs = self.checker.wait_until(self.artifact.success_condition, self.config.checkpoint_timeout_ms)
        if not met:
            return self._finish(self._fail(FailureKind.CHECKPOINT_TIMEOUT, "success_condition",
                                           "the success condition did not hold after the last step",
                                           self.artifact.success_condition, obs))
        missing = [o.name for o in self.artifact.outputs if o.name not in self.outputs]
        if missing:
            return self._finish(self._fail(FailureKind.MISSING_OUTPUT, None, f"outputs never read: {missing}",
                                           None, obs, expected_text=f"outputs {missing} extracted"))
        return self._finish(Success(outputs=self.outputs, recoveries=self.recoveries))

    # ------------------------------------------------------------ human handoff

    def _hand_off(self, index: int, failure: Failure) -> int | Failure:
        """Give the live window to a human; on resume, re-observe and pick the step to continue from."""
        while self.handoffs < MAX_HANDOFFS:
            self.handoffs += 1
            outcome = self.handoff.request(trigger="replay_failure", step_id=failure.step_id,
                                           reason=f"{failure.kind.value}: {failure.message}")
            if outcome.command != "resume":
                return failure.model_copy(update={"message": f"{failure.message} (operator aborted the handoff)"})
            resume, why = self._resume_after_human(index)
            self._record(RecoveryRecord(step_id=failure.step_id or "run", kind="human_handoff",
                                        detail=f"{outcome.human_actions} human action(s); {why}"))
            if resume is not None:
                return resume
            failure = failure.model_copy(update={"message": f"after handoff: {why}"})
        return failure.model_copy(update={"message": f"{failure.message} (gave up after {MAX_HANDOFFS} handoffs)"})

    def _resume_after_human(self, index: int) -> tuple[int | None, str]:
        """Continue after the latest step whose checkpoint holds now (routes bound to this run's params)."""
        obs = self.checker.observe()
        if (sig := self._signature(obs)) is not None:
            return None, f"{sig.outcome_code} is still showing"
        resume = next((k + 1 for k in range(len(self.steps) - 1, -1, -1)
                       if self.steps[k].checkpoint is not None and self.checker.holds(self.steps[k].checkpoint, obs)),
                      None)
        if resume is None:
            precondition = self.steps[index].precondition
            if precondition is not None and self.checker.holds(precondition, obs):
                resume = index
        if resume is None:
            return None, "no step's checkpoint matches the current screen"
        repeated = sorted(self.steps[i].id for i in self.irreversible_done if i >= resume)
        if repeated:
            return None, f"continuing from here would repeat irreversible step(s) {repeated}"
        where = self.steps[resume].id if resume < len(self.steps) else "the success check"
        return resume, f"verified the screen; continuing at {where}"

    # ------------------------------------------------------------ one step

    def _run_step(self, index: int):
        step = self.steps[index]

        while step.precondition is not None:
            met, obs = self.checker.wait_until(step.precondition, self.config.precondition_timeout_ms)
            if met:
                break
            outcome = self._on_trouble(index, "precondition", step.precondition, obs)
            if outcome is not RETRY:
                return outcome

        while True:
            action = self._action(step)
            # Irreversible steps of a draft need a human's approval; an approved artifact's approval
            # (the review that promoted it) stands in for it.
            gated = self.gate.execute(
                self.surface, action, irreversible=step.risk == "irreversible",
                require_human=self.artifact.capability.status != "approved",
                context=f"replay of {self.artifact.capability.id} v{self.artifact.capability.version} "
                        f"({self.artifact.capability.status}), step {step.id}: {step.intent}")
            self._log_step(step, "act", gated.decision.model_dump(mode="json"), gated.result)
            if gated.result is None:
                blocked = gated.decision.verdict is Verdict.BLOCK
                return self._fail(FailureKind.POLICY_BLOCKED if blocked else FailureKind.APPROVAL_DENIED, step.id,
                                  gated.decision.reason, None, self.checker.observe(),
                                  expected_text=f"permission to {step.intent}")
            if gated.result.ok:
                break
            outcome = self._on_trouble(index, "act", None, self.checker.observe(), gated.result)
            if outcome is not RETRY:
                return outcome

        result = gated.result
        if step.output is not None:
            self.outputs[step.output] = result.extracted or ""
            spec = next((o for o in self.artifact.outputs if o.name == step.output), None)
            if spec is not None and spec.redact_in_logs and result.extracted:
                self.log.redactor = self.log.redactor.with_secrets({step.output: result.extracted})
        if step.risk == "irreversible":
            self.irreversible_done.add(index)

        while step.checkpoint is not None:
            met, obs, attempts = recovery.wait_with_backoff(
                self.checker, step.checkpoint, base_ms=self.config.checkpoint_timeout_ms,
                attempts=self.config.backoff_attempts, give_up=lambda o: self._signature(o) is not None)
            if met and attempts > 1:
                self._record(RecoveryRecord(step_id=step.id, kind="slow_load_wait",
                                            detail=f"checkpoint met after {attempts} waits"))
            if met and self._signature(obs) is None:
                self.verified.append(index)
                return None
            outcome = self._on_trouble(index, "checkpoint", step.checkpoint, obs)
            if outcome is not RETRY:
                return outcome

        obs = self.checker.observe()
        if self._signature(obs) is not None:
            outcome = self._on_trouble(index, "after", None, obs)
            if outcome is not RETRY:
                return outcome
        return None

    def _target(self, step: Step):
        """The step's candidates, minus coordinates unless this is a plain click on a safe step.

        A coordinate "matches" whatever is at that point, so it is only acceptable where a wrong
        hit is harmless: never for typing, selecting or reading, and never for an irreversible step.
        """
        if step.target is None:
            return None
        target = step.target.to_target()
        if step.action == "click" and step.risk == "safe":
            return target
        structural = [c for c in target.candidates if c.strategy is not Strategy.COORDS]
        return target.model_copy(update={"candidates": structural}) if structural else target

    def _action(self, step: Step) -> Action:
        target = self._target(step)
        value = render_template(step.value_template, self.params) if step.value_template is not None else None
        if step.action == "click":
            return Click(target=target)
        if step.action == "fill":
            secret = bool(set(template_fields(step.value_template)) & self.sensitive)
            return Fill(target=target, value=value, sensitive=secret)
        if step.action == "select":
            return Select(target=target, option=value)
        if step.action == "press":
            return Press(key=value, target=target)
        if step.action == "navigate":
            return Navigate(url=value)
        if step.action == "extract":
            return Extract(target=target)
        return WaitFor(target=target, timeout_ms=self.config.checkpoint_timeout_ms)

    # ------------------------------------------------------------ trouble: classify, recover, or fail

    def _signature(self, obs: Observation) -> ErrorSignature | None:
        return recovery.match_signature(self.artifact.error_signatures, self.checker, obs)

    def _on_trouble(self, index: int, phase: str, expected: Condition | None, obs: Observation,
                    act_result: ActionResult | None = None):
        step = self.steps[index]
        sig = self._signature(obs)
        if sig is None:
            if phase == "act":
                kind = _ACT_FAILURES.get(act_result.error_code, FailureKind.ACTION_FAILED)
                return self._fail(kind, step.id, act_result.error or "action failed", None, obs,
                                  expected_text=f"{step.intent} to succeed")
            kind = FailureKind.PRECONDITION_FAILED if phase == "precondition" else FailureKind.CHECKPOINT_TIMEOUT
            return self._fail(kind, step.id, f"{phase} not met and no known error screen is showing", expected, obs)

        message = sig.message
        if sig.classification == "business_outcome":
            self.log.echo(f"   {step.id}: business outcome {sig.outcome_code}")
            return BusinessOutcome(code=sig.outcome_code, message=message, step_id=step.id)
        if sig.classification == "hard_failure":
            return self._fail(FailureKind.HARD_FAILURE, step.id, message, expected, obs, code=sig.outcome_code)

        in_flight = step.risk == "irreversible" and phase in ("checkpoint", "after")
        if in_flight and not sig.recovery.allowed_on_irreversible:
            return self._fail(FailureKind.UNSAFE_TO_RECOVER, step.id,
                              f"{sig.outcome_code} after an irreversible action; not safe to recover automatically",
                              expected, obs, code=sig.outcome_code)
        if not self.budget.take(sig):
            return self._fail(FailureKind.RECOVERY_EXHAUSTED, step.id,
                              f"{sig.outcome_code} kept recurring after {sig.recovery.max_attempts} recoveries",
                              expected, obs, code=sig.outcome_code)

        self.log.echo(f"   {step.id}: {sig.outcome_code}, recovering by {sig.recovery.kind}")
        if sig.recovery.kind == "dismiss":
            cleared, record = recovery.dismiss(sig, step.id, surface=self.surface, gate=self.gate,
                                               checker=self.checker, timeout_ms=self.config.checkpoint_timeout_ms)
            self._record(record)
            if not cleared:
                return self._fail(FailureKind.RECOVERY_EXHAUSTED, step.id, record.detail, expected,
                                  self.checker.observe(), code=sig.outcome_code)
            return RETRY
        return self._reauthenticate(index, sig, expected)

    def _reauthenticate(self, index: int, sig: ErrorSignature, expected: Condition | None):
        step = self.steps[index]
        for k in range(len(self.artifact.sign_in_steps)):
            outcome = self._run_step(k)
            if outcome is not None:
                return outcome if not isinstance(outcome, _Jump) else self._fail(
                    FailureKind.RECOVERY_EXHAUSTED, step.id, "session expired again while signing in", expected,
                    self.checker.observe(), code=sig.outcome_code)
        obs = self.checker.observe()
        resume = recovery.resume_index(self.artifact, self.verified, self.checker, obs)
        repeated = sorted(self.steps[i].id for i in self.irreversible_done if i >= resume)
        if repeated:
            return self._fail(FailureKind.UNSAFE_TO_RECOVER, step.id,
                              f"resuming after sign-in would repeat irreversible step(s) {repeated}",
                              expected, obs, code=sig.outcome_code)
        self._record(RecoveryRecord(step_id=step.id, kind="reauthenticate", signature=sig.id,
                                    detail=f"signed in again; resuming at {self.steps[resume].id}"))
        return _Jump(resume)

    # ------------------------------------------------------------ results, evidence, logging

    def _fail(self, kind: FailureKind, step_id: str | None, message: str, expected: Condition | None,
              obs: Observation | None, *, code: str | None = None, expected_text: str | None = None) -> Failure:
        return Failure(kind=kind, step_id=step_id, code=code, message=message,
                       expected=expected_text or describe_condition(expected),
                       observed=describe_observation(obs), evidence_paths=self._evidence(step_id or "run"))

    def _evidence(self, label: str) -> list[str]:
        """Masked screenshot + redacted accessibility and DOM snapshots. Never lets evidence mask the failure."""
        folder = self.log.dir / "evidence"
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        try:
            paths.append(self.surface.screenshot(folder / f"{label}-screen.png"))
            snapshot = self.surface.debug_snapshot()
            for name, suffix in (("accessibility", "aria.txt"), ("dom", "dom.html")):
                path = folder / f"{label}-{suffix}"
                path.write_text(self.log.redactor.text(snapshot[name]), encoding="utf-8")
                paths.append(path)
        except Exception as exc:  # noqa: BLE001 - evidence is best effort
            self.log.echo(f"   (evidence capture failed: {type(exc).__name__})")
        return [str(p) for p in paths]

    def _record(self, record: RecoveryRecord) -> None:
        self.recoveries.append(record)
        self.log.step({"event": "recovery", **record.model_dump(mode="json")})

    def _log_step(self, step: Step, phase: str, policy: dict, result: ActionResult | None) -> None:
        resolution = result.resolution if result else None
        fallback = resolution is not None and resolution.matched_index not in (None, 0)
        self.log.step({
            "event": "step", "step_id": step.id, "phase": phase, "action": step.action,
            "value_template": step.value_template, "policy": policy,
            "result": result.model_dump(mode="json") if result else None,
            "locator_fallback": fallback,
        })
        status = "ok" if result and result.ok else (result.error_code.value if result and result.error_code else "denied")
        drift = f" (fallback candidate {resolution.matched_index}: {resolution.strategy.value})" if fallback else ""
        self.log.echo(f"{step.id} {step.intent}: {status}{drift}")

    def _finish(self, result: ReplayResult) -> ReplayResult:
        result = result.model_copy(update={"run_dir": str(self.log.dir),
                                           "recoveries": getattr(self, "recoveries", [])})
        self.log.write_json("result.json", result)
        return result


def approved_artifact_approver(artifact: CapabilityArtifact, fallback: Approver) -> Approver:
    """Approves irreversible steps of an *approved* artifact (its review is the approval);
    anything else goes to `fallback`, normally a human."""
    def approve(request: ApprovalRequest) -> ApprovalDecision:
        cap = artifact.capability
        if cap.status == "approved":
            return ApprovalDecision(approved=True, by=f"artifact {cap.id} v{cap.version} (approved)", by_human=False)
        return fallback(request)
    return approve


def replay(artifact: CapabilityArtifact, params: dict[str, str], *, surface: Surface, gate: PolicyGate, log: RunLog,
           config: ReplayConfig = ReplayConfig(), handoff: Handoff | None = None) -> ReplayResult:
    return ReplayEngine(artifact, surface=surface, gate=gate, log=log, config=config, handoff=handoff).run(params)
