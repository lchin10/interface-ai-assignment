# Computer-use automation for legacy back-office apps

A model drives a legacy banking UI once to work out how a task is done. That run is recorded as a
typed, versioned **capability artifact**. From then on the artifact is **replayed deterministically**
with no model in the loop, returning typed outputs, known business outcomes, or a debuggable failure —
and handing the live session to a human when it cannot safely continue.

```
goal ──▶ discovery (LLM)  ──▶  capability artifact (YAML)  ──▶  replay (no LLM)  ──▶ result
              │                                                      │
              └──────────────── human operator takes over ───────────┘
```

**[`evidence/`](evidence/)** holds saved runs of both paths — a real discovery run, nine replays
(including failures and business outcomes) and two human takeovers. The design write-up, with the
reasoning behind every decision below, is in **[`REPORT.md`](REPORT.md)**.

## Setup

Python 3.11+ is required. Use either your local Python or a virtual environment; every command below
is written as `python ...`, so with a venv just activate it first (or call `.venv/Scripts/python`).

**Local Python:**

```bash
python -m pip install -e .                 # the system itself
python -m pip install -e ".[demo,dev]"     # + the mock bank (Flask) and the test-suite (pytest)
python -m playwright install chromium
```

**Or a virtual environment:**

```bash
python -m venv .venv
.venv/Scripts/activate                     # Windows; source .venv/bin/activate elsewhere
python -m pip install -e ".[demo,dev]"
python -m playwright install chromium
```

**Configuration** lives in a `.env` file in the project root (gitignored). Copy the template and fill
in your key:

```bash
cp .env.example .env
```

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `discover` only | Replay never calls a model. |
| `MOCKBANK_USER`, `MOCKBANK_PASS` | sign-on to the demo app | Any values; the mock app and the automation both read them. |
| `AUTOMATION_MODEL` | optional | Defaults to `claude-opus-5`. |

The CLI, the mock bank, `demo.make_logs` and the test-suite all load `.env` automatically. A variable
already set in your real environment takes precedence over the file. Secrets are never sent to the
model, and never reach an artifact or a log.

## Run without any live service

The whole suite runs offline, with no API key: the model is replaced by a scripted stand-in and the
target app runs in-process.

```bash
python -m pytest          # 198 tests, about 2½ minutes (Chromium, headless)
python -m pytest -m live  # adds one real model-driven discovery run (uses ANTHROPIC_API_KEY)
```

## Demo path

Run everything from the project root, so `.env` is found.

**1. Start the mock legacy bank** (frameset, table layouts, no test ids):

```bash
python -m demo.mockbank.app --port 5001
```

**2. Discovery — let the model do the task once and record it:**

```bash
python -m automation discover \
  --goal "Look up member 10001 and read their current savings balance" \
  --capability demo_bank.lookup_balance \
  --param member_id=10001:pii \
  --app demo/apps/demo_bank.yaml --policy demo/policy.yaml \
  --artifacts-dir demo/artifacts --logs-dir demo/logs --headed
```

Writes `demo/artifacts/demo_bank.lookup_balance.yaml` (status `draft`) and immediately verifies it
with one replay.

**3. Replay — the production path, no model:**

```bash
python -m automation replay demo/artifacts/demo_bank.lookup_balance.yaml \
  --param member_id=10002 \
  --app demo/apps/demo_bank.yaml --policy demo/policy.yaml --logs-dir demo/logs
```

```json
{ "status": "success", "outputs": { "savings_balance": "8000.00" }, "warnings": [], ... }
```

No discovery run yet? The hand-authored artifacts in [`demo/artifacts/reviewed/`](demo/artifacts/reviewed/)
replay the same way — they are what a capability looks like after a human has tightened its contract.

**4. The interesting cases.** The mock app injects real runtime conditions with `MOCKBANK_FAULTS`
(`slow`, `notice`, `notice_always`, `session_expiry`, `server_error`, `relabel`), or `--faults`:

| What you want to see | How |
|---|---|
| Business outcome, not a crash | replay with `--param member_id=99999` → `business_outcome` |
| Permission denial | `--param member_id=40300` → `business_outcome` / `PERMISSION_DENIED` |
| Recovered interruption | start the app with `--faults notice` (dismissed) or `--faults session_expiry` (re-sign-on, flow restarted) |
| Hard failure with evidence | `--faults server_error` → `failure` / `HTTP_500`, screenshot + per-frame HTML in the run folder |
| Tenant variant / drift | `--faults relabel` → still succeeds via a fallback locator, with a drift warning |
| Irreversible action | replay `demo/artifacts/reviewed/open_sub_account.yaml` → `approval_required` unless `--approve-irreversible` |
| Human takeover | add `--escalate --headed`; open the operator page printed on stderr (`http://127.0.0.1:8765/`), press **Take control**, work in the automation's own browser window, then **Resume** |

Exit codes: `0` success, `1` failure, `2` business outcome, `3` needs a human / approval, `4` aborted.

**5. Regenerate the saved runs in [`evidence/`](evidence/)** (starts its own mock bank per scenario):

```bash
python -m demo.make_logs              # every replay scenario, no API key needed
python -m demo.make_logs --discover   # also records a real LLM discovery run
```

## Layout

```
automation/           the system
  schema.py           artifacts, app profiles, results, log events (all Pydantic)
  surface.py          perception + action seam; Playwright implementation
  policy.py           allowlist, risk classification, redaction, the single enforcement point
  logs.py             one ordered, typed, redacted log per run + its integrity checks
  replay.py           deterministic executor and error taxonomy
  agent.py            discovery loop + recorder
  handoff.py          control state machine, operator console, human-action capture
demo/                 everything mock: the bank app, its profile, policy, run logs
  artifacts/          demo_bank.lookup_balance.yaml is the discovered capability
  artifacts/reviewed/ hand-authored artifacts: the same contracts after human review
tests/                the test-suite
evidence/             saved runs of both paths
REPORT.md             design write-up
.env.example          configuration template
```

`automation/` never imports from `demo/`: the app profile, policy, artifacts and log directory are
all command-line arguments, so another tenant or product is a different set of files, not a code change.
