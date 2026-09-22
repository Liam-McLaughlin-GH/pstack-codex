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


SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_DIR / "SKILL.md"
LEDGER_PATH = SKILL_DIR / "references" / "source-ledger.json"
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
FRONTEND_BOUNDARY_SUPPORTED = (
    "Start with the `showpass-frontend` repository guidance for its web, mobile, "
    "and desktop apps built with React, Next.js, and React Native."
)
FRONTEND_BOUNDARY_UNSUPPORTED = (
    "owns React, Next.js, and React Native surfaces",
    "Do not apply Django or deployment rules",
)
FAB_LIST_RE = re.compile(r"\bfab\s+--list(?:\s|$)")
FABRIC3_RE = re.compile(r"\bfabric\s*3\b")
FAB_ENVIRONMENT_RE = re.compile(
    r"\bfab\s+(?:demo|beta|live|prod|production)\b"
)
KUBECTL_MUTATION_RE = re.compile(
    r"\bkubectl\s+(?:apply|create|delete|patch|replace|rollout|scale|set)\b"
)
MUTATION_TOKEN = (
    r"(?<![a-z0-9])(?:apply|change|create|delete|deploy(?:ed|ing|ment)?|"
    r"destroy|edit|flush|migrat(?:e|ed|ing|ion|ions)?|modify|mutat(?:e|ed|ing|ion|ions)?|"
    r"patch|promote|provision|publish|release|replace|reset|restart|roll(?:out|ing)?|"
    r"rolling|scale|seed|set|truncate|update|upgrade)(?![a-z0-9])"
)
FAB_MUTATION_RE = re.compile(
    r"\bfab(?:ric)?\b[^\n]*" + MUTATION_TOKEN
)
ENVIRONMENT_RE = re.compile(
    r"(?<![a-z0-9])(?:beta|demo|live|prod|production|staging|"
    r"beta\.showpass\.com|demo\.showpass\.com|www\.showpass\.com)(?![a-z0-9])"
)
MUTATION_RE = re.compile(MUTATION_TOKEN)
DEPLOYMENT_OPERATION_RE = re.compile(
    r"(?<![a-z0-9])(?:deploy(?:ed|ing|ment)?|promote|provision|publish|release|"
    r"roll(?:out|ing)?|rolling|scale)"
    r"(?![a-z0-9])"
)
PRODUCTION_LIVE_RE = re.compile(r"\b(?:production|prod|live)\b")
SOURCE_TASK_RE = re.compile(
    r"\b(?:write|edit|implement|refactor|add|fix|review)\b.*?"
    r"\b(?:component|function|class|module|parser|source|code|file|package|"
    r"queryset|serializer|view)\b"
)
BACKEND_CUE_RE = re.compile(r"\b(?:web-app|django|drf|orm|manage\.py|celery)\b")
FRONTEND_CUE_RE = re.compile(
    r"\b(?:showpass-frontend|next(?:\.js|js)?|react(?:\s+native)?|frontend|client)\b"
)
FRONTEND_OWNER_CUE_RE = re.compile(
    r"\b(?:showpass-frontend|next(?:\.js|js)?|react(?:\s+native)?)\b"
)
LEGACY_UI_CUE_RE = re.compile(
    r"\b(?:angularjs|angular\s+js|legacy\s+(?:ui|frontend|template|bridge|redirect)|"
    r"(?:ui|frontend)\s+bridge|bridge\s+template|django\s+template|"
    r"apps/main/(?:static|templates)|gulp|embedded\s+page)\b"
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
)


def _has_infrastructure_cue(text: str) -> bool:
    return any(marker in text for marker in ("web-k8s", "kubectl", "gcloud")) or bool(
        re.search(r"\bfab(?:\s|$)", text)
    ) or "fabric" in text


def _production_live_requires_review(text: str) -> bool:
    return bool(PRODUCTION_LIVE_RE.search(text)) and not SOURCE_TASK_RE.search(text)


def repositories_for_task(task: str) -> Tuple[str, ...]:
    """Return the repository owners signaled by a short task cue."""

    text = task.lower()
    infrastructure = _has_infrastructure_cue(text)
    backend_cue = bool(BACKEND_CUE_RE.search(text))
    frontend_cue = bool(FRONTEND_CUE_RE.search(text))
    modern_frontend_cue = bool(FRONTEND_OWNER_CUE_RE.search(text))
    legacy_ui_cue = bool(LEGACY_UI_CUE_RE.search(text))

    repositories: List[str] = []
    if backend_cue or legacy_ui_cue:
        repositories.append("web-app")
    if modern_frontend_cue or (backend_cue and frontend_cue):
        repositories.append("showpass-frontend")
    if infrastructure:
        repositories.append("web-k8s")
    return tuple(dict.fromkeys(repositories))


