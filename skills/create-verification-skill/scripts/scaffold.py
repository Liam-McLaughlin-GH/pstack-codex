#!/usr/bin/env python3
"""Scaffold and validate a project-local Codex verification skill.

The helper consumes an explicit JSON specification. Commands are structured
as an executable plus argv, so generated instructions never need a shell
parser. The helper never runs commands from a specification and never
overwrites an existing target.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass, field
import errno
import json
import os
import re
import shlex
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
    "node": frozenset({"-e", "--eval", "-p", "--print"}),
    "nodejs": frozenset({"-e", "--eval", "-p", "--print"}),
    "deno": frozenset({"eval", "-e", "--eval"}),
    "lua": frozenset({"-e", "--execute"}),
    "osascript": frozenset({"-e"}),
    "perl": frozenset({"-e", "--execute"}),
    "php": frozenset({"-r", "--run"}),
    "pypy": frozenset({"-c", "--command"}),
    "py": frozenset({"-c", "--command"}),
    "pythonw": frozenset({"-c", "--command"}),
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


@dataclass(frozen=True)
class CleanupFailure:
    """One cleanup operation that could not be completed during rollback."""

    operation: str
    entry: str
    error_type: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "operation": self.operation,
            "entry": self.entry,
            "error_type": self.error_type,
            "message": self.message,
        }


@dataclass
class RollbackReceipt:
    """Structured accounting for publication cleanup attempts."""

    attempted: int = 0
    cleaned: int = 0
    failures: list[CleanupFailure] = field(default_factory=list)
    _attempted_entries: set[tuple[tuple[int, int], str, int | None, int | None, bool]] = field(
        default_factory=set,
        repr=False,
    )

    def _entry_key(self, entry: PublishedEntry) -> tuple[tuple[int, int], str, int | None, int | None, bool]:
        parent_fd, name, device, inode, is_directory = entry
        try:
            parent = os.fstat(parent_fd)
            parent_identity = (parent.st_dev, parent.st_ino)
        except OSError:
            parent_identity = (-1, parent_fd)
        return parent_identity, name, device, inode, is_directory

    def begin(self, entry: PublishedEntry) -> bool:
        key = self._entry_key(entry)
        if key in self._attempted_entries:
            return False
        self._attempted_entries.add(key)
        self.attempted += 1
        return True

    def record_failure(self, operation: str, entry: str, error: BaseException) -> None:
        self.failures.append(
            CleanupFailure(
                operation=operation,
                entry=entry,
                error_type=type(error).__name__,
                message=str(error),
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "cleaned": self.cleaned,
            "failures": [failure.as_dict() for failure in self.failures],
        }


class RollbackError(CollisionError):
    """Raised when the original operation failed and rollback was incomplete."""

    def __init__(self, cause: BaseException, receipt: RollbackReceipt) -> None:
        self.cause = cause
        self.receipt = receipt
        details = "; ".join(
            f"{failure.operation} {failure.entry}: {failure.message}"
            for failure in receipt.failures
        )
        super().__init__(f"{cause}; rollback incomplete: {details}")


def _record_cleanup_failure(
    receipt: RollbackReceipt | None,
    operation: str,
    entry: str,
    error: BaseException,
) -> None:
    if receipt is not None:
        receipt.record_failure(operation, entry, error)


def _raise_if_rollback_incomplete(cause: BaseException, receipt: RollbackReceipt) -> None:
    if receipt.failures:
        if isinstance(cause, RollbackError):
            raise cause
        raise RollbackError(cause, receipt) from cause


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
    if basename in {"python", "pythonw", "py"}:
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

    ignored_interpreter_values: set[int] = set()
    if basename in {"python", "pythonw", "py"}:
        # Keep the bounded Python launcher forms useful without treating the
        # script or module name as disposable state.  Deliberately support
        # only the option prefixes used by the generated command contract.
        option_index = 0
        while option_index < len(argv) and argv[option_index] == "-u":
            ignored_interpreter_values.add(option_index)
            option_index += 1
        if option_index < len(argv) and argv[option_index] == "-m":
            ignored_interpreter_values.add(option_index)
            option_index += 1
            if option_index < len(argv):
                ignored_interpreter_values.add(option_index)
        elif option_index < len(argv) and not argv[option_index].startswith("-"):
            ignored_interpreter_values.add(option_index)
    elif _interpreter_code_flags(command["executable"]):
        ignored_interpreter_values.add(0)
    consumed_values: set[int] = set()
    after_options = False
    for index, argument in enumerate(argv):
        if index in consumed_values:
            continue
        if index in ignored_interpreter_values:
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
        if argument.startswith("-"):
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


def _write_file_at(parent_fd: int, name: str, text: str) -> tuple[int, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        data = text.encode("utf-8")
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short write")
            offset += written
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise ScaffoldError(f"generated file is not regular: {name}")
        return identity.st_dev, identity.st_ino
    except BaseException:
        try:
            written_identity = os.fstat(descriptor)
            if stat.S_ISDIR(written_identity.st_mode):
                raise ScaffoldError(f"generated file became a directory: {name}")
            _native_unlink_at(
                parent_fd,
                name,
                is_directory=False,
                expected_identity=(written_identity.st_dev, written_identity.st_ino),
            )
        except (OSError, ScaffoldError):
            pass
        raise
    finally:
        os.close(descriptor)


def _record_created_file(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    reservations: list[PublishedEntry] | None,
) -> None:
    """Retain an identity-bound reservation for a generated file."""

    if reservations is None:
        return
    try:
        reservation_parent_fd = os.dup(parent_fd)
    except BaseException:
        _native_unlink_at(
            parent_fd,
            name,
            is_directory=False,
            expected_identity=identity,
        )
        raise
    reservations.append((reservation_parent_fd, name, identity[0], identity[1], False))


def _write_tree(
    spec: dict[str, Any],
    root: Path,
    root_fd: int | None = None,
    *,
    reservations: list[PublishedEntry] | None = None,
) -> None:
    if reservations is None:
        reservations = _RESERVATION_SINK
    app = spec["app"]
    owned_root_fd = root_fd is None
    if root_fd is None:
        root_fd = _open_directory_descriptor(root, "staging directory")
    features_fd: int | None = None
    features_reservation: PublishedEntry | None = None
    try:
        identity = _write_file_at(root_fd, "SKILL.md", _skill_markdown(spec))
        _record_created_file(root_fd, "SKILL.md", identity, reservations)
        features_fd, _, features_reservation = _create_new_directory_with_reservation(
            root_fd,
            "features",
            "generated features directory",
            mode=0o700,
        )
        if features_reservation is not None and reservations is not None:
            reservations.append(features_reservation)
            features_reservation = None
        identity = _write_file_at(features_fd, "README.md", _index_markdown(spec))
        _record_created_file(features_fd, "README.md", identity, reservations)
        for feature in spec["features"]:
            feature_name = f"{feature['id']}.md"
            identity = _write_file_at(
                features_fd,
                feature_name,
                _feature_markdown(feature, app["harness"]),
            )
            _record_created_file(features_fd, feature_name, identity, reservations)
    finally:
        if features_reservation is not None:
            if reservations is not None:
                reservations.append(features_reservation)
            else:
                try:
                    os.close(features_reservation[0])
                except OSError:
                    pass
        if features_fd is not None:
            try:
                os.close(features_fd)
            except OSError:
                pass
        if owned_root_fd:
            try:
                os.close(root_fd)
            except OSError:
                pass


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


PublishedEntry = tuple[int, str, int | None, int | None, bool]
_RESERVATION_SINK: list[PublishedEntry] | None = None


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
) -> tuple[int, bool, PublishedEntry | None]:
    """Open or create one directory entry without resolving through its parent path."""

    try:
        return _open_directory_descriptor(name, label, dir_fd=parent_fd), False, None
    except ScaffoldError as exc:
        if not isinstance(exc.__cause__, FileNotFoundError):
            raise

    try:
        return _create_new_directory_with_reservation(
            parent_fd,
            name,
            label,
            mode=0o777,
        )
    except FileExistsError:
        return _open_directory_descriptor(name, label, dir_fd=parent_fd), False, None


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


def _remove_identity_bound_empty_directory(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
) -> bool:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(current.st_mode) or not _same_identity(current, identity):
        return False
    try:
        _native_unlink_at(
            parent_fd,
            name,
            is_directory=True,
            expected_identity=identity,
        )
    except (OSError, ScaffoldError):
        return False
    return True


def _remove_unrecorded_directory(parent_fd: int, name: str) -> bool:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(current.st_mode):
        return False
    return _remove_identity_bound_empty_directory(
        parent_fd,
        name,
        (current.st_dev, current.st_ino),
    )


def _bind_created_directory(
    parent_fd: int,
    name: str,
    label: str,
    reservation_parent_fd: int,
) -> tuple[int, PublishedEntry]:
    descriptor: int | None = None
    reservation: PublishedEntry | None = None
    path_identity: tuple[int, int] | None = None
    path_stat_error: OSError | None = None
    try:
        try:
            path_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            path_stat = None
            path_stat_error = exc
        if path_stat is not None:
            if not stat.S_ISDIR(path_stat.st_mode):
                raise ScaffoldError(f"{label} is not a directory")
            path_identity = (path_stat.st_dev, path_stat.st_ino)
        descriptor = _open_directory_descriptor(
            name,
            label,
            dir_fd=parent_fd,
            collision=True,
        )
        identity = _descriptor_identity(descriptor, label, is_directory=True)
        if path_stat_error is not None:
            raise ScaffoldError(f"{label} identity could not be captured") from path_stat_error
        if path_identity is None or identity != path_identity:
            raise CollisionError(f"{label} changed before descriptor binding")
        reservation = (reservation_parent_fd, name, identity[0], identity[1], True)
        observed = _published_identity(
            parent_fd,
            name,
            is_directory=True,
            entry_fd=descriptor,
        )
        _close_published([observed])
        return descriptor, reservation
    except BaseException:
        if reservation is None and path_identity is not None:
            reservation = (
                reservation_parent_fd,
                name,
                path_identity[0],
                path_identity[1],
                True,
            )
        if descriptor is not None and reservation is None:
            try:
                identity = os.fstat(descriptor)
            except OSError:
                identity = None
            if identity is not None and stat.S_ISDIR(identity.st_mode):
                reservation = (
                    reservation_parent_fd,
                    name,
                    identity.st_dev,
                    identity.st_ino,
                    True,
                )
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if reservation is not None:
            _remove_identity_bound_empty_directory(
                parent_fd,
                name,
                (reservation[2], reservation[3]),
            )
        # If identity capture failed, the current public name is unowned from
        # this process's perspective. Leave it in place rather than reacquiring
        # a name-only identity and risking a competitor's deletion.
        try:
            os.close(reservation_parent_fd)
        except OSError:
            pass
        raise


def _create_new_directory_with_reservation(
    parent_fd: int,
    name: str,
    label: str,
    *,
    mode: int,
) -> tuple[int, bool, PublishedEntry]:
    reservation_parent_fd = os.dup(parent_fd)
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
    except BaseException:
        try:
            os.close(reservation_parent_fd)
        except OSError:
            pass
        raise
    descriptor, reservation = _bind_created_directory(
        parent_fd,
        name,
        label,
        reservation_parent_fd,
    )
    return descriptor, True, reservation


def _create_staging_directory(parent_fd: int, prefix: str) -> tuple[str, int, PublishedEntry]:
    """Create a private staging directory relative to an already-open parent."""

    candidates = tempfile._get_candidate_names()
    for _ in range(100):
        name = f"{prefix}{next(candidates)}"
        try:
            descriptor, _, reservation = _create_new_directory_with_reservation(
                parent_fd,
                name,
                "staging directory",
                mode=0o700,
            )
        except FileExistsError:
            continue
        return name, descriptor, reservation
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


def _close_published(
    entries: list[PublishedEntry],
    *,
    receipt: RollbackReceipt | None = None,
) -> None:
    for parent_fd, name, _, _, _ in entries:
        try:
            os.close(parent_fd)
        except OSError as exc:
            _record_cleanup_failure(receipt, "close rollback descriptor", name, exc)


def _same_identity(left: os.stat_result, right: tuple[int, int]) -> bool:
    return (left.st_dev, left.st_ino) == right


def _atomic_rename_noreplace(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
) -> None:
    """Rename without replacing a destination, or fail closed."""

    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flags = 0x00000001  # RENAME_NOREPLACE
    else:
        raise ScaffoldError("identity-bound cleanup is unavailable on this platform")

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, function_name)
    except AttributeError as exc:
        raise ScaffoldError("identity-bound cleanup is unavailable on this platform") from exc

    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        flags,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
        raise ScaffoldError("identity-bound cleanup is unavailable on this platform")
    raise OSError(error_number, os.strerror(error_number), old_name, new_name)


def _native_unlink_at(
    parent_fd: int,
    name: str,
    *,
    is_directory: bool,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Claim an entry with a no-replace rename before removing it."""

    if expected_identity is None:
        raise ScaffoldError("identity-bound cleanup requires an expected identity")

    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CollisionError(f"entry changed during cleanup: {name}") from exc
    if (current.st_dev, current.st_ino) != expected_identity:
        raise CollisionError(f"entry changed during cleanup: {name}")
    if is_directory and not stat.S_ISDIR(current.st_mode):
        raise CollisionError(f"entry is not a directory during cleanup: {name}")
    if not is_directory and stat.S_ISDIR(current.st_mode):
        raise CollisionError(f"entry is a directory during cleanup: {name}")

    claimed_name = _unique_private_name(parent_fd, ".verify-unlink-")
    _atomic_rename_noreplace(parent_fd, name, parent_fd, claimed_name)
    try:
        claimed = os.stat(claimed_name, dir_fd=parent_fd, follow_symlinks=False)
        if (claimed.st_dev, claimed.st_ino) != expected_identity:
            raise CollisionError(f"entry changed during cleanup: {name}")
        if is_directory and not stat.S_ISDIR(claimed.st_mode):
            raise CollisionError(f"entry is not a directory during cleanup: {name}")
        if not is_directory and stat.S_ISDIR(claimed.st_mode):
            raise CollisionError(f"entry is a directory during cleanup: {name}")

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "unlinkat")
        except AttributeError as exc:
            raise ScaffoldError("identity-bound cleanup is unavailable on this platform") from exc

        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        function.restype = ctypes.c_int
        # Darwin uses AT_REMOVEDIR=0x80; Linux uses AT_REMOVEDIR=0x200.
        flags = (0x00000080 if sys.platform == "darwin" else 0x00000200) if is_directory else 0
        result = function(parent_fd, os.fsencode(claimed_name), flags)
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
            raise ScaffoldError("identity-bound cleanup is unavailable on this platform")
        raise OSError(error_number, os.strerror(error_number), claimed_name)
    except BaseException:
        try:
            _atomic_rename_noreplace(parent_fd, claimed_name, parent_fd, name)
        except (OSError, ScaffoldError):
            pass
        raise


