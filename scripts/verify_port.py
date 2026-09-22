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
LOCAL_SEAMS = (
    "~/.cursor",
    ".cursor/",
    "subagent_type",
    "run_in_background",
    "agent-transcripts",
    "cursor-team-kit",
    "AskQuestion",
    "create-skill",
)
BOUNDARY_WORDS = re.compile(
    r"\b(?:unsupported|not supported|does not|do not|don't|never|cannot|can't|unavailable|fallback|boundary|not ported|not copied)\b",
    re.IGNORECASE,
)
TOP_LEVEL_FIELD = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(?P<value>.*))?$")
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\((?:<(?P<bracket>[^>]+)>|(?P<plain>[^)\s]+))[^)]*\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?:<(?P<bracket>[^>]+)>|(?P<plain>\S+))\s*$")


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
        return value[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
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
        if not line.strip() or line.lstrip().startswith("#") or line[:1].isspace():
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


def _iter_local_links(path: Path, text: str):
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in MARKDOWN_LINK.finditer(line):
            target = match.groupdict().get("bracket") or match.groupdict().get("plain")
            if target:
                yield line_number, target
        match = REFERENCE_LINK.match(line)
        if match:
            target = match.groupdict().get("bracket") or match.groupdict().get("plain")
            if target:
                yield line_number, target


def _check_links(root: Path, path: Path, text: str, errors: list[str]) -> None:
    for line_number, target in _iter_local_links(path, text):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith("#"):
            continue
        link_path = unquote(parsed.path)
        if not link_path:
            continue
        candidate = (path.parent / link_path).resolve()
        if not candidate.is_relative_to(root.resolve()):
            errors.append(f"{path.relative_to(root)}:{line_number} link escapes skill/repository scope: {target}")
        elif not candidate.exists():
            errors.append(f"{path.relative_to(root)}:{line_number} link target does not exist: {target}")


def _check_boundary_mentions(root: Path, path: Path, text: str, errors: list[str]) -> None:
    lines = text.splitlines()
    for line_number, line in enumerate(lines):
        for marker in LOCAL_SEAMS:
            if marker.lower() not in line.lower():
                continue
            context = "\n".join(lines[max(0, line_number - 2) : min(len(lines), line_number + 3)])
            if not BOUNDARY_WORDS.search(context):
                errors.append(
                    f"{path.relative_to(root)}:{line_number + 1} contains an active unsupported seam {marker!r}; "
                    "document the boundary or use a Codex surface"
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

        for document in sorted(skill_dir.rglob("*.md")):
            try:
                document_text = document.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{document.relative_to(root)} cannot be read: {exc}")
                continue
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
