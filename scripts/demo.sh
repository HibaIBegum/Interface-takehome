#!/usr/bin/env bash
# End-to-end demo against the mock app. Every scenario runs through the real CLI; the discovery run
# is a real LLM run. Redacted logs, the artifact and failure evidence are collected into evidence/.
#
#   export ANTHROPIC_API_KEY=...  ANTHROPIC_MODEL=claude-opus-5
#   make demo                      # or: scripts/demo.sh
#
# Scenario 7 (human handoff) needs you at the keyboard; set DEMO_HANDOFF=skip to leave it out.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-.venv/bin/python}
PORT=${DEMO_PORT:-5055}
BASE="http://127.0.0.1:$PORT"
CAP=lookup-savings-balance
export MOCK_USERNAME=${MOCK_USERNAME:-teller01}
export MOCK_PASSWORD=${MOCK_PASSWORD:-demo-pass-2931}
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY: the discovery run is a real LLM run}"
: "${ANTHROPIC_MODEL:?set ANTHROPIC_MODEL, e.g. claude-opus-5}"

RUNS="runs/demo-$(date -u +%Y%m%dT%H%M%S)"
mkdir -p "$RUNS"
SECRETS=(--secret-param username=MOCK_USERNAME --secret-param password=MOCK_PASSWORD)

if curl -s -o /dev/null "$BASE/login"; then
  echo "Something is already listening on $BASE. Stop it (it may be a serve-mock) or set DEMO_PORT." >&2
  exit 1
fi
"$PY" -m cua.cli serve-mock --port "$PORT" > "$RUNS/mock-app.log" 2>&1 &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -s -o /dev/null "$BASE/login" && break; sleep 0.2; done

banner() { printf '\n==================== %s ====================\n' "$1"; }
reset_app() { curl -s -X POST "$BASE/__reset" > /dev/null; }
arm() { curl -s -X POST "$BASE/__faults" -H 'Content-Type: application/json' -d "{\"fault\":\"$1\"}" > /dev/null; }
replay() {  # replay <scenario> [extra args]; a non-success result is expected in some scenarios
  local scenario=$1; shift
  "$PY" -m cua.cli replay "$CAP" --base-url "$BASE" --run-dir "$RUNS/$scenario" "${SECRETS[@]}" "$@" || true
}

banner "1. discovery (real LLM: $ANTHROPIC_MODEL)"
"$PY" -m cua.cli discover --headless \
  --goal "look up member {member_id} and read their savings balance" \
  --url "$BASE/login" --param member_id=100234 "${SECRETS[@]}" \
  --run-dir "$RUNS/01-discovery" \
  || { echo "Discovery did not complete; see $RUNS/01-discovery. Stopping." >&2; exit 1; }

banner "2. record the artifact, then approve it"
"$PY" -m cua.cli record "$RUNS/01-discovery" --id "$CAP" --name "Look up savings balance" --overwrite
"$PY" -m cua.cli approve "$CAP"

banner "3. replay: success"
reset_app
replay 03-replay-success --headless --param member_id=100234

banner "4. replay: MEMBER_NOT_FOUND"
reset_app
replay 04-member-not-found --headless --param member_id=999999

banner "5. replay: session expires mid-run and is recovered"
reset_app; arm session_timeout
replay 05-session-timeout-recovered --headless --param member_id=100234

banner "6. replay: server error is a hard failure (unattended, so no human takes over)"
reset_app; arm server_error
replay 06-server-error-hard-failure --headless --param member_id=100234

banner "7. replay: server error, fixed by a human in the same browser"
if [[ "${DEMO_HANDOFF:-}" != "skip" && -t 0 ]]; then
  cat <<'EOF'
A browser window will open and hit "Internal Server Error". When the terminal shows HUMAN NEEDED:
  1. type  take   and press Enter
  2. in the browser: click "Member Search" (top right), enter 100234, click Search
  3. type  resume  and press Enter
EOF
  read -r -p "Press Enter to start..."
  reset_app; arm server_error
  replay 07-human-handoff --param member_id=100234
else
  echo "skipped (no interactive terminal, or DEMO_HANDOFF=skip)"
fi

banner "collecting evidence"
"$PY" scripts/collect_evidence.py "$RUNS" "artifacts/$CAP" --secret-env MOCK_PASSWORD --secret-env MOCK_USERNAME
echo "Done. Index: evidence/README.md   Raw runs: $RUNS"
