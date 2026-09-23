"""Append one sanitized row to a six-column audit TSV."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


HEADER = ("ts", "phase", "decision", "why", "evidence", "result")


class AuditLogError(ValueError):
    """The log path or row cannot satisfy the local audit contract."""


def sanitize_cell(value: str) -> str:
    """Keep a cell on one row and neutralize spreadsheet formula prefixes."""

    if not isinstance(value, str):
        raise AuditLogError("audit log fields must be strings")
    cleaned = value.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    return f"'{cleaned}" if cleaned.startswith(("=", "+", "-", "@")) else cleaned


def _timestamp(clock: Callable[[], datetime] | None) -> str:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _evidence_value(evidence: str | os.PathLike[str] | Sequence[str | os.PathLike[str]]) -> str:
    if isinstance(evidence, (str, os.PathLike)):
        value = os.fspath(evidence)
    else:
        value = ";".join(os.fspath(item) for item in evidence)
    if not value:
        raise AuditLogError("evidence path must not be empty")
    return value


def append_row(
    logfile: str | os.PathLike[str],
    phase: str,
    decision: str,
    why: str,
    evidence: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    result: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Append one row and return its rendered TSV line."""

    path = Path(logfile)
    if path.is_symlink():
        raise AuditLogError(f"audit log must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise AuditLogError(f"audit log must be a regular file: {path}")
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            descriptor = -1
        if descriptor != -1:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write("\t".join(HEADER) + "\n")
    fields = (
        _timestamp(clock),
        sanitize_cell(phase),
        sanitize_cell(decision),
        sanitize_cell(why),
        sanitize_cell(_evidence_value(evidence)),
        sanitize_cell(result),
    )
    line = "\t".join(fields) + "\n"
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(line)
    return line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logfile")
    parser.add_argument("phase")
    parser.add_argument("decision")
    parser.add_argument("why")
    parser.add_argument("evidence")
    parser.add_argument("result")
    args = parser.parse_args(argv)
    try:
        append_row(args.logfile, args.phase, args.decision, args.why, args.evidence, args.result)
    except AuditLogError as exc:
        parser.error(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
