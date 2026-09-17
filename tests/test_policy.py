from __future__ import annotations

import pytest
from conftest import ROOT

from automation.handoff import ControlState, Controller
from automation.logs import read_events
from automation.policy import GuardedSurface, NotInControl, Policy, PolicyViolation, Redactor
from automation.logs import RunLog

POLICY = Policy(
    allowed_origins=["http://127.0.0.1:5001", "https://bank.example:*"],
    allowed_paths=["/", "/search", "/member", "/open/*"],
    allowed_actions=["navigate", "click", "fill", "select", "extract", "wait"],
    irreversible_patterns=[r"\bconfirm\b", r"\btransfer\b", r"\bsubmit payment\b"],
)


@pytest.mark.parametrize("url, rule", [
    ("http://127.0.0.1:5001/search", None),
    ("http://127.0.0.1:5001/member?mid=1#top", None),
    ("http://127.0.0.1:5001/open/review", None),
    ("http://127.0.0.1:5001", None),
    ("https://bank.example:8443/search", None),
    ("https://bank.example/search", None),
    ("http://127.0.0.1:5002/search", "origin_not_allowed"),
    ("http://127.0.0.1.evil.com:5001/search", "origin_not_allowed"),
    ("http://evil.com/127.0.0.1:5001/search", "origin_not_allowed"),
    ("http://localhost:5001/search", "origin_not_allowed"),
    ("https://127.0.0.1:5001/search", "origin_not_allowed"),
    ("http://bank.example/search", "origin_not_allowed"),
    ("http://user:pw@127.0.0.1:5001/search", "origin_not_allowed"),
    ("javascript:alert(1)", "scheme_not_allowed"),
    ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
    ("data:text/html,hi", "scheme_not_allowed"),
    ("about:blank", "scheme_not_allowed"),
    ("", "scheme_not_allowed"),
    ("http://127.0.0.1:5001/admin", "path_not_allowed"),
    ("http://127.0.0.1:5001/open/../admin", "path_not_allowed"),
    ("http://127.0.0.1:5001/open", "path_not_allowed"),
    ("http://127.0.0.1:5001/SEARCH", "path_not_allowed"),
])
def test_url_allowlist(url, rule):
    assert POLICY.url_violation(url) == rule


def test_demo_policy_loads_and_blocks_sign_off():
    policy = Policy.load(ROOT / "demo" / "policy.yaml")
    assert policy.url_violation("http://localhost:5001/member?mid=1") is None
    assert policy.url_violation("http://localhost:5001/logout") == "path_not_allowed"


def test_bad_origin_spec_is_an_error():
    with pytest.raises(ValueError, match="bad origin spec"):
        Policy(allowed_origins=["127.0.0.1"], allowed_paths=["/"], allowed_actions=["click"]).url_violation(
            "http://127.0.0.1/")


@pytest.mark.parametrize("action, element, risk", [
    ("click", {"name": "Confirm"}, "irreversible"),
    ("click", {"name": "confirm order"}, "irreversible"),
    ("click", {"text": "Transfer funds"}, "irreversible"),
    ("click", {"name": "Submit payment now"}, "irreversible"),
    ("click", {"name": "Search"}, "safe"),
    ("click", {"name": "Confirmation history"}, "safe"),
    ("click", {"name": "Transferable limits"}, "safe"),
    ("fill", {"name": "Confirm"}, "safe"),
    ("click", None, "safe"),
])
def test_risk_classification(action, element, risk):
    assert POLICY.classify(action, element) == risk


def test_check_decisions():
    url = "http://127.0.0.1:5001/open/review"
    assert POLICY.check("click", url=url, element={"name": "Continue"}).allowed
    blocked = POLICY.check("click", url=url, element={"name": "Confirm"})
    assert (blocked.allowed, blocked.rule, blocked.risk) == (False, "irreversible_requires_approval", "irreversible")
    approved = POLICY.check("click", url=url, element={"name": "Confirm"}, approved=True)
    assert (approved.allowed, approved.risk) == (True, "irreversible")
    declared = POLICY.check("click", url=url, element={"name": "Next"}, declared_risk="irreversible")
    assert declared.rule == "irreversible_requires_approval"
    off_site = POLICY.check("click", url="http://evil.com/", element={"name": "Confirm"}, approved=True)
    assert off_site.rule == "origin_not_allowed"
    link = POLICY.check("click", url=url, element={"name": "Admin", "href": "http://127.0.0.1:5001/admin"})
    assert link.rule == "link_path_not_allowed"
    assert POLICY.check("wait", url=None).allowed


def test_disallowed_action_type():
    read_only = POLICY.model_copy(update={"allowed_actions": ["navigate", "extract"]})
    assert read_only.check("fill", url="http://127.0.0.1:5001/search").rule == "action_not_allowed"
    assert read_only.check("extract", url="http://127.0.0.1:5001/search").allowed


# --------------------------------------------------------------------------- the chokepoint


class FakeSurface:
    url = "http://127.0.0.1:5001/search"

    def __init__(self):
        self.performed = []

    def perform(self, action, **kw):
        self.performed.append((action, kw.get("value")))
        return "text"


class FakeResolved:
    frame_url = "http://127.0.0.1:5001/search"

    def __init__(self, name):
        self.element = {"name": name, "role": "button", "tag": "input"}

    def describe(self):
        return f'button "{self.element["name"]}"'


@pytest.fixture
def guarded(tmp_path):
    log = RunLog(tmp_path, Redactor())
    surface = FakeSurface()
    controller = Controller(log)
    return GuardedSurface(surface, POLICY, controller, log, actor="replay"), surface, controller, log


def test_allowed_action_logs_decision_then_action(guarded):
    g, surface, _, log = guarded
    out, decision = g.perform("s1", "click", resolved=FakeResolved("Search"))
    assert out == "text" and decision.allowed
    log.close()
    events = read_events(log.dir)
    assert [e.type for e in events] == ["policy.decision", "action.performed"]
    assert events[0].data["target"] == 'button "Search"' and events[0].step_id == "s1"
    assert surface.performed == [("click", None)]


def test_blocked_action_never_reaches_surface(guarded):
    g, surface, _, log = guarded
    with pytest.raises(PolicyViolation) as exc:
        g.perform("s7", "click", resolved=FakeResolved("Confirm"))
    assert exc.value.decision.rule == "irreversible_requires_approval"
    assert surface.performed == []
    log.close()
    assert [e.type for e in read_events(log.dir)] == ["policy.decision"]


def test_automation_cannot_act_while_human_has_control(guarded):
    g, surface, controller, log = guarded
    controller.transfer(ControlState.AWAITING_HUMAN, by="automation")
    with pytest.raises(NotInControl):
        g.perform("s1", "click", resolved=FakeResolved("Search"))
    controller.transfer(ControlState.HUMAN, by="operator:test")
    with pytest.raises(NotInControl):
        g.perform("s1", "fill", resolved=FakeResolved("Member ID"), value="1")
    assert surface.performed == []
    log.close()
    decisions = [e.data for e in read_events(log.dir) if e.type == "policy.decision"]
    assert [d["rule"] for d in decisions] == ["not_in_control:awaiting_human", "not_in_control:human"]
