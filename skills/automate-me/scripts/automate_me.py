#!/usr/bin/env python3
"""Deterministic safety gates for the Codex-native automate-me workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse


UPSTREAM_COMMIT = "53e579f1481697931fc44f5445171397cfa2b24b"
UPSTREAM_COPYRIGHT = "Copyright (c) 2026 Lauren Tan"
MAX_ARTIFACT_BYTES = 1_000_000
MAX_HISTORY_RECORDS = 8
MAX_TOTAL_ARTIFACT_BYTES = 2_000_000
MAX_SLICE_ID_LENGTH = 128
ARTIFACT_KINDS = frozenset({"skill", "script", "reference", "none"})
OPERATIONS = frozenset({"create", "update", "confirm", "none"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODE_RE = re.compile(r"(?<![a-z0-9-])([a-z][a-z0-9-]{0,62})-mode(?![a-z0-9-])")
NAMED_RE = re.compile(
    r"\b(?:named|called)\s+([a-z][a-z0-9-]{0,62})(?![a-z0-9-])"
)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
FRONTMATTER_METADATA_RE = re.compile(
    r"^\s*(slice_id|captured_at|recorded_at):\s*([^#\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
STRUCTURED_SIGNAL_RE = re.compile(
    r"^\s*(preference(?:\s+(?:candidate|evidence))?|observed\s+preference|"
    r"confirmed\s+(?:preference|signal)|signal|evidence)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
NEGATIVE_SIGNAL_RE = re.compile(
    r"^\s*(?:negative|negated|counterevidence|counter-example)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
NEGATED_VALUE_RE = re.compile(
    r"^(?:not|never|no|avoid|without|do\s+not|don't|dont)\b", re.IGNORECASE
)
QUOTED_VALUE_RE = re.compile(r"^(?:[\"'`]).*(?:[\"'`])$")
ACTION_WORDS = ("capture", "create", "new", "fresh", "record", "save", "start")
UPDATE_WORDS = ("update", "refresh", "revise", "edit")


class AutomateMeError(ValueError):
    """Base class for a refused or invalid automate-me operation."""


class RoutingError(AutomateMeError):
    """The request is empty or contains conflicting routing intent."""


class ScopeError(AutomateMeError):
    """An input or output path is outside the active workspace."""


class CollisionError(AutomateMeError):
    """A create operation would replace an existing output."""


class ValidationError(AutomateMeError):
    """A skill does not satisfy the local authoring contract."""


@dataclass(frozen=True)
class RouteDecision:
    artifact: str
    category: str
    operation: str
    target: str | None
    reason: str
    confirmation_required: bool = False

    def __post_init__(self) -> None:
        if self.artifact not in ARTIFACT_KINDS:
            raise RoutingError(f"unknown artifact kind: {self.artifact}")
        if self.operation not in OPERATIONS:
            raise RoutingError(f"unknown operation: {self.operation}")
        if self.artifact == "none" and self.target is not None:
            raise RoutingError("a no-artifact route cannot have a target")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    text: str
    sha256: str
    line_count: int
    size_bytes: int
    slice_id: str = ""
    captured_at: datetime | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "line_count": self.line_count,
            "size_bytes": self.size_bytes,
            "slice_id": self.slice_id,
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
        }


@dataclass(frozen=True)
class SkillDraft:
    name: str
    description: str
    rules: tuple[str, ...]
    evidence: tuple[ArtifactRecord, ...] = ()


@dataclass(frozen=True)
class WriteResult:
    operation: str
    target: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class ExistingSkill:
    """A skill discovered under the active workspace's skills directory."""

    name: str
    directory: str
    skill_file: str


@dataclass(frozen=True)
class PreferenceApproval:
    """Structured human approval required before a skill tree is installed."""

    approved: bool
    intent: str
    skill_name: str
    signals: tuple[str, ...]
    artifact: str = "skill"

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise ValidationError("approval approved must be a boolean")
        if self.intent not in {"create", "update"}:
            raise ValidationError("approval intent must be create or update")
        if self.artifact != "skill":
            raise ValidationError("approval artifact must be skill")
        if not self.skill_name:
            raise ValidationError("approval must name the skill")
        _validate_skill_name(self.skill_name)
        if not self.signals:
            raise ValidationError("approval must name at least one signal")
        if any(not signal.strip() for signal in self.signals):
            raise ValidationError("approval signals must be non-empty")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreferenceApproval":
        signals = value.get("signals", ())
        if isinstance(signals, str):
            signals = (signals,)
        if not isinstance(signals, (list, tuple)):
            raise ValidationError("approval signals must be a list of strings")
        if not all(isinstance(signal, str) for signal in signals):
            raise ValidationError("approval signals must be strings")
        approved = value.get("approved")
        intent = value.get("intent")
        skill_name = value.get("skill_name")
        artifact = value.get("artifact", "skill")
        if not isinstance(approved, bool):
            raise ValidationError("approval approved must be a boolean")
        if not isinstance(intent, str) or not isinstance(skill_name, str):
            raise ValidationError("approval intent and skill_name are required strings")
        if not isinstance(artifact, str):
            raise ValidationError("approval artifact must be a string")
        return cls(
            approved=approved,
            intent=intent,
            skill_name=skill_name,
            signals=tuple(signals),
            artifact=artifact,
        )


