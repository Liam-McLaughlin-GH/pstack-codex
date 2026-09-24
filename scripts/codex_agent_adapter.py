#!/usr/bin/env python3
"""Validate and select the pstack Codex roles without starting a child."""

from __future__ import annotations

import argparse
import json
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
DIRECT_SKILL_FALLBACKS = {
    "poteto-agent": "poteto-mode",
    "Comment Sicko": "no-comments",
}
EXPECTED_ROLE_NAMES = {"poteto-agent", "Comment Sicko"}
EXPECTED_ROLE_DESCRIPTIONS = {
    "poteto-agent": "Routing target for /poteto-mode and requests for poteto's style.",
    "Comment Sicko": "A deranged comment-hater that savors deletion and condemns workaround code.",
}
EXPECTED_ROLE_CONFIG_FILES = {
    "poteto-agent": "agents/poteto-agent.toml",
    "Comment Sicko": "agents/comment-sicko.toml",
}
EXPECTED_ROLE_SETTINGS = {
    "poteto-agent": {},
    "Comment Sicko": {"sandbox_mode": "read-only"},
}
EXPECTED_ROLE_DEVELOPER_INSTRUCTIONS = {
    "poteto-agent": (
        "You are operating as poteto-mode's full agent style.\n"
        "\n"
        "Before doing any work, read the complete skills/poteto-mode/SKILL.md file, including its inline Principles index and working rules. Do not replace that read with a summary. When applying a principle, read the complete relevant skills/principle-*/SKILL.md leaf before using it.\n"
        "\n"
        "Preserve the parent task contract, scope, and verification requirements. If an existing poteto-agent identity is supplied by the caller, continue that identity instead of creating a sibling. A generic-purpose worker is not a substitute for this role because it can skip the required skill reads.\n"
    ),
    "Comment Sicko": (
        "Your first output must be exactly this single line:\n"
        "Yes... Ha ha ha... Yes!\n"
        "\n"
        "You are Comment Sicko. Work only from the parent-scoped files or diff. If no scope is supplied, inspect the current diff against main. If that diff is empty or unavailable, report the skip instead of inventing findings.\n"
        "\n"
        "Review narration, banners, commented-out code, workaround explanations, and long justifications. Keep only these exceptions:\n"
        "\n"
        "- Legal or license headers.\n"
        "- Non-obvious behavior forced by an external dependency, platform, vendor, or protocol that cannot be reshaped. If our own code is surprising, mark the exact symbol MUST KILL for rename, extraction, typing, or rearchitecture that makes the behavior obvious without prose.\n"
        "- // prettier-ignore.\n"
        "- Lint suppressions only when the rule is faulty, pedantic, or style-only. Look up the rule. If it protects correctness or safety, flag the exact guilty symbol MUST KILL.\n"
        "- Public API doc comments.\n"
        "- Issue or RFC links that express a constraint code cannot express.\n"
        "\n"
        "Read nearby code before judging. When a claim is not obvious there, use the $how or $why skill on the named symbol or call. Do not write application code. This is a report-only review.\n"
        "\n"
        "The report must contain these sections after the first line, using one line for each MUST KILL flag:\n"
        "Touched files:\n"
        "Deletion count:\n"
        "MUST KILL:\n"
        "Skips:\n"
    ),
}
EXPECTED_REGISTRY = {
    role_name: {
        "description": EXPECTED_ROLE_DESCRIPTIONS[role_name],
        "config_file": EXPECTED_ROLE_CONFIG_FILES[role_name],
    }
    for role_name in EXPECTED_ROLE_NAMES
}


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
    return value


def _resolve_path(path: Path, description: str) -> Path:
    try:
        return path.resolve()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise AdapterError(f"could not resolve {description}: {path}: {exc}") from exc


def _path_is_symlink(path: Path, description: str) -> bool:
    try:
        return path.is_symlink()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise AdapterError(f"could not inspect {description}: {path}: {exc}") from exc


def _path_is_dir(path: Path, description: str) -> bool:
    try:
        return path.is_dir()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise AdapterError(f"could not inspect {description}: {path}: {exc}") from exc


