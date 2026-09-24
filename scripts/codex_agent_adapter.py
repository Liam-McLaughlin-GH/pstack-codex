#!/usr/bin/env python3
"""Validate and select the pstack Codex roles without starting a child."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.dont_write_bytecode = True


COMMENT_SICKO_FIRST_OUTPUT = "Yes... Ha ha ha... Yes!"
COMMENT_SICKO_REPORT_FIELDS = (
    "Touched files:",
    "Deletion count:",
    "MUST KILL:",
    "Skips:",
)
COMMENT_SICKO_REQUIRED_INSTRUCTIONS = (
    "Your first output must be exactly this single line:",
    COMMENT_SICKO_FIRST_OUTPUT,
    *COMMENT_SICKO_REPORT_FIELDS,
)
VALID_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
DIRECT_SKILL_FALLBACKS = {
    "poteto-agent": "poteto-mode",
    "Comment Sicko": "no-comments",
}
EXPECTED_ROLE_NAMES = {"poteto-agent", "Comment Sicko"}
EXPECTED_ROLE_DESCRIPTIONS = {
    "poteto-agent": "Routing target for /poteto-mode and requests for poteto's style.",
    "Comment Sicko": "A deranged comment-hater that savors deletion and condemns workaround code.",
}
EXPECTED_ROLE_SANDBOX = {"poteto-agent": None, "Comment Sicko": "read-only"}


class AdapterError(ValueError):
    """Raised when a role or model cannot be used safely."""


class UnknownRoleError(AdapterError):
    """Raised when a requested role is not explicitly registered."""


class ModelUnavailableError(AdapterError):
    """Raised when no available model satisfies the requested policy."""


class ReportContractError(AdapterError):
    """Raised when a Comment Sicko response is not report-shaped."""


@dataclass(frozen=True)
class RoleDefinition:
    path: Path
    name: str
    description: str
    developer_instructions: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class ModelSelection:
    requested_model: str | None
    effective_model: str | None
    fallback_applied: bool
    fallback_model: str | None
    reason: str
    available_models: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _non_empty_string(value: object, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{path}: {field} must be a non-empty string")
    return value.strip()


def _visible_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(
        character
        for character in normalized
        if character.isprintable() and unicodedata.category(character) not in {"Cf", "Mn"}
    )


def parse_role_file(path: Path) -> RoleDefinition:
    """Parse one current-format Codex role file and validate its required fields."""

    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise AdapterError(f"role file does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AdapterError(f"role file is not valid TOML: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AdapterError(f"role file must contain a TOML table: {path}")
    name = _non_empty_string(raw.get("name"), "name", path)
    description = _non_empty_string(raw.get("description"), "description", path)
    developer_instructions = _non_empty_string(
        raw.get("developer_instructions"),
        "developer_instructions",
        path,
    )
    settings = {
        key: value
        for key, value in raw.items()
        if key not in {"name", "description", "developer_instructions"}
    }
    role = RoleDefinition(
        path=path.resolve(),
        name=name,
        description=description,
        developer_instructions=developer_instructions,
        settings=settings,
    )
    canonical_instructions = unicodedata.normalize("NFKC", role.developer_instructions)
    canonical_instructions = "".join(
        character
        for character in canonical_instructions
        if character in {"\r", "\n"}
        or (character.isprintable() and unicodedata.category(character) not in {"Cf", "Mn"})
    )
    if canonical_instructions != role.developer_instructions or any(
        ord(character) > 127 for character in role.developer_instructions
    ):
        raise AdapterError(f"{path}: developer_instructions contain hidden or non-canonical text")
    sandbox_mode = role.settings.get("sandbox_mode")
    if sandbox_mode is not None and (
        not isinstance(sandbox_mode, str) or sandbox_mode not in VALID_SANDBOX_MODES
    ):
        raise AdapterError(
            f"{path}: sandbox_mode must be one of {', '.join(sorted(VALID_SANDBOX_MODES))}"
        )
    expected_sandbox = EXPECTED_ROLE_SANDBOX.get(role.name)
    if sandbox_mode != expected_sandbox:
        raise AdapterError(
            f"{path}: sandbox_mode does not match the shipped {role.name} contract"
        )
    if role.name == "Comment Sicko":
        if sandbox_mode != "read-only":
            raise AdapterError(f"{path}: Comment Sicko requires sandbox_mode = 'read-only'")
        instruction_lines = [line.strip() for line in role.developer_instructions.splitlines()]
        expected_prefix = [
            "Your first output must be exactly this single line:",
            COMMENT_SICKO_FIRST_OUTPUT,
        ]
        if instruction_lines[:2] != expected_prefix:
            raise AdapterError(
                f"{path}: Comment Sicko must begin with its exact first-output contract"
            )
        missing = [
            required
            for required in COMMENT_SICKO_REQUIRED_INSTRUCTIONS
            if required not in instruction_lines
        ]
        if not any(
            "Do not write application code." in line
            for line in instruction_lines
        ):
            missing.append("Do not write application code.")
        if missing:
            raise AdapterError(
                f"{path}: Comment Sicko is missing required report-only instructions: "
                + "; ".join(missing)
            )
        if any(ord(character) > 127 for character in role.developer_instructions):
            raise AdapterError(f"{path}: Comment Sicko instructions must use canonical visible text")
        unsafe_pattern = (
            r"\b(?:writ\w*|rewrit\w*|overwrit\w*|edit\w*|modif\w*|creat\w*|delet\w*|remov\w*|chang\w*|mutat\w*|appl\w*|updat\w*|sav\w*|persist\w*|export\w*)\b"
            r".{0,120}\b(?:application code|source code|code|files?|repository|repo|data|patch|directory|output|disk)\b"
            r"|\b(?:application code|source code|code|files?|repository|repo|data|patch|directory|output|disk)\b.{0,120}"
            r"\b(?:writ\w*|rewrit\w*|overwrit\w*|edit\w*|modif\w*|creat\w*|delet\w*|remov\w*|chang\w*|mutat\w*|appl\w*|updat\w*|sav\w*|persist\w*|export\w*)\b"
        )
        scan_lines: list[str] = []
        for line in instruction_lines:
            content = line
            for field in COMMENT_SICKO_REPORT_FIELDS:
                if content == field:
                    content = ""
                    break
                if content.startswith(field):
                    content = content[len(field) :].lstrip()
                    break
            scan_lines.append(content)
        normalized_instructions = re.sub(
            r"\s+",
            " ",
            _visible_text(" ".join(scan_lines)).replace(
                "Do not write application code.", ""
            ),
        )
        unsafe_match = re.search(unsafe_pattern, normalized_instructions, re.IGNORECASE)
        if unsafe_match:
            raise AdapterError(
                f"{path}: Comment Sicko contains write-capable instructions: "
                + unsafe_match.group(0)
            )
    expected_description = EXPECTED_ROLE_DESCRIPTIONS.get(role.name)
    if expected_description is not None and role.description != expected_description:
        raise AdapterError(f"{path}: {role.name} description does not match the shipped role contract")
    return role


def _load_toml_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise AdapterError(f"configuration file does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AdapterError(f"configuration file is not valid TOML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterError(f"configuration file must contain a TOML table: {path}")
    return raw


def load_explicit_roles(project_root: Path) -> dict[str, RoleDefinition]:
    """Load only roles named by the project's explicit ``[agents]`` registry."""

    project_root = project_root.resolve()
    codex_path = project_root / ".codex"
    if codex_path.is_symlink() or not codex_path.is_dir():
        raise AdapterError(f"{codex_path}: explicit .codex directory must be real")
    config_path = codex_path / "config.toml"
    if config_path.is_symlink() or not config_path.is_file():
        raise AdapterError(f"{config_path}: explicit config must be a real file")
    raw = _load_toml_table(config_path)
    agents = raw.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise AdapterError(f"{config_path}: expected a non-empty [agents] registry")
    if set(agents) != EXPECTED_ROLE_NAMES:
        missing = ", ".join(sorted(EXPECTED_ROLE_NAMES - set(agents))) or "(none)"
        extra = ", ".join(sorted(set(agents) - EXPECTED_ROLE_NAMES)) or "(none)"
        raise AdapterError(
            f"{config_path}: explicit registry must contain exactly the shipped roles; "
            f"missing={missing}; extra={extra}"
        )

    codex_root = codex_path
    roles: dict[str, RoleDefinition] = {}
    for declared_name, declaration in agents.items():
        if not isinstance(declared_name, str) or not declared_name.strip():
            raise AdapterError(f"{config_path}: agent names must be non-empty strings")
        if declared_name not in EXPECTED_ROLE_NAMES:
            raise AdapterError(f"{config_path}: unsupported registered role {declared_name!r}")
        if not isinstance(declaration, dict):
            raise AdapterError(f"{config_path}: agents.{declared_name} must be a table")
        description = _non_empty_string(
            declaration.get("description"), "description", config_path
        )
        config_file = _non_empty_string(
            declaration.get("config_file"),
            "config_file",
            config_path,
        )
        if Path(config_file).is_absolute():
            raise AdapterError(f"{config_path}: role config_file must be relative: {config_file}")
        declared_role_path = config_path.parent / config_file
        current = declared_role_path
        while current != codex_root:
            if current == current.parent:
                raise AdapterError(f"{config_path}: role path escapes .codex: {config_file}")
            if current.is_symlink():
                raise AdapterError(f"{config_path}: role path contains a symlink: {config_file}")
            current = current.parent
        role_path = declared_role_path.resolve()
        if not role_path.is_relative_to(codex_root):
            raise AdapterError(f"{config_path}: role file escapes .codex: {config_file}")
        role = parse_role_file(role_path)
        if role.name != declared_name:
            raise AdapterError(
                f"{config_path}: declared role {declared_name!r} resolves to {role.name!r}"
            )
        if description is not None and role.description != description:
            raise AdapterError(
                f"{config_path}: description for {declared_name!r} does not match its role file"
            )
        if role.name in roles:
            raise AdapterError(f"{config_path}: duplicate role name {role.name!r}")
        roles[role.name] = role
    return roles


