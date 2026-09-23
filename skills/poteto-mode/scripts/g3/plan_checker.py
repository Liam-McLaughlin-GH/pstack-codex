"""Profile-driven Markdown checker for local completion plans.

The checker owns document structure, not a particular upstream program,
model, lane count, or orchestration vocabulary.  A profile supplies required
files, headings, markers, and optional box rules.  The bundled profile is a
small structural contract for the current ``plans/completion`` documents.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class PlanInputError(ValueError):
    """A profile or document cannot be safely checked."""


@dataclass(frozen=True)
class FileRule:
    pattern: str
    required_headings: tuple[str, ...] = ()
    heading_prefixes: tuple[str, ...] = ()
    required_markers: tuple[str, ...] = ()
    require_checked_boxes: bool = False


@dataclass(frozen=True)
class PlanProfile:
    name: str
    required_files: tuple[str, ...]
    rules: tuple[FileRule, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlanProfile":
        if not isinstance(value, Mapping):
            raise PlanInputError("plan profile must be an object")
        name = value.get("name")
        required_files = value.get("required_files")
        raw_rules = value.get("rules")
        if not isinstance(name, str) or not name.strip():
            raise PlanInputError("plan profile name must be a non-empty string")
        if not isinstance(required_files, list) or any(not isinstance(item, str) or not item for item in required_files):
            raise PlanInputError("plan profile required_files must be a string array")
        if not isinstance(raw_rules, list):
            raise PlanInputError("plan profile rules must be an array")
        rules: list[FileRule] = []
        for index, raw in enumerate(raw_rules):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("pattern"), str) or not raw["pattern"]:
                raise PlanInputError(f"plan profile rule {index} needs a pattern")
            rule = FileRule(
                pattern=raw["pattern"],
                required_headings=_string_array(raw.get("required_headings", []), f"rule {index} required_headings"),
                heading_prefixes=_string_array(raw.get("heading_prefixes", []), f"rule {index} heading_prefixes"),
                required_markers=_string_array(raw.get("required_markers", []), f"rule {index} required_markers"),
                require_checked_boxes=raw.get("require_checked_boxes", False),
            )
            if type(rule.require_checked_boxes) is not bool:
                raise PlanInputError(f"rule {index} require_checked_boxes must be boolean")
            rules.append(rule)
        return cls(name.strip(), tuple(required_files), tuple(rules))


@dataclass(frozen=True)
class PlanIssue:
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class PlanReport:
    profile: str
    root: str
    checked_files: tuple[str, ...]
    issues: tuple[PlanIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "root": self.root,
            "checked_files": list(self.checked_files),
            "issues": [asdict(issue) for issue in self.issues],
            "ok": self.ok,
        }


def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PlanInputError(f"{label} must be a string array")
    return tuple(value)


def completion_profile() -> PlanProfile:
    """Return the current completion-document profile.

    These are document headings and evidence labels, not upstream model or
    program markers.  A caller can load a different profile without changing
    checker code.
    """

    return PlanProfile(
        name="current-completion",
        required_files=(
            "GOAL.md",
            "README.md",
            "models.md",
            "orchestration.md",
            "parity.md",
            "skills.md",
            "status.md",
            "issues/*.md",
        ),
        rules=(
            FileRule("GOAL.md", required_markers=("/goal", "Permission envelope:", "Completion:")),
            FileRule("README.md", required_headings=("pstack completion plan", "Evidence and scope"), required_markers=("GOAL.md", "parity.md", "status.md")),
            FileRule("models.md", required_headings=("Models and usage controls",), required_markers=("Role", "Model", "Allocation")),
            FileRule("orchestration.md", required_headings=("Dispatch and integration", "Start and foundation", "Queue", "Worktree and PR ownership", "Verification and review", "Resume and finish")),
            FileRule("parity.md", required_headings=("Remaining upstream parity",), required_markers=("G1 playbooks", "G2 agent definitions", "G3 helper tooling", "G4 Benny and controls")),
            FileRule("skills.md", required_headings=("Skill assignments",), required_markers=("Required use", "avoid-ai-writing")),
            FileRule("status.md", required_headings=("Execution status",), required_markers=("Issue", "Next action")),
            FileRule("issues/*.md", heading_prefixes=("Issue #",), required_headings=("Scope", "Required proof", "Handoff"), required_markers=("Source:", "Required proof")),
        ),
    )


def load_profile(path: str | Path) -> PlanProfile:
    profile_path = Path(path)
    try:
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanInputError(f"could not read profile {profile_path}: {exc}") from exc
    return PlanProfile.from_mapping(value)


def _safe_root(path: str | Path) -> Path:
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise PlanInputError(f"plan root must be a real directory: {root}")
    return root.resolve()


def _relative_files(root: Path, pattern: str) -> list[Path]:
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise PlanInputError(f"profile pattern must stay below the plan root: {pattern}")
    matches = sorted(path for path in root.glob(pattern) if path.is_file() and not path.is_symlink())
    return matches


def _lines_without_frontmatter(path: Path) -> list[tuple[int, str, bool]]:
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanInputError(f"could not read plan document {path}: {exc}") from exc
    start = 0
    if raw and raw[0].strip() == "---":
        try:
            start = raw.index("---", 1) + 1
        except ValueError as exc:
            raise PlanInputError(f"{path} has unterminated frontmatter") from exc
    result: list[tuple[int, str, bool]] = []
    fenced = False
    for number, text in enumerate(raw[start:], start + 1):
        if re.match(r"^\s*(```|~~~)", text):
            fenced = not fenced
        result.append((number, text, fenced))
    return result


def _rule_for(path: Path, root: Path, profile: PlanProfile) -> FileRule | None:
    relative = path.relative_to(root).as_posix()
    for rule in profile.rules:
        if Path(relative).match(rule.pattern):
            return rule
    return None


def _check_file(path: Path, root: Path, rule: FileRule) -> list[PlanIssue]:
    lines = _lines_without_frontmatter(path)
    relative = path.relative_to(root).as_posix()
    headings: list[tuple[int, str]] = []
    prose: list[tuple[int, str]] = []
    for number, text, fenced in lines:
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", text) if not fenced else None
        if match:
            headings.append((number, match.group(1)))
        if not fenced:
            prose.append((number, text))
    values = [heading for _, heading in headings]
    issues: list[PlanIssue] = []
    for required in rule.required_headings:
        if required not in values:
            issues.append(PlanIssue(relative, 1, f"missing heading {required!r}"))
    for prefix in rule.heading_prefixes:
        if not any(heading.startswith(prefix) for heading in values):
            issues.append(PlanIssue(relative, 1, f"missing heading with prefix {prefix!r}"))
    prose_text = "\n".join(text for _, text in prose)
    for marker in rule.required_markers:
        if marker not in prose_text:
            issues.append(PlanIssue(relative, 1, f"missing marker {marker!r}"))
    if rule.require_checked_boxes:
        for number, text in prose:
            if re.match(r"^\s*-\s*\[ \]", text):
                issues.append(PlanIssue(relative, number, "unchecked box"))
    return issues


def check_plan(root: str | Path, profile: PlanProfile | None = None) -> PlanReport:
    """Check an explicit plan directory against a supplied profile."""

    plan_root = _safe_root(root)
    selected = profile or completion_profile()
    issues: list[PlanIssue] = []
    checked: set[str] = set()
    for pattern in selected.required_files:
        matches = _relative_files(plan_root, pattern)
        if not matches:
            issues.append(PlanIssue(pattern, 1, "required file pattern has no matches"))
            continue
        for path in matches:
            relative = path.relative_to(plan_root).as_posix()
            checked.add(relative)
            rule = _rule_for(path, plan_root, selected)
            if rule is not None:
                issues.extend(_check_file(path, plan_root, rule))
    return PlanReport(selected.name, str(plan_root), tuple(sorted(checked)), tuple(issues))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--profile", help="explicit JSON profile; defaults to current completion profile")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile) if args.profile else completion_profile()
        report = check_plan(args.root, profile)
    except PlanInputError as exc:
        parser.error(str(exc))
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
