# Report

## 1. Architecture

Three phases share one browser abstraction and one safety chokepoint:

```
discover (LLM)  ──► run log ──► record ──► artifact (draft) ──► review/approve ──► replay (no LLM)
      │                                                                              │
      └──────────── Surface (observe / act / extract / screenshot) ◄─────────────────┘
                              ▲ every action passes PolicyGate first
                              └ HandoffSession: a human can take the same live session
```

- **Surface** (`cua/surface`) is the only thing that touches a UI. An observation is a compact list of
  interactive elements and addressable data cells (role, accessible name, caption label, form field
  name, frame path, box), visible text, a masked screenshot and a *state hash* that identifies the
  screen, not its data.
- **Discovery** (`cua/agent`) is the only package that imports the LLM SDK; a test imports every other
  module in a fresh interpreter and fails if the SDK is loaded. Each step is one fresh request
  (goal, params, our own compact history, elements, screenshot) and the model must call exactly one
  tool: click / fill / select / navigate / extract / done / request_human, referring to elements by
  index.
- **Recorder** turns a successful run log into an artifact offline. **Replay** executes it with no model.
- **PolicyGate** is the only caller of `Surface.act()` (enforced by an AST test), for the agent,
  replay and recovery alike.

## 2. Artifact schema

`CapabilityArtifact` (`schema_version: "1.0"`, Pydantic, JSON) is the contract between discovery and
replay:

- **capability**: id, name, description written for a calling agent (inputs, outputs, possible
  business outcomes, read-only or not), semver, status `draft | approved`.
- **inputs**: typed `ParamSpec`s with a validation pattern and a `sensitive` flag. Sensitive inputs are
  referenced only by name, and only inside a fill step; the schema rejects them anywhere else.
- **steps**: intent, action, `value_template` (`{member_id}`), risk `safe | irreversible`, precondition,
  checkpoint, and a **target**: 2-4 ranked locator candidates, each with the recorder's note on why it
  is robust. Order: role + accessible name, caption label, HTML field name, caption-cell position,
  and coordinates last.
- **conditions** are composable (`url_matches`, `element_visible`, `text_present`, `all`, `any`). Routes
  are normalized: parameter values become `:member_id`, other ids `:id`.
- **error_signatures**: a match condition, a classification (`business_outcome | recoverable |
  hard_failure`), an `outcome_code` and, for recoverable ones only, a bounded recovery. They come from
  a per-app catalog and are copied in, so an artifact is self-contained.
- **success_condition**, **sign_in_steps** (for re-authentication) and **provenance** (run id, every
  model that decided a step, including fallbacks, and any human involvement).

## 3. Determinism & error handling

Replay per step: validate params, check the precondition, resolve the target, pass the policy
gate, act, wait for the checkpoint, then classify the screen.

- **Resolution** takes the first candidate with *exactly one* visible match; zero is not-found, more
  than one is ambiguous; it never picks `.first`. Falling back to a lower candidate is logged as
  locator drift. Coordinates are used only for plain clicks on safe steps: never to type, read or
  commit.
- **Waits are explicit conditions**: frame readiness, visible elements, navigation events. Checks are
  capped at 500 ms apart and every wait has a deadline. Slow pages get bounded backoff (1x, 2x, 4x the
  checkpoint timeout). There are no fixed sleeps in the automation path.
- **Nothing is called a failure before the screen is classified** by the artifact's signatures, in
  order, most specific first. A met checkpoint still fails if a signature is showing: the mock's 500
  page is served at the expected URL.
