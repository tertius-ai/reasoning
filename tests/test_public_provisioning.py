"""Mock-provider creation/recovery checks, not evidence of a real paid deployment."""

import copy
import fcntl
import json
import os
from pathlib import Path

import httpx
import pytest

from reasoning_ledger.deployment import cli
from reasoning_ledger.deployment.digitalocean import DigitalOcean, ProviderUpdateRejected, dns_conflicts
from reasoning_ledger.deployment.host import InstallError, canonical
from reasoning_ledger.deployment.private import read_document, read_private, write_document
from reasoning_ledger.deployment.provision import Provisioner, plan_identity, save_plan, update_ssh_access


@pytest.mark.parametrize("path,category,scope", [
    ("account", "account", "account:read"),
    ("domains/private.example/records/123", "DNS", "domain:read"),
    ("droplets/456", "Droplet", "droplet:read"),
    ("droplets/456/firewalls", "firewall association", "firewall:read, droplet:read, and tag:read"),
    ("firewalls/private-id", "firewall", "firewall:read"),
    ("tags/private-tag", "tag", "tag:read"),
    ("account/keys/123", "SSH key", "ssh_key:read"),
    ("sizes", "size", "sizes:read"),
    ("unknown/private-id", "resource", "the endpoint's read"),
])
@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_denied_reads_report_safe_category_status_and_scope_without_provider_details(path, category, scope, status):
    secret = "fixture-private-" + "x" * 32
    transport = httpx.MockTransport(lambda _: httpx.Response(status, json={"message": secret + path}))
    provider = DigitalOcean(secret, transport=transport)
    try:
        with pytest.raises(InstallError) as failure:
            provider.get(path)
        message = str(failure.value)
        assert f"DigitalOcean {category} read failed (HTTP {status})" in message
        assert scope in message
        assert secret not in message and "private.example" not in message and "private-id" not in message
    finally:
        provider.close()


