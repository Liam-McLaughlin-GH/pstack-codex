"""Local file-backed orchestration bookkeeping.

This module is intentionally a store, not an orchestrator runtime.  It can
record units, verification, inbox pointers, gates, a stack frontier, and a
status report.  It has no subprocess runner and exposes no agent-launch,
merge, or worktree-delete operation.  A caller supplies a ``StackReader`` for
frontier data, which keeps Graphite or another stack provider outside the
store.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


UNIT_HEADER = "id\ttrack\tstate\tbranch\tpr\tsha\tbrief"
LEDGER_HEADER = "pr\tsha\tverdict\tevidence\tverifier\tts"
LOCK_NAME = ".orch.lock"
VERDICTS = {
    "live-ui-verified",
    "unit-test-verified",
    "type-check-only",
    "verifier-blocked",
    "verifier-failed",
}
FRONTIER_STATES = {"OPEN", "MERGED", "CLOSED"}


class StoreError(RuntimeError):
    """A local store operation failed or found malformed state."""


class NotFoundError(StoreError):
    pass


class UnsupportedSideEffect(StoreError):
    """The requested operation is outside the store's offline boundary."""


@dataclass(frozen=True)
class Unit:
    id: str
    track: str
    state: str
    branch: str
    pr: str
    sha: str
    brief: str


@dataclass(frozen=True)
class LedgerEntry:
    pr: str
    sha: str
    verdict: str
    evidence: str
    verifier: str
    ts: str


@dataclass(frozen=True)
class InboxPointer:
    ts: str
    agent: str
    unit: str
    status: str
    report: str


@dataclass(frozen=True)
class OpenGate:
    kind: str
    id: str
    question: str
    options: str
    default_answer: str


@dataclass(frozen=True)
class ResolvedGate:
    kind: str
    id: str
    question: str
    options: str
    default_answer: str
    answer: str


Gate = OpenGate | ResolvedGate


@dataclass(frozen=True)
class FrontierEntry:
    pr: int
    branches: str
    sha: str
    state: str

    def __post_init__(self) -> None:
        if type(self.pr) is not int or self.pr <= 0:
            raise StoreError("frontier PR must be a positive integer")
        _required_line(self.branches, "frontier branch")
        _required_line(self.sha, "frontier SHA")
        if self.state not in FRONTIER_STATES:
            raise StoreError(f"frontier state is unsupported: {self.state!r}")


@dataclass(frozen=True)
class Frontier:
    generation: int
    prs: tuple[FrontierEntry, ...]
    lowest_unmerged: int | None


@dataclass(frozen=True)
class StandingLine:
    number: int
    line: str


class StackReader(Protocol):
    """Injected source for a local, recorded, or provider-backed stack."""

    def read_frontier(self, repo: Path) -> Sequence[FrontierEntry]: ...


@dataclass(frozen=True)
class StaticStackReader:
    entries: tuple[FrontierEntry, ...]

    def read_frontier(self, repo: Path) -> Sequence[FrontierEntry]:
        del repo
        return self.entries


@dataclass(frozen=True)
class StatusReport:
    units: tuple[Unit, ...]
    ledger: tuple[LedgerEntry, ...]
    frontier: Frontier
    gates: tuple[Gate, ...]
    summary: dict[str, Any]
    changed: str


def _clean_cell(value: Any, label: str = "value") -> str:
    if not isinstance(value, str):
        raise StoreError(f"{label} must be a string")
    cleaned = value.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    return f"'{cleaned}" if cleaned.startswith(("=", "+", "-", "@")) else cleaned


def _required_cell(value: Any, label: str) -> str:
    cleaned = _clean_cell(value, label)
    if not cleaned.strip():
        raise StoreError(f"{label} must not be empty")
    return cleaned


