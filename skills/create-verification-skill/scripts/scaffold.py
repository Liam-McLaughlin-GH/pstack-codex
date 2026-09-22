#!/usr/bin/env python3
"""Scaffold and validate a project-local Codex verification skill.

The helper consumes an explicit JSON specification. Commands are structured
as an executable plus argv, so generated instructions never need a shell
parser. The helper never runs commands from a specification and never
overwrites an existing target.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SKILL_NAME_PATTERN = re.compile(r"^verify-[a-z0-9]+(?:-[a-z0-9]+)*$")
TOP_LEVEL_FIELD = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(?P<value>.*))?$")
MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?:\\.|[^\]])*\]\((?:<(?P<bracket>[^>]+)>|(?P<plain>[^)\s]+))\)"
)
REFERENCE_DEFINITION_PREFIX = re.compile(r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:")
REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?:<(?P<bracket>[^>]+)>|(?P<plain>\S+))(?:[ \t]+.*)?$"
)
REFERENCE_USE = re.compile(r"(?<!!)\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\]")
COMMAND_SHELL_MARKERS = frozenset(";&|<>$`{}*?!()")
SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "pwsh", "powershell", "cmd", "cmd.exe"}
)
CONTEXT_FIELDS = (
    "setup",
    "environment",
    "seed_auth",
    "isolation",
    "ownership",
    "evidence_scope",
    "failure_signals",
)
CONTEXT_LABELS = {
    "setup": "Setup",
    "environment": "Environment",
    "seed_auth": "Seed/auth",
    "isolation": "Isolation",
    "ownership": "Ownership",
    "evidence_scope": "Evidence scope",
    "failure_signals": "Failure signals",
}
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
    "cleanup",
    *CONTEXT_FIELDS,
    "disposable_root",
    "evidence_root",
    "disposable_paths",
)
REQUIRED_FEATURE_FIELDS = (
    "id",
    "title",
    "summary",
    "sub_features",
    "entry_points",
    *CONTEXT_FIELDS,
    "actors",
    "fixtures",
    "actions",
    "gotchas",
)
REQUIRED_ACTION_FIELDS = (
    "action",
    "command",
    "expected_result",
    "failure_signals",
    "evidence",
)
HEADING_ORDER = (
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with",
    "Gotchas",
)
SKILL_HEADING_ORDER = (
    "Launch",
    "Doctor",
    "Drive",
    "Evidence",
    "Cleanup",
    "Helpers",
    "Maintenance",
)
TRUSTED_SYSTEM_SYMLINKS = {
    "/var": "/private/var",
    "/tmp": "/private/tmp",
    "/etc": "/private/etc",
}


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
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ScaffoldError(f"{label} must be one line without NUL")
    if any(ord(character) < 32 and character != "\t" for character in value):
        raise ScaffoldError(f"{label} contains a control character")
    return value


def _require_text_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ScaffoldError(f"{label} must be an array")
    if not allow_empty and not value:
        raise ScaffoldError(f"{label} must contain at least one item")
    return [_require_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _require_mapping_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ScaffoldError(f"{label} must be an array")
    return [_require_mapping(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _slug(value: Any, label: str) -> str:
    value = _require_text(value, label)
    if not NAME_PATTERN.fullmatch(value):
        raise ScaffoldError(f"{label} must be lowercase hyphenated text")
    return value


def _safe_relative_path(value: Any, label: str) -> str:
    """Validate a path that remains below the project root when rendered."""

    value = _require_text(value, label)
    if "\\" in value or value.startswith(("/", "~", "//")):
        raise ScaffoldError(f"{label} must be a relative POSIX path")
    if re.match(r"^[A-Za-z]:", value) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise ScaffoldError(f"{label} must not be an absolute path or URI")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ScaffoldError(f"{label} must not contain empty, '.', or '..' path parts")
    return "/".join(parts)


def _path_is_within(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return path_parts[: len(root_parts)] == root_parts


def _validate_roots(app: dict[str, Any]) -> tuple[str, str]:
    disposable_root = _safe_relative_path(app["disposable_root"], "app.disposable_root")
    evidence_root = _safe_relative_path(app["evidence_root"], "app.evidence_root")
    if _path_is_within(disposable_root, evidence_root) or _path_is_within(evidence_root, disposable_root):
        raise ScaffoldError("app.disposable_root and app.evidence_root must not overlap")

    disposable_paths = _require_text_list(app["disposable_paths"], "app.disposable_paths")
    for index, path in enumerate(disposable_paths):
        normalized = _safe_relative_path(path, f"app.disposable_paths[{index}]")
        if not _path_is_within(normalized, disposable_root):
            raise ScaffoldError(
                f"app.disposable_paths[{index}] must be below app.disposable_root"
            )
    return disposable_root, evidence_root


def _validate_context(value: dict[str, Any], label: str) -> None:
    for field in CONTEXT_FIELDS:
        _require_text_list(value.get(field), f"{label}.{field}")


def _command_argument_candidates(argument: str) -> tuple[str, ...]:
    candidates = [argument]
    if "=" in argument:
        candidates.append(argument.split("=", 1)[1])
    return tuple(candidates)


def _validate_command(value: Any, label: str, *, forbidden_roots: tuple[str, ...] = ()) -> dict[str, Any]:
    command = _require_mapping(value, label)
    allowed_fields = {"executable", "argv", "cwd"}
    unknown = set(command) - allowed_fields
    if unknown:
        raise ScaffoldError(f"{label} has unsupported fields: {', '.join(sorted(unknown))}")
    executable = _require_text(command.get("executable"), f"{label}.executable")
    if any(character in COMMAND_SHELL_MARKERS for character in executable):
        raise ScaffoldError(
            f"{label}.executable must be an executable path or name, not shell syntax"
        )
    argv = command.get("argv")
    if not isinstance(argv, list):
        raise ScaffoldError(f"{label}.argv must be an array")
    if Path(executable).name.casefold() in SHELL_EXECUTABLES:
        if any(
            argument.casefold() in {"-c", "/c", "-command", "-encodedcommand"}
            for argument in argv
            if isinstance(argument, str)
        ):
            raise ScaffoldError(f"{label} must not introduce a free-form shell command")
    for index, argument in enumerate(argv):
        argument = _require_text(argument, f"{label}.argv[{index}]")
        for candidate in _command_argument_candidates(argument):
            if candidate.startswith(("/", "~", "//")) or re.match(r"^[A-Za-z]:[\\/]", candidate):
                raise ScaffoldError(
                    f"{label}.argv[{index}] must not contain an absolute path"
                )
            if candidate.casefold().startswith("file:"):
                raise ScaffoldError(
                    f"{label}.argv[{index}] must not contain a file URI"
                )
            if "\\" not in candidate and ".." in PurePosixPath(candidate).parts:
                raise ScaffoldError(
                    f"{label}.argv[{index}] must not escape the project with '..'"
                )
            for root in forbidden_roots:
                if candidate == root or candidate.startswith(f"{root}/"):
                    raise ScaffoldError(
                        f"{label}.argv[{index}] must not address protected path {root!r}"
                    )
    if "cwd" in command:
        cwd = _safe_relative_path(command["cwd"], f"{label}.cwd")
        for root in forbidden_roots:
            if _path_is_within(cwd, root):
                raise ScaffoldError(
                    f"{label}.cwd must not address protected path {root!r}"
                )
        command["cwd"] = cwd
    command["executable"] = executable
    command["argv"] = argv
    return command


def _validate_evidence(value: Any, label: str, evidence_root: str) -> dict[str, str]:
    evidence = _require_mapping(value, label)
    if set(evidence) != {"path", "proves"}:
        raise ScaffoldError(f"{label} must contain only path and proves")
    path = _safe_relative_path(evidence["path"], f"{label}.path")
    if not _path_is_within(path, evidence_root):
        raise ScaffoldError(f"{label}.path must be below app.evidence_root")
    proves = _require_text(evidence["proves"], f"{label}.proves")
    evidence["path"] = path
    evidence["proves"] = proves
    return evidence


def _validate_action(
    action: Any,
    label: str,
    *,
    evidence_root: str,
) -> None:
    action = _require_mapping(action, label)
    for field in REQUIRED_ACTION_FIELDS:
        if field not in action:
            raise ScaffoldError(f"{label}.{field} is required")
    _require_text(action["action"], f"{label}.action")
    _validate_command(action["command"], f"{label}.command")
    _require_text(action["expected_result"], f"{label}.expected_result")
    _require_text_list(action["failure_signals"], f"{label}.failure_signals")
    _validate_evidence(action["evidence"], f"{label}.evidence", evidence_root)
    for field in ("actors", "fixtures"):
        if field in action:
            _require_text_list(action[field], f"{label}.{field}")


def validate_spec(spec: Any) -> dict[str, Any]:
    """Validate and return a scaffold specification."""

    spec = _require_mapping(spec, "spec")
    app = _require_mapping(spec.get("app"), "app")
    for field in REQUIRED_APP_FIELDS:
        if field not in app:
            raise ScaffoldError(f"app.{field} is required")
    app["name"] = _slug(app["name"], "app.name")
    generated_name = f"verify-{app['name']}"
    if len(generated_name) > MAX_SKILL_NAME_LENGTH:
        raise ScaffoldError(
            f"generated skill name is too long ({len(generated_name)} characters; maximum is {MAX_SKILL_NAME_LENGTH})"
        )
    for field in ("label", "description", "surface", "harness", "ready"):
        _require_text(app[field], f"app.{field}")
    _frontmatter_description(app)
    _validate_context(app, "app")
    disposable_root, evidence_root = _validate_roots(app)
    _validate_command(app["launch"], "app.launch")
    _validate_command(app["doctor"], "app.doctor")
    _validate_command(app["drive"], "app.drive")
    _validate_command(app["cleanup"], "app.cleanup", forbidden_roots=(evidence_root,))
    if "evidence" in app:
        _require_text(app["evidence"], "app.evidence")
    for field in ("baseline", "helpers"):
        if field in spec:
            _require_text_list(spec[field], field, allow_empty=True)

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
        sub_ids: set[str] = set()
        for sub_index, sub_feature in enumerate(sub_features):
            sub_id = _slug(sub_feature.get("id"), f"{label}.sub_features[{sub_index}].id")
            if sub_id in sub_ids:
                raise ScaffoldError(f"duplicate sub-feature id: {sub_id}")
            sub_ids.add(sub_id)
            _require_text(
                sub_feature.get("description"),
                f"{label}.sub_features[{sub_index}].description",
            )
        _require_text_list(feature["entry_points"], f"{label}.entry_points")
        _validate_context(feature, label)
        _require_text_list(feature["actors"], f"{label}.actors")
        _require_text_list(feature["fixtures"], f"{label}.fixtures")
        actions = _require_mapping_list(feature["actions"], f"{label}.actions")
        if not actions:
            raise ScaffoldError(f"{label}.actions must contain at least one action")
        for action_index, action in enumerate(actions):
            _validate_action(
                action,
                f"{label}.actions[{action_index}]",
                evidence_root=evidence_root,
            )
        _require_text_list(feature["gotchas"], f"{label}.gotchas", allow_empty=True)
    return spec


def load_spec(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScaffoldError(f"could not read JSON spec {path}: {exc}") from exc
    return validate_spec(raw)


def _markdown_text(value: str) -> str:
    """Escape text inserted into prose, headings, and list items."""

    escaped = value.replace("\\", "\\\\")
    for character in "`*_[]()<>#!|":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _code_span(value: str) -> str:
    runs = [len(run) for run in re.findall(r"`+", value)]
    delimiter = "`" * (max(runs, default=0) + 1)
    if value.startswith("`") or value.endswith("`"):
        return f"{delimiter} {value} {delimiter}"
    return f"{delimiter}{value}{delimiter}"


def _render_command(command: dict[str, Any]) -> str:
    return shlex.join([command["executable"], *command["argv"]])


def _command_lines(label: str, command: dict[str, Any], *, indent: str = "") -> list[str]:
    lines: list[str] = []
    if "cwd" in command:
        lines.append(f"{indent}- Working directory: {_code_span(command['cwd'])}")
    lines.append(f"{indent}- {label}: {_code_span(_render_command(command))}")
    return lines


def _bullet(items: list[str]) -> str:
    return "\n".join(f"- {_markdown_text(item)}" for item in items) or "- None recorded."


def _context_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for field in CONTEXT_FIELDS:
        lines.append(f"- {CONTEXT_LABELS[field]}:")
        lines.extend(f"  - {_markdown_text(item)}" for item in value[field])
    return lines


def _action_text(action: dict[str, Any], feature: dict[str, Any]) -> str:
    actors = action.get("actors", feature["actors"])
    fixtures = action.get("fixtures", feature["fixtures"])
    evidence = action["evidence"]
    lines = [
        f"- **{_markdown_text(action['action'])}.**",
        f"  - Actors: {_markdown_text(', '.join(actors))}",
        f"  - Fixtures: {_markdown_text(', '.join(fixtures))}",
    ]
    lines.extend(_command_lines("Action", action["command"], indent="  "))
    lines.extend(
        (
            f"  - Expected result: {_markdown_text(action['expected_result'])}",
            f"  - Failure signals: {_markdown_text('; '.join(action['failure_signals']))}",
            f"  - Evidence: {_code_span(evidence['path'])}, {_markdown_text(evidence['proves'])}",
        )
    )
    return "\n".join(lines)


def _frontmatter_description(app: dict[str, Any]) -> str:
    description = (
        f"Drive and verify {app['label']} through its {app['surface']} surface "
        f"using the repository's {app['harness']} harness."
    )
    if len(description) > MAX_DESCRIPTION_LENGTH or "<" in description or ">" in description:
        raise ScaffoldError("generated frontmatter description exceeds Codex limits")
    return description


def _skill_markdown(spec: dict[str, Any]) -> str:
    app = spec["app"]
    name = f"verify-{app['name']}"
    description = _frontmatter_description(app)
    evidence_context = {field: app[field] for field in CONTEXT_FIELDS}
    lines = [
        "---",
        f"name: {json.dumps(name)}",
        f"description: {json.dumps(description)}",
        "---",
        "",
        f"# Verify {_markdown_text(app['label'])}",
        "",
        _markdown_text(app["description"]),
        "",
        "Use this project-local skill to drive the real user-facing surface and retain proof. Generated instructions are not product proof until they run.",
        "",
        "## Launch",
        "",
        *_context_lines(app),
        *_command_lines("Command", app["launch"]),
        f"- Ready when: {_markdown_text(app['ready'])}",
        f"- Surface: {_markdown_text(app['surface'])}",
        "",
        "## Doctor",
        "",
        *_command_lines("Command", app["doctor"]),
        "- This is a read-only check of the process, build or version, endpoint or prompt, auth, and disposable state.",
        *_context_lines(app),
        f"- Healthy result: {_markdown_text(app['ready'])}",
        "",
        "## Drive",
        "",
        f"- Harness: {_markdown_text(app['harness'])}",
        *_command_lines("Command", app["drive"]),
        "- Start from the feature map's Preconditions and use the linked recipe's literal action command.",
        f"- Isolation: {_markdown_text('; '.join(app['isolation']))}",
        "",
        "## Evidence",
        "",
        f"- Evidence root: {_code_span(app['evidence_root'])}",
        *_context_lines(evidence_context),
        "- Capture the command, stdout, stderr, exit code, visible result, and a read-only side-effect check.",
        "- Keep evidence paths below the evidence root and never remove them during cleanup.",
        "",
        "## Cleanup",
        "",
        *_command_lines("Command", app["cleanup"]),
        *_context_lines(app),
        f"- Disposable root: {_code_span(app['disposable_root'])}",
        f"- Disposable paths: {', '.join(_code_span(path) for path in app['disposable_paths'])}",
        f"- Evidence survives cleanup under {_code_span(app['evidence_root'])}.",
        "",
        "## Helpers",
        "",
        _bullet(spec.get("helpers", [])),
        "",
        "## Maintenance",
        "",
        "Feature-map maintenance is a #4 follow-up and is unsupported until #4 is available. Do not claim stale-map detection from this creation flow.",
        "",
    ]
    return "\n".join(lines)


def _index_markdown(spec: dict[str, Any]) -> str:
    app = spec["app"]
    features = spec["features"]
    baseline = spec.get("baseline", [])
    lines = [
        f"# {_markdown_text(app['label'])} verification map",
        "",
        _markdown_text(app["description"]),
        "",
        "## Baseline preconditions",
        "",
        *_context_lines(app),
        f"- Disposable root: {_code_span(app['disposable_root'])}",
        f"- Evidence root: {_code_span(app['evidence_root'])}",
    ]
    if baseline:
        lines.extend(("", "- Additional baseline notes:", *[f"  - {_markdown_text(item)}" for item in baseline]))
    lines.extend(
        (
            "",
            "## Driving conventions",
            "",
            f"- Use the {_code_span(app['harness'])} harness and the exact argv commands recorded in each recipe.",
            "- Start every recipe from its stated Preconditions.",
            "- Prefer stable accessible names, prompt strings, routes, and flags over coordinates or tab order.",
            "",
            "## Proof and skip reporting",
            "",
            "- Capture the action and resulting state, not only the final view.",
            "- Record stdout, stderr, exit code, visible result, and a read-only side-effect check when relevant.",
            "- Keep proof artifacts after cleanup.",
            "- Report an unreachable path with the attempted command and unmet precondition.",
            "- Do not report one entry point as verified through a different path.",
            "",
            "## Features",
            "",
        )
    )
    for feature in features:
        lines.append(
            f"- [{_markdown_text(feature['title'])}](./{feature['id']}.md): "
            f"{_markdown_text(feature['summary'])}"
        )
    lines.append("")
    return "\n".join(lines)


def _feature_markdown(feature: dict[str, Any], harness: str) -> str:
    lines = [
        f"# {_markdown_text(feature['title'])}",
        "",
        _markdown_text(feature["summary"]),
        "",
        "## Sub-features",
        "",
    ]
    lines.extend(
        f"- `{_markdown_text(item['id'])}`: {_markdown_text(item['description'])}"
        for item in feature["sub_features"]
    )
    lines.extend(("", "## How to get to it (user POV)", ""))
    lines.extend(f"- {_markdown_text(entry)}" for entry in feature["entry_points"])
    lines.extend(("", f"## Driving it with {_markdown_text(harness)}", "", "Preconditions:", ""))
    lines.extend(_context_lines(feature))
    lines.append(f"- Actors: {_markdown_text(', '.join(feature['actors']))}")
    lines.append(f"- Fixtures: {_markdown_text(', '.join(feature['fixtures']))}")
    lines.append("")
    for action in feature["actions"]:
        lines.extend((_action_text(action, feature), ""))
    lines.extend(("## Gotchas", "", _bullet(feature["gotchas"]), ""))
    return "\n".join(lines)


def _write_tree(spec: dict[str, Any], root: Path) -> None:
    app = spec["app"]
    (root / "features").mkdir()
    (root / "SKILL.md").write_text(_skill_markdown(spec), encoding="utf-8")
    (root / "features" / "README.md").write_text(_index_markdown(spec), encoding="utf-8")
    for feature in spec["features"]:
        path = root / "features" / f"{feature['id']}.md"
        path.write_text(_feature_markdown(feature, app["harness"]), encoding="utf-8")


YAML_NON_STRING_PLAIN_SCALAR = re.compile(
    r"^[+-]?(?:"
    r"0[bB][01_]+|0[oO][0-7_]+|0[xX][0-9a-fA-F_]+|"
    r"0[0-7_]+|"
    r"(?:0|[1-9][0-9_]*)(?:\.[0-9_]*)?(?:[eE][+-]?[0-9_]+)?|"
    r"\.[0-9_]+(?:[eE][+-]?[0-9_]+)?|"
    r"\.(?:inf|nan)|"
    r"[0-9]+(?::[0-5]?[0-9])+)$",
    re.IGNORECASE,
)
YAML_DATE_PLAIN_SCALAR = re.compile(
    r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:$|[Tt ][0-9])"
)


def _is_non_string_yaml_scalar(value: str) -> bool:
    plain_value = re.split(r"[ \t]+#", value, maxsplit=1)[0].rstrip()
    if plain_value.casefold() in {"~", "null", "true", "false", "yes", "no", "on", "off"}:
        return True
    return bool(
        YAML_NON_STRING_PLAIN_SCALAR.fullmatch(plain_value)
        or YAML_DATE_PLAIN_SCALAR.fullmatch(plain_value)
    )


def _strict_frontmatter_subset(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = TOP_LEVEL_FIELD.fullmatch(line)
        if not match:
            raise ScaffoldError(f"invalid YAML frontmatter line: {line!r}")
        key = match.group("key")
        if key in fields:
            raise ScaffoldError(f"duplicate frontmatter field: {key}")
        value = (match.group("value") or "").strip()
        if not value:
            raise ScaffoldError(f"frontmatter field {key!r} is empty")
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ScaffoldError(f"frontmatter field {key!r} has invalid YAML quoting") from exc
            if not isinstance(parsed, str):
                raise ScaffoldError(f"frontmatter field {key!r} must be a string")
            fields[key] = parsed
            continue
        if value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ScaffoldError(f"frontmatter field {key!r} has an unterminated quote")
            fields[key] = value[1:-1].replace("''", "'")
            continue
        if value.startswith(("[", "{", "&", "*", "!", "|", ">", "%", "@", "`")):
            raise ScaffoldError(f"frontmatter field {key!r} is not a supported scalar")
        if _is_non_string_yaml_scalar(value):
            raise ScaffoldError(f"frontmatter field {key!r} must be a string")
        fields[key] = value
    return fields


def _parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScaffoldError(f"cannot read {path}: {exc}") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ScaffoldError(f"{path} is missing YAML frontmatter")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index] == "---")
    except StopIteration as exc:
        raise ScaffoldError(f"{path} has unterminated YAML frontmatter") from exc
    source = "\n".join(lines[1:closing])
    try:
        import yaml  # type: ignore[import-not-found]

        class UniqueLoader(yaml.SafeLoader):  # type: ignore[name-defined]
            pass

        def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            mapping: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ScaffoldError(f"duplicate frontmatter field: {key}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        UniqueLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping,
        )
        parsed = yaml.load(source, Loader=UniqueLoader)
        if not isinstance(parsed, dict):
            raise ScaffoldError("frontmatter must be a YAML mapping")
        fields = parsed
    except ModuleNotFoundError:
        fields = _strict_frontmatter_subset(source)
    except ScaffoldError:
        raise
    except Exception as exc:
        raise ScaffoldError(f"invalid YAML frontmatter: {exc}") from exc

    if set(fields) - {"name", "description"}:
        unknown = ", ".join(sorted(set(fields) - {"name", "description"}))
        raise ScaffoldError(f"unsupported frontmatter field(s): {unknown}")
    for field in ("name", "description"):
        if field not in fields:
            raise ScaffoldError(f"{path} frontmatter is missing {field}")
        if not isinstance(fields[field], str) or not fields[field].strip():
            raise ScaffoldError(f"frontmatter field {field!r} must be a non-empty string")
    return {"name": fields["name"], "description": fields["description"]}


def _validate_frontmatter_fields(
    frontmatter: dict[str, str], *, expected_name: str, path: Path
) -> None:
    name = frontmatter["name"].strip()
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise ScaffoldError(f"{path} has an invalid Codex skill name: {name!r}")
    if name != expected_name:
        raise ScaffoldError("frontmatter name does not match the generated directory")
    if len(name) > MAX_SKILL_NAME_LENGTH:
        raise ScaffoldError(f"frontmatter name exceeds {MAX_SKILL_NAME_LENGTH} characters")
    description = frontmatter["description"].strip()
    if description.startswith("[TODO:"):
        raise ScaffoldError("frontmatter description contains an unfinished TODO")
    if "<" in description or ">" in description:
        raise ScaffoldError("frontmatter description must not contain angle brackets")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ScaffoldError(f"frontmatter description exceeds {MAX_DESCRIPTION_LENGTH} characters")


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _symlink_component_errors(path: Path, *, include_final: bool = True) -> list[str]:
    absolute = _lexical_absolute(path)
    components = absolute.parts
    current = Path(absolute.anchor)
    errors: list[str] = []
    limit = len(components) if include_final else max(1, len(components) - 1)
    for component in components[1:limit]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            trusted_target = TRUSTED_SYSTEM_SYMLINKS.get(os.fspath(current))
            if trusted_target and os.path.realpath(current) == trusted_target:
                continue
            errors.append(f"symlinked path component is not allowed: {current}")
    return errors


def _tree_errors(root: Path) -> list[str]:
    errors = _symlink_component_errors(root)
    absolute_root = _lexical_absolute(root)
    try:
        root_mode = os.lstat(absolute_root).st_mode
    except OSError as exc:
        return [*errors, f"cannot inspect generated root: {exc}"]
    if stat.S_ISLNK(root_mode):
        errors.append("skill root must not be a symlink")
    elif not stat.S_ISDIR(root_mode):
        errors.append("skill root must be a directory")
    if errors or not stat.S_ISDIR(root_mode):
        return errors

    pending = [absolute_root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            errors.append(f"cannot inspect {directory}: {exc}")
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                errors.append(f"cannot inspect {path}: {exc}")
                continue
            if stat.S_ISLNK(mode):
                errors.append(f"generated tree contains symlink: {path}")
            elif stat.S_ISDIR(mode):
                pending.append(path)
            elif not stat.S_ISREG(mode):
                errors.append(f"generated tree contains non-regular file: {path}")
    return errors


def _skill_location_error(skill_root: Path) -> str | None:
    if not SKILL_NAME_PATTERN.fullmatch(skill_root.name):
        return "skill root must be named verify-<app>"
    if len(skill_root.name) > MAX_SKILL_NAME_LENGTH:
        return f"skill root name exceeds {MAX_SKILL_NAME_LENGTH} characters"
    if len(skill_root.parts) < 3 or skill_root.parts[-3:-1] != (".agents", "skills"):
        return "skill root must be under .agents/skills/verify-<app>"
    return None


def _strip_markdown_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    return re.sub(r"(?P<ticks>`+).*?(?P=ticks)", "", text, flags=re.DOTALL)


def _reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _validate_link_target(root: Path, path: Path, target: str) -> None:
    parsed = urlsplit(target)
    if target.startswith("#"):
        return
    if parsed.scheme or parsed.netloc or target.startswith("//"):
        raise ScaffoldError(f"{path.relative_to(root)} contains a non-local link: {target}")
    link_path = unquote(parsed.path)
    if not link_path:
        raise ScaffoldError(f"{path.relative_to(root)} contains an empty link")
    candidate = _lexical_absolute(path.parent / link_path)
    if not candidate.is_relative_to(root):
        raise ScaffoldError(f"{path.relative_to(root)} link escapes generated skill: {target}")
    if not candidate.is_file():
        raise ScaffoldError(f"{path.relative_to(root)} link target does not exist: {target}")


def _check_links(root: Path) -> None:
    root = _lexical_absolute(root)
    for path in sorted(root.rglob("*.md")):
        text = _strip_markdown_code(path.read_text(encoding="utf-8"))
        definitions: dict[str, str] = {}
        for line in text.splitlines():
            prefix = REFERENCE_DEFINITION_PREFIX.match(line)
            if not prefix:
                continue
            definition = REFERENCE_DEFINITION.match(line)
            if definition is None:
                raise ScaffoldError(
                    f"{path.relative_to(root)} contains a malformed Markdown reference definition"
                )
            label = _reference_label(prefix.group("label"))
            if label in definitions:
                raise ScaffoldError(
                    f"{path.relative_to(root)} repeats Markdown reference definition {label!r}"
                )
            target = definition.group("bracket") or definition.group("plain") or ""
            definitions[label] = target
            _validate_link_target(root, path, target)
        for match in MARKDOWN_LINK.finditer(text):
            target = match.group("bracket") or match.group("plain") or ""
            _validate_link_target(root, path, target)
        for match in REFERENCE_USE.finditer(text):
            label = _reference_label(match.group("label") or match.group("text"))
            if label not in definitions:
                raise ScaffoldError(f"{path.relative_to(root)} has an unresolved Markdown reference: {label!r}")


def _nonempty_lines_between(lines: list[str], start: int, end: int) -> list[str]:
    return [line.strip() for line in lines[start:end] if line.strip()]


def _section_lines(lines: list[str], heading: str) -> tuple[int, int, list[str]] | None:
    try:
        start = lines.index(heading)
    except ValueError:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end, lines[start + 1 : end]


def _require_label(lines: list[str], label: str, location: str, errors: list[str]) -> None:
    prefix = f"- {label}:"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        if stripped[len(prefix) :].strip():
            return
        if index + 1 < len(lines) and lines[index + 1].strip().startswith("-"):
            return
    errors.append(f"{location} is missing {label}:")


def _code_label_values(lines: list[str], label: str) -> list[str]:
    pattern = re.compile(rf"^- {re.escape(label)}: `([^`]+)`\s*$")
    for line in lines:
        match = pattern.fullmatch(line.strip())
        if match:
            return [match.group(1)]
    return []


def _code_label_value(lines: list[str], label: str) -> str | None:
    values = _code_label_values(lines, label)
    return values[0] if values else None


def _inline_code_label_value(line: str, label: str) -> str | None:
    match = re.fullmatch(
        rf"- {re.escape(label)}: (?P<ticks>`+)(?P<value>.*?)(?P=ticks)\s*",
        line.strip(),
    )
    if match is None:
        return None
    return match.group("value").strip()


def _validate_rendered_command(
    lines: list[str],
    label: str,
    location: str,
    errors: list[str],
    *,
    forbidden_roots: tuple[str, ...] = (),
) -> None:
    prefix = f"- {label}:"
    command_lines = [line for line in lines if line.strip().startswith(prefix)]
    if len(command_lines) != 1:
        errors.append(f"{location} must contain exactly one {label} command")
        return
    rendered = _inline_code_label_value(command_lines[0], label)
    if not rendered:
        errors.append(f"{location} {label} must be a non-empty inline code command")
        return
    try:
        argv = shlex.split(rendered, posix=True)
    except ValueError as exc:
        errors.append(f"{location} {label} has invalid command quoting: {exc}")
        return
    if not argv:
        errors.append(f"{location} {label} must contain an executable")
        return

    command: dict[str, Any] = {"executable": argv[0], "argv": argv[1:]}
    cwd_prefix = "- Working directory:"
    cwd_lines = [line for line in lines if line.strip().startswith(cwd_prefix)]
    if len(cwd_lines) > 1:
        errors.append(f"{location} must contain at most one Working directory")
        return
    if cwd_lines:
        cwd = _inline_code_label_value(cwd_lines[0], "Working directory")
        if not cwd:
            errors.append(f"{location} Working directory must be a non-empty inline code path")
            return
        command["cwd"] = cwd

    try:
        _validate_command(command, f"{location}.{label}", forbidden_roots=forbidden_roots)
    except ScaffoldError as exc:
        errors.append(str(exc))


def _inline_code_values(lines: list[str], label: str) -> list[str]:
    prefix = f"- {label}:"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return re.findall(r"`([^`]+)`", stripped[len(prefix) :])
    return []


def _validate_index(
    index: Path,
    feature_files: list[Path],
    root: Path,
    errors: list[str],
    *,
    disposable_root: str | None = None,
    evidence_root: str | None = None,
) -> None:
    lines = index.read_text(encoding="utf-8").splitlines()
    nonempty = [line for line in lines if line.strip()]
    if not nonempty or not nonempty[0].startswith("# ") or nonempty[0].startswith("## "):
        errors.append("features/README.md must start with an H1")
    if len([line for line in lines if line.startswith("# ")]) != 1:
        errors.append("features/README.md must contain exactly one H1")
    first_h2 = next((index for index, line in enumerate(lines) if line.startswith("## ")), len(lines))
    if not _nonempty_lines_between(lines, 1, first_h2):
        errors.append("features/README.md must contain a summary paragraph")
    headings = [line.removeprefix("## ") for line in lines if line.startswith("## ")]
    expected_headings = [
        "Baseline preconditions",
        "Driving conventions",
        "Proof and skip reporting",
        "Features",
    ]
    if headings != expected_headings:
        errors.append("features/README.md headings are incomplete or out of order")
    for label in (*CONTEXT_LABELS.values(), "Disposable root", "Evidence root"):
        _require_label(lines, label, "features/README.md", errors)

    expected_ids = [path.stem for path in feature_files]
    links = re.findall(r"\]\(\./([a-z0-9-]+)\.md\)", index.read_text(encoding="utf-8"))
    if len(links) != len(set(links)):
        errors.append("features/README.md repeats a feature link")
    if set(links) != set(expected_ids) or len(links) != len(expected_ids):
        errors.append("features/README.md must link every feature exactly once")
    for feature_id in links:
        if not any(path.stem == feature_id for path in feature_files):
            errors.append(f"features/README.md links an unknown feature: {feature_id}")
    index_disposable_root = _code_label_value(lines, "Disposable root")
    if index_disposable_root is None:
        errors.append("features/README.md must name a relative Disposable root")
    else:
        try:
            normalized = _safe_relative_path(index_disposable_root, "features/README.md Disposable root")
            if disposable_root is not None and normalized != disposable_root:
                errors.append("features/README.md Disposable root does not match SKILL.md")
        except ScaffoldError as exc:
            errors.append(str(exc))
    index_evidence_root = _code_label_value(lines, "Evidence root")
    if index_evidence_root is None:
        errors.append("features/README.md must name a relative Evidence root")
    else:
        try:
            normalized = _safe_relative_path(index_evidence_root, "features/README.md Evidence root")
            if evidence_root is not None and normalized != evidence_root:
                errors.append("features/README.md Evidence root does not match SKILL.md")
        except ScaffoldError as exc:
            errors.append(str(exc))


def _validate_feature(
    path: Path,
    root: Path,
    errors: list[str],
    *,
    evidence_root: str | None = None,
) -> None:
    relative = path.relative_to(root)
    lines = path.read_text(encoding="utf-8").splitlines()
    nonempty = [line for line in lines if line.strip()]
    if not nonempty or not nonempty[0].startswith("# ") or nonempty[0].startswith("## "):
        errors.append(f"{relative} must start with an H1")
    if len([line for line in lines if line.startswith("# ")]) != 1:
        errors.append(f"{relative} must contain exactly one H1")
    first_h2_index = next((index for index, line in enumerate(lines) if line.startswith("## ")), len(lines))
    if not _nonempty_lines_between(lines, 1, first_h2_index):
        errors.append(f"{relative} must contain a summary paragraph")
    headings = [line.removeprefix("## ") for line in lines if line.startswith("## ")]
    if (
        len(headings) != 4
        or headings[:2] != list(HEADING_ORDER[:2])
        or not headings[2].startswith(HEADING_ORDER[2])
        or headings[3] != HEADING_ORDER[3]
    ):
        errors.append(f"{relative} has the wrong feature section contract")

    driving = next((index for index, line in enumerate(lines) if line.startswith("## Driving it with ")), None)
    if driving is None:
        return
    first_after_heading = next(
        (line.strip() for line in lines[driving + 1 :] if line.strip()),
        "",
    )
    if first_after_heading != "Preconditions:":
        errors.append(f"{relative} driving section must start with Preconditions:")
    for label in (*CONTEXT_LABELS.values(), "Actors", "Fixtures"):
        _require_label(lines[driving + 1 :], label, str(relative), errors)

    action_starts = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"- \*\*.+\.\*\*", line)
    ]
    if not action_starts:
        errors.append(f"{relative} must contain at least one action record")
    for action_index, start in enumerate(action_starts):
        end = action_starts[action_index + 1] if action_index + 1 < len(action_starts) else len(lines)
        block = lines[start:end]
        for label in ("Actors", "Fixtures", "Action", "Expected result", "Failure signals", "Evidence"):
            _require_label(block, label, str(relative), errors)
        _validate_rendered_command(block, "Action", str(relative), errors)
        evidence_line = next((line.strip() for line in block if line.strip().startswith("- Evidence:")), None)
        evidence_match = re.match(r"^- Evidence: `([^`]+)`(?:,|$)", evidence_line or "")
        if evidence_match is None:
            errors.append(f"{relative} action evidence must name an artifact path")
        elif evidence_root is not None:
            try:
                evidence_path = _safe_relative_path(evidence_match.group(1), f"{relative} evidence path")
                if not _path_is_within(evidence_path, evidence_root):
                    errors.append(f"{relative} evidence path escapes the evidence root")
            except ScaffoldError as exc:
                errors.append(str(exc))


def validate_generated_skill(
    skill_root: Path,
    *,
    expected_name: str | None = None,
    enforce_location: bool = True,
) -> tuple[str, ...]:
    """Return structural errors for a generated skill tree."""

    skill_root = _lexical_absolute(skill_root)
    errors = _tree_errors(skill_root)
    if errors:
        return tuple(errors)
    if enforce_location:
        location_error = _skill_location_error(skill_root)
        if location_error:
            errors.append(location_error)
    expected_name = expected_name or skill_root.name
    skill_file = skill_root / "SKILL.md"
    features_dir = skill_root / "features"
    index = features_dir / "README.md"
    if not skill_file.is_file():
        errors.append("missing SKILL.md")
    if not features_dir.is_dir():
        errors.append("missing features directory")
    if not index.is_file():
        errors.append("missing features/README.md")
    if errors:
        return tuple(errors)

    disposable_root: str | None = None
    evidence_root: str | None = None
    try:
        frontmatter = _parse_frontmatter(skill_file)
        _validate_frontmatter_fields(frontmatter, expected_name=expected_name, path=skill_file)
        skill_lines = skill_file.read_text(encoding="utf-8").splitlines()
        disposable_root = _code_label_value(skill_lines, "Disposable root")
        if disposable_root is None:
            errors.append("SKILL.md must name a relative Disposable root")
        else:
            try:
                disposable_root = _safe_relative_path(disposable_root, "SKILL.md Disposable root")
            except ScaffoldError as exc:
                errors.append(str(exc))
                disposable_root = None
        disposable_paths = _inline_code_values(skill_lines, "Disposable paths")
        if not disposable_paths:
            errors.append("SKILL.md must name disposable paths")
        elif disposable_root is not None:
            for path in disposable_paths:
                try:
                    normalized = _safe_relative_path(path, "SKILL.md disposable path")
                    if not _path_is_within(normalized, disposable_root):
                        errors.append("SKILL.md disposable path escapes the disposable root")
                except ScaffoldError as exc:
                    errors.append(str(exc))
        evidence_root = _code_label_value(skill_lines, "Evidence root")
        if evidence_root is None:
            errors.append("SKILL.md must name a relative Evidence root")
        else:
            try:
                evidence_root = _safe_relative_path(evidence_root, "SKILL.md Evidence root")
            except ScaffoldError as exc:
                errors.append(str(exc))
                evidence_root = None
        headings = [line.removeprefix("## ") for line in skill_lines if line.startswith("## ")]
        if headings != list(SKILL_HEADING_ORDER):
            errors.append("generated SKILL.md sections are incomplete or out of order")
        for section_name in ("Launch", "Doctor", "Evidence", "Cleanup"):
            section = _section_lines(skill_lines, f"## {section_name}")
            if section is None:
                continue
            for label in CONTEXT_LABELS.values():
                _require_label(section[2], label, f"SKILL.md {section_name}", errors)
        for section_name in SKILL_HEADING_ORDER[:-1]:
            section = _section_lines(skill_lines, f"## {section_name}")
            if section is None:
                continue
            if section_name in {"Launch", "Doctor", "Drive", "Cleanup"}:
                _validate_rendered_command(
                    section[2],
                    "Command",
                    f"SKILL.md {section_name}",
                    errors,
                    forbidden_roots=(evidence_root,)
                    if section_name == "Cleanup" and evidence_root is not None
                    else (),
                )
        maintenance = _section_lines(skill_lines, "## Maintenance")
        maintenance_ok = maintenance is not None and any(
            "#4 follow-up" in line and "unsupported until #4" in line
            for line in maintenance[2]
        )
        if not maintenance_ok:
            errors.append("SKILL.md must state the unsupported #4 maintenance handoff")
        _check_links(skill_root)
    except (OSError, ScaffoldError) as exc:
        errors.append(str(exc))

    feature_files = sorted(path for path in features_dir.glob("*.md") if path.name != "README.md")
    if not feature_files:
        errors.append("feature map has no feature files")
    _validate_index(
        index,
        feature_files,
        skill_root,
        errors,
        disposable_root=disposable_root,
        evidence_root=evidence_root,
    )
    for path in feature_files:
        try:
            _validate_feature(path, skill_root, errors, evidence_root=evidence_root)
        except OSError as exc:
            errors.append(f"{path.relative_to(skill_root)} cannot be read: {exc}")
    return tuple(errors)


def _remove_path(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink()
    else:
        shutil.rmtree(path)


def _remove_empty_parents(created_parents: list[Path]) -> None:
    for directory in reversed(created_parents):
        try:
            mode = os.lstat(directory).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def _existing_directory(path: Path, label: str) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise ScaffoldError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise ScaffoldError(f"{label} must be a directory: {path}")
    return True


def scaffold(spec: dict[str, Any], project_root: Path) -> Path:
    """Create a new generated skill and return its path."""

    spec = validate_spec(spec)
    project_root = _lexical_absolute(project_root.expanduser())
    if _symlink_component_errors(project_root):
        raise ScaffoldError("project root or one of its parents is a symlink")
    try:
        project_mode = os.lstat(project_root).st_mode
    except OSError as exc:
        raise ScaffoldError(f"project root cannot be inspected: {exc}") from exc
    if stat.S_ISLNK(project_mode) or not stat.S_ISDIR(project_mode):
        raise ScaffoldError("project root must be an existing non-symlink directory")

    target = project_root / ".agents" / "skills" / f"verify-{spec['app']['name']}"
    if os.path.lexists(target):
        raise CollisionError(f"output already exists: {target}")
    parent = project_root / ".agents"
    skills_parent = parent / "skills"
    created_parents: list[Path] = []
    staging: Path | None = None
    target_reserved = False
    moved_names: list[str] = []
    try:
        for directory, label in ((parent, ".agents"), (skills_parent, ".agents/skills")):
            if not _existing_directory(directory, f"output parent {label}"):
                try:
                    directory.mkdir()
                except FileExistsError:
                    _existing_directory(directory, f"output parent {label}")
                else:
                    created_parents.append(directory)
        staging = Path(tempfile.mkdtemp(prefix=f".verify-{spec['app']['name']}.staging-", dir=skills_parent))
        _write_tree(spec, staging)
        errors = validate_generated_skill(
            staging,
            expected_name=target.name,
            enforce_location=False,
        )
        if errors:
            raise ScaffoldError("generated skill failed validation: " + "; ".join(errors))

        # Reserve the final directory with an exclusive mkdir. Unlike rename,
        # this cannot replace a target that appeared after the initial check.
        try:
            os.mkdir(target)
        except FileExistsError as exc:
            raise CollisionError(f"output appeared during generation: {target}") from exc
        target_reserved = True
        for child in sorted(staging.iterdir(), key=lambda path: path.name):
            destination = target / child.name
            if os.path.lexists(destination):
                raise CollisionError(f"target changed during publication: {destination}")
            child.rename(destination)
            moved_names.append(child.name)
        staging.rmdir()
        staging = None
        return target
    except Exception:
        if staging is not None:
            _remove_path(staging)
        if target_reserved:
            for name in reversed(moved_names):
                _remove_path(target / name)
            try:
                target.rmdir()
            except OSError:
                pass
        _remove_empty_parents(created_parents)
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