def _unique_private_name(parent_fd: int, prefix: str = ".verify-cleanup-") -> str:
    candidates = tempfile._get_candidate_names()
    for _ in range(100):
        name = f"{prefix}{next(candidates)}"
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise ScaffoldError("could not reserve a private cleanup name")


class _PrivateDirectory:
    def __init__(self, name: str, descriptor: int, identity: tuple[int, int]) -> None:
        self.name = name
        self.descriptor = descriptor
        self.identity = identity

    def __iter__(self):
        # Keep the small helper's historical two-value unpacking contract for
        # focused tests and callers while retaining the bound identity.
        yield self.name
        yield self.descriptor

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> str | int:
        return (self.name, self.descriptor)[index]


def _create_private_directory(parent_fd: int) -> _PrivateDirectory:
    """Create a mode-700 directory and retain its descriptor as a quarantine."""

    for _ in range(100):
        name = _unique_private_name(parent_fd, ".verify-dispose-")
        try:
            descriptor, _, reservation = _create_new_directory_with_reservation(
                parent_fd,
                name,
                "private cleanup directory",
                mode=0o700,
            )
        except FileExistsError:
            continue
        try:
            os.close(reservation[0])
        except OSError:
            pass
        return _PrivateDirectory(name, descriptor, (reservation[2], reservation[3]))
    raise ScaffoldError("could not reserve a private cleanup directory")


