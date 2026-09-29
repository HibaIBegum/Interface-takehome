"""Turn a successful discovery run (run.json + steps.jsonl) into a draft CapabilityArtifact.

The recorder works offline from the run log: it never re-opens the app. What it keeps, and why:
- successful actions only (failed, rejected and policy-denied attempts are the agent's detours);
- minus actions taken while a *recoverable* signature was on screen (e.g. dismissing a system
  notice): that is incidental recovery, which replay handles through the signature instead;
- values become templates ({member_id}); secrets were only ever logged as references.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml
from pydantic import BaseModel

from cua.observability.records import ObservationSummary, RunManifest, StepRecord
from cua.surface.base import ElementInfo, LocatorCandidate, Strategy

from .schema import (
    AllOf, AnyOf, Capability, CapabilityArtifact, Condition, ElementVisible, ErrorSignature, OutputSpec,
    ParamSpec, Provenance, RankedCandidate, Step, StepTarget, TargetApp, TextPresent, UrlMatches,
)

DEFAULT_APP_CATALOG = Path(__file__).resolve().parents[2] / "config" / "apps" / "ffcu_member_services.yaml"
_REF = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
_ID_LIKE = re.compile(r"^(\d{3,}|[0-9a-f]{8,}|[0-9a-f-]{32,36})$", re.IGNORECASE)
_SENSITIVE_OUTPUT = re.compile(r"ssn|social security|tax id|account number|routing", re.IGNORECASE)
_ACTIONS = {"click", "fill", "select", "press", "navigate", "extract"}
MAX_CANDIDATES = 4


class AppInfo(BaseModel):
    vendor_product: str
    app_version: str


class AppCatalog(BaseModel):
    app: AppInfo
    signatures: list[ErrorSignature]

    @classmethod
    def load(cls, path: Path = DEFAULT_APP_CATALOG) -> AppCatalog:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class RecordingError(Exception):
    pass


# ---------------------------------------------------------------- routes and templates

def normalize_route(url: str, examples: dict[str, str]) -> str:
    """Path+query of `url`, with parameter values as :name and other id-like values as :id."""
    parts = urlsplit(url)

    def token(value: str) -> str:
        if value in examples:
            return f":{examples[value]}"
        return ":id" if _ID_LIKE.match(value) else value

    path = "/".join(token(seg) if seg else seg for seg in parts.path.split("/"))
    query = "&".join(f"{k}={token(v)}" for k, v in parse_qsl(parts.query, keep_blank_values=True))
    return path + (f"?{query}" if query else "")


def to_template(raw: str, examples: dict[str, str]) -> str:
    """'{{member_id}}' or a literal equal to a param's example value -> '{member_id}'; else escaped literal."""
    if (m := _REF.fullmatch(raw.strip())) is not None:
        return "{" + m.group(1) + "}"
    if raw in examples:
        return "{" + examples[raw] + "}"
    return raw.replace("{", "{{").replace("}", "}}")


def url_template(url: str, examples: dict[str, str]) -> str:
    parts = urlsplit(url)
    path = "/".join("{" + examples[s] + "}" if s in examples else s for s in parts.path.split("/"))
    query = "&".join(f"{k}=" + ("{" + examples[v] + "}" if v in examples else v)
                     for k, v in parse_qsl(parts.query, keep_blank_values=True))
    return path + (f"?{query}" if query else "")


# ---------------------------------------------------------------- locator candidates

