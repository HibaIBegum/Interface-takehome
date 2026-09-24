"""Command-line entry point: `python -m cua.cli <command>`."""

from __future__ import annotations

import argparse
import sys


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