def _required_line(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StoreError(f"{label} must be a string")
    cleaned = value.replace("\n", " ").replace("\r", " ").strip()
    if not cleaned:
        raise StoreError(f"{label} must not be empty")
    return cleaned


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise StoreError(f"{label} must be a positive integer")
    return value


def parse_verdict(value: str) -> str:
    if value not in VERDICTS:
        raise StoreError(
            "verdict must be live-ui-verified, unit-test-verified, type-check-only, verifier-blocked, or verifier-failed"
        )
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise StoreError("clock must return a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_root(path: str | Path) -> Path:
    root = Path(path).expanduser()
    ambient_temp = Path(tempfile.gettempdir()).absolute()
    if any(candidate.is_symlink() and not ambient_temp.is_relative_to(candidate.absolute()) for candidate in (root, *root.parents)):
        raise StoreError(f"store root must not be a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise StoreError(f"store root must be a directory: {root}")
    return root.resolve()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_if_missing(path: Path, text: str) -> None:
    if path.exists():
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read_required(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise StoreError(f"store file is missing or unsafe: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StoreError(f"could not read {path.name}: {exc}") from exc


def _read_tsv(path: Path, header: str, width: int) -> list[list[str]]:
    lines = _read_required(path).replace("\r", "").split("\n")
    if not lines or lines.pop(0) != header:
        raise StoreError(f"{path.name} has an invalid header")
    rows: list[list[str]] = []
    for line in lines:
        if not line:
            continue
        cells = line.split("\t")
        if len(cells) != width:
            raise StoreError(f"{path.name} has a malformed row")
        rows.append(cells)
    return rows


def _write_tsv(path: Path, header: str, rows: Sequence[Sequence[str]]) -> None:
    body = "\n".join("\t".join(_clean_cell(cell) for cell in row) for row in rows)
    _atomic_write(path, f"{header}\n{body}{'\n' if body else ''}")


def _counts(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class OrchestratorStore:
    """Synchronous local store with explicit lock and stack-reader seams."""

    def __init__(
        self,
        root: str | Path,
        *,
        force: bool = False,
        pid: int | None = None,
        pid_alive: Callable[[int], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        stack_reader: StackReader | None = None,
    ) -> None:
        self.root = _safe_root(root)
        self.force = force
        self.pid = os.getpid() if pid is None else pid
        self.pid_alive = pid_alive or _process_alive
        self.clock = clock or _utc_now
        self.stack_reader = stack_reader
        self._closed = False
        self._locked = False
        self._lock_descriptor: int | None = None

    @property
    def units(self) -> "_UnitsApi":
        return _UnitsApi(self)

    @property
    def ledger(self) -> "_LedgerApi":
        return _LedgerApi(self)

    @property
    def inbox(self) -> "_InboxApi":
        return _InboxApi(self)

    @property
    def gates(self) -> "_GatesApi":
        return _GatesApi(self)

    @property
    def frontier(self) -> "_FrontierApi":
        return _FrontierApi(self)

    @property
    def standing(self) -> "_StandingApi":
        return _StandingApi(self)

    @property
    def status(self) -> "_StatusApi":
        return _StatusApi(self)

    @property
    def initialized(self) -> bool:
        inbox = self.root / "inbox"
        return (self.root / "units.tsv").is_file() and inbox.is_dir() and not inbox.is_symlink()

    def _safe_inbox(self) -> Path:
        inbox = self.root / "inbox"
        if inbox.is_symlink() or not inbox.is_dir():
            raise StoreError("inbox is missing or unsafe")
        return inbox

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    def _acquire(self) -> None:
        self._ensure_open()
        if self._locked:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / LOCK_NAME
        owner = str(self.pid)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock, flags, 0o600)
        except OSError as exc:
            raise StoreError(f"could not open store lock: {exc}") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                try:
                    holder = lock.read_text(encoding="ascii").strip()
                except OSError:
                    holder = "unknown"
                raise StoreError(f"store lock held by pid {holder or 'unknown'}") from exc
            os.lseek(descriptor, 0, os.SEEK_SET)
            holder = os.read(descriptor, 128).decode("ascii", errors="replace").strip()
            is_stale = bool(re.fullmatch(r"[1-9][0-9]*", holder)) and not self.pid_alive(int(holder))
            if holder and not is_stale and not self.force:
                raise StoreError(f"store lock held by pid {holder}")
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, f"{owner}\n".encode("ascii"))
            os.fsync(descriptor)
            self._lock_descriptor = descriptor
            self._locked = True
            return
        except Exception:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

    def _release(self) -> None:
        lock = self.root / LOCK_NAME
        descriptor = self._lock_descriptor
        try:
            if descriptor is not None:
                lock.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StoreError(f"could not release store lock: {exc}") from exc
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                self._lock_descriptor = None
        self._locked = False

    def _begin_write(self) -> None:
        self._ensure_open()
        if not self.initialized:
            raise StoreError(f"store is not initialized at {self.root}; run init")
        self._acquire()

    def init(self) -> dict[str, str]:
        self._ensure_open()
        self.root.mkdir(parents=True, exist_ok=True)
        self._acquire()
        inbox = self.root / "inbox"
        if inbox.is_symlink() or (inbox.exists() and not inbox.is_dir()):
            self._release()
            raise StoreError("inbox is missing or unsafe")
        _write_if_missing(self.root / "units.tsv", f"{UNIT_HEADER}\n")
        _write_if_missing(self.root / "ledger.tsv", f"{LEDGER_HEADER}\n")
        inbox.mkdir(exist_ok=True)
        _write_if_missing(self.root / "gates.md", "")
        _write_if_missing(self.root / "preferences.md", "")
        _write_if_missing(self.root / "frontier.json", "{}\n")
        return {"store": str(self.root)}

    def close(self) -> None:
        if self._closed:
            return
        if self._locked:
            self._release()
        self._closed = True

    def __enter__(self) -> "OrchestratorStore":
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def units_add(self, unit_id: str, track: str, brief: str = "") -> Unit:
        self._begin_write()
        row = Unit(_required_cell(unit_id, "unit id"), _required_cell(track, "track"), "pending", "", "", "", _clean_cell(brief, "brief"))
        rows = self.units_list()
        if any(item.id == row.id for item in rows):
            raise StoreError(f"unit {row.id} already exists")
        rows.append(row)
        self._save_units(rows)
        return row

    def units_set(
        self,
        unit_id: str,
        state: str,
        *,
        branch: str | None = None,
        pr: int | None = None,
        sha: str | None = None,
    ) -> Unit:
        self._begin_write()
        clean_id = _required_cell(unit_id, "unit id")
        rows = self.units_list()
        index = next((index for index, row in enumerate(rows) if row.id == clean_id), -1)
        if index < 0:
            raise NotFoundError(f"unit {clean_id} not found")
        old = rows[index]
        row = Unit(
            old.id,
            old.track,
            _required_cell(state, "state"),
            old.branch if branch is None else _required_cell(branch, "branch"),
            old.pr if pr is None else str(_positive(pr, "PR")),
            old.sha if sha is None else _required_cell(sha, "SHA"),
            old.brief,
        )
        rows[index] = row
        self._save_units(rows)
        return row

    def _read_units(self) -> list[Unit]:
        return [Unit(*row) for row in _read_tsv(self.root / "units.tsv", UNIT_HEADER, 7)]

    def _save_units(self, rows: Sequence[Unit]) -> None:
        _write_tsv(self.root / "units.tsv", UNIT_HEADER, [asdict(row).values() for row in rows])

    def units_get(self, unit_id: str) -> Unit:
        self._ensure_open()
        clean_id = _required_cell(unit_id, "unit id")
        row = next((row for row in self._read_units() if row.id == clean_id), None)
        if row is None:
            raise NotFoundError(f"unit {clean_id} not found")
        return row

    def units_list(self, *, state: str | None = None, track: str | None = None) -> list[Unit]:
        self._ensure_open()
        rows = self._read_units()
        return [
            row
            for row in rows
            if (state is None or row.state == _required_cell(state, "state"))
            and (track is None or row.track == _required_cell(track, "track"))
        ]

    def units_counts(self) -> dict[str, int]:
        self._ensure_open()
        return _counts([row.state for row in self._read_units()])

    def ledger_record(
        self,
        pr: int,
        sha: str,
        verdict: str,
        evidence: str,
        verifier: str = "",
    ) -> LedgerEntry:
        self._begin_write()
        number = _positive(pr, "PR")
        parse_verdict(verdict)
        row = LedgerEntry(
            str(number),
            _required_cell(sha, "SHA"),
            verdict,
            _required_cell(evidence, "evidence"),
            _clean_cell(verifier, "verifier"),
            _timestamp(self.clock),
        )
        rows = self._read_ledger()
        index = next((index for index, item in enumerate(rows) if item.pr == row.pr and item.sha == row.sha), -1)
        if index < 0:
            rows.append(row)
        else:
            rows[index] = row
        self._save_ledger(rows)
        return row

    def _read_ledger(self) -> list[LedgerEntry]:
        result: list[LedgerEntry] = []
        for row in _read_tsv(self.root / "ledger.tsv", LEDGER_HEADER, 6):
            if row[2] not in VERDICTS:
                raise StoreError(f"ledger.tsv has invalid verdict {row[2]}")
            result.append(LedgerEntry(*row))
        return result

    def _save_ledger(self, rows: Sequence[LedgerEntry]) -> None:
        _write_tsv(self.root / "ledger.tsv", LEDGER_HEADER, [asdict(row).values() for row in rows])

    def ledger_check(self, pr: int, sha: str) -> LedgerEntry:
        self._ensure_open()
        number = str(_positive(pr, "PR"))
        clean_sha = _required_cell(sha, "SHA")
        row = next((item for item in self._read_ledger() if item.pr == number and item.sha == clean_sha), None)
        if row is None:
            raise NotFoundError("NOT-VERIFIED")
        return row

    def ledger_summary(self) -> dict[str, int]:
        self._ensure_open()
        return _counts([row.verdict for row in self._read_ledger()])

    def inbox_push(self, agent: str, unit: str, status: str, report: str = "") -> tuple[InboxPointer, str]:
        self._begin_write()
        pointer = InboxPointer(
            _timestamp(self.clock),
            _required_cell(agent, "agent"),
            _required_cell(unit, "unit"),
            _required_cell(status, "status"),
            _clean_cell(report, "report"),
        )
        inbox = self._safe_inbox()
        filename = f"{pointer.ts.replace(':', '-').replace('.', '-')}-{self.pid}-{uuid.uuid4().hex}.tsv"
        _atomic_write(inbox / filename, "\t".join(asdict(pointer).values()) + "\n")
        return pointer, filename

    def _read_inbox(self, directory: Path | None = None) -> list[InboxPointer]:
        inbox = self._safe_inbox() if directory is None else directory
        if not inbox.is_dir() or inbox.is_symlink():
            raise StoreError("inbox is missing or unsafe")
        result: list[InboxPointer] = []
        for path in sorted(inbox.glob("*.tsv")):
            raw = _read_required(path).rstrip("\n")
            if "\n" in raw or "\r" in raw:
                raise StoreError(f"inbox pointer {path.name} is malformed")
            row = raw.split("\t")
            if len(row) != 5:
                raise StoreError(f"inbox pointer {path.name} is malformed")
            result.append(InboxPointer(*row))
        return result

    def inbox_peek(self) -> list[InboxPointer]:
        self._ensure_open()
        return self._read_inbox()

    def inbox_count(self) -> int:
        return len(self.inbox_peek())

    def inbox_drain(self) -> list[InboxPointer]:
        self._begin_write()
        inbox = self.root / "inbox"
        rows = self._read_inbox()
        retired = self.root / f".inbox-drain-{self.pid}-{uuid.uuid4().hex}"
        os.replace(inbox, retired)
        try:
            inbox.mkdir()
        except OSError:
            os.replace(retired, inbox)
            raise
        # Retain the retired directory as a local recovery record.  No agent,
        # repository, worktree, merge, or deletion operation is performed.
        return rows

    def _read_gates(self) -> list[Gate]:
        raw = _read_required(self.root / "gates.md").replace("\r", "").strip()
        if not raw:
            return []
        if not raw.startswith("# Gates\n\n## "):
            raise StoreError("gates.md has an invalid heading")
        result: list[Gate] = []
        for block in raw[len("# Gates\n\n## ") :].split("\n\n## "):
            lines = [line for line in block.split("\n") if line]
            gate_id = lines.pop(0) if lines else ""
            fields: dict[str, str] = {}
            for line in lines:
                match = re.fullmatch(r"- ([^:]+): (.*)", line)
                if match is None:
                    raise StoreError(f"gates.md has a malformed gate {gate_id}")
                fields[match.group(1)] = match.group(2)
            required = [fields.get(name) for name in ("Question", "Options", "Default")]
            if not gate_id or any(value is None for value in required):
                raise StoreError(f"gates.md has a malformed gate {gate_id}")
            common = dict(id=gate_id, question=fields["Question"], options=fields["Options"], default_answer=fields["Default"])
            if fields.get("Status") == "open":
                result.append(OpenGate("open", **common))
            elif fields.get("Status") == "resolved" and "Answer" in fields:
                result.append(ResolvedGate("resolved", answer=fields["Answer"], **common))
            else:
                raise StoreError(f"gates.md has invalid status for {gate_id}")
        if len({gate.id for gate in result}) != len(result):
            raise StoreError("gates.md has duplicate gate ids")
        return result

    @staticmethod
    def _render_gates(rows: Sequence[Gate]) -> str:
        if not rows:
            return ""
        blocks: list[str] = []
        for gate in rows:
            answer = f"\n- Answer: {gate.answer}" if isinstance(gate, ResolvedGate) else ""
            blocks.append(
                f"## {gate.id}\n\n- Status: {gate.kind}\n- Question: {gate.question}\n- Options: {gate.options}\n- Default: {gate.default_answer}{answer}"
            )
        return "# Gates\n\n" + "\n\n".join(blocks) + "\n"

    def gate_park(self, gate_id: str, question: str, options: str, default_answer: str) -> OpenGate:
        self._begin_write()
        gate = OpenGate(
            "open",
            _required_line(gate_id, "gate id"),
            _required_line(question, "question"),
            _required_line(options, "options"),
            _required_line(default_answer, "default answer"),
        )
        rows = self._read_gates()
        index = next((index for index, item in enumerate(rows) if item.id == gate.id), -1)
        if index < 0:
            rows.append(gate)
        else:
            rows[index] = gate
        _atomic_write(self.root / "gates.md", self._render_gates(rows))
        return gate

    def gates_list(self) -> list[OpenGate]:
        self._ensure_open()
        return [gate for gate in self._read_gates() if isinstance(gate, OpenGate)]

    def gate_resolve(self, gate_id: str, answer: str) -> ResolvedGate:
        self._begin_write()
        clean_id = _required_line(gate_id, "gate id")
        rows = self._read_gates()
        index = next((index for index, item in enumerate(rows) if item.id == clean_id), -1)
        if index < 0:
            raise NotFoundError(f"gate {clean_id} not found")
        old = rows[index]
        resolved = ResolvedGate("resolved", old.id, old.question, old.options, old.default_answer, _required_line(answer, "answer"))
        rows[index] = resolved
        _atomic_write(self.root / "gates.md", self._render_gates(rows))
        return resolved

    def _read_frontier(self) -> Frontier:
        raw = _read_required(self.root / "frontier.json")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreError("frontier.json is not valid JSON") from exc
        if value == {}:
            return Frontier(0, (), None)
        if not isinstance(value, Mapping):
            raise StoreError("frontier.json must contain an object")
        generation = value.get("generation")
        rows = value.get("prs")
        lowest = value.get("lowest_unmerged")
        if type(generation) is not int or generation < 0 or not isinstance(rows, list):
            raise StoreError("frontier.json has an invalid shape")
        if lowest is not None and (type(lowest) is not int or lowest <= 0):
            raise StoreError("frontier.json has an invalid lowest_unmerged")
        parsed: list[FrontierEntry] = []
        for index, item in enumerate(rows):
            if not isinstance(item, Mapping):
                raise StoreError(f"frontier.json row {index} is not an object")
            try:
                parsed.append(FrontierEntry(item["pr"], item["branches"], item["sha"], item["state"]))
            except KeyError as exc:
                raise StoreError(f"frontier.json row {index} is missing {exc.args[0]}") from exc
        if len({row.pr for row in parsed}) != len(parsed):
            raise StoreError("frontier.json contains duplicate pull requests")
        return Frontier(generation, tuple(parsed), lowest)

    def frontier_show(self) -> Frontier:
        self._ensure_open()
        return self._read_frontier()

    def frontier_set(self, repo: str | Path, *, prs: Sequence[int] | None = None) -> Frontier:
        self._begin_write()
        if self.stack_reader is None:
            raise StoreError("frontier set requires an injected stack reader")
        repo_path = Path(repo).expanduser()
        if not repo_path.is_dir() or repo_path.is_symlink():
            raise StoreError(f"frontier repo is not a real directory: {repo_path}")
        entries = tuple(self.stack_reader.read_frontier(repo_path))
        if any(not isinstance(entry, FrontierEntry) for entry in entries):
            raise StoreError("stack reader returned invalid frontier data")
        if len({entry.pr for entry in entries}) != len(entries):
            raise StoreError("stack reader returned duplicate pull requests")
        actual = [entry.pr for entry in entries]
        if prs is not None:
            expected = [_positive(value, "PR") for value in prs]
            if actual != expected:
                raise StoreError(f"frontier pin mismatch: expected {expected}; got {actual}")
        old = self._read_frontier()
        value = Frontier(old.generation + 1, entries, next((entry.pr for entry in entries if entry.state == "OPEN"), None))
        _atomic_write(self.root / "frontier.json", json.dumps(asdict(value), indent=2) + "\n")
        return value

    def standing_show(self) -> list[StandingLine]:
        self._ensure_open()
        raw = _read_required(self.root / "preferences.md").replace("\r", "")
        result: list[StandingLine] = []
        for line in [value for value in raw.split("\n") if value]:
            match = re.fullmatch(r"([1-9][0-9]*)\. (.+)", line)
            if match is None or int(match.group(1)) != len(result) + 1:
                raise StoreError("preferences.md has malformed numbering")
            result.append(StandingLine(int(match.group(1)), match.group(2)))
        return result

    def standing_add(self, line: str) -> StandingLine:
        self._begin_write()
        rows = self.standing_show()
        item = StandingLine(len(rows) + 1, _required_line(line, "standing order"))
        rows.append(item)
        _atomic_write(self.root / "preferences.md", "\n".join(f"{row.number}. {row.line}" for row in rows) + "\n")
        return item

    def _summary(self, units: Sequence[Unit], ledger: Sequence[LedgerEntry], frontier: Frontier, gates: Sequence[Gate]) -> dict[str, Any]:
        return {
            "unit_states": _counts([row.state for row in units]),
            "ledger_verdicts": _counts([row.verdict for row in ledger]),
            "frontier_generation": frontier.generation,
            "open_gate_ids": sorted(gate.id for gate in gates if isinstance(gate, OpenGate)),
        }

    def status_render(self) -> StatusReport:
        self._begin_write()
        units = tuple(self._read_units())
        ledger = tuple(self._read_ledger())
        frontier = self._read_frontier()
        gates = tuple(self._read_gates())
        summary = self._summary(units, ledger, frontier, gates)
        status_path = self.root / "status.md"
        before: dict[str, Any] | None = None
        if status_path.is_file() and not status_path.is_symlink():
            match = re.search(r"<!-- orch-summary (.+) -->", status_path.read_text(encoding="utf-8"))
            if match:
                try:
                    value = json.loads(match.group(1))
                    if isinstance(value, dict):
                        before = value
                except json.JSONDecodeError:
                    before = None
        if before is None:
            changed = "first render"
        elif before == summary:
            changed = "no derived changes"
        else:
            changed = "derived summary changed"
        generated = _timestamp(self.clock)
        markdown = (
            "# Orchestrate status\n\n"
            f"Generated: {generated}\n\n"
            f"## Units\n\nStates: {json.dumps(summary['unit_states'], sort_keys=True)}\n\n"
            + _markdown_table(("ID", "Track", "State", "Branch", "PR", "SHA", "Brief"), [asdict(row).values() for row in units])
            + "\n\n## Verification ledger\n\n"
            + _markdown_table(("PR", "SHA", "Verdict", "Evidence", "Verifier", "Timestamp"), [asdict(row).values() for row in ledger])
            + "\n\n## Frontier\n\n"
            + _markdown_table(("Branch", "PR", "SHA", "State"), [(row.branches, row.pr, row.sha, row.state) for row in frontier.prs])
            + "\n\n## Gates\n\n"
            + _markdown_table(("ID", "Status", "Question", "Options", "Default", "Answer"), [
                (row.id, row.kind, row.question, row.options, row.default_answer, row.answer if isinstance(row, ResolvedGate) else "")
                for row in gates
            ])
            + f"\n\n<!-- orch-summary {json.dumps(summary, sort_keys=True)} -->\n"
        )
        _atomic_write(status_path, markdown)
        return StatusReport(units, ledger, frontier, gates, summary, changed)

    def refuse_side_effect(self, action: str) -> None:
        """Make unsupported destructive paths explicit and testable."""

        clean = _required_line(action, "side effect")
        raise UnsupportedSideEffect(
            f"offline orchestration store refuses {clean}; it never launches agents, merges, or deletes worktrees"
        )


def _params(value: Mapping[str, Any] | None, extra: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value or {})
    result.update(extra)
    return result


class _UnitsApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def add(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Unit:
        value = _params(params, kwargs)
        return self.store.units_add(value.get("id"), value.get("track"), value.get("brief", ""))

    def set(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Unit:
        value = _params(params, kwargs)
        return self.store.units_set(value.get("id"), value.get("state"), branch=value.get("branch"), pr=value.get("pr"), sha=value.get("sha"))

    def get(self, unit_id: str) -> Unit:
        return self.store.units_get(unit_id)

    def list(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> list[Unit]:
        value = _params(params, kwargs)
        return self.store.units_list(state=value.get("state"), track=value.get("track"))

    def counts(self) -> dict[str, int]:
        return self.store.units_counts()


class _LedgerApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def record(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> LedgerEntry:
        value = _params(params, kwargs)
        return self.store.ledger_record(value.get("pr"), value.get("sha"), value.get("verdict"), value.get("evidence"), value.get("verifier", ""))

    def check(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> LedgerEntry:
        value = _params(params, kwargs)
        return self.store.ledger_check(value.get("pr"), value.get("sha"))

    def summary(self) -> dict[str, int]:
        return self.store.ledger_summary()


class _InboxApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def push(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> tuple[InboxPointer, str]:
        value = _params(params, kwargs)
        return self.store.inbox_push(value.get("agent"), value.get("unit"), value.get("status"), value.get("report", ""))

    def drain(self) -> list[InboxPointer]:
        return self.store.inbox_drain()

    def peek(self) -> list[InboxPointer]:
        return self.store.inbox_peek()

    def count(self) -> int:
        return self.store.inbox_count()


class _GatesApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def park(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> OpenGate:
        value = _params(params, kwargs)
        return self.store.gate_park(value.get("id"), value.get("question"), value.get("options"), value.get("defaultAnswer", value.get("default_answer")))

    def list(self) -> list[OpenGate]:
        return self.store.gates_list()

    def resolve(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> ResolvedGate:
        value = _params(params, kwargs)
        return self.store.gate_resolve(value.get("id"), value.get("answer"))


class _FrontierApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def set(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> Frontier:
        value = _params(params, kwargs)
        return self.store.frontier_set(value.get("repo"), prs=value.get("prs"))

    def show(self) -> Frontier:
        return self.store.frontier_show()


class _StandingApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def show(self) -> list[StandingLine]:
        return self.store.standing_show()

    def add(self, params: Mapping[str, Any] | None = None, **kwargs: Any) -> StandingLine:
        value = _params(params, kwargs)
        return self.store.standing_add(value.get("line"))


class _StatusApi:
    def __init__(self, store: OrchestratorStore) -> None:
        self.store = store

    def render(self) -> StatusReport:
        return self.store.status_render()


def open_store(root: str | Path, **kwargs: Any) -> OrchestratorStore:
    """Compatibility constructor for callers that use the upstream name."""

    return OrchestratorStore(root, **kwargs)


Store = OrchestratorStore


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "(none)"
    clean = lambda value: str(value).replace("|", "\\|")
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(clean(value) for value in row) + " |" for row in rows],
        ]
    )


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--force", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("status")
    unit = subparsers.add_parser("unit-add")
    unit.add_argument("id")
    unit.add_argument("track")
    unit.add_argument("--brief", default="")
    ledger = subparsers.add_parser("ledger-record")
    ledger.add_argument("pr", type=int)
    ledger.add_argument("sha")
    ledger.add_argument("verdict", choices=sorted(VERDICTS))
    ledger.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    store = OrchestratorStore(args.store, force=args.force)
    try:
        if args.command == "init":
            value = store.init()
        elif args.command == "unit-add":
            store.init()
            value = store.units_add(args.id, args.track, args.brief)
        elif args.command == "ledger-record":
            store.init()
            value = store.ledger_record(args.pr, args.sha, args.verdict, args.evidence)
        else:
            value = store.status_render()
        print(json.dumps(value, default=_json_default, sort_keys=True) if args.json else value)
        return 0
    except StoreError as exc:
        parser.error(str(exc))
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
