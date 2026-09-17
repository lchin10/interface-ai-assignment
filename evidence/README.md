# Saved runs

Each folder is one complete run:

| File | What it is |
|---|---|
| `events.jsonl` | the ordered, typed, redacted record of everything that happened and why |
| `result.json` | the result the caller received (identical to the run's last event) |
| `screenshots/` | one masked screenshot per step, plus one per failure or intervention |
| `frames/` | per-frame HTML captured on failure (redacted, input values stripped) |
| `artifact.yaml` | the capability recorded by a discovery run |
| `intervention.json` | the request that was sent to a human operator, when there was one |

Regenerate everything with `python -m demo.make_logs` (add `--discover` for the discovery run).

## Discovery

| Run | Result | Shows |
|---|---|---|
| `discovery` | `artifact_written` | a real Claude session driving the app: `llm.request` / `llm.response` events with the model's own reasoning, each action policy-checked and recorded, then the artifact written |
| `discovery-verification-replay` | `success` | the artifact it just recorded, replayed immediately with no model in the loop |

The artifact produced is [`demo/artifacts/demo_bank.lookup_balance.yaml`](../demo/artifacts/demo_bank.lookup_balance.yaml).
It is `status: draft`: the model recorded the flow, the types and the sensitivity of each value, and a
checkpoint for the click. Tightening the contract — an id `pattern`, capability-specific outcomes —
is the human review step before `approved`. The hand-authored artifacts in
[`demo/artifacts/reviewed/`](../demo/artifacts/reviewed/) show what a reviewed one looks like.

## Replay runs

The lookup runs below replay the **discovered** artifact. The sub-account runs use the hand-authored
[`demo/artifacts/reviewed/open_sub_account.yaml`](../demo/artifacts/reviewed/open_sub_account.yaml),
which has the irreversible confirm step.

| Run | Result | Shows |
|---|---|---|
| `replay-success` | `success`, `savings_balance = 12450.33` | the production path: sign-on, locate, act, checkpoint, typed output |
| `replay-business-outcome-not-found` | `business_outcome` / `RECORD_NOT_FOUND` | "no such member" returned as an answer, not an error — from the product-wide catalogue in the app profile, so every capability inherits it |
| `replay-recovered-session-expiry` | `success` | session expiry detected, re-sign-on, flow restarted, then completed |
| `replay-recovered-interstitial` | `success` | a maintenance interstitial dismissed on a deliberately slow app — noticed while the next step was still looking for its control |
| `replay-tenant-variant-drift` | `success` + warnings | the field is relabelled "Member #:"; a fallback locator carries the run and reports drift |
| `replay-failure-server-error` | `failure` / `HTTP_500` | a hard failure with step, expectation, observation, screenshot and frame HTML |
| `replay-invalid-input` | `failure` / `INVALID_INPUT` | the contract rejects the input before a browser is even launched (the reviewed artifact, whose `member_id` carries a pattern) |
| `replay-approval-required` | `approval_required` | an irreversible step refuses to run unattended |
| `replay-approved-irreversible` | `success` | the same step with the caller's explicit approval |

## Human-in-the-loop runs

In both of these the automation paused, published an intervention, and handed the live session over.
The operator was driven by a script (`demo/make_logs.py`) standing in for a person at the console; the
state machine, the action capture and the resume semantics are the real ones.

| Run | Result | Shows |
|---|---|---|
| `handoff-operator-approves` | `success` | operator takes control, approves by resuming, automation performs the irreversible step |
| `handoff-operator-confirms` | `success` | operator performs the irreversible step themselves; replay sees the checkpoint already met, records the step as `completed_by_human`, and carries on |

Follow `control.transferred` and `human.action` in `events.jsonl` to see control move and what the
person did.
