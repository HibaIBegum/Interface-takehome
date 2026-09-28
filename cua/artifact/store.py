"""Artifact persistence: artifacts/<id>/v<major>.json plus a generated v<major>.md summary."""

from __future__ import annotations

import re
from pathlib import Path

from cua.policy.redact import Redactor

from .schema import AllOf, CapabilityArtifact, Condition, ElementVisible, TextPresent, UrlMatches

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "artifacts"


class StoreError(Exception):
    pass


class ArtifactStore:
    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = root

    def path(self, capability_id: str, major: int | str) -> Path:
        return self.root / capability_id / f"v{major}.json"

    def save(self, artifact: CapabilityArtifact, redactor: Redactor, *, overwrite: bool = False) -> Path:
        """Redacts, re-validates and writes the JSON and its markdown summary. Never overwrites silently."""
        json_path = self.path(artifact.capability.id, artifact.capability.version.split(".")[0])
        if json_path.exists() and not overwrite:
            raise StoreError(f"{json_path} already exists; bump the major version or pass overwrite")
        safe = CapabilityArtifact.model_validate(redactor.obj(artifact.model_dump(mode="json")))
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(safe.model_dump_json(indent=2) + "\n", encoding="utf-8")
        json_path.with_suffix(".md").write_text(redactor.text(render_markdown(safe)), encoding="utf-8")
        return json_path

    def load(self, capability_id: str, major: int | None = None) -> CapabilityArtifact:
        """A specific major version, or the highest one present."""
        if major is None:
            versions = sorted(int(m.group(1)) for f in (self.root / capability_id).glob("v*.json")
                              if (m := re.fullmatch(r"v(\d+)\.json", f.name)))
            if not versions:
                raise StoreError(f"no artifact named {capability_id!r} in {self.root}")
            major = versions[-1]
        return CapabilityArtifact.model_validate_json(self.path(capability_id, major).read_text())



def _cond_text(cond: Condition | None) -> str:
    if cond is None:
        return "-"
    if isinstance(cond, UrlMatches):
        return f"`{'/'.join(cond.frame_path) or 'top'}` at `{cond.route}`"
    if isinstance(cond, TextPresent):
        return f"text \"{cond.text}\""
    if isinstance(cond, ElementVisible):
        return f"{cond.target.description} visible"
    joiner = " and " if isinstance(cond, AllOf) else " or "
    return joiner.join(_cond_text(c) for c in cond.conditions)


def render_markdown(a: CapabilityArtifact) -> str:
    c = a.capability
    lines = [
        f"# {c.name} (`{c.id}` v{c.version}, {c.status})", "",
        c.description, "",
        f"**App:** {a.target_app.vendor_product} {a.target_app.app_version}, entry `{a.target_app.entry_route}`  ",
        f"**Recorded:** {a.provenance.recorded_at:%Y-%m-%d %H:%M} UTC from run `{a.provenance.discovery_run_id}` "
        f"by {', '.join(a.provenance.models) or 'unknown model'}", "",
        "## Inputs", "", "| Name | Type | Pattern | Sensitive | Description |", "|---|---|---|---|---|",
    ]
    lines += [f"| `{p.name}` | {p.type} | {f'`{p.pattern}`' if p.pattern else '-'} | {'yes' if p.sensitive else 'no'} "
              f"| {p.description} |" for p in a.inputs]
    lines += ["", "## Outputs", "", "| Name | Type | From step | Redact in logs |", "|---|---|---|---|"]
    lines += [f"| `{o.name}` | {o.type} | {o.source_step} | {'yes' if o.redact_in_logs else 'no'} |" for o in a.outputs]
    lines += ["", "## Steps", "", "| Step | Intent | Risk | Best locator (of n) | Checkpoint |", "|---|---|---|---|---|"]
    for s in a.steps:
        best = "-"
        if s.target:
            cand = s.target.candidates[0].candidate
            best = f"{cand.strategy.value} `{cand.value}` ({len(s.target.candidates)})"
        who = " (human)" if s.performed_by == "human" else ""
        lines.append(f"| {s.id} | {s.intent}{who} | {s.risk} | {best} | {_cond_text(s.checkpoint)} |")
    lines += ["", f"**Success when:** {_cond_text(a.success_condition)}", "",
              "## Error signatures", "", "| Outcome | Class | Matches | Recovery |", "|---|---|---|---|"]
    for sig in a.error_signatures:
        rec = f"{sig.recovery.kind} (max {sig.recovery.max_attempts})" if sig.recovery else "-"
        lines.append(f"| `{sig.outcome_code}` | {sig.classification} | {_cond_text(sig.match)} | {rec} |")
    lines += ["", "## Provenance", "", a.provenance.note, ""]
    return "\n".join(lines)
