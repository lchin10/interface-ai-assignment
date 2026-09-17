"""RunLog: the ordered, typed, redacted record of one run.

One writer per run. Every component emits through RunLog.emit, which (under one lock)
redacts, validates the payload against its event type, assigns a gap-free seq and flushes
the line. check_invariants proves the file tells a coherent story.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .handoff import TRANSITIONS, ControlState
from .schema import EVENT_DATA, Event

if TYPE_CHECKING:
    from .policy import Redactor

FILE_KEYS = ("screenshot", "path")


class LogIntegrityError(RuntimeError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class RunLog:
    def __init__(self, root: str | Path, redactor: Redactor, run_id: str | None = None):
        self.run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
        self.redactor = redactor
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self._file = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0
        self._files = 0
        self._closed = False

    def emit(self, type: str, data: BaseModel | dict[str, Any], *, actor: str, step_id: str | None = None) -> Event:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        payload = EVENT_DATA[type].model_validate(self.redactor.obj(payload)).model_dump(mode="json")
        with self._lock:
            if self._closed:
                raise RuntimeError(f"log {self.run_id} is closed; cannot emit {type}")
            self._seq += 1
            event = Event(
                seq=self._seq,
                ts=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                mono_ns=time.monotonic_ns(),
                run_id=self.run_id,
                actor=actor,
                type=type,
                step_id=step_id,
                data=payload,
            )
            self._file.write(event.model_dump_json() + "\n")
            self._file.flush()
            return event

    def reserve(self, folder: str, label: str, ext: str) -> tuple[Path, str]:
        """A fresh file path inside the run dir: (absolute, relative)."""
        with self._lock:
            self._files += 1
            rel = f"{folder}/{self._files:04d}-{label.replace('/', '_')}.{ext}"
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path, rel

    def write_text(self, rel: str, text: str) -> str:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.text(text), encoding="utf-8")
        return rel

    def finish(self, result: BaseModel, duration_ms: int) -> dict[str, Any]:
        """Emit run.finished, write result.json (both redacted, identical), close, verify."""
        redacted = self.redactor.obj(result.model_dump(mode="json"))
        self.emit("run.finished", {"status": redacted["status"], "result": redacted, "duration_ms": duration_ms},
                  actor="system")
        (self.dir / "result.json").write_text(json.dumps(redacted, indent=2), encoding="utf-8")
        self.close()
        errors = check_invariants(read_events(self.dir), self.dir)
        if errors:
            raise LogIntegrityError(errors)
        return redacted

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._file.close()


def read_events(run_dir: str | Path) -> list[Event]:
    lines = Path(run_dir, "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [Event.model_validate_json(line) for line in lines if line.strip()]


def check_invariants(events: list[Event], run_dir: str | Path) -> list[str]:
    run_dir = Path(run_dir)
    errors: list[str] = []
    if not events:
        return ["log is empty"]

    for i, e in enumerate(events, start=1):
        if e.seq != i:
            errors.append(f"seq gap: expected {i}, got {e.seq}")
            break
    for prev, cur in zip(events, events[1:]):
        if cur.mono_ns < prev.mono_ns or cur.ts < prev.ts:
            errors.append(f"time went backwards at seq {cur.seq}")
    for e in events:
        try:
            EVENT_DATA[e.type].model_validate(e.data)
        except KeyError:
            errors.append(f"seq {e.seq}: unknown event type {e.type}")
        except ValueError as exc:
            errors.append(f"seq {e.seq}: invalid {e.type} data: {exc}")
    if len({e.run_id for e in events}) != 1:
        errors.append("events from more than one run")

    if events[0].type != "run.started":
        errors.append("first event is not run.started")
    if events[-1].type != "run.finished":
        errors.append("last event is not run.finished")
    for t in ("run.started", "run.finished"):
        if sum(e.type == t for e in events) != 1:
            errors.append(f"expected exactly one {t}")

    open_step: str | None = None
    pending_turn: int | None = None
    state = ControlState.AUTOMATION
    for e in events:
        if e.type == "step.started":
            if open_step is not None:
                errors.append(f"seq {e.seq}: step {e.step_id} started while {open_step} still open")
            open_step = e.step_id
        elif e.type == "step.finished":
            if open_step != e.step_id:
                errors.append(f"seq {e.seq}: step {e.step_id} finished but open step is {open_step}")
            open_step = None
        elif e.type == "llm.request":
            if pending_turn is not None:
                errors.append(f"seq {e.seq}: llm.request before response to turn {pending_turn}")
            pending_turn = e.data["turn"]
        elif e.type == "llm.response":
            if pending_turn != e.data["turn"]:
                errors.append(f"seq {e.seq}: llm.response for turn {e.data['turn']} without matching request")
            pending_turn = None
        elif e.type == "control.transferred":
            frm, to = ControlState(e.data["from_state"]), ControlState(e.data["to_state"])
            if frm is not state or to not in TRANSITIONS[frm]:
                errors.append(f"seq {e.seq}: illegal control transfer {frm}->{to} (state was {state})")
            state = to
        elif e.type == "human.action" and state is not ControlState.HUMAN:
            errors.append(f"seq {e.seq}: human.action while control is {state}")
        elif e.type == "run.finished" and open_step is not None:
            errors.append(f"run finished with step {open_step} still open")
        for key in FILE_KEYS:
            rel = e.data.get(key)
            if isinstance(rel, str) and e.type != "artifact.written" and not (run_dir / rel).exists():
                errors.append(f"seq {e.seq}: referenced file {rel} does not exist")
        if e.type == "artifact.written" and not (run_dir / e.data["path"]).exists():
            errors.append(f"seq {e.seq}: artifact copy {e.data['path']} does not exist")
        if e.type == "run.finished":
            for rel in e.data["result"].get("log_files", []):
                if not (run_dir / rel).exists():
                    errors.append(f"result references missing file {rel}")
    if pending_turn is not None:
        errors.append(f"llm turn {pending_turn} has no response")

    result_path = run_dir / "result.json"
    if not result_path.exists():
        errors.append("result.json missing")
    elif events[-1].type == "run.finished":
        if json.loads(result_path.read_text(encoding="utf-8")) != events[-1].data["result"]:
            errors.append("result.json differs from run.finished")
    return errors
