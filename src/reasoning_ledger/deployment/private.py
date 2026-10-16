"""Protected operator plans and single-owner provisioning journals."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .host import InstallError, atomic_write, canonical

MAX_STATE = 256 * 1024


class PrivateDirectoryBusy(InstallError):
    """Another process holds the private directory's ownership lock."""


def read_private(path: Path, maximum: int = MAX_STATE) -> bytes:
    try:
        protected_parent(path.absolute(), writable=False)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > maximum
            ):
                raise InstallError("operator input must be a bounded owner-owned private regular file")
            body = stream.read(maximum + 1)
            if len(body) > maximum:
                raise InstallError("operator input exceeds its byte allowance")
            return body
    except OSError as exc:
        raise InstallError("operator input must be a readable private regular file, not a symlink") from exc


def protected_parent(path: Path, *, writable: bool = True) -> None:
    if ".." in path.parts:
        raise InstallError("operator paths must not contain parent traversal")
    # User namespaces/read-only environments can map the system root owner.
    # Anchor system ownership to / rather than accepting arbitrary owners.
    system_owner = Path("/").stat().st_uid
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        problem = None
        if stat.S_ISLNK(info.st_mode):
            problem = "is a symlink; use the real directory path"
        elif not stat.S_ISDIR(info.st_mode):
            problem = "is not a directory"
        elif info.st_uid not in {system_owner, os.getuid()}:
            problem = f"belongs to another user (uid {info.st_uid})"
        elif info.st_mode & 0o022 and not (parent != path.parent and info.st_uid == system_owner and info.st_mode & stat.S_ISVTX):
            writers = " and ".join(name for bit, name in ((0o020, "group"), (0o002, "others")) if info.st_mode & bit)
            problem = f"is writable by {writers} (mode {stat.S_IMODE(info.st_mode):04o}); remove that with chmod go-w"
        if problem is not None:
            raise InstallError(f"operator state parents must be protected directories, not symlinks: {parent} {problem}")
    if writable and path.parent.stat().st_uid != os.getuid():
        raise InstallError(f"operator state parent must belong to the current user: {path.parent}")


def read_document(path: Path) -> dict:
    protected_parent(path.absolute())
    body = read_private(path)
    try:
        value = json.loads(body)
        if not isinstance(value, dict) or canonical(value) != body:
            raise ValueError
        return value
    except (ValueError, TypeError) as exc:
        raise InstallError("operator document must be canonical JSON without duplicate keys") from exc


def write_document(path: Path, value: dict, *, new: bool = False) -> None:
    protected_parent(path.absolute())
    body = canonical(value)
    if len(body) > MAX_STATE:
        raise InstallError("operator document exceeds its byte allowance")
    if not new:
        if path.exists() or path.is_symlink():
            read_private(path)
        atomic_write(path, body)
        return
    write_private(path, body)


def write_private(path: Path, body: bytes, *, maximum: int = MAX_STATE) -> None:
    """Exclusively create a private byte file within its caller's bound; default to small state documents."""
    protected_parent(path.absolute())
    if not isinstance(body, bytes) or len(body) > maximum:
        raise InstallError("operator document exceeds its byte allowance")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise InstallError("operator document already exists; choose another explicit filename") from exc
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def protected_temporary_directory(prefix: str):
    """Create an internal private directory without trusting a symlinked temp path."""
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
        # macOS returns /var/folders/... even though /var is a system symlink to
        # /private/var.  Resolve only this process-created directory; caller
        # paths continue to reject symlinks in protected_parent().
        path = Path(temporary).resolve(strict=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InstallError("private temporary directory is not owner-protected")
        protected_parent(path / "entry")
        yield path


@contextmanager
def locked_directory(path: Path):
    path = path.absolute()
    protected_parent(path)
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise InstallError("provisioning directory must be an owner-owned private directory, not a symlink")
    fd = os.open(path / "provision.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise InstallError("invalid private provisioning lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PrivateDirectoryBusy("another process owns this provisioning journal") from exc
        yield path
    finally:
        os.close(fd)
