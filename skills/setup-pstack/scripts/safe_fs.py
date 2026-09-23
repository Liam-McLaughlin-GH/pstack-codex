"""Small POSIX helpers for no-follow, directory-handle based file updates."""

from __future__ import annotations

import errno
import ctypes
import fcntl
import json
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class SafeFilesystemError(RuntimeError):
    """Raised when a requested path cannot be used without following a link."""

    def __init__(
        self,
        message: str,
        *,
        cleanup_receipt: "CleanupReceipt | None" = None,
        preserve_private: bool = False,
    ) -> None:
        super().__init__(message)
        self.cleanup_receipt = cleanup_receipt
        self.recovery_receipt = cleanup_receipt
        self.receipt = cleanup_receipt
        self.preserve_private = preserve_private


@dataclass(frozen=True)
class CleanupReceiptEntry:
    path: Path
    expected_dev: int
    expected_ino: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "expected_dev": self.expected_dev,
            "expected_ino": self.expected_ino,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CleanupReceipt:
    entries: tuple[CleanupReceiptEntry, ...]
    truncated: bool = False

    @classmethod
    def from_entries(cls, entries: list[CleanupReceiptEntry]) -> "CleanupReceipt":
        ordered = sorted(entries, key=lambda entry: (str(entry.path), entry.expected_dev, entry.expected_ino, entry.reason))
        return cls(tuple(ordered[:8]), len(ordered) > 8)

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.as_dict() for entry in self.entries],
            "truncated": self.truncated,
        }

    def render(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass
class CreatedDirectory:
    parent_fd: int
    name: str
    path: Path
    metadata: os.stat_result


@dataclass
class DirectoryHandle:
    fd: int
    path: Path
    created: list[CreatedDirectory]


@dataclass(frozen=True)
class TreeSnapshot:
    directories: tuple[str, ...]
    files: dict[str, bytes]
    symlinks: dict[str, str]


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_CREATE_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def canonical_path(path: Path) -> Path:
    """Normalize a path while canonicalizing only macOS's trusted temp links."""

    normalized = Path(os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(path)))))
    if len(normalized.parts) > 1 and normalized.parts[1] in {"tmp", "var"}:
        system_path = Path("/") / normalized.parts[1]
        if system_path.is_symlink():
            return Path("/private") / normalized.parts[1] / Path(*normalized.parts[2:])
    return normalized


def _path_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    normalized = canonical_path(path)
    if normalized.anchor != "/":
        raise SafeFilesystemError(f"only absolute paths are supported: {path}")
    return normalized, normalized.parts[1:]


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafeFilesystemError(f"path component is a symlink: {name}") from exc
        if exc.errno == errno.ENOTDIR:
            raise SafeFilesystemError(f"path component is not a directory: {name}") from exc
        raise


