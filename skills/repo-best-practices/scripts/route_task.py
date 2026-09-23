#!/usr/bin/env python3
"""Return repository-routing hints for a task request.

This intentionally uses a small, dependency-free signal table. It is a first
pass for choosing which current repository guidance to read, not an ownership
authority. Exact source paths and repository guidance take precedence.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass


BACKEND_SIGNALS = (
    "django",
    "drf",
    "viewset",
    "serializer",
    "model",
    "migration",
    "celery",
    "backend",
    "postgres",
    "database",
    "permission",
    "ledger",
    "api contract",
    "api endpoint",
    "api integration",
    "checkout",
    "basket",
)

MODERN_FRONTEND_SIGNALS = (
    "react",
    "next.js",
    "nextjs",
    "next app",
    "mobile",
    "desktop",
    "showpass-frontend",
)

FRONTEND_CONTEXT_SIGNALS = (
    "repository",
    "service hook",
    "component",
    "page",
    "route",
    "ui",
)

LEGACY_FRONTEND_SIGNALS = (
    "angularjs",
    "django template",
    "legacy frontend",
    "legacy template",
    "gulp",
    "apps/main/static",
    "apps/main/templates",
    "legacy bridge",
    "legacy redirect",
    "embedded page",
)

INFRA_SIGNALS = (
    "kubernetes",
    "k8s",
    "fabric",
    "fabric3",
    "jinja",
    "kubectl",
    "gcloud",
    "ingress",
    "rollout",
    "deployment",
    "hpa",
    "keda",
    "cluster",
    "helm",
    "infrastructure",
    "infra",
    "web-k8s",
    "monitoring",
    "environment configuration",
    "environment config",
    "environment selection",
)

DEPLOYMENT_ENVIRONMENTS = r"(?:demo|beta|live|production)"
DEPLOYMENT_WORDS = r"(?:deploy|deploying|deployed|deployment|deployments)"
FAB_COMMAND_RE = re.compile(r"\bfab\s+(?:demo|beta|live)\b")
FABRIC3_RE = re.compile(r"\bfabric\s*3\b")
ENVIRONMENT_CONTEXT_RE = re.compile(
    rf"\b{DEPLOYMENT_ENVIRONMENTS}\s+environment\b"
    rf"|\benvironment\s+{DEPLOYMENT_ENVIRONMENTS}\b"
)
DEPLOYMENT_ENVIRONMENT_RE = re.compile(
    rf"""
    \b{DEPLOYMENT_WORDS}\b
    (?:
        \s+(?:the\s+)?{DEPLOYMENT_ENVIRONMENTS}\b
        | [^.!?\n]*?\b(?:to|against|on|in|for)\s+(?:the\s+)?{DEPLOYMENT_ENVIRONMENTS}\b
        | [^.!?\n]*?--env(?:=|\s+){DEPLOYMENT_ENVIRONMENTS}\b
    )
    |
    \b{DEPLOYMENT_ENVIRONMENTS}\s+{DEPLOYMENT_WORDS}\b
    """,
    re.VERBOSE,
)
DEPLOYMENT_VERB_RE = re.compile(r"\b(?:deploy|deploying|deployed|deployment|deployments)\b")
DEPLOYMENT_TARGET_RE = re.compile(
    r"\b(?:next\.?js|django|angularjs)\b"
    r"|\blegacy frontend\b"
    r"|\b(?:web\.(?:rolling_update|quick_rolling_update|"
    r"frontend\.deploy_nextjs(?:_workload)?|tasks\.rolling_update)|"
    r"ingress\.rolling_update|monitoring\.rolling_update)\b"
)
DEPLOYMENT_TASK_RE = re.compile(
    r"\b(?:web\.(?:rolling_update|quick_rolling_update|"
    r"frontend\.deploy_nextjs(?:_workload)?|tasks\.rolling_update)|"
    r"ingress\.rolling_update|monitoring\.rolling_update)\b"
)


@dataclass(frozen=True)
class RouteDecision:
    category: str
    repositories: tuple[str, ...]
    signals: tuple[str, ...]
    next_step: str


def _contains_signal(text: str, signal: str) -> bool:
    if " " in signal or "." in signal or "/" in signal:
        return signal in text
    return re.search(rf"\b{re.escape(signal)}\b", text) is not None


def _matched(text: str, signals: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(signal for signal in signals if _contains_signal(text, signal))


def _special_infrastructure_signals(text: str) -> tuple[str, ...]:
    signals: list[str] = []
    if FAB_COMMAND_RE.search(text):
        signals.append("fab environment command")
    if FABRIC3_RE.search(text):
        signals.append("fabric3")
    if ENVIRONMENT_CONTEXT_RE.search(text):
        signals.append("explicit environment")
    if DEPLOYMENT_ENVIRONMENT_RE.search(text) or (
        DEPLOYMENT_VERB_RE.search(text) and DEPLOYMENT_TARGET_RE.search(text)
    ) or DEPLOYMENT_TASK_RE.search(text):
        signals.append("deployment context")
    return tuple(signals)


def route_request(request: str) -> RouteDecision:
    normalized = " ".join(request.lower().split())
    if not normalized:
        raise ValueError("request must not be empty")

    backend = _matched(normalized, BACKEND_SIGNALS)
    legacy_frontend = _matched(normalized, LEGACY_FRONTEND_SIGNALS)
    explicit_modern_frontend = _matched(normalized, MODERN_FRONTEND_SIGNALS)
    frontend_context = (
        _matched(normalized, FRONTEND_CONTEXT_SIGNALS)
        if explicit_modern_frontend
        else ()
    )
    modern_frontend = explicit_modern_frontend + frontend_context
    infrastructure = tuple(
        dict.fromkeys(
            _matched(normalized, INFRA_SIGNALS)
            + _special_infrastructure_signals(normalized)
        )
    )

    repositories: list[str] = []
    signals: list[str] = []
    if backend or legacy_frontend:
        repositories.append("web-app")
    if backend:
        signals.extend(f"backend:{signal}" for signal in backend)
    if modern_frontend:
        repositories.append("showpass-frontend")
        signals.extend(f"frontend:{signal}" for signal in modern_frontend)
    signals.extend(f"legacy-frontend:{signal}" for signal in legacy_frontend)
    if infrastructure:
        repositories.append("web-k8s")
        signals.extend(f"infra:{signal}" for signal in infrastructure)

    if not repositories:
        category = "unknown"
        next_step = "Read current root AGENTS.md and trace the exact entrypoint before choosing an owner."
    elif len(repositories) > 1:
        category = "mixed"
        next_step = "Trace the backend contract, then the client adapter, then deployment configuration; include only changed owners."
    elif legacy_frontend and not modern_frontend and not infrastructure:
        category = "legacy-frontend"
        next_step = "Read web-app root AGENTS.md and the legacy frontend/domain guidance before editing."
    elif repositories == ["web-app"]:
        category = "backend"
        next_step = "Read web-app root AGENTS.md and the exact backend/domain source path before editing."
    elif repositories == ["showpass-frontend"]:
        category = "frontend"
        next_step = "Read showpass-frontend root AGENTS.md, then trace Repository -> Service Hook -> UI."
    else:
        category = "infrastructure"
        next_step = "Read web-k8s root AGENTS.md, select the environment, and verify authorization before any operational command."

    return RouteDecision(
        category=category,
        repositories=tuple(dict.fromkeys(repositories)),
        signals=tuple(signals),
        next_step=next_step,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="+", help="task request to classify")
    args = parser.parse_args(argv)
    try:
        decision = route_request(" ".join(args.request))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(decision), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
