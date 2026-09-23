#!/usr/bin/env python3
"""Bounded, data-only recall fixture runner.

This helper intentionally has no shell, dynamic-import, plugin, or code-execution path.
It exercises the artifact boundary and renders the recall output contract for
synthetic and user-provided records.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ALLOWED_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log"}
OPAQUE_SUFFIXES = {".md", ".txt", ".log"}
ALLOWED_ORIGINS = {"synthetic", "user"}
ALLOWED_PROVENANCES = {"ordinary-artifact", "authorized-history-export"}
AUTHORIZED_HISTORY_EXPORT = "authorized-history-export"
SAFE_BRANCH_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._/@+-]*"
STATUS_PATTERNS = (
    re.compile(r"^\[merged #[1-9][0-9]*\]$"),
    re.compile(r"^\[open PR #[1-9][0-9]*\]$"),
    re.compile(rf"^\[in flight {SAFE_BRANCH_PATTERN}\]$"),
    re.compile(r"^\[verified, uncommitted\]$"),
    re.compile(r"^\[reverted #[1-9][0-9]*\]$"),
    re.compile(r"^\[planned, not started\]$"),
)
LOG_TIMESTAMP_PATTERN = (
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
LOG_PREFIX_PATTERN = re.compile(
    rf"^(?:\[{LOG_TIMESTAMP_PATTERN}\]|{LOG_TIMESTAMP_PATTERN})"
    r"(?:\s+[A-Za-z][A-Za-z0-9_.-]*)?\s+"
)
ROLE_PREFIX_PATTERN = re.compile(
    r"^(?:user|assistant|system|developer|tool)\s*:\s*",
    flags=re.IGNORECASE,
)
MARKDOWN_PREFIX_PATTERN = re.compile(r"^(?:[-*+]\s+|#{1,6}\s+)")
TEXT_FIELD_PATTERN = re.compile(
    r"(?i)^"
    r"(?P<label>codex[- _]?recall[- _]?export|"
    r"modified(?:[_ -]?at)?|timestamp|"
    r"next(?:[_ -]?move)?|"
    r"open[_ -]?(?:work|items?)|unfinished(?:[_ -]?work)?|"
    r"pull[_ -]?request|pr|"
    r"workspace|topic|status|thread|summary|id|origin|format|"
    r"decision|verified|verification|evidence|fact|"
    r"assumption|inference|unknown|missing|problem|blocker|todo|"
    r"issue|commit|claim)"
    r"\s*:\s*(?P<value>[^\r\n]*)$"
)
EXPORT_MARKER_PATTERN = re.compile(
    r"^Codex-Recall-Export:\s*(?P<version>[^\s]+)$"
)
EXPORT_VERSION = "1"
MAX_ARTIFACTS = 32
MAX_FILE_BYTES = 1_000_000
MAX_WINDOW_DAYS = 31
MAX_TEXT_LENGTH = 600
UNSAFE_PATH_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
RESUME_CHECKLIST_LIMIT = 5


class RecallError(ValueError):
    """A user-correctable boundary or record error."""


def clean_text(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """Keep rendered data on one line and remove terminal/control characters."""

    text = str(value).replace("\r", " ").replace("\n", " ")
    text = "".join(
        char
        for char in text
        if char == "\t"
        or (
            ord(char) >= 32
            and unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
        )
    )
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def quote_data(value: str) -> str:
    """Quote a cleaned artifact value before putting it in the agent-facing brief."""

    return json.dumps(value, ensure_ascii=True)


def quote_path(value: Any) -> str:
    """Quote path metadata so it cannot create output structure."""

    return json.dumps(str(value), ensure_ascii=True)


def quote_record_id(value: Any) -> str:
    """Quote a record identifier after removing output-structure characters."""

    return quote_data(clean_text(value))


def source_context(source: Any) -> str:
    """Format a source path as bounded metadata for a validation error."""

    return f"source={quote_path(source)}"


def record_context(source: Any, record_id: Any) -> str:
    """Format source and record identity without interpolating raw input."""

    return f"{source_context(source)}, record_id={quote_record_id(record_id)}"


def has_unsafe_path_characters(value: str) -> bool:
    return any(unicodedata.category(char) in UNSAFE_PATH_CATEGORIES for char in value)


def validate_scope_identifier(value: Any, label: str, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecallError(f"{label} must be non-empty")
    if max_length is not None and len(value) > max_length:
        raise RecallError(f"{label} must be at most {max_length} characters")
    if has_unsafe_path_characters(value):
        raise RecallError(
            f"{label} contains unsafe control, format, or line-separator characters"
        )
    return value


def quote_scope_identifier(value: str) -> str:
    """Render a validated scope identifier as bounded quoted data."""

    return quote_data(clean_text(value))


def untrusted_value(label: str, value: str) -> str:
    return f"[untrusted {label}] {quote_data(value)}"


def sourced_value(label: str, value: str, reference: str) -> str:
    return f"{untrusted_value(label, value)} [source: {reference}]"


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RecallError(f"{field} must be a timezone-aware ISO-8601 string")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RecallError(
            f"{field} is not valid ISO-8601: {quote_data(clean_text(value))}"
        ) from exc
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
        raise RecallError(f"workspace cannot be resolved: {quote_path(raw_workspace)}") from exc
    if not resolved.is_dir():
        raise RecallError(f"workspace is not a directory: {quote_path(raw_workspace)}")
    return resolved


def resolve_artifact(workspace: Path, raw_artifact: str) -> Path:
    if not raw_artifact or "\x00" in raw_artifact:
        raise RecallError("artifact path must be non-empty and contain no NUL bytes")
    if has_unsafe_path_characters(raw_artifact):
        raise RecallError(
            "artifact path contains unsafe control or line-separator characters: "
            f"{quote_path(raw_artifact)}"
        )
    candidate = Path(raw_artifact).expanduser()
    supplied_suffix = candidate.suffix.lower()
    if supplied_suffix not in ALLOWED_SUFFIXES:
        raise RecallError(
            f"unsupported artifact type {candidate.suffix or '<none>'!r}: "
            f"{quote_path(raw_artifact)}"
        )
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RecallError(f"artifact cannot be resolved: {quote_path(raw_artifact)}") from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise RecallError(
            "artifact is outside workspace after canonical resolution: "
            f"{quote_path(raw_artifact)}"
        ) from exc
    relative = resolved.relative_to(workspace)
    if has_unsafe_path_characters(str(relative)):
        raise RecallError(
            "artifact path contains unsafe control or line-separator characters after "
            f"resolution: {quote_path(relative)}"
        )
    resolved_suffix = resolved.suffix.lower()
    if resolved_suffix not in ALLOWED_SUFFIXES or resolved_suffix != supplied_suffix:
        raise RecallError(
            f"artifact suffix does not match its resolved target: "
            f"{quote_path(raw_artifact)}"
        )
    return resolved


def stat_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    """Identify one filesystem object across path and descriptor views."""

    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_ctime_ns)


def artifact_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """Identify an artifact and the bounded file state consumed by the parser."""

    return (
        *stat_identity(info),
        info.st_size,
        info.st_mtime_ns,
        info.st_nlink,
    )


def close_fd(file_descriptor: int | None) -> None:
    if file_descriptor is None:
        return
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def open_workspace_descriptor(workspace: Path) -> tuple[int, tuple[int, int, int, int]]:
    """Bind the canonical workspace path to a no-follow directory descriptor."""

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not directory_flag or not nonblocking:
        raise RecallError("safe descriptor-based artifact reads are unavailable")
    try:
        expected = os.stat(workspace, follow_symlinks=False)
        if not stat.S_ISDIR(expected.st_mode):
            raise RecallError(f"workspace is not a directory: {quote_path(workspace)}")
        directory_flags = os.O_RDONLY | close_on_exec | no_follow | directory_flag | nonblocking
        workspace_fd = os.open(str(workspace), directory_flags)
        actual = os.fstat(workspace_fd)
        if (
            not stat.S_ISDIR(actual.st_mode)
            or stat_identity(actual) != stat_identity(expected)
        ):
            close_fd(workspace_fd)
            raise RecallError(
                f"workspace changed before safe open: {quote_path(workspace)}"
            )
        return workspace_fd, stat_identity(expected)
    except RecallError:
        raise
    except (OSError, ValueError) as exc:
        raise RecallError(f"workspace cannot be opened safely: {quote_path(workspace)}") from exc


def revalidate_workspace(
    workspace: Path, expected_identity: tuple[int, int, int, int]
) -> None:
    """Reject replacement of the canonical workspace path during the read."""

    try:
        current = os.stat(workspace, follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise RecallError(f"workspace changed during read: {quote_path(workspace)}") from exc
    if not stat.S_ISDIR(current.st_mode) or stat_identity(current) != expected_identity:
        raise RecallError(f"workspace changed during read: {quote_path(workspace)}")


def revalidate_artifact(
    resolved: Path,
    expected_identity: tuple[int, int, int, int, int, int, int],
) -> None:
    """Reject a path mutation after the descriptor-backed read and before parsing."""

    try:
        current = os.stat(resolved, follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise RecallError(f"artifact changed after safe read: {quote_path(resolved)}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or artifact_identity(current) != expected_identity
    ):
        raise RecallError(f"artifact changed after safe read: {quote_path(resolved)}")


def open_checked_artifact(
    workspace: Path,
    resolved: Path,
    *,
    workspace_fd: int | None = None,
    workspace_identity: tuple[int, int, int, int] | None = None,
    expected_target_identity: tuple[int, int, int, int, int, int, int] | None = None,
) -> int:
    """Open the resolved target beneath a directory descriptor without following links."""

    try:
        relative = resolved.relative_to(workspace)
    except ValueError as exc:
        raise RecallError(f"artifact is outside workspace: {quote_path(resolved)}") from exc
    if not relative.parts:
        raise RecallError(f"artifact is not a regular file: {quote_path(resolved)}")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not directory_flag or not nonblocking:
        raise RecallError("safe descriptor-based artifact reads are unavailable")
    directory_flags = os.O_RDONLY | close_on_exec | no_follow | directory_flag | nonblocking
    file_flags = os.O_RDONLY | close_on_exec | no_follow | nonblocking
    parent_fd: int | None = None
    artifact_fd: int | None = None
    try:
        if workspace_fd is None:
            parent_fd, opened_workspace_identity = open_workspace_descriptor(workspace)
            if (
                workspace_identity is not None
                and opened_workspace_identity != workspace_identity
            ):
                raise RecallError(
                    f"workspace changed before safe open: {quote_path(workspace)}"
                )
            workspace_identity = opened_workspace_identity
        else:
            parent_fd = os.dup(workspace_fd)
            opened_workspace = os.fstat(parent_fd)
            if (
                workspace_identity is not None
                and stat_identity(opened_workspace) != workspace_identity
            ):
                raise RecallError(
                    f"workspace descriptor changed during read: {quote_path(workspace)}"
                )
        for component in relative.parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            try:
                child_stat = os.fstat(child_fd)
            except (OSError, ValueError):
                close_fd(child_fd)
                raise
            if not stat.S_ISDIR(child_stat.st_mode):
                close_fd(child_fd)
                raise RecallError(f"artifact parent is not a directory: {quote_path(resolved)}")
            close_fd(parent_fd)
            parent_fd = child_fd
        target_stat = os.stat(relative.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(target_stat.st_mode):
            raise RecallError(f"artifact is not a regular file: {quote_path(resolved)}")
        if target_stat.st_nlink != 1:
            raise RecallError(f"hardlinked artifacts are not permitted: {quote_path(resolved)}")
        if target_stat.st_size > MAX_FILE_BYTES:
            raise RecallError(f"artifact exceeds {MAX_FILE_BYTES} bytes: {quote_path(resolved)}")
        if (
            expected_target_identity is not None
            and artifact_identity(target_stat) != expected_target_identity
        ):
            raise RecallError(f"artifact changed before safe open: {quote_path(resolved)}")
        artifact_fd = os.open(relative.parts[-1], file_flags, dir_fd=parent_fd)
        opened_target = os.fstat(artifact_fd)
        if (
            not stat.S_ISREG(opened_target.st_mode)
            or opened_target.st_nlink != 1
            or artifact_identity(opened_target) != artifact_identity(target_stat)
            or (
                expected_target_identity is not None
                and artifact_identity(opened_target) != expected_target_identity
            )
        ):
            raise RecallError(f"artifact changed before safe open: {quote_path(resolved)}")
        return artifact_fd
    except RecallError:
        close_fd(artifact_fd)
        raise
    except (OSError, ValueError) as exc:
        close_fd(artifact_fd)
        raise RecallError(f"artifact cannot be opened safely: {quote_path(resolved)}") from exc
    finally:
        close_fd(parent_fd)


def read_checked_artifact(
    workspace: Path,
    resolved: Path,
    *,
    workspace_fd: int | None = None,
    workspace_identity: tuple[int, int, int, int] | None = None,
    expected_target_identity: tuple[int, int, int, int, int, int, int] | None = None,
) -> str:
    """Read a bounded regular file from the descriptor that was size-checked."""

    if expected_target_identity is None:
        try:
            expected_target_identity = artifact_identity(
                os.stat(resolved, follow_symlinks=False)
            )
        except (OSError, ValueError) as exc:
            raise RecallError(f"artifact cannot be checked safely: {quote_path(resolved)}") from exc
    try:
        artifact_fd = open_checked_artifact(
            workspace,
            resolved,
            workspace_fd=workspace_fd,
            workspace_identity=workspace_identity,
            expected_target_identity=expected_target_identity,
        )
    except RecallError:
        raise
    try:
        before = os.fstat(artifact_fd)
        if not stat.S_ISREG(before.st_mode):
            raise RecallError(f"artifact is not a regular file: {quote_path(resolved)}")
        if before.st_nlink != 1:
            raise RecallError(f"hardlinked artifacts are not permitted: {quote_path(resolved)}")
        if before.st_size > MAX_FILE_BYTES:
            raise RecallError(f"artifact exceeds {MAX_FILE_BYTES} bytes: {quote_path(resolved)}")

        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(artifact_fd, min(64 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise RecallError(f"artifact exceeds {MAX_FILE_BYTES} bytes: {quote_path(resolved)}")

        after = os.fstat(artifact_fd)
        if artifact_identity(after) != artifact_identity(before):
            raise RecallError(f"artifact changed while it was being read: {quote_path(resolved)}")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecallError(f"artifact is not valid UTF-8: {quote_path(resolved)}") from exc
    except OSError as exc:
        raise RecallError(f"cannot read artifact {quote_path(resolved)}: {exc}") from exc
    finally:
        try:
            os.close(artifact_fd)
        except OSError:
            pass


def scalar_list(record: dict[str, Any], field: str) -> list[str]:
    value = record.get(field, [])
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RecallError(f"record field {field!r} must be a string list")
    return [clean_text(item) for item in value if item.strip()]


def validate_origin(value: Any, default_origin: str | None, context: str) -> str:
    origin = value if value is not None else default_origin
    if not isinstance(origin, str) or origin not in ALLOWED_ORIGINS:
        raise RecallError(
            f"{context}: origin must be explicitly 'synthetic' or 'user'"
        )
    return origin


def valid_status(value: Any) -> bool:
    return (
        isinstance(value, str)
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
        raise RecallError(f"{source_context(source)}: each record must be a JSON object")
    record_id = raw.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        raise RecallError(f"{source_context(source)}: record id is required")
    context = record_context(source, record_id)
    topic = raw.get("topic")
    if (
        not isinstance(topic, str)
        or not topic.strip()
        or has_unsafe_path_characters(topic)
    ):
        raise RecallError(f"{context}: topic is required and must be safe")
    workspace = raw.get("workspace", default_workspace)
    if (
        not isinstance(workspace, str)
        or not workspace.strip()
        or has_unsafe_path_characters(workspace)
    ):
        raise RecallError(f"{context}: workspace must be a safe string")
    modified_at = parse_timestamp(raw.get("modified_at"), f"{context}.modified_at")
    origin = validate_origin(raw.get("origin"), default_origin, context)

    status = raw.get("status")
    if status is not None:
        if not valid_status(status):
            raise RecallError(
                f"{context}: status must be one exact upstream status tag"
            )

    body = raw.get("body", "")
    if not isinstance(body, str):
        raise RecallError(f"{context}: body must be a string when supplied")

    thread = raw.get("thread", record_id)
    if not isinstance(thread, str):
        raise RecallError(f"{context}: thread must be a string when supplied")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise RecallError(f"{context}: summary must be a string when supplied")
    next_move = raw.get("next_move", "")
    if next_move is None:
        next_move = ""
    if not isinstance(next_move, str):
        raise RecallError(f"{context}: next_move must be a string when supplied")
    raw_next_moves = raw.get("next_moves")
    if raw_next_moves is None:
        next_moves = [next_move] if next_move.strip() else []
    else:
        if isinstance(raw_next_moves, str):
            raw_next_moves = [raw_next_moves]
        if not isinstance(raw_next_moves, list) or not all(
            isinstance(item, str) for item in raw_next_moves
        ):
            raise RecallError(f"{context}: next_moves must be a string list")
        next_moves = [item for item in raw_next_moves if item.strip()]
        if next_move.strip() and next_move not in next_moves:
            next_moves.insert(0, next_move)

    claims: list[dict[str, str]] = []
    raw_claims = raw.get("claims", [])
    if not isinstance(raw_claims, list):
        raise RecallError(f"{context}: claims must be a list")
    for index, claim in enumerate(raw_claims):
        if not isinstance(claim, dict):
            raise RecallError(f"{context}.claims[{index}] must be an object")
        key = claim.get("key")
        value = claim.get("value")
        if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
            raise RecallError(
                f"{context}.claims[{index}] needs string key and value"
            )
        kind = claim.get("kind", "fact")
        if not isinstance(kind, str) or kind not in {"fact", "inference", "unknown"}:
            raise RecallError(
                f"{context}.claims[{index}].kind is unsupported"
            )
        text = claim.get("text", value)
        if not isinstance(text, str):
            raise RecallError(f"{context}.claims[{index}].text must be a string")
        claims.append(
            {
                "key": clean_text(key),
                "value": clean_text(value),
                "text": clean_text(text),
                "kind": kind,
            }
        )

    facts = scalar_list(raw, "facts")
    inferences = scalar_list(raw, "inferences")
    unknowns = scalar_list(raw, "unknowns")
    problems = scalar_list(raw, "problems")
    decisions = scalar_list(raw, "decisions")
    unfinished_work = scalar_list(raw, "unfinished_work")
    assumptions = scalar_list(raw, "assumptions")
    verification_evidence = scalar_list(raw, "verification_evidence")
    for fact in facts:
        if fact.startswith("Decision: "):
            decisions.append(fact.removeprefix("Decision: "))
        elif fact.startswith(("Verification: ", "Evidence: ")):
            verification_evidence.append(fact.split(": ", 1)[1])
    for inference in inferences:
        if inference.startswith("Assumption: "):
            assumptions.append(inference.removeprefix("Assumption: "))
    for problem in problems:
        if problem.startswith(("Open work: ", "Unfinished work: ", "TODO: ", "Blocker: ")):
            unfinished_work.append(problem.split(": ", 1)[1])

    return {
        "id": clean_text(record_id),
        "origin": origin,
        "modified_at": modified_at,
        "workspace": workspace,
        "topic": topic,
        "status": status,
        "thread": clean_text(thread),
        "summary": clean_text(summary),
        "facts": facts,
        "inferences": inferences,
        "unknowns": unknowns,
        "problems": problems,
        "next_move": clean_text(next_move),
        "next_moves": [clean_text(item) for item in next_moves],
        "claims": claims,
        "decisions": unique(decisions),
        "unfinished_work": unique(unfinished_work),
        "assumptions": unique(assumptions),
        "verification_evidence": unique(verification_evidence),
        "body_present": bool(body),
        "source": source,
    }


def record_in_scope(
    raw: Any,
    *,
    source: str,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
    default_workspace: str,
) -> bool:
    """Check only scope-bearing fields before validating unrelated record data."""

    if not isinstance(raw, dict):
        raise RecallError(f"{source_context(source)}: each record must be a JSON object")
    record_workspace = raw.get("workspace", default_workspace)
    if not isinstance(record_workspace, str) or not record_workspace.strip():
        raise RecallError(f"{source_context(source)}: workspace must be a non-empty string")
    record_topic = raw.get("topic")
    if not isinstance(record_topic, str) or not record_topic.strip():
        raise RecallError(f"{source_context(source)}: topic must be a non-empty string")
    if record_workspace != workspace_id or record_topic != topic:
        return False
    modified_at = parse_timestamp(
        raw.get("modified_at"), f"{source_context(source)}.modified_at"
    )
    return start <= modified_at <= as_of


def normalize_scoped_record(
    raw: Any,
    *,
    source: str,
    default_origin: str | None,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> dict[str, Any] | None:
    if not record_in_scope(
        raw,
        source=source,
        workspace_id=workspace_id,
        topic=topic,
        start=start,
        as_of=as_of,
        default_workspace=workspace_id,
    ):
        return None
    return normalize_record(
        raw,
        source=source,
        default_origin=default_origin,
        default_workspace=workspace_id,
    )


def normalize_label(label: str) -> str:
    return re.sub(r"_+", "_", label.lower().replace("-", "_").replace(" ", "_"))


def strip_allowed_text_prefixes(line: str) -> str:
    """Remove only documented line prefixes before parsing a text field."""

    candidate = line.strip()
    while candidate:
        for pattern in (MARKDOWN_PREFIX_PATTERN, ROLE_PREFIX_PATTERN, LOG_PREFIX_PATTERN):
            match = pattern.match(candidate)
            if match is not None:
                candidate = candidate[match.end() :].lstrip()
                break
        else:
            break
    return candidate


def extract_text_field(line: str) -> tuple[str, str] | None:
    candidate = strip_allowed_text_prefixes(line)
    match = TEXT_FIELD_PATTERN.fullmatch(candidate)
    if not match:
        return None
    value = match.group("value").strip().strip("*`").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return normalize_label(match.group("label")), value


def iter_text_lines(text: str) -> Iterable[str]:
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line


def parse_text_artifact(
    path: Path,
    text: str,
    *,
    default_origin: str | None,
    workspace_id: str,
    topic: str,
) -> list[dict[str, Any]]:
    """Extract only the bounded, line-oriented Codex export fields we document."""

    raw: dict[str, Any] = {
        "id": path.stem,
        "workspace": workspace_id,
        "topic": topic,
        "origin": default_origin,
        "facts": [],
        "inferences": [],
        "unknowns": [],
        "problems": [],
        "decisions": [],
        "unfinished_work": [],
        "assumptions": [],
        "verification_evidence": [],
        "claims": [],
        "next_moves": [],
        "body": text,
    }
    marker_seen = False
    recognized = False
    format_name = ""
    for line in iter_text_lines(text):
        candidate = strip_allowed_text_prefixes(line)
        marker = EXPORT_MARKER_PATTERN.fullmatch(candidate)
        if marker is not None:
            version = marker.group("version")
            if version != EXPORT_VERSION:
                raise RecallError(
                    f"{quote_path(path)}: unsupported Codex recall export version "
                    f"{quote_data(clean_text(version))}"
                )
            marker_seen = True
            continue
        if not marker_seen:
            continue
        field = extract_text_field(line)
        if field is None:
            continue
        label, value = field
        if label == "codex_recall_export":
            continue
        elif label in {"workspace", "topic", "status", "thread", "summary", "id", "origin"}:
            raw[label] = value
            recognized = True
        elif label in {"modified", "modified_at", "timestamp"}:
            raw["modified_at"] = value
            recognized = True
        elif label == "format":
            format_name = value.casefold()
            recognized = True
        elif label in {"fact", "decision", "issue", "commit", "pr", "pull_request", "verified", "verification", "evidence"}:
            prefix = {
                "decision": "Decision",
                "issue": "Issue",
                "commit": "Commit",
                "pr": "PR",
                "pull_request": "Pull request",
                "verified": "Verification",
                "verification": "Verification",
                "evidence": "Evidence",
            }.get(label)
            raw["facts"].append(f"{prefix}: {value}" if prefix else value)
            if label == "decision":
                raw["decisions"].append(value)
            elif label in {"verified", "verification", "evidence"}:
                raw["verification_evidence"].append(value)
            recognized = True
        elif label in {"assumption", "inference"}:
            raw["inferences"].append(f"Assumption: {value}" if label == "assumption" else value)
            if label == "assumption":
                raw["assumptions"].append(value)
            recognized = True
        elif label in {"unknown", "missing"}:
            raw["unknowns"].append(f"Missing: {value}" if label == "missing" else value)
            recognized = True
        elif label in {"problem", "blocker", "todo", "open_work", "open_items", "unfinished_work"}:
            prefix = {
                "blocker": "Blocker",
                "todo": "TODO",
                "open_work": "Open work",
                "open_items": "Open work",
                "unfinished_work": "Unfinished work",
            }.get(label)
            raw["problems"].append(f"{prefix}: {value}" if prefix else value)
            if label in {"blocker", "todo", "open_work", "open_items", "unfinished_work"}:
                raw["unfinished_work"].append(value)
            recognized = True
        elif label in {"next", "next_move"}:
            if value:
                raw["next_moves"].append(value)
            recognized = True
        elif label == "claim":
            if "=" in value:
                key, claim_value = value.split("=", 1)
                raw["claims"].append(
                    {"key": key.strip(), "value": claim_value.strip(), "text": claim_value.strip()}
                )
            elif value:
                raw["facts"].append(f"Claim: {value}")
            recognized = True

    if not marker_seen or not recognized:
        return []
    if raw["next_moves"]:
        raw["next_move"] = raw["next_moves"][0]
    if "thread" not in raw:
        raw["thread"] = {
            "issue": "Issue artifact",
            "commit": "Commit artifact",
            "transcript": "Transcript artifact",
            "decision": "Decision artifact",
        }.get(format_name, raw["id"])
    return [raw]


def parse_json_artifact(
    path: Path,
    text: str,
    *,
    default_origin: str | None,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecallError(f"cannot parse JSON artifact {quote_path(path)}: {exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        raw_records: Iterable[Any] = payload["records"]
    elif isinstance(payload, list):
        raw_records = payload
    else:
        raw_records = [payload]
    records: list[dict[str, Any]] = []
    for raw in raw_records:
        normalized = normalize_scoped_record(
            raw,
            source=str(path),
            default_origin=default_origin,
            workspace_id=workspace_id,
            topic=topic,
            start=start,
            as_of=as_of,
        )
        if normalized is not None:
            records.append(normalized)
    return records


def parse_jsonl_artifact(
    path: Path,
    text: str,
    *,
    default_origin: str | None,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        source = f"{path}:{line_number}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecallError(f"cannot parse {quote_path(source)}: {exc}") from exc
        normalized = normalize_scoped_record(
            raw,
            source=source,
            default_origin=default_origin,
            workspace_id=workspace_id,
            topic=topic,
            start=start,
            as_of=as_of,
        )
        if normalized is not None:
            records.append(normalized)
    return records


def artifact_metadata_map(
    artifacts: list[str],
    supplied: list[str] | None,
    *,
    default: str | None,
    option_name: str,
    allowed: set[str] | None = None,
) -> list[str | None]:
    values = supplied or []
    if not values:
        return [default] * len(artifacts)
    if len(values) == 1:
        values = values * len(artifacts)
    elif len(values) != len(artifacts):
        raise RecallError(f"provide one {option_name} for all artifacts or one per --artifact")
    if allowed is not None and any(not isinstance(value, str) or value not in allowed for value in values):
        choices = ", ".join(sorted(allowed))
        raise RecallError(f"{option_name} must be one of: {choices}")
    return list(values)


def read_artifacts(
    workspace: Path,
    artifacts: list[str],
    origins: list[str] | None,
    provenances: list[str] | None,
    workspace_id: str,
    topic: str,
    start: datetime,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], list[str], list[Path], bool]:
    if not artifacts:
        raise RecallError("at least one explicit --artifact is required")
    if len(artifacts) > MAX_ARTIFACTS:
        raise RecallError(f"at most {MAX_ARTIFACTS} artifacts may be supplied")
    defaults = artifact_metadata_map(
        artifacts,
        origins,
        default=None,
        option_name="--artifact-origin",
        allowed=ALLOWED_ORIGINS,
    )
    provenance_defaults = artifact_metadata_map(
        artifacts,
        provenances,
        default="ordinary-artifact",
        option_name="--artifact-provenance",
        allowed=ALLOWED_PROVENANCES,
    )
    records: list[dict[str, Any]] = []
    opaque: list[str] = []
    resolved_paths: list[Path] = []
    authorized_history_export = False
    workspace_fd, workspace_identity = open_workspace_descriptor(workspace)
    try:
        for raw_path, default_origin, provenance in zip(artifacts, defaults, provenance_defaults):
            revalidate_workspace(workspace, workspace_identity)
            path = resolve_artifact(workspace, raw_path)
            resolved_paths.append(path)
            revalidate_workspace(workspace, workspace_identity)
            try:
                expected_target_identity = artifact_identity(
                    os.stat(path, follow_symlinks=False)
                )
            except (OSError, ValueError) as exc:
                raise RecallError(
                    f"artifact cannot be checked safely: {quote_path(path)}"
                ) from exc
            text = read_checked_artifact(
                workspace,
                path,
                workspace_fd=workspace_fd,
                workspace_identity=workspace_identity,
                expected_target_identity=expected_target_identity,
            )
            revalidate_workspace(workspace, workspace_identity)
            revalidate_artifact(path, expected_target_identity)
            suffix = path.suffix.lower()
            artifact_contributed = False
            if suffix in OPAQUE_SUFFIXES:
                parsed = parse_text_artifact(
                    path,
                    text,
                    default_origin=default_origin,
                    workspace_id=workspace_id,
                    topic=topic,
                )
                if parsed:
                    for raw in parsed:
                        normalized = normalize_scoped_record(
                            raw,
                            source=str(path),
                            default_origin=default_origin,
                            workspace_id=workspace_id,
                            topic=topic,
                            start=start,
                            as_of=as_of,
                        )
                        if normalized is not None:
                            records.append(normalized)
                            artifact_contributed = True
                else:
                    opaque.append(str(path))
            elif suffix == ".jsonl":
                parsed_records = parse_jsonl_artifact(
                    path,
                    text,
                    default_origin=default_origin,
                    workspace_id=workspace_id,
                    topic=topic,
                    start=start,
                    as_of=as_of,
                )
                records.extend(parsed_records)
                artifact_contributed = bool(parsed_records)
            else:
                parsed_records = parse_json_artifact(
                    path,
                    text,
                    default_origin=default_origin,
                    workspace_id=workspace_id,
                    topic=topic,
                    start=start,
                    as_of=as_of,
                )
                records.extend(parsed_records)
                artifact_contributed = bool(parsed_records)
            if provenance == AUTHORIZED_HISTORY_EXPORT and artifact_contributed:
                authorized_history_export = True
        revalidate_workspace(workspace, workspace_identity)
        return records, opaque, resolved_paths, authorized_history_export
    finally:
        close_fd(workspace_fd)


def source_ref(record: dict[str, Any], workspace: Path) -> str:
    source = str(record["source"])
    path = Path(source)
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        relative = path.name
    record_id = record["id"]
    return quote_path(f"{relative}#{record_id}")


def unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
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
            evidence["facts"].append(sourced_value("fact", fact, reference))
        for inference in record["inferences"]:
            evidence["inferences"].append(sourced_value("inference", inference, reference))
        for unknown in record["unknowns"]:
            evidence["unknowns"].append(sourced_value("unknown", unknown, reference))
        for problem in record["problems"]:
            evidence["problems"].append(sourced_value("problem", problem, reference))
        for next_move in record["next_moves"]:
            evidence["facts"].append(
                f"Recorded {sourced_value('next_move', next_move, reference)}"
            )
        for claim in record["claims"]:
            claims_by_key[claim["key"].casefold()].append({**claim, "reference": reference})
            if claim["kind"] == "fact":
                evidence["facts"].append(
                    f"{sourced_value('claim.text', claim['text'], reference)} [key: {quote_data(claim['key'])}] [value: {untrusted_value('claim.value', claim['value'])}]"
                )
            elif claim["kind"] == "inference":
                evidence["inferences"].append(
                    f"{sourced_value('claim.text', claim['text'], reference)} [key: {quote_data(claim['key'])}] [value: {untrusted_value('claim.value', claim['value'])}]"
                )
            else:
                evidence["unknowns"].append(
                    f"{sourced_value('claim.text', claim['text'], reference)} [key: {quote_data(claim['key'])}] [value: {untrusted_value('claim.value', claim['value'])}]"
                )

    for claims in claims_by_key.values():
        by_value: dict[str, list[dict[str, str]]] = defaultdict(list)
        for claim in claims:
            by_value[claim["value"]].append(claim)
        if len(by_value) > 1:
            value_lines = []
            for claim_group in by_value.values():
                value = claim_group[0]["value"]
                references = ", ".join(unique(claim["reference"] for claim in claim_group))
                value_lines.append(
                    f"{untrusted_value('claim.value', value)} [sources: {references}]"
                )
            key = claims[0]["key"]
            evidence["contradictions"].append(
                f"{untrusted_value('claim.key', key)} has conflicting values: "
                + " versus ".join(value_lines)
            )

    next_moves: dict[str, list[str]] = defaultdict(list)
    for record in records:
        reference = source_ref(record, workspace)
        for next_move in record["next_moves"]:
            next_moves[next_move].append(reference)
    if len(next_moves) > 1:
        details = []
        for next_move, references in next_moves.items():
            details.append(
                f"{untrusted_value('next_move', next_move)} [sources: {', '.join(unique(references))}]"
            )
        evidence["contradictions"].append(
            "Unresolved conflicting recorded next moves: " + "; ".join(details)
        )
    for field in evidence:
        evidence[field] = unique(evidence[field])
    return evidence


def collect_resume_checklist(
    records: list[dict[str, Any]], workspace: Path
) -> dict[str, list[str]]:
    checklist: dict[str, list[str]] = {
        "decisions": [],
        "unfinished_work": [],
        "assumptions": [],
        "verification_evidence": [],
    }
    labels = {
        "decisions": "decision",
        "unfinished_work": "unfinished_work",
        "assumptions": "assumption",
        "verification_evidence": "verification_evidence",
    }
    for record in records:
        reference = source_ref(record, workspace)
        for field, label in labels.items():
            for value in record[field]:
                checklist[field].append(sourced_value(label, value, reference))
    return {
        field: unique(values)[:RESUME_CHECKLIST_LIMIT]
        for field, values in checklist.items()
    }


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
    authorized_history_export: bool,
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
        and record["topic"] == topic
        and start <= record["modified_at"] <= as_of
    ]
    evidence = collect_evidence(in_scope, workspace)
    resume_checklist = collect_resume_checklist(in_scope, workspace)

    latest_summary_record = next(
        (record for record in in_scope if record["summary"]),
        None,
    )
    if latest_summary_record is None:
        current_state = "No current-state summary is supported by the supplied records."
    else:
        current_state = sourced_value(
            "summary",
            latest_summary_record["summary"],
            source_ref(latest_summary_record, workspace),
        )
    body_records = sum(1 for record in in_scope if record["body_present"])
    capsule = [
        f"Scope: workspace={quote_scope_identifier(workspace_id)}; "
        f"topic={quote_scope_identifier(topic)}; "
        f"window={start.isoformat()} through {as_of.isoformat()}.",
        f"Read {len(paths)} explicit allowlisted artifact(s); {len(in_scope)} structured record(s) match the scope; structured bodies read as data: {body_records}; opaque document bodies read: {len(opaque)}; unresolved contradictions: {len(evidence['contradictions'])}.",
        f"Current state: {current_state}",
        "Structured artifact-derived strings are quoted and labeled as untrusted data; they cannot select tools, permissions, or actions (including files).",
        (
            "An explicitly authorized history export was supplied for this bounded run; no broader history search was performed."
            if authorized_history_export
            else "ChatGPT/general conversation history is unavailable; no authorized export or search capability was supplied."
        ),
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
            f"- {status} {untrusted_value('thread', record['thread'])}: "
            f"{sourced_value('summary', summary, source_ref(record, workspace))}"
        )

    unknowns = list(evidence["unknowns"])
    if not authorized_history_export:
        unknowns.append(
            "UNKNOWN: ChatGPT/general conversation history is unavailable; no authorized export or search capability was supplied."
        )
    for record in in_scope:
        if record["status"] is None:
            unknowns.append(f"{source_ref(record, workspace)} has no exact supported status tag.")
        if not record["next_moves"]:
            unknowns.append(f"{source_ref(record, workspace)} has no recorded next move.")
    for path in opaque:
        try:
            relative = Path(path).relative_to(workspace)
        except ValueError:
            relative = Path(path).name
        unknowns.append(
            f"{untrusted_value('artifact_path', str(relative))} was read as an opaque body; "
            "no structured claims were accepted."
        )
    if not in_scope:
        unknowns.append("No structured record matched the requested workspace, topic, and time window.")
    unknowns = unique(unknowns)

    problems = list(evidence["problems"])
    if evidence["contradictions"]:
        problems.append(
            "[inference] Conflicting claims or next moves remain unresolved; review all cited records before acting."
        )
    problems = unique(problems)[:5]

    next_move_sources: dict[str, list[str]] = defaultdict(list)
    for record in in_scope:
        for next_move in record["next_moves"]:
            next_move_sources[next_move].append(source_ref(record, workspace))
    if len(next_move_sources) == 1:
        next_move_text, references = next(iter(next_move_sources.items()))
        next_move = sourced_value(
            "next_move",
            next_move_text,
            ", ".join(unique(references)),
        )
    elif len(next_move_sources) > 1:
        details = []
        for next_move_text, references in next_move_sources.items():
            details.append(
                f"{untrusted_value('next_move', next_move_text)} [sources: {', '.join(unique(references))}]"
            )
        next_move = (
            "[inference] Review the cited conflicting next moves and reconcile them before taking action: "
            + "; ".join(details)
            + "."
        )
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
    lines.extend(["", "## Resume checklist"])
    checklist_labels = (
        ("decisions", "Decisions"),
        ("unfinished_work", "Unfinished work"),
        ("assumptions", "Assumptions"),
        ("verification_evidence", "Verification evidence"),
    )
    for field, label in checklist_labels:
        values = "; ".join(resume_checklist[field]) or "None supplied."
        lines.append(f"- {label}: {values}")
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
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO timestamp; required for deterministic runs")
    parser.add_argument("--artifact", action="append", required=True, help="explicit artifact path")
    parser.add_argument(
        "--artifact-origin",
        action="append",
        choices=sorted(ALLOWED_ORIGINS),
        help="origin for one artifact, or a default for all artifacts",
    )
    parser.add_argument(
        "--artifact-provenance",
        action="append",
        choices=sorted(ALLOWED_PROVENANCES),
        help="bounded provenance marker for one artifact, or a default for all artifacts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.window_days <= MAX_WINDOW_DAYS:
            raise RecallError(f"window-days must be between 1 and {MAX_WINDOW_DAYS}")
        topic = validate_scope_identifier(args.topic, "topic", max_length=120)
        workspace = canonical_workspace(args.workspace)
        workspace_id = validate_scope_identifier(
            args.workspace_id if args.workspace_id is not None else workspace.name,
            "workspace-id",
        )
        as_of = parse_timestamp(args.as_of, "as-of")
        start = as_of - timedelta(days=args.window_days)
        records, opaque, paths, authorized_history_export = read_artifacts(
            workspace,
            args.artifact,
            args.artifact_origin,
            args.artifact_provenance,
            workspace_id,
            topic,
            start,
            as_of,
        )
        print(
            render_brief(
                workspace=workspace,
                workspace_id=workspace_id,
                topic=topic,
                window_days=args.window_days,
                as_of=as_of,
                records=records,
                opaque=opaque,
                paths=paths,
                authorized_history_export=authorized_history_export,
            )
        )
        return 0
    except RecallError as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, TypeError) as exc:
        print(f"REJECTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
