"""Pure offline pull-request watcher policy.

The policy accepts a small reader protocol instead of invoking a provider.  A
provider adapter may translate a recorded response (or a live provider in a
separate application) into these validated types, but policy code never knows
how the data was obtained.  Unknown provider enums and malformed shapes raise
``UnknownProviderData`` rather than being treated as healthy or pending.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence


class UnknownProviderData(ValueError):
    """A provider response was not part of the explicitly supported schema."""


def _unsafe_symlink(path: Path) -> Path | None:
    """Return a selected-path symlink, ignoring the host temp-dir alias."""

    ambient_temp = Path(tempfile.gettempdir()).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink() and not ambient_temp.is_relative_to(candidate.absolute()):
            return candidate
    return None


class QueryFailure(RuntimeError):
    """A bounded, retryable provider query failure."""

    def __init__(self, detail: str, *, retryable: bool = True, kind: str = "query") -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.kind = kind

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "retryable": self.retryable, "detail": self.detail}


MERGEABLE = {"MERGEABLE", "CONFLICTING"}
MERGE_STATES = {
    "BEHIND",
    "BLOCKED",
    "CLEAN",
    "CONFLICTING",
    "DIRTY",
    "DRAFT",
    "HAS_HOOKS",
    "UNSTABLE",
}
REVIEW_DECISIONS = {None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"}
PR_STATES = {"OPEN", "CLOSED", "MERGED"}
CHECK_KINDS = {"passed", "skipped", "failed", "pending", "code-review-gate"}
ROLLUP_STATES = {"ERROR", "EXPECTED", "FAILURE", "PENDING", "SUCCESS"}
CHECK_SOURCES = {"gh-pr-checks", "graphql-rollup", "recorded"}
CHECK_REPORTED_STATES = {
    "passed": {"", "SUCCESS"},
    "failed": {"", "FAILURE", "ERROR"},
    "pending": {"PENDING"},
    "skipped": {"", "SKIPPED", "SUCCESS"},
    "code-review-gate": {"PENDING", "FAILURE", "ERROR", "SUCCESS"},
}
SNAPSHOT_KINDS = {"open", "merged", "closed"}
CI_KINDS = {"ci-clean", "ci-pending", "ci-failing", "ci-github-rejected"}


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnknownProviderData(f"{label} must be a non-empty string")
    if any(character in value for character in "\x00\t\r\n"):
        raise UnknownProviderData(f"{label} contains a control character")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UnknownProviderData(f"{label} must be an object")
    return value


def _exact_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise UnknownProviderData(f"{label} must be a boolean")
    return value


def _validate_utc_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise UnknownProviderData(f"{label} must be an ISO timestamp")
    try:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError) as exc:
        raise UnknownProviderData(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or offset != timedelta(0):
        raise UnknownProviderData(f"{label} must be UTC-aware")


def _revalidate(value: Any, label: str) -> None:
    """Re-run a frozen record's constructor validation at a reader seam."""

    try:
        value.__post_init__()
    except UnknownProviderData:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise UnknownProviderData(f"{label} is invalid") from exc


def _enum(value: Any, values: set[Any], label: str) -> Any:
    try:
        supported = value in values
    except TypeError:
        supported = False
    if not supported:
        raise UnknownProviderData(f"{label} has unsupported value {value!r}")
    return value


def _validate_check_state(kind: str, reported_state: str, label: str = "check") -> None:
    if reported_state not in CHECK_REPORTED_STATES[kind]:
        raise UnknownProviderData(
            f"{label} reported_state contradicts kind {kind!r}"
        )


@dataclass(frozen=True, order=True)
class PRContext:
    owner: str
    repo: str
    number: int

    def __post_init__(self) -> None:
        _nonempty(self.owner, "owner")
        _nonempty(self.repo, "repo")
        if type(self.number) is not int or self.number <= 0:
            raise ValueError("PR number must be a positive integer")

    def as_dict(self) -> dict[str, Any]:
        return {"owner": self.owner, "repo": self.repo, "number": self.number}


@dataclass(frozen=True)
class Check:
    kind: str
    name: str
    reported_state: str = ""
    description: str = ""
    link: str = ""
    workflow: str = ""

    def __post_init__(self) -> None:
        _enum(self.kind, CHECK_KINDS, "check kind")
        _nonempty(self.name, "check name")
        for label, value in (
            ("check reported state", self.reported_state),
            ("check description", self.description),
            ("check link", self.link),
            ("check workflow", self.workflow),
        ):
            if not isinstance(value, str):
                raise UnknownProviderData(f"{label} must be a string")
        _validate_check_state(self.kind, self.reported_state)


@dataclass(frozen=True)
class CheckRead:
    source: str
    checks: tuple[Check, ...]

    def __post_init__(self) -> None:
        _enum(self.source, CHECK_SOURCES, "check source")
        if not isinstance(self.checks, tuple):
            raise UnknownProviderData("checks must be a tuple")
        if any(not isinstance(check, Check) for check in self.checks):
            raise UnknownProviderData("check read contains an invalid check")


@dataclass(frozen=True)
class PRFacts:
    context: PRContext
    mergeable: str = "MERGEABLE"
    merge_state_status: str = "CLEAN"
    review_decision: str | None = "APPROVED"
    head_ref_oid: str | None = "head"
    head_ref_name: str = "feature"
    base_ref_name: str = "main"
    state: str = "OPEN"
    merged_at: str | None = None
    is_draft: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context, PRContext):
            raise UnknownProviderData("pull request context is invalid")
        _revalidate(self.context, "pull request context")
        _enum(self.mergeable, MERGEABLE, "mergeable")
        _enum(self.merge_state_status, MERGE_STATES, "merge state status")
        _enum(self.review_decision, REVIEW_DECISIONS, "review decision")
        _enum(self.state, PR_STATES, "pull request state")
        _exact_bool(self.is_draft, "draft state")
        if self.head_ref_oid is None and self.state == "OPEN":
            raise UnknownProviderData("open pull requests require a head ref oid")
        if self.head_ref_oid is not None:
            _nonempty(self.head_ref_oid, "head ref oid")
        _nonempty(self.head_ref_name, "head ref name")
        _nonempty(self.base_ref_name, "base ref name")
        if self.merged_at is not None:
            _nonempty(self.merged_at, "merged timestamp")
            _validate_utc_timestamp(self.merged_at, "merged timestamp")
            if self.state != "MERGED":
                raise UnknownProviderData("only merged pull requests can have merged_at")
        elif self.state == "MERGED":
            raise UnknownProviderData("merged pull requests require merged_at")


