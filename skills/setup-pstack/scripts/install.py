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


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_skill_name(name: str) -> None:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
        raise InstallError(f"invalid skill directory name: {name!r}")


def _validate_source(source: Path) -> tuple[str, ...]:
    source = source.resolve()
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
                resolved = candidate.resolve(strict=False)
                if not _is_within(resolved, source):
                    raise InstallError(f"source symlink escapes skill: {candidate}")
                if not resolved.exists():
                    raise InstallError(f"source directory symlink is broken: {candidate}")
        for filename in filenames:
            candidate = current_path / filename
            relative = candidate.relative_to(source).as_posix()
            if candidate.is_symlink():
                resolved = candidate.resolve(strict=False)
                if not _is_within(resolved, source):
                    raise InstallError(f"source symlink escapes skill: {candidate}")
                # Keep an internal broken link in the source long enough for
                # copytree to fail and exercise the transaction cleanup path.
            elif not candidate.is_file():
                raise InstallError(f"source contains a non-file entry: {candidate}")
            files.append(relative)
    return tuple(files)


def _validate_destination(destination: Path) -> None:
    """Reject an existing symlink anywhere in the destination path."""

    current = destination
    while True:
        if current.is_symlink():
            raise InstallError(f"destination path contains a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise InstallError(f"destination path is not a directory: {current}")
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


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


def preview_install(source: Path, destination: Path) -> InstallPlan:
    """Build an install plan without creating or changing a path."""

    source = source.resolve()
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
    """Install a skill with a staged copy and cleanup on every failure."""

    plan = preview_install(source, destination)
    if plan.status != "create":
        if plan.status == "unchanged":
            return plan
        raise InstallError(plan.reason or f"cannot install into {plan.target}")

    destination = plan.destination
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir() or destination.is_symlink():
        raise InstallError(f"destination is not a real directory: {destination}")

    staging = Path(tempfile.mkdtemp(prefix=f".{plan.source.name}.install-", dir=destination))
    target_created = False
    completed = False
    moved_children: list[Path] = []
    try:
        # Do not dereference a symlink outside the validated source tree.
        shutil.copytree(plan.source, staging, symlinks=False, dirs_exist_ok=True)

        # Reserve the final name. mkdir(exist_ok=False) refuses both real
        # collisions and broken symlinks without following or deleting them.
        plan.target.mkdir()
        target_created = True
        for child in sorted(staging.iterdir(), key=lambda path: path.name):
            moved = plan.target / child.name
            shutil.move(str(child), str(moved))
            moved_children.append(moved)
        staging.rmdir()
        completed = True
        return plan
    except FileExistsError as exc:
        raise InstallError(f"target appeared during install: {plan.target}") from exc
    except (OSError, shutil.Error) as exc:
        raise InstallError(f"install failed and was rolled back: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if target_created and not completed and plan.target.exists():
            # Remove only content created by this invocation. If another
            # writer appears while the transaction is open, preserve entries
            # that were not moved from our staging directory.
            try:
                known_names = {path.name for path in moved_children}
                unknown_children = [child for child in plan.target.iterdir() if child.name not in known_names]
                if not unknown_children:
                    shutil.rmtree(plan.target)
                else:
                    for child in moved_children:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        elif child.exists() or child.is_symlink():
                            child.unlink()
            except OSError:
                pass


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
