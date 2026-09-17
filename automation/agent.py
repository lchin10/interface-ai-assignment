"""Discovery: a model drives the live surface once, and the recorder turns that run into an artifact.

The model only ever sees an element index (plus a masked screenshot) and acts through refs.
Each successful action is immediately re-expressed as a multi-strategy Target that is verified
against the live page, so what gets recorded is exactly what replay will use. Sign-on runs
deterministically from the app profile: credentials never reach the model.
"""
from __future__ import annotations

import base64
import inspect
import os
import re
import shutil
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from pydantic import ValidationError

from .logs import RunLog
from .policy import NotInControl, Policy, PolicyViolation, Redactor
from .replay import Executor, HandoffFactory, SurfaceFactory, _Stop, parse_output, policy_sha256, replay
from .schema import (
    AppProfile,
    AppRef,
    Artifact,
    CapabilityMeta,
    Condition,
    DiscoveryResult,
    Expect,
    InputSpec,
    Outcome,
    OutputSpec,
    Provenance,
    Step,
    Target,
    dump_yaml,
    load_yaml,
    sha256_file,
)
from .surface import Element, Observation, Surface, TargetError

DEFAULT_MODEL = "claude-opus-5"
MAX_TOOL_ERRORS = 3
NO_PROGRESS_LIMIT = 3
EXPECT_WAIT_MS = 8_000
SENSITIVITIES = ("public", "pii", "financial", "secret")
INPUT_TYPES = ("string", "integer", "decimal")

CreateMessage = Callable[..., Any]

SYSTEM_PROMPT = """\
You operate a legacy back-office banking application on behalf of an automation platform. \
This run is recorded and turned into a deterministic capability that will later be replayed \
without you, so work like a careful, conventional human operator.

Each turn you get the current screen: an indexed list of elements ([eN] role "name" ...) and a \
screenshot in which sensitive fields are blacked out. Act with exactly one tool call per turn, \
referring to elements by ref from the latest list.

- When typing a value that is one of the declared inputs, call fill/select with `input` set to the \
input name rather than typing the literal value.
- Take the most direct path through the application's own menus and forms. Don't explore unrelated screens.
- For clicks and navigation, set expect_text to short text that becomes visible only once the action \
has worked (typically the next page's heading). It must not already be on screen.
- Read requested data with extract, choosing a snake_case output_name and the right type.
- If the screen shows a business message relevant to the goal (for example a not-found or validation \
message), record it with record_outcome.
- Irreversible actions (confirm, transfer, delete, ...) are reserved for humans: attempting one hands \
the session to an operator, who may do it for you.
- If you are stuck or unsure it is safe to continue, call escalate. Never work around a policy block.
- When the goal is achieved, call done with success_text that is visible now and proves it."""


class ToolError(Exception):
    pass


@dataclass
class Param:
    value: str
    type: str = "string"
    sensitivity: str = "public"


def parse_param(text: str) -> tuple[str, Param]:
    """name=value[:sensitivity[:type]]"""
    name, sep, rest = text.partition("=")
    if not sep or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"bad --param {text!r}; expected name=value[:sensitivity[:type]]")
    parts = rest.split(":")
    if len(parts) >= 3 and parts[-2] in SENSITIVITIES and parts[-1] in INPUT_TYPES:
        return name, Param(":".join(parts[:-2]), parts[-1], parts[-2])
    if len(parts) >= 2 and parts[-1] in SENSITIVITIES:
        return name, Param(":".join(parts[:-1]), "string", parts[-1])
    return name, Param(rest)


FALLBACK_BETA = "server-side-fallback-2026-07-01"


def anthropic_create_message() -> CreateMessage:
    import anthropic

    client = anthropic.Anthropic()
    create = client.beta.messages.create
    # If the primary model declines, the API retries on a fallback model. Only newer SDKs accept it,
    # and it is a nicety, not a requirement: on an older SDK we simply send the request without it.
    extra = {}
    if "fallbacks" in inspect.signature(create).parameters:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    def send(**kwargs):
        return create(**extra, **kwargs)

    return send