@dataclass(frozen=True)
class ReviewThread:
    id: str
    body: str = ""
    author: str | None = None
    path: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        _nonempty(self.id, "review thread id")
        if not isinstance(self.body, str):
            raise UnknownProviderData("review thread body must be a string")
        if self.author is not None and not isinstance(self.author, str):
            raise UnknownProviderData("review thread author must be a string or null")
        if self.path is not None and not isinstance(self.path, str):
            raise UnknownProviderData("review thread path must be a string or null")
        if self.line is not None and (type(self.line) is not int or self.line <= 0):
            raise UnknownProviderData("review thread line must be a positive integer or null")


@dataclass(frozen=True)
class CommitRollup:
    oid: str
    state: str

    def __post_init__(self) -> None:
        _nonempty(self.oid, "commit oid")
        _enum(self.state, ROLLUP_STATES, "commit rollup state")


@dataclass(frozen=True)
class MergeAssessment:
    kind: Literal["allowed", "refused"]
    basis: Literal["merge-state", "rollup"] | None
    merge_state_status: str
    head_rollup_state: str | None


@dataclass(frozen=True)
class CIState:
    kind: Literal["ci-clean", "ci-pending", "ci-failing", "ci-github-rejected"]
    source: str
    all: tuple[Check, ...]
    failed: tuple[Check, ...]
    pending: tuple[Check, ...]
    had_previous_passing_ci: bool
    github: MergeAssessment | None


@dataclass(frozen=True)
class PRSnapshot:
    kind: Literal["open", "merged", "closed"]
    context: PRContext
    facts: PRFacts
    threads: tuple[ReviewThread, ...] = ()
    ci: CIState | None = None


@dataclass(frozen=True)
class Blocker:
    kind: Literal["merge-conflicts", "review-threads", "failing-checks", "merge-gate"]
    pr: PRContext
    reason: str | None = None
    threads: tuple[ReviewThread, ...] = ()
    failed: tuple[Check, ...] = ()
    ci: CIState | None = None


@dataclass(frozen=True)
class Decision:
    kind: Literal["blocker", "waiting", "ready", "merged", "clear"]
    snapshot: PRSnapshot | None = None
    blocker: Blocker | None = None
    frontier: PRContext | None = None
    pending: tuple[Check, ...] = ()
    snapshots: tuple[PRSnapshot, ...] = ()


class WatcherReader(Protocol):
    """Validated reader boundary consumed by policy."""

    def pull_request(self, context: PRContext) -> PRFacts: ...

    def review_threads(self, context: PRContext) -> Sequence[ReviewThread]: ...

    def checks(self, context: PRContext) -> CheckRead: ...

    def commit_rollups(self, context: PRContext) -> Sequence[CommitRollup]: ...


class ProviderAdapter(WatcherReader, Protocol):
    """Provider implementations belong behind this adapter contract."""


