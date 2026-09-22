#!/usr/bin/env python3
"""Run a bounded, data-only reflect contract fixture.

The helper exercises the Codex port without spawning agents or applying edits.
It reads one explicitly supplied transcript and, when requested, one reviewer
packet for each lens. Transcript and reviewer fields remain untrusted data.
"""

from __future__ import annotations

import argparse
import json
import os
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
MAX_FINDINGS = 64
MAX_WINDOW_DAYS = 31
MAX_TEXT_LENGTH = 400
LENSES = ("judgment", "tooling", "divergent")
COMPLETE_STATES = {"complete", "completed", "done"}
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


class ReflectError(ValueError):
    """A bounded input or contract error that should not produce a traceback."""


@dataclass(frozen=True)
class Decision:
    record_id: str
    timestamp: datetime
    state: str
    claim: str
    required_checks: tuple[str, ...]
    completed_checks: tuple[str, ...]
    evidence: tuple[str, ...]
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


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReflectError(f"{field} must be a timezone-aware ISO-8601 string")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ReflectError(f"{field} is not valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReflectError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_workspace(raw_workspace: str) -> Path:
    if not isinstance(raw_workspace, str) or not raw_workspace or "\x00" in raw_workspace:
        raise ReflectError("workspace must be a non-empty path")
    try:
        workspace = Path(raw_workspace).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ReflectError("workspace cannot be resolved") from exc
    if not workspace.is_dir():
        raise ReflectError("workspace is not a directory")
    return workspace


def resolve_file(workspace: Path, raw_path: str, *, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ReflectError(f"{label} path must be non-empty and contain no NUL bytes")
    candidate = Path(raw_path).expanduser()
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
    if raw is None and not required:
        return ()
    if not isinstance(raw, list) or len(raw) > 16:
        raise ReflectError(f"{field} must be a list of at most 16 strings")
    if not all(isinstance(item, str) and item.strip() for item in raw):
        raise ReflectError(f"{field} must contain non-empty strings")
    return tuple(clean_text(item) for item in raw)


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

    raw_workspace = raw.get("workspace")
    raw_topic = raw.get("topic")
    if raw_workspace != workspace_id or raw_topic != topic:
        return None

    record_id = raw.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ReflectError(f"{source}: in-scope record id is required")
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

    return Decision(
        record_id=clean_text(record_id),
        timestamp=timestamp,
        state=clean_text(state),
        claim=clean_text(claim),
        required_checks=string_list(
            raw.get("required_checks"),
            field=f"{source}#{record_id}.required_checks",
        ),
        completed_checks=string_list(
            raw.get("completed_checks"),
            field=f"{source}#{record_id}.completed_checks",
        ),
        evidence=string_list(
            raw.get("evidence"),
            field=f"{source}#{record_id}.evidence",
        ),
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


def load_reviewer_packet(workspace: Path, raw_path: str) -> tuple[str, Path, list[ReviewerFinding]]:
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
    raw_findings = packet.get("findings")
    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS:
        raise ReflectError(f"{path}: findings must be a bounded list")

    findings: list[ReviewerFinding] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_findings):
        if not isinstance(raw, dict):
            raise ReflectError(f"{path}: finding {index} must be an object")
        values = {field: raw.get(field) for field in ("id", "problem", "proposal", "routing")}
        if not all(isinstance(value, str) and value.strip() for value in values.values()):
            raise ReflectError(f"{path}: finding {index} needs id, problem, proposal, and routing")
        finding_id = clean_text(values["id"], limit=120)
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
                routing=clean_text(values["routing"], limit=200),
                evidence=evidence,
                enforcement=enforcement,
            )
        )
    return lens, path, findings


def load_reviewers(workspace: Path, raw_paths: list[str]) -> tuple[dict[str, list[ReviewerFinding]], list[Path]]:
    if not raw_paths:
        return {}, []
    if len(raw_paths) != len(LENSES):
        raise ReflectError("supply exactly one reviewer packet for each lens")
    packets: dict[str, list[ReviewerFinding]] = {}
    paths: list[Path] = []
    finding_ids: set[str] = set()
    for raw_path in raw_paths:
        lens, path, findings = load_reviewer_packet(workspace, raw_path)
        if lens in packets:
            raise ReflectError(f"duplicate reviewer lens {lens!r}")
        duplicate_ids = finding_ids.intersection(item.finding_id for item in findings)
        if duplicate_ids:
            raise ReflectError(
                "reviewer finding ids must be unique across lenses: "
                + ", ".join(sorted(duplicate_ids))
            )
        finding_ids.update(item.finding_id for item in findings)
        packets[lens] = findings
        paths.append(path)
    missing = [lens for lens in LENSES if lens not in packets]
    if missing:
        raise ReflectError(f"missing reviewer lens: {', '.join(missing)}")
    return packets, paths


