"""Install credential-isolated Modal relays on an existing managed chat host."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path

from reasoning_ledger.server.compute_catalog import ComputeImage, canonical, fits
from reasoning_ledger.server.compute_modal import modal_profile
from reasoning_ledger.server.compute_worker import WorkerClient, WorkerFault, enrollment, verify_identity

from .host import ENVIRONMENT, MANAGED, HostInstaller, InstallError, atomic_write, install_python_payload, public_origin
from .private import locked_directory, read_document, read_private, write_document


def validate_request(value, operation):
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "setup_id", "public_url", "images", "workers"}
                or type(value["format"]) is not int or value["format"] != 1
                or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                       for key in ("release_id", "setup_id"))
                or operation not in {"prepare", "activate"}
                or not isinstance(value["images"], list) or not 1 <= len(value["images"]) <= 32
                or not isinstance(value["workers"], list) or len(value["workers"]) > 8
                or operation == "prepare" and value["workers"]
                or operation == "activate" and not value["workers"]):
            raise ValueError
        origin = public_origin(value["public_url"])
        images = {record["id"]: ComputeImage.parse(record) for record in value["images"]}
        if len(images) != len(value["images"]):
            raise ValueError
        workers = []
        for worker in value["workers"]:
            if not isinstance(worker, dict) or set(worker) != {"enrollment", "profile"}:
                raise ValueError
            config = enrollment(origin, worker["enrollment"])
            profile = modal_profile(worker["profile"])
            if (config != worker["enrollment"] or config["resources"] != profile["resources"]
                    or set(config["image_ids"]) != set(images)
                    or any(not fits(config["resources"], image.maximum_resources) for image in images.values())):
                raise ValueError
            workers.append(worker)
        if len({worker["enrollment"]["id"] for worker in workers}) != len(workers):
            raise ValueError
        if workers and len({worker["enrollment"]["organization_id"] for worker in workers}) != 1:
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, WorkerFault):
        raise InstallError("invalid Modal host request, enrollment, images, or resource profile") from None


def relay_unit(release, worker):
    if not re.fullmatch(r"[a-f0-9]{64}", release) or not re.fullmatch(r"[a-f0-9]{32}", worker):
        raise InstallError("invalid Modal relay unit identity")
    return MANAGED + f"""[Unit]
Description=Reasoning Modal compute relay
After=network-online.target reasoning-server.service
Wants=network-online.target

