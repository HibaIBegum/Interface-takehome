"""Command-line entry point: `python -m cua.cli <command>`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _parse_pairs(pairs: list[str], flag: str) -> list[tuple[str, str]]:
    parsed = []
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"error: {flag} expects name=value, got {pair!r}")
        parsed.append((name.strip(), value))
    return parsed


def _discover(args: argparse.Namespace) -> int:
    from cua.agent.llm import ClaudeDecider, LLMError
    from cua.agent.loop import DiscoveryAgent, Limits, Outcome, Param
    from cua.observability.runlog import RunLog, new_run_dir
    from cua.policy.gate import PolicyConfig, PolicyGate
    from cua.surface.playwright_surface import PlaywrightSurface

    params = [Param(name=n, value=v) for n, v in _parse_pairs(args.param, "--param")]
    for name, env_var in _parse_pairs(args.secret_param, "--secret-param"):
        if env_var not in os.environ:
            print(f"error: environment variable {env_var} (for secret {name}) is not set", file=sys.stderr)
            return 2
        params.append(Param(name=name, value=os.environ[env_var], sensitive=True))

    policy = PolicyConfig.load(args.policy)
    redactor = policy.redactor({p.name: p.value for p in params if p.sensitive})
    log = RunLog(new_run_dir(args.runs_dir, args.goal), redactor)
    try:
        decider = ClaudeDecider()
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    entry = urlsplit(args.url)
    base_url = f"{entry.scheme}://{entry.netloc}"
    approver = (lambda action, risk: True) if args.allow_commit else None
    log.echo(f"run dir: {log.dir}")
    with PlaywrightSurface.launch(base_url, headless=not args.headed, mask=policy.mask_targets()) as surface:
        agent = DiscoveryAgent(
            surface=surface, gate=PolicyGate(policy, approver=approver, base_url=base_url), decider=decider,
            log=log, screenshots=not args.no_screenshots,
        )
        result = agent.run(goal=args.goal, entry_url=args.url, params=params,
                           limits=Limits(max_steps=args.max_steps, timeout_s=args.timeout))
    print(redactor.text(result.model_dump_json(indent=2)))
    return 0 if result.outcome is Outcome.DONE else 1


def _record(args: argparse.Namespace) -> int:
    from cua.artifact.recorder import AppCatalog, RecordingError, record_run
    from cua.artifact.store import ArtifactStore, StoreError
    from cua.policy.gate import PolicyConfig

    try:
        artifact = record_run(args.run_dir, capability_id=args.id, name=args.name, version=args.version,
                              catalog=AppCatalog.load(args.app))
        path = ArtifactStore(args.artifacts_dir).save(artifact, PolicyConfig.load(args.policy).redactor(),
                                                       overwrite=args.overwrite)
    except (RecordingError, StoreError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {path} and {path.with_suffix('.md')} ({len(artifact.steps)} steps, status {artifact.capability.status})")
    return 0


def _serve_mock(args: argparse.Namespace) -> int:
    from mock_app import create_app

    try:
        app = create_app()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Bound to loopback only: the mock and its /__faults endpoint are never exposed.
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cua")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve-mock", help="Run the mock legacy credit-union app on localhost")
    serve.add_argument("--port", type=int, default=5055)
    serve.set_defaults(func=_serve_mock)

    disc = sub.add_parser("discover", help="Let the LLM agent accomplish a goal once, logging every step")
    disc.add_argument("--goal", required=True, help='e.g. "look up member {member_id} and read their savings balance"')
    disc.add_argument("--url", required=True, help="entry URL, e.g. http://127.0.0.1:5055/login")
    disc.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="visible parameter")
    disc.add_argument("--secret-param", action="append", default=[], metavar="NAME=ENV_VAR",
                      help="secret parameter read from an environment variable; the LLM never sees its value")
    disc.add_argument("--max-steps", type=int, default=25)
    disc.add_argument("--timeout", type=float, default=300, help="wall-clock limit in seconds")
    disc.add_argument("--allow-commit", action="store_true", help="approve submit/confirm-type actions")
    disc.add_argument("--headed", action="store_true", help="show the browser")
    disc.add_argument("--no-screenshots", action="store_true", help="do not send screenshots to the LLM")
    disc.add_argument("--policy", type=Path, default=Path("config/policy.yaml"))
    disc.add_argument("--runs-dir", type=Path, default=Path("runs"))
    disc.set_defaults(func=_discover)

    rec = sub.add_parser("record", help="Turn a successful discovery run into a draft capability artifact")
    rec.add_argument("run_dir", type=Path)
    rec.add_argument("--id", required=True, help="capability id, e.g. lookup-savings-balance")
    rec.add_argument("--name", required=True, help='e.g. "Look up savings balance"')
    rec.add_argument("--version", default="1.0.0")
    rec.add_argument("--app", type=Path, default=Path("config/apps/ffcu_member_services.yaml"))
    rec.add_argument("--policy", type=Path, default=Path("config/policy.yaml"))
    rec.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"))
    rec.add_argument("--overwrite", action="store_true")
    rec.set_defaults(func=_record)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
