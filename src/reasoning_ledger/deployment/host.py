"""Standalone standard-library bootstrap for a dedicated Ubuntu 24.04 host."""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import gzip
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAX_BUNDLE = 128 * 1024**2
MAX_MANIFEST = 256 * 1024
MAX_OWNER_INPUT = 32768
MAX_MAIL_INPUT = 8192
INSTALLATION_FEATURES = (
    "existing-mail-setup-v1",
    "invitation-mail-v1",
    "modal-add-images-v1",
    "modal-relay-v1",
    "server-diagnostics-v1",
    "server-maintenance-v1",
    "server-recovery-activation-v1",
    "server-recovery-preparation-v1",
    "server-release-identity-v1",
    "server-rollback-activation-v1",
    "server-rollback-preparation-v1",
    "server-rollback-state-v1",
    "server-rollback-upgrade-v1",
    "server-upgrade-activation-v1",
    "server-upgrade-preparation-v1",
)
MANAGED = "# Managed by Reasoning deployment\n"
ENVIRONMENT = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "DEBIAN_FRONTEND": "noninteractive",
    "PIP_CONFIG_FILE": "/dev/null",
}


class InstallError(Exception):
    """Operator-facing bootstrap failure without credentials or command output."""


def atomic_rename(source: Path, target: Path, *, exchange: bool = False) -> None:
    """Atomically publish or exchange paths without an overwrite window."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes, target_bytes = os.fsencode(source), os.fsencode(target)
    try:
        rename = libc.renameat2
    except AttributeError:
        try:
            rename = libc.renamex_np
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "atomic rename primitive unavailable") from exc
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        arguments = (source_bytes, target_bytes, 0x00000002 if exchange else 0x00000004)
    else:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        arguments = (-100, source_bytes, -100, target_bytes, 2 if exchange else 1)
    ctypes.set_errno(0)
    if rename(*arguments):
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "atomic rename target already exists", target)
        raise OSError(error, "atomic rename failed", source, target)


def public_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
            or parsed.netloc != parsed.netloc.lower()
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", parsed.hostname)
            or "." not in parsed.hostname
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in parsed.hostname.split("."))
        ):
            raise ValueError
        import ipaddress

        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return "https://" + parsed.hostname
        raise ValueError
    except ValueError as exc:
        raise InstallError("public URL requires a lowercase HTTPS DNS origin on port 443") from exc


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def install_python_payload(command, target, manifest, *, venv=None):
    """Application code is source; retained distribution releases remain readable."""
    source = "source/reasoning-source.zip"
    venv = venv or target / "venv"
    if source in manifest["files"]:
        command([str(venv / "bin/python"), "-I", str(target / "source-installer.py"), str(target / source)])
    else:
        wheel = next(name for name in manifest["files"] if name.startswith("wheel/"))
        command([str(venv / "bin/pip"), "--disable-pip-version-check", "install", "--no-cache-dir", "--no-deps", str(target / wheel)])


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    if str(path) != name or name.startswith("/") or ".." in path.parts or "\\" in name:
        return False
    if name in {
        "installer.py",
        "worker-installer.py",
        "server-operations.py",
        "server-upgrade.py",
        "server-recovery.py",
        "requirements.txt",
        "modal-requirements.txt",
        "compute-images.json",
        "LICENSE",
        "NOTICE",
        "source-installer.py",
        "source/reasoning-source.zip",
    }:
        return True
    if len(path.parts) == 2 and path.parts[0] == "wheel":
        return bool(re.fullmatch(r"reasoning_ledger-[0-9][A-Za-z0-9.]*-py3-none-any\.whl", path.name))
    if name in {"web/organization.html", "web/reasoning.svg"}:
        return True
    if len(path.parts) == 3 and path.parts[:2] in {("web", "assets"), ("web", "licenses")}:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", path.name))
    return name in {
        "docs/server.md",
        "docs/inference.md",
        "docs/compute.md",
        "docs/deployment.md",
        "docs/backup.md",
        "docs/bots.md",
        "docs/images.md",
        "docs/operations.md",
    }


def validate_manifest(body: bytes) -> dict:
    try:
        value = json.loads(body)
        if (
            len(body) > MAX_MANIFEST
            or not isinstance(value, dict)
            or set(value)
            not in (
                {"format", "source_revision", "dirty", "database_schema", "files"},
                {"format", "source_revision", "dirty", "database_schema", "files", "installation_features"},
            )
            or type(value["format"]) is not int
            or value["format"] != 1
            or type(value["dirty"]) is not bool
            or type(value["database_schema"]) is not int
            or not 1 <= value["database_schema"] <= 1000
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value["source_revision"])
        ):
            raise ValueError
        features = value.get("installation_features", [])
        if (
            not isinstance(features, list)
            or len(features) > 32
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", item) for item in features)
            or features != sorted(set(features))
        ):
            raise ValueError
        files = value["files"]
        if not isinstance(files, dict) or not 10 <= len(files) <= 512:
            raise ValueError
        required = {"installer.py", "requirements.txt", "LICENSE", "NOTICE", "web/organization.html", "web/reasoning.svg"}
        if "modal-relay-v1" in features:
            required.add("modal-requirements.txt")
        if "server-diagnostics-v1" in features:
            required |= {"server-operations.py", "docs/operations.md"}
        if {
            "server-upgrade-preparation-v1",
            "server-upgrade-activation-v1",
            "server-rollback-preparation-v1",
            "server-rollback-activation-v1",
        } & set(features):
            required |= {"server-upgrade.py", "server-operations.py", "docs/operations.md"}
        if {"server-recovery-preparation-v1", "server-recovery-activation-v1"} & set(features):
            required |= {"server-recovery.py", "server-upgrade.py", "server-operations.py", "docs/operations.md"}
        payloads = [name for name in files if name.startswith("wheel/") or name == "source/reasoning-source.zip"]
        if "source/reasoning-source.zip" in files:
            required.add("source-installer.py")
        if not required <= files.keys() or len(payloads) != 1:
            raise ValueError
        total = 0
        for name, row in files.items():
            if (
                not safe_name(name)
                or not isinstance(row, dict)
                or set(row) != {"size", "sha256"}
                or type(row["size"]) is not int
                or not 0 <= row["size"] <= 32 * 1024**2
                or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
            ):
                raise ValueError
            total += row["size"]
        if total > MAX_BUNDLE:
            raise ValueError
        if body != canonical(value):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid release manifest") from exc


def inspect_bundle(bundle: Path, expected: str | None = None) -> tuple[dict, str, str]:
    info = bundle.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE:
        raise InstallError("release must be a bounded regular archive, not a symlink")
    hasher = hashlib.sha256()
    with bundle.open("rb") as source:
        while block := source.read(65536):
            hasher.update(block)
    checksum = hasher.hexdigest()
    if expected is not None and (not re.fullmatch(r"[a-f0-9]{64}", expected) or checksum != expected):
        raise InstallError("release archive does not match its expected SHA-256")
    try:
        with gzip.open(bundle, "rb") as compressed:
            reader = TarReader(compressed)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                first = archive.next()
                if first is None or first.name != "manifest.json" or not first.isreg() or not 1 <= first.size <= MAX_MANIFEST:
                    raise InstallError("release must begin with a bounded regular manifest")
                stream = archive.extractfile(first)
                assert stream is not None
                manifest_bytes = stream.read(MAX_MANIFEST + 1)
                manifest = validate_manifest(manifest_bytes)
                seen, total = set(), 0
                while member := archive.next():
                    if (
                        member.name in seen
                        or member.name not in manifest["files"]
                        or not member.isreg()
                        or member.sparse is not None
                        or member.pax_headers
                        or member.size != manifest["files"][member.name]["size"]
                    ):
                        raise InstallError("release has duplicate, unexpected, or unsafe entries")
                    seen.add(member.name)
                    total += member.size
                    if total > MAX_BUNDLE:
                        raise InstallError("release exceeds its expanded byte allowance")
                    source = archive.extractfile(member)
                    assert source is not None
                    entry_hash = hashlib.sha256()
                    while block := source.read(65536):
                        entry_hash.update(block)
                    if entry_hash.hexdigest() != manifest["files"][member.name]["sha256"]:
                        raise InstallError("release file does not match its manifest")
                if seen != set(manifest["files"]):
                    raise InstallError("release omits files from its manifest")
            while reader.read(65536):
                pass
            if not reader.ended:
                raise InstallError("release has no complete end marker")
        return manifest, hashlib.sha256(manifest_bytes).hexdigest(), checksum
    except (tarfile.TarError, OSError, ValueError, EOFError) as exc:
        raise InstallError("invalid release archive") from exc


class TarReader:
    """Bound expanded bytes and reject extension metadata before tar parsing."""

    def __init__(self, stream):
        self.stream, self.consumed = stream, 0
        self.header, self.remaining, self.ended = bytearray(), 0, False

    def read(self, size: int = -1) -> bytes:
        allowance = MAX_BUNDLE + 1024**2 - self.consumed
        body = self.stream.read(min(65536 if size < 0 else size, allowance + 1))
        self.consumed += len(body)
        if self.consumed > MAX_BUNDLE + 1024**2:
            raise InstallError("release exceeds its expanded wire allowance")
        offset = 0
        while offset < len(body):
            if self.ended:
                if any(body[offset:]):
                    raise InstallError("release has nonzero trailing data")
                break
            if self.remaining:
                amount = min(self.remaining, len(body) - offset)
                self.remaining -= amount
                offset += amount
                continue
            amount = min(512 - len(self.header), len(body) - offset)
            self.header.extend(body[offset : offset + amount])
            offset += amount
            if len(self.header) < 512:
                continue
            if not any(self.header):
                self.ended = True
                self.header.clear()
                continue
            entry = tarfile.TarInfo.frombuf(bytes(self.header), "utf8", "surrogateescape")
            self.header.clear()
            if entry.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or not 0 <= entry.size <= 32 * 1024**2:
                raise InstallError("release entries must be bounded regular USTAR files without extended metadata")
            self.remaining = ((entry.size + 511) // 512) * 512
        return body


def extract_bundle(bundle: Path, destination: Path) -> None:
    if any(destination.iterdir()):
        raise InstallError("release staging directory must be empty")
    inspect_bundle(bundle)
    with gzip.open(bundle, "rb") as compressed, tarfile.open(fileobj=TarReader(compressed), mode="r|") as archive:
        for member in archive:
            if not member.isreg() or (member.name != "manifest.json" and not safe_name(member.name)):
                raise InstallError("unsafe release entry")
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            source = archive.extractfile(member)
            assert source is not None
            with os.fdopen(fd, "wb") as output, source:
                os.fchmod(output.fileno(), 0o644)
                shutil.copyfileobj(source, output, 65536)
                output.flush()
                os.fsync(output.fileno())
    for path in destination.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o755)


def atomic_write(path: Path, body: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise InstallError("managed files must not be symlinks")
    fd, temporary = tempfile.mkstemp(prefix=".reasoning-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def server_unit(release: str, *, release_identity: bool = False, maintenance_operation: str | None = None) -> str:
    if not re.fullmatch(r"[a-f0-9]{64}", release):
        raise InstallError("invalid release ID")
    if maintenance_operation is not None and (
        not release_identity or not isinstance(maintenance_operation, str) or not re.fullmatch(r"[a-f0-9]{64}", maintenance_operation)
    ):
        raise InstallError("maintenance unit requires exact operation and release identity")
    body = (
        MANAGED
        + f"""[Unit]
