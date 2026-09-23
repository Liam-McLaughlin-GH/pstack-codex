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
INDIRECT_LAUNCHERS = frozenset(
    {"busybox", "chroot", "command", "doas", "env", "exec", "nice", "nohup", "sudo", "timeout", "xargs"}
)
SHELL_COMMAND_FLAGS = frozenset(
    {
        "-c",
        "/c",
        "-command",
        "--command",
        "-encodedcommand",
        "--encodedcommand",
        "-ec",
        "-lc",
        "-xc",
    }
)
INTERPRETER_CODE_FLAGS = {
    "node": frozenset({"-e", "--eval"}),
    "nodejs": frozenset({"-e", "--eval"}),
    "deno": frozenset({"eval", "-e", "--eval"}),
    "lua": frozenset({"-e", "--execute"}),
    "osascript": frozenset({"-e"}),
    "perl": frozenset({"-e", "--execute"}),
    "php": frozenset({"-r", "--run"}),
    "pypy": frozenset({"-c", "--command"}),
    "powershell": SHELL_COMMAND_FLAGS,
    "pwsh": SHELL_COMMAND_FLAGS,
    "ruby": frozenset({"-e", "--eval"}),
}
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


def _command_basename(value: str) -> str:
    return re.split(r"[/\\]", value)[-1].casefold()


COMMAND_FAMILIES = frozenset(
    {
        *SHELL_EXECUTABLES,
        *INDIRECT_LAUNCHERS,
        *INTERPRETER_CODE_FLAGS,
        "python",
    }
)


def _command_family(value: str) -> str:
    """Normalize known command families across versions and Windows suffixes."""

    basename = _command_basename(value)
    if basename.endswith(".exe"):
        basename = basename[:-4]
    for family in sorted(COMMAND_FAMILIES, key=len, reverse=True):
        if basename == family:
            return family
        if re.fullmatch(rf"{re.escape(family)}[0-9]+(?:\.[0-9]+)*", basename):
            return family
    return basename


def _option_name(value: str) -> str:
    return value.split("=", 1)[0].casefold()


def _interpreter_code_flags(executable: str) -> frozenset[str]:
    basename = _command_family(executable)
    if basename == "python":
        return frozenset({"-c", "--command"})
    return INTERPRETER_CODE_FLAGS.get(basename, frozenset())


def _matches_code_flag(value: str, flags: frozenset[str]) -> bool:
    option = _option_name(value)
    if option in flags:
        return True
    return any(
        flag.startswith("-")
        and not flag.startswith("--")
        and len(flag) == 2
        and option.startswith(flag)
        for flag in flags
    )


def _validate_command_boundary(executable: str, argv: list[str], label: str) -> None:
    """Reject shell, launcher, and interpreter code-string boundaries."""

    basename = _command_family(executable)
    if basename in SHELL_EXECUTABLES:
        raise ScaffoldError(f"{label} must not introduce a free-form shell command")
    if basename in INDIRECT_LAUNCHERS:
        raise ScaffoldError(f"{label} must not use an indirect command launcher")

    tokens = [executable, *argv]
    for index, token in enumerate(tokens):
        token_basename = _command_family(token)
        if token_basename in SHELL_EXECUTABLES:
            if any(_matches_code_flag(candidate, SHELL_COMMAND_FLAGS) for candidate in tokens[index + 1 :]):
                raise ScaffoldError(f"{label} must not introduce a free-form shell command")
        code_flags = _interpreter_code_flags(token)
        if code_flags and any(
            _matches_code_flag(candidate, code_flags) for candidate in tokens[index + 1 :]
        ):
            raise ScaffoldError(
                f"{label} must not introduce an interpreter code-string boundary"
            )


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


