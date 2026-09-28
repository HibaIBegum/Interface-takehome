"""Redaction applied to everything before it is written to disk or stdout."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


class Redactor:
    """Replaces known secret values and sensitive patterns inside strings, recursively."""

    def __init__(self, secrets: Mapping[str, str] | None = None, patterns: Mapping[str, str] | None = None):
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(((v, k) for k, v in (secrets or {}).items() if v), key=lambda p: -len(p[0]))
        self._patterns = [(re.compile(p), name) for name, p in (patterns or {}).items()]

    def with_secrets(self, secrets: Mapping[str, str]) -> Redactor:
        merged = Redactor()
        merged._secrets = sorted(self._secrets + [(v, k) for k, v in secrets.items() if v], key=lambda p: -len(p[0]))
        merged._patterns = list(self._patterns)
        return merged

    def text(self, value: str) -> str:
        # Patterns first: replacing a short secret inside, say, an SSN would stop the pattern matching it.
        for pattern, name in self._patterns:
            value = pattern.sub(f"[REDACTED:{name}]", value)
        for secret, name in self._secrets:
            value = value.replace(secret, f"[REDACTED:{name}]")
        return value

    def obj(self, value: Any) -> Any:
        """Redact every string inside JSON-like data (dict keys are left alone)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value
