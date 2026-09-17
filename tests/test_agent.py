"""Discovery with a scripted stand-in for the model: the loop, the recorder, stuck detection,
guardrails, the human handoff, and what is (not) sent to the model."""
from __future__ import annotations

import json
import re
import threading
import time
from types import SimpleNamespace

import pytest
from conftest import PASSWORD, SECRETS, USER, policy_for
from test_handoff import Operator

from automation.agent import FALLBACK_BETA, Param, anthropic_create_message, discover, parse_param
from automation.handoff import Handoff
from automation.logs import check_invariants, read_events
from automation.replay import replay
from automation.schema import Artifact, load_yaml


class FakeModel:
    """Plays back a script. Each entry sees the latest screen text and returns content blocks."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def __call__(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=lambda o: getattr(o, "__dict__", str(o)))))
        screen = latest_screen(kwargs["messages"])
        entry = self.script.pop(0) if self.script else (lambda s: text("I am out of ideas."))
        blocks = entry(screen)
        blocks = blocks if isinstance(blocks, list) else [blocks]
        stop = "refusal" if blocks and blocks[0] == "REFUSAL" else ("tool_use" if any(
            b.type == "tool_use" for b in blocks) else "end_turn")
        blocks = [] if stop == "refusal" else blocks
        return SimpleNamespace(content=blocks, stop_reason=stop, usage={"input_tokens": 100, "output_tokens": 20},
                               _request_id=f"req_{len(self.requests)}")


def latest_screen(messages) -> str:
    for msg in reversed(messages):
        content = msg["content"] if isinstance(msg["content"], list) else []
        for block in content:
            if not isinstance(block, dict):
                continue
            inner = block.get("content", []) if block.get("type") == "tool_result" else [block]
            for b in inner:
                if b.get("type") == "text" and b["text"].startswith("Current screen:"):
                    return b["text"]
    return ""


_ids = iter(range(10_000))


def call(name, **args):
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=args)


def text(t):
    return SimpleNamespace(type="text", text=t)


def ref(screen: str, *needles: str) -> str:
    for line in screen.splitlines():
        if all(n in line for n in needles):
            return re.match(r"\[(e\d+)\]", line).group(1)
    raise AssertionError(f"no element with {needles} in:\n{screen}")


LOOKUP_SCRIPT = [
    lambda s: call("fill", ref=ref(s, 'textbox(text) "Member ID"'), input="member_id",
                   reasoning="Search for the member by ID"),
    lambda s: [text("Submitting the search."), call("click", ref=ref(s, 'button "Search"'), expect_text="Member Detail",
                                                  reasoning="Run the member search")],
    lambda s: call("extract", ref=ref(s, 'row="Savings"', 'column="Balance"'), output_name="savings_balance",
                   type="decimal", sensitivity="financial", reasoning="Read the savings balance"),
    lambda s: call("done", success_text="Member Detail", summary="Savings balance read"),
]


@pytest.fixture
def run_discovery(mockbank, surface_factory, app_profile, logs_root, tmp_path):
    def run(script, *, goal="Look up member 10001 and read their savings balance", params=None, faults=(),
            capability="demo_bank.lookup_balance", handoff_factory=None, max_steps=30, verify=True):
        url = mockbank(*faults)
        model = FakeModel(script)
        result = discover(
            goal=goal, capability_id=capability, params=params or {"member_id": Param("10001", "string", "pii")},
            app=app_profile, policy=policy_for(url), logs_root=logs_root, artifacts_dir=tmp_path / "artifacts",
            surface_factory=surface_factory, create_message=model, base_url=url, max_steps=max_steps,
            handoff_factory=handoff_factory, verify=verify, secrets=SECRETS,
        )
        run_dir = logs_root / result.run_id
        events = read_events(run_dir)
        assert check_invariants(events, run_dir) == []
        return result, events, model, url

    return run


def test_parse_param():
    assert parse_param("member_id=10001:pii") == ("member_id", Param("10001", "string", "pii"))
    assert parse_param("initial_deposit=250.00:financial:decimal") == \
        ("initial_deposit", Param("250.00", "decimal", "financial"))
    assert parse_param("url=http://x:1") == ("url", Param("http://x:1"))
    assert parse_param("note=a:b:public") == ("note", Param("a:b", "string", "public"))
    with pytest.raises(ValueError):
        parse_param("NoEquals")


def test_fallbacks_are_sent_only_when_the_installed_sdk_supports_them(monkeypatch):
    import anthropic

    sent = {}

    def make_client(with_fallbacks: bool):
        def new_sdk(*, model, messages, fallbacks=None, betas=None, **kw):
            sent.update(model=model, fallbacks=fallbacks, betas=betas)

        def old_sdk(*, model, messages, **kw):
            sent.update(model=model, fallbacks=None, betas=None)

        create = new_sdk if with_fallbacks else old_sdk
        return lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))

    monkeypatch.setattr(anthropic, "Anthropic", make_client(True))
    anthropic_create_message()(model="claude-opus-5", messages=[])
    assert sent == {"model": "claude-opus-5", "fallbacks": "default", "betas": [FALLBACK_BETA]}

    monkeypatch.setattr(anthropic, "Anthropic", make_client(False))
    anthropic_create_message()(model="claude-opus-5", messages=[])
    assert sent == {"model": "claude-opus-5", "fallbacks": None, "betas": None}


def test_discovery_records_a_replayable_capability(run_discovery, app_profile, surface_factory, logs_root, mockbank):
    result, events, model, url = run_discovery(LOOKUP_SCRIPT)
    assert result.status == "artifact_written", result
    assert (result.steps_recorded, result.turns) == (3, 4)
    assert result.verification["status"] == "success"

    artifact = load_yaml(result.artifact_path, Artifact)
    assert artifact.capability.summary == "Look up member {member_id} and read their savings balance"
    assert (artifact.capability.version, artifact.capability.status) == ("1.0.0", "draft")
    assert artifact.capability.provenance.run_id == result.run_id
    assert artifact.inputs["member_id"].sensitivity == "pii"
    assert artifact.requires == ["session.authenticated"]
    s1, s2, s3 = artifact.steps
    assert (s1.action, s1.value, s1.intent) == ("fill", "{{inputs.member_id}}", "Search for the member by ID")
    assert [s.by for s in s1.target.strategies] == ["adjacent_label", "css"] and s1.target.frame == ["main"]
    assert (s2.action, s2.expect.text_visible, [s.by for s in s2.target.strategies]) == \
        ("click", "Member Detail", ["role", "css"])
    assert (s3.action, s3.output, s3.target.strategies[0].by) == ("extract", "savings_balance", "table_cell")
    assert artifact.outputs["savings_balance"].model_dump() == {
        "type": "decimal", "description": "Read the savings balance", "sensitivity": "financial", "from_step": "s3"}
    assert artifact.success.text_visible == "Member Detail"
    assert all(s.recorded_from == "agent" for s in artifact.steps)
    assert "10001" not in (logs_root / result.run_id / "artifact.yaml").read_text()

    other = mockbank()
    again = replay(artifact, {"member_id": "10002"}, app=app_profile, policy=policy_for(other), base_url=other,
                   logs_root=logs_root, surface_factory=surface_factory, secrets=SECRETS)
    assert again.status == "success" and again.outputs == {"savings_balance": "8000.00"}

    types = [e.type for e in events]
    assert types[0] == "run.started" and types[-1] == "run.finished"
    assert types.count("llm.request") == types.count("llm.response") == 4
    assert types.index("artifact.written") < types.index("run.finished")
    assert all(e.data["usage"] == {"input_tokens": 100, "output_tokens": 20} for e in events if e.type == "llm.response")
    first_response = next(e for e in events if e.type == "llm.response" and e.data["turn"] == 2)
    assert first_response.data["text"] == "Submitting the search." and first_response.data["tool"] == "click"


def test_model_requests_are_well_formed_and_never_carry_secrets(run_discovery):
    result, events, model, _ = run_discovery(LOOKUP_SCRIPT)
    assert result.status == "artifact_written"
    req = model.requests[0]
    assert req["model"] == "claude-opus-5" and req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert {t["name"] for t in req["tools"]} >= {"click", "fill", "extract", "escalate", "done"}
    assert req["messages"][0]["content"][0]["text"].startswith("Goal: Look up member 10001")
    assert model.requests[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    everything = json.dumps(model.requests)
    for leaked in (PASSWORD, USER, "123-45-6789", "Jane Q. Sample"):
        assert leaked not in everything
    assert "[masked]" in everything, "masked screen values must still be listed, without their text"
    logged = [e.data["new_content"] for e in events if e.type == "llm.request"]
    assert all(b["type"] != "image" or b["screenshot"].startswith("screenshots/") for c in logged for b in c)
    assert "Current screen" not in json.dumps(logged)


def test_useless_checkpoint_is_rejected_and_replaced(run_discovery):
    script = [
        LOOKUP_SCRIPT[0],
        # "Member Search" is on screen before the click, so it cannot prove the click worked
        lambda s: call("click", ref=ref(s, 'button "Search"'), expect_text="Member Search", reasoning="Search"),
        LOOKUP_SCRIPT[2],
        LOOKUP_SCRIPT[3],
    ]
    result, _, model, _ = run_discovery(script, verify=False)
    assert result.status == "artifact_written", result
    artifact = load_yaml(result.artifact_path, Artifact)
    assert artifact.steps[1].expect.text_visible == "Member Detail"
    reply = model.requests[2]["messages"][-1]["content"][0]["content"][0]["text"]
    assert "already visible before the action" in reply
    assert 'Recorded checkpoint "Member Detail"' in reply


def test_recorder_proposes_a_checkpoint_when_the_model_omits_one(run_discovery):
    script = [
        LOOKUP_SCRIPT[0],
        lambda s: call("click", ref=ref(s, 'button "Search"'), reasoning="Run the search"),  # no expect_text
        LOOKUP_SCRIPT[2],
        LOOKUP_SCRIPT[3],
    ]
    result, _, model, _ = run_discovery(script, verify=False)
    assert result.status == "artifact_written", result
    artifact = load_yaml(result.artifact_path, Artifact)
    # a step replay cannot verify is worthless, so the recorder records text the click itself produced
    assert artifact.steps[1].expect.text_visible == "Member Detail"
    reply = model.requests[2]["messages"][-1]["content"][0]["content"][0]["text"]
    assert 'Recorded checkpoint "Member Detail"' in reply


def test_business_outcome_can_be_recorded(run_discovery):
    script = [
        lambda s: call("fill", ref=ref(s, 'textbox(text) "Member ID"'), input="member_id", reasoning="Search"),
        lambda s: call("click", ref=ref(s, 'button "Search"'), reasoning="Run the search"),
        lambda s: call("record_outcome", code="NO_SUCH_MEMBER", text="No member found", reasoning="Unknown ID"),
        lambda s: call("done", success_text="No member found", summary="documented not-found"),
    ]
    result, _, _, _ = run_discovery(script, params={"member_id": Param("99999", "string", "pii")}, verify=False)
    artifact = load_yaml(result.artifact_path, Artifact)
    assert [(o.code, o.kind, o.when.text_visible, o.steps) for o in artifact.outcomes] == \
        [("NO_SUCH_MEMBER", "business", "No member found", ["s2"])]


def test_no_progress_is_detected_and_escalated(run_discovery):
    same = lambda s: call("wait", seconds=0.5, reasoning="Wait for something to change")
    result, events, _, _ = run_discovery([same, same, same, same])
    assert (result.status, result.code) == ("needs_human", "NEEDS_HUMAN")
    assert "no visible progress after 3 actions" in result.message


def test_turn_budget_is_enforced(run_discovery):
    wait = lambda s: call("wait", seconds=0.5, reasoning="Let the page settle")
    result, events, _, _ = run_discovery([wait] * 5, max_steps=2)
    assert (result.status, result.code, result.turns) == ("failure", "MAX_STEPS", 2)
    assert [e.type for e in events].count("llm.request") == 2


def test_repeated_bad_tool_calls_stop_the_run(run_discovery):
    bad = lambda s: call("click", ref="e999", reasoning="guess")
    result, events, model, _ = run_discovery([bad, bad, bad])
    assert (result.status, result.code) == ("failure", "REPEATED_TOOL_ERRORS")
    assert "unknown ref 'e999'" in result.message
    assert [e.data["message"] for e in events if e.type == "error"][0].startswith("unknown ref")
    message = model.requests[1]["messages"][-1]["content"]
    tool_result = message[0]
    assert tool_result["is_error"] is True
    # the API rejects a failed tool_result that carries anything but text
    assert {b["type"] for b in tool_result["content"]} == {"text"}
    assert [b["type"] for b in message] == ["tool_result", "image"]


def test_model_that_stops_calling_tools_and_refusals(run_discovery):
    chat = lambda s: text("Here is what I see.")
    result, _, _, _ = run_discovery([chat, chat, chat])
    assert (result.status, result.code) == ("failure", "NO_TOOL_CALL")
    result, _, _, _ = run_discovery([lambda s: "REFUSAL"])
    assert (result.status, result.code) == ("failure", "MODEL_REFUSAL")


def test_policy_block_is_reported_to_the_model(run_discovery):
    script = [
        lambda s: call("navigate", path="/logout", reasoning="Sign off"),
        lambda s: call("wait", seconds=0.5, reasoning="ok"),
    ]
    result, events, model, _ = run_discovery(script, max_steps=2, verify=False)
    reply = model.requests[1]["messages"][-1]["content"][0]["content"][0]["text"]
    assert "blocked" in reply and "path_not_allowed" in reply
    assert any(e.type == "policy.decision" and not e.data["allowed"] for e in events)


OPEN_PARAMS = {"member_id": Param("10001", "string", "pii"), "account_type": Param("savings"),
               "initial_deposit": Param("300.00", "decimal", "financial")}


def open_script(after_confirm):
    return [
        lambda s: call("fill", ref=ref(s, 'textbox(text) "Member ID"'), input="member_id", reasoning="Find member"),
        lambda s: call("click", ref=ref(s, 'button "Search"'), expect_text="Member Detail", reasoning="Search"),
        lambda s: call("click", ref=ref(s, 'button "Open Sub-Account"'), expect_text="Initial Deposit:",
                       reasoning="Start a sub-account"),
        lambda s: call("select", ref=ref(s, 'combobox "Account Type"'), input="account_type", reasoning="Type"),
        lambda s: call("fill", ref=ref(s, 'textbox(text) "Initial Deposit"'), input="initial_deposit",
                       reasoning="Deposit"),
        lambda s: call("click", ref=ref(s, 'button "Continue"'), expect_text="Review Sub-Account", reasoning="Review"),
        lambda s: call("click", ref=ref(s, 'button "Confirm"'), expect_text="Account opened", reasoning="Commit"),
        *after_confirm,
    ]


def test_irreversible_action_without_operator_needs_human(run_discovery):
    result, events, _, _ = run_discovery(open_script([]), goal="Open a savings sub-account for member 10001",
                                         params=OPEN_PARAMS, capability="demo_bank.open_sub_account")
    assert (result.status, result.code) == ("needs_human", "NEEDS_HUMAN")
    blocked = [e for e in events if e.type == "step.finished" and e.data["status"] == "blocked"]
    assert len(blocked) == 1 and blocked[0].data["detail"] == "irreversible_requires_approval"
    assert result.artifact_path is None


def test_operator_performs_irreversible_action_during_discovery(run_discovery, app_profile, surface_factory,
                                                               logs_root, mockbank):
    holder = []

    def hook(surface, handoff):
        main = surface.page.frame(name="main")
        main.click("input[value=Confirm]")
        main.wait_for_selector("text=Account opened successfully")
        Operator(handoff.server.port).post("resume")

    def factory(log, surface, redactor):
        h = Handoff(log, surface, redactor, subject="discovery", port=0, timeout_s=30, on_human_control=hook)
        holder.append(h)
        return h

    def operator():
        while not holder:
            time.sleep(0.05)
        Operator(holder[0].server.port).wait_for("awaiting_human", timeout=120)
        Operator(holder[0].server.port).post("take")

    threading.Thread(target=operator, daemon=True).start()
    after = [
        lambda s: call("extract", ref=ref(s, 'row="New Account Number:"'), output_name="new_account_number",
                       type="string", sensitivity="pii", reasoning="Read the new account number"),
        lambda s: call("done", success_text="Account opened successfully", summary="Opened"),
    ]
    result, events, model, _ = run_discovery(open_script(after), goal="Open a savings sub-account for member 10001",
                                             params=OPEN_PARAMS, capability="demo_bank.open_sub_account",
                                             handoff_factory=factory, verify=False)
    assert result.status == "artifact_written", result
    artifact = load_yaml(result.artifact_path, Artifact)
    human = [s for s in artifact.steps if s.recorded_from == "human"]
    assert len(human) == 1
    assert (human[0].action, human[0].risk, human[0].expect.text_visible) == ("click", "irreversible", "Account Opened")
    assert [s.action for s in artifact.steps] == ["fill", "click", "click", "select", "fill", "click", "click", "extract"]
    reply = model.requests[7]["messages"][-1]["content"][0]["content"][0]["text"]
    assert "human operator took control" in reply and "click Confirm" in reply

    # the draft (with the human's step) replays, and still needs approval for the irreversible step
    url = mockbank()
    inputs = {"member_id": "10002", "account_type": "checking", "initial_deposit": "40.00"}
    kwargs = dict(app=app_profile, policy=policy_for(url), base_url=url, logs_root=logs_root,
                  surface_factory=surface_factory, secrets=SECRETS)
    assert replay(artifact, inputs, **kwargs).status == "approval_required"
    assert replay(artifact, inputs, approve_irreversible=True, **kwargs).status == "success"
