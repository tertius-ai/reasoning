"""Owner-confirmed dedicated bot VMs, shared by the web wizard and CLI.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from reasoning_ledger.server.bot_client import configuration
from reasoning_ledger.server.bot_runtime import MODEL

from .digitalocean import ProviderRejected
from .host import InstallError, canonical, public_origin
from .private import locked_directory, read_document, read_private, write_document, write_private
from .provision import PLAN_KEYS, Provisioner, plan_identity, save_plan, ssh_access_cidr, validate_compute_plan
from .ssh_host import PIN_REQUIREMENT, _owned_trust

BOT_KEYS = (PLAN_KEYS - {"public_url", "dns_zone", "dns_record_name"}) | {"purpose", "bot"}
SCOPES = ["messages:read", "messages:write", "knowledge:read", "knowledge:write"]
TOKEN_SCOPES = "account:read, sizes:read, ssh_key:read/create, tag:read/create, firewall:read/create, droplet:read/create"
LOGIN = {"codex": "codex login --device-auth", "claude": "claude auth login"}


def validate_spec(value):
    try:
        # A retained setup may carry allow_other_users; it is ignored, since who can use the bot is set in My bots.
        if set(value) - {"allow_other_users"} != {"server", "engine", "models", "cas", "lean", "wheel_sha256"}:
            raise ValueError
        if value["engine"] not in LOGIN or public_origin(value["server"]) != value["server"]:
            raise ValueError
        if any(type(value[key]) is not bool for key in {"cas", "lean"} | {"allow_other_users"} & set(value)):
            raise ValueError
        if (not isinstance(value["models"], list) or not 1 <= len(value["models"]) <= 20
                or any(not isinstance(item, str) or not MODEL.fullmatch(item) for item in value["models"])
                or len(set(value["models"])) != len(value["models"])
                or not re.fullmatch(r"[a-f0-9]{64}", value["wheel_sha256"])):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid cloud bot engine, models, tools, server, or artifact") from exc


def check_size(quote, spec):
    validate_spec(spec)
    minimum = 8192 if spec["lean"] else 4096 if spec["cas"] else 2048
    if quote["resources"]["memory_mib"] < minimum:
        raise InstallError(f"selected tools require at least {minimum // 1024} GiB RAM")


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != BOT_KEYS or plan.get("purpose") != "personal-bot":
        raise InstallError("invalid personal cloud bot plan")
    validate_compute_plan(plan)
    validate_spec(plan["bot"])
    check_size(plan, plan["bot"])
    ssh_access_cidr(plan["admin_cidr"])
    if plan["requirements"] != [PIN_REQUIREMENT]:
        raise InstallError("cloud bots require original pinned SSH trust")


def identity(document):
    return plan_identity(document, validator=validate_plan)


def enroll_key(provider, directory):
    """Write intent before upload and recover ONLY this generated public key."""
    path = directory / "operator.json"
    if path.exists():
        value = read_document(path)
    else:
        key = Ed25519PrivateKey.generate()
        value = {
            "private": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                         serialization.NoEncryption()).decode(),
            "public": key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode(),
            "phase": "new", "id": None,
        }
        write_document(path, value, new=True)
        write_private(directory / "operator-key", value["private"].encode())
    matches = [row for row in provider.pages("account/keys", "ssh_keys") if row.get("public_key") == value["public"]]
    if len(matches) > 1 or matches and value["phase"] == "new":
        raise InstallError("operator SSH key ownership is ambiguous")
    if matches:
        key = matches[0]
    else:
        if value["phase"] != "new":
            raise InstallError("SSH key upload uncertain; resume reads, do not upload another key")
        value["phase"] = "uploading"
        write_document(path, value)
        try:
            key = provider.post("account/keys", {"name": "reasoning-bot-" + directory.name[:32],
                                                  "public_key": value["public"]}).get("ssh_key", {})
        except ProviderRejected:
            value["phase"] = "new"
            write_document(path, value)
            raise
    if key.get("public_key") != value["public"] or type(key.get("id")) is not int or key["id"] <= 0:
        raise InstallError("provider returned a different operator SSH key")
    if value["id"] is not None and value["id"] != key["id"]:
        raise InstallError("operator SSH key identity changed")
    value.update(phase="enrolled", id=key["id"])
    write_document(path, value)
    return key["id"]


def prepare(provider, directory, quote, spec):
    """Called only after accepting a read-only quote; retain one exact plan."""
    check_size(quote, spec)
    path = directory / "plan.json"
    if path.exists():
        document = read_document(path)
        identity(document)
        if document["plan"]["bot"] != spec:
            raise InstallError("bot inputs changed; preserve this original operation")
        return document
    if provider.identity() != quote["account_identity"]:
        raise InstallError("DigitalOcean account changed since the quote")
    ssh_key_id = enroll_key(provider, directory)
    current = provider.compute_plan(**{key: quote[key] for key in ("name", "region", "size", "admin_cidr")},
                                    maximum_hourly_usd=quote["quote"]["maximum_hourly_usd"], ssh_key_id=ssh_key_id)
    if {key: current[key] for key in quote} != quote:
        raise InstallError("DigitalOcean quote changed; request and confirm a fresh quote")
    plan = {**current, "purpose": "personal-bot", "bot": spec, "requirements": [PIN_REQUIREMENT]}
    save_plan(path, plan, validator=validate_plan)
    return read_document(path)


def provision_ready(controller, directory, *, wait=None, attempts=36):
    """Wait through ordinary readiness using the same retained creation intents.

    Unknown requests still raise from apply: this never blindly reissues a
    cloud write. Each attempt rechecks the owner and the confirmed quote.
    """
    if type(attempts) is not int or not 1 <= attempts <= 36:
        raise InstallError("invalid bounded cloud bot readiness wait")
    for attempt in range(attempts):
        result = controller.apply(directory, controller.identity)
        if result["ssh_host"] or attempt == attempts - 1:
            return result
        (wait or time.sleep)(5)


class BotProvisioner(Provisioner):
    kinds = ("tag", "firewall", "droplet")
    identity_function = staticmethod(identity)

    def __init__(self, provider, document, *, authorize=lambda: None):
        super().__init__(provider, document)
        self.authorize = authorize
        self.marker = "reasoning-bot-" + document["deployment_id"]
        self.vm_name = self.marker

    def _current_plan(self):
        self.authorize()
        return self.provider.compute_plan(**{key: self.plan[key] for key in ("name", "region", "size", "ssh_key_id", "admin_cidr")},
                                          maximum_hourly_usd=self.plan["quote"]["maximum_hourly_usd"])

    def _before_creation(self, kind):
        self.authorize()

    def _firewall_body(self):
        body = super()._firewall_body()
        body["inbound_rules"] = body["inbound_rules"][:1]
        return body

    def _dns_preflight(self):
        pass

    def _publish_dns(self, address):
        pass

    def _result(self, phase, *, address=None):
        droplet = self.state["resources"]["droplet"]["id"]
        return {"phase": phase, "ssh_host": address, "droplet_id": droplet,
                "console_url": "https://cloud.digitalocean.com/droplets" + (f"/{droplet}" if droplet else "")}

    @contextmanager
    def connection(self, directory):
        """Recheck exact resource ownership and firewall before every host action."""
        with locked_directory(directory):
            self.authorize()
            if self.provider.identity() != self.plan["account_identity"]:
                raise InstallError("DigitalOcean account no longer matches this bot")
            self.state = read_document(directory / "digitalocean.json")
            self._validate_state()
            if any(row["status"] != "created" for row in self.state["resources"].values()):
                raise InstallError("VM creation is not verified; resume provisioning")
            droplet_id = self.state["resources"]["droplet"]["id"]
            droplet = self.provider.get(f"droplets/{droplet_id}").get("droplet")
            self._check("droplet", droplet)
            applied = self.provider.pages(f"droplets/{droplet_id}/firewalls", "firewalls")
            if len(applied) != 1:
                raise InstallError("bot VM firewall attachment changed")
            self._check("firewall", applied[0])
            if droplet.get("status") != "active" or not self._firewall_ready(applied[0]):
                raise InstallError("VM or firewall is not ready; retry the same operation")
            address = self._address(droplet)
            request = {"format": 1, "plan_id": self.identity, "deployment_id": self.document["deployment_id"],
                       "droplet_id": droplet_id}
            with _owned_trust(self.document, directory, address, request, None, self.identity) as trust:
                yield SSH(address, directory / "operator-key", trust), request


class SSH:
    def __init__(self, address, key, trust):
        read_private(key)
        self.host = "root@" + address
        self.options = ["-i", str(key), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(trust),
                        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]

    def command(self, args, *, timeout=30, interactive=False):
        try:
            result = subprocess.run(["ssh", *self.options, *( ["-t"] if interactive else []), self.host, shlex.join(args)],
                                    stdin=None if interactive else subprocess.DEVNULL,
                                    stdout=None if interactive else subprocess.DEVNULL,
                                    stderr=None if interactive else subprocess.DEVNULL, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallError("bot SSH action unavailable or timed out; preserve state and resume") from exc
        if result.returncode:
            raise InstallError("bot host action failed; inspect /var/log/reasoning-bot-install.log in the owned Droplet console")

    def upload(self, local, remote):
        try:
            subprocess.run(["scp", *self.options, str(local), self.host + ":" + remote],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError("bot artifact upload unverified; resume with the same artifact and state") from exc


def install(controller, directory, wheel, credential, *, console_password=None):
    spec = controller.plan["bot"]
    # Never transmit a different artifact or enrollment on a retry.
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != spec["wheel_sha256"]:
        raise InstallError("bot source changed; preserve the confirmed artifact" if wheel.suffix == ".zip"
                           else "bot wheel changed; preserve the confirmed artifact")
    if (not (wheel.name == "reasoning-source.zip" or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}\.whl", wheel.name))
            or wheel.is_symlink()):
        raise InstallError("invalid bot wheel filename or symlink")
    config = configuration(spec["server"], credential)
    if console_password is not None and not re.fullmatch(r"[a-z]{32}", console_password):
        raise InstallError("invalid private setup-console password")
    binding = hashlib.sha256(canonical({"spec": spec, "config": config, "console_password": console_password})).hexdigest()
    with controller.connection(directory) as (ssh, request):
        journal = directory / "installation.json"
        if journal.exists() and read_document(journal)["binding"] != binding:
            raise InstallError("bot installation inputs changed")
        write_document(journal, {"phase": "installing", "binding": binding})
        # Provider "active" precedes cloud-init/sshd readiness. Retry only a
        # pinned, read-only probe, never a cloud write or inference invocation.
        for attempt in range(6):
            controller.authorize()
            try:
                ssh.command(["test", "-r", "/etc/reasoning-provisioning.json"])
                break
            except InstallError:
                if attempt == 5:
                    raise
                time.sleep(5)
        payload = directory / "install-input.json"
        write_document(payload, {"spec": spec, "config": config, "request": request, "wheel_name": wheel.name,
                                 "console_password": console_password})
        ssh.command(["install", "-d", "-m", "0700", "/root/reasoning-bot-install"])
        ssh.upload(wheel, "/root/reasoning-bot-install/" + wheel.name)
        if wheel.suffix == ".zip":
            from zipfile import ZipFile

            from reasoning_ledger import source_install

            with ZipFile(wheel) as source:
                source_install.inspect_source(source)
                helper = source.read("reasoning_ledger/source_install.py")
            helper_path = directory / "source-installer.py"
            write_private(helper_path, helper, maximum=1024**2)
            ssh.upload(helper_path, "/root/reasoning-bot-install/source-installer.py")
        try:
            ssh.upload(payload, "/root/reasoning-bot-install/input.json")
            ssh.upload(Path(__file__).with_name("bot_cloud_host.py"), "/root/reasoning-bot-install/install.py")
            ssh.command(["python3", "/root/reasoning-bot-install/install.py"], timeout=1800)
        finally:
            payload.unlink(missing_ok=True)
        write_document(journal, {"phase": "login_required", "binding": binding})


def start(controller, directory):
    engine = controller.plan["bot"]["engine"]
    with controller.connection(directory) as (ssh, _request):
        try:
            ssh.command(["sudo", "-u", "reasoning-bot", "-H", *(["codex", "login", "status"] if engine == "codex"
                        else ["claude", "auth", "status"] )])
        except InstallError as exc:
            raise InstallError("Provider sign-in could not be verified. "
                               "Complete login as reasoning-bot in the owned console, then retry.") from exc
        ssh.command(["systemctl", "enable", "--now", "reasoning-bot.service"])
        ssh.command(["systemctl", "is-active", "--quiet", "reasoning-bot.service"])
        ssh.command(["passwd", "--lock", "reasoning-bot"])


def login_command(engine):
    return "sudo -u reasoning-bot -H " + LOGIN[engine]


def wheel_for_release(release):
    """Use the installed source artifact, never a Git credential or unverified download."""
    if not release or not re.fullmatch(r"[a-f0-9]{64}", release):
        raise InstallError("web cloud bots require a bundled installed release; use make bot-cloud from a checkout")
    root = Path("/opt/reasoning/releases") / release
    source = root / "source/reasoning-source.zip"
    if source.exists() or source.is_symlink():
        if source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 32 * 1024**2:
            raise InstallError("installed source artifact unavailable")
        return source
    wheels = list((root / "wheel").glob("*.whl"))
    if (len(wheels) != 1 or wheels[0].is_symlink() or not wheels[0].is_file()
            or not 1 <= wheels[0].stat().st_size <= 32 * 1024**2
            or not re.fullmatch(r"reasoning_ledger-[A-Za-z0-9_.-]{1,160}\.whl", wheels[0].name)):
        raise InstallError("installed release wheel unavailable")
    return wheels[0]
