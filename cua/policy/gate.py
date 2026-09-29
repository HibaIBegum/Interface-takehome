"""The single policy chokepoint. Nothing outside this module calls Surface.act() (a test enforces it).

For every action: action type allowed? page and target frame on an allowed origin + route?
navigation target allowed? Then risk: irreversible actions are held until an approver decides.
Discovery and draft replays additionally require that decision to come from a human.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.surface.base import Action, ActionResult, Fill, LocatorCandidate, Navigate, Strategy, Surface, Target

from .allowlist import url_allowed
from .redact import Redactor
from .risk import IrreversibleMarker, Risk, classify

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_origins: list[str] = Field(min_length=1)
    allowed_routes: list[str] = Field(min_length=1)
    allowed_actions: list[str] = Field(min_length=1)
    irreversible_markers: list[IrreversibleMarker] = Field(default_factory=list)
    commit_keywords: list[str] = Field(default_factory=list)
    redact_patterns: dict[str, str] = Field(default_factory=dict)
    sensitive_keys: list[str] = Field(default_factory=list)
    screenshot_mask: list[LocatorCandidate] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> PolicyConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def mask_targets(self) -> list[Target]:
        return [Target(candidates=[c], description="policy mask") for c in self.screenshot_mask]

    def redactor(self, secrets: dict[str, str] | None = None) -> Redactor:
        return Redactor(secrets, self.redact_patterns, self.sensitive_keys)


# ---------------------------------------------------------------- approval

class ApprovalRequest(BaseModel):
    """What a human sees before an irreversible action. Built already redacted."""

    context: str   # e.g. "replay of open-sub-account v1.0.0 (draft), step s12"
    action: str
    target: str
    value: str | None = None
    page: str      # route of the frame the action happens in
    reason: str    # why it is considered irreversible


class ApprovalDecision(BaseModel):
    approved: bool
    by: str
    by_human: bool
    note: str = ""


Approver = Callable[[ApprovalRequest], ApprovalDecision]


def deny_all(request: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(approved=False, by="policy", by_human=False, note="no approver configured")


# ---------------------------------------------------------------- decisions

class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"  # outside the allowlist: never asked, never executed
    DENY = "deny"    # irreversible and not approved (by a human, where one is required)


class PolicyDecision(BaseModel):
    verdict: Verdict
    risk: Risk
    reason: str
    approval: ApprovalDecision | None = None


class GatedResult(BaseModel):
    decision: PolicyDecision
    result: ActionResult | None  # None unless allowed: the surface was never touched


def _frame_path(action: Action) -> list[str] | None:
    target: Target | None = getattr(action, "target", None)
    if target is None:
        return None
    structural = next((c for c in target.candidates if c.strategy is not Strategy.COORDS), None)
    return structural.frame_path if structural is not None else []


class PolicyGate:
    def __init__(self, config: PolicyConfig, *, approver: Approver = deny_all, redactor: Redactor | None = None,
                 base_url: str = ""):
        self.config = config
        self.approver = approver
        self.redactor = redactor or config.redactor()
        self.base_url = base_url
        # Set by a handoff session: automation may only act while it owns the live session.
        self.may_act: Callable[[], bool] = lambda: True

    def check(self, action: Action, *, page_url: str, frame_url: str | None, irreversible: bool = False,
              require_human: bool = True, context: str = "") -> PolicyDecision:
        cfg = self.config
        risk, why = classify(action, frame_url=frame_url, markers=cfg.irreversible_markers,
                             commit_keywords=cfg.commit_keywords)
        if irreversible and risk is not Risk.COMMIT:  # the artifact's own marking can only raise risk
            risk, why = Risk.COMMIT, "step is marked irreversible in the artifact"

        def block(reason: str) -> PolicyDecision:
            return PolicyDecision(verdict=Verdict.BLOCK, risk=risk, reason=reason)

        if action.kind not in cfg.allowed_actions:
            return block(f"action type {action.kind!r} is not allowed")
        on_blank_page = page_url in ("", "about:blank")
        if on_blank_page and not isinstance(action, Navigate):
            return block("only navigation is allowed before a page is loaded")
        for label, url in (("page", None if on_blank_page else page_url), ("frame", frame_url)):
            if url:
                ok, reason = url_allowed(url, cfg.allowed_origins, cfg.allowed_routes)
                if not ok:
                    return block(f"{label} not allowlisted: {reason}")
        if isinstance(action, Navigate):
            ok, reason = url_allowed(action.url, cfg.allowed_origins, cfg.allowed_routes, self.base_url)
            if not ok:
                return block(f"navigation target not allowlisted: {reason}")

        if risk is not Risk.COMMIT:
            return PolicyDecision(verdict=Verdict.ALLOW, risk=risk, reason="within policy")
        decision = self.approver(self._request(action, frame_url or page_url, why, context))
        if not decision.approved:
            return PolicyDecision(verdict=Verdict.DENY, risk=risk, approval=decision,
                                  reason=f"irreversible action not approved ({decision.by}: {decision.note or 'denied'})")
        if require_human and not decision.by_human:
            return PolicyDecision(verdict=Verdict.DENY, risk=risk, approval=decision,
                                  reason=f"irreversible action needs a human decision; {decision.by} is not one")
        return PolicyDecision(verdict=Verdict.ALLOW, risk=risk, approval=decision,
                              reason=f"irreversible action approved by {decision.by}")

    def execute(self, surface: Surface, action: Action, *, irreversible: bool = False, require_human: bool = True,
                context: str = "") -> GatedResult:
        if not self.may_act():
            return GatedResult(result=None, decision=PolicyDecision(
                verdict=Verdict.BLOCK, risk=Risk.READ, reason="automation does not hold control of the session"))
        frame_path = _frame_path(action)
        frame_url = surface.frame_url(frame_path) if frame_path else None
        decision = self.check(action, page_url=surface.current_url(), frame_url=frame_url,
                              irreversible=irreversible, require_human=require_human, context=context)
        if decision.verdict is not Verdict.ALLOW:
            return GatedResult(decision=decision, result=None)
        return GatedResult(decision=decision, result=surface.act(action))

    def _request(self, action: Action, url: str, why: str, context: str) -> ApprovalRequest:
        target = getattr(action, "target", None)
        value = None
        if isinstance(action, Fill):
            value = "[secret]" if action.sensitive else self.redactor.text(action.value)
        return ApprovalRequest(
            context=self.redactor.text(context), action=action.kind,
            target=self.redactor.text(target.description if target else getattr(action, "key", "")),
            value=value, page=urlsplit(url).path, reason=why,
        )
