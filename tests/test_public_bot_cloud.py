"""Cloud bot acceptance with fake DO/SSH; no paid hardware or provider calls.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from test_public_bots import fixture as bot_fixture
from test_public_bots import member
from test_public_provisioning import ProviderFixture
from test_public_provisioning import provisioning as provisioning
from test_public_server import installation as installation

from reasoning_ledger.deployment import bot_cloud, bot_cloud_access, bot_cloud_cli, bot_cloud_host
from reasoning_ledger.deployment.digitalocean import DigitalOcean
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import locked_directory, read_document
from reasoning_ledger.deployment.provision import BOT_SSH_EGRESS, update_ssh_access
from reasoning_ledger.server import bot_cloud_api
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.backup import BackupError, backup, create_key, restore
from reasoning_ledger.server.bot_cloud_api import CloudBots, QuoteInput
from reasoning_ledger.server.bot_store import BotStore
from reasoning_ledger.server.store import Store, StoreError


class Provider(ProviderFixture):
    def handle(self, request):
        path = request.url.path.removeprefix("/v2/")
        if path == "account/keys":
            self.requests.append(request)
            if request.method == "POST":
                self.key = {**json.loads(request.content), "id": 123, "fingerprint": "fixture"}
                return httpx.Response(201, json={"ssh_key": self.key})
            return httpx.Response(200, json={"ssh_keys": [self.key]})
        return super().handle(request)


@pytest.fixture
def cloud(tmp_path):
    provider = Provider()
    spec = {"engine": "codex", "models": ["default"], "server": "https://chat.example.org", "cas": True,
            "lean": False, "wheel_sha256": hashlib.sha256(b"wheel fixture").hexdigest()}
    quote = provider.client.compute_quote(name="bot", region="fra1", size="s-2vcpu-4gb",
                                         admin_cidr="139.59.1.9/32", maximum_hourly_usd="0.10")
    with locked_directory(tmp_path / "host") as directory:
        document = bot_cloud.prepare(provider.client, directory, quote, spec)
    yield provider, spec, quote, directory, document
    provider.client.close()


def test_quote_is_read_only_and_tool_size_requirements_are_enforced():
    provider = Provider()
    quote = provider.client.compute_quote(name="bot", region="fra1", size="s-2vcpu-4gb",
                                         admin_cidr="139.59.1.9/32", maximum_hourly_usd="0.10")
    assert not provider.writes
    assert {request.url.path for request in provider.requests} == {"/v2/account", "/v2/sizes"}
    spec = {"engine": "claude", "models": ["default"], "server": "https://chat.example.org", "cas": True,
            "lean": True, "wheel_sha256": "a" * 64}
    with pytest.raises(InstallError, match="8 GiB"):
        bot_cloud.check_size(quote, spec)
    provider.client.close()


def test_create_pins_ssh_and_has_only_restricted_ssh_inbound(cloud):
    provider, _spec, _quote, directory, document = cloud
    controller = bot_cloud.BotProvisioner(provider.client, document)
    result = controller.apply(directory, controller.identity)
    assert result["ssh_host"] == "139.59.1.2"
    firewall = json.loads(next(row for row in provider.writes if row.url.path == "/v2/firewalls").content)
    assert firewall["inbound_rules"] == [{"protocol": "tcp", "ports": "22", "sources": {"addresses": ["139.59.1.9/32"]}}]
    droplet = json.loads(next(row for row in provider.writes if row.url.path == "/v2/droplets").content)
    config = json.loads(droplet["user_data"].split("\n", 1)[1])
    assert config["ssh_keys"]["ed25519_public"] == read_document(directory / "digitalocean-ssh.json")["public_key"]
    assert provider.token not in json.dumps(droplet)
    assert "chat.example.org" not in json.dumps(droplet) and "rnb_" not in json.dumps(droplet)
    count = len(provider.writes)
    assert controller.apply(directory, controller.identity) == result
    assert len(provider.writes) == count


@pytest.mark.parametrize("kind", ["tag", "firewall", "droplet"])
@pytest.mark.parametrize("failure", ["lost_before", "lost_after"])
def test_uncertain_creation_never_duplicates_paid_hardware(cloud, kind, failure):
    provider, _, _, directory, document = cloud
    controller = bot_cloud.BotProvisioner(provider.client, document)
    provider.fail[kind] = failure
    with pytest.raises(InstallError):
        controller.apply(directory, controller.identity)
    count = len(provider.writes)
    if failure == "lost_before":
        with pytest.raises(InstallError, match="uncertain"):
            controller.apply(directory, controller.identity)
        assert len(provider.writes) == count
    else:
        assert controller.apply(directory, controller.identity)["droplet_id"] == 456
        assert sum(row.url.path == "/v2/droplets" for row in provider.writes) == 1


def test_authority_is_rechecked_before_cloud_mutations(cloud):
    provider, _, _, directory, document = cloud

    def refuse():
        raise InstallError("revoked")

    controller = bot_cloud.BotProvisioner(provider.client, document, authorize=refuse)
    count = len(provider.writes)
    with pytest.raises(InstallError, match="revoked"):
        controller.apply(directory, controller.identity)
    assert len(provider.writes) == count


def test_ssh_never_uses_keyscan_or_provider_supplied_host(cloud, monkeypatch):
    provider, _, _, directory, document = cloud
    controller = bot_cloud.BotProvisioner(provider.client, document)
    controller.apply(directory, controller.identity)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bot_cloud.subprocess, "run", run)
    with controller.connection(directory) as (ssh, _request):
        ssh.command(["systemctl", "is-active", "--quiet", "reasoning-bot"])
    args, kwargs = calls[0]
    assert "StrictHostKeyChecking=yes" in args and "IdentitiesOnly=yes" in args
    assert "root@139.59.1.2" in args and kwargs["timeout"] == 30
    provider.extra_firewalls.append(copy.deepcopy(provider.firewalls[0]))
    with pytest.raises(InstallError, match="attachment"), controller.connection(directory):
        pytest.fail("must not SSH to changed VM association")
    assert len(calls) == 1


@pytest.fixture
def web_cloud(tmp_path, monkeypatch):
    fixture = bot_fixture.__wrapped__(tmp_path)
    store, owner = fixture[:2]
    wheel = tmp_path / "reasoning_ledger-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"trusted fixture wheel")
    monkeypatch.setattr(bot_cloud, "wheel_for_release", lambda _: wheel)
    monkeypatch.setattr(bot_cloud_api, "current_public_ipv4", lambda: "139.59.1.9/32")
    remote = Provider()

    def new_provider(token):
        assert token == remote.token
        return DigitalOcean(token, transport=httpx.MockTransport(remote.handle))

    monkeypatch.setattr(bot_cloud_api, "DigitalOcean", new_provider)
    controller = CloudBots(store, Config(store.data_dir, "https://chat.example.org", inference_enabled=False))
    body = QuoteInput(token=remote.token, name="web-bot", engine="codex", size="s-2vcpu-4gb", maximum_hourly_usd="0.10", cas=True)
    public = controller.quote(owner["user_id"], owner["organization_id"], body)
    yield fixture, controller, remote, public
    remote.client.close()


def test_web_quote_owner_scoped_encrypted_and_has_no_bearer_or_cloud_side_effect(web_cloud):
    fixture, controller, remote, public = web_cloud
    store, owner = fixture[:2]
    user, org = owner["user_id"], owner["organization_id"]
    assert not remote.writes and not BotStore(store).list(user, org, personal=True)
    assert remote.token not in json.dumps(public) and "credentials" not in public
    job = controller.read(user, org, public["id"])
    assert remote.token not in json.dumps(job)
    assert controller.credentials(job)["digitalocean"] == remote.token
    with pytest.raises(StoreError) as error:
        controller.read(member(fixture), org, public["id"])
    assert error.value.status == 404
    with pytest.raises(StoreError):
        controller.quote(member(fixture, "viewer"), org, QuoteInput(token=remote.token, name="viewer", engine="codex",
                                                                  maximum_hourly_usd="0.10"))


def test_web_confirm_resume_install_login_and_start_are_separate(web_cloud, monkeypatch):
    fixture, controller, remote, public = web_cloud
    user, org = fixture[1]["user_id"], fixture[1]["organization_id"]
    tasks, installs = BackgroundTasks(), []
    with pytest.raises(StoreError, match="exact"):
        controller.launch(user, org, public["id"], "create", "", tasks)
    assert not remote.writes
    monkeypatch.setattr(bot_cloud, "install", lambda *args, **kwargs: installs.append(args))
    controller.capacity.acquire()
    controller.perform(user, org, public["id"], "create")
    job = controller.read(user, org, public["id"])
    assert job["phase"] == "login_required" and len(installs) == 1 and job["bot_id"]
    bot = BotStore(fixture[0]).list(user, org, personal=True)[0]
    assert bot["public_access"] and bot["scopes"] == sorted(bot_cloud.SCOPES)
    secret = controller.credentials(job)["bot"]
    assert secret not in json.dumps(controller.public(job))
    count = len(remote.writes)
    started = []
    monkeypatch.setattr(bot_cloud, "start", lambda *args: started.append(args))
    controller.capacity.acquire()
    controller.perform(user, org, public["id"], "start")
    assert controller.read(user, org, public["id"])["phase"] == "ready"
    assert "console_password" not in controller.credentials(controller.read(user, org, public["id"]))
    assert len(started) == 1 and len(remote.writes) == count


def test_revoked_bot_blocks_resuming_cloud_creation(web_cloud, monkeypatch):
    fixture, controller, remote, public = web_cloud
    user, org = fixture[1]["user_id"], fixture[1]["organization_id"]
    monkeypatch.setattr(bot_cloud, "install", lambda *args, **kwargs: None)
    controller.capacity.acquire()
    controller.perform(user, org, public["id"], "create")
    job = controller.read(user, org, public["id"])
    BotStore(fixture[0]).revoke(user, org, job["bot_id"], personal=True)
    count = len(remote.writes)
    controller.capacity.acquire()
    controller.perform(user, org, public["id"], "create")
    assert controller.read(user, org, public["id"])["phase"] == "setup_failed"
    assert len(remote.writes) == count


def test_interrupted_web_job_exposes_resume_not_permanent_spinner(web_cloud):
    fixture, controller, _, public = web_cloud
    job = controller.read(fixture[1]["user_id"], fixture[1]["organization_id"], public["id"])
    job["phase"] = "installing"
    assert controller.public(job)["phase"] == "setup_failed"
    directory = controller.directory(job["user"], job["org"], job["id"])
    with locked_directory(directory / "operation"):
        assert controller.public(job)["phase"] == "installing"


def test_cloud_journals_survive_encrypted_backup_and_are_path_bounded(web_cloud, tmp_path):
    fixture, controller, remote, public = web_cloud
    store, owner = fixture[:2]
    key, archive = tmp_path / "backup-key", tmp_path / "backup.rnbk"
    create_key(key)
    result = backup(store.data_dir, archive, key)
    assert remote.token.encode() not in archive.read_bytes()
    target = tmp_path / "restored"
    restore(archive, key, result["sha256"], target, source_stopped=True)
    restored = CloudBots(Store(target), controller.config)
    job = restored.read(owner["user_id"], owner["organization_id"], public["id"])
    assert restored.credentials(job)["digitalocean"] == remote.token
    directory = restored.directory(owner["user_id"], owner["organization_id"], public["id"])
    assert hashlib.sha256((directory / "artifact" / job["wheel"]).read_bytes()).hexdigest() == job["spec"]["wheel_sha256"]
    path = controller.directory(owner["user_id"], owner["organization_id"], public["id"]) / "unexpected"
    path.write_bytes(b"not a supported journal")
    path.chmod(0o600)
    with pytest.raises(BackupError, match="unsupported"):
        backup(store.data_dir, tmp_path / "bad.rnbk", key)


def test_cloud_owned_vm_journals_and_original_ssh_keys_survive_backup(web_cloud, tmp_path, monkeypatch):
    fixture, controller, _, public = web_cloud
    store, owner = fixture[:2]
    user, org = owner["user_id"], owner["organization_id"]
    monkeypatch.setattr(bot_cloud, "install", lambda *args, **kwargs: None)
    controller.capacity.acquire()
    controller.perform(user, org, public["id"], "create")
    directory = controller.directory(user, org, public["id"])
    original = {path.name: path.read_bytes() for path in (directory / "host").iterdir() if path.name != "provision.lock"}
    key, archive = tmp_path / "cloud-backup-key", tmp_path / "cloud-backup.rnbk"
    create_key(key)
    result = backup(store.data_dir, archive, key)
    target = tmp_path / "cloud-restored"
    restore(archive, key, result["sha256"], target, source_stopped=True)
    restored = CloudBots(Store(target), controller.config)
    recovered = restored.directory(user, org, public["id"]) / "host"
    assert original == {path.name: path.read_bytes() for path in recovered.iterdir()}


def test_install_never_transmits_changed_artifact_or_enrollment(cloud, tmp_path, monkeypatch):
    provider, _, _, directory, document = cloud
    controller = bot_cloud.BotProvisioner(provider.client, document)
    controller.apply(directory, controller.identity)
    wheel = tmp_path / "reasoning_ledger-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wrong artifact")
    calls = []
    monkeypatch.setattr(bot_cloud.SSH, "command", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(bot_cloud.SSH, "upload", lambda *args: calls.append(args))
    credential = "rnb_" + "a" * 43
    with pytest.raises(InstallError, match="wheel changed"):
        bot_cloud.install(controller, directory, wheel, credential)
    assert not calls
    wheel.write_bytes(b"wheel fixture")
    bot_cloud.install(controller, directory, wheel, credential)
    assert read_document(directory / "installation.json")["phase"] == "login_required"
    count = len(calls)
    with pytest.raises(InstallError, match="inputs changed"):
        bot_cloud.install(controller, directory, wheel, "rnb_" + "b" * 43)
    assert len(calls) == count


def test_bot_tokens_cannot_access_cloud_controller(tmp_path):
    app = create_app(Config(tmp_path / "app", "https://chat.example.org", inference_enabled=False))
    with TestClient(app) as client:
        response = client.get("/api/organizations/" + "a" * 32 + "/my-cloud-bots",
                              headers={"Authorization": "Bearer rnb_" + "b" * 43})
        assert response.status_code == 401


def test_authenticated_web_quote_and_explicit_confirmation_use_the_shared_controller(installation, tmp_path, monkeypatch):
    _config, _store, client, owner = installation
    wheel = tmp_path / "reasoning_ledger-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"fixture wheel")
    remote = Provider()
    monkeypatch.setattr(bot_cloud, "wheel_for_release", lambda _: wheel)
    monkeypatch.setattr(bot_cloud_api, "current_public_ipv4", lambda: "139.59.1.9/32")
    monkeypatch.setattr(bot_cloud_api, "DigitalOcean", lambda token: DigitalOcean(token, transport=httpx.MockTransport(remote.handle)))
    monkeypatch.setattr(bot_cloud, "install", lambda *args, **kwargs: None)
    root = f"/api/organizations/{owner['organization_id']}/my-cloud-bots"
    response = client.post(root + "/quote", json={"token": remote.token, "engine": "claude", "name": "test-bot",
        "size": "s-2vcpu-4gb", "maximum_hourly_usd": "0.10"})
    assert response.status_code == 201, response.text
    assert "allow_other_users" not in response.json()
    # Who can use the bot is chosen in My bots, not at setup.
    assert client.post(root + "/quote", json={"token": remote.token, "engine": "claude", "name": "test-bot",
        "size": "s-2vcpu-4gb", "maximum_hourly_usd": "0.10", "allow_other_users": True}).status_code == 422
    assert not remote.writes
    path = root + "/" + response.json()["id"]
    assert client.post(path + "/create", json={"confirmation": ""}).status_code == 400
    assert not remote.writes
    assert client.post(path + "/create", json={"confirmation": "create bot test-bot"}).status_code == 202
    assert client.get(root).json()[0]["phase"] == "login_required"
    assert remote.token not in client.get(root).text
    access = client.post(path + "/console-access", json={})
    assert access.status_code == 200
    assert access.json()["username"] == "reasoning-bot"
    assert len(access.json()["password"]) == 32 and access.json()["command"] == "claude auth login"
    assert access.json()["password"] not in client.get(root).text
    remote.client.close()


def test_cli_cloud_pairing_does_not_need_provider_cli_on_local_computer():
    result = bot_cloud_cli.checked_agent("codex", "/workspace", True)
    assert result["engine"] == "codex" and result["installed_cas"] == []
    assert result["local_execution_available"] is False


def test_cloud_runner_leaves_who_can_use_the_bot_to_the_server_setting():
    spec = {"engine": "codex", "models": ["default"], "server": "https://chat.example.org", "cas": False,
            "lean": False, "wheel_sha256": "a" * 64}
    bot_cloud.validate_spec(spec)
    # A retained setup's runner flag is accepted but never passed on; My bots decides who can use the bot.
    for retained in (True, False):
        bot_cloud.validate_spec({**spec, "allow_other_users": retained})
        assert "--allow-other-users" not in bot_cloud_host.runner_command({**spec, "allow_other_users": retained})
    with pytest.raises(InstallError):
        bot_cloud.validate_spec({**spec, "allow_other_users": "yes"})
    with pytest.raises(InstallError):
        bot_cloud.validate_spec({**spec, "shared": True})


def test_shared_wizard_waits_through_normal_vm_readiness_but_not_unknown_creation():
    responses = iter([{"ssh_host": None}, {"ssh_host": None}, {"ssh_host": "139.59.1.2"}])
    sleeps, calls = [], []

    def apply(directory, confirmation):
        calls.append((directory, confirmation))
        return next(responses)

    controller = SimpleNamespace(identity="retained-plan", apply=apply)
    assert bot_cloud.provision_ready(controller, "same-state", wait=sleeps.append)["ssh_host"] == "139.59.1.2"
    assert sleeps == [5, 5] and calls == [("same-state", "retained-plan")] * 3

    def uncertain(*_args):
        raise InstallError("creation uncertain")

    controller.apply = uncertain
    with pytest.raises(InstallError, match="uncertain"):
        bot_cloud.provision_ready(controller, "same-state", wait=sleeps.append)
    assert sleeps == [5, 5]


def test_installer_uses_same_pins_as_existing_lean_image():
    from pathlib import Path

    inputs = json.loads((Path(__file__).parents[1] / "images/lean-mathlib/inputs.json").read_bytes())
    assert inputs["lean_version"] == bot_cloud_host.LEAN_VERSION
    assert inputs["mathlib_revision"] == bot_cloud_host.MATHLIB_REV
    assert inputs["mathlib_sha256"] == bot_cloud_host.MATHLIB_SHA


def test_enable_web_controller_adds_only_outbound_ssh_and_operator_close_preserves_it(provisioning):
    remote, saved, document, instance, directory = provisioning
    instance.apply(directory, saved["plan_id"])
    previous = copy.deepcopy(remote.firewalls[0])
    result = bot_cloud_access.enable(remote.client, document, directory)
    assert result["outbound_ssh_enabled"] and not result["inbound_ssh_changed"]
    assert remote.firewalls[0]["inbound_rules"] == previous["inbound_rules"]
    assert remote.firewalls[0]["outbound_rules"] == [*previous["outbound_rules"], BOT_SSH_EGRESS]
    count = len(remote.writes)
    bot_cloud_access.enable(remote.client, document, directory)
    assert len(remote.writes) == count
    update_ssh_access(remote.client, document, directory, None, wait_seconds=0)
    assert BOT_SSH_EGRESS in remote.firewalls[0]["outbound_rules"]
    assert all(row["ports"] != "22" for row in remote.firewalls[0]["inbound_rules"])


def test_enable_web_controller_refuses_unrelated_firewall_drift(provisioning):
    remote, saved, document, instance, directory = provisioning
    instance.apply(directory, saved["plan_id"])
    remote.firewalls[0]["outbound_rules"].append({"protocol": "tcp", "ports": "25", "destinations": {"addresses": ["0.0.0.0/0"]}})
    count = len(remote.writes)
    with pytest.raises(InstallError, match="configuration changed"):
        bot_cloud_access.enable(remote.client, document, directory)
    assert len(remote.writes) == count


def test_enable_web_controller_lost_update_is_reconciled_by_read(provisioning):
    remote, saved, document, instance, directory = provisioning
    instance.apply(directory, saved["plan_id"])
    remote.fail["firewall_update"] = "lost_after"
    with pytest.raises(InstallError, match="uncertain"):
        bot_cloud_access.enable(remote.client, document, directory)
    count = len(remote.writes)
    assert bot_cloud_access.enable(remote.client, document, directory)["outbound_ssh_enabled"]
    assert len(remote.writes) == count
