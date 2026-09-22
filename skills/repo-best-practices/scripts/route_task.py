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
    "angular",
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
    infrastructure = _matched(normalized, INFRA_SIGNALS)

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
