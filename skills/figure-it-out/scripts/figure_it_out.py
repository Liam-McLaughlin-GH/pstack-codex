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


SCHEMA_VERSION = 1
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
    if value.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    if "\\" in value:
        return False
    if ".." in PurePosixPath(value).parts or any(char.isspace() for char in value):
        return False
    if _is_placeholder_reference(value) or _is_scheme_shaped_reference(value):
        return False
    return bool(BOUNDED_REFERENCE.fullmatch(value))


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
    allowed_paths = scope.get("allowed_paths", [])
    if not isinstance(allowed_paths, list):
        raise PlanError("scope.allowed_paths must be a list")
    normalized_paths: list[str] = []
    for path in allowed_paths:
        path = _relative_path(path, "scope.allowed_paths[]")
        normalized_paths.append(path.rstrip("/") + "/")
    logical_actions = scope.get("logical_actions", [])
    logical_actions = _string_list(logical_actions, "scope.logical_actions", allow_empty=True)
    for action in logical_actions:
        if not SAFE_ACTION.fullmatch(action):
            raise PlanError(f"scope.logical_actions contains invalid action {action!r}")
    if not set(logical_actions).issubset(allowed_actions):
        raise PlanError("scope.logical_actions must be a subset of scope.allowed_actions")
    out_of_scope_actions = scope.get("out_of_scope_actions") or []
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


def _state_path(path: str | os.PathLike[str], workspace_root: str | os.PathLike[str] | None) -> tuple[Path, Path]:
    raw = Path(path)
    if workspace_root is not None:
        root = Path(workspace_root)
    elif raw.is_absolute():
        root = raw.parent
    else:
        root = Path.cwd()
    root = root.absolute().resolve()
    candidate = raw if raw.is_absolute() else root / raw
    candidate = candidate.absolute()
    if candidate.is_symlink():
        raise ScopeRefusal(
            "state path must not be a symlink",
            {"status": "refused", "reason": "state path is a symlink", "next_action": "choose a regular local state path"},
        )
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


