"""No-fetch worktree audit over explicit local metadata.

The audit does not invoke Git, a forge CLI, a fetch, a home-directory scan,
or a transcript search.  The caller supplies the worktree and PR/activity
metadata it is authorized to use.  Missing activity stays ``unknown`` and is
visible in the report instead of being inferred from a private session store.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class AuditInputError(ValueError):
    """Explicit metadata is malformed or outside the supported schema."""


def _text(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise AuditInputError(f"{label} must be a non-empty string")
    if any(character in value for character in "\x00\r\n"):
        raise AuditInputError(f"{label} contains a control character")
    return value


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AuditInputError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditInputError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise AuditInputError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Activity:
    status: str
    at: datetime | None
    source: str | None

    @classmethod
    def from_mapping(cls, value: Any, *, label: str = "activity") -> "Activity":
        if value is None:
            return cls("unknown", None, None)
        if not isinstance(value, Mapping):
            raise AuditInputError(f"{label} must be an object or null")
        status = value.get("status", "unknown")
        if status not in {"known", "unknown"}:
            raise AuditInputError(f"{label}.status must be known or unknown")
        source = value.get("source")
        if source is not None:
            source = _text(source, f"{label}.source")
        at = None if value.get("at") is None else _parse_time(value.get("at"), f"{label}.at")
        if status == "known" and at is None:
            raise AuditInputError(f"{label}.at is required for known activity")
        return cls(status, at, source)


@dataclass(frozen=True)
class WorktreeMetadata:
    path: str
    branch: str | None
    head: str | None
    dirty: str
    remote: str | None
    pr: str | None
    merged: bool | None
    activity: Activity
    is_main: bool = False

    @classmethod
    def from_mapping(cls, value: Any, *, is_main: bool = False, label: str = "worktree") -> "WorktreeMetadata":
        if not isinstance(value, Mapping):
            raise AuditInputError(f"{label} must be an object")
        path = _text(value.get("path"), f"{label}.path")
        branch = value.get("branch")
        if branch is not None:
            branch = _text(branch, f"{label}.branch")
        head = value.get("head")
        if head is not None:
            head = _text(head, f"{label}.head")
        dirty = value.get("dirty", "unknown")
        if dirty not in {"clean", "wip", "scratch", "unknown"}:
            raise AuditInputError(f"{label}.dirty is unsupported")
        remote = value.get("remote")
        if remote is not None:
            remote = _text(remote, f"{label}.remote")
        pr_value = value.get("pr")
        pr: str | None
        if pr_value is None:
            pr = None
        elif isinstance(pr_value, Mapping):
            number = pr_value.get("number")
            state = pr_value.get("state")
            if type(number) is not int or number <= 0 or state not in {"OPEN", "MERGED", "CLOSED"}:
                raise AuditInputError(f"{label}.pr is malformed")
            pr = f"#{number}/{state}"
        else:
            pr = _text(pr_value, f"{label}.pr")
        merged = value.get("merged")
        if merged is not None and type(merged) is not bool:
            raise AuditInputError(f"{label}.merged must be boolean or null")
        activity_value = value.get("activity")
        if activity_value is None and "last_activity" in value:
            activity_value = {
                "status": "known",
                "at": value.get("last_activity"),
                "source": value.get("activity_source", "explicit-metadata"),
            }
        elif activity_value is None and "activity_artifact" in value:
            activity_value = {
                "status": "unknown",
                "source": value.get("activity_artifact"),
            }
        return cls(
            path,
            branch,
            head,
            dirty,
            remote,
            pr,
            merged,
            Activity.from_mapping(activity_value, label=f"{label}.activity"),
            is_main,
        )


@dataclass(frozen=True)
class WorktreeRow:
    path: str
    branch: str | None
    head: str | None
    dirty: str
    remote: str | None
    pr: str | None
    merged: bool | None
    activity: str
    activity_at: str | None
    activity_age_days: int | None
    bucket: str
    unknowns: tuple[str, ...]
    is_main: bool


@dataclass(frozen=True)
class AuditReport:
    rows: tuple[WorktreeRow, ...]
    fetch_attempted: bool
    transcript_scan_attempted: bool
    merge_performed: bool
    deletion_performed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [asdict(row) for row in self.rows],
            "fetch_attempted": self.fetch_attempted,
            "transcript_scan_attempted": self.transcript_scan_attempted,
            "merge_performed": self.merge_performed,
            "deletion_performed": self.deletion_performed,
        }


def _as_of(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AuditInputError("as_of must include a timezone")
        return value.astimezone(timezone.utc)
    return _parse_time(value, "as_of")


def _row(metadata: WorktreeMetadata, as_of: datetime, recent_days: int) -> WorktreeRow:
    unknowns: list[str] = []
    if metadata.branch is None:
        unknowns.append("branch")
    if metadata.head is None:
        unknowns.append("head")
    if metadata.remote is None:
        unknowns.append("remote")
    if metadata.pr is None:
        unknowns.append("pr")
    if metadata.merged is None:
        unknowns.append("merged")
    if metadata.activity.status != "known" or metadata.activity.at is None:
        unknowns.append("activity")
        activity_age = None
        activity_at = None
    else:
        activity_at = metadata.activity.at.isoformat(timespec="seconds").replace("+00:00", "Z")
        age_seconds = max(0, int((as_of - metadata.activity.at).total_seconds()))
        activity_age = age_seconds // 86400
    if metadata.dirty == "wip":
        bucket = "hold-wip"
    elif metadata.pr is not None and metadata.pr.endswith("/OPEN"):
        bucket = "hold-open-pr"
    elif metadata.activity.status != "known":
        bucket = "unknown-activity"
    elif metadata.activity.at is not None and activity_age is not None and activity_age <= recent_days:
        bucket = "verify-recent-activity"
    elif metadata.merged is True or metadata.pr is not None:
        bucket = "safe"
    else:
        bucket = "review"
    return WorktreeRow(
        metadata.path,
        metadata.branch,
        metadata.head,
        metadata.dirty,
        metadata.remote,
        metadata.pr,
        metadata.merged,
        metadata.activity.status,
        activity_at,
        activity_age,
        bucket,
        tuple(unknowns),
        metadata.is_main,
    )


def audit_worktrees(
    worktrees: Sequence[WorktreeMetadata],
    *,
    as_of: datetime | str | None = None,
    recent_days: int = 4,
    include_main: bool = False,
) -> AuditReport:
    """Classify explicit metadata without fetching, scanning, or mutating."""

    if recent_days < 0:
        raise AuditInputError("recent_days must be zero or positive")
    current = _as_of(as_of)
    selected = [item for item in worktrees if include_main or not item.is_main]
    rows = tuple(sorted((_row(item, current, recent_days) for item in selected), key=lambda row: row.path))
    return AuditReport(rows, False, False, False, False)


def load_metadata(path: str | Path) -> tuple[WorktreeMetadata, ...]:
    metadata_path = Path(path).expanduser()
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise AuditInputError(f"metadata must be an explicit regular file: {metadata_path}")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"could not read metadata: {exc}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("worktrees"), list):
        raise AuditInputError("metadata must contain a worktrees array")
    main_path = payload.get("main_worktree")
    result: list[WorktreeMetadata] = []
    for index, value in enumerate(payload["worktrees"]):
        is_main = main_path is not None and isinstance(value, Mapping) and value.get("path") == main_path
        result.append(WorktreeMetadata.from_mapping(value, is_main=is_main, label=f"worktrees[{index}]"))
    return tuple(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, help="explicit local JSON metadata")
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO timestamp")
    parser.add_argument("--recent-days", type=int, default=4)
    parser.add_argument("--include-main", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_worktrees(
            load_metadata(args.metadata),
            as_of=args.as_of,
            recent_days=args.recent_days,
            include_main=args.include_main,
        )
    except AuditInputError as exc:
        parser.error(str(exc))
        return 2
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
