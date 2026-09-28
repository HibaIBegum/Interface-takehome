"""System prompt, tool schemas and per-step rendering for the discovery agent."""

from __future__ import annotations

import base64
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cua.surface.base import ElementInfo, Observation

SYSTEM_PROMPT = """\
You operate a legacy back-office web application for a credit union, one UI action at a time, to \
accomplish the operator's goal. This is the institution's own internal system: its operations staff \
started this run, and the sign-in credentials are theirs, supplied for this task. Your successful run \
is recorded and later replayed without you, so take the direct, conventional path a trained teller \
would take.

How to act:
- Every turn, call exactly one tool. Refer to elements by their [index] in the current ELEMENTS list; \
indexes change between screens.
- Give `reason` as one short sentence. It is written to the audit log.
- Type parameters by reference, as the exact string {{name}}, never by retyping their value. Secret \
parameters (such as passwords) are only available by reference.
- Read values with extract(name, element_index) on the data cell holding the value, using a short \
snake_case name. Rows marked `cell` are read-only data; you cannot click or fill them.
- If an unexpected notice or modal appears, dismiss it and continue.
- Only use controls that submit, confirm or commit changes when the goal requires it. Such actions may \
be denied by policy; if that blocks the goal, call request_human.
- Call done(summary) as soon as the goal is achieved. Call request_human(reason) instead of guessing \
when you are blocked: access denied, missing data, a business rule stopping you, or anything ambiguous.
"""


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClickInput(_ToolInput):
    element_index: int
    reason: str


class FillInput(_ToolInput):
    element_index: int
    value: str = Field(description="Literal text, or a parameter reference like {{member_id}}.")
    reason: str


class SelectInput(_ToolInput):
    element_index: int
    option: str = Field(description="Visible option label, or a parameter reference.")
    reason: str


class NavigateInput(_ToolInput):
    url: str
    reason: str


class ExtractInput(_ToolInput):
    name: str = Field(description="snake_case name for the value, e.g. savings_balance.")
    element_index: int
    reason: str


class DoneInput(_ToolInput):
    summary: str = Field(description="What was accomplished, including any values read.")


class RequestHumanInput(_ToolInput):
    reason: str


TOOL_INPUTS: dict[str, type[_ToolInput]] = {
    "click": ClickInput,
    "fill": FillInput,
    "select": SelectInput,
    "navigate": NavigateInput,
    "extract": ExtractInput,
    "done": DoneInput,
    "request_human": RequestHumanInput,
}

_DESCRIPTIONS = {
    "click": "Click a button or link.",
    "fill": "Replace the contents of a text field.",
    "select": "Choose an option in a dropdown.",
    "navigate": "Go to a URL (same application only).",
    "extract": "Read the value shown in a data cell and remember it under a name.",
    "done": "Finish: the goal has been achieved.",
    "request_human": "Stop and ask a human operator for help.",
}


def _strip_titles(schema: dict) -> dict:
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


def tool_definitions() -> list[dict]:
    return [
        {"name": name, "description": _DESCRIPTIONS[name], "strict": True,
         "input_schema": _strip_titles(model.model_json_schema())}
        for name, model in TOOL_INPUTS.items()
    ]


def describe_element(e: ElementInfo) -> str:
    frame = f" (frame {'/'.join(e.frame_path)})" if e.frame_path else ""
    if e.role == "cell":
        where = f"row {e.label!r}" + (f", column {e.column!r}" if e.column else "")
        return f"[{e.index}] cell {where}: {e.text!r}{frame}"
    text = f"[{e.index}] {e.role}"
    if e.name:
        text += f" {e.name!r}"
    if e.label and e.label != e.name:
        text += f" label={e.label!r}"
    if e.options:
        text += f" options={e.options}"
    if e.occluded:
        text += " (covered by an overlay)"
    return text + frame


def render_step(*, goal: str, params: list[tuple[str, str | None]], extracted: dict[str, str],
                history: list[str], obs: Observation, step: int, max_steps: int) -> list[dict]:
    """The user turn for one step. `params` holds (name, value), with value None for secrets."""
    lines = [f"GOAL: {goal}", "", "PARAMETERS (type as {{name}}):"]
    lines += [f"  {n} = {v!r}" if v is not None else f"  {n} = <secret>" for n, v in params] or ["  (none)"]
    lines += ["", "EXTRACTED SO FAR:"]
    lines += [f"  {k} = {v!r}" for k, v in extracted.items()] or ["  (nothing yet)"]
    lines += ["", "HISTORY:"] + ([f"  {h}" for h in history] or ["  (no actions yet)"])
    frames = ", ".join(f"{'/'.join(f.path) or 'top'}={f.url}" for f in obs.frames)
    lines += ["", f"CURRENT SCREEN (step {step} of at most {max_steps}): {obs.title!r}", f"  frames: {frames}",
              "", "ELEMENTS:"] + [f"  {describe_element(e)}" for e in obs.elements]
    lines += ["", "VISIBLE TEXT:", obs.visible_text or "(none)"]

    content: list[dict] = []
    if obs.screenshot_path is not None and Path(obs.screenshot_path).exists():
        data = base64.standard_b64encode(Path(obs.screenshot_path).read_bytes()).decode("ascii")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    content.append({"type": "text", "text": "\n".join(lines)})
    return content
