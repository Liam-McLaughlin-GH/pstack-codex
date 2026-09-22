#!/usr/bin/env python3
"""Run a finite, persisted, and auditable multi-phase workflow."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
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
VALID_RESULTS = {"open", "verified", "failed", "reverted", "blocked"}
SAFE_ACTION = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


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
    value = value.strip()
    if len(value) > max_length:
        raise PlanError(f"{field} exceeds {max_length} characters")
    if any(char in value for char in "\x00\r\n"):
        raise PlanError(f"{field} contains a control character")
    return value


def _string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise PlanError(f"{field} must be a non-empty list")
    result = [_text(item, f"{field}[]", max_length=1000) for item in value]
    if len(result) != len(set(result)):
        raise PlanError(f"{field} contains duplicates")
    return result


def _evidence(value: Any, field: str = "evidence") -> list[str]:
    if isinstance(value, str):
        value = [value]
    return _string_list(value, field)


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
    if path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:/", path):
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
    if candidate.exists() and candidate.is_symlink():
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


def _require_state_object(state: Mapping[str, Any]) -> None:
    if state.get("schema_version") != SCHEMA_VERSION:
        raise StateError("unsupported state schema")
    if not isinstance(state.get("plan"), Mapping):
        raise StateError("state has no plan")
    if not isinstance(state.get("phases"), list) or not state["phases"]:
        raise StateError("state has no phases")


class Workflow:
    """A finite state machine backed by one local JSON state file."""

    def __init__(self, state_path: Path, workspace_root: Path, state: dict[str, Any]):
        _require_state_object(state)
        self.state_path = state_path
        self.workspace_root = workspace_root
        self.state = state

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
        if path.exists():
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
            with path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except FileNotFoundError as exc:
            raise StateError(f"state file does not exist: {path}") from exc
        except json.JSONDecodeError as exc:
            raise StateError(f"state file is not valid JSON: {path}") from exc
        if not isinstance(state, dict):
            raise StateError("state root must be an object")
        return cls(path, root, state)

    def _persist(self) -> None:
        self.state["updated_at"] = _now()
        self._validate_state_limits()
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

    def _validate_state_limits(self) -> None:
        limits = self.state["plan"]["limits"]
        if len(self.state["checkpoints"]) > limits["max_checkpoints"]:
            raise StateError("checkpoint limit exceeded")
        if len(self.state["decisions"]) > limits["max_decisions"]:
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
        phase = self._phase()
        if phase["status"] != status:
            raise StateError(f"phase {phase['id']} is {phase['status']}; expected {status}")
        return phase

    def start_phase(self) -> dict[str, Any]:
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
            self._set_blocked(
                "phase attempt limit reached",
                [f"phase:{phase['id']}:attempts={phase['attempt']}"],
                f"replan phase {phase['id']} with an explicit new approach",
            )
            self._persist()
            raise StateError("phase attempt limit reached; run is blocked")
        phase["attempt"] += 1
        phase["status"] = "in_progress"
        self.state["status"] = "running"
        self.state["next_action"] = f"produce and verify phase {phase['id']} output"
        self.state["blocker"] = None
        self._event("phase_started", phase=phase["id"], attempt=phase["attempt"])
        self._persist()
        return deepcopy(self.state)

    def checkpoint(self, label: str, evidence: Sequence[str] | str, next_action: str) -> dict[str, Any]:
        phase = self._require_current("in_progress")
        label = _text(label, "checkpoint.label", max_length=200)
        evidence_values = _evidence(evidence, "checkpoint.evidence")
        next_action = _text(next_action, "checkpoint.next_action", max_length=1000)
        limit = self.state["plan"]["limits"]["max_checkpoints"]
        if len(self.state["checkpoints"]) >= limit:
            self._set_blocked(
                "checkpoint limit reached",
                evidence_values,
                "replan with a justified checkpoint budget or finish the current phase",
            )
            self._persist()
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
        self._event("checkpoint", checkpoint=checkpoint["id"], phase=phase["id"])
        self._persist()
        return deepcopy(checkpoint)

    def complete_phase(self, output: Any, verification: Mapping[str, Any]) -> dict[str, Any]:
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
        self._event("phase_passed", phase=phase["id"], check_id=check_id)
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

    def fail_phase(self, reason: str, evidence: Sequence[str] | str, next_action: str) -> dict[str, Any]:
        phase = self._require_current("in_progress")
        reason = _text(reason, "failure.reason", max_length=1000)
        evidence_values = _evidence(evidence, "failure.evidence")
        next_action = _text(next_action, "failure.next_action", max_length=1000)
        phase["status"] = "failed"
        phase["failure"] = {"reason": reason, "evidence": evidence_values, "next_action": next_action, "at": _now()}
        self.state["status"] = "failed"
        self.state["next_action"] = f"replan phase {phase['id']}: {next_action}"
        self._event("phase_failed", phase=phase["id"], reason=reason)
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
        evidence: Sequence[str] | str,
        next_action: str,
        replacement: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.state["status"] != "failed":
            raise StateError("replan requires a failed phase")
        reason = _text(reason, "replan.reason", max_length=1000)
        evidence_values = _evidence(evidence, "replan.evidence")
        next_action = _text(next_action, "replan.next_action", max_length=1000)
        limit = self.state["plan"]["limits"]["max_replans"]
        if len(self.state["replans"]) >= limit:
            self._set_blocked("replan limit reached", evidence_values, "request a new bounded plan from the owner")
            self._persist()
            raise StateError("replan limit reached; run is blocked")
        phase = self._phase()
        if phase["status"] != "failed":
            raise StateError("current phase is not failed")
        replacement_spec = None
        if replacement is not None:
            replacement_spec = _phase_spec(replacement, 0)
            if replacement_spec["id"] != phase["id"]:
                raise PlanError("replacement phase must keep the failed phase id")
            phase.update(replacement_spec)
        phase["status"] = "pending"
        phase["failure"] = None
        phase["output_value"] = None
        phase["verification_result"] = None
        phase["checkpoints"] = []
        replan_record = {
            "number": len(self.state["replans"]) + 1,
            "at": _now(),
            "phase": phase["id"],
            "reason": reason,
            "evidence": evidence_values,
            "next_action": next_action,
            "replacement": deepcopy(replacement_spec or phase["verification"]),
        }
        self.state["replans"].append(replan_record)
        self.state["status"] = "planned"
        self.state["next_action"] = f"start replanned phase {phase['id']}"
        self.record_decision(phase["id"], "replan phase", reason, evidence_values, "open", persist=False)
        self._event("replanned", phase=phase["id"], number=replan_record["number"])
        self._persist()
        return deepcopy(self.state)

    def interrupt(self, reason: str) -> dict[str, Any]:
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
        self._event("interrupted", phase=phase["id"], checkpoint=last_checkpoint)
        self._persist()
        return deepcopy(self.state)

    def resume(self) -> dict[str, Any]:
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

    def _set_blocked(self, reason: str, evidence: Sequence[str] | str, next_action: str) -> None:
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
        self._event("blocked", phase=current_id, reason=reason)

    def block(self, reason: str, evidence: Sequence[str] | str, next_action: str) -> dict[str, Any]:
        if self.state["status"] == "completed":
            raise StateError("completed workflow cannot be blocked")
        self._set_blocked(reason, evidence, next_action)
        self.record_decision(
            self.state.get("current_phase") or "run",
            f"blocked: {reason}",
            "the run cannot proceed within its current constraints",
            self.state["blocker"]["evidence"],
            "blocked",
            persist=False,
        )
        self._persist()
        return deepcopy(self.state)

    def record_decision(
        self,
        phase: str,
        decision: str,
        why: str,
        evidence: Sequence[str] | str,
        result: str,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        phase = _text(phase, "decision.phase", max_length=100)
        decision = _text(decision, "decision.decision", max_length=1000)
        why = _text(why, "decision.why", max_length=1000)
        evidence_values = _evidence(evidence, "decision.evidence")
        result = _text(result, "decision.result", max_length=40)
        if result not in VALID_RESULTS:
            raise StateError(f"decision.result must be one of {sorted(VALID_RESULTS)}")
        limit = self.state["plan"]["limits"]["max_decisions"]
        if len(self.state["decisions"]) >= limit:
            raise StateError("decision limit reached")
        record = {"at": _now(), "phase": phase, "decision": decision, "why": why, "evidence": evidence_values, "result": result}
        self.state["decisions"].append(record)
        if persist:
            self._persist()
        return deepcopy(record)

    def record_predicate(
        self,
        check_id: str,
        passed: bool,
        evidence: Sequence[str] | str,
        observed: str = "",
    ) -> dict[str, Any]:
        check_id = _text(check_id, "predicate.check_id", max_length=100)
        if not isinstance(passed, bool):
            raise PredicateError("predicate.passed must be a boolean")
        checks = self.state["plan"]["terminal_predicate"]["checks"]
        if check_id not in checks:
            raise PredicateError(f"unknown terminal check: {check_id}")
        evidence_values = _evidence(evidence, "predicate.evidence")
        result = {
            "check_id": check_id,
            "passed": bool(passed),
            "evidence": evidence_values,
            "observed": _text(observed or ("passed" if passed else "failed"), "predicate.observed", max_length=1000),
            "at": _now(),
        }
        self.state["predicate_results"][check_id] = result
        self.state["next_action"] = "finish when all terminal checks pass" if passed else f"address failed terminal check {check_id}"
        self._event("predicate_recorded", check_id=check_id, passed=bool(passed))
        self.record_decision(
            self.state.get("current_phase") or "run",
            f"terminal check {check_id}",
            "the terminal predicate check was recorded",
            evidence_values,
            "verified" if passed else "failed",
            persist=False,
        )
        self._persist()
        return deepcopy(result)

    def finish(self) -> dict[str, Any]:
        if self.state["status"] != "running":
            raise PredicateError(f"cannot finish a {self.state['status']} workflow")
        if any(phase["status"] != "passed" for phase in self.state["phases"]):
            missing = [phase["id"] for phase in self.state["phases"] if phase["status"] != "passed"]
            raise PredicateError(f"terminal predicate is not ready; phases not passed: {', '.join(missing)}")
        results = self.state["predicate_results"]
        missing_checks = [
            check_id
            for check_id in self.state["plan"]["terminal_predicate"]["checks"]
            if not results.get(check_id) or results[check_id].get("passed") is not True
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
        self._event("completed")
        self._persist()
        return deepcopy(self.state)

    def guard_action(self, action: str, target: str | None = None) -> dict[str, Any]:
        try:
            action = _text(action, "action", max_length=100)
        except PlanError as exc:
            raise ScopeRefusal("action name is invalid and was refused") from exc
        if not SAFE_ACTION.fullmatch(action):
            raise ScopeRefusal("action name is invalid and was refused")
        if self.state["status"] == "completed":
            raise StateError("completed workflow cannot authorize another action")
        target_invalid = target is not None and not isinstance(target, str)
        if not target_invalid:
            target_invalid = bool(target) and (len(target) > 500 or any(char in target for char in "\x00\r\n"))
        target_value = "" if target is None else target if isinstance(target, str) else "<invalid-target>"
        if target_invalid:
            target_value = "<invalid-target>"
        scope = self.state["plan"]["scope"]
        reason: str | None = None
        if target_invalid:
            reason = "target is invalid or contains a control character"
        elif action in scope["out_of_scope_actions"]:
            reason = f"action {action!r} is explicitly out of scope"
        elif action not in scope["allowed_actions"]:
            reason = f"action {action!r} is not declared in allowed_actions"
        elif target_value and (
            target_value.startswith(("/", "~", "http:", "https:", "ssh:", "git@"))
            or re.match(r"^[A-Za-z]:[/\\]", target_value)
            or ".." in PurePosixPath(target_value.replace("\\", "/")).parts
        ):
            reason = "target is absolute, external, or escapes the declared scope"
        elif scope["allowed_paths"] and target_value:
            normalized = str(PurePosixPath(target_value.replace("\\", "/")))
            if not any(normalized == prefix[:-1] or normalized.startswith(prefix) for prefix in scope["allowed_paths"]):
                reason = "target is outside scope.allowed_paths"
        elif scope["allowed_paths"] and not target_value:
            reason = "an action target is required when allowed_paths is constrained"
        if reason:
            refusal = {
                "at": _now(),
                "status": "refused",
                "action": action,
                "target": target_value or None,
                "reason": reason,
                "next_action": "choose a declared local action or replan with owner approval",
            }
            self.state["events"].append(refusal)
            self.record_decision(
                self.state.get("current_phase") or "run",
                f"refused action {action}",
                reason,
                [f"scope:{action}", f"target:{target_value or '<none>'}"],
                "blocked",
            )
            raise ScopeRefusal(reason, refusal)
        allowed = {"at": _now(), "status": "allowed", "action": action, "target": target_value or None}
        self._event("action_allowed", action=action, target=target_value or None)
        self._persist()
        return allowed

    def export_audit(self, audit_path: str | os.PathLike[str]) -> Path:
        path, _ = _state_path(audit_path, self.workspace_root)
        rows = [["ts", "phase", "decision", "why", "evidence", "result"]]
        for item in self.state["decisions"]:
            rows.append([
                item["at"],
                item["phase"],
                item["decision"],
                item["why"],
                "; ".join(item["evidence"]),
                item["result"],
            ])
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
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise PlanError(f"JSON file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"JSON file is invalid: {path}") from exc


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
            replacement = json.loads(args.replacement) if args.replacement else None
            result = workflow.replan(args.reason, args.evidence, args.next_action, replacement)
        elif args.command == "interrupt":
            result = workflow.interrupt(args.reason)
        elif args.command == "block":
            result = workflow.block(args.reason, args.evidence, args.next_action)
        elif args.command == "predicate":
            result = workflow.record_predicate(args.check_id, args.passed, args.evidence, args.observed or "")
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
    except WorkflowError as exc:
        error = {"status": "refused" if isinstance(exc, ScopeRefusal) else "error", "error": str(exc)}
        if isinstance(exc, ScopeRefusal):
            error["refusal"] = exc.refusal
        print(json.dumps(error, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