- **Three outcomes.** A *business outcome* (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`,
  `INSUFFICIENT_FUNDS`) is the app's answer, returned to the caller and not retried. *Recoverable*:
  dismiss an interstitial, or re-authenticate and resume after the latest verified checkpoint that
  still holds (never re-running a completed irreversible step). *Hard failure*: a server error,
  expired request, rejected sign-on, or exhausted recovery.
- Server errors are deliberately hard failures: retrying needs the previous screen back, and the error
  may have followed a commit.
- Every `Failure` carries expected vs observed, plus a masked screenshot and redacted
  accessibility and DOM snapshots.

## 4. Heterogeneity & multi-tenant

**The Surface seam.** The agent, the recorder and replay see only the `Surface` protocol and its
models: elements described by role, name, label and a path of containers, and locator candidates
expressed in those terms. Playwright is one implementation. A desktop app (Win32, WinForms, Java
Swing) would be another built on the platform accessibility API (UI Automation on Windows, AX on
macOS, AT-SPI on Linux). Those APIs expose the same concepts: control type (role), Name,
LabeledBy, AutomationId (the analogue of the HTML field name) and a window/pane tree (the analogue
of the frame path). `role_name`, `label` and `css`-like strategies map onto them directly;
screenshot masking and coordinates work the same way. A terminal/green-screen surface would map
screen positions and field labels the same way. The artifact schema and replay engine do not change.

**Multi-tenant reuse** (design, not built). Many institutions run the same vendor product with small
differences. The proposal:

- **One base artifact per vendor product and version**, discovered once.
- **Per-tenant override layers** keyed by tenant: *locator overrides* (a step's candidates),
  *label overrides* (a tenant renamed "Member ID" to "Account Holder No."), *route overrides*
  (different paths or menu depth), and tenant-specific signatures (their error texts).
- Replay merges base + overlay into the effective artifact, and records which layers were applied.
- **Drift** is detected two ways. (1) Checkpoint failures and locator fallbacks (already logged per
  step) tell us *where* the UI changed, which drives a targeted re-discovery of that step, not the
  whole flow. (2) A **version-fingerprint check at replay start**: the artifact records
  `target_app.app_version` (today taken from the app's own version marker, e.g. the `MSD/4.2.1`
  footer). Replay would read the live fingerprint first and refuse, or require approval, on a
  mismatch, so an upgraded app never runs an artifact recorded against the old one.

## 5. Escalation & handoff

Control is explicit: `AGENT → AWAITING_HUMAN → HUMAN → AGENT`, or `ABORTED`. Every transition is logged
with who and why, and the policy gate refuses automation actions unless the state is `AGENT`.

- **Triggers:** discovery stuck (the same action on the same screen 3 times, or 3 failures in a row),
  the model's `request_human`, an unrecoverable replay failure, and any irreversible action that
  needs approval.
- **The request:** a redacted `InterventionRequest` (run, capability or goal, step, reason, URLs,
  masked screenshot) is written to `intervention.json` and printed.
- **The human's work:** the human works in the same headed browser. An injected `capture.js` plus
  `expose_binding` report clicks, field changes (password values never sent) and navigations. Events
  are logged from the moment automation pauses until it resumes, since automation's clicks fire the
  same DOM events. Each one is tagged with the control state at that instant, so a person acting
  before typing `take` is visible in the log rather than lost.
- **Resume is verified, never trusted.** Replay re-observes; if an error signature still shows it
  asks again. Otherwise it continues after the latest step whose checkpoint holds, with routes bound
  to this run's parameters, so fixing the wrong member's page is rejected. It never repeats a
  completed irreversible step, and it fails if an output step was skipped.

## 6. Safety

- **Allowlist** of origins, routes and action types, checked against the frame the action happens
  in (in a frameset the address bar always says `/desk`). Blocked actions return `policy_blocked`
  and are never offered for approval.
- **Irreversible actions** are identified by explicit markers (Submit on the review page), then by
  keywords as a fallback. The artifact can only raise a step's risk. They are held for approval, and
  discovery and draft replays require the decision to come from a human (`by_human`), so an
  auto-approver cannot push a Submit through. Only an *approved* artifact waives this; its review is
  the sign-off.
- **Redaction before any write:** SSNs (including masked), account numbers, money, emails, phones,
  API keys, tokens, `password=` pairs, sensitive keys, and every sensitive param value, applied to
  logs, stdout, artifacts, evidence, intervention requests and captured human actions. Patterns run
  before secret values so a short secret cannot split an SSN.
- **Secrets are safe by construction:** the model sees `password = <secret>` and types
  `{{password}}`; the value is resolved only when the action reaches the browser. The store refuses,
  rather than silently rewrites, an artifact that redaction would change. Screenshots mask password
  fields and configured regions.

## 7. Cuts

- **The operator console is a mock:** a terminal prompt in the same process, not a web console,
  queue or notification. The protocol (`Operator.show / next_command`) is the seam where a real one
  plugs in.
- **No desktop surface.** Section 4 is a design; only the Playwright surface exists.
- **No multi-tenant storage:** no overlay layers and no tenant-keyed artifact store. The version
  fingerprint is recorded in each artifact but not yet checked at replay start.
- **Human actions taken during discovery are logged but not turned into artifact steps.** Provenance
  lists them for review.
- **Single-session, single-process:** no queues, scheduling, or parallel replays, by design for this
  exercise.

**Next steps**, in order:

1. The fingerprint check at replay start.
2. Base + overlay artifacts, with drift reports turning locator fallbacks into targeted
   re-discovery of the affected step.
3. Recording human handoff actions as `performed_by: human` steps.
4. A real operator console (web, with notifications) on the existing protocol.
5. A desktop surface on UI Automation.
