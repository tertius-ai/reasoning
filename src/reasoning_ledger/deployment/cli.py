"""Prepare public releases, plan DigitalOcean resources, and install Linux hosts."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .digitalocean import DigitalOcean
from .host import (
    MAX_BUNDLE,
    InstallError,
    canonical,
    inspect_bundle,
    installation_binding,
    installation_result,
    owner_binding,
    provisioning_request,
    public_origin,
)
from .private import protected_parent, protected_temporary_directory, read_document, read_private, write_document
from .provision import Provisioner, plan_identity, save_plan
from .release import build_bundle
from .worker_host import decode_input
from .worker_provision import WorkerProvisioner, plan_worker, validate_worker_plan, worker_inputs, worker_plan_identity

STAGE_SCRIPT = """import os, pathlib, stat, sys
root = pathlib.Path('/var/lib/reasoning-deploy')
target = pathlib.Path(sys.argv[1])
if target.parent != root / 'staging' or len(target.name) != 64 or any(c not in '0123456789abcdef' for c in target.name):
    raise SystemExit('invalid staging target')
for parent in (pathlib.Path('/var'), pathlib.Path('/var/lib')):
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('unsafe staging parent')
for directory in (root, root / 'staging', target):
    if not directory.exists() and not directory.is_symlink():
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise SystemExit('unsafe private staging directory')
"""


def ssh_arguments(hostname: str, identity: Path, known_hosts: Path) -> list[str]:
    # Only a DNS name or IPv4 literal is accepted here. No remote username,
    # options, commands, proxy jump, or unverified host-key bypass is inferred.
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", hostname):
        raise InstallError("SSH host must be a DNS name or IPv4 address")
    read_private(identity, 65536)
    if known_hosts.is_symlink() or not known_hosts.is_file():
        raise InstallError("provide a known-hosts file containing the independently verified server key")
    protected_parent(known_hosts.absolute(), writable=False)
    info = known_hosts.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {os.getuid(), Path("/").stat().st_uid}
        or info.st_mode & 0o022
        or info.st_nlink != 1
        or info.st_size > 1024**2
    ):
        raise InstallError("known-hosts file must be protected from public writes and belong to the operator or system")
    if any(char in str(known_hosts) for char in '\r\n\x00"\\'):
        raise InstallError("known-hosts path contains unsupported control or quoting characters")
    return [
        "-F",
        "/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "VerifyHostKeyDNS=no",
        "-o",
        "KnownHostsCommand=none",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        'UserKnownHostsFile="' + str(known_hosts.absolute()) + '"',
        "-i",
        str(identity.absolute()),
    ]


@contextmanager
def bundle_snapshot(bundle: Path, confirmed_release: str | None = None):
    with tempfile.TemporaryDirectory(prefix="reasoning-release-input-") as temporary:
        path = Path(temporary) / "release.tar.gz"
        fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE:
                raise InstallError("release input must be a bounded regular file")
            target = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(target, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                total = 0
                while block := source.read(65536):
                    total += len(block)
                    if total > MAX_BUNDLE:
                        raise InstallError("release input grew beyond its byte allowance")
                    output.write(block)
        _, release, _ = inspect_bundle(path)
        if confirmed_release is not None and release != confirmed_release:
            raise InstallError("confirm-release does not match the verified upload snapshot")
        yield path


def install_remote(
    bundle: Path,
    origin: str,
    hostname: str,
    identity: Path,
    known_hosts: Path,
    *,
    command=None,
    confirmed_release=None,
    provisioning=None,
    owner=None,
    mail=None,
) -> dict:
    with bundle_snapshot(bundle, confirmed_release) as snapshot:
        return _install_remote(
            snapshot, origin, hostname, identity, known_hosts, command=command, provisioning=provisioning, owner=owner, mail=mail
        )


def _install_remote(
    bundle: Path, origin: str, hostname: str, identity: Path, known_hosts: Path, *, command=None, provisioning=None, owner=None, mail=None
) -> dict:
    origin = public_origin(origin)
    manifest, release, checksum = inspect_bundle(bundle)
    options = ssh_arguments(hostname, identity, known_hosts)
    request = provisioning_request(provisioning) if provisioning is not None else None
    request_body = canonical(request) if request is not None else None
    owner_body = owner_expected = None
    if owner is not None:
        owner = validated_owner(owner)
        owner_body = canonical(owner)
        owner_expected = owner_binding(owner_body)
    mail_body = mail_expected = None
    if mail is not None:
        from .host import mail_binding, require_mail_release

        require_mail_release(manifest)
        mail_body = canonical(mail)
        mail_expected = mail_binding(mail_body)
    stage = hashlib.sha256(
        canonical(
            {
                "release": release,
                "origin": origin,
                "provisioning": request,
                **({"owner": owner_expected} if owner_expected else {}),
                **({"mail": mail_expected} if mail_expected else {}),
            }
        )
    ).hexdigest()
    destination = f"/var/lib/reasoning-deploy/staging/{stage}"

    def run(args, *, capture=False):
        if command is not None:
            return command(args)
        result = subprocess.run(args, check=False, capture_output=capture, timeout=1200)
        if result.returncode:
            raise InstallError("remote installation command failed; its verified staging bundle is retained for resumption")
        return result

    run(["ssh", *options, "root@" + hostname, shlex.join(["python3", "-c", STAGE_SCRIPT, destination])])
    with protected_temporary_directory("reasoning-installer-") as temporary:
        with tarfile.open(bundle, "r:gz") as archive:
            stream = archive.extractfile("installer.py")
            assert stream is not None
            installer = temporary / "installer.py"
            with installer.open("xb") as output:
                output.write(stream.read())
        run(["scp", *options, str(bundle.absolute()), "root@" + hostname + ":" + destination + "/release.tar.gz"])
        run(["scp", *options, str(installer), "root@" + hostname + ":" + destination + "/installer.py"])
        if request_body is not None:
            from .private import write_private

            request_path = temporary / "provisioning.json"
            write_private(request_path, request_body)
            run(["scp", *options, str(request_path), "root@" + hostname + ":" + destination + "/provisioning.json"])
        if owner_body is not None:
            from .private import write_private

            owner_path = temporary / "owner.json"
            write_private(owner_path, owner_body)
            run(["scp", *options, str(owner_path), "root@" + hostname + ":" + destination + "/owner.json"])
        if mail_body is not None:
            from .private import write_private

            mail_path = temporary / "mail.json"
            write_private(mail_path, mail_body)
            run(["scp", *options, str(mail_path), "root@" + hostname + ":" + destination + "/mail.json"])
    response = run(
        [
            "ssh",
            *options,
            "root@" + hostname,
            shlex.join(
                [
                    "python3",
                    destination + "/installer.py",
                    "--bundle",
                    destination + "/release.tar.gz",
                    "--expected-sha256",
                    checksum,
                    "--public-url",
                    origin,
                    *(
                        [
                            "--provisioning",
                            destination + "/provisioning.json",
                            "--expected-provisioning-sha256",
                            hashlib.sha256(request_body).hexdigest(),
                        ]
                        if request_body is not None
                        else []
                    ),
                    *(
                        ["--owner-input", destination + "/owner.json", "--expected-owner-sha256", owner_expected["input_sha256"]]
                        if owner_expected
                        else []
                    ),
                    *(
                        ["--mail-input", destination + "/mail.json", "--expected-mail-sha256", mail_expected["input_sha256"]]
                        if mail_expected
                        else []
                    ),
                ]
            ),
        ],
        capture=True,
    )
    try:
        if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 65536:
            raise ValueError
        value = decode_input(response.stdout)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InstallError("remote server installation did not return a bounded verified status") from exc
    return installation_result(value, installation_binding(manifest, release, checksum, origin), request, owner_expected, mail_expected)


def validated_owner(value):
    from reasoning_ledger.server.onboarding import owner_input
    from reasoning_ledger.server.store import StoreError

    try:
        return owner_input(value)
    except StoreError as exc:
        raise InstallError("invalid private initial-owner input") from exc


def read_owner(path):
    from .host import MAX_OWNER_INPUT

    raw = read_private(path, MAX_OWNER_INPUT)
    value = validated_owner(decode_input(raw))
    if canonical(value) != raw:
        raise InstallError("private initial-owner input must be canonical JSON")
    return value


def read_mail(path):
    from reasoning_ledger.server.mail import MailConfig

    from .host import MAX_MAIL_INPUT, mail_binding

    raw = read_private(path, MAX_MAIL_INPUT)
    mail_binding(raw)
    value = decode_input(raw)
    # Validate with the actual runtime as well as the standalone root bootstrap.
    MailConfig(value["sender"], value["api_key"], value["daily_limit"])
    return value


def owned_server_remote(document, directory, bundle, address, identity, known_hosts=None, **kwargs):
    from .ssh_host import server_trust

    with server_trust(document, directory, address, kwargs.get("provisioning"), known_hosts) as trusted:
        return install_remote(bundle, document["plan"]["public_url"], address, identity, trusted, **kwargs)


def provision_until_ready(provisioner: Provisioner, directory: Path, confirmation: str, wait_seconds: int) -> dict:
    if not 0 <= wait_seconds <= 60:
        raise InstallError("provisioning wait must be between zero and 60 seconds")
    deadline = time.monotonic() + wait_seconds
    while True:
        result = provisioner.apply(directory, confirmation)
        remaining = deadline - time.monotonic()
        if result["phase"] in {"ssh_verification_required", "ssh_key_pinned"} or remaining <= 0:
            return result
        time.sleep(min(10, remaining))


def owned_worker_remote(document, directory, bundle, origin, registration, catalog, address, identity, known_hosts=None, **kwargs):
    from .ssh_host import worker_trust

    request = kwargs.get("destruction") or kwargs.get("provisioning")
    with worker_trust(document, directory, address, request, known_hosts) as trusted:
        return install_worker_remote(bundle, origin, registration, catalog, address, identity, trusted, **kwargs)


def install_worker_remote(
    bundle: Path,
    origin: str,
    registration: Path,
    catalog: Path,
    hostname: str,
    identity: Path,
    known_hosts: Path,
    *,
    command=None,
    confirmed_release=None,
    confirmed_worker=None,
    operation="install",
    destruction=None,
    provisioning=None,
) -> dict:
    if operation not in {"install", "retire", "prepare-destroy"}:
        raise InstallError("unsupported worker host operation")
    if (operation == "prepare-destroy") != (destruction is not None):
        raise InstallError("VM retirement needs exact destruction inputs; ordinary host operations must not contain them")
    if provisioning is not None and operation != "install":
        raise InstallError("VM provisioning inputs are only valid for worker installation")
    with bundle_snapshot(bundle, confirmed_release) as snapshot:
        return _install_worker_remote(
            snapshot,
            origin,
            registration,
            catalog,
            hostname,
            identity,
            known_hosts,
            command=command,
            confirmed_worker=confirmed_worker,
            operation=operation,
            destruction=destruction,
            provisioning=provisioning,
        )


def _install_worker_remote(
    bundle: Path,
    origin: str,
    registration: Path,
    catalog: Path,
    hostname: str,
    identity: Path,
    known_hosts: Path,
    *,
    command=None,
    confirmed_worker=None,
    operation="install",
    destruction=None,
    provisioning=None,
) -> dict:
    from reasoning_ledger.server.compute_catalog import ComputeImage

    origin = public_origin(origin)
    config, images = worker_inputs(origin, decode_input(read_private(registration, 65536)), decode_input(read_private(catalog, 65536)))
    if confirmed_worker is not None and config["id"] != confirmed_worker:
        raise InstallError("confirm-worker does not match the verified enrollment snapshot")
    # Recheck the pre-bootstrap schema against the authoritative installed type.
    for image in images:
        ComputeImage.parse(image)
    manifest, release, checksum = inspect_bundle(bundle)
    if "worker-installer.py" not in manifest["files"]:
        raise InstallError("release does not include a worker installer; rebuild the public release")
    options = ssh_arguments(hostname, identity, known_hosts)
    from .host import canonical

    enrollment_body, catalog_body = canonical(config), canonical(images)
    enrollment_hash, catalog_hash = hashlib.sha256(enrollment_body).hexdigest(), hashlib.sha256(catalog_body).hexdigest()
    destruction_body = provisioning_body = None
    if destruction is not None:
        from .worker_host import destruction_request

        destruction_body = canonical(destruction_request(destruction))
    stage_binding = {"release": release, "enrollment": enrollment_hash, "catalog": catalog_hash}
    if provisioning is not None:
        from .worker_host import provisioning_request

        provisioning = provisioning_request(provisioning)
        provisioning_body = canonical(provisioning)
        stage_binding["provisioning"] = hashlib.sha256(provisioning_body).hexdigest()
    stage = hashlib.sha256(canonical(stage_binding)).hexdigest()
    destination = "/var/lib/reasoning-deploy/staging/" + stage

    def run(args, *, capture=False):
        if command is not None:
            return command(args)
        response = subprocess.run(args, check=False, capture_output=capture, timeout=1200)
        if response.returncode:
            raise InstallError("worker host operation failed; retain the exact inputs and private staging/journal for resumption")
        return response

    run(["ssh", *options, "root@" + hostname, shlex.join(["python3", "-c", STAGE_SCRIPT, destination])])
    with tempfile.TemporaryDirectory(prefix="reasoning-worker-bootstrap-") as temporary:
        directory = Path(temporary)
        with tarfile.open(bundle, "r:gz") as archive:
            for name in ("installer.py", "worker-installer.py"):
                stream = archive.extractfile(name)
                if stream is None:
                    raise InstallError("worker installer missing from verified bundle")
                with (directory / name).open("xb") as output:
                    output.write(stream.read())
        for name, body in (("enrollment.json", enrollment_body), ("images.json", catalog_body)):
            fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(body)
        for name, body in (("destruction.json", destruction_body), ("provisioning.json", provisioning_body)):
            if body is not None:
                fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    output.write(body)
        run(["scp", *options, str(bundle.absolute()), "root@" + hostname + ":" + destination + "/release.tar.gz"])
        for name in (
            "installer.py",
            "worker-installer.py",
            "enrollment.json",
            "images.json",
            *(["destruction.json"] if destruction_body else []),
            *(["provisioning.json"] if provisioning_body else []),
        ):
            run(["scp", *options, str(directory / name), "root@" + hostname + ":" + destination + "/" + name])
    response = run(
        [
            "ssh",
            *options,
            "root@" + hostname,
            shlex.join(
                [
                    "python3",
                    destination + "/worker-installer.py",
                    "--bundle",
                    destination + "/release.tar.gz",
                    "--expected-sha256",
                    checksum,
                    "--server-url",
                    origin,
                    "--registration",
                    destination + "/enrollment.json",
                    "--catalog",
                    destination + "/images.json",
                    "--expected-enrollment-sha256",
                    enrollment_hash,
                    "--expected-catalog-sha256",
                    catalog_hash,
                    *(["--operation", operation] if operation != "install" else []),
                    *(
                        [
                            "--provisioning",
                            destination + "/provisioning.json",
                            "--expected-provisioning-sha256",
                            hashlib.sha256(provisioning_body).hexdigest(),
                        ]
                        if provisioning_body is not None
                        else []
                    ),
                    *(
                        [
                            "--destruction",
                            destination + "/destruction.json",
                            "--expected-destruction-sha256",
                            hashlib.sha256(destruction_body).hexdigest(),
                        ]
                        if destruction_body is not None
                        else []
                    ),
                ]
            ),
        ],
        capture=True,
    )
    binding = {
        "release_id": release,
        "bundle_sha256": checksum,
        "server_url": origin,
        "worker_id": config["id"],
        "organization_id": config["organization_id"],
        "source_revision": manifest["source_revision"],
        "dirty": manifest["dirty"],
        "enrollment_sha256": enrollment_hash,
        "catalog_sha256": catalog_hash,
    }
    from .worker_host import destruction_result, installation_result, retirement_result

    try:
        if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > 65536:
            raise ValueError
        value = decode_input(response.stdout)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InstallError("remote worker operation did not return a bounded verified status") from exc
    if operation == "prepare-destroy":
        return destruction_result(value, binding, destruction)
    if operation == "retire":
        return retirement_result(value, binding)
    return installation_result(value, binding, provisioning)


def main(argv: list[str] | None = None, *, raise_errors: bool = False) -> int:
    parser = argparse.ArgumentParser(prog="reasoning-deploy", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="interactive first organization launch; prompts for inputs and explicit spending consent")
    setup.add_argument("--bundle", required=True, type=Path)
    setup.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy", help="private resumable operator directory")
    setup.add_argument(
        "--compute-backend",
        choices=("auto", "none", "modal"),
        default="auto",
        help="offer Modal compute by default; choose none to skip or modal to require it",
    )
    compute_setup = commands.add_parser("compute-setup", help="enable Modal compute on an existing synchronized organization")
    compute_setup.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    mail_setup = commands.add_parser("email-setup", help="enable Resend mail on an existing synchronized managed deployment")
    mail_setup.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    upgrade_stage = commands.add_parser("upgrade-stage", help="retain the currently installed release before building its replacement")
    upgrade_stage.add_argument("--bundle", type=Path, help="installed release archive if it is not already retained in state")
    upgrade_stage.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    guided_upgrade = commands.add_parser("upgrade", help="interactively plan, prepare, activate, and verify a managed-server upgrade")
    guided_upgrade.add_argument("--bundle", required=True, type=Path, help="new target release built after upgrade-stage")
    guided_upgrade.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    onboard = commands.add_parser("onboard", help="interactive new-computer operator preparation, handoff, join, and verification")
    onboard.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy", help="new independent deployment state")
    onboard.add_argument("--onboard-dir", type=Path, default=Path.home() / ".reasoning-onboard",
                         help="private resumable key/token preparation")
    onboard.add_argument("--replace-token", action="store_true",
                         help="privately collect a new same-Team token without repeating a completed join")
    operator_authorize = commands.add_parser(
        "operator-authorize", help="authorize a collaborator's SSH key and create a credential-free deployment handoff"
    )
    operator_authorize.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_authorize.add_argument("--public-key", type=Path,
                                    help="collaborator's Ed25519 public-key file; prompted for when omitted")
    operator_authorize.add_argument("--output", type=Path,
                                    help="handoff archive to send to the collaborator; defaults to a per-key, "
                                         "per-release file under STATE_DIR/handoffs")
    operator_authorize.add_argument("--bundle", type=Path, help="installed release archive, needed only if it is not already retained")
    operator_join = commands.add_parser(
        "operator-join", help="create independent deployment state from an authorized handoff and your own credentials"
    )
    operator_join.add_argument("--handoff", required=True, type=Path)
    operator_join.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_join.add_argument("--token-file", required=True, type=Path, help="your own same-Team DigitalOcean token file")
    operator_join.add_argument("--identity", required=True, type=Path, help="private key matching the authorized public key")
    operator_join.add_argument("--confirm-handoff-sha256", required=True, help="SHA-256 received from the existing operator")
    operator_doctor = commands.add_parser(
        "operator-doctor", help="verify retained deployment state, SSH access, installed release, and HTTPS; close SSH afterward"
    )
    operator_doctor.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_sync = commands.add_parser(
        "operator-sync", help="verify and retain the live installed release using your independent SSH key and cloud token"
    )
    operator_sync.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_sync.add_argument("--require-no-pending-upgrade", action="store_true",
                              help="refuse before a source build if this operator owns an unfinished upgrade")
    operator_resume = commands.add_parser(
        "operator-resume-upgrade", help="resume an exact locally owned pending upgrade without rebuilding"
    )
    operator_resume.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_session = commands.add_parser(
        "operator-session", help="prepare command-scoped local SSH authentication before running a command"
    )
    operator_session.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_session.add_argument("arguments", nargs=argparse.REMAINDER, help="local command after --")
    operator_kit = commands.add_parser(
        "operator-kit", help="authorize and create an encrypted offline operator recovery kit"
    )
    operator_kit.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_kit.add_argument("--output", required=True, type=Path)
    operator_recover = commands.add_parser(
        "operator-recover", help="reconstruct independent operator state from an encrypted recovery kit"
    )
    operator_recover.add_argument("--kit", required=True, type=Path)
    operator_recover.add_argument("--state-dir", type=Path, default=Path.home() / ".reasoning-deploy")
    operator_recover.add_argument("--token-file", required=True, type=Path, help="fresh token from the deployment's DigitalOcean Team")
    bundle = commands.add_parser("bundle", help="build a verified public release from source and the built frontend")
    bundle.add_argument("--source-root", type=Path, default=Path.cwd())
    bundle.add_argument("--wheel", type=Path, help="read a retained legacy wheel instead of bundling source")
    bundle.add_argument(
        "--compute-catalog", type=Path, help="override the published images/compute-images.json catalog with explicit maintainer approvals"
    )
    bundle.add_argument("--frontend", required=True, type=Path)
    bundle.add_argument("--output", required=True, type=Path)
    bundle.add_argument("--allow-dirty", action="store_true", help="explicitly label a development release from uncommitted source")
    inspect = commands.add_parser("inspect", help="verify an existing release without installing it")
    inspect.add_argument("--bundle", required=True, type=Path)
    for name, help_text in (
        ("doctor", "inspect one exact managed server over verified SSH without changing it"),
        ("upgrade-plan", "check installed source and target releases without upgrading or stopping services"),
    ):
        operation = commands.add_parser(name, help=help_text)
        operation.add_argument("--bundle", required=True, type=Path, help="original installed release archive")
        operation.add_argument("--confirm-release", required=True, help="exact original installed release ID")
        operation.add_argument("--public-url", required=True)
        operation.add_argument("--ssh-host", required=True)
        operation.add_argument("--identity", required=True, type=Path)
        operation.add_argument("--known-hosts", required=True, type=Path)
        if name == "upgrade-plan":
            operation.add_argument("--target-bundle", required=True, type=Path)
            operation.add_argument("--confirm-target-release", required=True)
            operation.add_argument("--output", type=Path, help="create an immutable private plan file for subsequent release changes")
    for name in ("upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"):
        operation = commands.add_parser(name, help="explicit managed release change on a disposable development host over verified SSH")
        operation.add_argument("--plan", required=True, type=Path, help="original private saved upgrade-plan document")
        operation.add_argument("--confirm-plan", required=True)
        operation.add_argument(
            "--operation-id", required=True, help="fresh 64-hex ID for preparation; reuse it for every retry/activation/rollback"
        )
        operation.add_argument("--state-dir", required=True, type=Path, help="original private operator operation directory")
        operation.add_argument("--ssh-host", required=True)
        operation.add_argument("--identity", required=True, type=Path)
        operation.add_argument("--known-hosts", required=True, type=Path)
        if name == "upgrade-prepare":
            operation.add_argument("--source-bundle", required=True, type=Path)
            operation.add_argument("--target-bundle", required=True, type=Path)
            operation.add_argument("--maximum-bytes", type=int)
        if name.startswith("rollback"):
            operation.add_argument(
                "--rollback-id", required=True, help="fresh 64-hex ID for rollback preparation; reuse for source activation"
            )
        if name.endswith("prepare"):
            operation.add_argument("--confirm-workers-fenced", required=True, action="store_true")
        else:
            operation.add_argument("--confirm-activation", required=True, action="store_true")
        if name == "rollback-prepare":
            operation.add_argument("--confirm-newer-data-loss", required=True, action="store_true")
    recovery_plan = commands.add_parser(
        "recovery-plan", help="bind a matching backup/release for replacement-host recovery without host access"
    )
    recovery_plan.add_argument("--bundle", required=True, type=Path)
    recovery_plan.add_argument("--confirm-release", required=True)
    recovery_plan.add_argument("--backup", required=True, type=Path)
    recovery_plan.add_argument("--expected-backup-sha256", required=True)
    recovery_plan.add_argument("--key-file", required=True, type=Path)
    recovery_plan.add_argument("--public-url", required=True)
    recovery_plan.add_argument("--maximum-bytes", type=int, default=8 * 1024**3)
    recovery_plan.add_argument("--mail-file", type=Path)
    recovery_plan.add_argument(
        "--provisioning-file", type=Path, help="original canonical request for the owned replacement VM, not the source VM"
    )
    recovery_plan.add_argument("--output", required=True, type=Path, help="exclusively create the private saved recovery input plan")
    for name in ("recovery-prepare", "recovery-activate"):
        operation = commands.add_parser(name, help="separately confirmed replacement-host recovery over verified root SSH")
        operation.add_argument("--plan", required=True, type=Path)
        operation.add_argument("--confirm-recovery", required=True, help="exact saved plan input_sha256")
        operation.add_argument("--recovery-id", required=True, help="fresh 64-hex ID for preparation; retain for all retries/activation")
        operation.add_argument("--state-dir", required=True, type=Path)
        operation.add_argument("--ssh-host", required=True)
        operation.add_argument("--identity", required=True, type=Path)
        operation.add_argument("--known-hosts", required=True, type=Path)
        if name == "recovery-prepare":
            operation.add_argument("--bundle", required=True, type=Path)
            operation.add_argument("--backup", required=True, type=Path)
            operation.add_argument("--key-file", required=True, type=Path)
            operation.add_argument("--mail-file", type=Path)
            operation.add_argument("--confirm-execution-fenced", required=True, action="store_true")
        else:
            operation.add_argument("--confirm-activation", required=True, action="store_true")
    install = commands.add_parser("install", help="install a verified release on a dedicated Ubuntu 24.04 host over verified SSH")
    install.add_argument("--bundle", required=True, type=Path)
    install.add_argument("--public-url", required=True)
    install.add_argument("--ssh-host", required=True)
    install.add_argument("--identity", required=True, type=Path)
    install.add_argument("--known-hosts", required=True, type=Path)
    install.add_argument("--confirm-release", required=True, help="release ID from inspect; acknowledges root host changes")
    install.add_argument("--owner-file", type=Path, help="private immutable initial-owner input from owner-input; creates the organization")
    install.add_argument("--mail-file", type=Path, help="private immutable Resend configuration from mail-input")
    mail_input = commands.add_parser(
        "mail-input", help="prepare private invitation email inputs using a hidden key prompt; no cloud changes"
    )
    mail_input.add_argument("--sender", required=True)
    mail_input.add_argument("--daily-limit", type=int, default=100)
    mail_input.add_argument("--output", required=True, type=Path)
    owner_input = commands.add_parser(
        "owner-input", help="prepare private initial-owner inputs using password prompts; no server/cloud changes"
    )
    owner_input.add_argument("--email", required=True)
    owner_input.add_argument("--name", required=True)
    owner_input.add_argument("--organization", required=True)
    owner_input.add_argument("--output", required=True, type=Path)
    enroll = commands.add_parser("worker-enroll", help="recoverably enroll one worker with a scoped credential; no cloud resources created")
    enroll.add_argument("--server-url", required=True)
    enroll.add_argument("--organization-id", required=True)
    enroll.add_argument("--scaler-token-file", required=True, type=Path, help="private organization-scoped scaler credential")
    enroll.add_argument("--name", required=True)
    enroll.add_argument("--image-id", action="append", required=True)
    enroll.add_argument("--resources-file", required=True, type=Path, help="private JSON object containing all six resource limits")
    enroll.add_argument("--state-dir", required=True, type=Path, help="separate private original enrollment intent; preserve it")
    enroll.add_argument("--registration-output", required=True, type=Path)
    enroll.add_argument("--catalog-output", required=True, type=Path)
    for name, help_text in (
        ("install-worker", "bootstrap a separate Ubuntu 24.04 Docker worker using verified SSH"),
        ("retire-worker", "drain, stop, and irreversibly retire an installed worker over verified SSH; does not delete its VM"),
    ):
        worker = commands.add_parser(name, help=help_text)
        worker.add_argument("--bundle", required=True, type=Path)
        worker.add_argument("--server-url", required=True)
        worker.add_argument("--registration", required=True, type=Path, help="private one-time enrollment JSON")
        worker.add_argument("--catalog", required=True, type=Path, help="private copy of the operator-approved immutable image catalog")
        worker.add_argument("--ssh-host", required=True)
        worker.add_argument("--identity", required=True, type=Path)
        worker.add_argument("--known-hosts", required=True, type=Path)
        worker.add_argument("--confirm-release", required=True)
        worker.add_argument("--confirm-worker", required=True, help="exact worker ID; acknowledges the requested root host changes")
    dns_setup = commands.add_parser("do-dns-setup", help="create or reuse the confirmed DNS zone without changing existing records")
    dns_setup.add_argument("--token-file", required=True, type=Path)
    dns_setup.add_argument("--zone", required=True)
    dns_setup.add_argument("--state-dir", required=True, type=Path)
    dns_setup.add_argument("--confirm-zone", required=True)
    dns_check = commands.add_parser("do-dns-check", help="check public NS resolution; no provider token or writes")
    dns_check.add_argument("--zone", required=True)
    plan = commands.add_parser("do-plan", help="read live DigitalOcean availability/prices without creating resources")
    plan.add_argument("--token-file", required=True, type=Path, help="private token file; token is never placed in shell arguments")
    plan.add_argument("--name", required=True)
    plan.add_argument("--region", required=True)
    plan.add_argument("--size", required=True)
    plan.add_argument("--public-url", required=True)
    plan.add_argument("--dns-zone", required=True)
    plan.add_argument("--ssh-key-id", required=True, type=int)
    plan.add_argument("--admin-cidr", required=True)
    plan.add_argument("--maximum-hourly-usd", required=True)
    plan.add_argument("--output", type=Path, help="save a private immutable plan for confirmed do-apply; never overwrite a plan")
    plan.add_argument(
        "--pin-ssh-host-key", action="store_true", help="consent to placing a unique pinned host private key in provider user data"
    )
    apply = commands.add_parser("do-apply", help="create/resume a billable Droplet, scoped firewall, and unused DNS record")
    apply.add_argument("--token-file", required=True, type=Path)
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--state-dir", required=True, type=Path, help="private resumable resource ownership journal; preserve it")
    apply.add_argument("--confirm-plan", required=True, help="exact saved plan ID; acknowledges ongoing provider charges")
    apply.add_argument("--wait-seconds", type=int, default=60, help="bounded provider readiness wait, 0–60 seconds; default 60")
    ssh_access = commands.add_parser("do-ssh-access", help="open or close ownership-checked temporary SSH firewall access")
    ssh_access.add_argument("--token-file", required=True, type=Path)
    ssh_access.add_argument("--plan", required=True, type=Path)
    ssh_access.add_argument("--state-dir", required=True, type=Path)
    ssh_access.add_argument("--confirm-plan", required=True)
    access_action = ssh_access.add_mutually_exclusive_group(required=True)
    access_action.add_argument("--open-cidr", help="one current public IPv4 /32 to allow temporarily")
    access_action.add_argument("--close", action="store_true", help="remove inbound SSH access")
    ssh_access.add_argument("--wait-seconds", type=int, default=60, help="bounded firewall readiness wait, 0–60 seconds")
    for name, help_text in (
        ("do-install", "install the original owned server VM and verify HTTPS; no cloud resources created or deleted"),
        ("do-launch", "create/resume a confirmed server plan, install the exact release, and verify HTTPS; incurs provider charges"),
    ):
        server_install = commands.add_parser(name, help=help_text)
        for flag in ("token-file", "plan", "state-dir", "bundle", "identity"):
            server_install.add_argument("--" + flag, required=True, type=Path)
        server_install.add_argument("--known-hosts", type=Path, help="required unless the original plan pins its SSH host key")
        server_install.add_argument(
            "--owner-file", type=Path, help="private immutable initial-owner input; no secrets in command arguments"
        )
        server_install.add_argument("--mail-file", type=Path, help="private immutable mail configuration; no secrets in command arguments")
        for flag in ("confirm-plan", "confirm-release"):
            server_install.add_argument("--" + flag, required=True)
        if name == "do-install":
            server_install.add_argument("--confirm-droplet", required=True, type=int)
        else:
            server_install.add_argument("--wait-seconds", type=int, default=60, help="bounded provider readiness wait, 0–60 seconds")
            server_install.add_argument("--ssh-cidr", help="current public IPv4 /32 for this launch session")
    worker_plan = commands.add_parser(
        "do-worker-plan", help="read live prices for an enrollment-bound separate worker; no resources created"
    )
    worker_apply = commands.add_parser("do-worker-apply", help="create/resume a confirmed worker Droplet and SSH-only scoped firewall")
    for command in (worker_plan, worker_apply):
        command.add_argument("--token-file", required=True, type=Path)
        command.add_argument("--server-url", required=True)
        command.add_argument("--registration", required=True, type=Path, help="private one-time worker enrollment JSON")
        command.add_argument("--catalog", required=True, type=Path, help="private operator-approved immutable image catalog")
    worker_plan.add_argument("--name", required=True)
    worker_plan.add_argument("--region", required=True)
    worker_plan.add_argument("--size", required=True)
    worker_plan.add_argument("--ssh-key-id", required=True, type=int)
    worker_plan.add_argument("--admin-cidr", required=True)
    worker_plan.add_argument("--maximum-hourly-usd", required=True)
    worker_plan.add_argument(
        "--pin-ssh-host-key",
        action="store_true",
        help="initialize a unique host key through provider user data; trusts provider/VM metadata, avoids manual host-key verification",
    )
    worker_plan.add_argument("--output", type=Path, help="save a private immutable worker plan; never overwrite an existing plan")
    worker_apply.add_argument("--plan", required=True, type=Path)
    worker_apply.add_argument("--state-dir", required=True, type=Path)
    worker_apply.add_argument("--confirm-plan", required=True, help="exact worker plan ID; acknowledges ongoing provider charges")
    worker_apply.add_argument("--wait-seconds", type=int, default=60, help="bounded provider readiness wait, 0–60 seconds")
    worker_install = commands.add_parser(
        "do-worker-install", help="install an exact owned worker VM over verified SSH; no cloud resources created or deleted"
    )
    for flag in ("token-file", "plan", "state-dir", "bundle", "registration", "catalog", "identity"):
        worker_install.add_argument("--" + flag, required=True, type=Path)
    worker_install.add_argument(
        "--known-hosts", type=Path, help="required for manually verified plans; pinned plans use their original key"
    )
    worker_install.add_argument("--server-url", required=True)
    for flag in ("confirm-plan", "confirm-worker", "confirm-release"):
        worker_install.add_argument("--" + flag, required=True)
    worker_install.add_argument("--confirm-droplet", required=True, type=int, help="exact VM ID from the original ownership journal")
    worker_retire = commands.add_parser(
        "do-worker-retire", help="retire an exact owned worker and destroy its VM/local data, firewall, and unused tag"
    )
    for flag in ("token-file", "plan", "state-dir", "bundle", "registration", "catalog", "identity"):
        worker_retire.add_argument("--" + flag, required=True, type=Path)
    worker_retire.add_argument("--known-hosts", type=Path, help="required for manually verified plans; pinned plans use their original key")
    worker_retire.add_argument("--server-url", required=True)
    for flag in ("confirm-plan", "confirm-worker", "confirm-release"):
        worker_retire.add_argument("--" + flag, required=True)
    worker_retire.add_argument("--confirm-droplet", required=True, type=int, help="exact VM ID from the original ownership journal")
    worker_retire.add_argument(
        "--confirm-delete-host-data", required=True, action="store_true", help="acknowledge irreversible destruction of all VM-local data"
    )
    worker_retire.add_argument(
        "--retry-delete",
        choices=("droplet", "firewall", "tag"),
        help="explicitly retry only this pending deletion; VM retry repeats fresh host checks",
    )
    fleet_command = commands.add_parser(
        "do-worker-fleet", help="save a private bounded fleet from original enrolled worker plans; no cloud changes"
    )
    fleet_command.add_argument("--server-url", required=True)
    fleet_command.add_argument("--organization-id", required=True)
    fleet_command.add_argument("--minimum-workers", type=int, default=0)
    fleet_command.add_argument("--maximum-workers", required=True, type=int)
    fleet_command.add_argument("--maximum-hourly-usd", required=True)
    fleet_command.add_argument("--idle-seconds", type=int, default=300)
    fleet_command.add_argument("--slot", action="append", nargs=4, metavar=("PLAN", "REGISTRATION", "CATALOG", "STATE_DIR"), default=[])
    fleet_command.add_argument("--output", required=True, type=Path)
    scale = commands.add_parser(
        "do-worker-scale-plan", help="read queue and owned worker capacity/costs; recommend actions without executing them"
    )
    scale.add_argument("--fleet", required=True, type=Path, help="private bounded fleet configuration with original per-worker inputs")
    scale.add_argument(
        "--session-file", required=True, type=Path, help="private scoped scaler credential or current owner/admin bearer session token"
    )
    scale.add_argument("--token-file", required=True, type=Path, help="private read-only DigitalOcean token")
    scale.add_argument("--output", type=Path, help="save a private expiring advisory decision; never overwrite an existing file")
    fleet_reconcile = commands.add_parser(
        "do-worker-fleet-reconcile", help="bind local fleet journals and reconcile durable VM budget reservations; no cloud writes"
    )
    fleet_amend = commands.add_parser(
        "do-worker-fleet-amend", help="activate a confirmed next fleet without resetting reservations; no cloud writes"
    )
    fleet_amend.add_argument("--next-fleet", required=True, type=Path, help="separate saved next fleet; preserve the original")
    fleet_amend.add_argument("--confirm-fleet", required=True, help="exact current fleet SHA-256")
    fleet_amend.add_argument("--confirm-next-fleet", required=True, help="exact next fleet SHA-256; acknowledges its new limits")
    scale_apply = commands.add_parser(
        "do-worker-scale-apply", help="reserve a fleet budget and create/resume one exactly confirmed worker; ongoing charges"
    )
    fleet_cancel = commands.add_parser(
        "do-worker-fleet-cancel",
        help="irreversibly cancel an exact reserved slot before any paid VM request; deletes only owned free resources",
    )
    for command in (fleet_reconcile, scale_apply, fleet_cancel, fleet_amend):
        command.add_argument("--fleet", required=True, type=Path)
        command.add_argument("--fleet-state-dir", required=True, type=Path, help="original private coordinator directory; preserve it")
        command.add_argument("--token-file", required=True, type=Path)
    scale_apply.add_argument(
        "--session-file", required=True, type=Path, help="private scoped scaler credential or current owner/admin bearer session token"
    )
    selection = scale_apply.add_mutually_exclusive_group(required=True)
    selection.add_argument("--decision", type=Path, help="private fresh provision recommendation; rechecked before execution")
    selection.add_argument("--resume-reservation", help="exact original durable reservation ID; never starts a replacement VM")
    scale_apply.add_argument("--confirm-decision", help="exact advisory decision ID, required with --decision")
    scale_apply.add_argument("--confirm-worker", required=True)
    scale_apply.add_argument("--confirm-plan", required=True, help="exact original worker plan ID; acknowledges ongoing provider charges")
    for flag in ("confirm-worker", "confirm-plan", "confirm-reservation"):
        fleet_cancel.add_argument("--" + flag, required=True)
    fleet_cancel.add_argument(
        "--retry-delete", choices=("firewall", "tag"), help="explicitly retry only this pending free-resource deletion"
    )
    controller_plan = commands.add_parser(
        "do-worker-controller-plan", help="save a bounded worker-lifecycle policy; no provider access or cloud writes"
    )
    for flag in ("fleet", "fleet-state-dir", "bundle", "identity", "output"):
        controller_plan.add_argument("--" + flag, required=True, type=Path)
    controller_plan.add_argument("--known-hosts", type=Path, help="omit only when every prepared worker plan pins its SSH host key")
    controller_plan.add_argument("--allow-provision", action="store_true", help="permit paid growth within this original fleet's ceilings")
    controller_plan.add_argument("--allow-retirement", action="store_true", help="permit idle retirement and unpaid setup cleanup")
    controller_plan.add_argument(
        "--confirm-delete-host-data", action="store_true", help="required with retirement: destroy all retired VM-local data"
    )
    controller_run = commands.add_parser(
        "do-worker-controller-run", help="execute confirmed queue-aware worker cycles; authorized creation/destruction may incur charges"
    )
    for flag in ("policy", "token-file", "session-file"):
        controller_run.add_argument("--" + flag, required=True, type=Path)
    controller_run.add_argument("--confirm-controller", required=True, help="exact saved policy SHA-256; authorizes its lifecycle actions")
    controller_run.add_argument("--replace-controller", help="exact previous controller hash; retains the original budget and release")
    controller_run.add_argument("--cycles", type=int, default=1, help="one to 10000 cycles; default one; stop on any error")
    controller_run.add_argument("--interval-seconds", type=int, default=15, help="10–60 seconds between cycles; default 15")
    pool_plan = commands.add_parser("do-worker-pool-plan", help="authorize bounded fresh worker generations under an original fleet budget")
    for flag in ("controller-policy", "pool-state-dir", "output"):
        pool_plan.add_argument("--" + flag, required=True, type=Path)
    pool_plan.add_argument(
        "--template-worker", action="append", required=True, help="original worker ID whose images/resources/DO shape are authorized"
    )
    pool_plan.add_argument("--scaler-id", required=True, help="original scoped enrollment grant ID; rotation retains this namespace")
    pool_plan.add_argument("--maximum-new-workers", required=True, type=int, help="1–64 fresh enrollments over this policy's lifetime")
    pool_plan.add_argument(
        "--pin-ssh-host-key", required=True, action="store_true", help="consent to provider user-data SSH identities for new hosts"
    )
    pool_run = commands.add_parser(
        "do-worker-pool-run", help="run original worker lifecycles and recoverably replenish authorized templates"
    )
    for flag in ("policy", "token-file", "scaler-token-file"):
        pool_run.add_argument("--" + flag, required=True, type=Path)
    pool_run.add_argument(
        "--confirm-pool", required=True, help="exact pool policy hash; acknowledges enrollment, paid growth, and its lifecycle permissions"
    )
    pool_run.add_argument("--cycles", type=int, default=1)
    pool_run.add_argument("--interval-seconds", type=int, default=15, help="10–60 seconds; default 15")
    args = parser.parse_args(argv)
    provider = None
    scaler_client = None
    try:
        if args.command == "operator-session":
            from .operator_session import run_session

            return run_session(args.state_dir, args.arguments)
        elif args.command == "compute-setup":
            from .compute_setup import run_compute_setup

            if not sys.stdin.isatty():
                raise InstallError("run compute setup in your terminal; paid-compute consent and secrets belong in the CLI")
            try:
                result = run_compute_setup(args.state_dir, lambda arguments: main(arguments, raise_errors=True))
            except (EOFError, KeyboardInterrupt):
                raise InstallError("compute setup stopped; preserve its private directory and resume the same command") from None
        elif args.command == "email-setup":
            from .mail_setup import run_mail_setup

            if not sys.stdin.isatty():
                raise InstallError("run email setup in your terminal; sending credentials and consent belong in the CLI")
            try:
                result = run_mail_setup(args.state_dir, lambda arguments: main(arguments, raise_errors=True))
            except (EOFError, KeyboardInterrupt):
                raise InstallError("email setup stopped; preserve its private directory and resume make email-setup") from None
        elif args.command == "setup":
            from .setup import run_setup

            if not sys.stdin.isatty():
                raise InstallError("run setup in your own interactive terminal; approvals and secrets are not collected through chat")
            try:
                result = run_setup(
                    args.bundle,
                    args.state_dir,
                    lambda arguments: main(arguments, raise_errors=True),
                    compute_backend=args.compute_backend,
                )
            except (EOFError, KeyboardInterrupt):
                raise InstallError("interactive setup stopped; preserve the state directory and resume the same command") from None
        elif args.command == "upgrade-stage":
            from .release_state import stage_current

            result = stage_current(args.bundle, args.state_dir)
        elif args.command == "upgrade":
            from .setup_upgrade import run_upgrade

            if not sys.stdin.isatty():
                raise InstallError("run upgrade in your own interactive terminal; approvals are not collected through chat")
            try:
                result = run_upgrade(args.bundle, args.state_dir, lambda arguments: main(arguments, raise_errors=True))
            except (EOFError, KeyboardInterrupt):
                raise InstallError("interactive upgrade stopped; preserve its state directory and resume the same command") from None
        elif args.command == "onboard":
            from .onboard import run_onboard

            if not sys.stdin.isatty():
                raise InstallError("run onboard in your own interactive terminal; approvals and secrets are not collected through chat")
            try:
                result = run_onboard(args.state_dir, args.onboard_dir, lambda arguments: main(arguments, raise_errors=True),
                                     replace_token=args.replace_token)
            except (EOFError, KeyboardInterrupt):
                raise InstallError("onboarding stopped; preserve both private directories "
                                   "and resume the same make onboard command") from None
        elif args.command == "operator-authorize":
            from .operator_handoff import grant_operator

            if not sys.stdin.isatty():
                raise InstallError("authorize a deployment operator in your own interactive terminal")
            try:
                result = grant_operator(
                    args.state_dir,
                    args.public_key,
                    args.output,
                    lambda arguments: main(arguments, raise_errors=True),
                    source_bundle=args.bundle,
                )
            except (EOFError, KeyboardInterrupt):
                raise InstallError("operator authorization stopped; retry with the same state and public key") from None
        elif args.command == "operator-join":
            from .operator_handoff import join_operator

            result = join_operator(args.handoff, args.state_dir, args.token_file, args.identity, args.confirm_handoff_sha256)
        elif args.command == "operator-doctor":
            from .operator_doctor import verify_operator

            result = verify_operator(args.state_dir, lambda arguments: main(arguments, raise_errors=True))
        elif args.command == "operator-sync":
            from .operator_sync import synchronize_operator

            result = synchronize_operator(args.state_dir, lambda arguments: main(arguments, raise_errors=True),
                                          require_no_pending_upgrade=args.require_no_pending_upgrade)
        elif args.command == "operator-resume-upgrade":
            from .operator_sync import resume_operator_upgrade

            result = resume_operator_upgrade(args.state_dir, lambda arguments: main(arguments, raise_errors=True))
        elif args.command == "operator-kit":
            from .operator_recovery import create_operator_recovery

            if not sys.stdin.isatty():
                raise InstallError("create an operator recovery kit in your own interactive terminal")
            try:
                result = create_operator_recovery(
                    args.state_dir,
                    args.output,
                    lambda arguments: main(arguments, raise_errors=True),
                )
            except (EOFError, KeyboardInterrupt):
                raise InstallError("operator recovery-kit creation stopped; preserve the deployment state") from None
        elif args.command == "operator-recover":
            from .operator_recovery import recover_operator

            if not sys.stdin.isatty():
                raise InstallError("recover deployment-operator state in your own interactive terminal")
            try:
                code = getpass.getpass("Operator recovery code: ").strip()
                result = recover_operator(args.kit, args.state_dir, args.token_file, code)
            except (EOFError, KeyboardInterrupt):
                raise InstallError("deployment-operator recovery stopped; preserve the recovery kit") from None
        elif args.command == "bundle":
            result = build_bundle(
                args.source_root, args.wheel, args.frontend, args.output, allow_dirty=args.allow_dirty, compute_catalog=args.compute_catalog
            )
        elif args.command == "inspect":
            manifest, release, checksum = inspect_bundle(args.bundle)
            result = {
                "release_id": release,
                "bundle_sha256": checksum,
                "source_revision": manifest["source_revision"],
                "dirty": manifest["dirty"],
                "files": len(manifest["files"]),
                "database_schema": manifest["database_schema"],
                "installation_features": manifest.get("installation_features", []),
            }
        elif args.command in {"doctor", "upgrade-plan"}:
            from .server_diagnostics import diagnose_remote

            result = diagnose_remote(
                args.bundle,
                args.public_url,
                args.ssh_host,
                args.identity,
                args.known_hosts,
                confirmed_release=args.confirm_release,
                **(
                    {"target_bundle": args.target_bundle, "confirmed_target": args.confirm_target_release}
                    if args.command == "upgrade-plan"
                    else {}
                ),
            )
            if args.command == "upgrade-plan" and args.output is not None:
                write_document(args.output, result, new=True)
        elif args.command in {"upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"}:
            from .server_changes import change_remote

            result = change_remote(
                args.command,
                read_document(args.plan),
                args.ssh_host,
                args.identity,
                args.known_hosts,
                args.state_dir,
                operation_id=args.operation_id,
                confirmed_plan=args.confirm_plan,
                source_bundle=getattr(args, "source_bundle", None),
                target_bundle=getattr(args, "target_bundle", None),
                maximum=getattr(args, "maximum_bytes", None),
                workers_fenced=getattr(args, "confirm_workers_fenced", False),
                activation_confirmed=getattr(args, "confirm_activation", False),
                rollback_id=getattr(args, "rollback_id", None),
                data_loss_confirmed=getattr(args, "confirm_newer_data_loss", False),
            )
        elif args.command == "recovery-plan":
            from .recovery_changes import plan_recovery

            result = plan_recovery(
                args.bundle,
                args.backup,
                args.key_file,
                args.public_url,
                confirmed_release=args.confirm_release,
                expected_backup=args.expected_backup_sha256,
                maximum=args.maximum_bytes,
                mail_file=args.mail_file,
                provisioning_file=args.provisioning_file,
            )
            write_document(args.output, result, new=True)
        elif args.command in {"recovery-prepare", "recovery-activate"}:
            from .recovery_changes import recover_remote

            result = recover_remote(
                args.command,
                read_document(args.plan),
                args.ssh_host,
                args.identity,
                args.known_hosts,
                args.state_dir,
                recovery_id=args.recovery_id,
                confirmed_recovery=args.confirm_recovery,
                bundle=getattr(args, "bundle", None),
                backup=getattr(args, "backup", None),
                key_file=getattr(args, "key_file", None),
                mail_file=getattr(args, "mail_file", None),
                execution_fenced=getattr(args, "confirm_execution_fenced", False),
                activation_confirmed=getattr(args, "confirm_activation", False),
            )
        elif args.command == "do-worker-pool-plan":
            from .fleet import digest
            from .pool import plan_pool

            policy = plan_pool(
                read_document(args.controller_policy),
                args.pool_state_dir,
                args.template_worker,
                args.scaler_id,
                args.maximum_new_workers,
                pin_ssh_host_key=args.pin_ssh_host_key,
            )
            write_document(args.output, policy, new=True)
            result = {
                "policy_file": str(args.output.absolute()),
                "pool_sha256": digest(policy),
                "policy": policy,
                "creates_cloud_resources": False,
                "enrolls_workers": False,
            }
        elif args.command == "do-worker-pool-run":
            from .enrollment import ScalerClient
            from .fleet import digest
            from .pool import WorkerPool, validate_policy
            from .private import write_private

            policy = validate_policy(read_document(args.policy))
            if args.confirm_pool != digest(policy) or not 1 <= args.cycles <= 10000 or not 10 <= args.interval_seconds <= 60:
                raise InstallError("confirm the exact pool policy and bounded cycles/interval before provider access")
            initial = policy["initial_policy"]
            credential = read_private(args.scaler_token_file, 4096).decode().strip()
            with bundle_snapshot(Path(initial["bundle_path"]), initial["release_id"]) as snapshot:
                pool = WorkerPool(None, policy, bundle=snapshot)
                scaler_client = ScalerClient(pool.initial.fleet["server_url"], credential)
                provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
                pool.provider = provider

                def remote(profile, address, request, *, retirement=False):
                    with protected_temporary_directory("reasoning-pool-worker-") as temporary:
                        registration, catalog = temporary / "enrollment.json", temporary / "images.json"
                        write_private(registration, canonical(profile.config))
                        write_private(catalog, canonical(profile.images))
                        return owned_worker_remote(
                            profile.document,
                            Path(profile.fleet_paths["state_dir"]),
                            snapshot,
                            profile.config["server_url"],
                            registration,
                            catalog,
                            address,
                            Path(initial["identity"]),
                            Path(initial["known_hosts"]) if initial["known_hosts"] is not None else None,
                            confirmed_release=initial["release_id"],
                            confirmed_worker=profile.config["id"],
                            operation="prepare-destroy" if retirement else "install",
                            destruction=request if retirement else None,
                            provisioning=None if retirement else request,
                        )

                for index in range(args.cycles):
                    result = pool.cycle(
                        credential,
                        confirm_pool=args.confirm_pool,
                        client=scaler_client,
                        install_host=remote,
                        retire_host=lambda profile, address, request: remote(profile, address, request, retirement=True),
                    )
                    print(json.dumps(result, sort_keys=True), flush=True)
                    if index + 1 < args.cycles:
                        time.sleep(args.interval_seconds)
                return 0
        elif args.command == "do-worker-controller-plan":
            from .controller import plan_controller
            from .fleet import digest

            policy = plan_controller(
                args.fleet,
                args.fleet_state_dir,
                args.bundle,
                args.identity,
                args.known_hosts,
                allow_provision=args.allow_provision,
                allow_retirement=args.allow_retirement,
                delete_host_data=args.confirm_delete_host_data,
            )
            write_document(args.output, policy, new=True)
            result = {
                "policy_file": str(args.output.absolute()),
                "policy_sha256": digest(policy),
                "policy": policy,
                "creates_resources": False,
                "destroys_resources": False,
            }
        elif args.command == "do-worker-controller-run":
            from .controller import WorkerController, validate_policy
            from .fleet import digest, hash_id
            from .private import write_private

            policy = validate_policy(read_document(args.policy))
            if (
                args.confirm_controller != digest(policy)
                or args.replace_controller is not None
                and not hash_id(args.replace_controller)
                or not 1 <= args.cycles <= 10000
                or not 10 <= args.interval_seconds <= 60
            ):
                raise InstallError("confirm the exact controller policy and bounded cycles/interval before provider access")
            session = read_private(args.session_file, 4096).decode().strip()
            with bundle_snapshot(Path(policy["bundle_path"]), policy["release_id"]) as snapshot:
                # Local pinned-input validation must precede provider access.
                controller = WorkerController(None, policy, bundle=snapshot)
                provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
                controller.provider = provider
                for profile in controller.profiles:
                    profile.provider = provider

                def remote(profile, address, request, *, retirement=False):
                    with protected_temporary_directory("reasoning-controller-worker-") as temporary:
                        registration, catalog = temporary / "enrollment.json", temporary / "images.json"
                        write_private(registration, canonical(profile.config))
                        write_private(catalog, canonical(profile.images))
                        return owned_worker_remote(
                            profile.document,
                            Path(profile.fleet_paths["state_dir"]),
                            snapshot,
                            profile.config["server_url"],
                            registration,
                            catalog,
                            address,
                            Path(policy["identity"]),
                            Path(policy["known_hosts"]) if policy["known_hosts"] is not None else None,
                            confirmed_release=policy["release_id"],
                            confirmed_worker=profile.config["id"],
                            operation="prepare-destroy" if retirement else "install",
                            destruction=request if retirement else None,
                            provisioning=None if retirement else request,
                        )

                for index in range(args.cycles):
                    result = controller.cycle(
                        session,
                        confirm_controller=args.confirm_controller,
                        replace_controller=args.replace_controller if index == 0 else None,
                        install_host=remote,
                        retire_host=lambda profile, address, request: remote(profile, address, request, retirement=True),
                    )
                    print(json.dumps(result, sort_keys=True), flush=True)
                    if index + 1 < args.cycles:
                        time.sleep(args.interval_seconds)
                return 0
        elif args.command == "worker-enroll":
            from .enrollment import ScalerClient, enroll_worker

            limits = decode_input(read_private(args.resources_file, 4096))
            credential = read_private(args.scaler_token_file, 4096).decode().strip()
            scaler_client = ScalerClient(args.server_url, credential)
            result = enroll_worker(
                scaler_client,
                args.state_dir,
                args.server_url,
                args.organization_id,
                args.name,
                args.image_id,
                limits,
                args.registration_output,
                args.catalog_output,
            )
        elif args.command == "mail-input":
            from reasoning_ledger.server.mail import MailConfig

            configuration = MailConfig(args.sender, getpass.getpass("Resend sending API key: "), args.daily_limit)
            write_document(args.output, configuration.document(), new=True)
            result = {"mail_file": str(args.output), "provider": "resend", "creates_cloud_resources": False, "sends_email": False}
        elif args.command == "owner-input":
            password = getpass.getpass("Initial owner password (at least 12 characters): ")
            if password != getpass.getpass("Confirm password: "):
                raise InstallError("passwords do not match")
            value = validated_owner(
                {
                    "format": 1,
                    "setup_id": secrets.token_hex(16),
                    "request_token": secrets.token_urlsafe(32),
                    "email": args.email.strip().lower(),
                    "name": args.name,
                    "organization_name": args.organization,
                    "password": password,
                }
            )
            write_document(args.output, value, new=True)
            result = {
                "owner_file": str(args.output),
                "setup_id": value["setup_id"],
                "creates_organization": False,
                "creates_cloud_resources": False,
            }
        elif args.command == "install":
            _, release, _ = inspect_bundle(args.bundle)
            if args.confirm_release != release:
                raise InstallError("confirm-release must exactly match this verified release ID")
            owner = read_owner(args.owner_file) if args.owner_file is not None else None
            mail = read_mail(args.mail_file) if args.mail_file is not None else None
            result = install_remote(
                args.bundle,
                args.public_url,
                args.ssh_host,
                args.identity,
                args.known_hosts,
                confirmed_release=args.confirm_release,
                owner=owner,
                mail=mail,
            )
        elif args.command in {"install-worker", "retire-worker"}:
            _, release, _ = inspect_bundle(args.bundle)
            if args.confirm_release != release:
                raise InstallError("confirm-release must exactly match the verified worker release ID")
            record = decode_input(read_private(args.registration, 65536))
            if not isinstance(record, dict) or record.get("id") != args.confirm_worker:
                raise InstallError("confirm-worker must exactly match the private enrollment worker ID")
            result = install_worker_remote(
                args.bundle,
                args.server_url,
                args.registration,
                args.catalog,
                args.ssh_host,
                args.identity,
                args.known_hosts,
                confirmed_release=args.confirm_release,
                confirmed_worker=args.confirm_worker,
                operation="retire" if args.command == "retire-worker" else "install",
            )
        elif args.command == "do-worker-fleet":
            from .scaling import fleet_inputs

            fleet = {
                "format": 1,
                "server_url": args.server_url,
                "organization_id": args.organization_id,
                "minimum_workers": args.minimum_workers,
                "maximum_workers": args.maximum_workers,
                "maximum_hourly_usd": args.maximum_hourly_usd,
                "idle_seconds": args.idle_seconds,
                "workers": [
                    {
                        key: str(Path(value).absolute())
                        for key, value in zip(("plan", "registration", "catalog", "state_dir"), entry, strict=True)
                    }
                    for entry in args.slot
                ],
            }
            _, profiles = fleet_inputs(fleet)
            write_document(args.output, fleet, new=True)
            result = {
                "fleet_file": str(args.output.absolute()),
                "fleet_sha256": hashlib.sha256(canonical(fleet)).hexdigest(),
                "organization_id": fleet["organization_id"],
                "worker_ids": [instance.config["id"] for instance in profiles],
                "creates_resources": False,
            }
        elif args.command == "do-worker-scale-plan":
            from .scaling import decide, inventory, load_fleet, read_snapshot

            fleet, profiles = load_fleet(args.fleet)
            session = read_private(args.session_file, 4096).decode().strip()
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            owned = inventory(provider, profiles)
            snapshot = read_snapshot(fleet["server_url"], fleet["organization_id"], session)
            result = decide(fleet, snapshot, owned)
            if args.output is not None:
                write_document(args.output, result, new=True)
        elif args.command in {"do-worker-fleet-reconcile", "do-worker-scale-apply", "do-worker-fleet-cancel", "do-worker-fleet-amend"}:
            from .fleet import FleetCoordinator, hash_id, selected_advice
            from .scaling import load_fleet

            # Local confirmation failures precede provider/session access and
            # any coordinator-directory or ownership-journal changes.
            if args.command == "do-worker-fleet-amend":
                from .fleet import digest

                current, _ = load_fleet(args.fleet)
                following, _ = load_fleet(args.next_fleet)
                if args.confirm_fleet != digest(current) or args.confirm_next_fleet != digest(following) or current == following:
                    raise InstallError("confirm exact distinct original and next fleet hashes before accessing the provider")
            if args.command in {"do-worker-scale-apply", "do-worker-fleet-cancel"}:
                fleet, profiles = load_fleet(args.fleet)
                if not any(p.config["id"] == args.confirm_worker and p.identity == args.confirm_plan for p in profiles):
                    raise InstallError("confirm exact original fleet worker and plan IDs")
            if args.command == "do-worker-fleet-cancel" and not hash_id(args.confirm_reservation):
                raise InstallError("cancel requires the exact original reservation ID")
            if args.command == "do-worker-scale-apply":
                advice = read_document(args.decision) if args.decision else None
                if advice is not None:
                    selected_advice(advice, fleet, args.confirm_worker, args.confirm_plan, args.confirm_decision)
                elif not hash_id(args.resume_reservation) or args.confirm_decision is not None:
                    raise InstallError("resume requires the exact original reservation ID, not a new decision")
                session = read_private(args.session_file, 4096).decode().strip()
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            coordinator = FleetCoordinator(provider, args.fleet, args.fleet_state_dir)
            if args.command == "do-worker-fleet-amend":
                from .fleet_amend import amend

                result = amend(coordinator, args.next_fleet, confirm_fleet=args.confirm_fleet, confirm_next_fleet=args.confirm_next_fleet)
            elif args.command == "do-worker-fleet-reconcile":
                result = coordinator.reconcile()
            elif args.command == "do-worker-fleet-cancel":
                result = coordinator.cancel(
                    confirm_worker=args.confirm_worker,
                    confirm_plan=args.confirm_plan,
                    confirm_reservation=args.confirm_reservation,
                    retry_delete=args.retry_delete,
                )
            else:
                result = coordinator.apply(
                    session,
                    confirm_worker=args.confirm_worker,
                    confirm_plan=args.confirm_plan,
                    advice=advice,
                    confirm_decision=args.confirm_decision,
                    resume_reservation=args.resume_reservation,
                )
        elif args.command == "do-worker-install":
            from .private import write_private
            from .worker_install import WorkerInstaller

            document = read_document(args.plan)
            if args.confirm_plan != worker_plan_identity(document):
                raise InstallError("confirm-plan must exactly match the saved worker plan ID")
            from .ssh_host import pinned

            if args.known_hosts is None and not pinned(document["plan"]):
                raise InstallError("this worker plan requires independently verified --known-hosts")
            config, images = worker_inputs(
                args.server_url, decode_input(read_private(args.registration, 65536)), decode_input(read_private(args.catalog, 65536))
            )
            if args.confirm_worker != config["id"] or not 0 < args.confirm_droplet < 2**63:
                raise InstallError("confirm the exact worker and owned Droplet before installation")
            # Pin private inputs too: a source file changed during provider
            # reads must not be installed on the original owned VM.
            with (
                bundle_snapshot(args.bundle, args.confirm_release) as snapshot,
                protected_temporary_directory("reasoning-owned-worker-") as temporary,
            ):
                registration, catalog = temporary / "enrollment.json", temporary / "images.json"
                write_private(registration, canonical(config))
                write_private(catalog, canonical(images))
                provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
                result = WorkerInstaller(provider, document, config, images, snapshot).install(
                    args.state_dir,
                    confirm_plan=args.confirm_plan,
                    confirm_worker=args.confirm_worker,
                    confirm_release=args.confirm_release,
                    confirm_droplet=args.confirm_droplet,
                    install_host=lambda address, request: owned_worker_remote(
                        document,
                        args.state_dir,
                        snapshot,
                        args.server_url,
                        registration,
                        catalog,
                        address,
                        args.identity,
                        args.known_hosts,
                        confirmed_release=args.confirm_release,
                        confirmed_worker=args.confirm_worker,
                        provisioning=request,
                    ),
                )
        elif args.command == "do-worker-retire":
            from .worker_retire import WorkerRetirer

            document = read_document(args.plan)
            if args.confirm_plan != worker_plan_identity(document):
                raise InstallError("confirm-plan must exactly match the saved worker plan ID")
            from .ssh_host import pinned

            if args.known_hosts is None and not pinned(document["plan"]):
                raise InstallError("this worker plan requires independently verified --known-hosts")
            config, images = worker_inputs(
                args.server_url, decode_input(read_private(args.registration, 65536)), decode_input(read_private(args.catalog, 65536))
            )
            _, release, _ = inspect_bundle(args.bundle)
            if args.confirm_worker != config["id"] or args.confirm_release != release:
                raise InstallError("confirm the exact installed worker and release before VM deletion")
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            result = WorkerRetirer(provider, document, config, images, args.bundle).retire(
                args.state_dir,
                confirm_plan=args.confirm_plan,
                confirm_worker=args.confirm_worker,
                confirm_release=args.confirm_release,
                confirm_droplet=args.confirm_droplet,
                confirm_delete_host_data=args.confirm_delete_host_data,
                retry_delete=args.retry_delete,
                prepare_host=lambda address, request: owned_worker_remote(
                    document,
                    args.state_dir,
                    args.bundle,
                    args.server_url,
                    args.registration,
                    args.catalog,
                    address,
                    args.identity,
                    args.known_hosts,
                    confirmed_release=args.confirm_release,
                    confirmed_worker=args.confirm_worker,
                    operation="prepare-destroy",
                    destruction=request,
                ),
            )
        elif args.command in {"do-install", "do-launch"}:
            from .server_install import ServerInstaller
            from .ssh_host import pinned

            document = read_document(args.plan)
            launching = args.command == "do-launch"
            if (
                args.confirm_plan != plan_identity(document)
                or not launching
                and (
                    not isinstance(args.confirm_droplet, int)
                    or isinstance(args.confirm_droplet, bool)
                    or not 0 < args.confirm_droplet < 2**63
                )
            ):
                raise InstallError("confirm the exact server plan and owned Droplet before installation")
            if launching and (not pinned(document["plan"]) or not 0 <= args.wait_seconds <= 60):
                raise InstallError("do-launch requires an original pinned-SSH plan and a wait between zero and 60 seconds")
            if args.known_hosts is None and not pinned(document["plan"]):
                raise InstallError("this server plan requires independently verified --known-hosts")
            read_private(args.identity, 65536)
            if args.known_hosts is not None:
                ssh_arguments("127.0.0.1", args.identity, args.known_hosts)
            owner = read_owner(args.owner_file) if args.owner_file is not None else None
            mail = read_mail(args.mail_file) if args.mail_file is not None else None
            from .host import mail_binding, require_mail_release

            mail_expected = mail_binding(canonical(mail)) if mail is not None else None
            with bundle_snapshot(args.bundle, args.confirm_release) as snapshot:
                if mail is not None:
                    require_mail_release(inspect_bundle(snapshot)[0])
                provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
                installer = ServerInstaller(provider, document, snapshot, ssh_cidr=args.ssh_cidr if launching else None)
                if launching:
                    installer.prepare_launch(
                        args.state_dir, owner=owner_binding(canonical(owner)) if owner is not None else None, mail=mail_expected
                    )
                    result = provision_until_ready(
                        Provisioner(provider, document, ssh_cidr=args.ssh_cidr),
                        args.state_dir,
                        args.confirm_plan,
                        args.wait_seconds,
                    )
                    if result["phase"] != "ssh_key_pinned":
                        result["next_step"] = "resume the same do-launch command, plan, bundle, and ownership after provider readiness"
                        print(json.dumps(result, sort_keys=True))
                        return 0
                    droplet = result["resources"]["droplet"]
                else:
                    droplet = args.confirm_droplet
                result = installer.install(
                    args.state_dir,
                    confirm_plan=args.confirm_plan,
                    confirm_release=args.confirm_release,
                    confirm_droplet=droplet,
                    owner=owner_binding(canonical(owner)) if owner is not None else None,
                    mail=mail_expected,
                    install_host=lambda address, request: owned_server_remote(
                        document,
                        args.state_dir,
                        snapshot,
                        address,
                        args.identity,
                        args.known_hosts,
                        confirmed_release=args.confirm_release,
                        provisioning=request,
                        owner=owner,
                        mail=mail,
                    ),
                )
        elif args.command == "do-dns-setup":
            from .dns import setup_zone, zone_name

            zone_name(args.zone)
            if args.confirm_zone != args.zone:
                raise InstallError("confirm the exact DNS zone before provider access or creation")
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            result = setup_zone(provider, args.zone, args.state_dir, args.confirm_zone)
        elif args.command == "do-dns-check":
            from .dns import check_delegation

            result = check_delegation(args.zone)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["delegation_verified"] else 1
        elif args.command == "do-ssh-access":
            from .provision import update_ssh_access

            document = read_document(args.plan)
            if args.confirm_plan != plan_identity(document):
                raise InstallError("confirm-plan must exactly match the owned server plan before changing SSH access")
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            result = update_ssh_access(
                provider,
                document,
                args.state_dir,
                args.open_cidr if not args.close else None,
                wait_seconds=args.wait_seconds,
            )
        elif args.command == "do-apply":
            document = read_document(args.plan)
            if args.confirm_plan != plan_identity(document):
                raise InstallError("confirm-plan must exactly match the saved plan ID; creation incurs ongoing provider charges")
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            result = provision_until_ready(Provisioner(provider, document), args.state_dir, args.confirm_plan, args.wait_seconds)
        elif args.command in {"do-worker-plan", "do-worker-apply"}:
            if args.command == "do-worker-apply":
                document = read_document(args.plan)
                if args.confirm_plan != worker_plan_identity(document):
                    raise InstallError("confirm-plan must exactly match the saved worker plan ID; creation incurs ongoing provider charges")
            config, images = worker_inputs(
                args.server_url, decode_input(read_private(args.registration, 65536)), decode_input(read_private(args.catalog, 65536))
            )
            provider = DigitalOcean(read_private(args.token_file, 4096).decode().strip())
            if args.command == "do-worker-apply":
                result = provision_until_ready(
                    WorkerProvisioner(provider, document, config, images), args.state_dir, args.confirm_plan, args.wait_seconds
                )
            else:
                result = plan_worker(
                    provider,
                    config,
                    images,
                    pin_ssh_host_key=args.pin_ssh_host_key,
                    **{name: getattr(args, name) for name in ("name", "region", "size", "ssh_key_id", "admin_cidr", "maximum_hourly_usd")},
                )
                if args.output is not None:
                    result = save_plan(args.output, result, validator=validate_worker_plan)
        else:
            token = read_private(args.token_file, 4096).decode().strip()
            provider = DigitalOcean(token)
            result = provider.plan(
                name=args.name,
                region=args.region,
                size=args.size,
                origin=args.public_url,
                dns_zone=args.dns_zone,
                ssh_key_id=args.ssh_key_id,
                admin_cidr=args.admin_cidr,
                maximum_hourly_usd=args.maximum_hourly_usd,
                pin_ssh_host_key=args.pin_ssh_host_key,
            )
            if args.output is not None:
                result = save_plan(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, InstallError) else "deployment input, private state, or build prerequisites are invalid"
        if raise_errors:
            if isinstance(exc, InstallError):
                raise
            raise InstallError(message) from exc
        parser.exit(2, f"reasoning-deploy: {message}\n")
    finally:
        if scaler_client is not None:
            scaler_client.close()
        if provider is not None:
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
