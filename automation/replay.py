"""Deterministic replay: the production execution path. No model is consulted anywhere here.

Per step:  pre-scan known states -> resolve target -> policy check + act -> wait for the
checkpoint while watching for known states -> classify what happened:
    business outcome   -> return it to the caller (a legitimate answer, not an error)
    recoverable        -> run its handler (bounded), then re-check / retry
    failure / unknown  -> stop with a debuggable Failure (or hand the live session to a human)
"""
from __future__ import annotations

import hashlib
import os
import time
import traceback
from decimal import Decimal, InvalidOperation
from typing import Callable

from .handoff import Controller, Handoff
from .logs import RunLog
from .policy import GuardedSurface, NotInControl, Policy, PolicyViolation, Redactor
from .schema import (
    Aborted,
    AppProfile,
    ApprovalRequired,
    Artifact,
    BusinessOutcome,
    Condition,
    Failure,
    InputValidationError,
    NeedsHuman,
    Outcome,
    OutputSpec,
    ReplayResult,
    ResultBase,
    Step,
    Success,
    Target,
    render,
    template_refs,
    validate_inputs,
)
from .surface import Surface, TargetError

MAX_RECOVERIES_PER_STEP = 2
MAX_RESTARTS = 1
MAX_ESCALATIONS_PER_STEP = 2
RESOLVE_TIMEOUT_MS = 5_000
SUCCESS_TIMEOUT_MS = 5_000

SurfaceFactory = Callable[[], Surface]
HandoffFactory = Callable[[RunLog, Surface, Redactor], Handoff]


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def policy_sha256(policy: Policy) -> str:
    return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()


def parse_output(text: str, type_: str) -> str | int:
    """Decimals are returned as strings so no precision is lost on the way to the caller."""
    if type_ == "string":
        return text
    cleaned = text.replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"not a {type_}: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"not a {type_}: {text!r}")
    number = -number if negative else number
    if type_ == "integer":
        if number != number.to_integral_value():
            raise ValueError(f"not an integer: {text!r}")
        return int(number)
    return str(number)


class _Stop(Exception):
    """Unwinds the run with its final result."""

    def __init__(self, result: ResultBase, step_status: str):
        super().__init__(result.status)
        self.result = result
        self.step_status = step_status


class _Restart(Exception):
    pass


class _ConditionHit(Exception):
    """A known state showed up while we were still looking for the control."""

    def __init__(self, outcome: Outcome):
        super().__init__(outcome.code)
        self.outcome = outcome


