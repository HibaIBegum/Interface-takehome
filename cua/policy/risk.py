"""Risk classification of a single action, from what it does and what it targets."""

from __future__ import annotations

import re
from enum import Enum

from cua.surface.base import Action, Click, Extract, Fill, Navigate, Press, Select, Target, WaitFor


class Risk(str, Enum):
    READ = "read"          # extract, wait_for: observes only
    NAVIGATE = "navigate"  # goto a URL
    INPUT = "input"        # fill, select, press (not Enter), click on anything non-committing
    COMMIT = "commit"      # click on a submit/confirm-like control, or Enter: may change the system of record


def _target_words(target: Target) -> str:
    parts = [target.description] + [c.value for c in target.candidates]
    return " ".join(parts).lower()


def classify(action: Action, commit_keywords: list[str]) -> Risk:
    if isinstance(action, (Extract, WaitFor)):
        return Risk.READ
    if isinstance(action, Navigate):
        return Risk.NAVIGATE
    if isinstance(action, Press) and action.key.lower() in ("enter", "numpadenter"):
        return Risk.COMMIT
    if isinstance(action, Click):
        words = _target_words(action.target)
        if any(re.search(rf"\b{re.escape(k.lower())}\b", words) for k in commit_keywords):
            return Risk.COMMIT
        return Risk.INPUT
    if isinstance(action, (Fill, Select, Press)):
        return Risk.INPUT
    raise TypeError(f"unclassified action {type(action).__name__}")
