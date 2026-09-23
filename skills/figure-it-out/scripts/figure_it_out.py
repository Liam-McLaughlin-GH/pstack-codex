#!/usr/bin/env python3
"""Run a finite, persisted, and auditable multi-phase workflow."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
MAX_TEXT = 4000
DEFAULT_LIMITS = {
    "max_attempts_per_phase": 2,
    "max_checkpoints": 8,
    "max_replans": 3,
    "max_decisions": 128,
}
VALID_RESULTS = {"open", "verified", "failed", "reverted", "blocked", "not_run", "inconclusive"}
CHECK_STATUSES = {"verified", "failed", "blocked", "not_run", "inconclusive"}
RUN_STATUSES = {"planned", "running", "failed", "interrupted", "blocked", "completed"}
PHASE_STATUSES = {"pending", "in_progress", "passed", "failed", "interrupted", "blocked"}
ACTION_REFUSAL_STATUSES = {"failed", "interrupted", "blocked", "completed"}
LIFECYCLE_EVENTS = {
    "phase_started",
    "checkpoint",
    "phase_passed",
    "phase_failed",
    "replanned",
    "interrupted",
    "resumed",
    "blocked",
    "predicate_recorded",
    "completed",
}
AUDIT_EVENTS = {"action_allowed", "action_refused"}
DECISION_EVENTS = {
    "phase_passed",
    "phase_failed",
    "replanned",
    "blocked",
    "predicate_recorded",
    "decision_recorded",
    "action_refused",
}
STRUCTURED_EVIDENCE_KINDS = {"artifact", "blocker", "command", "path", "reference", "test"}
EVIDENCE_PLACEHOLDER_KEYS = {
    "agent report",
    "agent summary",
    "checked",
    "done",
    "inconclusive",
    "looks good",
    "n/a",
    "na",
    "none",
    "no evidence",
    "no result",
    "no results",
    "not applicable",
    "not available",
    "not checked",
    "not found",
    "not provided",
    "not run",
    "not tested",
    "not yet run",
    "not verified",
    "null",
    "passed",
    "pending",
    "placeholder",
    "self report",
    "skipped",
    "success",
    "tbd",
    "todo",
    "unknown",
    "unavailable",
    "verified",
}
LOCAL_REFERENCE_NAMESPACES = {"check", "decisions", "phase", "scope", "target"}
SAFE_ACTION = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
BOUNDED_REFERENCE = re.compile(
    r"^[A-Za-z0-9_.-]+(?:(?:::|/|:)[A-Za-z0-9_.@#=+%~-]+)*$"
)


class WorkflowError(Exception):
    """Base class for errors that are safe to show to a workflow runner."""


class PlanError(WorkflowError):
    """The run contract is missing a required or bounded field."""


class StateError(WorkflowError):
    """The requested transition is invalid for the current state."""


class ReplanRequired(StateError):
    """A failed phase cannot be retried without an explicit replan."""


class PredicateError(StateError):
    """The terminal predicate is not satisfied."""


class ScopeRefusal(StateError):
    """An action is outside the declared scope and was not performed."""

    def __init__(self, message: str, refusal: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.refusal = dict(refusal or {})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, field: str, *, max_length: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a non-empty string")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise PlanError(f"{field} contains a control or format character")
    value = value.strip()
    if len(value) > max_length:
        raise PlanError(f"{field} exceeds {max_length} characters")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise PlanError(f"{field} must be a non-empty list")
    result = [_text(item, f"{field}[]", max_length=1000) for item in value]
    if len(result) != len(set(result)):
        raise PlanError(f"{field} contains duplicates")
    return result


def _placeholder_key(value: str) -> str:
    return re.sub(r"[\s_-]+", " ", value.strip().casefold())


def _is_placeholder_reference(value: str) -> bool:
    normalized = _placeholder_key(value)
    if normalized in EVIDENCE_PLACEHOLDER_KEYS:
        return True
    return bool(re.fullmatch(r"(?:agent|assistant|model) (?:claim|report|summary)", normalized))


def _is_scheme_shaped_reference(value: str) -> bool:
    match = URI_SCHEME.match(value)
    if not match:
        return False
    scheme = match.group(0)[:-1].casefold()
    suffix = value[match.end() :]
    if scheme in LOCAL_REFERENCE_NAMESPACES:
        return False
    # A file:line reference is a local source location, not a URI. The slash
    # form (tests/file.py:42) does not match URI_SCHEME at all.
    if "." in scheme and suffix.isdigit():
        return False
    return True


def _is_bounded_reference(value: str) -> bool:
    # Evidence references are deliberately lexical and local: a relative
    # path/name, a positive line suffix, or an approved symbolic namespace.
    # Recording evidence never resolves or follows a caller-controlled path.
    if value.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    if "\\" in value:
        return False
    if ".." in PurePosixPath(value).parts or any(char.isspace() for char in value):
        return False
    if _is_placeholder_reference(value) or _is_scheme_shaped_reference(value):
        return False
    if not BOUNDED_REFERENCE.fullmatch(value):
        return False
    namespace, separator, suffix = value.partition(":")
    if separator and namespace in LOCAL_REFERENCE_NAMESPACES:
        if ".." in PurePosixPath(suffix).parts:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9_.@#=+%~-]+(?:/[A-Za-z0-9_.@#=+%~-]+)*", suffix))
    if ":" in value:
        path, line_separator, line = value.rpartition(":")
        if not line_separator or not line.isdigit() or int(line) < 1 or not path:
            return False
    return True


def _structured_evidence(value: Mapping[str, Any], field: str, index: int) -> dict[str, Any]:
    allowed_keys = {"kind", "ref", "line", "result", "command", "detail"}
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        raise PlanError(f"{field}[{index}] has unsupported fields: {sorted(unknown_keys)}")
    kind = _text(value.get("kind"), f"{field}[{index}].kind", max_length=40)
    if kind not in STRUCTURED_EVIDENCE_KINDS:
        raise PlanError(f"{field}[{index}].kind is not a supported evidence kind")
    reference = _text(value.get("ref"), f"{field}[{index}].ref", max_length=500)
    if not _is_bounded_reference(reference):
        raise PlanError(f"{field}[{index}].ref must be a bounded local reference")
    normalized: dict[str, Any] = {"kind": kind, "ref": reference}
    if "line" in value:
        line = value["line"]
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 1_000_000:
            raise PlanError(f"{field}[{index}].line must be a positive line number")
        normalized["line"] = line
    if "result" in value:
        result = _text(value["result"], f"{field}[{index}].result", max_length=40)
        if result not in CHECK_STATUSES and result != "passed":
            raise PlanError(f"{field}[{index}].result is not a check result")
        normalized["result"] = result
    if "command" in value:
        normalized["command"] = _text(value["command"], f"{field}[{index}].command", max_length=1000)
    if "detail" in value:
        normalized["detail"] = _text(value["detail"], f"{field}[{index}].detail", max_length=1000)
    if kind == "command" and "command" not in normalized:
        raise PlanError(f"{field}[{index}].command is required for command evidence")
    return normalized


def _evidence(value: Any, field: str = "evidence") -> list[Any]:
    if isinstance(value, (str, Mapping)):
        value = [value]
    if not isinstance(value, list) or not value:
        raise PlanError(f"{field} must be a non-empty list")
    if len(value) != len({json.dumps(item, sort_keys=True, default=str) for item in value}):
        raise PlanError(f"{field} contains duplicates")
    normalized: list[Any] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            reference = _text(item, f"{field}[{index}]", max_length=500)
            if _is_placeholder_reference(reference):
                raise PlanError(f"{field}[{index}] must identify inspectable evidence")
            if not _is_bounded_reference(reference):
                raise PlanError(f"{field}[{index}] must be a bounded local reference or structured evidence")
            normalized.append(reference)
        elif isinstance(item, Mapping):
            normalized.append(_structured_evidence(item, field, index))
        else:
            raise PlanError(f"{field}[{index}] must be a bounded reference or structured evidence")
    return normalized


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise PlanError(f"{field} must be between {minimum} and {maximum}")
    return value


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise PlanError("plan values must be JSON serializable") from exc


def _relative_path(value: Any, field: str, *, max_length: int = 500) -> str:
    path = _text(value, field, max_length=max_length).replace("\\", "/")
    path_without_dot_prefix = re.sub(r"^(?:\./)+", "", path)
    if (
        path.startswith(("/", "~"))
        or re.match(r"^[A-Za-z]:", path_without_dot_prefix)
        or URI_SCHEME.match(path_without_dot_prefix)
    ):
        raise PlanError(f"{field} must be relative and contained")
    if ".." in PurePosixPath(path).parts:
        raise PlanError(f"{field} must be relative and contained")
    return str(PurePosixPath(path))


def _scope_action_reason(scope: Mapping[str, Any], action: Any, target: Any) -> str | None:
    if not isinstance(action, str):
        return "action name is not a string"
    if not action:
        return "action name is empty"
    if len(action) > 100:
        return "action name exceeds 100 characters"
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in action):
        return "action name contains a control or format character"
    if action != action.strip():
        return "action name contains surrounding whitespace"
    if not SAFE_ACTION.fullmatch(action):
        return "action name is not a declared safe action"

    target_provided = target is not None
    if target_provided and not isinstance(target, str):
        return "target is not a string"
    if target_provided and isinstance(target, str):
        if not target:
            return "target is empty; omit it only for a logical action"
        if len(target) > 500:
            return "target exceeds 500 characters"
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in target):
            return "target contains a control or format character"
        if target != target.strip():
            return "target contains surrounding whitespace"
        if "\\" in target:
            return "target contains a backslash escape"
        if target.startswith(("/", "~", "//")) or re.match(r"^[A-Za-z]:", target):
            return "target is absolute, a drive path, or a UNC path"
        if URI_SCHEME.match(target):
            return "target contains a URL or URI scheme"
        if "@" in target:
            return "target looks like a credential or external host"
        if ".." in PurePosixPath(target).parts:
            return "target escapes the declared scope"

    if action in scope.get("out_of_scope_actions", []):
        return f"action {action!r} is explicitly out of scope"
    if action not in scope["allowed_actions"]:
        return f"action {action!r} is not declared in allowed_actions"
    if target_provided and isinstance(target, str):
        normalized = str(PurePosixPath(target))
        if scope.get("allowed_paths", []) and not any(
            normalized == prefix[:-1] or normalized.startswith(prefix) for prefix in scope["allowed_paths"]
        ):
            return "target is outside scope.allowed_paths"
    elif scope.get("allowed_paths", []) and action not in scope.get("logical_actions", []):
        return "a target is required for this action in a path-constrained scope"
    return None


def _sanitize_refusal_target(target: Any) -> str | None:
    """Return only a safe local target representation for refusal records."""

    if target is None:
        return None
    if not isinstance(target, str):
        return "<invalid-target>"
    if (
        not target
        or len(target) > 500
        or any(unicodedata.category(char) in {"Cc", "Cf"} for char in target)
        or target != target.strip()
        or "\\" in target
        or target.startswith(("/", "~", "//"))
        or re.match(r"^[A-Za-z]:", target)
        or URI_SCHEME.match(target)
        or "@" in target
        or ".." in PurePosixPath(target).parts
    ):
        return "<invalid-target>"
    return target


def _phase_spec(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"phases[{index}] must be an object")
    phase_id = _text(value.get("id"), f"phases[{index}].id", max_length=80)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", phase_id):
        raise PlanError(f"phases[{index}].id must be kebab-case")
    name = _text(value.get("name", phase_id), f"phases[{index}].name", max_length=160)
    hypothesis = _text(value.get("hypothesis"), f"phases[{index}].hypothesis", max_length=1000)
    output = _text(
        value.get("output") or value.get("output_contract"),
        f"phases[{index}].output",
        max_length=1000,
    )
    verification = value.get("verification")
    if not isinstance(verification, Mapping):
        raise PlanError(f"phases[{index}].verification must be an object")
    check_id = _text(verification.get("id"), f"phases[{index}].verification.id", max_length=100)
    method = _text(verification.get("method"), f"phases[{index}].verification.method", max_length=1000)
    return {
        "id": phase_id,
        "name": name,
        "hypothesis": hypothesis,
        "output": output,
        "verification": {"id": check_id, "method": method},
    }


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a complete task-specific run contract."""

    if not isinstance(plan, Mapping):
        raise PlanError("plan must be an object")
    desired_outcome = _text(plan.get("desired_outcome"), "desired_outcome")

    scope = plan.get("scope")
    if not isinstance(scope, Mapping):
        raise PlanError("scope must be an object")
    included = _string_list(scope.get("included"), "scope.included")
    excluded = _string_list(scope.get("excluded"), "scope.excluded")
    allowed_actions = _string_list(scope.get("allowed_actions"), "scope.allowed_actions")
    for action in allowed_actions:
        if not SAFE_ACTION.fullmatch(action):
            raise PlanError(f"scope.allowed_actions contains invalid action {action!r}")
    allowed_paths = scope["allowed_paths"] if "allowed_paths" in scope else []
    if not isinstance(allowed_paths, list):
        raise PlanError("scope.allowed_paths must be a list")
    normalized_paths: list[str] = []
    for path in allowed_paths:
        path = _relative_path(path, "scope.allowed_paths[]")
        normalized_paths.append(path.rstrip("/") + "/")
    logical_actions = scope["logical_actions"] if "logical_actions" in scope else []
    logical_actions = _string_list(logical_actions, "scope.logical_actions", allow_empty=True)
    for action in logical_actions:
        if not SAFE_ACTION.fullmatch(action):
            raise PlanError(f"scope.logical_actions contains invalid action {action!r}")
    if not set(logical_actions).issubset(allowed_actions):
        raise PlanError("scope.logical_actions must be a subset of scope.allowed_actions")
    out_of_scope_actions = scope["out_of_scope_actions"] if "out_of_scope_actions" in scope else []
    out_of_scope_actions = _string_list(out_of_scope_actions, "scope.out_of_scope_actions", allow_empty=True)
    for action in out_of_scope_actions:
        if not SAFE_ACTION.fullmatch(action):
            raise PlanError(f"scope.out_of_scope_actions contains invalid action {action!r}")

    constraints = _string_list(plan.get("constraints"), "constraints")
    predicate = plan.get("terminal_predicate")
    if not isinstance(predicate, Mapping):
        raise PlanError("terminal_predicate must be an object")
    predicate_description = _text(predicate.get("description"), "terminal_predicate.description")
    predicate_checks = _string_list(predicate.get("checks"), "terminal_predicate.checks")

    phase_values = plan.get("phases")
    if not isinstance(phase_values, list) or not phase_values:
        raise PlanError("phases must be a non-empty list")
    phases = [_phase_spec(value, index) for index, value in enumerate(phase_values)]
    phase_ids = [phase["id"] for phase in phases]
    if len(phase_ids) != len(set(phase_ids)):
        raise PlanError("phase ids must be unique")
    verification_ids = [phase["verification"]["id"] for phase in phases]
    if len(verification_ids) != len(set(verification_ids)):
        raise PlanError("phase verification ids must be unique")

    limits_input = plan.get("limits")
    if not isinstance(limits_input, Mapping):
        raise PlanError("limits must be an object with finite bounds")
    limits = {
        key: _bounded_int(
            limits_input.get(key),
            f"limits.{key}",
            minimum=1,
            maximum=256 if key != "max_decisions" else 4096,
        )
        for key in DEFAULT_LIMITS
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "desired_outcome": desired_outcome,
        "scope": {
            "included": included,
            "excluded": excluded,
            "allowed_actions": allowed_actions,
            "allowed_paths": normalized_paths,
            "logical_actions": logical_actions,
            "out_of_scope_actions": out_of_scope_actions,
        },
        "constraints": constraints,
        "terminal_predicate": {"description": predicate_description, "checks": predicate_checks},
        "limits": limits,
        "phases": phases,
    }


