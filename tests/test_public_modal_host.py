"""Managed-host relay installation uses a fake filesystem and commands only."""

import copy
import fcntl
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from test_public_compute import IMAGE
from test_public_deployment import bootstrap as bootstrap
from test_public_deployment import pack
from test_public_deployment import payload as payload

from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.modal_host import ModalHostInstaller, relay_unit, validate_request
from reasoning_ledger.deployment.private import write_document, write_private
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES
from reasoning_ledger.server.compute_modal import SUPERVISOR_IMAGE


@pytest.fixture
def host(tmp_path):
    release = "a" * 64
    root = tmp_path / "root"
    for directory in ("var/lib/reasoning-deploy", "etc/reasoning", "etc/systemd/system", "opt/reasoning/releases/" + release + "/wheel"):
        path = root / directory
        path.mkdir(parents=True, mode=0o700)
    for path in (root, *root.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
    write_document(root / "var/lib/reasoning-deploy/installation.json",
                   {"release_id": release, "public_url": "https://chat.example.org", "phase": "services_started"}, new=True)
    write_private(root / "var/lib/reasoning-deploy/bootstrap.lock", b"")
    (root / "etc/reasoning/compute-images.json").write_text("[]\n")
    (root / "etc/reasoning/compute-images.json").chmod(0o644)
    original = root / "opt/reasoning/releases" / release
    (original / "modal-requirements.txt").write_text("noninstalling fixture\n")
    (original / "wheel/reasoning_ledger-0.1.0-py3-none-any.whl").write_bytes(b"noninstalling fixture")
    (original / "manifest.json").write_text(json.dumps({"files": {"wheel/reasoning_ledger-0.1.0-py3-none-any.whl": {}}}))
    payload = {"format": 1, "release_id": release, "setup_id": "b" * 64, "public_url": "https://chat.example.org",
               "images": [IMAGE], "workers": []}
    config = {"server_url": payload["public_url"], "id": "c" * 32, "organization_id": "d" * 32, "token": "rnw_" + "x" * 43,
              "image_ids": [IMAGE["id"]], "resources": DEFAULT_RESOURCES}
    worker = {"enrollment": config, "profile": {"format": 1, "app": "reasoning-compute", "environment": "main",
             "token_id": "ak_fixture_identifier", "token_secret": "as_fixture_secret", "resources": DEFAULT_RESOURCES,
             "supervisor_image": SUPERVISOR_IMAGE}}
    calls = []

    identity = {**config, "revoked_at": None, "draining": 0}
    identity["service"] = {}

    def command(arguments, **options):
        calls.append(arguments)
        if arguments[:2] == ["systemctl", "show"]:
            fields = {"FragmentPath": "/etc/systemd/system/" + arguments[-1] + ".service", "DropInPaths": "",
                      "User": "reasoning-modal", "Group": "reasoning-modal", "LoadState": "loaded",
                      "NeedDaemonReload": "no", "ActiveState": "active", **identity["service"]}
            return "".join(key + "=" + value + "\n" for key, value in fields.items()).encode()
    def client(_):
        return SimpleNamespace(request=lambda *a: identity, close=lambda: None)
    installer = ModalHostInstaller(root, command=command, account=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
                                    identity_client=client)
    return root, payload, worker, installer, calls, identity


def test_prepare_and_activate_create_credential_isolated_relays_without_docker_on_chat_host(host):
    root, payload, worker, installer, calls, _ = host
    assert installer.install(payload, "prepare")["catalog_installed"] is True
    value = {**payload, "workers": [worker]}
    result = installer.install(value, "activate")
    installer.install(value, "activate")
    assert result == {"setup_id": payload["setup_id"], "relays_active": 1, "live_compute_verified": False}
    assert json.loads((root / "etc/reasoning/compute-images.json").read_text()) == [IMAGE]
    state = root / "var/lib/reasoning-modal" / worker["enrollment"]["id"]
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "modal-profile.json").stat().st_mode & 0o777 == 0o600
    assert any("--require-hashes" in call for call in calls) and any("verify-images" in call for call in calls)
    assert not any("docker" in call or "apt-get" in call for call in calls)
    assert "as_fixture_secret" not in str(calls)
    unit = (root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")).read_text()
    assert "User=reasoning-modal" in unit and "InaccessiblePaths=/var/lib/reasoning" in unit


def test_modal_preparation_accepts_the_actual_completed_bootstrap_journal(tmp_path, payload, bootstrap):
    chat, _ = bootstrap
    bundle = pack(tmp_path / "release.tar.gz", payload)
    completed = chat.install(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), "https://chat.example.org", skip_platform=True)
    original = chat.state_path.read_bytes()
    request = {"format": 1, "release_id": completed["release_id"], "setup_id": "b" * 64,
               "public_url": completed["public_url"], "images": [IMAGE], "workers": []}
    calls = []
    modal = ModalHostInstaller(chat.root, command=lambda arguments, **options: calls.append(arguments))

    result = modal.install(request, "prepare")

    assert result == {"setup_id": request["setup_id"], "catalog_installed": True, "relays_active": False}
    assert json.loads((chat.root / "etc/reasoning/compute-images.json").read_text()) == [IMAGE]
    assert calls == [["systemctl", "restart", "reasoning-server"]]
    assert chat.state_path.read_bytes() == original


