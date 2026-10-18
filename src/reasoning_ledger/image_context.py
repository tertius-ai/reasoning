"""Stage exact, credential-free Noether sources for a named image build context.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from .server.compute_catalog import canonical

MAXIMUM_ARCHIVE = 64 * 1024**2
MAXIMUM_EXPANDED = 256 * 1024**2
NOETHER_PUBLIC_COMMIT = "eebc33d4f37ed47af83c88dbc9f8886b254f72c6"
NOETHER_VERSION = "0.3.2"
# The image lock names the public repository; the pinned commit, not the repository name, is the trust anchor.
NOETHER_REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/noether")


def validate_noether(lock):
    value = lock.get("noether")
    if not isinstance(value, dict) or not re.fullmatch(r"[a-f0-9]{40}", value.get("commit", "")):
        raise ValueError("Noether requires an exact source commit")
    if value.get("version") != lock["versions"].get("noether") or value.get("build_context") != "noether-source":
        raise ValueError("Noether source/version/context differs from the image lock")
    if value["commit"] != NOETHER_PUBLIC_COMMIT or value["version"] != NOETHER_VERSION:
        raise ValueError("Noether image recipe must use the approved public release")
    archive = value.get("source_archive", {})
    url = urlsplit(archive.get("url", ""))
    repository = NOETHER_REPOSITORY.fullmatch(value.get("source_repository", ""))
    if (repository is None or archive.get("transport") != "https" or url.scheme != "https"
            or url.netloc != "codeload.github.com" or url.query or url.fragment
            or url.path != f"/{repository[1]}/noether/tar.gz/{value['commit']}"
            or archive.get("strip_prefix") != "noether-" + value["commit"]
            or not re.fullmatch(r"[a-f0-9]{64}", archive.get("sha256", ""))
            or type(archive.get("bytes")) is not int or not 0 < archive["bytes"] <= MAXIMUM_ARCHIVE):
        raise ValueError("Noether requires a bounded hash-locked public commit archive")
    return value


def checked_archive(path, lock):
    expected = lock["source_archive"]
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["bytes"]:
        raise ValueError("Noether source archive byte size differs from its lock")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while body := stream.read(1024**2):
            hasher.update(body)
    if hasher.hexdigest() != expected["sha256"]:
        raise ValueError("Noether source archive SHA256 differs from its lock")


def materialize(archive_path, output, lock, *, prefix=None):
    """Extract only bounded regular files/directories; no links or repository data."""
    from .image_release import BoundedTarInfo

    output.mkdir(mode=0o700)
    prefix = lock["source_archive"]["strip_prefix"] if prefix is None else prefix
    count = size = 0
    names = set()
    with tarfile.open(archive_path, "r:gz", tarinfo=BoundedTarInfo) as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            count += 1
            if (count > 20000 or name.is_absolute() or ".." in name.parts or str(name) in names
                    or len(member.name) > 4096 or "\x00" in member.name
                    or any(part in {".git", ".env", "reasoning-source-pin.json"} for part in name.parts)
                    or member.sparse is not None or not (member.isdir() or member.isreg())):
                raise ValueError("unsafe or duplicate Noether source archive member")
            names.add(str(name))
            if prefix:
                if not name.parts or name.parts[0] != prefix:
                    raise ValueError("unsafe Noether source archive root; use the exact public commit archive")
                name = PurePosixPath(*name.parts[1:])
                if not name.parts:
                    if not member.isdir():
                        raise ValueError("unsafe Noether source archive root")
                    continue
            target = output / name
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            size += member.size
            if member.size < 0 or size > MAXIMUM_EXPANDED:
                raise ValueError("Noether source archive expands beyond its allowance")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as destination:
                copied = 0
                while body := source.read(1024**2):
                    copied += len(body)
                    if copied > member.size:
                        raise ValueError("Noether source archive member exceeds its declared size")
                    destination.write(body)
                if copied != member.size:
                    raise ValueError("truncated Noether source archive member")
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for required in ("LICENSE", "Cargo.toml", "Cargo.lock", "pyproject.toml", "rust-toolchain.toml", "Makefile"):
        if not (output / required).is_file():
            raise ValueError("Noether source archive lacks required build/license inputs")
    (output / "reasoning-source-pin.json").write_text(canonical({"commit": lock["commit"], "version": lock["version"]}) + "\n")


def download_archive(expected, path):
    """Fetch the pinned public archive anonymously with byte and time bounds."""
    deadline = time.monotonic() + 180
    try:
        with (
            httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client,
            client.stream("GET", expected["url"], headers={"Accept-Encoding": "identity"}) as response,
        ):
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("exact public Noether source download failed")
            count = 0
            with path.open("xb") as stream:
                for body in response.iter_raw():
                    if time.monotonic() >= deadline:
                        raise ValueError("exact Noether source download timed out")
                    count += len(body)
                    if count > expected["bytes"]:
                        raise ValueError("Noether source download exceeds its locked byte allowance")
                    stream.write(body)
    except httpx.HTTPError:
        raise ValueError("exact public Noether source download failed; supply --noether-source-root or --noether-source-archive") from None


def stage_noether(lock, output, *, source_root=None, source_archive=None):
    """Export the locked Git object or snapshot the byte-locked public archive."""
    if source_root is not None and source_archive is not None:
        raise ValueError("select either a Noether Git source root or an exact source archive")
    with tempfile.TemporaryDirectory(prefix="reasoning-noether-input-") as temporary:
        path = Path(temporary) / "source.tar.gz"
        if source_root is not None:
            # Archive the locked object, not HEAD, the index, untracked files or a working tree.
            subprocess.run(["git", "archive", "--format=tar.gz", "--output=" + str(path), lock["commit"]],
                           cwd=source_root, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif source_archive is not None:
            # Snapshot through one no-follow descriptor before checking/extracting.
            descriptor = os.open(source_archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source, path.open("xb") as destination:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != lock["source_archive"]["bytes"]:
                    raise ValueError("Noether source archive is not a regular exact-size file")
                copied = 0
                while body := source.read(1024**2):
                    copied += len(body)
                    if copied > metadata.st_size:
                        raise ValueError("Noether source archive changed while snapshotting")
                    destination.write(body)
        else:
            expected = lock["source_archive"]
            download_archive(expected, path)
        if source_root is None:
            checked_archive(path, lock)
        else:
            if path.stat().st_size > MAXIMUM_ARCHIVE:
                raise ValueError("Noether Git export exceeds its archive allowance")
            with tarfile.open(path, "r:gz") as archive:
                if archive.pax_headers.get("comment") != lock["commit"]:
                    raise ValueError("Noether Git export differs from its locked source commit")
        materialize(path, output, lock, prefix="" if source_root is not None else None)
    return output
