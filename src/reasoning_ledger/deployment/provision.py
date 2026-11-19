"""Confirmed, single-owner DigitalOcean provisioning without blind write retries."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
import time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .digitalocean import DigitalOcean, ProviderRejected, ProviderUpdateRejected, compute_selection, dns_conflicts, selection
from .host import InstallError, canonical
from .private import locked_directory, read_document, write_document
from .ssh_host import PIN_REQUIREMENT, host_identity, pinned

KINDS = ("tag", "firewall", "droplet", "dns")
SSH_ACCESS_JOURNAL = "digitalocean-ssh-access.json"
BOT_SSH_EGRESS = {"protocol": "tcp", "ports": "22", "destinations": {"addresses": ["0.0.0.0/0"]}}
PLAN_KEYS = {
    "provider",
    "account_identity",
    "name",
    "region",
    "size",
    "image",
    "public_url",
    "dns_zone",
    "dns_record_name",
    "ssh_key_id",
    "ssh_public_key_fingerprint",
    "ssh_public_key_sha256",
    "admin_cidr",
    "resources",
    "quote",
    "requirements",
    "creates_resources",
}


def parameters(plan: dict) -> dict:
    return {
        **{name: plan[name] for name in ("name", "region", "size", "dns_zone", "ssh_key_id", "admin_cidr")},
        "origin": plan["public_url"],
        "maximum_hourly_usd": plan["quote"]["maximum_hourly_usd"],
    }


def validate_compute_plan(plan: dict) -> None:
    """Validate shared saved CPU host metadata, without a role-specific schema."""
    try:
        if not isinstance(plan, dict) or plan["provider"] != "digitalocean":
            raise ValueError
        network, maximum = compute_selection(
            **{name: plan[name] for name in ("name", "region", "size", "ssh_key_id", "admin_cidr")},
            maximum_hourly_usd=plan["quote"]["maximum_hourly_usd"],
        )
        if (
            plan["admin_cidr"] != str(network)
            or plan["image"] != "ubuntu-24-04-x64"
            or plan["creates_resources"] is not False
            or not re.fullmatch(r"(?:team|user):[A-Za-z0-9-]{16,80}", plan["account_identity"])
            or not re.fullmatch(r"[a-f0-9]{64}", plan["ssh_public_key_sha256"])
            or plan["ssh_public_key_fingerprint"] is not None
            and (not isinstance(plan["ssh_public_key_fingerprint"], str) or len(plan["ssh_public_key_fingerprint"]) > 256)
        ):
            raise ValueError
        resources = plan["resources"]
        if not isinstance(resources, dict) or set(resources) != {"vcpus", "memory_mib", "disk_gib"}:
            raise ValueError
        for key, minimum, limit in (("vcpus", 1, 1024), ("memory_mib", 2048, 8 * 1024**2), ("disk_gib", 10, 100000)):
            if type(resources[key]) is not int or not minimum <= resources[key] <= limit:
                raise ValueError
        quote = plan["quote"]
        if not isinstance(quote, dict) or set(quote) != {
            "compute_hourly_usd",
            "estimated_730_hours_usd",
            "maximum_hourly_usd",
            "excluded",
        }:
            raise ValueError
        hourly = Decimal(quote["compute_hourly_usd"])
        if not hourly.is_finite() or not 0 < hourly <= maximum or quote["estimated_730_hours_usd"] != str(hourly * 730):
            raise ValueError
        if quote["excluded"] != ["tax", "bandwidth overages", "backups", "storage", "other services"]:
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise InstallError("invalid DigitalOcean saved compute plan") from exc


def validate_plan(plan: dict) -> None:
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
            raise ValueError
        validate_compute_plan(plan)
        origin, hostname, _, _ = selection(**parameters(plan))
        record = "@" if hostname == plan["dns_zone"] else hostname[: -(len(plan["dns_zone"]) + 1)]
        if plan["public_url"] != origin or plan["dns_record_name"] != record:
            raise ValueError
        requirements = [
            "delegate the DNS zone to DigitalOcean",
            "allow HTTPS/HTTP and restricted SSH",
            "verify the new host SSH key",
            "install a verified public release",
        ]
        if plan["requirements"] not in (requirements, [*requirements[:2], PIN_REQUIREMENT, requirements[3]]):
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise InstallError("invalid DigitalOcean saved plan") from exc


def plan_identity(document: dict, *, validator=validate_plan) -> str:
    try:
        if (
            not isinstance(document, dict)
            or set(document) != {"format", "deployment_id", "created_at", "plan"}
            or type(document["format"]) is not int
            or document["format"] != 1
            or not re.fullmatch(r"[a-f0-9]{32}", document["deployment_id"])
            or type(document["created_at"]) is not int
            or not 0 < document["created_at"] < 2**63
        ):
            raise ValueError
        validator(document["plan"])
        return hashlib.sha256(canonical(document)).hexdigest()
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid DigitalOcean saved plan") from exc


def save_plan(path: Path, plan: dict, *, validator=validate_plan) -> dict:
    document = {"format": 1, "deployment_id": secrets.token_hex(16), "created_at": int(time.time()), "plan": plan}
    identity = plan_identity(document, validator=validator)
    write_document(path, document, new=True)
    return {"plan_id": identity, "plan_file": str(path.absolute()), "plan": plan}


# The public base image each supported plan image slug names, as DigitalOcean describes it once that image is retired.
BASE_IMAGES = {"ubuntu-24-04-x64": ("Ubuntu", "24.04 (LTS) x64")}


def original_image(image, slug: str) -> bool:
    """Whether a droplet still runs the base image its plan created it from.

    DigitalOcean replaces its public base images over time. A droplet created from a replaced image keeps that image,
    but the image record loses its slug and is marked retired. Only that exact retired base image is accepted in place
    of the slug; any other image, including a snapshot or a different release, is a changed droplet.
    """
    if not isinstance(image, dict):
        return False
    if image.get("slug") == slug:
        return True
    distribution, name = BASE_IMAGES.get(slug, (None, None))
    return (
        name is not None
        and image.get("slug") is None
        and image.get("type") == "base"
        and image.get("status") == "retired"
        and image.get("distribution") == distribution
        and image.get("name") == name
    )


def ssh_access_cidr(value: str) -> str:
    try:
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or network.prefixlen != 32:
            raise ValueError
        return str(network)
    except (TypeError, ValueError) as exc:
        raise InstallError("temporary SSH access requires one public IPv4 address with /32") from exc


def firewall_body(marker: str, admin_cidr: str | None) -> dict:
    public = {"addresses": ["0.0.0.0/0"]}
    ssh = [] if admin_cidr is None else [{"protocol": "tcp", "ports": "22", "sources": {"addresses": [admin_cidr]}}]
    return {
        "name": marker,
        "tags": [marker],
        "inbound_rules": [
            *ssh,
            *({"protocol": "tcp", "ports": port, "sources": public} for port in ("80", "443")),
        ],
        "outbound_rules": [
            *({"protocol": "tcp", "ports": port, "destinations": public} for port in ("53", "80", "443")),
            *({"protocol": "udp", "ports": port, "destinations": public} for port in ("53", "123")),
            {"protocol": "icmp", "ports": "0", "destinations": public},
        ],
    }


def normalized_rules(value, peer: str) -> list[bytes]:
    if not isinstance(value, list) or len(value) > 32:
        raise InstallError("DigitalOcean firewall rules changed or are invalid")
    normalized = []
    for row in value:
        if not isinstance(row, dict) or set(row) - {"protocol", "ports", "action", peer} or row.get("action", "allow") != "allow":
            raise InstallError("DigitalOcean firewall rules changed or are invalid")
        addresses = row.get(peer)
        if (
            not isinstance(addresses, dict)
            or any(key != "addresses" and item not in ([], None) for key, item in addresses.items())
            or not isinstance(addresses.get("addresses"), list)
            or any(not isinstance(item, str) for item in addresses["addresses"])
        ):
            raise InstallError("DigitalOcean firewall rules changed or are invalid")
        normalized.append(canonical({"protocol": row.get("protocol"), "ports": row.get("ports"), peer: sorted(addresses["addresses"])}))
    return sorted(normalized)


def _ssh_access_document(plan_id: str, cidr: str | None) -> dict:
    return {"format": 1, "plan_id": plan_id, "phase": "applied", "current": cidr, "desired": cidr}


def _validate_ssh_access(state: dict, plan_id: str) -> dict:
    try:
        if (
            not isinstance(state, dict)
            or set(state) != {"format", "plan_id", "phase", "current", "desired"}
            or type(state["format"]) is not int
            or state["format"] != 1
            or state["plan_id"] != plan_id
            or state["phase"] not in {"updating", "applied"}
        ):
            raise ValueError
        for key in ("current", "desired"):
            if state[key] is not None and ssh_access_cidr(state[key]) != state[key]:
                raise ValueError
        if state["phase"] == "applied" and state["current"] != state["desired"]:
            raise ValueError
        return state
    except (KeyError, TypeError, ValueError, InstallError) as exc:
        raise InstallError("invalid temporary SSH access journal; preserve the ownership directory") from exc


class Provisioner:
    kinds = KINDS
    identity_function = staticmethod(plan_identity)

    def __init__(self, provider: DigitalOcean, document: dict, *, ssh_cidr: str | None = None):
        self.provider, self.document = provider, document
        self.identity = self.identity_function(document)
        self.plan = document["plan"]
        self.session_ssh_cidr = ssh_access_cidr(ssh_cidr) if ssh_cidr is not None else None
        self.access_cidr = self.session_ssh_cidr if ssh_cidr is not None else self.plan["admin_cidr"]
        self.marker = "reasoning-" + document["deployment_id"]
        self.vm_name = "reasoning-" + self.plan["name"][:16] + "-" + document["deployment_id"]
        self.state, self.journal = None, None
        self.bot_controller_ssh = False

    def apply(self, directory: Path, confirmation: str) -> dict:
        if confirmation != self.identity:
            raise InstallError("confirm-plan must exactly match the saved plan ID; creation incurs ongoing provider charges")
        with locked_directory(directory) as directory:
            self._before_apply(directory)
            retirement = directory / "digitalocean-retirement.json"
            if retirement.exists() or retirement.is_symlink():
                raise InstallError("retirement intent exists; provisioning must not recreate or reactivate resources")
            self.journal = directory / "digitalocean.json"
            if self.journal.exists() or self.journal.is_symlink():
                self.state = read_document(self.journal)
                self._validate_state()
            else:
                self.state = {
                    "format": 1,
                    "plan_id": self.identity,
                    "resources": {kind: {"status": "not_started", "id": None} for kind in self.kinds},
                }
                write_document(self.journal, self.state, new=True)
            current = self._current_plan()
            if (
                current["account_identity"] != self.plan["account_identity"]
                or current["ssh_public_key_sha256"] != self.plan["ssh_public_key_sha256"]
                or current["resources"] != self.plan["resources"]
                or Decimal(current["quote"]["compute_hourly_usd"]) > Decimal(self.plan["quote"]["compute_hourly_usd"])
            ):
                raise InstallError("DigitalOcean account, key, resources, or price changed; no new resources will be created")
            self._dns_preflight()
            self._ensure("tag", "tags", "tag", {"name": self.marker})
            # Tag-based firewall membership may already expose a VM whose
            # creation response was lost. Recover its identity by read before
            # checking that the firewall contains only our known VM.
            if self.state["resources"]["droplet"]["status"] == "request_pending":
                matches = self._collect("droplet")
                if len(matches) > 1:
                    raise InstallError("multiple matching DigitalOcean resources; reconcile ownership without creating another")
                if matches:
                    self._check("droplet", matches[0])
                    self.state["resources"]["droplet"].update(status="created", id=matches[0]["id"])
                    write_document(self.journal, self.state)
            expected = self._firewall_body()
            firewall = self._ensure("firewall", "firewalls", "firewall", expected)
            if not self._firewall_ready(firewall):
                return self._result("firewall_pending")
            self._dns_preflight()
            droplet = self._ensure("droplet", "droplets", "droplet", self._droplet_body())
            # Acceptance is not readiness; inspect the actual provider resource.
            droplet = self.provider.get(f"droplets/{droplet['id']}").get("droplet")
            self._check("droplet", droplet)
            if droplet.get("status") != "active":
                if droplet.get("status") != "new":
                    raise InstallError("owned DigitalOcean Droplet is not active; inspect it without replacing it")
                return self._result("droplet_pending")
            address = self._address(droplet)
            applied = self.provider.pages(f"droplets/{droplet['id']}/firewalls", "firewalls")
            if not applied:
                return self._result("firewall_pending")
            if len(applied) != 1 or applied[0].get("id") != self.state["resources"]["firewall"]["id"]:
                raise InstallError("Droplet firewall association is missing or changed; DNS will not be published")
            self._check("firewall", applied[0])
            if not self._firewall_ready(applied[0]):
                return self._result("firewall_pending")
            self._publish_dns(address)
            return self._result("ssh_key_pinned" if pinned(self.plan) else "ssh_verification_required", address=address)

    def _current_plan(self):
        return self.provider.plan(**parameters(self.plan), check_dns=False, pin_ssh_host_key=pinned(self.plan))

    def _firewall_body(self):
        body = firewall_body(self.marker, self.access_cidr)
        if self.bot_controller_ssh:
            body["outbound_rules"].append(BOT_SSH_EGRESS)
        return body

    def _publish_dns(self, address):
        record = {"type": "A", "name": self.plan["dns_record_name"], "data": address, "ttl": 300}
        self._ensure("dns", f"domains/{self.plan['dns_zone']}/records", "domain_record", record, address=address)
        # Verify the resulting records, not only the creation receipt.
        self._ensure("dns", f"domains/{self.plan['dns_zone']}/records", "domain_record", record, address=address)

    def _validate_state(self):
        try:
            if (
                set(self.state) != {"format", "plan_id", "resources"}
                or type(self.state["format"]) is not int
                or self.state["format"] != 1
                or self.state["plan_id"] != self.identity
                or not isinstance(self.state["resources"], dict)
                or set(self.state["resources"]) != set(self.kinds)
            ):
                raise ValueError
            for index, kind in enumerate(self.kinds):
                row = self.state["resources"][kind]
                if (
                    not isinstance(row, dict)
                    or set(row) != {"status", "id"}
                    or not isinstance(row["status"], str)
                    or row["status"] not in {"not_started", "request_pending", "created"}
                ):
                    raise ValueError
                if row["status"] == "created":
                    self._identifier(kind, row["id"])
                elif row["id"] is not None:
                    raise ValueError
                if row["status"] != "not_started" and any(
                    self.state["resources"][prior]["status"] != "created" for prior in self.kinds[:index]
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise InstallError(
                "invalid provisioning journal or a different saved plan; do not discard existing resource ownership"
            ) from exc

    def _identifier(self, kind, identifier):
        if kind == "tag":
            valid = identifier == self.marker
        elif kind == "firewall":
            try:
                valid = isinstance(identifier, str) and str(UUID(identifier)) == identifier
            except ValueError:
                valid = False
        else:
            valid = type(identifier) is int and 0 < identifier < 2**63
        if not valid:
            raise InstallError("invalid owned DigitalOcean resource identity")
        return identifier

    def _collect(self, kind) -> list:
        if kind == "tag":
            return [row for row in self.provider.pages("tags", "tags") if row.get("name") == self.marker]
        if kind == "firewall":
            return [
                row
                for row in self.provider.pages("firewalls", "firewalls")
                if (row.get("name") == self.marker or isinstance(row.get("tags"), list) and self.marker in row["tags"])
            ]
        if kind == "droplet":
            return self.provider.pages("droplets", "droplets", tag_name=self.marker)
        return dns_conflicts(
            self.provider.pages(f"domains/{self.plan['dns_zone']}/records", "domain_records"), self.plan["dns_record_name"]
        )

    def _dns_preflight(self):
        if self.state["resources"]["dns"]["status"] == "not_started" and self._collect("dns"):
            raise InstallError("selected DNS hostname is already in use; existing records will not be replaced")

    def _before_creation(self, kind):
        pass

    def _before_apply(self, directory):
        self._load_ssh_access(directory, initialize=self.session_ssh_cidr)

    def _load_ssh_access(self, directory: Path, *, initialize: str | None = None) -> None:
        path = directory / SSH_ACCESS_JOURNAL
        if not path.exists() and not path.is_symlink():
            if initialize is not None:
                write_document(path, _ssh_access_document(self.identity, initialize), new=True)
                self.access_cidr = initialize
            else:
                self.access_cidr = self.plan["admin_cidr"]
            return
        state = _validate_ssh_access(read_document(path), self.identity)
        if state["phase"] != "applied":
            raise InstallError("temporary SSH firewall access is uncertain; reconcile it before using the server")
        if initialize is not None and state["current"] != initialize:
            raise InstallError("temporary SSH access does not match this setup session; reconcile it before launch")
        self.access_cidr = state["current"]

    def _ensure(self, kind, path, field, body, *, address=None):
        row = self.state["resources"][kind]
        matches = self._collect(kind)
        if len(matches) > 1:
            raise InstallError("multiple matching DigitalOcean resources; reconcile ownership without creating another")
        if matches:
            if row["status"] == "not_started":
                raise InstallError("refusing to adopt an existing unmanaged DigitalOcean resource")
            existing = matches[0]
            self._check(kind, existing, address=address)
            identifier = self._identifier(kind, existing.get("name") if kind == "tag" else existing.get("id"))
            if row["status"] == "created" and row["id"] != identifier:
                raise InstallError("owned DigitalOcean resource identity changed; it will not be replaced")
            row.update(status="created", id=identifier)
            write_document(self.journal, self.state)
            return existing
        if row["status"] != "not_started":
            raise InstallError(
                "DigitalOcean creation or ownership is uncertain; keep the journal and resume reads, never blindly retry creation"
            )
        self._before_creation(kind)
        row["status"] = "request_pending"
        write_document(self.journal, self.state)
        try:
            response = self.provider.post(path, body)
        except ProviderRejected:
            row["status"] = "not_started"
            write_document(self.journal, self.state)
            raise
        created = response.get(field)
        self._check(kind, created, address=address)
        row.update(status="created", id=self._identifier(kind, created.get("name") if kind == "tag" else created.get("id")))
        write_document(self.journal, self.state)
        return created

    def _check(self, kind, row, *, address=None):
        try:
            self._check_resource(kind, row, address=address)
        except (KeyError, TypeError, ValueError) as exc:
            raise InstallError("invalid owned DigitalOcean resource response") from exc

    def _forbidden_droplet_features(self):
        return {"backups", "ipv6"}

    def _check_resource(self, kind, row, *, address=None):
        if not isinstance(row, dict):
            raise InstallError("invalid owned DigitalOcean resource response")
        identifier = self._identifier(kind, row.get("name") if kind == "tag" else row.get("id"))
        expected_id = self.state["resources"][kind]["id"]
        if expected_id is not None and expected_id != identifier:
            raise InstallError("owned DigitalOcean resource identity changed; it will not be replaced")
        valid = False
        if kind == "tag":
            valid = row.get("name") == self.marker
        elif kind == "firewall":
            if "purpose" not in self.plan:
                # Preserve the explicitly supported controller SSH egress
                # variant when another operator opens/closes inbound SSH.
                # No other rule/membership drift is accepted.
                baseline = firewall_body(self.marker, self.access_cidr)["outbound_rules"]
                outbound = normalized_rules(row.get("outbound_rules"), "destinations")
                if outbound == normalized_rules(baseline, "destinations"):
                    self.bot_controller_ssh = False
                elif outbound == normalized_rules([*baseline, BOT_SSH_EGRESS], "destinations"):
                    self.bot_controller_ssh = True
            expected = self._firewall_body()
            valid = (
                row.get("name") == self.marker
                and row.get("tags") == [self.marker]
                and isinstance(row.get("droplet_ids", []), list)
                and all(type(item) is int and item > 0 for item in row.get("droplet_ids", []))
                and set(row.get("droplet_ids", [])) <= {self.state["resources"]["droplet"]["id"]}
                and normalized_rules(row.get("inbound_rules"), "sources") == normalized_rules(expected["inbound_rules"], "sources")
                and normalized_rules(row.get("outbound_rules"), "destinations")
                == normalized_rules(expected["outbound_rules"], "destinations")
            )
        elif kind == "droplet":
            valid = (
                row.get("name") == self.vm_name
                and row.get("tags") == [self.marker]
                and isinstance(row.get("region"), dict)
                and row["region"].get("slug") == self.plan["region"]
                and original_image(row.get("image"), self.plan["image"])
                and row.get("size_slug") == self.plan["size"]
                and all(type(row.get(key)) is int for key in ("memory", "vcpus", "disk"))
                and row.get("memory") == self.plan["resources"]["memory_mib"]
                and row.get("vcpus") == self.plan["resources"]["vcpus"]
                and row.get("disk") == self.plan["resources"]["disk_gib"]
                and isinstance(row.get("features"), list)
                and all(isinstance(item, str) for item in row["features"])
                and not self._forbidden_droplet_features() & set(row["features"])
                and row.get("volume_ids") == []
            )
        elif kind == "dns":
            valid = (
                row.get("type") == "A"
                and row.get("name") == self.plan["dns_record_name"]
                and row.get("data") == address
                and type(row.get("ttl")) is int
                and row["ttl"] == 300
            )
        if not valid:
            raise InstallError("owned DigitalOcean resource configuration changed; it will not be overwritten or replaced")

    @staticmethod
    def _firewall_ready(row) -> bool:
        if (
            not isinstance(row.get("status"), str)
            or row["status"] not in {"waiting", "succeeded"}
            or not isinstance(row.get("pending_changes"), list)
        ):
            raise InstallError("DigitalOcean firewall application is failed or unverified")
        return row["status"] == "succeeded" and not row["pending_changes"]

    @staticmethod
    def _address(droplet) -> str:
        try:
            addresses = [row["ip_address"] for row in droplet["networks"]["v4"] if row["type"] == "public"]
            if len(addresses) != 1 or not ipaddress.IPv4Address(addresses[0]).is_global:
                raise ValueError
            return addresses[0]
        except (KeyError, TypeError, ValueError) as exc:
            raise InstallError("DigitalOcean Droplet lacks a verified public IPv4 address") from exc

    def _droplet_body(self):
        configuration = {
            "ssh_pwauth": False,
            "disable_root": False,
            "write_files": [
                {
                    "path": "/etc/reasoning-provisioning.json",
                    "permissions": "0600",
                    "owner": "root:root",
                    "content": canonical({"plan_id": self.identity, "deployment_id": self.document["deployment_id"]}).decode(),
                }
            ],
        }
        if pinned(self.plan):
            # Persist before the paid VM intent. Lost responses must never
            # cause a replacement host key or an unverified SSH connection.
            value = host_identity(
                self.journal.parent,
                self.identity,
                self.document["deployment_id"],
                allow_new=self.state["resources"]["droplet"]["status"] == "not_started",
            )
            configuration.update(
                ssh_deletekeys=True,
                ssh_genkeytypes=[],
                ssh_keys={"ed25519_private": value["private_key"], "ed25519_public": value["public_key"]},
            )
        return {
            "name": self.vm_name,
            "region": self.plan["region"],
            "size": self.plan["size"],
            "image": self.plan["image"],
            "ssh_keys": [self.plan["ssh_key_id"]],
            "backups": False,
            "ipv6": False,
            "monitoring": False,
            "with_droplet_agent": True,
            "tags": [self.marker],
            "user_data": "#cloud-config\n" + json.dumps(configuration),
        }

    def _result(self, phase, *, address=None):
        return {
            "plan_id": self.identity,
            "phase": phase,
            "deployment_tag": self.marker,
            "resources": {kind: self.state["resources"][kind]["id"] for kind in KINDS},
            "provider_charges_possible": self.state["resources"]["droplet"]["status"] != "not_started",
            "public_url": self.plan["public_url"],
            "ssh_host": address,
            "application_installed": False,
            "https_ready": False,
            "control_panel_url": "https://cloud.digitalocean.com/droplets"
            + ("/" + str(self.state["resources"]["droplet"]["id"]) if self.state["resources"]["droplet"]["id"] is not None else ""),
            "next_step": "do-install uses the original pinned SSH identity; preserve digitalocean-ssh.json with ownership state"
            if address and pinned(self.plan)
            else "verify SSH host key in the provider console, then do-install with original ownership and exact release"
            if address
            else ("resume the same saved plan and journal after provider readiness; do not discard ownership state"),
        }


def update_ssh_access(
    provider: DigitalOcean,
    document: dict,
    directory: Path,
    desired_cidr: str | None,
    *,
    wait_seconds: int = 60,
) -> dict:
    """Set or remove one ownership-bound /32 SSH rule with uncertain-update reconciliation."""
    if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
        raise InstallError("temporary SSH access wait must be between zero and 60 seconds")
    desired = ssh_access_cidr(desired_cidr) if desired_cidr is not None else None
    instance = Provisioner(provider, document)
    with locked_directory(directory) as state_dir:
        ownership_path = state_dir / "digitalocean.json"
        if not ownership_path.exists() and not ownership_path.is_symlink():
            raise InstallError("temporary SSH access requires an existing provisioning journal")
        instance.journal = ownership_path
        instance.state = read_document(ownership_path)
        instance._validate_state()
        row = instance.state["resources"]["firewall"]
        if row["status"] == "request_pending":
            raise InstallError("firewall creation is uncertain; resume the original launch before changing SSH access")

        access_path = state_dir / SSH_ACCESS_JOURNAL
        if access_path.exists() or access_path.is_symlink():
            access = _validate_ssh_access(read_document(access_path), instance.identity)
        else:
            access = _ssh_access_document(instance.identity, instance.plan["admin_cidr"])
            write_document(access_path, access, new=True)

        if row["status"] == "not_started":
            access = _ssh_access_document(instance.identity, desired)
            write_document(access_path, access)
            return {"phase": "open" if desired else "closed", "ssh_cidr": desired, "firewall_id": None}
        if row["status"] != "created":
            raise InstallError("invalid owned firewall state")

        firewall_id = instance._identifier("firewall", row["id"])
        remote = provider.get("firewalls/" + firewall_id).get("firewall")

        def matches(cidr):
            instance.access_cidr = cidr
            try:
                instance._check("firewall", remote)
                return True
            except InstallError:
                return False

        candidates = [access["current"]]
        if access["phase"] == "updating" and access["desired"] not in candidates:
            candidates.append(access["desired"])
        elif access["phase"] == "applied" and access["current"] is not None:
            # Another authorized operator may have closed SSH, or this is a
            # new operator's first journal. Only an exact owned closed firewall
            # can refresh local evidence; other open addresses remain drift.
            candidates.append(None)
        actual = [candidate for candidate in candidates if matches(candidate)]
        if len(actual) != 1:
            raise InstallError("owned firewall or membership does not match the temporary SSH access journal; "
                               "ensure the token includes droplet:read and tag:read as well as firewall:read/update; "
                               "no update was attempted")
        actual = actual[0]
        if access["phase"] == "applied" and actual is None and access["current"] is not None:
            access = _ssh_access_document(instance.identity, None)
            write_document(access_path, access)
        if access["phase"] == "updating":
            if actual == access["desired"]:
                access = _ssh_access_document(instance.identity, actual)
                write_document(access_path, access)
            elif desired != access["desired"]:
                raise InstallError("finish reconciling the previous temporary SSH update before choosing a new address")
        if access["phase"] != "applied" or desired != access["current"]:
            previous = actual
            access = {"format": 1, "plan_id": instance.identity, "phase": "updating", "current": previous, "desired": desired}
            write_document(access_path, access)
            instance.access_cidr = desired
            body = instance._firewall_body()
            try:
                updated = provider.update_firewall(firewall_id, body).get("firewall")
                instance._check("firewall", updated)
            except ProviderUpdateRejected:
                write_document(access_path, _ssh_access_document(instance.identity, previous))
                raise
            write_document(access_path, _ssh_access_document(instance.identity, desired))
            remote = updated

        instance.access_cidr = desired
        deadline = time.monotonic() + wait_seconds
        while True:
            instance._check("firewall", remote)
            if instance._firewall_ready(remote):
                phase = "open" if desired else "closed"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                phase = "opening" if desired else "closing"
                break
            time.sleep(min(2, remaining))
            remote = provider.get("firewalls/" + firewall_id).get("firewall")
        return {"phase": phase, "ssh_cidr": desired, "firewall_id": firewall_id}