class ProviderFixture:
    """Stateful API fixture with explicit acceptance-versus-observation failures."""

    def __init__(self):
        self.token = "fixture-private-" + "x" * 32
        self.requests, self.tags, self.firewalls, self.droplets, self.records = [], [], [], [], []
        self.account = {"uuid": "a" * 40, "status": "active", "email_verified": True}
        self.size = {
            "slug": "s-2vcpu-4gb",
            "available": True,
            "regions": ["fra1"],
            "price_hourly": 0.035,
            "memory": 4096,
            "vcpus": 2,
            "disk": 80,
        }
        self.key = {"id": 123, "public_key": "ssh-ed25519 fixture-public-key", "fingerprint": "fixture fingerprint"}
        self.fail, self.before_post, self.droplet_status, self.firewall_status = {}, None, "active", "succeeded"
        self.extra_firewalls = []
        self.association_visible = True
        self.get_id_override = None
        self.client = DigitalOcean(self.token, transport=httpx.MockTransport(self.handle))

    @property
    def writes(self):
        return [request for request in self.requests if request.method != "GET"]

    def create(self, kind, body):
        row = copy.deepcopy(body)
        if kind == "tag":
            row["resources"] = {"count": 0}
            self.tags.append(row)
        elif kind == "firewall":
            row.update(id="11111111-2222-4333-8444-555555555555", status=self.firewall_status, pending_changes=[], droplet_ids=[])
            self.firewalls.append(row)
        elif kind == "droplet":
            row = {
                "id": 456,
                "name": body["name"],
                "region": {"slug": body["region"]},
                "image": {"slug": body["image"]},
                "size_slug": body["size"],
                "memory": 4096,
                "vcpus": 2,
                "disk": 80,
                "tags": body["tags"],
                "features": ["private_networking"],
                "volume_ids": [],
                "status": self.droplet_status,
                "networks": {"v4": [{"type": "public", "ip_address": "139.59.1.2"}], "v6": []},
            }
            self.droplets.append(row)
            self.firewalls[0]["droplet_ids"] = [row["id"]]
        else:
            row["id"] = 789
            self.records.append(row)
        return row

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.digitalocean.com" and request.url.scheme == "https"
        assert request.headers["Authorization"] == "Bearer " + self.token
        path = request.url.path.removeprefix("/v2/")
        if request.method == "GET":
            if path == "account":
                body = {"account": self.account}
            elif path == "sizes":
                body = {"sizes": [self.size]}
            elif path == "account/keys/123":
                body = {"ssh_key": self.key}
            elif path == "domains/example.org":
                body = {"domain": {"name": "example.org"}}
            elif path == "domains/example.org/records":
                body = {"domain_records": self.records}
            elif path == "tags":
                body = {"tags": self.tags}
            elif path == "firewalls":
                body = {"firewalls": self.firewalls}
            elif path == "firewalls/11111111-2222-4333-8444-555555555555":
                body = {"firewall": self.firewalls[0]}
            elif path == "droplets":
                assert request.url.params["tag_name"].startswith("reasoning-")
                body = {"droplets": [row for row in self.droplets if request.url.params["tag_name"] in row["tags"]]}
            elif path == "droplets/456":
                row = copy.deepcopy(self.droplets[0])
                if self.get_id_override is not None:
                    row["id"] = self.get_id_override
                body = {"droplet": row}
            else:
                assert path == "droplets/456/firewalls"
                body = {"firewalls": self.firewalls + self.extra_firewalls if self.association_visible else []}
            return httpx.Response(200, json=body)
        if request.method == "PUT":
            assert path == "firewalls/11111111-2222-4333-8444-555555555555"
            failure = self.fail.pop("firewall_update", None)
            if failure == "rejected":
                return httpx.Response(403, json={"message": self.token})
            body = json.loads(request.content)
            row = {**copy.deepcopy(body), **{key: self.firewalls[0][key]
                                             for key in ("id", "status", "pending_changes", "droplet_ids")}}
            self.firewalls[0] = row
            if failure == "lost_after":
                raise httpx.ReadTimeout(self.token, request=request)
            return httpx.Response(200, json={"firewall": row})
        assert request.method == "POST"
        kind, field = {
            "tags": ("tag", "tag"),
            "firewalls": ("firewall", "firewall"),
            "droplets": ("droplet", "droplet"),
            "domains/example.org/records": ("dns", "domain_record"),
        }[path]
        body = json.loads(request.content)
        assert self.token not in json.dumps(body)
        if self.before_post:
            self.before_post(kind, body)
        failure = self.fail.pop(kind, None)
        if failure == "rejected":
            return httpx.Response(422, json={"message": self.token})
        if failure == "lost_before":
            raise httpx.ReadTimeout(self.token, request=request)
        created = self.create(kind, body)
        if failure == "lost_after":
            raise httpx.ReadTimeout(self.token, request=request)
        if failure == "invalid_json":
            return httpx.Response(202, content=b"invalid json " + self.token.encode())
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.example"}, json={"message": self.token})
        return httpx.Response(201 if kind in {"tag", "dns"} else 202, json={field: created})

    def plan(self):
        return self.client.plan(
            name="mathematics",
            region="fra1",
            size="s-2vcpu-4gb",
            origin="https://chat.example.org",
            dns_zone="example.org",
            ssh_key_id=123,
            admin_cidr="203.0.113.10/32",
            maximum_hourly_usd="0.10",
        )


@pytest.fixture
def provisioning(tmp_path):
    remote = ProviderFixture()
    saved = save_plan(tmp_path / "plan.json", remote.plan())
    document = read_document(tmp_path / "plan.json")
    provisioner = Provisioner(remote.client, document)
    directory = tmp_path / "ownership"
    yield remote, saved, document, provisioner, directory
    remote.client.close()


def apply(provisioning):
    _remote, saved, _document, provisioner, directory = provisioning
    return provisioner.apply(directory, saved["plan_id"])