def _restore_quarantined_entry(parent_fd: int, public_name: str, private_name: str) -> None:
    """Restore a quarantined entry without replacing a competitor."""

    try:
        os.stat(private_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except (OSError, ScaffoldError):
        return
    try:
        os.stat(public_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            _atomic_rename_noreplace(parent_fd, private_name, parent_fd, public_name)
        except (OSError, ScaffoldError):
            pass
    except (OSError, ScaffoldError):
        pass


def _move_owned_entry_to_quarantine(
    parent_fd: int,
    name: str,
    device: int,
    inode: int,
    *,
    prefix: str = ".verify-cleanup-",
) -> str:
    """Claim an identity-checked entry before any destructive operation."""

    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_identity(current, (device, inode)):
        raise CollisionError(f"entry changed during cleanup: {name}")
    private_name = _unique_private_name(parent_fd, prefix)
    try:
        _atomic_rename_noreplace(parent_fd, name, parent_fd, private_name)
    except FileNotFoundError as exc:
        raise CollisionError(f"entry changed during cleanup: {name}") from exc
    try:
        moved = os.stat(private_name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(moved, (device, inode)):
            raise CollisionError(f"entry changed during cleanup: {name}")
    except BaseException:
        _restore_quarantined_entry(parent_fd, name, private_name)
        raise
    return private_name


def _delete_quarantined_entry(
    parent_fd: int,
    public_name: str,
    private_name: str,
    device: int,
    inode: int,
    *,
    is_directory: bool,
) -> None:
    """Delete only a freshly re-claimed private entry.

    A replacement at either the public name or the private quarantine name is
    moved aside by the no-replace claim and left intact when its identity does
    not match.  Cleanup therefore never uses a stale public-name check as a
    deletion authority.
    """

    delete_name: str | None = None
    quarantine_name: str | None = None
    quarantine_fd: int | None = None
    quarantine_entry_name: str | None = None
    quarantine_identity: tuple[int, int] | None = None
    try:
        current = os.stat(private_name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(current, (device, inode)):
            raise CollisionError(f"private cleanup entry changed: {private_name}")
        delete_name = _unique_private_name(parent_fd)
        _atomic_rename_noreplace(parent_fd, private_name, parent_fd, delete_name)
        claimed = os.stat(delete_name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(claimed, (device, inode)):
            raise CollisionError(f"private cleanup entry changed: {private_name}")
        quarantine_directory = _create_private_directory(parent_fd)
        if isinstance(quarantine_directory, tuple):
            quarantine_name, quarantine_fd = quarantine_directory
            quarantine_stat = os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
            quarantine_identity = (quarantine_stat.st_dev, quarantine_stat.st_ino)
        else:
            quarantine_name = quarantine_directory.name
            quarantine_fd = quarantine_directory.descriptor
            quarantine_identity = quarantine_directory.identity
        quarantine_entry_name = _unique_private_name(
            quarantine_fd,
            ".verify-entry-",
        )
        _atomic_rename_noreplace(
            parent_fd,
            delete_name,
            quarantine_fd,
            quarantine_entry_name,
        )
        quarantined = os.stat(
            quarantine_entry_name,
            dir_fd=quarantine_fd,
            follow_symlinks=False,
        )
        if not _same_identity(quarantined, (device, inode)):
            raise CollisionError(f"private cleanup entry changed in quarantine: {private_name}")
        _native_unlink_at(
            quarantine_fd,
            quarantine_entry_name,
            is_directory=is_directory,
            expected_identity=(device, inode),
        )
        quarantine_entry_name = None
        os.close(quarantine_fd)
        quarantine_fd = None
        if quarantine_identity is None:
            raise CollisionError(f"private cleanup directory identity is unavailable: {quarantine_name}")
        _native_unlink_at(
            parent_fd,
            quarantine_name,
            is_directory=True,
            expected_identity=quarantine_identity,
        )
        quarantine_name = None
        try:
            os.stat(public_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise CollisionError(f"replacement appeared during cleanup: {public_name}")
    except BaseException:
        if quarantine_fd is not None and quarantine_entry_name is not None:
            try:
                os.stat(
                    quarantine_entry_name,
                    dir_fd=quarantine_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if delete_name is None:
                    try:
                        delete_name = _unique_private_name(parent_fd)
                    except (OSError, ScaffoldError):
                        delete_name = None
                if delete_name is not None:
                    try:
                        _atomic_rename_noreplace(
                            quarantine_fd,
                            quarantine_entry_name,
                            parent_fd,
                            delete_name,
                        )
                    except (OSError, ScaffoldError):
                        pass
        if delete_name is not None:
            _restore_quarantined_entry(parent_fd, public_name, delete_name)
        if quarantine_name is not None and quarantine_identity is not None:
            try:
                _native_unlink_at(
                    parent_fd,
                    quarantine_name,
                    is_directory=True,
                    expected_identity=quarantine_identity,
                )
            except (OSError, ScaffoldError):
                pass
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        raise
    finally:
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass


def _remove_owned_entry(
    parent_fd: int,
    name: str,
    device: int,
    inode: int,
    *,
    is_directory: bool,
) -> None:
    private_name = _move_owned_entry_to_quarantine(parent_fd, name, device, inode)
    try:
        _delete_quarantined_entry(
            parent_fd,
            name,
            private_name,
            device,
            inode,
            is_directory=is_directory,
        )
    except BaseException:
        _restore_quarantined_entry(parent_fd, name, private_name)
        raise


def _remove_owned_empty_directory(parent_fd: int, name: str, device: int, inode: int) -> None:
    """Remove an owned directory only when it is still empty."""

    private_name = _move_owned_entry_to_quarantine(parent_fd, name, device, inode)
    try:
        directory_fd = _open_directory_descriptor(
            private_name,
            "cleanup directory",
            dir_fd=parent_fd,
            collision=True,
        )
        try:
            if os.listdir(directory_fd):
                raise CollisionError(f"directory is no longer empty during cleanup: {name}")
        finally:
            os.close(directory_fd)
        _delete_quarantined_entry(
            parent_fd,
            name,
            private_name,
            device,
            inode,
            is_directory=True,
        )
    except BaseException:
        _restore_quarantined_entry(parent_fd, name, private_name)
        raise


def _remove_tree_at(
    parent_fd: int,
    name: str,
    device: int,
    inode: int,
) -> None:
    """Remove a tree after binding each entry to its inode."""

    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not _same_identity(current, (device, inode)):
        raise CollisionError(f"entry changed during cleanup: {name}")
    if stat.S_ISDIR(current.st_mode):
        directory_fd = _open_directory_descriptor(
            name,
            "cleanup directory",
            dir_fd=parent_fd,
            collision=True,
        )
        try:
            opened = os.fstat(directory_fd)
            if not _same_identity(opened, (device, inode)):
                raise CollisionError(f"entry changed during cleanup: {name}")
            children = sorted(os.listdir(directory_fd))
            child_identities = {
                child_name: os.stat(
                    child_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                for child_name in children
            }
            for child_name in children:
                child = child_identities[child_name]
                _remove_tree_at(directory_fd, child_name, child.st_dev, child.st_ino)
            if os.listdir(directory_fd):
                raise CollisionError(f"directory changed during cleanup: {name}")
        finally:
            os.close(directory_fd)
        _remove_owned_entry(
            parent_fd,
            name,
            device,
            inode,
            is_directory=True,
        )
        return
    _remove_owned_entry(parent_fd, name, device, inode, is_directory=False)


def _rollback_published(
    entries: list[PublishedEntry],
    *,
    recursive: bool = True,
    receipt: RollbackReceipt | None = None,
) -> RollbackReceipt:
    receipt = receipt or RollbackReceipt()
    for parent_fd, name, device, inode, is_directory in reversed(entries):
        entry = (parent_fd, name, device, inode, is_directory)
        if not receipt.begin(entry):
            try:
                os.close(parent_fd)
            except OSError as exc:
                receipt.record_failure("close rollback descriptor", name, exc)
            continue
        try:
            if device is None or inode is None:
                continue
            if is_directory:
                if recursive:
                    _remove_tree_at(parent_fd, name, device, inode)
                else:
                    _remove_owned_empty_directory(parent_fd, name, device, inode)
            else:
                _remove_owned_entry(parent_fd, name, device, inode, is_directory=False)
            receipt.cleaned += 1
        except (OSError, ScaffoldError) as exc:
            receipt.record_failure("rollback entry", name, exc)
        finally:
            try:
                os.close(parent_fd)
            except OSError as exc:
                receipt.record_failure("close rollback descriptor", name, exc)
    return receipt


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


def _assert_reserved_target_contents(
    parent_fd: int,
    target_name: str,
    target: Path,
    target_fd: int,
    identity: tuple[int, int],
    published: list[PublishedEntry],
) -> None:
    """Require the final target tree to match only identity-bound entries."""

    _assert_reserved_target_identity(parent_fd, target_name, target, target_fd, identity)
    expected_by_parent: dict[tuple[int, int], dict[str, tuple[int, int, bool]]] = {
        identity: {}
    }
    for entry_parent_fd, name, device, inode, is_directory in published:
        if device is None or inode is None:
            raise CollisionError(f"published entry identity is unavailable: {target / name}")
        parent_identity = _descriptor_identity(
            entry_parent_fd,
            f"target parent for {target / name}",
            is_directory=True,
        )
        children = expected_by_parent.setdefault(parent_identity, {})
        expected = (device, inode, is_directory)
        if name in children and children[name] != expected:
            raise CollisionError(f"target manifest contains duplicate entry: {target / name}")
        children[name] = expected
        if is_directory:
            expected_by_parent.setdefault((device, inode), {})

    visited: set[tuple[int, int]] = set()

    def check_directory(
        directory_fd: int,
        directory_identity: tuple[int, int],
        location: Path,
    ) -> None:
        if directory_identity in visited:
            raise CollisionError(f"target manifest contains a directory cycle: {location}")
        visited.add(directory_identity)
        expected = expected_by_parent.get(directory_identity)
        if expected is None:
            raise CollisionError(f"target manifest lost a published directory: {location}")
        try:
            with os.scandir(directory_fd) as entries:
                actual_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise CollisionError(f"target contents cannot be inspected: {location}") from exc
        actual_names = {entry.name for entry in actual_entries}
        expected_names = set(expected)
        if actual_names != expected_names:
            unexpected = sorted(actual_names - expected_names)
            missing = sorted(expected_names - actual_names)
            details = []
            if unexpected:
                details.append(f"unrecorded entries: {', '.join(unexpected)}")
            if missing:
                details.append(f"missing entries: {', '.join(missing)}")
            raise CollisionError(
                f"target contents changed during publication: {location} ({'; '.join(details)})"
            )

        for entry in actual_entries:
            expected_device, expected_inode, expected_directory = expected[entry.name]
            try:
                current = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CollisionError(
                    f"target entry cannot be inspected during publication: {location / entry.name}"
                ) from exc
            current_identity = (current.st_dev, current.st_ino)
            if (
                current_identity != (expected_device, expected_inode)
                or stat.S_ISDIR(current.st_mode) != expected_directory
                or (not expected_directory and not stat.S_ISREG(current.st_mode))
            ):
                raise CollisionError(
                    f"target entry changed during publication: {location / entry.name}"
                )
            if expected_directory:
                child_fd = _open_directory_descriptor(
                    entry.name,
                    f"target directory {location / entry.name}",
                    dir_fd=directory_fd,
                    collision=True,
                )
                try:
                    child_identity = _descriptor_identity(
                        child_fd,
                        f"target directory {location / entry.name}",
                        is_directory=True,
                    )
                    if child_identity != (expected_device, expected_inode):
                        raise CollisionError(
                            f"target directory changed during publication: {location / entry.name}"
                        )
                    check_directory(child_fd, child_identity, location / entry.name)
                finally:
                    os.close(child_fd)

    check_directory(target_fd, identity, target)
    if visited != set(expected_by_parent):
        raise CollisionError(f"target manifest is not fully reachable: {target}")


def _remove_reserved_target(
    parent_fd: int,
    target_name: str,
    identity: tuple[int, int] | None,
    *,
    receipt: RollbackReceipt | None = None,
) -> None:
    """Remove a reserved directory through the same quarantine path as rollback."""

    if identity is None:
        return
    try:
        _remove_owned_empty_directory(parent_fd, target_name, identity[0], identity[1])
    except FileNotFoundError:
        return
    except (OSError, ScaffoldError) as exc:
        _record_cleanup_failure(receipt, "remove reserved target", target_name, exc)
        if receipt is None:
            raise ScaffoldError(f"reserved target cleanup incomplete: {target_name}: {exc}") from exc


def _remove_reserved_directory_tree(
    entry: PublishedEntry,
    *,
    receipt: RollbackReceipt | None = None,
) -> bool:
    """Remove a reserved directory tree only through identity-bound quarantine."""

    parent_fd, name, device, inode, is_directory = entry
    try:
        if not is_directory or device is None or inode is None:
            return False
        _remove_tree_at(parent_fd, name, device, inode)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (NotImplementedError, OSError, ScaffoldError) as exc:
        _record_cleanup_failure(receipt, "remove reserved directory tree", name, exc)
        return False
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _remove_reserved_empty_directory(
    entry: PublishedEntry,
    *,
    receipt: RollbackReceipt | None = None,
) -> bool:
    """Remove a reserved directory only after recorded children are gone."""

    parent_fd, name, device, inode, is_directory = entry
    try:
        if not is_directory or device is None or inode is None:
            return False
        _remove_owned_empty_directory(parent_fd, name, device, inode)
        return True
    except (FileNotFoundError, NotADirectoryError, OSError, ScaffoldError) as exc:
        _record_cleanup_failure(receipt, "remove reserved empty directory", name, exc)
        return False
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _open_regular_descriptor(
    name: Path | str,
    label: str,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open a regular file without following a replacement link or FIFO."""

    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | os.O_NONBLOCK | nofollow_flag | getattr(os, "O_CLOEXEC", 0)
    try:
        if dir_fd is None:
            descriptor = os.open(os.fspath(name), flags)
        else:
            descriptor = os.open(os.fspath(name), flags, dir_fd=dir_fd)
    except OSError as exc:
        raise CollisionError(f"{label} cannot be opened without following links: {name}") from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise CollisionError(f"{label} is not a regular file: {name}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _publish_entry_no_replace(
    source: Path,
    destination: Path,
    *,
    destination_dir_fd: int,
    source_dir_fd: int | None = None,
    source_name: str | None = None,
    source_metadata: os.stat_result | None = None,
    reservation_sink: list[PublishedEntry] | None = None,
    rollback_receipt: RollbackReceipt | None = None,
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
        source_stat = source_metadata or os.lstat(source)
    else:
        if source_name is None:
            raise ScaffoldError(f"staging source name is missing: {source}")
        if source_metadata is None:
            try:
                source_stat = os.stat(source_name, dir_fd=source_dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise ScaffoldError(f"staging source cannot be inspected: {source}") from exc
        else:
            source_stat = source_metadata
    source_mode = source_stat.st_mode
    source_descriptor: int | None = None
    try:
        if stat.S_ISDIR(source_mode):
            source_descriptor = _open_directory_descriptor(
                source_name if source_dir_fd is not None else source,
                "staging source directory",
                dir_fd=source_dir_fd,
                collision=True,
            )
            source_identity = _descriptor_identity(
                source_descriptor,
                f"staging source {source}",
                is_directory=True,
            )
        elif stat.S_ISREG(source_mode):
            source_descriptor = _open_regular_descriptor(
                source_name if source_dir_fd is not None else source,
                "staging source file",
                dir_fd=source_dir_fd,
            )
            source_identity = _descriptor_identity(
                source_descriptor,
                f"staging source {source}",
                is_directory=False,
            )
        else:
            raise ScaffoldError(f"staging contains a non-regular entry: {source}")
        if source_identity != (source_stat.st_dev, source_stat.st_ino):
            raise CollisionError(f"staging source changed during publication: {source}")
        try:
            current_source = os.stat(
                source_name if source_dir_fd is not None else source,
                dir_fd=source_dir_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CollisionError(f"staging source changed during publication: {source}") from exc
        if (
            not stat.S_ISDIR(current_source.st_mode) and not stat.S_ISREG(current_source.st_mode)
        ) or (current_source.st_dev, current_source.st_ino) != source_identity:
            raise CollisionError(f"staging source changed during publication: {source}")
    except BaseException:
        if source_descriptor is not None:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        raise

    published: list[PublishedEntry] = []

    def record(entry: PublishedEntry) -> None:
        published.append(entry)
        if reservation_sink is None:
            return
        outer_parent_fd: int | None = None
        try:
            outer_parent_fd = os.dup(entry[0])
            reservation_sink.append(
                (
                    outer_parent_fd,
                    entry[1],
                    entry[2],
                    entry[3],
                    entry[4],
                )
            )
        except BaseException:
            published.pop()
            if outer_parent_fd is not None:
                try:
                    os.close(outer_parent_fd)
                except OSError:
                    pass
            raise

    if stat.S_ISDIR(source_mode):
        child_fd: int | None = None
        child_reservation: PublishedEntry | None = None
        try:
            child_fd, _, child_reservation = _create_new_directory_with_reservation(
                destination_dir_fd,
                destination_name,
                "published directory",
                mode=0o777,
            )
        except FileExistsError as exc:
            os.close(source_descriptor)
            raise CollisionError(f"target changed during publication: {destination}") from exc
        except BaseException:
            os.close(source_descriptor)
            raise
        if child_fd is None or child_reservation is None:
            os.close(source_descriptor)
            raise ScaffoldError(f"published directory reservation is unavailable: {destination}")
        try:
            record(child_reservation)
            with os.scandir(source_descriptor) as entries:
                child_entries = sorted(
                    (
                        entry.name,
                        entry.stat(follow_symlinks=False),
                    )
                    for entry in entries
                )
            for child_name, child_metadata in child_entries:
                child = source / child_name
                published.extend(
                    _publish_entry_no_replace(
                        child,
                        destination / child_name,
                        destination_dir_fd=child_fd,
                        source_dir_fd=source_descriptor,
                        source_name=child_name,
                        source_metadata=child_metadata,
                        reservation_sink=reservation_sink,
                        rollback_receipt=rollback_receipt,
                    )
                )
            # Keep the staged source intact until the top-level reservation is
            # removed through its descriptor-bound cleanup path.
            return published
        except Exception as original_error:
            receipt = _rollback_published(published, receipt=rollback_receipt)
            if child_reservation is not None and child_reservation not in published:
                if receipt.begin(child_reservation):
                    try:
                        _remove_owned_empty_directory(
                            child_reservation[0],
                            child_reservation[1],
                            child_reservation[2],
                            child_reservation[3],
                        )
                        receipt.cleaned += 1
                    except (OSError, ScaffoldError) as exc:
                        receipt.record_failure(
                            "rollback child directory",
                            child_reservation[1],
                            exc,
                        )
                    finally:
                        try:
                            os.close(child_reservation[0])
                        except OSError as exc:
                            receipt.record_failure(
                                "close rollback descriptor",
                                child_reservation[1],
                                exc,
                            )
            if receipt.failures:
                if isinstance(original_error, RollbackError):
                    raise
                raise RollbackError(original_error, receipt) from original_error
            raise
        finally:
            if child_fd is not None:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
            try:
                os.close(source_descriptor)
            except OSError:
                pass
    reservation_parent_fd: int | None = None
    temporary_name: str | None = None
    destination_reservation: PublishedEntry | None = None
    private_link_created = False
    temporary_identity: tuple[int, int] | None = None
    reservation_recorded = False
    try:
        reservation_parent_fd = os.dup(destination_dir_fd)
        temporary_name = _unique_private_name(destination_dir_fd, ".verify-publish-")
    except BaseException:
        if reservation_parent_fd is not None:
            try:
                os.close(reservation_parent_fd)
            except OSError:
                pass
        os.close(source_descriptor)
        raise

    temporary_descriptor: int | None = None
    try:
        try:
            # Build the link under a private name, bind its identity, and only
            # then atomically publish it at the requested destination.
            if source_dir_fd is None:
                os.link(
                    source,
                    temporary_name,
                    dst_dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                )
            else:
                os.link(
                    source_name,
                    temporary_name,
                    src_dir_fd=source_dir_fd,
                    dst_dir_fd=destination_dir_fd,
                    follow_symlinks=False,
                )
        except FileExistsError as exc:
            raise CollisionError(f"target changed during publication: {destination}") from exc
        private_link_created = True

        temporary_descriptor = _open_regular_descriptor(
            temporary_name,
            "published file",
            dir_fd=destination_dir_fd,
        )
        temporary_stat = os.fstat(temporary_descriptor)
        temporary_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
        destination_reservation = (
            reservation_parent_fd,
            temporary_name,
            temporary_identity[0],
            temporary_identity[1],
            False,
        )
        record(destination_reservation)
        reservation_recorded = True
        # The private name was created by this call even when the source path
        # changed between validation and os.link(). Bind rollback to the
        # identity actually created so a failed race cannot strand our private
        # entry, while still refusing to touch a later replacement.
        published[-1] = (
            destination_reservation[0],
            temporary_name,
            temporary_identity[0],
            temporary_identity[1],
            False,
        )
        if reservation_sink is not None:
            sink_parent_fd = reservation_sink[-1][0]
            reservation_sink[-1] = (
                sink_parent_fd,
                temporary_name,
                temporary_identity[0],
                temporary_identity[1],
                False,
            )
        if not stat.S_ISREG(temporary_stat.st_mode) or temporary_identity != source_identity:
            raise CollisionError(f"staging source changed during publication: {source}")
        # Keep the original source identity in the rollback reservation until
        # the temporary entry has passed the source check. A raced replacement
        # must never become the object rollback is authorized to remove.
        if _descriptor_identity(
            temporary_descriptor,
            f"published file {destination}",
            is_directory=False,
        ) != source_identity or _descriptor_identity(
            source_descriptor,
            f"staging source {source}",
            is_directory=False,
        ) != source_identity:
            raise CollisionError(f"staging source changed during publication: {source}")
        _atomic_rename_noreplace(
            destination_dir_fd,
            temporary_name,
            destination_dir_fd,
            destination_name,
        )
        published[-1] = (
            destination_reservation[0],
            destination_name,
            source_identity[0],
            source_identity[1],
            False,
        )
        if reservation_sink is not None:
            sink_parent_fd = reservation_sink[-1][0]
            reservation_sink[-1] = (
                sink_parent_fd,
                published[-1][1],
                published[-1][2],
                published[-1][3],
                published[-1][4],
            )
        observed = _published_identity(
            destination_dir_fd,
            destination_name,
            is_directory=False,
            entry_fd=temporary_descriptor,
        )
        _close_published([observed])
        # Recheck after closing the observation descriptor. A concurrent
        # hook can swap the published name immediately after the first
        # comparison; the second descriptor-versus-path check must turn that
        # race into rollback.
        observed = _published_identity(
            destination_dir_fd,
            destination_name,
            is_directory=False,
            entry_fd=temporary_descriptor,
        )
        _close_published([observed])
    except Exception as original_error:
        receipt = rollback_receipt or RollbackReceipt()
        if not reservation_recorded and private_link_created and temporary_identity is not None:
            _rollback_published(
                [
                    (
                        reservation_parent_fd,
                        temporary_name,
                        temporary_identity[0],
                        temporary_identity[1],
                        False,
                    )
                ],
                receipt=receipt,
            )
        elif destination_reservation is None and reservation_parent_fd is not None:
            try:
                os.close(reservation_parent_fd)
            except OSError as exc:
                receipt.record_failure("close rollback descriptor", temporary_name or destination, exc)
        _rollback_published(published, receipt=receipt)
        if receipt.failures:
            if isinstance(original_error, RollbackError):
                raise
            raise RollbackError(original_error, receipt) from original_error
        raise
    finally:
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        try:
            os.close(source_descriptor)
        except OSError:
            pass
    return published


def scaffold(spec: dict[str, Any], project_root: Path) -> Path:
    """Create a new generated skill and return its path."""

    spec = validate_spec(spec)
    project_root = _lexical_absolute(project_root.expanduser())
    if _symlink_component_errors(project_root):
        raise ScaffoldError("project root or one of its parents is a symlink")
    try:
        project_stat = os.lstat(project_root)
    except OSError as exc:
        raise ScaffoldError(f"project root cannot be inspected: {exc}") from exc
    project_mode = project_stat.st_mode
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
    target_reservation: PublishedEntry | None = None
    published_entries: list[PublishedEntry] = []
    staging_child_reservations: list[PublishedEntry] = []
    skills_parent_fd: int | None = None
    target_fd: int | None = None
    target_identity: tuple[int, int] | None = None
    rollback_receipt = RollbackReceipt()
    try:
        project_fd = _open_directory_descriptor(project_root, "project root")
        opened_project = os.fstat(project_fd)
        if (opened_project.st_dev, opened_project.st_ino) != (
            project_stat.st_dev,
            project_stat.st_ino,
        ):
            raise CollisionError(f"project root changed during publication: {project_root}")
        parent_fd, parent_created, parent_reservation = _open_or_create_directory(
            project_fd,
            ".agents",
            "output parent .agents",
        )
        if parent_created:
            if parent_reservation is None:
                raise ScaffoldError("output parent reservation is unavailable")
            created_parent_entries.append(parent_reservation)
        skills_parent_fd, skills_parent_created, skills_parent_reservation = _open_or_create_directory(
            parent_fd,
            "skills",
            "output parent .agents/skills",
        )
        if skills_parent_created:
            if skills_parent_reservation is None:
                raise ScaffoldError("output parent reservation is unavailable")
            created_parent_entries.append(skills_parent_reservation)
        _assert_path_matches_descriptor(skills_parent, skills_parent_fd, "output parent")
        staging_name, staging_fd, staging_reservation = _create_staging_directory(
            skills_parent_fd,
            f".verify-{spec['app']['name']}.staging-",
        )
        staging = skills_parent / staging_name
        _assert_path_matches_descriptor(staging, staging_fd, "staging directory")
        global _RESERVATION_SINK
        previous_reservation_sink = _RESERVATION_SINK
        _RESERVATION_SINK = staging_child_reservations
        try:
            _write_tree(spec, staging, root_fd=staging_fd)
        finally:
            _RESERVATION_SINK = previous_reservation_sink
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
            target_fd, _, target_reservation = _create_new_directory_with_reservation(
                skills_parent_fd,
                target.name,
                "reserved target",
                mode=0o777,
            )
        except FileExistsError as exc:
            raise CollisionError(f"output appeared during generation: {target}") from exc
        if target_reservation is None or target_fd is None:
            raise ScaffoldError("reserved target reservation is unavailable")
        target_reserved = True
        target_identity = (target_reservation[2], target_reservation[3])
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
            child_entries = sorted(
                (
                    entry.name,
                    entry.stat(follow_symlinks=False),
                )
                for entry in entries
            )
        for child_name, child_metadata in child_entries:
            _assert_reserved_target_identity(
                skills_parent_fd,
                target.name,
                target,
                target_fd,
                target_identity,
            )
            child = staging / child_name
            destination = target / child_name
            _publish_entry_no_replace(
                child,
                destination,
                destination_dir_fd=target_fd,
                source_dir_fd=staging_fd,
                source_name=child_name,
                source_metadata=child_metadata,
                reservation_sink=published_entries,
                rollback_receipt=rollback_receipt,
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
        if staging_reservation is None:
            staging_reservation = None
            raise CollisionError(f"staging directory changed during publication: {staging}")
        _rollback_published(
            staging_child_reservations,
            recursive=False,
            receipt=rollback_receipt,
        )
        staging_child_reservations = []
        _raise_if_rollback_incomplete(
            ScaffoldError("staging child cleanup failed"),
            rollback_receipt,
        )
        if not _remove_reserved_empty_directory(
            staging_reservation,
            receipt=rollback_receipt,
        ):
            staging_reservation = None
            raise CollisionError(f"staging directory changed during publication: {staging}")
        staging_reservation = None
        staging = None
        _assert_reserved_target_contents(
            skills_parent_fd,
            target.name,
            target,
            target_fd,
            target_identity,
            published_entries,
        )
        if skills_parent_fd is None or target_identity is None or target_reservation is None:
            raise ScaffoldError("reserved target publication state is unavailable")
        # Keep the incremental descriptor-bound publication path, but make the
        # final commit atomic. The authoritative manifest walk happens while
        # the reserved root has no public target name, so a subtree swap from
        # the public path cannot turn into a successful publication.
        sealed_target_name = _move_owned_entry_to_quarantine(
            skills_parent_fd,
            target.name,
            target_identity[0],
            target_identity[1],
            prefix=".verify-target-",
        )
        target_reservation = (
            target_reservation[0],
            sealed_target_name,
            target_reservation[2],
            target_reservation[3],
            target_reservation[4],
        )
        sealed_target = skills_parent / sealed_target_name
        _assert_reserved_target_contents(
            skills_parent_fd,
            sealed_target_name,
            sealed_target,
            target_fd,
            target_identity,
            published_entries,
        )
        _atomic_rename_noreplace(
            skills_parent_fd,
            sealed_target_name,
            skills_parent_fd,
            target.name,
        )
        target_reservation = (
            target_reservation[0],
            target.name,
            target_reservation[2],
            target_reservation[3],
            target_reservation[4],
        )
        _assert_reserved_target_identity(
            skills_parent_fd,
            target.name,
            target,
            target_fd,
            target_identity,
        )
        if target_reservation is not None:
            _close_published([target_reservation])
            target_reservation = None
        _close_published(published_entries)
        published_entries = []
        _close_published(created_parent_entries)
        created_parent_entries = []
        return target
    except Exception as original_error:
        staging_name_still_bound = False
        if staging_reservation is not None:
            try:
                current_staging = os.stat(
                    staging_reservation[1],
                    dir_fd=staging_reservation[0],
                    follow_symlinks=False,
                )
                staging_name_still_bound = (
                    (current_staging.st_dev, current_staging.st_ino)
                    == (staging_reservation[2], staging_reservation[3])
                )
            except OSError:
                staging_name_still_bound = False
        if staging_child_reservations:
            # Remove only entries created by this run before attempting the
            # staging root. Unknown competitors remain and make root cleanup
            # fail closed.
            if staging_name_still_bound:
                _rollback_published(
                    staging_child_reservations,
                    recursive=False,
                    receipt=rollback_receipt,
                )
            else:
                # The original staging root was moved away; preserve that
                # detached tree and never reacquire the replacement by name.
                _close_published(staging_child_reservations, receipt=rollback_receipt)
            staging_child_reservations = []
        if staging_reservation is not None:
            if staging_name_still_bound:
                _remove_reserved_empty_directory(
                    staging_reservation,
                    receipt=rollback_receipt,
                )
            else:
                _close_published([staging_reservation], receipt=rollback_receipt)
            staging_reservation = None
        if target_reserved:
            _rollback_published(
                published_entries,
                recursive=False,
                receipt=rollback_receipt,
            )
            if target_reservation is not None:
                _remove_reserved_target(
                    target_reservation[0],
                    target_reservation[1],
                    None
                    if target_reservation[2] is None or target_reservation[3] is None
                    else (target_reservation[2], target_reservation[3]),
                    receipt=rollback_receipt,
                )
                try:
                    os.close(target_reservation[0])
                except OSError as exc:
                    rollback_receipt.record_failure(
                        "close rollback descriptor",
                        target_reservation[1],
                        exc,
                    )
                target_reservation = None
            elif skills_parent_fd is not None and target_identity is not None:
                _remove_reserved_target(
                    skills_parent_fd,
                    target.name,
                    target_identity,
                    receipt=rollback_receipt,
                )
        _rollback_published(
            created_parent_entries,
            recursive=False,
            receipt=rollback_receipt,
        )
        created_parent_entries = []
        _raise_if_rollback_incomplete(original_error, rollback_receipt)
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
