# Discover once, replay deterministically

Computer-use automation for legacy back-office apps that have no API. An LLM agent completes a goal
once by driving the real UI; the successful run is recorded as a typed, versioned, parameterized
**capability artifact**; production **replays** the artifact with no LLM in the loop. When replay
cannot proceed, a human takes over the same live browser and hands control back.

The target here is a deliberately legacy-looking mock credit-union back office (`mock_app/`):
framesets, table layouts, no ids, and injectable faults.

See [REPORT.md](REPORT.md) for the design and [DECISIONS.md](DECISIONS.md) for the reasoning behind it.
Demo output is in [evidence/](evidence/README.md).

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

Configuration is read from environment variables (see [.env.example](.env.example)). Nothing loads
`.env` automatically; export what you need:

```bash
export MOCK_USERNAME=teller01 MOCK_PASSWORD=change-me          # mock app sign-in
export ANTHROPIC_API_KEY=sk-ant-... ANTHROPIC_MODEL=claude-opus-5  # only for `discover` / `make demo`
```

> macOS note: if `import cua` fails inside the venv, the editable-install `.pth` file may have been
> given the hidden flag (seen with iCloud-synced folders): `chflags nohidden .venv/lib/python3.*/site-packages/*.pth`.

## Tests (no API key needed)

```bash
make test        # or: pytest -q      (~80 s; drives headless Chromium against the mock app)
```

Replay never uses an LLM, and discovery is tested with a scripted stand-in for the model, so the
whole suite runs offline. The one live-LLM test (`tests/test_discovery_live.py`) is skipped unless
`ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are set.

## The mock app

```bash
python -m cua.cli serve-mock            # http://127.0.0.1:5055, sign in with MOCK_USERNAME / MOCK_PASSWORD
```

Members: `100234`-`100238` (`100237` is restricted; any other id is "not found").
Flow: sign in, Member Search, Member Detail, Open Sub-Account, Review, Submit, confirmation.

Faults are armed through a local-only endpoint and fire on the next page in the main frame:

```bash
curl -X POST localhost:5055/__faults -H 'Content-Type: application/json' -d '{"fault":"server_error","count":1}'
#   fault: slow_load (with delay_ms) | session_timeout | interstitial | server_error
curl localhost:5055/__faults              # what is armed
curl -X POST localhost:5055/__reset       # restore seed data and clear faults
```

## Commands

```bash
# 1. Discover (real LLM). Secrets are passed by env-var name; the model never sees their values.
python -m cua.cli discover --goal "look up member {member_id} and read their savings balance" \
  --url http://127.0.0.1:5055/login --param member_id=100234 \
  --secret-param username=MOCK_USERNAME --secret-param password=MOCK_PASSWORD

# 2. Record the run as a draft artifact, review artifacts/<id>/v1.md, then approve it.
python -m cua.cli record runs/<run folder> --id lookup-savings-balance --name "Look up savings balance"
python -m cua.cli approve lookup-savings-balance

# 3. Replay (no LLM). Any member id works; the browser is headed so a human can take over.
python -m cua.cli replay lookup-savings-balance --base-url http://127.0.0.1:5055 \
  --param member_id=100236 --secret-param username=MOCK_USERNAME --secret-param password=MOCK_PASSWORD
```

Useful flags: `--headless` (no browser window, so no human handoff), `--allow-draft` (replay an
unapproved artifact; irreversible steps then need a human's approval). Every run writes a folder
under `runs/` with a redacted `steps.jsonl`, `result.json` and, on failure, masked evidence.

When a human is needed, the terminal prints the request (also saved as `intervention.json`) and
accepts `take`, `resume`, `approve`, `deny` or `abort`. After `take`, use the browser window; your
clicks and edits are captured in the run log. Type `resume` to hand back.

## Demo

```bash
export ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=claude-opus-5
make demo                   # DEMO_HANDOFF=skip make demo  to leave out the manual step
```

Starts its own mock app and runs: a real discovery run, record and approve, a successful replay,
`MEMBER_NOT_FOUND`, a session timeout that recovers, a server error as a hard failure, and a human
handoff you perform in the browser. Redacted logs, the artifact and failure evidence are collected
into `evidence/<scenario>/` with an index at [evidence/README.md](evidence/README.md).

## Layout

```
mock_app/        Flask "legacy" credit-union app (the target) with fault injection
cua/surface/     Surface protocol + Playwright implementation (observe / act / extract / screenshot)
cua/agent/       discovery loop, prompts, Claude client: the only code that uses an LLM
cua/artifact/    artifact schema, recorder (run -> artifact), store
cua/replay/      deterministic replay engine, conditions, recovery
cua/policy/      allowlist, risk, redaction, and the policy gate (the only caller of Surface.act)
cua/handoff/     control state, intervention requests, human action capture, operator console
cua/observability/ run logs and record models
config/          policy.yaml, per-app error signature catalog
scripts/         demo.sh, collect_evidence.py
```
