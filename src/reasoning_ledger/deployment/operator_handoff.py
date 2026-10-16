"""Enroll a second deployment operator without copying the first operator's secrets."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import setup_ui
from .digitalocean import DigitalOcean
from .host import MAX_BUNDLE, InstallError, canonical, inspect_bundle
from .private import locked_directory, protected_parent, read_document, read_private, write_document, write_private
from .provision import plan_identity
from .release_state import archived_release, current_binding, record_current, retain_release, validate_binding
from .setup import approve, ask, digitalocean_token, temporary_ssh_cidr
from .setup_ssh import derive_public, public_identity, ssh_fingerprint
from .setup_upgrade import _ssh_action
from .ssh_host import JOURNAL, PUBLIC_JOURNAL, host_identity, public_host_identity, server_trust

DOCUMENT_NAMES = (
    "delegation.json",
    "plan.json",
    "setup-completion.json",
    "ownership/digitalocean-installation.json",
    "ownership/digitalocean.json",
    "ownership/" + PUBLIC_JOURNAL,
    "current-release.json",
)
MAX_HANDOFF = MAX_BUNDLE + 1024**2

AUTHORIZE_SCRIPT = r"""import base64, hashlib, json, os, pathlib, stat, sys
key = base64.b64decode(sys.argv[1], validate=True).decode('ascii')
parts = key.split()
if len(parts) != 2 or parts[0] != 'ssh-ed25519' or any(c in key for c in '\r\n\0'):
    raise SystemExit('invalid operator key')
directory = pathlib.Path('/root/.ssh')
path = directory / 'authorized_keys'
if not directory.exists():
    directory.mkdir(mode=0o700)
info = directory.lstat()
if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
    raise SystemExit('unsafe root SSH directory')
body = b''
if path.exists() or path.is_symlink():
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 1024 * 1024:
            raise SystemExit('unsafe root authorized keys')
        body = stream.read(1024 * 1024 + 1)
try:
    text = body.decode('utf-8')
except UnicodeDecodeError:
    raise SystemExit('invalid root authorized keys')
present = any(line.split()[:2] == parts for line in text.splitlines())
if not present:
    separator = b'' if not body or body.endswith(b'\n') else b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(separator + key.encode('ascii') + b' Reasoning deployment operator\n')
        stream.flush()
        os.fsync(stream.fileno())
fingerprint = hashlib.sha256(base64.b64decode(parts[1])).hexdigest()
print(json.dumps({'format': 1, 'phase': 'authorized', 'public_key_sha256': fingerprint}, sort_keys=True, separators=(',', ':')))
"""


def _read_public_key(path: Path) -> str:
    path = path.expanduser().absolute()
    try:
        protected_parent(path, writable=False)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_nlink != 1
                or info.st_size > 16384
            ):
                raise InstallError("operator public key must be a bounded owner-owned regular file")
            return public_identity(stream.read().decode("ascii"))
    except (OSError, UnicodeError) as exc:
        raise InstallError("operator public key must be a readable local public-key file") from exc


def _fingerprint(public: str) -> str:
    return hashlib.sha256(base64.b64decode(public.split()[1])).hexdigest()


def prompt_public_key() -> str:
    """Ask for the collaborator's public key; the file is read once and need not be kept."""
    setup_ui.section(
        "Collaborator public key",
        "Enter the path of the public-key file the collaborator sent (for example ~/Downloads/operator.pub), "
        "or paste its single ssh-ed25519 line. The file is read once; it does not need a permanent location.",
    )
    value = ask("Public-key file path or key line")
    if value.startswith("ssh-"):
        return public_identity(value)
    return _read_public_key(Path(value))


def default_handoff(state: Path, public: str, release_id: str) -> Path:
    """Name one handoff per collaborator key and release inside the deployment state's private handoffs directory."""
    directory = state / "handoffs"
    if not directory.exists() and not directory.is_symlink():
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstallError(f"handoff directory {directory} must be an owner-only directory (mode 0700), not a symlink")
    return directory / f"operator-{_fingerprint(public)[:16]}-release-{release_id[:12]}.tar.gz"


