"""Typed contracts: capability artifacts, app profiles, replay results, interventions and log events.

Everything that crosses a boundary (disk, the calling agent, the operator, the log) is a
Pydantic model here, so there is exactly one definition of each shape.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

SCHEMA_VERSION = 1
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_]+)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

Sensitivity = Literal["public", "pii", "financial", "secret"]
Risk = Literal["safe", "irreversible"]
ActionKind = Literal["navigate", "click", "fill", "select", "extract", "wait"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- targets
# A target is an ordered list of independent ways to find one control. Replay tries them
# in order and requires exactly one match; a later strategy winning is reported as drift.


class RoleStrategy(Model):
    """Accessibility role + accessible name. Most stable when the markup exposes names."""

    by: Literal["role"]
    role: str
    name: str


class AdjacentLabelStrategy(Model):
    """The control sitting in the cell right after a label cell (legacy table forms)."""

    by: Literal["adjacent_label"]
    text: str
    control: Literal["input", "select", "cell"] = "input"


class TableCellStrategy(Model):
    """A data cell addressed by its row's label cell and its column header."""

    by: Literal["table_cell"]
    row: str
    column: str


class TextStrategy(Model):
    """Exact visible text (links, buttons with text content)."""

    by: Literal["text"]
    text: str


class CssStrategy(Model):
    """Structural path. Last resort: survives relabelling, breaks on layout changes."""

    by: Literal["css"]
    value: str


Strategy = Annotated[
    Union[RoleStrategy, AdjacentLabelStrategy, TableCellStrategy, TextStrategy, CssStrategy],
    Field(discriminator="by"),
]


def describe_strategy(s: Strategy) -> str:
    match s:
        case RoleStrategy():
            return f'role={s.role} name="{s.name}"'
        case AdjacentLabelStrategy():
            return f'{s.control} after label "{s.text}"'
        case TableCellStrategy():
            return f'cell row="{s.row}" column="{s.column}"'
        case TextStrategy():
            return f'text="{s.text}"'
        case CssStrategy():
            return f"css={s.value}"


class Fingerprint(Model):
    """What the matched element must look like. tag/type are hard checks; name is soft (drift)."""

    tag: str
    type: str | None = None
    role: str | None = None
    name: str | None = None


class Target(Model):
    frame: list[str] = Field(default_factory=list, description="Frame names from the top document")
    strategies: list[Strategy] = Field(min_length=1)
    fingerprint: Fingerprint | None = None

    def describe(self) -> str:
        where = "/".join(self.frame) or "top"
        return f"[{where}] " + " | ".join(describe_strategy(s) for s in self.strategies)


# --------------------------------------------------------------------------- conditions


class Condition(Model):
    """Exactly one of the fields is set. Deliberately tiny: easy to review, easy to port."""

    text_visible: str | None = None
    url_matches: str | None = None
    element_present: Target | None = None
    all: list[Condition] | None = None
    any: list[Condition] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        fields = ("text_visible", "url_matches", "element_present", "all", "any")
        set_ = [f for f in fields if getattr(self, f) is not None]
        if len(set_) != 1:
            raise ValueError(f"condition needs exactly one of {fields}, got {set_ or 'none'}")
        if self.url_matches is not None:
            re.compile(self.url_matches)
        return self

    def describe(self) -> str:
        if self.text_visible is not None:
            return f'text visible "{self.text_visible}"'
        if self.url_matches is not None:
            return f"url matches /{self.url_matches}/"
        if self.element_present is not None:
            return f"element present {self.element_present.describe()}"
        joiner, parts = (" AND ", self.all) if self.all is not None else (" OR ", self.any)
        return "(" + joiner.join(c.describe() for c in parts) + ")"

    def texts(self) -> list[str]:
        if self.text_visible is not None:
            return [self.text_visible]
        return [t for c in (self.all or self.any or []) for t in c.texts()]


class Expect(Condition):
    """A checkpoint: the condition that proves a step worked, and how long to wait for it."""

    timeout_ms: int = Field(10_000, ge=100, le=120_000)


# --------------------------------------------------------------------------- artifact


class InputSpec(Model):
    type: Literal["string", "integer", "decimal", "enum"]
    description: str = ""
    pattern: str | None = None
    values: list[str] | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    sensitivity: Sensitivity = "public"

    @model_validator(mode="after")
    def _check(self) -> InputSpec:
        if self.type == "enum" and not self.values:
            raise ValueError("enum inputs need values")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self