def test_confirmed_provisioning_creates_only_scoped_resources_and_keeps_installation_separate(provisioning):
    remote, saved, document, provisioner, directory = provisioning

    def pending(kind, _body):
        journal = read_document(directory / "digitalocean.json")
        assert journal["plan_id"] == saved["plan_id"]
        assert journal["resources"][kind] == {"status": "request_pending", "id": None}

    remote.before_post = pending
    result = apply(provisioning)
    assert result["phase"] == "ssh_verification_required" and result["ssh_host"] == "139.59.1.2"
    assert result["application_installed"] is False and result["https_ready"] is False and result["provider_charges_possible"] is True
    assert [request.url.path for request in remote.writes] == [
        "/v2/tags",
        "/v2/firewalls",
        "/v2/droplets",
        "/v2/domains/example.org/records",
    ]
    firewall = json.loads(remote.writes[1].content)
    assert firewall["tags"] == [provisioner.marker]
    assert firewall["inbound_rules"][0] == {"protocol": "tcp", "ports": "22", "sources": {"addresses": ["203.0.113.10/32"]}}
    assert {row["ports"] for row in firewall["inbound_rules"]} == {"22", "80", "443"}
    assert all(row["ports"] != "8766" for row in firewall["inbound_rules"])
    droplet = json.loads(remote.writes[2].content)
    assert droplet["backups"] is False and droplet["ipv6"] is False and droplet["ssh_keys"] == [123]
    assert json.loads(droplet["user_data"].split("\n", 1)[1])["ssh_pwauth"] is False
    assert remote.token not in json.dumps(document) + (directory / "digitalocean.json").read_text()
    assert (directory / "digitalocean.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "provision.lock").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert apply(provisioning) == result and len(remote.writes) == 4


def test_owned_temporary_ssh_access_rotates_and_closes_without_plan_drift(provisioning):
    remote, saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    opened = update_ssh_access(remote.client, document, directory, "198.51.100.7/32")
    assert opened["phase"] == "open" and opened["ssh_cidr"] == "198.51.100.7/32"
    assert remote.firewalls[0]["inbound_rules"][0]["sources"]["addresses"] == ["198.51.100.7/32"]
    closed = update_ssh_access(remote.client, document, directory, None)
    assert closed["phase"] == "closed" and {row["ports"] for row in remote.firewalls[0]["inbound_rules"]} == {"80", "443"}
    assert Provisioner(remote.client, document).apply(directory, saved["plan_id"])["phase"] == "ssh_verification_required"


def test_new_operator_observes_exact_owned_closed_firewall_without_cloud_write(provisioning):
    remote, _saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    remote.firewalls[0]["inbound_rules"] = [rule for rule in remote.firewalls[0]["inbound_rules"] if rule["ports"] != "22"]
    before = len(remote.writes)
    assert update_ssh_access(remote.client, document, directory, None)["phase"] == "closed"
    assert len(remote.writes) == before
    assert read_document(directory / "digitalocean-ssh-access.json")["current"] is None
    assert update_ssh_access(remote.client, document, directory, "198.51.100.7/32")["phase"] == "open"
    assert len(remote.writes) == before + 1


@pytest.mark.parametrize("change", ["filtered-membership", "other-open-address", "outbound-rule", "uncertain-update"])
def test_closed_ssh_reconciliation_never_bypasses_ownership_or_uncertain_update(provisioning, change):
    remote, saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    access = {"format": 1, "plan_id": saved["plan_id"], "phase": "applied",
              "current": document["plan"]["admin_cidr"], "desired": document["plan"]["admin_cidr"]}
    remote.firewalls[0]["inbound_rules"] = [rule for rule in remote.firewalls[0]["inbound_rules"] if rule["ports"] != "22"]
    if change == "filtered-membership":
        remote.firewalls[0].update(tags=[], droplet_ids=[])
    elif change == "other-open-address":
        remote.firewalls[0]["inbound_rules"].append({"protocol": "tcp", "ports": "22",
                                                  "sources": {"addresses": ["198.51.100.8/32"]}})
    elif change == "outbound-rule":
        remote.firewalls[0]["outbound_rules"][0]["ports"] = "0"
    else:
        access.update(phase="updating", desired="198.51.100.9/32")
    path = directory / "digitalocean-ssh-access.json"
    write_document(path, access, new=True)
    before, original = len(remote.writes), path.read_bytes()
    with pytest.raises(InstallError, match="no update was attempted"):
        update_ssh_access(remote.client, document, directory, "198.51.100.7/32")
    assert len(remote.writes) == before and path.read_bytes() == original


def test_uncertain_ssh_access_update_reconciles_without_duplicate_write(provisioning):
    remote, _saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    remote.fail["firewall_update"] = "lost_after"
    with pytest.raises(InstallError, match="uncertain"):
        update_ssh_access(remote.client, document, directory, None)
    writes = len(remote.writes)
    assert update_ssh_access(remote.client, document, directory, None)["phase"] == "closed"
    assert len(remote.writes) == writes


def test_rejected_ssh_access_update_preserves_previous_rule(provisioning):
    remote, _saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    remote.fail["firewall_update"] = "rejected"
    with pytest.raises(InstallError, match="firewall:update"):
        update_ssh_access(remote.client, document, directory, None)
    assert update_ssh_access(remote.client, document, directory, "203.0.113.10/32")["phase"] == "open"


def test_ssh_access_reports_pending_until_firewall_change_is_applied(provisioning):
    remote, _saved, document, _provisioner, directory = provisioning
    apply(provisioning)
    remote.firewalls[0].update(status="waiting", pending_changes=[{"droplet_id": 456, "status": "waiting"}])
    assert update_ssh_access(remote.client, document, directory, None, wait_seconds=0)["phase"] == "closing"
    remote.firewalls[0].update(status="succeeded", pending_changes=[])
    assert update_ssh_access(remote.client, document, directory, None, wait_seconds=0)["phase"] == "closed"


@pytest.mark.parametrize("kind", ["tag", "firewall", "droplet", "dns"])
@pytest.mark.parametrize("failure", ["lost_after", "invalid_json", "redirect"])
def test_lost_or_untrusted_creation_receipt_resumes_by_identity_without_duplicate_writes(provisioning, kind, failure):
    remote, saved, document, _provisioner, directory = provisioning
    remote.fail[kind] = failure
    with pytest.raises(InstallError) as error:
        apply(provisioning)
    assert remote.token not in str(error.value)
    assert read_document(directory / "digitalocean.json")["resources"][kind]["status"] == "request_pending"
    # A fresh process loads the actual private journal and observes the resource.
    result = Provisioner(remote.client, document).apply(directory, saved["plan_id"])
    assert result["phase"] == "ssh_verification_required" and len(remote.writes) == 4


@pytest.mark.parametrize("kind", ["tag", "firewall", "droplet", "dns"])
def test_uncertain_absent_creation_never_blindly_retries_even_after_restart(provisioning, kind):
    remote, saved, document, _provisioner, directory = provisioning
    remote.fail[kind] = "lost_before"
    with pytest.raises(InstallError):
        apply(provisioning)
    count = len(remote.writes)
    with pytest.raises(InstallError, match="uncertain"):
        Provisioner(remote.client, document).apply(directory, saved["plan_id"])
    assert len(remote.writes) == count


@pytest.mark.parametrize("kind", ["tag", "firewall", "droplet", "dns"])
def test_definitively_rejected_creation_can_be_retried_without_abandoning_prior_resources(provisioning, kind):
    remote, _saved, _document, _provisioner, directory = provisioning
    remote.fail[kind] = "rejected"
    with pytest.raises(InstallError, match="rejected") as error:
        apply(provisioning)
    assert remote.token not in str(error.value)
    assert read_document(directory / "digitalocean.json")["resources"][kind] == {"status": "not_started", "id": None}
    assert apply(provisioning)["phase"] == "ssh_verification_required" and len(remote.writes) == 5


@pytest.mark.parametrize("change", ["account", "team", "price", "memory", "key", "availability", "unverified"])
def test_live_plan_revalidation_refuses_changed_identity_key_resources_and_increased_quote_before_any_write(provisioning, change):
    remote, _saved, _document, _provisioner, _directory = provisioning
    if change == "account":
        remote.account["uuid"] = "b" * 40
    elif change == "team":
        remote.account["team"] = {"uuid": "b" * 40}
    elif change == "price":
        remote.size["price_hourly"] = 0.04  # Below the ceiling, but above the confirmed quote.
    elif change == "memory":
        remote.size["memory"] = 8192
    elif change == "key":
        remote.key["public_key"] += "-changed"
    elif change == "availability":
        remote.size["available"] = False
    else:
        remote.account["email_verified"] = False
    with pytest.raises(InstallError):
        apply(provisioning)
    assert not remote.writes


def test_lower_live_price_is_allowed_but_does_not_change_the_saved_plan(provisioning):
    remote, saved, document, _provisioner, _directory = provisioning
    remote.size["price_hourly"] = 0.02
    assert apply(provisioning)["phase"] == "ssh_verification_required"
    assert plan_identity(document) == saved["plan_id"] and document["plan"]["quote"]["compute_hourly_usd"] == "0.035"


def test_wrong_confirmation_has_no_local_journal_or_provider_reads(provisioning):
    remote, _saved, _document, provisioner, directory = provisioning
    count = len(remote.requests)
    with pytest.raises(InstallError, match="confirm-plan"):
        provisioner.apply(directory, "0" * 64)
    assert not directory.exists() and len(remote.requests) == count


def test_provider_readiness_is_not_confused_with_creation_acceptance(provisioning):
    remote, _saved, _document, _provisioner, _directory = provisioning
    remote.firewall_status = "waiting"
    result = apply(provisioning)
    assert result["phase"] == "firewall_pending" and result["provider_charges_possible"] is False and len(remote.writes) == 2
    remote.firewalls[0]["status"] = "succeeded"
    remote.droplet_status = "new"
    result = apply(provisioning)
    assert result["phase"] == "droplet_pending" and result["provider_charges_possible"] is True and len(remote.writes) == 3
    assert not remote.records
    remote.droplets[0]["status"] = "active"
    assert apply(provisioning)["phase"] == "ssh_verification_required" and len(remote.writes) == 4


@pytest.mark.parametrize("change", ["extra", "missing", "waiting", "failed", "open_ssh"])
def test_dns_is_not_published_until_exact_firewall_association_and_rules_are_verified(provisioning, change):
    remote, _saved, _document, _provisioner, _directory = provisioning

    def modify(kind, _body):
        if kind != "droplet":
            return
        if change == "extra":
            remote.extra_firewalls = [{"id": "66666666-2222-4333-8444-555555555555"}]
        elif change == "missing":
            remote.association_visible = False
        elif change in {"waiting", "failed"}:
            remote.firewalls[0]["status"] = change
            remote.firewalls[0]["pending_changes"] = [{"droplet_id": 456, "status": "waiting"}]
        else:
            remote.firewalls[0]["inbound_rules"][0]["sources"]["addresses"] = ["0.0.0.0/0"]

    remote.before_post = modify
    if change in {"waiting", "missing"}:
        assert apply(provisioning)["phase"] == "firewall_pending"
    else:
        with pytest.raises(InstallError):
            apply(provisioning)
    assert len(remote.writes) == 3 and not remote.records


@pytest.mark.parametrize("when", ["before", "after_firewall", "after_vm"])
def test_unrelated_dns_is_never_replaced_even_if_it_appears_during_provisioning(provisioning, when):
    remote, _saved, _document, _provisioner, _directory = provisioning
    existing = {"id": 999, "type": "CNAME", "name": "chat", "data": "existing.example.org", "ttl": 600}
    if when == "before":
        remote.records.append(existing)
    else:
        remote.before_post = lambda kind, _body: (
            remote.records.append(existing)
            if kind
            == {
                "after_firewall": "firewall",
                "after_vm": "droplet",
            }[when]
            else None
        )
    with pytest.raises(InstallError):
        apply(provisioning)
    assert remote.records == [existing]
    assert len(remote.writes) == {"before": 0, "after_firewall": 2, "after_vm": 3}[when]


@pytest.mark.parametrize("kind", ["tag", "firewall", "droplet", "dns"])
def test_missing_owned_resources_are_not_implicitly_replaced(provisioning, kind):
    remote, _saved, _document, _provisioner, _directory = provisioning
    apply(provisioning)
    getattr(remote, {"tag": "tags", "firewall": "firewalls", "droplet": "droplets", "dns": "records"}[kind]).clear()
    with pytest.raises(InstallError, match="uncertain"):
        apply(provisioning)
    assert len(remote.writes) == 4


def test_unmanaged_marker_collision_is_not_adopted(provisioning):
    remote, _saved, _document, provisioner, _directory = provisioning
    remote.tags.append({"name": provisioner.marker, "resources": {"count": 0}})
    with pytest.raises(InstallError, match="unmanaged"):
        apply(provisioning)
    assert not remote.writes


def test_duplicate_uncertain_droplets_require_manual_reconciliation_not_another_creation(provisioning):
    remote, _saved, _document, _provisioner, _directory = provisioning
    remote.fail["droplet"] = "lost_after"
    with pytest.raises(InstallError):
        apply(provisioning)
    duplicate = copy.deepcopy(remote.droplets[0])
    duplicate["id"] = 457
    remote.droplets.append(duplicate)
    with pytest.raises(InstallError, match="multiple matching"):
        apply(provisioning)
    assert len(remote.writes) == 3


@pytest.mark.parametrize("change", ["identity", "configuration", "ttl", "record_type"])
def test_existing_dns_drift_is_refused_without_overwrite(provisioning, change):
    remote, _saved, _document, _provisioner, _directory = provisioning
    apply(provisioning)
    remote.records[0][{"identity": "id", "configuration": "data", "ttl": "ttl", "record_type": "type"}[change]] = {
        "identity": 999,
        "configuration": "139.59.1.3",
        "ttl": 600,
        "record_type": "AAAA",
    }[change]
    with pytest.raises(InstallError):
        apply(provisioning)
    assert len(remote.writes) == 4


def test_plan_is_private_exclusive_canonical_and_does_not_accept_duplicate_keys(provisioning, tmp_path):
    _remote, saved, document, _provisioner, _directory = provisioning
    path = Path(saved["plan_file"])
    assert path.stat().st_mode & 0o777 == 0o600 and read_document(path) == document
    with pytest.raises(InstallError, match="already exists"):
        write_document(path, document, new=True)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_bytes(canonical(document).replace(b'"format":1', b'"format":1,"format":1'))
    duplicate.chmod(0o600)
    with pytest.raises(InstallError, match="canonical JSON"):
        read_document(duplicate)


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink", "fifo"])
def test_private_operator_files_refuse_unsafe_permissions_and_types_without_blocking(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "fifo":
        os.mkfifo(path, mode=0o600)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "outside")
    else:
        path.write_bytes(b"fixture secret")
        path.chmod(0o644 if kind == "public" else 0o600)
        if kind == "hardlink":
            os.link(path, tmp_path / "second")
    with pytest.raises(InstallError):
        read_private(path)


def test_provisioning_lock_prevents_concurrent_provider_access(provisioning):
    remote, _saved, _document, _provisioner, directory = provisioning
    directory.mkdir(mode=0o700)
    fd = os.open(directory / "provision.lock", os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = len(remote.requests)
        with pytest.raises(InstallError, match="another process"):
            apply(provisioning)
        assert len(remote.requests) == before
    finally:
        os.close(fd)


@pytest.mark.parametrize("change", ["different_plan", "invalid_status", "invalid_id", "out_of_order"])
def test_invalid_journal_cannot_rebind_resources_or_issue_provider_requests(provisioning, change):
    remote, _saved, _document, _provisioner, directory = provisioning
    apply(provisioning)
    path = directory / "digitalocean.json"
    state = read_document(path)
    if change == "different_plan":
        state["plan_id"] = "0" * 64
    elif change == "invalid_status":
        state["resources"]["droplet"]["status"] = []
    elif change == "invalid_id":
        state["resources"]["droplet"]["id"] = True
    else:
        state["resources"]["firewall"] = {"status": "not_started", "id": None}
    write_document(path, state)
    before = len(remote.requests)
    with pytest.raises(InstallError):
        apply(provisioning)
    assert len(remote.requests) == before


def test_cli_requires_exact_saved_plan_confirmation_before_loading_token_or_creating_client(provisioning, monkeypatch, capsys):
    _remote, saved, _document, _provisioner, directory = provisioning
    monkeypatch.setattr(cli, "DigitalOcean", lambda _token: pytest.fail("provider must not be initialized"))
    with pytest.raises(SystemExit) as error:
        cli.main(
            ["do-apply", "--plan", saved["plan_file"], "--state-dir", str(directory), "--token-file", "absent", "--confirm-plan", "0" * 64]
        )
    assert error.value.code == 2 and "confirm-plan" in capsys.readouterr().err and not directory.exists()


def test_do_plan_cli_saves_then_do_apply_uses_explicit_confirmation(tmp_path, monkeypatch, capsys):
    remote = ProviderFixture()
    token = tmp_path / "token"
    token.write_text(remote.token)
    token.chmod(0o600)

    def client(value):
        assert value == remote.token
        return DigitalOcean(value, transport=httpx.MockTransport(remote.handle))

    monkeypatch.setattr(cli, "DigitalOcean", client)
    plan_file = tmp_path / "plan.json"
    assert (
        cli.main(
            [
                "do-plan",
                "--token-file",
                str(token),
                "--name",
                "mathematics",
                "--region",
                "fra1",
                "--size",
                "s-2vcpu-4gb",
                "--public-url",
                "https://chat.example.org",
                "--dns-zone",
                "example.org",
                "--ssh-key-id",
                "123",
                "--admin-cidr",
                "203.0.113.10/32",
                "--maximum-hourly-usd",
                "0.10",
                "--output",
                str(plan_file),
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert not remote.writes
    assert (
        cli.main(
            [
                "do-apply",
                "--plan",
                str(plan_file),
                "--state-dir",
                str(tmp_path / "ownership"),
                "--token-file",
                str(token),
                "--confirm-plan",
                saved["plan_id"],
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert remote.token not in output and json.loads(output)["phase"] == "ssh_verification_required" and len(remote.writes) == 4
    remote.client.close()


def test_temporary_ssh_access_cli_requires_plan_confirmation_and_exact_action(provisioning, tmp_path, monkeypatch, capsys):
    remote, saved, _document, _provisioner, directory = provisioning
    apply(provisioning)
    token = tmp_path / "token"
    token.write_text(remote.token)
    token.chmod(0o600)
    monkeypatch.setattr(cli, "DigitalOcean", lambda value: DigitalOcean(value, transport=httpx.MockTransport(remote.handle)))
    arguments = ["do-ssh-access", "--token-file", str(token), "--plan", saved["plan_file"],
                 "--state-dir", str(directory), "--confirm-plan", saved["plan_id"]]
    assert cli.main([*arguments, "--close"]) == 0
    assert json.loads(capsys.readouterr().out)["phase"] == "closed"
    with pytest.raises(SystemExit) as error:
        cli.main([*arguments[:-1], "0" * 64, "--open-cidr", "198.51.100.7/32"])
    assert error.value.code == 2 and "confirm-plan" in capsys.readouterr().err


def test_nested_cli_can_return_typed_firewall_scope_rejection(provisioning, tmp_path, monkeypatch):
    remote, saved, _document, _provisioner, directory = provisioning
    apply(provisioning)
    remote.fail["firewall_update"] = "rejected"
    token = tmp_path / "token"
    token.write_text(remote.token)
    token.chmod(0o600)
    monkeypatch.setattr(cli, "DigitalOcean", lambda value: DigitalOcean(value, transport=httpx.MockTransport(remote.handle)))
    arguments = [
        "do-ssh-access", "--token-file", str(token), "--plan", saved["plan_file"],
        "--state-dir", str(directory), "--confirm-plan", saved["plan_id"], "--close",
    ]

    with pytest.raises(ProviderUpdateRejected, match="firewall:update"):
        cli.main(arguments, raise_errors=True)


@pytest.mark.parametrize("change", ["format", "id", "time", "provider", "creates", "resources", "quote", "origin", "cidr", "key"])
def test_malformed_saved_plans_never_become_provisioners(provisioning, change):
    remote, _saved, document, _provisioner, _directory = provisioning
    before = len(remote.requests)
    document = copy.deepcopy(document)
    if change == "format":
        document["format"] = True
    elif change == "id":
        document["deployment_id"] = "../../outside"
    elif change == "time":
        document["created_at"] = None
    elif change == "provider":
        document["plan"]["provider"] = "https://attacker.example"
    elif change == "creates":
        document["plan"]["creates_resources"] = True
    elif change == "resources":
        document["plan"]["resources"]["memory_mib"] = "4096"
    elif change == "quote":
        document["plan"]["quote"]["compute_hourly_usd"] = "NaN"
    elif change == "origin":
        document["plan"]["public_url"] = "https://chat.example.org\n{bad}"
    elif change == "cidr":
        document["plan"]["admin_cidr"] = "::/0"
    else:
        document["plan"]["ssh_key_id"] = True
    with pytest.raises(InstallError):
        Provisioner(remote.client, document)
    assert len(remote.requests) == before


@pytest.mark.parametrize("type,name,target", [("NS", "math", "chat.math"), ("CNAME", "math", "chat.math"), ("NS", "chat", "chat")])
def test_dns_delegations_and_ancestor_aliases_block_provisioning(type, name, target):
    row = {"type": type, "name": name, "data": "other.example.org"}
    assert dns_conflicts([row], target) == [row]


def test_apex_zone_nameservers_are_not_confused_with_subdomain_delegation():
    assert dns_conflicts([{"name": "@", "type": "NS", "data": "ns1.digitalocean.com"}], "@") == []


@pytest.mark.parametrize("wait", [-1, 61])
def test_readiness_wait_is_bounded_and_validated_before_provider_access(provisioning, wait):
    remote, saved, _document, provisioner, directory = provisioning
    before = len(remote.requests)
    with pytest.raises(InstallError, match="wait"):
        cli.provision_until_ready(provisioner, directory, saved["plan_id"], wait)
    assert len(remote.requests) == before and not directory.exists()


def test_readiness_wait_resumes_without_recreating_resources(provisioning, monkeypatch):
    remote, saved, _document, provisioner, directory = provisioning
    remote.droplet_status = "new"
    elapsed = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed[0])

    def advance(seconds):
        assert seconds <= 10
        elapsed[0] += seconds
        remote.droplets[0]["status"] = "active"

    monkeypatch.setattr(cli.time, "sleep", advance)
    result = cli.provision_until_ready(provisioner, directory, saved["plan_id"], 60)
    assert result["phase"] == "ssh_verification_required" and elapsed[0] == 10 and len(remote.writes) == 4


def test_readiness_timeout_retains_ownership_and_reports_unverified_application(provisioning, monkeypatch):
    remote, saved, _document, provisioner, directory = provisioning
    remote.droplet_status = "new"
    elapsed = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    result = cli.provision_until_ready(provisioner, directory, saved["plan_id"], 15)
    assert result["phase"] == "droplet_pending" and result["provider_charges_possible"] is True
    assert result["https_ready"] is False and elapsed[0] == 15 and len(remote.writes) == 3
    assert read_document(directory / "digitalocean.json")["resources"]["droplet"]["id"] == 456


@pytest.mark.parametrize("change", ["symlink", "public_write"])
def test_unsafe_provisioning_directory_is_refused_before_provider_reads(provisioning, tmp_path, change):
    remote, _saved, _document, _provisioner, directory = provisioning
    if change == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        directory.symlink_to(outside)
    else:
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
    before = len(remote.requests)
    with pytest.raises(InstallError, match="private directory"):
        apply(provisioning)
    assert len(remote.requests) == before


def test_private_files_refuse_symlinked_parent_paths(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    secret = outside / "secret"
    secret.write_bytes(b"fixture private value")
    secret.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(InstallError, match="protected directories"):
        read_private(link / "secret")


RETIRED_UBUNTU = {"id": 247516813, "slug": None, "type": "base", "status": "retired", "distribution": "Ubuntu",
                  "name": "24.04 (LTS) x64", "public": False}


@pytest.mark.parametrize("image,accepted", [
    (RETIRED_UBUNTU, True),
    ({**RETIRED_UBUNTU, "status": "available"}, False),
    ({**RETIRED_UBUNTU, "type": "snapshot"}, False),
    ({**RETIRED_UBUNTU, "name": "22.04 (LTS) x64"}, False),
    ({**RETIRED_UBUNTU, "distribution": "Debian"}, False),
    ({**RETIRED_UBUNTU, "slug": "ubuntu-22-04-x64"}, False),
    (None, False),
])
def test_a_droplet_on_its_retired_original_base_image_is_still_the_owned_droplet(provisioning, image, accepted):
    # DigitalOcean drops the slug from a replaced public base image; the droplet itself has not changed.
    remote, *_ = provisioning
    apply(provisioning)
    remote.droplets[0]["status"] = "active"
    assert apply(provisioning)["phase"] == "ssh_verification_required"
    remote.droplets[0]["image"] = image
    if accepted:
        assert apply(provisioning)["phase"] == "ssh_verification_required"
    else:
        with pytest.raises(InstallError, match="configuration changed"):
            apply(provisioning)


def test_direct_droplet_inspection_must_match_the_journaled_identity_before_dns_publication(provisioning):
    remote, _saved, _document, _provisioner, _directory = provisioning
    remote.get_id_override = 457
    with pytest.raises(InstallError, match="identity changed"):
        apply(provisioning)
    assert len(remote.writes) == 3 and not remote.records
