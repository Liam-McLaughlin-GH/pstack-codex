#!/usr/bin/env python3
"""Validate pstack skills, references, and the setup-pstack helpers offline."""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_SEAMS = (
    ("Cursor path", re.compile(r"(?:~/?\.cursor(?:/|$)|\.cursor/)", re.IGNORECASE)),
    (
        "Cursor Task call",
        re.compile(r"(?:\b(?:call|use|invoke)\s+(?:the\s+)?Cursor\s+Task\b|\bCursor\s+Task\s+(?:tool|call)\b)", re.IGNORECASE),
    ),
    ("subagent_type", re.compile(r"\bsubagent_type\s*[:=]", re.IGNORECASE)),
    ("background-agent setting", re.compile(r"\brun_in_background\s*[:=]", re.IGNORECASE)),
    ("transcript path", re.compile(r"\bagent-transcripts(?:/|\\)", re.IGNORECASE)),
    ("Cursor team kit", re.compile(r"\bcursor-team-kit(?:/|\\|$)", re.IGNORECASE)),
    (
        "AskQuestion call",
        re.compile(r"(?:\b(?:call|use|invoke)\s+AskQuestion\b|\bAskQuestion\s*\()", re.IGNORECASE),
    ),
    ("create-skill call", re.compile(r"(?:/create-skill\b|\bcreate-skill\s+(?:tool|command)\b)", re.IGNORECASE)),
)
BOUNDARY_ANNOTATION = re.compile(r"\bUNSUPPORTED[-_ ]SEAM\s*:\s*(?P<detail>[^\n]+)", re.IGNORECASE)
SEAM_ANNOTATION_ALIASES = {
    "Cursor path": ("cursor path", "cursor rules", "cursor target"),
    "Cursor Task call": ("cursor task",),
    "subagent_type": ("subagent_type",),
    "background-agent setting": ("background-agent", "run_in_background"),
    "transcript path": ("transcript path", "agent-transcripts"),
    "Cursor team kit": ("cursor team kit",),
    "AskQuestion call": ("askquestion",),
    "create-skill call": ("create-skill",),
}
TOP_LEVEL_FIELD = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(?P<value>.*))?$")
INLINE_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\((?P<body>[^)\n]*)\)")
REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?:<(?P<bracket>[^>]+)>|(?P<plain>\S+))(?:[ \t]+.*)?$"
)
REFERENCE_DEFINITION_PREFIX = re.compile(r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:")
REFERENCE_USE = re.compile(r"(?<!!)\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\]")
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"`[^`]*`")


class ValidationError(ValueError):
    """Raised when a skill document cannot be validated."""


@dataclass(frozen=True)
class Frontmatter:
    fields: dict[str, str]
    end_line: int


def _scalar(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"frontmatter field {field!r} is empty")
    if value[0] in {"'", '"'}:
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            raise ValidationError(f"frontmatter field {field!r} has an unterminated quote")
        if quote == "'":
            return value[1:-1].replace("''", "'")
        escaped = value[1:-1]
        decoded: list[str] = []
        index = 0
        simple_escapes = {
            "0": "\0",
            "a": "\a",
            "b": "\b",
            "t": "\t",
            "n": "\n",
            "v": "\v",
            "f": "\f",
            "r": "\r",
            "e": "\x1b",
            " ": " ",
            '"': '"',
            "/": "/",
            "\\": "\\",
            "N": "\u0085",
            "_": "\u00a0",
            "L": "\u2028",
            "P": "\u2029",
        }
        while index < len(escaped):
            character = escaped[index]
            if character != "\\":
                decoded.append(character)
                index += 1
                continue
            if index + 1 >= len(escaped):
                raise ValidationError(f"frontmatter field {field!r} has a dangling YAML escape")
            escape = escaped[index + 1]
            if escape in simple_escapes:
                decoded.append(simple_escapes[escape])
                index += 2
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(escape)
            if width is None:
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML escape: \\{escape}")
            digits = escaped[index + 2 : index + 2 + width]
            if len(digits) != width or not re.fullmatch(rf"[0-9A-Fa-f]{{{width}}}", digits):
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML escape: \\{escape}{digits}")
            try:
                decoded.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML code point") from exc
            index += 2 + width
        return "".join(decoded)
    if value.startswith(("[", "{", "&", "*", "!")):
        raise ValidationError(f"frontmatter field {field!r} is not a scalar")
    return value


def parse_frontmatter(text: str) -> Frontmatter:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValidationError("missing YAML frontmatter opening delimiter")
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        raise ValidationError("missing YAML frontmatter closing delimiter")

    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = TOP_LEVEL_FIELD.match(line)
        if not match:
            raise ValidationError(f"invalid frontmatter line: {line!r}")
        key = match.group("key")
        if key in fields:
            raise ValidationError(f"duplicate frontmatter field: {key}")
        fields[key] = _scalar(match.group("value") or "", key)
    for required in ("name", "description"):
        if required not in fields:
            raise ValidationError(f"frontmatter is missing {required}")
    return Frontmatter(fields, closing + 1)


def _reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _markdown_content_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    in_fence = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append((line_number, CODE_SPAN.sub("", line)))
    return lines


