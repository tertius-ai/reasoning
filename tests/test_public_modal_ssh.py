"""Pinned owned-server Modal uploads use provider/SSH fixtures, not cloud writes."""

import json
from pathlib import Path

import pytest
from test_public_compute import IMAGE
from test_public_deployment import bootstrap, pack, payload  # noqa: F401
from test_public_server_installation import execute, installation  # noqa: F401

from reasoning_ledger.deployment import digitalocean
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.modal_setup import owned_modal_remote
from reasoning_ledger.deployment.private import read_private, write_private


@pytest.fixture
def bundle(tmp_path, payload):  # noqa: F811
    content = {**payload, "modal-requirements.txt": payload["requirements.txt"]}
    return pack(tmp_path / "release.tar.gz", content,
                manifest_change=lambda manifest: manifest.update(installation_features=["modal-relay-v1"]))


@pytest.fixture
def prepared(installation, monkeypatch):  # noqa: F811
    controller, _, provider, _, _, _, directory, document, bundle, tmp_path = installation
    execute(installation)
    token, identity = tmp_path / "operator-token", tmp_path / "operator-identity"
    write_private(token, provider.token.encode())
    write_private(identity, b"nonfunctional SSH fixture")

    class Provider:
        def __getattr__(self, name):
            return getattr(provider.client, name)

        def close(self):
            pass

    monkeypatch.setattr(digitalocean, "DigitalOcean", lambda _: Provider())
    config = {"token_file": str(token), "identity": str(identity), "public_url": document["plan"]["public_url"]}
    value = {"format": 1, "release_id": controller.binding["release_id"], "setup_id": "e" * 64,
             "public_url": config["public_url"], "images": [IMAGE], "workers": []}
    calls, contents = [], []
    receipt = {"setup_id": value["setup_id"], "catalog_installed": True, "relays_active": False}

    def command(arguments, *, capture=False):
        calls.append(arguments)
        if arguments[0] == "scp":
            path = Path(arguments[-2])
            assert path.stat().st_mode & 0o777 == 0o600
            contents.append(json.loads(read_private(path)))
        if capture:
            return json.dumps(receipt).encode()

    return config, document, directory, bundle, value, command, calls, contents, provider, receipt


@pytest.mark.parametrize("independent_operator", [False, True])
@pytest.mark.parametrize("backups", [False, True])
def test_modal_upload_uses_original_pinned_host_and_protected_config_without_cloud_creation(prepared, independent_operator, backups):
    config, document, directory, bundle, value, command, calls, contents, provider, receipt = prepared
    if backups:
        provider.droplets[0]["features"].append("backups")
    if independent_operator:
        config.pop("public_url")
    original = (directory / "digitalocean.json").read_bytes()
    writes = len(provider.writes)
    result = owned_modal_remote(config, document, directory, bundle, value, "prepare", command=command)
    assert result == receipt and contents == [value]
    assert [call[0] for call in calls] == ["ssh", "scp", "ssh"]
    assert all("StrictHostKeyChecking=yes" in call and "GlobalKnownHostsFile=/dev/null" in call for call in calls)
    assert "reasoning_ledger.deployment.modal_host" in calls[-1][-1]
    assert provider.token not in str(calls) and len(provider.writes) == writes
    assert (directory / "digitalocean.json").read_bytes() == original
    assert ("backups" in provider.droplets[0]["features"]) is backups


@pytest.mark.parametrize("drift", ["account", "shape", "url", "ipv6", "firewall", "dns"])
def test_modal_upload_refuses_changed_provider_resources_or_url_before_ssh(prepared, drift):
    config, document, directory, bundle, value, command, calls, _, provider, _ = prepared
    provider.droplets[0]["features"].append("backups")
    if drift == "account":
        provider.account["uuid"] = "0" * 40
    elif drift == "shape":
        provider.droplets[0]["memory"] = 8192
    elif drift == "ipv6":
        provider.droplets[0]["features"].append("ipv6")
    elif drift == "firewall":
        provider.firewalls[0]["inbound_rules"].clear()
    elif drift == "dns":
        provider.records[0]["data"] = "203.0.113.9"
    else:
        value = {**value, "public_url": "https://different.example.org"}
    writes = len(provider.writes)
    with pytest.raises(InstallError):
        owned_modal_remote(config, document, directory, bundle, value, "prepare", command=command)
    assert calls == [] and len(provider.writes) == writes


def test_image_addition_needs_an_installed_release_that_has_the_host_operation(prepared):
    config, document, directory, bundle, value, command, calls, _, provider, _ = prepared
    tex = {**IMAGE, "id": "tex", "reference": "registry.example.org/research/tex@sha256:" + "e" * 64}
    addition = {"format": 1, "release_id": value["release_id"], "addition_id": "9" * 64, "public_url": value["public_url"],
                "previous": [IMAGE], "images": sorted([IMAGE, tex], key=lambda record: record["id"]),
                "source_worker": "1" * 32, "workers": []}
    writes = len(provider.writes)
    with pytest.raises(InstallError, match="can add Modal images"):
        owned_modal_remote(config, document, directory, bundle, addition, "extend", command=command)
    with pytest.raises(InstallError, match="invalid Modal image addition"):
        owned_modal_remote(config, document, directory, bundle, {**addition, "images": [IMAGE]}, "extend", command=command)
    assert calls == [] and len(provider.writes) == writes