class Executor:
    """Runs recorded steps against a surface. Shared by replay and by discovery (for sign-on)."""

    def __init__(
        self,
        *,
        log: RunLog,
        surface: Surface,
        redactor: Redactor,
        policy: Policy,
        app: AppProfile,
        context: dict[str, dict[str, str]],
        actor: str,
        capability_id: str,
        capability_version: str,
        handoff: Handoff | None = None,
        approve_irreversible: bool = False,
        outputs: dict[str, OutputSpec] | None = None,
        mask: list[Target] | None = None,
        screenshots: bool = True,
    ):
        self.log, self.surface, self.redactor, self.app = log, surface, redactor, app
        self.context, self.actor, self.handoff = context, actor, handoff
        self.approve_irreversible = approve_irreversible
        self.output_specs = outputs or {}
        self.mask = list(app.mask) + list(mask or [])
        self.screenshots = screenshots
        self.controller = handoff.controller if handoff else Controller(log)
        self.guarded = GuardedSurface(surface, policy, self.controller, log, actor)
        self.base = {"run_id": log.run_id, "capability_id": capability_id, "capability_version": capability_version}
        self.outputs: dict[str, str | int] = {}
        self.warnings: list[str] = []
        self.recoveries: list[str] = []
        self.irreversible_done = False
        self._escalations: dict[str, int] = {}

    # -- results ----------------------------------------------------------------------
    def result(self, cls: type[ResultBase], **kw) -> ResultBase:
        return cls(**self.base, warnings=list(self.warnings), **kw)

    def failure(self, step_id: str | None, code: str, message: str, *, expected: str | None = None,
                observed: str | None = None) -> Failure:
        return self.result(Failure, code=code, message=message, step_id=step_id, expected=expected,
                           observed=observed, log_files=self.capture(step_id or "run"),
                           recoveries_attempted=list(self.recoveries))

    def capture(self, label: str) -> list[str]:
        """Failure artifacts: masked screenshot + per-frame HTML (redacted, input values stripped)."""
        files: list[str] = []
        try:
            path, rel = self.log.reserve("screenshots", label, "png")
            self.surface.screenshot(path, self.mask)
            self.log.emit("screenshot", {"path": rel, "reason": "failure"}, actor=self.actor,
                          step_id=None if label == "run" else label)
            files.append(rel)
        except Exception:
            pass
        try:
            for frame, html in self.surface.frame_html().items():
                _, rel = self.log.reserve("frames", f"{label}-{frame}", "html")
                files.append(self.log.write_text(rel, html))
        except Exception:
            pass
        return files

    def observed(self) -> str:
        texts = self.surface.visible_text()
        parts = [f"[{k}] {' '.join(v.split())[:400]}" for k, v in texts.items() if v.strip()]
        return self.redactor.text(" | ".join(parts)) or "(blank page)"

    # -- steps ------------------------------------------------------------------------
    def login(self) -> None:
        for step in self.app.login:
            self.run_step(step, [], allow_relogin=False)

    def run_step(self, step: Step, outcomes: list[Outcome], *, attempt: int = 1, allow_relogin: bool = True) -> None:
        # capability-specific outcomes first: the more specific interpretation wins
        conditions = [o for o in outcomes if o.steps is None or step.id in o.steps] + [
            c for c in self.app.conditions
            if allow_relogin or c.handler is None or c.handler.do != "relogin"
        ]
        self.log.emit("step.started", {"intent": step.intent, "action": step.action, "attempt": attempt},
                      actor=self.actor, step_id=step.id)
        t0 = time.monotonic()
        status, detail = "failed", "unexpected error"
        try:
            status, detail = self._execute(step, conditions)
        except _Stop as stop:
            status, detail = stop.step_status, getattr(stop.result, "code", stop.result.status)
            raise
        except _Restart:
            status, detail = "interrupted", "session lost; restarting the flow"
            raise
        finally:
            self.log.emit("step.finished", {"status": status, "duration_ms": _ms(t0), "detail": detail},
                          actor=self.actor, step_id=step.id)
        if self.screenshots:
            path, rel = self.log.reserve("screenshots", step.id, "png")
            self.surface.screenshot(path, self.mask)
            self.log.emit("screenshot", {"path": rel, "reason": "after_step"}, actor=self.actor, step_id=step.id)

    def _execute(self, step: Step, conditions: list[Outcome]) -> tuple[str, str]:
        recoveries = 0
        performed = False
        while True:
            if not performed:
                hit = self._detect(conditions)
                if hit is not None:
                    recoveries = self._handle(step, hit, recoveries)
                    continue
                try:
                    if self._act(step, conditions) == "completed_by_human":
                        return "completed_by_human", "operator completed the step"
                except _ConditionHit as hit:
                    recoveries = self._handle(step, hit.outcome, recoveries)
                    continue
                performed = True
            if step.expect is None:
                return "ok", ""
            outcome, hit, waited = self._wait(step.expect, conditions)
            if outcome == "passed":
                self.log.emit("checkpoint.passed", {"condition": step.expect.describe(), "waited_ms": waited},
                              actor=self.actor, step_id=step.id)
                return "ok", ""
            if hit is not None:
                recoveries = self._handle(step, hit, recoveries)
                if hit.handler and hit.handler.do == "wait_retry" and step.risk == "safe":
                    performed = False
                continue
            observed = self.observed()
            self.log.emit("checkpoint.failed", {"condition": step.expect.describe(), "observed": observed,
                                                "waited_ms": waited}, actor=self.actor, step_id=step.id)
            self._escalate_or_fail(step, "CHECKPOINT_FAILED",
                                   f"step {step.id} did not reach its checkpoint and no known state matched",
                                   expected=step.expect.describe(), observed=observed)
            if self.surface.check(step.expect):
                return "completed_by_human", "checkpoint satisfied after operator intervention"
            performed = False  # the operator put the app back in a usable state: retry

    def _act(self, step: Step, conditions: list[Outcome]) -> str | None:
        resolved = None
        if step.target is not None:
            # watch for known states while looking for the control: a step without a checkpoint would
            # otherwise burn its whole budget staring at an interstitial it knows how to dismiss
            deadline = time.monotonic() + RESOLVE_TIMEOUT_MS / 1000
            while resolved is None:
                try:
                    resolved = self.surface.resolve(step.target, 500)
                except TargetError as e:
                    hit = self._detect(conditions)
                    if hit is not None:
                        raise _ConditionHit(hit) from None
                    if time.monotonic() < deadline:
                        continue
                    self._escalate_or_fail(step, e.code, str(e), expected=step.target.describe(),
                                           observed=self.observed())
                    if step.expect is not None and self.surface.check(step.expect):
                        return "completed_by_human"
            self.log.emit("target.resolved", {
                "strategy_index": resolved.strategy_index, "strategy": resolved.strategy.by,
                "match_count": resolved.match_count, "fingerprint_ok": resolved.fingerprint_ok,
                "drift": resolved.drift,
            }, actor=self.actor, step_id=step.id)
            self.warnings.extend(f"{step.id}: {d}" for d in resolved.drift)
        value = render(step.value, self.context) if step.value is not None else None
        url = render(step.url, self.context) if step.url else None
        approved = self.approve_irreversible
        while True:
            try:
                out, decision = self.guarded.perform(
                    step.id, step.action, resolved=resolved, url=url, value=value, value_log=step.value,
                    wait_ms=step.wait_ms, declared_risk=step.risk, approved=approved,
                )
                break
            except PolicyViolation as v:
                if v.decision.rule != "irreversible_requires_approval":
                    raise _Stop(self.failure(step.id, "POLICY_BLOCKED", f"{step.action} blocked: {v.decision.rule}",
                                             expected=step.intent), "blocked") from None
                if self.handoff is None:
                    raise _Stop(self.result(ApprovalRequired, step_id=step.id, intent=step.intent),
                                "approval_required") from None
                resolution = self.handoff.escalate(
                    step_id=step.id, kind="approval",
                    reason=f"Irreversible step {step.id} needs approval: {step.intent}",
                )
                self._after_escalation(step, resolution)
                if step.expect is not None and self.surface.check(step.expect):
                    return "completed_by_human"
                approved = True  # resuming without doing it yourself is the approval
            except NotInControl as e:
                raise _Stop(self.failure(step.id, "NOT_IN_CONTROL", str(e)), "failed") from None
        if decision.risk == "irreversible":
            self.irreversible_done = True
        if step.action == "extract":
            self._store_output(step, out or "")
        return None

    def _store_output(self, step: Step, text: str) -> None:
        spec = self.output_specs[step.output]
        try:
            value = parse_output(text, spec.type)
        except ValueError:
            raise _Stop(self.failure(step.id, "OUTPUT_PARSE_ERROR", f"could not read {step.output} as {spec.type}",
                                     expected=spec.type, observed=self.redactor.text(text)), "failed") from None
        if spec.sensitivity != "public":
            self.redactor.add(text, step.output)
            self.redactor.add(value, step.output)
        self.outputs[step.output] = value
        self.log.emit("output.extracted", {"name": step.output, "type": spec.type, "sensitivity": spec.sensitivity,
                                           "value": str(value)}, actor=self.actor, step_id=step.id)

    # -- states -----------------------------------------------------------------------
    def _detect(self, conditions: list[Outcome], snapshot=None) -> Outcome | None:
        """First matching known state. One snapshot for all of them, so the order of the list decides
        which interpretation wins - not which check happened to run a few milliseconds later."""
        snapshot = snapshot or self.surface.snapshot()
        return next((c for c in conditions if self.surface.check(c.when, snapshot)), None)

    def wait_condition(self, condition: Condition, timeout_ms: int) -> tuple[bool, int]:
        t0 = time.monotonic()
        while not self.surface.check(condition):
            if _ms(t0) >= timeout_ms:
                return False, _ms(t0)
            self.surface.pump(100)
        return True, _ms(t0)

    def _wait(self, expect: Condition, conditions: list[Outcome]) -> tuple[str, Outcome | None, int]:
        t0 = time.monotonic()
        timeout = getattr(expect, "timeout_ms", SUCCESS_TIMEOUT_MS)
        while True:
            snapshot = self.surface.snapshot()
            if self.surface.check(expect, snapshot):
                return "passed", None, _ms(t0)
            hit = self._detect(conditions, snapshot)
            if hit is not None:
                return "condition", hit, _ms(t0)
            if _ms(t0) >= timeout:
                return "timeout", None, _ms(t0)
            self.surface.pump(100)

    def _handle(self, step: Step, hit: Outcome, recoveries: int) -> int:
        self.log.emit("condition.detected", {"code": hit.code, "kind": hit.kind,
                                             "handler": hit.handler.do if hit.handler else None},
                      actor=self.actor, step_id=step.id)
        if hit.kind == "business":
            message = self._matched_line(hit.when) or hit.message
            raise _Stop(self.result(BusinessOutcome, code=hit.code, message=message, step_id=step.id),
                        "business_outcome")
        if hit.kind == "failure":
            self._escalate_or_fail(step, hit.code, hit.message or f"application reported {hit.code}",
                                   expected=step.expect.describe() if step.expect else step.intent,
                                   observed=self.observed())
            return recoveries
        recoveries += 1
        handler = hit.handler
        if recoveries > MAX_RECOVERIES_PER_STEP:
            self.log.emit("recovery.attempted", {"code": hit.code, "attempt": recoveries, "action": handler.do,
                                                 "outcome": "exhausted"}, actor=self.actor, step_id=step.id)
            self._escalate_or_fail(step, "RECOVERY_EXHAUSTED",
                                   f"{hit.code} persisted after {MAX_RECOVERIES_PER_STEP} recovery attempts",
                                   expected=step.expect.describe() if step.expect else step.intent,
                                   observed=self.observed())
            return 0
        self.recoveries.append(hit.code)
        if handler.do == "relogin":
            self.log.emit("recovery.attempted", {"code": hit.code, "attempt": recoveries, "action": "relogin",
                                                 "outcome": "ok"}, actor=self.actor, step_id=step.id)
            raise _Restart(hit.code)
        outcome = "ok"
        if handler.do == "dismiss":
            try:
                resolved = self.surface.resolve(handler.target, RESOLVE_TIMEOUT_MS)
                self.guarded.perform(step.id, "click", resolved=resolved)
                # don't re-detect the interstitial we just dismissed while its page is still unloading
                if not self.surface.wait_detached(resolved, RESOLVE_TIMEOUT_MS):
                    outcome = "failed"
            except (TargetError, PolicyViolation):
                outcome = "failed"
        else:
            self.surface.pump(handler.wait_ms)
        self.log.emit("recovery.attempted", {"code": hit.code, "attempt": recoveries, "action": handler.do,
                                             "outcome": outcome}, actor=self.actor, step_id=step.id)
        return recoveries

    def _matched_line(self, condition: Condition) -> str | None:
        needles = [" ".join(n.split()) for n in condition.texts()]
        for text in self.surface.visible_text().values():
            for line in text.splitlines():
                collapsed = " ".join(line.split())
                if any(n in collapsed for n in needles):
                    return collapsed
        return None

    # -- escalation -------------------------------------------------------------------
    def _escalate_or_fail(self, step: Step, code: str, message: str, *, expected: str | None,
                          observed: str | None) -> None:
        """Returns only if a human took over and resumed; otherwise raises _Stop."""
        count = self._escalations.get(step.id, 0) + 1
        self._escalations[step.id] = count
        if self.handoff is None or count > MAX_ESCALATIONS_PER_STEP:
            raise _Stop(self.failure(step.id, code, message, expected=expected, observed=observed), "failed")
        resolution = self.handoff.escalate(step_id=step.id, kind="failure", reason=f"{code}: {message}")
        self._after_escalation(step, resolution)

    def _after_escalation(self, step: Step, resolution: str) -> None:
        if resolution == "resumed":
            return
        if resolution == "aborted":
            raise _Stop(self.result(Aborted, by=self.handoff.resolved_by, step_id=step.id), "aborted")
        raise _Stop(self.result(NeedsHuman, intervention_id=self.handoff.current.id, step_id=step.id,
                                reason="no operator responded before the handoff timed out"), "needs_human")


