"""Human-in-the-loop: who controls the live session, and how control moves.

Control model
    AUTOMATION ──escalate──▶ AWAITING_HUMAN ──take──▶ HUMAN ──resume──▶ AUTOMATION
                                   │                    │
                                   └──────abort─────────┴──▶ ABORTED
Only the automation thread changes state. The operator surface (HTTP thread) *requests*
transitions; the automation loop applies them between Playwright calls. That keeps every
log event ordered and lets pending human-action events drain before control comes back.
"""
from __future__ import annotations

import html
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable, Literal

from .schema import Intervention, Target

if TYPE_CHECKING:
    from .logs import RunLog
    from .policy import Redactor
    from .surface import Surface


class ControlState(StrEnum):
    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    ABORTED = "aborted"


TRANSITIONS: dict[ControlState, set[ControlState]] = {
    ControlState.AUTOMATION: {ControlState.AWAITING_HUMAN},
    ControlState.AWAITING_HUMAN: {ControlState.HUMAN, ControlState.ABORTED},
    ControlState.HUMAN: {ControlState.AUTOMATION, ControlState.ABORTED},
    ControlState.ABORTED: set(),
}


class InvalidTransition(RuntimeError):
    pass


class Controller:
    """Single source of truth for who owns the session. Every change is logged."""

    def __init__(self, log: RunLog):
        self.log = log
        self._state = ControlState.AUTOMATION
        self._lock = threading.Lock()

    @property
    def state(self) -> ControlState:
        return self._state

    def transfer(self, to: ControlState, by: str) -> None:
        with self._lock:
            if to not in TRANSITIONS[self._state]:
                raise InvalidTransition(f"{self._state} -> {to} is not allowed")
            frm, self._state = self._state, to
            actor = "operator" if by.startswith("operator") else "system"
            self.log.emit("control.transferred", {"from_state": frm, "to_state": to, "by": by}, actor=actor)


Resolution = Literal["resumed", "aborted", "timeout"]
DRAIN_MS = 400  # ponytail: fixed wait for in-flight human events; a JS-side ack would make it exact


class Handoff:
    """Escalation for one run: raises interventions, serves the operator page, records the human."""

    def __init__(
        self,
        log: RunLog,
        surface: Surface,
        redactor: Redactor,
        *,
        subject: str,
        mask: list[Target] | None = None,
        port: int = 8765,
        timeout_s: float = 900,
        on_human_control: Callable[[Surface, Handoff], None] | None = None,
    ):
        self.log, self.surface, self.redactor = log, surface, redactor
        self.subject, self.mask, self.timeout_s = subject, mask or [], timeout_s
        self.on_human_control = on_human_control
        self.controller = Controller(log)
        self.current: Intervention | None = None
        self.resolved_by: str | None = None
        self.human_actions: list[dict[str, Any]] = []
        self._pending: tuple[str, str] | None = None
        self._req_lock = threading.Lock()
        surface.on_human_event(self._on_human_event)
        self.server = OperatorServer(self, port)

    @property
    def operator_url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}/"

    def close(self) -> None:
        self.server.close()

    # -- operator side (HTTP thread) --------------------------------------------------
    def request(self, action: str, by: str) -> tuple[int, str]:
        allowed = {
            "take": {ControlState.AWAITING_HUMAN},
            "resume": {ControlState.HUMAN},
            "abort": {ControlState.AWAITING_HUMAN, ControlState.HUMAN},
        }
        if action not in allowed:
            return 404, f"unknown action {action}"
        with self._req_lock:
            state = self.controller.state
            if state not in allowed[action] or self._pending is not None:
                return 409, f"cannot {action} while {state}" + (" (request pending)" if self._pending else "")
            self._pending = (action, by)
        return 202, f"{action} requested"

    # -- automation side --------------------------------------------------------------
    def escalate(self, *, step_id: str | None, reason: str, kind: Literal["stuck", "failure", "approval"]) -> Resolution:
        shot, rel = self.log.reserve("screenshots", step_id or "run", "png")
        try:
            self.surface.screenshot(shot, self.mask)
            self.log.emit("screenshot", {"path": rel, "reason": "intervention"}, actor="system", step_id=step_id)
        except Exception:  # a broken page must not block asking a human for help
            rel = None
        self.current = Intervention(
            id="int-" + secrets.token_hex(4),
            run_id=self.log.run_id,
            subject=self.subject,
            step_id=step_id,
            kind=kind,
            reason=reason,
            url=self.surface.url,
            screenshot=rel,
            requested_at=datetime.now(timezone.utc),
        )
        self.log.emit("intervention.requested", {"intervention": self.current.model_dump(mode="json")},
                      actor="system", step_id=step_id)
        self.log.write_text("intervention.json", self.current.model_dump_json(indent=2))
        print(f"[handoff] {kind}: {self.redactor.text(reason)}\n[handoff] operator page: {self.operator_url}",
              file=sys.stderr, flush=True)
        self.controller.transfer(ControlState.AWAITING_HUMAN, by="automation")

        started = len(self.human_actions)
        deadline = time.monotonic() + self.timeout_s
        hook_ran = False
        resolved_by = "timeout"
        while self.controller.state not in (ControlState.AUTOMATION, ControlState.ABORTED):
            pending = self._take_pending()
            if pending:
                action, by = pending
                if action == "take":
                    self.controller.transfer(ControlState.HUMAN, by=by)
                elif action == "resume":
                    self.surface.pump(DRAIN_MS)
                    self.controller.transfer(ControlState.AUTOMATION, by=by)
                    resolved_by = by
                else:
                    self.controller.transfer(ControlState.ABORTED, by=by)
                    resolved_by = by
                continue
            if self.controller.state is ControlState.HUMAN and self.on_human_control and not hook_ran:
                hook_ran = True
                self.on_human_control(self.surface, self)
                continue
            if time.monotonic() > deadline:
                self.controller.transfer(ControlState.ABORTED, by="timeout")
                break
            self.surface.pump(100)

        if self.controller.state is ControlState.AUTOMATION:
            resolution: Resolution = "resumed"
        else:
            resolution = "timeout" if resolved_by == "timeout" else "aborted"
        self.resolved_by = resolved_by
        self.log.emit("intervention.resolved", {
            "intervention_id": self.current.id,
            "resolution": resolution,
            "by": resolved_by,
            "human_action_count": len(self.human_actions) - started,
        }, actor="operator" if resolution != "timeout" else "system", step_id=step_id)
        return resolution

    def actions_since(self, index: int) -> list[dict[str, Any]]:
        return self.human_actions[index:]

    def _take_pending(self) -> tuple[str, str] | None:
        with self._req_lock:
            pending, self._pending = self._pending, None
            return pending

    def _on_human_event(self, frame: str, payload: dict[str, Any]) -> None:
        if self.controller.state is not ControlState.HUMAN:
            return  # automation's own clicks fire the same DOM events
        element = payload.get("element")
        value = payload.get("value")
        if element and element.get("type") == "password":
            value = "[SECRET]" if value is not None else None
        data = {
            "kind": payload["kind"],
            "frame": frame,
            "element": element,
            "value": value,
            "url": payload.get("url"),
        }
        self.log.emit("human.action", data, actor="human", step_id=self.current.step_id if self.current else None)
        self.human_actions.append(data)