def _check_link_target(root: Path, path: Path, line_number: int, target: str, errors: list[str]) -> None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith("#"):
        return
    link_path = unquote(parsed.path)
    if not link_path:
        return
    candidate = (path.parent / link_path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        errors.append(f"{path.relative_to(root)}:{line_number} link escapes skill/repository scope: {target}")
    elif not candidate.exists():
        errors.append(f"{path.relative_to(root)}:{line_number} link target does not exist: {target}")


def _check_links(root: Path, path: Path, text: str, errors: list[str]) -> None:
    definitions: dict[str, str] = {}
    lines = _markdown_content_lines(text)
    for line_number, line in lines:
        prefix = REFERENCE_DEFINITION_PREFIX.match(line)
        if not prefix:
            continue
        definition = REFERENCE_DEFINITION.match(line)
        if not definition:
            errors.append(f"{path.relative_to(root)}:{line_number} has a malformed Markdown reference definition")
            continue
        label = _reference_label(prefix.group("label"))
        if label in definitions:
            errors.append(f"{path.relative_to(root)}:{line_number} repeats Markdown reference definition {label!r}")
            continue
        target = definition.groupdict().get("bracket") or definition.groupdict().get("plain")
        if target:
            definitions[label] = target
            _check_link_target(root, path, line_number, target, errors)

    for line_number, line in lines:
        for match in INLINE_LINK.finditer(line):
            body = match.group("body").strip()
            if not body:
                errors.append(f"{path.relative_to(root)}:{line_number} has a malformed Markdown link with an empty destination")
                continue
            if body.startswith("<"):
                end = body.find(">")
                if end <= 1:
                    errors.append(f"{path.relative_to(root)}:{line_number} has a malformed Markdown link destination")
                    continue
                target = body[1:end]
            else:
                target = body.split(None, 1)[0]
            _check_link_target(root, path, line_number, target, errors)
        for match in REFERENCE_USE.finditer(line):
            label = _reference_label(match.group("label") or match.group("text"))
            if label not in definitions:
                errors.append(f"{path.relative_to(root)}:{line_number} unresolved Markdown reference: {label!r}")


def _check_boundary_mentions(root: Path, path: Path, text: str, errors: list[str]) -> None:
    lines = text.splitlines()
    for line_number, line in enumerate(lines):
        for marker, pattern in ACTIVE_SEAMS:
            if not pattern.search(line):
                continue
            annotations = [match.group("detail").casefold() for match in BOUNDARY_ANNOTATION.finditer(line)]
            aliases = SEAM_ANNOTATION_ALIASES[marker]
            if not any(alias in detail for detail in annotations for alias in aliases):
                errors.append(
                    f"{path.relative_to(root)}:{line_number + 1} contains an active unsupported seam {marker!r}; "
                    "add an explicit same-line UNSUPPORTED-SEAM annotation or use a Codex surface"
                )


def _load_skill_helper(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot import helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_helper_checks(root: Path, errors: list[str]) -> None:
    helper_dir = root / "skills" / "setup-pstack" / "scripts"
    expected = (helper_dir / "install.py", helper_dir / "configure.py")
    for helper in expected:
        if not helper.is_file():
            errors.append(f"missing setup-pstack helper: {helper.relative_to(root)}")
            continue
        try:
            _load_skill_helper(helper)
        except Exception as exc:  # pragma: no cover - the error is reported to the caller
            errors.append(f"could not import {helper.relative_to(root)}: {exc}")
    for helper in expected:
        if not helper.is_file():
            continue
        self_test_args = ["--self-test"] if helper.name == "install.py" else ["self-test"]
        try:
            completed = subprocess.run(
                [sys.executable, str(helper), *self_test_args],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{helper.relative_to(root)} self-check could not run: {exc}")
            continue
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            errors.append(f"{helper.relative_to(root)} self-check failed: {detail}")


def verify_port(root: Path, *, run_helpers: bool = True) -> list[str]:
    root = root.resolve()
    skills = root / "skills"
    errors: list[str] = []
    if not skills.is_dir():
        return ["skills/ is missing"]

    skill_dirs = sorted(path for path in skills.iterdir() if path.is_dir() and not path.is_symlink())
    if not skill_dirs:
        errors.append("skills/ is empty")

    names: set[str] = set()
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        relative = skill_file.relative_to(root)
        if not skill_file.is_file():
            errors.append(f"{skill_dir.relative_to(root)} is missing SKILL.md")
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
            metadata = parse_frontmatter(text)
            name = metadata.fields["name"]
            if name != skill_dir.name:
                errors.append(f"{relative} name {name!r} does not match directory {skill_dir.name!r}")
            if not re.fullmatch(r"[a-z0-9-]+", name):
                errors.append(f"{relative} has an invalid skill name: {name!r}")
            if "TODO" in metadata.fields["description"]:
                errors.append(f"{relative} still contains a TODO description")
            names.add(name)
        except (OSError, ValidationError) as exc:
            errors.append(f"{relative} has invalid frontmatter: {exc}")
            text = ""

        for document in sorted(path for path in skill_dir.rglob("*") if path.is_file() and not path.is_symlink()):
            try:
                raw = document.read_bytes()
                if b"\x00" in raw:
                    continue
                document_text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(f"{document.relative_to(root)} cannot be read: {exc}")
                continue
            if document.suffix.lower() == ".md":
                _check_links(root, document, document_text, errors)
            _check_boundary_mentions(root, document, document_text, errors)

    if len(names) != len(skill_dirs):
        errors.append("active skill names are not unique")
    if run_helpers and not errors:
        _run_helper_checks(root, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="pstack repository root")
    parser.add_argument("--skip-helper-checks", action="store_true", help="skip the setup-pstack temporary self-checks")
    args = parser.parse_args(argv)
    errors = verify_port(args.root, run_helpers=not args.skip_helper_checks)
    if errors:
        print("Port verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    skill_count = len([path for path in (args.root / "skills").iterdir() if path.is_dir() and not path.is_symlink()])
    print(f"Verified {skill_count} active Codex skills.")
    print("Validated frontmatter and local Markdown references.")
    print("Validated unsupported-boundary wording without banning documentation mentions.")
    if not args.skip_helper_checks:
        print("Ran setup-pstack installer and configuration self-checks in temporary directories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
