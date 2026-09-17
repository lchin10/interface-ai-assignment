"""CLI: python -m automation discover|replay"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import load_env
from .agent import DEFAULT_MODEL, anthropic_create_message, discover, parse_param
from .handoff import Handoff
from .policy import Policy
from .replay import replay, sensitive_mask
from .schema import AppProfile, Artifact, load_yaml, sha256_file
from .surface import WebSurface

# success-ish 0, known business outcome 2, needs a person 3, aborted 4, failure 1
EXIT_CODES = {"success": 0, "artifact_written": 0, "failure": 1, "business_outcome": 2,
              "needs_human": 3, "approval_required": 3, "aborted": 4}


def main(argv: list[str] | None = None) -> int:
    load_env()
    p = argparse.ArgumentParser(prog="python -m automation")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--app", required=True, help="app profile YAML")
        sp.add_argument("--policy", required=True, help="policy YAML")
        sp.add_argument("--logs-dir", required=True, help="run logs are written to <logs-dir>/<run_id>/")
        sp.add_argument("--base-url", help="override the app profile's base_url (tenant deployment)")
        sp.add_argument("--param", action="append", default=[], help="input value, repeatable")
        sp.add_argument("--headed", action="store_true", help="show the browser (needed for a human takeover)")
        sp.add_argument("--operator-port", type=int, default=8765)
        sp.add_argument("--handoff-timeout", type=float, default=900, help="seconds to wait for an operator")

    d = sub.add_parser("discover", help="let the model complete a goal once and record it as a capability")
    common(d)
    d.add_argument("--goal", required=True)
    d.add_argument("--capability", required=True, help="capability id, e.g. demo_bank.lookup_balance")
    d.add_argument("--artifacts-dir", required=True)
    d.add_argument("--model", default=os.environ.get("AUTOMATION_MODEL", DEFAULT_MODEL))
    d.add_argument("--max-steps", type=int, default=30)
    d.add_argument("--timeout", type=float, default=900)
    d.add_argument("--no-verify", action="store_true", help="skip the verification replay")

    r = sub.add_parser("replay", help="run a saved capability deterministically (no model)")
    common(r)
    r.add_argument("artifact")
    r.add_argument("--approve-irreversible", action="store_true", help="caller authorises irreversible steps")
    r.add_argument("--escalate", action="store_true", help="hand the live session to a human instead of failing")

    args = p.parse_args(argv)
    app = load_yaml(args.app, AppProfile)
    policy = Policy.load(args.policy)
    headless = not args.headed

    def surface_factory():
        return WebSurface.launch(headless=headless)

    def handoff_factory(subject: str, mask):
        if headless:
            print("[handoff] note: browser is headless; run with --headed so an operator can take over", file=sys.stderr)
        return lambda log, surface, redactor: Handoff(log, surface, redactor, subject=subject, mask=mask,
                                                     port=args.operator_port, timeout_s=args.handoff_timeout)

    if args.command == "discover":
        params = dict(parse_param(x) for x in args.param)
        result = discover(
            goal=args.goal, capability_id=args.capability, params=params, app=app, policy=policy,
            logs_root=args.logs_dir, artifacts_dir=args.artifacts_dir, surface_factory=surface_factory,
            create_message=anthropic_create_message(), model=args.model, base_url=args.base_url,
            max_steps=args.max_steps, timeout_s=args.timeout, handoff_factory=handoff_factory(args.goal, app.mask),
            verify=not args.no_verify,
        )
    else:
        artifact = load_yaml(args.artifact, Artifact)
        inputs = {}
        for item in args.param:
            name, sep, value = item.partition("=")
            if not sep:
                p.error(f"bad --param {item!r}; expected name=value")
            inputs[name] = value
        result = replay(
            artifact, inputs, app=app, policy=policy, logs_root=args.logs_dir, surface_factory=surface_factory,
            base_url=args.base_url, approve_irreversible=args.approve_irreversible,
            handoff_factory=handoff_factory(artifact.capability.id, app.mask + sensitive_mask(artifact))
            if args.escalate else None,
            artifact_sha256=sha256_file(args.artifact),
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return EXIT_CODES[result.status]


if __name__ == "__main__":
    sys.exit(main())