class _Replay(Executor):
    def __init__(self, artifact: Artifact, **kw):
        super().__init__(outputs=artifact.outputs, **kw)
        self.artifact = artifact

    def run(self) -> ResultBase:
        restarts = 0
        while True:
            try:
                if "session.authenticated" in self.artifact.requires:
                    self.login()
                for step in self.artifact.steps:
                    self.run_step(step, self.artifact.outcomes, attempt=restarts + 1)
                ok, waited = self.wait_condition(self.artifact.success, SUCCESS_TIMEOUT_MS)
                condition = "success: " + self.artifact.success.describe()
                if not ok:
                    observed = self.observed()
                    self.log.emit("checkpoint.failed", {"condition": condition, "observed": observed,
                                                        "waited_ms": waited}, actor=self.actor)
                    return self.failure(None, "SUCCESS_CONDITION_FAILED", "all steps ran but the success condition "
                                        "was not met", expected=self.artifact.success.describe(), observed=observed)
                self.log.emit("checkpoint.passed", {"condition": condition, "waited_ms": waited}, actor=self.actor)
                return self.result(Success, outputs=dict(self.outputs))
            except _Restart as restart:
                if self.irreversible_done:
                    return self.failure(None, "RESTART_UNSAFE", f"{restart} after an irreversible step; "
                                        "refusing to replay the flow from the start")
                if restarts >= MAX_RESTARTS:
                    return self.failure(None, "RECOVERY_EXHAUSTED", f"{restart}: flow already restarted "
                                        f"{MAX_RESTARTS} time(s)")
                restarts += 1
            except _Stop as stop:
                return stop.result


