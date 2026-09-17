# Design write-up

## 1. Architecture

One Python process, synchronous, four seams. Nothing is queued, nothing is a service: a capability
invocation is a function call that drives a browser and returns a typed result.

| Seam | Module | Why it exists |
|---|---|---|
| Perception & action | `surface.py` | Everything above it speaks *element index*, never DOM. The recorded flow does not know what a browser is. |
| Policy | `policy.py` | One chokepoint (`GuardedSurface.perform`). Discovery, replay and post-handoff resume all go through it, so there is one place to audit. |
| Evidence | `logs.py` | One writer per run; ordered, typed, redacted; self-checking. Saved runs are in `evidence/`. |
| Control | `handoff.py` | Who owns the live session, as an explicit state machine. |

Discovery and replay share one `Executor`: sign-on (from the app profile) and step execution are the
same code, so the deterministic path is exercised even during discovery.

**Key trade-offs.** Sync Playwright over async: replay is inherently sequential and a human has to be
able to grab the same window. YAML artifacts on disk over a registry: they are reviewed in pull
requests, which is what "reviewable" means in a bank. A single process over services: the brief
rewards abstractions that *could* scale, and a queue would add operational surface without testing any
of the interesting decisions. The costs are real — no concurrency across capabilities, no artifact
catalogue, in-memory interventions — and are listed in § 7.

**Choices the brief leaves open.** *Python*, because Playwright, Pydantic and the Anthropic SDK are all
first-class there and the whole system is I/O-bound glue. *Playwright* over Selenium (weaker waiting, no
role locators), raw CDP (frames, input and screenshots all rewritten by hand) and screenshot +
coordinates (not deterministically replayable, and reading a value needs OCR): nothing here is scraped —
the system drives a live session that a human must be able to seize mid-run — and it is confined to
`surface.py` behind the `Surface` protocol. *Claude `claude-opus-5`* for discovery, injected as a
`messages.create`-compatible callable so the tests pass a scripted stand-in; one tool call per turn with
`disable_parallel_tool_use`, and every call carries the reasoning that becomes the step's `intent`. The
*target* is a local mock core-banking app rather than a public demo site: framesets, table layout and no
test ids are exactly the conditions § 1 of the brief describes, faults can be injected on demand, and no
third-party service is hammered and no real credentials or PII are involved.

## 2. Artifact schema

An artifact is a **callable contract**, not a macro. `demo_bank.lookup_balance` is: typed `inputs`,
typed `outputs` (with `from_step` provenance), `requires: [session.authenticated]`, ordered `steps`,
known `outcomes`, and a `success` condition. It is versioned (semver), carries `provenance`
(run id, model, timestamp) and a `status` of `draft` → `approved`; discovery always writes `draft`.

Four deliberate choices:

**Targets are a ranked list of independent strategies, plus a fingerprint.** Legacy markup has no test
ids, so a locator is a hypothesis, not a fact:

```yaml
target:
  frame: [main]
  strategies:
    - {by: adjacent_label, text: "Member ID:", control: input}   # the cell next to the label cell
    - {by: css, value: "body > font ... > tr:nth-of-type(1) > td:nth-of-type(2) > input"}
  fingerprint: {tag: input, type: text, role: textbox, name: Member ID}
```

Strategy kinds are `role` (accessible role + name), `adjacent_label` (the legacy table-form idiom),
`table_cell` (row key × column header), `text`, and structural `css` as the last resort. `tag`/`type`
in the fingerprint are hard checks — a match that is the wrong kind of control is refused, not used;
the name is a soft check that produces a drift warning. Recording verifies every candidate against the
live page and keeps only those that resolve uniquely to the element just acted on.

**Values are templates, never literals.** `{{inputs.member_id}}` is recorded, never `10001`. The
recorder also refuses to build a locator out of an input value or a masked screen value, so an artifact
cannot encode one member's data.

