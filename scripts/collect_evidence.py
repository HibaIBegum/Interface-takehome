"""Copy a demo's run folders into evidence/<scenario>/ and write evidence/README.md.

Everything copied was already redacted when it was written (RunLog, ArtifactStore). As a last
check, this refuses to finish if any secret passed via --secret-env appears in a copied text file.

    python scripts/collect_evidence.py runs/demo-<stamp> artifacts/lookup-savings-balance \
        --secret-env MOCK_PASSWORD --secret-env MOCK_USERNAME
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

SCENARIOS = {
    "01-discovery": "Real LLM discovery run: the agent signs in, looks up the member and reads the savings balance.",
    "02-artifact": "The capability artifact recorded from that run (draft, then approved for replay).",
    "03-replay-success": "Deterministic replay, no LLM: returns savings_balance.",
    "04-member-not-found": "Replay with member 999999: the app's answer is classified as a business outcome.",
    "05-session-timeout-recovered": "Session expires mid-run: replay signs in again and resumes from the last "
                                    "verified checkpoint.",
    "06-server-error-hard-failure": "Server error page: a hard failure (not retried), with expected vs observed, "
                                    "masked screenshot and redacted DOM/accessibility snapshots.",
    "07-human-handoff": "Same server error, but a human takes over the live browser, fixes it, and resumes; "
                        "the human's actions are captured in the log.",
}
RUN_FILES = ("run.json", "replay.json", "result.json", "steps.jsonl")
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".html"}


def copy_run(run_dir: Path, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in RUN_FILES:
        if (run_dir / name).exists():
            shutil.copy2(run_dir / name, dest / name)
            copied.append(name)
    for path in sorted(run_dir.glob("intervention*")):
        shutil.copy2(path, dest / path.name)
        copied.append(path.name)
    if (run_dir / "evidence").is_dir():
        for path in sorted((run_dir / "evidence").iterdir()):
            shutil.copy2(path, dest / path.name)
            copied.append(path.name)
    return copied


def outcome_of(dest: Path) -> str:
    result = dest / "result.json"
    if not result.exists():
        return "-"
    data = json.loads(result.read_text())
    if "outcome" in data:  # discovery
        return f"discovery `{data['outcome']}`, {data['steps']} steps"
    status = data["status"]
    recoveries = ", ".join(r["kind"] for r in data.get("recoveries", []))
    if status == "success":
        text = f"`success` outputs: {', '.join(data['outputs'])}"
    elif status == "business_outcome":
        text = f"`business_outcome` **{data['code']}** at {data['step_id']}"
    else:
        text = f"`failure` kind `{data['kind']}`" + (f" **{data['code']}**" if data.get("code") else "") \
               + f" at {data['step_id']}"
    return text + (f"; recoveries: {recoveries}" if recoveries else "")


def check_no_secrets(root: Path, secrets: list[str]) -> None:
    leaks = [str(path) for path in root.rglob("*") if path.suffix in TEXT_SUFFIXES
             for secret in secrets if secret and secret in path.read_text(errors="ignore")]
    if leaks:
        sys.exit(f"refusing to finish: secret value found in {sorted(set(leaks))}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path, help="the demo's runs folder (one subfolder per scenario)")
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("--out", type=Path, default=Path("evidence"))
    parser.add_argument("--secret-env", action="append", default=[])
    args = parser.parse_args()

    for scenario in SCENARIOS:
        shutil.rmtree(args.out / scenario, ignore_errors=True)
    rows = []
    for scenario, what in SCENARIOS.items():
        dest = args.out / scenario
        if scenario == "02-artifact":
            dest.mkdir(parents=True, exist_ok=True)
            files = []
            for path in sorted(args.artifact_dir.glob("v*.*")):
                shutil.copy2(path, dest / path.name)
                files.append(path.name)
            outcome = "recorded, then approved" if files else "missing"
        elif (args.runs / scenario).is_dir():
            files = copy_run(args.runs / scenario, dest)
            outcome = outcome_of(dest)
        else:
            rows.append(f"| `{scenario}` | {what} | not run (skipped) | - |")
            continue
        listed = ", ".join(f"[{f}]({scenario}/{f})" for f in files) or "-"
        rows.append(f"| `{scenario}` | {what} | {outcome} | {listed} |")

    readme = "\n".join([
        "# Evidence", "",
        f"Produced by `make demo` (scripts/demo.sh) from `{args.runs}`. Every file here was redacted when it was "
        "written: secrets are never logged (only `{{name}}` references), and SSNs, balances, account numbers and "
        "similar values appear as `[REDACTED:<kind>]`. Output values such as the balance are returned to the "
        "calling program only, never logged. Screenshots are masked (password fields, SSN).", "",
        "| Scenario | What it shows | Outcome | Files |", "|---|---|---|---|", *rows, "",
        "In each run folder: `steps.jsonl` is the step-by-step log (policy decision, locator strategy used, "
        "recoveries, control transitions, captured human actions), `result.json` is the final result, "
        "`intervention.json` is what the human was asked, and `*-screen.png` / `*-aria.txt` / `*-dom.html` are "
        "failure evidence.", "",
    ])
    (args.out / "README.md").write_text(readme, encoding="utf-8")
    check_no_secrets(args.out, [os.environ.get(name, "") for name in args.secret_env])
    print(f"evidence written to {args.out}/ ({len(rows)} scenarios)")


if __name__ == "__main__":
    main()
