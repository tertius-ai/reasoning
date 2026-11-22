"""Content-verified, reproducible releases with only the public web entry point."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import tarfile
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from reasoning_ledger import source_install
from reasoning_ledger.distribution import verify_wheel
from reasoning_ledger.server.compute_catalog import ComputeImage
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.source_release import source_bytes

from . import host, server_operations, server_recovery, server_upgrade, worker_host

MAX_FILE = 32 * 1024**2
# Quoted or url(...) references, and the template literals the bundler emits for imported asset URLs. A template
# literal counts only as a whole `/assets/...` path, so library defaults such as pdf.js's `./pdf.worker.mjs` do not.
STATIC_REFERENCE = re.compile(r"""(?:["'(]|`(?=/assets/[A-Za-z0-9._/-]+`))((?:\./|\.\./|/assets/|assets/)[A-Za-z0-9._/-]+)""")
# Built files that JavaScript or CSS loads by URL: modules and module workers (.mjs), styles, fonts (including the
# Type 1 standard fonts pdf.js fetches as .pfb), images, data and WebAssembly decoders.
REFERENCED_ASSET_SUFFIXES = frozenset({".js", ".mjs", ".css", ".woff", ".woff2", ".ttf", ".pfb", ".svg", ".png", ".json", ".wasm"})
# Notices for the pdf.js image decoders and standard fonts the PDF viewer ships as separate build files.
PDFJS_NOTICES = ("pdf.js-JBIG2.txt", "pdf.js-JBIG2-wasm.txt", "pdf.js-OpenJPEG.txt", "pdf.js-OpenJPEG-wasm.txt",
                 "pdf.js-Foxit-fonts.txt")


class PublicHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "link"}:
            value = attributes.get("src") if tag == "script" else attributes.get("href")
            if value:
                self.references.append(value)


def read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise host.InstallError("release input must be a bounded regular file, not a symlink")
    return path.read_bytes()


def public_frontend(directory: Path) -> dict[str, bytes]:
    if directory.is_symlink() or not directory.is_dir():
        raise host.InstallError("frontend must be a built public web directory")
    html = read_file(directory / "organization.html")
    parser = PublicHTML()
    parser.feed(html.decode("utf8"))
    files = {"web/organization.html": html, "web/reasoning.svg": read_file(directory / "reasoning.svg")}
    if not (directory / "licenses").is_dir() or (directory / "licenses").is_symlink():
        raise host.InstallError("frontend requires license material")
    for path in sorted((directory / "licenses").iterdir()):
        if not host.safe_name("web/licenses/" + path.name):
            raise host.InstallError("unsafe frontend license name")
        files["web/licenses/" + path.name] = read_file(path)
    required = {
        "web/licenses/Reasoning-Apache-2.0.txt",
        "web/licenses/Reasoning-NOTICE.txt",
        "web/licenses/KaTeX-MIT.txt",
        "web/licenses/IBM-Plex-OFL-1.1.txt",
        *("web/licenses/" + name for name in PDFJS_NOTICES),
    }
    if not required <= files.keys():
        raise host.InstallError("frontend omits required notices")
    pending = []
    for reference in parser.references:
        if reference == "/reasoning.svg":
            continue
        if not reference.startswith("/assets/"):
            raise host.InstallError("public frontend contains a nonlocal dependency")
        pending.append(reference.removeprefix("/"))
    if not any(name.endswith(".js") for name in pending):
        raise host.InstallError("public frontend requires its built JavaScript entry point")
    while pending:
        name = pending.pop()
        key = "web/" + name
        if key in files:
            continue
        if not host.safe_name(key) or not name.startswith("assets/") or "workbench" in Path(name).name:
            raise host.InstallError("public frontend imports an unsafe or local-workbench dependency")
        if (directory / "assets").is_symlink():
            raise host.InstallError("frontend assets must not traverse symlinks")
        body = read_file(directory / name)
        files[key] = body
        if name.endswith((".js", ".css")):
            for reference in STATIC_REFERENCE.findall(body.decode("utf8")):
                if reference.startswith("/assets/"):
                    target = reference[1:]
                elif reference.startswith("assets/"):
                    target = reference
                else:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), reference))
                if Path(target).suffix in REFERENCED_ASSET_SUFFIXES:
                    pending.append(target)
        if len(files) > 500 or sum(len(body) for body in files.values()) > host.MAX_BUNDLE:
            raise host.InstallError("frontend exceeds release bounds")
    return files