def build_candidates(el: ElementInfo) -> list[RankedCandidate]:
    """2-4 ranked candidates: semantic first, structural next, coordinates last."""
    fp = el.frame_path
    ranked: list[RankedCandidate] = []
    if el.role == "cell":
        ranked.append(RankedCandidate(
            candidate=LocatorCandidate(strategy=Strategy.TEXT_NEAR, value=el.label, column=el.column or None,
                                       frame_path=fp),
            why=("row caption + column header: survives row reordering and restyling" if el.column
                 else "the value cell right of its caption, as a teller reads the screen"),
        ))
    else:
        if el.name:
            ranked.append(RankedCandidate(
                candidate=LocatorCandidate(strategy=Strategy.ROLE_NAME, role=el.role, value=el.name, frame_path=fp),
                why="ARIA role + accessible name, how assistive tech finds it; survives layout and styling changes",
            ))
        if el.label:
            ranked.append(RankedCandidate(
                candidate=LocatorCandidate(strategy=Strategy.LABEL, value=el.label, frame_path=fp),
                why="the visible caption a teller reads next to the field",
            ))
        if el.field_name:
            ranked.append(RankedCandidate(
                candidate=LocatorCandidate(strategy=Strategy.CSS, value=f'[name="{el.field_name}"]', frame_path=fp),
                why="HTML form field name: the server's POST contract, stable across reskins but invisible to users",
            ))
        if el.label and len(ranked) < MAX_CANDIDATES - 1:
            ranked.append(RankedCandidate(
                candidate=LocatorCandidate(strategy=Strategy.TEXT_NEAR, value=el.label, role=el.role, frame_path=fp),
                why="the control in the table cell right of its caption",
            ))
    ranked = ranked[:MAX_CANDIDATES - 1]
    if el.bbox is not None:
        x, y = el.bbox.center()
        ranked.append(RankedCandidate(
            candidate=LocatorCandidate(strategy=Strategy.COORDS, value=f"{x:.0f},{y:.0f}"),
            why="last resort: position at the recorded 1280x800 viewport; breaks on any layout change",
        ))
    if not ranked:
        raise RecordingError(f"element [{el.index}] has nothing to locate it by")
    return ranked


def _output_type(example: str) -> str:
    # The run log is redacted, so a balance arrives as "[REDACTED:money]" rather than "$5,230.17".
    if example.startswith("$") or example == "[REDACTED:money]":
        return "currency"
    return "integer" if example.isdigit() else "string"


def _describe(el: ElementInfo) -> str:
    if el.role == "cell":
        return f"'{el.label}'" + (f" / '{el.column}'" if el.column else "")
    return f"'{el.name or el.label}'"


# ---------------------------------------------------------------- conditions from the log

def _frame_texts(obs: ObservationSummary) -> dict[str, str]:
    texts: dict[str, str] = {}
    for line in obs.visible_text.splitlines():
        if m := re.match(r"^\[([^\]]+)\] (.*)$", line):
            texts[m.group(1)] = m.group(2)
    return texts


def _text_eval(cond: Condition, texts: dict[str, str]) -> bool | None:
    """Evaluate a condition against logged text only. None when it can't be decided from text."""
    if isinstance(cond, TextPresent):
        if cond.frame_path is None:
            return any(cond.text in t for t in texts.values())
        return cond.text in texts.get("/".join(cond.frame_path) or "top", "")
    if isinstance(cond, AllOf):
        results = [_text_eval(c, texts) for c in cond.conditions]
        return False if False in results else (True if True in results else None)
    if isinstance(cond, AnyOf):
        results = [_text_eval(c, texts) for c in cond.conditions]
        return True if True in results else (None if None in results else False)
    return None


def _routes(obs: ObservationSummary, examples: dict[str, str]) -> dict[str, str]:
    return {name: normalize_route(url, examples) for name, url in obs.frames.items()}


def _frame_path(name: str) -> list[str]:
    return [] if name == "top" else name.split("/")


def _url_condition(routes: dict[str, str]) -> Condition | None:
    conds = [UrlMatches(route=route, frame_path=_frame_path(name)) for name, route in routes.items()]
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else AllOf(conditions=conds)


# ---------------------------------------------------------------- recording

def load_run(run_dir: Path) -> tuple[RunManifest, list[StepRecord]]:
    manifest, records, _ = load_run_with_events(run_dir)
    return manifest, records


