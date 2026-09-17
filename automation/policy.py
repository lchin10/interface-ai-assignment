"""Safety: the allowlist, risk classification, redaction, and the one chokepoint that enforces them.

Every action from the discovery agent, the replay engine and the resume path goes through
GuardedSurface.perform. Nothing else in the codebase calls Surface.perform directly.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml
from pydantic import Field

from .handoff import ControlState
from .schema import ActionKind, Model, Risk

if TYPE_CHECKING:
    from .handoff import Controller
    from .logs import RunLog
    from .surface import Resolved, Surface

_ORIGIN_RE = re.compile(r"^(https?)://([^:/\s]+)(?::(\d+|\*))?$")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str
    risk: Risk


class Policy(Model):
    allowed_origins: list[str] = Field(min_length=1, description="scheme://host[:port|*]")
    allowed_paths: list[str] = Field(min_length=1, description="Glob patterns on the normalised path")
    allowed_actions: list[ActionKind] = Field(min_length=1)
    irreversible_patterns: list[str] = Field(default_factory=list, description="Regexes on control names")

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        with open(path, encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def url_violation(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return "scheme_not_allowed"
        host, port = (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)
        if parts.username or parts.password:
            return "origin_not_allowed"
        if not any(self._origin_ok(spec, parts.scheme, host, port) for spec in self.allowed_origins):
            return "origin_not_allowed"
        path = posixpath.normpath(parts.path or "/")
        path = "/" if path == "." else path
        if not any(fnmatch.fnmatchcase(path, g) for g in self.allowed_paths):
            return "path_not_allowed"
        return None

    @staticmethod
    def _origin_ok(spec: str, scheme: str, host: str, port: int) -> bool:
        m = _ORIGIN_RE.match(spec)
        if not m:
            raise ValueError(f"bad origin spec {spec!r}")
        s_scheme, s_host, s_port = m.group(1), m.group(2).lower(), m.group(3)
        default_port = "443" if s_scheme == "https" else "80"
        return scheme == s_scheme and host == s_host and (s_port == "*" or int(s_port or default_port) == port)

    def classify(self, action: str, element: dict[str, Any] | None) -> Risk:
        """Clicks on controls whose visible name looks like a commit are irreversible."""
        if action != "click" or not element:
            return "safe"
        label = " ".join(str(element.get(k) or "") for k in ("name", "text"))
        if any(re.search(p, label, re.IGNORECASE) for p in self.irreversible_patterns):
            return "irreversible"
        return "safe"

    def check(
        self,
        action: str,
        *,
        url: str | None,
        element: dict[str, Any] | None = None,
        declared_risk: Risk = "safe",
        approved: bool = False,
    ) -> Decision:
        risk: Risk = "irreversible" if "irreversible" in (declared_risk, self.classify(action, element)) else "safe"
        if action not in self.allowed_actions:
            return Decision(False, "action_not_allowed", risk)
        if action != "wait":
            violation = self.url_violation(url or "")
            if violation:
                return Decision(False, violation, risk)
        if action == "click" and element and element.get("href") and not element["href"].startswith("javascript:void"):
            violation = self.url_violation(element["href"])
            if violation:
                return Decision(False, f"link_{violation}", risk)
        if risk == "irreversible" and not approved:
            return Decision(False, "irreversible_requires_approval", risk)
        return Decision(True, "allowed", risk)


# --------------------------------------------------------------------------- redaction


class Redactor:
    """Scrubs known sensitive values (registered at runtime) and common financial identifiers."""

    PATTERNS = (
        ("SSN", re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")),
        ("CARD", re.compile(r"(?<!\d)(?:\d{4}[ -]){3}\d{4}(?!\d)")),
        ("ACCOUNT", re.compile(r"(?<![\d.,])\d{8,19}(?![\d.,])")),
    )
    # identifiers we generate ourselves; never sensitive, and digit runs in them are not accounts
    SAFE_KEYS = frozenset({"observation_hash", "sha256", "artifact_sha256", "policy_sha256", "run_id",
                           "intervention_id", "request_id", "id", "path", "screenshot", "published_to"})

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def add(self, value: Any, label: str) -> None:
        text = str(value).strip()
        if len(text) >= 3:
            self._values[text] = f"[REDACTED:{label}]"

    def text(self, s: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            s = s.replace(value, self._values[value])
        for label, pattern in self.PATTERNS:
            s = pattern.sub(f"[{label}]", s)
        return s

    def obj(self, o: Any, key: str | None = None) -> Any:
        if isinstance(o, str):
            return o if key in self.SAFE_KEYS else self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v, k) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v, key) for v in o]
        return o


# --------------------------------------------------------------------------- chokepoint


class PolicyViolation(Exception):
    def __init__(self, decision: Decision, action: str):
        super().__init__(f"{action} blocked: {decision.rule}")
        self.decision = decision


class NotInControl(Exception):
    pass


class GuardedSurface:
    def __init__(self, surface: Surface, policy: Policy, controller: Controller, log: RunLog, actor: str):
        self.surface, self.policy, self.controller, self.log, self.actor = surface, policy, controller, log, actor

    def perform(
        self,
        step_id: str | None,
        action: ActionKind,
        *,
        resolved: Resolved | None = None,
        url: str | None = None,
        value: str | None = None,
        value_log: str | None = None,
        wait_ms: int | None = None,
        declared_risk: Risk = "safe",
        approved: bool = False,
    ) -> tuple[str | None, Decision]:
        target = resolved.describe() if resolved else None
        if self.controller.state is not ControlState.AUTOMATION:
            self.log.emit("policy.decision", {
                "action": action, "target": target, "url": url, "allowed": False,
                "rule": f"not_in_control:{self.controller.state}", "risk": declared_risk,
            }, actor=self.actor, step_id=step_id)
            raise NotInControl(f"automation does not hold control ({self.controller.state})")
        check_url = url if action == "navigate" else (resolved.frame_url if resolved else self.surface.url)
        decision = self.policy.check(action, url=check_url, element=resolved.element if resolved else None,
                                     declared_risk=declared_risk, approved=approved)
        self.log.emit("policy.decision", {
            "action": action, "target": target, "url": check_url, "allowed": decision.allowed,
            "rule": decision.rule, "risk": decision.risk,
        }, actor=self.actor, step_id=step_id)
        if not decision.allowed:
            raise PolicyViolation(decision, action)
        t0 = time.monotonic()
        out = self.surface.perform(action, resolved=resolved, url=url, value=value, wait_ms=wait_ms)
        self.log.emit("action.performed", {
            "action": action, "value": value_log, "duration_ms": int((time.monotonic() - t0) * 1000),
        }, actor=self.actor, step_id=step_id)
        return out, decision
