# Decisions

Each entry: the decision, why, and what it costs. Newest phases last.

## D1. Every step is a fresh LLM request built from our own history
**Decision.** Discovery sends one self-contained request per step (goal, params, a compact history we
maintain, current elements, screenshot) instead of growing a chat transcript.
**Why.** Cost stays flat with run length. The model sees exactly what the audit log records. It
also sidesteps model-specific rules about replaying or editing earlier turns.
**Cost.** The model cannot refer back to earlier screenshots; the history must carry what matters.

## D2. Parameters are typed by reference, never by value
**Decision.** The model sees `password = <secret>` and types `{{password}}`; values are resolved only
when the action reaches the browser. The log keeps the reference.
**Why.** Secrets never reach the model, the logs or the artifact, and the recorder knows exactly
which step uses which parameter without guessing from values.
**Cost.** The model must follow the reference convention; a literal it types instead is recorded as
a constant (or templated if it equals a known non-secret example).

## D3. `tool_choice: auto` + strict schemas + server-side refusal fallback
**Decision.** No forced tool choice. Instead: one call per turn, strict tool schemas, one nudge if no
tool is called, and `fallbacks: "default"`.
**Why.** Some current models reject forced tool use. In a real run, Opus 5's cyber classifier
declined every step (the agent types a password into an "Authorized use only" page), and the
fallback answered each one. Provenance records every model that actually decided a step.
**Cost.** A run may be served by more than one model; that is visible, not hidden.

## D4. A target needs exactly one visible match; first candidate that has one wins
**Decision.** Zero matches means not found and more than one means ambiguous; resolution never picks
`.first`. Falling back to a lower-ranked candidate is logged as drift.
**Why.** Guessing between two "Search" buttons is how automation clicks the wrong thing silently.
**Cost.** Some ambiguous-but-harmless screens fail loudly and need a better locator.

## D5. Coordinates are last, and never used to type, read or commit
**Decision.** Recorded as the last candidate; replay only uses them for plain clicks on safe steps.
**Why.** A coordinate "matches" whatever is at that point; found when drifted locators made replay
fall through to coordinates for a fill.
**Cost.** A layout change can't be papered over for inputs or commits; it becomes a handoff.

## D6. Data cells are addressable by row caption + column header
**Decision.** The observation lists value cells; `text_near` can take a `column`.
**Why.** "Read the savings balance" targets a table cell whose neighbour is an empty nickname, so
"the cell after the caption" is wrong. Row + header is how a person reads the table.
**Cost.** Relies on a header row with a matching cell count (colspans are skipped, not guessed).

## D7. Stuck means the same action on the same screen three times
**Decision.** Keyed on (state hash, action), not on the state hash alone; plus three consecutive
failures.
**Why.** The state hash deliberately ignores typed values, so filling a three-field form produces
the same hash three times legitimately.
**Cost.** Paging through identical-looking screens with the same click would trip it.

## D8. Error signatures are per-app data, ordered, copied into each artifact
**Decision.** A YAML catalog per app; the recorder copies it; replay checks it in order (specific
before generic).
**Why.** A new legacy app means a new file, not new code. An approved artifact's behaviour cannot
change because the catalog changed later.
**Cost.** Catalog updates need re-recording or re-approval to reach existing artifacts.

## D9. Only two recoveries; server errors are hard failures
**Decision.** `dismiss` and `reauthenticate`; slow loads are bounded checkpoint backoff; no "retry the
step".
**Why.** After a 500 the previous screen is gone and the request may have committed. A generic
retry would mean improvised navigation or a duplicate commit.
**Cost.** Some transient errors that a person would simply retry go to a human instead.

## D10. Nothing fails before the screen is classified
**Decision.** Every failed precondition, failed action or missed checkpoint, and even a met
checkpoint, is first checked against the artifact's error signatures.
**Why.** It separates the app's *answer* (`MEMBER_NOT_FOUND`) from *breakage*, and catches error pages
served at the expected URL.
**Cost.** One extra observation per step.

## D11. Checkpoint routes are normalized and bound to this run's parameters
**Decision.** `?m=100234` is recorded as `?m=:member_id`; at replay `:member_id` must equal the actual
param. Other ids (draft tokens) become the wildcard `:id`.
**Why.** Without normalization a checkpoint only matches the discovery member. Without binding, a
human who "fixes" a run on the wrong member would pass verification.
**Cost.** Parameter values must appear verbatim in URLs to be bound (true here).

## D12. The policy gate is the only caller of `Surface.act()`, and LLM isolation is tested
**Decision.** An AST test fails on any other `.act(` call. Another test imports every non-agent
module in a fresh interpreter and fails if `anthropic` gets loaded.
**Why.** "Single chokepoint" and "no LLM in replay" are properties worth enforcing, not just
following.
**Cost.** None beyond keeping the tests green.

## D13. The allowlist checks the frame the action happens in, by origin and route
**Why.** In a frameset the address bar always says `/desk`; what matters is `main`. Route-level
checks block the same host's `/__reset`.
**Cost.** Routes must be maintained per app in `policy.yaml`.

## D14. Irreversible actions need a *human* decision unless the artifact is approved
**Decision.** Approvals carry `by_human`. Discovery and draft replays require it, so an auto-approver
is refused. An approved artifact's review stands in for per-run approval.
**Why.** "Never auto-execute in discovery or draft replay" is then true by construction, not by
wiring.
**Cost.** Approving an artifact is a real act of trust; its review has to be meaningful.

## D15. Balances are redacted in logs, not in results
**Decision.** Callers get `$5,230.17` in `Success.outputs`; logs, results on disk and stdout show
`[REDACTED:money]`.
**Why.** The policy says balances do not belong in logs, and the value is needed only by the caller.
**Cost.** The CLI cannot show the value it read; the recorder infers output types from the redacted
marker.

## D16. The store refuses artifacts that redaction would change
**Why.** Silently redacting a literal like `000123456789` in a fill step would make replay type
`[REDACTED:account_number]` into the bank's form. Refusing names the field and asks for an input.
**Cost.** Occasional false positives need the literal turned into a parameter.

## D17. The recorder works offline and keeps only what worked
**Decision.** Failed, denied and rejected steps are dropped, as are actions taken while a
*recoverable* signature was on screen (e.g. dismissing a notice).
**Why.** Replay handles those through signatures; recording them would make every replay dismiss
a notice that is not there.
**Cost.** Agent detours that "succeeded" but were unnecessary survive; draft review catches them.

## D18. Handoff ownership is enforced at the gate; resume is verified
**Decision.** The gate refuses automation unless control is `AGENT`. Human events are recorded from the
moment automation pauses until it resumes (by event timestamp), each tagged with who held control
at that instant. The first real demo showed why: the operator searched in the browser *before*
typing `take`, and with a take-to-resume window those actions were missing from the audit log. On resume, replay continues after the latest checkpoint
that holds, never re-running a completed irreversible step.
**Why.** Automation and a person must never both act, and "the human said resume" is not evidence
the screen is right.
**Cost.** A human who leaves the app somewhere no checkpoint recognizes gets asked again.

## D19. The operator console reads stdin on a background thread
**Why.** Playwright's sync API delivers browser events only while Python is inside a Playwright call.
Blocking on `input()` would freeze capture until the human typed a command.
**Cost.** A small event-pump loop (250 ms ticks) while waiting for a human.