Description=Reasoning organization server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=reasoning
Group=reasoning
WorkingDirectory=/var/lib/reasoning
ExecStart=/opt/reasoning/releases/{release}/venv/bin/reasoning-server \\
 --data-dir /var/lib/reasoning serve --public-url PUBLIC_ORIGIN \\
 --frontend-dir /opt/reasoning/releases/{release}/web --compute-images /etc/reasoning/compute-images.json
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/reasoning
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    )
    if release_identity:
        body = body.replace("serve --public-url", f"serve --deployment-release {release} --public-url")
    if maintenance_operation is not None:
        body = body.replace(" --public-url", f" --maintenance-operation {maintenance_operation} --public-url")
    return body


def proxy_unit() -> str:
    return (
        MANAGED
        + """[Unit]
Description=Reasoning HTTPS reverse proxy
After=network-online.target reasoning-server.service
Wants=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/bin/caddy run --config /etc/reasoning/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
StateDirectory=reasoning-proxy
StateDirectoryMode=0700
Environment=XDG_DATA_HOME=/var/lib/reasoning-proxy/data
Environment=XDG_CONFIG_HOME=/var/lib/reasoning-proxy/config
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/reasoning-proxy
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""
    )


def caddyfile(origin: str) -> str:
    hostname = urlsplit(public_origin(origin)).hostname
    return (
        MANAGED
        + f"""{{
    admin off
}}
{hostname} {{
    encode zstd gzip
    reverse_proxy 127.0.0.1:8766 {{
        header_up X-Forwarded-For {{remote_host}}
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {hostname}
    }}
}}
"""
    )


def provisioning_request(value) -> dict:
    """Exact server VM identity, with no resource/data deletion authority."""
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "plan_id", "deployment_id", "droplet_id"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or not re.fullmatch(r"[a-f0-9]{64}", value["plan_id"])
            or not re.fullmatch(r"[a-f0-9]{32}", value["deployment_id"])
            or type(value["droplet_id"]) is not int
            or not 0 < value["droplet_id"] < 2**63
        ):
            raise ValueError
        return dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid exact server VM installation binding") from exc


def installation_binding(manifest, release, checksum, origin):
    return {
        "release_id": release,
        "bundle_sha256": checksum,
        "public_url": public_origin(origin),
        "source_revision": manifest["source_revision"],
        "dirty": manifest["dirty"],
    }


def owner_binding(body):
    try:
        value = json.loads(body)
        if (
            len(body) > MAX_OWNER_INPUT
            or not isinstance(value, dict)
            or set(value) != {"format", "setup_id", "request_token", "email", "name", "password", "organization_name"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or not re.fullmatch(r"[a-f0-9]{32}", value["setup_id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value["request_token"])
            or any(not isinstance(value[key], str) for key in ("email", "name", "password", "organization_name"))
            or canonical(value) != body
        ):
            raise ValueError
        return {"setup_id": value["setup_id"], "input_sha256": hashlib.sha256(body).hexdigest()}
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid bounded canonical initial-owner input") from exc


def owner_result(value, binding):
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "setup_id", "input_sha256", "organization_id", "user_id", "initialized"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["initialized"] is not True
            or any(value[key] != item for key, item in binding.items())
            or not re.fullmatch(r"[a-f0-9]{32}", value["organization_id"])
            or not re.fullmatch(r"[a-f0-9]{32}", value["user_id"])
        ):
            raise ValueError
        return dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("initial-owner setup did not return an exact secret-free receipt") from exc


def mail_binding(body):
    """Validate the standalone installer's bounded canonical sending configuration."""
    try:
        value = json.loads(body)
        if (
            len(body) > MAX_MAIL_INPUT
            or not isinstance(value, dict)
            or set(value) != {"format", "provider", "sender", "api_key", "daily_limit"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["provider"] != "resend"
            or not isinstance(value["sender"], str)
            or not 3 <= len(value["sender"]) <= 254
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value["sender"])
            or not isinstance(value["api_key"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{8,4096}", value["api_key"])
            or type(value["daily_limit"]) is not int
            or not 1 <= value["daily_limit"] <= 10000
            or canonical(value) != body
        ):
            raise ValueError
        local, domain = value["sender"].split("@")
        if (
            len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in value["sender"]
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in domain.split("."))
        ):
            raise ValueError
        return {"input_sha256": hashlib.sha256(body).hexdigest()}
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid bounded canonical private mail input") from exc


def require_mail_release(manifest):
    if "invitation-mail-v1" not in manifest.get("installation_features", []):
        raise InstallError("release does not support private mail installation; build a current mail-enabled bundle")


def mail_result(value, binding):
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "input_sha256", "configuration_installed", "worker_running"}
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["input_sha256"] != binding["input_sha256"]
            or value["configuration_installed"] is not True
            or value["worker_running"] is not True
        ):
            raise ValueError
        return dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("mail installation did not return an exact secret-free receipt") from exc