def route_for_task(task: str) -> str:
    """Return a safe repository route for a short task cue."""

    text = task.lower()
    infrastructure = _has_infrastructure_cue(text)
    cluster_mutation = bool(
        FABRIC3_RE.search(text)
        or FAB_ENVIRONMENT_RE.search(text)
        or FAB_MUTATION_RE.search(text)
        or KUBECTL_MUTATION_RE.search(text)
    )
    environment_mutation = bool(ENVIRONMENT_RE.search(text) and MUTATION_RE.search(text))
    deployment_mutation = bool(DEPLOYMENT_OPERATION_RE.search(text))
    if (
        cluster_mutation
        or environment_mutation
        or deployment_mutation
        or _production_live_requires_review(text)
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


def load_ledger() -> Dict[str, Any]:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def _citation_entries(rule: Dict[str, Any]) -> List[Dict[str, str]]:
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

    source = repositories.get(repo, {})
    if repo not in repositories:
        errors.append("%s references unknown repository %r" % (rule_id, repo))
    if match.group("repo") != repo:
        errors.append("%s citation repository does not match its rule: %s" % (rule_id, citation))
    if not re.fullmatch(r"[0-9a-f]{40}", source.get("sha", "")):
        errors.append("%s has an invalid repository SHA" % rule_id)
    if source.get("sha") != match.group("sha"):
        errors.append("%s citation does not carry the repository SHA: %s" % (rule_id, citation))

    path = entry.get("path", "")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts:
        errors.append("%s has an unsafe source path" % rule_id)
    expected_citation_path = "%s/%s" % (repo, path)
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
    ledger: Dict[str, Any],
    skill_text: str,
    errors: List[str],
) -> Dict[str, str]:
    repositories = ledger.get("repositories", {})
    registered: Dict[str, str] = {}
    rule_ids = set()
    for rule in ledger.get("rules", []):
        rule_id = rule.get("id")
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
            first = entries[0] if entries else {}
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
    if safety not in {"safe", "unsafe"}:
        return "command has unknown safety classification: %s" % value
    return None


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
        for field in ("name", "description"):
            if not re.search(r"^%s:\s*\S" % field, frontmatter, re.MULTILINE):
                errors.append("SKILL.md frontmatter is missing %s" % field)

    for line_number, line in enumerate(skill_text.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            errors.append("SKILL.md line %d has trailing whitespace" % line_number)
    if "\r" in skill_text:
        errors.append("SKILL.md contains carriage returns")

    rule_ids = {rule.get("id") for rule in ledger.get("rules", [])}
    _registered_citations(ledger, skill_text, errors)
    errors.extend(_validate_frontend_boundary(skill_text))

    for case in ledger.get("routing_cases", []):
        rule = case.get("rule")
        if case.get("expected") == "unsafe" and not rule:
            errors.append("unsafe routing case is missing a source rule: %s" % case.get("name"))
        elif rule is not None and rule not in rule_ids:
            errors.append("routing case references unknown rule: %s" % case.get("name"))

    for marker in set(MARKER_RE.findall(skill_text)):
        if marker not in rule_ids:
            errors.append("SKILL.md contains an unregistered source marker %s" % marker)

    for command in ledger.get("commands", []):
        value = command.get("command", "")
        if not value or value not in skill_text:
            errors.append("command is missing from SKILL.md: %s" % value)
        status_error = _validate_command_status(command)
        if status_error:
            errors.append(status_error)
        if command.get("rule") not in rule_ids:
            errors.append("command references unknown rule: %s" % value)

    if not SOURCE_RE.search(skill_text):
        errors.append("SKILL.md has no source citations")
    for source_like in SOURCE_LIKE_RE.findall(skill_text):
        if not SOURCE_PARTS_RE.fullmatch(source_like):
            errors.append("SKILL.md contains a malformed source citation %s" % source_like)

    return errors


def check_scenarios(ledger: Dict[str, Any], safety: Optional[str]) -> List[str]:
    errors: List[str] = []
    cases: Iterable[Dict[str, str]] = ledger.get("routing_cases", [])
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
    """Prove that stale statuses and source-register drift fail closed."""

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
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("safe", "unsafe", "negative", "all"), default="all")
    args = parser.parse_args()

    ledger = load_ledger()
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
        case_count = len(NEGATIVE_STATUS_CASES) + len(ROUTING_GUARD_CASES) + 3
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