**A step replay cannot verify is not worth recording.** The model is asked for a checkpoint after each
click; if it gives one that was already on screen (so it proves nothing) or gives none at all, the
recorder proposes its own: text that appeared *because of* that action. What the model cannot supply —
input patterns, capability-specific outcomes — is what the `draft → approved` review step is for.

**Product knowledge lives in the app profile, not in every artifact.** `demo/apps/demo_bank.yaml`
holds sign-on, the masks, and the states any capability can hit (`SESSION_EXPIRED`, `SYSTEM_NOTICE`,
`PERMISSION_DENIED`, `RECORD_NOT_FOUND`, `HTTP_500`). An artifact adds only what is specific to it.
Capability-specific outcomes are matched first, so a capability can give a product-wide message a more
precise meaning.

## 3. Determinism & error handling

Replay never asks a model anything. Determinism comes from: inputs validated against the contract
*before* the browser opens; targets resolved by strategy order with a **unique-match requirement**
(two matches is an error, not a coin flip); a settle check so nothing reasons about a half-loaded
screen; and an explicit `expect` checkpoint per step instead of assuming a click worked.

Known states are watched **both** while waiting for a checkpoint and while looking for a control —
otherwise a step whose checkpoint is missing would spend its whole resolve budget staring at an
interstitial it knows how to dismiss. Every poll takes **one snapshot of the screen** and evaluates the
checkpoint and all known states against it, so which interpretation wins is decided by the order of the
list (capability-specific before product-wide), never by which check happened to run a few milliseconds
later while the page was still rendering. The classification:

| Class | Example | Behaviour |
|---|---|---|
| **business** | no such member, permission denied, validation error | Returned as `business_outcome` with the message the app showed. Not an error. |
| **recoverable** | maintenance interstitial, session expiry, slow load | Handler runs (dismiss / re-sign-on / wait), bounded to 2 attempts per step, each logged. Re-sign-on restarts the flow — and is refused if an irreversible step already ran. |
| **failure** | HTTP 500, unknown screen, missing control | Stops with the step id, what was expected, what was observed, and a masked screenshot plus per-frame HTML. |

The result contract makes the distinction explicit: `success` (with outputs) · `business_outcome` ·
`failure` · `approval_required` · `needs_human` · `aborted`. Conflating the first two is the mistake
the brief warns about, so it is a type-level distinction, not a convention.

Drift is a *signal*, not a crash: when a fallback strategy wins, or the element's name changed, the run
still succeeds and reports a warning naming the step and the strategy. The `relabel` fault
("Member ID:" → "Member #:") is a standing test of exactly that.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is the element index: `{role, name, label, row/column header, text,
frame path, structural path}`. A modern web app fills it from the DOM; a legacy app fills it from the
same DOM but leans on `adjacent_label`/`table_cell` because nothing else is stable; a desktop app would
fill it from UI Automation / AT-SPI, where role and name are exactly the fields the accessibility tree
already exposes; a Citrix-style pixel surface would fill it from OCR with an image anchor strategy.
Steps, conditions, checkpoints and the whole error taxonomy are surface-independent — only strategy
kinds are surface-typed, and they are a discriminated union, so adding `by: ui_automation` or
`by: ocr_text` is additive. The screenshot path already proves the model can work from pixels plus an
index rather than from markup.

**Multi-tenant.** An artifact is keyed to a *product* and version range, not to a tenant; the tenant
supplies `base_url` and its own policy. Hundreds of tenants on one vendor product therefore share one
recording. Where a tenant genuinely differs (branding, a relabelled field, an extra confirmation
screen), the intended mechanism is a **tenant overlay**: a merge patch addressed by step id that can
add a strategy, override a label, or insert a step, resolved base → product version → tenant. Nothing
in the schema needs to change for that; the loader does. Per-tenant drift is detected from data the
system already emits: which strategy won, fingerprint name changes, and recovery counts, per tenant —
a tenant whose runs suddenly resolve via `css` has drifted, and a canary replay per tenant per release
turns that into an alert before a caller sees it. I implemented the signal, not the fleet.

## 5. Escalation & handoff

