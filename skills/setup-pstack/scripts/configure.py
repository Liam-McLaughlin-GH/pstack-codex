#!/usr/bin/env python3
"""Discover Codex models and render pstack custom-agent configuration."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from safe_fs import (  # noqa: E402
    SafeFilesystemError,
    CleanupReceipt,
    atomic_exchange_at,
    atomic_rename_noreplace_at,
    TreeSnapshot,
    canonical_path,
    cleanup_created_directories,
    close_created_directories,
    create_unique_directory,
    directory_lock,
    lstat_at,
    merge_cleanup_receipts,
    open_child_directory,
    open_directory,
    remove_tree_at,
    recovery_receipt_for_created_directories,
    snapshot_tree,
    sync_directory,
    unique_entry_name,
    write_regular_file_at,
    write_snapshot,
)


EFFORT_RANK = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4, "ultra": 5}
BUDGET_TARGET = {"small": "medium", "medium": "high", "large": "xhigh", "unlimited": "max"}


def _cleanup_retry_hint(residue: Path, expected_metadata: os.stat_result | None = None) -> str:
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "configure.py"),
        "cleanup",
        "--residue",
        str(residue),
    ]
    if expected_metadata is not None:
        command.extend(("--expected-dev", str(expected_metadata.st_dev), "--expected-ino", str(expected_metadata.st_ino)))
    return shlex.join(command)


def _recovery_hint(root: Path, residue: Path) -> str:
    return f"inspect {root} and {residue}, then rerun preview before cleanup"


def _parent_cleanup_retry_hint(receipt: CleanupReceipt | None) -> str | None:
    if receipt is None:
        return None
    paths = ", ".join(str(entry.path) for entry in receipt.entries)
    if receipt.truncated:
        paths += ", ..."
    return f"inspect created-parent identities ({paths}) before retrying the original configuration command"


class ConfigurationError(RuntimeError):
    """Raised when a model or configuration cannot be proven safe to write."""

    def __init__(
        self,
        message: str,
        *,
        status: str = "failed",
        residue: Path | None = None,
        retry: str | None = None,
        recovery_receipt: CleanupReceipt | None = None,
        recovery_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.residue = residue
        self.retry = retry
        self.recovery_receipt = recovery_receipt
        self.receipt = recovery_receipt
        self.recovery_state = recovery_state

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status, "error": str(self)}
        if self.residue is not None:
            result["residue"] = str(self.residue)
        if self.retry is not None:
            result["retry"] = self.retry
        if self.recovery_receipt is not None:
            result["recovery_receipt"] = self.recovery_receipt.as_dict()
        if self.recovery_state is not None:
            result["recovery_state"] = self.recovery_state
        return result


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
    expected_content: bytes | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "name": self.name,
            "path": str(self.path),
            "model": self.model,
            "effort": self.effort,
            "status": self.status,
        }


@dataclass(frozen=True)
class TransactionOutcome:
    """Describe the durable state after an apply or cleanup attempt."""

    status: str
    residue: Path | None = None
    retry: str | None = None
    recovery_receipt: CleanupReceipt | None = None
    recovery_state: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        if self.residue is not None:
            result["residue"] = str(self.residue)
        if self.retry is not None:
            result["retry"] = self.retry
        if self.recovery_receipt is not None:
            result["recovery_receipt"] = self.recovery_receipt.as_dict()
        if self.recovery_state is not None:
            result["recovery_state"] = self.recovery_state
        return result


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
    RoleSpec("reflect-tooling", "dormant reflect tooling review"),
    RoleSpec("reflect-judgment", "dormant reflect judgment review"),
    RoleSpec("arena-runners", "arena candidate runs", fanout=4),
    RoleSpec("arena-cross-judge-pool", "arena cross-judging", fanout=4),
    RoleSpec("swarm-workers", "swarm work units"),
    RoleSpec("architect-runners", "architect candidates", fanout=4),
    RoleSpec("interrogate-reviewers", "interrogate reviews", fanout=4),
)
ROLE_BY_NAME = {spec.role: spec for spec in ROLE_SPECS}
UNCONSUMED_UPSTREAM_ROLES = {
    "divergent": "the upstream role has no active Codex consumer in this port",
    "synthesizer": "the upstream role has no active Codex consumer in this port",
}
UNCONSUMED_CODEX_ROLES = {
    "reflect-tooling": "no active Codex reflect skill consumes this generated agent",
    "reflect-judgment": "no active Codex reflect skill consumes this generated agent",
}
NO_INFERRED_DEFAULT = object()
ALIASES_ARE_DEFAULT = object()


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
        if role in UNCONSUMED_UPSTREAM_ROLES:
            raise ConfigurationError(f"cannot configure upstream role {role!r}: {UNCONSUMED_UPSTREAM_ROLES[role]}")
        if role not in ROLE_BY_NAME:
            raise ConfigurationError(f"unknown pstack role: {role}")
        if not model:
            raise ConfigurationError(f"empty model for role: {role}")
        if role in choices:
            raise ConfigurationError(f"role specified more than once: {role}")
        choices[role] = model
    return choices


def _agents_snapshot(agents_root: Path) -> TreeSnapshot:
    agents_root = canonical_path(agents_root)
    try:
        handle = open_directory(agents_root)
    except FileNotFoundError:
        return TreeSnapshot((), {}, {})
    except (OSError, SafeFilesystemError) as exc:
        raise ConfigurationError(f"agents root is not a real no-follow directory: {agents_root}: {exc}") from exc
    try:
        return snapshot_tree(handle.fd)
    except (OSError, SafeFilesystemError) as exc:
        raise ConfigurationError(f"could not read agents root safely: {agents_root}: {exc}") from exc
    finally:
        os.close(handle.fd)
        close_created_directories(handle.created)


def _validate_agents_snapshot(snapshot: TreeSnapshot, agents_root: Path) -> None:
    generated_names = {
        relative
        for relative in (*snapshot.files.keys(), *snapshot.symlinks.keys(), *snapshot.directories)
        if "/" not in relative and relative.startswith("pstack-") and relative.endswith(".toml")
    }
    for name in generated_names:
        if name in snapshot.symlinks:
            raise ConfigurationError(f"existing agent path is a symlink: {agents_root / name}")
        if name in snapshot.directories:
            raise ConfigurationError(f"existing agent path is not a regular file: {agents_root / name}")


def _existing_agents(agents_root: Path) -> dict[str, tuple[str | None, str | None]]:
    agents_root = canonical_path(agents_root)
    snapshot = _agents_snapshot(agents_root)
    _validate_agents_snapshot(snapshot, agents_root)
    existing: dict[str, tuple[str | None, str | None]] = {}
    for name, raw in sorted(snapshot.files.items()):
        if "/" in name or not name.startswith("pstack-") or not name.endswith(".toml"):
            continue
        path = agents_root / name
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot parse existing agent {path}: {exc}") from exc
        agent_name = data.get("name")
        if not isinstance(agent_name, str) or not agent_name.startswith("pstack-"):
            raise ConfigurationError(f"existing pstack agent has invalid name: {path}")
        model = data.get("model")
        effort = data.get("model_reasoning_effort")
        if model is not None and not isinstance(model, str):
            raise ConfigurationError(f"existing pstack model is not a string: {path}")
        if effort is not None and not isinstance(effort, str):
            raise ConfigurationError(f"existing pstack effort is not a string: {path}")
        existing[agent_name] = (model, effort)
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


def _inferred_default_model(existing: dict[str, tuple[str | None, str | None]]) -> str | object:
    """Infer the previous concrete default without treating aliases as models."""

    values = [
        existing[_agent_name(spec, 1)][0]
        for spec in ROLE_SPECS
        if spec.fanout == 1 and _agent_name(spec, 1) in existing
    ]
    if not values:
        values = [model for model, _effort in existing.values()]
    if not values:
        return NO_INFERRED_DEFAULT
    concrete_counts = Counter(model for model in values if model is not None)
    alias_count = len(values) - sum(concrete_counts.values())
    if alias_count > len(values) / 2:
        if not concrete_counts:
            return ALIASES_ARE_DEFAULT
        return concrete_counts.most_common(1)[0][0]
    if concrete_counts:
        return concrete_counts.most_common(1)[0][0]
    return ALIASES_ARE_DEFAULT


def _preserve_existing_selection(
    spec: RoleSpec,
    existing: dict[str, tuple[str | None, str | None]],
    inferred_default: str | object,
) -> bool:
    if inferred_default is NO_INFERRED_DEFAULT:
        return False
    names = [_agent_name(spec, index) for index in range(1, spec.fanout + 1)]
    if not all(name in existing for name in names):
        return False
    if inferred_default is ALIASES_ARE_DEFAULT:
        return True
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
    if role in UNCONSUMED_CODEX_ROLES:
        instructions += f" This generated role is dormant: {UNCONSUMED_CODEX_ROLES[role]}."
    lines.append(f"developer_instructions = {json.dumps(instructions)}")
    return "\n".join(lines) + "\n"


def _agent_contents(agents_root: Path) -> dict[str, bytes]:
    snapshot = _agents_snapshot(agents_root)
    _validate_agents_snapshot(snapshot, canonical_path(agents_root))
    return {name: content for name, content in snapshot.files.items() if "/" not in name}


def build_plan(
    models: dict[str, Model],
    agents_root: Path,
    *,
    budget: str | None = None,
    default_model: str | None = None,
    role_values: list[str] | None = None,
) -> list[AgentPlan]:
    agents_root = canonical_path(agents_root)
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
    contents = _agent_contents(agents_root)
    inferred_default = _inferred_default_model(existing)
    default_is_explicit = default_model is not None
    default_is_parent_alias = default_model in {"inherit-parent", "auto"}
    plan: list[AgentPlan] = []
    for spec in ROLE_SPECS:
        if spec.role in choices:
            selected = _selected_models(spec, choices[spec.role])
        elif default_is_parent_alias:
            selected = _selected_models(spec, default_model)
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
            raw = contents.get(path.name)
            status = "create" if raw is None else "unchanged" if raw.decode("utf-8") == content else "update"
            plan.append(AgentPlan(spec.role, name, path, content, selected_model, effort, status, raw))
    return plan


def _validate_plan_snapshot(plan: list[AgentPlan], agents_root: Path, snapshot: TreeSnapshot) -> None:
    agents_root = canonical_path(agents_root)
    directories = set(snapshot.directories)
    for item in plan:
        if item.status == "unchanged":
            continue
        if item.path.parent != agents_root:
            raise ConfigurationError(f"agent path is outside the configured root: {item.path}")
        name = item.path.name
        if name in snapshot.symlinks or name in directories:
            raise ConfigurationError(f"refusing to overwrite non-regular agent path: {item.path}")
        exists = name in snapshot.files
        if item.status == "create" and exists:
            raise ConfigurationError(f"agent appeared during configuration: {item.path}")
        if item.status == "update" and not exists:
            raise ConfigurationError(f"agent disappeared during configuration: {item.path}")
        if item.expected_content is not None and snapshot.files.get(name) != item.expected_content:
            raise ConfigurationError(
                f"agent changed after preview; rebuild and review the plan before replacing: {item.path}"
            )


def _open_agents_root_at(parent_fd: int, name: str) -> tuple[int | None, TreeSnapshot, os.stat_result | None]:
    try:
        metadata = lstat_at(parent_fd, name)
    except FileNotFoundError:
        return None, TreeSnapshot((), {}, {}), None
    if stat.S_ISLNK(metadata.st_mode):
        raise ConfigurationError(f"agents root is a symlink: {name}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError(f"agents root is not a directory: {name}")
    try:
        root_fd = open_child_directory(parent_fd, name)
        snapshot = snapshot_tree(root_fd)
    except (OSError, SafeFilesystemError) as exc:
        if "root_fd" in locals():
            os.close(root_fd)
        raise ConfigurationError(f"could not open agents root without following a link: {name}: {exc}") from exc
    return root_fd, snapshot, metadata


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _open_expected_root(
    parent_fd: int,
    root_name: str,
    expected_root: os.stat_result,
) -> int | None:
    try:
        live_root = lstat_at(parent_fd, root_name)
    except FileNotFoundError:
        return None
    if not _same_entry(live_root, expected_root):
        return None
    try:
        root_fd = open_child_directory(parent_fd, root_name)
        opened_root = os.fstat(root_fd)
    except (OSError, SafeFilesystemError):
        if "root_fd" in locals():
            os.close(root_fd)
        return None
    if not _same_entry(opened_root, expected_root):
        os.close(root_fd)
        return None
    return root_fd


def _root_descriptor_matches(
    parent_fd: int,
    root_name: str,
    root_fd: int,
    expected_root: os.stat_result,
    expected_snapshot: TreeSnapshot | None,
) -> bool:
    try:
        descriptor_before = os.fstat(root_fd)
        live_before = lstat_at(parent_fd, root_name)
        if not _same_entry(descriptor_before, expected_root) or not _same_entry(live_before, expected_root):
            return False
        if expected_snapshot is not None and snapshot_tree(root_fd) != expected_snapshot:
            return False
        descriptor_after = os.fstat(root_fd)
        live_after = lstat_at(parent_fd, root_name)
        descriptor_final = os.fstat(root_fd)
        live_final = lstat_at(parent_fd, root_name)
    except (OSError, SafeFilesystemError):
        return False
    return (
        _same_entry(descriptor_after, expected_root)
        and _same_entry(live_after, expected_root)
        and _same_entry(descriptor_final, expected_root)
        and _same_entry(live_final, expected_root)
    )


def _restore_backup_if_root_absent(parent_fd: int, root_name: str, backup_name: str) -> bool:
    try:
        lstat_at(parent_fd, root_name)
    except FileNotFoundError:
        try:
            atomic_rename_noreplace_at(parent_fd, backup_name, root_name)
        except FileExistsError:
            return False
        return True
    return False


def _publication_state(
    parent_fd: int,
    parent_path: Path,
    root_name: str,
    residue_name: str,
    expected_root: os.stat_result,
    expected_residue: os.stat_result | None,
    expected_snapshot: TreeSnapshot | None = None,
    recovery_receipt: CleanupReceipt | None = None,
    recovery_state: str | None = None,
) -> TransactionOutcome:
    residue = parent_path / residue_name
    try:
        live_root = lstat_at(parent_fd, root_name)
    except FileNotFoundError:
        live_root = None
    state_matches = live_root is not None and _same_entry(live_root, expected_root)
    if state_matches:
        root_fd: int | None = None
        try:
            root_fd = _open_expected_root(parent_fd, root_name, expected_root)
            state_matches = root_fd is not None and _root_descriptor_matches(
                parent_fd,
                root_name,
                root_fd,
                expected_root,
                expected_snapshot,
            )
        except (OSError, SafeFilesystemError):
            state_matches = False
        finally:
            if root_fd is not None:
                os.close(root_fd)
    if not state_matches:
        try:
            current_residue = lstat_at(parent_fd, residue_name)
        except FileNotFoundError:
            return TransactionOutcome(
                "publication_recovery_pending",
                residue,
                _recovery_hint(parent_path / root_name, residue),
                recovery_receipt,
                recovery_state,
            )
        if expected_residue is not None and _same_entry(current_residue, expected_residue):
            if live_root is not None and _same_entry(live_root, expected_root):
                return TransactionOutcome(
                    "publication_recovery_pending",
                    residue,
                    _recovery_hint(parent_path / root_name, residue),
                    recovery_receipt,
                    recovery_state,
                )
            return TransactionOutcome(
                "root_replacement_cleanup_pending",
                residue,
                _cleanup_retry_hint(residue, current_residue),
                recovery_receipt,
                recovery_state,
            )
        return TransactionOutcome(
            "publication_recovery_pending",
            residue,
            _recovery_hint(parent_path / root_name, residue),
            recovery_receipt,
            recovery_state,
        )

    try:
        current_residue = lstat_at(parent_fd, residue_name)
    except FileNotFoundError:
        if recovery_receipt is not None or recovery_state is not None:
            return TransactionOutcome(
                "publication_recovery_pending",
                residue,
                _recovery_hint(parent_path / root_name, residue),
                recovery_receipt,
                recovery_state,
            )
        return TransactionOutcome("published")
    if expected_residue is not None and _same_entry(current_residue, expected_residue):
        return TransactionOutcome(
            "published_cleanup_pending",
            residue,
            _cleanup_retry_hint(residue, current_residue),
            recovery_receipt,
            recovery_state,
        )
    return TransactionOutcome(
        "publication_recovery_pending",
        residue,
        _recovery_hint(parent_path / root_name, residue),
        recovery_receipt,
        recovery_state,
    )


def _cleanup_published_generation(
    parent_fd: int,
    parent_path: Path,
    root_name: str,
    residue_name: str,
    expected_root: os.stat_result,
    expected_residue: os.stat_result,
    expected_snapshot: TreeSnapshot | None = None,
) -> TransactionOutcome:
    """Remove only the old generation still behind the expected live root."""

    residue = parent_path / residue_name
    root_fd = _open_expected_root(parent_fd, root_name, expected_root)
    if root_fd is None or not _root_descriptor_matches(
        parent_fd,
        root_name,
        root_fd,
        expected_root,
        expected_snapshot,
    ):
        if root_fd is not None:
            os.close(root_fd)
        return _publication_state(
            parent_fd,
            parent_path,
            root_name,
            residue_name,
            expected_root,
            expected_residue,
            expected_snapshot,
        )
    try:
        try:
            old_generation = lstat_at(parent_fd, residue_name)
        except FileNotFoundError:
            return TransactionOutcome("publication_recovery_pending", residue, _recovery_hint(parent_path / root_name, residue))
        if not stat.S_ISDIR(old_generation.st_mode) or stat.S_ISLNK(old_generation.st_mode):
            return TransactionOutcome("publication_recovery_pending", residue, _recovery_hint(parent_path / root_name, residue))
        if not _same_entry(old_generation, expected_residue):
            return TransactionOutcome("publication_recovery_pending", residue, _recovery_hint(parent_path / root_name, residue))

        try:
            remove_tree_at(
                parent_fd,
                residue_name,
                expected_metadata=expected_residue,
                guard_name=root_name,
                guard_metadata=expected_root,
                receipt_root=residue,
            )
        except (OSError, SafeFilesystemError) as exc:
            recovery_receipt = exc.cleanup_receipt if isinstance(exc, SafeFilesystemError) else None
            recovery_state = "publication_cleanup_recovery_pending"
            state = _publication_state(
                parent_fd,
                parent_path,
                root_name,
                residue_name,
                expected_root,
                expected_residue,
                expected_snapshot,
                recovery_receipt,
                recovery_state,
            )
            if state.status == "published":
                return TransactionOutcome(
                    "publication_recovery_pending",
                    residue,
                    _recovery_hint(parent_path / root_name, residue),
                    recovery_receipt,
                    recovery_state,
                )
            return state
        if not _root_descriptor_matches(
            parent_fd,
            root_name,
            root_fd,
            expected_root,
            expected_snapshot,
        ):
            return _publication_state(
                parent_fd,
                parent_path,
                root_name,
                residue_name,
                expected_root,
                expected_residue,
                expected_snapshot,
            )
        try:
            current_residue = lstat_at(parent_fd, residue_name)
        except FileNotFoundError:
            pass
        else:
            return _publication_state(
                parent_fd,
                parent_path,
                root_name,
                residue_name,
                expected_root,
                expected_residue,
                expected_snapshot,
            )
        return _publication_state(
            parent_fd,
            parent_path,
            root_name,
            residue_name,
            expected_root,
            None,
            expected_snapshot,
        )
    finally:
        os.close(root_fd)


def _publish_generation_with_recovery(
    parent_fd: int,
    parent_path: Path,
    root_name: str,
    staging_name: str,
    root_metadata: os.stat_result,
    expected_snapshot: TreeSnapshot | None = None,
) -> TransactionOutcome:
    """Fallback for platforms without an atomic directory exchange primitive."""

    backup_name = unique_entry_name(parent_fd, ".pstack-agents-old-")
    staged_metadata = lstat_at(parent_fd, staging_name)
    root_moved = False
    try:
        current = lstat_at(parent_fd, root_name)
        if not _same_entry(current, root_metadata):
            raise ConfigurationError("agents root changed during configuration")
        atomic_rename_noreplace_at(parent_fd, root_name, backup_name)
        root_moved = True
        moved = lstat_at(parent_fd, backup_name)
        if not _same_entry(moved, root_metadata):
            restored = _restore_backup_if_root_absent(parent_fd, root_name, backup_name)
            root_moved = not restored
            raise ConfigurationError(
                f"agents root was replaced during publication; preserved replacement at "
                f"{parent_path / backup_name}"
            )

        try:
            atomic_rename_noreplace_at(parent_fd, staging_name, root_name)
        except BaseException:
            if not _restore_backup_if_root_absent(parent_fd, root_name, backup_name):
                root_moved = True
                raise ConfigurationError(
                    f"agents root appeared during publication; preserved old generation at "
                    f"{parent_path / backup_name}"
                )
            root_moved = False
            raise
        root_moved = False
        try:
            published_metadata = lstat_at(parent_fd, root_name)
        except FileNotFoundError:
            return _publication_state(
                parent_fd,
                parent_path,
                root_name,
                backup_name,
                staged_metadata,
                root_metadata,
                expected_snapshot,
            )
        if not _same_entry(published_metadata, staged_metadata):
            return _publication_state(
                parent_fd,
                parent_path,
                root_name,
                backup_name,
                staged_metadata,
                root_metadata,
                expected_snapshot,
            )
        return _cleanup_published_generation(
            parent_fd,
            parent_path,
            root_name,
            backup_name,
            staged_metadata,
            root_metadata,
            expected_snapshot,
        )
    finally:
        if root_moved:
            try:
                if _restore_backup_if_root_absent(parent_fd, root_name, backup_name):
                    root_moved = False
            except (OSError, SafeFilesystemError):
                # Keep the backup in place. The caller's error names it so a
                # later explicit cleanup/recovery attempt cannot miss it.
                pass


def _publish_generation(
    parent_fd: int,
    parent_path: Path,
    root_name: str,
    staging_name: str,
    root_metadata: os.stat_result | None,
    expected_snapshot: TreeSnapshot | None = None,
) -> TransactionOutcome:
    if root_metadata is None:
        staged_metadata = lstat_at(parent_fd, staging_name)
        try:
            atomic_rename_noreplace_at(parent_fd, staging_name, root_name)
        except FileExistsError as exc:
            raise ConfigurationError(f"agents root appeared during configuration: {root_name}") from exc
        return _publication_state(
            parent_fd,
            parent_path,
            root_name,
            staging_name,
            staged_metadata,
            None,
            expected_snapshot,
        )

    residue = parent_path / staging_name
    staged_metadata = lstat_at(parent_fd, staging_name)
    try:
        atomic_exchange_at(parent_fd, root_name, staging_name)
    except (OSError, SafeFilesystemError) as exc:
        try:
            current_root = lstat_at(parent_fd, root_name)
            current_staging = lstat_at(parent_fd, staging_name)
        except OSError:
            return TransactionOutcome(
                "publication_recovery_pending",
                residue,
                _recovery_hint(parent_path / root_name, residue),
            )
        exchanged = _same_entry(current_root, staged_metadata) and _same_entry(current_staging, root_metadata)
        unchanged = _same_entry(current_root, root_metadata) and _same_entry(current_staging, staged_metadata)
        if exchanged:
            pass
        elif unchanged and isinstance(exc, SafeFilesystemError):
            return _publish_generation_with_recovery(
                parent_fd,
                parent_path,
                root_name,
                staging_name,
                root_metadata,
                expected_snapshot,
            )
        elif unchanged:
            raise
        else:
            return TransactionOutcome(
                "publication_recovery_pending",
                residue,
                _recovery_hint(parent_path / root_name, residue),
            )
    exchanged_old = lstat_at(parent_fd, staging_name)
    if not _same_entry(exchanged_old, root_metadata):
        # The root entry changed after the initial check. If the root still
        # points at our staged generation, exchange back so the replacement
        # remains the live root instead of becoming cleanup residue.
        current_root = lstat_at(parent_fd, root_name)
        if _same_entry(current_root, staged_metadata):
            try:
                atomic_exchange_at(parent_fd, root_name, staging_name)
            except (OSError, SafeFilesystemError) as exc:
                raise ConfigurationError(
                    f"agents root was replaced during publication; generated root remains at "
                    f"{parent_path / root_name} and replacement residue is {parent_path / staging_name}"
                ) from exc
        raise ConfigurationError(
            f"agents root was replaced during publication; preserved replacement at "
            f"{parent_path / root_name}"
        )

    return _cleanup_published_generation(
        parent_fd,
        parent_path,
        root_name,
        staging_name,
        staged_metadata,
        root_metadata,
        expected_snapshot,
    )


def _cleanup_residue(
    residue: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> TransactionOutcome:
    residue = canonical_path(residue)
    if not residue.name.startswith(".pstack-agents-"):
        raise ConfigurationError(f"refusing to clean an unrecognized transaction residue: {residue}")
    try:
        parent_handle = open_directory(residue.parent)
    except (OSError, SafeFilesystemError) as exc:
        raise ConfigurationError(f"could not open residue parent safely: {residue.parent}: {exc}") from exc
    try:
        with directory_lock(parent_handle.fd):
            try:
                metadata = lstat_at(parent_handle.fd, residue.name)
            except FileNotFoundError:
                return TransactionOutcome("cleanup_already_complete")
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ConfigurationError(f"transaction residue is not a real directory: {residue}")
            if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise ConfigurationError(
                    f"cleanup refused (status=cleanup_refused); residue identity changed at {residue}. "
                    "Inspect the replacement before issuing a new cleanup command.",
                    status="cleanup_refused",
                    residue=residue,
                )
            try:
                remove_tree_at(
                    parent_handle.fd,
                    residue.name,
                    expected_metadata=metadata,
                    receipt_root=residue,
                )
            except SafeFilesystemError as exc:
                retry = _cleanup_retry_hint(residue, metadata)
                raise ConfigurationError(
                    f"cleanup refused (status=cleanup_refused); residue is preserved at {residue}. "
                    f"Retry with {retry}: {exc}",
                    status="cleanup_refused",
                    residue=residue,
                    retry=retry,
                    recovery_receipt=exc.cleanup_receipt,
                    recovery_state="cleanup_recovery_pending" if exc.cleanup_receipt is not None else None,
                ) from exc
            except OSError as exc:
                retry = _cleanup_retry_hint(residue, metadata)
                raise ConfigurationError(
                    f"cleanup is incomplete (status=cleanup_pending); residue is preserved at {residue}. "
                    f"Retry with {retry}: {exc}",
                    status="cleanup_pending",
                    residue=residue,
                    retry=retry,
                ) from exc
            sync_directory(parent_handle.fd)
            return TransactionOutcome("cleanup_complete")
    finally:
        close_created_directories(parent_handle.created)
        os.close(parent_handle.fd)


def apply_plan(plan: list[AgentPlan], agents_root: Path, *, allow_replace: bool = False) -> TransactionOutcome:
    conflicts = [item.path for item in plan if item.status == "update"]
    if conflicts and not allow_replace:
        joined = ", ".join(str(path) for path in conflicts)
        raise ConfigurationError(f"existing agent files differ; preview and review before replacing, then pass --replace: {joined}")
    agents_root = canonical_path(agents_root)
    changed = [item for item in plan if item.status in {"create", "update"}]
    if not changed:
        return TransactionOutcome("unchanged")

    parent_handle = open_directory(agents_root.parent, create=True)
    staging_name: str | None = None
    staging_fd: int | None = None
    staging_metadata: os.stat_result | None = None
    root_fd: int | None = None
    completed = False
    primary_error: BaseException | None = None
    outcome: TransactionOutcome | None = None
    published_snapshot: TreeSnapshot | None = None
    recovery_receipt: CleanupReceipt | None = None
    recovery_state: str | None = None
    try:
        with directory_lock(parent_handle.fd):
            try:
                root_fd, current_snapshot, root_metadata = _open_agents_root_at(parent_handle.fd, agents_root.name)
                try:
                    _validate_agents_snapshot(current_snapshot, agents_root)
                    _validate_plan_snapshot(plan, agents_root, current_snapshot)
                    staging_name, staging_fd = create_unique_directory(parent_handle.fd, ".pstack-agents-")
                    staging_metadata = os.fstat(staging_fd)
                    write_snapshot(staging_fd, current_snapshot)
                    for item in changed:
                        write_regular_file_at(
                            staging_fd,
                            item.path.name,
                            item.content.encode("utf-8"),
                            replace=True,
                        )
                    published_snapshot = snapshot_tree(staging_fd)
                    sync_directory(staging_fd)
                    os.close(staging_fd)
                    staging_fd = None
                    outcome = _publish_generation(
                        parent_handle.fd,
                        parent_handle.path,
                        agents_root.name,
                        staging_name,
                        root_metadata,
                        published_snapshot,
                    )
                    published_residue_name = staging_name
                    staging_name = None
                    sync_directory(parent_handle.fd)
                    if outcome.status in {"published", "published_cleanup_pending"}:
                        if staging_metadata is None:
                            raise ConfigurationError("published generation has no identity")
                        for _verification in range(2):
                            outcome = _publication_state(
                                parent_handle.fd,
                                parent_handle.path,
                                agents_root.name,
                                published_residue_name,
                                staging_metadata,
                                root_metadata,
                                published_snapshot,
                            )
                            if outcome.status not in {"published", "published_cleanup_pending"}:
                                break
                    completed = True
                    return outcome
                finally:
                    if root_fd is not None:
                        os.close(root_fd)
                        root_fd = None
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                cleanup_errors: list[str] = []
                if staging_fd is not None:
                    os.close(staging_fd)
                if staging_name is not None:
                    try:
                        remove_tree_at(
                            parent_handle.fd,
                            staging_name,
                            expected_metadata=staging_metadata,
                            receipt_root=parent_handle.path / staging_name,
                        )
                    except (OSError, SafeFilesystemError) as exc:
                        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
                            recovery_receipt = merge_cleanup_receipts(recovery_receipt, exc.cleanup_receipt)
                            recovery_state = "rollback_cleanup_pending"
                        cleanup_errors.append(f"staging residue {parent_handle.path / staging_name}: {exc}")
                if not completed:
                    try:
                        cleanup_created_directories(parent_handle.created)
                    except (OSError, SafeFilesystemError) as exc:
                        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
                            parent_receipt = exc.cleanup_receipt
                        else:
                            parent_receipt = recovery_receipt_for_created_directories(parent_handle.created)
                        recovery_receipt = merge_cleanup_receipts(recovery_receipt, parent_receipt)
                        recovery_state = "rollback_parent_cleanup_pending"
                        cleanup_errors.append(f"created parent cleanup: {exc}")
                        close_created_directories(parent_handle.created)
                else:
                    close_created_directories(parent_handle.created)
                if cleanup_errors:
                    detail = "; ".join(cleanup_errors)
                    retry = next(
                        (
                            part.split(":", 1)[0].removeprefix("staging residue ")
                            for part in cleanup_errors
                            if part.startswith("staging residue ")
                        ),
                        None,
                    )
                    message = f"configuration cleanup is incomplete (status=cleanup_pending): {detail}"
                    residue = Path(retry) if retry else None
                    if recovery_state == "rollback_parent_cleanup_pending":
                        retry_command = _parent_cleanup_retry_hint(recovery_receipt)
                    else:
                        retry_command = _cleanup_retry_hint(residue, staging_metadata) if residue is not None else None
                        if retry_command is None:
                            retry_command = _parent_cleanup_retry_hint(recovery_receipt)
                    if retry_command:
                        message += f". Retry with {retry_command}"
                    if recovery_receipt is not None:
                        message += f". Recovery receipt: {recovery_receipt.render()}"
                    if recovery_state is not None:
                        message += f". Recovery state: {recovery_state}"
                    raise ConfigurationError(
                        message,
                        status="cleanup_pending",
                        residue=residue,
                        retry=retry_command,
                        recovery_receipt=recovery_receipt,
                        recovery_state=recovery_state,
                    ) from primary_error
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, ConfigurationError):
            raise
        if isinstance(exc, (OSError, SafeFilesystemError)):
            raise ConfigurationError(f"configuration failed and was rolled back: {exc}") from exc
        raise
    finally:
        os.close(parent_handle.fd)


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
        reset = build_plan(models, agents_root, budget="medium", default_model="inherit-parent")
        assert all(item.model is None and item.effort is None for item in reset)
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
    cleanup = subparsers.add_parser("cleanup", help="retry removal of a reported transaction residue")
    cleanup.add_argument("--residue", type=Path, required=True, help="residue path reported by an apply")
    cleanup.add_argument("--expected-dev", type=int, help="expected residue device from a cleanup receipt")
    cleanup.add_argument("--expected-ino", type=int, help="expected residue inode from a cleanup receipt")
    cleanup.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "self-test":
            _self_test()
            return 0
        if args.command == "cleanup":
            if (args.expected_dev is None) != (args.expected_ino is None):
                raise ConfigurationError("--expected-dev and --expected-ino must be provided together")
            expected_identity = None
            if args.expected_dev is not None and args.expected_ino is not None:
                expected_identity = (args.expected_dev, args.expected_ino)
            outcome = _cleanup_residue(args.residue, expected_identity=expected_identity)
            print(json.dumps(outcome.as_dict(), indent=2 if args.json else None, sort_keys=True))
            return 0
        if args.command == "discover":
            result = discover_models(args.codex, bundled=args.bundled, include_hidden=args.include_hidden)
            print(json.dumps(result, indent=2 if args.json else None, sort_keys=True))
            return 0
        models = load_catalog(args.catalog)
        plan = build_plan(
            models,
            args.agents_root,
            budget=args.budget,
            default_model=args.default_model,
            role_values=args.role,
        )
        outcome = None
        if args.command == "apply":
            outcome = apply_plan(plan, args.agents_root, allow_replace=args.replace)
        elif args.replace:
            raise ConfigurationError("--replace is only valid with apply")
        result = {"action": args.command, "agents": [item.as_dict() for item in plan]}
        if outcome is not None:
            result["transaction"] = outcome.as_dict()
        print(json.dumps(result, indent=2 if args.json else None, sort_keys=True))
        return 0
    except (ConfigurationError, OSError, SafeFilesystemError) as exc:
        if isinstance(exc, ConfigurationError) and getattr(args, "json", False):
            print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
            return 1
        print(f"setup-pstack configuration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
