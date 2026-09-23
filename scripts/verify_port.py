#!/usr/bin/env python3
"""Validate pstack skills, references, and the setup-pstack helpers offline."""

from __future__ import annotations

import argparse
import io
import importlib.util
import re
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_SEAMS = (
    ("Cursor path", re.compile(r"(?:~/?\.cursor(?:/|$)|\.cursor/)", re.IGNORECASE)),
    (
        "Cursor Task call",
        re.compile(
            r"(?:\b(?:call|use|invoke)\s+(?:the\s+)?Cursor\s+Task\b"
            r"|\bCursor\s+Task\s+(?:tool|call)\b"
            r"|\bTask\s*\()",
            re.IGNORECASE,
        ),
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
EXPLICIT_CONTINUATION = r"\\[ \t]*\r?\n[ \t]*"
IMPORT_SEPARATOR = rf"[ \t]+(?:{EXPLICIT_CONTINUATION})?"
BOUNDARY_ANNOTATION = re.compile(r"\bUNSUPPORTED[-_ ]SEAM\s*:\s*(?P<detail>[^\n]+)", re.IGNORECASE)
SEAM_ANNOTATION_ALIASES = {
    "Cursor path": ("cursor path", "cursor rules", "cursor target"),
    "Cursor Task call": ("cursor task", "task call", "task(", "cursor_tools"),
    "subagent_type": ("subagent_type",),
    "background-agent setting": ("background-agent", "run_in_background"),
    "transcript path": ("transcript path", "agent-transcripts"),
    "Cursor team kit": ("cursor team kit",),
    "AskQuestion call": ("askquestion",),
    "create-skill call": ("create-skill",),
}
TOP_LEVEL_FIELD = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(?P<value>.*))?$")
INLINE_LINK_START = re.compile(r"(?<!!)\[[^\]\n]*\]\(")
REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?:<(?P<bracket>[^>\n]+)>|(?P<plain>\S+))[ \t]*$"
)
REFERENCE_DEFINITION_PREFIX = re.compile(r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:")
REFERENCE_USE = re.compile(r"(?<!!)\[(?P<text>[^\]]+)\]\[(?P<label>[^\]]*)\]")
FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"`[^`]*`")


def _mask_comments(text: str) -> str:
    """Keep import spans stable while removing Python and C-style comments."""

    characters = list(text)
    line_offsets = [0]
    for index, character in enumerate(text):
        if character == "\n":
            line_offsets.append(index + 1)

    def offset(position: tuple[int, int]) -> int:
        line, column = position
        if line <= 0 or line > len(line_offsets):
            return len(text)
        return min(line_offsets[line - 1] + column, len(text))

    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            start = offset(token.start)
            end = offset(token.end)
            for index in range(start, end):
                if characters[index] not in "\r\n":
                    characters[index] = " "
    except (IndentationError, SyntaxError, tokenize.TokenError):
        pass

    for line_start, line in enumerate("".join(characters).splitlines(keepends=True)):
        quote: str | None = None
        escaped = False
        for column, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\" and quote is not None:
                escaped = True
                continue
            if character in {"'", '"'}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                continue
            if quote is None and line[column : column + 2] == "//":
                start = sum(len(item) for item in "".join(characters).splitlines(keepends=True)[:line_start]) + column
                for index in range(start, start + len(line.rstrip("\r\n")) - column):
                    characters[index] = " "
                break
    return "".join(characters)


def _skip_import_whitespace(text: str, position: int) -> int:
    while position < len(text):
        if text[position] in " \t\r\n":
            position += 1
            continue
        continuation = re.match(EXPLICIT_CONTINUATION, text[position:])
        if continuation is not None:
            position += continuation.end()
            continue
        break
    return position