class OutputSpec(Model):
    type: Literal["string", "integer", "decimal"]
    description: str = ""
    sensitivity: Sensitivity = "public"
    from_step: str


class Handler(Model):
    do: Literal["dismiss", "relogin", "wait_retry"]
    target: Target | None = None
    wait_ms: int = Field(1_000, ge=0, le=60_000)

    @model_validator(mode="after")
    def _check(self) -> Handler:
        if self.do == "dismiss" and self.target is None:
            raise ValueError("dismiss handler needs a target")
        return self


class Outcome(Model):
    """A recognisable runtime state and what it means.

    business    a legitimate answer for the caller (e.g. no such member) - not an error
    recoverable a known interruption with a deterministic handler (interstitial, expiry)
    failure     a known app error; stop and report it
    """

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    kind: Literal["business", "recoverable", "failure"]
    when: Condition
    message: str = ""
    steps: list[str] | None = Field(None, description="Only checked during these steps; null = every step")
    handler: Handler | None = None

    @model_validator(mode="after")
    def _check(self) -> Outcome:
        if (self.kind == "recoverable") != (self.handler is not None):
            raise ValueError("recoverable outcomes need a handler; other kinds must not have one")
        return self


class Step(Model):
    id: str = Field(pattern=r"^[a-z0-9_.]+$")
    intent: str = ""
    action: ActionKind
    target: Target | None = None
    url: str | None = None
    value: str | None = Field(None, description="Template or literal; never a raw sensitive value")
    output: str | None = None
    wait_ms: int | None = None
    risk: Risk = "safe"
    expect: Expect | None = None
    recorded_from: Literal["author", "agent", "human"] = "author"

    @model_validator(mode="after")
    def _check(self) -> Step:
        needs_target = self.action in ("click", "fill", "select", "extract")
        if needs_target and self.target is None:
            raise ValueError(f"{self.id}: {self.action} needs a target")
        if self.action == "navigate" and not self.url:
            raise ValueError(f"{self.id}: navigate needs a url")
        if self.action in ("fill", "select") and self.value is None:
            raise ValueError(f"{self.id}: {self.action} needs a value")
        if self.action == "extract" and not self.output:
            raise ValueError(f"{self.id}: extract needs an output name")
        if self.action != "extract" and self.output:
            raise ValueError(f"{self.id}: only extract steps may set output")
        if self.risk == "irreversible" and self.expect is None:
            # after a handoff we must be able to tell whether the human already did it
            raise ValueError(f"{self.id}: irreversible steps need an expect checkpoint")
        return self


class AppRef(Model):
    product: str
    product_version: str
    surface: Literal["web", "desktop", "terminal"] = "web"


class Provenance(Model):
    run_id: str | None = None
    model: str | None = None
    recorded_at: datetime | None = None
    reviewed_by: str | None = None