"Stuck" is never guessed. Discovery escalates when: the model calls `escalate`; it attempts an
irreversible action (always); three consecutive actions leave the screen unchanged; tool errors repeat;
or the turn budget runs out. Replay escalates (when run with `--escalate`) on a hard failure, an
unrecoverable condition, or an irreversible step awaiting approval.

Control is an explicit state machine with a single owner:

```
AUTOMATION ──escalate──▶ AWAITING_HUMAN ──take──▶ HUMAN ──resume──▶ AUTOMATION
                              └──────────abort───────┴──▶ ABORTED
```

Only the automation thread applies transitions; the operator console *requests* them and the run loop
applies them between Playwright calls. That is what keeps the log ordered and lets in-flight human
events drain before control returns. `GuardedSurface` refuses any automation action unless the state is
`AUTOMATION`, so "who is in control" is enforced, not documented.

The human works in **the same browser window** the automation was using — the intervention record
carries the capability, step, reason, URL and a masked screenshot. Their actions are captured through
an init script plus a Playwright binding and logged as `human.action` (password values never), and in
discovery they are recorded into the artifact as steps flagged `recorded_from: human` for review.

Resume is semantic, not positional: replay re-checks the paused step's checkpoint. Satisfied means the
human did the work (`completed_by_human`, no double submit); unsatisfied means retry the step. For an
irreversible step, resuming *is* the approval. No answer within the timeout gives `needs_human` with the
intervention id; **Abort** gives `aborted`.

The operator console is deliberately a mock: one local HTML page and a JSON API, no auth, no queue.
The mechanism behind it — the state machine, capture, and resume semantics — is real.

## 6. Safety

**Allowlist.** Origins, path globs (normalised, so `/open/../admin` is caught) and action types, checked
at one chokepoint on every action — including the destination of a link before it is clicked. Every
decision is logged, allowed or not.

**Irreversible actions.** Declared per step in the artifact *and* classified independently from the
control's name (`confirm`, `transfer`, `delete`, ...), so an author's omission is still caught. The
discovery agent may never perform one: attempting it hands the session to a human. Replay stops with
`approval_required` unless the caller passes `--approve-irreversible` or an operator approves live. The
asymmetry is deliberate: a read is cheap to retry, a money movement is not.

**Data.** Credentials come from the environment, are typed as `secrets.*` references, and never enter a
prompt, an artifact or a log. Sensitivity is part of the contract (`public` / `pii` / `financial` /
`secret`), and everything crosses the log writer, which redacts registered values plus SSN, card and
account patterns. Screenshots mask profile-configured regions, and masked values are withheld from the
model's element list too — including where a neighbouring cell would leak them back as a row key, which
is a bug this design caught during development. Playwright traces are deliberately *not* recorded:
they capture full DOM and network, including a password POST.

**Limits.** Free-text PII on screen (a member's name) still reaches the model during discovery and can
appear in a failure's `observed` summary; only configured regions are masked. The operator console has
no authentication. The policy is per-run configuration, not a signed artifact. A determined model could
still take an unwise-but-allowed action — the allowlist bounds blast radius, it does not judge intent.

## 7. Cuts

Deliberately not built: a real operator console (remote co-browsing over CDP/noVNC, auth, queueing of
interventions); a desktop or OCR `Surface`; the tenant-overlay loader and drift dashboard described in
§ 4; an agent-facing capability catalogue with typed discovery; persisted approval workflow
(`draft → approved` exists as a field, nothing enforces it before unattended replay); bounded LLM
recovery for a single failed step; multi-run stability scoring; any queue, worker or multi-tenant
plumbing.

With more time, in order: (1) the tenant overlay loader plus a second app variant in the demo, because
that is the claim in § 4 that is argued rather than shown; (2) the approval gate, since `draft`
artifacts replaying unattended is the weakest link in the safety story; (3) the capability catalogue,
which is what actually makes these artifacts callable by the agent-facing product; (4) stability
scoring, to turn drift warnings into a trend instead of a per-run footnote.