def handoff_destination(output: Path) -> Path:
    """Check a handoff destination before any access change; name the exact offending path."""
    output = output.expanduser().absolute()
    parent = output.parent
    if not parent.is_dir() and not parent.is_symlink():
        raise InstallError(f"handoff archive directory {parent} does not exist; create it with mode 0700, "
                           "or use the default handoff path under the deployment state's handoffs directory")
    protected_parent(output)
    if output.is_symlink() or output.exists() and not output.is_file():
        raise InstallError(f"handoff archive path {output} must be a new file name, not a symlink or directory")
    return output


def _authorize_remote(address: str, identity: Path, known_hosts: Path, public: str, *, runner=subprocess.run) -> dict:
    from .cli import ssh_arguments

    encoded = base64.b64encode(public.encode("ascii")).decode("ascii")
    try:
        response = runner(
            [
                "ssh",
                *ssh_arguments(address, identity, known_hosts),
                "root@" + address,
                shlex.join(["python3", "-c", AUTHORIZE_SCRIPT, encoded]),
            ],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if response.returncode or not isinstance(response.stdout, bytes) or len(response.stdout) > 4096:
            raise ValueError
        value = json.loads(response.stdout)
        expected = {"format": 1, "phase": "authorized", "public_key_sha256": _fingerprint(public)}
        if value != expected:
            raise ValueError
        return value
    except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError("deployment-operator SSH enrollment is unverified; retry with the same key and state") from exc


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def _write_handoff(output: Path, documents: dict[str, dict], release_name: str, release: Path) -> None:
    output = handoff_destination(output)
    if output.exists() or output.is_symlink():
        raise InstallError("operator handoff output already exists; choose a new filename")
    fd, temporary_name = tempfile.mkstemp(prefix=".operator-handoff-", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w+b") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in DOCUMENT_NAMES:
                body = canonical(documents[name])
                archive.addfile(_tar_info(name, len(body)), io.BytesIO(body))
            size = release.stat().st_size
            if not 0 < size <= MAX_BUNDLE:
                raise InstallError("installed release archive has an invalid size")
            with release.open("rb") as source:
                archive.addfile(_tar_info(release_name, size), source)
        if temporary.stat().st_size > MAX_HANDOFF:
            raise InstallError("operator handoff exceeds its size allowance")
        os.replace(temporary, output)
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _read_handoff_body(body: bytes) -> tuple[dict[str, dict], str, bytes]:
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_HANDOFF:
        raise InstallError("invalid deployment-operator handoff archive")
    documents: dict[str, dict] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            release_names = [name for name in names if name.startswith("releases/") and name.endswith(".tar.gz")]
            if len(release_names) != 1 or set(names) != set(DOCUMENT_NAMES) | set(release_names) or len(names) != len(set(names)):
                raise ValueError
            release_name = release_names[0]
            for member in members:
                if not member.isfile() or member.size < 0 or member.size > (MAX_BUNDLE if member.name == release_name else 256 * 1024):
                    raise ValueError
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError
                entry = stream.read(member.size + 1)
                if len(entry) != member.size:
                    raise ValueError
                if member.name == release_name:
                    release_body = entry
                else:
                    value = json.loads(entry)
                    if not isinstance(value, dict) or canonical(value) != entry:
                        raise ValueError
                    documents[member.name] = value
        return documents, release_name, release_body
    except (KeyError, OSError, tarfile.TarError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError("invalid deployment-operator handoff archive") from exc


def _read_handoff(path: Path) -> tuple[dict[str, dict], str, bytes]:
    return _read_handoff_body(read_private(path.absolute(), MAX_HANDOFF))


def _validate_documents(documents: dict[str, dict], release_name: str, release_body: bytes) -> tuple[dict, str]:
    plan = documents["plan.json"]
    plan_id = plan_identity(plan)
    binding = validate_binding(documents["current-release.json"])
    completion = documents["setup-completion.json"]
    installation = documents["ownership/digitalocean-installation.json"]
    ownership = documents["ownership/digitalocean.json"]
    host = documents["ownership/" + PUBLIC_JOURNAL]
    delegation = documents["delegation.json"]
    expected_release_name = "releases/" + binding["release_id"] + ".tar.gz"
    try:
        intent = installation["intent"]
        request = intent["request"]
        if (
            release_name != expected_release_name
            or completion["plan_id"] != plan_id
            or completion["release_id"] != binding["release_id"]
            or completion["public_url"] != binding["public_url"]
            or completion["compute_backend"] not in {"none", "modal"}
            or completion["compute_backend"] == "none" and completion["compute_setup"] is not None
            or completion["compute_backend"] == "modal" and not isinstance(completion["compute_setup"], dict)
            or intent["binding"]["release_id"] != binding["release_id"]
            or intent["binding"]["bundle_sha256"] != binding["bundle_sha256"]
            or intent["binding"]["source_revision"] != binding["source_revision"]
            or intent["binding"]["public_url"] != binding["public_url"]
            or request["plan_id"] != plan_id
            or ownership["plan_id"] != plan_id
            or ownership["resources"]["droplet"] != {"status": "created", "id": request["droplet_id"]}
            or host
            != {
                "format": 1,
                "plan_id": plan_id,
                "deployment_id": plan["deployment_id"],
                "public_key": public_identity(host["public_key"]),
            }
            or delegation
            != {
                "format": 1,
                "account_identity": plan["plan"]["account_identity"],
                "operator_public_key": public_identity(delegation["operator_public_key"]),
            }
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("deployment-operator handoff records disagree") from exc
    with tempfile.TemporaryDirectory(prefix="reasoning-handoff-release-") as temporary:
        release_path = Path(temporary) / "release.tar.gz"
        release_path.write_bytes(release_body)
        manifest, release_id, checksum = inspect_bundle(release_path, binding["bundle_sha256"])
        if (
            release_id != binding["release_id"]
            or manifest["source_revision"] != binding["source_revision"]
            or manifest["dirty"]
            or checksum != binding["bundle_sha256"]
        ):
            raise InstallError("handoff release differs from the installed release")
    return binding, plan_id


def _handoff_installation(state: Path, source: dict) -> tuple[dict, dict, str]:
    plan = read_document(state / "plan.json")
    completion = read_document(state / "setup-completion.json")
    installation = read_document(state / "ownership" / "digitalocean-installation.json")
    try:
        intent = installation["intent"]
        if (
            completion["release_id"] != source["release_id"]
            or completion["public_url"] != source["public_url"]
            or intent["binding"]["release_id"] != source["release_id"]
            or intent["binding"]["public_url"] != source["public_url"]
            or not isinstance(intent["ssh_host"], str)
            or intent["request"]["plan_id"] != completion["plan_id"]
        ):
            raise ValueError
        return plan, intent["request"], intent["ssh_host"]
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("operator handoff requires the original completed deployment state") from exc


def grant_operator_locked(
    state: Path,
    public: str,
    output: Path | None,
    command,
    *,
    source_bundle: Path | None = None,
    detect_public_ipv4=None,
    authorize_remote=_authorize_remote,
    display: bool = True,
    header: bool = True,
) -> dict:
    """Authorize one key and create its handoff while the caller owns the state lock.

    Without an explicit output, the handoff is named for the key and release under ``state/handoffs``.
    The destination is checked before SSH opens or the key is enrolled.
    """
    public = public_identity(public)
    if not public.startswith("ssh-ed25519 "):
        raise InstallError("deployment operators must use an Ed25519 SSH key")
    if display and header:
        setup_ui.operator_header("authorize")
    source = current_binding(state)
    plan_path, ownership = state / "plan.json", state / "ownership"
    deployment, request, address = _handoff_installation(state, source)
    plan_id = request["plan_id"]
    source_archive = archived_release(state, source["release_id"])
    if source_archive.exists() or source_archive.is_symlink():
        inspect_bundle(source_archive, source["bundle_sha256"])
    elif source_bundle is not None:
        source_archive = retain_release(source_bundle, state, source)
    else:
        raise InstallError("retain the installed release before creating an operator handoff")
    output = handoff_destination(default_handoff(state, public, source["release_id"]) if output is None else output)
    fingerprint = ssh_fingerprint(public)
    if output.exists():
        documents, release_name, release_body = _read_handoff(output)
        binding, _existing_plan = _validate_documents(documents, release_name, release_body)
        if binding != source or documents["delegation.json"]["operator_public_key"] != public:
            raise InstallError(f"existing operator handoff {output} differs from this deployment or public key; "
                               "choose a new file name")
        result = {
            "phase": "operator_handoff_ready",
            "public_url": source["public_url"],
            "handoff": str(output),
            "handoff_sha256": hashlib.sha256(read_private(output, MAX_HANDOFF)).hexdigest(),
            "operator_public_key_sha256": _fingerprint(public),
            "operator_public_key_fingerprint": fingerprint,
        }
        if display:
            setup_ui.operator_handoff_ready(source["public_url"], fingerprint, str(output), result["handoff_sha256"])
        return result
    config_path = state / "setup.json"
    config = read_document(config_path)
    session_cidr = temporary_ssh_cidr(detect_public_ipv4)
    if display:
        setup_ui.operator_review(source["public_url"], fingerprint, str(output), session_cidr)
    config, opened = _ssh_action(
        command,
        state,
        config_path,
        config,
        plan_path,
        plan_id,
        ownership,
        ["--open-cidr", session_cidr, "--wait-seconds", 60],
    )
    if opened.get("phase") != "open":
        return {"phase": "ssh_access_opening", "state_dir": str(state)}
    result = None
    try:
        approve(
            "Authorize the collaborator key with the fingerprint shown above on this site's server?",
            "authorize operator " + urlsplit(source["public_url"]).hostname,
        )
        with server_trust(deployment, ownership, address, request) as known_hosts:
            authorize_remote(address, Path(config["identity"]), known_hosts, public)
        private_host = ownership / JOURNAL
        original_host = (
            host_identity(ownership, plan_id, deployment["deployment_id"])
            if private_host.exists() or private_host.is_symlink()
            else public_host_identity(ownership, plan_id, deployment["deployment_id"])
        )
        release_name = "releases/" + source["release_id"] + ".tar.gz"
        documents = {
            "delegation.json": {
                "format": 1,
                "account_identity": deployment["plan"]["account_identity"],
                "operator_public_key": public,
            },
            "plan.json": deployment,
            "setup-completion.json": read_document(state / "setup-completion.json"),
            "ownership/digitalocean-installation.json": read_document(ownership / "digitalocean-installation.json"),
            "ownership/digitalocean.json": read_document(ownership / "digitalocean.json"),
            "ownership/" + PUBLIC_JOURNAL: {
                "format": 1,
                "plan_id": plan_id,
                "deployment_id": deployment["deployment_id"],
                "public_key": original_host["public_key"],
            },
            "current-release.json": source,
        }
        try:
            _write_handoff(output, documents, release_name, source_archive)
        except (InstallError, OSError) as exc:
            raise InstallError(f"the collaborator key is authorized on the server, but the handoff archive {output} "
                               f"was not written ({exc}); fix that and rerun the same command, "
                               "which reuses the authorization") from exc
        result = {
            "phase": "operator_handoff_ready",
            "public_url": source["public_url"],
            "handoff": str(output),
            "handoff_sha256": hashlib.sha256(read_private(output, MAX_HANDOFF)).hexdigest(),
            "operator_public_key_sha256": _fingerprint(public),
            "operator_public_key_fingerprint": fingerprint,
        }
    finally:
        config, closed = _ssh_action(
            command,
            state,
            config_path,
            config,
            plan_path,
            plan_id,
            ownership,
            ["--close", "--wait-seconds", 60],
        )
        if result is not None and closed.get("phase") != "closed":
            result = {**result, "phase": "ssh_access_closing"}
    if result is None:
        raise InstallError("operator handoff did not complete; retry with the same state and public key")
    if display:
        setup_ui.operator_handoff_ready(source["public_url"], fingerprint, str(output), result["handoff_sha256"],
                                        closing=result["phase"] != "operator_handoff_ready")
    return result


def grant_operator(
    directory: Path,
    public_key_file: Path | None,
    output: Path | None,
    command,
    *,
    source_bundle: Path | None = None,
    detect_public_ipv4=None,
    authorize_remote=_authorize_remote,
) -> dict:
    """Authorize a collaborator; without a key file, ask for it interactively."""
    directory = directory.absolute()
    if public_key_file is None:
        setup_ui.operator_header("authorize")
        public = prompt_public_key()
    else:
        public = _read_public_key(public_key_file)
    with locked_directory(directory) as state:
        return grant_operator_locked(
            state,
            public,
            output,
            command,
            source_bundle=source_bundle,
            detect_public_ipv4=detect_public_ipv4,
            authorize_remote=authorize_remote,
            header=public_key_file is not None,
        )


def join_operator(
    handoff: Path,
    directory: Path,
    token_file: Path,
    identity: Path,
    expected_handoff_sha256: str,
    *,
    retain_identity: bool = False,
    public_key: str | None = None,
) -> dict:
    """Join from an authenticated handoff.

    ``public_key`` is the public key the caller derived from ``identity`` in this process; passing it
    avoids a second local passphrase prompt. Without it, the key is derived here.
    """
    setup_ui.operator_header("join")
    handoff, directory = handoff.absolute(), directory.absolute()
    token_file, identity = token_file.absolute(), identity.absolute()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_handoff_sha256) or not secrets.compare_digest(
        hashlib.sha256(read_private(handoff, MAX_HANDOFF)).hexdigest(), expected_handoff_sha256
    ):
        raise InstallError("operator handoff SHA-256 does not match the value confirmed by the existing operator")
    documents, release_name, release_body = _read_handoff(handoff)
    binding, _plan_id = _validate_documents(documents, release_name, release_body)
    delegation = documents["delegation.json"]
    authorized = derive_public(identity) if public_key is None else public_identity(public_key)
    if authorized != delegation["operator_public_key"]:
        raise InstallError("SSH private key does not match the key authorized by this handoff")
    try:
        token = digitalocean_token(read_private(token_file, 4096))
    except InstallError as exc:
        raise InstallError(f"DigitalOcean token file {token_file}: {exc}") from None
    provider = DigitalOcean(token)
    try:
        if provider.identity() != delegation["account_identity"]:
            raise InstallError("DigitalOcean token must belong to the deployment's existing Team")
    finally:
        provider.close()
    with locked_directory(directory) as state:
        if set(path.name for path in state.iterdir()) != {"provision.lock"}:
            raise InstallError("new deployment-operator state directory must be empty")
        ownership = state / "ownership"
        releases = state / "releases"
        for child in (ownership, releases):
            child.mkdir(mode=0o700)
            child.chmod(0o700)
        for name in DOCUMENT_NAMES:
            if name == "delegation.json":
                continue
            write_document(state / name, documents[name], new=True)
        saved_identity = identity
        if retain_identity:
            saved_identity = state / "recovery-identity"
            write_private(saved_identity, read_private(identity, 65536))
        write_document(state / "setup.json", {"identity": str(saved_identity), "token_file": str(token_file)}, new=True)
        release_path = state / release_name
        fd = os.open(release_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(release_body)
            stream.flush()
            os.fsync(stream.fileno())
        inspect_bundle(release_path, binding["bundle_sha256"])
        record_current(state, binding)
    result = {
        "phase": "operator_ready",
        "public_url": binding["public_url"],
        "release_id": binding["release_id"],
        "state_dir": str(directory),
    }
    setup_ui.operator_joined(binding["public_url"], "Private state: " + str(directory))
    return result
