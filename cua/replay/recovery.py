"""Recovery primitives. Every one is bounded and returns a record for the replay log.

- slow load:      wait for the checkpoint again with a doubled timeout (backoff), a few times
- interstitial:   click the signature's dismiss target, then check the screen is clear
- session expiry: handled by the engine (it re-runs sign_in_steps), using `resume_index` here
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from cua.artifact.schema import CapabilityArtifact, Condition, ErrorSignature
from cua.policy.gate import PolicyGate
from cua.surface.base import Click, Observation, Surface

from .conditions import ConditionChecker
from .results import RecoveryRecord


def match_signature(signatures: list[ErrorSignature], checker: ConditionChecker,
                    obs: Observation) -> ErrorSignature | None:
    """First matching signature, in artifact order (most specific first)."""
    return next((sig for sig in signatures if checker.holds(sig.match, obs)), None)


class RecoveryBudget:
    """Per-signature attempt counter for the whole replay, capped by each signature's max_attempts."""

    def __init__(self) -> None:
        self._used: Counter[str] = Counter()

    def take(self, sig: ErrorSignature) -> bool:
        if sig.recovery is None or self._used[sig.id] >= sig.recovery.max_attempts:
            return False
        self._used[sig.id] += 1
        return True


def wait_with_backoff(checker: ConditionChecker, cond: Condition, *, base_ms: float, attempts: int,
                      give_up: Callable[[Observation], bool]) -> tuple[bool, Observation, int]:
    """Wait base, 2*base, 4*base ... ms. Stops early when `give_up` says an error screen is showing.

    Returns (met, last observation, attempts used). More than one attempt means the page was slow.
    """
    obs: Observation | None = None
    for attempt in range(attempts):
        met, obs = checker.wait_until(cond, base_ms * (2 ** attempt))
        if met:
            return True, obs, attempt + 1
        if give_up(obs):
            return False, obs, attempt + 1
    return False, obs, attempts


def dismiss(sig: ErrorSignature, step_id: str, *, surface: Surface, gate: PolicyGate, checker: ConditionChecker,
            timeout_ms: float) -> tuple[bool, RecoveryRecord]:
    """Click the signature's dismiss target (through the policy gate), then wait for the signature to clear."""
    target = sig.recovery.target.to_target()
    gated = gate.execute(surface, Click(target=target), context=f"recovery at {step_id}: dismiss {sig.id}")
    if gated.result is None or not gated.result.ok:
        why = gated.decision.reason if gated.result is None else gated.result.error
        return False, RecoveryRecord(step_id=step_id, kind="dismiss", signature=sig.id, detail=f"failed: {why}")
    deadline_ms = timeout_ms
    cleared = False
    while deadline_ms > 0 and not cleared:
        obs = checker.observe()
        cleared = not checker.holds(sig.match, obs)
        if not cleared:
            surface.wait_for_navigation(min(deadline_ms, 250))
            deadline_ms -= 250
    detail = f"clicked {sig.recovery.target.description}" + ("" if cleared else ", but it is still showing")
    return cleared, RecoveryRecord(step_id=step_id, kind="dismiss", signature=sig.id, detail=detail)


def resume_index(artifact: CapabilityArtifact, verified: list[int], checker: ConditionChecker,
                 obs: Observation) -> int:
    """After signing in again: resume after the latest verified checkpoint that still holds.

    Falls back to the first step after sign-in (re-running the task steps). The caller must refuse
    if that would repeat a completed irreversible step.
    """
    after_sign_in = len(artifact.sign_in_steps)
    for index in sorted(set(verified), reverse=True):
        if index < after_sign_in:
            break
        checkpoint = artifact.steps[index].checkpoint
        if checkpoint is not None and checker.holds(checkpoint, obs):
            return index + 1
    return after_sign_in
