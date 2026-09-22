#!/usr/bin/env python3
"""Check the Python skill's source ledger and deterministic routing examples."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_DIR / "SKILL.md"
LEDGER_PATH = SKILL_DIR / "references" / "source-ledger.json"
SOURCE_RE = re.compile(r"(?:web-app|web-k8s|showpass-frontend)@[0-9a-f]{40}:[^`\s]+")
MARKER_RE = re.compile(r"\bSP-[A-Z0-9-]+\b")


def route_for_task(task: str) -> str:
    """Return the route the skill's task table describes for a short task cue."""

    text = task.lower()
    production = any(term in text for term in ("production", "live", "www.showpass.com"))
    mutation = any(term in text for term in ("deploy", "rollout", "migrate", "apply", "delete", "scale", "write"))
    if production and mutation:
        return "unsafe"
    if "showpass-frontend" in text or "next.js" in text or "react native" in text:
        return "showpass-frontend"
    if any(term in text for term in ("web-k8s", "fabric", "kubectl", "gcloud")):
        return "web-k8s"
    if any(term in text for term in ("web-app", "django", "drf", "orm", "manage.py", "celery")):
        return "web-app"
    return "general"


def load_ledger() -> Dict[str, Any]:
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


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

    repositories = ledger.get("repositories", {})
    rule_ids = set()
    for rule in ledger.get("rules", []):
        rule_id = rule.get("id")
        rule_ids.add(rule_id)
        repo = rule.get("repo")
        source = repositories.get(repo, {})
        citation = rule.get("citation", "")
        sha = citation.split("@", 1)[-1].split(":", 1)[0]
        path = rule.get("path", "")
        if repo not in repositories:
            errors.append("%s references unknown repository %r" % (rule_id, repo))
        if source.get("sha") != sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
            errors.append("%s does not carry the repository SHA" % rule_id)
        if not path or path.startswith("/") or ".." in Path(path).parts:
            errors.append("%s has an unsafe source path" % rule_id)
        if rule_id not in skill_text:
            errors.append("%s is missing from SKILL.md" % rule_id)
        if citation not in skill_text:
            errors.append("%s citation is missing from SKILL.md" % rule_id)

    for marker in set(MARKER_RE.findall(skill_text)):
        if marker not in rule_ids:
            errors.append("SKILL.md contains an unregistered source marker %s" % marker)

    for command in ledger.get("commands", []):
        value = command.get("command", "")
        status = command.get("status", "")
        if not value or value not in skill_text:
            errors.append("command is missing from SKILL.md: %s" % value)
        if not any(token in status for token in ("VERIFIED", "NOT RUN", "UNSAFE:")):
            errors.append("command has no execution label: %s" % value)
        if status.startswith("UNSAFE:") and "NOT RUN" not in status:
            errors.append("unsafe command is not marked NOT RUN: %s" % value)
        if command.get("rule") not in rule_ids:
            errors.append("command references unknown rule: %s" % value)

    if not SOURCE_RE.findall(skill_text):
        errors.append("SKILL.md has no source citations")

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("safe", "unsafe", "all"), default="all")
    args = parser.parse_args()

    ledger = load_ledger()
    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    errors = check_documents(skill_text, ledger)
    scenario = None if args.scenario == "all" else args.scenario
    errors.extend(check_scenarios(ledger, scenario))

    if errors:
        print("Python skill check failed:", file=sys.stderr)
        for error in errors:
            print("- %s" % error, file=sys.stderr)
        return 1

    rule_count = len(ledger.get("rules", []))
    command_count = len(ledger.get("commands", []))
    case_count = len(ledger.get("routing_cases", [])) if scenario is None else sum(
        case.get("safety") == scenario for case in ledger.get("routing_cases", [])
    )
    print("Verified %d source rules, %d labeled commands, and %d %s routing cases." % (
        rule_count,
        command_count,
        case_count,
        "all" if scenario is None else scenario,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
