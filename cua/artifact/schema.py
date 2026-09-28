"""CapabilityArtifact: the typed, versioned contract between discovery and replay.

An artifact is everything replay needs to perform a capability deterministically: typed inputs,
ordered steps with ranked locator candidates, per-step checkpoints, the app's error signatures
with their classification, and the success condition. It never contains a secret value; sensitive
inputs are only ever referenced by name in a fill step's value template.
"""

from __future__ import annotations

import re
import string
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.surface.base import FramePath, LocatorCandidate, Target

SCHEMA_VERSION = "1.0"

_ID = r"^[a-z][a-z0-9_-]{1,63}$"
_PARAM = r"^[a-z][a-z0-9_]{0,40}$"
_SEMVER = r"^\d+\.\d+\.\d+$"
_CODE = r"^[A-Z][A-Z0-9_]{1,40}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- conditions

class UrlMatches(_Model):
    """The frame's URL path+query matches a route pattern; `:name` segments match any single value."""

    kind: Literal["url_matches"] = "url_matches"
    route: str
    frame_path: FramePath = Field(default_factory=list)


class ElementVisible(_Model):
    kind: Literal["element_visible"] = "element_visible"
    target: StepTarget


class TextPresent(_Model):
    """Visible text contains `text` (may use {param} templates). frame_path None = any frame."""

    kind: Literal["text_present"] = "text_present"
    text: str
    frame_path: FramePath | None = None


class AllOf(_Model):
    kind: Literal["all"] = "all"
    conditions: list[Condition] = Field(min_length=1)


class AnyOf(_Model):
    kind: Literal["any"] = "any"
    conditions: list[Condition] = Field(min_length=1)


Condition = Annotated[Union[UrlMatches, ElementVisible, TextPresent, AllOf, AnyOf], Field(discriminator="kind")]


def route_regex(route: str) -> re.Pattern[str]:
    """'/app/member?m=:member_id' -> regex over path+query, each :name matching one value."""
    parts = re.split(r"(:[a-z_][a-z0-9_]*)", route)
    body = "".join(r"[^/?&#]+" if p.startswith(":") else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


def iter_conditions(condition: Condition):
    yield condition
    if isinstance(condition, (AllOf, AnyOf)):
        for child in condition.conditions:
            yield from iter_conditions(child)


# ---------------------------------------------------------------- targets

class RankedCandidate(_Model):
    candidate: LocatorCandidate
    why: str  # recorder's note on what makes this candidate robust (or not)


class StepTarget(_Model):
    candidates: list[RankedCandidate] = Field(min_length=1)
    description: str

    def to_target(self) -> Target:
        return Target(candidates=[r.candidate for r in self.candidates], description=self.description)


# ---------------------------------------------------------------- capability parts

class Capability(_Model):
    id: str = Field(pattern=_ID)
    name: str
    description: str  # written for a calling agent: what it does, inputs, outputs, outcomes
    version: str = Field(pattern=_SEMVER)
    status: Literal["draft", "approved"] = "draft"


class TargetApp(_Model):
    vendor_product: str
    app_version: str
    entry_route: str


class ParamSpec(_Model):
    name: str = Field(pattern=_PARAM)
    type: Literal["string", "integer", "decimal"] = "string"
    pattern: str | None = None  # full-match regex a value must satisfy before replay starts
    sensitive: bool = False
    description: str = ""


class OutputSpec(_Model):
    name: str = Field(pattern=_PARAM)
    type: Literal["string", "currency", "integer"] = "string"
    source_step: str
    redact_in_logs: bool = False


StepAction = Literal["click", "fill", "select", "press", "navigate", "wait_for", "extract"]


class Step(_Model):
    id: str = Field(pattern=r"^s\d{2,3}$")
    intent: str
    action: StepAction
    target: StepTarget | None = None
    value_template: str | None = None  # fill value, select option, navigate route or key; {param} templates
    output: str | None = None          # extract: output name
    risk: Literal["safe", "irreversible"] = "safe"
    precondition: Condition | None = None
    checkpoint: Condition | None = None
    performed_by: Literal["agent", "human"] = "agent"

    @model_validator(mode="after")
    def _shape(self) -> Step:
        needs_target = {"click", "fill", "select", "extract"}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"{self.id}: {self.action} needs a target")
        if self.action in {"fill", "select", "navigate", "press"} and self.value_template is None:
            raise ValueError(f"{self.id}: {self.action} needs a value_template")
        if (self.action == "extract") != (self.output is not None):
            raise ValueError(f"{self.id}: output is set exactly on extract steps")
        return self


