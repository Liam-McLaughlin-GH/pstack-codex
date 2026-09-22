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
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from safe_fs import (  # noqa: E402
    SafeFilesystemError,
    TreeSnapshot,
    atomic_rename_noreplace_at,
    cleanup_created_directories,
    close_created_directories,
    create_unique_directory,
    directory_lock,
    lstat_at,
    open_child_directory,
    open_directory,
    remove_tree_at,
    snapshot_tree,
    sync_directory,
    write_snapshot,
    canonical_path,
)


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


def _validate_skill_name(name: str) -> None:
    if not name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name):
        raise InstallError(f"invalid skill directory name: {name!r}")


def _source_snapshot(source: Path) -> TreeSnapshot:
    source = canonical_path(source)
    try:
        handle = open_directory(source)
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"skill source is not a real no-follow directory: {source}: {exc}") from exc
    try:
        snapshot = snapshot_tree(handle.fd, reject_symlinks=True)
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"skill source contains an unsafe entry: {source}: {exc}") from exc
    finally:
        os.close(handle.fd)
        close_created_directories(handle.created)
    if "SKILL.md" not in snapshot.files:
        raise InstallError(f"skill source is missing SKILL.md: {source}")
    return snapshot


def _validate_source(source: Path) -> tuple[str, ...]:
    return tuple(sorted(_source_snapshot(source).files))


def _read_tree(root: Path) -> TreeSnapshot:
    root = canonical_path(root)
    try:
        handle = open_directory(root)
        try:
            return snapshot_tree(handle.fd, reject_symlinks=True)
        finally:
            os.close(handle.fd)
            close_created_directories(handle.created)
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"could not read no-follow tree {root}: {exc}") from exc


def _same_contents(source: Path, target: Path) -> bool:
    try:
        source_tree = _source_snapshot(source)
        target_tree = _read_tree(target)
    except InstallError:
        return False
    return (
        source_tree.directories == target_tree.directories
        and source_tree.files == target_tree.files
        and not target_tree.symlinks
    )


def _validate_destination(destination: Path) -> None:
    destination = canonical_path(destination)
    try:
        handle = open_directory(destination)
    except FileNotFoundError:
        return
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"destination path is not a real no-follow directory: {destination}: {exc}") from exc
    else:
        os.close(handle.fd)
        close_created_directories(handle.created)


def _target_plan(source: Path, destination: Path, destination_fd: int, snapshot: TreeSnapshot) -> InstallPlan:
    target = destination / source.name
    try:
        metadata = lstat_at(destination_fd, source.name)
    except FileNotFoundError:
        return InstallPlan(source, destination, target, "create", tuple(sorted(snapshot.files)))
    except OSError as exc:
        raise InstallError(f"could not inspect install target {target}: {exc}") from exc

    files = tuple(sorted(snapshot.files))
    if stat.S_ISLNK(metadata.st_mode):
        return InstallPlan(
            source,
            destination,
            target,
            "conflict",
            files,
            "target already exists as a symlink; broken symlinks are retained",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return InstallPlan(source, destination, target, "conflict", files, "target is not a directory")
    try:
        target_fd = open_child_directory(destination_fd, source.name)
        try:
            target_tree = snapshot_tree(target_fd, reject_symlinks=True)
        finally:
            os.close(target_fd)
    except (OSError, SafeFilesystemError):
        return InstallPlan(
            source,
            destination,
            target,
            "conflict",
            files,
            "target directory contains an unsafe entry",
        )
    if (
        snapshot.directories == target_tree.directories
        and snapshot.files == target_tree.files
        and not target_tree.symlinks
    ):
        return InstallPlan(source, destination, target, "unchanged", files)
    return InstallPlan(
        source,
        destination,
        target,
        "conflict",
        files,
        "target directory exists with different contents",
    )


def preview_install(source: Path, destination: Path) -> InstallPlan:
    """Build an install plan without creating or changing a path."""

    source = canonical_path(source)
    destination = canonical_path(destination)
    _validate_skill_name(source.name)
    snapshot = _source_snapshot(source)
    _validate_destination(destination)
    try:
        handle = open_directory(destination)
    except FileNotFoundError:
        return InstallPlan(source, destination, destination / source.name, "create", tuple(sorted(snapshot.files)))
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"could not open destination {destination}: {exc}") from exc
    try:
        return _target_plan(source, destination, handle.fd, snapshot)
    finally:
        os.close(handle.fd)
        close_created_directories(handle.created)


def _destination_target_is_absent(destination_fd: int, name: str) -> None:
    try:
        lstat_at(destination_fd, name)
    except FileNotFoundError:
        return
    raise InstallError(f"target appeared during install: {name}")


def apply_install(source: Path, destination: Path) -> InstallPlan:
    """Install a skill from a stable snapshot with atomic directory publication."""

    source = canonical_path(source)
    destination = canonical_path(destination)
    _validate_skill_name(source.name)
    snapshot = _source_snapshot(source)
    try:
        destination_handle = open_directory(destination, create=True)
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"could not open destination without following a link: {destination}: {exc}") from exc

    staging_name: str | None = None
    staging_fd: int | None = None
    completed = False
    try:
        with directory_lock(destination_handle.fd):
            plan = _target_plan(source, destination, destination_handle.fd, snapshot)
            if plan.status == "unchanged":
                completed = True
                return plan
            if plan.status != "create":
                raise InstallError(plan.reason or f"cannot install into {plan.target}")

            staging_name, staging_fd = create_unique_directory(destination_handle.fd, ".setup-pstack-install-")
            write_snapshot(
                staging_fd,
                TreeSnapshot(snapshot.directories, snapshot.files, {}),
            )
            sync_directory(staging_fd)
            _destination_target_is_absent(destination_handle.fd, source.name)
            try:
                atomic_rename_noreplace_at(destination_handle.fd, staging_name, source.name)
            except FileExistsError as exc:
                raise InstallError(f"target appeared during install: {source.name}") from exc
            staging_name = None
            os.close(staging_fd)
            staging_fd = None
            sync_directory(destination_handle.fd)
            completed = True
            return plan
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"install failed and was rolled back: {exc}") from exc
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        if staging_name is not None:
            try:
                remove_tree_at(destination_handle.fd, staging_name)
            except OSError:
                pass
        if not completed:
            try:
                cleanup_created_directories(destination_handle.created)
            except OSError:
                pass
        else:
            close_created_directories(destination_handle.created)
        os.close(destination_handle.fd)


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
        assert not rollback_destination.exists()

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
