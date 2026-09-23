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
import shlex
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
    CleanupReceipt,
    SafeFilesystemError,
    TreeSnapshot,
    atomic_rename_noreplace_at,
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
    write_snapshot,
    canonical_path,
)


class InstallError(RuntimeError):
    """Raised when a skill cannot be installed without risking a collision."""

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
class CleanupOutcome:
    status: str
    residue: Path | None = None
    retry: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"status": self.status}
        if self.residue is not None:
            result["residue"] = str(self.residue)
        if self.retry is not None:
            result["retry"] = self.retry
        return result


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


def _cleanup_retry_hint(residue: Path, expected_metadata: os.stat_result | None = None) -> str:
    command = [
        sys.executable,
        str(SCRIPT_DIRECTORY / "install.py"),
        "cleanup",
        "--residue",
        str(residue),
    ]
    if expected_metadata is not None:
        command.extend(("--expected-dev", str(expected_metadata.st_dev), "--expected-ino", str(expected_metadata.st_ino)))
    return shlex.join(command)


def _parent_cleanup_retry_hint(receipt: CleanupReceipt | None) -> str | None:
    if receipt is None:
        return None
    paths = ", ".join(str(entry.path) for entry in receipt.entries)
    if receipt.truncated:
        paths += ", ..."
    return f"inspect created-parent identities ({paths}) before retrying the original install command"


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


