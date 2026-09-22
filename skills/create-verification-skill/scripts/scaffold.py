#!/usr/bin/env python3
"""Scaffold and validate a project-local Codex verification skill.

The helper consumes an explicit JSON specification. It never runs commands
from that specification and never overwrites an existing target.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
FRONTMATTER_FIELD = re.compile(r"^(name|description):\s*(.+)$")
REQUIRED_APP_FIELDS = (
    "name",
    "label",
    "description",
    "surface",
    "harness",
    "launch",
    "ready",
    "doctor",
    "drive",
    "evidence",
    "cleanup",
)
REQUIRED_FEATURE_FIELDS = (
    "id",
    "title",
    "summary",
    "sub_features",
    "entry_points",
    "actors",
    "fixtures",
    "actions",
    "gotchas",
)
REQUIRED_ACTION_FIELDS = ("action", "command", "expected_result", "evidence")
HEADING_ORDER = (
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with",
    "Gotchas",
)


class ScaffoldError(ValueError):
    """Raised for invalid specifications or generated skill trees."""


class CollisionError(ScaffoldError):
    """Raised when the requested output already exists, including as a link."""


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScaffoldError(f"{label} must be an object")
    return value


def _require_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ScaffoldError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise ScaffoldError(f"{label} must not be empty")
    if "\n" in value or "\r" in value:
        raise ScaffoldError(f"{label} must be one line")
    return value


def _require_text_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ScaffoldError(f"{label} must be an array")
    if not allow_empty and not value:
        raise ScaffoldError(f"{label} must contain at least one item")
    return [_require_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _slug(value: Any, label: str) -> str:
    value = _require_text(value, label)
    if not NAME_PATTERN.fullmatch(value):
        raise ScaffoldError(f"{label} must be lowercase hyphenated text")
    return value


def _validate_action(action: Any, label: str) -> None:
    action = _require_mapping(action, label)
    for field in REQUIRED_ACTION_FIELDS:
        _require_text(action.get(field), f"{label}.{field}")
    for field in ("actors", "fixtures"):
        if field in action:
            _require_text_list(action[field], f"{label}.{field}")


def validate_spec(spec: Any) -> dict[str, Any]:
    """Validate and return a scaffold specification."""

    spec = _require_mapping(spec, "spec")
    app = _require_mapping(spec.get("app"), "app")
    for field in REQUIRED_APP_FIELDS:
        _require_text(app.get(field), f"app.{field}")
    app["name"] = _slug(app["name"], "app.name")

    features = spec.get("features")
    if not isinstance(features, list) or not features:
        raise ScaffoldError("features must contain at least one feature")
    seen_ids: set[str] = set()
    for feature_index, raw_feature in enumerate(features):
        label = f"features[{feature_index}]"
        feature = _require_mapping(raw_feature, label)
        for field in REQUIRED_FEATURE_FIELDS:
            if field not in feature:
                raise ScaffoldError(f"{label}.{field} is required")
        feature_id = _slug(feature["id"], f"{label}.id")
        if feature_id in seen_ids:
            raise ScaffoldError(f"duplicate feature id: {feature_id}")
        seen_ids.add(feature_id)
        _require_text(feature["title"], f"{label}.title")
        _require_text(feature["summary"], f"{label}.summary")
        sub_features = _require_mapping_list(feature["sub_features"], f"{label}.sub_features")
        if not sub_features:
            raise ScaffoldError(f"{label}.sub_features must contain at least one item")
        for sub_index, sub_feature in enumerate(sub_features):
            _slug(sub_feature.get("id"), f"{label}.sub_features[{sub_index}].id")
            _require_text(sub_feature.get("description"), f"{label}.sub_features[{sub_index}].description")
        _require_text_list(feature["entry_points"], f"{label}.entry_points")
        _require_text_list(feature["actors"], f"{label}.actors")
        _require_text_list(feature["fixtures"], f"{label}.fixtures")
        actions = _require_mapping_list(feature["actions"], f"{label}.actions")
        if not actions:
            raise ScaffoldError(f"{label}.actions must contain at least one action")
        for action_index, action in enumerate(actions):
            _validate_action(action, f"{label}.actions[{action_index}]")
        _require_text_list(feature["gotchas"], f"{label}.gotchas", allow_empty=True)

    for field in ("baseline", "helpers"):
        if field in spec:
            _require_text_list(spec[field], field, allow_empty=True)
    return spec


def _require_mapping_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ScaffoldError(f"{label} must be an array")
    return [_require_mapping(item, f"{label}[{index}]") for index, item in enumerate(value)]


def load_spec(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScaffoldError(f"could not read JSON spec {path}: {exc}") from exc
    return validate_spec(raw)


def _bullet(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- None recorded."


def _action_text(action: dict[str, Any], feature: dict[str, Any]) -> str:
    actors = action.get("actors", feature["actors"])
    fixtures = action.get("fixtures", feature["fixtures"])
    return "\n".join(
        (
            f"- **{action['action']}.**",
            f"  - Actors: {', '.join(actors)}",
            f"  - Fixtures: {', '.join(fixtures)}",
            f"  - Action: `{action['command']}`",
            f"  - Expected result: {action['expected_result']}",
            f"  - Evidence: {action['evidence']}",
        )
    )


def _skill_markdown(spec: dict[str, Any]) -> str:
    app = spec["app"]
    name = f"verify-{app['name']}"
    description = f"Drive and verify {app['label']} through its {app['surface']} surface using the repository's {app['harness']} harness."
    return "\n".join(
        (
            "---",
            f"name: {json.dumps(name)}",
            f"description: {json.dumps(description)}",
            "---",
            "",
            f"# Verify {app['label']}",
            "",
            app["description"],
            "",
            "Use this project-local skill to drive the real user-facing surface and retain proof. Generated instructions are not product proof until they run.",
            "",
            "## Launch",
            "",
            f"- Command: `{app['launch']}`",
            f"- Ready when: {app['ready']}",
            f"- Surface: {app['surface']}",
            "",
            "## Doctor",
            "",
            f"- Run: `{app['doctor']}`",
            f"- A healthy result means: {app['ready']}",
            "- Do not drive an instance that this run did not start or cannot identify.",
            "",
            "## Drive",
            "",
            f"- Harness: {app['harness']}",
            f"- Main path: `{app['drive']}`",
            "- Use the feature map in `features/README.md` and the linked feature recipe.",
            "",
            "## Evidence",
            "",
            f"- {app['evidence']}",
            "- Capture the action and resulting state, not only the final screen or output.",
            "- Verify relevant side effects with a read-only second view.",
            "- Report PASS, FAIL, NOT RUN, or UNSUPPORTED with the command and artifact path.",
            "",
            "## Cleanup",
            "",
            f"- {app['cleanup']}",
            "- Keep evidence artifacts after cleanup.",
            "",
            "## Helpers",
            "",
            _bullet(spec.get("helpers", [])),
            "",
            "The feature map is maintained under `features/`; it describes coverage and does not replace execution.",
            "",
        )
    )


def _index_markdown(spec: dict[str, Any]) -> str:
    app = spec["app"]
    features = spec["features"]
    baseline = spec.get("baseline", [])
    lines = [
        f"# {app['label']} verification map",
        "",
        "This directory is the maintained source for user-facing verification paths. Read the index before driving the app, then use the matching feature file.",
        "",
        "## Baseline preconditions",
        "",
        _bullet(baseline),
        "",
        "## Driving conventions",
        "",
        f"- Use the `{app['harness']}` harness and the exact commands recorded in each recipe.",
        "- Start every recipe from its stated preconditions.",
        "- Prefer stable accessible names, prompt strings, routes, and flags over coordinates or tab order.",
        "",
        "## Proof and skip reporting",
        "",
        "- Capture the action and resulting state, not only the final view.",
        "- Verify mutations with a read-only second view when the surface permits it.",
        "- Keep proof artifacts after cleanup.",
        "- Report an unreachable path with the attempted command and unmet precondition.",
        "- Do not report one entry point as verified through a different path.",
        "",
        "## Features",
        "",
    ]
    for feature in features:
        lines.append(f"- [{feature['title']}](./{feature['id']}.md): {feature['summary']}")
    lines.extend(("",))
    return "\n".join(lines)


def _feature_markdown(feature: dict[str, Any], harness: str) -> str:
    lines = [
        f"# {feature['title']}",
        "",
        feature["summary"],
        "",
        "## Sub-features",
        "",
    ]
    lines.extend(
        f"- `{item['id']}`: {item['description']}" for item in feature["sub_features"]
    )
    lines.extend(("", "## How to get to it (user POV)", ""))
    lines.extend(f"- {entry}" for entry in feature["entry_points"])
    lines.extend(("", f"## Driving it with {harness}", "", "Preconditions:", ""))
    lines.append(f"- Actors: {', '.join(feature['actors'])}")
    lines.append(f"- Fixtures: {', '.join(feature['fixtures'])}")
    lines.append("")
    for action in feature["actions"]:
        lines.extend((_action_text(action, feature), ""))
    lines.extend(("## Gotchas", ""))
    lines.append(_bullet(feature["gotchas"]))
    lines.append("")
    return "\n".join(lines)


def _write_tree(spec: dict[str, Any], root: Path) -> None:
    app = spec["app"]
    (root / "features").mkdir(parents=True)
    (root / "SKILL.md").write_text(_skill_markdown(spec), encoding="utf-8")
    (root / "features" / "README.md").write_text(_index_markdown(spec), encoding="utf-8")
    for feature in spec["features"]:
        path = root / "features" / f"{feature['id']}.md"
        path.write_text(_feature_markdown(feature, app["harness"]), encoding="utf-8")


def _parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScaffoldError(f"cannot read {path}: {exc}") from exc
    if not lines or lines[0] != "---":
        raise ScaffoldError(f"{path} is missing YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ScaffoldError(f"{path} has unterminated YAML frontmatter") from exc
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        match = FRONTMATTER_FIELD.fullmatch(line)
        if match:
            value = match.group(2).strip()
            if value.startswith('"') and value.endswith('"'):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise ScaffoldError(f"{path} has invalid quoted frontmatter") from exc
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            fields[match.group(1)] = value
    for field in ("name", "description"):
        if field not in fields or not fields[field].strip():
            raise ScaffoldError(f"{path} frontmatter is missing {field}")
    return fields


def _check_links(root: Path) -> None:
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for target in MARKDOWN_LINK.findall(text):
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue
            candidate = (path.parent / target.split("#", 1)[0]).resolve()
            if not candidate.is_relative_to(root.resolve()):
                raise ScaffoldError(f"{path.relative_to(root)} link escapes generated skill: {target}")
            if not candidate.exists():
                raise ScaffoldError(f"{path.relative_to(root)} link target does not exist: {target}")


def validate_generated_skill(
    skill_root: Path, *, expected_name: str | None = None
) -> tuple[str, ...]:
    """Return structural errors for a generated skill tree."""

    errors: list[str] = []
    if skill_root.is_symlink():
        return ("skill root must not be a symlink",)
    skill_root = skill_root.resolve()
    skill_file = skill_root / "SKILL.md"
    index = skill_root / "features" / "README.md"
    if not skill_file.is_file():
        errors.append("missing SKILL.md")
    if not index.is_file():
        errors.append("missing features/README.md")
    if errors:
        return tuple(errors)
    try:
        frontmatter = _parse_frontmatter(skill_file)
        expected_name = expected_name or skill_root.name
        if frontmatter["name"] != expected_name:
            errors.append("frontmatter name does not match the generated directory")
        headings = [
            line.removeprefix("## ")
            for line in skill_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        required_skill_headings = ["Launch", "Doctor", "Drive", "Evidence", "Cleanup", "Helpers"]
        if headings != required_skill_headings:
            errors.append("generated SKILL.md sections are incomplete or out of order")
        _check_links(skill_root)
    except ScaffoldError as exc:
        errors.append(str(exc))

    feature_files = sorted(path for path in (skill_root / "features").glob("*.md") if path.name != "README.md")
    if not feature_files:
        errors.append("feature map has no feature files")
    for path in feature_files:
        lines = path.read_text(encoding="utf-8").splitlines()
        h2 = [line.removeprefix("## ") for line in lines if line.startswith("## ")]
        if len(h2) != 4 or h2[:2] != list(HEADING_ORDER[:2]) or not h2[2].startswith(HEADING_ORDER[2]) or h2[3] != HEADING_ORDER[3]:
            errors.append(f"{path.relative_to(skill_root)} has the wrong feature section contract")
        for label in ("Actors:", "Fixtures:", "Action:", "Expected result:", "Evidence:"):
            if not any(line.strip().startswith(f"- {label}") or line.strip().startswith(f"  - {label}") for line in lines):
                errors.append(f"{path.relative_to(skill_root)} is missing {label}")
    return tuple(errors)


def scaffold(spec: dict[str, Any], project_root: Path) -> Path:
    """Create a new generated skill and return its path."""

    spec = validate_spec(spec)
    project_root = project_root.expanduser()
    if project_root.is_symlink() or not project_root.is_dir():
        raise ScaffoldError("project root must be an existing non-symlink directory")
    project_root = project_root.resolve()
    target = project_root / ".agents" / "skills" / f"verify-{spec['app']['name']}"
    if os.path.lexists(target):
        raise CollisionError(f"output already exists: {target}")
    parent = project_root / ".agents"
    for directory in (parent, parent / "skills"):
        if os.path.lexists(directory) and directory.is_symlink():
            raise ScaffoldError(f"output parent must not be a symlink: {directory}")
        directory.mkdir(exist_ok=True)
        if not directory.is_dir():
            raise ScaffoldError(f"output parent is not a directory: {directory}")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        _write_tree(spec, staging)
        errors = validate_generated_skill(staging, expected_name=target.name)
        if errors:
            raise ScaffoldError("generated skill failed validation: " + "; ".join(errors))
        if os.path.lexists(target):
            raise CollisionError(f"output appeared during generation: {target}")
        staging.rename(target)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _cli_scaffold(args: argparse.Namespace) -> int:
    try:
        target = scaffold(load_spec(Path(args.spec)), Path(args.project_root))
    except (OSError, ScaffoldError) as exc:
        print(f"scaffold error: {exc}", file=sys.stderr)
        return 2
    payload = {"status": "created", "skill_root": str(target)}
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Created {target}")
    return 0


def _cli_validate(args: argparse.Namespace) -> int:
    try:
        errors = validate_generated_skill(Path(args.skill_root))
    except (OSError, ScaffoldError) as exc:
        print(f"validation error: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("Validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Valid generated verification skill.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scaffold_parser = subparsers.add_parser("scaffold", help="create a new project-local skill")
    scaffold_parser.add_argument("--spec", required=True, help="JSON specification")
    scaffold_parser.add_argument("--project-root", required=True, help="existing project directory")
    scaffold_parser.add_argument("--json", action="store_true", help="emit a machine-readable result")
    scaffold_parser.set_defaults(func=_cli_scaffold)
    validate_parser = subparsers.add_parser("validate", help="validate an existing generated skill")
    validate_parser.add_argument("--skill-root", required=True, help="generated skill directory")
    validate_parser.set_defaults(func=_cli_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
