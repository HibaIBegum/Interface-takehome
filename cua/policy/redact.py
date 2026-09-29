"""Redaction applied to everything before it is written to disk or stdout."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


class Redactor:
    """Replaces sensitive patterns, known secret values, and values under sensitive keys.

    Order matters: patterns run before secret values, because replacing a short secret inside
    (say) an SSN first would stop the SSN pattern from matching it.
    """

    def __init__(self, secrets: Mapping[str, str] | None = None, patterns: Mapping[str, str] | None = None,
                 sensitive_keys: Iterable[str] = ()):
        self._secrets = self._sorted((v, k) for k, v in (secrets or {}).items() if v)
        self._patterns = [(re.compile(p), name) for name, p in (patterns or {}).items()]
        self._keys = {k.lower() for k in sensitive_keys}

    @staticmethod
    def _sorted(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        # Longest first, so a secret that contains another is replaced whole.
        return sorted(pairs, key=lambda pair: -len(pair[0]))

    def with_secrets(self, secrets: Mapping[str, str]) -> Redactor:
        merged = Redactor()
        merged._secrets = self._sorted(self._secrets + [(v, k) for k, v in secrets.items() if v])
        merged._patterns = list(self._patterns)
        merged._keys = set(self._keys)
        return merged

    def text(self, value: str) -> str:
        for pattern, name in self._patterns:
            value = pattern.sub(f"[REDACTED:{name}]", value)
        for secret, name in self._secrets:
            value = value.replace(secret, f"[REDACTED:{name}]")
        return value

    def obj(self, value: Any) -> Any:
        """Redact every string inside JSON-like data. Keys are kept; values under sensitive keys go whole."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {k: (f"[REDACTED:{k}]" if isinstance(k, str) and k.lower() in self._keys and v not in (None, "")
                        and not isinstance(v, (Mapping, list, tuple, bool)) else self.obj(v))
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value
