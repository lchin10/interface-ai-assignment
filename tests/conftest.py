from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from automation import load_env
from automation.logs import check_invariants, read_events
from automation.policy import Policy
from automation.replay import replay
from automation.schema import AppProfile, Artifact, load_yaml
from automation.surface import WebSurface
from demo.mockbank.app import create_app

ROOT = Path(__file__).resolve().parents[1]
REVIEWED = ROOT / "demo" / "artifacts" / "reviewed"  # hand-authored artifacts: what a reviewed contract looks like
load_env(ROOT / ".env")  # lets `pytest -m live` pick up ANTHROPIC_API_KEY; offline tests pass their own secrets
USER, PASSWORD = "teller-test", "Pw-test-7731"
SECRETS = {"MOCKBANK_USER": USER, "MOCKBANK_PASS": PASSWORD}


def load_artifact(name: str) -> Artifact:
    return load_yaml(REVIEWED / name, Artifact)


def policy_for(base_url: str) -> Policy:
    return Policy.load(ROOT / "demo" / "policy.yaml").model_copy(update={"allowed_origins": [base_url]})


@pytest.fixture(scope="session")
def app_profile() -> AppProfile:
    return load_yaml(ROOT / "demo" / "apps" / "demo_bank.yaml", AppProfile)


@pytest.fixture
def mockbank():
    """Start a fresh mock bank (own state, own faults) on a free port: mockbank('notice', seed=1)."""
    servers = []

    def start(*faults: str, seed: int = 0) -> str:
        app = create_app(faults=faults, seed=seed, user=USER, password=PASSWORD, slow_seconds=1.2)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def surface_factory(browser):
    created: list[WebSurface] = []

    def make() -> WebSurface:
        s = WebSurface.launch(browser=browser)
        created.append(s)
        return s

    make.created = created
    yield make
    for s in created:
        s.close()


@pytest.fixture
def logs_root(tmp_path) -> Path:
    return tmp_path / "logs"


@pytest.fixture
def run_replay(mockbank, surface_factory, app_profile, logs_root):
    """Replay a fixture artifact against a fresh mock bank; returns (result, events, run_dir)."""

    def run(artifact: str | Artifact, inputs: dict[str, str], *faults: str, approve: bool = False,
            handoff_factory=None, seed: int = 0, policy: Policy | None = None, secrets=None):
        url = mockbank(*faults, seed=seed)
        artifact = artifact if isinstance(artifact, Artifact) else load_artifact(artifact)
        policy = policy.model_copy(update={"allowed_origins": [url]}) if policy else policy_for(url)
        result = replay(artifact, inputs, app=app_profile, policy=policy, base_url=url,
                        logs_root=logs_root, surface_factory=surface_factory, approve_irreversible=approve,
                        handoff_factory=handoff_factory, secrets=SECRETS if secrets is None else secrets)
        run_dir = logs_root / result.run_id
        events = read_events(run_dir)
        assert check_invariants(events, run_dir) == []
        return result, events, run_dir

    return run


def key_events(events, types=("step.started", "step.finished", "condition.detected", "recovery.attempted",
                              "checkpoint.passed", "checkpoint.failed", "output.extracted", "control.transferred",
                              "intervention.requested", "intervention.resolved", "run.finished")) -> list[str]:
    """Compact, order-preserving timeline used to assert exact event sequences."""
    out = []
    for e in events:
        if e.type not in types:
            continue
        label = e.type
        if e.step_id:
            label += f":{e.step_id}"
        if e.type == "step.finished":
            label += f"={e.data['status']}"
        elif e.type in ("condition.detected", "recovery.attempted"):
            label += f"={e.data['code']}"
        elif e.type == "run.finished":
            label += f"={e.data['status']}"
        elif e.type == "control.transferred":
            label += f"={e.data['to_state']}"
        out.append(label)
    return out