def _validate_command_path_form(value: str, label: str, *, allow_absolute: bool = False) -> None:
    if "\\" in value:
        raise ScaffoldError(f"{label} must not contain Windows path separators")
    if re.match(r"^[A-Za-z]:", value):
        raise ScaffoldError(f"{label} must not contain a Windows drive path")
    if value.startswith("//"):
        raise ScaffoldError(f"{label} must not contain a UNC path")
    if not allow_absolute and value.startswith(("/", "~")):
        raise ScaffoldError(f"{label} must not contain an absolute path")
    if ".." in PurePosixPath(value).parts:
        raise ScaffoldError(f"{label} must not escape the project with '..'")


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
    _validate_command_path_form(executable, f"{label}.executable", allow_absolute=True)
    argv = command.get("argv")
    if not isinstance(argv, list):
        raise ScaffoldError(f"{label}.argv must be an array")
    for index, argument in enumerate(argv):
        argument = _require_text(argument, f"{label}.argv[{index}]")
        for candidate in _command_argument_candidates(argument):
            _validate_command_path_form(candidate, f"{label}.argv[{index}]")
            if candidate.casefold().startswith("file:"):
                raise ScaffoldError(
                    f"{label}.argv[{index}] must not contain a file URI"
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
    _validate_command_boundary(executable, argv, label)
    return command


DISPOSABLE_TARGET_EXECUTABLES = frozenset(
    {"del", "erase", "rm", "rmdir", "shred", "unlink"}
)
DISPOSABLE_COMMAND_WORDS = frozenset({"clean", "cleanup", "delete", "remove", "teardown"})
DISPOSABLE_PATH_OPTIONS = frozenset(
    {
        "--data-dir",
        "--directory",
        "--dir",
        "--file",
        "--output",
        "--out",
        "--path",
        "--state-file",
        "--state",
        "--target",
        "--workdir",
        "-d",
        "-f",
        "-o",
    }
)


def _normalize_cleanup_path(value: str, label: str) -> str:
    """Normalize a relative cleanup target without allowing it to escape."""

    value = _require_text(value, label)
    if "\\" in value or value.startswith(("/", "~", "//")):
        raise ScaffoldError(f"{label} must be a relative POSIX path")
    if re.match(r"^[A-Za-z]:", value) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
        raise ScaffoldError(f"{label} must not be an absolute path or URI")

    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ScaffoldError(f"{label} must not contain traversal '..'")
        parts.append(part)
    return "/".join(parts)


def _cleanup_target_candidates(command: dict[str, Any]) -> list[tuple[int, str]]:
    """Return argv values that a cleanup command treats as filesystem targets."""

    argv = command["argv"]
    basename = _command_family(command["executable"])
    candidates: list[tuple[int, str]] = []

    if basename in DISPOSABLE_TARGET_EXECUTABLES:
        after_options = False
        for index, argument in enumerate(argv):
            if not after_options and argument == "--":
                after_options = True
                continue
            if not after_options and argument.startswith("-"):
                continue
            candidates.append((index, argument))
        return candidates

    script_index = 0 if _interpreter_code_flags(command["executable"]) else None
    consumed_values: set[int] = set()
    after_options = False
    for index, argument in enumerate(argv):
        if index in consumed_values:
            continue
        if after_options:
            candidates.append((index, argument))
            continue
        if argument == "--":
            after_options = True
            continue
        if argument.startswith("-") and "=" in argument:
            option, value = argument.split("=", 1)
            if option.casefold() in DISPOSABLE_PATH_OPTIONS:
                if not value:
                    raise ScaffoldError(f"cleanup path option {option!r} must have a value")
                candidates.append((index, value))
                continue
        if _option_name(argument) in DISPOSABLE_PATH_OPTIONS:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                raise ScaffoldError(f"cleanup path option {argument!r} must have a value")
            consumed_values.add(index + 1)
            candidates.append((index + 1, argv[index + 1]))
            continue
        if index == script_index or argument.startswith("-"):
            continue
        if argument.casefold() in DISPOSABLE_COMMAND_WORDS:
            continue
        if argument:
            candidates.append((index, argument))

    deduplicated: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduplicated.append(candidate)
    return deduplicated


def _validate_cleanup_command(
    value: Any,
    label: str,
    *,
    disposable_root: str,
    disposable_paths: tuple[str, ...],
    evidence_root: str,
) -> dict[str, Any]:
    """Validate a typed cleanup command against its declared disposable state."""

    command = _validate_command(value, label, forbidden_roots=(evidence_root,))
    candidates = _cleanup_target_candidates(command)
    if not candidates:
        raise ScaffoldError(f"{label} must name at least one disposable target")

    cwd = command.get("cwd", "")
    for index, candidate in candidates:
        normalized_candidate = _normalize_cleanup_path(
            candidate,
            f"{label}.argv[{index}]",
        )
        combined = "/".join(part for part in (cwd, normalized_candidate) if part)
        normalized_target = _normalize_cleanup_path(
            combined or ".",
            f"{label}.argv[{index}]",
        )
        if not normalized_target:
            raise ScaffoldError(f"{label}.argv[{index}] must not target the project root")
        if _path_is_within(normalized_target, evidence_root):
            raise ScaffoldError(
                f"{label}.argv[{index}] must not target the evidence root or a path below it"
            )
        declared_path = any(
            normalized_target == allowed or _path_is_within(normalized_target, allowed)
            for allowed in disposable_paths
        )
        if normalized_target != disposable_root and not declared_path:
            raise ScaffoldError(
                f"{label}.argv[{index}] must target declared disposable state"
            )
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
    _validate_cleanup_command(
        app["cleanup"],
        "app.cleanup",
        disposable_root=disposable_root,
        disposable_paths=tuple(
            _normalize_cleanup_path(path, f"app.disposable_paths[{index}]")
            for index, path in enumerate(app["disposable_paths"])
        ),
        evidence_root=evidence_root,
    )
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
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
        if value.startswith(("#", "- ", "? ")) or re.search(r":(?:[ \t]|$|[\[\]{},])", value):
            raise ScaffoldError(f"frontmatter field {key!r} has invalid plain scalar syntax")
        if _is_non_string_yaml_scalar(value):
            raise ScaffoldError(f"frontmatter field {key!r} must be a string")
        fields[key] = value
    return fields


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ScaffoldError(f"cannot decode {path} as UTF-8: {exc}") from exc
    except OSError as exc:
        raise ScaffoldError(f"cannot read {path}: {exc}") from exc


def _parse_frontmatter(path: Path) -> dict[str, str]:
    text = _read_utf8(path)
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
                if not isinstance(key, str):
                    raise ScaffoldError("frontmatter mapping keys must be strings")
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

    if any(not isinstance(key, str) for key in fields):
        raise ScaffoldError("frontmatter mapping keys must be strings")
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
        text = _strip_markdown_code(_read_utf8(path))
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
    found_label = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        found_label = True
        if stripped[len(prefix) :].strip():
            continue
        label_indent = len(line) - len(line.lstrip(" \t"))
        child_index = index + 1
        found_child = False
        while child_index < len(lines):
            child = lines[child_index]
            if not child.strip():
                break
            child_indent = len(child) - len(child.lstrip(" \t"))
            if child_indent <= label_indent:
                break
            child_match = re.match(r"^[ \t]+-[ \t]*(?P<value>.*)$", child)
            if child_match is None:
                break
            found_child = True
            if not child_match.group("value").strip():
                errors.append(f"{location} {label} must have a non-empty value")
                return
            child_index += 1
        if found_child:
            continue
        errors.append(f"{location} {label} must have a non-empty value")
        return
    if not found_label:
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


def _parse_rendered_command(
    lines: list[str],
    label: str,
    location: str,
    errors: list[str],
) -> dict[str, Any] | None:
    prefix = f"- {label}:"
    command_lines = [line for line in lines if line.strip().startswith(prefix)]
    if len(command_lines) != 1:
        errors.append(f"{location} must contain exactly one {label} command")
        return None
    rendered = _inline_code_label_value(command_lines[0], label)
    if not rendered:
        errors.append(f"{location} {label} must be a non-empty inline code command")
        return None
    try:
        argv = shlex.split(rendered, posix=True)
    except ValueError as exc:
        errors.append(f"{location} {label} has invalid command quoting: {exc}")
        return None
    if not argv:
        errors.append(f"{location} {label} must contain an executable")
        return None

    command: dict[str, Any] = {"executable": argv[0], "argv": argv[1:]}
    cwd_prefix = "- Working directory:"
    cwd_lines = [line for line in lines if line.strip().startswith(cwd_prefix)]
    if len(cwd_lines) > 1:
        errors.append(f"{location} must contain at most one Working directory")
        return None
    if cwd_lines:
        cwd = _inline_code_label_value(cwd_lines[0], "Working directory")
        if not cwd:
            errors.append(f"{location} Working directory must be a non-empty inline code path")
            return None
        command["cwd"] = cwd
    return command


def _validate_rendered_command(
    lines: list[str],
    label: str,
    location: str,
    errors: list[str],
    *,
    forbidden_roots: tuple[str, ...] = (),
) -> None:
    command = _parse_rendered_command(lines, label, location, errors)
    if command is None:
        return

    try:
        _validate_command(command, f"{location}.{label}", forbidden_roots=forbidden_roots)
    except ScaffoldError as exc:
        errors.append(str(exc))


def _validate_rendered_cleanup(
    lines: list[str],
    location: str,
    errors: list[str],
    *,
    disposable_root: str | None,
    disposable_paths: tuple[str, ...],
    evidence_root: str | None,
) -> None:
    command = _parse_rendered_command(lines, "Command", location, errors)
    if command is None:
        return
    try:
        if disposable_root is None or evidence_root is None:
            raise ScaffoldError(
                f"{location}.Command cleanup policy cannot be validated without both roots"
            )
        _validate_cleanup_command(
            command,
            f"{location}.Command",
            disposable_root=disposable_root,
            disposable_paths=disposable_paths,
            evidence_root=evidence_root,
        )
    except ScaffoldError as exc:
        errors.append(str(exc))


def _inline_code_values(lines: list[str], label: str) -> list[str]:
    prefix = f"- {label}:"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return re.findall(r"`([^`]+)`", stripped[len(prefix) :])
    return []


def _validate_generated_root_contract(
    disposable_root: str | None,
    evidence_root: str | None,
    disposable_paths: tuple[str, ...],
    location: str,
    errors: list[str],
) -> None:
    if disposable_root is not None and evidence_root is not None:
        if _path_is_within(disposable_root, evidence_root) or _path_is_within(
            evidence_root, disposable_root
        ):
            errors.append(f"{location} disposable and evidence roots must not overlap")
    if disposable_root is None:
        return
    for path in disposable_paths:
        try:
            normalized = _safe_relative_path(path, f"{location} disposable path")
            if not _path_is_within(normalized, disposable_root):
                errors.append(f"{location} disposable path escapes the disposable root")
        except ScaffoldError as exc:
            errors.append(str(exc))


def _validate_index(
    index: Path,
    feature_files: list[Path],
    root: Path,
    errors: list[str],
    *,
    disposable_root: str | None = None,
    evidence_root: str | None = None,
) -> None:
    lines = _read_utf8(index).splitlines()
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
    links = re.findall(r"\]\(\./([a-z0-9-]+)\.md\)", _read_utf8(index))
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
    if index_disposable_root is not None and index_evidence_root is not None:
        try:
            normalized_disposable = _safe_relative_path(
                index_disposable_root,
                "features/README.md Disposable root",
            )
            normalized_evidence = _safe_relative_path(
                index_evidence_root,
                "features/README.md Evidence root",
            )
            if _path_is_within(normalized_disposable, normalized_evidence) or _path_is_within(
                normalized_evidence, normalized_disposable
            ):
                errors.append(
                    "features/README.md disposable and evidence roots must not overlap"
                )
        except ScaffoldError:
            pass


def _validate_feature(
    path: Path,
    root: Path,
    errors: list[str],
    *,
    evidence_root: str | None = None,
) -> None:
    relative = path.relative_to(root)
    lines = _read_utf8(path).splitlines()
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
        evidence_match = re.fullmatch(
            r"^- Evidence: `([^`]+)`,[ \t]*(?P<proves>\S.*)$",
            evidence_line or "",
        )
        if evidence_match is None:
            errors.append(f"{relative} action evidence must name an artifact path and proof")
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
    disposable_paths: tuple[str, ...] = ()
    try:
        frontmatter = _parse_frontmatter(skill_file)
        _validate_frontmatter_fields(frontmatter, expected_name=expected_name, path=skill_file)
        skill_lines = _read_utf8(skill_file).splitlines()
        disposable_root = _code_label_value(skill_lines, "Disposable root")
        if disposable_root is None:
            errors.append("SKILL.md must name a relative Disposable root")
        else:
            try:
                disposable_root = _safe_relative_path(disposable_root, "SKILL.md Disposable root")
            except ScaffoldError as exc:
                errors.append(str(exc))
                disposable_root = None
        raw_disposable_paths = _inline_code_values(skill_lines, "Disposable paths")
        if not raw_disposable_paths:
            errors.append("SKILL.md must name disposable paths")
        else:
            normalized_paths: list[str] = []
            for path in raw_disposable_paths:
                try:
                    normalized_paths.append(_safe_relative_path(path, "SKILL.md disposable path"))
                except ScaffoldError as exc:
                    errors.append(str(exc))
            disposable_paths = tuple(normalized_paths)
        evidence_root = _code_label_value(skill_lines, "Evidence root")
        if evidence_root is None:
            errors.append("SKILL.md must name a relative Evidence root")
        else:
            try:
                evidence_root = _safe_relative_path(evidence_root, "SKILL.md Evidence root")
            except ScaffoldError as exc:
                errors.append(str(exc))
                evidence_root = None
        _validate_generated_root_contract(
            disposable_root,
            evidence_root,
            disposable_paths,
            "SKILL.md",
            errors,
        )
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
            if section_name == "Cleanup":
                _validate_rendered_cleanup(
                    section[2],
                    "SKILL.md Cleanup",
                    errors,
                    disposable_root=disposable_root,
                    disposable_paths=disposable_paths,
                    evidence_root=evidence_root,
                )
            elif section_name in {"Launch", "Doctor", "Drive"}:
                _validate_rendered_command(
                    section[2],
                    "Command",
                    f"SKILL.md {section_name}",
                    errors,
                )
        maintenance = _section_lines(skill_lines, "## Maintenance")
        maintenance_ok = maintenance is not None and any(
            "#4 follow-up" in line and "unsupported until #4" in line
            for line in maintenance[2]
        )
        if not maintenance_ok:
            errors.append("SKILL.md must state the unsupported #4 maintenance handoff")
        _check_links(skill_root)
    except (OSError, UnicodeError, ScaffoldError) as exc:
        errors.append(str(exc))

    feature_files = sorted(path for path in features_dir.glob("*.md") if path.name != "README.md")
    if not feature_files:
        errors.append("feature map has no feature files")
    try:
        _validate_index(
            index,
            feature_files,
            skill_root,
            errors,
            disposable_root=disposable_root,
            evidence_root=evidence_root,
        )
    except (OSError, UnicodeError, ScaffoldError) as exc:
        errors.append(f"features/README.md cannot be read: {exc}")
    for path in feature_files:
        try:
            _validate_feature(path, skill_root, errors, evidence_root=evidence_root)
        except (OSError, UnicodeError, ScaffoldError) as exc:
            errors.append(f"{path.relative_to(skill_root)} cannot be read: {exc}")
    return tuple(errors)


PublishedEntry = tuple[int, str, int, int, bool]


def _descriptor_identity(descriptor: int, label: str, *, is_directory: bool) -> tuple[int, int]:
    try:
        identity = os.fstat(descriptor)
    except OSError as exc:
        raise ScaffoldError(f"{label} descriptor cannot be inspected") from exc
    if is_directory and not stat.S_ISDIR(identity.st_mode):
        raise ScaffoldError(f"{label} is not a directory")
    if not is_directory and not stat.S_ISREG(identity.st_mode):
        raise ScaffoldError(f"{label} is not a regular file")
    return identity.st_dev, identity.st_ino


def _open_directory_descriptor(
    name: Path | str,
    label: str,
    *,
    dir_fd: int | None = None,
    collision: bool = False,
) -> int:
    """Open a directory without following the final path component."""

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow_flag:
        raise ScaffoldError("descriptor-bound publication is unavailable on this platform")
    flags = os.O_RDONLY | directory_flag | nofollow_flag | getattr(os, "O_CLOEXEC", 0)
    error_type = CollisionError if collision else ScaffoldError
    try:
        if dir_fd is None:
            descriptor = os.open(os.fspath(name), flags)
        else:
            descriptor = os.open(os.fspath(name), flags, dir_fd=dir_fd)
    except OSError as exc:
        raise error_type(f"{label} cannot be opened without following symlinks: {name}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise error_type(f"{label} is not a directory: {name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_or_create_directory(
    parent_fd: int,
    name: str,
    label: str,
) -> tuple[int, bool]:
    """Open or create one directory entry without resolving through its parent path."""

    try:
        return _open_directory_descriptor(name, label, dir_fd=parent_fd), False
    except ScaffoldError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise

    try:
        os.mkdir(name, mode=0o777, dir_fd=parent_fd)
    except FileExistsError:
        return _open_directory_descriptor(name, label, dir_fd=parent_fd), False

    try:
        return _open_directory_descriptor(name, label, dir_fd=parent_fd), True
    except Exception:
        # Do not remove the entry here: if a concurrent actor replaced it after
        # mkdir, a path-based cleanup could delete that actor's directory.
        raise


def _published_identity(
    parent_fd: int,
    name: str,
    *,
    is_directory: bool,
    entry_fd: int | None = None,
) -> PublishedEntry:
    """Record an entry's identity, preferring the descriptor that was opened."""

    if entry_fd is None:
        identity = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        device, inode = identity.st_dev, identity.st_ino
    else:
        device, inode = _descriptor_identity(
            entry_fd,
            f"published entry {name}",
            is_directory=is_directory,
        )
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise CollisionError(f"published entry changed during publication: {name}") from exc
        if (current.st_dev, current.st_ino) != (device, inode):
            raise CollisionError(f"published entry changed during publication: {name}")
    if is_directory and entry_fd is None and not stat.S_ISDIR(identity.st_mode):
        raise CollisionError(f"published entry is not a directory: {name}")
    if not is_directory and entry_fd is None and not stat.S_ISREG(identity.st_mode):
        raise CollisionError(f"published entry is not a regular file: {name}")
    return os.dup(parent_fd), name, device, inode, is_directory


def _create_staging_directory(parent_fd: int, prefix: str) -> tuple[str, int]:
    """Create a private staging directory relative to an already-open parent."""

    candidates = tempfile._get_candidate_names()
    for _ in range(100):
        name = f"{prefix}{next(candidates)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue

        descriptor: int | None = None
        try:
            descriptor = _open_directory_descriptor(
                name,
                "staging directory",
                dir_fd=parent_fd,
                collision=True,
            )
            reservation = _published_identity(
                parent_fd,
                name,
                is_directory=True,
                entry_fd=descriptor,
            )
            os.close(reservation[0])
            return name, descriptor
        except Exception:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
    raise ScaffoldError("could not reserve a unique staging directory")


def _assert_path_matches_descriptor(path: Path, descriptor: int, label: str) -> None:
    """Reject a path that no longer names the directory held by descriptor."""

    if _symlink_component_errors(path):
        raise CollisionError(f"{label} contains a symlinked path component: {path}")
    try:
        path_stat = os.stat(path, follow_symlinks=False)
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise CollisionError(f"{label} changed during publication: {path}") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or not stat.S_ISDIR(descriptor_stat.st_mode)
        or (path_stat.st_dev, path_stat.st_ino) != (descriptor_stat.st_dev, descriptor_stat.st_ino)
    ):
        raise CollisionError(f"{label} changed during publication: {path}")


def _close_published(entries: list[PublishedEntry]) -> None:
    for parent_fd, _, _, _, _ in entries:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _rollback_published(entries: list[PublishedEntry]) -> None:
    for parent_fd, name, device, inode, is_directory in reversed(entries):
        try:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if current.st_dev != device or current.st_ino != inode:
                continue
            try:
                if is_directory:
                    os.rmdir(name, dir_fd=parent_fd)
                else:
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        finally:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _assert_reserved_target_identity(
    parent_fd: int,
    target_name: str,
    target: Path,
    target_fd: int,
    identity: tuple[int, int],
) -> None:
    """Refuse publication if the reserved target path no longer names its inode."""

    try:
        current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        current_fd = os.fstat(target_fd)
    except OSError as exc:
        raise CollisionError(f"target changed during publication: {target}") from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or (current_fd.st_dev, current_fd.st_ino) != identity
    ):
        raise CollisionError(f"target changed during publication: {target}")


def _remove_reserved_target(parent_fd: int, target_name: str, identity: tuple[int, int]) -> None:
    """Remove only the inode reserved by this run, never a replacement entry."""

    try:
        current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        return
    try:
        os.rmdir(target_name, dir_fd=parent_fd)
    except OSError:
        pass


def _remove_reserved_directory_tree(entry: PublishedEntry) -> bool:
    """Remove a reserved directory tree only if the entry still has its inode."""

    parent_fd, name, device, inode, is_directory = entry
    try:
        if not is_directory or not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
            return False
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (device, inode)
        ):
            return False
        try:
            shutil.rmtree(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return False
        except (NotImplementedError, OSError):
            return False
        return True
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _publish_entry_no_replace(
    source: Path,
    destination: Path,
    *,
    destination_dir_fd: int,
    source_dir_fd: int | None = None,
    source_name: str | None = None,
) -> list[PublishedEntry]:
    """Publish one staged entry through a reserved directory descriptor."""

    # Keep this lexical probe for collision races, but never use the path for a
    # write. The descriptor below remains bound to the reserved directory.
    if os.path.lexists(destination):
        raise CollisionError(f"target changed during publication: {destination}")
    destination_name = destination.name
    try:
        os.stat(destination_name, dir_fd=destination_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CollisionError(f"target changed during publication: {destination}") from exc
    else:
        raise CollisionError(f"target changed during publication: {destination}")

    if source_dir_fd is None:
        source_name = source.name
        source_stat = os.lstat(source)
    else:
        if source_name is None:
            raise ScaffoldError(f"staging source name is missing: {source}")
        try:
            source_stat = os.stat(source_name, dir_fd=source_dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise ScaffoldError(f"staging source cannot be inspected: {source}") from exc
    source_mode = source_stat.st_mode
    published: list[PublishedEntry] = []
    if stat.S_ISDIR(source_mode):
        child_fd: int | None = None
        source_child_fd: int | None = None
        published_directory = False
        child_identity: tuple[int, int] | None = None
        try:
            os.mkdir(destination_name, dir_fd=destination_dir_fd)
        except FileExistsError as exc:
            raise CollisionError(f"target changed during publication: {destination}") from exc
        try:
            child_fd = _open_directory_descriptor(
                destination_name,
                "published directory",
                dir_fd=destination_dir_fd,
                collision=True,
            )
            published.append(
                _published_identity(
                    destination_dir_fd,
                    destination_name,
                    is_directory=True,
                    entry_fd=child_fd,
                )
            )
            published_directory = True
            if source_dir_fd is not None:
                source_child_fd = _open_directory_descriptor(
                    source_name,
                    "staging source directory",
                    dir_fd=source_dir_fd,
                    collision=True,
                )
                if _descriptor_identity(
                    source_child_fd,
                    f"staging source {source}",
                    is_directory=True,
                ) != (source_stat.st_dev, source_stat.st_ino):
                    raise CollisionError(f"staging source changed during publication: {source}")
        except Exception:
            if not published_directory and child_fd is not None:
                try:
                    child_identity = _descriptor_identity(
                        child_fd,
                        f"published directory {destination_name}",
                        is_directory=True,
                    )
                except ScaffoldError:
                    child_identity = None
            if child_fd is not None:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
            if not published_directory and child_identity is not None:
                _remove_reserved_directory_tree(
                    (
                        os.dup(destination_dir_fd),
                        destination_name,
                        *child_identity,
                        True,
                    )
                )
            elif published_directory:
                _rollback_published(published)
            raise
        try:
            if source_child_fd is None:
                child_names = sorted(path.name for path in source.iterdir())
            else:
                with os.scandir(source_child_fd) as entries:
                    child_names = sorted(entry.name for entry in entries)
            for child_name in child_names:
                child = source / child_name
                published.extend(
                    _publish_entry_no_replace(
                        child,
                        destination / child_name,
                        destination_dir_fd=child_fd,
                        source_dir_fd=source_child_fd,
                        source_name=child_name,
                    )
                )
            # Keep the staged source intact until the top-level reservation is
            # removed through its descriptor-bound cleanup path.
            return published
        except Exception:
            _rollback_published(published)
            raise
        finally:
            if source_child_fd is not None:
                try:
                    os.close(source_child_fd)
                except OSError:
                    pass
            if child_fd is not None:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
    if not stat.S_ISREG(source_mode):
        raise ScaffoldError(f"staging contains a non-regular entry: {source}")

    try:
        # A hard link through the reserved descriptor is an atomic no-replace
        # publication that cannot follow a replacement target symlink.
        if source_dir_fd is None:
            os.link(
                source,
                destination_name,
                dst_dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
        else:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
    except FileExistsError as exc:
        raise CollisionError(f"target changed during publication: {destination}") from exc
    published.append(
        _published_identity(destination_dir_fd, destination_name, is_directory=False)
    )
    # The top-level staging reservation removes the source tree after all
    # entries publish; do not unlink a name that could have been replaced.
    return published


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
    project_fd: int | None = None
    parent_fd: int | None = None
    created_parent_entries: list[PublishedEntry] = []
    staging: Path | None = None
    staging_name: str | None = None
    staging_fd: int | None = None
    staging_reservation: PublishedEntry | None = None
    target_reserved = False
    published_entries: list[PublishedEntry] = []
    skills_parent_fd: int | None = None
    target_fd: int | None = None
    target_identity: tuple[int, int] | None = None
    try:
        project_fd = _open_directory_descriptor(project_root, "project root")
        parent_fd, parent_created = _open_or_create_directory(
            project_fd,
            ".agents",
            "output parent .agents",
        )
        if parent_created:
            created_parent_entries.append(
                _published_identity(
                    project_fd,
                    ".agents",
                    is_directory=True,
                    entry_fd=parent_fd,
                )
            )
        skills_parent_fd, skills_parent_created = _open_or_create_directory(
            parent_fd,
            "skills",
            "output parent .agents/skills",
        )
        if skills_parent_created:
            created_parent_entries.append(
                _published_identity(
                    parent_fd,
                    "skills",
                    is_directory=True,
                    entry_fd=skills_parent_fd,
                )
            )
        _assert_path_matches_descriptor(skills_parent, skills_parent_fd, "output parent")
        staging_name, staging_fd = _create_staging_directory(
            skills_parent_fd,
            f".verify-{spec['app']['name']}.staging-",
        )
        staging = skills_parent / staging_name
        staging_reservation = _published_identity(
            skills_parent_fd,
            staging_name,
            is_directory=True,
            entry_fd=staging_fd,
        )
        _assert_path_matches_descriptor(staging, staging_fd, "staging directory")
        _write_tree(spec, staging)
        _assert_path_matches_descriptor(skills_parent, skills_parent_fd, "output parent")
        _assert_path_matches_descriptor(staging, staging_fd, "staging directory")
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
            if skills_parent_fd is None:
                raise ScaffoldError("output parent descriptor is unavailable")
            os.mkdir(target.name, dir_fd=skills_parent_fd)
        except FileExistsError as exc:
            raise CollisionError(f"output appeared during generation: {target}") from exc
        target_reserved = True
        target_fd = _open_directory_descriptor(
            target.name,
            "reserved target",
            dir_fd=skills_parent_fd,
            collision=True,
        )
        target_stat = os.fstat(target_fd)
        target_identity = (target_stat.st_dev, target_stat.st_ino)
        _assert_reserved_target_identity(
            skills_parent_fd,
            target.name,
            target,
            target_fd,
            target_identity,
        )
        if staging is None or staging_fd is None:
            raise ScaffoldError("staging directory descriptor is unavailable")
        _assert_path_matches_descriptor(staging, staging_fd, "staging directory")
        with os.scandir(staging_fd) as entries:
            child_names = sorted(entry.name for entry in entries)
        for child_name in child_names:
            _assert_reserved_target_identity(
                skills_parent_fd,
                target.name,
                target,
                target_fd,
                target_identity,
            )
            child = staging / child_name
            destination = target / child_name
            published_entries.extend(
                _publish_entry_no_replace(
                    child,
                    destination,
                    destination_dir_fd=target_fd,
                    source_dir_fd=staging_fd,
                    source_name=child_name,
                )
            )
            _assert_reserved_target_identity(
                skills_parent_fd,
                target.name,
                target,
                target_fd,
                target_identity,
            )
        _assert_reserved_target_identity(
            skills_parent_fd,
            target.name,
            target,
            target_fd,
            target_identity,
        )
        _assert_path_matches_descriptor(skills_parent, skills_parent_fd, "output parent")
        _assert_path_matches_descriptor(target, target_fd, "reserved target")
        if staging_reservation is None or not _remove_reserved_directory_tree(staging_reservation):
            staging_reservation = None
            raise CollisionError(f"staging directory changed during publication: {staging}")
        staging_reservation = None
        staging = None
        _close_published(published_entries)
        published_entries = []
        _close_published(created_parent_entries)
        created_parent_entries = []
        return target
    except Exception:
        if staging_reservation is not None:
            _remove_reserved_directory_tree(staging_reservation)
            staging_reservation = None
        if target_reserved:
            _rollback_published(published_entries)
            if skills_parent_fd is not None and target_identity is not None:
                _remove_reserved_target(skills_parent_fd, target.name, target_identity)
        _rollback_published(created_parent_entries)
        created_parent_entries = []
        raise
    finally:
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except OSError:
                pass
        if target_fd is not None:
            try:
                os.close(target_fd)
            except OSError:
                pass
        if skills_parent_fd is not None:
            try:
                os.close(skills_parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        if project_fd is not None:
            try:
                os.close(project_fd)
            except OSError:
                pass


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