class OperatorServer:
    """Mock operator console: one page + a tiny JSON API on localhost. No auth (local demo only)."""

    def __init__(self, handoff: Handoff, port: int):
        hand = handoff

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep stderr for the handoff banner
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/intervention":
                    cur = hand.current
                    body = {"state": hand.controller.state, "intervention": cur.model_dump(mode="json") if cur else None}
                    return self._send(200, json.dumps(body).encode(), "application/json")
                if self.path == "/screenshot" and hand.current and hand.current.screenshot:
                    return self._send(200, (hand.log.dir / hand.current.screenshot).read_bytes(), "image/png")
                if self.path == "/":
                    return self._send(200, _operator_page(hand).encode(), "text/html; charset=utf-8")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                action = self.path.removeprefix("/api/")
                operator = self.headers.get("X-Operator", "operator")
                code, msg = hand.request(action, by=f"operator:{operator}")
                if "text/html" in (self.headers.get("Accept") or ""):
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                self._send(code, json.dumps({"message": msg}).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _operator_page(hand: Handoff) -> str:
    cur, state = hand.current, hand.controller.state
    if cur is None:
        body = f"<p>No open intervention. Control: <b>{state}</b></p>"
    else:
        reason = html.escape(hand.redactor.text(cur.reason))
        shot = "<img src='/screenshot' style='max-width:100%;border:1px solid #999'>" if cur.screenshot else ""
        buttons = "".join(
            f"<form method='post' action='/api/{a}' style='display:inline'><button>{label}</button></form> "
            for a, label in (("take", "Take control"), ("resume", "Resume automation"), ("abort", "Abort run"))
        )
        body = (
            f"<h2>Intervention {cur.id} ({cur.kind})</h2>"
            f"<p><b>Subject:</b> {html.escape(cur.subject)}<br><b>Step:</b> {cur.step_id}<br>"
            f"<b>Why:</b> {reason}<br><b>Control:</b> {state}</p>"
            "<p>Take control, operate the automation's browser window directly, then resume.</p>"
            f"{buttons}<br><br>{shot}"
        )
    return f"<html><head><title>Operator</title><meta http-equiv='refresh' content='3'></head><body style='font-family:sans-serif'>{body}</body></html>"