def validate_requirements(body: bytes) -> None:
    try:
        logical = body.decode("utf8").replace("\\\n", " ")
        entries = [line.strip() for line in logical.splitlines() if line.strip()]
        expression = re.compile(
            r"""[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9.+_-]+(?:\s*;[A-Za-z0-9_ .<>=!()'"-]+)?(?:\s+--hash=sha256:[a-f0-9]{64})+"""
        )
        if not entries or len(body) > MAX_FILE or any(not expression.fullmatch(line) for line in entries):
            raise ValueError
    except (UnicodeError, ValueError) as exc:
        raise host.InstallError("dependencies must be version-pinned, hash-locked PyPI requirements without extra sources") from exc


def export_requirements(source_root: Path, *, modal: bool = False) -> bytes:
    with tempfile.TemporaryDirectory(prefix="reasoning-export-") as cache:
        environment = {**os.environ, "UV_CACHE_DIR": cache}
        result = subprocess.run(
            [
                "uv",
                "export",
                "--offline",
                "--locked",
                "--no-dev",
                "--no-default-groups",
                "--no-emit-project",
                "--no-editable",
                "--no-header",
                "--no-annotate",
                *(["--extra", "modal"] if modal else []),
            ],
            cwd=source_root,
            env=environment,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise host.InstallError("locked dependency export failed; synchronize the source lockfile first")
        validate_requirements(result.stdout)
        return result.stdout


def source_identity(source_root: Path, allow_dirty: bool) -> tuple[str, bool]:
    def git(*args):
        result = subprocess.run(["git", "-C", str(source_root), *args], capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise host.InstallError("release source must have a readable Git identity")
        return result.stdout.decode().strip()

    revision, dirty = git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    if dirty and not allow_dirty:
        raise host.InstallError("release source is dirty; commit it or explicitly select --allow-dirty for development")
    return revision, dirty


def approved_catalog(body: bytes) -> bytes:
    """Validate a maintainer approval, not registry availability or publisher trust."""
    try:
        if len(body) > 65536:
            raise ValueError
        rows = json.loads(body)
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise ValueError
        images = [ComputeImage.parse(row) for row in rows]
        if len({image.id for image in images}) != len(images):
            raise ValueError
        result = host.canonical([image.record() for image in sorted(images, key=lambda image: image.id)])
        if len(result) > 65536:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError):
        raise host.InstallError("release catalog requires 1–32 approved immutable images with source/evidence digests") from None


def select_release_catalog(source_root: Path, override: Path | None = None) -> Path | None:
    """Use the checkout's published approvals, never discover/download candidates."""
    if override is not None:
        return override
    published = source_root / "images/compute-images.json"
    if published.exists() or published.is_symlink():
        if published.parent.is_symlink():
            raise host.InstallError("published image approvals must not traverse a symlink")
        return published
    return None


def save_release_catalog(bundle: Path, manifest: dict, directory: Path) -> None:
    """Copy only catalog bytes from a fully content-verified trusted release."""
    from .private import read_private, write_private

    name = "compute-images.json"
    destination = directory / "release-compute-images.json"
    if name not in manifest.get("files", {}):
        if destination.exists() or destination.is_symlink():
            raise host.InstallError("private release catalog exists but is absent from this release")
        return
    # Callers may also use this helper independently of the setup wizard.
    verified, _, _ = host.inspect_bundle(bundle)
    if verified != manifest:
        raise host.InstallError("release changed while preparing its catalog")
    with tarfile.open(bundle, "r:gz") as archive:
        stream = archive.extractfile(name)
        if stream is None:
            raise host.InstallError("release omits its approved catalog")
        body = stream.read(65537)
    canonical = approved_catalog(body)
    if canonical != body or hashlib.sha256(body).hexdigest() != manifest["files"][name]["sha256"]:
        raise host.InstallError("release catalog differs from its immutable manifest")
    if destination.exists() or destination.is_symlink():
        if read_private(destination, 65536) != body:
            raise host.InstallError("saved release catalog changed; preserve original approvals")
    else:
        write_private(destination, body)
    print("Using the approved compute catalog supplied by this trusted release.")


def build_bundle(source_root: Path, wheel: Path | None, frontend: Path, output: Path, *, allow_dirty: bool = False,
                 compute_catalog: Path | None = None) -> dict:
    if wheel is not None:
        verify_wheel(wheel, source_root)
    if output.exists() or output.is_symlink():
        raise host.InstallError("release output already exists; choose another explicit filename")
    revision, dirty = source_identity(source_root, allow_dirty)
    files = public_frontend(frontend)
    for target, source in {
        "Reasoning-Apache-2.0.txt": source_root / "LICENSE",
        "Reasoning-NOTICE.txt": source_root / "NOTICE",
        "KaTeX-MIT.txt": source_root / "web/public/licenses/KaTeX-MIT.txt",
        "IBM-Plex-OFL-1.1.txt": source_root / "web/public/licenses/IBM-Plex-OFL-1.1.txt",
        **{name: source_root / "web/public/licenses" / name for name in PDFJS_NOTICES},
    }.items():
        if files["web/licenses/" + target] != read_file(source):
            raise host.InstallError("frontend contains stale legal material; rebuild it from the current source")
    files.update(
        {
            "installer.py": Path(host.__file__).read_bytes(),
            "worker-installer.py": Path(worker_host.__file__).read_bytes(),
            "server-operations.py": Path(server_operations.__file__).read_bytes(),
            "server-upgrade.py": Path(server_upgrade.__file__).read_bytes(),
            "server-recovery.py": Path(server_recovery.__file__).read_bytes(),
            "requirements.txt": export_requirements(source_root),
            "modal-requirements.txt": export_requirements(source_root, modal=True),
            "LICENSE": read_file(source_root / "LICENSE"),
            "NOTICE": read_file(source_root / "NOTICE"),
        }
    )
    if wheel is None:
        files["source/reasoning-source.zip"] = source_bytes(source_root, files["requirements.txt"], files["modal-requirements.txt"])
        files["source-installer.py"] = read_file(Path(source_install.__file__))
    else:
        files["wheel/" + wheel.name] = read_file(wheel)
    compute_catalog = select_release_catalog(source_root, compute_catalog)
    if compute_catalog is not None:
        files["compute-images.json"] = approved_catalog(read_file(compute_catalog))
    for name in ("server", "inference", "compute", "deployment", "backup", "bots", "images", "operations"):
        files[f"docs/{name}.md"] = read_file(source_root / f"docs/{name}.md")
    metadata = {name: {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)} for name, body in files.items()}
    manifest_bytes = host.canonical(
        {
            "format": 1,
            "source_revision": revision,
            "dirty": dirty,
            "database_schema": len(MIGRATIONS),
            "files": metadata,
            "installation_features": list(host.INSTALLATION_FEATURES),
        }
    )
    host.validate_manifest(manifest_bytes)
    fd, temporary = tempfile.mkstemp(prefix=".release-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as destination, gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, body in [("manifest.json", manifest_bytes), *sorted(files.items())]:
                    entry = tarfile.TarInfo(name)
                    entry.mode, entry.uid, entry.gid, entry.mtime, entry.size = 0o644, 0, 0, 0, len(body)
                    archive.addfile(entry, io.BytesIO(body))
            compressed.close()
            destination.flush()
            os.fsync(destination.fileno())
        _, release_id, checksum = host.inspect_bundle(Path(temporary))
        os.link(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "release_id": release_id,
            "bundle_sha256": checksum,
            "source_revision": revision,
            "dirty": dirty,
            "bundle": str(output.absolute()),
            "files": len(files),
            "size_bytes": output.stat().st_size,
        }
    finally:
        os.unlink(temporary)