def open_directory(path: Path, *, create: bool = False) -> DirectoryHandle:
    """Open every path component with no-follow semantics.

    The returned descriptor remains attached to the original directory if an
    ancestor is renamed or replaced after this function returns. Newly-created
    directories retain stable parent descriptors for safe rollback.
    """

    normalized, components = _path_parts(path)
    current_fd = os.open("/", _DIRECTORY_FLAGS & ~getattr(os, "O_NOFOLLOW", 0))
    current_path = Path("/")
    created: list[CreatedDirectory] = []
    try:
        for component in components:
            try:
                child_fd = _open_child_directory(current_fd, component)
            except FileNotFoundError:
                if not create:
                    raise
                parent_for_cleanup = os.dup(current_fd)
                try:
                    os.mkdir(component, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    os.close(parent_for_cleanup)
                else:
                    try:
                        metadata = lstat_at(current_fd, component)
                    except BaseException:
                        os.close(parent_for_cleanup)
                        raise
                    created.append(CreatedDirectory(parent_for_cleanup, component, current_path / component, metadata))
                child_fd = _open_child_directory(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
            current_path /= component
        return DirectoryHandle(current_fd, normalized, created)
    except BaseException:
        os.close(current_fd)
        try:
            cleanup_created_directories(created)
        except (OSError, SafeFilesystemError):
            close_created_directories(created)
        raise


def open_child_directory(parent_fd: int, name: str) -> int:
    return _open_child_directory(parent_fd, name)


def close_created_directories(created: list[CreatedDirectory]) -> None:
    for entry in created:
        try:
            os.close(entry.parent_fd)
        except OSError:
            pass


def cleanup_created_directories(created: list[CreatedDirectory]) -> None:
    failures: list[CleanupReceiptEntry] = []
    for entry in reversed(created):
        try:
            try:
                current = lstat_at(entry.parent_fd, entry.name)
            except FileNotFoundError:
                continue
            if not _same_entry(current, entry.metadata):
                raise SafeFilesystemError("created directory identity changed during cleanup")
            if not stat.S_ISDIR(current.st_mode):
                raise SafeFilesystemError("created path is no longer a directory during cleanup")
            _remove_owned_empty_directory(entry.parent_fd, entry.name, entry.metadata)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                reason = "not-empty" if exc.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY} else "cleanup-error"
                failures.append(
                    CleanupReceiptEntry(
                        entry.path,
                        entry.metadata.st_dev,
                        entry.metadata.st_ino,
                        reason,
                    )
                )
        except SafeFilesystemError as exc:
            reason = (
                "replaced"
                if "identity" in str(exc) or "no longer a directory" in str(exc) or "replacement" in str(exc)
                else "cleanup-error"
            )
            failures.append(
                CleanupReceiptEntry(
                    entry.path,
                    entry.metadata.st_dev,
                    entry.metadata.st_ino,
                    reason,
                )
            )
        finally:
            try:
                os.close(entry.parent_fd)
            except OSError:
                pass
    if failures:
        receipt = CleanupReceipt.from_entries(failures)
        detail = ", ".join(f"{entry.path} ({entry.reason})" for entry in receipt.entries)
        if receipt.truncated:
            detail += ", ..."
        raise SafeFilesystemError(
            "created directory cleanup incomplete: " + detail,
            cleanup_receipt=receipt,
        )


def recovery_receipt_for_created_directories(
    created: list[CreatedDirectory],
    *,
    reason: str = "rollback-parent-cleanup",
) -> CleanupReceipt:
    return CleanupReceipt.from_entries(
        [
            CleanupReceiptEntry(
                entry.path,
                entry.metadata.st_dev,
                entry.metadata.st_ino,
                reason,
            )
            for entry in created
        ]
    )


def merge_cleanup_receipts(*receipts: CleanupReceipt | None) -> CleanupReceipt | None:
    """Combine bounded recovery receipts without losing earlier failures."""

    present = [receipt for receipt in receipts if receipt is not None]
    if not present:
        return None
    entries = [entry for receipt in present for entry in receipt.entries]
    merged = CleanupReceipt.from_entries(entries)
    return CleanupReceipt(merged.entries, merged.truncated or any(receipt.truncated for receipt in present))


@contextmanager
def directory_lock(fd: int):
    """Serialize writers that publish through the same stable directory."""

    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lstat_at(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _cleanup_receipt_path(
    receipt_root: Path | None,
    relative: tuple[str, ...],
    fallback_name: str,
) -> Path:
    if receipt_root is not None:
        return receipt_root.joinpath(*relative) if relative else receipt_root
    return Path(*relative) if relative else Path(fallback_name)


def _with_cleanup_receipt(
    error: BaseException,
    *,
    path: Path,
    expected_metadata: os.stat_result,
    reason: str,
) -> SafeFilesystemError:
    entries: list[CleanupReceiptEntry] = []
    if isinstance(error, SafeFilesystemError) and error.cleanup_receipt is not None:
        entries.extend(error.cleanup_receipt.entries)
    entries.append(
        CleanupReceiptEntry(
            path,
            expected_metadata.st_dev,
            expected_metadata.st_ino,
            reason,
        )
    )
    return SafeFilesystemError(
        str(error),
        cleanup_receipt=CleanupReceipt.from_entries(entries),
        preserve_private=getattr(error, "preserve_private", False),
    )


def read_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    expected_metadata: os.stat_result | None = None,
) -> bytes:
    expected_metadata = expected_metadata or lstat_at(directory_fd, name)
    if not stat.S_ISREG(expected_metadata.st_mode):
        raise SafeFilesystemError(f"file is not regular: {name}")
    try:
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafeFilesystemError(f"file is a symlink: {name}") from exc
        raise
    try:
        opened_metadata = os.fstat(file_fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise SafeFilesystemError(f"file is not regular: {name}")
        if not _same_entry(opened_metadata, expected_metadata):
            raise SafeFilesystemError(f"file identity changed during read: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(file_fd)


def _write_all(file_fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def write_regular_file_at(directory_fd: int, name: str, content: bytes, *, replace: bool = False) -> None:
    flags = _CREATE_FILE_FLAGS if not replace else (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_fd = os.open(name, flags, 0o644, dir_fd=directory_fd)
    try:
        _write_all(file_fd, content)
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def create_unique_directory(parent_fd: int, prefix: str, *, mode: int = 0o755) -> tuple[str, int]:
    for _attempt in range(100):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            os.mkdir(name, mode, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            return name, _open_child_directory(parent_fd, name)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    raise SafeFilesystemError(f"could not allocate a temporary directory in {parent_fd}")


def unique_entry_name(parent_fd: int, prefix: str) -> str:
    for _attempt in range(100):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            lstat_at(parent_fd, name)
        except FileNotFoundError:
            return name
    raise SafeFilesystemError(f"could not allocate a temporary name in {parent_fd}")


class _DarwinFSRef(ctypes.Structure):
    _fields_ = [("hidden", ctypes.c_ubyte * 80)]


def _darwin_file_manager_functions():
    try:
        framework = ctypes.CDLL(None, use_errno=True)
        make_reference = getattr(framework, "FSPathMakeRefWithOptions")
        delete_reference = getattr(framework, "FSDeleteObject")
    except (AttributeError, OSError) as exc:
        raise SafeFilesystemError(
            "Darwin has no identity-bound cleanup primitive",
            preserve_private=True,
        ) from exc
    make_reference.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DarwinFSRef),
        ctypes.POINTER(ctypes.c_ubyte),
    ]
    make_reference.restype = ctypes.c_int32
    delete_reference.argtypes = [ctypes.POINTER(_DarwinFSRef)]
    delete_reference.restype = ctypes.c_int16
    return make_reference, delete_reference


def _darwin_path_for_fd(directory_fd: int) -> bytes:
    get_path = getattr(fcntl, "F_GETPATH", 50)
    try:
        raw_path = fcntl.fcntl(directory_fd, get_path, b"\0" * 1024)
    except OSError as exc:
        raise SafeFilesystemError("could not resolve a Darwin directory descriptor") from exc
    path = raw_path.split(b"\0", 1)[0]
    if not path:
        raise SafeFilesystemError("Darwin returned an empty directory descriptor path")
    return path


def _darwin_identity_reference_at(
    directory_fd: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    receipt_path: Path,
) -> _DarwinFSRef:
    """Bind a Darwin entry to an FSRef while checking its descriptor-relative identity."""

    try:
        current_before = lstat_at(directory_fd, name)
        if not _same_entry(current_before, expected_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {name}")
        make_reference, _ = _darwin_file_manager_functions()
        reference = _DarwinFSRef()
        is_directory = ctypes.c_ubyte()
        entry_path = _darwin_path_for_fd(directory_fd).rstrip(b"/") + b"/" + os.fsencode(name)

        def validate_path_binding() -> None:
            path_parent = entry_path.rsplit(b"/", 1)[0] or b"/"
            try:
                path_parent_metadata = os.stat(path_parent, follow_symlinks=False)
            except OSError as exc:
                raise SafeFilesystemError(f"could not validate the Darwin directory path: {name}") from exc
            if not _same_entry(path_parent_metadata, os.fstat(directory_fd)):
                raise SafeFilesystemError(f"Darwin directory path changed during removal: {name}")
            try:
                path_metadata = os.lstat(entry_path)
            except OSError as exc:
                raise SafeFilesystemError(f"could not validate the Darwin entry path: {name}") from exc
            if not _same_entry(path_metadata, expected_metadata):
                raise SafeFilesystemError(f"Darwin entry path changed during removal: {name}")

        # F_GETPATH produces a pathname, not a descriptor-relative handle.
        # Validate both the parent directory and the entry through that path
        # before and after the path-based FSRef call so a renamed directory
        # cannot redirect the reference to a replacement tree.
        validate_path_binding()
        status = make_reference(entry_path, 0x01, ctypes.byref(reference), ctypes.byref(is_directory))
        if status != 0:
            raise OSError(errno.EIO, f"FSPathMakeRefWithOptions failed with status {status}", name)
        validate_path_binding()
        current_after = lstat_at(directory_fd, name)
        if not _same_entry(current_after, expected_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {name}")
        if bool(is_directory.value) != stat.S_ISDIR(expected_metadata.st_mode):
            raise SafeFilesystemError(f"entry type changed during removal: {name}")
        return reference
    except BaseException as exc:
        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
            raise exc
        reason = "identity-delete-unavailable" if getattr(exc, "preserve_private", False) else "replaced"
        raise _with_cleanup_receipt(
            exc,
            path=receipt_path,
            expected_metadata=expected_metadata,
            reason=reason,
        ) from exc


def _darwin_delete_reference(reference: _DarwinFSRef, name: str) -> None:
    _, delete_reference = _darwin_file_manager_functions()
    status = delete_reference(ctypes.byref(reference))
    if status != 0:
        raise OSError(errno.EIO, f"FSDeleteObject failed with status {status}", name)


def _native_unlink_at(
    parent_fd: int,
    name: str,
    *,
    is_directory: bool,
    expected_metadata: os.stat_result,
    identity_reference: _DarwinFSRef | None = None,
    receipt_path: Path | None = None,
) -> None:
    """Claim an identity-bound entry before removing it by descriptor."""

    current_metadata = lstat_at(parent_fd, name)
    if not _same_entry(current_metadata, expected_metadata):
        raise _with_cleanup_receipt(
            SafeFilesystemError(f"entry identity changed during removal: {name}"),
            path=receipt_path or Path(name),
            expected_metadata=expected_metadata,
            reason="replaced",
        )
    if stat.S_ISDIR(expected_metadata.st_mode) != is_directory:
        raise SafeFilesystemError(f"entry type changed during removal: {name}")
    if sys.platform == "darwin" and identity_reference is not None:
        # The reference is bound after the entry reaches its private name. It
        # removes that inode even if the private name is replaced meanwhile.
        try:
            _darwin_delete_reference(identity_reference, name)
            return
        except OSError as exc:
            # Some Darwin filesystems reject an otherwise valid FSRef after a
            # private rename with status -47. The descriptor-relative claim
            # below remains the bounded fallback for that platform case.
            if "status -47" not in str(exc):
                raise

    claimed_name = unique_entry_name(parent_fd, ".pstack-unlink-")
    atomic_rename_noreplace_at(parent_fd, name, claimed_name)
    try:
        claimed_metadata = lstat_at(parent_fd, claimed_name)
        if not _same_entry(claimed_metadata, expected_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {name}", preserve_private=True)
        # Make a second atomic claim before the pathname delete.  The first
        # private name is observable to cleanup hooks and tests, so a
        # replacement can appear after its identity check.  Rebinding the
        # entry to a fresh name and checking that name closes that race for
        # the fallback used when the native identity primitive returns
        # Darwin status -47.
        final_name = unique_entry_name(parent_fd, ".pstack-delete-")
        atomic_rename_noreplace_at(parent_fd, claimed_name, final_name)
        claimed_name = final_name
        claimed_metadata = lstat_at(parent_fd, claimed_name)
        if not _same_entry(claimed_metadata, expected_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {name}", preserve_private=True)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "unlinkat")
        except AttributeError as exc:
            raise SafeFilesystemError("the platform has no descriptor-relative unlink primitive") from exc
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        function.restype = ctypes.c_int
        flags = (0x00000080 if sys.platform == "darwin" else 0x00000200) if is_directory else 0
        result = function(parent_fd, os.fsencode(claimed_name), flags)
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), claimed_name)
    except BaseException as exc:
        try:
            atomic_rename_noreplace_at(parent_fd, claimed_name, name)
        except (OSError, SafeFilesystemError) as restore_error:
            raise _with_cleanup_receipt(
                SafeFilesystemError(f"{exc}; could not restore claimed cleanup entry: {restore_error}"),
                path=(receipt_path or Path(name)).parent / claimed_name,
                expected_metadata=expected_metadata,
                reason="cleanup-recovery-pending",
            ) from exc
        raise


def _native_rename(old_dir_fd: int, old_name: str, new_dir_fd: int, new_name: str, flags: int) -> bool:
    """Call the platform no-replace/exchange primitive when it exists."""

    function_name: str
    if sys.platform == "darwin":
        function_name = "renameatx_np"
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
    else:
        return False

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, function_name)
    except AttributeError:
        return False

    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        flags,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
        return False
    raise OSError(error_number, os.strerror(error_number), old_name, new_name)


def atomic_exchange_at(directory_fd: int, first_name: str, second_name: str) -> None:
    """Atomically exchange two sibling entries, or fail closed."""

    # macOS renameatx_np uses RENAME_SWAP; Linux renameat2 uses
    # RENAME_EXCHANGE. Both values are stable kernel ABI constants.
    flags = 0x00000002
    if not _native_rename(directory_fd, first_name, directory_fd, second_name, flags):
        raise SafeFilesystemError("the platform has no atomic directory-exchange primitive")


def atomic_rename_noreplace_at(directory_fd: int, source_name: str, target_name: str) -> None:
    """Rename a sibling without replacing a target that appeared meanwhile."""

    atomic_rename_noreplace_between(directory_fd, source_name, directory_fd, target_name)


def atomic_rename_noreplace_between(
    source_dir_fd: int,
    source_name: str,
    target_dir_fd: int,
    target_name: str,
) -> None:
    """Rename between opened directories without replacing the target."""

    # macOS renameatx_np uses RENAME_EXCL; Linux renameat2 uses
    # RENAME_NOREPLACE.
    flags = 0x00000004 if sys.platform == "darwin" else 0x00000001
    if not _native_rename(source_dir_fd, source_name, target_dir_fd, target_name, flags):
        raise SafeFilesystemError("the platform has no atomic no-replace rename primitive")


def ensure_relative_directory(root_fd: int, relative: str) -> int:
    current_fd = os.dup(root_fd)
    try:
        for component in Path(relative).parts:
            try:
                child_fd = _open_child_directory(current_fd, component)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=current_fd)
                child_fd = _open_child_directory(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def snapshot_tree(root_fd: int, *, reject_symlinks: bool = False) -> TreeSnapshot:
    directories: list[str] = []
    files: dict[str, bytes] = {}
    symlinks: dict[str, str] = {}

    def visit(directory_fd: int, prefix: str) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            raise
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            metadata = lstat_at(directory_fd, name)
            mode = metadata.st_mode
            if stat.S_ISDIR(mode):
                directories.append(relative)
                child_fd = _open_child_directory(directory_fd, name)
                try:
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(mode):
                files[relative] = read_regular_file_at(
                    directory_fd,
                    name,
                    expected_metadata=metadata,
                )
            elif stat.S_ISLNK(mode):
                if reject_symlinks:
                    raise SafeFilesystemError(f"tree contains a symlink: {relative}")
                symlinks[relative] = os.readlink(name, dir_fd=directory_fd)
            else:
                raise SafeFilesystemError(f"tree contains a non-regular entry: {relative}")

    visit(root_fd, "")
    return TreeSnapshot(tuple(directories), files, symlinks)


def write_snapshot(root_fd: int, snapshot: TreeSnapshot) -> None:
    for relative in sorted(snapshot.directories, key=lambda value: (value.count("/"), value)):
        parent, _, name = relative.rpartition("/")
        parent_fd = ensure_relative_directory(root_fd, parent) if parent else os.dup(root_fd)
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    for relative, content in sorted(snapshot.files.items()):
        parent, _, name = relative.rpartition("/")
        parent_fd = ensure_relative_directory(root_fd, parent) if parent else os.dup(root_fd)
        try:
            write_regular_file_at(parent_fd, name, content)
        finally:
            os.close(parent_fd)
    for relative, target in sorted(snapshot.symlinks.items()):
        parent, _, name = relative.rpartition("/")
        parent_fd = ensure_relative_directory(root_fd, parent) if parent else os.dup(root_fd)
        try:
            os.symlink(target, name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)


def _bind_tree_at(
    parent_fd: int,
    name: str,
    expected_metadata: os.stat_result | None,
) -> dict[tuple[str, ...], os.stat_result]:
    metadata = lstat_at(parent_fd, name)
    if expected_metadata is not None and not _same_entry(metadata, expected_metadata):
        raise SafeFilesystemError(f"entry identity changed during removal: {name}")
    identities: dict[tuple[str, ...], os.stat_result] = {(): metadata}

    def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
        for child in sorted(os.listdir(directory_fd)):
            child_metadata = lstat_at(directory_fd, child)
            child_path = prefix + (child,)
            identities[child_path] = child_metadata
            if stat.S_ISDIR(child_metadata.st_mode):
                child_fd = _open_child_directory(directory_fd, child)
                try:
                    opened_metadata = os.fstat(child_fd)
                    if not _same_entry(opened_metadata, child_metadata):
                        raise SafeFilesystemError(f"entry identity changed during removal: {child}")
                    visit(child_fd, child_path)
                finally:
                    os.close(child_fd)

    if stat.S_ISDIR(metadata.st_mode):
        directory_fd = _open_child_directory(parent_fd, name)
        try:
            opened_metadata = os.fstat(directory_fd)
            if not _same_entry(opened_metadata, metadata):
                raise SafeFilesystemError(f"entry identity changed during removal: {name}")
            visit(directory_fd, ())
        finally:
            os.close(directory_fd)
    return identities


def _restore_bound_entry(parent_fd: int, public_name: str, private_name: str) -> None:
    """Put a quarantined entry back without overwriting a replacement."""

    try:
        lstat_at(parent_fd, private_name)
    except FileNotFoundError:
        return
    try:
        lstat_at(parent_fd, public_name)
    except FileNotFoundError:
        try:
            atomic_rename_noreplace_at(parent_fd, private_name, public_name)
        except FileExistsError:
            return
        except (OSError, SafeFilesystemError) as exc:
            raise SafeFilesystemError(
                f"could not restore cleanup entry {private_name} as {public_name}: {exc}"
            ) from exc


def _move_bound_entry(
    parent_fd: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    receipt_path: Path | None = None,
) -> str:
    """Move an identity-checked entry to a private name before removing it."""

    receipt_path = receipt_path or Path(name)
    current_metadata = lstat_at(parent_fd, name)
    if not _same_entry(current_metadata, expected_metadata):
        raise SafeFilesystemError(f"entry identity changed during removal: {name}")
    private_name = unique_entry_name(parent_fd, ".pstack-cleanup-")
    try:
        atomic_rename_noreplace_at(parent_fd, name, private_name)
    except FileNotFoundError as exc:
        raise _with_cleanup_receipt(
            exc,
            path=receipt_path,
            expected_metadata=expected_metadata,
            reason="replaced",
        ) from exc
    try:
        moved_metadata = lstat_at(parent_fd, private_name)
        if not _same_entry(moved_metadata, expected_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {name}")
    except BaseException as exc:
        try:
            _restore_bound_entry(parent_fd, name, private_name)
        except SafeFilesystemError as restore_error:
            exc = SafeFilesystemError(f"{exc}; {restore_error}")
        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
            raise exc
        raise _with_cleanup_receipt(
            exc,
            path=receipt_path,
            expected_metadata=expected_metadata,
            reason="replaced",
        ) from exc
    return private_name


def _delete_private_entry(
    parent_fd: int,
    public_name: str,
    private_name: str,
    expected_metadata: os.stat_result,
    *,
    is_directory: bool,
    receipt_path: Path,
) -> None:
    """Claim an entry, move it into a held quarantine, and remove that claim.

    The second no-replace rename protects the first private name.  The third
    move puts the expected identity below a separately opened, mode-700
    quarantine directory.  Darwin cleanup uses a reference bound before the
    final removal; an unavailable identity-bound primitive leaves the private
    entry in place and returns a bounded recovery receipt.
    """

    delete_name: str | None = None
    quarantine_name: str | None = None
    quarantine_fd: int | None = None
    quarantine_entry_name: str | None = "entry"
    quarantine_directory_metadata: os.stat_result | None = None
    quarantine_directory_reference: _DarwinFSRef | None = None
    quarantine_entry_metadata: os.stat_result | None = None
    quarantine_entry_reference: _DarwinFSRef | None = None
    quarantine_receipt_path: Path | None = None
    try:
        current_metadata = lstat_at(parent_fd, private_name)
        if not _same_entry(current_metadata, expected_metadata):
            raise SafeFilesystemError(f"private cleanup identity changed: {private_name}")
        delete_name = unique_entry_name(parent_fd, ".pstack-cleanup-")
        atomic_rename_noreplace_at(parent_fd, private_name, delete_name)
        claimed_metadata = lstat_at(parent_fd, delete_name)
        if not _same_entry(claimed_metadata, expected_metadata):
            raise SafeFilesystemError(f"private cleanup identity changed: {private_name}")
        quarantine_name, quarantine_fd = create_unique_directory(
            parent_fd,
            ".pstack-dispose-",
            mode=0o700,
        )
        quarantine_receipt_path = receipt_path.parent / quarantine_name
        quarantine_directory_metadata = os.fstat(quarantine_fd)
        if sys.platform == "darwin":
            quarantine_directory_reference = _darwin_identity_reference_at(
                parent_fd,
                quarantine_name,
                quarantine_directory_metadata,
                receipt_path=quarantine_receipt_path,
            )
        atomic_rename_noreplace_between(
            parent_fd,
            delete_name,
            quarantine_fd,
            quarantine_entry_name,
        )
        quarantine_entry_metadata = lstat_at(quarantine_fd, quarantine_entry_name)
        if not _same_entry(quarantine_entry_metadata, expected_metadata):
            raise SafeFilesystemError(f"private cleanup identity changed in quarantine: {private_name}")
        if sys.platform == "darwin":
            quarantine_entry_reference = _darwin_identity_reference_at(
                quarantine_fd,
                quarantine_entry_name,
                quarantine_entry_metadata,
                receipt_path=quarantine_receipt_path / quarantine_entry_name,
            )
        _native_unlink_at(
            quarantine_fd,
            quarantine_entry_name,
            is_directory=is_directory,
            expected_metadata=quarantine_entry_metadata,
            identity_reference=quarantine_entry_reference,
            receipt_path=receipt_path,
        )
        quarantine_entry_name = None
        os.close(quarantine_fd)
        quarantine_fd = None
        _native_unlink_at(
            parent_fd,
            quarantine_name,
            is_directory=True,
            expected_metadata=quarantine_directory_metadata,
            identity_reference=quarantine_directory_reference,
            receipt_path=quarantine_receipt_path,
        )
        quarantine_name = None
        try:
            lstat_at(parent_fd, public_name)
        except FileNotFoundError:
            return
        raise SafeFilesystemError(f"replacement appeared during removal: {public_name}")
    except BaseException as exc:
        preserve_private = isinstance(exc, SafeFilesystemError) and exc.preserve_private
        if preserve_private and delete_name is not None and quarantine_entry_name is not None:
            exc = _with_cleanup_receipt(
                exc,
                path=receipt_path.parent / delete_name,
                expected_metadata=expected_metadata,
                reason="private-cleanup-pending",
            )
        if preserve_private and quarantine_entry_metadata is not None and quarantine_entry_name is not None:
            exc = _with_cleanup_receipt(
                exc,
                path=(quarantine_receipt_path or receipt_path.parent) / quarantine_entry_name,
                expected_metadata=quarantine_entry_metadata,
                reason="private-cleanup-pending",
            )
        if not preserve_private and quarantine_fd is not None and quarantine_entry_name is not None:
            try:
                current_entry_metadata = lstat_at(quarantine_fd, quarantine_entry_name)
            except FileNotFoundError:
                pass
            else:
                if quarantine_entry_metadata is not None and not _same_entry(
                    current_entry_metadata,
                    quarantine_entry_metadata,
                ):
                    exc = _with_cleanup_receipt(
                        exc,
                        path=(quarantine_receipt_path or receipt_path.parent) / quarantine_entry_name,
                        expected_metadata=quarantine_entry_metadata,
                        reason="replaced",
                    )
                else:
                    if delete_name is None:
                        delete_name = unique_entry_name(parent_fd, ".pstack-cleanup-")
                    try:
                        atomic_rename_noreplace_between(
                            quarantine_fd,
                            quarantine_entry_name,
                            parent_fd,
                            delete_name,
                        )
                    except (OSError, SafeFilesystemError) as restore_error:
                        exc = SafeFilesystemError(f"{exc}; {restore_error}")
        if delete_name is not None and not preserve_private:
            try:
                _restore_bound_entry(parent_fd, public_name, delete_name)
            except SafeFilesystemError as restore_error:
                exc = SafeFilesystemError(f"{exc}; {restore_error}")
        if quarantine_name is not None:
            current_quarantine_metadata: os.stat_result | None = None
            try:
                current_quarantine_metadata = lstat_at(parent_fd, quarantine_name)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISDIR(current_quarantine_metadata.st_mode):
                    if quarantine_directory_metadata is None:
                        quarantine_directory_metadata = current_quarantine_metadata
                    try:
                        _native_unlink_at(
                            parent_fd,
                            quarantine_name,
                            is_directory=True,
                            expected_metadata=quarantine_directory_metadata,
                            identity_reference=quarantine_directory_reference,
                            receipt_path=quarantine_receipt_path or receipt_path.parent / quarantine_name,
                        )
                    except (OSError, SafeFilesystemError) as cleanup_error:
                        exc = _with_cleanup_receipt(
                            exc,
                            path=quarantine_receipt_path or receipt_path.parent / quarantine_name,
                            expected_metadata=quarantine_directory_metadata,
                            reason="cleanup-recovery-pending",
                        )
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass
        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
            raise exc
        reason = "replaced" if "identity" in str(exc) or "replacement" in str(exc) else "cleanup-error"
        raise _with_cleanup_receipt(
            exc,
            path=receipt_path,
            expected_metadata=expected_metadata,
            reason=reason,
        ) from exc
    finally:
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError:
                pass


def _remove_owned_empty_directory(
    parent_fd: int,
    name: str,
    expected_metadata: os.stat_result,
    *,
    receipt_path: Path | None = None,
) -> None:
    """Remove a created directory without ever rmdir-ing a replacement."""

    receipt_path = receipt_path or Path(name)
    private_name = _move_bound_entry(
        parent_fd,
        name,
        expected_metadata,
        receipt_path=receipt_path,
    )
    try:
        _delete_private_entry(
            parent_fd,
            name,
            private_name,
            expected_metadata,
            is_directory=True,
            receipt_path=receipt_path,
        )
    except BaseException as exc:
        try:
            _restore_bound_entry(parent_fd, name, private_name)
        except SafeFilesystemError as restore_error:
            exc = SafeFilesystemError(f"{exc}; {restore_error}")
        if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
            raise exc
        raise _with_cleanup_receipt(
            exc,
            path=receipt_path,
            expected_metadata=expected_metadata,
            reason="replaced" if "replacement" in str(exc) or "identity" in str(exc) else "cleanup-error",
        ) from exc


def remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    expected_metadata: os.stat_result | None = None,
    guard_name: str | None = None,
    guard_metadata: os.stat_result | None = None,
    receipt_root: Path | None = None,
) -> None:
    """Remove an entry by first binding every owned entry to a private name.

    ``guard_name``/``guard_metadata`` protect a sibling publication root while
    a residue generation is removed. Each entry is identity-checked, moved
    with a no-replace rename, and only then deleted from its private name. A
    replacement at the public name is therefore never the victim of cleanup.
    """

    if guard_name is not None and guard_metadata is None:
        raise ValueError("guard_metadata is required when guard_name is set")

    def validate_entry(directory_fd: int, entry_name: str, expected: os.stat_result) -> os.stat_result:
        entry_metadata = lstat_at(directory_fd, entry_name)
        if not _same_entry(entry_metadata, expected):
            raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
        return entry_metadata

    def validate_guard() -> None:
        if guard_name is not None and guard_metadata is not None:
            validate_entry(parent_fd, guard_name, guard_metadata)

    identities = _bind_tree_at(parent_fd, name, expected_metadata)
    metadata = identities[()]

    def remove_directory_children(directory_fd: int, relative: tuple[str, ...]) -> None:
        children = sorted(os.listdir(directory_fd))
        expected_children = {
            path[-1]
            for path in identities
            if len(path) == len(relative) + 1 and path[:-1] == relative
        }
        if set(children) != expected_children:
            raise SafeFilesystemError("directory contents changed during removal")
        for child in children:
            child_relative = relative + (child,)
            remove_bound(
                directory_fd,
                child,
                child_relative,
                identities[child_relative],
            )

    def remove_bound(
        directory_parent_fd: int,
        entry_name: str,
        relative: tuple[str, ...],
        entry_metadata: os.stat_result,
        *,
        top_level: bool = False,
    ) -> None:
        validate_guard()
        private_name: str | None = None
        moved = True
        receipt_path = _cleanup_receipt_path(receipt_root, relative, entry_name)
        try:
            if top_level and stat.S_ISDIR(entry_metadata.st_mode):
                # Bind the public root before opening or mutating any child.
                # A replacement at the public name must never turn recursive
                # cleanup into mutation of the old root after the names swap.
                private_name = _move_bound_entry(
                    directory_parent_fd,
                    entry_name,
                    entry_metadata,
                    receipt_path=receipt_path,
                )
                moved_metadata = validate_entry(directory_parent_fd, private_name, entry_metadata)
                if not stat.S_ISDIR(moved_metadata.st_mode):
                    raise SafeFilesystemError(f"entry is no longer a directory during removal: {entry_name}")
                directory_fd = _open_child_directory(directory_parent_fd, private_name)
                try:
                    opened_metadata = os.fstat(directory_fd)
                    if not _same_entry(opened_metadata, entry_metadata):
                        raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
                    remove_directory_children(directory_fd, relative)
                finally:
                    os.close(directory_fd)
                validate_guard()
                _delete_private_entry(
                    directory_parent_fd,
                    entry_name,
                    private_name,
                    entry_metadata,
                    is_directory=True,
                    receipt_path=receipt_path,
                )
            else:
                private_name = _move_bound_entry(
                    directory_parent_fd,
                    entry_name,
                    entry_metadata,
                    receipt_path=receipt_path,
                )
                moved_metadata = validate_entry(directory_parent_fd, private_name, entry_metadata)
                if stat.S_ISDIR(moved_metadata.st_mode):
                    directory_fd = _open_child_directory(directory_parent_fd, private_name)
                    try:
                        opened_metadata = os.fstat(directory_fd)
                        if not _same_entry(opened_metadata, entry_metadata):
                            raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
                        remove_directory_children(directory_fd, relative)
                    finally:
                        os.close(directory_fd)
                    validate_guard()
                    _delete_private_entry(
                        directory_parent_fd,
                        entry_name,
                        private_name,
                        entry_metadata,
                        is_directory=True,
                        receipt_path=receipt_path,
                    )
                else:
                    validate_guard()
                    _delete_private_entry(
                        directory_parent_fd,
                        entry_name,
                        private_name,
                        entry_metadata,
                        is_directory=False,
                        receipt_path=receipt_path,
                    )
            moved = False
        except BaseException as exc:
            if moved and private_name is not None:
                try:
                    _restore_bound_entry(directory_parent_fd, entry_name, private_name)
                except SafeFilesystemError as restore_error:
                    exc = SafeFilesystemError(f"{exc}; {restore_error}")
            if isinstance(exc, SafeFilesystemError) and exc.cleanup_receipt is not None:
                raise exc
            if private_name is None:
                raise
            raise _with_cleanup_receipt(
                exc,
                path=receipt_path,
                expected_metadata=entry_metadata,
                reason="replaced" if "replacement" in str(exc) or "identity" in str(exc) else "cleanup-error",
            ) from exc

    remove_bound(parent_fd, name, (), metadata, top_level=True)


def sync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
