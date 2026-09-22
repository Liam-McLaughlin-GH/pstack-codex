"""Small POSIX helpers for no-follow, directory-handle based file updates."""

from __future__ import annotations

import errno
import fcntl
import os
import secrets
import stat
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
                    created.append(CreatedDirectory(parent_for_cleanup, component, current_path / component))
                child_fd = _open_child_directory(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
            current_path /= component
        return DirectoryHandle(current_fd, normalized, created)
    except BaseException:
        os.close(current_fd)
        try:
            cleanup_created_directories(created)
        except OSError:
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
    for entry in reversed(created):
        try:
            os.rmdir(entry.name, dir_fd=entry.parent_fd)
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY}:
                raise
        finally:
            try:
                os.close(entry.parent_fd)
            except OSError:
                pass


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


def read_regular_file_at(directory_fd: int, name: str) -> bytes:
    try:
        file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SafeFilesystemError(f"file is a symlink: {name}") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise SafeFilesystemError(f"file is not regular: {name}")
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
                files[relative] = read_regular_file_at(directory_fd, name)
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


def remove_tree_at(parent_fd: int, name: str) -> None:
    metadata = lstat_at(parent_fd, name)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    directory_fd = _open_child_directory(parent_fd, name)
    try:
        for child in sorted(os.listdir(directory_fd)):
            remove_tree_at(directory_fd, child)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


def sync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
