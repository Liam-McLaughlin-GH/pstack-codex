#!/usr/bin/env python3
"""Deterministic safety gates for the Codex-native automate-me workflow."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlparse

import fcntl


UPSTREAM_COMMIT = "53e579f1481697931fc44f5445171397cfa2b24b"
UPSTREAM_COPYRIGHT = "Copyright (c) 2026 Lauren Tan"
MAX_ARTIFACT_BYTES = 1_000_000
MAX_HISTORY_RECORDS = 8
MAX_TOTAL_ARTIFACT_BYTES = 2_000_000
MAX_SLICE_ID_LENGTH = 128
RECOVERY_DIRECTORY_NAME = ".automate-me-recovery"
TRANSACTION_ARTIFACT_RE = re.compile(
    r"^\.[a-z0-9]+(?:-[a-z0-9]+)*\.(?:backup|stage)-[A-Za-z0-9_]+$"
)
ARTIFACT_KINDS = frozenset({"skill", "script", "reference", "none"})
OPERATIONS = frozenset({"create", "update", "confirm", "none"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODE_RE = re.compile(
    r"(?<![a-z0-9_.-])([a-z][a-z0-9_.-]*-mode[a-z0-9_.-]*)(?![a-z0-9_.-])"
)
NAMED_RE = re.compile(
    r"\b(?:named|called)\s+([^\s,;:!?()[\]{}]+)", re.IGNORECASE
)
INVALID_ACTION_NAME_RE = re.compile(
    r"\b(?:capture|create|new|fresh|record|save|start|update|refresh|revise|edit)\b"
    r"\s+(?:(?:my|a|an|the)\s+)?([a-z][a-z0-9_.-]*\.[a-z0-9_.-]+)"
    r"(?=$|[\s,;:!?()[\]{}])",
    re.IGNORECASE,
)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
MARKDOWN_REFERENCE_LINK_RE = re.compile(
    r"(?m)^\s{0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))"
)
MARKDOWN_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
FRONTMATTER_METADATA_RE = re.compile(
    r"^\s*(slice_id|captured_at|recorded_at):\s*([^#\n]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
FRONTMATTER_NON_STRING_RE = re.compile(
    r"^(?:[-+]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?|"
    r"[-+]?0[xX][0-9a-fA-F]+|[-+]?0[oO][0-7]+|[-+]?0[bB][01]+|"
    r"[-+]?\.(?:inf|nan)|~|null|true|false|yes|no|on|off|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ][^ ]+)?|"
    r"\[.*\]|\{.*\}|[!&*].*)$",
    re.IGNORECASE,
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
NEGATION_PREFIX = r"(?:do\s+not|don't|dont|never|avoid|refuse\s+to|not)"
NEGATED_ACTION_RE = re.compile(
    r"\b"
    + NEGATION_PREFIX
    + r"\b(?:(?!\b(?:but|however)\b)[^\n.!?;]){0,96}?\b(?:"
    + "|".join(re.escape(word) for word in ACTION_WORDS + UPDATE_WORDS)
    + r")\b",
    re.IGNORECASE,
)
AUTOMATE_ME_RE = re.compile(
    r"\bautomate\b(?:[\s,;:!?()[\]{}\u2013\u2014-]+)\bme\b", re.IGNORECASE
)
NEGATED_AUTOMATE_ME_RE = re.compile(
    r"\b"
    + NEGATION_PREFIX
    + r"\b(?:(?!\b(?:but|however)\b)[^\n.!?;]){0,120}?\bautomate\b"
    + r"(?:[\s,;:!?()[\]{}\u2013\u2014-]+)\bme\b",
    re.IGNORECASE,
)
NOFOLLOW_FLAGS = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_CLOEXEC
if not NOFOLLOW_FLAGS:
    raise RuntimeError("automate-me requires O_NOFOLLOW support")

_ALLOWLIST_READER_MARKER = object()


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


class PublicationRecoveryError(OSError):
    """Publication stopped after reporting the deterministic recovery state."""

    def __init__(
        self,
        *,
        target: Path,
        recovery_path: Path,
        cause: BaseException,
        recovery_status: str,
        cleanup_status: str,
        competitor_preserved: bool,
    ) -> None:
        self.target = target
        self.recovery_path = recovery_path
        self.recovery_status = recovery_status
        self.cleanup_status = cleanup_status
        self.competitor_preserved = competitor_preserved
        self.cause = cause
        super().__init__(
            "update publication stopped; recovery_status="
            f"{recovery_status} cleanup_status={cleanup_status} "
            f"competitor_preserved={str(competitor_preserved).lower()} "
            f"approved_tree={recovery_path}"
        )


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
    _allowlist_reader_marker: object | None = field(
        default=None, init=False, repr=False, compare=False
    )

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
class PreferenceQuestion:
    """One bounded structured question asked before durable generation."""

    question_id: str
    prompt: str
    options: tuple[str, ...]
    allow_multiple: bool = False
    follow_up_for: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question_id, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", self.question_id
        ):
            raise ValidationError(f"invalid preference question id: {self.question_id!r}")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValidationError("preference question prompt is required")
        if not isinstance(self.allow_multiple, bool):
            raise ValidationError("preference allow_multiple must be a boolean")
        if not isinstance(self.options, (list, tuple)) or not all(
            isinstance(option, str) for option in self.options
        ):
            raise ValidationError("preference question options must be strings")
        if not 4 <= len(self.options) <= 6:
            raise ValidationError("each preference question needs 4 to 6 options")
        normalized = [option.strip().casefold() for option in self.options]
        if any(not option for option in normalized):
            raise ValidationError("preference options must be non-empty")
        if len(set(normalized)) != len(normalized):
            raise ValidationError("preference options must be unique")
        if self.follow_up_for is not None:
            if not isinstance(self.follow_up_for, str) or not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,63}", self.follow_up_for
            ):
                raise ValidationError("invalid preference follow-up question id")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreferenceQuestion":
        question_id = value.get("id", value.get("question_id"))
        prompt = value.get("prompt")
        options = value.get("options")
        allow_multiple = value.get("allow_multiple", value.get("multi_select", False))
        follow_up_for = value.get("follow_up_for")
        if not isinstance(question_id, str) or not isinstance(prompt, str):
            raise ValidationError("preference questions need string id and prompt")
        if not isinstance(options, (list, tuple)) or not all(
            isinstance(option, str) for option in options
        ):
            raise ValidationError("preference question options must be strings")
        if not isinstance(allow_multiple, bool):
            raise ValidationError("preference allow_multiple must be a boolean")
        if follow_up_for is not None and not isinstance(follow_up_for, str):
            raise ValidationError("preference follow_up_for must be a string")
        return cls(
            question_id=question_id,
            prompt=prompt,
            options=tuple(options),
            allow_multiple=allow_multiple,
            follow_up_for=follow_up_for,
        )


@dataclass(frozen=True)
class PreferenceInteraction:
    """The required small, structured preference interview."""

    questions: tuple[PreferenceQuestion, ...]
    free_form_prompt: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.questions) <= 2:
            raise ValidationError("preference interaction needs 1 or 2 questions")
        if not all(isinstance(question, PreferenceQuestion) for question in self.questions):
            raise ValidationError("preference interaction questions must be structured")
        if len({question.question_id for question in self.questions}) != len(self.questions):
            raise ValidationError("preference question ids must be unique")
        if not isinstance(self.free_form_prompt, str) or not self.free_form_prompt.strip():
            raise ValidationError("a free-form preference prompt is required")
        if len(self.questions) == 1:
            if self.questions[0].follow_up_for is not None:
                raise ValidationError("a one-question interaction cannot have a follow-up")
        else:
            first, follow_up = self.questions
            if first.follow_up_for is not None:
                raise ValidationError("the first preference question cannot be a follow-up")
            if follow_up.follow_up_for != first.question_id:
                raise ValidationError(
                    "the second preference question must be a focused follow-up"
                )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreferenceInteraction":
        questions = value.get("questions")
        free_form_prompt = value.get("free_form_prompt", value.get("free_form"))
        if not isinstance(questions, (list, tuple)):
            raise ValidationError("preference interaction questions are required")
        if not all(isinstance(question, Mapping) for question in questions):
            raise ValidationError("preference interaction questions must be objects")
        if not isinstance(free_form_prompt, str):
            raise ValidationError("preference interaction free_form_prompt is required")
        return cls(
            questions=tuple(
                PreferenceQuestion.from_mapping(question) for question in questions
            ),
            free_form_prompt=free_form_prompt,
        )

    def validate_response(self, response: "PreferenceResponse") -> tuple[str, ...]:
        errors: list[str] = []
        if response.status not in {"incomplete", "declined", "accepted"}:
            errors.append("preference response status must be incomplete, declined, or accepted")
            return tuple(errors)
        if response.status != "accepted":
            return tuple(errors)
        expected_ids = {question.question_id for question in self.questions}
        actual_ids = set(response.answers)
        missing = expected_ids - actual_ids
        extra = actual_ids - expected_ids
        if missing:
            errors.append("accepted preference response is missing question answers")
        if extra:
            errors.append("accepted preference response contains an unknown question")
        for question in self.questions:
            answer = response.answers.get(question.question_id, ())
            if not isinstance(answer, (list, tuple)):
                errors.append(f"answer for {question.question_id} must be a list")
                continue
            if not answer:
                errors.append(f"answer for {question.question_id} is required")
            if not question.allow_multiple and len(answer) > 1:
                errors.append(f"answer for {question.question_id} must select one option")
            allowed = {option.casefold() for option in question.options}
            if any(not isinstance(option, str) for option in answer):
                errors.append(f"answer for {question.question_id} must contain strings")
            elif any(option.casefold() not in allowed for option in answer):
                errors.append(f"answer for {question.question_id} contains an unknown option")
        if not response.free_form.strip():
            errors.append("accepted preference response needs a free-form response")
        return tuple(errors)


@dataclass(frozen=True)
class PreferenceResponse:
    """The user's response to a :class:`PreferenceInteraction`."""

    status: str
    answers: Mapping[str, tuple[str, ...]]
    free_form: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"incomplete", "declined", "accepted"}:
            raise ValidationError(
                "preference response status must be incomplete, declined, or accepted"
            )
        if not isinstance(self.answers, Mapping):
            raise ValidationError("preference answers must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for question_id, answer in self.answers.items():
            if not isinstance(question_id, str):
                raise ValidationError("preference answer ids must be strings")
            if not isinstance(answer, (list, tuple)) or not all(
                isinstance(option, str) for option in answer
            ):
                raise ValidationError("preference answers must be lists of strings")
            normalized[question_id] = tuple(answer)
        object.__setattr__(self, "answers", normalized)
        if not isinstance(self.free_form, str):
            raise ValidationError("preference free_form must be a string")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreferenceResponse":
        status = value.get("status")
        answers = value.get("answers", {})
        free_form = value.get("free_form", value.get("free_form_response", ""))
        if not isinstance(status, str) or not isinstance(answers, Mapping):
            raise ValidationError("preference response needs status and answers")
        if not isinstance(free_form, str):
            raise ValidationError("preference free_form must be a string")
        converted: dict[str, tuple[str, ...]] = {}
        for question_id, answer in answers.items():
            if not isinstance(question_id, str) or not isinstance(answer, (list, tuple)):
                raise ValidationError("preference answers must map ids to lists")
            if not all(isinstance(option, str) for option in answer):
                raise ValidationError("preference answers must contain strings")
            converted[question_id] = tuple(answer)
        return cls(status=status, answers=converted, free_form=free_form)


@dataclass(frozen=True)
class PreferenceApproval:
    """Structured human approval required before a skill tree is installed."""

    approved: bool
    intent: str
    skill_name: str
    signals: tuple[str, ...]
    artifact: str = "skill"
    interaction: PreferenceInteraction | None = None
    response: PreferenceResponse | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise ValidationError("approval approved must be a boolean")
        if self.intent not in {"create", "update"}:
            raise ValidationError("approval intent must be create or update")
        if self.artifact != "skill":
            raise ValidationError("approval artifact must be skill")
        if not isinstance(self.skill_name, str) or not self.skill_name:
            raise ValidationError("approval must name the skill")
        _validate_skill_name(self.skill_name)
        if not isinstance(self.signals, (list, tuple)) or not all(
            isinstance(signal, str) for signal in self.signals
        ):
            raise ValidationError("approval signals must be strings")
        if not self.signals:
            raise ValidationError("approval must name at least one signal")
        if any(not signal.strip() for signal in self.signals):
            raise ValidationError("approval signals must be non-empty")
        if self.interaction is not None and not isinstance(
            self.interaction, PreferenceInteraction
        ):
            raise ValidationError("approval interaction must be structured")
        if self.response is not None and not isinstance(self.response, PreferenceResponse):
            raise ValidationError("approval response must be structured")
        if self.interaction is not None and self.response is not None:
            response_errors = self.interaction.validate_response(self.response)
            if response_errors:
                raise ValidationError("; ".join(response_errors))
        if self.approved and (
            self.interaction is None
            or self.response is None
            or self.response.status != "accepted"
        ):
            raise ValidationError("approved generation requires an accepted preference response")

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
        interaction_value = value.get("interaction")
        response_value = value.get("response")
        if not isinstance(approved, bool):
            raise ValidationError("approval approved must be a boolean")
        if not isinstance(intent, str) or not isinstance(skill_name, str):
            raise ValidationError("approval intent and skill_name are required strings")
        if not isinstance(artifact, str):
            raise ValidationError("approval artifact must be a string")
        if not isinstance(interaction_value, Mapping):
            raise ValidationError("approval interaction is required")
        if not isinstance(response_value, Mapping):
            raise ValidationError("approval response is required")
        return cls(
            approved=approved,
            intent=intent,
            skill_name=skill_name,
            signals=tuple(signals),
            artifact=artifact,
            interaction=PreferenceInteraction.from_mapping(interaction_value),
            response=PreferenceResponse.from_mapping(response_value),
        )


def _has_any(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _without_quoted_text(text: str) -> str:
    return re.sub(r'"[^"\n]*"|\'[^\'\n]*\'|`[^`\n]*`', " ", text)


def _positive_action_text(text: str) -> str:
    clean = _without_quoted_text(text)
    return NEGATED_ACTION_RE.sub(" ", clean)


def _has_positive_action(text: str) -> bool:
    return _has_any(_positive_action_text(text), ACTION_WORDS + UPDATE_WORDS)


def _has_automate_me_invocation(text: str) -> bool:
    """Recognize an invocation, not a noun phrase naming the helper."""

    for match in AUTOMATE_ME_RE.finditer(text):
        following = text[match.end() :].lstrip(" ,;:!?()[]{}\u2013\u2014-")
        if re.match(r"(?:helper|workflow|skill|command)\b", following):
            continue
        if re.match(r"(?:is|was|has|had|already|currently)\b", following):
            continue
        return True
    return False


def _has_negated_artifact_intent(text: str) -> bool:
    clean = _without_quoted_text(text)
    negated_action = NEGATED_ACTION_RE.search(clean)
    negated_automate = NEGATED_AUTOMATE_ME_RE.search(clean)
    return (negated_action is not None or negated_automate is not None) and not _has_positive_action(
        clean
    )


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
    candidate = named.group(1).rstrip(".") if named else default
    if not SKILL_NAME_RE.fullmatch(candidate):
        raise RoutingError(f"invalid artifact name: {candidate}")
    return candidate


def _mode_name_from_request(text: str) -> str | None:
    match = MODE_RE.search(text)
    if match is None:
        return None
    candidate = match.group(1).rstrip(".")
    if not SKILL_NAME_RE.fullmatch(candidate):
        raise RoutingError(f"invalid artifact name: {candidate}")
    return candidate


def route_request(request: str) -> RouteDecision:
    """Choose an artifact without reading history or inspecting the filesystem."""

    text = " ".join(request.strip().lower().split())
    if not text:
        raise RoutingError("request must not be empty")
    unquoted_text = _without_quoted_text(text)
    has_positive_action = _has_positive_action(text)
    if NEGATED_AUTOMATE_ME_RE.search(unquoted_text) and not has_positive_action:
        return RouteDecision(
            artifact="none",
            category="route-elsewhere",
            operation="none",
            target=None,
            reason="negated automate-me intent is not an artifact request",
        )
    if _has_negated_artifact_intent(text):
        return RouteDecision(
            artifact="none",
            category="route-elsewhere",
            operation="none",
            target=None,
            reason="negated artifact intent is not a request to create or update",
        )

    invalid_name = INVALID_ACTION_NAME_RE.search(unquoted_text)
    if invalid_name is not None:
        raise RoutingError(f"invalid artifact name: {invalid_name.group(1).rstrip('.')}")
    mode_name = _mode_name_from_request(unquoted_text)
    named = NAMED_RE.search(unquoted_text)
    if named is not None and not SKILL_NAME_RE.fullmatch(named.group(1).rstrip(".")):
        raise RoutingError(f"invalid artifact name: {named.group(1).rstrip('.')}")
    automate_signal = _has_automate_me_invocation(unquoted_text)
    personal_signal = (
        automate_signal
        or (
            has_positive_action
            and mode_name is not None
            and _has_any(
                unquoted_text,
                ("my", "personal", "working style", "personal mode"),
            )
        )
        or (
            has_positive_action
            and "working style" in unquoted_text
            and "skill" in unquoted_text
        )
    )
    if personal_signal:
        operation = _operation_for(text)
        target = f"skills/{mode_name}" if mode_name else None
        reason = "personal working conventions belong in an explicit -mode skill"
        return RouteDecision(
            artifact="skill",
            category="personal-mode",
            operation=operation,
            target=target,
            reason=reason,
            confirmation_required=mode_name is None or operation == "confirm",
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
    root = Path(os.path.normpath(os.path.abspath(os.fspath(workspace_root))))
    try:
        root_info = root.lstat()
    except FileNotFoundError as exc:
        raise ScopeError(f"workspace root is not a directory: {root}") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ScopeError(f"workspace root is not a directory: {root}")
    return root


def _reject_symlinked_ancestors(path: Path, *, label: str) -> None:
    current = Path(os.path.normpath(os.path.abspath(os.fspath(path))))
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            current = current.parent
            if current == Path(current.anchor):
                break
            continue
        if stat.S_ISLNK(info.st_mode):
            resolved_name = Path(os.path.realpath(current)).name
            if current.parent != Path(current.anchor) or resolved_name != current.name:
                raise ScopeError(f"{label} has a symlinked ancestor: {current}")
        if current == Path(current.anchor):
            break
        current = current.parent


@dataclass(frozen=True)
class _CanonicalPath:
    """One normalized lexical path plus its root-relative components.

    The lexical form is deliberately not resolved through symlinks. Resolution
    happens only through descriptor-relative ``O_NOFOLLOW`` opens below.
    """

    root: Path
    absolute: Path
    relative_parts: tuple[str, ...]

    @property
    def relative(self) -> str:
        return PurePosixPath(*self.relative_parts).as_posix()


def _canonical_path(
    root: Path, candidate: Path | str, *, allow_root: bool = False
) -> _CanonicalPath:
    raw = os.fspath(candidate)
    if "\x00" in raw:
        raise ScopeError("path contains a NUL byte")
    root_text = os.path.normpath(os.fspath(root))
    candidate_text = (
        raw if os.path.isabs(raw) else os.path.join(root_text, raw)
    )
    absolute_text = os.path.normpath(os.path.abspath(candidate_text))
    try:
        if os.path.commonpath((root_text, absolute_text)) != root_text:
            raise ScopeError(f"path escapes the active workspace: {candidate}")
    except ValueError as exc:
        raise ScopeError(f"path escapes the active workspace: {candidate}") from exc
    relative_text = os.path.relpath(absolute_text, root_text)
    if relative_text == ".":
        if allow_root:
            return _CanonicalPath(root, Path(absolute_text), ())
        raise ScopeError("path must identify an entry below the workspace root")
    relative_parts = tuple(PurePosixPath(relative_text).parts)
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise ScopeError(f"path is not a normalized workspace-relative entry: {candidate}")
    return _CanonicalPath(root, Path(absolute_text), relative_parts)


def _scoped_path(root: Path, candidate: Path | str) -> Path:
    """Return the canonical lexical absolute path without following aliases."""

    return _canonical_path(root, candidate).absolute


def _open_directory(path: Path, *, label: str) -> int:
    try:
        return os.open(
            os.fspath(path), DIRECTORY_FLAGS | NOFOLLOW_FLAGS
        )
    except FileNotFoundError as exc:
        raise ScopeError(f"{label} does not exist: {path}") from exc
    except OSError as exc:
        raise ScopeError(f"{label} is not a safe directory: {path}") from exc


def _open_directory_at(parent_fd: int, name: str, *, label: str) -> int:
    try:
        return os.open(name, DIRECTORY_FLAGS | NOFOLLOW_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise ScopeError(f"{label} does not exist: {name}") from exc
    except OSError as exc:
        raise ScopeError(f"{label} is not a safe directory: {name}") from exc


def _open_directory_chain(
    root_fd: int, parts: Sequence[str], *, create: bool = False
) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            try:
                next_fd = _open_directory_at(current_fd, part, label="path component")
            except ScopeError as exc:
                if not create or "does not exist" not in str(exc):
                    raise
                try:
                    os.mkdir(part, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = _open_directory_at(current_fd, part, label="path component")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


@contextmanager
def _open_relative_file(
    root: Path,
    relative_parts: Sequence[str],
    *,
    flags: int,
    label: str,
    create_parent_dirs: bool = False,
) -> Iterator[tuple[int, int, str]]:
    if not relative_parts:
        raise ScopeError(f"{label} must identify a file below the root")
    root_fd = _open_directory(root, label="root")
    parent_fd: int | None = None
    leaf_fd: int | None = None
    try:
        parent_fd = _open_directory_chain(
            root_fd, relative_parts[:-1], create=create_parent_dirs
        )
        leaf_name = relative_parts[-1]
        try:
            leaf_fd = os.open(
                leaf_name,
                flags | NOFOLLOW_FLAGS,
                0o644,
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise ScopeError(f"{label} does not exist: {PurePosixPath(*relative_parts)}") from exc
        except OSError as exc:
            raise ScopeError(f"{label} is not a safe file: {PurePosixPath(*relative_parts)}") from exc
        yield leaf_fd, parent_fd, leaf_name
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ScopeError(f"unable to inspect output entry: {name}") from exc


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _remove_directory_contents(directory_fd: int) -> None:
    """Best-effort cleanup through descriptors without following aliases."""

    try:
        entries = list(os.scandir(directory_fd))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        try:
            entry_info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(entry_info.st_mode) and not stat.S_ISLNK(entry_info.st_mode):
            try:
                child_fd = _open_directory_at(
                    directory_fd, name, label="owned cleanup directory"
                )
            except ScopeError:
                continue
            try:
                child_info = os.fstat(child_fd)
                if _same_file_identity(entry_info, child_info):
                    _remove_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            current_info = _stat_at(directory_fd, name)
            if current_info is None or not _same_file_identity(entry_info, current_info):
                continue
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except OSError:
                continue
            continue
        current_info = _stat_at(directory_fd, name)
        if current_info is None or not _same_file_identity(entry_info, current_info):
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            continue


def _remove_owned_directory(
    parent_fd: int, name: str, expected_info: os.stat_result
) -> bool:
    """Remove a directory only while its directory entry is still ours."""

    current_info = _stat_at(parent_fd, name)
    if (
        current_info is None
        or stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return False
    try:
        directory_fd = _open_directory_at(parent_fd, name, label="owned cleanup directory")
    except ScopeError:
        return False
    try:
        opened_info = os.fstat(directory_fd)
        if not _same_file_identity(opened_info, expected_info):
            return False
        _remove_directory_contents(directory_fd)
    finally:
        os.close(directory_fd)
    current_info = _stat_at(parent_fd, name)
    if (
        current_info is None
        or stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return current_info is None
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _remove_empty_owned_directory(
    parent_fd: int, name: str, expected_info: os.stat_result
) -> bool:
    """Remove only an unchanged empty directory owned by this operation."""

    current_info = _stat_at(parent_fd, name)
    if (
        current_info is None
        or stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return False
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _cleanup_owned_stage(
    parent_fd: int,
    stage_parent_name: str,
    expected_parent_info: os.stat_result,
    stage_name: str,
    expected_stage_info: os.stat_result | None,
) -> None:
    """Remove only this operation's stage entries and an empty stage parent."""

    current_parent = _stat_at(parent_fd, stage_parent_name)
    if (
        current_parent is None
        or stat.S_ISLNK(current_parent.st_mode)
        or not stat.S_ISDIR(current_parent.st_mode)
        or not _same_file_identity(current_parent, expected_parent_info)
    ):
        return
    stage_parent_fd: int | None = None
    try:
        stage_parent_fd = _open_directory_at(
            parent_fd, stage_parent_name, label="owned stage parent"
        )
        if expected_stage_info is not None:
            _remove_owned_directory(stage_parent_fd, stage_name, expected_stage_info)
    finally:
        if stage_parent_fd is not None:
            os.close(stage_parent_fd)
    _remove_empty_owned_directory(parent_fd, stage_parent_name, expected_parent_info)


def _read_relative_bytes(
    root: Path,
    relative_parts: Sequence[str],
    *,
    max_bytes: int | None,
    label: str,
) -> tuple[bytes, os.stat_result]:
    with _open_relative_file(
        root,
        relative_parts,
        flags=os.O_RDONLY | os.O_CLOEXEC,
        label=label,
    ) as (file_fd, parent_fd, leaf_name):
        before_path = _stat_at(parent_fd, leaf_name)
        if before_path is None or stat.S_ISLNK(before_path.st_mode):
            raise ScopeError(f"{label} is not a stable regular file: {leaf_name}")
        if not stat.S_ISREG(before_path.st_mode):
            raise ScopeError(f"{label} is not a regular file: {leaf_name}")
        before_fd = os.fstat(file_fd)
        if not _same_file_state(before_path, before_fd):
            raise ScopeError(f"{label} changed while it was opened: {leaf_name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            read_size = 64 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes + 1 - total)
                if read_size <= 0:
                    break
            chunk = os.read(file_fd, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ScopeError(f"{label} is too large: {leaf_name}")
        after_fd = os.fstat(file_fd)
        after_path = _stat_at(parent_fd, leaf_name)
        if (
            after_path is None
            or stat.S_ISLNK(after_path.st_mode)
            or not _same_file_state(before_fd, after_fd)
            or not _same_file_state(after_path, after_fd)
        ):
            raise ScopeError(f"{label} changed while it was read: {leaf_name}")
        return b"".join(chunks), after_fd


def _read_relative_text(
    root: Path,
    relative_parts: Sequence[str],
    *,
    max_bytes: int | None,
    label: str,
) -> tuple[str, os.stat_result]:
    data, info = _read_relative_bytes(
        root, relative_parts, max_bytes=max_bytes, label=label
    )
    try:
        return data.decode("utf-8"), info
    except UnicodeDecodeError as exc:
        raise ScopeError(f"{label} is not UTF-8 text: {PurePosixPath(*relative_parts)}") from exc


def _relative_regular_file_exists(
    root: Path, relative_parts: Sequence[str], *, label: str
) -> bool:
    try:
        with _open_relative_file(
            root,
            relative_parts,
            flags=os.O_RDONLY | os.O_CLOEXEC,
            label=label,
        ) as (file_fd, parent_fd, leaf_name):
            path_info = _stat_at(parent_fd, leaf_name)
            fd_info = os.fstat(file_fd)
            if (
                path_info is None
                or stat.S_ISLNK(path_info.st_mode)
                or not stat.S_ISREG(path_info.st_mode)
                or not _same_file_state(path_info, fd_info)
            ):
                raise ScopeError(f"{label} is not a stable regular file: {leaf_name}")
            return True
    except ScopeError as exc:
        if "does not exist" in str(exc):
            return False
        raise


@contextmanager
def _workspace_write_lock(root: Path) -> Iterator[None]:
    lock_fd = _open_directory(root, label="workspace root")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


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
        if not isinstance(record, ArtifactRecord):
            raise ScopeError("history evidence must contain artifact records")
        if not isinstance(record.text, str):
            raise ScopeError(f"history record text is invalid: {record.path}")
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
        if record.sha256 != hashlib.sha256(record.text.encode("utf-8")).hexdigest():
            raise ScopeError(f"history record SHA-256 metadata is invalid: {record.path}")
        if record.line_count != len(record.text.splitlines()):
            raise ScopeError(f"history record line-count metadata is invalid: {record.path}")
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
    canonical_paths: set[str] = set()
    file_identities: set[tuple[int, int]] = set()
    total_bytes = 0
    for artifact_path in artifact_paths:
        canonical = _canonical_path(root, artifact_path)
        canonical_key = canonical.relative.casefold()
        if canonical_key in canonical_paths:
            raise ScopeError(f"duplicate canonical history file: {artifact_path}")
        canonical_paths.add(canonical_key)
        text, info = _read_relative_text(
            root,
            canonical.relative_parts,
            max_bytes=max_bytes,
            label="allowlisted artifact",
        )
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
        slice_id, captured_at = _slice_metadata(canonical.relative, text)
        record = ArtifactRecord(
            path=canonical.relative,
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            line_count=len(text.splitlines()),
            size_bytes=size,
            slice_id=slice_id,
            captured_at=captured_at,
        )
        object.__setattr__(record, "_allowlist_reader_marker", _ALLOWLIST_READER_MARKER)
        records.append(record)
    _validate_record_set(records)
    return tuple(records)


def _validate_generation_evidence(
    workspace_root: Path, records: Sequence[ArtifactRecord]
) -> tuple[ArtifactRecord, ...]:
    """Re-read and match the exact records that authorize generation."""

    if not all(isinstance(record, ArtifactRecord) for record in records):
        raise ScopeError("generation evidence must contain artifact records")
    if any(
        record._allowlist_reader_marker is not _ALLOWLIST_READER_MARKER
        for record in records
    ):
        raise ScopeError("generation evidence must come from the allowlisted artifact reader")
    try:
        current = read_allowlisted_artifacts(
            workspace_root, [record.path for record in records]
        )
    except (ScopeError, TypeError, ValueError) as exc:
        raise ScopeError(f"allowlisted generation evidence is no longer valid: {exc}") from exc
    if len(current) != len(records):
        raise ScopeError("allowlisted generation evidence changed record count")
    for expected, actual in zip(records, current):
        if expected.path != actual.path:
            raise ScopeError(
                f"generation evidence path changed: {expected.path} -> {actual.path}"
            )
        if expected.sha256 != actual.sha256:
            raise ScopeError(f"generation evidence SHA-256 changed: {expected.path}")
        if expected.line_count != actual.line_count:
            raise ScopeError(f"generation evidence line count changed: {expected.path}")
        if expected.slice_id != actual.slice_id:
            raise ScopeError(f"generation evidence slice identity changed: {expected.path}")
        if expected.captured_at != actual.captured_at:
            raise ScopeError(f"generation evidence capture metadata changed: {expected.path}")
        if expected.size_bytes != actual.size_bytes or expected.text != actual.text:
            raise ScopeError(f"generation evidence content changed: {expected.path}")
    return current


def discover_existing_skills(
    workspace_root: Path | str, *, name: str | None = None
) -> tuple[ExistingSkill, ...]:
    """Discover only direct ``skills/<name>/SKILL.md`` files in the workspace."""

    root = _resolved_root(workspace_root)
    if name is not None:
        _validate_skill_name(name)
    skills_path = _canonical_path(root, "skills")
    found: list[ExistingSkill] = []
    canonical_files: set[str] = set()
    file_identities: set[tuple[int, int]] = set()
    try:
        root_fd = _open_directory(root, label="workspace root")
        try:
            skills_fd = _open_directory_chain(root_fd, skills_path.relative_parts)
        finally:
            os.close(root_fd)
    except ScopeError as exc:
        if "does not exist" in str(exc):
            return ()
        raise
    try:
        with os.scandir(skills_fd) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                child_info = entry.stat(follow_symlinks=False)
                child = PurePosixPath("skills", entry.name)
                if stat.S_ISLNK(child_info.st_mode):
                    raise ScopeError(f"symlink skill alias is not allowed: {child}")
                if entry.name == RECOVERY_DIRECTORY_NAME or TRANSACTION_ARTIFACT_RE.fullmatch(
                    entry.name
                ):
                    continue
                if not stat.S_ISDIR(child_info.st_mode):
                    continue
                try:
                    _validate_skill_name(entry.name)
                except ValidationError as exc:
                    raise ScopeError(f"invalid direct skill directory: {child}") from exc
                if name is not None and entry.name != name:
                    continue
                child_fd = _open_directory_at(skills_fd, entry.name, label="skill directory")
                try:
                    skill_info = _stat_at(child_fd, "SKILL.md")
                    if skill_info is None:
                        continue
                    if stat.S_ISLNK(skill_info.st_mode):
                        raise ScopeError(
                            f"symlink or dangling skill discovery alias is not allowed: {child / 'SKILL.md'}"
                        )
                    if not stat.S_ISREG(skill_info.st_mode):
                        raise ScopeError(f"skill file is not a regular file: {child / 'SKILL.md'}")
                    canonical_name = (child / "SKILL.md").as_posix().casefold()
                    if canonical_name in canonical_files:
                        raise ScopeError(f"duplicate canonical skill file: {child / 'SKILL.md'}")
                    canonical_files.add(canonical_name)
                    identity = (skill_info.st_dev, skill_info.st_ino)
                    if identity in file_identities:
                        raise ScopeError(f"duplicate skill file alias: {child / 'SKILL.md'}")
                    file_identities.add(identity)
                    found.append(
                        ExistingSkill(
                            name=entry.name,
                            directory=child.parent.as_posix() + "/" + entry.name,
                            skill_file=(child / "SKILL.md").as_posix(),
                        )
                    )
                finally:
                    os.close(child_fd)
    finally:
        os.close(skills_fd)
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
        fence_character: str | None = None
        fence_length = 0
        for line in text.splitlines():
            fence = MARKDOWN_FENCE_RE.match(line)
            if fence_character is not None:
                if (
                    fence is not None
                    and fence.group(1)[0] == fence_character
                    and len(fence.group(1)) >= fence_length
                ):
                    fence_character = None
                    fence_length = 0
                continue
            if fence is not None:
                fence_character = fence.group(1)[0]
                fence_length = len(fence.group(1))
                continue
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


def _parse_block_scalar(
    lines: Sequence[str], start: int, header: str
) -> tuple[str, int]:
    match = re.fullmatch(
        r"(?P<style>[>|])(?P<indicators>(?:[+-]?[1-9]?|[1-9][+-]?))(?:[ \t]+#.*)?",
        header,
    )
    if match is None:
        raise ValidationError(f"invalid YAML block scalar header: {header!r}")
    indicators = match.group("indicators")
    chomping = next((character for character in indicators if character in "+-"), "")
    indent_value = next(
        (int(character) for character in indicators if character.isdigit()), None
    )

    raw_content: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line == "":
            raw_content.append("")
            index += 1
            continue
        if not line.startswith(" "):
            break
        raw_content.append(line)
        index += 1

    if indent_value is None and raw_content:
        nonempty_indents = [
            len(line) - len(line.lstrip(" "))
            for line in raw_content
            if line.strip()
        ]
        if nonempty_indents:
            indent_value = nonempty_indents[0]
    if indent_value is None:
        indent_value = 1

    content: list[tuple[str, bool]] = []
    for line in raw_content:
        if not line or not line.strip():
            content.append(("", False))
            continue
        leading_spaces = len(line) - len(line.lstrip(" "))
        if leading_spaces < indent_value:
            raise ValidationError("YAML block scalar content is under-indented")
        content.append((line[indent_value:], leading_spaces > indent_value))

    if match.group("style") == "|":
        value = "\n".join(line for line, _ in content)
    else:
        folded: list[str] = []
        for position, (line, more_indented) in enumerate(content):
            folded.append(line)
            if position == len(content) - 1:
                continue
            next_line, next_more_indented = content[position + 1]
            if (
                line
                and next_line
                and not more_indented
                and not next_more_indented
            ):
                folded.append(" ")
            else:
                folded.append("\n")
        value = "".join(folded)

    if content:
        value += "\n"
    if chomping == "-":
        value = value.rstrip("\n")
    elif chomping != "+":
        value = value.rstrip("\n")
        if value:
            value += "\n"
    return value, index


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        raise ValidationError("SKILL.md must start with frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValidationError("SKILL.md frontmatter is not closed")

    fields: dict[str, str] = {}
    lines = text[4:end].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line[0].isspace():
            raise ValidationError(f"unsupported frontmatter line: {line!r}")
        match = re.fullmatch(r"([a-z][a-z0-9_-]*):[ \t]*(.*)", line)
        if not match:
            raise ValidationError(f"unsupported frontmatter line: {line!r}")
        key, value = match.groups()
        if key in fields:
            raise ValidationError(f"duplicate frontmatter key: {key}")
        value = value.strip()
        if value.startswith((">", "|")):
            value, index = _parse_block_scalar(lines, index + 1, value)
            fields[key] = value
            continue
        if value.startswith('"'):
            if len(value) < 2 or not value.endswith('"'):
                raise ValidationError(f"invalid quoted frontmatter value for {key}")
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"invalid quoted frontmatter value for {key}") from exc
            if not isinstance(parsed, str):
                raise ValidationError(f"frontmatter value must be a string: {key}")
            value = parsed
        elif value.startswith("'"):
            if len(value) < 2 or not value.endswith("'"):
                raise ValidationError(f"invalid quoted frontmatter value for {key}")
            value = value[1:-1].replace("''", "'")
        else:
            plain_value = value.split(" #", 1)[0].rstrip()
            if FRONTMATTER_NON_STRING_RE.fullmatch(plain_value):
                raise ValidationError(f"frontmatter value must be a string: {key}")
        fields[key] = value
        index += 1
    return fields


def _blank_markdown_line(line: str) -> str:
    return "".join(
        "\n" if character == "\n" else "\r" if character == "\r" else " "
        for character in line
    )


def _markdown_without_fenced_code(text: str) -> str:
    """Mask fenced code while retaining line boundaries for Markdown scanning."""

    masked: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        fence = MARKDOWN_FENCE_RE.match(line)
        if fence_character is not None:
            if (
                fence is not None
                and fence.group(1)[0] == fence_character
                and len(fence.group(1)) >= fence_length
                and not line[fence.end() :].strip()
            ):
                fence_character = None
                fence_length = 0
            masked.append(_blank_markdown_line(line))
            continue
        if line.startswith("    ") or line.startswith("\t"):
            masked.append(_blank_markdown_line(line))
            continue
        if fence is not None:
            fence_character = fence.group(1)[0]
            fence_length = len(fence.group(1))
            masked.append(_blank_markdown_line(line))
            continue
        masked.append(line)
    return "".join(masked)


def _markdown_link_targets(text: str) -> Iterator[str]:
    text = _markdown_without_fenced_code(text)
    for match in MARKDOWN_LINK_RE.finditer(text):
        target = match.group(1).strip()
        if target.startswith("<") and ">" in target:
            target = target[1 : target.find(">")]
        else:
            parts = target.split(None, 1)
            target = parts[0] if parts else ""
        yield target
    for match in MARKDOWN_REFERENCE_LINK_RE.finditer(text):
        yield (match.group(1) or match.group(2)).strip()


def _relative_link_target(
    skill_dir: Path, raw_target: str, source_dir: Path | None = None
) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if target.startswith("#"):
        return None
    if not target:
        raise ValidationError("reference link target is empty")
    try:
        parsed = urlparse(target)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"invalid reference link: {raw_target}") from exc
    if target.startswith("//") or parsed.scheme or parsed.netloc:
        raise ValidationError(f"reference links must be relative: {raw_target}")
    target = target.split("#", 1)[0].split("?", 1)[0]
    if not target:
        return None
    if Path(target).is_absolute():
        raise ValidationError(f"absolute reference link is not allowed: {raw_target}")
    base = source_dir or skill_dir
    candidate_text = os.path.normpath(os.path.join(os.fspath(base), unquote(target)))
    try:
        if os.path.commonpath((os.fspath(skill_dir), candidate_text)) != os.fspath(skill_dir):
            raise ValidationError(f"reference link escapes skill directory: {raw_target}")
    except ValueError as exc:
        raise ValidationError(f"reference link escapes skill directory: {raw_target}") from exc
    candidate = Path(candidate_text)
    if candidate == skill_dir:
        raise ValidationError(f"reference link escapes skill directory: {raw_target}")
    return candidate


def _scan_tree_entries(
    root: Path,
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[str]]:
    """List a tree through directory descriptors without following aliases."""

    root_fd = _open_directory(root, label="skill directory")
    directories: list[tuple[str, ...]] = [()]
    files: list[tuple[str, ...]] = []
    errors: list[str] = []
    pending: list[tuple[int, tuple[str, ...]]] = [(root_fd, ())]
    try:
        while pending:
            directory_fd, directory_parts = pending.pop()
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        relative = directory_parts + (entry.name,)
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            errors.append(f"unable to inspect tree entry {entry.name}: {exc}")
                            continue
                        display = PurePosixPath(*relative).as_posix()
                        if stat.S_ISLNK(info.st_mode):
                            errors.append(f"symlink output is not allowed: {display}")
                        elif stat.S_ISDIR(info.st_mode):
                            try:
                                child_fd = _open_directory_at(
                                    directory_fd, entry.name, label="skill directory"
                                )
                            except ScopeError as exc:
                                errors.append(str(exc))
                                continue
                            directories.append(relative)
                            pending.append((child_fd, relative))
                        elif stat.S_ISREG(info.st_mode):
                            files.append(relative)
                        else:
                            errors.append(f"unsupported skill output entry: {display}")
            except OSError as exc:
                errors.append(f"unable to inspect skill tree: {exc}")
            finally:
                os.close(directory_fd)
    except BaseException:
        for directory_fd, _ in pending:
            os.close(directory_fd)
        raise
    return directories, files, errors


def _capture_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture bounded structure, metadata, and content for an existing tree."""

    source_info = root.lstat()
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise ScopeError(f"skill output must be a real directory: {root}")
    directories, files, errors = _scan_tree_entries(root)
    if errors:
        raise ScopeError("; ".join(errors))

    entries: list[tuple[object, ...]] = [
        (relative, "directory", ()) for relative in directories if relative
    ]
    total_bytes = 0
    source_root = _resolved_root(root)
    for relative in files:
        data, info = _read_relative_bytes(
            source_root,
            relative,
            max_bytes=MAX_ARTIFACT_BYTES,
            label="existing skill output",
        )
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ScopeError("aggregate preserved skill size exceeds the bounded limit")
        entries.append(
            (
                relative,
                "file",
                _file_state(info),
                hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(sorted(entries, key=lambda entry: (entry[0], entry[1])))


def validate_skill_tree(
    skill_dir: Path | str, *, require_provenance: bool = True
) -> tuple[str, ...]:
    """Validate every bundled Markdown file and its local links safely."""

    raw_root = Path(os.path.normpath(os.path.abspath(os.fspath(skill_dir))))
    try:
        _reject_symlinked_ancestors(raw_root, label="skill validation path")
    except ScopeError as exc:
        return (str(exc),)
    if raw_root.is_symlink():
        return ("skill directory must not be a symlink",)
    root = _resolved_root(raw_root)
    errors: list[str] = []
    try:
        _validate_skill_name(root.name)
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        _, files, scan_errors = _scan_tree_entries(root)
        errors.extend(scan_errors)
    except ScopeError as exc:
        errors.append(str(exc))
        return tuple(errors)

    markdown_files = [relative for relative in files if relative[-1].casefold().endswith(".md")]
    markdown_text: dict[tuple[str, ...], str] = {}
    for relative in markdown_files:
        try:
            markdown_text[relative], _ = _read_relative_text(
                root,
                relative,
                max_bytes=MAX_ARTIFACT_BYTES,
                label="bundled Markdown file",
            )
        except ScopeError as exc:
            errors.append(str(exc))

    skill_relative = ("SKILL.md",)
    text = markdown_text.get(skill_relative)
    if text is None:
        errors.append("SKILL.md not found or is not a safe regular file")
        return tuple(errors)

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

    linked_text: dict[tuple[str, ...], str] = {}
    for source_relative, source_text in markdown_text.items():
        source_dir = root / PurePosixPath(*source_relative).parent
        for raw_target in _markdown_link_targets(source_text):
            try:
                target = _relative_link_target(root, raw_target, source_dir)
            except ValidationError as exc:
                errors.append(str(exc))
                continue
            if target is None:
                continue
            try:
                target_path = _canonical_path(root, target)
                target_text, _ = _read_relative_text(
                    root,
                    target_path.relative_parts,
                    max_bytes=MAX_ARTIFACT_BYTES,
                    label="reference link target",
                )
            except (ScopeError, ValidationError) as exc:
                errors.append(f"invalid reference link {raw_target}: {exc}")
                continue
            linked_text[target_path.relative_parts] = target_text

    if require_provenance:
        provenance_text = "\n".join(markdown_text.values())
        if UPSTREAM_COMMIT not in provenance_text:
            errors.append("upstream source commit is missing from the skill")
        if UPSTREAM_COPYRIGHT not in provenance_text:
            errors.append("upstream copyright notice is missing from the skill")
        if "MIT License" not in provenance_text:
            errors.append("upstream license notice is missing from the skill")
    return tuple(errors)


def validate_skill_discoverability(skill_dir: Path | str) -> tuple[str, ...]:
    errors = list(validate_skill_tree(skill_dir))
    raw_root = Path(os.path.normpath(os.path.abspath(os.fspath(skill_dir))))
    try:
        root = _resolved_root(raw_root)
    except ScopeError as exc:
        errors.append(str(exc))
        return tuple(errors)
    if raw_root.parent.name != "skills":
        errors.append("skill output must be a direct skills/<name>/ directory")
    try:
        parent_info = raw_root.parent.lstat()
    except FileNotFoundError:
        parent_info = None
    if parent_info is not None and stat.S_ISLNK(parent_info.st_mode):
        errors.append("skill discovery parent must not be a symlink alias")
    try:
        _validate_skill_name(root.name)
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        _read_relative_bytes(
            root,
            ("agents", "openai.yaml"),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="skill discovery interface",
        )
    except ScopeError as exc:
        errors.append(str(exc))
    return tuple(errors)


def is_skill_discoverable(skill_dir: Path | str) -> bool:
    return not validate_skill_discoverability(skill_dir)


def _is_skill_creator_scaffold(skill_dir: Path) -> bool:
    """Recognize only the unedited initializer placeholder for update staging."""

    try:
        text, _ = _read_relative_text(
            skill_dir,
            ("SKILL.md",),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="skill-creator scaffold",
        )
    except ScopeError:
        return False
    return re.search(r"(?m)^description:\s*\[TODO:.*\]\s*$", text) is not None


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

## Working rules

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


def _write_relative_bytes(root: Path, relative_parts: Sequence[str], data: bytes) -> None:
    canonical_root = _resolved_root(root)
    canonical = _canonical_path(canonical_root, PurePosixPath(*relative_parts))
    parts = canonical.relative_parts
    root_fd = _open_directory(canonical_root, label="write root")
    parent_fd: int | None = None
    temporary_name = f".{parts[-1]}.{uuid.uuid4().hex}.tmp"
    temporary_fd: int | None = None
    try:
        parent_fd = _open_directory_chain(
            root_fd, parts[:-1], create=True
        )
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | NOFOLLOW_FLAGS,
            0o644,
            dir_fd=parent_fd,
        )
        offset = 0
        while offset < len(data):
            offset += os.write(temporary_fd, data[offset:])
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        os.rename(
            temporary_name,
            parts[-1],
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        info = _stat_at(parent_fd, parts[-1])
        if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ScopeError(f"written output is not a regular file: {parts[-1]}")
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
        os.close(root_fd)


def _write_relative_text(root: Path, relative: str, content: str) -> None:
    canonical = _canonical_path(_resolved_root(root), relative)
    _write_relative_bytes(
        root,
        canonical.relative_parts,
        content.encode("utf-8"),
    )


def _ensure_relative_directory(root: Path, relative_parts: Sequence[str]) -> None:
    canonical_root = _resolved_root(root)
    canonical = _canonical_path(canonical_root, PurePosixPath(*relative_parts))
    root_fd = _open_directory(canonical_root, label="directory root")
    try:
        directory_fd = _open_directory_chain(
            root_fd, canonical.relative_parts, create=True
        )
        os.close(directory_fd)
    finally:
        os.close(root_fd)


def _copy_tree_without_symlinks(
    source: Path, destination: Path
) -> tuple[tuple[object, ...], ...]:
    source_info = source.lstat()
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise ScopeError(f"skill output must be a real directory: {source}")
    try:
        destination_info = destination.lstat()
    except FileNotFoundError:
        destination_info = None
    if destination_info is not None and (
        stat.S_ISLNK(destination_info.st_mode)
        or not stat.S_ISDIR(destination_info.st_mode)
    ):
        raise ScopeError(f"skill output must be a real directory: {destination}")
    snapshot = _capture_tree_snapshot(source)
    destination.mkdir(parents=True, exist_ok=True)
    source_root = _resolved_root(source)
    destination_root = _resolved_root(destination)
    directories, files, errors = _scan_tree_entries(source_root)
    if errors:
        raise ScopeError("; ".join(errors))
    for relative in directories:
        if not relative:
            continue
        _ensure_relative_directory(destination_root, relative)
    total_bytes = 0
    for relative in files:
        data, _ = _read_relative_bytes(
            source_root,
            relative,
            max_bytes=MAX_ARTIFACT_BYTES,
            label="existing skill output",
        )
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ScopeError("aggregate preserved skill size exceeds the bounded limit")
        _write_relative_bytes(destination_root, relative, data)
    if _capture_tree_snapshot(source) != snapshot:
        raise ScopeError(f"existing skill output changed while it was copied: {source}")
    return snapshot


def _write_relative_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        _write_relative_text(root, relative, content)


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


def _merge_working_style_reference(
    existing: str, generated: str, rules: Sequence[str]
) -> str:
    """Keep approved reference prose while adding the current managed rules."""

    updated = _merge_working_rules(existing, rules)
    additions = [f"- {rule.strip()}" for rule in rules]
    existing_lines = {line.strip().casefold() for line in updated.splitlines() if line.strip()}
    missing = [line for line in additions if line.casefold() not in existing_lines]
    if missing:
        evidence_heading = re.search(r"(?m)^## Evidence pointers[ \t]*\n", updated)
        insertion = "\n## Working rules\n\n" + "\n".join(missing) + "\n"
        if evidence_heading:
            updated = updated[: evidence_heading.start()] + insertion + updated[evidence_heading.start() :]
        else:
            updated = updated.rstrip() + insertion

    generated_evidence = re.search(
        r"(?ms)^## Evidence pointers[ \t]*\n(?P<section>.*?)(?=^##\s|\Z)",
        generated,
    )
    if generated_evidence is None:
        return updated
    existing_evidence = re.search(
        r"(?ms)^## Evidence pointers[ \t]*\n(?P<section>.*?)(?=^##\s|\Z)",
        updated,
    )
    if existing_evidence is None:
        return updated.rstrip() + "\n\n" + generated_evidence.group(0).rstrip() + "\n"

    pointer_lines = [
        line
        for line in generated_evidence.group("section").splitlines()
        if line.strip()
    ]
    existing_section = existing_evidence.group("section")
    for pointer_line in pointer_lines:
        path_match = re.match(r"\s*-\s+`([^`]+)`\s+\([^)]+\)\s*$", pointer_line)
        if path_match is None:
            continue
        path = re.escape(path_match.group(1))
        existing_section = re.sub(
            rf"(?m)^\s*-\s+`{path}`\s+\([^)]+\)\s*$\n?",
            "",
            existing_section,
        )
    updated_section = existing_section.rstrip()
    if updated_section:
        updated_section += "\n"
    updated_section += "\n".join(pointer_lines) + "\n"
    return (
        updated[: existing_evidence.start("section")]
        + updated_section
        + updated[existing_evidence.end("section") :]
    )


def _merge_existing_skill(
    skill_text: str,
    generated_text: str,
    draft: SkillDraft,
    *,
    allow_skill_creator_scaffold: bool = False,
) -> str:
    try:
        _parse_frontmatter(skill_text)
    except ValidationError as exc:
        if not (
            allow_skill_creator_scaffold
            and str(exc) == "frontmatter value must be a string: description"
        ):
            raise
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
    existing_skill, _ = _read_relative_text(
        staged_root,
        ("SKILL.md",),
        max_bytes=MAX_ARTIFACT_BYTES,
        label="existing SKILL.md",
    )
    generated_files = dict(generated_files)
    generated_files["SKILL.md"] = _merge_existing_skill(
        existing_skill,
        generated_files["SKILL.md"],
        draft,
        allow_skill_creator_scaffold=_is_skill_creator_scaffold(staged_root),
    )
    try:
        provenance, _ = _read_relative_text(
            staged_root,
            ("references", "provenance.md"),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="existing provenance reference",
        )
    except ScopeError as exc:
        if "does not exist" not in str(exc):
            raise
    else:
        if _has_complete_provenance(provenance):
            generated_files.pop("references/provenance.md", None)
        else:
            generated_files["references/provenance.md"] = (
                provenance.rstrip() + "\n\n" + generated_files["references/provenance.md"]
            )
    try:
        existing_working_style, _ = _read_relative_text(
            staged_root,
            ("references", "working-style.md"),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="existing working-style reference",
        )
    except ScopeError as exc:
        if "does not exist" not in str(exc):
            raise
    else:
        generated_files["references/working-style.md"] = _merge_working_style_reference(
            existing_working_style,
            generated_files["references/working-style.md"],
            draft.rules,
        )
    if _relative_regular_file_exists(
        staged_root,
        ("agents", "openai.yaml"),
        label="existing generated reference",
    ):
        generated_files.pop("agents/openai.yaml", None)
    _write_relative_files(staged_root, generated_files)


def _new_stage(target: Path) -> tuple[Path, Path]:
    stage_parent = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=str(target.parent))
    )
    stage_root = stage_parent / target.name
    stage_root.mkdir()
    return stage_parent, stage_root


def _prepare_recovery_copy(
    target: Path,
    target_parent_fd: int,
    expected_snapshot: tuple[tuple[object, ...], ...],
) -> tuple[int, os.stat_result, bool, os.stat_result, Path]:
    """Keep a deterministic copy available before an update can be displaced."""

    recovery_parent_fd: int | None = None
    recovery_parent_info: os.stat_result | None = None
    recovery_parent_created = False
    recovery_info: os.stat_result | None = None
    try:
        try:
            os.mkdir(RECOVERY_DIRECTORY_NAME, 0o755, dir_fd=target_parent_fd)
            recovery_parent_created = True
        except FileExistsError:
            pass
        recovery_parent_fd = _open_directory_at(
            target_parent_fd,
            RECOVERY_DIRECTORY_NAME,
            label="recovery directory",
        )
        recovery_parent_info = os.fstat(recovery_parent_fd)
        if _stat_at(recovery_parent_fd, target.name) is not None:
            raise CollisionError(
                "update has an unresolved prior recovery: "
                f"{target.parent / RECOVERY_DIRECTORY_NAME / target.name}"
            )
        os.mkdir(target.name, 0o755, dir_fd=recovery_parent_fd)
        recovery_info = _stat_at(recovery_parent_fd, target.name)
        if (
            recovery_info is None
            or stat.S_ISLNK(recovery_info.st_mode)
            or not stat.S_ISDIR(recovery_info.st_mode)
        ):
            raise ScopeError("recovery output is not a real directory")
        recovery_path = target.parent / RECOVERY_DIRECTORY_NAME / target.name
        copied_snapshot = _copy_tree_without_symlinks(target, recovery_path)
        if copied_snapshot != expected_snapshot:
            raise ScopeError(
                f"update target changed while its recovery copy was prepared: {target}"
            )
        return (
            recovery_parent_fd,
            recovery_parent_info,
            recovery_parent_created,
            recovery_info,
            recovery_path,
        )
    except BaseException:
        if recovery_parent_fd is not None:
            if recovery_info is not None:
                _remove_owned_directory(
                    recovery_parent_fd, target.name, recovery_info
                )
            os.close(recovery_parent_fd)
        if recovery_parent_created and recovery_parent_info is not None:
            _remove_empty_owned_directory(
                target_parent_fd,
                RECOVERY_DIRECTORY_NAME,
                recovery_parent_info,
            )
        raise


def _backup_matches_expected(
    target_parent_fd: int,
    backup_name: str,
    backup_path: Path,
    expected_info: os.stat_result,
    expected_snapshot: tuple[tuple[object, ...], ...],
) -> bool:
    """Check the hidden backup again before any rollback restore or cleanup."""

    current_info = _stat_at(target_parent_fd, backup_name)
    if (
        current_info is None
        or stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return False
    try:
        return _capture_tree_snapshot(backup_path) == expected_snapshot
    except ScopeError:
        return False


def _install_staged_tree(
    stage_root: Path,
    target: Path,
    operation: str,
    failure_injector: Callable[[str], None] | None,
    target_parent_fd: int,
    expected_target_info: os.stat_result | None,
    expected_target_snapshot: tuple[tuple[object, ...], ...] | None,
    expected_stage_info: os.stat_result,
    expected_stage_snapshot: tuple[tuple[object, ...], ...],
    validate_installed: Callable[[], None],
) -> None:
    if failure_injector:
        failure_injector("before_install")
    stage_parent_fd = _open_directory(stage_root.parent, label="stage parent")
    stage_fd: int | None = None
    try:
        stage_fd = _open_directory_at(
            stage_parent_fd, stage_root.name, label="staged skill output"
        )
        stage_info = os.fstat(stage_fd)
        if not _same_file_identity(stage_info, expected_stage_info):
            raise ScopeError(f"staged skill output was replaced before install: {stage_root}")
        if _capture_tree_snapshot(stage_root) != expected_stage_snapshot:
            raise ScopeError(f"staged skill output changed before install: {stage_root}")
    except BaseException:
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(stage_parent_fd)
        raise
    assert stage_fd is not None
    if operation == "create":
        target_fd: int | None = None
        owned_target_info: os.stat_result | None = None
        try:
            try:
                os.mkdir(target.name, 0o755, dir_fd=target_parent_fd)
            except FileExistsError as exc:
                raise CollisionError(f"create refused existing output: {target}") from exc
            target_fd = _open_directory_at(
                target_parent_fd, target.name, label="new skill output"
            )
            owned_target_info = os.fstat(target_fd)
            with os.scandir(stage_fd) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise ScopeError(f"symlink output is not allowed: {entry.name}")
                    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                        raise ScopeError(f"unsupported staged output entry: {entry.name}")
                    os.rename(
                        entry.name,
                        entry.name,
                        src_dir_fd=stage_fd,
                        dst_dir_fd=target_fd,
                    )
            validate_installed()
            if failure_injector:
                failure_injector("after_install")
        except BaseException:
            if owned_target_info is not None:
                _remove_owned_directory(
                    target_parent_fd, target.name, owned_target_info
                )
            raise
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(stage_fd)
            os.close(stage_parent_fd)
        return

    if expected_target_info is None or expected_target_snapshot is None:
        raise ScopeError("update publication is missing the expected target snapshot")
    backup_name = f".{target.name}.backup-{uuid.uuid4().hex}"
    backup_info: os.stat_result | None = None
    installed_target_info: os.stat_result | None = None
    recovery_parent_fd: int | None = None
    recovery_parent_info: os.stat_result | None = None
    recovery_parent_created = False
    recovery_info: os.stat_result | None = None
    recovery_path: Path | None = None
    preserve_recovery = False
    try:
        current_info = _stat_at(target_parent_fd, target.name)
        if (
            current_info is None
            or stat.S_ISLNK(current_info.st_mode)
            or not stat.S_ISDIR(current_info.st_mode)
            or not _same_file_state(current_info, expected_target_info)
        ):
            raise ScopeError(f"update target changed before publication: {target}")
        if _capture_tree_snapshot(target) != expected_target_snapshot:
            raise ScopeError(f"update target content changed after preflight: {target}")
        (
            recovery_parent_fd,
            recovery_parent_info,
            recovery_parent_created,
            recovery_info,
            recovery_path,
        ) = _prepare_recovery_copy(
            target,
            target_parent_fd,
            expected_target_snapshot,
        )
        if _capture_tree_snapshot(target) != expected_target_snapshot:
            raise ScopeError(f"update target content changed before publication: {target}")
        os.rename(
            target.name,
            backup_name,
            src_dir_fd=target_parent_fd,
            dst_dir_fd=target_parent_fd,
        )
        backup_info = _stat_at(target_parent_fd, backup_name)
        if (
            backup_info is None
            or stat.S_ISLNK(backup_info.st_mode)
            or not stat.S_ISDIR(backup_info.st_mode)
            or not _same_file_identity(backup_info, expected_target_info)
        ):
            raise ScopeError(f"update target changed while it was moved: {target}")
        if (
            _capture_tree_snapshot(target.with_name(backup_name))
            != expected_target_snapshot
        ):
            raise ScopeError(f"update target content changed while it was moved: {target}")
        if failure_injector:
            failure_injector("after_backup")
        if _stat_at(target_parent_fd, target.name) is not None:
            raise CollisionError(f"update target appeared during publication: {target}")
        staged_info = _stat_at(stage_parent_fd, stage_root.name)
        if (
            staged_info is None
            or stat.S_ISLNK(staged_info.st_mode)
            or not stat.S_ISDIR(staged_info.st_mode)
            or not _same_file_identity(staged_info, stage_info)
        ):
            raise ScopeError(f"staged skill output is not a directory: {stage_root}")
        if _capture_tree_snapshot(stage_root) != expected_stage_snapshot:
            raise ScopeError(f"staged skill output changed before install: {stage_root}")
        os.rename(
            stage_root.name,
            target.name,
            src_dir_fd=stage_parent_fd,
            dst_dir_fd=target_parent_fd,
        )
        installed_info = _stat_at(target_parent_fd, target.name)
        if (
            installed_info is None
            or stat.S_ISLNK(installed_info.st_mode)
            or not stat.S_ISDIR(installed_info.st_mode)
            or not _same_file_identity(installed_info, stage_info)
        ):
            raise ScopeError(f"installed target changed during publication: {target}")
        installed_target_info = installed_info
        validate_installed()
        if failure_injector:
            failure_injector("after_install")
    except BaseException as exc:
        current_info = _stat_at(target_parent_fd, target.name)
        if (
            installed_target_info is not None
            and current_info is not None
            and _same_file_identity(current_info, installed_target_info)
        ):
            _remove_owned_directory(
                target_parent_fd, target.name, installed_target_info
            )
            current_info = _stat_at(target_parent_fd, target.name)
        if backup_info is not None and recovery_info is not None and recovery_path is not None:
            backup_valid = _backup_matches_expected(
                target_parent_fd,
                backup_name,
                target.with_name(backup_name),
                backup_info,
                expected_target_snapshot,
            )
            restored = False
            if current_info is None and backup_valid:
                try:
                    os.rename(
                        backup_name,
                        target.name,
                        src_dir_fd=target_parent_fd,
                        dst_dir_fd=target_parent_fd,
                    )
                except FileExistsError:
                    current_info = _stat_at(target_parent_fd, target.name)
                else:
                    backup_info = None
                    restored = True
            if restored:
                raise PublicationRecoveryError(
                    target=target,
                    recovery_path=recovery_path,
                    cause=exc,
                    recovery_status="approved_tree_restored",
                    cleanup_status="hidden_backup_removed",
                    competitor_preserved=False,
                ) from exc

            preserve_recovery = True
            if current_info is not None:
                if backup_valid and _remove_owned_directory(
                    target_parent_fd, backup_name, backup_info
                ):
                    backup_info = None
                    cleanup_status = "hidden_backup_removed"
                    recovery_status = "approved_tree_recovered"
                else:
                    cleanup_status = "hidden_backup_preserved"
                    recovery_status = "backup_rejected_recovery_preserved"
                raise PublicationRecoveryError(
                    target=target,
                    recovery_path=recovery_path,
                    cause=exc,
                    recovery_status=recovery_status,
                    cleanup_status=cleanup_status,
                    competitor_preserved=True,
                ) from exc

            raise PublicationRecoveryError(
                target=target,
                recovery_path=recovery_path,
                cause=exc,
                recovery_status="backup_rejected_recovery_preserved",
                cleanup_status="hidden_backup_preserved",
                competitor_preserved=False,
            ) from exc
        raise
    else:
        if backup_info is not None:
            if _remove_owned_directory(target_parent_fd, backup_name, backup_info):
                backup_info = None
    finally:
        if recovery_parent_fd is not None:
            if not preserve_recovery and recovery_info is not None:
                _remove_owned_directory(
                    recovery_parent_fd, target.name, recovery_info
                )
            os.close(recovery_parent_fd)
        if (
            not preserve_recovery
            and recovery_parent_created
            and recovery_parent_info is not None
        ):
            _remove_empty_owned_directory(
                target_parent_fd,
                RECOVERY_DIRECTORY_NAME,
                recovery_parent_info,
            )
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(stage_parent_fd)


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
    canonical_target = _canonical_path(workspace, skill_dir)
    target = canonical_target.absolute
    if canonical_target.relative_parts[:1] != ("skills",) or len(
        canonical_target.relative_parts
    ) != 2:
        raise ValidationError("generated skills must be direct skills/<name>/ outputs")
    if target.name != draft.name:
        raise ValidationError(
            f"skill directory basename {target.name!r} does not match draft name {draft.name!r}"
        )
    if approval is None:
        raise ValidationError("durable generation requires structured preference approval")
    if not approval.approved:
        if approval.response is not None and approval.response.status == "declined":
            raise ValidationError("preference interaction was declined")
        raise ValidationError("durable generation requires explicit approval")
    if approval.interaction is None or approval.response is None:
        raise ValidationError("durable generation requires a structured preference interaction")
    response_errors = approval.interaction.validate_response(approval.response)
    if response_errors:
        raise ValidationError("; ".join(response_errors))
    if approval.response.status != "accepted":
        raise ValidationError("durable generation requires an accepted preference response")
    if approval.intent != operation:
        raise ValidationError("approval intent does not match the write operation")
    if approval.skill_name != draft.name:
        raise ValidationError("approval skill_name does not match the draft")
    if len(draft.evidence) < 2:
        raise ValidationError("durable generation requires at least two history slices")

    with _workspace_write_lock(workspace):
        try:
            live_evidence = _validate_generation_evidence(workspace, draft.evidence)
            draft = replace(draft, evidence=live_evidence)
            confirmed = {
                signal.casefold()
                for signal in confirmed_signals(draft.evidence, approval.signals)
            }
        except ScopeError as exc:
            raise ValidationError(f"invalid generation evidence: {exc}") from exc
        if {signal.casefold() for signal in approval.signals} - confirmed:
            raise ValidationError("approval signals lack positive structured evidence")
        files = render_skill_files(draft)
        root_fd = _open_directory(workspace, label="workspace root")
        parent_fd: int | None = None
        stage_parent: Path | None = None
        stage_root: Path | None = None
        stage_parent_info: os.stat_result | None = None
        expected_stage_info: os.stat_result | None = None
        expected_stage_snapshot: tuple[tuple[object, ...], ...] | None = None
        expected_target_info: os.stat_result | None = None
        expected_target_snapshot: tuple[tuple[object, ...], ...] | None = None
        try:
            parent_fd = _open_directory_chain(
                root_fd, canonical_target.relative_parts[:-1], create=True
            )
            existing_info = _stat_at(parent_fd, canonical_target.relative_parts[-1])
            if operation == "create":
                if existing_info is not None:
                    raise CollisionError(f"create refused existing output: {target}")
            else:
                if (
                    existing_info is None
                    or stat.S_ISLNK(existing_info.st_mode)
                    or not stat.S_ISDIR(existing_info.st_mode)
                ):
                    raise ScopeError(f"update target is not an existing directory: {target}")
                expected_target_info = existing_info
                existing_fd = _open_directory_at(
                    parent_fd, canonical_target.relative_parts[-1], label="update target"
                )
                try:
                    current_info = os.fstat(existing_fd)
                    if not _same_file_state(existing_info, current_info):
                        raise ScopeError(f"update target changed while it was opened: {target}")
                finally:
                    os.close(existing_fd)
                existing_errors = validate_skill_tree(target, require_provenance=False)
                if existing_errors:
                    if any("symlink" in error for error in existing_errors):
                        raise ScopeError("existing skill tree contains a symlink output")
                    scaffold_errors = {
                        "frontmatter value must be a string: description"
                    }
                    if not (
                        _is_skill_creator_scaffold(target)
                        and set(existing_errors) <= scaffold_errors
                    ):
                        raise ValidationError(
                            "update requires a structurally valid existing skill: "
                            + "; ".join(existing_errors)
                        )

            stage_parent, stage_root = _new_stage(target)
            stage_parent_info = _stat_at(parent_fd, stage_parent.name)
            if (
                stage_parent_info is None
                or stat.S_ISLNK(stage_parent_info.st_mode)
                or not stat.S_ISDIR(stage_parent_info.st_mode)
            ):
                raise ScopeError(f"stage parent is not an owned directory: {stage_parent}")
            stage_parent_fd = _open_directory_at(
                parent_fd, stage_parent.name, label="stage parent"
            )
            try:
                expected_stage_info = _stat_at(stage_parent_fd, stage_root.name)
                if (
                    expected_stage_info is None
                    or stat.S_ISLNK(expected_stage_info.st_mode)
                    or not stat.S_ISDIR(expected_stage_info.st_mode)
                ):
                    raise ScopeError(f"staged skill output is not an owned directory: {stage_root}")
            finally:
                os.close(stage_parent_fd)
            if operation == "update":
                expected_target_snapshot = _copy_tree_without_symlinks(
                    target, stage_root
                )
                _prepare_update_tree(stage_root, files, draft)
            else:
                _write_relative_files(stage_root, files)
            errors = validate_skill_tree(stage_root)
            if errors:
                raise ValidationError("generated skill failed validation: " + "; ".join(errors))
            stage_parent_fd = _open_directory_at(
                parent_fd, stage_parent.name, label="stage parent"
            )
            try:
                expected_stage_snapshot = _capture_tree_snapshot(stage_root)
            finally:
                os.close(stage_parent_fd)

            def validate_installed() -> None:
                installed_errors = validate_skill_discoverability(target)
                if installed_errors:
                    raise ValidationError(
                        "installed skill is not directly discoverable: "
                        + "; ".join(installed_errors)
                    )

            _install_staged_tree(
                stage_root,
                target,
                operation,
                failure_injector,
                parent_fd,
                expected_target_info,
                expected_target_snapshot,
                expected_stage_info,
                expected_stage_snapshot,
                validate_installed,
            )
        finally:
            if (
                parent_fd is not None
                and stage_parent is not None
                and stage_parent_info is not None
            ):
                _cleanup_owned_stage(
                    parent_fd,
                    stage_parent.name,
                    stage_parent_info,
                    stage_root.name if stage_root is not None else target.name,
                    expected_stage_info,
                )
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)
    return WriteResult(
        operation=operation,
        target=canonical_target.relative,
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