class RecoveryAction(_Model):
    kind: Literal["dismiss", "retry_step", "restart"]
    target: StepTarget | None = None      # dismiss: what to click
    max_attempts: int = Field(default=1, ge=1, le=5)
    allowed_on_irreversible: bool = False  # retrying a commit step could duplicate it

    @model_validator(mode="after")
    def _shape(self) -> RecoveryAction:
        if (self.kind == "dismiss") != (self.target is not None):
            raise ValueError("dismiss recovery needs a target (and only dismiss has one)")
        return self


class ErrorSignature(_Model):
    id: str = Field(pattern=_ID)
    match: Condition
    classification: Literal["business_outcome", "recoverable", "hard_failure"]
    outcome_code: str = Field(pattern=_CODE)
    message: str
    recovery: RecoveryAction | None = None

    @model_validator(mode="after")
    def _recovery_only_when_recoverable(self) -> ErrorSignature:
        if (self.classification == "recoverable") != (self.recovery is not None):
            raise ValueError(f"{self.id}: recoverable signatures (and only those) carry a recovery action")
        return self


class Provenance(_Model):
    discovery_run_id: str
    models: list[str]  # every model that made a decision in the run (fallbacks included)
    recorded_at: datetime
    human_steps: list[str] = Field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------- the artifact

class CapabilityArtifact(_Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability: Capability
    target_app: TargetApp
    inputs: list[ParamSpec]
    outputs: list[OutputSpec]
    steps: list[Step] = Field(min_length=1)
    error_signatures: list[ErrorSignature]
    success_condition: Condition
    provenance: Provenance

    @model_validator(mode="after")
    def _consistent(self) -> CapabilityArtifact:
        steps = {s.id: s for s in self.steps}
        if len(steps) != len(self.steps):
            raise ValueError("step ids must be unique")
        inputs = {p.name: p for p in self.inputs}
        sensitive = {n for n, p in inputs.items() if p.sensitive}
        for step in self.steps:
            for name in template_fields(step.value_template):
                if name not in inputs:
                    raise ValueError(f"{step.id}: template uses undeclared input {{{name}}}")
                if name in sensitive and step.action != "fill":
                    raise ValueError(f"{step.id}: sensitive input {{{name}}} may only be typed into a field")
            for cond in (step.precondition, step.checkpoint):
                self._check_condition_templates(cond, inputs, sensitive, step.id)
        self._check_condition_templates(self.success_condition, inputs, sensitive, "success_condition")
        for sig in self.error_signatures:
            self._check_condition_templates(sig.match, inputs, sensitive, sig.id)
        for out in self.outputs:
            source = steps.get(out.source_step)
            if source is None or source.output != out.name:
                raise ValueError(f"output {out.name}: source_step must be the extract step producing it")
        return self

    @staticmethod
    def _check_condition_templates(cond, inputs, sensitive, where) -> None:
        if cond is None:
            return
        for c in iter_conditions(cond):
            if isinstance(c, TextPresent):
                for name in template_fields(c.text):
                    if name not in inputs or name in sensitive:
                        raise ValueError(f"{where}: text condition may only use declared, non-sensitive inputs")


def template_fields(template: str | None) -> list[str]:
    if not template:
        return []
    return [field for _, field, _, _ in string.Formatter().parse(template) if field]


def render_template(template: str, values: dict[str, str]) -> str:
    return template.format_map(values)


ElementVisible.model_rebuild()
AllOf.model_rebuild()
AnyOf.model_rebuild()