def resolve_role(project_root: Path, role_name: str) -> RoleDefinition:
    """Resolve an exact role name without replacing it with a generic worker."""

    roles = load_explicit_roles(project_root)
    try:
        return roles[role_name]
    except KeyError as exc:
        fallback = DIRECT_SKILL_FALLBACKS.get(role_name)
        fallback_text = (
            f"use the direct ${fallback} skill explicitly"
            if fallback is not None
            else "select a role explicitly"
        )
        raise UnknownRoleError(
            f"role {role_name!r} is not explicitly registered; {fallback_text}; "
            "no generic role was selected"
        ) from exc


def parse_model_catalog(payload: str | bytes | Mapping[str, Any]) -> tuple[str, ...]:
    """Extract stable model slugs from ``codex debug models`` JSON."""

    if isinstance(payload, (str, bytes)):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"model catalog is not valid JSON: {exc}") from exc
    else:
        decoded = payload
    if not isinstance(decoded, Mapping):
        raise AdapterError("model catalog must be a JSON object")
    entries = decoded.get("models")
    if not isinstance(entries, list):
        raise AdapterError("model catalog must contain a models list")
    slugs: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AdapterError("model catalog entries must be objects")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise AdapterError("model catalog entries must have a non-empty slug")
        slugs.add(slug.strip())
    return tuple(sorted(slugs))


