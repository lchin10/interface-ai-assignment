"""Replay against a live mock bank: every scenario asserts the returned result AND the exact
order of the key log events, so the log is proven to tell the same story as the result."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import key_events, load_artifact, policy_for

from automation.schema import Condition

LOGIN = [
    "step.started:login.s1", "checkpoint.passed:login.s1", "step.finished:login.s1=ok",
    "step.started:login.s2", "step.finished:login.s2=ok",
    "step.started:login.s3", "step.finished:login.s3=ok",
    "step.started:login.s4", "checkpoint.passed:login.s4", "step.finished:login.s4=ok",
]
LOOKUP_OK = [
    "step.started:s1", "step.finished:s1=ok",
    "step.started:s2", "checkpoint.passed:s2", "step.finished:s2=ok",
    "step.started:s3", "output.extracted:s3", "step.finished:s3=ok",
    "checkpoint.passed", "run.finished=success",
]
OPEN_TO_REVIEW = [
    "step.started:s1", "step.finished:s1=ok",
    "step.started:s2", "checkpoint.passed:s2", "step.finished:s2=ok",
    "step.started:s3", "checkpoint.passed:s3", "step.finished:s3=ok",
    "step.started:s4", "step.finished:s4=ok",
    "step.started:s5", "step.finished:s5=ok",
]


def of_type(events, type_, step_id=None):
    return [e for e in events if e.type == type_ and (step_id is None or e.step_id == step_id)]


# --------------------------------------------------------------------------- success paths


def test_success_returns_typed_outputs(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"})
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    assert result.warnings == []
    assert key_events(events) == LOGIN + LOOKUP_OK
    assert [e.data["strategy"] for e in of_type(events, "target.resolved") if e.step_id.startswith("s")] == \
        ["adjacent_label", "role", "table_cell"]


def test_same_artifact_other_inputs(run_replay):
    result, _, _ = run_replay("lookup_balance.yaml", {"member_id": "10002"})
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "8000.00"}


def test_replay_is_deterministic(run_replay):
    first, events1, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"})
    second, events2, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"})
    assert first.outputs == second.outputs
    assert key_events(events1) == key_events(events2)
    assert [(e.step_id, e.data["strategy"]) for e in of_type(events1, "target.resolved")] == \
        [(e.step_id, e.data["strategy"]) for e in of_type(events2, "target.resolved")]


def test_screenshot_after_every_step(run_replay):
    _, events, run_dir = run_replay("lookup_balance.yaml", {"member_id": "10001"})
    finished = [e.step_id for e in of_type(events, "step.finished")]
    shots = of_type(events, "screenshot")
    assert [e.step_id for e in shots] == finished
    assert all((run_dir / e.data["path"]).stat().st_size > 1000 for e in shots)


# --------------------------------------------------------------------------- business outcomes


def test_member_not_found_is_business_outcome(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "99999"})
    assert result.status == "business_outcome"
    assert (result.code, result.step_id) == ("MEMBER_NOT_FOUND", "s2")
    assert result.message == "No member found matching the search criteria."
    assert key_events(events) == LOGIN + [
        "step.started:s1", "step.finished:s1=ok",
        "step.started:s2", "condition.detected:s2=MEMBER_NOT_FOUND", "step.finished:s2=business_outcome",
        "run.finished=business_outcome",
    ]


def test_permission_denied_comes_from_app_profile(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "40300"})
    assert result.status == "business_outcome"
    assert result.code == "PERMISSION_DENIED"
    assert result.message == "Insufficient privileges to view this member record."
    assert "condition.detected:s2=PERMISSION_DENIED" in key_events(events)


def test_validation_error_is_business_outcome(run_replay):
    inputs = {"member_id": "10001", "account_type": "savings", "initial_deposit": "50000.00"}
    result, events, _ = run_replay("open_sub_account.yaml", inputs, approve=True)
    assert result.status == "business_outcome"
    assert (result.code, result.step_id) == ("VALIDATION_ERROR", "s6")
    assert result.message.startswith("Validation error: Initial deposit must be between")
    assert key_events(events) == LOGIN + OPEN_TO_REVIEW + [
        "step.started:s6", "condition.detected:s6=VALIDATION_ERROR", "step.finished:s6=business_outcome",
        "run.finished=business_outcome",
    ]
    assert not of_type(events, "step.started", "s7"), "must never reach the irreversible step"


# --------------------------------------------------------------------------- recoverable conditions


def test_interstitial_is_dismissed_and_logged(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "notice")
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    assert key_events(events) == LOGIN + [
        "step.started:s1", "step.finished:s1=ok",
        "step.started:s2", "condition.detected:s2=SYSTEM_NOTICE", "recovery.attempted:s2=SYSTEM_NOTICE",
        "checkpoint.passed:s2", "step.finished:s2=ok",
    ] + LOOKUP_OK[5:]
    clicks = [e.data["target"] for e in of_type(events, "policy.decision", "s2")]
    assert len(clicks) == 2 and "OK" in clicks[1]


def test_interstitial_is_dismissed_even_for_a_step_without_a_checkpoint(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.steps[1].expect = None  # as recorded when the model gives no expect_text
    result, events, _ = run_replay(artifact, {"member_id": "10001"}, "notice")
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    # the notice turns up while step 3 is still looking for the balance cell
    assert "condition.detected:s3=SYSTEM_NOTICE" in key_events(events)
    assert "recovery.attempted:s3=SYSTEM_NOTICE" in key_events(events)


def test_persistent_interstitial_exhausts_recovery(run_replay):
    result, events, run_dir = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "notice_always")
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("RECOVERY_EXHAUSTED", "s2")
    assert result.recoveries_attempted == ["SYSTEM_NOTICE", "SYSTEM_NOTICE"]
    assert [e.data["outcome"] for e in of_type(events, "recovery.attempted")] == ["ok", "ok", "exhausted"]
    assert key_events(events)[-2:] == ["step.finished:s2=failed", "run.finished=failure"]
    assert all((run_dir / f).exists() for f in result.log_files)


def test_session_expiry_relogs_in_and_restarts(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "session_expiry")
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    assert key_events(events) == LOGIN + [
        "step.started:s1", "step.finished:s1=ok",
        "step.started:s2", "condition.detected:s2=SESSION_EXPIRED", "recovery.attempted:s2=SESSION_EXPIRED",
        "step.finished:s2=interrupted",
    ] + LOGIN + LOOKUP_OK
    assert [e.data["attempt"] for e in of_type(events, "step.started", "s1")] == [1, 2]


def test_slow_pages_wait_within_checkpoint_timeout(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "slow")
    assert result.status == "success"
    assert of_type(events, "checkpoint.passed", "s2")[0].data["waited_ms"] >= 1000


def test_relabelled_variant_uses_fallback_and_reports_drift(run_replay):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "relabel")
    assert result.status == "success"
    assert result.outputs == {"savings_balance": "12450.33"}
    assert len(result.warnings) == 2
    assert result.warnings[0].startswith("s1: fallback strategy #1 (css=")
    assert result.warnings[1] == 's1: element name changed: "Member ID" -> "Member #"'
    s1 = of_type(events, "target.resolved", "s1")[0].data
    assert (s1["strategy"], s1["strategy_index"]) == ("css", 1)
    assert result.model_dump(mode="json")["warnings"] == \
        of_type(events, "run.finished")[0].data["result"]["warnings"]


# --------------------------------------------------------------------------- hard failures


def test_server_error_is_hard_failure_with_debug_files(run_replay):
    result, events, run_dir = run_replay("lookup_balance.yaml", {"member_id": "10001"}, "server_error")
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("HTTP_500", "s2")
    assert result.expected == 'text visible "Member Detail"'
    assert "Internal Server Error" in result.observed
    kinds = sorted(Path(f).parts[0] for f in result.log_files)
    assert kinds.count("screenshots") == 1 and kinds.count("frames") >= 2
    for f in result.log_files:
        assert (run_dir / f).stat().st_size > 0
    assert key_events(events) == LOGIN + [
        "step.started:s1", "step.finished:s1=ok",
        "step.started:s2", "condition.detected:s2=HTTP_500", "step.finished:s2=failed", "run.finished=failure",
    ]
    assert of_type(events, "screenshot")[-1].data["reason"] == "failure"


def test_unknown_state_fails_checkpoint_with_observation(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.steps[1].expect.text_visible = "Member Profile"
    artifact.steps[1].expect.timeout_ms = 1500
    result, events, _ = run_replay(artifact, {"member_id": "10001"})
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("CHECKPOINT_FAILED", "s2")
    assert result.expected == 'text visible "Member Profile"'
    assert "Member Detail" in result.observed
    failed = of_type(events, "checkpoint.failed", "s2")[0].data
    assert failed["waited_ms"] >= 1500 and "Member Detail" in failed["observed"]


def test_target_not_found_reports_expected_and_observed(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.steps[0].target.strategies = artifact.steps[0].target.strategies[:1]
    artifact.steps[0].target.strategies[0].text = "Customer No:"
    result, _, run_dir = run_replay(artifact, {"member_id": "10001"})
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("TARGET_NOT_FOUND", "s1")
    assert "Customer No:" in result.expected
    assert "Member Search" in result.observed
    assert result.log_files and all((run_dir / f).exists() for f in result.log_files)


def test_success_condition_is_verified(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.success = Condition(text_visible="Balance Certified")
    result, events, _ = run_replay(artifact, {"member_id": "10001"})
    assert result.status == "failure"
    assert result.code == "SUCCESS_CONDITION_FAILED"
    assert key_events(events)[-2:] == ["checkpoint.failed", "run.finished=failure"]


def test_unparseable_output_is_a_failure(run_replay):
    artifact = load_artifact("lookup_balance.yaml")
    artifact.outputs["savings_balance"].type = "integer"
    result, _, _ = run_replay(artifact, {"member_id": "10001"})
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("OUTPUT_PARSE_ERROR", "s3")


def test_policy_blocks_disallowed_action(run_replay, mockbank):
    policy = policy_for("http://x").model_copy(update={"allowed_actions": ["navigate", "click", "fill", "select"]})
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, policy=policy)
    assert result.status == "failure"
    assert (result.code, result.step_id) == ("POLICY_BLOCKED", "s3")
    decision = of_type(events, "policy.decision", "s3")[0].data
    assert (decision["allowed"], decision["rule"]) == (False, "action_not_allowed")
    assert not of_type(events, "action.performed", "s3")


# --------------------------------------------------------------------------- irreversible steps


def test_irreversible_step_requires_approval(run_replay):
    inputs = {"member_id": "10001", "account_type": "checking", "initial_deposit": "250.00"}
    result, events, _ = run_replay("open_sub_account.yaml", inputs)
    assert result.status == "approval_required"
    assert (result.step_id, result.intent) == ("s7", "Confirm opening the account")
    assert key_events(events)[-4:] == ["step.finished:s6=ok", "step.started:s7", "step.finished:s7=approval_required",
                                       "run.finished=approval_required"]
    decision = of_type(events, "policy.decision", "s7")[0].data
    assert (decision["allowed"], decision["rule"], decision["risk"]) == \
        (False, "irreversible_requires_approval", "irreversible")
    assert not of_type(events, "action.performed", "s7")


def test_irreversible_step_with_approval_succeeds(run_replay):
    inputs = {"member_id": "10001", "account_type": "checking", "initial_deposit": "250.00"}
    result, events, _ = run_replay("open_sub_account.yaml", inputs, approve=True)
    assert result.status == "success"
    assert re.fullmatch(r"\d{10}", result.outputs["new_account_number"])
    decision = of_type(events, "policy.decision", "s7")[0].data
    assert (decision["allowed"], decision["risk"]) == (True, "irreversible")


def test_classifier_catches_undeclared_irreversible_step(run_replay):
    artifact = load_artifact("open_sub_account.yaml")
    artifact.steps[6].risk = "safe"  # author forgot to mark Confirm; the policy classifier still does
    inputs = {"member_id": "10001", "account_type": "checking", "initial_deposit": "250.00"}
    result, _, _ = run_replay(artifact, inputs)
    assert result.status == "approval_required"


# --------------------------------------------------------------------------- before the browser


def test_invalid_input_never_launches_browser(run_replay, surface_factory):
    result, events, _ = run_replay("lookup_balance.yaml", {"member_id": "12ab"})
    assert result.status == "failure"
    assert result.code == "INVALID_INPUT"
    assert "does not match" in result.message
    assert surface_factory.created == []
    assert [e.type for e in events] == ["run.started", "run.finished"]


def test_missing_secret_never_launches_browser(run_replay, surface_factory):
    result, _, _ = run_replay("lookup_balance.yaml", {"member_id": "10001"}, secrets={})
    assert result.status == "failure"
    assert result.code == "MISSING_SECRET"
    assert surface_factory.created == []
