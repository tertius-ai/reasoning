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

from reasoning_ledger.server.compute_catalog import ComputeImage, canonical, fits, resources
from reasoning_ledger.server.compute_modal import modal_profile
from reasoning_ledger.server.compute_worker import WorkerClient, WorkerFault, drain_status, enrollment, verify_identity

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


RELAY_UNIT = re.compile(r"reasoning-modal-([a-f0-9]{32})\.service\Z")
RELAY_RUNTIME = re.compile(rb"^ExecStart=/opt/reasoning/modal/([a-f0-9]{64})/venv/bin/reasoning-compute-worker \\$", re.MULTILINE)


def validate_refresh(value):
    """Move every installed relay onto the installed release's relay runtime."""
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "public_url"}
                or type(value["format"]) is not int or value["format"] != 1
                or not isinstance(value["release_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["release_id"])):
            raise ValueError
        public_origin(value["public_url"])
        return value
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid Modal relay update request") from None


def validate_limits(value):
    """Give every installed relay the same new per-job limits, for exactly the relay workers the operator changed."""
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "public_url", "resources", "workers"}
                or type(value["format"]) is not int or value["format"] != 1
                or not isinstance(value["release_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["release_id"])
                or not isinstance(value["workers"], list) or not 1 <= len(value["workers"]) <= 64
                or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{32}", item) for item in value["workers"])
                or value["workers"] != sorted(set(value["workers"]))
                or value["resources"] != resources(value["resources"])):
            raise ValueError
        public_origin(value["public_url"])
        return value
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid Modal relay limits request") from None


def validate_probe(value):
    """Run the Lean environment mount probe on one installed relay of the installed release."""
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "public_url"}
                or type(value["format"]) is not int or value["format"] != 1
                or not isinstance(value["release_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["release_id"])):
            raise ValueError
        public_origin(value["public_url"])
        return value
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid Lean environment probe request") from None


# Two VMs, each bounded by its Modal lifetime, plus the image download; see compute_environment_probe.
PROBE_SECONDS = 1140
# One VM for at most an hour (compute_environment_prepare.PREPARE_SECONDS), plus the relay's checks and reads.
PREPARE_SECONDS = 3900


def validate_preparation(value):
    """Prepare one approved Lean environment on one installed relay of the installed release."""
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "public_url", "environment_id"}
                or type(value["format"]) is not int or value["format"] != 1
                or not isinstance(value["release_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["release_id"])
                or not isinstance(value["environment_id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", value["environment_id"])):
            raise ValueError
        public_origin(value["public_url"])
        return value
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid Lean environment preparation request") from None


def validate_addition(value, operation):
    """Add approved images to a running Modal installation: new catalog entries and new relays only."""
    try:
        if (not isinstance(value, dict)
                or set(value) != {"format", "release_id", "addition_id", "public_url", "previous", "images", "source_worker", "workers"}
                or type(value["format"]) is not int or value["format"] != 1
                or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                       for key in ("release_id", "addition_id"))
                or not isinstance(value["source_worker"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["source_worker"])
                or operation not in {"extend", "relays"}
                or not isinstance(value["previous"], list) or not 1 <= len(value["previous"]) <= 32
                or not isinstance(value["images"], list) or not len(value["previous"]) < len(value["images"]) <= 32
                or not isinstance(value["workers"], list) or len(value["workers"]) > 8
                or (operation == "extend") == bool(value["workers"])):
            raise ValueError
        origin = public_origin(value["public_url"])
        previous = {record["id"]: ComputeImage.parse(record) for record in value["previous"]}
        images = {record["id"]: ComputeImage.parse(record) for record in value["images"]}
        if (len(previous) != len(value["previous"]) or len(images) != len(value["images"])
                or value["previous"] != [image.record() for image in previous.values()]
                or value["images"] != [images[key].record() for key in sorted(images)]
                or any(key not in images or images[key] != image for key, image in previous.items())):
            raise ValueError
        additions = {key: image for key, image in images.items() if key not in previous}
        for worker in value["workers"]:
            if not isinstance(worker, dict) or set(worker) != {"enrollment"}:
                raise ValueError
            config = enrollment(origin, worker["enrollment"])
            if (config != worker["enrollment"] or set(config["image_ids"]) != set(additions)
                    or config["resources"] != value["workers"][0]["enrollment"]["resources"]
                    or any(not fits(config["resources"], image.maximum_resources) for image in additions.values())):
                raise ValueError
        identities = [worker["enrollment"]["id"] for worker in value["workers"]]
        if len(set(identities)) != len(identities) or value["source_worker"] in identities:
            raise ValueError
        if value["workers"] and len({worker["enrollment"]["organization_id"] for worker in value["workers"]}) != 1:
            raise ValueError
        return value, additions
    except (KeyError, TypeError, ValueError, WorkerFault):
        raise InstallError("invalid Modal image addition, catalog, enrollment, or relay source") from None


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

    def run(self, arguments, *, capture=False, timeout=900):
        if self.command is not None:
            return self.command(arguments, capture=capture)
        result = subprocess.run(arguments, env=ENVIRONMENT, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
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
        return self._locked(lambda: self._install(value, operation))

    def _locked(self, action):
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
            return action()
        finally:
            os.close(fd)

    def _installed(self, value):
        installed = read_document(self.root / "var/lib/reasoning-deploy/installation.json")
        if (installed.get("release_id") != value["release_id"] or installed.get("public_url") != value["public_url"]
                or installed.get("phase") != "services_started"):
            raise InstallError("Modal setup requires the exact installed managed chat release and origin")
        for pending in ("active-server-upgrade.json", "active-server-recovery.json"):
            pending_path = self.root / "var/lib/reasoning-deploy" / pending
            if pending_path.exists() or pending_path.is_symlink():
                raise InstallError("managed server operation is pending; Modal setup cannot change its catalog")

    def _install(self, value, operation):
        value = validate_request(value, operation)
        release = value["release_id"]
        self._installed(value)
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
            venv = self._relay_venv(release)
            account = self._relay_account(create=True)
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
                self._unit(release, identifier)
            self._start_relays(venv, parent, [worker["enrollment"]["id"] for worker in value["workers"]])
            return {"setup_id": value["setup_id"], "relays_active": len(value["workers"]), "live_compute_verified": False}

    def _relay_venv(self, release):
        """The relay runtime of one installed release, with hash-locked Modal dependencies."""
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
        return venv

    def _relay_account(self, *, create):
        if self.account is not None:
            return self.account
        try:
            return pwd.getpwnam("reasoning-modal")
        except KeyError:
            if not create:
                raise InstallError("the Modal relay account is missing; run the original Modal setup first") from None
            self.run(["useradd", "--system", "--user-group", "--home-dir", "/var/lib/reasoning-modal",
                      "--shell", "/usr/sbin/nologin", "reasoning-modal"])
            return pwd.getpwnam("reasoning-modal")

    def _probe(self, venv, state):
        # Read-only provider/configuration probe: no sandbox or program, and no claim on the relay's state lock.
        self.run(["runuser", "-u", "reasoning-modal", "--", str(venv / "bin/reasoning-compute-worker"),
                  "--data-dir", str(state), "verify-images", "--backend", "modal", "--modal-profile",
                  str(state / "modal-profile.json"), "--catalog", str(state / "catalog.json")])

    def _active(self, name):
        output = self.run(["systemctl", "show", "--property=ActiveState", name], capture=True)
        try:
            fields = dict(line.split("=", 1) for line in output.decode().splitlines())
        except (AttributeError, ValueError, UnicodeError):
            raise InstallError("invalid Modal service state receipt") from None
        return fields.get("ActiveState") == "active"

    def _start_relays(self, venv, parent, identifiers):
        self.run(["systemctl", "daemon-reload"])
        for identifier in identifiers:
            self._probe(venv, parent / identifier)
            name = "reasoning-modal-" + identifier
            self.service_identity(name)
            self.run(["systemctl", "enable", "--now", name])
            self.run(["systemctl", "is-active", "--quiet", name])
            self.service_identity(name, active=True)

    def _private_relay_file(self, path, account):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 65536):
            raise InstallError("Modal relay state changed; do not overwrite its credentials or catalog")
        return path.read_bytes()

    def _source_profile(self, parent, worker, account):
        """Reuse an installed relay's Modal profile in place; its credentials never leave this host."""
        state = parent / worker
        try:
            info = state.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError
        except (OSError, ValueError):
            raise InstallError("the source Modal relay is missing or unsafe") from None
        try:
            return modal_profile(json.loads(self._private_relay_file(state / "modal-profile.json", account)))
        except (InstallError, OSError, ValueError, TypeError):
            raise InstallError("the source Modal relay profile is missing, unsafe or invalid") from None

    def add_images(self, value, operation):
        validate_addition(value, operation)
        return self._locked(lambda: self._add_images(value, operation))

    def _add_images(self, value, operation):
        value, additions = validate_addition(value, operation)
        release = value["release_id"]
        self._installed(value)
        setup = self.root / "var/lib/reasoning-deploy/modal-setup"
        if not (setup / "installation.json").is_file():
            raise InstallError("add images only to a host with an installed Modal setup")
        with locked_directory(setup):
            directory = self.host._directory("var/lib/reasoning-deploy/modal-setup/additions", mode=0o700)
            path = directory / (value["addition_id"] + ".json")
            binding = {key: value[key] for key in value if key != "workers"}
            if path.exists() or path.is_symlink():
                journal = read_document(path)
                if set(journal) != {"binding", "workers_sha256"} or journal["binding"] != binding:
                    raise InstallError("Modal image addition inputs changed; preserve the original addition")
            else:
                journal = {"binding": binding, "workers_sha256": None}
                write_document(path, journal, new=True)
            account = self._relay_account(create=False)
            parent = self.root / "var/lib/reasoning-modal"
            profile = self._source_profile(parent, value["source_worker"], account)
            catalog = self.root / "etc/reasoning/compute-images.json"
            info = catalog.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_size > 65536:
                raise InstallError("unsafe installed image catalog")
            current = json.loads(catalog.read_bytes())
            if current not in (value["previous"], value["images"]):
                raise InstallError("installed image catalog changed; the addition will not overwrite it")
            if operation == "extend":
                if current != value["images"]:
                    atomic_write(catalog, (canonical(value["images"]) + "\n").encode(), mode=0o644)
                # Restart on every retry: a lost reply could leave the server on the old catalog.
                self.run(["systemctl", "restart", "reasoning-server"])
                return {"addition_id": value["addition_id"], "catalog_installed": True, "relays_active": 0}
            if current != value["images"]:
                raise InstallError("extend the image catalog before adding its relays")
            if any(worker["enrollment"]["resources"] != profile["resources"] for worker in value["workers"]):
                raise InstallError("new relay limits differ from the source relay's Modal profile")
            fingerprint = hashlib.sha256(canonical(value["workers"]).encode()).hexdigest()
            if journal["workers_sha256"] not in (None, fingerprint):
                raise InstallError("Modal relay enrollments changed; do not replace them")
            journal["workers_sha256"] = fingerprint
            write_document(path, journal)
            for worker in value["workers"]:
                client = self.identity_client(worker["enrollment"])
                try:
                    if verify_identity(worker["enrollment"], client)["draining"]:
                        raise InstallError("Modal enrollment is draining; do not reactivate it")
                finally:
                    client.close()
            venv = self._relay_venv(release)
            selected = [additions[key].record() for key in sorted(additions)]
            for worker in value["workers"]:
                identifier = worker["enrollment"]["id"]
                state = parent / identifier
                if not state.exists() and not state.is_symlink():
                    state.mkdir(mode=0o700)
                    os.chown(state, account.pw_uid, account.pw_gid)
                info = state.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
                    raise InstallError("unsafe Modal relay state")
                for name, body in (("enrollment.json", worker["enrollment"]), ("modal-profile.json", profile),
                                   ("catalog.json", selected)):
                    output = state / name
                    encoded = canonical(body).encode()
                    if output.exists() or output.is_symlink():
                        if self._private_relay_file(output, account) != encoded:
                            raise InstallError("Modal relay state changed; do not overwrite its credentials or catalog")
                    else:
                        atomic_write(output, encoded)
                        os.chown(output, account.pw_uid, account.pw_gid)
                self._unit(release, identifier)
            self._start_relays(venv, parent, [worker["enrollment"]["id"] for worker in value["workers"]])
            return {"addition_id": value["addition_id"], "relays_active": len(value["workers"]), "live_compute_verified": False}

    def refresh(self, value):
        validate_refresh(value)
        return self._locked(lambda: self._refresh(value))

    def _relay_safety(self, parent, identifier, account):
        state = parent / identifier
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o700:
            raise InstallError("unsafe Modal relay state")
        return state

    def _drained_relays(self, value, account, parent, *, offered=None):
        """Every installed relay, each proven unchanged, drained, settled and idle locally.

        The server must report each relay's own enrollment, or, with ``offered``, those limits in its place.
        """
        units = self.root / "etc/systemd/system"
        relays = []
        for path in sorted(units.iterdir()):
            match = RELAY_UNIT.fullmatch(path.name)
            if not match:
                continue
            identifier, name = match[1], path.name[:-len(".service")]
            info = path.lstat()
            body = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
            runtime = RELAY_RUNTIME.search(body)
            if (not runtime or info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1
                    or body != relay_unit(runtime[1].decode(), identifier).encode()):
                raise InstallError("Modal service definition changed; do not overwrite it")
            self.service_identity(name)
            state = self._relay_safety(parent, identifier, account)
            sandbox = state / "modal-sandbox.json"
            if ((sandbox.exists() or sandbox.is_symlink())
                    and json.loads(self._private_relay_file(sandbox, account)) != {"phase": "idle"}):
                raise InstallError("a Modal relay has an unfinished VM attempt; let it settle first")
            try:
                config = enrollment(value["public_url"], json.loads(self._private_relay_file(state / "enrollment.json", account)))
                if config["id"] != identifier:
                    raise ValueError
            except (ValueError, TypeError, KeyError, WorkerFault):
                raise InstallError("a Modal relay enrollment is missing or does not match its service") from None
            expected = config if offered is None else {**config, "resources": offered}
            client = self.identity_client(expected)
            try:
                try:
                    identity = verify_identity(expected, client)
                except WorkerFault as exc:
                    if str(exc) != "enrollment_mismatch" or offered is None:
                        raise
                    raise InstallError("the server does not record the new limits for every relay; change them there first") from None
                if not identity["draining"]:
                    raise InstallError("drain every Modal relay worker first; resume claims afterwards")
                if not drain_status(expected, client.request("GET", "/api/worker/status"))["quiescent"]:
                    raise InstallError("a Modal relay still has running or unknown jobs; settle them first")
            finally:
                client.close()
            relays.append((identifier, name, runtime[1].decode()))
        if not relays:
            raise InstallError("no Modal relays are installed on this host")
        return relays

    def _refresh(self, value):
        """Replace only the relay runtime. Enrollments, credentials, catalogs and Modal profiles stay as they are.

        Every relay must be drained with no running or unknown job, and idle locally, so no attempt is cut off. All
        relays are probed with the new runtime before any is stopped. Only relays that were running are started again.
        """
        value = validate_refresh(value)
        release = value["release_id"]
        self._installed(value)
        setup = self.root / "var/lib/reasoning-deploy/modal-setup"
        if not (setup / "installation.json").is_file():
            raise InstallError("update relays only on a host with an installed Modal setup")
        with locked_directory(setup):
            account = self._relay_account(create=False)
            parent = self.root / "var/lib/reasoning-modal"
            units = self.root / "etc/systemd/system"
            relays = self._drained_relays(value, account, parent)
            venv = self._relay_venv(release)
            for identifier, _, _ in relays:
                self._probe(venv, parent / identifier)
            # A lost reply after a stop must still start the relays that were running: keep that intent first.
            journal = setup / f"relay-refresh-{release}.json"
            restart = read_document(journal)["restart"] if journal.exists() or journal.is_symlink() else []
            changed = []
            for identifier, name, current in relays:
                if current == release:
                    continue
                if self._active(name) and identifier not in restart:
                    restart.append(identifier)
                    write_document(journal, {"release_id": release, "restart": sorted(restart)})
                self.run(["systemctl", "stop", name])
                atomic_write(units / (name + ".service"), relay_unit(release, identifier).encode(), mode=0o644)
                changed.append(identifier)
            self.run(["systemctl", "daemon-reload"])
            self._restart(relays, restart)
            return {"release_id": release, "relays": [identifier for identifier, _, _ in relays], "updated": sorted(changed),
                    "restarted": sorted(restart), "live_compute_verified": False}

    def _restart(self, relays, restart):
        for identifier, name, _ in relays:
            self.service_identity(name)
            if identifier in restart:
                self.run(["systemctl", "start", name])
                self.run(["systemctl", "is-active", "--quiet", name])
                self.service_identity(name, active=True)

    def limits(self, value):
        validate_limits(value)
        return self._locked(lambda: self._limits(value))

    def _limits(self, value):
        """Give every relay the per-job limits the server already records for it, in its enrollment, its Modal profile
        and the runtime binding that names that profile. Credentials, catalogs and the relay runtime stay as they are.

        Every relay must be drained with no running or unknown job, and idle locally, so a relay and the server never
        disagree while it can claim a job. All relays are checked before any is stopped, and each is probed with its new
        files before any starts. Only relays that were running are started again.
        """
        value = validate_limits(value)
        offered = value["resources"]
        self._installed(value)
        setup = self.root / "var/lib/reasoning-deploy/modal-setup"
        if not (setup / "installation.json").is_file():
            raise InstallError("change relay limits only on a host with an installed Modal setup")
        with locked_directory(setup):
            account = self._relay_account(create=False)
            parent = self.root / "var/lib/reasoning-modal"
            relays = self._drained_relays(value, account, parent, offered=offered)
            if sorted(identifier for identifier, _, _ in relays) != value["workers"]:
                raise InstallError("the relays on this host differ from the relay workers on the server; reconcile them first")
            changes = []
            for identifier, name, _ in relays:
                state = parent / identifier
                try:
                    config = json.loads(self._private_relay_file(state / "enrollment.json", account))
                    profile = modal_profile(json.loads(self._private_relay_file(state / "modal-profile.json", account)))
                    catalog = json.loads(self._private_relay_file(state / "catalog.json", account))
                    images = [ComputeImage.parse(record) for record in catalog]
                except (InstallError, OSError, ValueError, TypeError, KeyError):
                    raise InstallError("a Modal relay profile or catalog is missing, unsafe or invalid") from None
                if not images or any(not fits(offered, image.maximum_resources) for image in images):
                    raise InstallError("the new limits exceed the maximum resources of an image a relay serves")
                changed_profile = {**profile, "resources": offered}
                files = {"enrollment.json": {**config, "resources": offered}, "modal-profile.json": changed_profile}
                binding = state / "modal-runtime.json"
                if binding.exists() or binding.is_symlink():
                    files["modal-runtime.json"] = self._runtime_binding(binding, account, identifier, images, profile,
                                                                        changed_profile)
                changes.append((identifier, name, state, files))
            # A lost reply after a stop must still start the relays that were running: keep that intent first.
            journal = setup / "relay-limits.json"
            restart = read_document(journal)["restart"] if journal.exists() or journal.is_symlink() else []
            changed = []
            for identifier, name, state, files in changes:
                if all(self._private_relay_file(state / file, account) == canonical(body).encode() for file, body in files.items()):
                    continue
                if self._active(name) and identifier not in restart:
                    restart.append(identifier)
                    write_document(journal, {"restart": sorted(restart)})
                self.run(["systemctl", "stop", name])
                for file, body in files.items():
                    atomic_write(state / file, canonical(body).encode())
                    os.chown(state / file, account.pw_uid, account.pw_gid)
                changed.append(identifier)
            for identifier, _, runtime in relays:
                self._probe(self.root / f"opt/reasoning/modal/{runtime}/venv", parent / identifier)
            self._restart(relays, restart)
            if journal.exists() or journal.is_symlink():
                journal.unlink()
            return {"resources": offered, "relays": [identifier for identifier, _, _ in relays], "updated": sorted(changed),
                    "restarted": sorted(restart), "live_compute_verified": False}

    def _runtime_binding(self, path, account, identifier, images, profile, changed_profile):
        """The relay's Modal runtime binding for its changed profile: the same worker, Modal app and catalog.

        The relay checks this binding on every start and probe, and it names the exact Modal profile by digest. Before
        any file is rewritten it must name the relay's current profile. A rerun after a partial rewrite finds the
        changed profile already in place, and then only the worker, app and catalog can still be checked.
        """
        def digest(value):
            return hashlib.sha256(canonical(value).encode()).hexdigest()

        try:
            binding = json.loads(self._private_relay_file(path, account))
            if (not isinstance(binding, dict) or set(binding) != {"format", "worker_id", "app_id", "profile_sha256", "images"}
                    or type(binding["format"]) is not int or binding["format"] != 1 or binding["worker_id"] != identifier
                    or not isinstance(binding["app_id"], str) or not re.fullmatch(r"ap-[A-Za-z0-9]+", binding["app_id"])
                    or not isinstance(binding["profile_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", binding["profile_sha256"])
                    or binding["images"] != [image.record() for image in sorted(images, key=lambda image: image.id)]
                    or profile != changed_profile and binding["profile_sha256"] != digest(profile)):
                raise ValueError
        except (InstallError, OSError, ValueError, TypeError, KeyError):
            raise InstallError("a Modal relay's runtime binding is unsafe or does not match its profile and catalog") from None
        return {**binding, "profile_sha256": digest(changed_profile)}

    def environment_probe(self, value):
        validate_probe(value)
        return self._locked(lambda: self._environment_probe(value))

    def _environment_probe(self, value):
        """Run `probe-environment-mount` as the relay account, with the first installed relay's own state, profile and
        catalog, on that relay's runtime, which must be the installed release's. The relay keeps running: the probe
        uses its own VMs and its own directory inside the relay's state, and claims no job."""
        value = validate_probe(value)
        identifier, output = self._first_relay(value, ["probe-environment-mount"], PROBE_SECONDS,
                                               "probe Lean environments")
        try:
            receipt = json.loads(output)
            if not isinstance(receipt, dict) or receipt.get("format") != 1 or not isinstance(receipt.get("failures"), list):
                raise ValueError
        except (TypeError, ValueError):
            raise InstallError("invalid Lean environment probe receipt") from None
        return {"release_id": value["release_id"], "relay": identifier, "probe": receipt}

    def environment_prepare(self, value):
        validate_preparation(value)
        return self._locked(lambda: self._environment_prepare(value))

    def _environment_prepare(self, value):
        """Run `prepare-environment` for one approved environment as the relay account, with the first installed
        relay's own enrollment, profile and catalog. The relay keeps running: the preparation uses its own VM and its
        own directory inside the relay's state, and claims no job."""
        value = validate_preparation(value)
        identifier, output = self._first_relay(
            value, ["prepare-environment", "--environment", value["environment_id"]], PREPARE_SECONDS,
            "prepare Lean environments")
        try:
            receipt = json.loads(output)
            if (not isinstance(receipt, dict) or receipt.get("format") != 1 or not isinstance(receipt.get("failures"), list)
                    or receipt.get("environment_id") != value["environment_id"]):
                raise ValueError
        except (TypeError, ValueError):
            raise InstallError("invalid Lean environment preparation receipt") from None
        return {"release_id": value["release_id"], "relay": identifier, "preparation": receipt}

    def _first_relay(self, value, command, timeout, purpose):
        """Run a relay worker command as the relay account for the first installed relay, whose service must run the
        installed release, and return that relay's identifier and the command's output."""
        release = value["release_id"]
        self._installed(value)
        setup = self.root / "var/lib/reasoning-deploy/modal-setup"
        if not (setup / "installation.json").is_file():
            raise InstallError(f"{purpose} only on a host with an installed Modal setup")
        with locked_directory(setup):
            account = self._relay_account(create=False)
            parent = self.root / "var/lib/reasoning-modal"
            for path in sorted((self.root / "etc/systemd/system").iterdir()):
                match = RELAY_UNIT.fullmatch(path.name)
                if not match:
                    continue
                identifier = match[1]
                info = path.lstat()
                body = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
                if (info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1
                        or body != relay_unit(release, identifier).encode()):
                    raise InstallError("update the Modal relays to the installed release first (make compute-relays-update)")
                self.service_identity(path.name[:-len(".service")])
                state = self._relay_safety(parent, identifier, account)
                venv = self.root / f"opt/reasoning/modal/{release}/venv"
                output = self.run(["runuser", "-u", "reasoning-modal", "--", str(venv / "bin/reasoning-compute-worker"),
                                   "--data-dir", str(state), command[0], *command[1:], "--modal-profile",
                                   str(state / "modal-profile.json"), "--catalog", str(state / "catalog.json")],
                                  capture=True, timeout=timeout)
                return identifier, output
            raise InstallError("no Modal relays are installed on this host")

    def _unit(self, release, identifier):
        unit = self.root / f"etc/systemd/system/reasoning-modal-{identifier}.service"
        body = relay_unit(release, identifier).encode()
        if unit.exists() or unit.is_symlink():
            info = unit.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                    or info.st_nlink != 1 or unit.read_bytes() != body):
                raise InstallError("Modal service definition changed; do not overwrite it")
        else:
            atomic_write(unit, body, mode=0o644)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--operation", required=True,
                        choices=("prepare", "activate", "extend", "relays", "refresh", "limits", "environment-probe",
                                 "environment-prepare"))
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InstallError("Modal host installation requires root on the managed chat host")
        installer, value = ModalHostInstaller(), json.loads(read_private(args.input))
        if args.operation == "refresh":
            result = installer.refresh(value)
        elif args.operation == "limits":
            result = installer.limits(value)
        elif args.operation == "environment-probe":
            result = installer.environment_probe(value)
        elif args.operation == "environment-prepare":
            result = installer.environment_prepare(value)
        elif args.operation in {"extend", "relays"}:
            result = installer.add_images(value, args.operation)
        else:
            result = installer.install(value, args.operation)
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