@pytest.mark.parametrize("phase", ["packages", "installed"])
def test_incomplete_or_provider_receipt_phase_cannot_enable_modal(host, phase):
    root, payload, _, installer, calls, _ = host
    path = root / "var/lib/reasoning-deploy/installation.json"
    state = json.loads(path.read_text())
    write_document(path, {**state, "phase": phase})

    with pytest.raises(InstallError, match="exact installed"):
        installer.install(payload, "prepare")

    assert calls == [] and not (root / "var/lib/reasoning-deploy/modal-setup").exists()


def test_changed_release_catalog_credentials_and_drained_enrollment_cannot_be_adopted(host):
    root, payload, worker, installer, calls, identity = host
    with pytest.raises(InstallError, match="exact installed"):
        installer.install({**payload, "release_id": "f" * 64}, "prepare")
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    changed = copy.deepcopy(worker)
    changed["profile"]["token_secret"] = "changed_fixture_secret"
    with pytest.raises(InstallError, match="credentials changed"):
        installer.install({**payload, "workers": [changed]}, "activate")
    identity["draining"] = 1
    calls.clear()
    with pytest.raises(InstallError, match="draining"):
        installer.install({**payload, "workers": [worker]}, "activate")
    assert calls == []
    (root / "etc/reasoning/compute-images.json").write_text(json.dumps([{**IMAGE, "name": "Changed"}]))
    with pytest.raises(InstallError, match="catalog changed"):
        installer.install(payload, "prepare")


def test_prepare_retry_restarts_server_even_if_catalog_bytes_were_already_installed(host):
    _, payload, _, installer, calls, _ = host
    installer.install(payload, "prepare")
    calls.clear()
    installer.install(payload, "prepare")
    assert calls == [["systemctl", "restart", "reasoning-server"]]


@pytest.mark.parametrize("field,value", [("User", "root"), ("DropInPaths", "/etc/foreign.conf"), ("NeedDaemonReload", "yes")])
def test_changed_service_identity_is_refused_before_relay_activation(host, field, value):
    _, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    identity["service"][field] = value
    calls.clear()
    with pytest.raises(InstallError, match="service identity"):
        installer.install({**payload, "workers": [worker]}, "activate")
    assert not any(call[:2] == ["systemctl", "enable"] for call in calls)


def test_shared_bootstrap_lock_prevents_concurrent_catalog_or_service_changes(host):
    root, payload, _, installer, calls, _ = host
    with (root / "var/lib/reasoning-deploy/bootstrap.lock").open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallError, match="another managed host"):
            installer.install(payload, "prepare")
    assert calls == [] and not (root / "var/lib/reasoning-deploy/modal-setup").exists()


def test_activation_without_prepared_catalog_refuses_without_services_or_provider_calls(host):
    _, payload, worker, installer, calls, _ = host
    with pytest.raises(InstallError, match="prepare the exact"):
        installer.install({**payload, "workers": [worker]}, "activate")
    assert calls == []


@pytest.mark.parametrize("mutation", ["workers", "profile", "limits", "organization", "image"])
def test_host_rejects_invalid_enrollment_before_writes(host, mutation):
    _, payload, worker, _, _, _ = host
    value = copy.deepcopy({**payload, "workers": [worker]})
    if mutation == "workers":
        value["workers"] *= 2
    elif mutation == "profile":
        value["workers"][0]["profile"]["token_secret"] = "secret\nunsafe"
    elif mutation == "limits":
        value["workers"][0]["enrollment"]["resources"]["cpu"] = 8
    elif mutation == "organization":
        value["workers"][0]["enrollment"]["organization_id"] = "../unsafe"
    else:
        value["images"][0]["reference"] = "registry.example/cpu:latest"
    with pytest.raises(InstallError):
        validate_request(value, "activate")


def test_service_units_reject_shell_path_injection():
    with pytest.raises(InstallError):
        relay_unit("a" * 64, "../unsafe")
