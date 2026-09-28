"""Per-run output directory: redacted JSONL step log, JSON summaries, masked screenshots."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.policy.redact import Redactor


def new_run_dir(root: Path, label: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "run"
    return root / f"{stamp}-{slug}"


class RunLog:
    def __init__(self, run_dir: Path, redactor: Redactor, *, echo: bool = True):
        run_dir.mkdir(parents=True, exist_ok=True)
        self.dir = run_dir
        self.redactor = redactor
        self._echo = echo
        self._steps_path = run_dir / "steps.jsonl"

    def _safe(self, record: BaseModel | dict[str, Any]) -> Any:
        data = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        return self.redactor.obj(data)

    def step(self, record: BaseModel | dict[str, Any]) -> None:
        with self._steps_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self._safe(record), ensure_ascii=False) + "\n")

    def write_json(self, name: str, record: BaseModel | dict[str, Any]) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(self._safe(record), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def echo(self, message: str) -> None:
        if self._echo:
            print(self.redactor.text(message), flush=True)

    def screenshot_path(self, step: int) -> Path:
        return self.dir / f"step_{step:03d}.png"
