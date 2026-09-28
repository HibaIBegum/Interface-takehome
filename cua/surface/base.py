"""Surface protocol and the data models that cross it.

Agent, recorder and replay only ever see these types. Anything that can drive a UI
(Playwright, a desktop accessibility API, a terminal emulator) implements `Surface`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

FramePath = list[str]  # frame names from the top document down; unnamed frames as "[i]"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- targets

class Strategy(str, Enum):
    ROLE_NAME = "role_name"  # ARIA role + exact accessible name
    LABEL = "label"          # form control by its label (real <label>/aria-label, or table caption cell)
    TEXT_NEAR = "text_near"  # the cell next to a caption cell with this text (optionally narrowed by role)
    CSS = "css"              # raw CSS selector; brittle, last structural resort
    COORDS = "coords"        # "x,y" in top-level viewport pixels; no structural check at all


class LocatorCandidate(_Model):
    strategy: Strategy
    value: str
    role: str | None = None
    frame_path: FramePath = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> LocatorCandidate:
        if self.strategy is Strategy.ROLE_NAME and not self.role:
            raise ValueError("role_name candidates need a role")
        if self.strategy is Strategy.COORDS:
            self.point()  # validates format
        return self

    def point(self) -> tuple[float, float]:
        x, y = (float(p) for p in self.value.split(","))
        return x, y


class Target(_Model):
    """Ordered fallbacks for one UI element. Resolution takes the first candidate with exactly one match."""

    candidates: list[LocatorCandidate] = Field(min_length=1)
    description: str = ""


class CandidateAttempt(_Model):
    index: int
    strategy: Strategy
    matches: int


class Resolution(_Model):
    """How a target was (or was not) resolved. `matched_index` is None on failure."""

    matched_index: int | None
    strategy: Strategy | None
    attempts: list[CandidateAttempt]


# ---------------------------------------------------------------- observation

class BBox(_Model):
    x: float
    y: float
    width: float
    height: float

    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


class ElementInfo(_Model):
    index: int
    role: str
    name: str
    label: str
    nearby_text: str
    frame_path: FramePath
    bbox: BBox | None
    options: list[str] | None = None  # visible option labels, for comboboxes
    occluded: bool = False            # something else (e.g. a modal) is on top of its center point


class FrameInfo(_Model):
    path: FramePath
    url: str
    title: str


class Observation(_Model):
    url: str
    title: str
    frames: list[FrameInfo]
    elements: list[ElementInfo]
    visible_text: str
    screenshot_path: Path | None = None
    state_hash: str  # identifies the *screen* (frame URL paths + element roles/names), not its data


def target_for(element: ElementInfo) -> Target:
    """Structural candidates for an observed element, most semantic first. Never coords."""
    candidates: list[LocatorCandidate] = []
    if element.name:
        candidates.append(LocatorCandidate(
            strategy=Strategy.ROLE_NAME, role=element.role, value=element.name, frame_path=element.frame_path))
    if element.label:
        candidates.append(LocatorCandidate(
            strategy=Strategy.LABEL, value=element.label, frame_path=element.frame_path))
    if not candidates:
        raise ValueError(f"element {element.index} has neither name nor label")
    return Target(candidates=candidates, description=f"{element.role} {element.name or element.label!r}")


# ---------------------------------------------------------------- actions

class Click(_Model):
    kind: Literal["click"] = "click"
    target: Target


class Fill(_Model):
    kind: Literal["fill"] = "fill"
    target: Target
    value: str = Field(repr=False)
    sensitive: bool = False


class Select(_Model):
    kind: Literal["select"] = "select"
    target: Target
    option: str  # visible option label


class Press(_Model):
    kind: Literal["press"] = "press"
    key: str
    target: Target | None = None  # None: press on whatever has focus


class Navigate(_Model):
    kind: Literal["navigate"] = "navigate"
    url: str  # absolute, or relative to the surface's base URL


class WaitFor(_Model):
    kind: Literal["wait_for"] = "wait_for"
    target: Target | None = None
    text: str | None = None
    frame_path: FramePath = Field(default_factory=list)  # for `text`
    state: Literal["visible", "hidden"] = "visible"
    timeout_ms: int | None = None

    @model_validator(mode="after")
    def _one_condition(self) -> WaitFor:
        if (self.target is None) == (self.text is None):
            raise ValueError("wait_for needs exactly one of target or text")
        return self


class Extract(_Model):
    kind: Literal["extract"] = "extract"
    target: Target


Action = Annotated[Union[Click, Fill, Select, Press, Navigate, WaitFor, Extract], Field(discriminator="kind")]


class ErrorCode(str, Enum):
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_AMBIGUOUS = "target_ambiguous"
    OBSTRUCTED = "obstructed"        # element found but something else receives the click
    TIMEOUT = "timeout"
    NAVIGATION_FAILED = "navigation_failed"
    ACTION_FAILED = "action_failed"


class ActionResult(_Model):
    kind: str
    ok: bool
    resolution: Resolution | None = None
    extracted: str | None = None
    error_code: ErrorCode | None = None
    error: str | None = None
    duration_ms: int


class TargetResolutionError(Exception):
    def __init__(self, target: Target, resolution: Resolution):
        self.target = target
        self.resolution = resolution
        counts = ", ".join(f"{a.strategy.value}={a.matches}" for a in resolution.attempts)
        super().__init__(f"could not resolve {target.description or 'target'} uniquely ({counts})")

    @property
    def code(self) -> ErrorCode:
        if any(a.matches > 1 for a in self.resolution.attempts):
            return ErrorCode.TARGET_AMBIGUOUS
        return ErrorCode.TARGET_NOT_FOUND


# ---------------------------------------------------------------- protocol

class Surface(Protocol):
    def observe(self, screenshot_path: Path | None = None) -> Observation: ...

    def act(self, action: Action) -> ActionResult: ...

    def extract(self, target: Target) -> str: ...

    def screenshot(self, path: Path) -> Path: ...
