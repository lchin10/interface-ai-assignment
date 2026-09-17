"""Regenerate the saved runs in /evidence/.

    python -m demo.make_logs              # replay scenarios only (no API key needed)
    python -m demo.make_logs --discover   # also record a real LLM discovery run first

Each scenario runs against a fresh mock bank with its own faults, and the run folder is copied to
evidence/<name>/. The handoff scenario drives the operator console from a background thread, standing in
for a person clicking Take control / Resume; everything it exercises (the state machine, capture,
resume) is the real mechanism.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from werkzeug.serving import make_server

from automation import load_env
from automation.agent import Param, anthropic_create_message, discover
from automation.handoff import Handoff
from automation.policy import Policy
from automation.replay import replay, sensitive_mask
from automation.schema import AppProfile, Artifact, load_yaml, sha256_file
from automation.surface import WebSurface
from demo.mockbank.app import create_app

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
PROFILE = ROOT / "demo" / "apps" / "demo_bank.yaml"
POLICY = ROOT / "demo" / "policy.yaml"
USER, PASSWORD = "teller01", "mock-only-password"
SECRETS = {"MOCKBANK_USER": USER, "MOCKBANK_PASS": PASSWORD}
LOOKUP = {"member_id": "10001"}
OPEN = {"member_id": "10001", "account_type": "savings", "initial_deposit": "250.00"}


def start_bank(*faults: str):
    server = make_server("127.0.0.1", 0, create_app(faults=faults, user=USER, password=PASSWORD), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


REVIEWED = ROOT / "demo" / "artifacts" / "reviewed"


def artifact_path(name: str) -> Path:
    discovered = ROOT / "demo" / "artifacts" / f"demo_bank.{name}.yaml"
    return discovered if discovered.exists() else REVIEWED / f"{name}.yaml"


def operator_takeover(holder: list[Handoff], hook_actions=None):
    """A background 'operator': waits for the intervention, takes control, optionally acts, resumes."""
    def post(port: int, action: str) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{action}", data=b"", method="POST",
                                     headers={"X-Operator": "demo-operator"})
        urllib.request.urlopen(req, timeout=5).read()

    def state(port: int) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/intervention", timeout=5) as r:
            return json.load(r)["state"]

    def hook(surface, handoff):
        if hook_actions:
            hook_actions(surface.page.frame(name="main"))
        post(handoff.server.port, "resume")

    def watcher():
        while not holder:
            time.sleep(0.05)
        port = holder[0].server.port
        while state(port) != "awaiting_human":
            time.sleep(0.05)
        post(port, "take")

    def factory(log, surface, redactor):
        h = Handoff(log, surface, redactor, subject="demo", port=0, timeout_s=60, on_human_control=hook)
        holder.append(h)
        return h

    threading.Thread(target=watcher, daemon=True).start()
    return factory


def run_scenario(name: str, capability: str, inputs: dict, *, faults=(), approve=False, handoff=False,
                 hook_actions=None, logs_root: Path, reviewed: bool = False) -> str:
    server, url = start_bank(*faults)
    # `reviewed`: use the hand-authored artifact, whose contract a human tightened (e.g. an id pattern)
    path = REVIEWED / f"{capability}.yaml" if reviewed else artifact_path(capability)
    artifact = load_yaml(path, Artifact)
    policy = Policy.load(POLICY).model_copy(update={"allowed_origins": [url]})
    factory = operator_takeover([], hook_actions) if handoff else None
    try:
        result = replay(artifact, inputs, app=load_yaml(PROFILE, AppProfile), policy=policy, base_url=url,
                        logs_root=logs_root, surface_factory=lambda: WebSurface.launch(headless=True),
                        approve_irreversible=approve, handoff_factory=factory, secrets=SECRETS,
                        artifact_sha256=sha256_file(path))
    finally:
        server.shutdown()
    publish(logs_root / result.run_id, name)
    print(f"  {name:28} {result.status:18} {getattr(result, 'code', '') or ''}")
    return result.status


def publish(run_dir: Path, name: str) -> None:
    target = EVIDENCE / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(run_dir, target)


def run_discovery(logs_root: Path) -> None:
    server, url = start_bank()
    try:
        result = discover(
            goal="Look up member 10001 and read their current savings balance",
            capability_id="demo_bank.lookup_balance",
            params={"member_id": Param("10001", "string", "pii")},
            app=load_yaml(PROFILE, AppProfile),
            policy=Policy.load(POLICY).model_copy(update={"allowed_origins": [url]}),
            logs_root=logs_root, artifacts_dir=ROOT / "demo" / "artifacts",
            surface_factory=lambda: WebSurface.launch(headless=True),
            create_message=anthropic_create_message(), base_url=url, max_steps=20, secrets=SECRETS,
        )
    finally:
        server.shutdown()
    publish(logs_root / result.run_id, "discovery")
    if result.verification:
        publish(logs_root / result.verification["run_id"], "discovery-verification-replay")
    print(f"  {'discovery':28} {result.status:18} {result.code or ''} "
          f"(verification: {(result.verification or {}).get('status')})")


def main() -> None:
    load_env(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--discover", action="store_true", help="record a real LLM discovery run (needs ANTHROPIC_API_KEY)")
    args = p.parse_args()
    if args.discover and not os.environ.get("ANTHROPIC_API_KEY"):
        p.error("--discover needs ANTHROPIC_API_KEY (set it in .env)")
    EVIDENCE.mkdir(exist_ok=True)
    logs_root = Path(tempfile.mkdtemp(prefix="make-logs-"))
    print(f"writing runs to {logs_root}, publishing to {EVIDENCE}")
    if args.discover:
        run_discovery(logs_root)
    run_scenario("replay-success", "lookup_balance", LOOKUP, logs_root=logs_root)
    run_scenario("replay-business-outcome-not-found", "lookup_balance", {"member_id": "99999"}, logs_root=logs_root)
    run_scenario("replay-recovered-session-expiry", "lookup_balance", LOOKUP, faults=("session_expiry",),
                 logs_root=logs_root)
    run_scenario("replay-recovered-interstitial", "lookup_balance", LOOKUP, faults=("notice", "slow"),
                 logs_root=logs_root)
    run_scenario("replay-tenant-variant-drift", "lookup_balance", LOOKUP, faults=("relabel",), logs_root=logs_root)
    run_scenario("replay-failure-server-error", "lookup_balance", LOOKUP, faults=("server_error",), logs_root=logs_root)
    run_scenario("replay-invalid-input", "lookup_balance", {"member_id": "not-an-id"}, logs_root=logs_root,
                 reviewed=True)
    run_scenario("replay-approval-required", "open_sub_account", OPEN, logs_root=logs_root)
    run_scenario("replay-approved-irreversible", "open_sub_account", OPEN, approve=True, logs_root=logs_root)
    run_scenario("handoff-operator-confirms", "open_sub_account", OPEN, handoff=True, logs_root=logs_root,
                 hook_actions=lambda main: (main.click("input[value=Confirm]"),
                                            main.wait_for_selector("text=Account opened successfully")))
    run_scenario("handoff-operator-approves", "open_sub_account", OPEN, handoff=True, logs_root=logs_root)


if __name__ == "__main__":
    main()