def installation_result(value, binding, provisioning=None, owner=None, mail=None):
    """Validate a successful, bounded, exact and secret-free host receipt."""
    request = provisioning_request(provisioning) if provisioning is not None else None
    try:
        fields = {"phase", "service_active", "https_ready", "initialized", "setup_token_file"}
        if request is not None:
            fields |= {"provisioning", "host_identity_verified"}
        if owner is not None:
            fields.add("owner_setup")
        if mail is not None:
            fields.add("mail_setup")
        if (
            not isinstance(value, dict)
            or set(value) != set(binding) | fields
            or any(canonical(value[key]) != canonical(item) for key, item in binding.items())
            or value["phase"] != "services_started"
            or value["service_active"] is not True
            or value["https_ready"] is not True
            or type(value["initialized"]) is not bool
            or value["setup_token_file"] != "/var/lib/reasoning/setup-token"
            or request is not None
            and (canonical(value["provisioning"]) != canonical(request) or value["host_identity_verified"] is not True)
        ):
            raise ValueError
        if owner is not None:
            owner_result(value["owner_setup"], owner)
            if value["initialized"] is not True:
                raise ValueError
        if mail is not None:
            mail_result(value["mail_setup"], mail)
        return dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("server bootstrap did not return a verified exact installation receipt") from exc


