#!/usr/bin/env python3
"""Check the Python skill's source ledger and deterministic routing examples."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PORT_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent
for scripts_dir in (PORT_SCRIPTS_DIR, SKILL_SCRIPTS_DIR):
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

from skill_frontmatter import required_frontmatter_errors


SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_DIR / "SKILL.md"
LEDGER_PATH = SKILL_DIR / "references" / "source-ledger.json"
EXPECTED_UPSTREAM_SHA = "53e579f1481697931fc44f5445171397cfa2b24b"
EXPECTED_REPOSITORIES = {
    "web-app": {
        "sha": "508b22a3fa7a25ef5cdac465a29195117f3f97cc",
        "root": "web-app",
    },
    "showpass-frontend": {
        "sha": "0aae56009bb1aabe09942adb04f6aa4782f98450",
        "root": "showpass-frontend",
    },
    "web-k8s": {
        "sha": "daa154cab26375c721251c0d6e74717c79c563ae",
        "root": "web-k8s",
    },
}
EXPECTED_RULE_REPOSITORIES = {
    **{rule_id: "web-app" for rule_id in (
        "SP-WEB-ENV",
        "SP-WEB-OWNERS",
        "SP-WEB-LEGACY-ROUTING",
        "SP-WEB-LIFECYCLE",
        "SP-WEB-TRANSACTION",
        "SP-WEB-QUERY",
        "SP-WEB-SERVICE",
        "SP-WEB-CELERY",
        "SP-WEB-IMPORTS",
        "SP-WEB-TEST",
        "SP-WEB-RUFF",
    )},
    **{rule_id: "web-k8s" for rule_id in (
        "SP-K8S-VERSION",
        "SP-K8S-OFFLINE",
        "SP-K8S-CONTEXT",
        "SP-K8S-SAFETY",
        "SP-K8S-DEPLOYED",
    )},
    "SP-FRONTEND-BOUNDARY": "showpass-frontend",
    "SP-FRONTEND-PACKAGES": "showpass-frontend",
}
EXPECTED_RULE_ROUTES = {
    rule_id: "unsafe/manual-review" if rule_id == "SP-K8S-SAFETY" else repository
    for rule_id, repository in EXPECTED_RULE_REPOSITORIES.items()
}
LOCAL_REFERENCE_TARGETS = {
    "references/source-ledger.json": LEDGER_PATH,
    "skills/python-best-practices/scripts/check_skill.py": Path(__file__).resolve(),
}
SOURCE_RE = re.compile(
    r"(?:web-app|web-k8s|showpass-frontend)@[0-9a-f]{40}:"
    r"[^`#\s]+#L[1-9][0-9]*-L[1-9][0-9]*"
)
SOURCE_PARTS_RE = re.compile(
    r"(?P<repo>web-app|web-k8s|showpass-frontend)@(?P<sha>[0-9a-f]{40}):"
    r"(?P<path>[^`#\s]+)#L(?P<start>[1-9][0-9]*)-L(?P<end>[1-9][0-9]*)"
)
SOURCE_LIKE_RE = re.compile(r"(?:web-app|web-k8s|showpass-frontend)@[0-9a-f]+:[^`\s]+")
MARKER_RE = re.compile(r"\bSP-[A-Z0-9-]+\b")
SAFE_COMMAND_STATUS = "VERIFIED / NOT RUN"
UNSAFE_COMMAND_STATUS_RE = re.compile(r"^UNSAFE: \S(?:[^/\n]*\S)? / NOT RUN$")
COMMAND_ROUTES = {
    "general",
    "web-app",
    "showpass-frontend",
    "web-k8s",
    "unsafe/manual-review",
}
COMMAND_TABLE_HEADER = "| Status | Command | Route and source |"
COMMAND_TABLE_SEPARATOR = "|---|---|---|"
LEDGER_REQUIRED_KEYS = {
    "upstream_sha_checked",
    "repositories",
    "rules",
    "commands",
    "routing_cases",
}
REPOSITORY_REQUIRED_KEYS = {"sha", "root"}
RULE_REQUIRED_KEYS = {
    "id",
    "route",
    "repo",
    "path",
    "lines",
    "citation",
    "citations",
}
CITATION_REQUIRED_KEYS = {"path", "lines", "citation"}
COMMAND_REQUIRED_KEYS = {"command", "status", "safety", "route", "citation", "rule"}
ROUTING_CASE_REQUIRED_KEYS = {"name", "safety", "input", "expected"}
ROUTING_CASE_OPTIONAL_KEYS = {"rule", "repositories"}
ROUTING_CASE_ROUTES = {
    "general",
    "web-app",
    "showpass-frontend",
    "web-k8s",
    "ambiguous",
    "unsafe",
}
REGISTERED_REPOSITORY_TOKENS = (
    "web-app",
    "web-k8s",
    "showpass-frontend",
    "packages/core",
    "packages/next-app",
    "packages/mobile",
    "packages/desktop",
)


REAL_TOKEN_BOUNDARY_CHARS = "a-z0-9_.-"
REAL_TOKEN_SUFFIX_CHARS = "a-z0-9_-"
REAL_TOKEN_END = (
    r"(?=$|[^%s]|\.(?=$|[^%s]))"
    % (REAL_TOKEN_BOUNDARY_CHARS, REAL_TOKEN_SUFFIX_CHARS)
)
COMMAND_END = r"(?=$|[.!?,;:](?:\s|$))"


def _real_token_expression(expression: str) -> str:
    """Match an exact cue, allowing punctuation but rejecting dotted suffixes."""

    return r"(?<![%s])(?:%s)%s" % (
        REAL_TOKEN_BOUNDARY_CHARS,
        expression,
        REAL_TOKEN_END,
    )


def _real_token_pattern(*tokens: str) -> str:
    """Build exact cue alternatives under the shared real-token policy."""

    return "|".join(
        _real_token_expression(re.escape(token))
        for token in tokens
    )


REPOSITORY_TOKEN_PATTERNS = {
    token: _real_token_pattern(token) for token in REGISTERED_REPOSITORY_TOKENS
}
FRONTEND_BOUNDARY_SUPPORTED = (
    "Start with the `showpass-frontend` repository guidance for its web, mobile, "
    "and desktop apps built with React, Next.js, and React Native."
)
FRONTEND_BOUNDARY_UNSUPPORTED = (
    "owns React, Next.js, and React Native surfaces",
    "Do not apply Django or deployment rules",
)
FAB_COMMAND_RE = re.compile(
    _real_token_expression(r"fab\s+(?:--list|[a-z0-9_.-]+)")
)
FAB_LIST_RE = re.compile(
    _real_token_pattern("fab") + r"\s+--list" + COMMAND_END
)
FABRIC3_RE = re.compile(
    _real_token_expression(r"fabric[-\s]*3")
)
FAB_ENVIRONMENT_RE = re.compile(
    _real_token_expression(r"fab\s+(?:demo|beta|live|prod|production)")
)
FABRIC_OPERATION_CONTEXT_RE = re.compile(
    r"(?:"
    + _real_token_expression(
        r"fabric\s+(?:task|tasks|command|commands|operation|operations|"
        r"tool|tooling|environment|cluster|deployment|deployments|runner|run|job|jobs)"
    )
    + r"|"
    + _real_token_expression(r"(?:run|use|inspect|debug|test)\s+fabric")
    + r")"
)
JINJA_CUE_RE = re.compile(_real_token_pattern("jinja", "jinja2"))
KUBERNETES_CUE_RE = re.compile(_real_token_pattern("kubernetes", "k8s"))
FABFILE_CUE_RE = re.compile(_real_token_pattern("fabfile.py"))
FABRIC_ENVIRONMENT_CUES = _real_token_pattern(
    "environment",
    "cluster",
    "beta",
    "demo",
    "live",
    "prod",
    "production",
    "staging",
)
FABRIC_ENVIRONMENT_OPERATION_RE = re.compile(
    r"(?:"
    + _real_token_expression(r"(?:fabric|fab)(?:\s*3)?")
    + r"[^\n]*"
    + r"(?:" + FABRIC_ENVIRONMENT_CUES + r")"
    + r"|"
    + r"(?:" + FABRIC_ENVIRONMENT_CUES + r")"
    + r"[^\n]*"
    + _real_token_expression(r"(?:fabric|fab)(?:\s*3)?")
    + r")"
)
KUBECTL_MUTATION_RE = re.compile(
    _real_token_expression(
        r"kubectl\s+(?:apply|create|delete|patch|replace|rollout|scale|set)"
    )
)
MUTATION_TOKEN = (
    r"(?<![a-z0-9])(?:apply|change|create|delete|deploy(?:ed|ing|ment)?|"
    r"destroy|edit|flush|migrat(?:e|ed|ing|ion|ions)?|modify|mutat(?:e|ed|ing|ion|ions)?|"
    r"patch|promote|provision|publish|release|replace|reset|restart|roll(?:out|ing)?|"
    r"rolling|scale|seed|set|truncate|update|upgrade)(?![a-z0-9])"
)
ENVIRONMENT_RE = re.compile(
    _real_token_pattern(
        "beta",
        "demo",
        "live",
        "prod",
        "production",
        "staging",
        "beta.showpass.com",
        "demo.showpass.com",
        "www.showpass.com",
    )
)
MUTATION_RE = re.compile(MUTATION_TOKEN)
GCLOUD_MUTATION_RE = re.compile(
    _real_token_expression("gcloud")
    + r"[^\n]*?"
    + (
        r"(?<![a-z0-9])(?:add|apply|attach|change|connect|create|cp|delete|destroy|"
        r"detach|disable|enable|export|import|login|move|modify|mv|patch|remove|rsync|"
        r"replace|reset|resize|restore|restart|revoke|rm|rollback|set|start|stop|"
        r"suspend|resume|undelete|unset|update|upgrade)(?:-[a-z0-9-]+)?"
        r"(?![a-z0-9_-])"
    )
)
GCLOUD_EFFECTFUL_RE = re.compile(
    _real_token_expression("gcloud")
    + r"[^\n]*?(?:auth\s+(?:activate-service-account|configure-docker)|"
    + r"builds\s+(?:submit|triggers\s+run)|"
    + r"components\s+install|"
    + r"config\s+configurations\s+activate|"
    + r"container\s+fleet\s+memberships\s+register|"
    + r"dns\s+record-sets\s+transaction\s+execute|"
    + r"run\s+jobs\s+execute|scheduler\s+jobs\s+(?:run|pause)|"
    + r"workflows\s+run)(?![a-z0-9_-])"
)
DEPLOYMENT_OPERATION_RE = re.compile(
    r"(?<![a-z0-9])(?:deploy(?:ed|ing|ment)?|promote|provision|publish|release|"
    r"roll(?:out|ing)?|rolling|scale)"
    r"(?=$|[ \t\r\n,!?;:]|\.(?=$|[ \t\r\n,!?;:]))"
)
PRODUCTION_LIVE_RE = re.compile(_real_token_pattern("production", "prod", "live"))
BACKEND_CUE_RE = re.compile(
    r"(?:%s|%s)"
    % (
        REPOSITORY_TOKEN_PATTERNS["web-app"],
        _real_token_pattern(
            "showpass backend",
            "django-backed",
            "django",
            "drf",
            "orm",
            "manage.py",
            "celery",
        ),
    )
)
FRONTEND_CUE_RE = re.compile(
    r"(?:%s|%s)"
    % (
        REPOSITORY_TOKEN_PATTERNS["showpass-frontend"],
        _real_token_pattern(
            "next.js",
            "nextjs",
            "next",
            "react native",
            "react",
            "frontend",
            "client",
        ),
    )
)
FRONTEND_OWNER_CUE_RE = re.compile(
    r"(?:%s|%s)"
    % (
        REPOSITORY_TOKEN_PATTERNS["showpass-frontend"],
        _real_token_pattern(
            "next.js",
            "nextjs",
            "next",
            "react native",
            "react",
        ),
    )
)
FRONTEND_REPOSITORY_CUE_RE = re.compile(
    "|".join(
        REPOSITORY_TOKEN_PATTERNS[token]
        for token in REGISTERED_REPOSITORY_TOKENS
        if token not in {"web-app", "web-k8s"}
    )
)
INFRASTRUCTURE_REPOSITORY_CUE_RE = re.compile(REPOSITORY_TOKEN_PATTERNS["web-k8s"])
SHOWPASS_PRODUCT_CUE_RE = re.compile(_real_token_pattern("showpass"))
SHOWPASS_UI_CUE_RE = re.compile(
    _real_token_pattern(
        "showpass web",
        "showpass mobile",
        "showpass desktop",
        "showpass frontend",
        "showpass ui",
        "showpass client",
    )
)
MODERN_UI_WORK_RE = re.compile(
    r"(?:"
    + _real_token_pattern(
        "component",
        "page",
        "screen",
        "hook",
        "widget",
        "modal",
        "form",
        "button",
        "layout",
        "style",
        "styles",
        "styling",
        "css",
        "tsx",
        "user interface",
        "render",
        "renderer",
        "rendering",
    )
    + r"|"
    + _real_token_expression(
        r"(?:modern\s+(?:ui|frontend|client)|"
        r"(?:react|next(?:\.js|js)?)\s+(?:ui|frontend|client))"
    )
    + r")"
)
LEGACY_UI_CUE_RE = re.compile(
    r"(?:"
    + _real_token_expression(
        r"next(?:\.js|js)?\s+(?:bridge|redirect|bridge[-\s]?route)"
    )
    + r"|"
    + _real_token_expression(
        r"(?:bridge[-\s]?route|bridge|redirect)\s+(?:for|in|from|to)\s+"
        r"next(?:\.js|js)?"
    )
    + r"|"
    + _real_token_pattern(
        "bridge-route",
        "bridge",
        "redirect",
        "angularjs",
        "angular js",
        "legacy ui",
        "legacy frontend",
        "legacy template",
        "legacy bridge",
        "legacy redirect",
        "ui bridge",
        "frontend bridge",
        "bridge template",
        "django template",
        "django redirect",
        "apps/main/static",
        "apps/main/templates",
        "gulp",
        "embedded page",
    )
    + r")"
)


NEGATIVE_STATUS_CASES = (
    ("missing-not-run", 0, "safe", "VERIFIED"),
    ("not-run-only", 0, "safe", "NOT RUN"),
    ("execution-claim", 0, "safe", "VERIFIED / RUN"),
    ("unsafe-marked-run", 6, "unsafe", "UNSAFE: unlabelled broad Django suite / RUN"),
    ("safe-marked-unsafe", 0, "safe", "UNSAFE: incorrect classification / NOT RUN"),
    ("unsafe-marked-verified", 6, "unsafe", SAFE_COMMAND_STATUS),
)

ROUTING_GUARD_CASES = (
    ("fabric3", "Review a Fabric3 task", "unsafe"),
    ("fabric-3", "Review a Fabric 3 task", "unsafe"),
    ("generic-production-operation", "Run a production operation", "unsafe"),
    ("generic-live-environment", "Review the live environment", "unsafe"),
    ("generic-production", "production", "unsafe"),
    ("generic-live", "live", "unsafe"),
    ("nested-fabric-beta-rollout", "fab beta web.frontend.deploy_nextjs", "unsafe"),
    ("nested-fabric-live-rollout", "fab live web.quick_rolling_update", "unsafe"),
    ("production-deployment-wording", "Run a production deployment", "unsafe"),
    ("beta-deployment-wording", "Deploy to beta", "unsafe"),
    ("unrecognized-deployment-wording", "Review an unrecognized deployment operation", "unsafe"),
    (
        "mixed-django-nextjs",
        "Review a Django-backed Next.js API route",
        "ambiguous",
    ),
    (
        "mixed-django-client",
        "Change a web-app Django view and its Next.js client",
        "ambiguous",
    ),
    (
        "legacy-ui-bridge",
        "Fix the legacy UI bridge",
        "web-app",
    ),
    (
        "angularjs-bridge",
        "Fix the AngularJS bridge",
        "web-app",
    ),
    (
        "mixed-angularjs-react",
        "Fix the AngularJS bridge and update the React component",
        "ambiguous",
    ),
    ("backend-repository-suffix", "Change web-app-extra", "general"),
    ("backend-repository-underscore-suffix", "Change web-app_extra", "general"),
    ("infrastructure-repository-suffix", "Change web-k8s-extra", "general"),
    ("infrastructure-repository-underscore-suffix", "Change web-k8s_extra", "general"),
    ("backend-framework-suffix", "Change django-extra", "general"),
    ("backend-phrase-suffix", "Review the Showpass backend-extra", "general"),
    (
        "infrastructure-fabricated-word",
        "Review a fabricated operation in beta",
        "general",
    ),
    (
        "infrastructure-fabrication-word",
        "Review a fabrication operation in beta",
        "general",
    ),
    (
        "infrastructure-manufactured-word",
        "Review a manufactured operation in beta",
        "general",
    ),
    (
        "natural-language-fabric-noun",
        "Discuss the fabric of the application",
        "general",
    ),
    ("bare-fabric-noun", "Review fabric", "general"),
    ("infrastructure-kubectl-suffix", "Review a kubectl-extra parser", "general"),
    ("infrastructure-gcloud-suffix", "Review a gcloud-extra parser", "general"),
    ("infrastructure-jinja-suffix", "Review a Jinja-extra template", "general"),
    ("infrastructure-kubernetes-suffix", "Review a Kubernetes-extra manifest", "general"),
    ("infrastructure-fabfile-suffix", "Review fabfile.py.bak", "general"),
    (
        "showpass-nextjs-dotted-suffix",
        "Change a Showpass Next.jsx component",
        "general",
    ),
    (
        "showpass-react-dotted-suffix",
        "Change a Showpass React.jsx component",
        "general",
    ),
    ("frontend-package-file-suffix", "Change packages/core.ts", "general"),
    ("frontend-package-dotted-suffix", "Change packages/core.extra", "general"),
)


def _has_infrastructure_cue(text: str) -> bool:
    return (
        bool(INFRASTRUCTURE_REPOSITORY_CUE_RE.search(text))
        or bool(re.search(_real_token_pattern("kubectl", "gcloud"), text))
        or bool(FAB_COMMAND_RE.search(text))
        or bool(FABRIC3_RE.search(text))
        or bool(FABRIC_OPERATION_CONTEXT_RE.search(text))
        or bool(JINJA_CUE_RE.search(text))
        or bool(KUBERNETES_CUE_RE.search(text))
        or bool(FABFILE_CUE_RE.search(text))
    )


def _production_live_requires_review(text: str) -> bool:
    return bool(PRODUCTION_LIVE_RE.search(text))


def repositories_for_task(task: str) -> Tuple[str, ...]:
    """Return the repository owners signaled by a short task cue."""

    text = task.lower()
    infrastructure = _has_infrastructure_cue(text)
    backend_cue = bool(BACKEND_CUE_RE.search(text))
    frontend_cue = bool(FRONTEND_CUE_RE.search(text))
    modern_frontend_cue = bool(FRONTEND_OWNER_CUE_RE.search(text))
    showpass_ui_cue = bool(SHOWPASS_UI_CUE_RE.search(text))
    explicit_repository_cue = bool(FRONTEND_REPOSITORY_CUE_RE.search(text))
    showpass_modern_cue = bool(
        SHOWPASS_PRODUCT_CUE_RE.search(text)
        and (modern_frontend_cue or showpass_ui_cue)
    )
    legacy_ui_cue = bool(LEGACY_UI_CUE_RE.search(text))
    modern_ui_work = bool(MODERN_UI_WORK_RE.search(text))

    repositories: List[str] = []
    if legacy_ui_cue:
        repositories.append("web-app")
        if modern_ui_work or explicit_repository_cue or showpass_ui_cue:
            repositories.append("showpass-frontend")
    elif backend_cue:
        repositories.append("web-app")
        if frontend_cue:
            repositories.append("showpass-frontend")
    elif explicit_repository_cue or showpass_modern_cue:
        repositories.append("showpass-frontend")
    if infrastructure:
        repositories.append("web-k8s")
    return tuple(dict.fromkeys(repositories))


def _is_documented_read_only_fab_list(text: str) -> bool:
    return bool(re.fullmatch(r"\s*fab\s+--list[.!?,;:]*\s*", text))


def _has_fabric_mutation(text: str) -> bool:
    """Treat Fabric mutations as unsafe only in a command/tooling context."""

    for line in text.splitlines() or [text]:
        if not MUTATION_RE.search(line):
            continue
        if FAB_COMMAND_RE.search(line) or FABRIC_OPERATION_CONTEXT_RE.search(line):
            return True
    return False


def route_for_task(task: str) -> str:
    """Return a safe repository route for a short task cue."""

    text = task.lower()
    infrastructure = _has_infrastructure_cue(text)
    cluster_mutation = bool(
        FABRIC3_RE.search(text)
        or FAB_ENVIRONMENT_RE.search(text)
        or _has_fabric_mutation(text)
        or KUBECTL_MUTATION_RE.search(text)
        or GCLOUD_MUTATION_RE.search(text)
        or GCLOUD_EFFECTFUL_RE.search(text)
    )
    environment_mutation = bool(ENVIRONMENT_RE.search(text) and MUTATION_RE.search(text))
    deployment_mutation = bool(DEPLOYMENT_OPERATION_RE.search(text))
    natural_fabric_environment_operation = bool(
        FABRIC_ENVIRONMENT_OPERATION_RE.search(text)
        and FABRIC_OPERATION_CONTEXT_RE.search(text)
    )
    if (
        cluster_mutation
        or environment_mutation
        or deployment_mutation
        or _production_live_requires_review(text)
        or (
            natural_fabric_environment_operation
            and not _is_documented_read_only_fab_list(text)
        )
    ):
        return "unsafe"

    repositories = repositories_for_task(text)
    if len(repositories) > 1:
        return "ambiguous"
    if repositories:
        return repositories[0]
    if FAB_LIST_RE.search(text) or infrastructure:
        return "web-k8s"
    return "general"


def load_ledger() -> Any:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def _record_key_errors(
    context: str,
    record: Any,
    required: set,
    optional: Optional[set] = None,
) -> List[str]:
    """Validate an object boundary without assuming JSON supplied a mapping."""

    if not isinstance(record, dict):
        return ["%s must be an object" % context]

    allowed = required | (optional or set())
    errors: List[str] = []
    for field in sorted(required - set(record), key=repr):
        errors.append("%s is missing required field: %s" % (context, field))
    for field in sorted(set(record) - allowed, key=repr):
        errors.append("%s contains unknown field: %r" % (context, field))
    return errors


def _required_string_error(context: str, field: str, value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return "%s.%s must be a non-empty string" % (context, field)
    if not value.strip():
        return "%s.%s must be a non-empty string" % (context, field)
    return None


def _validate_citation_record(context: str, citation: Any) -> List[str]:
    errors = _record_key_errors(context, citation, CITATION_REQUIRED_KEYS)
    if not isinstance(citation, dict):
        return errors
    for field in CITATION_REQUIRED_KEYS:
        error = _required_string_error(context, field, citation.get(field))
        if error:
            errors.append(error)
    lines = citation.get("lines")
    if isinstance(lines, str):
        match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", lines)
        if match is None:
            errors.append("%s.lines must use START-END line syntax" % context)
        elif int(match.group(1)) > int(match.group(2)):
            errors.append("%s.lines must not be reversed" % context)
    return errors


def _validate_repository_records(ledger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    repositories = ledger.get("repositories")
    if not isinstance(repositories, dict):
        return errors

    expected_names = set(EXPECTED_REPOSITORIES)
    actual_names = set(repositories)
    for missing in sorted(expected_names - actual_names):
        errors.append("source ledger is missing repository metadata: %s" % missing)
    for unexpected in sorted(actual_names - expected_names, key=repr):
        errors.append("source ledger contains unknown repository metadata: %r" % unexpected)

    for repository, expected in EXPECTED_REPOSITORIES.items():
        metadata = repositories.get(repository)
        context = "repository metadata %s" % repository
        errors.extend(_record_key_errors(context, metadata, REPOSITORY_REQUIRED_KEYS))
        if not isinstance(metadata, dict):
            continue
        for field in REPOSITORY_REQUIRED_KEYS:
            error = _required_string_error(context, field, metadata.get(field))
            if error:
                errors.append(error)
        for field in ("sha", "root"):
            if metadata.get(field) != expected[field]:
                errors.append(
                    "source ledger %s %s does not match the pinned evidence"
                    % (repository, field)
                )
        root = metadata.get("root")
        if (
            not isinstance(root, str)
            or not root
            or root.startswith("/")
            or ".." in Path(root).parts
        ):
            errors.append("source ledger repository root is unsafe: %s" % repository)
        sha = metadata.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            errors.append("source ledger repository SHA is invalid: %s" % repository)
    return errors


def _validate_rule_records(ledger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    rules = ledger.get("rules")
    if not isinstance(rules, list):
        return errors

    expected_rule_ids = set(EXPECTED_RULE_REPOSITORIES)
    seen_ids = set()
    for index, rule in enumerate(rules):
        context = "source rule %d" % (index + 1)
        errors.extend(_record_key_errors(context, rule, RULE_REQUIRED_KEYS))
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("id")
        id_error = _required_string_error(context, "id", rule_id)
        if id_error:
            errors.append(id_error)
            continue
        if rule_id in seen_ids:
            errors.append("duplicate source rule: %s" % rule_id)
        seen_ids.add(rule_id)
        if rule_id not in expected_rule_ids:
            errors.append("source ledger contains unknown source rule: %s" % rule_id)

        expected_repository = EXPECTED_RULE_REPOSITORIES.get(rule_id)
        expected_route = EXPECTED_RULE_ROUTES.get(rule_id)
        repo = rule.get("repo")
        route = rule.get("route")
        if not isinstance(repo, str) or repo not in EXPECTED_REPOSITORIES:
            errors.append("%s.repo has an unknown repository: %r" % (context, repo))
        elif expected_repository is not None and repo != expected_repository:
            errors.append("%s has unexpected repository ownership" % rule_id)
        if not isinstance(route, str) or route not in COMMAND_ROUTES:
            errors.append("%s.route has an unknown route: %r" % (context, route))
        elif expected_route is not None and route != expected_route:
            errors.append("%s has an unexpected route" % rule_id)

        path_error = _required_string_error(context, "path", rule.get("path"))
        if path_error:
            errors.append(path_error)
        lines_error = _required_string_error(context, "lines", rule.get("lines"))
        if lines_error:
            errors.append(lines_error)
        citation_error = _required_string_error(context, "citation", rule.get("citation"))
        if citation_error:
            errors.append(citation_error)

        citations = rule.get("citations")
        if not isinstance(citations, list) or not citations:
            errors.append("%s must contain a non-empty citation register" % rule_id)
            continue
        seen_citations = set()
        for citation_index, citation in enumerate(citations):
            citation_context = "%s citation %d" % (context, citation_index + 1)
            errors.extend(_validate_citation_record(citation_context, citation))
            if not isinstance(citation, dict):
                continue
            citation_value = citation.get("citation")
            if isinstance(citation_value, str):
                if citation_value in seen_citations:
                    errors.append("%s contains a duplicate citation" % context)
                seen_citations.add(citation_value)
        primary = citations[0]
        if isinstance(primary, dict):
            for field in ("path", "lines", "citation"):
                if rule.get(field) != primary.get(field):
                    errors.append(
                        "%s primary %s disagrees with its citation register"
                        % (rule_id, field)
                    )

    for missing in sorted(expected_rule_ids - seen_ids):
        errors.append("source ledger is missing source rule: %s" % missing)
    return errors


def _validate_command_records(ledger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    commands = ledger.get("commands")
    if not isinstance(commands, list):
        return errors

    seen_commands = set()
    known_rules = set(EXPECTED_RULE_REPOSITORIES)
    for index, command in enumerate(commands):
        context = "command %d" % (index + 1)
        errors.extend(_record_key_errors(context, command, COMMAND_REQUIRED_KEYS))
        if not isinstance(command, dict):
            continue
        value = command.get("command")
        for field in ("command", "status", "safety", "route", "citation", "rule"):
            error = _required_string_error(context, field, command.get(field))
            if error:
                errors.append(error)
        if isinstance(value, str):
            if value in seen_commands:
                errors.append("source ledger contains duplicate command: %s" % value)
            seen_commands.add(value)

        safety = command.get("safety")
        status = command.get("status")
        if safety not in ("safe", "unsafe"):
            errors.append("command has unknown safety classification: %s" % value)
        elif isinstance(status, str):
            if safety == "safe" and status != SAFE_COMMAND_STATUS:
                errors.append("safe command must use %r: %s" % (SAFE_COMMAND_STATUS, value))
            if safety == "unsafe" and UNSAFE_COMMAND_STATUS_RE.fullmatch(status) is None:
                errors.append(
                    "unsafe command must use 'UNSAFE: <reason> / NOT RUN': %s" % value
                )
        route = command.get("route")
        if not isinstance(route, str) or route not in COMMAND_ROUTES:
            errors.append("command has unknown route %r: %s" % (route, value))
        rule = command.get("rule")
        if not isinstance(rule, str) or rule not in known_rules:
            errors.append("command references unknown rule: %s" % value)
        citation = command.get("citation")
        if isinstance(citation, str) and SOURCE_PARTS_RE.fullmatch(citation) is None:
            errors.append("command has an invalid source citation: %s" % value)
    return errors


def _validate_routing_case_records(ledger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    cases = ledger.get("routing_cases")
    if not isinstance(cases, list):
        return errors

    known_rules = set(EXPECTED_RULE_REPOSITORIES)
    seen_names = set()
    for index, case in enumerate(cases):
        context = "routing case %d" % (index + 1)
        errors.extend(
            _record_key_errors(
                context,
                case,
                ROUTING_CASE_REQUIRED_KEYS,
                ROUTING_CASE_OPTIONAL_KEYS,
            )
        )
        if not isinstance(case, dict):
            continue
        name = case.get("name")
        for field in ("name", "safety", "input", "expected"):
            error = _required_string_error(context, field, case.get(field))
            if error:
                errors.append(error)
        if isinstance(name, str):
            if name in seen_names:
                errors.append("source ledger contains duplicate routing case: %s" % name)
            seen_names.add(name)
        safety = case.get("safety")
        if safety not in ("safe", "unsafe"):
            errors.append("%s.safety has an unknown value: %r" % (context, safety))
        expected = case.get("expected")
        if not isinstance(expected, str) or expected not in ROUTING_CASE_ROUTES:
            errors.append("%s.expected has an unknown route: %r" % (context, expected))
        elif safety == "unsafe" and expected != "unsafe":
            errors.append("%s unsafe cases must expect unsafe" % context)
        elif safety == "safe" and expected == "unsafe":
            errors.append("%s safe cases cannot expect unsafe" % context)

        expected_repositories = (
            repositories_for_task(case["input"])
            if isinstance(case.get("input"), str)
            else ()
        )
        if expected_repositories and "repositories" not in case:
            errors.append(
                "%s.repositories is required for task owners: expected %r"
                % (context, expected_repositories)
            )

        if "rule" in case:
            rule = case.get("rule")
            if not isinstance(rule, str) or not rule.strip():
                errors.append("%s.rule must be a non-empty source rule id" % context)
            elif rule not in known_rules:
                errors.append("%s references unknown rule: %s" % (context, rule))
            elif rule == "SP-K8S-SAFETY":
                if safety == "safe" and expected_repositories and "web-k8s" not in expected_repositories:
                    errors.append(
                        "%s.rule is incompatible with task owners: expected one of %r, got %r"
                        % (context, expected_repositories, EXPECTED_RULE_REPOSITORIES[rule])
                    )
            elif not expected_repositories:
                errors.append("%s.rule requires a task owner" % context)
            elif EXPECTED_RULE_REPOSITORIES[rule] not in expected_repositories:
                errors.append(
                    "%s.rule is incompatible with task owners: expected one of %r, got %r"
                    % (context, expected_repositories, EXPECTED_RULE_REPOSITORIES[rule])
                )
        elif safety == "unsafe":
            errors.append("%s unsafe cases must include a source rule" % context)
        if "repositories" in case:
            repositories = case.get("repositories")
            if not isinstance(repositories, list):
                errors.append("%s.repositories must be a list" % context)
            else:
                seen_repositories = set()
                for repository in repositories:
                    if not isinstance(repository, str) or repository not in EXPECTED_REPOSITORIES:
                        errors.append(
                            "%s.repositories contains an unknown repository: %r"
                            % (context, repository)
                        )
                    elif repository in seen_repositories:
                        errors.append(
                            "%s.repositories contains a duplicate repository: %s"
                            % (context, repository)
                        )
                    if isinstance(repository, str) and repository in EXPECTED_REPOSITORIES:
                        seen_repositories.add(repository)
                if tuple(repositories) != expected_repositories:
                    errors.append(
                        "%s.repositories does not match the task owners: expected %r, got %r"
                        % (context, expected_repositories, tuple(repositories))
                    )
    return errors


def _validate_ledger_schema(ledger: Any) -> List[str]:
    """Validate the complete JSON ledger before any consumer indexes it."""

    if not isinstance(ledger, dict):
        return ["source ledger must be an object"]

    errors = _record_key_errors("source ledger", ledger, LEDGER_REQUIRED_KEYS)
    upstream_sha = ledger.get("upstream_sha_checked")
    upstream_error = _required_string_error(
        "source ledger", "upstream_sha_checked", upstream_sha
    )
    if upstream_error:
        errors.append(upstream_error)
    elif upstream_sha != EXPECTED_UPSTREAM_SHA:
        errors.append("source ledger has an unexpected upstream SHA")

    repositories = ledger.get("repositories")
    if not isinstance(repositories, dict):
        errors.append("source ledger repositories must be an object")
    else:
        errors.extend(_validate_repository_records(ledger))

    for field, label in (
        ("rules", "rules"),
        ("commands", "commands"),
        ("routing_cases", "routing cases"),
    ):
        value = ledger.get(field)
        if not isinstance(value, list):
            errors.append("source ledger %s must be a list" % label)
        elif not value:
            errors.append("source ledger %s must be non-empty" % label)

    if isinstance(ledger.get("rules"), list):
        errors.extend(_validate_rule_records(ledger))
    if isinstance(ledger.get("commands"), list):
        errors.extend(_validate_command_records(ledger))
    if isinstance(ledger.get("routing_cases"), list):
        errors.extend(_validate_routing_case_records(ledger))
    return errors


def _validate_ledger_metadata(ledger: Any) -> List[str]:
    """Compatibility wrapper for callers that used the old metadata validator."""

    return _validate_ledger_schema(ledger)


def _validate_local_references(skill_text: str) -> List[str]:
    errors: List[str] = []
    for reference, target in LOCAL_REFERENCE_TARGETS.items():
        if reference not in skill_text:
            errors.append("SKILL.md is missing local reference %s" % reference)
        if not target.is_file():
            errors.append("local reference target does not exist: %s" % reference)
    return errors


def _citation_entries(rule: Dict[str, Any]) -> List[Dict[str, str]]:
    if not isinstance(rule, dict):
        return []
    entries = rule.get("citations")
    if entries is None:
        return [{
            "citation": rule.get("citation", ""),
            "path": rule.get("path", ""),
            "lines": rule.get("lines", ""),
        }]
    if not isinstance(entries, list):
        return []
    return entries


def _validate_citation(
    rule: Dict[str, Any],
    entry: Dict[str, Any],
    repositories: Dict[str, Any],
) -> List[str]:
    errors: List[str] = []
    rule_id = rule.get("id", "<unknown rule>")
    repo = rule.get("repo")
    citation = entry.get("citation", "")
    match = SOURCE_PARTS_RE.fullmatch(citation) if isinstance(citation, str) else None
    if match is None:
        errors.append("%s has an invalid source citation: %s" % (rule_id, citation))
        return errors

    source = repositories.get(repo, {}) if isinstance(repo, str) else {}
    if not isinstance(repositories, dict) or not isinstance(repo, str) or repo not in repositories:
        errors.append("%s references unknown repository %r" % (rule_id, repo))
    if match.group("repo") != repo:
        errors.append("%s citation repository does not match its rule: %s" % (rule_id, citation))
    if not isinstance(source, dict):
        source = {}
    source_sha = source.get("sha", "")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        errors.append("%s has an invalid repository SHA" % rule_id)
    if source_sha != match.group("sha"):
        errors.append("%s citation does not carry the repository SHA: %s" % (rule_id, citation))

    path = entry.get("path", "")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts:
        errors.append("%s has an unsafe source path" % rule_id)
    root = source.get("root", "")
    expected_citation_path = "%s/%s" % (root, path)
    if match.group("path") != expected_citation_path:
        errors.append("%s citation path does not match its ledger path: %s" % (rule_id, citation))

    lines = entry.get("lines", "")
    expected_lines = "%s-%s" % (match.group("start"), match.group("end"))
    if lines != expected_lines:
        errors.append("%s citation line range does not match its ledger range: %s" % (rule_id, citation))
    if int(match.group("start")) > int(match.group("end")):
        errors.append("%s citation line range is reversed: %s" % (rule_id, citation))
    return errors


def _registered_citations(
    ledger: Any,
    skill_text: str,
    errors: List[str],
) -> Dict[str, str]:
    if not isinstance(ledger, dict):
        return {}
    repositories = ledger.get("repositories", {})
    if not isinstance(repositories, dict):
        repositories = {}
    registered: Dict[str, str] = {}
    rule_ids = set()
    rules = ledger.get("rules", [])
    if not isinstance(rules, list):
        return registered
    for rule in rules:
        if not isinstance(rule, dict):
            errors.append("source ledger contains a malformed source rule")
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str):
            errors.append("source rule is missing a string id")
            continue
        if rule_id in rule_ids:
            errors.append("duplicate source rule: %s" % rule_id)
        rule_ids.add(rule_id)
        if not rule_id:
            errors.append("source rule is missing an id")
        if rule_id not in skill_text:
            errors.append("%s is missing from SKILL.md" % rule_id)

        entries = _citation_entries(rule)
        if not entries:
            errors.append("%s has no registered citations" % rule_id)
        for entry in entries:
            if not isinstance(entry, dict):
                errors.append("%s has a malformed citation entry" % rule_id)
                continue
            errors.extend(_validate_citation(rule, entry, repositories))
            citation = entry.get("citation", "")
            if not isinstance(citation, str) or not SOURCE_PARTS_RE.fullmatch(citation):
                continue
            previous = registered.get(citation)
            if previous is not None and previous != rule_id:
                errors.append("source citation is registered for multiple rules: %s" % citation)
            registered[citation] = rule_id
            if citation not in skill_text:
                errors.append("%s citation is missing from SKILL.md" % rule_id)

        legacy_fields = (rule.get("citation"), rule.get("path"), rule.get("lines"))
        if any(field is not None for field in legacy_fields):
            first = entries[0] if entries and isinstance(entries[0], dict) else {}
            if any(first.get(key) != value for key, value in zip(("citation", "path", "lines"), legacy_fields)):
                errors.append("%s legacy citation fields do not match its citation register" % rule_id)

    source_citations = {
        match.group(0) for match in SOURCE_RE.finditer(skill_text)
    }
    for citation in source_citations - set(registered):
        errors.append("SKILL.md contains an unregistered source citation %s" % citation)
    return registered


def _validate_command_status(command: Dict[str, Any]) -> Optional[str]:
    value = command.get("command", "")
    status = command.get("status", "")
    safety = command.get("safety")
    if safety == "safe" and status != SAFE_COMMAND_STATUS:
        return "safe command must use %r: %s" % (SAFE_COMMAND_STATUS, value)
    if safety == "unsafe" and (
        not isinstance(status, str) or UNSAFE_COMMAND_STATUS_RE.fullmatch(status) is None
    ):
        return "unsafe command must use 'UNSAFE: <reason> / NOT RUN': %s" % value
    if safety not in ("safe", "unsafe"):
        return "command has unknown safety classification: %s" % value
    return None


def _inline_code_cell(cell: str, field: str, errors: List[str]) -> Optional[str]:
    value = cell.strip()
    if len(value) < 2 or not value.startswith("`") or not value.endswith("`"):
        errors.append("command table %s cell is not inline code: %s" % (field, cell))
        return None
    return value[1:-1]


def _command_table_rows(skill_text: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """Parse the command table so its claims can be checked against the ledger."""

    errors: List[str] = []
    lines = skill_text.splitlines()
    headers = [index for index, line in enumerate(lines) if line.strip() == COMMAND_TABLE_HEADER]
    if len(headers) != 1:
        errors.append("command table must have exactly one header")
        return [], errors

    header_index = headers[0]
    if header_index + 1 >= len(lines) or lines[header_index + 1].strip() != COMMAND_TABLE_SEPARATOR:
        errors.append("command table has an invalid separator")
        return [], errors

    rows: List[Dict[str, str]] = []
    for line_number, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        if not line.strip():
            break
        if not line.lstrip().startswith("|"):
            break
        cells = line.strip()[1:-1].split("|") if line.strip().endswith("|") else []
        if len(cells) != 3:
            errors.append("command table row %d must have three cells" % line_number)
            continue

        status = _inline_code_cell(cells[0], "status", errors)
        command = _inline_code_cell(cells[1], "command", errors)
        route_source = re.fullmatch(
            r"`(?P<route>[^`]+)`\s*;\s*source:\s*`(?P<citation>[^`]+)`",
            cells[2].strip(),
        )
        if route_source is None:
            errors.append("command table route/source cell is malformed on row %d" % line_number)
            continue
        if status is None or command is None:
            continue
        rows.append({
            "status": status,
            "command": command,
            "route": route_source.group("route"),
            "citation": route_source.group("citation"),
        })
    return rows, errors


def _validate_command_table(
    skill_text: str,
    ledger: Any,
    rule_ids: set,
    registered_citations: Dict[str, str],
) -> List[str]:
    errors: List[str] = []
    rules = ledger.get("rules", []) if isinstance(ledger, dict) else []
    rules_by_id = {
        rule.get("id"): rule
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    } if isinstance(rules, list) else {}
    rows, parse_errors = _command_table_rows(skill_text)
    errors.extend(parse_errors)

    rows_by_command: Dict[str, Dict[str, str]] = {}
    for row in rows:
        command = row["command"]
        if command in rows_by_command:
            errors.append("command table contains duplicate command: %s" % command)
        rows_by_command[command] = row

    ledger_commands = ledger.get("commands", []) if isinstance(ledger, dict) else []
    if not isinstance(ledger_commands, list):
        return errors
    ledger_command_names = set()
    for index, command in enumerate(ledger_commands):
        if not isinstance(command, dict):
            errors.append("command %d must be an object" % (index + 1))
            continue
        value = command.get("command", "")
        if isinstance(value, str) and value in ledger_command_names:
            errors.append("source ledger contains duplicate command: %s" % value)
        if isinstance(value, str):
            ledger_command_names.add(value)

        status_error = _validate_command_status(command)
        if status_error:
            errors.append(status_error)
        rule = command.get("rule")
        if not isinstance(rule, str) or rule not in rule_ids:
            errors.append("command references unknown rule: %s" % value)

        route = command.get("route")
        if not isinstance(route, str) or route not in COMMAND_ROUTES:
            errors.append("command has unknown route %r: %s" % (route, value))
        source_rule = rules_by_id.get(rule, {}) if isinstance(rule, str) else {}
        source_route = source_rule.get("route") if isinstance(source_rule, dict) else None
        if (
            isinstance(source_route, str)
            and source_route in COMMAND_ROUTES
            and route != source_route
        ):
            errors.append("command route does not match its source rule: %s" % value)
        citation = command.get("citation")
        if not isinstance(citation, str) or not citation:
            errors.append("command is missing a source citation: %s" % value)
        elif registered_citations.get(citation) != rule:
            errors.append("command citation is not registered for its rule: %s" % value)

        row = rows_by_command.get(value) if isinstance(value, str) else None
        if row is None:
            errors.append("command is missing from SKILL.md command table: %s" % value)
            continue
        if row["status"] != command.get("status"):
            errors.append("command status claim does not match the ledger: %s" % value)
        if row["route"] != route:
            errors.append("command route claim does not match the ledger: %s" % value)
        if row["citation"] != citation:
            errors.append("command source claim does not match the ledger: %s" % value)

    for command in rows_by_command:
        if command not in ledger_command_names:
            errors.append("SKILL.md command table contains an unregistered command: %s" % command)
    return errors


def _validate_frontend_boundary(skill_text: str) -> List[str]:
    """Keep the frontend citation limited to the claims in AGENTS.md#L3-L7."""

    errors: List[str] = []
    boundary_lines = [
        line for line in skill_text.splitlines() if "[SP-FRONTEND-BOUNDARY]" in line
    ]
    if len(boundary_lines) != 1:
        errors.append("SP-FRONTEND-BOUNDARY must have exactly one routing-table row")
        return errors

    boundary_line = boundary_lines[0]
    if FRONTEND_BOUNDARY_SUPPORTED not in boundary_line:
        errors.append(
            "SP-FRONTEND-BOUNDARY must use the claim supported by showpass-frontend/AGENTS.md#L3-L7"
        )
    for unsupported in FRONTEND_BOUNDARY_UNSUPPORTED:
        if unsupported in boundary_line:
            errors.append(
                "SP-FRONTEND-BOUNDARY overclaims beyond showpass-frontend/AGENTS.md#L3-L7: %s"
                % unsupported
            )
    return errors


