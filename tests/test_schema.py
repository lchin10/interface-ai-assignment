from __future__ import annotations

import json

import pytest
import yaml
from conftest import REVIEWED, load_artifact
from pydantic import ValidationError

from automation.schema import (
    EVENT_DATA,
    REPLAY_RESULT,
    Aborted,
    AppProfile,
    ApprovalRequired,
    Artifact,
    BusinessOutcome,
    Condition,
    Failure,
    InputValidationError,
    NeedsHuman,
    Success,
    TemplateError,
    dump_yaml,
    load_yaml,
    render,
    validate_inputs,
)


def raw(name: str = "lookup_balance.yaml") -> dict:
    return yaml.safe_load((REVIEWED / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["lookup_balance.yaml", "open_sub_account.yaml"])
def test_artifacts_round_trip_through_yaml(tmp_path, name):
    artifact = load_artifact(name)
    dump_yaml(artifact, tmp_path / name)
    assert load_yaml(tmp_path / name, Artifact) == artifact


def test_app_profile_is_valid(app_profile: AppProfile):
    assert [s.id for s in app_profile.login] == ["login.s1", "login.s2", "login.s3", "login.s4"]
    kinds = {c.code: c.kind for c in app_profile.conditions}
    assert kinds["SESSION_EXPIRED"] == "recoverable"
    assert kinds["PERMISSION_DENIED"] == "business"
    assert kinds["HTTP_500"] == "failure"


def test_json_schema_export():
    schema = Artifact.model_json_schema()
    json.dumps(schema)
    assert {"capability", "inputs", "outputs", "steps", "outcomes", "success"} <= set(schema["properties"])
    assert {"RoleStrategy", "AdjacentLabelStrategy", "TableCellStrategy", "CssStrategy"} <= set(schema["$defs"])


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["steps"].append(dict(d["steps"][0])), "duplicate step ids"),
    (lambda d: d["steps"][0].update(value="{{inputs.nope}}"), "unknown input"),
    (lambda d: d["steps"][0].update(value="{{secrets.MOCKBANK_PASS}}"), "may only reference inputs"),
    (lambda d: d["steps"][0].update(value="{{app.password}}"), "unknown app value"),
    (lambda d: d["outputs"]["savings_balance"].update(from_step="s1"), "from_step must be the extract step"),
    (lambda d: d["steps"][2].update(output="other"), "undeclared output"),
    (lambda d: d["steps"][2]["target"].update(strategies=[]), "at least 1 item"),
    (lambda d: d["steps"][2]["target"]["strategies"].append({"by": "xpath", "value": "//td"}), "xpath"),
    (lambda d: d["capability"].update(version="1.0"), "String should match pattern"),
    (lambda d: d["capability"].update(id="NoNamespace"), "String should match pattern"),
    (lambda d: d["steps"][1].update(risk="irreversible", expect=None), "irreversible steps need an expect"),
    (lambda d: d["steps"][1].pop("target"), "click needs a target"),
    (lambda d: d["steps"][0].pop("value"), "fill needs a value"),
    (lambda d: d["steps"][1].update(output="x"), "only extract steps may set output"),
    (lambda d: d["outcomes"][0].update(steps=["s9"]), "unknown steps"),
    (lambda d: d["outcomes"][0].update(kind="recoverable"), "recoverable outcomes need a handler"),
    (lambda d: d["outcomes"][0].update(code="not-upper"), "String should match pattern"),
    (lambda d: d.update(surprise=True), "Extra inputs are not permitted"),
    (lambda d: d["success"].update(url_matches="Member"), "exactly one of"),
    (lambda d: d.update(success={}), "exactly one of"),
    (lambda d: d["inputs"]["member_id"].update(type="enum"), "enum inputs need values"),
])
def test_invalid_artifacts_are_rejected(mutate, message):
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Artifact.model_validate(data)


def test_app_profile_rejects_undeclared_secret(app_profile):
    data = app_profile.model_dump(mode="json")
    data["secrets"] = ["MOCKBANK_USER"]
    with pytest.raises(ValidationError, match="MOCKBANK_PASS is not declared"):
        AppProfile.model_validate(data)


@pytest.mark.parametrize("inputs, error", [
    ({}, "missing input 'member_id'"),
    ({"member_id": "1234"}, "member_id: does not match"),
    ({"member_id": "10001", "extra": "x"}, "unexpected input 'extra'"),
])
def test_input_validation_lookup(inputs, error):
    with pytest.raises(InputValidationError, match=error):
        validate_inputs(load_artifact("lookup_balance.yaml"), inputs)


@pytest.mark.parametrize("override, error", [
    ({"account_type": "loan"}, "account_type: must be one of"),
    ({"initial_deposit": "abc"}, "initial_deposit: not a decimal"),
    ({"initial_deposit": "NaN"}, "initial_deposit: not a decimal"),
    ({"initial_deposit": "Infinity"}, "initial_deposit: not a decimal"),
    ({"initial_deposit": "0"}, "initial_deposit: must be >= 0.01"),
])
def test_input_validation_open_account(override, error):
    inputs = {"member_id": "10001", "account_type": "savings", "initial_deposit": "10.00", **override}
    with pytest.raises(InputValidationError, match=error):
        validate_inputs(load_artifact("open_sub_account.yaml"), inputs)


def test_input_validation_collects_every_error_and_normalises():
    artifact = load_artifact("open_sub_account.yaml")
    with pytest.raises(InputValidationError) as exc:
        validate_inputs(artifact, {"member_id": "x", "account_type": "loan", "initial_deposit": "-1"})
    assert len(exc.value.errors) == 3
    assert validate_inputs(artifact, {"member_id": " 10001 ", "account_type": "savings", "initial_deposit": "5"}) == \
        {"member_id": "10001", "account_type": "savings", "initial_deposit": "5"}


def test_render_templates():
    ctx = {"inputs": {"member_id": "10001"}, "app": {"base_url": "http://h"}}
    assert render("{{app.base_url}}/member?mid={{ inputs.member_id }}", ctx) == "http://h/member?mid=10001"
    with pytest.raises(TemplateError):
        render("{{inputs.missing}}", ctx)


def test_condition_describe_and_texts():
    c = Condition.model_validate({"all": [{"text_visible": "A"}, {"any": [{"text_visible": "B"}, {"url_matches": "/x"}]}]})
    assert c.describe() == '(text visible "A" AND (text visible "B" OR url matches //x/))'
    assert c.texts() == ["A", "B"]


def test_replay_results_round_trip_by_discriminator():
    base = {"run_id": "r1", "capability_id": "a.b", "capability_version": "1.0.0"}
    results = [
        Success(**base, outputs={"x": "1.00"}),
        BusinessOutcome(**base, code="NOT_FOUND", message="none", step_id="s2"),
        Failure(**base, code="HTTP_500", message="boom", step_id="s2", expected="e", observed="o", log_files=["a.png"]),
        NeedsHuman(**base, intervention_id="int-1", step_id="s1", reason="r"),
        ApprovalRequired(**base, step_id="s7", intent="confirm"),
        Aborted(**base, by="operator:alice", step_id=None),
    ]
    for r in results:
        assert REPLAY_RESULT.validate_python(r.model_dump(mode="json")) == r
    assert {r.status for r in results} == {"success", "business_outcome", "failure", "needs_human",
                                            "approval_required", "aborted"}


def test_every_event_type_has_a_strict_model():
    for name, model in EVENT_DATA.items():
        assert model.model_config.get("extra") == "forbid", name
