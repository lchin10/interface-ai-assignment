"""The one test that calls a real model. Run it with: pytest -m live (needs ANTHROPIC_API_KEY)."""
from __future__ import annotations

import os

import pytest
from conftest import SECRETS, policy_for

from automation.agent import Param, anthropic_create_message, discover
from automation.logs import check_invariants, read_events
from automation.schema import Artifact, load_yaml

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"),
]


def test_real_discovery_produces_a_capability_that_replays(mockbank, surface_factory, app_profile, logs_root, tmp_path):
    url = mockbank()
    result = discover(
        goal="Look up member 10001 and read their current savings balance",
        capability_id="demo_bank.lookup_balance",
        params={"member_id": Param("10001", "string", "pii")},
        app=app_profile, policy=policy_for(url), logs_root=logs_root, artifacts_dir=tmp_path / "artifacts",
        surface_factory=surface_factory, create_message=anthropic_create_message(), base_url=url,
        max_steps=15, secrets=SECRETS,
    )
    assert result.status == "artifact_written", result
    artifact = load_yaml(result.artifact_path, Artifact)
    assert artifact.steps and artifact.outputs
    assert result.verification["status"] == "success", result.verification
    assert "12450.33" in str(result.verification["outputs"].values())

    events = read_events(logs_root / result.run_id)
    assert check_invariants(events, logs_root / result.run_id) == []
    assert [e for e in events if e.type == "llm.response" and e.data["usage"]], "real usage should be recorded"