def build_tools(params: dict[str, Param]) -> list[dict[str, Any]]:
    reasoning = {"type": "string", "description": "One sentence on why this is the right next action. "
                 "Recorded as the step's documented intent."}
    expect_text = {"type": "string", "description": "Short text that will be visible only once this action has "
                   "worked, e.g. the next page heading. Empty if nothing visible changes."}
    ref = {"type": "string", "description": "Element ref from the latest element list, e.g. e12"}
    value_props = {
        "input": {"type": "string", "enum": sorted(params) or [""], "description": "Name of the declared input to use"},
        "value": {"type": "string", "description": "Literal value, only when it is not a declared input"},
    }

    def tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"name": name, "description": description,
                "input_schema": {"type": "object", "properties": props, "required": required}}

    return [
        tool("click", "Click a link, button, checkbox or radio button.",
             {"ref": ref, "reasoning": reasoning, "expect_text": expect_text}, ["ref", "reasoning"]),
        tool("fill", "Type into a text field (replacing its content).",
             {"ref": ref, **value_props, "reasoning": reasoning}, ["ref", "reasoning"]),
        tool("select", "Choose an option in a dropdown, by option value or label.",
             {"ref": ref, **value_props, "reasoning": reasoning}, ["ref", "reasoning"]),
        tool("extract", "Read an element's text as a named output of the capability.",
             {"ref": ref, "output_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
              "type": {"type": "string", "enum": list(INPUT_TYPES)},
              "sensitivity": {"type": "string", "enum": list(SENSITIVITIES)},
              "reasoning": reasoning}, ["ref", "output_name", "type", "sensitivity", "reasoning"]),
        tool("navigate", "Open a path inside the application, e.g. /search.",
             {"path": {"type": "string"}, "reasoning": reasoning, "expect_text": expect_text}, ["path", "reasoning"]),
        tool("wait", "Wait for a slow page, 0.5-5 seconds.",
             {"seconds": {"type": "number"}, "reasoning": reasoning}, ["seconds", "reasoning"]),
        tool("record_outcome", "Record a business message visible now (e.g. not found) as a known outcome.",
             {"code": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
              "text": {"type": "string", "description": "Exact short text as shown on screen"},
              "reasoning": reasoning}, ["code", "text", "reasoning"]),
        tool("escalate", "Hand the live session to a human operator.",
             {"reason": {"type": "string"}}, ["reason"]),
        tool("done", "Finish: the goal is achieved.",
             {"success_text": {"type": "string", "description": "Text visible now that proves success"},
              "summary": {"type": "string"}}, ["success_text", "summary"]),
    ]


@dataclass
class Finish:
    status: str  # artifact_written | failure | needs_human | aborted
    code: str | None
    message: str
    success: Condition | None = None