def _validate_lifecycle_graph(
    state: Mapping[str, Any],
    validated_plan: Mapping[str, Any],
    phases: Sequence[Mapping[str, Any]],
    checkpoint_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Replay persisted transition events and compare them with the snapshot."""

    phase_ids = [phase["id"] for phase in validated_plan["phases"]]
    check_ids = set(validated_plan["terminal_predicate"]["checks"])
    phase_states = {phase_id: "pending" for phase_id in phase_ids}
    phase_attempts = {phase_id: 0 for phase_id in phase_ids}
    last_phase_passed: dict[str, Mapping[str, Any]] = {}
    last_phase_failed: dict[str, Mapping[str, Any]] = {}
    last_predicate_event: dict[str, Mapping[str, Any]] = {}
    seen_checkpoints: set[str] = set()
    latest_checkpoint: dict[str, str] = {}
    latest_interruption: Mapping[str, Any] | None = None
    latest_interruption_snapshot: Mapping[str, Any] | None = None
    latest_blocker: Mapping[str, Any] | None = None
    last_replacement: dict[str, Mapping[str, Any]] = {}
    run_status = "planned"
    resume_count = 0
    expected_next_action = f"start phase {phase_ids[0]}"
    next_replan_number = 1
    completed_event: Mapping[str, Any] | None = None

    replans_by_number = {replan["number"]: replan for replan in state["replans"]}

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

    for index, event in enumerate(state["events"]):
        event_name = event["event"]
        if event_name not in LIFECYCLE_EVENTS | AUDIT_EVENTS:
            fail(f"events[{index}].event is not a supported event")
        if completed_event is not None and event_name != "action_refused":
            fail("lifecycle events cannot be appended after completion")

        if event_name == "action_allowed":
            event_text(event, index, "action", max_length=100)
            if event.get("target") is not None:
                _state_text(event["target"], f"events[{index}].target", max_length=500)
            if completed_event is not None:
                fail("action_allowed cannot be appended after completion")
            continue

        if event_name == "action_refused":
            if event.get("status") != "refused":
                fail(f"events[{index}].action_refused must have refused status")
            event_text(event, index, "action", max_length=100)
            event_text(event, index, "reason", max_length=1000)
            event_text(event, index, "next_action", max_length=1000)
            if event.get("target") is not None:
                _state_text(event["target"], f"events[{index}].target", max_length=500)
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
            seen_checkpoints.add(checkpoint_id)
            latest_checkpoint[phase_id] = checkpoint_id
            if "checkpoint_snapshot" in event and event["checkpoint_snapshot"] != checkpoint:
                fail(f"events[{index}].checkpoint_snapshot does not match the checkpoint")
            expected_next_action = checkpoint["next_action"]
            continue

        if event_name == "phase_passed":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            check_id = event_text(event, index, "check_id", max_length=100)
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] passes a phase outside the current phase")
            phase_states[phase_id] = "passed"
            last_phase_passed[phase_id] = event
            if "verification_snapshot" in event:
                _state_phase_verification(
                    event["verification_snapshot"],
                    f"events[{index}].verification_snapshot",
                    check_id,
                )
            next_phase = current_phase()
            expected_next_action = (
                f"inspect output, then start phase {next_phase}" if next_phase is not None else "record terminal predicate checks"
            )
            continue

        if event_name == "phase_failed":
            phase_id = event_phase(event, index)
            assert phase_id is not None
            reason = event_text(event, index, "reason", max_length=1000)
            if run_status != "running" or phase_id != current_phase() or phase_states[phase_id] != "in_progress":
                fail(f"events[{index}] fails a phase outside the current phase")
            phase_states[phase_id] = "failed"
            last_phase_failed[phase_id] = event
            run_status = "failed"
            if "failure_snapshot" in event:
                _state_failure(event["failure_snapshot"], f"events[{index}].failure_snapshot")
                if event["failure_snapshot"]["reason"] != reason:
                    fail(f"events[{index}].failure_snapshot does not match the failure")
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
            if "replacement" in event and event["replacement"] != replan["replacement"]:
                fail(f"events[{index}].replacement does not match the replan record")
            phase_states[phase_id] = "pending"
            run_status = "planned"
            last_replacement[phase_id] = replan["replacement"]
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
            latest_interruption_snapshot = None
            if "interruption_snapshot" in event:
                snapshot = event["interruption_snapshot"]
                if not isinstance(snapshot, Mapping):
                    fail(f"events[{index}].interruption_snapshot must be an object")
                _require_state_fields(
                    snapshot,
                    {"at", "phase", "checkpoint", "reason"},
                    f"events[{index}].interruption_snapshot",
                )
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
            if run_status != "interrupted" or phase_id != current_phase() or phase_states[phase_id] != "interrupted":
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
            if phase_id is not None and phase_states[phase_id] in {"pending", "in_progress", "interrupted"}:
                phase_states[phase_id] = "blocked"
            run_status = "blocked"
            reason = event_text(event, index, "reason", max_length=1000)
            latest_blocker = event
            if "blocker_snapshot" in event:
                _state_failure(event["blocker_snapshot"], f"events[{index}].blocker_snapshot")
                if event["blocker_snapshot"]["reason"] != reason:
                    fail(f"events[{index}].blocker_snapshot does not match the blocker")
            expected_next_action = ""
            continue

        if event_name == "predicate_recorded":
            check_id = event_text(event, index, "check_id", max_length=100)
            passed = event_bool(event, index, "passed")
            if run_status != "running" or not all_phases_passed():
                fail(f"events[{index}] records a predicate before all phases pass")
            if check_id not in check_ids:
                fail(f"events[{index}] records an undeclared predicate")
            if "result_snapshot" in event:
                snapshot = event["result_snapshot"]
                if not isinstance(snapshot, Mapping):
                    fail(f"events[{index}].result_snapshot must be an object")
                _require_state_fields(
                    snapshot,
                    {"check_id", "passed", "status", "evidence", "observed", "at"},
                    f"events[{index}].result_snapshot",
                )
                if snapshot["check_id"] != check_id or snapshot["passed"] is not passed:
                    fail(f"events[{index}].result_snapshot does not match the predicate")
                if snapshot["status"] not in CHECK_STATUSES or (snapshot["status"] == "verified") is not passed:
                    fail(f"events[{index}].result_snapshot has an invalid status")
                _state_evidence(snapshot["evidence"], f"events[{index}].result_snapshot.evidence")
                _state_text(snapshot["observed"], f"events[{index}].result_snapshot.observed", max_length=1000)
                _state_text(snapshot["at"], f"events[{index}].result_snapshot.at")
            last_predicate_event[check_id] = event
            expected_next_action = (
                "finish when all terminal checks pass" if passed else f"address failed terminal check {check_id}"
            )
            continue

        if event_name == "completed":
            if run_status != "running" or not all_phases_passed():
                fail(f"events[{index}] completes a run before the predicate is ready")
            if any(check_id not in last_predicate_event or not last_predicate_event[check_id]["passed"] for check_id in check_ids):
                fail(f"events[{index}] completes a run without all verified predicate events")
            completed_event = event
            run_status = "completed"
            expected_next_action = ""
            continue

    if run_status != state["status"]:
        fail("run status does not match the lifecycle event graph")
    state_phase_statuses = {phase["id"]: phase["status"] for phase in phases}
    if state_phase_statuses != phase_states:
        fail("phase statuses do not match the lifecycle event graph")
    state_phase_attempts = {phase["id"]: phase["attempt"] for phase in phases}
    if state_phase_attempts != phase_attempts:
        fail("phase attempts do not match the lifecycle event graph")
    if state["resume_count"] != resume_count:
        fail("resume_count does not match the lifecycle event graph")
    if state["current_phase"] != current_phase():
        fail("current_phase does not match the lifecycle event graph")

    if state["status"] == "completed" and completed_event is None:
        fail("completed runs require a completed lifecycle event")
    if state["status"] != "completed" and completed_event is not None:
        fail("non-completed runs cannot retain a completed lifecycle event")
    if completed_event is not None and "terminal_snapshot" in completed_event:
        if completed_event["terminal_snapshot"] != state["terminal"]:
            fail("terminal snapshot does not match the completed lifecycle event")

    for phase in phases:
        phase_id = phase["id"]
        if phase_id in last_replacement:
            plan_phase = next(item for item in validated_plan["phases"] if item["id"] == phase_id)
            if plan_phase != last_replacement[phase_id]:
                fail(f"phase {phase_id} plan does not match its final replacement")
        if phase["status"] == "passed":
            pass_event = last_phase_passed.get(phase_id)
            if pass_event is None or pass_event["check_id"] != phase["verification"]["id"]:
                fail(f"phase {phase_id} has no final matching phase_passed event")
            if "verification_snapshot" in pass_event and pass_event["verification_snapshot"] != phase["verification_result"]:
                fail(f"phase {phase_id} verification evidence does not match its lifecycle event")
        if phase["status"] == "failed":
            failure_event = last_phase_failed.get(phase_id)
            if failure_event is None or failure_event["reason"] != phase["failure"]["reason"]:
                fail(f"phase {phase_id} failure does not match its lifecycle event")
            if "failure_snapshot" in failure_event and failure_event["failure_snapshot"] != phase["failure"]:
                fail(f"phase {phase_id} failure snapshot does not match its lifecycle event")

    if latest_interruption is None:
        if state["interruption"] is not None:
            fail("interruption details exist without an interrupted lifecycle event")
    else:
        interruption = state["interruption"]
        if interruption is None:
            fail("interrupted lifecycle history has no interruption details")
        if interruption["phase"] != latest_interruption["phase"] or interruption["checkpoint"] != latest_interruption["checkpoint"]:
            fail("interruption details do not match the lifecycle event")
        if "reason" in latest_interruption and interruption["reason"] != latest_interruption["reason"]:
            fail("interruption reason does not match the lifecycle event")
        if latest_interruption_snapshot is not None and latest_interruption_snapshot != interruption:
            fail("interruption snapshot does not match the lifecycle event")

    if latest_blocker is None:
        if state["status"] == "blocked":
            fail("blocked runs require a blocked lifecycle event")
    else:
        blocker = state["blocker"]
        if blocker is None or blocker["phase"] != latest_blocker.get("phase") or blocker["reason"] != latest_blocker["reason"]:
            fail("blocker details do not match the lifecycle event")
        if "blocker_snapshot" in latest_blocker and latest_blocker["blocker_snapshot"] != blocker:
            fail("blocker snapshot does not match the lifecycle event")

    for check_id in check_ids:
        result = state["predicate_results"][check_id]
        predicate_event = last_predicate_event.get(check_id)
        if result is None:
            if predicate_event is not None:
                fail(f"predicate_results[{check_id}] is missing its lifecycle event result")
        else:
            if predicate_event is None or result["passed"] is not predicate_event["passed"]:
                fail(f"predicate_results[{check_id}] does not match its lifecycle event")
            if "result_snapshot" in predicate_event and predicate_event["result_snapshot"] != result:
                fail(f"predicate_results[{check_id}] evidence does not match its lifecycle event")

        terminal_decisions = [
            decision
            for decision in state["decisions"]
            if decision["phase"] == "run" and decision["decision"] == f"terminal check {check_id}"
        ]
        if result is None:
            if terminal_decisions:
                fail(f"predicate decision exists without predicate_results[{check_id}]")
        else:
            if not terminal_decisions:
                fail(f"predicate_results[{check_id}] has no terminal decision")
            latest_decision = terminal_decisions[-1]
            if latest_decision["result"] != result["status"] or latest_decision["evidence"] != result["evidence"]:
                fail(f"predicate_results[{check_id}] evidence does not match its terminal decision")

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
        _require_state_fields(result, {"check_id", "passed", "status", "evidence", "observed", "at"}, f"predicate_results[{check_id}]")
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
        _require_state_fields(decision, {"at", "phase", "decision", "why", "evidence", "result"}, f"decisions[{index}]")
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
        _require_state_fields(checkpoint, {"id", "at", "phase", "label", "evidence", "next_action"}, f"checkpoints[{index}]")
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
        _require_state_fields(replan, {"number", "at", "phase", "reason", "evidence", "next_action", "replacement"}, f"replans[{index}]")
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
        _require_state_fields(event, {"at", "event"}, f"events[{index}]")
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

    _validate_lifecycle_graph(state, validated_plan, phases, checkpoint_by_id)


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
        path, root = _state_path(state_path, workspace_root)
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
        path, root = _state_path(state_path, workspace_root)
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

    def _persist_candidate(self, candidate: dict[str, Any]) -> None:
        """Persist a candidate state without exposing a partial in-memory mutation."""

        previous = self.state
        self.state = candidate
        try:
            self._persist()
        except Exception:
            self.state = previous
            raise

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

    def _event(self, event: str, **details: Any) -> None:
        self.state["events"].append({"at": _now(), "event": event, **details})

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

    def _append_decision_record(self, record: dict[str, Any], *, allow_limit_overflow: bool = False) -> None:
        limit = self.state["plan"]["limits"]["max_decisions"]
        if len(self.state["decisions"]) >= limit and not allow_limit_overflow:
            raise StateError("decision limit reached")
        if len(self.state["decisions"]) > limit or (
            len(self.state["decisions"]) == limit and not allow_limit_overflow
        ):
            raise StateError("decision limit reached")
        self.state["decisions"].append(record)

    def _block_and_persist(self, reason: str, evidence: Sequence[Any] | str, next_action: str) -> None:
        reason = _text(reason, "blocker.reason", max_length=1000)
        evidence_values = _evidence(evidence, "blocker.evidence")
        next_action = _text(next_action, "blocker.next_action", max_length=1000)
        self._set_blocked(reason, evidence_values, next_action)
        phase = self.state.get("current_phase") or "run"
        self._append_decision_record(
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
            allow_limit_overflow=True,
        )
        self._persist()

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
                [f"phase:{phase['id']}:attempts={phase['attempt']}"],
                f"replan phase {phase['id']} with an explicit new approach",
            )
            raise StateError("phase attempt limit reached; run is blocked")
        phase["attempt"] += 1
        phase["status"] = "in_progress"
        self.state["status"] = "running"
        self.state["next_action"] = f"produce and verify phase {phase['id']} output"
        self.state["blocker"] = None
        self._event("phase_started", phase=phase["id"], attempt=phase["attempt"])
        self._persist()
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
        checkpoint = {
            "id": f"cp-{len(self.state['checkpoints']) + 1}",
            "at": _now(),
            "phase": phase["id"],
            "label": label,
            "evidence": evidence_values,
            "next_action": next_action,
        }
        self.state["checkpoints"].append(checkpoint)
        phase["checkpoints"].append(checkpoint["id"])
        self.state["next_action"] = next_action
        self._event(
            "checkpoint",
            checkpoint=checkpoint["id"],
            phase=phase["id"],
            checkpoint_snapshot=deepcopy(checkpoint),
        )
        self._persist()
        return deepcopy(checkpoint)

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
        phase["output_value"] = output
        phase["verification_result"] = {
            "check_id": check_id,
            "passed": True,
            "evidence": evidence_values,
            "observed": _text(verification.get("observed", "passed"), "verification.observed", max_length=1000),
        }
        phase["status"] = "passed"
        phase["failure"] = None
        next_phase = self._next_pending()
        if next_phase is None:
            self.state["current_phase"] = None
            self.state["next_action"] = "record terminal predicate checks"
        else:
            self.state["current_phase"] = next_phase["id"]
            self.state["next_action"] = f"inspect output, then start phase {next_phase['id']}"
        self._event(
            "phase_passed",
            phase=phase["id"],
            check_id=check_id,
            verification_snapshot=deepcopy(phase["verification_result"]),
        )
        self.record_decision(
            phase["id"],
            f"passed phase {phase['id']}",
            "the phase output and declared verification both passed",
            evidence_values,
            "verified",
            persist=False,
        )
        self._persist()
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
        phase["status"] = "failed"
        phase["failure"] = {"reason": reason, "evidence": evidence_values, "next_action": next_action, "at": _now()}
        self.state["status"] = "failed"
        self.state["next_action"] = f"replan phase {phase['id']}: {next_action}"
        self._event(
            "phase_failed",
            phase=phase["id"],
            reason=reason,
            failure_snapshot=deepcopy(phase["failure"]),
        )
        self.record_decision(
            phase["id"],
            f"failed: {reason}",
            "the declared phase check did not pass",
            evidence_values,
            "failed",
            persist=False,
        )
        self._persist()
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

        candidate = deepcopy(self.state)
        candidate["plan"]["phases"] = validated_plan["phases"]
        candidate_phase = candidate["phases"][failed_phase_index]
        candidate_phase.update(validated_plan["phases"][failed_phase_index])
        candidate_phase["status"] = "pending"
        candidate_phase["failure"] = None
        candidate_phase["output_value"] = None
        candidate_phase["verification_result"] = None
        candidate_phase["checkpoints"] = []
        replan_record = {
            "number": len(candidate["replans"]) + 1,
            "at": _now(),
            "phase": candidate_phase["id"],
            "reason": reason,
            "evidence": evidence_values,
            "next_action": next_action,
            "replacement": deepcopy(validated_plan["phases"][failed_phase_index]),
        }
        candidate["replans"].append(replan_record)
        candidate["status"] = "planned"
        candidate["next_action"] = f"start replanned phase {candidate_phase['id']}"
        candidate["blocker"] = None
        candidate["decisions"].append(
            self._decision_record(candidate_phase["id"], "replan phase", reason, evidence_values, "open")
        )
        candidate["events"].append(
            {
                "at": _now(),
                "event": "replanned",
                "phase": candidate_phase["id"],
                "number": replan_record["number"],
                "replacement": deepcopy(replan_record["replacement"]),
            }
        )
        self._persist_candidate(candidate)
        return deepcopy(self.state)

    def interrupt(self, reason: str) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "running":
            raise StateError("only a running workflow can be interrupted")
        phase = self._require_current("in_progress")
        reason = _text(reason, "interruption.reason", max_length=1000)
        if not phase["checkpoints"]:
            raise StateError("checkpoint the current phase before interrupting")
        last_checkpoint = phase["checkpoints"][-1]
        self.state["status"] = "interrupted"
        phase["status"] = "interrupted"
        self.state["interruption"] = {"at": _now(), "phase": phase["id"], "checkpoint": last_checkpoint, "reason": reason}
        self.state["next_action"] = f"resume from checkpoint {last_checkpoint}"
        self._event(
            "interrupted",
            phase=phase["id"],
            checkpoint=last_checkpoint,
            reason=reason,
            interruption_snapshot=deepcopy(self.state["interruption"]),
        )
        self._persist()
        return deepcopy(self.state)

    def resume(self) -> dict[str, Any]:
        _require_state_object(self.state)
        if self.state["status"] != "interrupted":
            raise StateError("resume requires an interrupted workflow")
        phase = self._require_current("interrupted")
        phase["status"] = "in_progress"
        self.state["status"] = "running"
        self.state["resume_count"] += 1
        checkpoint = self.state["interruption"]["checkpoint"]
        self.state["next_action"] = f"continue phase {phase['id']} from checkpoint {checkpoint}"
        self._event("resumed", phase=phase["id"], checkpoint=checkpoint)
        self._persist()
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
        self._event("blocked", phase=current_id, reason=reason, blocker_snapshot=deepcopy(self.state["blocker"]))

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
        self._append_decision_record(record, allow_limit_overflow=_allow_limit_overflow)
        if persist:
            self._persist()
        return deepcopy(record)

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
        result = {
            "check_id": check_id,
            "passed": bool(passed),
            "status": status_value,
            "evidence": evidence_values,
            "observed": observed_value,
            "at": _now(),
        }
        self.record_decision(
            self.state.get("current_phase") or "run",
            f"terminal check {check_id}",
            "the terminal predicate check was recorded",
            evidence_values,
            status_value,
            persist=False,
        )
        self.state["predicate_results"][check_id] = result
        self.state["next_action"] = "finish when all terminal checks pass" if passed else f"address failed terminal check {check_id}"
        self._event("predicate_recorded", check_id=check_id, passed=bool(passed), result_snapshot=deepcopy(result))
        self._persist()
        return deepcopy(result)

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
        self.state["status"] = "completed"
        self.state["current_phase"] = None
        self.state["next_action"] = None
        self.state["terminal"] = {
            "at": _now(),
            "predicate": self.state["plan"]["terminal_predicate"]["description"],
            "checks": deepcopy(results),
        }
        self._event("completed", terminal_snapshot=deepcopy(self.state["terminal"]))
        self._persist()
        return deepcopy(self.state)

    def _refuse_action(self, action: str, target: str | None, reason: str) -> None:
        next_action = "choose a declared local action or replan with owner approval"
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
        event = {"event": "action_refused", **refusal}
        decision_limit = self.state["plan"]["limits"]["max_decisions"]
        try:
            self._append_decision_record(record, allow_limit_overflow=len(self.state["decisions"]) == decision_limit)
        except StateError:
            # The refusal event remains complete when the bounded decision list
            # has already used its one permitted overflow record.
            event["decision"] = deepcopy(record)
        self.state["events"].append(event)
        self._persist()
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
        target_display: str | None
        if not isinstance(target, str):
            target_display = None if target is None else "<invalid-target>"
        elif len(target) > 500 or any(unicodedata.category(char) in {"Cc", "Cf"} for char in target):
            target_display = "<invalid-target>"
        else:
            target_display = target

        if self.state["status"] in ACTION_REFUSAL_STATUSES:
            lifecycle_reasons = {
                "failed": "run is failed; replan before authorizing another action",
                "interrupted": "run is stopped; resume before authorizing another action",
                "blocked": "run is blocked; resolve the recorded blocker before authorizing another action",
                "completed": "run is completed; its terminal state is immutable",
            }
            self._refuse_action(action_value, target_display, lifecycle_reasons[self.state["status"]])

        action_invalid_reason: str | None = None
        if not isinstance(action, str):
            action_invalid_reason = "action name is not a string"
        elif not action:
            action_invalid_reason = "action name is empty"
        elif len(action) > 100:
            action_invalid_reason = "action name exceeds 100 characters"
        elif any(unicodedata.category(char) in {"Cc", "Cf"} for char in action):
            action_invalid_reason = "action name contains a control or format character"
        elif action != action.strip():
            action_invalid_reason = "action name contains surrounding whitespace"
        elif not SAFE_ACTION.fullmatch(action):
            action_invalid_reason = "action name is not a declared safe action"
        if action_invalid_reason:
            self._refuse_action(action_value, None, action_invalid_reason)
        assert isinstance(action, str)

        target_provided = target is not None
        target_invalid_reason: str | None = None
        target_display = target if isinstance(target, str) else None
        if target_provided and not isinstance(target, str):
            target_invalid_reason = "target is not a string"
            target_display = "<invalid-target>"
        elif target_provided and isinstance(target, str):
            if not target:
                target_invalid_reason = "target is empty; omit it only for a logical action"
                target_display = "<invalid-target>"
            elif len(target) > 500:
                target_invalid_reason = "target exceeds 500 characters"
                target_display = "<invalid-target>"
            elif any(unicodedata.category(char) in {"Cc", "Cf"} for char in target):
                target_invalid_reason = "target contains a control or format character"
                target_display = "<invalid-target>"
            elif target != target.strip():
                target_invalid_reason = "target contains surrounding whitespace"
                target_display = "<invalid-target>"
            elif "\\" in target:
                target_invalid_reason = "target contains a backslash escape"
                target_display = "<invalid-target>"
            elif target.startswith(("/", "~", "//")) or re.match(r"^[A-Za-z]:", target):
                target_invalid_reason = "target is absolute, a drive path, or a UNC path"
                target_display = "<invalid-target>"
            elif URI_SCHEME.match(target):
                target_invalid_reason = "target contains a URL or URI scheme"
                target_display = "<invalid-target>"
            elif "@" in target:
                target_invalid_reason = "target looks like a credential or external host"
                target_display = "<invalid-target>"
            elif ".." in PurePosixPath(target).parts:
                target_invalid_reason = "target escapes the declared scope"
                target_display = "<invalid-target>"

        scope = self.state["plan"]["scope"]
        reason: str | None = target_invalid_reason
        if reason is None and action in scope["out_of_scope_actions"]:
            reason = f"action {action!r} is explicitly out of scope"
        elif reason is None and action not in scope["allowed_actions"]:
            reason = f"action {action!r} is not declared in allowed_actions"
        elif reason is None and target_provided and target_display is not None:
            normalized = str(PurePosixPath(target_display))
            if scope["allowed_paths"] and not any(
                normalized == prefix[:-1] or normalized.startswith(prefix) for prefix in scope["allowed_paths"]
            ):
                reason = "target is outside scope.allowed_paths"
        elif reason is None and scope["allowed_paths"] and not target_provided:
            if action not in scope.get("logical_actions", []):
                reason = "a target is required for this action in a path-constrained scope"
        if reason:
            self._refuse_action(action, target_display, reason)

        allowed = {"at": _now(), "status": "allowed", "action": action, "target": target_display}
        self._event("action_allowed", action=action, target=target_display)
        self._persist()
        return allowed

    def export_audit(self, audit_path: str | os.PathLike[str]) -> Path:
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
        records: list[tuple[str, int, list[str]]] = []

        def evidence_text(values: Sequence[Any]) -> str:
            return "; ".join(
                value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in values
            )

        for item in self.state["decisions"]:
            records.append(
                (
                    item["at"],
                    1,
                    [
                        item["at"],
                        item["phase"],
                        item["decision"],
                        item["why"],
                        evidence_text(item["evidence"]),
                        item["result"],
                    ],
                )
            )
        for checkpoint in self.state["checkpoints"]:
            records.append(
                (
                    checkpoint["at"],
                    0,
                    [
                        checkpoint["at"],
                        checkpoint["phase"],
                        f"checkpoint: {checkpoint['label']}",
                        checkpoint["next_action"],
                        evidence_text(checkpoint["evidence"]),
                        "open",
                    ],
                )
            )
        rows.extend(record for _, _, record in sorted(records, key=lambda item: (item[0], item[1])))
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
