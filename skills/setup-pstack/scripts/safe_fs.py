"""Small POSIX helpers for no-follow, directory-handle based file updates."""

from __future__ import annotations

import errno
import ctypes
import fcntl
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class SafeFilesystemError(RuntimeError):
    """Raised when a requested path cannot be used without following a link."""


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
    errors: list[str] = []
    for entry in reversed(created):
        try:
            try:
                current = lstat_at(entry.parent_fd, entry.name)
            except FileNotFoundError:
                continue
            if not _same_entry(current, entry.metadata):
                raise SafeFilesystemError(f"created directory identity changed during cleanup: {entry.path}")
            if not stat.S_ISDIR(current.st_mode):
                raise SafeFilesystemError(f"created path is no longer a directory during cleanup: {entry.path}")
            os.rmdir(entry.name, dir_fd=entry.parent_fd)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                errors.append(f"{entry.path}: {exc}")
        except SafeFilesystemError as exc:
            errors.append(str(exc))
        finally:
            try:
                os.close(entry.parent_fd)
            except OSError:
                pass
    if errors:
        raise SafeFilesystemError("created directory cleanup incomplete: " + "; ".join(errors))


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


def create_unique_directory(parent_fd: int, prefix: str) -> tuple[str, int]:
    for _attempt in range(100):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
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

    # macOS renameatx_np uses RENAME_EXCL; Linux renameat2 uses
    # RENAME_NOREPLACE.
    flags = 0x00000004 if sys.platform == "darwin" else 0x00000001
    if not _native_rename(directory_fd, source_name, directory_fd, target_name, flags):
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


def remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    expected_metadata: os.stat_result | None = None,
    guard_name: str | None = None,
    guard_metadata: os.stat_result | None = None,
) -> None:
    """Remove an entry without following links, optionally binding identities.

    ``guard_name``/``guard_metadata`` protect a sibling publication root while
    a residue generation is removed. The caller still serializes the parent
    directory with ``directory_lock``; the repeated checks close the unsafe
    check-then-remove windows for callers that also need to detect an
    uncooperative replacement.
    """

    if guard_name is not None and guard_metadata is None:
        raise ValueError("guard_metadata is required when guard_name is set")

    def validate_entry(entry_name: str, expected: os.stat_result | None) -> os.stat_result:
        entry_metadata = lstat_at(parent_fd, entry_name)
        if expected is not None and not _same_entry(entry_metadata, expected):
            raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
        return entry_metadata

    identities = _bind_tree_at(parent_fd, name, expected_metadata)
    metadata = validate_entry(name, identities[()])
    if guard_name is not None:
        validate_entry(guard_name, guard_metadata)

    def remove_bound(
        directory_parent_fd: int,
        entry_name: str,
        relative: tuple[str, ...],
        entry_metadata: os.stat_result,
        *,
        top_level: bool = False,
    ) -> None:
        current_metadata = lstat_at(directory_parent_fd, entry_name)
        if not _same_entry(current_metadata, entry_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
        if stat.S_ISDIR(entry_metadata.st_mode):
            directory_fd = _open_child_directory(directory_parent_fd, entry_name)
            try:
                opened_metadata = os.fstat(directory_fd)
                if not _same_entry(opened_metadata, entry_metadata):
                    raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
                children = sorted(os.listdir(directory_fd))
                expected_children = {
                    path[0]
                    for path in identities
                    if len(path) == len(relative) + 1 and path[:-1] == relative
                }
                if set(children) != expected_children:
                    raise SafeFilesystemError(f"directory contents changed during removal: {entry_name}")
                for child in children:
                    child_relative = relative + (child,)
                    remove_bound(
                        directory_fd,
                        child,
                        child_relative,
                        identities[child_relative],
                    )
            finally:
                os.close(directory_fd)
            current_metadata = lstat_at(directory_parent_fd, entry_name)
            if not _same_entry(current_metadata, entry_metadata):
                raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
            if top_level and guard_name is not None:
                validate_entry(guard_name, guard_metadata)
            os.rmdir(entry_name, dir_fd=directory_parent_fd)
            return

        current_metadata = lstat_at(directory_parent_fd, entry_name)
        if not _same_entry(current_metadata, entry_metadata):
            raise SafeFilesystemError(f"entry identity changed during removal: {entry_name}")
        if top_level and guard_name is not None:
            validate_entry(guard_name, guard_metadata)
        os.unlink(entry_name, dir_fd=directory_parent_fd)

    remove_bound(parent_fd, name, (), metadata, top_level=True)


def sync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