def check_documents(skill_text: str, ledger: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    if not skill_text.startswith("---\n") or "\n---\n" not in skill_text[4:]:
        errors.append("SKILL.md has invalid frontmatter delimiters")
    else:
        frontmatter = skill_text[4 : skill_text.index("\n---\n", 4)]
        errors.extend(
            "SKILL.md frontmatter %s" % error
            for error in required_frontmatter_errors(frontmatter)
        )

    for line_number, line in enumerate(skill_text.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            errors.append("SKILL.md line %d has trailing whitespace" % line_number)
    if "\r" in skill_text:
        errors.append("SKILL.md contains carriage returns")

    rules = ledger.get("rules", []) if isinstance(ledger, dict) else []
    rule_ids = {
        rule.get("id")
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    } if isinstance(rules, list) else set()
    errors.extend(_validate_ledger_schema(ledger))
    errors.extend(_validate_local_references(skill_text))
    registered_citations = _registered_citations(ledger, skill_text, errors)
    errors.extend(_validate_frontend_boundary(skill_text))

    cases = ledger.get("routing_cases", []) if isinstance(ledger, dict) else []
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, dict):
            continue
        rule = case.get("rule")
        if case.get("expected") == "unsafe" and not rule:
            errors.append("unsafe routing case is missing a source rule: %s" % case.get("name"))
        elif rule is not None and (not isinstance(rule, str) or rule not in rule_ids):
            errors.append("routing case references unknown rule: %s" % case.get("name"))

    for marker in set(MARKER_RE.findall(skill_text)):
        if marker not in rule_ids:
            errors.append("SKILL.md contains an unregistered source marker %s" % marker)

    errors.extend(_validate_command_table(skill_text, ledger, rule_ids, registered_citations))

    if not SOURCE_RE.search(skill_text):
        errors.append("SKILL.md has no source citations")
    for source_like in SOURCE_LIKE_RE.findall(skill_text):
        if not SOURCE_PARTS_RE.fullmatch(source_like):
            errors.append("SKILL.md contains a malformed source citation %s" % source_like)

    return errors


def check_scenarios(ledger: Any, safety: Optional[str]) -> List[str]:
    if not isinstance(ledger, dict):
        return ["source ledger must be an object"]
    schema_errors = _validate_ledger_schema(ledger)
    if schema_errors:
        return schema_errors
    cases_value = ledger.get("routing_cases", [])
    if not isinstance(cases_value, list):
        return ["source ledger routing cases must be a list"]

    errors: List[str] = []
    cases: Iterable[Dict[str, str]] = cases_value
    if safety is not None:
        cases = [case for case in cases if case.get("safety") == safety]
    for case in cases:
        actual = route_for_task(case["input"])
        if actual != case["expected"]:
            errors.append("%s routed to %s, expected %s" % (case["name"], actual, case["expected"]))
        expected_repositories = case.get("repositories")
        if expected_repositories is not None:
            actual_repositories = repositories_for_task(case["input"])
            if actual_repositories != tuple(expected_repositories):
                errors.append(
                    "%s resolved to repositories %s, expected %s"
                    % (case["name"], actual_repositories, tuple(expected_repositories))
                )
    return errors


def check_negative_cases(skill_text: str, ledger: Dict[str, Any]) -> List[str]:
    """Prove that stale document claims and source-register drift fail closed."""

    schema_errors = _validate_ledger_schema(ledger)
    if schema_errors:
        return ["cannot run negative cases against an invalid source ledger"] + schema_errors

    errors: List[str] = []
    for name, task, expected in ROUTING_GUARD_CASES:
        actual = route_for_task(task)
        if actual != expected:
            errors.append("routing guard %s returned %s, expected %s" % (name, actual, expected))

    for name, index, safety, status in NEGATIVE_STATUS_CASES:
        candidate = copy.deepcopy(ledger)
        command = candidate["commands"][index]
        command["safety"] = safety
        command["status"] = status
        if not check_documents(skill_text, candidate):
            errors.append("negative case was accepted: %s" % name)

    first_command = ledger["commands"][0]
    first_row = (
        "| `{status}` | `{command}` | `{route}`; source: `{citation}` |".format(
            **first_command
        )
    )
    stale_status_row = first_row.replace(
        "`%s`" % first_command["status"], "`UNSAFE: stale status / NOT RUN`", 1
    )
    stale_status = skill_text.replace(first_row, stale_status_row, 1)
    if stale_status == skill_text or not check_documents(stale_status, ledger):
        errors.append("negative case was accepted: stale-command-status-claim")

    stale_route_row = first_row.replace("`%s`" % first_command["route"], "`web-k8s`", 1)
    stale_route = skill_text.replace(first_row, stale_route_row, 1)
    if stale_route == skill_text or not check_documents(stale_route, ledger):
        errors.append("negative case was accepted: stale-command-route-claim")

    stale_citation = ledger["rules"][1]["citations"][0]["citation"]
    stale_source_row = first_row.replace(
        first_command["citation"], stale_citation, 1
    )
    stale_source = skill_text.replace(first_row, stale_source_row, 1)
    if stale_source == skill_text or not check_documents(stale_source, ledger):
        errors.append("negative case was accepted: stale-command-source-claim")

    unsafe_command = ledger["commands"][6]
    unsafe_row = (
        "| `{status}` | `{command}` | `{route}`; source: `{citation}` |".format(
            **unsafe_command
        )
    )
    contradictory_safety_row = unsafe_row.replace(
        unsafe_command["status"], SAFE_COMMAND_STATUS, 1
    )
    contradictory_safety = skill_text.replace(unsafe_row, contradictory_safety_row, 1)
    if contradictory_safety == skill_text or not check_documents(contradictory_safety, ledger):
        errors.append("negative case was accepted: contradictory-command-safety-claim")

    unregistered = copy.deepcopy(ledger)
    unregistered_citation = (
        "web-app@508b22a3fa7a25ef5cdac465a29195117f3f97cc:"
        "web-app/README.md#L1-L1"
    )
    if not check_documents(skill_text + "\n<!-- %s -->\n" % unregistered_citation, unregistered):
        errors.append("negative case was accepted: unregistered-source-citation")

    mismatched = copy.deepcopy(ledger)
    mismatched["rules"][0]["citations"][0]["path"] = "invented.md"
    if not check_documents(skill_text, mismatched):
        errors.append("negative case was accepted: mismatched-source-path")

    overclaimed = skill_text.replace(
        FRONTEND_BOUNDARY_SUPPORTED,
        (
            "`showpass-frontend` owns React, Next.js, and React Native surfaces. "
            "Do not apply Django or deployment rules to its UI code."
        ),
    )
    if overclaimed == skill_text:
        errors.append("frontend-boundary negative fixture did not replace the supported claim")
    elif not check_documents(overclaimed, ledger):
        errors.append("negative case was accepted: overclaimed-frontend-boundary")

    blank_name = skill_text.replace("name: python-best-practices", "name:\n", 1)
    if blank_name == skill_text or not check_documents(blank_name, ledger):
        errors.append("negative case was accepted: blank-frontmatter-name")

    missing_ledger_reference = skill_text.replace(
        "references/source-ledger.json", "references/missing-ledger.json", 1
    )
    if (
        missing_ledger_reference == skill_text
        or not check_documents(missing_ledger_reference, ledger)
    ):
        errors.append("negative case was accepted: missing-local-ledger-reference")

    missing_checker_reference = skill_text.replace(
        "skills/python-best-practices/scripts/check_skill.py",
        "skills/python-best-practices/scripts/missing-checker.py",
        1,
    )
    if (
        missing_checker_reference == skill_text
        or not check_documents(missing_checker_reference, ledger)
    ):
        errors.append("negative case was accepted: missing-local-checker-reference")

    metadata_mutations = (
        (
            "upstream-sha-metadata",
            lambda candidate: candidate.__setitem__("upstream_sha_checked", "0" * 40),
        ),
        (
            "repository-root-metadata",
            lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                "root", "wrong-root"
            ),
        ),
        (
            "repository-sha-metadata",
            lambda candidate: candidate["repositories"]["web-app"].__setitem__(
                "sha", "0" * 40
            ),
        ),
        (
            "rule-route-metadata",
            lambda candidate: candidate["rules"][2].__setitem__("route", "web-k8s"),
        ),
        (
            "rule-repository-metadata",
            lambda candidate: candidate["rules"][2].__setitem__(
                "repo", "showpass-frontend"
            ),
        ),
        (
            "rule-primary-citation-metadata",
            lambda candidate: candidate["rules"][0].__setitem__(
                "citation", candidate["rules"][0]["citations"][1]["citation"]
            ),
        ),
    )
    for name, mutate in metadata_mutations:
        candidate = copy.deepcopy(ledger)
        mutate(candidate)
        if not check_documents(skill_text, candidate):
            errors.append("negative case was accepted: %s" % name)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("safe", "unsafe", "negative", "all"), default="all")
    args = parser.parse_args()

    try:
        ledger = load_ledger()
    except (OSError, json.JSONDecodeError) as exc:
        print("Python skill check failed: could not load source ledger: %s" % exc, file=sys.stderr)
        return 1
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    errors = check_documents(skill_text, ledger)
    scenario = None if args.scenario in {"negative", "all"} else args.scenario
    if args.scenario != "negative":
        errors.extend(check_scenarios(ledger, scenario))
    if args.scenario in {"negative", "all"}:
        errors.extend(check_negative_cases(skill_text, ledger))

    if errors:
        print("Python skill check failed:", file=sys.stderr)
        for error in errors:
            print("- %s" % error, file=sys.stderr)
        return 1

    rule_count = len(ledger.get("rules", []))
    command_count = len(ledger.get("commands", []))
    if args.scenario == "negative":
        case_count = len(NEGATIVE_STATUS_CASES) + len(ROUTING_GUARD_CASES) + 16
    else:
        case_count = len(ledger.get("routing_cases", [])) if scenario is None else sum(
            case.get("safety") == scenario for case in ledger.get("routing_cases", [])
        )
    print("Verified %d source rules, %d labeled commands, and %d %s routing cases." % (
        rule_count,
        command_count,
        case_count,
        "negative" if args.scenario == "negative" else "all" if scenario is None else scenario,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