def load_run_with_events(run_dir: Path) -> tuple[RunManifest, list[StepRecord], list[dict]]:
    """steps.jsonl holds agent steps plus handoff events (lines with an "event" key)."""
    manifest = RunManifest.model_validate_json((run_dir / "run.json").read_text())
    records, events = [], []
    for line in (run_dir / "steps.jsonl").read_text().splitlines():
        data = json.loads(line)
        if "event" in data:
            events.append(data)
        else:
            records.append(StepRecord.model_validate(data))
    result = json.loads((run_dir / "result.json").read_text())
    if result.get("outcome") != "done":
        raise RecordingError(f"run {run_dir.name} did not succeed (outcome: {result.get('outcome')})")
    return manifest, records, events


def record_run(run_dir: Path, *, capability_id: str, name: str, catalog: AppCatalog, version: str = "1.0.0",
               description: str | None = None) -> CapabilityArtifact:
    manifest, records, events = load_run_with_events(run_dir)
    human = [e for e in events if e.get("event") == "human_action" and e.get("kind") != "navigate"]
    examples = {p.example: p.name for p in manifest.params if p.example and not p.sensitive and len(p.example) > 1}
    recoverable = [s for s in catalog.signatures if s.classification == "recoverable"]

    def next_observation(i: int) -> ObservationSummary | None:
        return next((r.observation for r in records[i + 1:] if r.observation is not None), None)

    steps: list[Step] = []
    used_params: dict[str, ElementInfo | None] = {}
    outputs: list[OutputSpec] = []
    dropped = 0
    for i, rec in enumerate(records):
        if rec.tool not in _ACTIONS:
            continue
        if rec.result is None or not rec.result.ok or rec.action is None:
            dropped += 1
            continue
        if rec.observation is not None and any(
                _text_eval(s.match, _frame_texts(rec.observation)) for s in recoverable):
            dropped += 1
            continue

        step_id = f"s{len(steps) + 1:02d}"
        kind = rec.action["kind"]
        el = rec.element
        after = next_observation(i)
        before_routes = _routes(rec.observation, examples) if rec.observation else {}
        after_routes = _routes(after, examples) if after else {}
        changed = {n: r for n, r in after_routes.items() if before_routes.get(n) != r}

        value_template, output, intent = None, None, ""
        if kind == "navigate":
            value_template = url_template(rec.action["url"], examples)
            intent = f"Open {value_template}"
        elif kind == "fill":
            value_template = to_template(rec.tool_input.get("value", rec.action["value"]), examples)
            intent = f"Enter {value_template} into {_describe(el)}"
        elif kind == "select":
            value_template = to_template(rec.tool_input.get("option", rec.action["option"]), examples)
            intent = f"Choose {value_template} in {_describe(el)}"
        elif kind == "press":
            value_template = rec.action["key"]
            intent = f"Press {value_template}"
        elif kind == "click":
            intent = f"Click {_describe(el)}"
        elif kind == "extract":
            output = rec.tool_input["name"]
            intent = f"Read {_describe(el)} as {output}"
            example = rec.result.extracted or ""
            outputs.append(OutputSpec(
                name=output, source_step=step_id,
                type=_output_type(example),
                redact_in_logs=bool(_SENSITIVE_OUTPUT.search(f"{el.label} {el.column} {output}")),
            ))
        for field in re.findall(r"\{([a-z][a-z0-9_]*)\}", value_template or ""):
            used_params.setdefault(field, el)

        frame_key = ("/".join(el.frame_path) or "top") if el else None
        steps.append(Step(
            id=step_id, intent=intent, action=kind, value_template=value_template, output=output,
            target=StepTarget(candidates=build_candidates(el), description=_describe(el)) if el else None,
            risk="irreversible" if rec.policy is not None and rec.policy.risk.value == "commit" else "safe",
            precondition=(UrlMatches(route=before_routes[frame_key], frame_path=el.frame_path)
                          if el and frame_key in before_routes else None),
            checkpoint=_url_condition(changed),
            performed_by="human" if rec.performed_by == "human" else "agent",
        ))

    if len(steps) < 2:
        raise RecordingError("run has no successful actions beyond opening the app")

    final_obs = next((r.observation for r in reversed(records) if r.observation is not None), None)
    success_parts: list[Condition] = []
    if final_obs is not None and (url_cond := _url_condition(_routes(final_obs, examples))) is not None:
        success_parts.append(url_cond)
    for out in outputs:
        source = next(s for s in steps if s.id == out.source_step)
        success_parts.append(ElementVisible(target=source.target))
    success = success_parts[0] if len(success_parts) == 1 else AllOf(conditions=success_parts)

    inputs = []
    for p in manifest.params:
        if p.name not in used_params:
            continue
        el = used_params[p.name]
        where = f" Typed into {_describe(el)}." if el else ""
        if p.sensitive:
            inputs.append(ParamSpec(name=p.name, sensitive=True,
                                    description=f"Secret, supplied at run time and never stored.{where}"))
        else:
            pattern = rf"\d{{{len(p.example)}}}" if p.example and p.example.isdigit() else None
            inputs.append(ParamSpec(name=p.name, pattern=pattern,
                                    description=f"{where.strip()} Pattern inferred from one example; review."))

    # Sign-in = everything up to the first verified screen change after the last secret is typed.
    secrets = {p.name for p in manifest.params if p.sensitive}
    last_secret = max((i for i, s in enumerate(steps)
                       if set(re.findall(r"\{([a-z][a-z0-9_]*)\}", s.value_template or "")) & secrets), default=None)
    sign_in: list[str] = []
    if last_secret is not None:
        end = next((i for i in range(last_secret + 1, len(steps)) if steps[i].checkpoint is not None), None)
        if end is not None:
            sign_in = [s.id for s in steps[:end + 1]]

    models = sorted({r.llm["model"] for r in records if r.llm and r.llm.get("model")})
    human_steps = [s.id for s in steps if s.performed_by == "human"]
    handoff_note = ""
    if human:
        done = "; ".join(f"{a['kind']} {a.get('name') or a.get('label') or a.get('field_name')!r}" for a in human[:6])
        handoff_note = (f"A human took over during discovery and performed {len(human)} action(s) that are "
                        f"not steps of this artifact ({done}); review whether the flow still holds without them. ")
    kept = len(steps)
    artifact = CapabilityArtifact(
        capability=Capability(id=capability_id, name=name, version=version, status="draft",
                              description=description or _describe_for_agent(manifest.goal, inputs, outputs,
                                                                             steps, catalog.signatures)),
        target_app=TargetApp(vendor_product=catalog.app.vendor_product, app_version=catalog.app.app_version,
                             entry_route=normalize_route(manifest.entry_url, {})),
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        error_signatures=catalog.signatures,
        success_condition=success,
        sign_in_steps=sign_in,
        provenance=Provenance(
            discovery_run_id=manifest.run_id, models=models, recorded_at=datetime.now(timezone.utc),
            human_steps=human_steps,
            note=(f"Recorded offline from run {manifest.run_id}: kept {kept} actions, dropped {dropped} "
                  f"(failed, denied or incidental recovery). " + (handoff_note or "No human intervention. ")
                  + "Locator candidates are generated from the run's observations and verified at replay."),
        ),
    )
    return artifact


def _describe_for_agent(goal: str, inputs: list[ParamSpec], outputs: list[OutputSpec], steps: list[Step],
                        signatures: list[ErrorSignature]) -> str:
    text = goal[:1].upper() + goal[1:].rstrip(".") + "."
    if inputs:
        text += " Inputs: " + ", ".join(f"{p.name}{' (secret)' if p.sensitive else ''}" for p in inputs) + "."
    if outputs:
        text += " Returns: " + ", ".join(f"{o.name} ({o.type})" for o in outputs) + "."
    commits = any(s.risk == "irreversible" for s in steps)
    text += " Changes data in the system of record." if commits else " Read-only."
    codes = [s.outcome_code for s in signatures if s.classification == "business_outcome"]
    if codes:
        text += " May instead return a business outcome: " + ", ".join(codes) + "."
    return text