[Service]
Type=simple
User=reasoning-modal
Group=reasoning-modal
WorkingDirectory=/var/lib/reasoning-modal/{worker}
ExecStart=/opt/reasoning/modal/{release}/venv/bin/reasoning-compute-worker \\
 --data-dir /var/lib/reasoning-modal/{worker} run --backend modal \\
 --modal-profile /var/lib/reasoning-modal/{worker}/modal-profile.json \\
 --catalog /var/lib/reasoning-modal/{worker}/catalog.json
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/reasoning-modal/{worker}
InaccessiblePaths=/var/lib/reasoning /etc/reasoning /var/lib/reasoning-deploy
RestrictSUIDSGID=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
"""


class ModalHostInstaller:
    def __init__(self, root=Path("/"), *, command=None, account=None, identity_client=WorkerClient):
        self.root, self.command, self.account, self.identity_client = root, command, account, identity_client
        self.host = HostInstaller(root=root, command=self.run)

    def run(self, arguments, *, capture=False):
        if self.command is not None:
            return self.command(arguments, capture=capture)
        result = subprocess.run(arguments, env=ENVIRONMENT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=900, check=False)
        if result.returncode or capture and len(result.stdout) > 65536:
            raise InstallError("Modal host command failed; preserve the exact installation inputs")
        return result.stdout if capture else None

    def service_identity(self, name, *, active=False):
        output = self.run(["systemctl", "show", "--property=FragmentPath,DropInPaths,User,Group,LoadState,NeedDaemonReload,ActiveState",
                           name], capture=True)
        try:
            fields = dict(line.split("=", 1) for line in output.decode().splitlines())
        except (AttributeError, ValueError, UnicodeError):
            raise InstallError("invalid Modal service identity receipt") from None
        expected = {"FragmentPath": "/etc/systemd/system/" + name + ".service", "DropInPaths": "",
                    "User": "reasoning-modal", "Group": "reasoning-modal", "LoadState": "loaded", "NeedDaemonReload": "no"}
        if any(fields.get(key) != value for key, value in expected.items()) or active and fields.get("ActiveState") != "active":
            raise InstallError("Modal service identity, overrides, loaded definition, or activity changed")

    def install(self, value, operation):
        validate_request(value, operation)
        lock = self.root / "var/lib/reasoning-deploy/bootstrap.lock"
        read_private(lock, 0)
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            opened, current = os.fstat(fd), lock.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise InstallError("managed host bootstrap lock changed")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError("another managed host operation owns this server") from None
            return self._install(value, operation)
        finally:
            os.close(fd)

    def _install(self, value, operation):
        value = validate_request(value, operation)
        release = value["release_id"]
        installed = read_document(self.root / "var/lib/reasoning-deploy/installation.json")
        if (installed.get("release_id") != release or installed.get("public_url") != value["public_url"]
                or installed.get("phase") != "services_started"):
            raise InstallError("Modal setup requires the exact installed managed chat release and origin")
        for pending in ("active-server-upgrade.json", "active-server-recovery.json"):
            pending_path = self.root / "var/lib/reasoning-deploy" / pending
            if pending_path.exists() or pending_path.is_symlink():
                raise InstallError("managed server operation is pending; Modal setup cannot change its catalog")
        directory = self.host._directory("var/lib/reasoning-deploy/modal-setup", mode=0o700)
        with locked_directory(directory):
            binding = {key: value[key] for key in value if key != "workers"}
            path = directory / "installation.json"
            if path.exists() or path.is_symlink():
                journal = read_document(path)
                if set(journal) != {"binding", "workers_sha256"} or journal["binding"] != binding:
                    raise InstallError("Modal installation inputs changed; preserve the original setup")
            else:
                journal = {"binding": binding, "workers_sha256": None}
                write_document(path, journal, new=True)
            catalog = self.root / "etc/reasoning/compute-images.json"
            info = catalog.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 65536:
                raise InstallError("unsafe installed image catalog")
            previous = json.loads(catalog.read_bytes())
            if previous not in ([], value["images"]):
                raise InstallError("installed image catalog changed; Modal setup will not overwrite it")
            # Always restart on prepare: a lost previous response could have
            # installed bytes but left the running server on the empty catalog.
            if operation == "prepare":
                if previous != value["images"]:
                    atomic_write(catalog, (canonical(value["images"]) + "\n").encode(), mode=0o644)
                self.run(["systemctl", "restart", "reasoning-server"])
                return {"setup_id": value["setup_id"], "catalog_installed": True, "relays_active": False}
            if previous != value["images"]:
                raise InstallError("prepare the exact Modal catalog before activating relays")
            fingerprint = hashlib.sha256(canonical(value["workers"]).encode()).hexdigest()
            if journal["workers_sha256"] not in (None, fingerprint):
                raise InstallError("Modal relay enrollments or credentials changed; do not replace them")
            journal["workers_sha256"] = fingerprint
            write_document(path, journal)
            for worker in value["workers"]:
                client = self.identity_client(worker["enrollment"])
                try:
                    identity = verify_identity(worker["enrollment"], client)
                    if identity["draining"]:
                        raise InstallError("Modal enrollment is draining; do not reactivate it")
                finally:
                    client.close()
            target = self.host._directory(f"opt/reasoning/modal/{release}")
            original = self.root / f"opt/reasoning/releases/{release}"
            requirements = original / "modal-requirements.txt"
            if not requirements.is_file() or requirements.is_symlink():
                raise InstallError("release lacks hash-locked Modal dependencies; rebuild a current release")
            venv = target / "venv"
            if not venv.exists():
                self.run(["python3", "-m", "venv", str(venv)])
            self.run([str(venv / "bin/pip"), "--isolated", "install", "--require-hashes", "--only-binary=:all:",
                      "--index-url", "https://pypi.org/simple", "-r", str(requirements)])
            manifest = json.loads((original / "manifest.json").read_bytes())
            install_python_payload(self.run, original, manifest, venv=venv)
            if self.account is None:
                try:
                    account = pwd.getpwnam("reasoning-modal")
                except KeyError:
                    self.run(["useradd", "--system", "--user-group", "--home-dir", "/var/lib/reasoning-modal",
                              "--shell", "/usr/sbin/nologin", "reasoning-modal"])
                    account = pwd.getpwnam("reasoning-modal")
            else:
                account = self.account
            parent = self.host._directory("var/lib/reasoning-modal", mode=0o755)
            for worker in value["workers"]:
                identifier = worker["enrollment"]["id"]
                state = parent / identifier
                if not state.exists() and not state.is_symlink():
                    state.mkdir(mode=0o700)
                    os.chown(state, account.pw_uid, account.pw_gid)
                info = state.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise InstallError("unsafe Modal relay state")
                for name, body in (("enrollment.json", worker["enrollment"]), ("modal-profile.json", worker["profile"]),
                                   ("catalog.json", value["images"])):
                    output = state / name
                    encoded = canonical(body).encode()
                    if output.exists() or output.is_symlink():
                        info = output.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o077
                                or info.st_nlink != 1 or info.st_size > 65536 or output.read_bytes() != encoded):
                            raise InstallError("Modal relay state changed; do not overwrite its credentials or catalog")
                    else:
                        atomic_write(output, encoded)
                        os.chown(output, account.pw_uid, account.pw_gid)
                unit = self.root / f"etc/systemd/system/reasoning-modal-{identifier}.service"
                body = relay_unit(release, identifier).encode()
                if unit.exists() or unit.is_symlink():
                    info = unit.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                            or info.st_nlink != 1 or unit.read_bytes() != body):
                        raise InstallError("Modal service definition changed; do not overwrite it")
                else:
                    atomic_write(unit, body, mode=0o644)
            self.run(["systemctl", "daemon-reload"])
            for worker in value["workers"]:
                identifier = worker["enrollment"]["id"]
                state = parent / identifier
                # Read-only provider/configuration probe: no sandbox or program.
                self.run(["runuser", "-u", "reasoning-modal", "--", str(venv / "bin/reasoning-compute-worker"),
                          "--data-dir", str(state), "verify-images", "--backend", "modal", "--modal-profile",
                          str(state / "modal-profile.json"), "--catalog", str(state / "catalog.json")])
                name = "reasoning-modal-" + identifier
                self.service_identity(name)
                self.run(["systemctl", "enable", "--now", name])
                self.run(["systemctl", "is-active", "--quiet", name])
                self.service_identity(name, active=True)
            return {"setup_id": value["setup_id"], "relays_active": len(value["workers"]), "live_compute_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--operation", required=True, choices=("prepare", "activate"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InstallError("Modal host installation requires root on the managed chat host")
        result = ModalHostInstaller().install(json.loads(read_private(args.input)), args.operation)
        print(json.dumps(result, sort_keys=True))
        path = args.input
        if (path.name == "modal-input.json" and re.fullmatch(r"[a-f0-9]{64}", path.parent.name)
                and path.parent.parent == Path("/var/lib/reasoning-deploy/staging")):
            path.unlink()
        return 0
    except (InstallError, ValueError, TypeError, OSError, WorkerFault):
        parser.exit(2, "reasoning-modal-install: private configuration, managed host identity, or relay readiness failed; preserve state\n")


if __name__ == "__main__":
    raise SystemExit(main())