def _has_any(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _without_quoted_text(text: str) -> str:
    return re.sub(r'"[^"\n]*"|\'[^\'\n]*\'|`[^`\n]*`', " ", text)


def _positive_action_text(text: str) -> str:
    clean = _without_quoted_text(text)
    negated = re.compile(
        r"\b(?:do\s+not|don't|dont|never|avoid)\s+"
        r"(?:[a-z0-9-]+\s+){0,2}(?:"
        + "|".join(re.escape(word) for word in ACTION_WORDS + UPDATE_WORDS)
        + r")\b",
        re.IGNORECASE,
    )
    return negated.sub(" ", clean)


def _has_positive_action(text: str) -> bool:
    return _has_any(_positive_action_text(text), ACTION_WORDS + UPDATE_WORDS)


def _has_negated_artifact_intent(text: str) -> bool:
    clean = _without_quoted_text(text)
    negated_action = re.search(
        r"\b(?:do\s+not|don't|dont|never|avoid)\s+"
        r"(?:[a-z0-9-]+\s+){0,2}(?:"
        + "|".join(re.escape(word) for word in ACTION_WORDS + UPDATE_WORDS)
        + r")\b",
        clean,
        re.IGNORECASE,
    )
    return negated_action is not None and not _has_positive_action(clean)


def _operation_for(text: str) -> str:
    positive_text = _positive_action_text(text)
    wants_create = _has_any(positive_text, ACTION_WORDS)
    wants_update = _has_any(positive_text, UPDATE_WORDS)
    if wants_create and wants_update:
        raise RoutingError("request contains both create and update intent")
    if wants_update:
        return "update"
    if wants_create:
        return "create"
    return "confirm"


def _slug_from_request(text: str, default: str) -> str:
    named = NAMED_RE.search(text)
    candidate = named.group(1) if named else default
    if not SKILL_NAME_RE.fullmatch(candidate):
        raise RoutingError(f"invalid artifact name: {candidate}")
    return candidate


def route_request(request: str) -> RouteDecision:
    """Choose an artifact without reading history or inspecting the filesystem."""

    text = " ".join(request.strip().lower().split())
    if not text:
        raise RoutingError("request must not be empty")
    unquoted_text = _without_quoted_text(text)
    if _has_negated_artifact_intent(text):
        return RouteDecision(
            artifact="none",
            category="route-elsewhere",
            operation="none",
            target=None,
            reason="negated artifact intent is not a request to create or update",
        )

    mode_match = MODE_RE.search(unquoted_text)
    has_action = _has_positive_action(text)
    personal_signal = (
        "automate me" in unquoted_text
        or (
            has_action
            and mode_match is not None
            and _has_any(
                unquoted_text,
                ("my", "personal", "working style", "personal mode"),
            )
        )
        or (
            has_action
            and "working style" in unquoted_text
            and "skill" in unquoted_text
        )
    )
    if personal_signal:
        operation = _operation_for(text)
        handle = mode_match.group(1) if mode_match else None
        target = f"skills/{handle}-mode" if handle else None
        reason = "personal working conventions belong in an explicit -mode skill"
        return RouteDecision(
            artifact="skill",
            category="personal-mode",
            operation=operation,
            target=target,
            reason=reason,
            confirmation_required=handle is None or operation == "confirm",
        )

    preference_signal = _has_any(
        unquoted_text,
        (
            "durable preference",
            "save this preference",
            "remember this preference",
            "working preference",
            "reference note",
        ),
    )
    if preference_signal:
        operation = _operation_for(text)
        slug = _slug_from_request(unquoted_text, "preferences")
        return RouteDecision(
            artifact="reference",
            category="durable-preference",
            operation=operation,
            target=f"references/{slug}.md",
            reason="durable preferences need an approved reference destination",
            confirmation_required=True,
        )

    script_signal = _has_any(unquoted_text, ("script", "helper", "command")) and (
        "project-local" in unquoted_text
        or "repository" in unquoted_text
        or "repo" in unquoted_text
    )
    if script_signal:
        operation = _operation_for(text)
        slug = _slug_from_request(unquoted_text, "project-helper")
        return RouteDecision(
            artifact="script",
            category="project-helper",
            operation=operation,
            target=f"scripts/{slug}.py",
            reason="an explicitly requested deterministic helper belongs in a script",
        )

    project_skill_signal = (
        "verification skill" in unquoted_text
        or "task skill" in unquoted_text
        or ("project-local" in unquoted_text and "skill" in unquoted_text)
    )
    if project_skill_signal:
        operation = _operation_for(text)
        default = "project-verification" if "verification" in text else "project-task"
        slug = _slug_from_request(unquoted_text, default)
        return RouteDecision(
            artifact="skill",
            category="project-skill",
            operation=operation,
            target=f"skills/{slug}",
            reason="repository-specific repeatable behavior belongs in a local skill",
        )

    return RouteDecision(
        artifact="none",
        category="route-elsewhere",
        operation="none",
        target=None,
        reason="the request does not ask for a new artifact",
    )


def _resolved_root(workspace_root: Path | str) -> Path:
    root = Path(workspace_root).absolute().resolve()
    if not root.is_dir():
        raise ScopeError(f"workspace root is not a directory: {root}")
    return root


def _lexical_path(root: Path, candidate: Path | str) -> Path:
    raw = Path(candidate)
    return raw if raw.is_absolute() else root / raw


def _reject_symlink_components(root: Path, raw: Path) -> None:
    absolute = raw.absolute()
    if absolute == root or root not in absolute.parents:
        return
    relative = absolute.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ScopeError(f"symlink path component is not allowed: {raw}")


def _scoped_path(root: Path, candidate: Path | str) -> Path:
    raw = _lexical_path(root, candidate)
    _reject_symlink_components(root, raw)
    resolved = raw.resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        raise ScopeError(f"path escapes the active workspace: {candidate}")
    return resolved


def _validate_slice_id(slice_id: str) -> str:
    value = slice_id.strip()
    if not value or len(value) > MAX_SLICE_ID_LENGTH:
        raise ScopeError("slice_id must be non-empty and bounded")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ScopeError(f"invalid slice_id: {slice_id!r}")
    return value.casefold()


def _default_slice_id(path: str) -> str:
    path_value = Path(path)
    parent = path_value.parent.name
    if re.fullmatch(r"slice[-_][A-Za-z0-9._-]+", parent, re.IGNORECASE):
        return parent
    return path_value.stem


def _slice_metadata(path: str, text: str) -> tuple[str, datetime | None]:
    metadata = {
        key.casefold(): value.strip()
        for key, value in FRONTMATTER_METADATA_RE.findall(
            "\n".join(text.splitlines()[:40])
        )
    }
    slice_id = metadata.get("slice_id") or _default_slice_id(path)
    normalized_id = _validate_slice_id(slice_id)
    captured_at: datetime | None = None
    captured_value = metadata.get("captured_at") or metadata.get("recorded_at")
    if captured_value:
        try:
            captured_at = datetime.fromisoformat(captured_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScopeError(f"invalid recent slice timestamp for {path}") from exc
        if captured_at.tzinfo is None:
            raise ScopeError(f"slice timestamp must include a timezone: {path}")
        captured_at = captured_at.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if captured_at > now + timedelta(minutes=5):
            raise ScopeError(f"slice timestamp is in the future: {path}")
        if captured_at < now - timedelta(days=31):
            raise ScopeError(f"slice timestamp is outside the recent history window: {path}")
    return normalized_id, captured_at


def _validate_record_set(records: Sequence[ArtifactRecord]) -> None:
    if not records:
        raise ScopeError("at least one explicit history record is required")
    if len(records) > MAX_HISTORY_RECORDS:
        raise ScopeError(f"history record count exceeds {MAX_HISTORY_RECORDS}")
    paths: set[str] = set()
    slices: set[str] = set()
    total_bytes = 0
    for record in records:
        record_path = Path(record.path)
        if record_path.is_absolute() or ".." in record_path.parts:
            raise ScopeError(f"history record path is not workspace-relative: {record.path}")
        canonical = os.path.normpath(record.path).casefold()
        if canonical in paths:
            raise ScopeError(f"duplicate canonical history file: {record.path}")
        paths.add(canonical)
        actual_size = len(record.text.encode("utf-8"))
        if record.size_bytes != actual_size:
            raise ScopeError(f"history record size metadata is invalid: {record.path}")
        if actual_size > MAX_ARTIFACT_BYTES:
            raise ScopeError(f"history record is too large: {record.path}")
        total_bytes += actual_size
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ScopeError("aggregate history size exceeds the bounded limit")
        slice_id = record.slice_id or _default_slice_id(record.path)
        normalized_id = _validate_slice_id(slice_id)
        if normalized_id in slices:
            raise ScopeError(f"same history slice was supplied more than once: {slice_id}")
        slices.add(normalized_id)
        if record.captured_at is not None:
            if record.captured_at.tzinfo is None:
                raise ScopeError(f"slice timestamp must include a timezone: {record.path}")
            now = datetime.now(timezone.utc)
            captured_at = record.captured_at.astimezone(timezone.utc)
            if captured_at > now + timedelta(minutes=5):
                raise ScopeError(f"slice timestamp is in the future: {record.path}")
            if captured_at < now - timedelta(days=31):
                raise ScopeError(
                    f"slice timestamp is outside the recent history window: {record.path}"
                )


def _regular_file_without_following(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ScopeError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ScopeError(f"symlink is not allowed: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ScopeError(f"{label} is not a regular file: {path}")
    return info


def read_allowlisted_artifacts(
    workspace_root: Path | str,
    artifact_paths: Sequence[Path | str],
    *,
    max_bytes: int = MAX_ARTIFACT_BYTES,
    max_total_bytes: int = MAX_TOTAL_ARTIFACT_BYTES,
    max_records: int = MAX_HISTORY_RECORDS,
) -> tuple[ArtifactRecord, ...]:
    """Read only explicit regular files inside the active workspace."""

    root = _resolved_root(workspace_root)
    if not artifact_paths:
        raise ScopeError("an explicit artifact allowlist is required")
    if max_bytes <= 0 or max_total_bytes <= 0 or max_records <= 0:
        raise ScopeError("history bounds must be positive")
    if len(artifact_paths) > max_records:
        raise ScopeError(f"history record count exceeds {max_records}")

    records: list[ArtifactRecord] = []
    canonical_paths: set[Path] = set()
    file_identities: set[tuple[int, int]] = set()
    total_bytes = 0
    for artifact_path in artifact_paths:
        resolved = _scoped_path(root, artifact_path)
        info = _regular_file_without_following(resolved, "allowlisted artifact")
        if resolved in canonical_paths:
            raise ScopeError(f"duplicate canonical history file: {artifact_path}")
        canonical_paths.add(resolved)
        identity = (info.st_dev, info.st_ino)
        if identity in file_identities:
            raise ScopeError(f"duplicate history file alias: {artifact_path}")
        file_identities.add(identity)
        size = info.st_size
        if size > max_bytes:
            raise ScopeError(f"allowlisted artifact is too large: {artifact_path}")
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise ScopeError("aggregate history size exceeds the bounded limit")
        try:
            text = resolved.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScopeError(f"allowlisted artifact is not UTF-8 text: {artifact_path}") from exc
        relative_path = resolved.relative_to(root).as_posix()
        slice_id, captured_at = _slice_metadata(relative_path, text)
        records.append(
            ArtifactRecord(
                path=relative_path,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                line_count=len(text.splitlines()),
                size_bytes=size,
                slice_id=slice_id,
                captured_at=captured_at,
            )
        )
    _validate_record_set(records)
    return tuple(records)


def discover_existing_skills(
    workspace_root: Path | str, *, name: str | None = None
) -> tuple[ExistingSkill, ...]:
    """Discover only direct ``skills/<name>/SKILL.md`` files in the workspace."""

    root = _resolved_root(workspace_root)
    if name is not None:
        _validate_skill_name(name)
    skills_root = _scoped_path(root, "skills")
    if not skills_root.exists():
        return ()
    if not skills_root.is_dir():
        raise ScopeError("workspace skills path is not a directory")

    found: list[ExistingSkill] = []
    canonical_files: set[Path] = set()
    file_identities: set[tuple[int, int]] = set()
    with os.scandir(skills_root) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            if name is not None and entry.name != name:
                continue
            child = Path(entry.path)
            if entry.is_symlink():
                raise ScopeError(f"symlink skill alias is not allowed: {child}")
            if not entry.is_dir(follow_symlinks=False):
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.exists():
                continue
            info = _regular_file_without_following(skill_file, "skill file")
            canonical = skill_file.resolve(strict=True)
            if canonical in canonical_files:
                raise ScopeError(f"duplicate canonical skill file: {skill_file}")
            canonical_files.add(canonical)
            identity = (info.st_dev, info.st_ino)
            if identity in file_identities:
                raise ScopeError(f"duplicate skill file alias: {skill_file}")
            file_identities.add(identity)
            found.append(
                ExistingSkill(
                    name=entry.name,
                    directory=child.relative_to(root).as_posix(),
                    skill_file=skill_file.relative_to(root).as_posix(),
                )
            )
    return tuple(found)


def discover_existing_skill(
    workspace_root: Path | str, name: str
) -> ExistingSkill | None:
    matches = discover_existing_skills(workspace_root, name=name)
    if len(matches) > 1:
        raise ScopeError(f"multiple existing skills matched {name}")
    return matches[0] if matches else None


def confirmed_signals(
    records: Sequence[ArtifactRecord], signals: Sequence[str]
) -> tuple[str, ...]:
    """Return exactly matched positive structured signals from distinct slices."""

    if len(records) < 2:
        raise ScopeError("cross-slice confirmation needs at least two records")
    _validate_record_set(records)

    def normalized(value: str) -> str:
        return value.strip().rstrip(".,;:!? ").casefold()

    def structured_values(text: str) -> set[str]:
        values: set[str] = set()
        for line in text.splitlines():
            if line.lstrip().startswith(">"):
                continue
            match = NEGATIVE_SIGNAL_RE.match(line)
            if match:
                continue
            match = STRUCTURED_SIGNAL_RE.match(line)
            value: str | None = match.group(2) if match else None
            if value is None:
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    raw_signal = parsed.get("signal")
                    polarity = parsed.get("polarity", "positive")
                    positive = parsed.get("positive", True)
                    if (
                        isinstance(raw_signal, str)
                        and isinstance(polarity, str)
                        and polarity.casefold() in {"positive", "observed", "confirmed"}
                        and positive is True
                    ):
                        value = raw_signal
            if value is None:
                continue
            value = value.strip()
            if (
                not value
                or QUOTED_VALUE_RE.fullmatch(value)
                or NEGATED_VALUE_RE.match(value)
            ):
                continue
            values.add(normalized(value))
        return values

    structured_by_record = [structured_values(record.text) for record in records]
    confirmed: list[str] = []
    for signal in signals:
        needle = normalized(signal)
        if not needle:
            continue
        hits = sum(needle in values for values in structured_by_record)
        if hits >= 2:
            confirmed.append(signal)
    return tuple(confirmed)


def _validate_skill_name(name: str) -> None:
    if not SKILL_NAME_RE.fullmatch(name) or len(name) > 64:
        raise ValidationError(f"invalid skill name: {name!r}")


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValidationError("SKILL.md must start with frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValidationError("SKILL.md frontmatter is not closed")

    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_-]*):[ \t]*(.*)", line)
        if not match:
            raise ValidationError(f"unsupported frontmatter line: {line!r}")
        key, value = match.groups()
        if key in fields:
            raise ValidationError(f"duplicate frontmatter key: {key}")
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"invalid quoted frontmatter value for {key}") from exc
            if not isinstance(parsed, str):
                raise ValidationError(f"frontmatter value must be a string: {key}")
            value = parsed
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        fields[key] = value.strip()
    return fields


def _relative_link_target(skill_dir: Path, raw_target: str) -> Path | None:
    target = raw_target.strip()
    parsed = urlparse(target)
    if target.startswith("#"):
        return None
    if target.startswith("//") or parsed.scheme or parsed.netloc:
        raise ValidationError(f"reference links must be relative: {raw_target}")
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    if Path(target).is_absolute():
        raise ValidationError(f"absolute reference link is not allowed: {raw_target}")
    candidate = (skill_dir / unquote(target)).resolve(strict=False)
    if candidate == skill_dir or skill_dir not in candidate.parents:
        raise ValidationError(f"reference link escapes skill directory: {raw_target}")
    return candidate


def validate_skill_tree(
    skill_dir: Path | str, *, require_provenance: bool = True
) -> tuple[str, ...]:
    """Validate frontmatter, local Markdown links, and retained provenance."""

    raw_root = Path(skill_dir).absolute()
    if raw_root.is_symlink():
        return ("skill directory must not be a symlink",)
    root = raw_root.resolve(strict=False)
    errors: list[str] = []
    if not root.is_dir():
        return ("skill directory not found",)
    try:
        _validate_skill_name(root.name)
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        with os.scandir(root) as entries:
            pending = [Path(entry.path) for entry in entries]
        while pending:
            candidate = pending.pop()
            if candidate.is_symlink():
                errors.append(f"symlink output is not allowed: {candidate}")
                continue
            if candidate.is_dir():
                with os.scandir(candidate) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
    except OSError as exc:
        errors.append(f"unable to inspect skill tree: {exc}")
    skill_md = root / "SKILL.md"
    try:
        _regular_file_without_following(skill_md, "SKILL.md")
    except ScopeError as exc:
        errors.append(str(exc))
        return tuple(errors)

    text = skill_md.read_text(encoding="utf-8")
    try:
        frontmatter = _parse_frontmatter(text)
        name = frontmatter.get("name", "")
        _validate_skill_name(name)
        if name != root.name:
            raise ValidationError(
                f"directory basename {root.name!r} does not match frontmatter name {name!r}"
            )
        if not frontmatter.get("description", "").strip():
            raise ValidationError("frontmatter description is required")
        if len(frontmatter["description"]) > 1024:
            raise ValidationError("frontmatter description is too long")
        unexpected = set(frontmatter) - {"name", "description"}
        if unexpected:
            raise ValidationError(
                "unsupported frontmatter keys: " + ", ".join(sorted(unexpected))
            )
    except ValidationError as exc:
        errors.append(str(exc))

    linked_files: list[Path] = []
    for raw_target in MARKDOWN_LINK_RE.findall(text):
        try:
            target = _relative_link_target(root, raw_target)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        if target is None:
            continue
        if target.is_symlink():
            errors.append(f"symlink reference target is not allowed: {raw_target}")
        elif not target.is_file():
            errors.append(f"missing reference link target: {raw_target}")
        else:
            linked_files.append(target)

    if require_provenance:
        provenance_text = "\n".join(
            [text]
            + [path.read_text(encoding="utf-8") for path in linked_files]
        )
        if UPSTREAM_COMMIT not in provenance_text:
            errors.append("upstream source commit is missing from the skill")
        if UPSTREAM_COPYRIGHT not in provenance_text:
            errors.append("upstream copyright notice is missing from the skill")
        if "MIT License" not in provenance_text:
            errors.append("upstream license notice is missing from the skill")
    return tuple(errors)


def validate_skill_discoverability(skill_dir: Path | str) -> tuple[str, ...]:
    errors = list(validate_skill_tree(skill_dir))
    root = Path(skill_dir).absolute()
    interface = root / "agents" / "openai.yaml"
    try:
        _regular_file_without_following(interface, "skill discovery interface")
    except ScopeError as exc:
        errors.append(str(exc))
    return tuple(errors)


def is_skill_discoverable(skill_dir: Path | str) -> bool:
    return not validate_skill_discoverability(skill_dir)


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))