def _cleanup_residue(
    residue: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> CleanupOutcome:
    residue = canonical_path(residue)
    if not residue.name.startswith(".setup-pstack-install-"):
        raise InstallError(f"refusing to clean an unrecognized install residue: {residue}")
    try:
        parent_handle = open_directory(residue.parent)
    except (OSError, SafeFilesystemError) as exc:
        raise InstallError(f"could not open install residue parent safely: {residue.parent}: {exc}") from exc
    try:
        with directory_lock(parent_handle.fd):
            try:
                metadata = lstat_at(parent_handle.fd, residue.name)
            except FileNotFoundError:
                return CleanupOutcome("cleanup_already_complete")
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise InstallError(f"install residue is not a real directory: {residue}")
            if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise InstallError(
                    f"install cleanup refused (status=cleanup_refused); residue identity changed at {residue}. "
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
                raise InstallError(
                    f"install cleanup refused (status=cleanup_refused); residue is preserved at {residue}. "
                    f"Retry with {retry}: {exc}",
                    status="cleanup_refused",
                    residue=residue,
                    retry=retry,
                    recovery_receipt=exc.cleanup_receipt,
                    recovery_state="cleanup_recovery_pending" if exc.cleanup_receipt is not None else None,
                ) from exc
            except OSError as exc:
                retry = _cleanup_retry_hint(residue, metadata)
                raise InstallError(
                    f"install cleanup is incomplete (status=cleanup_pending); residue is preserved at {residue}. "
                    f"Retry with {retry}: {exc}",
                    status="cleanup_pending",
                    residue=residue,
                    retry=retry,
                ) from exc
            sync_directory(parent_handle.fd)
            return CleanupOutcome("cleanup_complete")
    finally:
        close_created_directories(parent_handle.created)
        os.close(parent_handle.fd)


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
    staging_metadata: os.stat_result | None = None
    completed = False
    primary_error: InstallError | None = None
    recovery_receipt: CleanupReceipt | None = None
    recovery_state: str | None = None
    try:
        with directory_lock(destination_handle.fd):
            try:
                plan = _target_plan(source, destination, destination_handle.fd, snapshot)
                if plan.status == "unchanged":
                    completed = True
                    return plan
                if plan.status != "create":
                    raise InstallError(plan.reason or f"cannot install into {plan.target}")

                staging_name, staging_fd = create_unique_directory(destination_handle.fd, ".setup-pstack-install-")
                staging_metadata = os.fstat(staging_fd)
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
            except BaseException as exc:
                if isinstance(exc, InstallError):
                    primary_error = exc
                else:
                    primary_error = InstallError(f"install failed and was rolled back: {exc}")
                raise
            finally:
                cleanup_errors: list[str] = []
                if staging_fd is not None:
                    os.close(staging_fd)
                if staging_name is not None:
                    try:
                        remove_tree_at(
                            destination_handle.fd,
                            staging_name,
                            expected_metadata=staging_metadata,
                            receipt_root=destination_handle.path / staging_name,
                        )
                    except (OSError, SafeFilesystemError) as exc:
                        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
                            recovery_receipt = merge_cleanup_receipts(recovery_receipt, exc.cleanup_receipt)
                            recovery_state = "rollback_cleanup_pending"
                        cleanup_errors.append(f"staging residue {destination_handle.path / staging_name}: {exc}")
                if not completed:
                    try:
                        cleanup_created_directories(destination_handle.created)
                    except (OSError, SafeFilesystemError) as exc:
                        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
                            parent_receipt = exc.cleanup_receipt
                        else:
                            parent_receipt = recovery_receipt_for_created_directories(destination_handle.created)
                        recovery_receipt = merge_cleanup_receipts(recovery_receipt, parent_receipt)
                        recovery_state = "rollback_parent_cleanup_pending"
                        cleanup_errors.append(f"created parent cleanup: {exc}")
                else:
                    close_created_directories(destination_handle.created)
                if cleanup_errors:
                    detail = "; ".join(cleanup_errors)
                    residue_text = next(
                        (
                            part.split(":", 1)[0].removeprefix("staging residue ")
                            for part in cleanup_errors
                            if part.startswith("staging residue ")
                        ),
                        None,
                    )
                    residue = Path(residue_text) if residue_text else None
                    if recovery_state == "rollback_parent_cleanup_pending":
                        retry = _parent_cleanup_retry_hint(recovery_receipt)
                    else:
                        retry = _cleanup_retry_hint(residue, staging_metadata) if residue is not None else None
                        if retry is None:
                            retry = _parent_cleanup_retry_hint(recovery_receipt)
                    message = f"install rollback cleanup is incomplete (status=cleanup_pending): {detail}"
                    if residue is not None and retry is not None:
                        message += f". Retry with {retry}"
                    if recovery_receipt is not None:
                        message += f". Recovery receipt: {recovery_receipt.render()}"
                    if recovery_state is not None:
                        message += f". Recovery state: {recovery_state}"
                    raise InstallError(
                        message,
                        status="cleanup_pending",
                        residue=residue,
                        retry=retry,
                        recovery_receipt=recovery_receipt,
                        recovery_state=recovery_state,
                    ) from primary_error
    except (OSError, SafeFilesystemError) as exc:
        if primary_error is None:
            primary_error = InstallError(f"install failed and was rolled back: {exc}")
        raise primary_error from exc
    finally:
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
    parser.add_argument("command", nargs="?", choices=("cleanup",), help="recover a reported staging residue")
    parser.add_argument("--source", type=Path, help="skill directory containing SKILL.md")
    parser.add_argument("--destination", type=Path, help="explicit Codex discovery skills directory")
    parser.add_argument("--residue", type=Path, help="reported staging residue for cleanup")
    parser.add_argument("--expected-dev", type=int, help="expected residue device from a cleanup receipt")
    parser.add_argument("--expected-ino", type=int, help="expected residue inode from a cleanup receipt")
    parser.add_argument("--apply", action="store_true", help="apply the preview")
    parser.add_argument("--json", action="store_true", help="print a machine-readable plan")
    parser.add_argument("--self-test", action="store_true", help="exercise isolated install scenarios")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        _self_test()
        return 0
    if args.command == "cleanup":
        if not args.residue:
            raise SystemExit("--residue is required for cleanup")
        if (args.expected_dev is None) != (args.expected_ino is None):
            raise SystemExit("--expected-dev and --expected-ino must be provided together")
        try:
            expected_identity = None
            if args.expected_dev is not None and args.expected_ino is not None:
                expected_identity = (args.expected_dev, args.expected_ino)
            outcome = _cleanup_residue(args.residue, expected_identity=expected_identity)
            print(json.dumps(outcome.as_dict(), indent=2 if args.json else None, sort_keys=True))
            return 0
        except InstallError as exc:
            if args.json:
                print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
            else:
                print(f"install cleanup failed: {exc}", file=sys.stderr)
            return 1
    if not args.source or not args.destination:
        raise SystemExit("--source and --destination are required unless --self-test is used")
    try:
        plan = apply_install(args.source, args.destination) if args.apply else preview_install(args.source, args.destination)
        print(_format_result(plan, args.json))
        return 0
    except InstallError as exc:
        if args.json:
            print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
