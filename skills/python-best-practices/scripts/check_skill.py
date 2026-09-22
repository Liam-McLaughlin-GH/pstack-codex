#!/usr/bin/env python3
"""Check the Python skill's source ledger and deterministic routing examples."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


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
FAB_LIST_RE = re.compile(r"\bfab\s+--list(?:\s|$)")
FAB_ROLLING_UPDATE_RE = re.compile(
    r"\bfab\s+(?:demo|beta|live|prod|production)\s+web\.rolling_update(?:\s|$)"
)
KUBECTL_MUTATION_RE = re.compile(
    r"\bkubectl\s+(?:apply|create|delete|patch|replace|rollout|scale|set)\b"
)
FAB_MUTATION_RE = re.compile(
    r"\bfab\s+(?:demo|beta|live|prod|production)\s+"
    r"(?:web\.)?(?:apply|create|delete|patch|replace|rollout|rolling_update|scale|set)\b"
)
ENVIRONMENT_RE = re.compile(r"\b(?:production|prod|live|www\.showpass\.com)\b")
MUTATION_RE = re.compile(
    r"\b(?:apply|create|delete|deploy|migrate|migration|patch|replace|rollout|"
    r"rolling_update|scale|set)\b"
)


NEGATIVE_STATUS_CASES = (
    ("missing-not-run", 0, "safe", "VERIFIED"),
    ("not-run-only", 0, "safe", "NOT RUN"),
    ("execution-claim", 0, "safe", "VERIFIED / RUN"),
    ("unsafe-marked-run", 6, "unsafe", "UNSAFE: unlabelled broad Django suite / RUN"),
    ("safe-marked-unsafe", 0, "safe", "UNSAFE: incorrect classification / NOT RUN"),
    ("unsafe-marked-verified", 6, "unsafe", SAFE_COMMAND_STATUS),
)


def route_for_task(task: str) -> str:
    """Return the route the skill's task table describes for a short task cue."""

    text = task.lower()
    infrastructure = any(
        marker in text
        for marker in ("web-k8s", "kubectl", "gcloud")
    ) or bool(re.search(r"\bfab(?:\s|$)", text))
    cluster_mutation = bool(
        FAB_ROLLING_UPDATE_RE.search(text)
        or FAB_MUTATION_RE.search(text)
        or KUBECTL_MUTATION_RE.search(text)
    )
    environment_mutation = bool(ENVIRONMENT_RE.search(text) and MUTATION_RE.search(text))
    if cluster_mutation or environment_mutation:
        return "unsafe"
    if FAB_LIST_RE.search(text) or infrastructure or "fabric" in text:
        return "web-k8s"
    if "showpass-frontend" in text or "next.js" in text or "react native" in text:
        return "showpass-frontend"
    if any(term in text for term in ("web-app", "django", "drf", "orm", "manage.py", "celery")):
        return "web-app"
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
    return errors


def check_negative_cases(skill_text: str, ledger: Dict[str, Any]) -> List[str]:
    """Prove that stale statuses and source-register drift fail closed."""

    errors: List[str] = []
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
        case_count = len(NEGATIVE_STATUS_CASES) + 2
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
