"""Evaluating artifact Conditions against the live screen, and waiting for them (bounded, event-driven)."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from cua.artifact.schema import AllOf, AnyOf, Condition, ElementVisible, TextPresent, UrlMatches, route_regex
from cua.surface.base import Observation, Surface

_RECHECK_MS = 500  # longest gap between checks when nothing navigates (e.g. text appearing via script)


def _route_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def frame_texts(obs: Observation) -> dict[str, str]:
    texts: dict[str, str] = {}
    for line in obs.visible_text.splitlines():
        if m := re.match(r"^\[([^\]]+)\] (.*)$", line):
            texts[m.group(1)] = m.group(2)
    return texts


def describe_observation(obs: Observation | None) -> str:
    if obs is None:
        return "(no observation)"
    frames = ", ".join(f"{'/'.join(f.path) or 'top'}={_route_of(f.url)}" for f in obs.frames)
    text = obs.visible_text.replace("\n", " | ")
    return f"frames: {frames}; text: {text[:400]}"


class ConditionChecker:
    def __init__(self, surface: Surface, text_values: dict[str, str]):
        self.surface = surface
        self.text_values = text_values  # non-sensitive params only: text conditions may use {name}

    def observe(self) -> Observation:
        return self.surface.observe()

    def holds(self, cond: Condition, obs: Observation) -> bool:
        if isinstance(cond, UrlMatches):
            frame = next((f for f in obs.frames if f.path == cond.frame_path), None)
            pattern = route_regex(cond.route, self.text_values)
            return frame is not None and pattern.match(_route_of(frame.url)) is not None
        if isinstance(cond, TextPresent):
            text = cond.text.format_map(self.text_values)
            texts = frame_texts(obs)
            if cond.frame_path is None:
                return any(text in t for t in texts.values())
            return text in texts.get("/".join(cond.frame_path) or "top", "")
        if isinstance(cond, ElementVisible):
            return self.surface.locate(cond.target.to_target(), timeout_ms=0).matched_index is not None
        if isinstance(cond, AllOf):
            return all(self.holds(c, obs) for c in cond.conditions)
        if isinstance(cond, AnyOf):
            return any(self.holds(c, obs) for c in cond.conditions)
        raise TypeError(f"unknown condition {type(cond).__name__}")

    def wait_until(self, cond: Condition, timeout_ms: float) -> tuple[bool, Observation]:
        """Re-check on every frame navigation (or at most every 500 ms) until true or the deadline."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            obs = self.observe()
            if self.holds(cond, obs):
                return True, obs
            remaining = (deadline - time.monotonic()) * 1000
            if remaining <= 0:
                return False, obs
            self.surface.wait_for_navigation(min(remaining, _RECHECK_MS))