def read_model_catalog(codex_binary: str = "codex", cwd: Path | None = None) -> tuple[str, ...]:
    """Read the active local model catalog through the Codex debug command."""

    try:
        completed = subprocess.run(
            [codex_binary, "debug", "models"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise AdapterError(f"could not run {codex_binary!r} to read the model catalog: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise AdapterError(f"model catalog command failed: {detail}")
    return parse_model_catalog(completed.stdout)


def preflight_model(
    requested_model: str | None,
    available_models: Iterable[str],
    *,
    fallback_model: str | None = None,
    parent_model: str | None = None,
) -> ModelSelection:
    """Select a model and record every fallback decision explicitly."""

    available = tuple(sorted({model.strip() for model in available_models if model.strip()}))
    requested = requested_model.strip() if requested_model else None
    fallback = fallback_model.strip() if fallback_model else None
    parent = parent_model.strip() if parent_model else None

    if requested is None:
        if fallback is not None:
            if fallback not in available:
                raise ModelUnavailableError(
                    f"explicit fallback model {fallback!r} is unavailable; "
                    f"available models: {', '.join(available) or '(none)'}"
                )
            return ModelSelection(
                requested_model=None,
                effective_model=fallback,
                fallback_applied=True,
                fallback_model=fallback,
                reason="no model was requested; explicit fallback selected",
                available_models=available,
            )
        if parent is not None:
            if parent not in available:
                raise ModelUnavailableError(
                    f"parent model {parent!r} is unavailable; "
                    f"available models: {', '.join(available) or '(none)'}"
                )
            return ModelSelection(
                requested_model=None,
                effective_model=parent,
                fallback_applied=False,
                fallback_model=None,
                reason="no role model was requested; parent model inherited",
                available_models=available,
            )
        return ModelSelection(
            requested_model=None,
            effective_model=None,
            fallback_applied=False,
            fallback_model=None,
            reason="no explicit model was requested; Codex session default remains unchanged",
            available_models=available,
        )

    if requested in available:
        return ModelSelection(
            requested_model=requested,
            effective_model=requested,
            fallback_applied=False,
            fallback_model=None,
            reason="requested model is available",
            available_models=available,
        )

    candidate = fallback or parent
    if candidate is None:
        raise ModelUnavailableError(
            f"requested model {requested!r} is unavailable; provide an available "
            "--fallback-model or --parent-model explicitly"
        )
    if candidate not in available:
        raise ModelUnavailableError(
            f"requested model {requested!r} and explicit fallback {candidate!r} are unavailable; "
            f"available models: {', '.join(available) or '(none)'}"
        )
    source = "fallback model" if fallback is not None else "parent model"
    return ModelSelection(
        requested_model=requested,
        effective_model=candidate,
        fallback_applied=True,
        fallback_model=candidate,
        reason=f"requested model is unavailable; explicit {source} selected",
        available_models=available,
    )


def validate_comment_sicko_report(output: str) -> dict[str, str]:
    """Validate the exact first line and required report sections."""

    if not isinstance(output, str):
        raise ReportContractError("Comment Sicko report must be text")
    for index, character in enumerate(output):
        if character == "\r":
            if index + 1 >= len(output) or output[index + 1] != "\n":
                raise ReportContractError("Comment Sicko report contains a bare carriage return")
            continue
        if character == "\n":
            continue
        if unicodedata.category(character).startswith(("C", "M")) or character in {"\u2028", "\u2029"}:
            raise ReportContractError("Comment Sicko report contains invisible or control characters")
    lines = output.splitlines()
    if not lines or lines[0] != COMMENT_SICKO_FIRST_OUTPUT:
        actual = lines[0] if lines else "(empty)"
        raise ReportContractError(
            f"Comment Sicko first output must be {COMMENT_SICKO_FIRST_OUTPUT!r}; got {actual!r}"
        )

    sections: dict[str, str] = {}
    for line in lines[1:]:
        if any(
            unicodedata.category(character).startswith(("C", "M"))
            or character in {"\u2028", "\u2029"}
            for character in line
        ):
            raise ReportContractError("Comment Sicko report contains invisible or control characters")
        matched = False
        for field in COMMENT_SICKO_REPORT_FIELDS:
            if line.startswith(field):
                matched = True
                if field in sections and field != "MUST KILL:":
                    raise ReportContractError(f"Comment Sicko report repeats {field!r}")
                value = line[len(field) :].strip()
                if field == "MUST KILL:" and field in sections:
                    sections[field] = f"{sections[field]}\n{value}"
                else:
                    sections[field] = value
                normalized_value = unicodedata.normalize("NFKC", value)
                if any(ord(character) > 127 for character in value) or normalized_value.startswith(
                    ("=", "+", "-", "@")
                ):
                    raise ReportContractError(
                        f"Comment Sicko report leaves {field!r} vulnerable to formula interpretation"
                    )
                if not value or not any(
                    character.isprintable()
                    and not character.isspace()
                    and unicodedata.category(character) not in {"Cf", "Mn"}
                    for character in unicodedata.normalize("NFKC", value)
                ):
                    raise ReportContractError(f"Comment Sicko report leaves {field!r} empty")
                break
        if line.strip() and not matched:
            raise ReportContractError("Comment Sicko report contains an unrecognized line")
    missing = [field for field in COMMENT_SICKO_REPORT_FIELDS if field not in sections]
    if missing:
        raise ReportContractError(
            "Comment Sicko report is missing sections: " + ", ".join(missing)
        )
    return sections


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--role", required=True)
    parser.add_argument("--model")
    parser.add_argument("--fallback-model")
    parser.add_argument("--parent-model")
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--codex-binary", default="codex")
    args = parser.parse_args(argv)

    try:
        project_root = args.project_root.resolve()
        role = resolve_role(project_root, args.role)
        if args.catalog_file is not None:
            catalog_payload = args.catalog_file.read_text(encoding="utf-8")
            available_models = parse_model_catalog(catalog_payload)
        elif any(value is not None for value in (args.model, args.fallback_model, args.parent_model)):
            available_models = read_model_catalog(args.codex_binary, cwd=project_root)
        else:
            available_models = ()
        model = preflight_model(
            args.model,
            available_models,
            fallback_model=args.fallback_model,
            parent_model=args.parent_model,
        )
        payload = {
            "status": "ready",
            "registration": "explicit-config",
            "role": role.name,
            "role_file": _relative_path(role.path, project_root),
            "report_only": role.name == "Comment Sicko",
            "model": model.as_dict(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (AdapterError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
