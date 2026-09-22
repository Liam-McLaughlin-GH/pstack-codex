#!/usr/bin/env python3
"""Install one skill into an explicit Codex discovery directory.

The command is deliberately scoped to a caller-supplied destination. It never
selects a home directory, follows an existing destination symlink, or replaces
an existing directory with different contents.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class InstallError(RuntimeError):
    """Raised when a skill cannot be installed without risking a collision."""


def _is_trusted_system_symlink(path: Path) -> bool:
    if path not in {Path("/var"), Path("/tmp")}:
        return False
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return resolved in {Path("/private/var"), Path("/private/tmp")}


@dataclass(frozen=True)
class InstallPlan:
    source: Path
    destination: Path
    target: Path
    status: str
    files: tuple[str, ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source": str(self.source),
            "destination": str(self.destination),
            "target": str(self.target),
            "status": self.status,
            "files": list(self.files),
        }
        if self.reason:
            result["reason"] = self.reason
        return result


def _validate_skill_name(name: str) -> None:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
        raise InstallError(f"invalid skill directory name: {name!r}")


def _validate_source(source: Path) -> tuple[str, ...]:
    source = source.expanduser().absolute()
    if source.is_symlink():
        raise InstallError(f"skill source is a symlink: {source}")
    if not source.is_dir():
        raise InstallError(f"skill source is not a directory: {source}")
    if not (source / "SKILL.md").is_file():
        raise InstallError(f"skill source is missing SKILL.md: {source}")

    files: list[str] = []
    for current, directories, filenames in os.walk(source, followlinks=False):
        current_path = Path(current)
        directories.sort()
        filenames.sort()
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise InstallError(f"source contains a symlink: {candidate}")
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(source).as_posix()
            if candidate.is_symlink():
                raise InstallError(f"source contains a symlink: {candidate}")
            elif not candidate.is_file():
                raise InstallError(f"source contains a non-file entry: {candidate}")
            files.append(relative)
    return tuple(files)


def _validate_destination(destination: Path) -> None:
    """Reject an existing symlink anywhere in the destination path."""

    destination = destination.expanduser().absolute()
    current = Path(destination.anchor)
    for component in destination.parts[1:]:
        current /= component
        if current.is_symlink() and not _is_trusted_system_symlink(current):
            raise InstallError(f"destination path contains a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise InstallError(f"destination path is not a directory: {current}")


def _read_tree(root: Path) -> dict[str, bytes]:
    """Read regular files without following destination directory links."""

    values: dict[str, bytes] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.sort()
        filenames.sort()
        if any((current_path / directory).is_symlink() for directory in directories):
            raise InstallError(f"destination contains a directory symlink: {current_path}")
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise InstallError(f"destination contains a file symlink: {candidate}")
            if not candidate.is_file():
                raise InstallError(f"destination contains a non-file entry: {candidate}")
            values[relative] = candidate.read_bytes()
    return values


def _same_contents(source: Path, target: Path) -> bool:
    try:
        source_files = _read_tree(source)
        target_files = _read_tree(target)
    except InstallError:
        return False
    return source_files == target_files


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


def preview_install(source: Path, destination: Path) -> InstallPlan:
    """Build an install plan without creating or changing a path."""

    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    _validate_destination(destination)
    files = _validate_source(source)
    target = destination / source.name
    _validate_skill_name(source.name)

    if not os.path.lexists(target):
        return InstallPlan(source, destination, target, "create", files)
    if target.is_symlink():
        return InstallPlan(
            source,
            destination,
            target,
            "conflict",
            files,
            "target already exists as a symlink; broken symlinks are retained",
        )
    if not target.is_dir():
        return InstallPlan(source, destination, target, "conflict", files, "target is not a directory")
    if _same_contents(source, target):
        return InstallPlan(source, destination, target, "unchanged", files)
    return InstallPlan(
        source,
        destination,
        target,
        "conflict",
        files,
        "target directory exists with different contents",
    )


def apply_install(source: Path, destination: Path) -> InstallPlan:
    """Install a skill with atomic staged publication and cleanup on failure."""

    plan = preview_install(source, destination)
    if plan.status != "create":
        if plan.status == "unchanged":
            return plan
        raise InstallError(plan.reason or f"cannot install into {plan.target}")

    destination = plan.destination
    created_directories: list[Path] = []
    staging: Path | None = None
    completed = False
    try:
        created_directories = _missing_directories(destination)
        for directory in reversed(created_directories):
            directory.mkdir()
        _validate_destination(destination)
        staging = Path(tempfile.mkdtemp(prefix=f".{plan.source.name}.install-", dir=destination))
        _validate_source(plan.source)
        shutil.copytree(plan.source, staging, symlinks=False, dirs_exist_ok=True)
        _validate_source(plan.source)
        if os.path.lexists(plan.target):
            raise InstallError(f"target appeared during install: {plan.target}")
        os.rename(staging, plan.target)
        staging = None
        completed = True
        return plan
    except (OSError, shutil.Error) as exc:
        raise InstallError(f"install failed and was rolled back: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if not completed:
            _cleanup_empty_directories(created_directories)


def _format_result(plan: InstallPlan, as_json: bool) -> str:
    if as_json:
        return json.dumps(plan.as_dict(), sort_keys=True)
    message = f"{plan.status}: {plan.target}"
    if plan.reason:
        message += f" ({plan.reason})"
    return message


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="setup-pstack-install-test-") as temporary:
        root = Path(temporary)
        source = root / "source" / "setup-pstack"
        destination = root / "repo" / ".agents" / "skills"
        (source / "references").mkdir(parents=True)
        (source / "SKILL.md").write_text("---\nname: setup-pstack\ndescription: test\n---\n", encoding="utf-8")
        (source / "references" / "runtime.md").write_text("runtime\n", encoding="utf-8")

        preview = preview_install(source, destination)
        assert preview.status == "create"
        assert not destination.exists()
        applied = apply_install(source, destination)
        assert applied.status == "create"
        assert (destination / "setup-pstack" / "SKILL.md").is_file()
        assert apply_install(source, destination).status == "unchanged"

        existing = root / "existing" / "setup-pstack"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("different\n", encoding="utf-8")
        try:
            apply_install(source, existing.parent)
        except InstallError:
            pass
        else:
            raise AssertionError("different existing directory was overwritten")
        assert (existing / "SKILL.md").read_text(encoding="utf-8") == "different\n"

        broken_parent = root / "broken" / ".agents" / "skills"
        broken_parent.mkdir(parents=True)
        os.symlink(str(root / "missing-target"), broken_parent / "setup-pstack")
        try:
            apply_install(source, broken_parent)
        except InstallError:
            pass
        else:
            raise AssertionError("broken symlink was replaced")
        assert (broken_parent / "setup-pstack").is_symlink()

        bad_source = root / "bad-source" / "setup-pstack"
        bad_source.mkdir(parents=True)
        (bad_source / "SKILL.md").write_text("valid\n", encoding="utf-8")
        os.symlink("missing.txt", bad_source / "zz-broken")
        rollback_destination = root / "rollback" / ".agents" / "skills"
        try:
            apply_install(bad_source, rollback_destination)
        except InstallError:
            pass
        else:
            raise AssertionError("broken source did not fail")
        assert not (rollback_destination / "setup-pstack").exists()
        assert not list(rollback_destination.glob(".setup-pstack.install-*")) if rollback_destination.exists() else True

    print("setup-pstack installer self-check passed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="skill directory containing SKILL.md")
    parser.add_argument("--destination", type=Path, help="explicit Codex discovery skills directory")
    parser.add_argument("--apply", action="store_true", help="apply the preview")
    parser.add_argument("--json", action="store_true", help="print a machine-readable plan")
    parser.add_argument("--self-test", action="store_true", help="exercise isolated install scenarios")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    if not args.source or not args.destination:
        raise SystemExit("--source and --destination are required unless --self-test is used")
    try:
        plan = apply_install(args.source, args.destination) if args.apply else preview_install(args.source, args.destination)
        print(_format_result(plan, args.json))
        return 0
    except InstallError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