def _cursor_task_import_spans(text: str) -> list[tuple[int, int]]:
    """Find imports whose imported symbol is actually named ``Task``."""

    comment_free_text = _mask_comments(text)
    import_head = re.compile(
        rf"\bfrom{IMPORT_SEPARATOR}cursor_tools{IMPORT_SEPARATOR}import\b",
        re.IGNORECASE,
    )
    spans: list[tuple[int, int]] = []
    for match in import_head.finditer(comment_free_text):
        position = _skip_import_whitespace(comment_free_text, match.end())
        if position >= len(comment_free_text):
            continue

        if comment_free_text[position] == "(":
            depth = 0
            end = position
            while end < len(comment_free_text):
                character = comment_free_text[end]
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            body = comment_free_text[position + 1 : end]
            statement_end = min(end + 1, len(comment_free_text))
        else:
            end = position
            while end < len(comment_free_text):
                newline = comment_free_text.find("\n", end)
                if newline == -1:
                    end = len(comment_free_text)
                    break
                line = comment_free_text[end:newline]
                if line.rstrip().endswith("\\"):
                    end = newline + 1
                    continue
                end = newline
                break
            body = comment_free_text[position:end]
            statement_end = end

        imported_task = False
        for item in body.replace("\\\n", " ").split(","):
            symbol = re.match(r"\s*([A-Za-z_]\w*)", item)
            if symbol is not None and symbol.group(1) == "Task":
                imported_task = True
                break
        if imported_task:
            spans.append((match.start(), statement_end))
    return spans


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
    if value[0] == "'":
        decoded: list[str] = []
        index = 1
        while index < len(value):
            character = value[index]
            if character != "'":
                decoded.append(character)
                index += 1
                continue
            if index + 1 < len(value) and value[index + 1] == "'":
                decoded.append("'")
                index += 2
                continue
            if index != len(value) - 1:
                raise ValidationError(f"frontmatter field {field!r} has trailing text after its quote")
            result = "".join(decoded)
            if not result:
                raise ValidationError(f"frontmatter field {field!r} is empty")
            return result
        raise ValidationError(f"frontmatter field {field!r} has an unterminated quote")

    if value[0] == '"':
        decoded = []
        index = 1
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
        while index < len(value):
            character = value[index]
            if character == '"':
                if index != len(value) - 1:
                    raise ValidationError(f"frontmatter field {field!r} has trailing text after its quote")
                result = "".join(decoded)
                if not result:
                    raise ValidationError(f"frontmatter field {field!r} is empty")
                return result
            if character != "\\":
                decoded.append(character)
                index += 1
                continue
            if index + 1 >= len(value):
                raise ValidationError(f"frontmatter field {field!r} has a dangling YAML escape")
            escape = value[index + 1]
            if escape in simple_escapes:
                decoded.append(simple_escapes[escape])
                index += 2
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(escape)
            if width is None:
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML escape: \\{escape}")
            digits = value[index + 2 : index + 2 + width]
            if len(digits) != width or not re.fullmatch(rf"[0-9A-Fa-f]{{{width}}}", digits):
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML escape: \\{escape}{digits}")
            try:
                decoded.append(chr(int(digits, 16)))
            except ValueError as exc:
                raise ValidationError(f"frontmatter field {field!r} has invalid YAML code point") from exc
            index += 2 + width
        raise ValidationError(f"frontmatter field {field!r} has an unterminated quote")

    explicit_string_tag = re.match(
        r"^(?:!!str|!<tag:yaml\.org,2002:str>)(?:[ \t]+(?P<payload>.*))?$",
        value,
        re.IGNORECASE,
    )
    if explicit_string_tag is not None:
        payload = (explicit_string_tag.group("payload") or "").strip()
        if not payload:
            raise ValidationError(f"frontmatter field {field!r} is empty")
        return payload
    if value.startswith(("[", "{", "&", "*", "!", "|", ">", "%", "@", "`", "#")):
        raise ValidationError(f"frontmatter field {field!r} is not a supported scalar")
    if re.search(r":(?:[ \t]|$)", value):
        raise ValidationError(f"frontmatter field {field!r} has unsupported mapping syntax")
    if value.casefold() in {"null", "~", "true", "false", "yes", "no", "on", "off", "y", "n"}:
        raise ValidationError(f"frontmatter field {field!r} is not a string scalar")
    if re.fullmatch(r"[-+]?(?:0|[1-9][0-9_]*)(?:\.[0-9_]*)?(?:[eE][-+]?[0-9_]+)?", value):
        raise ValidationError(f"frontmatter field {field!r} is not a string scalar")
    if re.fullmatch(r"[-+]?\.[0-9_]+|[-+]?0_[0-9_]+|[-+]?\.(?:inf|nan)", value, re.IGNORECASE):
        raise ValidationError(f"frontmatter field {field!r} is not a string scalar")
    if re.fullmatch(r"[-+]?0x[0-9A-Fa-f_]+|[-+]?0o[0-7_]+|[-+]?0b[01_]+", value):
        raise ValidationError(f"frontmatter field {field!r} is not a string scalar")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValidationError(f"frontmatter field {field!r} is not a string scalar")
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


def required_frontmatter_errors(metadata: str) -> list[str]:
    """Keep the small validation helper importable for skill-level tests."""

    try:
        parse_frontmatter(f"---\n{metadata.rstrip()}\n---\n")
    except ValidationError as exc:
        return [str(exc)]
    return []


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
        for match in INLINE_LINK_START.finditer(line):
            body_start = match.end()
            close = line.find(")", body_start)
            if close == -1:
                errors.append(f"{path.relative_to(root)}:{line_number} has an unterminated Markdown link")
                continue
            body = line[body_start:close].strip()
            if not body:
                errors.append(f"{path.relative_to(root)}:{line_number} has a malformed Markdown link with an empty destination")
                continue
            if body.startswith("<"):
                bracketed = re.fullmatch(r"<([^>\n]+)>", body)
                if bracketed is None:
                    errors.append(f"{path.relative_to(root)}:{line_number} has a malformed Markdown link destination")
                    continue
                target = bracketed.group(1)
            else:
                if re.search(r"\s|[()]", body):
                    errors.append(f"{path.relative_to(root)}:{line_number} has trailing or malformed Markdown link data")
                    continue
                target = body
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

    for start, end in _cursor_task_import_spans(text):
        closing_line_end = text.find("\n", end)
        if closing_line_end == -1:
            closing_line_end = len(text)
        original_match = text[start:closing_line_end]
        annotations = [
            annotation.group("detail").casefold()
            for annotation in BOUNDARY_ANNOTATION.finditer(original_match)
        ]
        if any(alias in detail for detail in annotations for alias in SEAM_ANNOTATION_ALIASES["Cursor Task call"]):
            continue
        line_number = text.count("\n", 0, start) + 1
        errors.append(
            f"{path.relative_to(root)}:{line_number} contains an active unsupported seam 'Cursor Task call'; "
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