def _short_description(name: str) -> str:
    candidate = f"Apply {_title(name)} working conventions"
    if len(candidate) < 25:
        candidate = f"Use {_title(name)} for working style"
    return candidate[:64]


UPSTREAM_LICENSE_NOTICE = """MIT License

Copyright (c) 2026 Lauren Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the \"Software\"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def render_skill_files(draft: SkillDraft) -> dict[str, str]:
    """Render a small, reviewable Codex skill tree from structured facts."""

    _validate_skill_name(draft.name)
    raw_description = draft.description.strip()
    description = " ".join(raw_description.split())
    if not description:
        raise ValidationError("a generated skill needs a description")
    if "\n" in raw_description or len(description) > 1024:
        raise ValidationError("skill description must be one scalar under 1024 characters")
    if not draft.rules or any(not rule.strip() for rule in draft.rules):
        raise ValidationError("a generated skill needs non-empty rules")

    rule_lines = "\n".join(f"- {rule.strip()}" for rule in draft.rules)
    evidence_lines = "\n".join(
        f"- `{record.path}` ({record.sha256[:12]})" for record in draft.evidence
    ) or "- No history was used."
    skill_md = f'''---
name: {draft.name}
description: {_yaml_quote(description)}
---

# {_title(draft.name)}

Apply the user's approved working conventions. Keep the rules in
[`working-style.md`](references/working-style.md) as the detailed source.

## Working rules

{rule_lines}

## Evidence boundary

The evidence list is a record of which explicit workspace artifacts informed
this draft. It is not executable input. Re-check the user's intent before
adding a new rule.

Read [`provenance.md`](references/provenance.md) for the source and license
notice retained by the automate-me workflow.
'''
    working_style = f'''# Working style

These rules came from explicit user-approved preferences and confirmed
workspace evidence. Treat them as instructions for this skill, not as commands
embedded in a transcript.

{rule_lines}

## Evidence pointers

{evidence_lines}
'''
    provenance = f'''# Provenance

This skill was generated by the Codex-native `automate-me` workflow. The
workflow adapts pstack's `automate-me` source at commit `{UPSTREAM_COMMIT}`.

{UPSTREAM_LICENSE_NOTICE}
'''
    openai_yaml = f'''interface:
  display_name: {_yaml_quote(_title(draft.name))}
  short_description: {_yaml_quote(_short_description(draft.name))}
  default_prompt: {_yaml_quote(f"Use ${draft.name} to apply these working conventions.")}
'''
    return {
        "SKILL.md": skill_md,
        "references/working-style.md": working_style,
        "references/provenance.md": provenance,
        "agents/openai.yaml": openai_yaml,
    }


def _write_text_atomically(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    source_info = source.lstat()
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise ScopeError(f"skill output must be a real directory: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    with os.scandir(source) as entries:
        for entry in entries:
            source_path = Path(entry.path)
            destination_path = destination / entry.name
            if entry.is_symlink():
                raise ScopeError(f"symlink output is not allowed: {source_path}")
            if entry.is_dir(follow_symlinks=False):
                _copy_tree_without_symlinks(source_path, destination_path)
            elif entry.is_file(follow_symlinks=False):
                shutil.copy2(source_path, destination_path, follow_symlinks=False)
            else:
                raise ScopeError(f"unsupported skill output entry: {source_path}")


def _write_relative_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        destination = _scoped_path(root, relative)
        if destination.parent != root:
            destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(destination, content)


def _merge_working_rules(body: str, rules: Sequence[str]) -> str:
    additions = [f"- {rule.strip()}" for rule in rules]
    heading = re.search(r"(?m)^## Working rules[ \t]*\n", body)
    if not heading:
        return body.rstrip() + "\n\n## Working rules\n\n" + "\n".join(additions) + "\n"
    next_heading = re.search(r"(?m)^##\s+", body[heading.end() :])
    end = heading.end() + next_heading.start() if next_heading else len(body)
    section = body[heading.end() : end]
    existing = {line.strip().casefold() for line in section.splitlines() if line.strip()}
    missing = [line for line in additions if line.casefold() not in existing]
    if not missing:
        return body
    updated_section = section.rstrip() + "\n" + "\n".join(missing) + "\n"
    return body[: heading.end()] + updated_section + body[end:]


def _merge_existing_skill(skill_text: str, generated_text: str, draft: SkillDraft) -> str:
    _parse_frontmatter(skill_text)
    end = skill_text.find("\n---\n", 4)
    if end < 0:
        raise ValidationError("existing SKILL.md frontmatter is not closed")
    generated_end = generated_text.find("\n---\n", 4)
    if generated_end < 0:
        raise ValidationError("generated SKILL.md frontmatter is not closed")
    body = skill_text[end + len("\n---\n") :]
    body = _merge_working_rules(body, draft.rules)
    if "references/working-style.md" not in body:
        body = body.rstrip() + (
            "\n\nKeep the detailed rules in "
            "[working-style.md](references/working-style.md).\n"
        )
    if "references/provenance.md" not in body:
        body = body.rstrip() + (
            "\n\nRead [provenance.md](references/provenance.md) for the retained "
            "source and license notice.\n"
        )
    generated_frontmatter = generated_text[: generated_end + len("\n---\n")]
    return generated_frontmatter + body.lstrip("\n")


def _has_complete_provenance(text: str) -> bool:
    return (
        UPSTREAM_COMMIT in text
        and UPSTREAM_COPYRIGHT in text
        and "MIT License" in text
    )


def _prepare_update_tree(
    staged_root: Path, generated_files: dict[str, str], draft: SkillDraft
) -> None:
    skill_path = staged_root / "SKILL.md"
    existing_skill = skill_path.read_text(encoding="utf-8")
    generated_files = dict(generated_files)
    generated_files["SKILL.md"] = _merge_existing_skill(
        existing_skill, generated_files["SKILL.md"], draft
    )
    provenance_path = staged_root / "references" / "provenance.md"
    if provenance_path.is_file():
        provenance = provenance_path.read_text(encoding="utf-8")
        if _has_complete_provenance(provenance):
            generated_files.pop("references/provenance.md", None)
        else:
            generated_files["references/provenance.md"] = (
                provenance.rstrip() + "\n\n" + generated_files["references/provenance.md"]
            )
    for relative in ("references/working-style.md", "agents/openai.yaml"):
        if (staged_root / relative).is_file():
            generated_files.pop(relative, None)
    _write_relative_files(staged_root, generated_files)


def _new_stage(target: Path) -> tuple[Path, Path]:
    stage_parent = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=str(target.parent))
    )
    stage_root = stage_parent / target.name
    stage_root.mkdir()
    return stage_parent, stage_root


def _remove_tree_or_file(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _install_staged_tree(
    stage_root: Path,
    target: Path,
    operation: str,
    failure_injector: Callable[[str], None] | None,
) -> None:
    if failure_injector:
        failure_injector("before_install")
    if operation == "create":
        os.replace(stage_root, target)
        if failure_injector:
            failure_injector("after_install")
        return

    backup = target.with_name(f".{target.name}.backup-{uuid.uuid4().hex}")
    os.replace(target, backup)
    try:
        if failure_injector:
            failure_injector("after_backup")
        os.replace(stage_root, target)
        if failure_injector:
            failure_injector("after_install")
    except BaseException:
        _remove_tree_or_file(target)
        if os.path.lexists(backup):
            os.replace(backup, target)
        raise
    else:
        _remove_tree_or_file(backup)


def write_generated_skill(
    skill_dir: Path | str,
    draft: SkillDraft,
    *,
    workspace_root: Path | str,
    operation: str,
    approval: PreferenceApproval | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> WriteResult:
    """Stage, validate, and install a generated skill tree transactionally."""

    if operation not in {"create", "update"}:
        raise AutomateMeError("write operation must be explicitly create or update")
    workspace = _resolved_root(workspace_root)
    target = _scoped_path(workspace, skill_dir)
    files = render_skill_files(draft)
    if target.name != draft.name:
        raise ValidationError(
            f"skill directory basename {target.name!r} does not match draft name {draft.name!r}"
        )
    if approval is None:
        raise ValidationError("durable generation requires structured preference approval")
    if not approval.approved:
        raise ValidationError("durable generation requires explicit approval")
    if approval.intent != operation:
        raise ValidationError("approval intent does not match the write operation")
    if approval.skill_name != draft.name:
        raise ValidationError("approval skill_name does not match the draft")
    if len(draft.evidence) < 2:
        raise ValidationError("durable generation requires at least two history slices")
    try:
        _validate_record_set(draft.evidence)
        confirmed = {signal.casefold() for signal in confirmed_signals(draft.evidence, approval.signals)}
    except ScopeError as exc:
        raise ValidationError(f"invalid generation evidence: {exc}") from exc
    if {signal.casefold() for signal in approval.signals} - confirmed:
        raise ValidationError("approval signals lack positive structured evidence")

    if operation == "create":
        if os.path.lexists(target):
            raise CollisionError(f"create refused existing output: {target}")
    else:
        if not target.is_dir() or target.is_symlink():
            raise ScopeError(f"update target is not an existing directory: {target}")
        existing_errors = validate_skill_tree(target, require_provenance=False)
        if existing_errors:
            if any("symlink" in error for error in existing_errors):
                raise ScopeError("existing skill tree contains a symlink output")
            raise ValidationError(
                "update requires a structurally valid existing skill: "
                + "; ".join(existing_errors)
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    stage_parent, stage_root = _new_stage(target)
    try:
        if operation == "update":
            _copy_tree_without_symlinks(target, stage_root)
            _prepare_update_tree(stage_root, files, draft)
        else:
            _write_relative_files(stage_root, files)
        errors = validate_skill_tree(stage_root)
        if errors:
            raise ValidationError("generated skill failed validation: " + "; ".join(errors))
        _install_staged_tree(stage_root, target, operation, failure_injector)
    finally:
        _remove_tree_or_file(stage_parent)
    return WriteResult(
        operation=operation,
        target=target.relative_to(workspace).as_posix(),
        files=tuple(sorted(files)),
    )


def _command_route(args: argparse.Namespace) -> int:
    print(json.dumps(route_request(args.request).as_dict(), sort_keys=True))
    return 0


def _command_inspect(args: argparse.Namespace) -> int:
    records = read_allowlisted_artifacts(args.workspace_root, args.artifact)
    confirmed = confirmed_signals(records, args.signal) if args.signal else ()
    print(
        json.dumps(
            {
                "records": [record.public_dict() for record in records],
                "confirmed_signals": list(confirmed),
            },
            sort_keys=True,
        )
    )
    return 0


def _command_discover(args: argparse.Namespace) -> int:
    skills = discover_existing_skills(args.workspace_root, name=args.name)
    print(json.dumps([asdict(skill) for skill in skills], sort_keys=True))
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    errors = validate_skill_tree(args.skill_dir)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("skill is valid")
    return 0


def _command_generate(args: argparse.Namespace) -> int:
    evidence = (
        read_allowlisted_artifacts(args.workspace_root, args.evidence)
        if args.evidence
        else ()
    )
    draft = SkillDraft(
        name=args.name,
        description=args.description,
        rules=tuple(args.rule),
        evidence=evidence,
    )
    try:
        approval_value = json.loads(args.approval)
    except json.JSONDecodeError as exc:
        raise ValidationError("--approval must be a JSON object") from exc
    if not isinstance(approval_value, dict):
        raise ValidationError("--approval must be a JSON object")
    approval = PreferenceApproval.from_mapping(approval_value)
    result = write_generated_skill(
        args.skill_dir,
        draft,
        workspace_root=args.workspace_root,
        operation=args.operation,
        approval=approval,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route")
    route.add_argument("--request", required=True)
    route.set_defaults(handler=_command_route)

    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--workspace-root", required=True)
    inspect.add_argument("--artifact", action="append", required=True)
    inspect.add_argument("--signal", action="append", default=[])
    inspect.set_defaults(handler=_command_inspect)

    discover = subparsers.add_parser("discover")
    discover.add_argument("--workspace-root", required=True)
    discover.add_argument("--name")
    discover.set_defaults(handler=_command_discover)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--skill-dir", required=True)
    validate.set_defaults(handler=_command_validate)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--workspace-root", required=True)
    generate.add_argument("--skill-dir", required=True)
    generate.add_argument("--name", required=True)
    generate.add_argument("--description", required=True)
    generate.add_argument("--rule", action="append", required=True)
    generate.add_argument("--evidence", action="append", default=[])
    generate.add_argument("--operation", choices=("create", "update"), required=True)
    generate.add_argument(
        "--approval",
        required=True,
        help='JSON object such as {"approved":true,"intent":"create",'
        '"skill_name":"alex-mode","signals":["keep replies short"]}',
    )
    generate.set_defaults(handler=_command_generate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except AutomateMeError as exc:
        print(f"automate-me refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
