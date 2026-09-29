"""Risk classification of a single action: explicit irreversible markers first, keyword fallback second."""

from __future__ import annotations

import re
from enum import Enum
from fnmatch import fnmatchcase
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from cua.surface.base import Action, Click, Extract, Fill, Navigate, Press, Select, Target, WaitFor


class Risk(str, Enum):
    READ = "read"          # extract, wait_for: observes only
    NAVIGATE = "navigate"  # goto a URL
    INPUT = "input"        # fill, select, press (not Enter), click on anything non-committing
    COMMIT = "commit"      # irreversible: may change the system of record


class IrreversibleMarker(BaseModel):
    """`target` (a whole word, case-insensitive) on `route` commits a change."""

    model_config = ConfigDict(extra="forbid")
    route: str
    action: str = "click"
    target: str


def _target_words(target: Target) -> str:
    return " ".join([target.description] + [c.value for c in target.candidates]).lower()


def _mentions(words: str, term: str) -> bool:
    return re.search(rf"\b{re.escape(term.lower())}\b", words) is not None


def classify(action: Action, *, frame_url: str | None, markers: list[IrreversibleMarker],
             commit_keywords: list[str]) -> tuple[Risk, str]:
    """(risk, why). Explicit markers are checked before the keyword fallback."""
    if isinstance(action, (Extract, WaitFor)):
        return Risk.READ, "read-only"
    if isinstance(action, Navigate):
        return Risk.NAVIGATE, "navigation"
    if isinstance(action, Press) and action.key.lower() in ("enter", "numpadenter"):
        return Risk.COMMIT, "Enter may submit a form"
    target = getattr(action, "target", None)
    if target is not None and frame_url is not None:
        path = urlsplit(frame_url).path
        for m in markers:
            if m.action == action.kind and fnmatchcase(path, m.route) and _mentions(_target_words(target), m.target):
                return Risk.COMMIT, f"marked irreversible: {m.target} on {m.route}"
    if isinstance(action, Click):
        for keyword in commit_keywords:
            if _mentions(_target_words(action.target), keyword):
                return Risk.COMMIT, f"control name contains {keyword!r}"
        return Risk.INPUT, "click"
    if isinstance(action, (Fill, Select, Press)):
        return Risk.INPUT, action.kind
    raise TypeError(f"unclassified action {type(action).__name__}")
