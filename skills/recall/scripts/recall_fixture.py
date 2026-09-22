#!/usr/bin/env python3
"""Bounded, data-only recall fixture runner.

This helper intentionally has no shell, dynamic-import, plugin, or code-execution path.
It exercises the artifact boundary and renders the recall output contract for
synthetic and user-provided records.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ALLOWED_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log"}
OPAQUE_SUFFIXES = {".md", ".txt", ".log"}
ALLOWED_ORIGINS = {"synthetic", "user"}
STATUS_PATTERNS = (
    re.compile(r"^\[merged #[0-9]+\]$"),
    re.compile(r"^\[open PR #[0-9]+\]$"),
    re.compile(r"^\[in flight [^\]]+\]$"),
    re.compile(r"^\[verified, uncommitted\]$"),
    re.compile(r"^\[reverted #[0-9]+\]$"),
    re.compile(r"^\[planned, not started\]$"),
)
MAX_ARTIFACTS = 32
MAX_FILE_BYTES = 1_000_000
MAX_WINDOW_DAYS = 31
MAX_TEXT_LENGTH = 600


class RecallError(ValueError):
    """A user-correctable boundary or record error."""


def clean_text(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Keep rendered data on one line and remove terminal control characters."""

    text = str(value).replace("\r", " ").replace("\n", " ")
    text = "".join(char for char in text if char == "\t" or ord(char) >= 32)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RecallError(f"{field} must be a timezone-aware ISO-8601 string")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RecallError(f"{field} is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecallError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_workspace(raw_workspace: str) -> Path:
    if not raw_workspace or "\x00" in raw_workspace:
        raise RecallError("workspace must be a non-empty path")
    workspace = Path(raw_workspace).expanduser()
    try:
        resolved = workspace.resolve(strict=True)
    except OSError as exc:
        raise RecallError(f"workspace cannot be resolved: {raw_workspace}") from exc
    if not resolved.is_dir():
        raise RecallError(f"workspace is not a directory: {raw_workspace}")
    return resolved


def resolve_artifact(workspace: Path, raw_artifact: str) -> Path:
    if not raw_artifact or "\x00" in raw_artifact:
        raise RecallError("artifact path must be non-empty and contain no NUL bytes")
    candidate = Path(raw_artifact).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RecallError(f"artifact cannot be resolved: {raw_artifact}") from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise RecallError(
            f"artifact is outside workspace after canonical resolution: {raw_artifact}"
        ) from exc
    try:
        info = resolved.stat()
    except OSError as exc:
        raise RecallError(f"artifact cannot be stat-ed: {raw_artifact}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RecallError(f"artifact is not a regular file: {raw_artifact}")
    if info.st_size > MAX_FILE_BYTES:
        raise RecallError(f"artifact exceeds {MAX_FILE_BYTES} bytes: {raw_artifact}")
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        raise RecallError(
            f"unsupported artifact type {resolved.suffix or '<none>'!r}: {raw_artifact}"
        )
    return resolved


def scalar_list(record: dict[str, Any], field: str) -> list[str]:
    value = record.get(field, [])
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecallError(f"record field {field!r} must be a string list")
    return [clean_text(item) for item in value if item.strip()]


def validate_origin(value: Any, default_origin: str | None, source: str) -> str:
    origin = value if value is not None else default_origin
    if origin not in ALLOWED_ORIGINS:
        raise RecallError(
            f"{source}: origin must be explicitly 'synthetic' or 'user'"
        )
    return str(origin)


def valid_status(value: Any) -> bool:
    return (
        isinstance(value, str)
        and not any(ord(char) < 32 for char in value)
        and any(pattern.fullmatch(value) for pattern in STATUS_PATTERNS)
    )


def normalize_record(
    raw: Any,
    *,
    source: str,
    default_origin: str | None,
    default_workspace: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RecallError(f"{source}: each record must be a JSON object")
    record_id = raw.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise RecallError(f"{source}: record id is required")
    topic = raw.get("topic")
    if (
        not isinstance(topic, str)
        or not topic.strip()
        or any(ord(char) < 32 for char in topic)
    ):
        raise RecallError(f"{source}#{record_id}: topic is required")
    workspace = raw.get("workspace", default_workspace)
    if (
        not isinstance(workspace, str)
        or not workspace.strip()
        or any(ord(char) < 32 for char in workspace)
    ):
        raise RecallError(f"{source}#{record_id}: workspace must be a string")
    modified_at = parse_timestamp(raw.get("modified_at"), f"{source}#{record_id}.modified_at")
    origin = validate_origin(raw.get("origin"), default_origin, f"{source}#{record_id}")

    status = raw.get("status")
    if status is not None:
        if not valid_status(status):
            raise RecallError(
                f"{source}#{record_id}: status must be one exact upstream status tag"
            )

    body = raw.get("body", "")
    if not isinstance(body, str):
        raise RecallError(f"{source}#{record_id}: body must be a string when supplied")

    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise RecallError(f"{source}#{record_id}: summary must be a string when supplied")
    next_move = raw.get("next_move", "")
    if next_move is None:
        next_move = ""
    if not isinstance(next_move, str):
        raise RecallError(f"{source}#{record_id}: next_move must be a string when supplied")

    claims: list[dict[str, str]] = []
    raw_claims = raw.get("claims", [])
    if not isinstance(raw_claims, list):
        raise RecallError(f"{source}#{record_id}: claims must be a list")
    for index, claim in enumerate(raw_claims):
        if not isinstance(claim, dict):
            raise RecallError(f"{source}#{record_id}.claims[{index}] must be an object")
        key = claim.get("key")
        value = claim.get("value")
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
            raise RecallError(
                f"{source}#{record_id}.claims[{index}] needs string key and value"
            )
        kind = claim.get("kind", "fact")
        if kind not in {"fact", "inference", "unknown"}:
            raise RecallError(
                f"{source}#{record_id}.claims[{index}].kind is unsupported"
            )
        text = claim.get("text", value)
        if not isinstance(text, str):
            raise RecallError(f"{source}#{record_id}.claims[{index}].text must be a string")
        claims.append(
            {
                "key": clean_text(key),
                "value": clean_text(value),
                "text": clean_text(text),
                "kind": kind,
            }
        )

    return {
        "id": clean_text(record_id),
        "origin": origin,
        "modified_at": modified_at,
        "workspace": workspace,
        "topic": topic,
        "status": status,
        "thread": clean_text(raw.get("thread", record_id)),
        "summary": clean_text(summary),
        "facts": scalar_list(raw, "facts"),
        "inferences": scalar_list(raw, "inferences"),
        "unknowns": scalar_list(raw, "unknowns"),
        "problems": scalar_list(raw, "problems"),
        "next_move": clean_text(next_move),
        "claims": claims,
        "body_present": bool(body),
        "source": source,
    }


def parse_json_artifact(
    path: Path, *, default_origin: str | None, workspace_id: str
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecallError(f"cannot read JSON artifact {path}: {exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        raw_records: Iterable[Any] = payload["records"]
    elif isinstance(payload, list):
        raw_records = payload
    else:
        raw_records = [payload]
    return [
        normalize_record(
            raw,
            source=str(path),
            default_origin=default_origin,
            default_workspace=workspace_id,
        )
        for raw in raw_records
    ]


def parse_jsonl_artifact(
    path: Path, *, default_origin: str | None, workspace_id: str
) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RecallError(f"cannot read JSONL artifact {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        source = f"{path}:{line_number}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecallError(f"cannot parse {source}: {exc}") from exc
        records.append(
            normalize_record(
                raw,
                source=source,
                default_origin=default_origin,
                default_workspace=workspace_id,
            )
        )
    return records


def artifact_origin_map(artifacts: list[str], origins: list[str] | None) -> list[str | None]:
    supplied = origins or []
    if not supplied:
        return [None] * len(artifacts)
    if len(supplied) == 1:
        return supplied * len(artifacts)
    if len(supplied) != len(artifacts):
        raise RecallError(
            "provide one --artifact-origin for all artifacts or one per --artifact"
        )
    return list(supplied)


def read_artifacts(
    workspace: Path,
    artifacts: list[str],
    origins: list[str] | None,
    workspace_id: str,
) -> tuple[list[dict[str, Any]], list[str], list[Path]]:
    if not artifacts:
        raise RecallError("at least one explicit --artifact is required")
    if len(artifacts) > MAX_ARTIFACTS:
        raise RecallError(f"at most {MAX_ARTIFACTS} artifacts may be supplied")
    defaults = artifact_origin_map(artifacts, origins)
    records: list[dict[str, Any]] = []
    opaque: list[str] = []
    resolved_paths: list[Path] = []
    for raw_path, default_origin in zip(artifacts, defaults):
        path = resolve_artifact(workspace, raw_path)
        resolved_paths.append(path)
        suffix = path.suffix.lower()
        if suffix in OPAQUE_SUFFIXES:
            if default_origin not in ALLOWED_ORIGINS:
                raise RecallError(
                    f"{path}: opaque documents require --artifact-origin synthetic or user"
                )
            try:
                path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RecallError(f"cannot read opaque artifact {path}: {exc}") from exc
            opaque.append(str(path))
        elif suffix == ".jsonl":
            records.extend(
                parse_jsonl_artifact(
                    path, default_origin=default_origin, workspace_id=workspace_id
                )
            )
        else:
            records.extend(
                parse_json_artifact(
                    path, default_origin=default_origin, workspace_id=workspace_id
                )
            )
    return records, opaque, resolved_paths


def source_ref(record: dict[str, Any], workspace: Path) -> str:
    path = Path(record["source"])
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        relative = path.name
    return f"{relative}#{record['id']}"


def unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.casefold()
        if normalized not in seen:
            result.append(value)
            seen.add(normalized)
    return result


def collect_evidence(records: list[dict[str, Any]], workspace: Path) -> dict[str, list[str]]:
    evidence: dict[str, list[str]] = {
        "facts": [],
        "inferences": [],
        "unknowns": [],
        "problems": [],
        "contradictions": [],
    }
    claims_by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        reference = source_ref(record, workspace)
        for fact in record["facts"]:
            evidence["facts"].append(f"{fact} [source: {reference}]")
        for inference in record["inferences"]:
            evidence["inferences"].append(f"{inference} [source: {reference}]")
        for unknown in record["unknowns"]:
            evidence["unknowns"].append(f"{unknown} [source: {reference}]")
        for problem in record["problems"]:
            evidence["problems"].append(f"{problem} [source: {reference}]")
        for claim in record["claims"]:
            claims_by_key[claim["key"].casefold()].append({**claim, "reference": reference})
            if claim["kind"] == "fact":
                evidence["facts"].append(f"{claim['text']} [source: {reference}]")
            elif claim["kind"] == "inference":
                evidence["inferences"].append(f"{claim['text']} [source: {reference}]")
            else:
                evidence["unknowns"].append(f"{claim['text']} [source: {reference}]")

    for claims in claims_by_key.values():
        by_value: dict[str, list[dict[str, str]]] = defaultdict(list)
        for claim in claims:
            by_value[claim["value"].casefold()].append(claim)
        if len(by_value) > 1:
            value_lines = []
            for claim_group in by_value.values():
                first = claim_group[0]
                value_lines.append(
                    f"{first['value']!r} ({first['reference']})"
                )
            key = claims[0]["key"]
            evidence["contradictions"].append(
                f"{key} has conflicting values: " + " versus ".join(value_lines)
            )
    for field in evidence:
        evidence[field] = unique(evidence[field])
    return evidence


def render_brief(
    *,
    workspace: Path,
    workspace_id: str,
    topic: str,
    window_days: int,
    as_of: datetime,
    records: list[dict[str, Any]],
    opaque: list[str],
    paths: list[Path],
) -> str:
    start = as_of - timedelta(days=window_days)
    ordered = sorted(
        records,
        key=lambda record: (record["modified_at"], record["source"], record["id"]),
        reverse=True,
    )
    in_scope = [
        record
        for record in ordered
        if record["workspace"] == workspace_id
        and record["topic"].casefold() == topic.casefold()
        and start <= record["modified_at"] <= as_of
    ]
    evidence = collect_evidence(in_scope, workspace)

    latest_summary = next((record["summary"] for record in in_scope if record["summary"]), "")
    current_state = latest_summary or "No current-state summary is supported by the supplied records."
    body_records = sum(1 for record in in_scope if record["body_present"])
    capsule = [
        f"Scope: workspace={workspace_id}; topic={topic}; window={start.isoformat()} through {as_of.isoformat()}.",
        f"Read {len(paths)} explicit allowlisted artifact(s); {len(in_scope)} structured record(s) match the scope.",
        f"Current state: {current_state}",
        f"Structured bodies read as data: {body_records}; opaque document bodies read: {len(opaque)}; unresolved contradictions: {len(evidence['contradictions'])}.",
        "ChatGPT/general conversation history is unavailable unless an authorized export was supplied.",
    ]

    thread_lines: list[str] = []
    seen_threads: set[str] = set()
    for record in in_scope:
        status = record["status"]
        if status is None:
            continue
        thread_key = f"{status}|{record['thread']}".casefold()
        if thread_key in seen_threads:
            continue
        seen_threads.add(thread_key)
        summary = record["summary"] or "No thread summary supplied."
        thread_lines.append(
            f"- {status} {record['thread']}: {summary} [source: {source_ref(record, workspace)}]"
        )
    if not thread_lines:
        thread_lines.append("- No status-bearing thread was supplied; status is unknown.")

    unknowns = list(evidence["unknowns"])
    unknowns.append(
        "UNKNOWN: ChatGPT/general conversation history is unavailable; no authorized export or search capability was supplied."
    )
    for record in in_scope:
        if record["status"] is None:
            unknowns.append(f"{source_ref(record, workspace)} has no exact supported status tag.")
        if not record["next_move"]:
            unknowns.append(f"{source_ref(record, workspace)} has no recorded next move.")
    for path in opaque:
        try:
            relative = Path(path).relative_to(workspace)
        except ValueError:
            relative = Path(path).name
        unknowns.append(f"{relative} was read as an opaque body; no structured claims were accepted.")
    if not in_scope:
        unknowns.append("No structured record matched the requested workspace, topic, and time window.")
    unknowns = unique(unknowns)

    problems = list(evidence["problems"])
    if evidence["contradictions"]:
        problems.append(
            "[inference] Conflicting claims remain unresolved; review both cited records before acting."
        )
    problems = unique(problems)[:5]

    next_move_sources: dict[str, list[str]] = defaultdict(list)
    for record in in_scope:
        if record["next_move"]:
            next_move_sources[record["next_move"].casefold()].append(
                source_ref(record, workspace)
            )
    if len(next_move_sources) == 1:
        first_move_record = next(record for record in in_scope if record["next_move"])
        next_move_text = first_move_record["next_move"]
        references = next_move_sources[next_move_text.casefold()]
        next_move = f"{next_move_text} [source: {', '.join(unique(references))}]"
    elif len(next_move_sources) > 1:
        next_move = "[inference] Resolve the conflicting next moves recorded in the supplied artifacts."
    else:
        next_move = "[unknown] Obtain an authorized current-state artifact with one concrete next move."

    lines = ["## Capsule"]
    lines.extend(f"- {item}" for item in capsule[:5])
    lines.append("")
    lines.append("## Threads")
    lines.extend(thread_lines)
    lines.extend(["", "## Evidence", "### Source-backed facts"])
    lines.extend(f"- {item}" for item in evidence["facts"] or ["None supplied."])
    lines.extend(["", "### Inference"])
    lines.extend(f"- {item}" for item in evidence["inferences"] or ["None supplied."])
    lines.extend(["", "### Unknowns"])
    lines.extend(f"- {item}" for item in unknowns)
    lines.extend(["", "### Contradictions"])
    lines.extend(f"- {item}" for item in evidence["contradictions"] or ["None detected in structured claims."])
    lines.extend(["", "## Problems"])
    lines.extend(f"- {item}" for item in problems or ["None recorded in the supplied artifacts."])
    lines.extend(["", "## Next move", next_move, ""])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="canonical containment root")
    parser.add_argument("--workspace-id", help="record workspace label; defaults to root name")
    parser.add_argument("--topic", required=True, help="one exact topic to recall")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--as-of", help="timezone-aware ISO timestamp; defaults to now")
    parser.add_argument("--artifact", action="append", required=True, help="explicit artifact path")
    parser.add_argument(
        "--artifact-origin",
        action="append",
        choices=sorted(ALLOWED_ORIGINS),
        help="origin for one artifact, or a default for all artifacts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.window_days <= MAX_WINDOW_DAYS:
            raise RecallError(f"window-days must be between 1 and {MAX_WINDOW_DAYS}")
        if (
            not args.topic.strip()
            or len(args.topic) > 120
            or any(ord(char) < 32 for char in args.topic)
        ):
            raise RecallError("topic must be non-empty and at most 120 characters")
        workspace = canonical_workspace(args.workspace)
        workspace_id = args.workspace_id or workspace.name
        if not workspace_id.strip() or any(ord(char) < 32 for char in workspace_id):
            raise RecallError("workspace-id must be non-empty")
        as_of = parse_timestamp(args.as_of, "as-of") if args.as_of else datetime.now(timezone.utc)
        records, opaque, paths = read_artifacts(
            workspace,
            args.artifact,
            args.artifact_origin,
            workspace_id,
        )
        print(
            render_brief(
                workspace=workspace,
                workspace_id=workspace_id,
                topic=args.topic,
                window_days=args.window_days,
                as_of=as_of,
                records=records,
                opaque=opaque,
                paths=paths,
            )
        )
        return 0
    except RecallError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