def source_ref(decision: Decision, workspace: Path) -> str:
    try:
        relative = decision.source.relative_to(workspace)
    except ValueError:
        relative = Path(decision.source.name)
    return f"{relative}#{decision.record_id}"


def missing_checks(decision: Decision) -> list[str]:
    completed = {item.casefold() for item in decision.completed_checks}
    return [item for item in decision.required_checks if item.casefold() not in completed]


def inspect_decisions(decisions: list[Decision], workspace: Path) -> dict[str, list[str]]:
    observed: list[str] = []
    unknowns: list[str] = []
    gaps: list[str] = []
    for decision in decisions:
        reference = source_ref(decision, workspace)
        observed.append(
            f"[observed] {reference} reports state {decision.state!r}: {decision.claim!r}."
        )
        missing = missing_checks(decision)
        if missing:
            checks = ", ".join(repr(item) for item in missing)
            unknowns.append(
                f"[unknown] {reference} does not establish completion; required checks not evidenced: {checks}."
            )
            gaps.append(
                f"[gap] {reference} leaves required checks skipped or unverified: {checks}."
            )
            if decision.state.casefold() in COMPLETE_STATES:
                gaps.append(
                    f"[gap] False completion signal: {reference} claims {decision.state!r} while checks remain missing."
                )
        elif decision.state.casefold() in COMPLETE_STATES and not decision.evidence:
            unknowns.append(f"[unknown] {reference} claims completion but cites no verification evidence.")
            gaps.append(f"[gap] Completion at {reference} is self-reported without cited evidence.")
        if not decision.evidence:
            unknowns.append(f"[unknown] {reference} has no supplied evidence list.")
    if not decisions:
        unknowns.append("[unknown] No structured decision record matched the requested scope.")
    return {
        "observed": unique(observed),
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


def classify_finding(finding: ReviewerFinding) -> tuple[str, str | None]:
    proposal = finding.proposal.casefold()
    if finding.enforcement in STRUCTURAL_ENFORCEMENT:
        return "backlog", f"declared {finding.enforcement} enforcement"
    if any(marker in proposal for marker in MECHANISM_MARKERS):
        return "backlog", "proposal names a deterministic enforcement mechanism"
    return "accepted", None


def synthesize(
    packets: dict[str, list[ReviewerFinding]],
    decisions: list[Decision],
) -> dict[str, list[dict[str, Any]]]:
    record_ids = {decision.record_id for decision in decisions}
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for lens in LENSES:
        for finding in packets.get(lens, []):
            evidence = [item for item in finding.evidence if item in record_ids]
            if not evidence:
                rejected.append(
                    {
                        "id": finding.finding_id,
                        "reason": "unresolved evidence reference",
                        "detail": f"{finding.lens} finding cites no in-scope transcript id",
                    }
                )
                continue
            key = (
                finding.problem.casefold(),
                finding.proposal.casefold(),
                finding.routing.casefold(),
            )
            item = grouped.setdefault(
                key,
                {
                    "id": finding.finding_id,
                    "problem": finding.problem,
                    "proposal": finding.proposal,
                    "routing": finding.routing,
                    "evidence": [],
                    "lenses": [],
                    "enforcement": finding.enforcement,
                },
            )
            item["evidence"].extend(evidence)
            item["lenses"].append(finding.lens)
            item["enforcement"] = finding.enforcement

    accepted: list[dict[str, Any]] = []
    backlog: list[dict[str, Any]] = []
    for item in grouped.values():
        item["evidence"] = unique(item["evidence"])
        item["lenses"] = unique(item["lenses"])
        bucket, reason = classify_finding(
            ReviewerFinding(
                finding_id=item["id"],
                lens=item["lenses"][0],
                problem=item["problem"],
                proposal=item["proposal"],
                routing=item["routing"],
                evidence=tuple(item["evidence"]),
                enforcement=item["enforcement"],
            )
        )
        if bucket == "backlog":
            item["mechanism"] = reason
            backlog.append(item)
        else:
            accepted.append(item)
    return {"accepted": accepted, "rejected": rejected, "backlog": backlog}


def render_item(item: dict[str, Any], evidence_refs: dict[str, str]) -> str:
    evidence = ", ".join(evidence_refs.get(value, value) for value in item["evidence"])
    lenses = ", ".join(item.get("lenses", []))
    return (
        f"- [{item['id']}] {item['problem']} Proposal: {item['proposal']} "
        f"(evidence: {evidence}; routing: {item['routing']}; lenses: {lenses})"
    )


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
    reviewer_paths: list[Path],
    packets: dict[str, list[ReviewerFinding]],
    synthesis: dict[str, list[dict[str, Any]]],
    approvals: list[str],
) -> str:
    try:
        transcript_ref = transcript_path.relative_to(workspace)
    except ValueError:
        transcript_ref = Path(transcript_path.name)
    evidence_refs = {
        decision.record_id: f"{transcript_ref}#{decision.record_id}"
        for decision in decisions
    }
    inspection = inspect_decisions(decisions, workspace)
    lines = [
        "## Active transcript",
        f"- Scope: workspace={workspace_id}; topic={topic}; window={start.isoformat()} through {as_of.isoformat()}.",
        f"- Source: {transcript_ref}; structured decisions in scope: {len(decisions)}.",
    ]
    if opaque:
        lines.append("- The explicitly supplied transcript was read as opaque data; no structured claims were accepted.")
    lines.extend(["", "## Findings", "### Observed"])
    lines.extend(f"- {item}" for item in inspection["observed"] or ["[observed] No structured decision was supplied."])
    lines.extend(["", "### Uncertainty"])
    lines.extend(f"- {item}" for item in inspection["unknowns"])
    lines.extend(["", "### Process/code gaps"])
    lines.extend(f"- {item}" for item in inspection["gaps"] or ["[gap] No supported process or code gap was derived."])
    lines.extend(["", "## Reviewer lenses"])
    if packets:
        for lens in LENSES:
            lines.append(
                f"- [{lens}] {len(packets[lens])} untrusted finding(s) from "
                f"{reviewer_paths[LENSES.index(lens)].relative_to(workspace)}."
            )
    else:
        lines.append("- No reviewer packets supplied; no proposals were synthesized.")

    lines.extend(["", "## Accepted"])
    if synthesis["accepted"]:
        lines.extend(render_item(item, evidence_refs) for item in synthesis["accepted"])
    else:
        lines.append("- None.")
    lines.extend(["", "## Rejected"])
    if synthesis["rejected"]:
        lines.extend(
            f"- [{item['id']}] {item['reason']}: {item['detail']}"
            for item in synthesis["rejected"]
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Backlog"])
    if synthesis["backlog"]:
        lines.extend(
            f"- [{item['id']}] {item['problem']} Proposal: {item['proposal']} "
            f"(mechanism: {item['mechanism']}; evidence: "
            f"{', '.join(evidence_refs.get(value, value) for value in item['evidence'])})."
            for item in synthesis["backlog"]
        )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Approval",
            "- approval_required: true",
            f"- approval_received_for: {', '.join(approvals) if approvals else 'none'}",
            "- persistent_edits: 0",
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
    parser.add_argument("--approve", action="append", default=[], help="explicitly approved finding id")
    parser.add_argument("--apply", action="store_true", help="check the approval gate; never writes files")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.window_days <= MAX_WINDOW_DAYS:
            raise ReflectError(f"window-days must be between 1 and {MAX_WINDOW_DAYS}")
        if not isinstance(args.topic, str) or not args.topic.strip() or any(ord(char) < 32 for char in args.topic):
            raise ReflectError("topic must be non-empty and free of control characters")
        workspace = canonical_workspace(args.workspace)
        workspace_id = args.workspace_id or workspace.name
        if not workspace_id.strip() or any(ord(char) < 32 for char in workspace_id):
            raise ReflectError("workspace-id must be non-empty and free of control characters")
        as_of = parse_timestamp(args.as_of, "as-of")
        start = as_of - timedelta(days=args.window_days)
        decisions, opaque, transcript_path = load_decisions(
            workspace,
            args.transcript,
            workspace_id=workspace_id,
            topic=args.topic,
            start=start,
            as_of=as_of,
        )
        packets, reviewer_paths = load_reviewers(workspace, args.reviewer)
        synthesis = synthesize(packets, decisions)
        accepted_ids = {item["id"] for item in synthesis["accepted"]}
        unknown_approvals = [item for item in args.approve if item not in accepted_ids]
        if unknown_approvals:
            raise ReflectError(
                "approval names a non-accepted finding: " + ", ".join(unknown_approvals)
            )
        if args.apply and not args.approve:
            raise ReflectError("explicit user approval is required before persistent skill edits")
        print(
            render_result(
                workspace=workspace,
                workspace_id=workspace_id,
                topic=args.topic,
                start=start,
                as_of=as_of,
                transcript_path=transcript_path,
                opaque=opaque,
                decisions=decisions,
                reviewer_paths=reviewer_paths,
                packets=packets,
                synthesis=synthesis,
                approvals=args.approve,
            ),
            end="",
        )
        return 0
    except (ReflectError, OSError, RuntimeError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