def _workspace_root(path: str | os.PathLike[str]) -> Path:
    root = Path(path).absolute()
    try:
        root_mode = root.lstat().st_mode
    except FileNotFoundError:
        root_mode = None
    except OSError as exc:
        raise StateError(f"workspace root could not be inspected: {root}") from exc
    if root_mode is not None and not stat.S_ISDIR(root_mode) and not stat.S_ISLNK(root_mode):
        raise StateError(f"workspace root must be a directory: {root}")

    root = root.resolve(strict=False)
    try:
        resolved_mode = root.lstat().st_mode
    except FileNotFoundError:
        return root
    except OSError as exc:
        raise StateError(f"workspace root could not be inspected: {root}") from exc
    if not stat.S_ISDIR(resolved_mode):
        raise StateError(f"workspace root must be a directory: {root}")
    return root


def _state_path(
    path: str | os.PathLike[str],
    workspace_root: str | os.PathLike[str] | None,
    *,
    require_regular: bool = False,
) -> tuple[Path, Path]:
    raw = Path(path)
    if workspace_root is not None:
        root = _workspace_root(workspace_root)
    elif raw.is_absolute():
        root = _workspace_root(raw.parent)
    else:
        root = _workspace_root(Path.cwd())
    candidate = raw if raw.is_absolute() else root / raw
    candidate = candidate.absolute()
    if candidate.is_symlink():
        raise ScopeRefusal(
            "state path must not be a symlink",
            {"status": "refused", "reason": "state path is a symlink", "next_action": "choose a regular local state path"},
        )
    if require_regular:
        try:
            candidate_mode = candidate.lstat().st_mode
        except FileNotFoundError:
            candidate_mode = None
        except OSError as exc:
            raise StateError(f"state path could not be inspected: {candidate}") from exc
        if candidate_mode is not None and not stat.S_ISREG(candidate_mode):
            raise StateError(f"state path must be a regular file: {candidate}")
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        refusal = {
            "status": "refused",
            "reason": "state path escapes workspace",
            "next_action": "choose a state path below the workspace root",
        }
        raise ScopeRefusal("state path is outside the workspace root", refusal) from exc
    return candidate, root


def _state_text(value: Any, field: str, *, max_length: int = MAX_TEXT) -> None:
    try:
        _text(value, field, max_length=max_length)
    except PlanError as exc:
        raise StateError(f"invalid state: {exc}") from exc