def _path_is_file(path: Path, description: str) -> bool:
    try:
        return path.is_file()
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        raise AdapterError(f"could not inspect {description}: {path}: {exc}") from exc


def parse_role_file(path: Path) -> RoleDefinition:
    """Parse one current-format Codex role file and validate its required fields."""

    if not isinstance(path, Path):
        raise AdapterError("role file path must be a pathlib.Path")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AdapterError(f"could not read role file: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AdapterError(f"role file is not valid TOML: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AdapterError(f"role file must contain a TOML table: {path}")
    name = _non_empty_string(raw.get("name"), "name", path)
    description = _non_empty_string(raw.get("description"), "description", path)
    developer_instructions_value = raw.get("developer_instructions")
    if not isinstance(developer_instructions_value, str) or not developer_instructions_value.strip():
        raise AdapterError(f"{path}: developer_instructions must be a non-empty string")
    developer_instructions = developer_instructions_value
    if name not in EXPECTED_ROLE_NAMES:
        raise AdapterError(f"{path}: unsupported shipped role {name!r}")
    expected_settings = EXPECTED_ROLE_SETTINGS[name]
    expected_keys = {"name", "description", "developer_instructions", *expected_settings}
    unknown_keys = set(raw) - expected_keys
    if unknown_keys:
        raise AdapterError(
            f"{path}: unknown role-file settings: {', '.join(sorted(unknown_keys))}"
        )
    settings = {
        key: value
        for key, value in raw.items()
        if key not in {"name", "description", "developer_instructions"}
    }
    if settings != expected_settings:
        raise AdapterError(f"{path}: role settings do not match the shipped {name} contract")
    expected_description = EXPECTED_ROLE_DESCRIPTIONS[name]
    if description != expected_description:
        raise AdapterError(f"{path}: {name} description does not match the shipped role contract")
    if developer_instructions != EXPECTED_ROLE_DEVELOPER_INSTRUCTIONS[name]:
        raise AdapterError(
            f"{path}: developer_instructions do not match the shipped {name} contract"
        )
    role = RoleDefinition(
        path=_resolve_path(path, "role file"),
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
    return role


def _load_toml_table(path: Path) -> dict[str, Any]:
    if not isinstance(path, Path):
        raise AdapterError("configuration file path must be a pathlib.Path")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AdapterError(f"could not read configuration file: {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AdapterError(f"configuration file is not valid TOML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdapterError(f"configuration file must contain a TOML table: {path}")
    return raw


def load_explicit_roles(project_root: Path) -> dict[str, RoleDefinition]:
    """Load only roles named by the project's explicit ``[agents]`` registry."""

    if not isinstance(project_root, Path):
        raise AdapterError("project root must be a pathlib.Path")
    project_root = _resolve_path(project_root, "project root")
    codex_path = project_root / ".codex"
    if _path_is_symlink(codex_path, "explicit .codex directory") or not _path_is_dir(
        codex_path, "explicit .codex directory"
    ):
        raise AdapterError(f"{codex_path}: explicit .codex directory must be real")
    config_path = codex_path / "config.toml"
    if _path_is_symlink(config_path, "explicit config") or not _path_is_file(
        config_path, "explicit config"
    ):
        raise AdapterError(f"{config_path}: explicit config must be a real file")
    raw = _load_toml_table(config_path)
    unknown_config_keys = set(raw) - {"agents"}
    if unknown_config_keys:
        raise AdapterError(
            f"{config_path}: unknown registry settings: {', '.join(sorted(unknown_config_keys))}"
        )
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
        expected_declaration = EXPECTED_REGISTRY[declared_name]
        unknown_keys = set(declaration) - set(expected_declaration)
        missing_keys = set(expected_declaration) - set(declaration)
        if unknown_keys or missing_keys:
            raise AdapterError(
                f"{config_path}: agents.{declared_name} must contain exactly the shipped "
                f"keys; missing={', '.join(sorted(missing_keys)) or '(none)'}; "
                f"extra={', '.join(sorted(unknown_keys)) or '(none)'}"
            )
        description = _non_empty_string(
            declaration.get("description"), "description", config_path
        )
        config_file = _non_empty_string(
            declaration.get("config_file"),
            "config_file",
            config_path,
        )
        if {"description": description, "config_file": config_file} != expected_declaration:
            raise AdapterError(
                f"{config_path}: agents.{declared_name} does not match the shipped registry contract"
            )
        if Path(config_file).is_absolute():
            raise AdapterError(f"{config_path}: role config_file must be relative: {config_file}")
        declared_role_path = config_path.parent / config_file
        current = declared_role_path
        while current != codex_root:
            if current == current.parent:
                raise AdapterError(f"{config_path}: role path escapes .codex: {config_file}")
            if _path_is_symlink(current, "role path"):
                raise AdapterError(f"{config_path}: role path contains a symlink: {config_file}")
            current = current.parent
        role_path = _resolve_path(declared_role_path, "role file")
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
        except (json.JSONDecodeError, UnicodeError, TypeError) as exc:
            raise AdapterError(f"model catalog is not valid JSON: {exc}") from exc
    elif isinstance(payload, Mapping):
        decoded = payload
    else:
        raise AdapterError("model catalog must be JSON text, bytes, or an object")
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

    if not isinstance(codex_binary, str) or not codex_binary.strip():
        raise AdapterError("codex binary must be a non-empty string")
    if cwd is not None and not isinstance(cwd, Path):
        raise AdapterError("codex command cwd must be a pathlib.Path")
    try:
        completed = subprocess.run(
            [codex_binary.strip(), "debug", "models"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, UnicodeError) as exc:
        raise AdapterError(f"could not run {codex_binary!r} to read the model catalog: {exc}") from exc
    try:
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError(f"model catalog command returned an invalid result: {exc}") from exc
    if not isinstance(returncode, int):
        raise AdapterError("model catalog command returned a non-integer exit status")
    if returncode != 0:
        if stderr is not None and not isinstance(stderr, (str, bytes)):
            raise AdapterError("model catalog command returned a non-text diagnostic")
        if stdout is not None and not isinstance(stdout, (str, bytes)):
            raise AdapterError("model catalog command returned non-text output")
        detail = (stderr.strip() if stderr else "") or (stdout.strip() if stdout else "") or "no diagnostic"
        raise AdapterError(f"model catalog command failed: {detail}")
    return parse_model_catalog(stdout)


def _normalize_optional_model(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdapterError(f"{field} must be a string or None")
    normalized = value.strip()
    return normalized or None


def _normalize_available_models(available_models: Iterable[str]) -> tuple[str, ...]:
    if available_models is None or isinstance(available_models, (str, bytes)):
        raise AdapterError("available_models must be an iterable of strings")
    try:
        iterator = iter(available_models)
    except TypeError as exc:
        raise AdapterError("available_models must be an iterable of strings") from exc
    normalized: list[str] = []
    try:
        for model in iterator:
            if not isinstance(model, str):
                raise AdapterError("available_models must contain only strings")
            model = model.strip()
            if model:
                normalized.append(model)
    except AdapterError:
        raise
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AdapterError(f"could not read available models: {exc}") from exc
    return tuple(sorted(set(normalized)))


def preflight_model(
    requested_model: str | None,
    available_models: Iterable[str],
    *,
    fallback_model: str | None = None,
    parent_model: str | None = None,
) -> ModelSelection:
    """Select a model and record every fallback decision explicitly."""

    available = _normalize_available_models(available_models)
    requested = _normalize_optional_model(requested_model, "requested_model")
    fallback = _normalize_optional_model(fallback_model, "fallback_model")
    parent = _normalize_optional_model(parent_model, "parent_model")

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
    resolved_path = _resolve_path(path, "role file")
    resolved_root = _resolve_path(root, "project root")
    try:
        return str(resolved_path.relative_to(resolved_root))
    except ValueError:
        return str(resolved_path)


def _read_catalog_file(path: Path) -> str:
    if not isinstance(path, Path):
        raise AdapterError("catalog file path must be a pathlib.Path")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AdapterError(f"could not read model catalog file: {path}: {exc}") from exc


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
            catalog_payload = _read_catalog_file(args.catalog_file)
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
    except AdapterError as exc:
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
    except (OSError, UnicodeError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        normalized = AdapterError(f"adapter input failure: {exc}")
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": type(normalized).__name__,
                    "message": str(normalized),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
