"""Control transfer: the state machine, the operator API, and real takeovers of a live replay."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import key_events, load_artifact

from automation.handoff import ControlState, Controller, Handoff, InvalidTransition
from automation.logs import RunLog, read_events
from automation.policy import Redactor

A, W, H, X = ControlState.AUTOMATION, ControlState.AWAITING_HUMAN, ControlState.HUMAN, ControlState.ABORTED


class Operator:
    """Test stand-in for a person using the operator console."""

    def __init__(self, port: int, name: str = "alice"):
        self.base, self.name = f"http://127.0.0.1:{port}", name

    def get(self, path: str = "/api/intervention"):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            body = r.read()
            return json.loads(body) if path.startswith("/api") else body

    def post(self, action: str) -> int:
        req = urllib.request.Request(f"{self.base}/api/{action}", data=b"", method="POST",
                                     headers={"X-Operator": self.name})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def wait_for(self, state: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while self.get()["state"] != state:
            if time.monotonic() > deadline:
                raise TimeoutError(f"state never became {state}")
            time.sleep(0.05)


# --------------------------------------------------------------------------- state machine


def test_controller_full_cycle_is_logged(tmp_path):
    log = RunLog(tmp_path, Redactor())
    c = Controller(log)
    for to, by in ((W, "automation"), (H, "operator:alice"), (A, "operator:alice"), (W, "automation"), (X, "timeout")):
        c.transfer(to, by)
    log.close()
    events = read_events(log.dir)
    assert [(e.data["from_state"], e.data["to_state"], e.data["by"], e.actor) for e in events] == [
        ("automation", "awaiting_human", "automation", "system"),
        ("awaiting_human", "human", "operator:alice", "operator"),
        ("human", "automation", "operator:alice", "operator"),
        ("automation", "awaiting_human", "automation", "system"),
        ("awaiting_human", "aborted", "timeout", "system"),
    ]


@pytest.mark.parametrize("path", [[H], [A], [X], [W, A], [W, W], [W, H, W], [W, X, A], [W, X, H]])
def test_controller_rejects_illegal_transitions(tmp_path, path):
    c = Controller(RunLog(tmp_path, Redactor()))
    for to in path[:-1]:
        c.transfer(to, "t")
    before = c.state
    with pytest.raises(InvalidTransition):
        c.transfer(path[-1], "t")
    assert c.state is before


# --------------------------------------------------------------------------- operator API (fake surface)


class FakeSurface:
    url = "http://127.0.0.1:5001/open/review"

    def __init__(self):
        self.callback = None

    def on_human_event(self, callback):
        self.callback = callback

    def screenshot(self, path, mask):
        Path(path).write_bytes(b"\x89PNG-fake")

    def pump(self, ms):
        time.sleep(ms / 1000)


@pytest.fixture
def fake_handoff(tmp_path):
    made = []

    def make(timeout_s: float = 20, hook=None):
        log = RunLog(tmp_path / f"run{len(made)}", Redactor())
        surface = FakeSurface()
        h = Handoff(log, surface, Redactor(), subject="demo_bank.open_sub_account", port=0, timeout_s=timeout_s,
                    on_human_control=hook)
        made.append(h)
        return h, surface, log

    yield make
    for h in made:
        h.close()


def test_operator_api_enforces_order_and_records_the_human(fake_handoff):
    h, surface, log = fake_handoff()
    op = Operator(h.server.port)
    assert op.get() == {"state": "automation", "intervention": None}
    assert [op.post(a) for a in ("take", "resume", "abort", "explode")] == [409, 409, 409, 404]
    surface.callback("main", {"kind": "click", "element": {"tag": "a"}})  # not in human control: ignored
    seen = {}

    def operator():
        op.wait_for("awaiting_human")
        seen["intervention"] = op.get()["intervention"]
        seen["page"] = op.get("/")
        seen["screenshot"] = op.get("/screenshot")
        seen["early_resume"] = op.post("resume")
        seen["take"] = op.post("take")
        seen["second_take"] = op.post("take")
        op.wait_for("human")
        surface.callback("main", {"kind": "click", "element": {"tag": "input", "type": "submit", "name": "Confirm"}})
        surface.callback("main", {"kind": "fill", "element": {"tag": "input", "type": "password"}, "value": "hunter2"})
        seen["resume"] = op.post("resume")

    t = threading.Thread(target=operator)
    t.start()
    resolution = h.escalate(step_id="s7", reason="Irreversible step s7 needs approval", kind="approval")
    t.join()

    assert resolution == "resumed" and h.controller.state is A and h.resolved_by == "operator:alice"
    assert (seen["early_resume"], seen["take"], seen["second_take"], seen["resume"]) == (409, 202, 409, 202)
    assert seen["intervention"]["step_id"] == "s7" and seen["intervention"]["kind"] == "approval"
    assert seen["intervention"]["subject"] == "demo_bank.open_sub_account"
    assert seen["intervention"]["id"].encode() in seen["page"] and seen["screenshot"] == b"\x89PNG-fake"
    log.close()
    events = read_events(log.dir)
    assert [e.type for e in events] == ["screenshot", "intervention.requested", "control.transferred",
                                        "control.transferred", "human.action", "human.action",
                                        "control.transferred", "intervention.resolved"]
    assert [e.data["to_state"] for e in events if e.type == "control.transferred"] == ["awaiting_human", "human",
                                                                                      "automation"]
    humans = [e for e in events if e.type == "human.action"]
    assert humans[0].data["element"]["name"] == "Confirm" and humans[1].data["value"] == "[SECRET]"
    assert all(e.actor == "human" and e.step_id == "s7" for e in humans)
    assert events[-1].data == {"intervention_id": seen["intervention"]["id"], "resolution": "resumed",
                               "by": "operator:alice", "human_action_count": 2}
    assert json.loads((log.dir / "intervention.json").read_text())["id"] == seen["intervention"]["id"]


def test_operator_can_abort_before_taking_control(fake_handoff):
    h, _, log = fake_handoff()
    op = Operator(h.server.port, "bob")
    t = threading.Thread(target=lambda: (op.wait_for("awaiting_human"), op.post("abort")))
    t.start()
    assert h.escalate(step_id=None, reason="stuck", kind="stuck") == "aborted"
    t.join()
    assert h.controller.state is X and h.resolved_by == "operator:bob"
    assert op.post("take") == 409


def test_nobody_answers_times_out(fake_handoff):
    h, _, log = fake_handoff(timeout_s=0.3)
    assert h.escalate(step_id="s2", reason="stuck", kind="failure") == "timeout"
    assert h.controller.state is X
    log.close()
    last = read_events(log.dir)[-1]
    assert (last.type, last.data["resolution"], last.data["by"]) == ("intervention.resolved", "timeout", "timeout")


# --------------------------------------------------------------------------- live takeovers during replay


OPEN_INPUTS = {"member_id": "10001", "account_type": "savings", "initial_deposit": "125.00"}


def takeover(hook=None, timeout_s: float = 30, operator_actions=("take",)):
    """Handoff factory + a background operator that performs `operator_actions` when asked."""
    holder: list[Handoff] = []

    def factory(log, surface, redactor):
        h = Handoff(log, surface, redactor, subject="test", port=0, timeout_s=timeout_s, on_human_control=hook)
        holder.append(h)
        return h

    def operator():
        while not holder:
            time.sleep(0.05)
        op = Operator(holder[0].server.port)
        for action in operator_actions:
            op.wait_for("awaiting_human" if action == "take" else "human", timeout=60)
            op.post(action)

    threading.Thread(target=operator, daemon=True).start()
    return factory


def resume_hook(actions=None):
    def hook(surface, handoff):
        if actions:
            actions(surface.page.frame(name="main"))
        Operator(handoff.server.port).post("resume")

    return hook


def test_operator_approval_lets_automation_perform_irreversible_step(run_replay):
    result, events, _ = run_replay("open_sub_account.yaml", OPEN_INPUTS, handoff_factory=takeover(resume_hook()))
    assert result.status == "success"
    s7 = [k for k in key_events(events) if ":s7" in k or k.startswith(("control", "intervention"))]
    assert s7 == ["step.started:s7", "intervention.requested:s7", "control.transferred=awaiting_human",
                  "control.transferred=human", "control.transferred=automation", "intervention.resolved:s7",
                  "checkpoint.passed:s7", "step.finished:s7=ok"]
    decisions = [e.data for e in events if e.type == "policy.decision" and e.step_id == "s7"]
    assert [(d["allowed"], d["rule"]) for d in decisions] == [(False, "irreversible_requires_approval"),
                                                              (True, "allowed")]


def test_operator_performs_irreversible_step_themselves(run_replay):
    def confirm(main):
        main.click("input[value=Confirm]")
        main.wait_for_selector("text=Account opened successfully")

    result, events, _ = run_replay("open_sub_account.yaml", OPEN_INPUTS, handoff_factory=takeover(resume_hook(confirm)))
    assert result.status == "success"
    assert result.outputs["new_account_number"].isdigit()
    assert "step.finished:s7=completed_by_human" in key_events(events)
    assert not [e for e in events if e.type == "action.performed" and e.step_id == "s7"]
    types = [e.type for e in events]
    take = next(i for i, e in enumerate(events) if e.type == "control.transferred" and e.data["to_state"] == "human")
    back = next(i for i, e in enumerate(events) if e.type == "control.transferred" and e.data["to_state"] == "automation")
    humans = [e for e in events[take:back] if e.type == "human.action"]
    assert any(h.data["kind"] == "click" and h.data["element"]["name"] == "Confirm" for h in humans)
    assert "human.action" not in types[:take] + types[back:]


def test_operator_repairs_failed_step_and_replay_continues(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.steps[1].target.strategies[0].name = "Find"  # this deployment's button is not where we expect

    def click_search(main):
        main.click("input[type=submit]")
        main.wait_for_selector("text=Member Detail")

    result, events, _ = run_replay(artifact, {"member_id": "10001"}, handoff_factory=takeover(resume_hook(click_search)))
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    assert "step.finished:s2=completed_by_human" in key_events(events)
    requested = next(e for e in events if e.type == "intervention.requested")
    assert requested.data["intervention"]["kind"] == "failure"
    assert requested.data["intervention"]["reason"].startswith("TARGET_NOT_FOUND")


def test_operator_abort_ends_the_run(run_replay):
    result, events, _ = run_replay("open_sub_account.yaml", OPEN_INPUTS,
                                   handoff_factory=takeover(operator_actions=("take", "abort")))
    assert result.status == "aborted"
    assert (result.by, result.step_id) == ("operator:alice", "s7")
    assert key_events(events)[-2:] == ["step.finished:s7=aborted", "run.finished=aborted"]


def test_unanswered_escalation_returns_needs_human(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "server_error",
                                   handoff_factory=takeover(timeout_s=0.5, operator_actions=()))
    assert result.status == "needs_human"
    assert result.step_id == "s2" and result.intervention_id.startswith("int-")
    assert key_events(events)[-2:] == ["step.finished:s2=needs_human", "run.finished=needs_human"]