class CapabilityMeta(Model):
    id: str = Field(pattern=r"^[a-z0-9_]+\.[a-z0-9_]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    summary: str
    app: AppRef
    status: Literal["draft", "approved"] = "draft"
    provenance: Provenance = Field(default_factory=Provenance)


def template_refs(text: str | None) -> list[tuple[str, str]]:
    return TEMPLATE_RE.findall(text or "")


class Artifact(Model):
    schema_version: Literal[1] = SCHEMA_VERSION
    capability: CapabilityMeta
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    requires: list[Literal["session.authenticated"]] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    outcomes: list[Outcome] = Field(default_factory=list)
    success: Condition

    @model_validator(mode="after")
    def _check(self) -> Artifact:
        ids = [s.id for s in self.steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate step ids: {dupes}")
        by_id = {s.id: s for s in self.steps}
        for s in self.steps:
            for ns, name in template_refs(s.value) + template_refs(s.url):
                if ns == "inputs" and name not in self.inputs:
                    raise ValueError(f"{s.id}: unknown input {{{{inputs.{name}}}}}")
                if ns == "app" and name != "base_url":
                    raise ValueError(f"{s.id}: unknown app value {{{{app.{name}}}}}")
                if ns not in ("inputs", "app"):
                    raise ValueError(f"{s.id}: artifacts may only reference inputs.* and app.base_url, not {ns}.*")
            if s.action == "extract" and s.output not in self.outputs:
                raise ValueError(f"{s.id}: extracts undeclared output {s.output!r}")
        for name, out in self.outputs.items():
            step = by_id.get(out.from_step)
            if step is None or step.action != "extract" or step.output != name:
                raise ValueError(f"output {name!r}: from_step must be the extract step that produces it")
        for o in self.outcomes:
            unknown = [sid for sid in o.steps or [] if sid not in by_id]
            if unknown:
                raise ValueError(f"outcome {o.code}: unknown steps {unknown}")
        return self


class AppProfile(Model):
    """Per-product knowledge shared by every capability: sign-on, app-wide states, masking."""

    schema_version: Literal[1] = SCHEMA_VERSION
    product: str
    product_version: str
    base_url: str
    secrets: list[str] = Field(default_factory=list, description="Env var names; values never stored")
    login: list[Step] = Field(default_factory=list)
    conditions: list[Outcome] = Field(default_factory=list)
    mask: list[Target] = Field(default_factory=list, description="Always masked in screenshots")

    @model_validator(mode="after")
    def _check(self) -> AppProfile:
        for s in self.login:
            for ns, name in template_refs(s.value) + template_refs(s.url):
                if ns == "secrets" and name not in self.secrets:
                    raise ValueError(f"{s.id}: secret {name} is not declared in secrets")
                if ns not in ("secrets", "app"):
                    raise ValueError(f"{s.id}: login steps may only use secrets.* and app.base_url")
        return self


# --------------------------------------------------------------------------- inputs & templates


class InputValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def validate_inputs(artifact: Artifact, raw: dict[str, str]) -> dict[str, str]:
    """Check caller-supplied values against the artifact contract. Returns normalised strings."""
    errors: list[str] = []
    out: dict[str, str] = {}
    for name in sorted(set(raw) - set(artifact.inputs)):
        errors.append(f"unexpected input {name!r}")
    for name, spec in artifact.inputs.items():
        if name not in raw:
            errors.append(f"missing input {name!r}")
            continue
        value = str(raw[name]).strip()
        if spec.type == "enum":
            if value not in (spec.values or []):
                errors.append(f"{name}: must be one of {spec.values}")
                continue
        elif spec.type in ("decimal", "integer"):
            try:
                number = Decimal(value)
                if not number.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                errors.append(f"{name}: not a {spec.type}")
                continue
            if spec.type == "integer" and number != number.to_integral_value():
                errors.append(f"{name}: not an integer")
                continue
            if spec.min is not None and number < spec.min:
                errors.append(f"{name}: must be >= {spec.min}")
                continue
            if spec.max is not None and number > spec.max:
                errors.append(f"{name}: must be <= {spec.max}")
                continue
        if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
            errors.append(f"{name}: does not match {spec.pattern}")
            continue
        out[name] = value
    if errors:
        raise InputValidationError(errors)
    return out


class TemplateError(KeyError):
    pass


def render(text: str, context: dict[str, dict[str, str]]) -> str:
    def sub(m: re.Match) -> str:
        try:
            return context[m.group(1)][m.group(2)]
        except KeyError:
            raise TemplateError(f"no value for {{{{{m.group(1)}.{m.group(2)}}}}}") from None

    return TEMPLATE_RE.sub(sub, text)


# --------------------------------------------------------------------------- results


class ResultBase(Model):
    run_id: str
    capability_id: str
    capability_version: str
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)


class Success(ResultBase):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]


class BusinessOutcome(ResultBase):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    step_id: str | None


class Failure(ResultBase):
    status: Literal["failure"] = "failure"
    code: str
    message: str
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    log_files: list[str] = Field(default_factory=list)
    recoveries_attempted: list[str] = Field(default_factory=list)


class NeedsHuman(ResultBase):
    status: Literal["needs_human"] = "needs_human"
    intervention_id: str
    step_id: str | None
    reason: str


class ApprovalRequired(ResultBase):
    status: Literal["approval_required"] = "approval_required"
    step_id: str
    intent: str


class Aborted(ResultBase):
    status: Literal["aborted"] = "aborted"
    by: str
    step_id: str | None


ReplayResult = Annotated[
    Union[Success, BusinessOutcome, Failure, NeedsHuman, ApprovalRequired, Aborted],
    Field(discriminator="status"),
]
REPLAY_RESULT = TypeAdapter(ReplayResult)


class DiscoveryResult(Model):
    status: Literal["artifact_written", "failure", "needs_human", "aborted"]
    run_id: str
    capability_id: str
    code: str | None = None
    message: str = ""
    artifact_path: str | None = None
    steps_recorded: int = 0
    turns: int = 0
    duration_ms: int = 0
    verification: dict[str, Any] | None = None


class Intervention(Model):
    id: str
    run_id: str
    subject: str
    step_id: str | None
    kind: Literal["stuck", "failure", "approval"]
    reason: str
    url: str
    screenshot: str | None
    requested_at: datetime