def private_document(path: Path, maximum=4096):
    for parent in path.absolute().parents:
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {os.getuid(), Path("/").stat().st_uid}
            or info.st_mode & 0o022
            and not (info.st_uid == Path("/").stat().st_uid and info.st_mode & stat.S_ISVTX)
        ):
            raise InstallError("private host inputs require protected nonsymlink parents")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise InstallError("host identity inputs must be owner-owned private regular files")
        body = source.read(maximum + 1)
        if len(body) > maximum:
            raise InstallError("host identity input exceeds its byte allowance")
    value = json.loads(body)
    if canonical(value) != body:
        raise InstallError("host identity input must be canonical JSON")
    return value, body


class HostInstaller:
    def __init__(self, *, root: Path = Path("/"), command=None):
        self.root, self.command = root, command or self._command
        self.state_dir = root / "var/lib/reasoning-deploy"
        self.state_path = self.state_dir / "installation.json"

    @staticmethod
    def _command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(args, env=ENVIRONMENT, umask=0o022, check=False, timeout=1200, capture_output=True)
        if check and result.returncode:
            raise InstallError("host command failed; inspect its service/package diagnostics")
        return result

    def _directory(self, relative: str, mode: int = 0o755) -> Path:
        path = self.root / relative
        current = self.root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise InstallError("managed paths must not traverse symlinks")
            if not current.exists():
                desired = mode if current == path else 0o755
                current.mkdir(mode=desired)
                os.chmod(current, desired)
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InstallError("managed paths must be owner-owned directories without public writes")
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise InstallError("managed paths must be owner-owned directories without public writes")
        return path

    def _managed(self, path: Path, body: str):
        self._check_managed(path, body)
        atomic_write(path, body.encode(), 0o644)

    def _check_managed(self, path: Path, expected: str):
        for parent in path.parents:
            if parent == self.root:
                break
            if not parent.exists() and not parent.is_symlink():
                continue
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InstallError("managed configuration parents must be protected directories, not symlinks")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_size != len(expected.encode())
                or path.read_bytes() != expected.encode()
            ):
                raise InstallError("refusing to overwrite an unmanaged configuration")

    def install(self, bundle: Path, expected: str, origin: str, *, skip_platform: bool = False, provisioning=None, mail=None) -> dict:
        public_origin(origin)
        manifest, _, _ = inspect_bundle(bundle, expected)
        if mail is not None:
            require_mail_release(manifest)
            mail_binding(mail)
        if not skip_platform and os.geteuid() != 0:
            raise InstallError("installation requires root")
        if provisioning is not None:
            provisioning = provisioning_request(provisioning)
            self._verify_provisioning(provisioning)
        directory = self._directory("var/lib/reasoning-deploy", 0o700)
        if directory.stat().st_mode & 0o077:
            raise InstallError("installation state must be private")
        fd = os.open(directory / "bootstrap.lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise InstallError("invalid private bootstrap lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("another bootstrap owns this host") from exc
            return self._install(bundle, expected, origin, skip_platform=skip_platform, provisioning=provisioning, mail=mail)
        finally:
            os.close(fd)

    def _install(
        self, bundle: Path, expected: str, origin: str, *, skip_platform: bool = False, provisioning=None, mail=None, recovery=None
    ) -> dict:
        recovery_path = self.state_dir / "active-server-recovery.json"
        if recovery_path.exists() or recovery_path.is_symlink():
            original, _ = private_document(recovery_path)
            if recovery is None or canonical(original) != canonical(recovery):
                raise InstallError("a managed recovery owns this installation; resume its original operation, not bootstrap")
        elif recovery is not None:
            raise InstallError("recovery staging requires its original protected ownership record")
        pending = self.state_dir / "active-server-upgrade.json"
        if pending.exists() or pending.is_symlink():
            raise InstallError("a managed upgrade owns this installation; resume its original operation, not bootstrap")
        origin = public_origin(origin)
        manifest, release_id, checksum = inspect_bundle(bundle, expected)
        unit_body = server_unit(release_id, release_identity="server-release-identity-v1" in manifest.get("installation_features", []))
        unit_body = unit_body.replace("PUBLIC_ORIGIN", origin)
        for relative in (
            "var/lib/reasoning-compute",
            "opt/reasoning-compute",
            "etc/systemd/system/reasoning-compute.service",
            "var/lib/reasoning-deploy/worker-installation.json",
        ):
            if (self.root / relative).exists() or (self.root / relative).is_symlink():
                raise InstallError("chat bootstrap refuses a compute-worker host; use separate hardware")
        if not skip_platform:
            if os.geteuid() != 0 or platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
                raise InstallError("installation requires root on a dedicated Linux amd64 host")
            os_release = (self.root / "etc/os-release").read_text()
            fields = dict(line.split("=", 1) for line in os_release.splitlines() if "=" in line)
            if fields.get("ID", "").strip('"') != "ubuntu" or fields.get("VERSION_ID", "").strip('"') != "24.04":
                raise InstallError("automated host bootstrap supports Ubuntu 24.04")
        state = None
        if self.state_path.exists() or self.state_path.is_symlink():
            info = self.state_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_MANIFEST:
                raise InstallError("invalid private installation journal")
            state = json.loads(self.state_path.read_bytes())
            if not isinstance(state, dict) or set(state) != {
                "release_id",
                "public_url",
                "bundle_sha256",
                "source_revision",
                "dirty",
                "phase",
            }:
                raise InstallError("invalid private installation journal")
            if (
                state["source_revision"] != manifest["source_revision"]
                or type(state["dirty"]) is not bool
                or state["dirty"] != manifest["dirty"]
                or not isinstance(state["phase"], str)
                or state["phase"] not in {"packages", "services_started"}
            ):
                raise InstallError("invalid private installation journal")
            if state.get("release_id") != release_id or state.get("public_url") != origin or state.get("bundle_sha256") != checksum:
                raise InstallError("an existing installation requires an explicit upgrade, not a new bootstrap")
        else:
            for relative in (
                "var/lib/reasoning",
                "etc/reasoning",
                "opt/reasoning",
                "etc/systemd/system/reasoning-server.service",
                "etc/systemd/system/reasoning-proxy.service",
            ):
                if (self.root / relative).exists() or (self.root / relative).is_symlink():
                    raise InstallError("refusing to adopt existing unmanaged Reasoning state or configuration")
            try:
                pwd.getpwnam("reasoning")
            except KeyError:
                pass
            else:
                raise InstallError("refusing to adopt an existing unmanaged service account")
            if (self.root / "etc/caddy/Caddyfile").exists():
                raise InstallError("use a dedicated host; an unmanaged Caddy installation already exists")
            for unit in ("caddy", "nginx", "apache2", "reasoning-server", "reasoning-proxy"):
                if self.command(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0:
                    raise InstallError("use a dedicated host without an active existing web service")
            self._directory("var/lib/reasoning-deploy", 0o700)
            os.chmod(self.state_dir, 0o700)
            state = {
                "release_id": release_id,
                "public_url": origin,
                "bundle_sha256": checksum,
                "source_revision": manifest["source_revision"],
                "dirty": manifest["dirty"],
                "phase": "packages",
            }
            atomic_write(self.state_path, canonical(state))
        self._directory("var/lib/reasoning-deploy", 0o700)
        binding = installation_binding(manifest, release_id, checksum, origin)
        mail_path = self.state_dir / "server-mail.json"
        if mail is not None:
            mail_expected = {"installation": binding, "mail": mail_binding(mail)}
            if mail_path.exists() or mail_path.is_symlink():
                previous_mail, _ = private_document(mail_path)
                if canonical(previous_mail) != canonical(mail_expected):
                    raise InstallError("mail installation requires its original exact input, release, and URL")
                secret = self.root / "var/lib/reasoning/mail.json"
                if secret.exists() or secret.is_symlink():
                    try:
                        self._mail_file(mail_expected["mail"], pwd.getpwnam("reasoning").pw_uid)
                    except KeyError as exc:
                        raise InstallError("managed mail service account is absent") from exc
                elif state["phase"] == "services_started":
                    raise InstallError("managed mail configuration is absent; installation cannot recreate a removed credential")
            else:
                secret = self.root / "var/lib/reasoning/mail.json"
                if secret.exists() or secret.is_symlink():
                    raise InstallError("refusing to adopt an existing unmanaged mail configuration")
                atomic_write(mail_path, canonical(mail_expected))
        elif mail_path.exists() or mail_path.is_symlink():
            raise InstallError("repeat the original --mail-file input; installation cannot omit managed mail")
        path = self.state_dir / "server-provisioning.json"
        if path.exists() or path.is_symlink():
            value, _ = private_document(path)
            if provisioning is None or canonical(value) != canonical({"format": 1, "binding": binding, "request": provisioning}):
                raise InstallError("owned server installation requires its original exact VM, release, and URL")
        elif provisioning is not None:
            atomic_write(path, canonical({"format": 1, "binding": binding, "request": provisioning}))
        for relative, body in (
            ("etc/reasoning/Caddyfile", caddyfile(origin)),
            ("etc/systemd/system/reasoning-server.service", unit_body),
            ("etc/systemd/system/reasoning-proxy.service", proxy_unit()),
        ):
            self._check_managed(self.root / relative, body)
        self.command(["apt-get", "update"])
        self.command(["apt-get", "install", "--yes", "ca-certificates", "python3-venv", "caddy"])
        self.command(["systemctl", "disable", "--now", "caddy"])
        releases = self._directory("opt/reasoning/releases")
        target = releases / release_id
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=releases))
            extract_bundle(bundle, temporary)
            os.chmod(temporary, 0o755)
            os.rename(temporary, target)
        else:
            if target.is_symlink() or not target.is_dir():
                raise InstallError("invalid managed release directory")
            for name, row in manifest["files"].items():
                path = target / name
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                    raise InstallError("existing release does not match its manifest")
        venv = target / "venv"
        self.command(["python3", "-m", "venv", str(venv)])
        self.command(
            [
                str(venv / "bin/pip"),
                "--disable-pip-version-check",
                "install",
                "--no-cache-dir",
                "--require-hashes",
                "--index-url",
                "https://pypi.org/simple",
                "-r",
                str(target / "requirements.txt"),
            ]
        )
        install_python_payload(self.command, target, manifest)
        try:
            identity = pwd.getpwnam("reasoning")
        except KeyError:
            self.command(
                ["useradd", "--system", "--user-group", "--home-dir", "/var/lib/reasoning", "--shell", "/usr/sbin/nologin", "reasoning"]
            )
            identity = pwd.getpwnam("reasoning")
        data = self.root / "var/lib/reasoning"
        if data.is_symlink():
            raise InstallError("server state must not be a symlink")
        if recovery is not None and (data.exists() or data.is_symlink()):
            raise InstallError("fresh recovery staging refuses existing canonical application state")
        if not data.exists() and recovery is None:
            data.mkdir(mode=0o700)
            os.chown(data, identity.pw_uid, identity.pw_gid)
        if recovery is None:
            info = data.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != identity.pw_uid or info.st_mode & 0o077:
                raise InstallError("server state must be private and owned by its service account")
        if mail is not None and recovery is None:
            secret = data / "mail.json"
            if secret.exists() or secret.is_symlink():
                self._mail_file(mail_binding(mail), identity.pw_uid)
            else:
                # Publish only complete, correctly owned bytes; do not overwrite a racing operator file.
                fd, temporary = tempfile.mkstemp(prefix=".mail-", dir=data)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(mail)
                        stream.flush()
                        os.fsync(stream.fileno())
                        os.fchown(stream.fileno(), identity.pw_uid, identity.pw_gid)
                        os.fsync(stream.fileno())
                    os.link(temporary, secret, follow_symlinks=False)
                    directory = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    os.unlink(temporary)
                self._mail_file(mail_binding(mail), identity.pw_uid)
        config = self._directory("etc/reasoning")
        catalog = config / "compute-images.json"
        if not catalog.exists():
            atomic_write(catalog, b"[]\n", 0o644)
        else:
            info = catalog.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 65536:
                raise InstallError("compute catalog must be a bounded protected regular file")
        units = self._directory("etc/systemd/system")
        self._managed(units / "reasoning-server.service", unit_body)
        self._managed(units / "reasoning-proxy.service", proxy_unit())
        self._managed(config / "Caddyfile", caddyfile(origin))
        self.command([str(venv / "bin/reasoning-server"), "--help"])
        if recovery is None:
            self.command(
                ["runuser", "-u", "reasoning", "--", str(venv / "bin/reasoning-server"), "--data-dir", "/var/lib/reasoning", "init"]
            )
        self.command(["caddy", "validate", "--config", str(config / "Caddyfile"), "--adapter", "caddyfile"])
        self.command(["systemctl", "daemon-reload"])
        if recovery is not None:
            if provisioning is not None:
                self._verify_provisioning(provisioning)
            return state
        self.command(["systemctl", "enable", "--now", "reasoning-server", "reasoning-proxy"])
        if mail is not None:
            self.command(["systemctl", "restart", "reasoning-server"])
        state["phase"] = "services_started"
        atomic_write(self.state_path, canonical(state))
        if provisioning is not None:
            self._verify_provisioning(provisioning)
        return state

    @staticmethod
    def _droplet_id():
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open("http://169.254.169.254/metadata/v1/id", timeout=3) as response:
                value = response.read(65)
                if response.status != 200 or not re.fullmatch(rb"[1-9][0-9]{0,18}\n?", value):
                    raise ValueError
                return int(value)
        except (OSError, TypeError, ValueError) as exc:
            raise InstallError("DigitalOcean host metadata is unavailable or invalid") from exc

    def _verify_provisioning(self, request):
        self._directory("etc")
        marker, _ = private_document(self.root / "etc/reasoning-provisioning.json")
        if (
            canonical(marker) != canonical({key: request[key] for key in ("plan_id", "deployment_id")})
            or self._droplet_id() != request["droplet_id"]
        ):
            raise InstallError("SSH host does not match the owned DigitalOcean deployment and Droplet")

    def _mail_file(self, binding, uid):
        path = self.root / "var/lib/reasoning/mail.json"
        try:
            parent = path.parent.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o077:
                raise ValueError
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != uid
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                    or info.st_size > MAX_MAIL_INPUT
                ):
                    raise ValueError
                body = stream.read(MAX_MAIL_INPUT + 1)
            if mail_binding(body) != binding:
                raise ValueError
        except (OSError, ValueError, InstallError) as exc:
            raise InstallError("managed mail configuration is absent, changed, or insecure") from exc

    def receipt(self, state, provisioning=None, mail=None):
        """Fresh checks; a journal or successful enable command is not readiness."""
        if state.get("phase") != "services_started":
            raise InstallError("server installation has not reached service activation")
        origin, release = state["public_url"], state["release_id"]
        modern = server_unit(release, release_identity=True).replace("PUBLIC_ORIGIN", origin)
        server_path = self.root / "etc/systemd/system/reasoning-server.service"
        # Both exact profiles retain the same managed executable and permissions.
        # Old installations do not provide an HTTPS release-identity assertion.
        identity = False
        if server_path.exists() or server_path.is_symlink():
            try:
                self._check_managed(server_path, modern)
                identity = True
            except InstallError:
                self._check_managed(server_path, server_unit(release).replace("PUBLIC_ORIGIN", origin))
        for unit, account, body in (
            ("reasoning-server", "reasoning", server_unit(release, release_identity=identity).replace("PUBLIC_ORIGIN", origin)),
            ("reasoning-proxy", "caddy", proxy_unit()),
        ):
            path = self.root / f"etc/systemd/system/{unit}.service"
            if not path.exists():
                raise InstallError("managed server/proxy unit is missing")
            self._check_managed(path, body)
            expected = {
                "FragmentPath": f"/etc/systemd/system/{unit}.service",
                "DropInPaths": "",
                "User": account,
                "Group": account,
                "LoadState": "loaded",
                "NeedDaemonReload": "no",
                "ActiveState": "active",
            }
            response = self.command(["systemctl", "show", "--no-pager", "--property=" + ",".join(expected), unit])
            try:
                if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 4096:
                    raise ValueError
                pairs = [line.split("=", 1) for line in response.stdout.decode().splitlines()]
                if len(pairs) != len(expected) or dict(pairs) != expected:
                    raise ValueError
            except (AttributeError, TypeError, ValueError) as exc:
                raise InstallError("server/proxy service is inactive, overridden, or has unverified configuration") from exc
        path = self.root / "etc/reasoning/Caddyfile"
        if not path.exists():
            raise InstallError("managed HTTPS proxy configuration is missing")
        self._check_managed(path, caddyfile(origin))
        if provisioning is not None:
            provisioning = provisioning_request(provisioning)
            self._verify_provisioning(provisioning)
        options = {"expected_release": release} if identity else {}
        if mail is not None:
            options["require_mail"] = True
        status = ready(origin, **options)
        binding = {key: state[key] for key in ("release_id", "public_url", "bundle_sha256", "source_revision", "dirty")}
        result = {
            **binding,
            "phase": "services_started",
            "service_active": True,
            "https_ready": status["https_ready"],
            "initialized": status["initialized"],
            "setup_token_file": "/var/lib/reasoning/setup-token",
        }
        if provisioning is not None:
            result.update(provisioning=provisioning, host_identity_verified=True)
        if mail is not None:
            expected, _ = private_document(self.state_dir / "server-mail.json")
            if canonical(expected) != canonical({"installation": binding, "mail": mail}):
                raise InstallError("managed mail installation binding changed")
            self._mail_file(mail, pwd.getpwnam("reasoning").pw_uid)
            result["mail_setup"] = {"format": 1, **mail, "configuration_installed": True, "worker_running": True}
        return installation_result(result, binding, provisioning, mail=mail)

    def setup_owner(self, state, body, provisioning=None):
        binding = owner_binding(body)
        fd = os.open(self.state_dir / "bootstrap.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise InstallError("invalid private bootstrap lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("another bootstrap owns this host") from exc
            current, _ = private_document(self.state_path)
            if canonical(current) != canonical(state):
                raise InstallError("server installation changed before initial-owner setup")
            # Do not use an old success journal as host/readiness evidence.
            self.receipt(state, provisioning)
            identity = pwd.getpwnam("reasoning")
            data = self.root / "var/lib/reasoning"
            info = data.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != identity.pw_uid or info.st_mode & 0o077:
                raise InstallError("owner setup requires the original private service-owned state")
            executable = self.root / "opt/reasoning/releases" / state["release_id"] / "venv/bin/reasoning-server"
            runtime = self._directory("run")
            with tempfile.TemporaryDirectory(prefix="reasoning-owner-", dir=runtime) as temporary:
                directory = Path(temporary)
                os.chown(directory, identity.pw_uid, identity.pw_gid)
                path = directory / "owner.json"
                atomic_write(path, body)
                os.chown(path, identity.pw_uid, identity.pw_gid)
                response = self.command(
                    [
                        "runuser",
                        "-u",
                        "reasoning",
                        "--",
                        str(executable),
                        "--data-dir",
                        str(data),
                        "setup-file",
                        "--input",
                        str(path),
                        "--expected-sha256",
                        binding["input_sha256"],
                    ]
                )
                try:
                    if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 4096:
                        raise ValueError
                    value = json.loads(response.stdout)
                except (AttributeError, TypeError, ValueError) as exc:
                    raise InstallError("host owner setup did not return a bounded receipt; retain original private inputs") from exc
                return owner_result(value, binding)
        finally:
            os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def ready(origin: str, *, timeout: float = 60, require_mail: bool = False, expected_release: str | None = None) -> dict:
    origin = public_origin(origin)
    if expected_release is not None and (not isinstance(expected_release, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_release)):
        raise InstallError("invalid expected health release ID")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(origin + "/api/health", timeout=3) as response:
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise ValueError
                if expected_release is not None and response.headers.get_all("X-Reasoning-Release", []) != [expected_release]:
                    raise ValueError
                value = json.loads(body)
                if not isinstance(value, dict) or value.get("status") != "ok" or type(value.get("initialized")) is not bool:
                    raise ValueError
                if require_mail and value.get("mail_worker_running") is not True:
                    raise ValueError
                return {"public_url": origin, "https_ready": True, "initialized": value["initialized"]}
        except (OSError, ValueError, TypeError):
            if time.monotonic() >= deadline:
                raise InstallError("services started but HTTPS readiness is unverified; check DNS, firewall, and service logs") from None
            time.sleep(min(2, max(0, deadline - time.monotonic())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--provisioning", type=Path)
    parser.add_argument("--expected-provisioning-sha256")
    parser.add_argument("--owner-input", type=Path)
    parser.add_argument("--expected-owner-sha256")
    parser.add_argument("--mail-input", type=Path)
    parser.add_argument("--expected-mail-sha256")
    args = parser.parse_args(argv)
    try:
        provisioning = None
        if args.provisioning is not None or args.expected_provisioning_sha256 is not None:
            if args.provisioning is None or args.expected_provisioning_sha256 is None:
                raise InstallError("server provisioning requires both a private request and its expected SHA-256")
            provisioning, body = private_document(args.provisioning)
            if hashlib.sha256(body).hexdigest() != args.expected_provisioning_sha256:
                raise InstallError("server provisioning input does not match its expected SHA-256")
            provisioning = provisioning_request(provisioning)
        installer = HostInstaller()
        owner_body = owner = None
        if args.owner_input is not None or args.expected_owner_sha256 is not None:
            if args.owner_input is None or args.expected_owner_sha256 is None:
                raise InstallError("owner setup requires both a private input and its expected SHA-256")
            _, owner_body = private_document(args.owner_input, MAX_OWNER_INPUT)
            owner = owner_binding(owner_body)
            if owner["input_sha256"] != args.expected_owner_sha256:
                raise InstallError("owner input does not match its expected SHA-256")
        mail_body = mail = None
        if args.mail_input is not None or args.expected_mail_sha256 is not None:
            if args.mail_input is None or args.expected_mail_sha256 is None:
                raise InstallError("mail setup requires both a private input and its expected SHA-256")
            _, mail_body = private_document(args.mail_input, MAX_MAIL_INPUT)
            mail = mail_binding(mail_body)
            if mail["input_sha256"] != args.expected_mail_sha256:
                raise InstallError("mail input does not match its expected SHA-256")
        state = installer.install(
            args.bundle,
            args.expected_sha256,
            args.public_url,
            provisioning=provisioning,
            **({"mail": mail_body} if mail_body is not None else {}),
        )
        result = installer.receipt(state, provisioning, **({"mail": mail} if mail is not None else {}))
        if owner_body is not None:
            proof = installer.setup_owner(state, owner_body, provisioning)
            result = installer.receipt(state, provisioning, **({"mail": mail} if mail is not None else {}))
            result["owner_setup"] = proof
            # The operator retains its original private file for lost-reply
            # recovery. Remove only our protected uploaded staging copy.
            path = args.owner_input
            if (
                path.name == "owner.json"
                and re.fullmatch(r"[a-f0-9]{64}", path.parent.name)
                and path.parent.parent == installer.state_dir / "staging"
            ):
                path.unlink()
        if mail is not None:
            path = args.mail_input
            if (
                path.name == "mail.json"
                and re.fullmatch(r"[a-f0-9]{64}", path.parent.name)
                and path.parent.parent == installer.state_dir / "staging"
            ):
                path.unlink()
        binding = {key: state[key] for key in ("release_id", "bundle_sha256", "public_url", "source_revision", "dirty")}
        print(json.dumps(installation_result(result, binding, provisioning, owner, mail)))
        return 0
    except (InstallError, OSError, ValueError) as exc:
        # Never echo package/server response bodies or secret file contents.
        message = str(exc) if isinstance(exc, InstallError) else "host installation state is unavailable or invalid"
        parser.exit(2, f"reasoning-deploy: {message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
