#!/usr/bin/env python3
"""Run a bounded, data-only reflect contract fixture.

The helper exercises the Codex port without spawning agents or applying edits.
It reads one explicitly supplied transcript and either a delegation manifest or
one reviewer packet for each lens. Transcript, reviewer, and manifest fields
remain untrusted data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ALLOWED_TRANSCRIPT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log"}
MAX_FILE_BYTES = 1_000_000
MAX_RECORDS = 64
MIN_FINDINGS = 3
MAX_FINDINGS = 5
MAX_WINDOW_DAYS = 31
MAX_TEXT_LENGTH = 400
MAX_SCOPE_LENGTH = 120
MAX_PATH_LENGTH = 512
MAX_SOURCE_LENGTH = 320
LENSES = ("judgment", "tooling", "divergent")
COMPLETE_STATES = {"complete", "completed", "done"}
LANE_STATUSES = {"complete", "unavailable", "inconclusive"}
EVIDENCE_STATES = {"fact", "inference", "unknown"}
CAPABILITY_FIELDS = ("reviewer_delegation", "parallel", "synthesizer")
DELEGATION_SCHEMA = "reflect-delegation.v1"
SHARED_CRITERIA = (
    "durability",
    "specificity",
    "decision_changing",
    "evidence",
    "routing",
)
STRUCTURAL_ENFORCEMENT = {
    "automation",
    "lint",
    "metadata",
    "runtime-check",
    "schema",
    "script",
    "test",
}
JUDGMENT_ENFORCEMENT = {"judgment", "prose"}
MECHANISM_MARKERS = (
    "lint rule",
    "metadata flag",
    "runtime check",
    "runtime guard",
    "deterministic script",
    "schema validation",
    "automation",
    "write a test",
    "add a test",
)
ROUTE_RE = re.compile(
    r"^(?:skills/[A-Za-z0-9][A-Za-z0-9._-]*/SKILL\.md|scripts/[A-Za-z0-9][A-Za-z0-9._-]*\.py)$"
)
PORT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class ReflectError(ValueError):
    """A bounded input or contract error that should not produce a traceback."""


@dataclass(frozen=True)
class Evidence:
    check: str | None
    source: str | None
    state: str
    label: str


@dataclass(frozen=True)
class Decision:
    record_id: str
    timestamp: datetime
    state: str
    claim: str
    required_checks: tuple[str, ...]
    completed_checks: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    schema_gaps: tuple[str, ...]
    source: Path


@dataclass(frozen=True)
class ReviewerFinding:
    finding_id: str
    lens: str
    problem: str
    proposal: str
    routing: str
    evidence: tuple[str, ...]
    enforcement: str
    criteria: dict[str, int | None]


@dataclass(frozen=True)
class ReviewerLane:
    lens: str
    status: str
    packet_path: Path | None
    findings: tuple[ReviewerFinding, ...]
    agent_id: str
    model: str
    reason: str


@dataclass(frozen=True)
class Delegation:
    run_id: str
    capabilities: dict[str, bool]
    execution_mode: str
    reviewer_independence: str
    lanes: tuple[ReviewerLane, ...]
    synthesizer_status: str
    synthesizer_agent_id: str
    synthesizer_model: str
    synthesizer_reason: str


def clean_text(value: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Keep untrusted strings bounded, single-line, and free of controls."""

    text = value.replace("\r", " ").replace("\n", " ")
    text = "".join(
        char
        for char in text
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"}
    )
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def validate_metadata(value: Any, field: str, *, limit: int) -> str:
    """Validate a value before it is used as scope, path, or output metadata."""

    if not isinstance(value, str) or not value.strip():
        raise ReflectError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ReflectError(f"{field} exceeds {limit} characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in value):
        raise ReflectError(f"{field} contains an unsafe control or separator")
    return value.strip()


def validate_token(value: Any, field: str, *, limit: int = 120) -> str:
    candidate = validate_metadata(value, field, limit=limit)
    if not TOKEN_RE.fullmatch(candidate):
        raise ReflectError(f"{field} must be a simple bounded identifier")
    return candidate


def quoted(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Quote a bounded value so data cannot become Markdown structure."""

    text = clean_text(str(value), limit=limit)
    return json.dumps(text, ensure_ascii=True)


def parse_timestamp(value: Any, field: str) -> datetime:
    candidate = validate_metadata(value, field, limit=64)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReflectError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReflectError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_path_input(raw_path: Any, label: str) -> str:
    value = validate_metadata(raw_path, f"{label} path", limit=MAX_PATH_LENGTH)
    if value.startswith("~"):
        raise ReflectError(f"{label} path must not use home-directory expansion")
    if any(part == ".." for part in Path(value).parts):
        raise ReflectError(f"{label} path traversal is not allowed")
    return value


def canonical_workspace(raw_workspace: str) -> Path:
    value = validate_path_input(raw_workspace, "workspace")
    try:
        workspace = Path(value).resolve(strict=True)
    except OSError as exc:
        raise ReflectError("workspace cannot be resolved") from exc
    if not workspace.is_dir():
        raise ReflectError("workspace is not a directory")
    validate_metadata(workspace.name, "workspace filename", limit=MAX_SCOPE_LENGTH)
    return workspace


def resolve_file(workspace: Path, raw_path: str, *, label: str) -> Path:
    value = validate_path_input(raw_path, label)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReflectError(f"{label} cannot be resolved") from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ReflectError(f"{label} is outside workspace after canonical resolution") from exc
    validate_metadata(resolved.name, f"{label} filename", limit=MAX_SCOPE_LENGTH)
    try:
        info = resolved.stat()
    except OSError as exc:
        raise ReflectError(f"{label} cannot be inspected") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReflectError(f"{label} is not a regular file")
    if info.st_size > MAX_FILE_BYTES:
        raise ReflectError(f"{label} exceeds {MAX_FILE_BYTES} bytes")
    if resolved.suffix.lower() not in ALLOWED_TRANSCRIPT_SUFFIXES:
        raise ReflectError(f"unsupported {label} type {resolved.suffix or '<none>'!r}")
    return resolved


def read_text(path: Path, *, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ReflectError(f"{label} is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise ReflectError(f"{label} exceeds {MAX_FILE_BYTES} bytes")
        payload = bytearray()
        while len(payload) <= MAX_FILE_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_FILE_BYTES:
                raise ReflectError(f"{label} exceeds {MAX_FILE_BYTES} bytes")
        return bytes(payload).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReflectError(f"cannot read {label}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def payload_records(payload: Any, *, source: str) -> list[Any]:
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
        if not isinstance(records, list):
            raise ReflectError(f"{source}: records must be a list")
        return records
    if isinstance(payload, list):
        return payload
    return [payload]


def load_json_records(path: Path, *, label: str) -> list[Any]:
    text = read_text(path, label=label)
    try:
        if path.suffix.lower() == ".jsonl":
            records: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ReflectError(f"{label}:{line_number} is not valid JSON") from exc
            return records
        return payload_records(json.loads(text), source=label)
    except json.JSONDecodeError as exc:
        raise ReflectError(f"{label} is not valid JSON") from exc


def string_list(raw: Any, *, field: str, required: bool = False) -> tuple[str, ...]:
    if raw is None:
        if required:
            raise ReflectError(f"{field} must be a list of at most 16 strings")
        return ()
    if not isinstance(raw, list) or len(raw) > 16:
        raise ReflectError(f"{field} must be a list of at most 16 strings")
    if not all(isinstance(item, str) and item.strip() for item in raw):
        raise ReflectError(f"{field} must contain non-empty strings")
    return tuple(clean_text(item) for item in raw)


def parse_evidence(raw: Any, *, field: str) -> tuple[tuple[Evidence, ...], list[str]]:
    if raw is None:
        return (), []
    if not isinstance(raw, list) or len(raw) > 16:
        raise ReflectError(f"{field} must be a list of at most 16 evidence entries")
    evidence: list[Evidence] = []
    gaps: list[str] = []
    for index, item in enumerate(raw):
        entry = f"{field}[{index}]"
        if isinstance(item, str) and item.strip():
            evidence.append(Evidence(None, None, "unknown", clean_text(item)))
            gaps.append(f"{entry} is not a source-bound evidence object")
            continue
        if not isinstance(item, dict):
            raise ReflectError(f"{entry} must be a string or object")
        check = item.get("check")
        source = item.get("source", item.get("ref"))
        state = item.get("state")
        if not isinstance(check, str) or not check.strip():
            gaps.append(f"{entry}.check is required")
            check_value = None
        else:
            check_value = clean_text(check)
        if source is None:
            gaps.append(f"{entry}.source is required")
            source_value = None
        else:
            source_value = validate_metadata(source, f"{entry}.source", limit=MAX_SOURCE_LENGTH)
        if state not in EVIDENCE_STATES:
            gaps.append(f"{entry}.state must be fact, inference, or unknown")
            state_value = "unknown"
        else:
            state_value = state
        evidence.append(
            Evidence(
                check=check_value,
                source=source_value,
                state=state_value,
                label=clean_text(json.dumps(item, sort_keys=True)),
            )
        )
    return tuple(evidence), gaps


def normalize_decision(
    raw: Any,
    *,
    source: Path,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> Decision | None:
    """Filter scope before reading fields unrelated to the requested topic."""

    if not isinstance(raw, dict):
        raise ReflectError(f"{source}: each transcript record must be an object")

    if raw.get("workspace") != workspace_id or raw.get("topic") != topic:
        return None

    record_id = raw.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ReflectError(f"{source}: in-scope record id is required")
    record_id = validate_metadata(record_id, f"{source} record id", limit=120)
    timestamp = parse_timestamp(raw.get("timestamp"), f"{source}#{record_id}.timestamp")
    if not start <= timestamp <= as_of:
        return None

    kind = raw.get("kind", "decision")
    if kind != "decision":
        return None
    state = raw.get("state")
    claim = raw.get("claim")
    if not isinstance(state, str) or not state.strip():
        raise ReflectError(f"{source}#{record_id}: state is required")
    if not isinstance(claim, str) or not claim.strip():
        raise ReflectError(f"{source}#{record_id}: claim is required")

    state_value = clean_text(state)
    complete = state_value.casefold() in COMPLETE_STATES
    schema_gaps: list[str] = []
    for field in ("required_checks", "completed_checks", "evidence"):
        if complete and field not in raw:
            schema_gaps.append(f"{field} is required for a completion claim")

    required_checks = string_list(
        raw.get("required_checks"),
        field=f"{source}#{record_id}.required_checks",
    )
    completed_checks = string_list(
        raw.get("completed_checks"),
        field=f"{source}#{record_id}.completed_checks",
    )
    evidence, evidence_gaps = parse_evidence(
        raw.get("evidence"),
        field=f"{source}#{record_id}.evidence",
    )
    schema_gaps.extend(evidence_gaps if complete else [])
    if complete and not required_checks:
        schema_gaps.append("required_checks must contain at least one check for a completion claim")
    if complete and any(check.casefold() not in {item.casefold() for item in required_checks} for check in completed_checks):
        schema_gaps.append("completed_checks contains a check absent from required_checks")

    return Decision(
        record_id=record_id,
        timestamp=timestamp,
        state=state_value,
        claim=clean_text(claim),
        required_checks=required_checks,
        completed_checks=completed_checks,
        evidence=evidence,
        schema_gaps=tuple(unique(schema_gaps)),
        source=source,
    )


def load_decisions(
    workspace: Path,
    transcript_path: str,
    *,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> tuple[list[Decision], bool, Path]:
    path = resolve_file(workspace, transcript_path, label="transcript")
    if path.suffix.lower() in {".md", ".txt", ".log"}:
        read_text(path, label="opaque transcript")
        return [], True, path

    raw_records = load_json_records(path, label="transcript")
    if len(raw_records) > MAX_RECORDS:
        raise ReflectError(f"transcript has more than {MAX_RECORDS} records")
    decisions: list[Decision] = []
    seen_ids: set[str] = set()
    for raw in raw_records:
        decision = normalize_decision(
            raw,
            source=path,
            workspace_id=workspace_id,
            topic=topic,
            start=start,
            as_of=as_of,
        )
        if decision is None:
            continue
        if decision.record_id in seen_ids:
            raise ReflectError(f"duplicate in-scope transcript id {decision.record_id!r}")
        seen_ids.add(decision.record_id)
        decisions.append(decision)
    decisions.sort(key=lambda item: (item.timestamp, item.record_id), reverse=True)
    return decisions, False, path


def validate_route(raw_route: Any) -> str:
    route = validate_metadata(raw_route, "reviewer routing", limit=200)
    if not ROUTE_RE.fullmatch(route):
        raise ReflectError(
            "reviewer routing must be an allowlisted existing skills/<name>/SKILL.md or scripts/<name>.py label"
        )
    if not (PORT_ROOT / route).is_file():
        raise ReflectError(f"reviewer routing is not an existing allowlisted route: {route!r}")
    return route


def parse_criteria(raw: Any, *, field: str) -> dict[str, int | None]:
    criteria = {name: None for name in SHARED_CRITERIA}
    if raw is None:
        return criteria
    if not isinstance(raw, dict) or len(raw) > len(SHARED_CRITERIA):
        raise ReflectError(f"{field} must be a bounded object with shared criteria")
    unknown = set(raw).difference(SHARED_CRITERIA)
    if unknown:
        raise ReflectError(f"{field} has unsupported criteria: {', '.join(sorted(unknown))}")
    for name, value in raw.items():
        if value == "unknown":
            criteria[name] = None
        elif isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2:
            criteria[name] = value
        else:
            raise ReflectError(f"{field}.{name} must be 0, 1, 2, or 'unknown'")
    return criteria


def identity_value(raw: Any, field: str) -> str:
    if raw is None:
        return "unreported"
    return validate_metadata(raw, field, limit=120)


def load_reviewer_packet(
    workspace: Path,
    raw_path: str,
    *,
    expected_lens: str | None = None,
    status_override: str | None = None,
    agent_id_override: str | None = None,
    model_override: str | None = None,
    reason_override: str | None = None,
) -> tuple[str, Path, tuple[ReviewerFinding, ...], str, str, str, str]:
    path = resolve_file(workspace, raw_path, label="reviewer packet")
    if path.suffix.lower() != ".json":
        raise ReflectError("reviewer packets must use the .json suffix")
    payload = load_json_records(path, label="reviewer packet")
    if len(payload) != 1 or not isinstance(payload[0], dict):
        raise ReflectError(f"{path}: reviewer packet must be one JSON object")
    packet = payload[0]
    lens = packet.get("lens")
    if lens not in LENSES:
        raise ReflectError(f"{path}: lens must be judgment, tooling, or divergent")
    if expected_lens is not None and lens != expected_lens:
        raise ReflectError(f"{path}: packet lens does not match manifest lens {expected_lens!r}")

    status = status_override or packet.get("status", "complete")
    if status not in LANE_STATUSES:
        raise ReflectError(f"{path}: status must be complete, unavailable, or inconclusive")
    agent_id = identity_value(
        agent_id_override if agent_id_override is not None else packet.get("agent_id"),
        f"{path} agent_id",
    )
    model = identity_value(
        model_override if model_override is not None else packet.get("model"),
        f"{path} model",
    )
    reason_raw = reason_override if reason_override is not None else packet.get("reason")
    reason = ""
    if status != "complete":
        reason = validate_metadata(
            reason_raw or "lane did not produce a usable reviewer result",
            f"{path} reason",
            limit=MAX_TEXT_LENGTH,
        )

    raw_findings = packet.get("findings", [])
    if not isinstance(raw_findings, list):
        raise ReflectError(f"{path}: findings must be a list")
    if status != "complete":
        if raw_findings:
            raise ReflectError(f"{path}: unavailable or inconclusive packets cannot contain findings")
        return lens, path, (), status, agent_id, model, reason
    if not MIN_FINDINGS <= len(raw_findings) <= MAX_FINDINGS:
        raise ReflectError(
            f"{path}: a complete {lens} packet must contain {MIN_FINDINGS} to {MAX_FINDINGS} findings"
        )

    findings: list[ReviewerFinding] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            raise ReflectError(f"{path}: finding {index} must be an object")
        values = {field: raw.get(field) for field in ("id", "problem", "proposal", "routing")}
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise ReflectError(f"{path}: finding {index} needs id, problem, proposal, and routing")
        finding_id = validate_token(values["id"], f"{path} finding {index} id")
        if finding_id in seen_ids:
            raise ReflectError(f"{path}: duplicate finding id {finding_id!r}")
        seen_ids.add(finding_id)
        evidence = string_list(raw.get("evidence"), field=f"{path}#{finding_id}.evidence", required=True)
        enforcement = raw.get("enforcement", "judgment")
        if not isinstance(enforcement, str) or not enforcement.strip():
            raise ReflectError(f"{path}#{finding_id}: enforcement is required")
        enforcement = clean_text(enforcement, limit=80).casefold()
        if enforcement not in JUDGMENT_ENFORCEMENT | STRUCTURAL_ENFORCEMENT:
            raise ReflectError(f"{path}#{finding_id}: unsupported enforcement category")
        findings.append(
            ReviewerFinding(
                finding_id=finding_id,
                lens=lens,
                problem=clean_text(values["problem"]),
                proposal=clean_text(values["proposal"]),
                routing=validate_route(values["routing"]),
                evidence=evidence,
                enforcement=enforcement,
                criteria=parse_criteria(raw.get("criteria"), field=f"{path}#{finding_id}.criteria"),
            )
        )
    return lens, path, tuple(findings), status, agent_id, model, reason


def normalize_capabilities(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        raise ReflectError("delegation capabilities must be an object")
    missing = [field for field in CAPABILITY_FIELDS if field not in raw]
    if missing:
        raise ReflectError("delegation capabilities are missing: " + ", ".join(missing))
    if not all(isinstance(raw[field], bool) for field in CAPABILITY_FIELDS):
        raise ReflectError("delegation capabilities must use boolean values")
    return {field: raw[field] for field in CAPABILITY_FIELDS}


def reviewer_independence(lanes: Iterable[ReviewerLane]) -> str:
    usable = [lane for lane in lanes if lane.status == "complete"]
    packet_paths = [lane.packet_path for lane in usable]
    if len(packet_paths) != len({path for path in packet_paths if path is not None}):
        return "failed"
    identities = [lane.agent_id for lane in usable if lane.agent_id != "unreported"]
    if len(identities) != len(set(identities)):
        return "failed"
    if len(usable) != len(LENSES) or len(identities) != len(LENSES):
        return "inconclusive"
    return "verified"


def ensure_unique_finding_ids(lanes: Iterable[ReviewerLane]) -> None:
    seen: dict[str, str] = {}
    for lane in lanes:
        for finding in lane.findings:
            previous_lens = seen.get(finding.finding_id)
            if previous_lens is not None:
                raise ReflectError(
                    f"reviewer finding id {finding.finding_id!r} is duplicated by {previous_lens} and {lane.lens}"
                )
            seen[finding.finding_id] = lane.lens


def synth_identity(raw: Any, field: str) -> tuple[str, str, str, str]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ReflectError(f"{field} must be an object")
    status = raw.get("status", "complete")
    if status not in LANE_STATUSES:
        raise ReflectError(f"{field}.status must be complete, unavailable, or inconclusive")
    agent_id = identity_value(raw.get("agent_id"), f"{field}.agent_id")
    model = identity_value(raw.get("model"), f"{field}.model")
    reason = ""
    if status != "complete":
        reason = validate_metadata(
            raw.get("reason") or "synthesizer did not produce a usable result",
            f"{field}.reason",
            limit=MAX_TEXT_LENGTH,
        )
    return status, agent_id, model, reason


def load_delegation_manifest(workspace: Path, raw_path: str) -> Delegation:
    path = resolve_file(workspace, raw_path, label="delegation manifest")
    if path.suffix.lower() != ".json":
        raise ReflectError("delegation manifests must use the .json suffix")
    payloads = load_json_records(path, label="delegation manifest")
    if len(payloads) != 1 or not isinstance(payloads[0], dict):
        raise ReflectError(f"{path}: delegation manifest must be one JSON object")
    payload = payloads[0]
    if payload.get("schema") != DELEGATION_SCHEMA:
        raise ReflectError(f"{path}: schema must be {DELEGATION_SCHEMA}")
    run_id = validate_token(payload.get("run_id"), f"{path} run_id")
    capabilities = normalize_capabilities(payload.get("capabilities"))
    raw_reviewers = payload.get("reviewers")
    if not isinstance(raw_reviewers, list) or len(raw_reviewers) != len(LENSES):
        raise ReflectError(f"{path}: reviewers must contain exactly one entry for each lens")

    lanes: list[ReviewerLane] = []
    seen_lenses: set[str] = set()
    for index, raw_lane in enumerate(raw_reviewers):
        if not isinstance(raw_lane, dict):
            raise ReflectError(f"{path}: reviewer entry {index} must be an object")
        lens = raw_lane.get("lens")
        if lens not in LENSES or lens in seen_lenses:
            raise ReflectError(f"{path}: reviewer entries must have unique known lenses")
        seen_lenses.add(lens)
        status = raw_lane.get("status", "complete")
        if status not in LANE_STATUSES:
            raise ReflectError(f"{path}#{lens}.status is invalid")
        agent_id = identity_value(raw_lane.get("agent_id"), f"{path}#{lens}.agent_id")
        model = identity_value(raw_lane.get("model"), f"{path}#{lens}.model")
        if status == "complete":
            packet = raw_lane.get("packet")
            if not isinstance(packet, str) or not packet.strip():
                raise ReflectError(f"{path}#{lens}.packet is required for a complete lane")
            packet_lens, packet_path, findings, packet_status, _, _, packet_reason = load_reviewer_packet(
                workspace,
                packet,
                expected_lens=lens,
                status_override=status,
                agent_id_override=agent_id,
                model_override=model,
            )
            if packet_status != "complete" or packet_lens != lens:
                raise ReflectError(f"{path}#{lens}: complete manifest lane did not load a complete packet")
            lanes.append(ReviewerLane(lens, status, packet_path, findings, agent_id, model, packet_reason))
        else:
            reason = validate_metadata(
                raw_lane.get("reason") or "reviewer lane was unavailable",
                f"{path}#{lens}.reason",
                limit=MAX_TEXT_LENGTH,
            )
            if raw_lane.get("packet") is not None:
                raise ReflectError(f"{path}#{lens}: unavailable lanes must not include a packet")
            lanes.append(ReviewerLane(lens, status, None, (), agent_id, model, reason))
    if set(seen_lenses) != set(LENSES):
        raise ReflectError(f"{path}: one reviewer entry is missing")
    ensure_unique_finding_ids(lanes)

    synth_status, synth_agent, synth_model, synth_reason = synth_identity(payload.get("synthesizer"), f"{path}#synthesizer")
    if not capabilities["synthesizer"]:
        synth_status = "unavailable"
        synth_reason = "Codex synthesizer capability was unavailable"
    execution_mode = "parallel" if capabilities["reviewer_delegation"] and capabilities["parallel"] else "sequential-fallback"
    return Delegation(
        run_id=run_id,
        capabilities=capabilities,
        execution_mode=execution_mode,
        reviewer_independence=reviewer_independence(lanes),
        lanes=tuple(sorted(lanes, key=lambda lane: LENSES.index(lane.lens))),
        synthesizer_status=synth_status,
        synthesizer_agent_id=synth_agent,
        synthesizer_model=synth_model,
        synthesizer_reason=synth_reason,
    )


def load_delegation(workspace: Path, raw_paths: list[str], manifest_path: str | None) -> Delegation:
    if manifest_path and raw_paths:
        raise ReflectError("use either --delegation-manifest or --reviewer, not both")
    if manifest_path:
        return load_delegation_manifest(workspace, manifest_path)

    if raw_paths:
        if len(raw_paths) != len(LENSES):
            raise ReflectError("supply exactly one reviewer packet for each lens, or use a delegation manifest")
        lanes: list[ReviewerLane] = []
        seen_lenses: set[str] = set()
        for raw_path in raw_paths:
            lens, path, findings, status, agent_id, model, reason = load_reviewer_packet(workspace, raw_path)
            if lens in seen_lenses:
                raise ReflectError(f"duplicate reviewer lens {lens!r}")
            seen_lenses.add(lens)
            lanes.append(ReviewerLane(lens, status, path, findings, agent_id, model, reason))
        missing = [lens for lens in LENSES if lens not in seen_lenses]
        if missing:
            raise ReflectError(f"missing reviewer lens: {', '.join(missing)}")
        lanes.sort(key=lambda lane: LENSES.index(lane.lens))
        ensure_unique_finding_ids(lanes)
        status = "complete" if all(lane.status == "complete" for lane in lanes) else "inconclusive"
        return Delegation(
            run_id="direct-fixture",
            capabilities={"reviewer_delegation": False, "parallel": False, "synthesizer": True},
            execution_mode="sequential-fallback",
            reviewer_independence=reviewer_independence(lanes),
            lanes=tuple(lanes),
            synthesizer_status=status,
            synthesizer_agent_id="local-fixture-synthesizer",
            synthesizer_model="unreported",
            synthesizer_reason="reviewer lanes were not all complete" if status != "complete" else "",
        )

    lanes = tuple(
        ReviewerLane(
            lens=lens,
            status="unavailable",
            packet_path=None,
            findings=(),
            agent_id="unreported",
            model="unreported",
            reason="no reviewer packet or delegation capability was supplied",
        )
        for lens in LENSES
    )
    return Delegation(
        run_id="no-delegation",
        capabilities={"reviewer_delegation": False, "parallel": False, "synthesizer": False},
        execution_mode="sequential-fallback",
        reviewer_independence="inconclusive",
        lanes=lanes,
        synthesizer_status="unavailable",
        synthesizer_agent_id="unreported",
        synthesizer_model="unreported",
        synthesizer_reason="no synthesizer capability was supplied",
    )


def load_reviewers(workspace: Path, raw_paths: list[str]) -> tuple[dict[str, list[ReviewerFinding]], dict[str, Path]]:
    """Compatibility wrapper for callers that only need the direct fixture packets."""

    delegation = load_delegation(workspace, raw_paths, None)
    return (
        {lane.lens: list(lane.findings) for lane in delegation.lanes if lane.status == "complete"},
        {lane.lens: lane.packet_path for lane in delegation.lanes if lane.packet_path is not None},
    )


def source_ref(decision: Decision, workspace: Path) -> str:
    try:
        relative = decision.source.relative_to(workspace)
    except ValueError:
        relative = Path(decision.source.name)
    return f"{relative.as_posix()}#{decision.record_id}"


def missing_checks(decision: Decision) -> list[str]:
    completed = {item.casefold() for item in decision.completed_checks}
    return [item for item in decision.required_checks if item.casefold() not in completed]


def inspect_decisions(decisions: list[Decision], workspace: Path) -> dict[str, list[str]]:
    observed: list[str] = []
    facts: list[str] = []
    inferences: list[str] = []
    unknowns: list[str] = []
    gaps: list[str] = []
    known_refs = {source_ref(decision, workspace) for decision in decisions}
    for decision in decisions:
        reference = source_ref(decision, workspace)
        observed.append(
            f"[observed] {quoted(reference)} reports state {quoted(decision.state)}: {quoted(decision.claim)}."
        )
        if decision.schema_gaps:
            for schema_gap in decision.schema_gaps:
                unknowns.append(f"[unknown] {quoted(reference)} has incomplete decision schema: {quoted(schema_gap)}.")
                gaps.append(f"[gap] {quoted(reference)} cannot establish completion: {quoted(schema_gap)}.")

        evidence_by_check: dict[str, list[Evidence]] = {}
        for item in decision.evidence:
            if item.source is None or item.source not in known_refs or item.check is None:
                unknowns.append(
                    f"[unknown] {quoted(reference)} cites unbound evidence {quoted(item.label)}; no source-backed check was accepted."
                )
                continue
            evidence_by_check.setdefault(item.check.casefold(), []).append(item)
            line = (
                f"[{item.state}] {quoted(reference)} binds check {quoted(item.check)} "
                f"to source {quoted(item.source)}."
            )
            if item.state == "fact":
                facts.append(line)
            elif item.state == "inference":
                inferences.append(line)
            else:
                unknowns.append(line)

        missing = missing_checks(decision)
        if missing:
            checks = ", ".join(quoted(item) for item in missing)
            unknowns.append(
                f"[unknown] {quoted(reference)} does not establish completion; required checks not evidenced: {checks}."
            )
            gaps.append(f"[gap] {quoted(reference)} leaves required checks skipped or unverified: {checks}.")
            if decision.state.casefold() in COMPLETE_STATES:
                gaps.append(
                    f"[gap] False completion signal: {quoted(reference)} claims {quoted(decision.state)} while checks remain missing."
                )

        if decision.state.casefold() in COMPLETE_STATES:
            for check in decision.required_checks:
                entries = evidence_by_check.get(check.casefold(), [])
                source_facts = [item for item in entries if item.state == "fact"]
                if not entries:
                    unknowns.append(
                        f"[unknown] {quoted(reference)} has no source-bound evidence for check {quoted(check)}."
                    )
                    gaps.append(f"[gap] Completion check {quoted(check)} has no evidence reference.")
                elif not source_facts:
                    unknowns.append(
                        f"[unknown] {quoted(reference)} has no fact-state evidence for check {quoted(check)}."
                    )
                    gaps.append(f"[gap] Completion check {quoted(check)} is supported only by inference or unknown evidence.")
            if not missing and not decision.schema_gaps and all(
                any(item.state == "fact" for item in evidence_by_check.get(check.casefold(), []))
                for check in decision.required_checks
            ):
                facts.append(f"[fact] {quoted(reference)} has source-backed evidence for every required check.")
        elif not decision.evidence:
            unknowns.append(f"[unknown] {quoted(reference)} has no supplied evidence list.")

    if not decisions:
        unknowns.append("[unknown] No structured decision record matched the requested scope.")
    return {
        "observed": unique(observed),
        "facts": unique(facts),
        "inferences": unique(inferences),
        "unknowns": unique(unknowns),
        "gaps": unique(gaps),
    }


def unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def classify_candidate(item: dict[str, Any]) -> tuple[str, str | None]:
    proposal = item["proposal"].casefold()
    enforcement = set(item["enforcement_categories"])
    if enforcement.intersection(STRUCTURAL_ENFORCEMENT):
        return "backlog", "at least one contributing finding requires structural enforcement"
    if any(marker in proposal for marker in MECHANISM_MARKERS):
        return "backlog", "proposal names a deterministic enforcement mechanism"
    return "accepted", None


def merge_criteria(current: dict[str, int | None], incoming: dict[str, int | None]) -> dict[str, int | None]:
    merged: dict[str, int | None] = {}
    for name in SHARED_CRITERIA:
        if current[name] is None:
            merged[name] = incoming[name]
        elif incoming[name] is None or current[name] == incoming[name]:
            merged[name] = current[name]
        else:
            merged[name] = None
    return merged


def score_candidate(item: dict[str, Any]) -> tuple[dict[str, int | None], tuple[int, int, int, int]]:
    criteria = dict(item["criteria"])
    if criteria["evidence"] is None:
        criteria["evidence"] = 2 if item["evidence"] else 0
    if criteria["routing"] is None:
        criteria["routing"] = 2
    known = sum(value is not None for value in criteria.values())
    total = sum(value or 0 for value in criteria.values())
    rank = (total, known, len(item["evidence"]), len(item["lenses"]))
    return criteria, rank


def compare_candidates(problem: str, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    for candidate in candidates:
        candidate["criteria"], candidate["rank"] = score_candidate(candidate)
    if len(candidates) == 1:
        selected = candidates[0]
        return (
            {
                "problem": problem,
                "candidate_ids": [selected["id"]],
                "criteria": list(SHARED_CRITERIA),
                "scores": {selected["id"]: {**selected["criteria"], "rank": selected["rank"]}},
                "selected_id": selected["id"],
                "status": "selected",
                "rationale": "only bounded candidate for this problem",
                "rejected_alternatives": [],
            },
            [],
        )

    top_rank = max(candidate["rank"] for candidate in candidates)
    top = [candidate for candidate in candidates if candidate["rank"] == top_rank]
    scores = {
        candidate["id"]: {**candidate["criteria"], "rank": candidate["rank"]}
        for candidate in candidates
    }
    if len(top) != 1:
        rejected = [
            {
                "id": candidate["id"],
                "reason": "comparison inconclusive",
                "detail": "shared criteria produced a tied bounded score; no candidate was selected",
            }
            for candidate in candidates
        ]
        return (
            {
                "problem": problem,
                "candidate_ids": [candidate["id"] for candidate in candidates],
                "criteria": list(SHARED_CRITERIA),
                "scores": scores,
                "selected_id": None,
                "status": "inconclusive",
                "rationale": "shared criteria tied; preserve the unresolved choice as unknown",
                "rejected_alternatives": rejected,
            },
            rejected,
        )

    selected = top[0]
    rejected = [
        {
            "id": candidate["id"],
            "reason": "rejected alternative",
            "detail": f"bounded score {candidate['rank']} is below selected candidate {selected['id']} at {selected['rank']}",
        }
        for candidate in candidates
        if candidate is not selected
    ]
    return (
        {
            "problem": problem,
            "candidate_ids": [candidate["id"] for candidate in candidates],
            "criteria": list(SHARED_CRITERIA),
            "scores": scores,
            "selected_id": selected["id"],
            "status": "selected",
            "rationale": f"selected the unique highest bounded score {selected['rank']}",
            "rejected_alternatives": rejected,
        },
        rejected,
    )


def synthesize(
    packets: dict[str, list[ReviewerFinding]],
    decisions: list[Decision],
    *,
    delegation: Delegation | None = None,
    comparison_owner: str = "reflect",
    workspace: Path | None = None,
) -> dict[str, Any]:
    if delegation is None:
        lanes = tuple(
            ReviewerLane(lens, "complete", None, tuple(packets.get(lens, [])), "unreported", "unreported", "")
            for lens in LENSES
        )
        delegation = Delegation(
            run_id="direct-synthesis",
            capabilities={"reviewer_delegation": False, "parallel": False, "synthesizer": True},
            execution_mode="sequential-fallback",
            reviewer_independence="inconclusive",
            lanes=lanes,
            synthesizer_status="complete",
            synthesizer_agent_id="unreported",
            synthesizer_model="unreported",
            synthesizer_reason="",
        )

    all_complete = all(lane.status == "complete" for lane in delegation.lanes)
    synthesis_status = delegation.synthesizer_status if all_complete else "inconclusive"
    base_trail: dict[str, Any] = {
        "trail_id": f"reflect:{delegation.run_id}",
        "status": synthesis_status,
        "comparison_owner": comparison_owner,
        "comparison_route": "skills/arena/SKILL.md" if comparison_owner == "arena" else "local reflect synthesizer",
        "criteria": list(SHARED_CRITERIA),
        "selected_decisions": [],
        "rejected_alternatives": [],
        "source_references": [
            source_ref(decision, workspace or Path(".")) for decision in decisions
        ],
    }
    empty = {
        "status": synthesis_status,
        "accepted": [],
        "rejected": [],
        "backlog": [],
        "comparisons": [],
        "decision_trail": base_trail,
    }
    if synthesis_status != "complete" or delegation.reviewer_independence == "failed":
        if delegation.reviewer_independence == "failed" and synthesis_status == "complete":
            synthesis_status = "inconclusive"
            empty["status"] = synthesis_status
            base_trail["status"] = synthesis_status
        base_trail["selected_decision"] = "none (inconclusive)"
        base_trail["reason"] = (
            delegation.synthesizer_reason
            or "all reviewer lenses were not complete and independently identified"
        )
        return empty

    if comparison_owner == "arena":
        base_trail["selected_decision"] = "none (arena result unavailable)"
        base_trail["reason"] = "candidate comparison is delegated to the allowlisted arena route"
        return empty

    record_ids = {decision.record_id for decision in decisions}
    grouped: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    rejected: list[dict[str, Any]] = []
    for lens in LENSES:
        for finding in packets.get(lens, []):
            evidence = [item for item in finding.evidence if item in record_ids]
            unresolved = [item for item in finding.evidence if item not in record_ids]
            if unresolved or not evidence:
                detail = (
                    f"{finding.lens} finding cites unresolved transcript ids: {', '.join(unresolved)}"
                    if unresolved
                    else f"{finding.lens} finding cites no in-scope transcript id"
                )
                rejected.append(
                    {
                        "id": finding.finding_id,
                        "reason": "unresolved evidence reference",
                        "detail": detail,
                    }
                )
                continue
            problem_key = finding.problem.casefold()
            candidate_key = (finding.proposal.casefold(), finding.routing.casefold())
            item = grouped.setdefault(problem_key, {}).setdefault(
                candidate_key,
                {
                    "id": finding.finding_id,
                    "finding_ids": [],
                    "problem": finding.problem,
                    "proposal": finding.proposal,
                    "routing": finding.routing,
                    "evidence": [],
                    "lenses": [],
                    "enforcement_categories": [],
                    "criteria": {name: None for name in SHARED_CRITERIA},
                },
            )
            item["finding_ids"].append(finding.finding_id)
            item["evidence"].extend(evidence)
            item["lenses"].append(finding.lens)
            item["enforcement_categories"].append(finding.enforcement)
            item["criteria"] = merge_criteria(item["criteria"], finding.criteria)

    accepted: list[dict[str, Any]] = []
    backlog: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    for problem_key, candidate_map in grouped.items():
        candidates = list(candidate_map.values())
        for item in candidates:
            item["evidence"] = unique(item["evidence"])
            item["lenses"] = unique(item["lenses"])
            item["finding_ids"] = unique(item["finding_ids"])
            item["enforcement_categories"] = unique(item["enforcement_categories"])
        problem = candidates[0]["problem"]
        comparison, alternatives = compare_candidates(problem, candidates)
        comparisons.append(comparison)
        rejected.extend(alternatives)
        selected_id = comparison["selected_id"]
        if selected_id is None:
            continue
        selected = next(item for item in candidates if item["id"] == selected_id)
        selected["comparison_status"] = comparison["status"]
        bucket, reason = classify_candidate(selected)
        if bucket == "backlog":
            selected["mechanism"] = reason
            backlog.append(selected)
            outcome = "backlog"
        else:
            accepted.append(selected)
            outcome = "accepted"
        base_trail["selected_decisions"].append(
            {
                "problem": problem,
                "candidate_id": selected["id"],
                "outcome": outcome,
                "finding_ids": selected["finding_ids"],
            }
        )
    base_trail["rejected_alternatives"] = rejected
    base_trail["selected_decision"] = (
        "selected bounded candidates" if base_trail["selected_decisions"] else "none (inconclusive)"
    )
    return {
        "status": synthesis_status,
        "accepted": accepted,
        "rejected": rejected,
        "backlog": backlog,
        "comparisons": comparisons,
        "decision_trail": base_trail,
    }


def render_item(item: dict[str, Any], evidence_refs: dict[str, str]) -> str:
    evidence = ", ".join(quoted(evidence_refs.get(value, value)) for value in item["evidence"])
    lenses = ", ".join(item.get("lenses", []))
    findings = ", ".join(item.get("finding_ids", [item["id"]]))
    return (
        f"- [{item['id']}] problem={quoted(item['problem'])}; proposal={quoted(item['proposal'])}; "
        f"evidence={evidence}; routing={quoted(item['routing'])}; route_is_label=true; "
        f"lenses={quoted(lenses)}; finding_ids={quoted(findings)}; "
        f"enforcement={quoted(', '.join(item.get('enforcement_categories', [])))}."
    )


def render_evidence(decisions: list[Decision], workspace: Path) -> list[str]:
    lines: list[str] = []
    for decision in decisions:
        reference = source_ref(decision, workspace)
        for item in decision.evidence:
            lines.append(
                f"- [{item.state}] decision={quoted(reference)}; check={quoted(item.check or '<unbound>')}; "
                f"source={quoted(item.source or '<unbound>')}."
            )
    return lines


def render_result(
    *,
    workspace: Path,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
    transcript_path: Path,
    opaque: bool,
    decisions: list[Decision],
    delegation: Delegation,
    packets: dict[str, list[ReviewerFinding]],
    synthesis: dict[str, Any],
    approvals: list[str],
) -> str:
    try:
        transcript_ref = transcript_path.relative_to(workspace).as_posix()
    except ValueError:
        transcript_ref = transcript_path.name
    evidence_refs = {
        decision.record_id: source_ref(decision, workspace)
        for decision in decisions
    }
    inspection = inspect_decisions(decisions, workspace)
    lines = [
        "## Active transcript",
        f"- Scope: workspace_id={quoted(workspace_id)}; topic={quoted(topic)}; window={quoted(start.isoformat())} through {quoted(as_of.isoformat())}.",
        f"- Source: {quoted(transcript_ref)}; structured decisions in scope: {len(decisions)}.",
    ]
    if opaque:
        lines.append("- The explicitly supplied transcript was read as opaque data; no structured claims were accepted.")
    lines.extend(["", "## Findings", "### Observed"])
    lines.extend(f"- {item}" for item in inspection["observed"] or ["[observed] No structured decision was supplied."])
    lines.extend(["", "### Source-backed facts"])
    lines.extend(f"- {item}" for item in inspection["facts"] or ["[fact] None established."])
    lines.extend(["", "### Inferences"])
    lines.extend(f"- {item}" for item in inspection["inferences"] or ["[inference] None established."])
    lines.extend(["", "### Uncertainty"])
    lines.extend(f"- {item}" for item in inspection["unknowns"] or ["[unknown] None recorded."])
    lines.extend(["", "### Process/code gaps"])
    lines.extend(f"- {item}" for item in inspection["gaps"] or ["[gap] No supported process or code gap was derived."])
    lines.extend(["", "### Evidence references"])
    lines.extend(render_evidence(decisions, workspace) or ["- None."])

    lines.extend(["", "## Delegation"])
    lines.append(f"- contract={quoted(DELEGATION_SCHEMA)}; run_id={quoted(delegation.run_id)}.")
    capability_text = ", ".join(f"{name}={str(value).lower()}" for name, value in delegation.capabilities.items())
    lines.append(f"- capability_detection: {capability_text}; execution_mode={quoted(delegation.execution_mode)}.")
    lines.append(f"- reviewer_independence={quoted(delegation.reviewer_independence)}; fallback_order={quoted(', '.join(LENSES))}.")
    for lane in delegation.lanes:
        packet = "<none>" if lane.packet_path is None else lane.packet_path.relative_to(workspace).as_posix()
        reason = f"; reason={quoted(lane.reason)}" if lane.reason else ""
        lines.append(
            f"- [{lane.lens}] status={quoted(lane.status)}; agent_id={quoted(lane.agent_id)}; "
            f"model={quoted(lane.model)}; packet={quoted(packet)}{reason}."
        )
    synth_reason = f"; reason={quoted(delegation.synthesizer_reason)}" if delegation.synthesizer_reason else ""
    lines.append(
        f"- [synthesizer] status={quoted(delegation.synthesizer_status)}; "
        f"agent_id={quoted(delegation.synthesizer_agent_id)}; model={quoted(delegation.synthesizer_model)}{synth_reason}."
    )

    lines.extend(["", "## Synthesis", f"- status={quoted(synthesis['status'])}; comparison_owner={quoted(synthesis['decision_trail']['comparison_owner'])}; comparison_route={quoted(synthesis['decision_trail']['comparison_route'])}."])
    lines.extend(["", "## Accepted"])
    if synthesis["accepted"]:
        lines.extend(render_item(item, evidence_refs) for item in synthesis["accepted"])
    else:
        lines.append("- None.")
    lines.extend(["", "## Rejected"])
    if synthesis["rejected"]:
        lines.extend(
            f"- [{item['id']}] reason={quoted(item['reason'])}; detail={quoted(item['detail'])}."
            for item in synthesis["rejected"]
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Backlog"])
    if synthesis["backlog"]:
        lines.extend(
            f"- [{item['id']}] problem={quoted(item['problem'])}; proposal={quoted(item['proposal'])}; "
            f"mechanism={quoted(item['mechanism'])}; evidence={quoted(', '.join(evidence_refs.get(value, value) for value in item['evidence']))}; "
            f"routing={quoted(item['routing'])}; route_is_label=true; "
            f"enforcement={quoted(', '.join(item.get('enforcement_categories', [])))}."
            for item in synthesis["backlog"]
        )
    else:
        lines.append("- None.")

    lines.extend(["", "## Comparisons"])
    if synthesis["comparisons"]:
        for comparison in synthesis["comparisons"]:
            selected = comparison["selected_id"] or "none (inconclusive)"
            lines.append(
                f"- problem={quoted(comparison['problem'])}; candidates={quoted(', '.join(comparison['candidate_ids']))}; "
                f"selected_decision={quoted(selected)}; status={quoted(comparison['status'])}; "
                f"criteria={quoted(', '.join(comparison['criteria']))}; rationale={quoted(comparison['rationale'])}."
            )
    else:
        lines.append("- None. Comparison is unavailable until all required lanes are usable.")

    trail = synthesis["decision_trail"]
    lines.extend(["", "## Decision trail"])
    lines.append(f"- trail_id={quoted(trail['trail_id'])}; status={quoted(trail['status'])}; comparison_owner={quoted(trail['comparison_owner'])}.")
    lines.append(f"- source_references={quoted(', '.join(trail.get('source_references', [])) or 'none')}.")
    lines.append(f"- unresolved_unknowns={quoted('; '.join(inspection['unknowns']) or 'none')}.")
    lines.append(f"- selected_decision={quoted(trail.get('selected_decision', 'none (inconclusive)'))}.")
    if trail["selected_decisions"]:
        for selected in trail["selected_decisions"]:
            lines.append(
                f"- selected candidate={quoted(selected['candidate_id'])}; problem={quoted(selected['problem'])}; "
                f"outcome={quoted(selected['outcome'])}; finding_ids={quoted(', '.join(selected['finding_ids']))}."
            )
    else:
        lines.append("- selected candidates: none; the unresolved choice remains inconclusive.")
    if trail["rejected_alternatives"]:
        lines.append("- rejected alternatives:")
        for item in trail["rejected_alternatives"]:
            lines.append(f"- rejected alternative={item['id']}; reason={quoted(item['reason'])}; detail={quoted(item['detail'])}.")
    else:
        lines.append("- rejected alternatives: none.")
    if trail.get("reason"):
        lines.append(f"- decision_reason={quoted(trail['reason'])}.")

    lines.extend(
        [
            "",
            "## Approval",
            "- approval_required: true",
            f"- approval_received_for: {', '.join(approvals) if approvals else 'none'}",
            "- persistent_edits: 0",
            "- Reviewer routing is an untrusted label. Any destination needs separate user approval and allowlisted re-resolution.",
            "- The fixture emits proposals only; explicit user approval is required before any persistent skill edit.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="bounded workspace root")
    parser.add_argument("--workspace-id", help="record workspace label; defaults to root name")
    parser.add_argument("--topic", required=True, help="one exact topic")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO timestamp")
    parser.add_argument("--transcript", required=True, help="one explicit transcript artifact")
    parser.add_argument("--reviewer", action="append", default=[], help="one reviewer packet per lens")
    parser.add_argument(
        "--delegation-manifest",
        "--delegation",
        dest="delegation_manifest",
        help="one explicit reflect-delegation.v1 manifest",
    )
    parser.add_argument(
        "--comparison-owner",
        choices=("reflect", "arena"),
        default="reflect",
        help="where bounded candidate comparison is performed",
    )
    parser.add_argument("--approve", action="append", default=[], help="explicitly approved finding id")
    parser.add_argument("--apply", action="store_true", help="check the approval gate; never writes files")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.window_days <= MAX_WINDOW_DAYS:
            raise ReflectError(f"window-days must be between 1 and {MAX_WINDOW_DAYS}")
        topic = validate_metadata(args.topic, "topic", limit=MAX_SCOPE_LENGTH)
        workspace = canonical_workspace(args.workspace)
        workspace_id = validate_metadata(args.workspace_id or workspace.name, "workspace-id", limit=MAX_SCOPE_LENGTH)
        as_of = parse_timestamp(args.as_of, "as-of")
        start = as_of - timedelta(days=args.window_days)
        decisions, opaque, transcript_path = load_decisions(
            workspace,
            args.transcript,
            workspace_id=workspace_id,
            topic=topic,
            start=start,
            as_of=as_of,
        )
        delegation = load_delegation(workspace, args.reviewer, args.delegation_manifest)
        packets = {
            lane.lens: list(lane.findings)
            for lane in delegation.lanes
            if lane.status == "complete"
        }
        synthesis = synthesize(
            packets,
            decisions,
            delegation=delegation,
            comparison_owner=args.comparison_owner,
            workspace=workspace,
        )
        approvals = [validate_token(item, "approval id") for item in args.approve]
        accepted_ids = {item["id"] for item in synthesis["accepted"]}
        unknown_approvals = [item for item in approvals if item not in accepted_ids]
        if unknown_approvals:
            raise ReflectError(
                "approval names a non-accepted finding: " + ", ".join(unknown_approvals)
            )
        if args.apply and not approvals:
            raise ReflectError("explicit user approval is required before persistent skill edits")
        print(
            render_result(
                workspace=workspace,
                workspace_id=workspace_id,
                topic=topic,
                start=start,
                as_of=as_of,
                transcript_path=transcript_path,
                opaque=opaque,
                decisions=decisions,
                delegation=delegation,
                packets=packets,
                synthesis=synthesis,
                approvals=approvals,
            ),
            end="",
        )
        return 0
    except (ReflectError, OSError, RuntimeError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