def _state_evidence(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise StateError(f"invalid state: {field} must be a list")
    try:
        _evidence(value, field)
    except (PlanError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise StateError(f"invalid state: {exc}") from exc


def _state_output(value: Any, field: str) -> None:
    if value is None:
        return
    if value == "" or value == {} or value == []:
        raise StateError(f"invalid state: {field} must be present")
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise StateError(f"invalid state: {field} must be JSON serializable") from exc


def _state_phase_verification(value: Any, field: str, expected_check_id: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise StateError(f"invalid state: {field} must be an object or null")
    _require_state_fields(value, {"check_id", "passed", "evidence", "observed"}, field)
    if value["check_id"] != expected_check_id or value["passed"] is not True:
        raise StateError(f"invalid state: {field} has an invalid check result")
    _state_evidence(value["evidence"], f"{field}.evidence")
    _state_text(value["observed"], f"{field}.observed", max_length=1000)


def _state_failure(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise StateError(f"invalid state: {field} must be an object or null")
    _require_state_fields(value, {"reason", "evidence", "next_action", "at"}, field)
    _state_text(value["reason"], f"{field}.reason", max_length=1000)
    _state_evidence(value["evidence"], f"{field}.evidence")
    _state_text(value["next_action"], f"{field}.next_action", max_length=1000)
    _state_text(value["at"], f"{field}.at")


def _require_state_fields(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(fields - set(value))
    if missing:
        raise StateError(f"invalid state: {label} is missing required fields: {', '.join(missing)}")


def _validate_lifecycle_graph_v2(
    state: Mapping[str, Any],
    validated_plan: Mapping[str, Any],
    phases: Sequence[Mapping[str, Any]],
    checkpoint_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Replay v2 events and require exact append-order accounting."""

    phase_ids = [phase["id"] for phase in validated_plan["phases"]]
    phase_by_id = {phase["id"]: phase for phase in phases}
    check_ids = set(validated_plan["terminal_predicate"]["checks"])
    phase_states = {phase_id: "pending" for phase_id in phase_ids}
    phase_attempts = {phase_id: 0 for phase_id in phase_ids}
    last_phase_passed: dict[str, Mapping[str, Any]] = {}
    last_phase_failed: dict[str, Mapping[str, Any]] = {}
    last_predicate_event: dict[str, Mapping[str, Any]] = {}
    seen_checkpoints: set[str] = set()
    checkpoint_event_order: list[str] = []
    latest_checkpoint: dict[str, str | None] = {phase_id: None for phase_id in phase_ids}
    latest_interruption: Mapping[str, Any] | None = None
    latest_interruption_snapshot: Mapping[str, Any] | None = None
    latest_blocker: Mapping[str, Any] | None = None
    last_replacement: dict[str, Mapping[str, Any]] = {}
    seen_replans: list[int] = []
    replan_event_order: list[int] = []
    run_status = "planned"
    resume_count = 0
    expected_next_action = f"start phase {phase_ids[0]}"
    next_replan_number = 1
    completed_event: Mapping[str, Any] | None = None

    decision_rows_by_sequence = {
        decision["event_sequence"]: decision for decision in state["decisions"]
    }
    decision_row_positions = {
        decision["event_sequence"]: index for index, decision in enumerate(state["decisions"])
    }
    replans_by_number = {replan["number"]: replan for replan in state["replans"]}
    listed_decision_sequences: list[int] = []
    listed_decision_count = 0

    def fail(message: str) -> None:
        raise StateError(f"invalid state: {message}")

    def current_phase() -> str | None:
        return next((phase_id for phase_id in phase_ids if phase_states[phase_id] != "passed"), None)

    def all_phases_passed() -> bool:
        return all(phase_states[phase_id] == "passed" for phase_id in phase_ids)

    def event_text(event: Mapping[str, Any], index: int, field: str, *, max_length: int = MAX_TEXT) -> str:
        if field not in event:
            fail(f"events[{index}] is missing {field}")
        _state_text(event[field], f"events[{index}].{field}", max_length=max_length)
        return event[field]

    def event_phase(event: Mapping[str, Any], index: int, *, allow_none: bool = False) -> str | None:
        if "phase" not in event:
            fail(f"events[{index}] is missing phase")
        phase_id = event["phase"]
        if allow_none and phase_id is None:
            return None
        if not isinstance(phase_id, str) or phase_id not in phase_ids:
            fail(f"events[{index}].phase is not a declared phase")
        return phase_id

    def event_int(event: Mapping[str, Any], index: int, field: str, *, minimum: int = 1) -> int:
        value = event.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            fail(f"events[{index}].{field} must be an integer at least {minimum}")
        return value

    def event_bool(event: Mapping[str, Any], index: int, field: str) -> bool:
        value = event.get(field)
        if not isinstance(value, bool):
            fail(f"events[{index}].{field} must be a boolean")
        return value

    def decision_for_event(event: Mapping[str, Any], index: int) -> Mapping[str, Any] | None:
        nonlocal listed_decision_count
        event_name = event["event"]
        decision_fields = {"decision", "decision_in_list", "decision_overflow"}
        present = decision_fields & set(event)
        if event_name not in DECISION_EVENTS:
            if present:
                fail(f"events[{index}] embeds a decision on a non-decision event")
            return None
        if present != decision_fields:
            fail(f"events[{index}] has incomplete decision accounting")
        decision = event["decision"]
        if not isinstance(decision, Mapping):
            fail(f"events[{index}].decision must be an object")
        _require_state_fields(
            decision,
            {"at", "phase", "decision", "why", "evidence", "result", "event_sequence"},
            f"events[{index}].decision",
        )
        for field in ("at", "phase", "decision", "why"):
            _state_text(decision[field], f"events[{index}].decision.{field}")
        if not isinstance(decision["result"], str) or decision["result"] not in VALID_RESULTS:
            fail(f"events[{index}].decision.result is invalid")
        _state_evidence(decision["evidence"], f"events[{index}].decision.evidence")
        if decision.get("status") is not None and (
            not isinstance(decision["status"], str) or decision["status"] not in {"blocked", "refused"}
        ):
            fail(f"events[{index}].decision.status is invalid")
        if ("reason" in decision) != ("next_action" in decision):
            fail(f"events[{index}].decision reason and next_action must be paired")
        if "reason" in decision:
            _state_text(decision["reason"], f"events[{index}].decision.reason")
            _state_text(decision["next_action"], f"events[{index}].decision.next_action")
        sequence = event["sequence"]
        if decision.get("event_sequence") != sequence:
            fail(f"events[{index}].decision has a mismatched event sequence")
        in_list = event["decision_in_list"]
        overflow = event["decision_overflow"]
        if not isinstance(in_list, bool) or not isinstance(overflow, bool):
            fail(f"events[{index}] decision accounting flags must be booleans")
        row = decision_rows_by_sequence.get(sequence)
        if in_list:
            if listed_decision_count >= len(state["decisions"]):
                fail(f"events[{index}] lists a decision row beyond the recorded decision history")
            expected_row = state["decisions"][listed_decision_count]
            if row is None or row != decision or expected_row != decision:
                fail(f"events[{index}] decision does not match its persisted decision row in append order")
            if decision_row_positions.get(sequence) != listed_decision_count:
                fail(f"events[{index}] decision row position does not match append order")
            listed_decision_sequences.append(sequence)
            expected_overflow = listed_decision_count >= validated_plan["limits"]["max_decisions"]
            if overflow != expected_overflow:
                fail(f"events[{index}] has an invalid decision overflow flag")
            if expected_overflow and decision.get("status") not in {"blocked", "refused"}:
                fail(f"events[{index}] records overflow without a blocked or refused decision")
            listed_decision_count += 1
        else:
            if row is not None or not overflow or event_name not in {"action_refused", "blocked"}:
                fail(f"events[{index}] has an unaccounted embedded decision")
            if listed_decision_count <= validated_plan["limits"]["max_decisions"]:
                fail(f"events[{index}] claims decision overflow before recorded capacity pressure")
            if decision.get("status") not in {"blocked", "refused"}:
                fail(f"events[{index}] records overflow without a blocked or refused decision")
        return decision

    for index, event in enumerate(state["events"]):
        if event["sequence"] != index + 1:
            fail(f"events[{index}].sequence is not contiguous append order")
        event_name = event["event"]
        if event_name not in LIFECYCLE_EVENTS | AUDIT_EVENTS | DECISION_EVENTS:
            fail(f"events[{index}].event is not a supported event")
        decision = decision_for_event(event, index)
        if completed_event is not None and event_name != "action_refused":
            fail("lifecycle events cannot be appended after completion")

        if event_name == "action_allowed":
            if run_status not in {"planned", "running"}:
                fail(f"events[{index}] allows an action from {run_status}")
            action = event_text(event, index, "action", max_length=100)
            scope_reason = _scope_action_reason(validated_plan["scope"], action, event.get("target"))
            if scope_reason is not None:
                fail(f"events[{index}] records an action outside scope: {scope_reason}")
            continue

        if event_name == "action_refused":
            if event.get("status") != "refused":
                fail(f"events[{index}].action_refused must have refused status")
            action = event_text(event, index, "action", max_length=100)
            reason = event_text(event, index, "reason", max_length=1000)
            next_action = event_text(event, index, "next_action", max_length=1000)
            if event.get("target") is not None:
                _state_text(event["target"], f"events[{index}].target", max_length=500)
            if decision is None or decision.get("status") != "refused" or decision.get("result") != "blocked":
                fail(f"events[{index}] refusal is missing its refusal decision")
            if decision["why"] != reason or decision["reason"] != reason or decision["next_action"] != next_action:
                fail(f"events[{index}] refusal decision does not match the refusal")
            if decision["phase"] != (current_phase() or "run"):
                fail(f"events[{index}] refusal decision phase does not match the run")
            if event.get("target") is not None and event["target"] != _sanitize_refusal_target(event["target"]):
                fail(f"events[{index}] refusal target is not sanitized")
            expected_action = action if SAFE_ACTION.fullmatch(action) else "invalid-action"
            if decision["decision"] != f"refused action {expected_action}":
                fail(f"events[{index}] refusal decision does not match the action")
            continue

        if event_name == "decision_recorded":
            if run_status == "completed":
                fail(f"events[{index}] records a decision after completion")
            event_phase_value = event.get("phase")
            if event_phase_value == "run":
                if decision is None or decision["phase"] != "run":
                    fail(f"events[{index}] run decision is not bound to phase run")
            elif event_phase_value in phase_ids:
                if decision is None or decision["phase"] != event_phase_value:
                    fail(f"events[{index}] decision phase does not match the event")
            else:
                fail(f"events[{index}].phase is not a declared phase or run")
            continue

        if event_name == "phase_started":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            if run_status not in {"planned", "running"}:
                fail(f"events[{index}] starts a phase from {run_status}")
            if phase_id != current_phase() or phase_states[phase_id] != "pending":
                fail(f"events[{index}] starts a phase that is not the current pending phase")
            attempt = event_int(event, index, "attempt")
            if attempt != phase_attempts[phase_id] + 1:
                fail(f"events[{index}] has a non-sequential attempt for {phase_id}")
            phase_attempts[phase_id] = attempt
            latest_checkpoint[phase_id] = None
            phase_states[phase_id] = "in_progress"
            run_status = "running"
            expected_next_action = f"produce and verify phase {phase_id} output"
            continue

        if event_name == "checkpoint":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            checkpoint_id = event_text(event, index, "checkpoint", max_length=100)
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] records a checkpoint outside the current phase")
            checkpoint = checkpoint_by_id.get(checkpoint_id)
            if checkpoint is None or checkpoint["phase"] != phase_id:
                fail(f"events[{index}] references a checkpoint for another phase")
            if checkpoint_id in seen_checkpoints:
                fail(f"events[{index}] repeats checkpoint {checkpoint_id}")
            if checkpoint["event_sequence"] != event["sequence"]:
                fail(f"events[{index}] checkpoint row is not bound to this event")
            seen_checkpoints.add(checkpoint_id)
            checkpoint_event_order.append(checkpoint_id)
            latest_checkpoint[phase_id] = checkpoint_id
            if event.get("checkpoint_snapshot") != checkpoint:
                fail(f"events[{index}].checkpoint_snapshot does not match the checkpoint")
            expected_next_action = checkpoint["next_action"]
            continue

        if event_name == "phase_passed":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            check_id = event_text(event, index, "check_id", max_length=100)
            if check_id != phase_by_id[phase_id]["verification"]["id"]:
                fail(f"events[{index}] uses an undeclared phase verification")
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] passes a phase outside the current phase")
            if decision is None or decision["phase"] != phase_id or decision["decision"] != f"passed phase {phase_id}":
                fail(f"events[{index}] phase pass is missing its matching decision")
            if decision["why"] != "the phase output and declared verification both passed" or decision.get("status") is not None or "reason" in decision:
                fail(f"events[{index}] phase pass decision has forged metadata")
            phase_states[phase_id] = "passed"
            last_phase_passed[phase_id] = event
            if "output_snapshot" not in event:
                fail(f"events[{index}] is missing output_snapshot")
            _state_output(event["output_snapshot"], f"events[{index}].output_snapshot")
            if event["output_snapshot"] != phase_by_id[phase_id]["output_value"]:
                fail(f"events[{index}].output_snapshot does not match the phase output")
            if "verification_snapshot" not in event:
                fail(f"events[{index}] is missing verification_snapshot")
            _state_phase_verification(event["verification_snapshot"], f"events[{index}].verification_snapshot", check_id)
            if event["verification_snapshot"] != phase_by_id[phase_id]["verification_result"]:
                fail(f"events[{index}].verification_snapshot does not match the phase verification")
            if decision["result"] != "verified" or decision["evidence"] != event["verification_snapshot"]["evidence"]:
                fail(f"events[{index}] phase pass decision does not match its verification")
            next_phase = current_phase()
            expected_next_action = f"inspect output, then start phase {next_phase}" if next_phase is not None else "record terminal predicate checks"
            continue

        if event_name == "phase_failed":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            reason = event_text(event, index, "reason", max_length=1000)
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] fails a phase outside the current phase")
            if decision is None or decision["phase"] != phase_id or decision["decision"] != f"failed: {reason}":
                fail(f"events[{index}] phase failure is missing its matching decision")
            if decision["why"] != "the declared phase check did not pass" or decision.get("status") is not None or "reason" in decision:
                fail(f"events[{index}] phase failure decision has forged metadata")
            phase_states[phase_id] = "failed"
            last_phase_failed[phase_id] = event
            run_status = "failed"
            if "failure_snapshot" not in event:
                fail(f"events[{index}] is missing failure_snapshot")
            _state_failure(event["failure_snapshot"], f"events[{index}].failure_snapshot")
            if event["failure_snapshot"]["reason"] != reason:
                fail(f"events[{index}].failure_snapshot does not match the failure")
            if decision["result"] != "failed" or decision["evidence"] != event["failure_snapshot"]["evidence"]:
                fail(f"events[{index}] phase failure decision does not match its failure")
            expected_next_action = ""
            continue

        if event_name == "replanned":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            number = event_int(event, index, "number")
            if run_status != "failed" or phase_id != current_phase() or phase_states[phase_id] != "failed":
                fail(f"events[{index}] replans outside the failed current phase")
            if number != next_replan_number:
                fail(f"events[{index}] has a non-sequential replan number")
            replan = replans_by_number.get(number)
            if replan is None or replan["phase"] != phase_id:
                fail(f"events[{index}] has no matching replan record")
            if replan["event_sequence"] != event["sequence"]:
                fail(f"events[{index}] replan row is not bound to this event")
            if decision is None or decision["phase"] != phase_id or decision["decision"] != "replan phase":
                fail(f"events[{index}] replan is missing its matching decision")
            if decision["why"] != replan["reason"]:
                fail(f"events[{index}] replan decision does not match the replan")
            if decision["result"] != "open" or decision["evidence"] != replan["evidence"] or decision.get("status") is not None or "reason" in decision:
                fail(f"events[{index}] replan decision has forged metadata")
            if event.get("replacement") != replan["replacement"] or event.get("replan_snapshot") != replan:
                fail(f"events[{index}] replan snapshot does not match the replan record")
            phase_states[phase_id] = "pending"
            latest_checkpoint[phase_id] = None
            run_status = "planned"
            last_replacement[phase_id] = replan["replacement"]
            seen_replans.append(number)
            replan_event_order.append(event["sequence"])
            next_replan_number += 1
            expected_next_action = f"start replanned phase {phase_id}"
            continue

        if event_name == "interrupted":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            checkpoint_id = event_text(event, index, "checkpoint", max_length=100)
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] interrupts outside the current phase")
            if latest_checkpoint.get(phase_id) != checkpoint_id:
                fail(f"events[{index}] interrupts without the latest checkpoint")
            phase_states[phase_id] = "interrupted"
            run_status = "interrupted"
            interruption = {"phase": phase_id, "checkpoint": checkpoint_id}
            if "reason" in event:
                interruption["reason"] = event["reason"]
                _state_text(event["reason"], f"events[{index}].reason", max_length=1000)
            if "interruption_snapshot" not in event:
                fail(f"events[{index}] is missing interruption_snapshot")
            snapshot = event["interruption_snapshot"]
            if not isinstance(snapshot, Mapping):
                fail(f"events[{index}].interruption_snapshot must be an object")
            _require_state_fields(snapshot, {"at", "phase", "checkpoint", "reason"}, f"events[{index}].interruption_snapshot")
            for field in ("at", "phase", "checkpoint", "reason"):
                _state_text(snapshot[field], f"events[{index}].interruption_snapshot.{field}")
            if snapshot["phase"] != phase_id or snapshot["checkpoint"] != checkpoint_id or snapshot["reason"] != interruption.get("reason"):
                fail(f"events[{index}].interruption_snapshot does not match the interruption")
            latest_interruption_snapshot = snapshot
            latest_interruption = interruption
            expected_next_action = f"resume from checkpoint {checkpoint_id}"
            continue

        if event_name == "resumed":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            checkpoint_id = event_text(event, index, "checkpoint", max_length=100)
            if run_status != "interrupted" or phase_states[phase_id] != "interrupted":
                fail(f"events[{index}] resumes outside the interrupted phase")
            if latest_interruption is None or latest_interruption["phase"] != phase_id or latest_interruption["checkpoint"] != checkpoint_id:
                fail(f"events[{index}] does not match the interruption")
            phase_states[phase_id] = "in_progress"
            run_status = "running"
            resume_count += 1
            expected_next_action = f"continue phase {phase_id} from checkpoint {checkpoint_id}"
            continue

        if event_name == "blocked":
            phase_id = event_phase(event, index, allow_none=True)
            if run_status == "completed":
                fail(f"events[{index}] blocks a completed run")
            if phase_id != current_phase():
                fail(f"events[{index}] blocker phase does not match the current phase")
            if decision is None or decision.get("status") != "blocked" or decision.get("result") != "blocked":
                fail(f"events[{index}] blocker is missing its matching decision")
            reason = event_text(event, index, "reason", max_length=1000)
            next_action = event_text(event, index, "next_action", max_length=1000)
            if decision["phase"] != (phase_id or "run") or decision["decision"] != f"blocked: {reason}":
                fail(f"events[{index}] blocker decision does not match the run")
            if decision["why"] != "the run cannot proceed within its current constraints" or decision["reason"] != reason or decision["next_action"] != next_action:
                fail(f"events[{index}] blocker decision does not match the blocker")
            if phase_id is not None and phase_states[phase_id] in {"pending", "in_progress", "interrupted"}:
                phase_states[phase_id] = "blocked"
            run_status = "blocked"
            latest_blocker = event
            if "blocker_snapshot" not in event:
                fail(f"events[{index}] is missing blocker_snapshot")
            _state_failure(event["blocker_snapshot"], f"events[{index}].blocker_snapshot")
            if event["blocker_snapshot"]["reason"] != reason or event["blocker_snapshot"]["next_action"] != next_action:
                fail(f"events[{index}].blocker_snapshot does not match the blocker")
            if decision["evidence"] != event["blocker_snapshot"]["evidence"]:
                fail(f"events[{index}] blocker decision evidence does not match the blocker")
            expected_next_action = next_action
            continue

        if event_name == "predicate_recorded":
            check_id = event_text(event, index, "check_id", max_length=100)
            passed = event_bool(event, index, "passed")
            if run_status != "running" or not all_phases_passed():
                fail(f"events[{index}] records a predicate before all phases pass")
            if check_id not in check_ids:
                fail(f"events[{index}] records an undeclared predicate")
            if decision is None or decision["phase"] != "run" or decision["decision"] != f"terminal check {check_id}":
                fail(f"events[{index}] predicate is missing its matching decision")
            if decision["why"] != "the terminal predicate check was recorded" or decision.get("status") is not None or "reason" in decision:
                fail(f"events[{index}] predicate decision has forged metadata")
            snapshot = event.get("result_snapshot")
            if not isinstance(snapshot, Mapping):
                fail(f"events[{index}].result_snapshot must be an object")
            _require_state_fields(snapshot, {"check_id", "passed", "status", "evidence", "observed", "at", "event_sequence"}, f"events[{index}].result_snapshot")
            if snapshot["check_id"] != check_id or snapshot["passed"] is not passed or snapshot["event_sequence"] != event["sequence"]:
                fail(f"events[{index}].result_snapshot does not match the predicate")
            if snapshot["status"] not in CHECK_STATUSES or (snapshot["status"] == "verified") is not passed:
                fail(f"events[{index}].result_snapshot has an invalid status")
            _state_evidence(snapshot["evidence"], f"events[{index}].result_snapshot.evidence")
            _state_text(snapshot["observed"], f"events[{index}].result_snapshot.observed", max_length=1000)
            _state_text(snapshot["at"], f"events[{index}].result_snapshot.at")
            if decision["result"] != snapshot["status"] or decision["evidence"] != snapshot["evidence"]:
                fail(f"events[{index}] predicate decision does not match its result")
            last_predicate_event[check_id] = event
            expected_next_action = "finish when all terminal checks pass" if passed else f"address failed terminal check {check_id}"
            continue

        if event_name == "completed":
            if run_status != "running" or not all_phases_passed():
                fail(f"events[{index}] completes a run before the predicate is ready")
            if any(check_id not in last_predicate_event or not last_predicate_event[check_id]["passed"] for check_id in check_ids):
                fail(f"events[{index}] completes a run without all verified predicate events")
            if not isinstance(event.get("terminal_snapshot"), Mapping):
                fail(f"events[{index}] is missing terminal_snapshot")
            completed_event = event
            run_status = "completed"
            expected_next_action = ""
            continue

    if listed_decision_count != len(state["decisions"]):
        fail("decision rows do not match the number of listed decision events")
    if listed_decision_sequences != [decision["event_sequence"] for decision in state["decisions"]]:
        fail("decision rows do not match decision-bearing events in append order")
    if len(checkpoint_event_order) != len(checkpoint_by_id) or set(checkpoint_event_order) != set(checkpoint_by_id):
        fail("checkpoint rows and checkpoint events are not a one-to-one set")
    if checkpoint_event_order != [checkpoint["id"] for checkpoint in state["checkpoints"]]:
        fail("checkpoint rows do not match checkpoint events in append order")
    for phase_id in phase_ids:
        expected_refs = [checkpoint_id for checkpoint_id in checkpoint_event_order if checkpoint_by_id[checkpoint_id]["phase"] == phase_id]
        if phase_by_id[phase_id]["checkpoints"] != expected_refs:
            fail(f"phase {phase_id} checkpoint references do not match checkpoint events")
    if seen_replans != [replan["number"] for replan in state["replans"]]:
        fail("replan rows do not match replan events in append order")
    if replan_event_order != [replan["event_sequence"] for replan in state["replans"]]:
        fail("replan rows do not match replan events in append order")

    if run_status != state["status"]:
        fail("run status does not match the lifecycle event graph")
    if {phase["id"]: phase["status"] for phase in phases} != phase_states:
        fail("phase statuses do not match the lifecycle event graph")
    if {phase["id"]: phase["attempt"] for phase in phases} != phase_attempts:
        fail("phase attempts do not match the lifecycle event graph")
    if state["resume_count"] != resume_count:
        fail("resume_count does not match the lifecycle event graph")
    if state["current_phase"] != current_phase():
        fail("current_phase does not match the lifecycle event graph")
    if state["status"] == "completed" and completed_event is None:
        fail("completed runs require a completed lifecycle event")
    if state["status"] != "completed" and completed_event is not None:
        fail("non-completed runs cannot retain a completed lifecycle event")
    if completed_event is not None and completed_event["terminal_snapshot"] != state["terminal"]:
        fail("terminal snapshot does not match the completed lifecycle event")

    for phase in phases:
        phase_id = phase["id"]
        if phase_id in last_replacement:
            final_spec = next(item for item in validated_plan["phases"] if item["id"] == phase_id)
            if final_spec != last_replacement[phase_id]:
                fail(f"phase {phase_id} plan does not match its final replacement")
        if phase["status"] == "passed":
            event = last_phase_passed.get(phase_id)
            if event is None or event["check_id"] != phase["verification"]["id"]:
                fail(f"phase {phase_id} has no final matching phase_passed event")
            if event["verification_snapshot"] != phase["verification_result"] or event["output_snapshot"] != phase["output_value"]:
                fail(f"phase {phase_id} output does not match its lifecycle event")
        if phase["status"] == "failed":
            event = last_phase_failed.get(phase_id)
            if event is None or event["failure_snapshot"] != phase["failure"]:
                fail(f"phase {phase_id} failure does not match its lifecycle event")

    if latest_interruption is None:
        if state["interruption"] is not None:
            fail("interruption details exist without an interrupted lifecycle event")
    else:
        interruption = state["interruption"]
        if interruption is None or interruption["phase"] != latest_interruption["phase"] or interruption["checkpoint"] != latest_interruption["checkpoint"]:
            fail("interruption details do not match the lifecycle event")
        if "reason" in latest_interruption and interruption.get("reason") != latest_interruption["reason"]:
            fail("interruption reason does not match the lifecycle event")
        if latest_interruption_snapshot != interruption:
            fail("interruption snapshot does not match the lifecycle event")

    if latest_blocker is None:
        if state["status"] == "blocked":
            fail("blocked runs require a blocked lifecycle event")
    else:
        blocker = state["blocker"]
        if blocker is None or blocker["phase"] != latest_blocker.get("phase") or blocker["reason"] != latest_blocker["reason"]:
            fail("blocker details do not match the lifecycle event")
        if latest_blocker["blocker_snapshot"] != blocker:
            fail("blocker snapshot does not match the lifecycle event")

    for check_id in check_ids:
        result = state["predicate_results"][check_id]
        event = last_predicate_event.get(check_id)
        if result is None:
            if event is not None:
                fail(f"predicate_results[{check_id}] is missing its lifecycle event result")
        else:
            if event is None or event["result_snapshot"] != result:
                fail(f"predicate_results[{check_id}] does not match its lifecycle event")
            if event["decision"]["result"] != result["status"] or event["decision"]["evidence"] != result["evidence"]:
                fail(f"predicate_results[{check_id}] does not match its terminal decision")

    if state["status"] == "completed":
        expected_next_action = None
    elif state["status"] == "blocked":
        expected_next_action = state["blocker"]["next_action"]
    elif state["status"] == "failed":
        phase = next(phase for phase in phases if phase["id"] == state["current_phase"])
        expected_next_action = f"replan phase {phase['id']}: {phase['failure']['next_action']}"
    elif state["status"] == "interrupted":
        expected_next_action = f"resume from checkpoint {state['interruption']['checkpoint']}"
    if state["next_action"] != expected_next_action:
        fail("next_action does not match the lifecycle event graph")


def _require_state_object(state: Mapping[str, Any]) -> None:
    if not isinstance(state, Mapping):
        raise StateError("invalid state: root must be an object")

    required_fields = {
        "schema_version",
        "run_id",
        "created_at",
        "updated_at",
        "status",
        "plan",
        "phases",
        "current_phase",
        "checkpoints",
        "decisions",
        "replans",
        "predicate_results",
        "resume_count",
        "next_action",
        "terminal",
        "blocker",
        "interruption",
        "events",
    }
    _require_state_fields(state, required_fields, "root")
    if state["schema_version"] != SCHEMA_VERSION:
        raise StateError("invalid state: unsupported state schema")
    for field in ("run_id", "created_at", "updated_at"):
        _state_text(state[field], field)

    status = state["status"]
    if not isinstance(status, str) or status not in RUN_STATUSES:
        raise StateError(f"invalid state: status must be one of {sorted(RUN_STATUSES)}")

    plan = state["plan"]
    if not isinstance(plan, Mapping):
        raise StateError("invalid state: plan must be an object")
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise StateError("invalid state: plan has an unsupported schema")
    try:
        validated_plan = validate_plan(plan)
    except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise StateError(f"invalid state.plan: {exc}") from exc

    phases = state["phases"]
    if not isinstance(phases, list) or not phases:
        raise StateError("invalid state: phases must be a non-empty list")
    if len(phases) != len(validated_plan["phases"]):
        raise StateError("invalid state: phases do not match the plan")
    phase_ids = [phase["id"] for phase in validated_plan["phases"]]
    phase_checkpoint_refs: dict[str, list[str]] = {}
    for index, (phase, spec) in enumerate(zip(phases, validated_plan["phases"])):
        if not isinstance(phase, Mapping):
            raise StateError(f"invalid state: phases[{index}] must be an object")
        _require_state_fields(
            phase,
            {
                "id",
                "name",
                "hypothesis",
                "output",
                "verification",
                "status",
                "attempt",
                "output_value",
                "verification_result",
                "failure",
                "checkpoints",
            },
            f"phases[{index}]",
        )
        for field in ("id", "name", "hypothesis", "output", "verification"):
            if phase[field] != spec[field]:
                raise StateError(f"invalid state: phases[{index}].{field} does not match the plan")
        if not isinstance(phase["status"], str) or phase["status"] not in PHASE_STATUSES:
            raise StateError(f"invalid state: phases[{index}].status is not supported")
        if (
            isinstance(phase["attempt"], bool)
            or not isinstance(phase["attempt"], int)
            or not 0 <= phase["attempt"] <= validated_plan["limits"]["max_attempts_per_phase"]
        ):
            raise StateError(f"invalid state: phases[{index}].attempt must be a non-negative integer")
        if phase["status"] in {"in_progress", "passed", "failed", "interrupted"} and phase["attempt"] < 1:
            raise StateError(f"invalid state: phases[{index}] has a lifecycle status with zero attempts")
        if not isinstance(phase["checkpoints"], list):
            raise StateError(f"invalid state: phases[{index}].checkpoints must be a list")
        checkpoint_refs: list[str] = []
        for checkpoint_index, checkpoint_id in enumerate(phase["checkpoints"]):
            _state_text(checkpoint_id, f"phases[{index}].checkpoints[{checkpoint_index}]", max_length=100)
            checkpoint_refs.append(checkpoint_id)
        if len(checkpoint_refs) != len(set(checkpoint_refs)):
            raise StateError(f"invalid state: phases[{index}].checkpoints contains duplicates")
        phase_checkpoint_refs[phase["id"]] = checkpoint_refs

        output_value = phase["output_value"]
        _state_output(output_value, f"phases[{index}].output_value")
        verification_result = phase["verification_result"]
        _state_phase_verification(
            verification_result,
            f"phases[{index}].verification_result",
            spec["verification"]["id"],
        )
        failure = phase["failure"]
        _state_failure(failure, f"phases[{index}].failure")
        if phase["status"] == "passed":
            if output_value is None:
                raise StateError(f"invalid state: phases[{index}].passed phase is missing output_value")
            if verification_result is None:
                raise StateError(f"invalid state: phases[{index}].passed phase is missing verification_result")
            if failure is not None:
                raise StateError(f"invalid state: phases[{index}].passed phase has a failure")
        else:
            if output_value is not None or verification_result is not None:
                raise StateError(f"invalid state: phases[{index}] has output or verification before passing")
        if phase["status"] == "failed" and failure is None:
            raise StateError(f"invalid state: phases[{index}].failed phase is missing failure")
        if phase["status"] != "failed" and failure is not None:
            raise StateError(f"invalid state: phases[{index}] has failure outside failed status")

    current_phase = state["current_phase"]
    if current_phase is not None and (not isinstance(current_phase, str) or current_phase not in phase_ids):
        raise StateError("invalid state: current_phase must name a declared phase or be null")

    phase_statuses = [phase["status"] for phase in phases]
    all_phases_passed = all(phase_status == "passed" for phase_status in phase_statuses)
    if all_phases_passed:
        if current_phase is not None:
            raise StateError("invalid state: all passed runs cannot have a current phase")
        if status not in {"running", "blocked", "completed"}:
            raise StateError("invalid state: completed phases require a running, blocked, or completed run")
    else:
        first_incomplete = next(index for index, phase_status in enumerate(phase_statuses) if phase_status != "passed")
        if any(phase_status != "passed" for phase_status in phase_statuses[:first_incomplete]):
            raise StateError("invalid state: phase lifecycle is not sequential")
        if any(phase_status != "pending" for phase_status in phase_statuses[first_incomplete + 1 :]):
            raise StateError("invalid state: later phases must remain pending")
        if current_phase != phase_ids[first_incomplete]:
            raise StateError("invalid state: current_phase must identify the first incomplete phase")
        current_status = phase_statuses[first_incomplete]
        allowed_current_statuses = {
            "planned": {"pending"},
            "running": {"pending", "in_progress"},
            "failed": {"failed"},
            "interrupted": {"interrupted"},
            "blocked": {"blocked", "failed"},
        }
        if status == "completed" or current_status not in allowed_current_statuses.get(status, set()):
            raise StateError("invalid state: run status and current phase status do not agree")

    if isinstance(state["resume_count"], bool) or not isinstance(state["resume_count"], int) or state["resume_count"] < 0:
        raise StateError("invalid state: resume_count must be a non-negative integer")
    if status == "completed":
        if state["next_action"] is not None:
            raise StateError("invalid state: completed runs cannot have a next action")
    else:
        _state_text(state["next_action"], "next_action")

    for field in ("checkpoints", "decisions", "replans", "events"):
        if not isinstance(state[field], list):
            raise StateError(f"invalid state: {field} must be a list")

    predicate_results = state["predicate_results"]
    if not isinstance(predicate_results, Mapping):
        raise StateError("invalid state: predicate_results must be an object")
    check_ids = set(validated_plan["terminal_predicate"]["checks"])
    if set(predicate_results) != check_ids:
        raise StateError("invalid state: predicate_results do not match the terminal predicate")
    for check_id, result in predicate_results.items():
        if result is None:
            continue
        if not isinstance(result, Mapping):
            raise StateError(f"invalid state: predicate_results[{check_id}] must be an object or null")
        _require_state_fields(
            result,
            {"check_id", "passed", "status", "evidence", "observed", "at", "event_sequence"},
            f"predicate_results[{check_id}]",
        )
        if isinstance(result["event_sequence"], bool) or not isinstance(result["event_sequence"], int) or result["event_sequence"] < 1:
            raise StateError(f"invalid state: predicate_results[{check_id}].event_sequence must be positive")
        if result["check_id"] != check_id or not isinstance(result["passed"], bool):
            raise StateError(f"invalid state: predicate_results[{check_id}] has an invalid check result")
        if not isinstance(result["status"], str) or result["status"] not in CHECK_STATUSES:
            raise StateError(f"invalid state: predicate_results[{check_id}] has an invalid status")
        if (result["status"] == "verified") != result["passed"]:
            raise StateError(f"invalid state: predicate_results[{check_id}] has an invalid passed/status pair")
        _state_evidence(result["evidence"], f"predicate_results[{check_id}].evidence")
        _state_text(result["observed"], f"predicate_results[{check_id}].observed", max_length=1000)
        _state_text(result["at"], f"predicate_results[{check_id}].at")

    for index, decision in enumerate(state["decisions"]):
        if not isinstance(decision, Mapping):
            raise StateError(f"invalid state: decisions[{index}] must be an object")
        _require_state_fields(
            decision,
            {"at", "phase", "decision", "why", "evidence", "result", "event_sequence"},
            f"decisions[{index}]",
        )
        if isinstance(decision["event_sequence"], bool) or not isinstance(decision["event_sequence"], int) or decision["event_sequence"] < 1:
            raise StateError(f"invalid state: decisions[{index}].event_sequence must be positive")
        for field in ("at", "phase", "decision", "why"):
            _state_text(decision[field], f"decisions[{index}].{field}")
        if not isinstance(decision["result"], str) or decision["result"] not in VALID_RESULTS:
            raise StateError(f"invalid state: decisions[{index}] has an invalid result")
        _state_evidence(decision["evidence"], f"decisions[{index}].evidence")
        decision_status = decision.get("status")
        if decision_status is not None and (not isinstance(decision_status, str) or decision_status not in {"blocked", "refused"}):
            raise StateError(f"invalid state: decisions[{index}].status is not supported")
        if ("reason" in decision) != ("next_action" in decision):
            raise StateError(f"invalid state: decisions[{index}] must contain reason and next_action together")
        if "reason" in decision:
            _state_text(decision["reason"], f"decisions[{index}].reason")
            _state_text(decision["next_action"], f"decisions[{index}].next_action")

    checkpoint_by_id: dict[str, Mapping[str, Any]] = {}
    for index, checkpoint in enumerate(state["checkpoints"]):
        if not isinstance(checkpoint, Mapping):
            raise StateError(f"invalid state: checkpoints[{index}] must be an object")
        _require_state_fields(
            checkpoint,
            {"id", "at", "phase", "label", "evidence", "next_action", "event_sequence"},
            f"checkpoints[{index}]",
        )
        if isinstance(checkpoint["event_sequence"], bool) or not isinstance(checkpoint["event_sequence"], int) or checkpoint["event_sequence"] < 1:
            raise StateError(f"invalid state: checkpoints[{index}].event_sequence must be positive")
        for field in ("id", "at", "phase", "label", "next_action"):
            _state_text(checkpoint[field], f"checkpoints[{index}].{field}")
        if checkpoint["phase"] not in phase_ids:
            raise StateError(f"invalid state: checkpoints[{index}].phase is not a declared phase")
        _state_evidence(checkpoint["evidence"], f"checkpoints[{index}].evidence")
        if checkpoint["id"] in checkpoint_by_id:
            raise StateError(f"invalid state: checkpoints[{index}].id is duplicated")
        checkpoint_by_id[checkpoint["id"]] = checkpoint

    for phase_id, checkpoint_refs in phase_checkpoint_refs.items():
        for checkpoint_id in checkpoint_refs:
            checkpoint = checkpoint_by_id.get(checkpoint_id)
            if checkpoint is None or checkpoint["phase"] != phase_id:
                raise StateError(f"invalid state: phases[{phase_id}].checkpoints references an incomplete checkpoint")

    for index, replan in enumerate(state["replans"]):
        if not isinstance(replan, Mapping):
            raise StateError(f"invalid state: replans[{index}] must be an object")
        _require_state_fields(
            replan,
            {"number", "at", "phase", "reason", "evidence", "next_action", "replacement", "event_sequence"},
            f"replans[{index}]",
        )
        if isinstance(replan["event_sequence"], bool) or not isinstance(replan["event_sequence"], int) or replan["event_sequence"] < 1:
            raise StateError(f"invalid state: replans[{index}].event_sequence must be positive")
        if isinstance(replan["number"], bool) or not isinstance(replan["number"], int) or replan["number"] < 1:
            raise StateError(f"invalid state: replans[{index}].number must be a positive integer")
        for field in ("at", "phase", "reason", "next_action"):
            _state_text(replan[field], f"replans[{index}].{field}")
        if replan["number"] != index + 1:
            raise StateError(f"invalid state: replans[{index}].number is not sequential")
        if replan["phase"] not in phase_ids:
            raise StateError(f"invalid state: replans[{index}].phase is not a declared phase")
        _state_evidence(replan["evidence"], f"replans[{index}].evidence")
        replacement = replan["replacement"]
        if not isinstance(replacement, Mapping):
            raise StateError(f"invalid state: replans[{index}].replacement must be an object")
        try:
            replacement_spec = _phase_spec(replacement, index)
        except (PlanError, TypeError, ValueError, KeyError, AttributeError) as exc:
            raise StateError(f"invalid state: replans[{index}].replacement is invalid: {exc}") from exc
        if replacement_spec["id"] != replan["phase"]:
            raise StateError(f"invalid state: replans[{index}].replacement id does not match the phase")

    for index, event in enumerate(state["events"]):
        if not isinstance(event, Mapping):
            raise StateError(f"invalid state: events[{index}] must be an object")
        _require_state_fields(event, {"at", "event", "sequence"}, f"events[{index}]")
        if isinstance(event["sequence"], bool) or not isinstance(event["sequence"], int) or event["sequence"] < 1:
            raise StateError(f"invalid state: events[{index}].sequence must be positive")
        _state_text(event["at"], f"events[{index}].at")
        _state_text(event["event"], f"events[{index}].event")

    def has_event(event_name: str, **expected: Any) -> bool:
        return any(
            event.get("event") == event_name and all(event.get(key) == value for key, value in expected.items())
            for event in state["events"]
        )

    if status == "completed" and not has_event("completed"):
        raise StateError("invalid state: completed runs require a completed event")

    for index, (phase, spec) in enumerate(zip(phases, validated_plan["phases"])):
        if phase["attempt"] > 0 and not has_event("phase_started", phase=phase["id"]):
            raise StateError(f"invalid state: phases[{index}] has attempts without a phase_started event")
        if phase["status"] == "passed" and not has_event(
            "phase_passed",
            phase=phase["id"],
            check_id=spec["verification"]["id"],
        ):
            raise StateError(f"invalid state: phases[{index}].passed has no phase_passed event")
        if phase["status"] == "failed" and not has_event("phase_failed", phase=phase["id"]):
            raise StateError(f"invalid state: phases[{index}].failed has no phase_failed event")
        if phase["status"] == "interrupted" and not phase["checkpoints"]:
            raise StateError(f"invalid state: phases[{index}].interrupted requires a checkpoint")

    for check_id, result in predicate_results.items():
        if result is not None and result["passed"]:
            if not all_phases_passed or current_phase is not None or status not in {"running", "blocked", "completed"}:
                raise StateError(
                    f"invalid state: verified predicate_results[{check_id}] were recorded before all phases passed"
                )
            if not has_event("predicate_recorded", check_id=check_id, passed=True):
                raise StateError(f"invalid state: predicate_results[{check_id}] has no predicate_recorded event")

    blocker = state["blocker"]
    if blocker is not None:
        if not isinstance(blocker, Mapping):
            raise StateError("invalid state: blocker must be an object or null")
        _require_state_fields(blocker, {"at", "phase", "reason", "evidence", "next_action"}, "blocker")
        _state_text(blocker["at"], "blocker.at")
        _state_text(blocker["reason"], "blocker.reason")
        _state_text(blocker["next_action"], "blocker.next_action")
        if blocker["phase"] is not None and blocker["phase"] not in phase_ids:
            raise StateError("invalid state: blocker.phase is not a declared phase")
        _state_evidence(blocker["evidence"], "blocker.evidence")
    if status == "blocked" and blocker is None:
        raise StateError("invalid state: blocked runs require a blocker")
    if status != "blocked" and blocker is not None:
        raise StateError("invalid state: only blocked runs may have a blocker")
    if status == "blocked" and blocker is not None and blocker["phase"] != current_phase:
        raise StateError("invalid state: blocker.phase must match current_phase")

    interruption = state["interruption"]
    if interruption is not None:
        if not isinstance(interruption, Mapping):
            raise StateError("invalid state: interruption must be an object or null")
        _require_state_fields(interruption, {"at", "phase", "checkpoint", "reason"}, "interruption")
        for field in ("at", "phase", "checkpoint", "reason"):
            _state_text(interruption[field], f"interruption.{field}")
        if interruption["phase"] not in phase_ids:
            raise StateError("invalid state: interruption.phase is not a declared phase")
        checkpoint = checkpoint_by_id.get(interruption["checkpoint"])
        if checkpoint is None or checkpoint["phase"] != interruption["phase"]:
            raise StateError("invalid state: interruption.checkpoint is not a checkpoint for the phase")
    if status == "interrupted" and interruption is None:
        raise StateError("invalid state: interrupted runs require interruption details")
    if status == "interrupted" and interruption is not None and interruption["phase"] != current_phase:
        raise StateError("invalid state: interruption.phase must match current_phase")
    if status != "interrupted" and any(phase["status"] == "interrupted" for phase in phases):
        raise StateError("invalid state: interrupted phases require an interrupted run")

    terminal = state["terminal"]
    if status == "completed":
        if not isinstance(terminal, Mapping):
            raise StateError("invalid state: completed runs require terminal details")
        _require_state_fields(terminal, {"at", "predicate", "checks"}, "terminal")
        _state_text(terminal["at"], "terminal.at")
        _state_text(terminal["predicate"], "terminal.predicate")
        if terminal["predicate"] != validated_plan["terminal_predicate"]["description"]:
            raise StateError("invalid state: terminal predicate does not match the plan")
        if not isinstance(terminal["checks"], Mapping) or terminal["checks"] != predicate_results:
            raise StateError("invalid state: terminal checks do not match predicate_results")
        if any(phase["status"] != "passed" for phase in phases):
            raise StateError("invalid state: completed runs require every phase to be passed")
        if any(result is None or result.get("passed") is not True or result.get("status") != "verified" for result in predicate_results.values()):
            raise StateError("invalid state: completed runs require every predicate check to be verified")
    elif terminal is not None:
        raise StateError("invalid state: only completed runs may have terminal details")

    _validate_lifecycle_graph_v2(state, validated_plan, phases, checkpoint_by_id)


def _migrate_schema_1_evidence(value: Any, field: str) -> list[Any]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise StateError(f"invalid state: schema-1 {field} must be a non-empty list")
    if not values:
        raise StateError(f"invalid state: schema-1 {field} must be a non-empty list")

    normalized: list[Any] = []
    for index, item in enumerate(values):
        if isinstance(item, Mapping):
            try:
                normalized.append(_structured_evidence(item, field, index))
            except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
                raise StateError(f"invalid state: schema-1 {field}[{index}] is invalid: {exc}") from exc
            continue
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > MAX_TEXT:
            raise StateError(f"invalid state: schema-1 {field}[{index}] must be a non-empty string")
        text = item.strip()
        if _is_bounded_reference(text):
            normalized.append(text)
            continue
        detail = "".join(
            char for char in text if unicodedata.category(char) not in {"Cc", "Cf"}
        )[:1000].strip()
        normalized.append(
            {
                "kind": "reference",
                "ref": f"legacy-evidence-{index + 1}",
                "detail": detail or "legacy evidence",
            }
        )
    try:
        return _evidence(normalized, field)
    except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise StateError(f"invalid state: schema-1 {field} is invalid: {exc}") from exc


def _migrate_schema_1_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade a schema-1 artifact into the explicitly bound schema-2 form."""

    if not isinstance(state, Mapping) or state.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise StateError("invalid state: schema-1 migration requires a schema-1 artifact")
    migrated = deepcopy(dict(state))
    plan = migrated.get("plan")
    if not isinstance(plan, Mapping):
        raise StateError("invalid state: schema-1 plan must be an object")
    legacy_plan = deepcopy(dict(plan))
    legacy_plan["schema_version"] = SCHEMA_VERSION
    try:
        migrated["plan"] = validate_plan(legacy_plan)
    except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise StateError(f"invalid state: schema-1 plan cannot be migrated: {exc}") from exc
    migrated["schema_version"] = SCHEMA_VERSION

    events = migrated.get("events")
    if not isinstance(events, list):
        raise StateError("invalid state: schema-1 events must be a list")
    phases = migrated.get("phases")
    if not isinstance(phases, list) or not phases:
        raise StateError("invalid state: schema-1 phases must be a non-empty list")
    checkpoints = migrated.get("checkpoints")
    if not isinstance(checkpoints, list):
        raise StateError("invalid state: schema-1 checkpoints must be a list")
    replans = migrated.get("replans")
    if not isinstance(replans, list):
        raise StateError("invalid state: schema-1 replans must be a list")
    legacy_decisions = migrated.get("decisions")
    if not isinstance(legacy_decisions, list):
        raise StateError("invalid state: schema-1 decisions must be a list")
    if any(not isinstance(item, Mapping) for item in legacy_decisions):
        raise StateError("invalid state: schema-1 decision must be an object")
    legacy_decisions = [deepcopy(dict(item)) for item in legacy_decisions]

    phase_by_id: dict[str, dict[str, Any]] = {}
    for index, phase in enumerate(phases):
        if not isinstance(phase, Mapping):
            raise StateError(f"invalid state: schema-1 phases[{index}] must be an object")
        phase_id = phase.get("id")
        if not isinstance(phase_id, str) or not phase_id:
            raise StateError(f"invalid state: schema-1 phases[{index}].id must be a string")
        if phase_id in phase_by_id:
            raise StateError(f"invalid state: schema-1 phases[{index}].id is duplicated")
        if not isinstance(phase.get("checkpoints"), list):
            raise StateError(f"invalid state: schema-1 phases[{index}].checkpoints must be a list")
        phase_by_id[phase_id] = phase  # type: ignore[assignment]
        for nested_field in ("verification_result", "failure"):
            nested = phase.get(nested_field)
            if isinstance(nested, dict) and "evidence" in nested:
                nested["evidence"] = _migrate_schema_1_evidence(
                    nested["evidence"], f"phases[{index}].{nested_field}.evidence"
                )

    checkpoint_by_id: dict[str, dict[str, Any]] = {}
    for index, checkpoint in enumerate(checkpoints):
        if not isinstance(checkpoint, Mapping):
            raise StateError(f"invalid state: schema-1 checkpoints[{index}] must be an object")
        checkpoint_id = checkpoint.get("id")
        checkpoint_phase = checkpoint.get("phase")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise StateError(f"invalid state: schema-1 checkpoints[{index}].id must be a string")
        if checkpoint_id in checkpoint_by_id:
            raise StateError(f"invalid state: schema-1 checkpoints[{index}].id is duplicated")
        if not isinstance(checkpoint_phase, str) or checkpoint_phase not in phase_by_id:
            raise StateError(f"invalid state: schema-1 checkpoints[{index}].phase is invalid")
        if "evidence" in checkpoint:
            checkpoint["evidence"] = _migrate_schema_1_evidence(
                checkpoint["evidence"], f"checkpoints[{index}].evidence"
            )
        checkpoint_by_id[checkpoint_id] = checkpoint  # type: ignore[assignment]

    replan_by_number: dict[int, dict[str, Any]] = {}
    for index, replan in enumerate(replans):
        if not isinstance(replan, Mapping):
            raise StateError(f"invalid state: schema-1 replans[{index}] must be an object")
        number = replan.get("number")
        phase_id = replan.get("phase")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise StateError(f"invalid state: schema-1 replans[{index}].number must be positive")
        if number in replan_by_number:
            raise StateError(f"invalid state: schema-1 replans[{index}].number is duplicated")
        if not isinstance(phase_id, str) or phase_id not in phase_by_id:
            raise StateError(f"invalid state: schema-1 replans[{index}].phase is invalid")
        if "evidence" in replan:
            replan["evidence"] = _migrate_schema_1_evidence(
                replan["evidence"], f"replans[{index}].evidence"
            )
        replan_by_number[number] = replan  # type: ignore[assignment]

    predicate_results = migrated.get("predicate_results")
    if not isinstance(predicate_results, Mapping):
        raise StateError("invalid state: schema-1 predicate_results must be an object")
    migrated["predicate_results"] = deepcopy(dict(predicate_results))
    for check_id, result in migrated["predicate_results"].items():
        if not isinstance(check_id, str):
            raise StateError("invalid state: schema-1 predicate result ids must be strings")
        if result is None:
            continue
        if not isinstance(result, Mapping):
            raise StateError(f"invalid state: schema-1 predicate_results[{check_id}] must be an object")
        result = deepcopy(dict(result))
        if "evidence" in result:
            result["evidence"] = _migrate_schema_1_evidence(
                result["evidence"], f"predicate_results[{check_id}].evidence"
            )
        if "status" not in result and isinstance(result.get("passed"), bool):
            result["status"] = "verified" if result["passed"] else "failed"
        migrated["predicate_results"][check_id] = result

    for index, decision in enumerate(legacy_decisions):
        if "evidence" in decision:
            decision["evidence"] = _migrate_schema_1_evidence(
                decision["evidence"], f"decisions[{index}].evidence"
            )

    blocker = migrated.get("blocker")
    if blocker is not None and not isinstance(blocker, Mapping):
        raise StateError("invalid state: schema-1 blocker must be an object or null")
    if isinstance(blocker, dict) and "evidence" in blocker:
        blocker["evidence"] = _migrate_schema_1_evidence(blocker["evidence"], "blocker.evidence")
    terminal = migrated.get("terminal")
    if terminal is not None and not isinstance(terminal, Mapping):
        raise StateError("invalid state: schema-1 terminal must be an object or null")
    if isinstance(terminal, dict) and isinstance(terminal.get("checks"), Mapping):
        terminal["checks"] = deepcopy(dict(terminal["checks"]))
        for check_id, result in terminal["checks"].items():
            if isinstance(result, dict) and "evidence" in result:
                result["evidence"] = _migrate_schema_1_evidence(
                    result["evidence"], f"terminal.checks[{check_id}].evidence"
                )
            if isinstance(result, dict) and "status" not in result and isinstance(result.get("passed"), bool):
                result["status"] = "verified" if result["passed"] else "failed"

    # Schema 1 could omit reverse phase checkpoint references after a replan.
    # Restore those references from the authoritative global rows before the
    # stricter schema-2 graph validator runs.
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, Mapping):
            continue
        phase = phase_by_id.get(checkpoint.get("phase"))
        if isinstance(phase, dict) and checkpoint.get("id") not in phase.setdefault("checkpoints", []):
            phase["checkpoints"].append(checkpoint["id"])

    for index, replan in enumerate(replans):
        replacement = replan.get("replacement")
        if not isinstance(replacement, Mapping):
            raise StateError(f"invalid state: schema-1 replans[{index}].replacement must be an object")
        try:
            replacement_spec = _phase_spec(replacement, index)
        except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
            phase = phase_by_id[replan["phase"]]
            if set(replacement) != {"id", "method"} or replacement.get("id") != phase.get("verification", {}).get("id"):
                raise StateError(f"invalid state: schema-1 replans[{index}].replacement is invalid: {exc}") from exc
            try:
                replacement_spec = _phase_spec(phase, index)
            except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as phase_exc:
                raise StateError(f"invalid state: schema-1 replans[{index}].replacement is invalid: {phase_exc}") from phase_exc
        if replacement_spec["id"] != replan["phase"]:
            raise StateError(f"invalid state: schema-1 replans[{index}].replacement id does not match the phase")
        replan["replacement"] = replacement_spec

    def make_decision(
        phase: str,
        decision: str,
        why: str,
        evidence: Any,
        result: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        evidence = _migrate_schema_1_evidence(evidence, "decision.evidence")
        record: dict[str, Any] = {
            "at": _now(),
            "phase": phase,
            "decision": decision,
            "why": why,
            "evidence": deepcopy(evidence),
            "result": result,
        }
        if status is not None:
            record["status"] = status
        if reason is not None:
            record["reason"] = reason
        if next_action is not None:
            record["next_action"] = next_action
        return record

    def matching_decision(event: Mapping[str, Any], used: set[int]) -> tuple[dict[str, Any] | None, int | None]:
        event_name = event.get("event")
        for index, decision in enumerate(legacy_decisions):
            if index in used:
                continue
            phase = event.get("phase") or "run"
            if event_name == "phase_passed" and decision.get("phase") == phase and decision.get("decision") == f"passed phase {phase}":
                return decision, index
            if event_name == "phase_failed" and decision.get("phase") == phase and decision.get("decision") == f"failed: {event.get('reason')}":
                return decision, index
            if event_name == "replanned" and decision.get("phase") == phase and decision.get("decision") == "replan phase":
                return decision, index
            if event_name == "predicate_recorded" and decision.get("phase") == "run" and decision.get("decision") == f"terminal check {event.get('check_id')}":
                return decision, index
            if event_name == "blocked" and decision.get("decision") == f"blocked: {event.get('reason')}" and decision.get("result") == "blocked":
                blocker_decision = deepcopy(decision)
                blocker_decision["status"] = "blocked"
                blocker_decision["reason"] = event.get("reason", "legacy blocker")
                blocker_decision["next_action"] = event.get("next_action", "resolve the recorded blocker")
                return blocker_decision, index
            if event_name == "blocked" and decision.get("status") == "blocked" and decision.get("phase") == phase:
                return decision, index
            if event_name == "action_refused":
                action = event.get("action", "invalid-action")
                action_reference = action if isinstance(action, str) and SAFE_ACTION.fullmatch(action) else "invalid-action"
                if decision.get("decision") == f"refused action {action_reference}" and decision.get("result") == "blocked":
                    refusal = deepcopy(decision)
                    refusal["status"] = "refused"
                    refusal["reason"] = event.get("reason", "legacy refusal")
                    refusal["next_action"] = event.get(
                        "next_action", "choose a declared local action or replan with owner approval"
                    )
                    return refusal, index
            if event_name == "action_refused" and decision.get("status") == "refused" and decision.get("phase") == phase:
                return decision, index
            if event_name == "action_refused" and decision.get("status") == "refused":
                action = event.get("action", "invalid-action")
                action_reference = action if isinstance(action, str) and SAFE_ACTION.fullmatch(action) else "invalid-action"
                if decision.get("decision") == f"refused action {action_reference}":
                    return decision, index
            if event_name == "decision_recorded" and decision.get("phase") == event.get("phase", "run"):
                return decision, index
        return None, None

    def event_decision(event: dict[str, Any], used: set[int]) -> tuple[dict[str, Any], bool]:
        event_name = event.get("event")
        if isinstance(event.get("decision"), Mapping):
            record = deepcopy(dict(event["decision"]))
            if "evidence" in record:
                record["evidence"] = _migrate_schema_1_evidence(record["evidence"], "event.decision.evidence")
            return record, False
        record, index = matching_decision(event, used)
        if record is not None and index is not None:
            used.add(index)
            return deepcopy(record), True
        phase = event.get("phase") or "run"
        if event_name == "phase_passed":
            snapshot = event.get("verification_snapshot") or {}
            return make_decision(phase, f"passed phase {phase}", "the phase output and declared verification both passed", snapshot.get("evidence", ["check:legacy-phase-pass"]), "verified"), True
        if event_name == "phase_failed":
            snapshot = event.get("failure_snapshot") or {}
            reason = event.get("reason") or snapshot.get("reason") or "legacy phase failure"
            return make_decision(phase, f"failed: {reason}", "the declared phase check did not pass", snapshot.get("evidence", ["check:legacy-phase-failure"]), "failed"), True
        if event_name == "replanned":
            replan = replan_by_number.get(event.get("number"), {})
            return make_decision(phase, "replan phase", replan.get("reason", "legacy replan"), replan.get("evidence", ["check:legacy-replan"]), "open"), True
        if event_name == "blocked":
            snapshot = event.get("blocker_snapshot") or migrated.get("blocker") or {}
            reason = event.get("reason") or snapshot.get("reason") or "legacy blocker"
            next_action = event.get("next_action") or snapshot.get("next_action") or "resolve the recorded blocker"
            return make_decision(phase, f"blocked: {reason}", "the run cannot proceed within its current constraints", snapshot.get("evidence", ["blocker:legacy"]), "blocked", status="blocked", reason=reason, next_action=next_action), True
        if event_name == "predicate_recorded":
            snapshot = event.get("result_snapshot") or migrated.get("predicate_results", {}).get(event.get("check_id")) or {}
            return make_decision("run", f"terminal check {event.get('check_id')}", "the terminal predicate check was recorded", snapshot.get("evidence", ["check:legacy-predicate"]), snapshot.get("status", "failed")), True
        if event_name == "action_refused":
            action = event.get("action", "invalid-action")
            action_reference = action if isinstance(action, str) and SAFE_ACTION.fullmatch(action) else "invalid-action"
            evidence = [
                {"kind": "reference", "ref": f"scope:{action_reference}"},
                {"kind": "reference", "ref": "target:provided" if event.get("target") is not None else "target:none"},
            ]
            return make_decision(phase, f"refused action {action_reference}", event.get("reason", "legacy refusal"), evidence, "blocked", status="refused", reason=event.get("reason", "legacy refusal"), next_action=event.get("next_action", "choose a declared local action or replan with owner approval")), False
        raise StateError(f"invalid state: schema-1 has an unsupported decision event {event_name!r}")

    used_legacy_decisions: set[int] = set()
    normalized_events: list[dict[str, Any]] = []
    last_interruption_index = max(
        (
            index
            for index, original in enumerate(events)
            if isinstance(original, Mapping) and original.get("event") == "interrupted"
        ),
        default=None,
    )
    for event_index, original in enumerate(events):
        if not isinstance(original, Mapping):
            raise StateError("invalid state: schema-1 event must be an object")
        event = deepcopy(dict(original))
        event_name = event.get("event")
        if event_name is None and event.get("status") == "refused" and "action" in event:
            event_name = "action_refused"
            event["event"] = event_name
        if not isinstance(event_name, str) or event_name not in LIFECYCLE_EVENTS | AUDIT_EVENTS | DECISION_EVENTS:
            raise StateError(f"invalid state: schema-1 event has an unsupported event {event_name!r}")
        if "decision" in event and not isinstance(event["decision"], Mapping):
            raise StateError("invalid state: schema-1 event decision must be an object")
        for snapshot_field in (
            "checkpoint_snapshot",
            "verification_snapshot",
            "failure_snapshot",
            "interruption_snapshot",
            "blocker_snapshot",
            "result_snapshot",
            "terminal_snapshot",
        ):
            snapshot = event.get(snapshot_field)
            if snapshot is not None and not isinstance(snapshot, Mapping):
                raise StateError(f"invalid state: schema-1 {snapshot_field} must be an object or null")
            if isinstance(snapshot, dict) and "evidence" in snapshot:
                snapshot["evidence"] = _migrate_schema_1_evidence(
                    snapshot["evidence"], f"event.{snapshot_field}.evidence"
                )
            if snapshot_field == "terminal_snapshot" and isinstance(snapshot, dict) and isinstance(snapshot.get("checks"), Mapping):
                snapshot["checks"] = deepcopy(dict(snapshot["checks"]))
                for check_id, result in snapshot["checks"].items():
                    if isinstance(result, dict) and "evidence" in result:
                        result["evidence"] = _migrate_schema_1_evidence(
                            result["evidence"], f"event.terminal_snapshot.checks[{check_id}].evidence"
                        )
        if event_name == "action_refused":
            event["target"] = _sanitize_refusal_target(event.get("target"))
        if event_name == "checkpoint":
            checkpoint_id = event.get("checkpoint")
            if not isinstance(checkpoint_id, str):
                raise StateError("invalid state: schema-1 checkpoint event has an invalid checkpoint id")
            checkpoint = checkpoint_by_id.get(checkpoint_id)
            if checkpoint is None:
                raise StateError("invalid state: schema-1 checkpoint event has no checkpoint row")
            event.setdefault("checkpoint_snapshot", deepcopy(checkpoint))
        elif event_name == "phase_passed":
            phase_id = event.get("phase")
            if not isinstance(phase_id, str) or phase_id not in phase_by_id:
                raise StateError("invalid state: schema-1 phase_passed event has an invalid phase")
            phase = phase_by_id[phase_id]
            event.setdefault("output_snapshot", deepcopy(phase.get("output_value")))
            event.setdefault("verification_snapshot", deepcopy(phase.get("verification_result")))
        elif event_name == "phase_failed":
            phase_id = event.get("phase")
            if not isinstance(phase_id, str) or phase_id not in phase_by_id:
                raise StateError("invalid state: schema-1 phase_failed event has an invalid phase")
            phase = phase_by_id[phase_id]
            if event.get("failure_snapshot") is None:
                failure = phase.get("failure")
                if failure is None:
                    legacy_failure = next(
                        (
                            decision
                            for decision in legacy_decisions
                            if decision.get("phase") == phase_id
                            and decision.get("decision") == f"failed: {event.get('reason')}"
                        ),
                        {},
                    )
                    event["failure_snapshot"] = {
                        "reason": event.get("reason", "legacy phase failure"),
                        "evidence": deepcopy(legacy_failure.get("evidence", ["check:legacy-phase-failure"])),
                        "next_action": legacy_failure.get("next_action", f"replan phase {phase_id}"),
                        "at": event.get("at", _now()),
                    }
                else:
                    event["failure_snapshot"] = deepcopy(failure)
        elif event_name == "replanned":
            number = event.get("number")
            if isinstance(number, bool) or not isinstance(number, int):
                raise StateError("invalid state: schema-1 replan event has an invalid number")
            replan = replan_by_number.get(number)
            if replan is None:
                raise StateError("invalid state: schema-1 replan event has no replan row")
            event.setdefault("replacement", deepcopy(replan.get("replacement")))
            event.setdefault("replan_snapshot", deepcopy(replan))
        elif event_name == "interrupted":
            interruption = migrated.get("interruption")
            if not isinstance(interruption, Mapping):
                raise StateError("invalid state: schema-1 interruption must be an object")
            phase_id = event.get("phase")
            checkpoint_id = event.get("checkpoint")
            if not isinstance(phase_id, str) or phase_id not in phase_by_id:
                raise StateError("invalid state: schema-1 interruption event has an invalid phase")
            if not isinstance(checkpoint_id, str):
                raise StateError("invalid state: schema-1 interruption event has an invalid checkpoint")
            checkpoint = checkpoint_by_id.get(checkpoint_id)
            if checkpoint is None or checkpoint.get("phase") != phase_id:
                raise StateError("invalid state: schema-1 interruption event has an invalid checkpoint")
            try:
                event_reason = _text(
                    event.get("reason", interruption.get("reason")),
                    "schema-1 interruption.reason",
                    max_length=1000,
                )
                event_at = _text(event.get("at"), "schema-1 interruption.at")
            except PlanError as exc:
                raise StateError(f"invalid state: schema-1 interruption event is invalid: {exc}") from exc
            event["reason"] = event_reason
            if "interruption_snapshot" not in event:
                snapshot = deepcopy(interruption) if event_index == last_interruption_index else {}
                snapshot["at"] = snapshot.get("at", event_at)
                snapshot["phase"] = phase_id
                snapshot["checkpoint"] = checkpoint_id
                snapshot["reason"] = event_reason
                event["interruption_snapshot"] = snapshot
        elif event_name == "blocked":
            blocker = migrated.get("blocker")
            if not isinstance(blocker, Mapping):
                raise StateError("invalid state: schema-1 blocker must be an object")
            event.setdefault("next_action", blocker.get("next_action", "resolve the recorded blocker"))
            event.setdefault("blocker_snapshot", deepcopy(blocker))
        elif event_name == "predicate_recorded":
            check_id = event.get("check_id")
            if not isinstance(check_id, str):
                raise StateError("invalid state: schema-1 predicate event has an invalid check id")
            result = migrated["predicate_results"].get(check_id)
            if isinstance(result, Mapping):
                event.setdefault("result_snapshot", deepcopy(result))
        elif event_name == "completed":
            event.setdefault("terminal_snapshot", deepcopy(migrated.get("terminal")))

        if event_name in DECISION_EVENTS:
            record, listed = event_decision(event, used_legacy_decisions)
            if event_name == "action_refused" and "phase" not in event:
                event["phase"] = record.get("phase", "run")
            event["__migration_decision"] = record
            event["__migration_listed"] = listed
        normalized_events.append(event)

    completed_index = next((index for index, event in enumerate(normalized_events) if event.get("event") == "completed"), len(normalized_events))
    for index, decision in enumerate(legacy_decisions):
        if index in used_legacy_decisions:
            continue
        event = {
            "at": decision.get("at", _now()),
            "event": "decision_recorded",
            "phase": decision.get("phase", "run"),
            "__migration_decision": deepcopy(decision),
            "__migration_listed": True,
        }
        normalized_events.insert(completed_index, event)
        completed_index += 1

    listed_records = [
        event["__migration_decision"]
        for event in normalized_events
        if event.get("event") in DECISION_EVENTS and event.get("__migration_listed")
    ]
    limit = migrated["plan"]["limits"]["max_decisions"]
    if len(listed_records) > limit + 1:
        raise StateError("invalid state: schema-1 decision history cannot fit the bounded schema-2 limit")
    for index, record in enumerate(listed_records):
        if index >= limit and record.get("status") not in {"blocked", "refused"}:
            raise StateError("invalid state: schema-1 decision overflow is not a bounded refusal or blocker")

    migrated["decisions"] = []
    latest_predicate_events: dict[str, dict[str, Any]] = {}
    migrated_decision_count = 0
    for sequence, event in enumerate(normalized_events, start=1):
        event["sequence"] = sequence
        if event.get("event") in DECISION_EVENTS:
            listed = bool(event.pop("__migration_listed", False))
            record = deepcopy(event.pop("__migration_decision"))
            record.pop("event_sequence", None)
            record["event_sequence"] = sequence
            if listed:
                decision_index = migrated_decision_count
                migrated["decisions"].append(record)
                decision_overflow = decision_index >= limit
                if decision_overflow and record.get("status") not in {"blocked", "refused"}:
                    raise StateError("invalid state: schema-1 decision overflow is not a bounded refusal or blocker")
                migrated_decision_count += 1
            else:
                if migrated_decision_count <= limit:
                    raise StateError("invalid state: schema-1 decision overflow appears before capacity pressure")
                if record.get("status") not in {"blocked", "refused"}:
                    raise StateError("invalid state: schema-1 decision overflow is not a bounded refusal or blocker")
                decision_overflow = True
            event["decision"] = deepcopy(record)
            event["decision_in_list"] = listed
            event["decision_overflow"] = decision_overflow
        if event.get("event") == "checkpoint":
            checkpoint = checkpoint_by_id[event["checkpoint"]]
            checkpoint["event_sequence"] = sequence
            event["checkpoint_snapshot"] = deepcopy(checkpoint)
        if event.get("event") == "replanned":
            replan = replan_by_number[event["number"]]
            replan["event_sequence"] = sequence
            event["replan_snapshot"] = deepcopy(replan)
        if event.get("event") == "predicate_recorded":
            snapshot = event.get("result_snapshot")
            if isinstance(snapshot, dict):
                snapshot["event_sequence"] = sequence
                latest_predicate_events[event.get("check_id")] = snapshot

    for check_id, result in migrated.get("predicate_results", {}).items():
        if isinstance(result, dict) and check_id in latest_predicate_events:
            result["event_sequence"] = latest_predicate_events[check_id]["event_sequence"]
    if isinstance(terminal, dict) and isinstance(terminal.get("checks"), Mapping):
        terminal["checks"] = deepcopy(migrated["predicate_results"])
        for event in normalized_events:
            if event.get("event") == "completed":
                event["terminal_snapshot"] = deepcopy(terminal)
    migrated["events"] = normalized_events
    return migrated


class Workflow:
    """A finite state machine backed by one local JSON state file."""

    def __init__(self, state_path: Path, workspace_root: Path, state: dict[str, Any]):
        _require_state_object(state)
        self.state_path = state_path
        self.workspace_root = workspace_root
        self.state = state
        self._validate_state_limits()

    @classmethod
    def create(
        cls,
        state_path: str | os.PathLike[str],
        plan: Mapping[str, Any],
        *,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> "Workflow":
        contract = validate_plan(plan)
        path, root = _state_path(state_path, workspace_root, require_regular=True)
        try:
            existing_mode = path.lstat().st_mode
        except FileNotFoundError:
            existing_mode = None
        except OSError as exc:
            raise StateError(f"state path could not be inspected: {path}") from exc
        if existing_mode is not None:
            if not stat.S_ISREG(existing_mode):
                raise StateError(f"state path must be a regular file: {path}")
            raise StateError(f"state file already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        phases = []
        for spec in contract["phases"]:
            phases.append(
                {
                    **deepcopy(spec),
                    "status": "pending",
                    "attempt": 0,
                    "output_value": None,
                    "verification_result": None,
                    "failure": None,
                    "checkpoints": [],
                }
            )
        run_id = plan.get("run_id")
        if run_id is not None:
            run_id = _text(run_id, "run_id", max_length=100)
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id or f"run-{uuid.uuid4().hex[:12]}",
            "created_at": _now(),
            "updated_at": _now(),
            "status": "planned",
            "plan": contract,
            "phases": phases,
            "current_phase": phases[0]["id"],
            "checkpoints": [],
            "decisions": [],
            "replans": [],
            "predicate_results": {check_id: None for check_id in contract["terminal_predicate"]["checks"]},
            "resume_count": 0,
            "next_action": f"start phase {phases[0]['id']}",
            "terminal": None,
            "blocker": None,
            "interruption": None,
            "events": [],
        }
        workflow = cls(path, root, state)
        workflow._persist()
        return workflow

    @classmethod
    def load(
        cls,
        state_path: str | os.PathLike[str],
        *,
        workspace_root: str | os.PathLike[str] | None = None,
    ) -> "Workflow":
        path, root = _state_path(state_path, workspace_root, require_regular=True)
        try:
            state_mode = path.lstat().st_mode
        except FileNotFoundError as exc:
            raise StateError(f"state file does not exist: {path}") from exc
        except OSError as exc:
            raise StateError(f"state file could not be inspected: {path}") from exc
        if not stat.S_ISREG(state_mode):
            raise StateError(f"state file must be a regular file: {path}")
        try:
            with path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except json.JSONDecodeError as exc:
            raise StateError(f"state file is not valid JSON: {path}") from exc
        except (OSError, UnicodeError) as exc:
            raise StateError(f"state file could not be read: {path}") from exc
        if not isinstance(state, dict):
            raise StateError("state root must be an object")
        if state.get("schema_version") == LEGACY_SCHEMA_VERSION:
            try:
                state = _migrate_schema_1_state(state)
            except (WorkflowError, TypeError, ValueError, KeyError, AttributeError) as exc:
                raise StateError(f"invalid state: schema-1 migration failed: {exc}") from exc
            workflow = cls(path, root, state)
            workflow._persist()
            return workflow
        if state.get("schema_version") != SCHEMA_VERSION:
            raise StateError("invalid state: unsupported state schema")
        return cls(path, root, state)

    def _persist(self) -> None:
        self.state["updated_at"] = _now()
        _require_state_object(self.state)
        self._validate_state_limits()
        try:
            state_mode = self.state_path.lstat().st_mode
        except FileNotFoundError:
            state_mode = None
        except OSError as exc:
            raise StateError(f"state path could not be inspected: {self.state_path}") from exc
        if state_mode is not None and not stat.S_ISREG(state_mode):
            raise StateError(f"state path must be a regular file: {self.state_path}")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.state_path.name}.", suffix=".tmp", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _transaction(self, mutate: Any) -> Any:
        """Apply one transition to a candidate and publish it exactly once."""

        previous = self.state
        self.state = deepcopy(previous)
        try:
            result = mutate()
            self._persist()
        except Exception:
            self.state = previous
            raise
        return result

    def _validate_state_limits(self) -> None:
        limits = self.state["plan"]["limits"]
        if len(self.state["checkpoints"]) > limits["max_checkpoints"]:
            raise StateError("checkpoint limit exceeded")
        decision_limit = limits["max_decisions"]
        last_decision = self.state["decisions"][-1] if self.state["decisions"] else None
        decision_overflow = (
            len(self.state["decisions"]) == decision_limit + 1
            and last_decision is not None
            and last_decision.get("result") == "blocked"
            and (
                (self.state.get("status") == "blocked" and last_decision.get("status") == "blocked")
                or last_decision.get("status") == "refused"
            )
        )
        if len(self.state["decisions"]) > decision_limit and not decision_overflow:
            raise StateError("decision limit exceeded")
        if len(self.state["replans"]) > limits["max_replans"]:
            raise StateError("replan limit exceeded")

    def _phase(self, phase_id: str | None = None) -> dict[str, Any]:
        wanted = phase_id or self.state.get("current_phase")
        for phase in self.state["phases"]:
            if phase["id"] == wanted:
                return phase
        raise StateError(f"unknown phase: {wanted}")

    def _event(self, event: str, **details: Any) -> dict[str, Any]:
        sequence = len(self.state["events"]) + 1
        value = {"sequence": sequence, "at": _now(), "event": event, **details}
        self.state["events"].append(value)
        return value

    def _require_current(self, status: str) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] == "completed":
            raise StateError("completed workflow is terminal and immutable")
        phase = self._phase()
        if phase["status"] != status:
            raise StateError(f"phase {phase['id']} is {phase['status']}; expected {status}")
        return phase

    @staticmethod
    def _decision_record(
        phase: str,
        decision: str,
        why: str,
        evidence: list[Any],
        result: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        next_action: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "at": _now(),
            "phase": phase,
            "decision": decision,
            "why": why,
            "evidence": deepcopy(evidence),
            "result": result,
        }
        if status is not None:
            record["status"] = status
        if reason is not None:
            record["reason"] = reason
        if next_action is not None:
            record["next_action"] = next_action
        return record

    def _append_decision_event(self, event: str, record: dict[str, Any], **details: Any) -> dict[str, Any]:
        limit = self.state["plan"]["limits"]["max_decisions"]
        sequence = len(self.state["events"]) + 1
        persisted_record = deepcopy(record)
        persisted_record["event_sequence"] = sequence
        if len(self.state["decisions"]) < limit:
            decision_in_list = True
            decision_overflow = False
        elif len(self.state["decisions"]) == limit:
            if persisted_record.get("status") not in {"blocked", "refused"}:
                raise StateError("decision limit reached")
            decision_in_list = True
            decision_overflow = True
        else:
            if persisted_record.get("status") not in {"blocked", "refused"}:
                raise StateError("decision limit reached")
            decision_in_list = False
            decision_overflow = True
        value = self._event(
            event,
            **details,
            decision=persisted_record,
            decision_in_list=decision_in_list,
            decision_overflow=decision_overflow,
        )
        if decision_in_list:
            self.state["decisions"].append(persisted_record)
        return persisted_record

    def _block_and_persist(self, reason: str, evidence: Sequence[Any] | str, next_action: str) -> None:
        reason = _text(reason, "blocker.reason", max_length=1000)
        evidence_values = _evidence(evidence, "blocker.evidence")
        next_action = _text(next_action, "blocker.next_action", max_length=1000)

        def mutate() -> None:
            self._set_blocked(reason, evidence_values, next_action)
            phase = self.state.get("current_phase") or "run"
            self._append_decision_event(
                "blocked",
                self._decision_record(
                    phase,
                    f"blocked: {reason}",
                    "the run cannot proceed within its current constraints",
                    evidence_values,
                    "blocked",
                    status="blocked",
                    reason=reason,
                    next_action=next_action,
                ),
                phase=self.state.get("current_phase"),
                reason=reason,
                next_action=next_action,
                blocker_snapshot=deepcopy(self.state["blocker"]),
            )

        self._transaction(mutate)

    def start_phase(self) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] == "failed":
            raise ReplanRequired(f"phase {self.state['current_phase']} failed; replan before retry")
        if self.state["status"] == "interrupted":
            raise StateError("resume the interrupted run before starting a phase")
        if self.state["status"] == "blocked":
            raise StateError("blocked run needs its recorded next action before continuing")
        if self.state["status"] == "completed":
            raise StateError("run is already completed")
        phase = self._require_current("pending")
        limit = self.state["plan"]["limits"]["max_attempts_per_phase"]
        if phase["attempt"] >= limit:
            self._block_and_persist(
                "phase attempt limit reached",
                [
                    {
                        "kind": "reference",
                        "ref": f"phase:{phase['id']}",
                        "detail": f"attempts exhausted at {phase['attempt']}",
                    }
                ],
                f"replan phase {phase['id']} with an explicit new approach",
            )
            raise StateError("phase attempt limit reached; run is blocked")
        phase_id = phase["id"]

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            candidate_phase["attempt"] += 1
            candidate_phase["status"] = "in_progress"
            self.state["status"] = "running"
            self.state["next_action"] = f"produce and verify phase {phase_id} output"
            self.state["blocker"] = None
            self._event("phase_started", phase=phase_id, attempt=candidate_phase["attempt"])

        self._transaction(mutate)
        return deepcopy(self.state)

    def checkpoint(self, label: str, evidence: Sequence[Any] | str, next_action: str) -> dict[str, Any]:
        _require_state_object(self.state)
        phase = self._require_current("in_progress")
        label = _text(label, "checkpoint.label", max_length=200)
        evidence_values = _evidence(evidence, "checkpoint.evidence")
        next_action = _text(next_action, "checkpoint.next_action", max_length=1000)
        limit = self.state["plan"]["limits"]["max_checkpoints"]
        if len(self.state["checkpoints"]) >= limit:
            self._block_and_persist(
                "checkpoint limit reached",
                evidence_values,
                "replan with a justified checkpoint budget or finish the current phase",
            )
            raise StateError("checkpoint limit reached; run is blocked")
        phase_id = phase["id"]
        checkpoint_holder: dict[str, Any] = {}

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            checkpoint = {
                "id": f"cp-{len(self.state['checkpoints']) + 1}",
                "at": _now(),
                "phase": phase_id,
                "label": label,
                "evidence": deepcopy(evidence_values),
                "next_action": next_action,
                "event_sequence": len(self.state["events"]) + 1,
            }
            self.state["checkpoints"].append(checkpoint)
            candidate_phase["checkpoints"].append(checkpoint["id"])
            self.state["next_action"] = next_action
            self._event(
                "checkpoint",
                checkpoint=checkpoint["id"],
                phase=phase_id,
                checkpoint_snapshot=deepcopy(checkpoint),
            )
            checkpoint_holder.update(checkpoint)

        self._transaction(mutate)
        return deepcopy(checkpoint_holder)

    def complete_phase(self, output: Any, verification: Mapping[str, Any]) -> dict[str, Any]:
        _require_state_object(self.state)
        phase = self._require_current("in_progress")
        if output is None or output == "" or output == {} or output == []:
            raise StateError("phase output must be present")
        output = _copy_json(output)
        if not isinstance(verification, Mapping):
            raise StateError("verification must be an object")
        check_id = _text(verification.get("check_id"), "verification.check_id", max_length=100)
        expected_id = phase["verification"]["id"]
        if check_id != expected_id:
            raise StateError(f"verification must use declared check {expected_id}")
        if verification.get("passed") is not True:
            raise StateError("phase verification must explicitly pass")
        evidence_values = _evidence(verification.get("evidence"), "verification.evidence")
        if len(self.state["decisions"]) >= self.state["plan"]["limits"]["max_decisions"]:
            self._block_and_persist(
                "decision limit reached",
                [{"kind": "reference", "ref": f"decisions:{len(self.state['decisions'])}"}],
                "request a new bounded plan from the owner",
            )
            raise StateError("decision limit reached; run is blocked")
        phase_id = phase["id"]

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            candidate_phase["output_value"] = deepcopy(output)
            candidate_phase["verification_result"] = {
                "check_id": check_id,
                "passed": True,
                "evidence": deepcopy(evidence_values),
                "observed": _text(verification.get("observed", "passed"), "verification.observed", max_length=1000),
            }
            candidate_phase["status"] = "passed"
            candidate_phase["failure"] = None
            next_phase = self._next_pending()
            if next_phase is None:
                self.state["current_phase"] = None
                self.state["next_action"] = "record terminal predicate checks"
            else:
                self.state["current_phase"] = next_phase["id"]
                self.state["next_action"] = f"inspect output, then start phase {next_phase['id']}"
            decision = self._decision_record(
                phase_id,
                f"passed phase {phase_id}",
                "the phase output and declared verification both passed",
                evidence_values,
                "verified",
            )
            self._append_decision_event(
                "phase_passed",
                decision,
                phase=phase_id,
                check_id=check_id,
                output_snapshot=deepcopy(candidate_phase["output_value"]),
                verification_snapshot=deepcopy(candidate_phase["verification_result"]),
            )

        self._transaction(mutate)
        return deepcopy(self.state)

    def _next_pending(self) -> dict[str, Any] | None:
        for phase in self.state["phases"]:
            if phase["status"] == "pending":
                return phase
        return None

    def fail_phase(self, reason: str, evidence: Sequence[Any] | str, next_action: str) -> dict[str, Any]:
        _require_state_object(self.state)
        phase = self._require_current("in_progress")
        reason = _text(reason, "failure.reason", max_length=1000)
        evidence_values = _evidence(evidence, "failure.evidence")
        next_action = _text(next_action, "failure.next_action", max_length=1000)
        if len(self.state["decisions"]) >= self.state["plan"]["limits"]["max_decisions"]:
            self._block_and_persist(
                "decision limit reached",
                [{"kind": "reference", "ref": f"decisions:{len(self.state['decisions'])}"}],
                "request a new bounded plan from the owner",
            )
            raise StateError("decision limit reached; run is blocked")
        phase_id = phase["id"]

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            candidate_phase["status"] = "failed"
            candidate_phase["failure"] = {
                "reason": reason,
                "evidence": deepcopy(evidence_values),
                "next_action": next_action,
                "at": _now(),
            }
            self.state["status"] = "failed"
            self.state["next_action"] = f"replan phase {phase_id}: {next_action}"
            decision = self._decision_record(
                phase_id,
                f"failed: {reason}",
                "the declared phase check did not pass",
                evidence_values,
                "failed",
            )
            self._append_decision_event(
                "phase_failed",
                decision,
                phase=phase_id,
                reason=reason,
                failure_snapshot=deepcopy(candidate_phase["failure"]),
            )

        self._transaction(mutate)
        return deepcopy(self.state)

    def replan(
        self,
        reason: str,
        evidence: Sequence[Any] | str,
        next_action: str,
        replacement: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "failed":
            raise StateError("replan requires a failed phase")
        reason = _text(reason, "replan.reason", max_length=1000)
        evidence_values = _evidence(evidence, "replan.evidence")
        next_action = _text(next_action, "replan.next_action", max_length=1000)
        limit = self.state["plan"]["limits"]["max_replans"]
        if len(self.state["replans"]) >= limit:
            self._block_and_persist("replan limit reached", evidence_values, "request a new bounded plan from the owner")
            raise StateError("replan limit reached; run is blocked")
        phase = self._phase()
        if phase["status"] != "failed":
            raise StateError("current phase is not failed")
        replacement_spec = None
        if replacement is not None:
            replacement_spec = _phase_spec(replacement, 0)
            if replacement_spec["id"] != phase["id"]:
                raise PlanError("replacement phase must keep the failed phase id")
        candidate_phases = deepcopy(self.state["plan"]["phases"])
        failed_phase_index = next(index for index, item in enumerate(candidate_phases) if item["id"] == phase["id"])
        if replacement_spec is not None:
            candidate_phases[failed_phase_index] = replacement_spec
        candidate_plan = deepcopy(self.state["plan"])
        candidate_plan["phases"] = candidate_phases
        validated_plan = validate_plan(candidate_plan)
        if len(self.state["decisions"]) >= self.state["plan"]["limits"]["max_decisions"]:
            self._block_and_persist(
                "decision limit reached",
                [{"kind": "reference", "ref": f"decisions:{len(self.state['decisions'])}"}],
                "request a new bounded plan from the owner",
            )
            raise StateError("decision limit reached; run is blocked")

        phase_id = phase["id"]

        def mutate() -> None:
            self.state["plan"]["phases"] = deepcopy(validated_plan["phases"])
            candidate_phase = self._phase(phase_id)
            candidate_phase.update(deepcopy(validated_plan["phases"][failed_phase_index]))
            candidate_phase["status"] = "pending"
            candidate_phase["failure"] = None
            candidate_phase["output_value"] = None
            candidate_phase["verification_result"] = None
            # Checkpoints are append-only lifecycle records. Keep historical
            # references when the phase is replanned so no global row becomes
            # orphaned.
            replan_record = {
                "number": len(self.state["replans"]) + 1,
                "at": _now(),
                "phase": phase_id,
                "reason": reason,
                "evidence": deepcopy(evidence_values),
                "next_action": next_action,
                "replacement": deepcopy(validated_plan["phases"][failed_phase_index]),
                "event_sequence": len(self.state["events"]) + 1,
            }
            self.state["replans"].append(replan_record)
            self.state["status"] = "planned"
            self.state["next_action"] = f"start replanned phase {phase_id}"
            self.state["blocker"] = None
            decision = self._decision_record(phase_id, "replan phase", reason, evidence_values, "open")
            self._append_decision_event(
                "replanned",
                decision,
                phase=phase_id,
                number=replan_record["number"],
                replacement=deepcopy(replan_record["replacement"]),
                replan_snapshot=deepcopy(replan_record),
            )

        self._transaction(mutate)
        return deepcopy(self.state)

    def interrupt(self, reason: str) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "running":
            raise StateError("only a running workflow can be interrupted")
        phase = self._require_current("in_progress")
        reason = _text(reason, "interruption.reason", max_length=1000)
        phase_id = phase["id"]
        starts = [
            event["sequence"]
            for event in self.state["events"]
            if event["event"] == "phase_started" and event.get("phase") == phase_id
        ]
        latest_start = max(starts, default=0)
        current_attempt_checkpoints = [
            event["checkpoint"]
            for event in self.state["events"]
            if event["event"] == "checkpoint"
            and event.get("phase") == phase_id
            and event["sequence"] > latest_start
        ]
        if not current_attempt_checkpoints:
            raise StateError("checkpoint the current phase before interrupting")
        last_checkpoint = current_attempt_checkpoints[-1]

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            self.state["status"] = "interrupted"
            candidate_phase["status"] = "interrupted"
            self.state["interruption"] = {"at": _now(), "phase": phase_id, "checkpoint": last_checkpoint, "reason": reason}
            self.state["next_action"] = f"resume from checkpoint {last_checkpoint}"
            self._event(
                "interrupted",
                phase=phase_id,
                checkpoint=last_checkpoint,
                reason=reason,
                interruption_snapshot=deepcopy(self.state["interruption"]),
            )

        self._transaction(mutate)
        return deepcopy(self.state)

    def resume(self) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "interrupted":
            raise StateError("resume requires an interrupted workflow")
        phase = self._require_current("interrupted")
        phase_id = phase["id"]
        checkpoint = self.state["interruption"]["checkpoint"]

        def mutate() -> None:
            candidate_phase = self._phase(phase_id)
            candidate_phase["status"] = "in_progress"
            self.state["status"] = "running"
            self.state["resume_count"] += 1
            self.state["next_action"] = f"continue phase {phase_id} from checkpoint {checkpoint}"
            self._event("resumed", phase=phase_id, checkpoint=checkpoint)

        self._transaction(mutate)
        return deepcopy(self.state)

    def _set_blocked(self, reason: str, evidence: Sequence[Any] | str, next_action: str) -> None:
        reason = _text(reason, "blocker.reason", max_length=1000)
        evidence_values = _evidence(evidence, "blocker.evidence")
        next_action = _text(next_action, "blocker.next_action", max_length=1000)
        current_id = self.state.get("current_phase")
        if current_id is not None:
            phase = self._phase(current_id)
            if phase["status"] in {"pending", "in_progress", "interrupted"}:
                phase["status"] = "blocked"
        self.state["status"] = "blocked"
        self.state["blocker"] = {"at": _now(), "phase": current_id, "reason": reason, "evidence": evidence_values, "next_action": next_action}
        self.state["next_action"] = next_action

    def block(self, reason: str, evidence: Sequence[Any] | str, next_action: str) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] == "completed":
            raise StateError("completed workflow cannot be blocked")
        self._block_and_persist(reason, evidence, next_action)
        return deepcopy(self.state)

    def record_decision(
        self,
        phase: str,
        decision: str,
        why: str,
        evidence: Sequence[Any] | str,
        result: str,
        *,
        status: str | None = None,
        reason: str | None = None,
        next_action: str | None = None,
        persist: bool = True,
        _allow_limit_overflow: bool = False,
    ) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] == "completed":
            raise StateError("completed workflow is terminal and immutable")
        phase = _text(phase, "decision.phase", max_length=100)
        decision = _text(decision, "decision.decision", max_length=1000)
        why = _text(why, "decision.why", max_length=1000)
        evidence_values = _evidence(evidence, "decision.evidence")
        result = _text(result, "decision.result", max_length=40)
        if result not in VALID_RESULTS:
            raise StateError(f"decision.result must be one of {sorted(VALID_RESULTS)}")
        status_value = None
        if status is not None:
            status_value = _text(status, "decision.status", max_length=40)
            if status_value not in {"blocked", "refused"}:
                raise StateError("decision.status must be blocked or refused")
        if (reason is None) != (next_action is None):
            raise StateError("decision.reason and decision.next_action must be provided together")
        reason_value = _text(reason, "decision.reason", max_length=1000) if reason is not None else None
        next_action_value = _text(next_action, "decision.next_action", max_length=1000) if next_action is not None else None
        limit = self.state["plan"]["limits"]["max_decisions"]
        if len(self.state["decisions"]) >= limit and not _allow_limit_overflow:
            self._block_and_persist(
                "decision limit reached",
                [{"kind": "reference", "ref": f"decisions:{len(self.state['decisions'])}"}],
                "request a new bounded plan from the owner",
            )
            raise StateError("decision limit reached; run is blocked")
        record = self._decision_record(
            phase,
            decision,
            why,
            evidence_values,
            result,
            status=status_value,
            reason=reason_value,
            next_action=next_action_value,
        )
        record_holder: dict[str, Any] = {}

        def mutate() -> None:
            record_holder.update(
                self._append_decision_event(
                    "decision_recorded",
                    record,
                    phase=phase,
                )
            )

        # The legacy flag is retained for call compatibility; every state
        # mutation now goes through the same candidate-and-publish path.
        self._transaction(mutate)
        return deepcopy(record_holder)

    def record_predicate(
        self,
        check_id: str,
        passed: bool,
        evidence: Sequence[Any] | str,
        observed: str = "",
        status: str | None = None,
    ) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] == "completed":
            raise PredicateError("completed workflow is terminal and immutable")
        if (
            self.state["status"] != "running"
            or self.state["current_phase"] is not None
            or any(phase["status"] != "passed" for phase in self.state["phases"])
        ):
            raise PredicateError("terminal predicate checks require every phase to be passed")
        check_id = _text(check_id, "predicate.check_id", max_length=100)
        if not isinstance(passed, bool):
            raise PredicateError("predicate.passed must be a boolean")
        checks = self.state["plan"]["terminal_predicate"]["checks"]
        if check_id not in checks:
            raise PredicateError(f"unknown terminal check: {check_id}")
        evidence_values = _evidence(evidence, "predicate.evidence")
        observed_value = _text(observed or ("passed" if passed else "failed"), "predicate.observed", max_length=1000)
        status_value = status
        if status_value is None:
            observed_lower = re.sub(r"[\s-]+", "_", observed_value.casefold())
            if not passed and "not_run" in observed_lower:
                status_value = "not_run"
            elif not passed and "inconclusive" in observed_lower:
                status_value = "inconclusive"
            else:
                status_value = "verified" if passed else "failed"
        status_value = _text(status_value, "predicate.status", max_length=40)
        if status_value not in CHECK_STATUSES:
            raise PredicateError(f"predicate.status must be one of {sorted(CHECK_STATUSES)}")
        if (status_value == "verified") != passed:
            raise PredicateError("only a verified predicate may have passed=true")
        if len(self.state["decisions"]) >= self.state["plan"]["limits"]["max_decisions"]:
            self._block_and_persist(
                "decision limit reached",
                [{"kind": "reference", "ref": f"decisions:{len(self.state['decisions'])}"}],
                "request a new bounded plan from the owner",
            )
            raise StateError("decision limit reached; run is blocked")
        result_holder: dict[str, Any] = {}

        def mutate() -> None:
            result = {
                "check_id": check_id,
                "passed": bool(passed),
                "status": status_value,
                "evidence": deepcopy(evidence_values),
                "observed": observed_value,
                "at": _now(),
                "event_sequence": len(self.state["events"]) + 1,
            }
            decision = self._decision_record(
                "run",
                f"terminal check {check_id}",
                "the terminal predicate check was recorded",
                evidence_values,
                status_value,
            )
            self.state["predicate_results"][check_id] = result
            self.state["next_action"] = "finish when all terminal checks pass" if passed else f"address failed terminal check {check_id}"
            self._append_decision_event(
                "predicate_recorded",
                decision,
                phase="run",
                check_id=check_id,
                passed=bool(passed),
                result_snapshot=deepcopy(result),
            )
            result_holder.update(result)

        self._transaction(mutate)
        return deepcopy(result_holder)

    def finish(self) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "running":
            raise PredicateError(f"cannot finish a {self.state['status']} workflow")
        if any(phase["status"] != "passed" for phase in self.state["phases"]):
            missing = [phase["id"] for phase in self.state["phases"] if phase["status"] != "passed"]
            raise PredicateError(f"terminal predicate is not ready; phases not passed: {', '.join(missing)}")
        results = self.state["predicate_results"]
        missing_checks = [
            check_id
            for check_id in self.state["plan"]["terminal_predicate"]["checks"]
            if not results.get(check_id)
            or results[check_id].get("passed") is not True
            or results[check_id].get("status") != "verified"
        ]
        if missing_checks:
            raise PredicateError("terminal predicate is not satisfied: " + ", ".join(missing_checks))

        def mutate() -> None:
            self.state["status"] = "completed"
            self.state["current_phase"] = None
            self.state["next_action"] = None
            self.state["terminal"] = {
                "at": _now(),
                "predicate": self.state["plan"]["terminal_predicate"]["description"],
                "checks": deepcopy(self.state["predicate_results"]),
            }
            self._event("completed", terminal_snapshot=deepcopy(self.state["terminal"]))

        self._transaction(mutate)
        return deepcopy(self.state)

    def _refuse_action(self, action: str, target: str | None, reason: str) -> None:
        next_action = "choose a declared local action or replan with owner approval"
        target = _sanitize_refusal_target(target)
        refusal = {
            "at": _now(),
            "status": "refused",
            "action": action,
            "target": target,
            "reason": reason,
            "next_action": next_action,
        }
        action_reference = action if SAFE_ACTION.fullmatch(action) else "invalid-action"
        evidence = [
            {"kind": "reference", "ref": f"scope:{action_reference}"},
            {"kind": "reference", "ref": "target:provided" if target is not None else "target:none"},
        ]
        record = self._decision_record(
            self.state.get("current_phase") or "run",
            f"refused action {action_reference}",
            reason,
            evidence,
            "blocked",
            status="refused",
            reason=reason,
            next_action=next_action,
        )

        def mutate() -> None:
            self._append_decision_event("action_refused", record, **refusal)

        self._transaction(mutate)
        raise ScopeRefusal(reason, refusal)

    def guard_action(self, action: str, target: str | None = None) -> dict[str, Any]:
        _require_state_object(self.state)
        action_value = (
            action
            if isinstance(action, str)
            and len(action) <= 100
            and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in action)
            else "<invalid-action>"
        )
        target_display = _sanitize_refusal_target(target)

        if self.state["status"] in ACTION_REFUSAL_STATUSES:
            lifecycle_reasons = {
                "failed": "run is failed; replan before authorizing another action",
                "interrupted": "run is stopped; resume before authorizing another action",
                "blocked": "run is blocked; resolve the recorded blocker before authorizing another action",
                "completed": "run is completed; its terminal state is immutable",
            }
            self._refuse_action(action_value, target_display, lifecycle_reasons[self.state["status"]])

        target_display = _sanitize_refusal_target(target)
        if target is not None and (
            not isinstance(target, str)
            or not target
            or len(target) > 500
            or any(unicodedata.category(char) in {"Cc", "Cf"} for char in target)
            or target != target.strip()
            or "\\" in target
            or target.startswith(("/", "~", "//"))
            or re.match(r"^[A-Za-z]:", target)
            or URI_SCHEME.match(target)
            or "@" in target
            or ".." in PurePosixPath(target).parts
        ):
            target_display = "<invalid-target>"

        scope = self.state["plan"]["scope"]
        reason = _scope_action_reason(scope, action, target)
        if reason:
            action_is_valid = (
                isinstance(action, str)
                and bool(action)
                and len(action) <= 100
                and not any(unicodedata.category(char) in {"Cc", "Cf"} for char in action)
                and action == action.strip()
                and SAFE_ACTION.fullmatch(action) is not None
            )
            self._refuse_action(
                action if action_is_valid else action_value,
                target_display if action_is_valid else None,
                reason,
            )

        allowed = {"at": _now(), "status": "allowed", "action": action, "target": target_display}

        def mutate() -> None:
            self._event("action_allowed", action=action, target=target_display)

        self._transaction(mutate)
        return allowed

    def export_audit(self, audit_path: str | os.PathLike[str]) -> Path:
        _require_state_object(self.state)
        path, _ = _state_path(audit_path, self.workspace_root)
        state_path = self.state_path.resolve(strict=False)
        audit_resolved = path.resolve(strict=False)
        same_file = False
        if path.exists() and self.state_path.exists():
            try:
                same_file = os.path.samefile(path, self.state_path)
            except OSError:
                same_file = False
        if audit_resolved == state_path or same_file:
            raise ScopeRefusal(
                "audit output must not overwrite the workflow state",
                {
                    "status": "refused",
                    "reason": "audit output path resolves to the persisted state path",
                    "next_action": "choose a separate local TSV audit path",
                },
            )
        try:
            destination_mode = path.lstat().st_mode
        except FileNotFoundError:
            destination_mode = None
        except OSError as exc:
            raise StateError(f"audit output destination could not be inspected: {path}") from exc
        if destination_mode is not None and not stat.S_ISREG(destination_mode):
            raise ScopeRefusal(
                "audit output must be a regular file path",
                {
                    "status": "refused",
                    "reason": "audit output destination is not a regular file",
                    "next_action": "choose a new or existing regular local TSV path",
                },
            )
        if path.parent.exists() and not path.parent.is_dir():
            raise ScopeRefusal(
                "audit output parent must be a directory",
                {
                    "status": "refused",
                    "reason": "audit output parent is not a directory",
                    "next_action": "choose a local TSV path below a directory",
                },
            )
        rows = [["ts", "phase", "decision", "why", "evidence", "result"]]
        checkpoints_by_id = {checkpoint["id"]: checkpoint for checkpoint in self.state["checkpoints"]}

        def evidence_text(values: Sequence[Any]) -> str:
            return "; ".join(
                value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in values
            )

        for event in self.state["events"]:
            if event["event"] == "checkpoint":
                checkpoint = checkpoints_by_id[event["checkpoint"]]
                rows.append(
                    [
                        checkpoint["at"],
                        checkpoint["phase"],
                        f"checkpoint: {checkpoint['label']}",
                        checkpoint["next_action"],
                        evidence_text(checkpoint["evidence"]),
                        "open",
                    ]
                )
            elif event["event"] in DECISION_EVENTS:
                decision = event["decision"]
                rows.append(
                    [
                        decision["at"],
                        decision["phase"],
                        decision["decision"],
                        decision["why"],
                        evidence_text(decision["evidence"]),
                        decision["result"],
                    ]
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path


def _load_json(path: str) -> Any:
    candidate = Path(path)
    try:
        input_mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise PlanError(f"JSON file does not exist: {path}") from exc
    except OSError as exc:
        raise PlanError(f"JSON file could not be inspected: {path}") from exc
    if not stat.S_ISREG(input_mode):
        raise PlanError(f"JSON input must be a regular file: {path}")
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise PlanError(f"JSON file is invalid: {path}") from exc
    except UnicodeError as exc:
        raise PlanError(f"JSON file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise PlanError(f"JSON file could not be read: {path}") from exc


def _parse_json_argument(value: str, field: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise PlanError(f"{field} must be valid JSON") from exc


def _print_state(state: Mapping[str, Any]) -> None:
    print(json.dumps(state, indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="validate a plan and create state")
    init.add_argument("--plan", required=True)
    init.add_argument("--state", required=True)
    init.add_argument("--workspace-root")

    for name in ("start-phase", "resume", "finish"):
        command = subparsers.add_parser(name)
        command.add_argument("--state", required=True)
        command.add_argument("--workspace-root")

    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("--state", required=True)
    checkpoint.add_argument("--workspace-root")
    checkpoint.add_argument("--label", required=True)
    checkpoint.add_argument("--evidence", action="append", required=True)
    checkpoint.add_argument("--next-action", required=True)

    complete = subparsers.add_parser("complete-phase")
    complete.add_argument("--state", required=True)
    complete.add_argument("--workspace-root")
    complete.add_argument("--output", required=True)
    complete.add_argument("--check-id", required=True)
    complete.add_argument("--evidence", action="append", required=True)

    fail = subparsers.add_parser("fail-phase")
    fail.add_argument("--state", required=True)
    fail.add_argument("--workspace-root")
    fail.add_argument("--reason", required=True)
    fail.add_argument("--evidence", action="append", required=True)
    fail.add_argument("--next-action", required=True)

    replan = subparsers.add_parser("replan")
    replan.add_argument("--state", required=True)
    replan.add_argument("--workspace-root")
    replan.add_argument("--reason", required=True)
    replan.add_argument("--evidence", action="append", required=True)
    replan.add_argument("--next-action", required=True)
    replan.add_argument("--replacement")

    interrupt = subparsers.add_parser("interrupt")
    interrupt.add_argument("--state", required=True)
    interrupt.add_argument("--workspace-root")
    interrupt.add_argument("--reason", required=True)

    block = subparsers.add_parser("block")
    block.add_argument("--state", required=True)
    block.add_argument("--workspace-root")
    block.add_argument("--reason", required=True)
    block.add_argument("--evidence", action="append", required=True)
    block.add_argument("--next-action", required=True)

    predicate = subparsers.add_parser("predicate")
    predicate.add_argument("--state", required=True)
    predicate.add_argument("--workspace-root")
    predicate.add_argument("--check-id", required=True)
    predicate.add_argument("--passed", action="store_true")
    predicate.add_argument("--status", choices=sorted(CHECK_STATUSES))
    predicate.add_argument("--evidence", action="append", required=True)
    predicate.add_argument("--observed")

    guard = subparsers.add_parser("guard-action")
    guard.add_argument("--state", required=True)
    guard.add_argument("--workspace-root")
    guard.add_argument("--action", required=True)
    guard.add_argument("--target")

    decision = subparsers.add_parser("decision")
    decision.add_argument("--state", required=True)
    decision.add_argument("--workspace-root")
    decision.add_argument("--phase", required=True)
    decision.add_argument("--decision", required=True)
    decision.add_argument("--why", required=True)
    decision.add_argument("--evidence", action="append", required=True)
    decision.add_argument("--result", required=True, choices=sorted(VALID_RESULTS))

    audit = subparsers.add_parser("audit")
    audit.add_argument("--state", required=True)
    audit.add_argument("--workspace-root")
    audit.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            workflow = Workflow.create(args.state, _load_json(args.plan), workspace_root=args.workspace_root)
            _print_state(workflow.state)
            return 0
        workflow = Workflow.load(args.state, workspace_root=args.workspace_root)
        if args.command == "start-phase":
            result = workflow.start_phase()
        elif args.command == "resume":
            result = workflow.resume()
        elif args.command == "finish":
            result = workflow.finish()
        elif args.command == "checkpoint":
            result = workflow.checkpoint(args.label, args.evidence, args.next_action)
        elif args.command == "complete-phase":
            try:
                output: Any = json.loads(args.output)
            except json.JSONDecodeError:
                output = args.output
            result = workflow.complete_phase(output, {"check_id": args.check_id, "passed": True, "evidence": args.evidence})
        elif args.command == "fail-phase":
            result = workflow.fail_phase(args.reason, args.evidence, args.next_action)
        elif args.command == "replan":
            replacement = _parse_json_argument(args.replacement, "replan replacement") if args.replacement else None
            result = workflow.replan(args.reason, args.evidence, args.next_action, replacement)
        elif args.command == "interrupt":
            result = workflow.interrupt(args.reason)
        elif args.command == "block":
            result = workflow.block(args.reason, args.evidence, args.next_action)
        elif args.command == "predicate":
            result = workflow.record_predicate(args.check_id, args.passed, args.evidence, args.observed or "", args.status)
        elif args.command == "guard-action":
            result = workflow.guard_action(args.action, args.target)
        elif args.command == "decision":
            result = workflow.record_decision(args.phase, args.decision, args.why, args.evidence, args.result)
        elif args.command == "audit":
            result = {"audit_path": str(workflow.export_audit(args.output))}
        else:
            raise StateError(f"unknown command: {args.command}")
        _print_state(result if isinstance(result, Mapping) else {"result": result})
        return 0
    except (WorkflowError, OSError) as exc:
        error = {"status": "refused" if isinstance(exc, ScopeRefusal) else "error", "error": str(exc)}
        if isinstance(exc, ScopeRefusal):
            error["refusal"] = exc.refusal
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