class WatchClock(Protocol):
    def now(self) -> float: ...

    def observed_at(self) -> str: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def __init__(self) -> None:
        self._started = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self._started

    def observed_at(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class DeterministicClock:
    """Clock/sleep fixture; sleeping advances time without blocking."""

    def __init__(self, start: datetime | None = None) -> None:
        self.start = (start or datetime(2026, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc)
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.elapsed

    def observed_at(self) -> str:
        value = self.start + timedelta(seconds=self.elapsed)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep duration must not be negative")
        self.sleeps.append(seconds)
        self.elapsed += seconds


@dataclass(frozen=True)
class PollOptions:
    interval: float = 60.0
    sweep_interval: float = 300.0
    timeout: float = 0.0
    max_query_errors: int = 5
    allow_draft: bool = False

    def __post_init__(self) -> None:
        for value, label, minimum, message in (
            (self.interval, "interval", 0, "poll intervals must be positive"),
            (self.sweep_interval, "sweep_interval", 0, "poll intervals must be positive"),
            (self.timeout, "timeout", -1, "timeout must be zero or positive"),
        ):
            if type(value) not in {int, float}:
                raise ValueError(f"{label} must be a finite number")
            try:
                finite = math.isfinite(value)
            except (TypeError, OverflowError) as exc:
                raise ValueError(f"{label} must be finite") from exc
            if not finite:
                raise ValueError(f"{label} must be finite")
            if (minimum == 0 and value <= 0) or (minimum == -1 and value < 0):
                raise ValueError(message)
        if type(self.max_query_errors) is not int or self.max_query_errors <= 0:
            raise ValueError("max_query_errors must be positive")
        if type(self.allow_draft) is not bool:
            raise ValueError("allow_draft must be a boolean")


def passing_check(name: str = "ci") -> Check:
    return Check("passed", name, "SUCCESS")


def pending_check(name: str = "ci") -> Check:
    return Check("pending", name, "PENDING")


def failed_check(name: str = "ci") -> Check:
    return Check("failed", name, "FAILURE")


def _choose(value: Any, number: int, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(number, default)
    return default if value is None else value


class FakeReader:
    """Offline reader with call history and optional per-PR fixtures."""

    def __init__(
        self,
        *,
        facts: PRFacts | Mapping[int, PRFacts] | None = None,
        checks: CheckRead | Mapping[int, CheckRead] | None = None,
        threads: Sequence[ReviewThread] | Mapping[int, Sequence[ReviewThread]] = (),
        commits: Sequence[CommitRollup] | Mapping[int, Sequence[CommitRollup]] = (),
    ) -> None:
        self.calls: list[str] = []
        self._facts = facts
        self._checks = checks
        self._threads = threads
        self._commits = commits

    @staticmethod
    def _default_facts(context: PRContext) -> PRFacts:
        return PRFacts(context=context)

    def pull_request(self, context: PRContext) -> PRFacts:
        self.calls.append(f"pull_request:{context.number}")
        value = _choose(self._facts, context.number, self._default_facts(context))
        if not isinstance(value, PRFacts):
            raise UnknownProviderData("fake facts fixture is not PRFacts")
        return replace(value, context=context)

    def review_threads(self, context: PRContext) -> Sequence[ReviewThread]:
        self.calls.append(f"review_threads:{context.number}")
        value = _choose(self._threads, context.number, ())
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise UnknownProviderData("fake review fixture is not a sequence")
        return tuple(value)

    def checks(self, context: PRContext) -> CheckRead:
        self.calls.append(f"checks:{context.number}")
        value = _choose(
            self._checks,
            context.number,
            CheckRead("recorded", (passing_check(),)),
        )
        if not isinstance(value, CheckRead):
            raise UnknownProviderData("fake check fixture is not CheckRead")
        return value

    def commit_rollups(self, context: PRContext) -> Sequence[CommitRollup]:
        self.calls.append(f"commit_rollups:{context.number}")
        value = _choose(
            self._commits,
            context.number,
            (CommitRollup("head", "SUCCESS"),),
        )
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise UnknownProviderData("fake commit fixture is not a sequence")
        return tuple(value)


class JsonProviderAdapter:
    """Validate a recorded provider response without making provider calls."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.payload = _mapping(payload, "provider payload")
        self.calls: list[str] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "JsonProviderAdapter":
        fixture_path = Path(path).expanduser()
        if _unsafe_symlink(fixture_path) is not None:
            raise UnknownProviderData(
                f"provider fixture path must not contain symlinks: {fixture_path}"
            )
        if not fixture_path.is_file():
            raise UnknownProviderData(
                f"provider fixture must be an explicit regular file: {fixture_path}"
            )
        try:
            value = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UnknownProviderData(f"provider fixture could not be read: {exc}") from exc
        return cls(_mapping(value, "provider payload"))

    def _section(self, name: str) -> Mapping[str, Any]:
        return _mapping(self.payload.get(name), f"provider payload {name}")

    def pull_request(self, context: PRContext) -> PRFacts:
        self.calls.append(f"pull_request:{context.number}")
        raw = self._section("pull_request")
        required = {
            "mergeable",
            "merge_state_status",
            "review_decision",
            "head_ref_oid",
            "head_ref_name",
            "base_ref_name",
            "state",
            "merged_at",
            "is_draft",
        }
        missing = sorted(required - set(raw))
        if missing:
            raise UnknownProviderData(f"provider pull_request is missing {', '.join(missing)}")
        return PRFacts(
            context=context,
            mergeable=raw["mergeable"],
            merge_state_status=raw["merge_state_status"],
            review_decision=raw["review_decision"],
            head_ref_oid=raw["head_ref_oid"],
            head_ref_name=raw["head_ref_name"],
            base_ref_name=raw["base_ref_name"],
            state=raw["state"],
            merged_at=raw["merged_at"],
            is_draft=raw["is_draft"],
        )

    def review_threads(self, context: PRContext) -> Sequence[ReviewThread]:
        self.calls.append(f"review_threads:{context.number}")
        if "review_threads" not in self.payload:
            raise UnknownProviderData("provider payload is missing review_threads")
        raw = self.payload["review_threads"]
        if not isinstance(raw, list):
            raise UnknownProviderData("provider review_threads must be an array")
        result: list[ReviewThread] = []
        for index, item in enumerate(raw):
            value = _mapping(item, f"provider review_threads[{index}]")
            result.append(
                ReviewThread(
                    id=value.get("id"),
                    body=value.get("body", ""),
                    author=value.get("author"),
                    path=value.get("path"),
                    line=value.get("line"),
                )
            )
        return tuple(result)

    def checks(self, context: PRContext) -> CheckRead:
        self.calls.append(f"checks:{context.number}")
        raw = self._section("checks")
        if "fast_path" in raw:
            fast = raw["fast_path"]
            if isinstance(fast, list):
                direct = self._parse_checks(fast, "provider checks.fast_path")
            else:
                fast_object = _mapping(fast, "provider checks.fast_path")
                kind = fast_object.get("kind")
                if kind == "checks":
                    direct = self._parse_checks(fast_object.get("checks"), "provider checks.fast_path.checks")
                elif kind == "unusable":
                    direct = ()
                else:
                    raise UnknownProviderData(f"provider checks.fast_path has unsupported kind {kind!r}")
            if direct:
                return CheckRead("gh-pr-checks", direct)
            pages = raw.get("rollup_pages", [])
            if not isinstance(pages, list):
                raise UnknownProviderData("provider checks.rollup_pages must be an array")
            fallback: list[Check] = []
            for index, page in enumerate(pages):
                page_object = _mapping(page, f"provider checks.rollup_pages[{index}]")
                fallback.extend(self._parse_checks(page_object.get("checks"), f"provider checks.rollup_pages[{index}].checks"))
            if fallback:
                return CheckRead("graphql-rollup", tuple(fallback))
            raise QueryFailure("provider fast path and rollup were empty", kind="checks-unavailable")
        source = raw.get("source")
        return CheckRead(source=source, checks=self._parse_checks(raw.get("checks"), "provider checks.checks"))

    @staticmethod
    def _parse_checks(values: Any, label: str) -> tuple[Check, ...]:
        if not isinstance(values, list):
            raise UnknownProviderData(f"{label} must be an array")
        checks: list[Check] = []
        for index, item in enumerate(values):
            value = _mapping(item, f"{label}[{index}]")
            kind = value.get("kind")
            reported_state = value.get("reported_state", "")
            checks.append(
                Check(
                    kind=kind,
                    name=value.get("name"),
                    reported_state=reported_state,
                    description=value.get("description", ""),
                    link=value.get("link", ""),
                    workflow=value.get("workflow", ""),
                )
            )
        return tuple(checks)

    def commit_rollups(self, context: PRContext) -> Sequence[CommitRollup]:
        self.calls.append(f"commit_rollups:{context.number}")
        if "commit_rollups" not in self.payload:
            raise UnknownProviderData("provider payload is missing commit_rollups")
        raw = self.payload["commit_rollups"]
        if not isinstance(raw, list):
            raise UnknownProviderData("provider commit_rollups must be an array")
        result: list[CommitRollup] = []
        for index, item in enumerate(raw):
            value = _mapping(item, f"provider commit_rollups[{index}]")
            result.append(CommitRollup(oid=value.get("oid"), state=value.get("state")))
        return tuple(result)


def _merge_assessment(facts: PRFacts, commits: Sequence[CommitRollup]) -> tuple[bool, MergeAssessment]:
    _revalidate(facts, "pull request facts")
    _enum(facts.mergeable, MERGEABLE, "mergeable")
    _enum(facts.merge_state_status, MERGE_STATES, "merge state status")
    if not isinstance(commits, Sequence) or isinstance(commits, (str, bytes)):
        raise UnknownProviderData("commit rollups must be an array")
    for index, commit in enumerate(commits):
        if not isinstance(commit, CommitRollup):
            raise UnknownProviderData("commit rollups contain an invalid row")
        _revalidate(commit, f"commit rollups[{index}]")
    if len({commit.oid for commit in commits}) != len(commits):
        raise UnknownProviderData("commit rollups contain duplicate oids")
    matching = [commit for commit in commits if commit.oid == facts.head_ref_oid]
    if len(matching) != 1:
        raise UnknownProviderData("commit rollups must contain one current head row")
    head_state = matching[0].state
    if head_state not in {"SUCCESS", "FAILURE", "ERROR", "PENDING"}:
        raise UnknownProviderData("current head rollup has an unknown state")
    previous_success = any(
        commit.oid != facts.head_ref_oid and commit.state == "SUCCESS" for commit in commits
    )
    if head_state != "SUCCESS":
        assessment = MergeAssessment("refused", "rollup", facts.merge_state_status, head_state)
    elif facts.merge_state_status == "BLOCKED":
        assessment = MergeAssessment("allowed", "rollup", facts.merge_state_status, head_state)
    else:
        assessment = MergeAssessment("allowed", "merge-state", facts.merge_state_status, head_state)
    return previous_success, assessment


def _ci_check_groups(checks: Sequence[Check]) -> tuple[tuple[Check, ...], tuple[Check, ...]]:
    failed = tuple(
        check
        for check in checks
        if check.kind == "failed"
        or (check.kind == "code-review-gate" and check.reported_state in {"FAILURE", "ERROR"})
    )
    pending = tuple(
        check
        for check in checks
        if check.kind == "pending"
        or (check.kind == "code-review-gate" and check.reported_state == "PENDING")
    )
    return failed, pending


def _validate_merge_assessment(value: Any, label: str) -> None:
    if not isinstance(value, MergeAssessment):
        raise UnknownProviderData(f"{label} is invalid")
    _enum(value.kind, {"allowed", "refused"}, f"{label} kind")
    _enum(value.basis, {"merge-state", "rollup"}, f"{label} basis")
    _enum(value.merge_state_status, MERGE_STATES, f"{label} merge state status")
    if value.head_rollup_state is not None:
        _enum(value.head_rollup_state, ROLLUP_STATES, f"{label} head rollup state")


def _validate_ci_state(value: Any, label: str = "CI state") -> None:
    if not isinstance(value, CIState):
        raise UnknownProviderData(f"{label} is invalid")
    _enum(value.kind, CI_KINDS, f"{label} kind")
    _enum(value.source, CHECK_SOURCES, f"{label} source")
    check_groups: dict[str, tuple[Check, ...]] = {}
    for name, checks in (("all", value.all), ("failed", value.failed), ("pending", value.pending)):
        if not isinstance(checks, tuple):
            raise UnknownProviderData(f"{label} {name} checks must be a tuple")
        for index, check in enumerate(checks):
            if not isinstance(check, Check):
                raise UnknownProviderData(f"{label} {name} check {index} is invalid")
            _revalidate(check, f"{label} {name} check {index}")
        check_groups[name] = checks
    _exact_bool(value.had_previous_passing_ci, f"{label} previous passing CI flag")
    if value.github is not None:
        _validate_merge_assessment(value.github, f"{label} GitHub assessment")

    expected_failed, expected_pending = _ci_check_groups(check_groups["all"])
    if check_groups["failed"] != expected_failed or check_groups["pending"] != expected_pending:
        raise UnknownProviderData(f"{label} failed or pending checks are inconsistent")
    if value.kind == "ci-clean" and (value.failed or value.pending):
        raise UnknownProviderData("clean CI contains failed or pending checks")
    if value.kind == "ci-pending" and (value.failed or not value.pending):
        raise UnknownProviderData("pending CI has inconsistent check state")
    if value.kind == "ci-failing" and not value.failed:
        raise UnknownProviderData("failing CI has no failed checks")
    if value.kind == "ci-github-rejected":
        if value.failed or value.github is None or value.github.kind != "refused":
            raise UnknownProviderData("GitHub-rejected CI has inconsistent assessment")
    elif value.github is not None and value.github.kind == "refused":
        raise UnknownProviderData("CI has a refused GitHub assessment but is not rejected")


def _validate_snapshot(value: Any, label: str = "snapshot") -> None:
    if not isinstance(value, PRSnapshot):
        raise UnknownProviderData(f"{label} is invalid")
    _enum(value.kind, SNAPSHOT_KINDS, f"{label} kind")
    if not isinstance(value.context, PRContext):
        raise UnknownProviderData(f"{label} context is invalid")
    _revalidate(value.context, f"{label} context")
    if not isinstance(value.facts, PRFacts):
        raise UnknownProviderData(f"{label} facts are invalid")
    _revalidate(value.facts, f"{label} facts")
    if value.facts.context != value.context:
        raise UnknownProviderData(f"{label} facts belong to another identity")
    if not isinstance(value.threads, tuple):
        raise UnknownProviderData(f"{label} review threads must be a tuple")
    for index, thread in enumerate(value.threads):
        if not isinstance(thread, ReviewThread):
            raise UnknownProviderData(f"{label} review thread {index} is invalid")
        _revalidate(thread, f"{label} review thread {index}")
    if value.ci is not None:
        _validate_ci_state(value.ci, f"{label} CI state")

    if value.kind == "open" and (value.facts.state != "OPEN" or value.facts.merged_at is not None):
        raise UnknownProviderData("open snapshot has merged or non-open facts")
    if value.kind == "closed" and (value.facts.state != "CLOSED" or value.facts.merged_at is not None):
        raise UnknownProviderData("closed snapshot has inconsistent facts")
    if value.kind != "open" and (value.threads or value.ci is not None):
        raise UnknownProviderData("non-open snapshot contains open pull request data")
    if value.kind == "open" and value.ci is None:
        raise UnknownProviderData("open snapshot has no CI state")
    if value.kind == "merged" and (
        value.facts.state != "MERGED" or value.facts.merged_at is None
    ):
        raise UnknownProviderData("merged snapshot has inconsistent facts")


def read_snapshot(
    reader: WatcherReader,
    context: PRContext,
    *,
    pending_history: Literal["include", "omit"] = "include",
    before_read: Callable[[], None] | None = None,
) -> PRSnapshot:
    if not isinstance(context, PRContext):
        raise UnknownProviderData("reader request context is invalid")
    _revalidate(context, "reader request context")

    def read(method: Callable[[PRContext], Any]) -> Any:
        _revalidate(context, "reader request context")
        if before_read is not None:
            before_read()
        try:
            value = method(context)
        except (UnknownProviderData, QueryFailure):
            raise
        except (TypeError, ValueError) as exc:
            raise UnknownProviderData("reader returned an invalid domain value") from exc
        _revalidate(context, "reader request context")
        return value

    facts = read(reader.pull_request)
    if not isinstance(facts, PRFacts):
        raise UnknownProviderData("reader returned invalid pull request facts")
    _revalidate(facts, "reader returned pull request facts")
    if facts.context != context:
        raise UnknownProviderData("reader returned pull request facts for another identity")
    _enum(facts.mergeable, MERGEABLE, "mergeable")
    _enum(facts.merge_state_status, MERGE_STATES, "merge state status")
    if facts.state == "MERGED":
        snapshot = PRSnapshot("merged", context, facts)
        _validate_snapshot(snapshot)
        return snapshot
    if facts.state == "CLOSED":
        snapshot = PRSnapshot("closed", context, facts)
        _validate_snapshot(snapshot)
        return snapshot

    threads = read(reader.review_threads)
    if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
        raise UnknownProviderData("reader returned invalid review threads")
    for index, thread in enumerate(threads):
        if not isinstance(thread, ReviewThread):
            raise UnknownProviderData(f"reader returned an invalid review thread {index}")
        _revalidate(thread, f"reader returned review thread {index}")
    check_read = read(reader.checks)
    if not isinstance(check_read, CheckRead):
        raise UnknownProviderData("reader returned invalid check data")
    _revalidate(check_read, "reader returned check data")
    for index, check in enumerate(check_read.checks):
        if not isinstance(check, Check):
            raise UnknownProviderData(f"reader returned invalid check {index}")
        _revalidate(check, f"reader returned check {index}")
    if not check_read.checks:
        raise QueryFailure("provider returned no checks", kind="checks-unavailable")
    failed, pending = _ci_check_groups(check_read.checks)
    if pending and not failed and pending_history == "omit":
        ci = CIState(
            "ci-pending",
            check_read.source,
            check_read.checks,
            (),
            pending,
            False,
            None,
        )
    else:
        commits = read(reader.commit_rollups)
        previous_success, github = _merge_assessment(facts, commits)
        if failed:
            ci = CIState(
                "ci-failing",
                check_read.source,
                check_read.checks,
                failed,
                pending,
                previous_success,
                github,
            )
        elif github.kind == "refused":
            ci = CIState(
                "ci-github-rejected",
                check_read.source,
                check_read.checks,
                (),
                pending,
                previous_success,
                github,
            )
        elif pending:
            ci = CIState(
                "ci-pending",
                check_read.source,
                check_read.checks,
                (),
                pending,
                previous_success,
                github,
            )
        else:
            ci = CIState(
                "ci-clean",
                check_read.source,
                check_read.checks,
                (),
                (),
                previous_success,
                github,
            )
    _revalidate(facts, "reader returned pull request facts")
    if facts.context != context:
        raise UnknownProviderData("reader returned pull request facts for another identity")
    for index, thread in enumerate(threads):
        _revalidate(thread, f"reader returned review thread {index}")
    _revalidate(check_read, "reader returned check data")
    for index, check in enumerate(check_read.checks):
        _revalidate(check, f"reader returned check {index}")
    if facts.state == "MERGED":
        snapshot = PRSnapshot("merged", context, facts)
        _validate_snapshot(snapshot)
        return snapshot
    if facts.state == "CLOSED":
        snapshot = PRSnapshot("closed", context, facts)
        _validate_snapshot(snapshot)
        return snapshot
    snapshot = PRSnapshot("open", context, facts, tuple(threads), ci)
    _validate_snapshot(snapshot)
    return snapshot


def _conflict(snapshot: PRSnapshot) -> bool:
    return snapshot.kind == "open" and (
        snapshot.facts.mergeable == "CONFLICTING"
        or snapshot.facts.merge_state_status in {"DIRTY", "CONFLICTING"}
    )


def _gate_reason(snapshot: PRSnapshot, allow_draft: bool) -> str | None:
    if snapshot.kind == "merged":
        return None
    if snapshot.kind == "closed":
        return "closed-without-merge"
    if snapshot.facts.is_draft and not allow_draft:
        return "draft-pr"
    if snapshot.facts.review_decision == "CHANGES_REQUESTED":
        return "changes-requested"
    if snapshot.facts.review_decision in {None, "REVIEW_REQUIRED"}:
        return "review-required"
    return None


def _gate_blocker(snapshot: PRSnapshot, allow_draft: bool) -> Blocker | None:
    reason = _gate_reason(snapshot, allow_draft)
    if reason is None:
        return None
    if reason == "draft-pr" and snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind == "ci-pending":
        return None
    return Blocker("merge-gate", snapshot.context, reason=reason)


def classify_pr(snapshot: PRSnapshot, *, allow_draft: bool = False) -> Decision:
    """Classify one snapshot using conflict > review > CI > gate precedence."""

    _validate_snapshot(snapshot)
    if _conflict(snapshot):
        return Decision("blocker", blocker=Blocker("merge-conflicts", snapshot.context))
    if snapshot.kind == "open" and snapshot.threads:
        return Decision(
            "blocker",
            blocker=Blocker("review-threads", snapshot.context, threads=snapshot.threads),
        )
    if snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind in {"ci-failing", "ci-github-rejected"}:
        return Decision(
            "blocker",
            blocker=Blocker(
                "failing-checks",
                snapshot.context,
                failed=snapshot.ci.failed,
                ci=snapshot.ci,
            ),
        )
    gate = _gate_blocker(snapshot, allow_draft)
    if gate is not None:
        return Decision("blocker", blocker=gate)
    if snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind == "ci-pending":
        return Decision("waiting", frontier=snapshot.context, pending=snapshot.ci.pending)
    if snapshot.kind == "merged":
        return Decision("merged", snapshot=snapshot)
    if snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind == "ci-clean":
        return Decision("ready", snapshot=snapshot)
    raise UnknownProviderData("snapshot has no safe classification")


def select_stack_decision(
    snapshots: Sequence[PRSnapshot],
    *,
    allow_draft: bool = False,
) -> Decision:
    """Apply blocker tiers across a bottom-to-top stack before waiting."""

    try:
        snapshots = tuple(snapshots)
    except (TypeError, ValueError) as exc:
        raise UnknownProviderData("stack snapshots are not a valid sequence") from exc
    if not snapshots:
        raise ValueError("stack must contain at least one pull request")
    for index, snapshot in enumerate(snapshots):
        _validate_snapshot(snapshot, f"stack snapshot {index}")
    for snapshot in snapshots:
        if _conflict(snapshot):
            return Decision("blocker", blocker=Blocker("merge-conflicts", snapshot.context))
    for snapshot in snapshots:
        if snapshot.kind == "open" and snapshot.threads:
            return Decision(
                "blocker",
                blocker=Blocker("review-threads", snapshot.context, threads=snapshot.threads),
            )
    for snapshot in snapshots:
        if snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind in {"ci-failing", "ci-github-rejected"}:
            return Decision(
                "blocker",
                blocker=Blocker(
                    "failing-checks",
                    snapshot.context,
                    failed=snapshot.ci.failed,
                    ci=snapshot.ci,
                ),
            )
    for snapshot in snapshots:
        gate = _gate_blocker(snapshot, allow_draft)
        if gate is not None:
            return Decision("blocker", blocker=gate)
    for snapshot in snapshots:
        if snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind == "ci-pending":
            return Decision("waiting", frontier=snapshot.context, pending=snapshot.ci.pending)
    if all(snapshot.kind == "merged" for snapshot in snapshots):
        return Decision("clear", snapshots=tuple(snapshots))
    if all(
        snapshot.kind == "merged"
        or (snapshot.kind == "open" and snapshot.ci is not None and snapshot.ci.kind == "ci-clean")
        for snapshot in snapshots
    ):
        return Decision("clear", snapshots=tuple(snapshots))
    raise UnknownProviderData("stack contains an unclassified snapshot")


def query_backoff_seconds(interval: float, failures: int) -> float:
    if failures <= 0:
        raise ValueError("failures must be positive")
    return min(max(interval, 60.0) * (2 ** (failures - 1)), 300.0)


def _context(value: PRContext | None) -> dict[str, Any] | None:
    return None if value is None else value.as_dict()


def _snapshot(value: PRSnapshot) -> dict[str, Any]:
    return asdict(value)


def _blocker(value: Blocker) -> dict[str, Any]:
    return asdict(value)


class _Emitter:
    def __init__(self, clock: WatchClock, mode: str, emit: Callable[[dict[str, Any]], None] | None) -> None:
        self.clock = clock
        self.mode = mode
        self.emit_callback = emit
        self.sequence = 0

    def stamp(self, payload: dict[str, Any], *, terminal: bool, exit_code: int | None = None) -> dict[str, Any]:
        self.sequence += 1
        result: dict[str, Any] = {
            "schema_version": 1,
            "sequence": self.sequence,
            "observed_at": self.clock.observed_at(),
            "mode": self.mode,
            **payload,
            "terminal": terminal,
        }
        if terminal:
            result["exit_code"] = exit_code
        return result

    def progress(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self.stamp(payload, terminal=False)
        if self.emit_callback is not None:
            self.emit_callback(value)
        return value


def _blocker_code(blocker: Blocker) -> int:
    return {
        "merge-conflicts": 2,
        "review-threads": 3,
        "failing-checks": 4,
        "merge-gate": 6,
    }[blocker.kind]


def _timeout(emitter: _Emitter, reason: dict[str, Any]) -> dict[str, Any]:
    return emitter.stamp({"kind": "TIMEOUT", "reason": reason}, terminal=True, exit_code=5)


class _DeadlineReached(Exception):
    """Internal control flow used to stop before a provider read."""


def _deadline_reached(clock: WatchClock, started: float, options: PollOptions) -> bool:
    return options.timeout > 0 and clock.now() - started >= options.timeout


def _guard_deadline(clock: WatchClock, started: float, options: PollOptions) -> None:
    if _deadline_reached(clock, started, options):
        raise _DeadlineReached


def _status_query(emitter: _Emitter, failure: QueryFailure, failures: int) -> dict[str, Any]:
    return emitter.stamp(
        {
            "kind": "BLOCKER",
            "blocker": {
                "kind": "status-query",
                "failures": failures,
                "failure": failure.as_dict(),
            },
        },
        terminal=True,
        exit_code=7,
    )


def _handle_query_failure(
    emitter: _Emitter,
    clock: WatchClock,
    options: PollOptions,
    started: float,
    failure: QueryFailure,
    failures: int,
) -> tuple[dict[str, Any] | None, int]:
    if _deadline_reached(clock, started, options):
        return _timeout(emitter, {"kind": "status-unavailable", "failure": failure.as_dict()}), failures
    if not failure.retryable or failures >= options.max_query_errors:
        return _status_query(emitter, failure, failures), failures
    retry = query_backoff_seconds(options.interval, failures)
    emitter.progress(
        {
            "kind": "RETRY",
            "failure": failure.as_dict(),
            "consecutive_failures": failures,
            "retry_in_seconds": retry,
        }
    )
    if options.timeout > 0 and clock.now() - started >= options.timeout:
        return _timeout(emitter, {"kind": "status-unavailable", "failure": failure.as_dict()}), failures
    remaining = retry
    if options.timeout > 0:
        remaining = min(remaining, max(0.0, options.timeout - (clock.now() - started)))
    clock.sleep(remaining)
    if options.timeout > 0 and clock.now() - started >= options.timeout:
        return _timeout(emitter, {"kind": "status-unavailable", "failure": failure.as_dict()}), failures
    return None, failures


def run_simple(
    reader: WatcherReader,
    contexts: Sequence[PRContext],
    *,
    mode: Literal["single", "stack"] = "single",
    status_only: bool = False,
    options: PollOptions | None = None,
    clock: WatchClock | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Poll one PR or a stack without invoking a provider or mutating it."""

    try:
        contexts = tuple(contexts)
    except (TypeError, ValueError) as exc:
        raise UnknownProviderData("watch contexts are not a valid sequence") from exc
    if not contexts:
        raise ValueError("watch context cannot be empty")
    if mode not in {"single", "stack"}:
        raise ValueError("run_simple mode must be single or stack")
    expected_context_keys: list[tuple[str, str, int]] = []
    for index, context in enumerate(contexts):
        if not isinstance(context, PRContext):
            raise UnknownProviderData(f"watch context {index} is not a pull request identity")
        _revalidate(context, f"watch context {index} identity")
        expected_context_keys.append((context.owner, context.repo, context.number))
    options = options or PollOptions()
    clock = clock or SystemClock()
    emitter = _Emitter(clock, mode, emit)
    started = clock.now()
    failures = 0
    while True:
        if _deadline_reached(clock, started, options):
            return _timeout(emitter, {"kind": "deadline"})
        try:
            rows = tuple(
                read_snapshot(
                    reader,
                    context,
                    before_read=lambda: _guard_deadline(clock, started, options),
                )
                for context in contexts
            )
            failures = 0
        except _DeadlineReached:
            return _timeout(emitter, {"kind": "deadline"})
        except QueryFailure as failure:
            failures += 1
            terminal, _ = _handle_query_failure(emitter, clock, options, started, failure, failures)
            if terminal is not None:
                return terminal
            continue
        if _deadline_reached(clock, started, options):
            return _timeout(emitter, {"kind": "deadline"})
        for index, (context, row) in enumerate(zip(contexts, rows)):
            _revalidate(context, f"watch context {index} identity")
            row_key = (row.context.owner, row.context.repo, row.context.number)
            if row_key != expected_context_keys[index]:
                raise UnknownProviderData("reader returned another pull request identity")
        if status_only:
            return emitter.stamp(
                {"kind": "STATUS", "reason": "status-only", "rows": [_snapshot(row) for row in rows]},
                terminal=True,
                exit_code=0,
            )
        if mode == "stack":
            emitter.progress({"kind": "STATUS", "reason": "poll", "rows": [_snapshot(row) for row in rows]})
            decision = select_stack_decision(rows, allow_draft=options.allow_draft)
        else:
            decision = classify_pr(rows[0], allow_draft=options.allow_draft)
        if decision.kind == "blocker" and decision.blocker is not None:
            return emitter.stamp(
                {"kind": "BLOCKER", "blocker": _blocker(decision.blocker)},
                terminal=True,
                exit_code=_blocker_code(decision.blocker),
            )
        if decision.kind in {"ready", "merged"} and decision.snapshot is not None:
            return emitter.stamp(
                {
                    "kind": "READY",
                    "scope": {"kind": "single", "pr": _snapshot(decision.snapshot)},
                },
                terminal=True,
                exit_code=0,
            )
        if decision.kind == "clear":
            return emitter.stamp(
                {"kind": "READY", "scope": {"kind": "stack", "prs": [_snapshot(row) for row in decision.snapshots]}},
                terminal=True,
                exit_code=0,
            )
        if decision.kind != "waiting" or decision.frontier is None:
            raise UnknownProviderData("watch decision could not be rendered")
        emitter.progress(
            {
                "kind": "WAITING",
                "frontier": _context(decision.frontier),
                "reason": {"kind": "pending-checks", "pending": [asdict(check) for check in decision.pending]},
            }
        )
        reason = {"kind": "pending-checks", "pending": [asdict(check) for check in decision.pending]}
        if options.timeout > 0 and clock.now() - started >= options.timeout:
            return _timeout(emitter, reason)
        remaining = options.interval
        if options.timeout > 0:
            remaining = min(remaining, max(0.0, options.timeout - (clock.now() - started)))
        clock.sleep(remaining)
        if _deadline_reached(clock, started, options):
            return _timeout(emitter, reason)


def _queue_context_key(context: PRContext, index: int) -> tuple[str, str, int]:
    if not isinstance(context, PRContext):
        raise ValueError(f"queue item {index} is not a pull request identity")
    _revalidate(context, f"queue item {index} identity")
    return (context.owner, context.repo, context.number)


def run_queued(
    reader: WatcherReader,
    contexts: Sequence[PRContext],
    *,
    options: PollOptions | None = None,
    clock: WatchClock | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Watch a frozen bottom-to-top queue; no merge is attempted."""

    try:
        contexts = tuple(contexts)
    except (TypeError, ValueError) as exc:
        raise UnknownProviderData("queue is not a valid sequence") from exc
    if not contexts:
        raise ValueError("queue cannot be empty")
    queue_keys = []
    for index, context in enumerate(contexts):
        queue_keys.append(_queue_context_key(context, index))
    if len(set(queue_keys)) != len(queue_keys):
        raise ValueError("queue contains duplicate pull request identities")
    options = options or PollOptions()
    clock = clock or SystemClock()
    emitter = _Emitter(clock, "queued-stack", emit)
    started = clock.now()
    snapshots: dict[PRContext, PRSnapshot] = {}
    sweep_index = 0
    next_sweep_at = started
    last_frontier: PRContext | None = None
    last_wait_key: tuple[Any, ...] | None = None
    failures = 0
    emitter.progress({"kind": "QUEUE", "queue": [_context(context) for context in contexts]})

    while True:
        if _deadline_reached(clock, started, options):
            return _timeout(emitter, {"kind": "queued-stack", "reason": "deadline"})
        active_contexts = tuple(
            context
            for context in contexts
            if snapshots.get(context) is None or snapshots[context].kind != "merged"
        )
        if not active_contexts:
            merged = [snapshots[context] for context in contexts if snapshots[context].kind == "merged"]
            return emitter.stamp(
                {"kind": "COMPLETE", "queue": [_context(context) for context in contexts], "merged": [_snapshot(row) for row in merged]},
                terminal=True,
                exit_code=0,
            )

        if not snapshots or clock.now() >= next_sweep_at:
            while sweep_index < len(contexts):
                context = contexts[sweep_index]
                if snapshots.get(context) is not None and snapshots[context].kind == "merged":
                    sweep_index += 1
                    continue
                try:
                    snapshot = read_snapshot(
                        reader,
                        context,
                        pending_history="omit",
                        before_read=lambda: _guard_deadline(clock, started, options),
                    )
                    _guard_deadline(clock, started, options)
                    failures = 0
                except _DeadlineReached:
                    return _timeout(emitter, {"kind": "queued-stack", "reason": "deadline"})
                except QueryFailure as failure:
                    failures += 1
                    terminal, _ = _handle_query_failure(emitter, clock, options, started, failure, failures)
                    if terminal is not None:
                        return terminal
                    continue
                if _queue_context_key(context, sweep_index) != queue_keys[sweep_index]:
                    raise UnknownProviderData("queue context identity changed during read")
                _validate_snapshot(snapshot, "queue reader snapshot")
                if _queue_context_key(snapshot.context, sweep_index) != queue_keys[sweep_index]:
                    raise UnknownProviderData("queue reader returned another pull request identity")
                snapshots[context] = snapshot
                sweep_index += 1
            sweep_index = 0
            next_sweep_at = clock.now() + options.sweep_interval
            emitter.progress(
                {
                    "kind": "STATUS",
                    "reason": "whole-stack-sweep",
                    "rows": [_snapshot(snapshots[context]) for context in contexts],
                }
            )
        else:
            frontier_context = next(context for context in active_contexts)
            try:
                snapshot = read_snapshot(
                    reader,
                    frontier_context,
                    pending_history="omit",
                    before_read=lambda: _guard_deadline(clock, started, options),
                )
                _guard_deadline(clock, started, options)
                failures = 0
            except _DeadlineReached:
                return _timeout(emitter, {"kind": "queued-stack", "reason": "deadline"})
            except QueryFailure as failure:
                failures += 1
                terminal, _ = _handle_query_failure(emitter, clock, options, started, failure, failures)
                if terminal is not None:
                    return terminal
                continue
            frontier_index = next(
                (index for index, item in enumerate(contexts) if item is frontier_context),
                0,
            )
            if _queue_context_key(frontier_context, frontier_index) != queue_keys[frontier_index]:
                raise UnknownProviderData("queue context identity changed during read")
            _validate_snapshot(snapshot, "queue reader snapshot")
            if _queue_context_key(snapshot.context, frontier_index) != queue_keys[frontier_index]:
                raise UnknownProviderData("queue reader returned another pull request identity")
            snapshots[frontier_context] = snapshot

        active_rows = tuple(snapshots[context] for context in contexts if snapshots[context].kind != "merged")
        if not active_rows:
            continue
        decision = select_stack_decision(active_rows, allow_draft=options.allow_draft)
        if decision.kind == "blocker" and decision.blocker is not None:
            return emitter.stamp(
                {"kind": "BLOCKER", "blocker": _blocker(decision.blocker)},
                terminal=True,
                exit_code=_blocker_code(decision.blocker),
            )
        frontier = active_rows[0].context
        if last_frontier is not None and frontier != last_frontier:
            emitter.progress(
                {
                    "kind": "ADVANCE",
                    "merged": _context(last_frontier),
                    "frontier": _context(frontier),
                    "remaining": len(active_rows),
                }
            )
            last_frontier = frontier
            last_wait_key = None
            continue
        last_frontier = frontier
        if options.timeout > 0 and clock.now() - started >= options.timeout:
            return _timeout(
                emitter,
                {"kind": "queued-stack", "frontier": _context(frontier), "unmerged_count": len(active_rows)},
            )
        if decision.kind == "waiting":
            reason: dict[str, Any] = {
                "kind": "pending-checks",
                "pending": [asdict(check) for check in decision.pending],
            }
            wait_key = ("pending", frontier.owner, frontier.repo, frontier.number, len(decision.pending))
        else:
            reason = {"kind": "merge-queue", "unmerged_count": len(active_rows)}
            wait_key = ("queue", frontier.owner, frontier.repo, frontier.number, len(active_rows))
        if wait_key != last_wait_key:
            emitter.progress({"kind": "WAITING", "frontier": _context(frontier), "reason": reason})
            last_wait_key = wait_key
        remaining = options.interval
        if options.timeout > 0:
            remaining = min(remaining, max(0.0, options.timeout - (clock.now() - started)))
        clock.sleep(remaining)
        if _deadline_reached(clock, started, options):
            return _timeout(
                emitter,
                {"kind": "queued-stack", "frontier": _context(frontier), "unmerged_count": len(active_rows)},
            )


def _json_default(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, help="explicit recorded provider JSON")
    parser.add_argument("--owner", default="owner")
    parser.add_argument("--repo", default="repo")
    parser.add_argument("--pr", type=int, default=1)
    parser.add_argument("--mode", choices=("single", "stack", "queued-stack"), default="single")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=0)
    args = parser.parse_args(argv)
    try:
        context = PRContext(args.owner, args.repo, args.pr)
        reader = JsonProviderAdapter.from_file(args.fixture)
        if args.mode == "queued-stack" and args.status_only:
            parser.error("--status-only is not supported with --mode queued-stack")
        options = PollOptions(timeout=args.timeout)
        if args.mode == "queued-stack":
            result = run_queued(reader, [context], options=options)
        else:
            result = run_simple(reader, [context], mode=args.mode, status_only=args.status_only, options=options)
        rendered = json.dumps(result, default=_json_default, sort_keys=True)
    except SystemExit:
        raise
    except (UnknownProviderData, QueryFailure, TypeError, ValueError) as exc:
        if isinstance(exc, (TypeError, ValueError)) and not isinstance(exc, UnknownProviderData):
            exc = UnknownProviderData(f"watcher input is invalid: {exc}")
        print(
            json.dumps(
                {
                    "kind": "ERROR",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "schema_version": 1,
                    "terminal": True,
                    "exit_code": 2,
                },
                sort_keys=True,
            )
        )
        return 2
    print(rendered)
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
