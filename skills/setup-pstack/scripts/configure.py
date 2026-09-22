#!/usr/bin/env python3
"""Discover Codex models and render pstack custom-agent configuration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}
BUDGET_TARGET = {"small": "medium", "medium": "high", "large": "xhigh", "unlimited": "max"}


class ConfigurationError(RuntimeError):
    """Raised when a model or configuration cannot be proven safe to write."""


@dataclass(frozen=True)
class Model:
    slug: str
    efforts: tuple[str, ...]
    hidden: bool = False


@dataclass(frozen=True)
class RoleSpec:
    role: str
    description: str
    fanout: int = 1


@dataclass(frozen=True)
class AgentPlan:
    role: str
    name: str
    path: Path
    content: str
    model: str | None
    effort: str | None
    status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "name": self.name,
            "path": str(self.path),
            "model": self.model,
            "effort": self.effort,
            "status": self.status,
        }


ROLE_SPECS = (
    RoleSpec("feature", "feature work"),
    RoleSpec("refactoring", "refactoring work"),
    RoleSpec("bug-fix", "bug-fix work"),
    RoleSpec("perf-issue", "performance work"),
    RoleSpec("hillclimb", "measured improvement work"),
    RoleSpec("judgment-and-prose", "judgment and prose work"),
    RoleSpec("hardest-tasks", "the hardest tasks"),
    RoleSpec("how-explorer", "read-heavy how exploration"),
    RoleSpec("how-explainer", "how explanations"),
    RoleSpec("why-investigators", "why investigation"),
    RoleSpec("why-synthesizer", "why synthesis"),
    RoleSpec("reflect-tooling", "reflect tooling review"),
    RoleSpec("reflect-judgment", "reflect judgment review"),
    RoleSpec("arena-runners", "arena candidate runs", fanout=4),
    RoleSpec("arena-cross-judge-pool", "arena cross-judging", fanout=4),
    RoleSpec("swarm-workers", "swarm work units"),
    RoleSpec("architect-runners", "architect candidates", fanout=4),
    RoleSpec("interrogate-reviewers", "interrogate reviews", fanout=4),
)
ROLE_BY_NAME = {spec.role: spec for spec in ROLE_SPECS}
NO_INFERRED_DEFAULT = object()


def _is_trusted_system_symlink(path: Path) -> bool:
    if path not in {Path("/var"), Path("/tmp")}:
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved in {Path("/private/var"), Path("/private/tmp")}


def _catalog_entries(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise ConfigurationError("model catalog must be a JSON object")
    if isinstance(payload.get("models"), list):
        entries = payload["models"]
    elif isinstance(payload.get("data"), list):
        entries = payload["data"]
    elif isinstance(payload.get("result"), dict) and isinstance(payload["result"].get("data"), list):
        entries = payload["result"]["data"]
    else:
        raise ConfigurationError("model catalog has no models or data list")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ConfigurationError("model catalog contains a non-object model entry")
    return entries  # type: ignore[return-value]


def _efforts(entry: dict[str, object]) -> tuple[str, ...]:
    raw = entry.get("supported_reasoning_levels")
    if isinstance(raw, list):
        values = [item.get("effort") for item in raw if isinstance(item, dict)]
    else:
        raw = entry.get("supportedReasoningEfforts")
        if isinstance(raw, list):
            values = [item.get("reasoningEffort") for item in raw if isinstance(item, dict)]
        else:
            normalized = entry.get("efforts")
            values = list(normalized) if isinstance(normalized, list) else []
    efforts = tuple(value for value in values if isinstance(value, str) and value in EFFORT_RANK)
    if not efforts:
        raise ConfigurationError(f"model entry has no documented reasoning efforts: {entry!r}")
    return tuple(dict.fromkeys(efforts))


def parse_catalog(payload: object, *, include_hidden: bool = False) -> dict[str, Model]:
    """Normalize the documented CLI and app-server catalog shapes."""

    models: dict[str, Model] = {}
    for entry in _catalog_entries(payload):
        slug = entry.get("slug") or entry.get("id") or entry.get("model")
        if not isinstance(slug, str) or not slug:
            raise ConfigurationError(f"model entry has no id: {entry!r}")
        hidden = entry.get("visibility") == "hide" or entry.get("hidden") is True
        if hidden and not include_hidden:
            continue
        if slug in models:
            raise ConfigurationError(f"duplicate model id in catalog: {slug}")
        models[slug] = Model(slug, _efforts(entry), hidden)
    if not models:
        raise ConfigurationError("model catalog has no selectable models")
    return models


def load_catalog(path: Path, *, include_hidden: bool = False) -> dict[str, Model]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read model catalog {path}: {exc}") from exc
    return parse_catalog(payload, include_hidden=include_hidden)


def discover_models(codex: str = "codex", *, bundled: bool = False, include_hidden: bool = False) -> dict[str, object]:
    command = [codex, "debug", "models"]
    if bundled:
        command.append("--bundled")
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigurationError(f"could not run {' '.join(command)}: {exc}") from exc
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ConfigurationError(f"{' '.join(command)} failed with {completed.returncode}: {detail}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{' '.join(command)} returned invalid JSON: {exc}") from exc
    models = parse_catalog(payload, include_hidden=include_hidden)
    return {
        "source": "codex debug models --bundled" if bundled else "codex debug models",
        "catalog_is_entitlement_proof": False,
        "models": [
            {"slug": model.slug, "efforts": list(model.efforts), "hidden": model.hidden}
            for model in models.values()
        ],
    }


def choose_effort(model: Model, budget: str) -> str:
    if budget not in BUDGET_TARGET:
        raise ConfigurationError(f"unknown budget: {budget}")
    target_rank = EFFORT_RANK[BUDGET_TARGET[budget]]
    candidates = [effort for effort in model.efforts if EFFORT_RANK[effort] <= target_rank]
    if not candidates:
        raise ConfigurationError(f"{model.slug} has no effort at or below the {budget} budget")
    return max(candidates, key=EFFORT_RANK.__getitem__)


def _parse_role_values(values: list[str]) -> dict[str, str]:
    choices: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ConfigurationError(f"role choice must use ROLE=MODEL: {value!r}")
        role, model = value.split("=", 1)
        if role not in ROLE_BY_NAME:
            raise ConfigurationError(f"unknown pstack role: {role}")
        if not model:
            raise ConfigurationError(f"empty model for role: {role}")
        if role in choices:
            raise ConfigurationError(f"role specified more than once: {role}")
        choices[role] = model
    return choices


def _validate_agents_root(agents_root: Path) -> None:
    """Reject an existing symlink or non-directory in the write path."""

    agents_root = agents_root.expanduser().absolute()
    current = Path(agents_root.anchor)
    for component in agents_root.parts[1:]:
        current /= component
        if current.is_symlink() and not _is_trusted_system_symlink(current):
            raise ConfigurationError(f"agents root path contains a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise ConfigurationError(f"agents root path is not a directory: {current}")


def _existing_agents(agents_root: Path) -> dict[str, tuple[str | None, str | None]]:
    existing: dict[str, tuple[str | None, str | None]] = {}
    _validate_agents_root(agents_root)
    if not agents_root.exists():
        return existing
    if not agents_root.is_dir() or agents_root.is_symlink():
        raise ConfigurationError(f"agents root is not a real directory: {agents_root}")
    for path in sorted(agents_root.glob("pstack-*.toml")):
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"existing agent path is not a regular file: {path}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot parse existing agent {path}: {exc}") from exc
        name = data.get("name")
        if not isinstance(name, str) or not name.startswith("pstack-"):
            raise ConfigurationError(f"existing pstack agent has invalid name: {path}")
        model = data.get("model")
        effort = data.get("model_reasoning_effort")
        if model is not None and not isinstance(model, str):
            raise ConfigurationError(f"existing pstack model is not a string: {path}")
        if effort is not None and not isinstance(effort, str):
            raise ConfigurationError(f"existing pstack effort is not a string: {path}")
        existing[name] = (model, effort)
    return existing


def _selected_models(spec: RoleSpec, choice: str | None) -> tuple[str | None, ...]:
    if choice is None:
        return (None,) * spec.fanout
    values = tuple(choice.split(","))
    if len(values) not in (1, spec.fanout):
        raise ConfigurationError(f"{spec.role} needs one model or exactly {spec.fanout} comma-separated models")
    if len(values) == 1:
        values *= spec.fanout
    return tuple(None if value in {"inherit-parent", "auto"} else value for value in values)


def _existing_selection(spec: RoleSpec, existing: dict[str, tuple[str | None, str | None]]) -> tuple[str | None, ...]:
    return tuple(existing.get(_agent_name(spec, index), (None, None))[0] for index in range(1, spec.fanout + 1))


def _inferred_default_model(existing: dict[str, tuple[str | None, str | None]]) -> str | None | object:
    """Infer the previous default from the generated singleton roles.

    Generated files do not record whether a choice came from the default or a
    role override. The most common singleton-role model is the only durable
    signal available, while panel lists retain their shape when they differ
    from that inferred default.
    """

    values = [
        existing[_agent_name(spec, 1)][0]
        for spec in ROLE_SPECS
        if spec.fanout == 1 and _agent_name(spec, 1) in existing
    ]
    if not values:
        values = [model for model, _effort in existing.values()]
    if not values:
        return NO_INFERRED_DEFAULT
    return Counter(values).most_common(1)[0][0]


def _preserve_existing_selection(
    spec: RoleSpec,
    existing: dict[str, tuple[str | None, str | None]],
    inferred_default: str | None | object,
) -> bool:
    if inferred_default is NO_INFERRED_DEFAULT:
        return False
    names = [_agent_name(spec, index) for index in range(1, spec.fanout + 1)]
    if not all(name in existing for name in names):
        return False
    values = [existing[name][0] for name in names]
    return values[0] != inferred_default if spec.fanout == 1 else any(value != inferred_default for value in values)


def _agent_name(spec: RoleSpec, index: int) -> str:
    base = f"pstack-{spec.role}"
    return base if spec.fanout == 1 else f"{base}-{index}"


def _render_agent(role: str, name: str, description: str, model: str | None, effort: str | None) -> str:
    lines = [
        f"name = {json.dumps(name)}",
        f"description = {json.dumps(f'Codex subagent for pstack {description}.')}",
    ]
    if model is not None:
        lines.append(f"model = {json.dumps(model)}")
    if effort is not None:
        lines.append(f"model_reasoning_effort = {json.dumps(effort)}")
    instructions = (
        f"Follow the pstack {role} role selected by the parent. "
        "Use the relevant pstack skill, stay within the parent's permission envelope, "
        "and report evidence and unsupported boundaries instead of inventing capabilities."
    )
    lines.append(f"developer_instructions = {json.dumps(instructions)}")
    return "\n".join(lines) + "\n"


def build_plan(
    models: dict[str, Model],
    agents_root: Path,
    *,
    budget: str | None = None,
    default_model: str | None = None,
    role_values: list[str] | None = None,
) -> list[AgentPlan]:
    if budget is not None and budget not in BUDGET_TARGET:
        raise ConfigurationError(f"unknown budget: {budget}")
    choices = _parse_role_values(role_values or [])
    if default_model not in {None, "inherit-parent", "auto"} and default_model not in models:
        raise ConfigurationError(f"model was not reported by Codex: {default_model}")
    for choice in choices.values():
        for model_name in choice.split(","):
            if model_name not in {"inherit-parent", "auto"} and model_name not in models:
                raise ConfigurationError(f"model was not reported by Codex: {model_name}")

    existing = _existing_agents(agents_root)
    inferred_default = _inferred_default_model(existing)
    default_is_explicit = default_model is not None
    plan: list[AgentPlan] = []
    for spec in ROLE_SPECS:
        if spec.role in choices:
            selected = _selected_models(spec, choices[spec.role])
        elif not default_is_explicit:
            selected = _existing_selection(spec, existing)
        elif _preserve_existing_selection(spec, existing, inferred_default):
            selected = _existing_selection(spec, existing)
        else:
            selected = _selected_models(spec, default_model)
        for index, selected_model in enumerate(selected, start=1):
            name = _agent_name(spec, index)
            existing_model, existing_effort = existing.get(name, (None, None))
            if selected_model is not None and selected_model not in models:
                raise ConfigurationError(f"existing or selected model was not reported by Codex: {selected_model}")
            if (
                selected_model is not None
                and existing_model == selected_model
                and existing_effort is not None
                and existing_effort not in models[selected_model].efforts
            ):
                raise ConfigurationError(
                    f"existing effort {existing_effort!r} for {name} was not reported for {selected_model}"
                )
            effort = None
            if selected_model is not None:
                if budget:
                    effort = choose_effort(models[selected_model], budget)
                elif existing_effort is not None and existing_effort in models[selected_model].efforts:
                    effort = existing_effort
                else:
                    raise ConfigurationError(f"choose a budget before configuring {name}")
            content = _render_agent(spec.role, name, spec.description, selected_model, effort)
            path = agents_root / f"{name}.toml"
            if path.is_symlink():
                raise ConfigurationError(f"refusing to overwrite agent symlink: {path}")
            if path.exists() and not path.is_file():
                raise ConfigurationError(f"refusing to overwrite non-file agent path: {path}")
            status = "create" if not path.exists() else "unchanged" if path.read_text(encoding="utf-8") == content else "update"
            plan.append(AgentPlan(spec.role, name, path, content, selected_model, effort, status))
    return plan


def _missing_directories(path: Path) -> list[Path]:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return missing


def _cleanup_empty_directories(paths: list[Path]) -> None:
    for path in sorted(set(paths), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _validate_plan_targets(plan: list[AgentPlan], agents_root: Path) -> None:
    for item in plan:
        if item.status == "unchanged":
            continue
        if item.path.parent != agents_root:
            raise ConfigurationError(f"agent path is outside the configured root: {item.path}")
        if item.path.is_symlink():
            raise ConfigurationError(f"refusing to overwrite agent symlink: {item.path}")
        if item.path.exists() and not item.path.is_file():
            raise ConfigurationError(f"refusing to overwrite non-file agent path: {item.path}")
        if item.status == "create" and item.path.exists():
            raise ConfigurationError(f"agent appeared during configuration: {item.path}")
        if item.status == "update" and not item.path.exists():
            raise ConfigurationError(f"agent disappeared during configuration: {item.path}")


def _rollback_published(
    applied: list[tuple[AgentPlan, Path | None, bool]],
) -> list[str]:
    errors: list[str] = []
    for item, backup, existed in reversed(applied):
        try:
            if existed:
                if backup is None:
                    raise OSError("missing transaction backup")
                os.replace(backup, item.path)
            elif item.path.is_symlink():
                errors.append(f"preserved concurrent symlink at {item.path}")
            elif item.path.exists():
                if item.path.read_text(encoding="utf-8") == item.content:
                    item.path.unlink()
                else:
                    errors.append(f"preserved changed file at {item.path}")
        except OSError as exc:
            errors.append(f"could not restore {item.path}: {exc}")
    return errors


def apply_plan(plan: list[AgentPlan], agents_root: Path, *, allow_replace: bool = False) -> None:
    conflicts = [item.path for item in plan if item.status == "update"]
    if conflicts and not allow_replace:
        joined = ", ".join(str(path) for path in conflicts)
        raise ConfigurationError(f"existing agent files differ; preview and review before replacing, then pass --replace: {joined}")
    agents_root = agents_root.expanduser().absolute()
    _validate_agents_root(agents_root)
    changed = [item for item in plan if item.status in {"create", "update"}]
    if not changed:
        return
    parent_directories: list[Path] = []
    staging: Path | None = None
    root_was_existing = agents_root.exists()
    completed = False
    try:
        parent_directories = _missing_directories(agents_root.parent)
        for directory in reversed(parent_directories):
            directory.mkdir()
        if not agents_root.exists():
            staging = Path(tempfile.mkdtemp(prefix=".pstack-agents-", dir=agents_root.parent))
            for item in changed:
                (staging / item.path.name).write_text(item.content, encoding="utf-8")
            if os.path.lexists(agents_root):
                raise ConfigurationError(f"agents root appeared during configuration: {agents_root}")
            os.rename(staging, agents_root)
            staging = None
            completed = True
            return

        _validate_agents_root(agents_root)
        _validate_plan_targets(plan, agents_root)
        staging = Path(tempfile.mkdtemp(prefix=".pstack-agents-", dir=agents_root.parent))
        backup_directory = staging / ".backups"
        backup_directory.mkdir()
        records: list[tuple[AgentPlan, Path, Path | None, bool]] = []
        for index, item in enumerate(changed):
            staged = staging / item.path.name
            staged.write_text(item.content, encoding="utf-8")
            backup: Path | None = None
            existed = item.path.exists()
            if existed:
                backup = backup_directory / f"{index}.bak"
                shutil.copy2(item.path, backup)
            records.append((item, staged, backup, existed))
        _validate_plan_targets(plan, agents_root)

        applied: list[tuple[AgentPlan, Path | None, bool]] = []
        try:
            for item, staged, backup, existed in records:
                if existed:
                    os.replace(staged, item.path)
                else:
                    os.link(staged, item.path)
                    staged.unlink()
                applied.append((item, backup, existed))
        except OSError as exc:
            rollback_errors = _rollback_published(applied)
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise ConfigurationError(f"configuration failed and rollback was incomplete: {detail}") from exc
            raise ConfigurationError(f"configuration failed and was rolled back: {exc}") from exc
        completed = True
    except OSError as exc:
        raise ConfigurationError(f"configuration failed before publication: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if not root_was_existing and not completed:
            _cleanup_empty_directories(parent_directories)


def _self_test() -> None:
    payload = {
        "models": [
            {"slug": "codex-test-a", "supported_reasoning_levels": [{"effort": value} for value in EFFORT_RANK]},
            {"slug": "codex-test-b", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
        ]
    }
    models = parse_catalog(payload)
    assert choose_effort(models["codex-test-a"], "medium") == "high"
    assert choose_effort(models["codex-test-b"], "unlimited") == "medium"
    try:
        parse_catalog({"models": [{"slug": "bad"}]})
    except ConfigurationError:
        pass
    else:
        raise AssertionError("catalog without documented efforts was accepted")

    with tempfile.TemporaryDirectory(prefix="setup-pstack-config-test-") as temporary:
        agents_root = Path(temporary) / ".codex" / "agents"
        plan = build_plan(models, agents_root, budget="medium", default_model="codex-test-a")
        assert all(item.effort == "high" for item in plan)
        apply_plan(plan, agents_root)
        repeat = build_plan(models, agents_root)
        assert all(item.status == "unchanged" for item in repeat)
        try:
            build_plan(models, agents_root, default_model="not-reported", budget="medium")
        except ConfigurationError:
            pass
        else:
            raise AssertionError("unreported model was accepted")
    print("setup-pstack configuration self-check passed")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--catalog", type=Path, required=True, help="JSON from codex debug models or app-server model/list")
    parser.add_argument("--agents-root", type=Path, required=True, help="explicit .codex/agents directory")
    parser.add_argument("--budget", choices=sorted(BUDGET_TARGET), help="effort ceiling to apply")
    parser.add_argument("--default-model", help="reported model to use for roles without an override")
    parser.add_argument("--role", action="append", default=[], metavar="ROLE=MODEL", help="override a role; repeat for multiple roles")
    parser.add_argument("--replace", action="store_true", help="allow a reviewed apply to replace differing generated files")
    parser.add_argument("--json", action="store_true", help="print a machine-readable plan")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="ask the Codex CLI for its model catalog")
    discover.add_argument("--codex", default="codex")
    discover.add_argument("--bundled", action="store_true", help="use the catalog bundled with the CLI")
    discover.add_argument("--include-hidden", action="store_true")
    discover.add_argument("--json", action="store_true")
    for command in ("preview", "apply"):
        command_parser = subparsers.add_parser(command)
        _common(command_parser)
    subparsers.add_parser("self-test", help="exercise model selection and config writes in a temporary directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "self-test":
            _self_test()
            return 0
        if args.command == "discover":
            result = discover_models(args.codex, bundled=args.bundled, include_hidden=args.include_hidden)
            print(json.dumps(result, indent=2 if args.json else None, sort_keys=True))
            return 0
        models = load_catalog(args.catalog)
        plan = build_plan(
            models,
            args.agents_root.expanduser().absolute(),
            budget=args.budget,
            default_model=args.default_model,
            role_values=args.role,
        )
        if args.command == "apply":
            apply_plan(plan, args.agents_root.expanduser().absolute(), allow_replace=args.replace)
        elif args.replace:
            raise ConfigurationError("--replace is only valid with apply")
        result = {"action": args.command, "agents": [item.as_dict() for item in plan]}
        print(json.dumps(result, indent=2 if args.json else None, sort_keys=True))
        return 0
    except ConfigurationError as exc:
        print(f"setup-pstack configuration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