def sensitive_mask(artifact: Artifact) -> list[Target]:
    """Controls that hold sensitive inputs or outputs never appear unmasked in screenshots."""
    out = []
    for s in artifact.steps:
        if s.target is None:
            continue
        refs = [name for ns, name in template_refs(s.value) if ns == "inputs"]
        if any(artifact.inputs[r].sensitivity != "public" for r in refs):
            out.append(s.target)
        if s.action == "extract" and artifact.outputs[s.output].sensitivity != "public":
            out.append(s.target)
    return out


def replay(
    artifact: Artifact,
    raw_inputs: dict[str, str],
    *,
    app: AppProfile,
    policy: Policy,
    logs_root,
    surface_factory: SurfaceFactory,
    base_url: str | None = None,
    approve_irreversible: bool = False,
    handoff_factory: HandoffFactory | None = None,
    secrets: dict[str, str] | None = None,
    artifact_sha256: str | None = None,
    screenshots: bool = True,
) -> ReplayResult:
    t0 = time.monotonic()
    secrets = dict(os.environ if secrets is None else secrets)
    redactor = Redactor()
    for name in app.secrets:
        if secrets.get(name):
            redactor.add(secrets[name], "secret")
    for name, value in raw_inputs.items():
        spec = artifact.inputs.get(name)
        if spec is None or spec.sensitivity != "public":
            redactor.add(value, name)

    log = RunLog(logs_root, redactor)
    base_url = (base_url or app.base_url).rstrip("/")
    cap = artifact.capability
    log.emit("run.started", {
        "mode": "replay", "capability_id": cap.id, "capability_version": cap.version,
        "artifact_sha256": artifact_sha256, "inputs": {k: str(v) for k, v in raw_inputs.items()},
        "policy_sha256": policy_sha256(policy), "app_product": app.product, "base_url": base_url,
    }, actor="replay")
    base = {"run_id": log.run_id, "capability_id": cap.id, "capability_version": cap.version}

    surface: Surface | None = None
    handoff: Handoff | None = None
    runner: _Replay | None = None
    try:
        inputs = validate_inputs(artifact, raw_inputs)
        missing = [n for n in app.secrets if not secrets.get(n)]
        if missing:
            result = Failure(**base, code="MISSING_SECRET", message=f"environment variables not set: {missing}")
        else:
            surface = surface_factory()
            handoff = handoff_factory(log, surface, redactor) if handoff_factory else None
            runner = _Replay(
                artifact, log=log, surface=surface, redactor=redactor, policy=policy, app=app,
                context={"inputs": inputs, "app": {"base_url": base_url},
                         "secrets": {n: secrets[n] for n in app.secrets}},
                actor="replay", capability_id=cap.id, capability_version=cap.version, handoff=handoff,
                approve_irreversible=approve_irreversible, mask=sensitive_mask(artifact), screenshots=screenshots,
            )
            result = runner.run()
    except InputValidationError as e:
        result = Failure(**base, code="INVALID_INPUT", message=str(e))
    except Exception as exc:  # a bug or a dead browser: still produce a complete, closed log
        log.emit("error", {"error_type": type(exc).__name__, "message": str(exc),
                           "traceback": traceback.format_exc()}, actor="system")
        files = runner.capture("run") if runner else []
        result = Failure(**base, code="INTERNAL_ERROR", message=f"{type(exc).__name__}: {exc}", log_files=files)
    result.duration_ms = _ms(t0)
    try:
        log.finish(result, result.duration_ms)
    finally:
        if handoff:
            handoff.close()
        if surface:
            surface.close()
    return result
