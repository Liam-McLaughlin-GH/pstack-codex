#!/usr/bin/env python3
"""Deterministic safety gates for the Codex-native automate-me workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import unquote, urlparse


UPSTREAM_COMMIT = "53e579f1481697931fc44f5445171397cfa2b24b"
UPSTREAM_COPYRIGHT = "Copyright (c) 2026 Lauren Tan"
MAX_ARTIFACT_BYTES = 1_000_000
ARTIFACT_KINDS = frozenset({"skill", "script", "reference", "none"})
OPERATIONS = frozenset({"create", "update", "confirm", "none"})
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MODE_RE = re.compile(r"(?<![a-z0-9-])([a-z][a-z0-9-]{0,62})-mode(?![a-z0-9-])")
NAMED_RE = re.compile(
    r"\b(?:named|called)\s+([a-z][a-z0-9-]{0,62})(?![a-z0-9-])"
)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


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

    def public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "line_count": self.line_count,
            "size_bytes": self.size_bytes,
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


def _has_any(text: str, words: Iterable[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _operation_for(text: str) -> str:
    wants_create = _has_any(
        text, ("capture", "create", "new", "fresh", "record", "save", "start")
    )
    wants_update = _has_any(text, ("update", "refresh", "revise", "edit")) or (
        "existing" in text
    )
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

    mode_match = MODE_RE.search(text)
    personal_signal = (
        mode_match is not None
        or "automate me" in text
        or ("working style" in text and "skill" in text)
        or ("mode skill" in text and ("personal" in text or "my" in text))
    )
    if personal_signal:
        operation = _operation_for(text)
        handle = mode_match.group(1) if mode_match else None
        target = f"skills/{handle}-mode/SKILL.md" if handle else None
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
        text,
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
        slug = _slug_from_request(text, "preferences")
        return RouteDecision(
            artifact="reference",
            category="durable-preference",
            operation=operation,
            target=f"references/{slug}.md",
            reason="durable preferences need an approved reference destination",
            confirmation_required=True,
        )

    script_signal = _has_any(text, ("script", "helper", "command")) and (
        "project-local" in text or "repository" in text or "repo" in text
    )
    if script_signal:
        operation = _operation_for(text)
        slug = _slug_from_request(text, "project-helper")
        return RouteDecision(
            artifact="script",
            category="project-helper",
            operation=operation,
            target=f"scripts/{slug}.py",
            reason="an explicitly requested deterministic helper belongs in a script",
        )

    project_skill_signal = (
        "verification skill" in text
        or "task skill" in text
        or ("project-local" in text and "skill" in text)
    )
    if project_skill_signal:
        operation = _operation_for(text)
        default = "project-verification" if "verification" in text else "project-task"
        slug = _slug_from_request(text, default)
        return RouteDecision(
            artifact="skill",
            category="project-skill",
            operation=operation,
            target=f"skills/{slug}/SKILL.md",
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


def _scoped_path(root: Path, candidate: Path | str) -> Path:
    raw = Path(candidate)
    if not raw.is_absolute():
        raw = root / raw
    resolved = raw.resolve(strict=False)
    if resolved == root or root not in resolved.parents:
        raise ScopeError(f"path escapes the active workspace: {candidate}")
    return resolved


def read_allowlisted_artifacts(
    workspace_root: Path | str,
    artifact_paths: Sequence[Path | str],
    *,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> tuple[ArtifactRecord, ...]:
    """Read only explicit regular files inside the active workspace."""

    root = _resolved_root(workspace_root)
    if not artifact_paths:
        raise ScopeError("an explicit artifact allowlist is required")

    records: list[ArtifactRecord] = []
    for artifact_path in artifact_paths:
        resolved = _scoped_path(root, artifact_path)
        if not resolved.is_file():
            raise ScopeError(f"allowlisted artifact is not a regular file: {artifact_path}")
        size = resolved.stat().st_size
        if size > max_bytes:
            raise ScopeError(f"allowlisted artifact is too large: {artifact_path}")
        text = resolved.read_text(encoding="utf-8")
        records.append(
            ArtifactRecord(
                path=resolved.relative_to(root).as_posix(),
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                line_count=len(text.splitlines()),
                size_bytes=size,
            )
        )
    return tuple(records)


def confirmed_signals(
    records: Sequence[ArtifactRecord], signals: Sequence[str]
) -> tuple[str, ...]:
    """Return signals present in at least two distinct explicit records."""

    if len(records) < 2:
        raise ScopeError("cross-slice confirmation needs at least two records")
    confirmed: list[str] = []
    for signal in signals:
        needle = signal.strip().casefold()
        if not needle:
            continue
        hits = sum(needle in record.text.casefold() for record in records)
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
    if target.startswith("#") or parsed.scheme or parsed.netloc:
        return None
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

    root = Path(skill_dir).absolute().resolve()
    errors: list[str] = []
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        return ("SKILL.md not found",)

    text = skill_md.read_text(encoding="utf-8")
    try:
        frontmatter = _parse_frontmatter(text)
        name = frontmatter.get("name", "")
        _validate_skill_name(name)
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
        if not target.is_file():
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


def _write_relative_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        destination = _scoped_path(root, relative)
        if destination.parent != root:
            destination.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomically(destination, content)


def write_generated_skill(
    skill_dir: Path | str,
    draft: SkillDraft,
    *,
    workspace_root: Path | str,
    operation: str,
) -> WriteResult:
    """Write a generated skill with an explicit create/update collision gate."""

    if operation not in {"create", "update"}:
        raise AutomateMeError("write operation must be explicitly create or update")
    workspace = _resolved_root(workspace_root)
    target = _scoped_path(workspace, skill_dir)
    files = render_skill_files(draft)

    if operation == "create":
        if os.path.lexists(target):
            raise CollisionError(f"create refused existing output: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
    else:
        if not target.is_dir():
            raise ScopeError(f"update target is not an existing directory: {target}")
        existing_errors = validate_skill_tree(target, require_provenance=False)
        if existing_errors:
            raise ValidationError(
                "update requires a structurally valid existing skill: "
                + "; ".join(existing_errors)
            )

    _write_relative_files(target, files)
    errors = validate_skill_tree(target)
    if errors:
        raise ValidationError("generated skill failed validation: " + "; ".join(errors))
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
    result = write_generated_skill(
        args.skill_dir,
        draft,
        workspace_root=args.workspace_root,
        operation=args.operation,
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
