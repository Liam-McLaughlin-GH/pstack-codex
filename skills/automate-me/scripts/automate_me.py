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
import subprocess
import sys
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
FRONTMATTER_SKILL_CREATOR_PLACEHOLDER_RE = re.compile(
    r"^\[TODO:\s[^\r\n]*\]$", re.IGNORECASE
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
NEGATION_PREFIX = r"(?:do(?:\s+|-)not|don't|dont|never|avoid|refuse(?:\s+|-)to|not)"
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
EXPLANATORY_PREFIX_RE = re.compile(
    r"^(?:explain|describe|define|summari[sz]e|outline|clarify|"
    r"what|how|why|when|where|which|whether)\b",
    re.IGNORECASE,
)
EXPLANATORY_LEAD_RE = re.compile(
    r"^(?:please\s+)?(?:tell(?:\s+|-)me|show(?:\s+|-)me|"
    r"walk(?:\s+|-)me(?:\s+|-)through|"
    r"help\s+me\s+understand)\b",
    re.IGNORECASE,
)
EXPLANATORY_PHRASE_RE = re.compile(
    r"\b(?:explain|describe|define|clarify|tell(?:\s+|-)me|"
    r"show(?:\s+|-)me|walk(?:\s+|-)me(?:\s+|-)through|"
    r"help\s+me\s+understand)\b",
    re.IGNORECASE,
)
DECISION_PREFIX_RE = re.compile(
    r"^(?:review|assess|evaluate|decide|recommend|advise|should|whether|"
    r"choose|select|which|what|how|why|when|where)\b",
    re.IGNORECASE,
)
DECISION_PHRASE_RE = re.compile(
    r"\b(?:review|assess|evaluate|decide|recommend|advise|"
    r"choose|select|help\s+me\s+decide|which\s+should|whether\s+to)\b",
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
        recovery_path: Path | None,
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
            "skill publication stopped; recovery_status="
            f"{recovery_status} cleanup_status={cleanup_status} "
            f"competitor_preserved={str(competitor_preserved).lower()} "
            f"approved_tree={recovery_path if recovery_path is not None else '<removed>'}"
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


def _is_explanatory_request(text: str) -> bool:
    """Recognize questions or decisions that must not authorize a writer."""

    clean = _without_quoted_text(text).strip()
    if not clean:
        return False
    concession = re.search(r"\b(?:but|however)\b", clean, re.IGNORECASE)
    if concession and _has_positive_action(clean[concession.end() :]):
        clean = clean[concession.end() :].strip()
    if (
        EXPLANATORY_PREFIX_RE.match(clean)
        or EXPLANATORY_LEAD_RE.match(clean)
        or DECISION_PREFIX_RE.match(clean)
    ):
        return True

    first_action = re.search(
        r"\b(?:" + "|".join(re.escape(word) for word in ACTION_WORDS + UPDATE_WORDS) + r")\b",
        clean,
        re.IGNORECASE,
    )
    leading_text = clean if first_action is None else clean[: first_action.start()]
    if EXPLANATORY_PHRASE_RE.search(leading_text) or DECISION_PHRASE_RE.search(
        leading_text
    ):
        return True
    return bool(
        re.match(
            r"^(?:do|does|did|is|are|should|would)\b",
            clean,
            re.IGNORECASE,
        )
    )


def _no_artifact_route(reason: str) -> RouteDecision:
    return RouteDecision(
        artifact="none",
        category="route-elsewhere",
        operation="none",
        target=None,
        reason=reason,
    )


def _slug_from_request(text: str, default: str) -> str:
    named = NAMED_RE.search(text)
    candidate = named.group(1).rstrip(".") if named else default
    if not SKILL_NAME_RE.fullmatch(candidate):
        raise RoutingError(f"invalid artifact name: {candidate}")
    return candidate


def _mode_name_from_request(text: str, *, strict: bool = True) -> str | None:
    matches = list(MODE_RE.finditer(text))
    if not matches:
        return None

    candidates: list[tuple[re.Match[str], str]] = []
    for match in matches:
        candidate = match.group(1).rstrip(".")
        if not SKILL_NAME_RE.fullmatch(candidate):
            if strict:
                raise RoutingError(f"invalid artifact name: {candidate}")
            return None
        candidates.append((match, candidate))

    unique_candidates = {candidate for _, candidate in candidates}
    if len(unique_candidates) == 1:
        return candidates[0][1]

    # A request may mention the existing mode as a template while naming a
    # different mode to create. Prefer the name explicitly attached to the
    # user's skill, and refuse the rest rather than selecting the first token.
    explicit: list[str] = []
    for match, candidate in candidates:
        prefix = text[max(0, match.start() - 48) : match.start()]
        if re.search(r"(?:named|called|my|personal)\s*$", prefix):
            explicit.append(candidate)
    if len(set(explicit)) == 1:
        return explicit[0]
    if strict:
        raise RoutingError("request contains multiple personal mode skill names")
    return None


def route_request(request: str) -> RouteDecision:
    """Choose an artifact without reading history or inspecting the filesystem."""

    text = " ".join(request.strip().lower().split())
    if not text:
        raise RoutingError("request must not be empty")
    unquoted_text = _without_quoted_text(text)
    has_positive_action = _has_positive_action(text)
    if NEGATED_AUTOMATE_ME_RE.search(unquoted_text) and not has_positive_action:
        return _no_artifact_route("negated automate-me intent is not an artifact request")
    if _has_negated_artifact_intent(text):
        return _no_artifact_route(
            "negated artifact intent is not a request to create or update"
        )
    if _is_explanatory_request(unquoted_text):
        return _no_artifact_route(
            "explanatory or decision requests do not authorize an artifact writer"
        )

    script_signal = _has_any(unquoted_text, ("script", "helper", "command")) and (
        "project-local" in unquoted_text
        or "repository" in unquoted_text
        or "repo" in unquoted_text
    )
    project_skill_signal = (
        "verification skill" in unquoted_text
        or "task skill" in unquoted_text
        or ("project-local" in unquoted_text and "skill" in unquoted_text)
    )
    if script_signal or project_skill_signal:
        if not has_positive_action or _is_explanatory_request(unquoted_text):
            return _no_artifact_route(
                "project-local wording is explanatory or lacks positive artifact intent"
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

    mode_signal = MODE_RE.search(unquoted_text) is not None
    mode_name = _mode_name_from_request(unquoted_text, strict=False)
    named = NAMED_RE.search(unquoted_text)
    automate_signal = _has_automate_me_invocation(unquoted_text)
    personal_signal = (
        (automate_signal and has_positive_action and not _is_explanatory_request(unquoted_text))
        or (
            has_positive_action
            and mode_signal
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
        mode_name = _mode_name_from_request(unquoted_text, strict=True)
        if named is not None and not SKILL_NAME_RE.fullmatch(named.group(1).rstrip(".")):
            raise RoutingError(f"invalid artifact name: {named.group(1).rstrip('.')}")
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

    return _no_artifact_route("the request does not ask for a new artifact")


def _resolved_root(workspace_root: Path | str) -> Path:
    root = Path(os.path.normpath(os.path.abspath(os.fspath(workspace_root))))
    _reject_symlinked_ancestors(root, label="workspace root")
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
            # macOS exposes /var and /tmp through same-basename /private aliases
            # at the filesystem root. Those host aliases are not workspace
            # aliases; reject every other symlinked component consistently.
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
    _reject_symlinked_ancestors(root, label="workspace root")
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
def _open_relative_file_at(
    root_fd: int,
    relative_parts: Sequence[str],
    *,
    flags: int,
    label: str,
    create_parent_dirs: bool = False,
) -> Iterator[tuple[int, int, str]]:
    if not relative_parts:
        raise ScopeError(f"{label} must identify a file below the root")
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


@contextmanager
def _open_relative_file(
    root: Path,
    relative_parts: Sequence[str],
    *,
    flags: int,
    label: str,
    create_parent_dirs: bool = False,
) -> Iterator[tuple[int, int, str]]:
    root_fd = _open_directory(root, label="root")
    try:
        with _open_relative_file_at(
            root_fd,
            relative_parts,
            flags=flags,
            label=label,
            create_parent_dirs=create_parent_dirs,
        ) as opened:
            yield opened
    finally:
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


def _assert_directory_descriptor_matches_path(
    path: Path, directory_fd: int, *, label: str
) -> None:
    """Reject a renamed, replaced, or symlinked path beside its reserved fd."""

    _reject_symlinked_ancestors(path, label=label)
    try:
        path_info = path.lstat()
    except FileNotFoundError as exc:
        raise ScopeError(f"{label} no longer exists: {path}") from exc
    fd_info = os.fstat(directory_fd)
    if (
        stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISDIR(path_info.st_mode)
        or not _same_file_identity(path_info, fd_info)
    ):
        raise ScopeError(f"{label} changed while it was reserved: {path}")


def _file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _snapshot_state_matches(info: os.stat_result, state: object) -> bool:
    return (
        isinstance(state, tuple)
        and len(state) == 6
        and (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        == (state[0], state[1], state[2])
    )


def _snapshot_file_content_matches(
    directory_fd: int,
    name: str,
    entry_info: os.stat_result,
    expected_entry: tuple[object, ...],
) -> bool:
    """Confirm that an owned file still has its recorded content before unlinking."""

    if len(expected_entry) < 4 or expected_entry[1] != "file":
        return False
    if not _snapshot_state_matches(entry_info, expected_entry[2]):
        return False
    expected_digest = expected_entry[3]
    if not isinstance(expected_digest, str):
        return False
    try:
        data, read_info = _read_relative_bytes_at(
            directory_fd,
            (name,),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="owned cleanup file",
        )
    except ScopeError:
        return False
    if not _same_file_state(entry_info, read_info):
        return False
    if hashlib.sha256(data).hexdigest() != expected_digest:
        return False
    current_info = _stat_at(directory_fd, name)
    return current_info is not None and _same_file_state(current_info, read_info)


def _snapshot_content(
    snapshot: tuple[tuple[object, ...], ...]
) -> tuple[tuple[object, ...], ...]:
    """Drop inode and timestamp state when comparing copied tree contents."""

    return tuple(
        sorted(
            (
                (entry[0], entry[1], entry[3] if entry[1] == "file" else None)
                for entry in snapshot
            ),
            key=lambda entry: (entry[0], entry[1]),
        )
    )


def _snapshot_matches(
    actual: tuple[tuple[object, ...], ...],
    expected: tuple[tuple[object, ...], ...],
    *,
    compare_contents: bool = False,
) -> bool:
    if compare_contents:
        return _snapshot_content(actual) == _snapshot_content(expected)
    return actual == expected


def _remove_owned_entries(
    directory_fd: int,
    snapshot: tuple[tuple[object, ...], ...],
    prefix: tuple[str, ...] = (),
    *,
    compare_file_contents: bool = False,
) -> bool:
    """Remove only entries recorded in a snapshot, preserving competitors."""

    expected = {
        tuple(entry[0]): entry
        for entry in snapshot
        if isinstance(entry[0], tuple)
    }
    try:
        entries = list(os.scandir(directory_fd))
    except OSError:
        return False
    cleanup_safe = True
    for entry in entries:
        relative = prefix + (entry.name,)
        expected_entry = expected.get(relative)
        if expected_entry is None:
            cleanup_safe = False
            continue
        try:
            entry_info = entry.stat(follow_symlinks=False)
        except OSError:
            cleanup_safe = False
            continue
        if not _snapshot_state_matches(entry_info, expected_entry[2]):
            cleanup_safe = False
            continue
        if expected_entry[1] == "file":
            current_info = _stat_at(directory_fd, entry.name)
            if (
                current_info is None
                or not _same_file_identity(entry_info, current_info)
                or (
                    compare_file_contents
                    and not _snapshot_file_content_matches(
                        directory_fd, entry.name, entry_info, expected_entry
                    )
                )
            ):
                cleanup_safe = False
                continue
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
            except OSError:
                cleanup_safe = False
            continue
        if expected_entry[1] != "directory" or not stat.S_ISDIR(entry_info.st_mode):
            cleanup_safe = False
            continue
        try:
            child_fd = _open_directory_at(
                directory_fd, entry.name, label="owned cleanup directory"
            )
        except ScopeError:
            cleanup_safe = False
            continue
        try:
            opened_info = os.fstat(child_fd)
            if _same_file_identity(entry_info, opened_info):
                if not _remove_owned_entries(
                    child_fd,
                    snapshot,
                    relative,
                    compare_file_contents=compare_file_contents,
                ):
                    cleanup_safe = False
            else:
                cleanup_safe = False
        finally:
            os.close(child_fd)
        current_info = _stat_at(directory_fd, entry.name)
        if current_info is None or not _same_file_identity(entry_info, current_info):
            cleanup_safe = False
            continue
        try:
            os.rmdir(entry.name, dir_fd=directory_fd)
        except OSError:
            # A new child or a changed child keeps this directory in place.
            cleanup_safe = False
    return cleanup_safe


def _remove_owned_directory_status(
    parent_fd: int,
    name: str,
    expected_info: os.stat_result,
    *,
    expected_snapshot: tuple[tuple[object, ...], ...] = (),
    require_snapshot_match: bool = False,
    compare_snapshot_contents: bool = False,
) -> str:
    """Remove an owned directory and report whether its state stayed safe."""

    current_info = _stat_at(parent_fd, name)
    if current_info is None:
        return "missing"
    if (
        stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return "changed"
    try:
        directory_fd = _open_directory_at(parent_fd, name, label="owned cleanup directory")
    except ScopeError:
        return "changed"
    try:
        opened_info = os.fstat(directory_fd)
        if not _same_file_identity(opened_info, expected_info):
            return "changed"
        if require_snapshot_match:
            try:
                if not _snapshot_matches(
                    _capture_tree_snapshot_at(directory_fd),
                    expected_snapshot,
                    compare_contents=compare_snapshot_contents,
                ):
                    return "changed"
            except ScopeError:
                return "changed"
        if not _remove_owned_entries(
            directory_fd,
            expected_snapshot,
            compare_file_contents=require_snapshot_match or compare_snapshot_contents,
        ):
            return "preserved"
    finally:
        os.close(directory_fd)
    current_info = _stat_at(parent_fd, name)
    if current_info is None:
        return "missing"
    if (
        stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return "changed"
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        return "preserved"
    return "removed"


def _remove_owned_directory(
    parent_fd: int,
    name: str,
    expected_info: os.stat_result,
    *,
    expected_snapshot: tuple[tuple[object, ...], ...] = (),
    require_snapshot_match: bool = False,
    compare_snapshot_contents: bool = False,
) -> bool:
    """Remove a directory only while its directory entry is still ours."""

    return _remove_owned_directory_status(
        parent_fd,
        name,
        expected_info,
        expected_snapshot=expected_snapshot,
        require_snapshot_match=require_snapshot_match,
        compare_snapshot_contents=compare_snapshot_contents,
    ) in {"removed", "missing"}


def _remove_owned_tree_identity_status(
    parent_fd: int, name: str, expected_info: os.stat_result
) -> str:
    """Remove a stage tree by its reserved identity when no content snapshot exists."""

    current_info = _stat_at(parent_fd, name)
    if current_info is None:
        return "missing"
    if (
        stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return "changed"
    try:
        directory_fd = _open_directory_at(parent_fd, name, label="owned stage directory")
    except ScopeError:
        return "changed"
    cleanup_safe = True
    try:
        opened_info = os.fstat(directory_fd)
        if not _same_file_identity(opened_info, expected_info):
            return "changed"
        try:
            entries = list(os.scandir(directory_fd))
        except OSError:
            return "preserved"
        for entry in entries:
            try:
                entry_info = entry.stat(follow_symlinks=False)
            except OSError:
                cleanup_safe = False
                continue
            current_entry = _stat_at(directory_fd, entry.name)
            if (
                current_entry is None
                or not _same_file_identity(entry_info, current_entry)
            ):
                cleanup_safe = False
                continue
            if stat.S_ISDIR(entry_info.st_mode):
                child_status = _remove_owned_tree_identity_status(
                    directory_fd, entry.name, entry_info
                )
                if child_status not in {"removed", "missing"}:
                    cleanup_safe = False
                continue
            if stat.S_ISLNK(entry_info.st_mode) or stat.S_ISREG(entry_info.st_mode):
                try:
                    os.unlink(entry.name, dir_fd=directory_fd)
                except OSError:
                    cleanup_safe = False
                continue
            cleanup_safe = False
        current_info = _stat_at(parent_fd, name)
        if (
            current_info is None
            or not _same_file_identity(current_info, expected_info)
        ):
            return "changed"
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            return "preserved"
        return "removed" if cleanup_safe else "preserved"
    finally:
        os.close(directory_fd)


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
    expected_stage_snapshot: tuple[tuple[object, ...], ...] = (),
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
            if expected_stage_snapshot:
                _remove_owned_directory(
                    stage_parent_fd,
                    stage_name,
                    expected_stage_info,
                    expected_snapshot=expected_stage_snapshot,
                )
            else:
                _remove_owned_tree_identity_status(
                    stage_parent_fd, stage_name, expected_stage_info
                )
    finally:
        if stage_parent_fd is not None:
            os.close(stage_parent_fd)
    _remove_empty_owned_directory(parent_fd, stage_parent_name, expected_parent_info)


def _read_relative_bytes_at(
    root_fd: int,
    relative_parts: Sequence[str],
    *,
    max_bytes: int | None,
    label: str,
) -> tuple[bytes, os.stat_result]:
    with _open_relative_file_at(
        root_fd,
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


def _read_relative_bytes(
    root: Path,
    relative_parts: Sequence[str],
    *,
    max_bytes: int | None,
    label: str,
) -> tuple[bytes, os.stat_result]:
    root_fd = _open_directory(root, label="root")
    try:
        return _read_relative_bytes_at(
            root_fd,
            relative_parts,
            max_bytes=max_bytes,
            label=label,
        )
    finally:
        os.close(root_fd)


def _read_relative_text_at(
    root_fd: int,
    relative_parts: Sequence[str],
    *,
    max_bytes: int | None,
    label: str,
) -> tuple[str, os.stat_result]:
    data, info = _read_relative_bytes_at(
        root_fd, relative_parts, max_bytes=max_bytes, label=label
    )
    try:
        return data.decode("utf-8"), info
    except UnicodeDecodeError as exc:
        raise ScopeError(
            f"{label} is not UTF-8 text: {PurePosixPath(*relative_parts)}"
        ) from exc


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


def _relative_regular_file_exists_at(
    root_fd: int, relative_parts: Sequence[str], *, label: str
) -> bool:
    try:
        with _open_relative_file_at(
            root_fd,
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
def _workspace_write_lock(root: Path) -> Iterator[int]:
    _reject_symlinked_ancestors(root, label="workspace root")
    lock_fd = _open_directory(root, label="workspace root")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        opened_info = os.fstat(lock_fd)
        try:
            current_info = root.lstat()
        except OSError as exc:
            raise ScopeError("workspace root changed while it was locked") from exc
        if (
            stat.S_ISLNK(current_info.st_mode)
            or not stat.S_ISDIR(current_info.st_mode)
            or not _same_file_identity(opened_info, current_info)
        ):
            raise ScopeError("workspace root changed while it was locked")
        yield lock_fd
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


def _frontmatter_bounds(text: str) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValidationError("SKILL.md must start with frontmatter")
    content_start = len(lines[0])
    offset = content_start
    for line in lines[1:]:
        if line.rstrip("\r\n") == "---":
            return content_start, offset
        offset += len(line)
    raise ValidationError("SKILL.md frontmatter is not closed")


def _strip_yaml_inline_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        character = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = None
        elif quote == "'":
            if character == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    index += 1
                else:
                    quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
        index += 1
    return value.rstrip()


def _parse_frontmatter(text: str) -> dict[str, str]:
    content_start, content_end = _frontmatter_bounds(text)
    fields: dict[str, str] = {}
    lines = text[content_start:content_end].splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.lstrip().startswith("#"):
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
        value = _strip_yaml_inline_comment(value.strip())
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
            if FRONTMATTER_NON_STRING_RE.fullmatch(
                value
            ) and not FRONTMATTER_SKILL_CREATOR_PLACEHOLDER_RE.fullmatch(value):
                raise ValidationError(f"frontmatter value must be a string: {key}")
        fields[key] = value
        index += 1
    return fields


def _markdown_without_frontmatter(text: str) -> str:
    """Mask YAML frontmatter while preserving the Markdown body byte-for-byte."""

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    offset = len(lines[0])
    for line in lines[1:]:
        if line.rstrip("\r\n") == "---":
            return _blank_markdown_line(text[:offset]) + text[offset + len(line) :]
        offset += len(line)
    return text


def _blank_markdown_line(line: str) -> str:
    return "".join(
        "\n" if character == "\n" else "\r" if character == "\r" else " "
        for character in line
    )


def _markdown_without_fenced_code(text: str) -> str:
    """Mask fenced, indented, and inline code for bounded Markdown scanning."""

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
    masked_text = "".join(masked)
    characters = list(masked_text)
    index = 0
    while index < len(characters):
        if characters[index] != "`":
            index += 1
            continue
        start = index
        while index < len(characters) and characters[index] == "`":
            index += 1
        run_length = index - start
        closing = -1
        cursor = index
        while cursor < len(characters):
            if characters[cursor] != "`":
                cursor += 1
                continue
            close_start = cursor
            while cursor < len(characters) and characters[cursor] == "`":
                cursor += 1
            if cursor - close_start == run_length:
                closing = close_start
                break
        if closing < 0:
            continue
        for position in range(start, cursor):
            if characters[position] not in "\r\n":
                characters[position] = " "
        index = cursor
    return "".join(characters)


def _escaped_at(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _matching_markdown_delimiter(
    text: str, start: int, opening: str, closing: str
) -> int | None:
    depth = 0
    index = start
    while index < len(text):
        if _escaped_at(text, index):
            index += 1
            continue
        character = text[index]
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _markdown_bracket_pairs(text: str) -> dict[int, int]:
    """Pair escaped-aware brackets once so malformed input stays linear."""

    stack: list[int] = []
    pairs: dict[int, int] = {}
    backslashes = 0
    for index, character in enumerate(text):
        if character == "\\":
            backslashes += 1
            continue
        escaped = backslashes % 2 == 1
        backslashes = 0
        if escaped:
            continue
        if character == "[":
            stack.append(index)
        elif character == "]" and stack:
            pairs[stack.pop()] = index
    return pairs


def _parse_markdown_destination(
    text: str, start: int, *, require_end: bool = True
) -> tuple[str, int] | None:
    """Parse one inline or reference-definition destination strictly."""

    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        return None
    if text[index] == "<":
        destination_start = index + 1
        index = destination_start
        closed_angle = False
        while index < len(text):
            if _escaped_at(text, index):
                index += 1
                continue
            if text[index] == "<":
                raise ValidationError("malformed Markdown link has nested angle bracket")
            if text[index] == ">":
                destination = text[destination_start:index]
                index += 1
                closed_angle = True
                break
            index += 1
        if not closed_angle:
            raise ValidationError("malformed Markdown link has an unmatched angle bracket")
    else:
        destination_start = index
        depth = 0
        closed_inline = False
        while index < len(text):
            if _escaped_at(text, index):
                index += 1
                continue
            character = text[index]
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    if not require_end:
                        raise ValidationError(
                            "malformed Markdown reference definition has trailing delimiter"
                        )
                    destination = text[destination_start:index].rstrip()
                    closed_inline = True
                    return destination, index + 1
                depth -= 1
            elif character.isspace() and depth == 0:
                destination = text[destination_start:index]
                break
            index += 1
        else:
            if depth:
                raise ValidationError(
                    "malformed Markdown link has an unmatched destination delimiter"
                )
            destination = text[destination_start:index].rstrip()
        if not destination and not closed_inline:
            raise ValidationError("malformed Markdown link has an empty destination")

    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and (require_end and text[index] == ")"):
        return destination, index + 1
    if index < len(text):
        title_open = text[index]
        title_close = {"\"": "\"", "'": "'", "(": ")"}.get(title_open)
        if title_close is None:
            raise ValidationError("malformed Markdown link has trailing tokens")
        if title_open == "(":
            title_end = _matching_markdown_delimiter(text, index, "(", ")")
            if title_end is None:
                raise ValidationError("malformed Markdown link has an unmatched title delimiter")
            index = title_end + 1
        else:
            index += 1
            while index < len(text):
                if text[index] == title_close and not _escaped_at(text, index):
                    index += 1
                    break
                index += 1
            else:
                raise ValidationError("malformed Markdown link has an unmatched title delimiter")
        while index < len(text) and text[index].isspace():
            index += 1
    if require_end:
        if index >= len(text) or text[index] != ")":
            raise ValidationError("malformed Markdown link has trailing tokens or no closing delimiter")
        return destination, index + 1
    if index != len(text):
        raise ValidationError("malformed Markdown reference definition has trailing tokens")
    return destination, index


def _reference_label(value: str) -> str:
    value = re.sub(r"\\([\\\[\]])", r"\1", value)
    return " ".join(value.replace("\n", " ").split()).casefold()


def _markdown_reference_definitions(text: str) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        leading = len(content) - len(content.lstrip(" "))
        if leading > 3:
            continue
        content = content[leading:]
        if not content.startswith("["):
            continue
        close = _matching_markdown_delimiter(content, 0, "[", "]")
        if close is None:
            if ":" in content and not content.casefold().startswith("[todo:"):
                raise ValidationError(
                    "malformed Markdown reference definition has an unmatched label delimiter"
                )
            # The skill-creator scaffold deliberately includes multi-line TODO
            # paragraphs with this shape; unlike a definition-looking line,
            # they remain ordinary Markdown until a label is closed.
            continue
        if close + 1 >= len(content) or content[close + 1] != ":":
            continue
        parsed = _parse_markdown_destination(content, close + 2, require_end=False)
        if parsed is None:
            raise ValidationError("malformed Markdown reference definition has no destination")
        destination, end = parsed
        if content[end:].strip():
            raise ValidationError("malformed Markdown reference definition has trailing tokens")
        definitions[_reference_label(content[1:close])] = destination
    return definitions


def _markdown_link_targets(text: str) -> Iterator[str]:
    """Yield local link destinations outside code with bounded delimiter scans."""

    masked = _markdown_without_fenced_code(_markdown_without_frontmatter(text))
    definitions = _markdown_reference_definitions(masked)
    bracket_pairs = _markdown_bracket_pairs(masked)
    for destination in definitions.values():
        yield destination

    index = 0
    while index < len(masked):
        if masked[index] != "[" or _escaped_at(masked, index):
            index += 1
            continue
        close = bracket_pairs.get(index)
        if close is None:
            index += 1
            continue
        after = close + 1
        if after < len(masked) and masked[after] == "(":
            parsed = _parse_markdown_destination(masked, after + 1)
            if parsed is None:
                raise ValidationError("malformed Markdown inline link has no destination")
            destination, index = parsed
            yield destination
            continue
        reference: str | None = None
        if after + 1 < len(masked) and masked[after] == "[":
            reference_close = bracket_pairs.get(after)
            if reference_close is None:
                raise ValidationError(
                    "malformed Markdown reference link has an unmatched label delimiter"
                )
            reference = masked[after + 1 : reference_close] or masked[index + 1 : close]
            index = reference_close + 1
        else:
            reference = masked[index + 1 : close]
            index = close + 1
        if reference is not None:
            destination = definitions.get(_reference_label(reference))
            if destination is not None:
                yield destination


def _relative_link_target(
    skill_dir: Path, raw_target: str, source_dir: Path | None = None
) -> Path | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    target = re.sub(r"\\([\\`*{}\[\]()#+.!_>~-])", r"\1", target)
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


def _scan_tree_entries_at(
    root_fd: int,
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[str]]:
    """List a tree from a reserved directory descriptor."""

    directories: list[tuple[str, ...]] = [()]
    files: list[tuple[str, ...]] = []
    errors: list[str] = []
    pending: list[tuple[int, tuple[str, ...]]] = [(os.dup(root_fd), ())]
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


def _scan_tree_entries(
    root: Path,
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[str]]:
    """List a tree through directory descriptors without following aliases."""

    root_fd = _open_directory(root, label="skill directory")
    try:
        return _scan_tree_entries_at(root_fd)
    finally:
        os.close(root_fd)


def _capture_tree_snapshot_at(root_fd: int) -> tuple[tuple[object, ...], ...]:
    """Capture bounded structure, metadata, and content from a directory fd."""

    source_info = os.fstat(root_fd)
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise ScopeError("skill output must be a real directory")
    directories, files, errors = _scan_tree_entries_at(root_fd)
    if errors:
        raise ScopeError("; ".join(errors))

    entries: list[tuple[object, ...]] = []
    for relative in directories:
        if not relative:
            continue
        directory_fd = _open_directory_chain(root_fd, relative)
        try:
            entries.append((relative, "directory", _file_state(os.fstat(directory_fd))))
        finally:
            os.close(directory_fd)
    total_bytes = 0
    for relative in files:
        data, info = _read_relative_bytes_at(
            root_fd,
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


def _capture_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture bounded structure, metadata, and content for an existing tree."""

    _reject_symlinked_ancestors(root, label="skill output path")
    root_fd = _open_directory(root, label="skill directory")
    try:
        return _capture_tree_snapshot_at(root_fd)
    finally:
        os.close(root_fd)


def _validate_skill_tree_at(
    root_fd: int,
    root: Path,
    *,
    require_provenance: bool = True,
) -> tuple[str, ...]:
    """Validate a skill through a reserved directory descriptor."""

    errors: list[str] = []
    try:
        _validate_skill_name(root.name)
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        _, files, scan_errors = _scan_tree_entries_at(root_fd)
        errors.extend(scan_errors)
    except ScopeError as exc:
        errors.append(str(exc))
        return tuple(errors)

    markdown_files = [relative for relative in files if relative[-1].casefold().endswith(".md")]
    markdown_text: dict[tuple[str, ...], str] = {}
    for relative in markdown_files:
        try:
            markdown_text[relative], _ = _read_relative_text_at(
                root_fd,
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

    for source_relative, source_text in markdown_text.items():
        source_dir = root / PurePosixPath(*source_relative).parent
        try:
            link_targets = _markdown_link_targets(source_text)
            for raw_target in link_targets:
                try:
                    target = _relative_link_target(root, raw_target, source_dir)
                except ValidationError as exc:
                    errors.append(str(exc))
                    continue
                if target is None:
                    continue
                try:
                    target_path = _canonical_path(root, target)
                    _read_relative_text_at(
                        root_fd,
                        target_path.relative_parts,
                        max_bytes=MAX_ARTIFACT_BYTES,
                        label="reference link target",
                    )
                except (ScopeError, ValidationError) as exc:
                    errors.append(f"invalid reference link {raw_target}: {exc}")
                    continue
        except ValidationError as exc:
            errors.append(f"invalid Markdown link syntax: {exc}")

    if require_provenance:
        provenance_text = "\n".join(markdown_text.values())
        if UPSTREAM_COMMIT not in provenance_text:
            errors.append("upstream source commit is missing from the skill")
        if UPSTREAM_COPYRIGHT not in provenance_text:
            errors.append("upstream copyright notice is missing from the skill")
        if "MIT License" not in provenance_text:
            errors.append("upstream license notice is missing from the skill")
    return tuple(errors)


def validate_skill_tree(
    skill_dir: Path | str, *, require_provenance: bool = True
) -> tuple[str, ...]:
    """Validate every bundled Markdown file and its local links safely."""

    raw_root = Path(os.path.normpath(os.path.abspath(os.fspath(skill_dir))))
    try:
        _reject_symlinked_ancestors(raw_root, label="skill validation path")
        if raw_root.is_symlink():
            return ("skill directory must not be a symlink",)
        root = _resolved_root(raw_root)
        root_fd = _open_directory(root, label="skill directory")
    except ScopeError as exc:
        return (str(exc),)
    try:
        return _validate_skill_tree_at(
            root_fd, root, require_provenance=require_provenance
        )
    finally:
        os.close(root_fd)


def _validate_skill_discoverability_at(
    root_fd: int, root: Path, *, require_provenance: bool = True
) -> tuple[str, ...]:
    errors = list(
        _validate_skill_tree_at(
            root_fd, root, require_provenance=require_provenance
        )
    )
    try:
        _validate_skill_name(root.name)
    except ValidationError as exc:
        errors.append(str(exc))
    try:
        _read_relative_bytes_at(
            root_fd,
            ("agents", "openai.yaml"),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="skill discovery interface",
        )
    except ScopeError as exc:
        errors.append(str(exc))
    return tuple(errors)


def validate_skill_discoverability(skill_dir: Path | str) -> tuple[str, ...]:
    raw_root = Path(os.path.normpath(os.path.abspath(os.fspath(skill_dir))))
    errors: list[str] = []
    try:
        _reject_symlinked_ancestors(raw_root, label="skill discovery path")
        if raw_root.is_symlink():
            return ("skill directory must not be a symlink",)
        root = _resolved_root(raw_root)
        root_fd = _open_directory(root, label="skill directory")
    except ScopeError as exc:
        return (str(exc),)
    try:
        if raw_root.parent.name != "skills":
            errors.append("skill output must be a direct skills/<name>/ directory")
        try:
            parent_info = raw_root.parent.lstat()
        except FileNotFoundError:
            parent_info = None
        if parent_info is not None and stat.S_ISLNK(parent_info.st_mode):
            errors.append("skill discovery parent must not be a symlink alias")
        errors.extend(_validate_skill_discoverability_at(root_fd, root))
    finally:
        os.close(root_fd)
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
    return re.search(r"(?mi)^description:\s*\[TODO:.*\]\s*$", text) is not None


def _is_skill_creator_scaffold_at(root_fd: int) -> bool:
    try:
        text, _ = _read_relative_text_at(
            root_fd,
            ("SKILL.md",),
            max_bytes=MAX_ARTIFACT_BYTES,
            label="skill-creator scaffold",
        )
    except ScopeError:
        return False
    return re.search(r"(?mi)^description:\s*\[TODO:.*\]\s*$", text) is not None


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


def _validate_relative_parts(relative_parts: Sequence[str], *, label: str) -> tuple[str, ...]:
    parts = tuple(relative_parts)
    if not parts or any(
        not isinstance(part, str) or not part or part in {".", ".."} or "/" in part
        for part in parts
    ):
        raise ScopeError(f"{label} is not a normalized relative path")
    return parts


def _write_relative_bytes_at(
    root_fd: int, relative_parts: Sequence[str], data: bytes
) -> None:
    parts = _validate_relative_parts(relative_parts, label="write path")
    parent_fd: int | None = None
    temporary_name = f".{parts[-1]}.{uuid.uuid4().hex}.tmp"
    temporary_fd: int | None = None
    try:
        parent_fd = _open_directory_chain(root_fd, parts[:-1], create=True)
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


def _write_relative_bytes(root: Path, relative_parts: Sequence[str], data: bytes) -> None:
    canonical_root = _resolved_root(root)
    canonical = _canonical_path(canonical_root, PurePosixPath(*relative_parts))
    root_fd = _open_directory(canonical_root, label="write root")
    try:
        _write_relative_bytes_at(root_fd, canonical.relative_parts, data)
    finally:
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
        directory_fd = _open_directory_chain(root_fd, canonical.relative_parts, create=True)
        os.close(directory_fd)
    finally:
        os.close(root_fd)


def _ensure_relative_directory_at(root_fd: int, relative_parts: Sequence[str]) -> None:
    parts = _validate_relative_parts(relative_parts, label="directory path")
    directory_fd = _open_directory_chain(root_fd, parts, create=True)
    os.close(directory_fd)


def _copy_tree_without_symlinks_at(
    source_fd: int, destination_fd: int
) -> tuple[tuple[object, ...], ...]:
    source_info = os.fstat(source_fd)
    destination_info = os.fstat(destination_fd)
    if (
        stat.S_ISLNK(source_info.st_mode)
        or not stat.S_ISDIR(source_info.st_mode)
        or stat.S_ISLNK(destination_info.st_mode)
        or not stat.S_ISDIR(destination_info.st_mode)
    ):
        raise ScopeError("skill output must be a real directory")
    snapshot = _capture_tree_snapshot_at(source_fd)
    directories, files, errors = _scan_tree_entries_at(source_fd)
    if errors:
        raise ScopeError("; ".join(errors))
    for relative in directories:
        if not relative:
            continue
        _ensure_relative_directory_at(destination_fd, relative)
    total_bytes = 0
    for relative in files:
        data, _ = _read_relative_bytes_at(
            source_fd,
            relative,
            max_bytes=MAX_ARTIFACT_BYTES,
            label="existing skill output",
        )
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
            raise ScopeError("aggregate preserved skill size exceeds the bounded limit")
        _write_relative_bytes_at(destination_fd, relative, data)
    if _capture_tree_snapshot_at(source_fd) != snapshot:
        raise ScopeError("existing skill output changed while it was copied")
    if not _snapshot_matches(
        _capture_tree_snapshot_at(destination_fd), snapshot, compare_contents=True
    ):
        raise ScopeError("preserved skill output was not copied completely")
    return snapshot


def _copy_tree_without_symlinks(
    source: Path, destination: Path
) -> tuple[tuple[object, ...], ...]:
    _reject_symlinked_ancestors(source, label="source path")
    _reject_symlinked_ancestors(destination, label="destination path")
    source_fd = _open_directory(source, label="source skill directory")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        destination_fd = _open_directory(destination, label="destination skill directory")
        try:
            return _copy_tree_without_symlinks_at(source_fd, destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _write_relative_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        _write_relative_text(root, relative, content)


def _write_relative_files_at(root_fd: int, files: dict[str, str]) -> None:
    for relative, content in files.items():
        _write_relative_bytes_at(
            root_fd,
            tuple(PurePosixPath(relative).parts),
            content.encode("utf-8"),
        )


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
    _, end = _frontmatter_bounds(skill_text)
    _, generated_end = _frontmatter_bounds(generated_text)
    body_start = skill_text.find("\n", end)
    generated_body_start = generated_text.find("\n", generated_end)
    body = skill_text[len(skill_text) if body_start < 0 else body_start + 1 :]
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
    generated_frontmatter = generated_text[
        : len(generated_text) if generated_body_start < 0 else generated_body_start + 1
    ]
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


def _prepare_update_tree_at(
    staged_fd: int,
    staged_root: Path,
    generated_files: dict[str, str],
    draft: SkillDraft,
) -> None:
    existing_skill, _ = _read_relative_text_at(
        staged_fd,
        ("SKILL.md",),
        max_bytes=MAX_ARTIFACT_BYTES,
        label="existing SKILL.md",
    )
    generated_files = dict(generated_files)
    generated_files["SKILL.md"] = _merge_existing_skill(
        existing_skill,
        generated_files["SKILL.md"],
        draft,
        allow_skill_creator_scaffold=_is_skill_creator_scaffold_at(staged_fd),
    )
    try:
        provenance, _ = _read_relative_text_at(
            staged_fd,
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
        existing_working_style, _ = _read_relative_text_at(
            staged_fd,
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
    if _relative_regular_file_exists_at(
        staged_fd,
        ("agents", "openai.yaml"),
        label="existing generated reference",
    ):
        generated_files.pop("agents/openai.yaml", None)
    _write_relative_files_at(staged_fd, generated_files)


def _skill_creator_initializer() -> Path:
    override = os.environ.get("AUTOMATE_ME_SKILL_CREATOR")
    candidates = [
        Path(override) if override else None,
        Path.home()
        / ".codex"
        / "skills"
        / ".system"
        / "skill-creator"
        / "scripts"
        / "init_skill.py",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            return candidate
    raise ValidationError("skill-creator initializer is not installed")


def _run_skill_creator_initializer(stage_parent_fd: int, skill_name: str) -> None:
    initializer = _skill_creator_initializer().absolute()
    # subprocess resolves ``cwd`` before pass_fds are installed on macOS. Run
    # a tiny child-side wrapper instead: the child receives the reserved fd,
    # fchdirs to it, and then invokes the required initializer by runpy.
    child_code = (
        "import os,runpy,sys;"
        "os.fchdir(int(os.environ['AUTOMATE_ME_STAGE_FD']));"
        "script=sys.argv[1];sys.argv=sys.argv[1:];"
        "sys.path.insert(0,os.path.dirname(script));"
        "runpy.run_path(script,run_name='__main__')"
    )
    initializer_args = [
        str(initializer),
        skill_name,
        "--path",
        ".",
        "--resources",
        "references",
        "--interface",
        f"short_description={_short_description(skill_name)}",
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            *initializer_args,
        ],
        pass_fds=(stage_parent_fd,),
        env={
            **os.environ,
            "AUTOMATE_ME_STAGE_FD": str(stage_parent_fd),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "initializer failed").strip()
        raise ValidationError(f"skill-creator initializer failed: {details}")


def _new_stage(
    parent_fd: int, target: Path
) -> tuple[str, int, Path]:
    """Reserve a stage parent under the already-open target parent."""

    for _ in range(16):
        stage_parent_name = f".{target.name}.stage-{uuid.uuid4().hex}"
        try:
            os.mkdir(stage_parent_name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            continue
        stage_parent_info = _stat_at(parent_fd, stage_parent_name)
        try:
            stage_parent_fd = _open_directory_at(
                parent_fd, stage_parent_name, label="stage parent"
            )
        except BaseException:
            if (
                stage_parent_info is not None
                and stat.S_ISDIR(stage_parent_info.st_mode)
                and not stat.S_ISLNK(stage_parent_info.st_mode)
            ):
                _remove_empty_owned_directory(
                    parent_fd, stage_parent_name, stage_parent_info
                )
            raise
        return stage_parent_name, stage_parent_fd, target.parent / stage_parent_name / target.name
    raise CollisionError("could not reserve a unique stage directory")


def _prepare_recovery_copy(
    target: Path,
    target_fd: int,
    target_parent_fd: int,
    expected_snapshot: tuple[tuple[object, ...], ...],
) -> tuple[
    int,
    os.stat_result,
    bool,
    os.stat_result,
    int,
    Path,
    tuple[tuple[object, ...], ...],
]:
    """Keep a deterministic copy available before an update can be displaced."""

    recovery_parent_fd: int | None = None
    recovery_parent_info: os.stat_result | None = None
    recovery_parent_created = False
    recovery_info: os.stat_result | None = None
    recovery_fd: int | None = None
    recovery_snapshot: tuple[tuple[object, ...], ...] = ()
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
        recovery_fd = _open_directory_at(
            recovery_parent_fd, target.name, label="recovery output"
        )
        recovery_path = target.parent / RECOVERY_DIRECTORY_NAME / target.name
        copied_snapshot = _copy_tree_without_symlinks_at(target_fd, recovery_fd)
        if copied_snapshot != expected_snapshot:
            raise ScopeError(
                f"update target changed while its recovery copy was prepared: {target}"
            )
        recovery_snapshot = _capture_tree_snapshot_at(recovery_fd)
        return (
            recovery_parent_fd,
            recovery_parent_info,
            recovery_parent_created,
            recovery_info,
            recovery_fd,
            recovery_path,
            recovery_snapshot,
        )
    except BaseException:
        if recovery_fd is not None:
            os.close(recovery_fd)
        if recovery_parent_fd is not None:
            if recovery_info is not None:
                _remove_owned_directory(
                    recovery_parent_fd,
                    target.name,
                    recovery_info,
                    expected_snapshot=recovery_snapshot,
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
    backup_fd: int | None = None
    try:
        backup_fd = _open_directory_at(
            target_parent_fd, backup_name, label="rollback backup"
        )
        opened_info = os.fstat(backup_fd)
        return (
            _same_file_identity(opened_info, expected_info)
            and _capture_tree_snapshot_at(backup_fd) == expected_snapshot
        )
    except ScopeError:
        return False
    finally:
        if backup_fd is not None:
            os.close(backup_fd)


def _recovery_state(
    recovery_parent_fd: int | None,
    name: str,
    expected_info: os.stat_result | None,
    recovery_path: Path | None,
    expected_snapshot: tuple[tuple[object, ...], ...],
) -> tuple[str, Path | None]:
    """Report only the recovery path that still names the expected state."""

    if recovery_parent_fd is None or expected_info is None:
        return "removed", None
    current_info = _stat_at(recovery_parent_fd, name)
    if current_info is None:
        return "removed", None
    if (
        stat.S_ISLNK(current_info.st_mode)
        or not stat.S_ISDIR(current_info.st_mode)
        or not _same_file_identity(current_info, expected_info)
    ):
        return "changed", recovery_path
    recovery_fd: int | None = None
    try:
        recovery_fd = _open_directory_at(
            recovery_parent_fd, name, label="recovery output"
        )
        if _snapshot_matches(
            _capture_tree_snapshot_at(recovery_fd),
            expected_snapshot,
            compare_contents=True,
        ):
            return "present", recovery_path
        return "changed", recovery_path
    except ScopeError:
        return "changed", recovery_path
    finally:
        if recovery_fd is not None:
            os.close(recovery_fd)


def _install_staged_tree(
    stage_parent_fd: int,
    stage_name: str,
    stage_fd: int,
    stage_root: Path,
    target: Path,
    operation: str,
    failure_injector: Callable[[str], None] | None,
    target_parent_fd: int,
    expected_target_info: os.stat_result | None,
    expected_target_snapshot: tuple[tuple[object, ...], ...] | None,
    expected_stage_info: os.stat_result,
    expected_stage_snapshot: tuple[tuple[object, ...], ...],
    validate_installed: Callable[[int], None],
) -> None:
    if failure_injector:
        failure_injector("before_install")

    _assert_directory_descriptor_matches_path(
        target.parent, target_parent_fd, label="target parent"
    )
    _assert_directory_descriptor_matches_path(
        stage_root.parent, stage_parent_fd, label="stage parent"
    )
    stage_info = os.fstat(stage_fd)
    if (
        stat.S_ISLNK(stage_info.st_mode)
        or not stat.S_ISDIR(stage_info.st_mode)
        or not _same_file_identity(stage_info, expected_stage_info)
    ):
        raise ScopeError(f"staged skill output was replaced before install: {stage_root}")
    live_stage_info = _stat_at(stage_parent_fd, stage_name)
    if (
        live_stage_info is None
        or stat.S_ISLNK(live_stage_info.st_mode)
        or not stat.S_ISDIR(live_stage_info.st_mode)
        or not _same_file_identity(live_stage_info, stage_info)
    ):
        raise ScopeError(f"staged skill output was replaced before install: {stage_root}")
    if _capture_tree_snapshot_at(stage_fd) != expected_stage_snapshot:
        raise ScopeError(f"staged skill output changed before install: {stage_root}")

    if operation == "create":
        target_fd: int | None = None
        owned_target_info: os.stat_result | None = None
        try:
            _assert_directory_descriptor_matches_path(
                target.parent, target_parent_fd, label="target parent"
            )
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
            if not _snapshot_matches(
                _capture_tree_snapshot_at(target_fd),
                expected_stage_snapshot,
                compare_contents=True,
            ):
                raise ScopeError(f"installed skill output changed during publication: {target}")
            validate_installed(target_fd)
            if failure_injector:
                failure_injector("after_install")
        except BaseException as exc:
            if owned_target_info is not None:
                cleanup_status = _remove_owned_directory_status(
                    target_parent_fd,
                    target.name,
                    owned_target_info,
                    expected_snapshot=expected_stage_snapshot,
                    require_snapshot_match=True,
                    compare_snapshot_contents=True,
                )
                if cleanup_status not in {"removed", "missing"}:
                    current_info = _stat_at(target_parent_fd, target.name)
                    same_owned_tree = (
                        current_info is not None
                        and _same_file_identity(current_info, owned_target_info)
                    )
                    raise PublicationRecoveryError(
                        target=target,
                        recovery_path=target if same_owned_tree else None,
                        cause=exc,
                        recovery_status=(
                            "installed_tree_changed_preserved"
                            if same_owned_tree
                            else "installed_tree_replaced_preserved"
                        ),
                        cleanup_status=(
                            "installed_tree_preserved"
                            if same_owned_tree
                            else "owned_tree_not_removed"
                        ),
                        competitor_preserved=not same_owned_tree,
                    ) from exc
            raise
        finally:
            if target_fd is not None:
                os.close(target_fd)
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
    recovery_fd: int | None = None
    recovery_path: Path | None = None
    recovery_snapshot: tuple[tuple[object, ...], ...] = ()
    preserve_recovery = False
    publication_succeeded = False
    target_fd: int | None = None
    installed_cleanup_status: str | None = None
    try:
        current_info = _stat_at(target_parent_fd, target.name)
        if (
            current_info is None
            or stat.S_ISLNK(current_info.st_mode)
            or not stat.S_ISDIR(current_info.st_mode)
            or not _same_file_state(current_info, expected_target_info)
        ):
            raise ScopeError(f"update target changed before publication: {target}")
        target_fd = _open_directory_at(target_parent_fd, target.name, label="update target")
        if _capture_tree_snapshot_at(target_fd) != expected_target_snapshot:
            raise ScopeError(f"update target content changed after preflight: {target}")
        (
            recovery_parent_fd,
            recovery_parent_info,
            recovery_parent_created,
            recovery_info,
            recovery_fd,
            recovery_path,
            recovery_snapshot,
        ) = _prepare_recovery_copy(
            target,
            target_fd,
            target_parent_fd,
            expected_target_snapshot,
        )
        if _capture_tree_snapshot_at(target_fd) != expected_target_snapshot:
            raise ScopeError(f"update target content changed before publication: {target}")
        _assert_directory_descriptor_matches_path(
            target.parent, target_parent_fd, label="target parent"
        )
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
        if not _backup_matches_expected(
            target_parent_fd, backup_name, backup_info, expected_target_snapshot
        ):
            raise ScopeError(f"update target content changed while it was moved: {target}")
        if failure_injector:
            failure_injector("after_backup")
        _assert_directory_descriptor_matches_path(
            target.parent, target_parent_fd, label="target parent"
        )
        _assert_directory_descriptor_matches_path(
            stage_root.parent, stage_parent_fd, label="stage parent"
        )
        if _stat_at(target_parent_fd, target.name) is not None:
            raise CollisionError(f"update target appeared during publication: {target}")
        staged_info = _stat_at(stage_parent_fd, stage_name)
        if (
            staged_info is None
            or stat.S_ISLNK(staged_info.st_mode)
            or not stat.S_ISDIR(staged_info.st_mode)
            or not _same_file_identity(staged_info, stage_info)
        ):
            raise ScopeError(f"staged skill output is not a directory: {stage_root}")
        if _capture_tree_snapshot_at(stage_fd) != expected_stage_snapshot:
            raise ScopeError(f"staged skill output changed before install: {stage_root}")
        os.rename(
            stage_name,
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
        validate_installed(stage_fd)
        if failure_injector:
            failure_injector("after_install")
        publication_succeeded = True
    except BaseException as exc:
        if publication_succeeded:
            raise
        current_info = _stat_at(target_parent_fd, target.name)
        if (
            installed_target_info is not None
            and current_info is not None
            and _same_file_identity(current_info, installed_target_info)
        ):
            installed_cleanup_status = _remove_owned_directory_status(
                target_parent_fd,
                target.name,
                installed_target_info,
                expected_snapshot=expected_stage_snapshot,
                require_snapshot_match=True,
                compare_snapshot_contents=True,
            )
            current_info = _stat_at(target_parent_fd, target.name)
        if backup_info is not None and recovery_info is not None:
            backup_valid = _backup_matches_expected(
                target_parent_fd,
                backup_name,
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
                recovery_state, reported_recovery_path = _recovery_state(
                    recovery_parent_fd,
                    target.name,
                    recovery_info,
                    recovery_path,
                    expected_target_snapshot,
                )
                if recovery_state == "present":
                    recovery_cleanup = _remove_owned_directory_status(
                        recovery_parent_fd,
                        target.name,
                        recovery_info,
                        expected_snapshot=recovery_snapshot,
                        require_snapshot_match=True,
                    )
                    if recovery_cleanup in {"removed", "missing"}:
                        reported_recovery_path = None
                        recovery_state = "removed"
                    else:
                        preserve_recovery = True
                        recovery_state = "changed"
                if recovery_state == "removed":
                    recovery_status = "approved_tree_restored"
                else:
                    preserve_recovery = True
                    recovery_status = "recovery_changed_preserved"
                raise PublicationRecoveryError(
                    target=target,
                    recovery_path=reported_recovery_path,
                    cause=exc,
                    recovery_status=recovery_status,
                    cleanup_status="hidden_backup_removed",
                    competitor_preserved=False,
                ) from exc

            preserve_recovery = True
            if current_info is not None:
                cleanup_status = "hidden_backup_preserved"
                if backup_valid and _remove_owned_directory_status(
                    target_parent_fd,
                    backup_name,
                    backup_info,
                    expected_snapshot=expected_target_snapshot,
                    require_snapshot_match=True,
                ) in {"removed", "missing"}:
                    backup_info = None
                    cleanup_status = "hidden_backup_removed"
                    recovery_status = "approved_tree_recovered"
                else:
                    recovery_status = "backup_rejected_recovery_preserved"
                same_installed_tree = (
                    installed_target_info is not None
                    and installed_cleanup_status in {"changed", "preserved"}
                    and _same_file_identity(current_info, installed_target_info)
                )
                if same_installed_tree:
                    recovery_status = "installed_tree_changed_recovery_preserved"
                _, reported_recovery_path = _recovery_state(
                    recovery_parent_fd,
                    target.name,
                    recovery_info,
                    recovery_path,
                    expected_target_snapshot,
                )
                raise PublicationRecoveryError(
                    target=target,
                    recovery_path=reported_recovery_path,
                    cause=exc,
                    recovery_status=recovery_status,
                    cleanup_status=cleanup_status,
                    competitor_preserved=not same_installed_tree,
                ) from exc

            _, reported_recovery_path = _recovery_state(
                recovery_parent_fd,
                target.name,
                recovery_info,
                recovery_path,
                expected_target_snapshot,
            )
            raise PublicationRecoveryError(
                target=target,
                recovery_path=reported_recovery_path,
                cause=exc,
                recovery_status="backup_rejected_recovery_preserved",
                cleanup_status="hidden_backup_preserved",
                competitor_preserved=False,
            ) from exc
        raise
    else:
        backup_cleanup = "hidden_backup_removed"
        if backup_info is not None:
            if not _backup_matches_expected(
                target_parent_fd, backup_name, backup_info, expected_target_snapshot
            ):
                backup_cleanup = "hidden_backup_preserved"
            elif _remove_owned_directory_status(
                target_parent_fd,
                backup_name,
                backup_info,
                expected_snapshot=expected_target_snapshot,
                require_snapshot_match=True,
            ) in {"removed", "missing"}:
                backup_info = None
            else:
                backup_cleanup = "hidden_backup_preserved"
        recovery_state, reported_recovery_path = _recovery_state(
            recovery_parent_fd,
            target.name,
            recovery_info,
            recovery_path,
            expected_target_snapshot,
        )
        if backup_cleanup == "hidden_backup_removed" and recovery_state == "present":
            recovery_cleanup = _remove_owned_directory_status(
                recovery_parent_fd,
                target.name,
                recovery_info,
                expected_snapshot=recovery_snapshot,
                require_snapshot_match=True,
            )
            if recovery_cleanup in {"removed", "missing"}:
                recovery_state = "removed"
                reported_recovery_path = None
            else:
                recovery_state = "changed"
        if backup_cleanup != "hidden_backup_removed" or recovery_state != "removed":
            preserve_recovery = True
            if backup_cleanup == "hidden_backup_preserved":
                recovery_status = "rollback_backup_changed_recovery_preserved"
            else:
                recovery_status = "recovery_changed_preserved"
            raise PublicationRecoveryError(
                target=target,
                recovery_path=reported_recovery_path,
                cause=ScopeError("publication cleanup did not preserve a verified rollback state"),
                recovery_status=recovery_status,
                cleanup_status=backup_cleanup,
                competitor_preserved=False,
            )
    finally:
        if recovery_fd is not None:
            os.close(recovery_fd)
        if recovery_parent_fd is not None:
            if not preserve_recovery and recovery_info is not None:
                _remove_owned_directory(
                    recovery_parent_fd,
                    target.name,
                    recovery_info,
                    expected_snapshot=recovery_snapshot,
                    require_snapshot_match=True,
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
        if target_fd is not None:
            os.close(target_fd)


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

    with _workspace_write_lock(workspace) as root_fd:
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
        parent_fd: int | None = None
        existing_fd: int | None = None
        stage_parent_name: str | None = None
        stage_parent_fd: int | None = None
        stage_fd: int | None = None
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
            _assert_directory_descriptor_matches_path(
                target.parent, parent_fd, label="target parent"
            )
            target_name = canonical_target.relative_parts[-1]
            existing_info = _stat_at(parent_fd, target_name)
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
                    parent_fd, target_name, label="update target"
                )
                current_info = os.fstat(existing_fd)
                if not _same_file_state(existing_info, current_info):
                    raise ScopeError(f"update target changed while it was opened: {target}")
                existing_errors = _validate_skill_tree_at(
                    existing_fd, target, require_provenance=False
                )
                if existing_errors:
                    if any("symlink" in error for error in existing_errors):
                        raise ScopeError("existing skill tree contains a symlink output")
                    scaffold_errors = {
                        "frontmatter value must be a string: description"
                    }
                    if not (
                        _is_skill_creator_scaffold_at(existing_fd)
                        and set(existing_errors) <= scaffold_errors
                    ):
                        raise ValidationError(
                            "update requires a structurally valid existing skill: "
                            + "; ".join(existing_errors)
                        )
                expected_target_snapshot = _capture_tree_snapshot_at(existing_fd)

            stage_parent_name, stage_parent_fd, stage_root = _new_stage(parent_fd, target)
            stage_parent_info = os.fstat(stage_parent_fd)
            _assert_directory_descriptor_matches_path(
                stage_root.parent, stage_parent_fd, label="stage parent"
            )
            if operation == "create":
                try:
                    _run_skill_creator_initializer(stage_parent_fd, draft.name)
                except BaseException:
                    # The initializer can create the skill directory before a
                    # later scaffold write fails. Bind that directory only
                    # after confirming it appeared below our reserved stage
                    # parent; never adopt a target discovered during final
                    # cleanup after ownership was lost.
                    candidate_info = _stat_at(stage_parent_fd, target_name)
                    if (
                        candidate_info is not None
                        and stat.S_ISDIR(candidate_info.st_mode)
                        and not stat.S_ISLNK(candidate_info.st_mode)
                    ):
                        candidate_fd = _open_directory_at(
                            stage_parent_fd,
                            target_name,
                            label="staged skill output",
                        )
                        if not _same_file_identity(
                            candidate_info, os.fstat(candidate_fd)
                        ):
                            os.close(candidate_fd)
                        else:
                            expected_stage_info = candidate_info
                            stage_fd = candidate_fd
                    raise
            else:
                os.mkdir(target_name, 0o755, dir_fd=stage_parent_fd)
            expected_stage_info = _stat_at(stage_parent_fd, target_name)
            if (
                expected_stage_info is None
                or stat.S_ISLNK(expected_stage_info.st_mode)
                or not stat.S_ISDIR(expected_stage_info.st_mode)
            ):
                raise ScopeError(f"staged skill output is not an owned directory: {stage_root}")
            stage_fd = _open_directory_at(
                stage_parent_fd, target_name, label="staged skill output"
            )
            opened_stage_info = os.fstat(stage_fd)
            if not _same_file_identity(expected_stage_info, opened_stage_info):
                raise ScopeError(f"staged skill output changed while it was opened: {stage_root}")
            if operation == "update":
                if existing_fd is None or expected_target_snapshot is None:
                    raise ScopeError("update staging is missing the reserved target")
                copied_snapshot = _copy_tree_without_symlinks_at(existing_fd, stage_fd)
                if copied_snapshot != expected_target_snapshot:
                    raise ScopeError(f"update target changed while it was copied: {target}")
                _prepare_update_tree_at(stage_fd, stage_root, files, draft)
            else:
                _write_relative_files_at(stage_fd, files)
            errors = _validate_skill_tree_at(stage_fd, stage_root)
            if errors:
                raise ValidationError("generated skill failed validation: " + "; ".join(errors))
            expected_stage_snapshot = _capture_tree_snapshot_at(stage_fd)

            def validate_installed(installed_fd: int) -> None:
                installed_errors = _validate_skill_discoverability_at(
                    installed_fd, target
                )
                if installed_errors:
                    raise ValidationError(
                        "installed skill is not directly discoverable: "
                        + "; ".join(installed_errors)
                    )

            _install_staged_tree(
                stage_parent_fd,
                target_name,
                stage_fd,
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
                stage_parent_fd is not None
                and stage_fd is not None
                and expected_stage_info is not None
                and expected_stage_snapshot is None
            ):
                live_stage_info = _stat_at(stage_parent_fd, target.name)
                if (
                    live_stage_info is not None
                    and _same_file_identity(live_stage_info, expected_stage_info)
                ):
                    try:
                        expected_stage_snapshot = _capture_tree_snapshot_at(stage_fd)
                    except ScopeError:
                        # The final cleanup remains identity-bound; an
                        # unbounded or malformed partial tree is not adopted
                        # as a different operation's output.
                        expected_stage_snapshot = ()
            if stage_fd is not None:
                os.close(stage_fd)
            if existing_fd is not None:
                os.close(existing_fd)
            if stage_parent_fd is not None:
                os.close(stage_parent_fd)
            if (
                parent_fd is not None
                and stage_parent_name is not None
                and stage_parent_info is not None
            ):
                _cleanup_owned_stage(
                    parent_fd,
                    stage_parent_name,
                    stage_parent_info,
                    target.name,
                    expected_stage_info,
                    expected_stage_snapshot or (),
                )
            if parent_fd is not None:
                os.close(parent_fd)
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