class _Discovery:
    def __init__(self, *, goal: str, capability_id: str, params: dict[str, Param], app: AppProfile,
                 policy: Policy, log: RunLog, surface: Surface, redactor: Redactor, handoff,
                 create_message: CreateMessage, model: str, base_url: str, max_steps: int, timeout_s: float,
                 secrets: dict[str, str]):
        self.goal, self.capability_id, self.params, self.app, self.policy = goal, capability_id, params, app, policy
        self.log, self.surface, self.redactor, self.handoff = log, surface, redactor, handoff
        self.create_message, self.model, self.base_url = create_message, model, base_url
        self.max_steps, self.timeout_s = max_steps, timeout_s
        self.tools = build_tools(params)
        self.executor = Executor(
            log=log, surface=surface, redactor=redactor, policy=policy, app=app, actor="agent",
            context={"inputs": {k: p.value for k, p in params.items()}, "app": {"base_url": base_url},
                     "secrets": {n: secrets[n] for n in app.secrets}},
            capability_id=capability_id, capability_version="draft", handoff=handoff,
        )
        self.steps: list[Step] = []
        self.outputs: dict[str, OutputSpec] = {}
        self.outcomes: list[Outcome] = []
        self.obs: Observation | None = None
        self._masked_texts: set[str] = set()
        self.shot_b64 = ""
        self.shot_rel = ""
        self.turns = 0

    # -- loop -------------------------------------------------------------------------
    def run(self) -> Finish:
        try:
            self.executor.login()
        except _Stop as stop:
            return Finish("failure", getattr(stop.result, "code", "LOGIN_FAILED"), "sign-on failed")
        self._observe()
        inputs = "\n".join(f"- {k} ({p.type}, {p.sensitivity}) = {p.value}" for k, p in self.params.items()) or "- none"
        messages: list[dict[str, Any]] = [{"role": "user", "content": [
            {"type": "text", "text": f"Goal: {self.goal}\n\nDeclared inputs (fill with input=<name>):\n{inputs}"},
            *self._screen_blocks(),
        ]}]
        errors = no_progress = 0
        deadline = time.monotonic() + self.timeout_s
        for turn in range(1, self.max_steps + 1):
            self.turns = turn
            if time.monotonic() > deadline:
                return Finish("failure", "TIMEOUT", f"no result within {self.timeout_s:.0f}s")
            response = self._call(turn, messages)
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                return Finish("failure", "MODEL_REFUSAL", "the model declined to continue")
            tool = next((b for b in response.content if b.type == "tool_use"), None)
            if tool is None:
                errors += 1
                if errors >= MAX_TOOL_ERRORS:
                    return Finish("failure", "NO_TOOL_CALL", "the model stopped calling tools")
                messages.append({"role": "user", "content": [{"type": "text", "text": "Continue with exactly one tool call."}]})
                continue
            before = self.obs.hash
            is_error = False
            try:
                reply, finish = self._dispatch(tool.name, dict(tool.input or {}))
                errors = 0
            except ToolError as e:
                reply, finish, is_error = f"Error: {e}", None, True
                errors += 1
                self.log.emit("error", {"error_type": "ToolError", "message": str(e), "traceback": ""},
                              actor="agent")
            if finish is not None:
                return finish
            if errors >= MAX_TOOL_ERRORS:
                return Finish("failure", "REPEATED_TOOL_ERRORS", reply)
            self._observe()
            if tool.name in ("click", "navigate", "wait") and not is_error:
                no_progress = no_progress + 1 if self.obs.hash == before else 0
            if no_progress >= NO_PROGRESS_LIMIT:
                summary, finish = self._escalate("stuck", f"no visible progress after {no_progress} actions", None)
                if finish is not None:
                    return finish
                reply += "\n\n" + summary
                no_progress = 0
                self._observe()
            # the screenshot rides alongside the tool_result, not inside it: an error result must be
            # text-only, and this keeps one shape for both cases
            result_text, screenshot = self._screen_blocks()
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool.id, "is_error": is_error,
                 "content": [{"type": "text", "text": reply}, result_text]},
                screenshot,
            ]})
        return Finish("failure", "MAX_STEPS", f"goal not reached within {self.max_steps} turns")

    def _call(self, turn: int, messages: list[dict[str, Any]]):
        self.log.emit("llm.request", {"turn": turn, "model": self.model, "message_count": len(messages),
                                      "new_content": self._summarize(messages[-1]["content"])}, actor="agent")
        t0 = time.monotonic()
        try:
            response = self.create_message(
                model=self.model, max_tokens=16_000, system=SYSTEM_PROMPT, tools=self.tools,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True}, messages=messages,
            )
        except Exception as exc:
            self.log.emit("llm.response", {"turn": turn, "stop_reason": "api_error", "text": f"{type(exc).__name__}: {exc}",
                                           "tool": None, "tool_input": None, "usage": {},
                                           "latency_ms": int((time.monotonic() - t0) * 1000), "request_id": None},
                          actor="agent")
            raise
        tool = next((b for b in response.content if b.type == "tool_use"), None)
        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage or {})
        self.log.emit("llm.response", {
            "turn": turn, "stop_reason": response.stop_reason,
            "text": "\n".join(b.text for b in response.content if b.type == "text"),
            "tool": tool.name if tool else None, "tool_input": dict(tool.input) if tool else None,
            "usage": {k: v for k, v in usage.items() if isinstance(v, (int, str)) or v is None},
            "latency_ms": int((time.monotonic() - t0) * 1000), "request_id": getattr(response, "_request_id", None),
        }, actor="agent")
        return response

    # -- observation ------------------------------------------------------------------
    def _observe(self) -> None:
        self.surface.settle()  # the model must reason about a finished screen, not a loading one
        obs = self.surface.observe()
        hidden = self.surface.masked_refs(self.executor.mask)
        # a masked value also leaks through neighbouring cells' derived row/label keys
        self._masked_texts = {e.text for e in obs.elements if e.ref in hidden and e.text}
        for e in obs.elements:
            if e.ref in hidden or e.text in self._masked_texts:
                e.text = "[masked]"
            if e.row_header in self._masked_texts:
                e.row_header = "[masked]"
            if e.label in self._masked_texts:
                e.label = "[masked]"
        self.obs = obs
        path, rel = self.log.reserve("screenshots", "observation", "png")
        self.surface.screenshot(path, self.executor.mask)
        self.shot_b64, self.shot_rel = base64.b64encode(path.read_bytes()).decode(), rel
        self.log.emit("observation", {"url": obs.url, "frames": list(obs.frames), "element_count": len(obs.elements),
                                      "observation_hash": obs.hash, "screenshot": rel}, actor="agent")

    def _screen_blocks(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return (
            {"type": "text", "text": "Current screen:\n" + self.obs.to_prompt()},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": self.shot_b64}},
        )

    def _summarize(self, content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """What was sent, minus the raw screen: the log references the observation instead."""
        out = []
        for block in content:
            if block.get("type") == "tool_result":
                out.append({"type": "tool_result", "is_error": block.get("is_error", False),
                            "content": self._summarize(block["content"])})
            elif block.get("type") == "image":
                out.append({"type": "image", "screenshot": self.shot_rel})
            elif block.get("text", "").startswith("Current screen:"):
                out.append({"type": "observation", "observation_hash": self.obs.hash,
                            "element_count": len(self.obs.elements)})
            else:
                out.append({"type": "text", "text": block.get("text", "")})
        return out

    # -- tools ------------------------------------------------------------------------
    def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, Finish | None]:
        reasoning = str(args.get("reasoning") or "").strip()
        if name in ("click", "fill", "select"):
            return self._act(name, args, reasoning)
        if name == "extract":
            return self._extract(args, reasoning), None
        if name == "navigate":
            return self._navigate(args, reasoning)
        if name == "wait":
            seconds = min(max(float(args.get("seconds") or 1), 0.5), 5)
            self.surface.pump(int(seconds * 1000))
            return f"Waited {seconds}s.", None
        if name == "record_outcome":
            return self._record_outcome(args, reasoning), None
        if name == "escalate":
            summary, finish = self._escalate("stuck", str(args.get("reason") or "the agent asked for help"), None)
            return summary, finish
        if name == "done":
            return self._done(args)
        raise ToolError(f"unknown tool {name}")

    def _element(self, ref: Any) -> Element:
        el = self.obs.element(str(ref))
        if el is None:
            raise ToolError(f"unknown ref {ref!r}; use a ref from the latest element list")
        return el

    def _next_id(self) -> str:
        return f"s{len(self.steps) + 1}"

    def _sensitive_values(self) -> list[str]:
        """Input values and masked screen values: never in a locator, a checkpoint or an outcome."""
        return [p.value for p in self.params.values() if p.sensitivity != "public"] + sorted(self._masked_texts)

    def _templated(self, literal: str) -> str:
        for name, p in self.params.items():
            if literal == p.value:
                return f"{{{{inputs.{name}}}}}"
        return literal

    def _target(self, el: Element) -> Target:
        try:
            return self.surface.verified_target_for(el, avoid=self._sensitive_values())
        except TargetError as e:
            raise ToolError(str(e)) from None

    def _begin(self, step_id: str, action: str, reasoning: str) -> float:
        self.log.emit("step.started", {"intent": reasoning, "action": action, "attempt": 1}, actor="agent", step_id=step_id)
        return time.monotonic()

    def _end(self, step_id: str, t0: float, status: str, detail: str = "") -> None:
        self.log.emit("step.finished", {"status": status, "duration_ms": int((time.monotonic() - t0) * 1000),
                                        "detail": detail}, actor="agent", step_id=step_id)

    def _resolve(self, step_id: str, target: Target):
        resolved = self.surface.resolve(target, 3_000)
        self.log.emit("target.resolved", {"strategy_index": resolved.strategy_index, "strategy": resolved.strategy.by,
                                          "match_count": resolved.match_count, "fingerprint_ok": resolved.fingerprint_ok,
                                          "drift": resolved.drift}, actor="agent", step_id=step_id)
        return resolved

    def _act(self, kind: str, args: dict[str, Any], reasoning: str) -> tuple[str, Finish | None]:
        el = self._element(args.get("ref"))
        value = template = None
        if kind in ("fill", "select"):
            if el.role == "password":
                raise ToolError("credential fields are filled by the platform, never by the agent")
            if args.get("input"):
                name = args["input"]
                if name not in self.params:
                    raise ToolError(f"unknown input {name!r}; declared: {sorted(self.params)}")
                value, template = self.params[name].value, f"{{{{inputs.{name}}}}}"
            elif args.get("value") is not None:
                value = str(args["value"])
                template = self._templated(value)
            else:
                raise ToolError(f"{kind} needs `input` or `value`")
        expect_text = str(args.get("expect_text") or "").strip()
        already_visible = bool(expect_text) and self.surface.check(Condition(text_visible=expect_text))
        before = {(e.text or "").strip() for e in self.obs.elements}
        target = self._target(el)
        step_id = self._next_id()
        t0 = self._begin(step_id, kind, reasoning)
        blocked = None
        status, detail = "failed", ""
        try:
            resolved = self._resolve(step_id, target)
            try:
                _, decision = self.executor.guarded.perform(step_id, kind, resolved=resolved, value=value,
                                                            value_log=template)
            except PolicyViolation as v:
                blocked, status, detail = v.decision, "blocked", v.decision.rule
            else:
                expect, note = self._expectation(expect_text, already_visible)
                if expect is None and kind == "click":
                    expect, note = self._derive_expectation(target.frame, before, note)
                if template and any(r == "inputs" and self.params[n].sensitivity != "public"
                                    for r, n in re.findall(r"\{\{(inputs)\.(\w+)\}\}", template)):
                    self.executor.mask.append(target)
                self.steps.append(Step(id=step_id, intent=reasoning, action=kind, target=target, value=template,
                                       risk=decision.risk, expect=expect, recorded_from="agent"))
                status = "ok"
        except (TargetError, NotInControl) as e:
            detail = str(e)
            raise ToolError(str(e)) from None
        finally:
            self._end(step_id, t0, status, detail)
        if blocked is not None:
            if blocked.rule != "irreversible_requires_approval":
                raise ToolError(f"blocked by policy ({blocked.rule}); do not try to work around it")
            summary, finish = self._escalate(
                "approval", f'The agent wants to click the irreversible control "{el.name}": {reasoning}', step_id)
            return "That action is irreversible, so it was handed to a human operator.\n" + summary, finish
        return f"{kind} on {el.display()} succeeded.{note}", None

    def _expectation(self, text: str, already_visible: bool) -> tuple[Expect | None, str]:
        if not text:
            return None, ""
        if already_visible:
            return None, f' Note: "{text}" was already visible before the action, so it cannot serve as a checkpoint.'
        if any(v in text for v in self._sensitive_values()):
            return None, " Note: expect_text contains an input value; not recorded."
        ok, _ = self.executor.wait_condition(Condition(text_visible=text), EXPECT_WAIT_MS)
        if not ok:
            return None, f' Note: "{text}" did not appear within {EXPECT_WAIT_MS // 1000}s.'
        return Expect(text_visible=text, timeout_ms=15_000), ""

    def _derive_expectation(self, frame: list[str], before: set[str], note: str) -> tuple[Expect | None, str]:
        """No usable checkpoint from the model: record text that appeared *because of* this action.

        A step with no checkpoint is a step replay cannot verify, so the recorder proposes one itself.
        """
        self.surface.settle()
        for e in self.surface.observe().elements:
            text = (e.text or "").strip()
            if e.frame != frame or e.role not in ("cell", "text", "columnheader"):
                continue
            if text in before or not 3 <= len(text) <= 60 or text == "[masked]":
                continue
            if any(v in text for v in self._sensitive_values()):
                continue
            return Expect(text_visible=text, timeout_ms=15_000), note + f' Recorded checkpoint "{text}".'
        return None, note + " No checkpoint recorded: nothing new appeared."

    def _extract(self, args: dict[str, Any], reasoning: str) -> str:
        el = self._element(args.get("ref"))
        name = str(args.get("output_name") or "")
        type_ = args.get("type") or "string"
        sensitivity = args.get("sensitivity") or "pii"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ToolError("output_name must be snake_case")
        if name in self.outputs:
            raise ToolError(f"output {name} was already extracted")
        if type_ not in INPUT_TYPES or sensitivity not in SENSITIVITIES:
            raise ToolError("invalid type or sensitivity")
        target = self._target(el)
        step_id = self._next_id()
        t0 = self._begin(step_id, "extract", reasoning)
        status, detail = "failed", ""
        try:
            resolved = self._resolve(step_id, target)
            text, _ = self.executor.guarded.perform(step_id, "extract", resolved=resolved)
            try:
                value = parse_output(text or "", type_)
            except ValueError as e:
                detail = str(e)
                raise ToolError(f"{e}; pick a different element or type") from None
            if sensitivity != "public":
                self.redactor.add(text, name)
                self.redactor.add(value, name)
                self.executor.mask.append(target)
            self.log.emit("output.extracted", {"name": name, "type": type_, "sensitivity": sensitivity,
                                               "value": str(value)}, actor="agent", step_id=step_id)
            self.outputs[name] = OutputSpec(type=type_, sensitivity=sensitivity, from_step=step_id, description=reasoning)
            self.steps.append(Step(id=step_id, intent=reasoning, action="extract", target=target, output=name,
                                   recorded_from="agent"))
            status = "ok"
        except (TargetError, NotInControl, PolicyViolation) as e:
            detail = str(e)
            raise ToolError(str(e)) from None
        finally:
            self._end(step_id, t0, status, detail)
        return f"Extracted {name} = {value}"

    def _navigate(self, args: dict[str, Any], reasoning: str) -> tuple[str, Finish | None]:
        path = "/" + str(args.get("path") or "").lstrip("/")
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        expect_text = str(args.get("expect_text") or "").strip()
        already_visible = bool(expect_text) and self.surface.check(Condition(text_visible=expect_text))
        step_id = self._next_id()
        t0 = self._begin(step_id, "navigate", reasoning)
        status, detail = "failed", ""
        try:
            self.executor.guarded.perform(step_id, "navigate", url=url, value_log=None)
            expect, note = self._expectation(expect_text, already_visible)
            self.steps.append(Step(id=step_id, intent=reasoning, action="navigate", url="{{app.base_url}}" + path,
                                   expect=expect, recorded_from="agent"))
            status = "ok"
        except (PolicyViolation, NotInControl) as e:
            detail = str(e)
            raise ToolError(f"blocked: {e}") from None
        finally:
            self._end(step_id, t0, status, detail)
        return f"Navigated to {path}.{note}", None

    def _record_outcome(self, args: dict[str, Any], reasoning: str) -> str:
        code, text = str(args.get("code") or ""), str(args.get("text") or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code) or not text:
            raise ToolError("record_outcome needs an UPPER_SNAKE code and the visible text")
        if not self.surface.check(Condition(text_visible=text)):
            raise ToolError(f'"{text}" is not visible on the current screen')
        if any(v in text for v in self._sensitive_values()):
            raise ToolError("outcome text must not contain input values")
        scope = [self.steps[-1].id] if self.steps else None
        self.outcomes.append(Outcome(code=code, kind="business", when=Condition(text_visible=text),
                                     message=reasoning, steps=scope))
        return f"Recorded business outcome {code}."

    def _done(self, args: dict[str, Any]) -> tuple[str, Finish | None]:
        text = str(args.get("success_text") or "").strip()
        if not self.steps:
            raise ToolError("nothing has been recorded yet")
        if not text or not self.executor.wait_condition(Condition(text_visible=text), 5_000)[0]:
            on_screen = " | ".join(" ".join(t.split())[:120] for t in self.surface.visible_text().values() if t.strip())
            raise ToolError(f'success_text "{text}" is not visible. Use a short phrase copied from one '
                            f'element, not several joined together. On screen now: {on_screen}')
        if any(v in text for v in self._sensitive_values()):
            raise ToolError("success_text must not contain input values")
        return "Done.", Finish("artifact_written", None, str(args.get("summary") or ""), Condition(text_visible=text))

    # -- humans -----------------------------------------------------------------------
    def _escalate(self, kind: str, reason: str, step_id: str | None) -> tuple[str, Finish | None]:
        if self.handoff is None:
            return "", Finish("needs_human", "NEEDS_HUMAN", reason)
        mark = len(self.handoff.human_actions)
        resolution = self.handoff.escalate(step_id=step_id, reason=reason, kind=kind)
        if resolution == "aborted":
            return "", Finish("aborted", "ABORTED", f"operator aborted: {reason}")
        if resolution == "timeout":
            return "", Finish("needs_human", "NEEDS_HUMAN", f"no operator responded: {reason}")
        recorded = self._record_human(self.handoff.actions_since(mark))
        if not recorded:
            return "A human operator reviewed the session and handed control back without acting.", None
        return "A human operator took control and performed:\n" + "\n".join(f"- {r}" for r in recorded) \
            + "\nContinue from the current screen.", None

    def _record_human(self, actions: list[dict[str, Any]]) -> list[str]:
        """Turn captured operator actions into draft steps (flagged recorded_from=human for review)."""
        lines: list[str] = []
        obs = self.surface.observe()
        pending_clicks: list[Step] = []
        for a in actions:
            el = a.get("element")
            if a["kind"] == "navigate" or not el:
                continue
            frame = [] if a["frame"] == "top" else a["frame"].split("/")
            target = self.surface.target_for(el, frame, avoid=self._sensitive_values())
            name = el.get("name") or el.get("label") or el.get("text") or el["tag"]
            value = None
            if a["kind"] in ("fill", "select"):
                raw = a.get("value")
                value = "[SECRET]" if raw is None else self._templated(self.redactor.text(raw))
            risk = self.policy.classify(a["kind"], el)
            step = Step(id=self._next_id(), intent=f"(operator) {a['kind']} {name}", action=a["kind"], target=target,
                        value=value, risk="safe", recorded_from="human")
            self.steps.append(step)
            if a["kind"] == "click":
                pending_clicks.append(step)
            if risk == "irreversible":
                step.risk = "irreversible"
            lines.append(f"{a['kind']} {name}" + (f" = {value}" if value else ""))
        if pending_clicks:
            # the first text in the frame the operator last clicked in is that click's checkpoint (reviewable draft)
            frame = pending_clicks[-1].target.frame
            hidden = self.surface.masked_refs(self.executor.mask)
            heading = next((e.text for e in obs.elements if e.frame == frame and e.text and e.ref not in hidden
                            and e.role in ("cell", "text")
                            and not any(v in e.text for v in self._sensitive_values())), None)
            if heading:
                for step in pending_clicks:
                    if step is pending_clicks[-1] or step.risk == "irreversible":
                        step.expect = Expect(text_visible=heading, timeout_ms=15_000)
        return lines

    # -- artifact ---------------------------------------------------------------------
    def build_artifact(self, finish: Finish, version: str) -> Artifact:
        summary = self.goal
        for name, p in self.params.items():
            summary = summary.replace(p.value, "{" + name + "}")
        inputs = {
            name: InputSpec(type="decimal" if p.type == "decimal" else "integer" if p.type == "integer" else "string",
                            sensitivity=p.sensitivity, description=f"Supplied by the caller for each invocation")
            for name, p in self.params.items()
        }
        return Artifact(
            capability=CapabilityMeta(
                id=self.capability_id, version=version, summary=summary,
                app=AppRef(product=self.app.product, product_version=self.app.product_version),
                status="draft",
                provenance=Provenance(run_id=self.log.run_id, model=self.model, recorded_at=datetime.now(timezone.utc)),
            ),
            inputs=inputs,
            outputs=self.outputs,
            requires=["session.authenticated"] if self.app.login else [],
            steps=self.steps,
            outcomes=self.outcomes,
            success=finish.success,
        )


def _next_version(path: Path) -> str:
    if not path.exists():
        return "1.0.0"
    try:
        major, minor, _ = load_yaml(path, Artifact).capability.version.split(".")
        return f"{major}.{int(minor) + 1}.0"
    except (ValidationError, OSError, ValueError):
        return "1.0.0"


def discover(
    *,
    goal: str,
    capability_id: str,
    params: dict[str, Param],
    app: AppProfile,
    policy: Policy,
    logs_root: str | Path,
    artifacts_dir: str | Path,
    surface_factory: SurfaceFactory,
    create_message: CreateMessage,
    model: str = DEFAULT_MODEL,
    base_url: str | None = None,
    max_steps: int = 30,
    timeout_s: float = 900,
    handoff_factory: HandoffFactory | None = None,
    verify: bool = True,
    secrets: dict[str, str] | None = None,
) -> DiscoveryResult:
    t0 = time.monotonic()
    secrets = dict(os.environ if secrets is None else secrets)
    redactor = Redactor()
    for name in app.secrets:
        if secrets.get(name):
            redactor.add(secrets[name], "secret")
    for name, p in params.items():
        if p.sensitivity != "public":
            redactor.add(p.value, name)
    log = RunLog(logs_root, redactor)
    base_url = (base_url or app.base_url).rstrip("/")
    log.emit("run.started", {
        "mode": "discovery", "capability_id": capability_id, "goal": goal,
        "inputs": {k: p.value for k, p in params.items()}, "policy_sha256": policy_sha256(policy),
        "app_product": app.product, "base_url": base_url, "model": model,
    }, actor="agent")

    agent: _Discovery | None = None
    surface = handoff = None
    try:
        missing = [n for n in app.secrets if not secrets.get(n)]
        if missing:
            finish = Finish("failure", "MISSING_SECRET", f"environment variables not set: {missing}")
        else:
            surface = surface_factory()
            handoff = handoff_factory(log, surface, redactor) if handoff_factory else None
            agent = _Discovery(goal=goal, capability_id=capability_id, params=params, app=app, policy=policy, log=log,
                               surface=surface, redactor=redactor, handoff=handoff, create_message=create_message,
                               model=model, base_url=base_url, max_steps=max_steps, timeout_s=timeout_s, secrets=secrets)
            finish = agent.run()
    except Exception as exc:
        log.emit("error", {"error_type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
                 actor="system")
        finish = Finish("failure", "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
    finally:
        if handoff:
            handoff.close()
        if surface:
            surface.close()

    result = DiscoveryResult(status=finish.status, run_id=log.run_id, capability_id=capability_id, code=finish.code,
                             message=finish.message, steps_recorded=len(agent.steps) if agent else 0,
                             turns=agent.turns if agent else 0)
    if finish.status == "artifact_written":
        path = Path(artifacts_dir) / f"{capability_id}.yaml"
        try:
            artifact = agent.build_artifact(finish, _next_version(path))
        except ValidationError as e:
            result.status, result.code, result.message = "failure", "ARTIFACT_INVALID", str(e)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_yaml(artifact, path)
            shutil.copyfile(path, log.dir / "artifact.yaml")
            sha = sha256_file(path)
            log.emit("artifact.written", {"path": "artifact.yaml", "published_to": str(path), "capability_id": capability_id,
                                          "version": artifact.capability.version, "sha256": sha}, actor="system")
            result.artifact_path = str(path)
            if verify:
                check = replay(artifact, {k: p.value for k, p in params.items()}, app=app, policy=policy,
                               logs_root=logs_root, surface_factory=surface_factory, base_url=base_url,
                               secrets=secrets, artifact_sha256=sha)
                result.verification = check.model_dump(mode="json")
                if check.status != "success":
                    result.message = (result.message + " | verification replay did not succeed").strip(" |")
    result.duration_ms = int((time.monotonic() - t0) * 1000)
    log.finish(result, result.duration_ms)
    return result