# --------------------------------------------------------------------------- log events

Actor = Literal["system", "agent", "replay", "human", "operator"]


class Event(Model):
    seq: int = Field(ge=1)
    ts: str
    mono_ns: int
    run_id: str
    actor: Actor
    type: str
    step_id: str | None = None
    data: dict[str, Any]


class RunStarted(Model):
    mode: Literal["replay", "discovery"]
    capability_id: str
    capability_version: str | None = None
    artifact_sha256: str | None = None
    goal: str | None = None
    inputs: dict[str, str]
    policy_sha256: str
    app_product: str
    base_url: str
    model: str | None = None


class ObservationData(Model):
    url: str
    frames: list[str]
    element_count: int
    observation_hash: str
    screenshot: str | None


class ScreenshotData(Model):
    path: str
    reason: Literal["after_step", "failure", "intervention"]


class LlmRequest(Model):
    turn: int
    model: str
    message_count: int
    new_content: list[dict[str, Any]]


class LlmResponse(Model):
    turn: int
    stop_reason: str | None
    text: str
    tool: str | None
    tool_input: dict[str, Any] | None
    usage: dict[str, Any]
    latency_ms: int
    request_id: str | None


class PolicyDecisionData(Model):
    action: str
    target: str | None
    url: str | None
    allowed: bool
    rule: str
    risk: Risk


class StepStarted(Model):
    intent: str
    action: str
    attempt: int = 1


class StepFinished(Model):
    status: Literal[
        "ok", "failed", "business_outcome", "interrupted", "completed_by_human",
        "approval_required", "aborted", "blocked", "needs_human",
    ]
    duration_ms: int
    detail: str = ""


class TargetResolved(Model):
    strategy_index: int
    strategy: str
    match_count: int
    fingerprint_ok: bool
    drift: list[str]


class ActionPerformed(Model):
    action: str
    value: str | None
    duration_ms: int


class CheckpointPassed(Model):
    condition: str
    waited_ms: int


class CheckpointFailed(Model):
    condition: str
    observed: str
    waited_ms: int


class ConditionDetected(Model):
    code: str
    kind: str
    handler: str | None


class RecoveryAttempted(Model):
    code: str
    attempt: int
    action: str
    outcome: Literal["ok", "failed", "exhausted"]


class OutputExtracted(Model):
    name: str
    type: str
    sensitivity: Sensitivity
    value: str


class InterventionRequested(Model):
    intervention: dict[str, Any]


class InterventionResolved(Model):
    intervention_id: str
    resolution: Literal["resumed", "aborted", "timeout"]
    by: str
    human_action_count: int


class ControlTransferred(Model):
    from_state: str
    to_state: str
    by: str


class HumanAction(Model):
    kind: Literal["click", "fill", "select", "navigate"]
    frame: str
    element: dict[str, Any] | None
    value: str | None
    url: str | None


class ArtifactWritten(Model):
    path: str
    published_to: str
    capability_id: str
    version: str
    sha256: str


class ErrorData(Model):
    error_type: str
    message: str
    traceback: str


class RunFinished(Model):
    status: str
    result: dict[str, Any]
    duration_ms: int


EVENT_DATA: dict[str, type[Model]] = {
    "run.started": RunStarted,
    "observation": ObservationData,
    "screenshot": ScreenshotData,
    "llm.request": LlmRequest,
    "llm.response": LlmResponse,
    "policy.decision": PolicyDecisionData,
    "step.started": StepStarted,
    "step.finished": StepFinished,
    "target.resolved": TargetResolved,
    "action.performed": ActionPerformed,
    "checkpoint.passed": CheckpointPassed,
    "checkpoint.failed": CheckpointFailed,
    "condition.detected": ConditionDetected,
    "recovery.attempted": RecoveryAttempted,
    "output.extracted": OutputExtracted,
    "intervention.requested": InterventionRequested,
    "intervention.resolved": InterventionResolved,
    "control.transferred": ControlTransferred,
    "human.action": HumanAction,
    "artifact.written": ArtifactWritten,
    "error": ErrorData,
    "run.finished": RunFinished,
}


# --------------------------------------------------------------------------- io


def load_yaml(path: str | Path, model: type[Model]):
    with open(path, encoding="utf-8") as f:
        return model.model_validate(yaml.safe_load(f))


def dump_yaml(obj: BaseModel, path: str | Path) -> None:
    data = obj.model_dump(mode="json", exclude_none=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


Condition.model_rebuild()
Expect.model_rebuild()
