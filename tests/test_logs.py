"""The log is a first-class output: ordered, complete, typed, internally consistent, and clean."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from automation.logs import LogIntegrityError, RunLog, check_invariants, read_events
from automation.policy import Redactor
from automation.schema import Event, Success


def new_log(tmp_path) -> RunLog:
    return RunLog(tmp_path, Redactor())


def started(log: RunLog) -> None:
    log.emit("run.started", {"mode": "replay", "capability_id": "a.b", "inputs": {}, "policy_sha256": "x",
                             "app_product": "p", "base_url": "http://h"}, actor="replay")


def result(log: RunLog) -> Success:
    return Success(run_id=log.run_id, capability_id="a.b", capability_version="1.0.0", outputs={})


def step(log: RunLog, sid: str, status: str = "ok") -> None:
    log.emit("step.started", {"intent": "i", "action": "click"}, actor="replay", step_id=sid)
    log.emit("step.finished", {"status": status, "duration_ms": 1}, actor="replay", step_id=sid)


def test_envelope_order_and_finish(tmp_path):
    log = new_log(tmp_path)
    started(log)
    step(log, "s1")
    step(log, "s2")
    redacted = log.finish(result(log), 5)
    events = read_events(log.dir)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5, 6]
    assert [e.type for e in events] == ["run.started", "step.started", "step.finished", "step.started",
                                        "step.finished", "run.finished"]
    assert all(a.mono_ns <= b.mono_ns and a.ts <= b.ts for a, b in zip(events, events[1:]))
    assert {e.run_id for e in events} == {log.run_id}
    assert json.loads((log.dir / "result.json").read_text()) == events[-1].data["result"] == redacted


def test_every_line_is_a_complete_valid_event(tmp_path):
    log = new_log(tmp_path)
    started(log)
    log.finish(result(log), 1)
    for line in (log.dir / "events.jsonl").read_text().splitlines():
        Event.model_validate_json(line)


def test_concurrent_emits_keep_order_and_integrity(tmp_path):
    log = new_log(tmp_path)
    started(log)

    def worker(n: int) -> None:
        for i in range(200):
            log.emit("error", {"error_type": "T", "message": f"w{n}-{i}", "traceback": ""}, actor="system")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.finish(result(log), 1)
    events = read_events(log.dir)
    assert len(events) == 1602
    assert [e.seq for e in events] == list(range(1, 1603))
    assert all(a.mono_ns <= b.mono_ns for a, b in zip(events, events[1:]))
    per_worker = {}
    for e in events[1:-1]:
        n, i = e.data["message"][1:].split("-")
        per_worker.setdefault(n, []).append(int(i))
    assert all(v == list(range(200)) for v in per_worker.values()), "each writer's events stay in its own order"
    assert check_invariants(events, log.dir) == []


def test_invalid_payload_is_rejected_without_consuming_a_seq(tmp_path):
    log = new_log(tmp_path)
    started(log)
    with pytest.raises(ValidationError):
        log.emit("step.finished", {"status": "maybe", "duration_ms": 1}, actor="replay", step_id="s1")
    with pytest.raises(ValidationError):
        log.emit("step.started", {"intent": "i", "action": "click", "surprise": 1}, actor="replay", step_id="s1")
    with pytest.raises(KeyError):
        log.emit("made.up", {}, actor="system")
    step(log, "s1")
    log.close()
    assert [e.seq for e in read_events(log.dir)] == [1, 2, 3]


def test_emit_after_close_is_an_error(tmp_path):
    log = new_log(tmp_path)
    started(log)
    log.finish(result(log), 1)
    with pytest.raises(RuntimeError, match="closed"):
        log.emit("error", {"error_type": "T", "message": "late", "traceback": ""}, actor="system")


def test_finish_refuses_an_incoherent_log(tmp_path):
    log = new_log(tmp_path)
    started(log)
    log.emit("step.started", {"intent": "i", "action": "click"}, actor="replay", step_id="s1")
    with pytest.raises(LogIntegrityError, match="still open"):
        log.finish(result(log), 1)


def test_reserved_files_are_unique_and_inside_the_run(tmp_path):
    log = new_log(tmp_path)
    paths = {log.reserve("screenshots", "s1", "png")[1] for _ in range(5)}
    assert len(paths) == 5 and all(p.startswith("screenshots/") for p in paths)
    abs_path, rel = log.reserve("frames", "s1/main", "html")
    assert abs_path == log.dir / rel and abs_path.parent.is_dir() and "s1_main" in rel


# --------------------------------------------------------------------------- invariant checker


def write(run_dir: Path, specs: list[tuple[str, dict, str | None]], *, result_data=None) -> list[Event]:
    run_dir.mkdir(parents=True, exist_ok=True)
    events = [Event(seq=i, ts=f"2026-01-01T00:00:{i:02d}.000000+00:00", mono_ns=i, run_id="r", actor="system",
                    type=t, step_id=sid, data=d) for i, (t, d, sid) in enumerate(specs, start=1)]
    (run_dir / "result.json").write_text(json.dumps(result_data if result_data is not None else RESULT))
    return events


RESULT = {"status": "success", "run_id": "r", "capability_id": "a.b", "capability_version": "1", "duration_ms": 1,
          "warnings": [], "outputs": {}}
START = ("run.started", {"mode": "replay", "capability_id": "a.b", "inputs": {}, "policy_sha256": "x",
                         "app_product": "p", "base_url": "h"}, None)
END = ("run.finished", {"status": "success", "result": RESULT, "duration_ms": 1}, None)
S1 = ("step.started", {"intent": "i", "action": "click"}, "s1")
F1 = ("step.finished", {"status": "ok", "duration_ms": 1}, "s1")
S2 = ("step.started", {"intent": "i", "action": "click"}, "s2")
F2 = ("step.finished", {"status": "ok", "duration_ms": 1}, "s2")
REQ = ("llm.request", {"turn": 1, "model": "m", "message_count": 1, "new_content": []}, None)
RESP = ("llm.response", {"turn": 1, "stop_reason": "tool_use", "text": "", "tool": "click", "tool_input": {},
                         "usage": {}, "latency_ms": 1, "request_id": None}, None)


def ctl(frm, to):
    return ("control.transferred", {"from_state": frm, "to_state": to, "by": "x"}, None)


HUMAN = ("human.action", {"kind": "click", "frame": "main", "element": {}, "value": None, "url": None}, None)


def test_checker_accepts_a_coherent_story(tmp_path):
    specs = [START, S1, F1, REQ, RESP, ctl("automation", "awaiting_human"), ctl("awaiting_human", "human"), HUMAN,
             ctl("human", "automation"), S2, F2, END]
    assert check_invariants(write(tmp_path, specs), tmp_path) == []


@pytest.mark.parametrize("specs, message", [
    ([S1, F1, END], "first event is not run.started"),
    ([START, S1, F1], "last event is not run.finished"),
    ([START, S1, END], "still open"),
    ([START, S1, S2, F2, F1, END], "started while s1 still open"),
    ([START, F1, END], "finished but open step is None"),
    ([START, REQ, END], "turn 1 has no response"),
    ([START, REQ, REQ, RESP, END], "llm.request before response"),
    ([START, RESP, END], "without matching request"),
    ([START, ctl("automation", "human"), END], "illegal control transfer"),
    ([START, ctl("automation", "awaiting_human"), ctl("human", "automation"), END], "illegal control transfer"),
    ([START, HUMAN, END], "human.action while control is automation"),
    ([START, ("screenshot", {"path": "screenshots/missing.png", "reason": "after_step"}, "s1"), END],
     "referenced file screenshots/missing.png does not exist"),
    ([START, ("made.up", {}, None), END], "unknown event type"),
    ([START, ("step.finished", {"status": "weird", "duration_ms": 1}, "s1"), END], "invalid step.finished"),
    ([START, START, END], "expected exactly one run.started"),
])
def test_checker_catches_each_violation(tmp_path, specs, message):
    errors = check_invariants(write(tmp_path, specs), tmp_path)
    assert any(message in e for e in errors), errors


def test_checker_catches_seq_gaps_time_travel_and_mixed_runs(tmp_path):
    events = write(tmp_path, [START, S1, F1, END])
    events[2] = events[2].model_copy(update={"seq": 9})
    assert any("seq gap" in e for e in check_invariants(events, tmp_path))
    events = write(tmp_path, [START, S1, F1, END])
    events[2] = events[2].model_copy(update={"mono_ns": 0})
    assert any("time went backwards" in e for e in check_invariants(events, tmp_path))
    events = write(tmp_path, [START, S1, F1, END])
    events[1] = events[1].model_copy(update={"run_id": "other"})
    assert any("more than one run" in e for e in check_invariants(events, tmp_path))


def test_checker_compares_result_json(tmp_path):
    events = write(tmp_path, [START, END], result_data={**RESULT, "outputs": {"x": "tampered"}})
    assert "result.json differs from run.finished" in check_invariants(events, tmp_path)
    (tmp_path / "result.json").unlink()
    assert "result.json missing" in check_invariants(events, tmp_path)


# --------------------------------------------------------------------------- real runs


SENSITIVE = ["10001", "99999", "40300", "12,450.33", "12450.33", "Pw-test-7731", "teller-test", "123-45-6789"]


def test_real_runs_leave_no_sensitive_values_in_any_log_file(run_replay, logs_root):
    run_replay("lookup_balance.yaml", {"member_id": "10001"})
    run_replay("lookup_balance.yaml", {"member_id": "99999"})
    run_replay("lookup_balance.yaml", {"member_id": "40300"})
    run_replay("lookup_balance.yaml", {"member_id": "10001"}, "server_error")
    ok, _, _ = run_replay("open_sub_account.yaml",
                          {"member_id": "10001", "account_type": "savings", "initial_deposit": "250.00"}, approve=True)
    account = ok.outputs["new_account_number"]
    files = [p for p in logs_root.rglob("*") if p.is_file() and p.suffix != ".png"]
    assert {p.suffix for p in files} >= {".jsonl", ".json", ".html"}
    for path in files:
        # for events, scan the payloads: the envelope's own seq/ts/mono_ns are generated counters,
        # and a nanosecond clock reading can contain any digit string by chance
        if path.name == "events.jsonl":
            chunks = [f"{e.type} {e.step_id} {json.dumps(e.data)}" for e in read_events(path.parent)]
        else:
            chunks = [path.read_text(encoding="utf-8")]
        for chunk in chunks:
            for value in SENSITIVE + [account]:
                assert value not in chunk, f"{value!r} leaked into {path.relative_to(logs_root)}"
