"""The single policy chokepoint. Nothing outside this module calls Surface.act() (a test enforces it)."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.surface.base import Action, ActionResult, LocatorCandidate, Navigate, Surface, Target

from .allowlist import host_allowed
from .redact import Redactor
from .risk import Risk, classify

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(min_length=1)
    commit_keywords: list[str] = Field(default_factory=list)
    commit_requires_approval: bool = True
    redact_patterns: dict[str, str] = Field(default_factory=dict)
    screenshot_mask: list[LocatorCandidate] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_POLICY_PATH) -> PolicyConfig:
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def mask_targets(self) -> list[Target]:
        return [Target(candidates=[c], description="policy mask") for c in self.screenshot_mask]

    def redactor(self, secrets: dict[str, str] | None = None) -> Redactor:
        return Redactor(secrets, self.redact_patterns)


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyDecision(BaseModel):
    verdict: Verdict
    risk: Risk
    reason: str
    approved: bool = False  # a commit action that an approver explicitly allowed


class GatedResult(BaseModel):
    decision: PolicyDecision
    result: ActionResult | None  # None when denied: the surface was never touched


# Called for commit actions; returns True to approve. Phase 6 plugs the human operator in here.
Approver = Callable[[Action, Risk], bool]


class PolicyGate:
    def __init__(self, config: PolicyConfig, *, approver: Approver | None = None, base_url: str = ""):
        self.config = config
        self.approver = approver
        self.base_url = base_url

    def check(self, action: Action, current_url: str) -> PolicyDecision:
        risk = classify(action, self.config.commit_keywords)

        def deny(reason: str) -> PolicyDecision:
            return PolicyDecision(verdict=Verdict.DENY, risk=risk, reason=reason)

        on_blank_page = current_url in ("", "about:blank")
        if not on_blank_page and not host_allowed(current_url, self.config.allowed_hosts):
            return deny("current page is not on an allowlisted host")
        if on_blank_page and not isinstance(action, Navigate):
            return deny("only navigation is allowed before a page is loaded")
        if isinstance(action, Navigate) and not host_allowed(action.url, self.config.allowed_hosts, self.base_url):
            return deny("navigation target is not on an allowlisted host")
        if risk is Risk.COMMIT and self.config.commit_requires_approval:
            if self.approver is None:
                return deny("commit action requires approval and no approver is configured")
            if not self.approver(action, risk):
                return deny("commit action was not approved")
            return PolicyDecision(verdict=Verdict.ALLOW, risk=risk, reason="commit approved", approved=True)
        return PolicyDecision(verdict=Verdict.ALLOW, risk=risk, reason="within policy")

    def execute(self, surface: Surface, action: Action, current_url: str) -> GatedResult:
        decision = self.check(action, current_url)
        if decision.verdict is Verdict.DENY:
            return GatedResult(decision=decision, result=None)
        return GatedResult(decision=decision, result=surface.act(action))
