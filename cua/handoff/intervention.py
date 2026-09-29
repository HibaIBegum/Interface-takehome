"""What the human is asked (InterventionRequest) and what the human did (captured actions)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cua.observability.runlog import RunLog
from cua.surface.base import FramePath

Trigger = Literal["stuck", "request_human", "replay_failure", "approval"]
Command = Literal["take", "resume", "approve", "deny", "abort"]


class InterventionRequest(BaseModel):
    """Written to runs/<id>/intervention.json and printed. Redacted before either."""

    run_id: str
    subject: str        # capability id + version, or the discovery goal
    step_id: str | None
    trigger: Trigger
    reason: str
    current_url: str
    frames: dict[str, str]
    screenshot: str | None
    allowed_commands: list[Command]
    requested_at: str


def write_request(log: RunLog, request: InterventionRequest, number: int) -> Path:
    """Latest request at intervention.json; every request also kept as intervention-<n>.json and logged."""
    safe = log.redactor.obj(request.model_dump(mode="json"))
    text = json.dumps(safe, indent=2) + "\n"
    (log.dir / f"intervention-{number}.json").write_text(text, encoding="utf-8")
    path = log.dir / "intervention.json"
    path.write_text(text, encoding="utf-8")
    log.step({"event": "intervention_requested", "number": number, **safe})
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HumanActionRecorder:
    """Receives capture events from the surface; logs the ones inside the human's control window."""

    def __init__(self, log: RunLog):
        self.log = log
        self.window: tuple[float, float] | None = None  # epoch ms [start, end]; end=inf while open
        self.actions: list[dict] = []

    def open(self) -> None:
        self.window = (time.time() * 1000, float("inf"))

    def close(self) -> None:
        if self.window is not None:
            self.window = (self.window[0], time.time() * 1000)

    def __call__(self, frame_path: FramePath, payload: dict) -> None:
        at = float(payload.get("at", 0))
        if self.window is None or not (self.window[0] <= at <= self.window[1]):
            return  # automation's own clicks fire the same DOM events; only the human window counts
        kind = payload.get("kind")
        action = {"event": "human_action", "kind": kind, "frame_path": frame_path,
                  "at": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat(timespec="milliseconds")}
        if kind == "navigate":
            action["url"] = payload.get("url", "")
        else:
            action.update({k: payload.get(k, "") for k in ("role", "name", "label", "field_name")})
            if kind in ("fill", "select"):
                action["value"] = "[secret]" if payload.get("sensitive") else payload.get("value")
        self.actions.append(action)
        self.log.step(action)  # redacted on write
        what = action.get("url") or action.get("name") or action.get("label") or action.get("field_name")
        value = f" = {action['value']}" if "value" in action else ""
        self.log.echo(f"   [human] {kind} {what!r}{value}")
