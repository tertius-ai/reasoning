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


TEX = {**IMAGE, "id": "tex", "name": "Reasoning TeX", "languages": ["python"],
       "reference": "registry.example.org/research/tex@sha256:" + "e" * 64}


@pytest.fixture
def addition(host):
    """A host whose original Modal setup is running, plus a request that adds one image."""
    root, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    calls.clear()
    installer.identity_client = lambda config: SimpleNamespace(
        request=lambda *a: {**config, "revoked_at": None, "draining": identity["draining"]}, close=lambda: None)
    config = {**worker["enrollment"], "id": "f" * 32, "image_ids": ["tex"]}
    request = {"format": 1, "release_id": payload["release_id"], "addition_id": "9" * 64, "public_url": payload["public_url"],
               "previous": [IMAGE], "images": sorted([IMAGE, TEX], key=lambda record: record["id"]),
               "source_worker": worker["enrollment"]["id"], "workers": []}
    return root, request, {"enrollment": config}, worker, installer, calls, identity


def relay_files(root, identifier):
    state = root / "var/lib/reasoning-modal" / identifier
    return {path.name: path.read_bytes() for path in state.iterdir()}


def test_adding_an_image_extends_the_catalog_and_starts_its_own_relays_without_touching_existing_ones(addition):
    root, request, new, worker, installer, calls, _ = addition
    existing = relay_files(root, worker["enrollment"]["id"])
    existing_unit = (root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")).read_bytes()

    assert installer.add_images(request, "extend") == {"addition_id": request["addition_id"], "catalog_installed": True,
                                                       "relays_active": 0}
    assert json.loads((root / "etc/reasoning/compute-images.json").read_text()) == request["images"]
    assert calls == [["systemctl", "restart", "reasoning-server"]]
    calls.clear()
    installer.add_images(request, "extend")
    assert calls == [["systemctl", "restart", "reasoning-server"]]

    value = {**request, "workers": [new]}
    result = installer.add_images(value, "relays")
    assert installer.add_images(value, "relays") == result == {"addition_id": request["addition_id"], "relays_active": 1,
                                                               "live_compute_verified": False}
    files = relay_files(root, "f" * 32)
    assert json.loads(files["catalog.json"]) == [TEX]
    assert json.loads(files["enrollment.json"]) == new["enrollment"]
    assert files["modal-profile.json"] == existing["modal-profile.json"]
    assert (root / "var/lib/reasoning-modal" / ("f" * 32) / "modal-profile.json").stat().st_mode & 0o777 == 0o600
    assert relay_files(root, worker["enrollment"]["id"]) == existing
    assert (root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")).read_bytes() == existing_unit
    assert "as_fixture_secret" not in str(calls)
    assert ["systemctl", "enable", "--now", "reasoning-modal-" + "f" * 32] in calls
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "disable"]) for call in calls)
    assert not any(call[-1] == "reasoning-modal-" + worker["enrollment"]["id"] and call[:2] == ["systemctl", "enable"]
                   for call in calls)


def test_adding_an_image_after_an_upgrade_runs_new_relays_from_the_installed_release(addition):
    root, request, new, worker, installer, calls, _ = addition
    upgraded = "8" * 64
    original = root / "opt/reasoning/releases" / ("a" * 64)
    target = root / "opt/reasoning/releases" / upgraded
    (target / "wheel").mkdir(parents=True)
    for name in ("modal-requirements.txt", "manifest.json", "wheel/reasoning_ledger-0.1.0-py3-none-any.whl"):
        (target / name).write_bytes((original / name).read_bytes())
    path = root / "var/lib/reasoning-deploy/installation.json"
    write_document(path, {**json.loads(path.read_text()), "release_id": upgraded})
    request = {**request, "release_id": upgraded}
    with pytest.raises(InstallError, match="inputs changed"):
        installer.install({"format": 1, "release_id": upgraded, "setup_id": "b" * 64, "public_url": request["public_url"],
                           "images": [IMAGE], "workers": []}, "prepare")

    installer.add_images(request, "extend")
    installer.add_images({**request, "workers": [new]}, "relays")

    unit = (root / "etc/systemd/system" / ("reasoning-modal-" + "f" * 32 + ".service")).read_text()
    assert f"/opt/reasoning/modal/{upgraded}/venv/bin/reasoning-compute-worker" in unit
    old = (root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")).read_text()
    assert "/opt/reasoning/modal/" + "a" * 64 + "/venv" in old


@pytest.mark.parametrize("change", ["host-catalog", "replaced-image", "relays-first", "limits", "source", "unsafe-source", "journal"])
def test_image_addition_refuses_changes_it_cannot_prove_safe(addition, change):
    root, request, new, worker, installer, calls, _ = addition
    value = copy.deepcopy(request)
    operation = "extend"
    match = None
    if change == "host-catalog":
        (root / "etc/reasoning/compute-images.json").write_text(json.dumps([{**IMAGE, "name": "Changed"}]))
        match = "catalog changed"
    elif change == "replaced-image":
        value["images"] = sorted([{**IMAGE, "name": "Replaced"}, TEX], key=lambda record: record["id"])
        match = "invalid Modal image addition"
    elif change == "relays-first":
        value["workers"], operation, match = [new], "relays", "extend the image catalog"
    elif change == "limits":
        installer.add_images(request, "extend")
        changed = copy.deepcopy(new)
        changed["enrollment"]["resources"]["cpu"] = 2
        value["workers"], operation, match = [changed], "relays", "differ from the source relay"
    elif change == "source":
        value["source_worker"], match = "7" * 32, "source Modal relay"
    elif change == "unsafe-source":
        (root / "var/lib/reasoning-modal" / worker["enrollment"]["id"] / "modal-profile.json").chmod(0o640)
        match = "source Modal relay profile"
    else:
        installer.add_images(request, "extend")
        value["images"] = sorted([IMAGE, TEX, {**TEX, "id": "tex-two"}], key=lambda record: record["id"])
        match = "addition inputs changed"
    before = (root / "etc/reasoning/compute-images.json").read_bytes()
    calls.clear()
    with pytest.raises(InstallError, match=match):
        installer.add_images(value, operation)
    assert (root / "etc/reasoning/compute-images.json").read_bytes() == before
    assert not any(call[:2] == ["systemctl", "enable"] for call in calls)
    assert not (root / "var/lib/reasoning-modal" / ("f" * 32)).exists()


def test_image_addition_requires_an_installed_modal_setup(host):
    root, payload, worker, installer, calls, _ = host
    request = {"format": 1, "release_id": payload["release_id"], "addition_id": "9" * 64, "public_url": payload["public_url"],
               "previous": [IMAGE], "images": sorted([IMAGE, TEX], key=lambda record: record["id"]),
               "source_worker": worker["enrollment"]["id"], "workers": []}
    with pytest.raises(InstallError, match="installed Modal setup"):
        installer.add_images(request, "extend")
    assert calls == []


@pytest.fixture
def refresh(host):
    """A running original relay, drained and idle, on a host upgraded to a newer release."""
    root, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    upgraded = "8" * 64
    original = root / "opt/reasoning/releases" / payload["release_id"]
    target = root / "opt/reasoning/releases" / upgraded
    (target / "wheel").mkdir(parents=True)
    for name in ("modal-requirements.txt", "manifest.json", "wheel/reasoning_ledger-0.1.0-py3-none-any.whl"):
        (target / name).write_bytes((original / name).read_bytes())
    path = root / "var/lib/reasoning-deploy/installation.json"
    write_document(path, {**json.loads(path.read_text()), "release_id": upgraded})
    state = {"draining": 1, "unsettled_jobs": []}

    def client(config):
        def request(method, endpoint, body=None):
            view = {**config, "revoked_at": None, "retired_at": None, "draining": state["draining"]}
            if endpoint == "/api/worker/status":
                return {**view, "unsettled_jobs": state["unsettled_jobs"], "quiescent": not state["unsettled_jobs"]}
            return view
        return SimpleNamespace(request=request, close=lambda: None)

    installer.identity_client = client
    write_private(root / "var/lib/reasoning-modal" / worker["enrollment"]["id"] / "modal-sandbox.json", b'{"phase":"idle"}')
    calls.clear()
    request = {"format": 1, "release_id": upgraded, "public_url": payload["public_url"]}
    return root, request, worker, installer, calls, identity, state


def unit_path(root, worker):
    return root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")


def test_relay_update_moves_drained_relays_to_the_installed_release_and_keeps_their_state(refresh):
    root, request, worker, installer, calls, _, _ = refresh
    identifier = worker["enrollment"]["id"]
    before = relay_files(root, identifier)

    result = installer.refresh(request)

    assert result == {"release_id": request["release_id"], "relays": [identifier], "updated": [identifier],
                      "restarted": [identifier], "live_compute_verified": False}
    assert unit_path(root, worker).read_text() == relay_unit(request["release_id"], identifier)
    assert relay_files(root, identifier) == before
    name = "reasoning-modal-" + identifier
    probes = [index for index, call in enumerate(calls) if "verify-images" in call]
    stop = calls.index(["systemctl", "stop", name])
    assert probes and max(probes) < stop
    assert any(f"/opt/reasoning/modal/{request['release_id']}/venv/bin/reasoning-compute-worker" in " ".join(call)
               for call in calls if "verify-images" in call)
    assert calls.index(["systemctl", "daemon-reload"]) < calls.index(["systemctl", "start", name])
    assert worker["profile"]["token_secret"] not in str(calls) and worker["enrollment"]["token"] not in str(calls)

    calls.clear()
    again = installer.refresh(request)
    assert again["updated"] == [] and again["restarted"] == [identifier]
    assert ["systemctl", "stop", name] not in calls


def test_relay_update_leaves_a_stopped_relay_stopped(refresh):
    root, request, worker, installer, calls, identity, _ = refresh
    identity["service"] = {"ActiveState": "inactive"}
    result = installer.refresh(request)
    assert result["updated"] == [worker["enrollment"]["id"]] and result["restarted"] == []
    assert not any(call[:2] == ["systemctl", "start"] for call in calls)


@pytest.mark.parametrize("change", ["claims", "unsettled", "attempt", "unit", "dropin", "release", "no-setup"])
def test_relay_update_refuses_before_stopping_anything_when_it_cannot_prove_it_safe(refresh, change):
    root, request, worker, installer, calls, identity, state = refresh
    identifier = worker["enrollment"]["id"]
    if change == "claims":
        state["draining"] = 0
    elif change == "unsettled":
        state["unsettled_jobs"] = [{"id": "e" * 32, "status": "outcome_unknown"}]
    elif change == "attempt":
        (root / "var/lib/reasoning-modal" / identifier / "modal-sandbox.json").unlink()
        write_private(root / "var/lib/reasoning-modal" / identifier / "modal-sandbox.json", b'{"phase":"creating"}')
    elif change == "unit":
        unit_path(root, worker).write_text(unit_path(root, worker).read_text() + "Environment=EXTRA=1\n")
    elif change == "dropin":
        identity["service"] = {"DropInPaths": "/run/systemd/system/reasoning-modal-" + identifier + ".service.d/90.conf"}
    elif change == "release":
        request = {**request, "release_id": "7" * 64}
    else:
        (root / "var/lib/reasoning-deploy/modal-setup/installation.json").unlink()
    original = unit_path(root, worker).read_bytes()
    with pytest.raises(InstallError):
        installer.refresh(request)
    assert unit_path(root, worker).read_bytes() == original
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "start"]) for call in calls)
    assert not any("--require-hashes" in call for call in calls)


@pytest.fixture
def limits(host):
    """A running original relay, drained and idle, whose limits the server already raised."""
    root, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    raised = {**DEFAULT_RESOURCES, "cpu": 4}
    server = {"draining": 1, "unsettled_jobs": [], "resources": raised}

    def client(config):
        def request(method, endpoint, body=None):
            view = {**config, "resources": server["resources"], "revoked_at": None, "retired_at": None,
                    "draining": server["draining"]}
            if endpoint == "/api/worker/status":
                return {**view, "unsettled_jobs": server["unsettled_jobs"], "quiescent": not server["unsettled_jobs"]}
            return view
        return SimpleNamespace(request=request, close=lambda: None)

    installer.identity_client = client
    write_private(root / "var/lib/reasoning-modal" / worker["enrollment"]["id"] / "modal-sandbox.json", b'{"phase":"idle"}')
    calls.clear()
    request = {"format": 1, "release_id": payload["release_id"], "public_url": payload["public_url"], "resources": raised,
               "workers": [worker["enrollment"]["id"]]}
    return root, request, worker, installer, calls, identity, server


def test_relay_limits_follow_the_server_and_keep_credentials_catalog_and_runtime(limits):
    root, request, worker, installer, calls, _, _ = limits
    identifier = worker["enrollment"]["id"]
    state = root / "var/lib/reasoning-modal" / identifier
    unit, catalog = unit_path(root, worker).read_bytes(), (state / "catalog.json").read_bytes()

    result = installer.limits(request)

    assert result == {"resources": request["resources"], "relays": [identifier], "updated": [identifier],
                      "restarted": [identifier], "live_compute_verified": False}
    assert json.loads((state / "enrollment.json").read_text()) == {**worker["enrollment"], "resources": request["resources"]}
    assert json.loads((state / "modal-profile.json").read_text()) == {**worker["profile"], "resources": request["resources"]}
    assert all((state / name).stat().st_mode & 0o077 == 0 for name in ("enrollment.json", "modal-profile.json"))
    assert unit_path(root, worker).read_bytes() == unit and (state / "catalog.json").read_bytes() == catalog
    name = "reasoning-modal-" + identifier
    probes = [index for index, call in enumerate(calls) if "verify-images" in call]
    assert probes and calls.index(["systemctl", "stop", name]) < min(probes) <= max(probes) < calls.index(["systemctl", "start", name])
    assert all(f"/opt/reasoning/modal/{request['release_id']}/venv/bin/" in " ".join(calls[index]) for index in probes)
    assert not any("--require-hashes" in call for call in calls)
    assert worker["profile"]["token_secret"] not in str(calls) and worker["enrollment"]["token"] not in str(calls)
    assert not (root / "var/lib/reasoning-deploy/modal-setup/relay-limits.json").exists()

    calls.clear()
    again = installer.limits(request)
    assert again["updated"] == [] and again["restarted"] == []
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "start"]) for call in calls)


def test_relay_limits_leave_a_stopped_relay_stopped(limits):
    _, request, worker, installer, calls, identity, _ = limits
    identity["service"] = {"ActiveState": "inactive"}
    result = installer.limits(request)
    assert result["updated"] == [worker["enrollment"]["id"]] and result["restarted"] == []
    assert not any(call[:2] == ["systemctl", "start"] for call in calls)


def test_relay_limits_resume_restarts_a_relay_stopped_before_a_failed_probe(limits):
    root, request, worker, installer, calls, _, _ = limits
    name = "reasoning-modal-" + worker["enrollment"]["id"]
    command = installer.command

    def failing(arguments, **options):
        if "verify-images" in arguments:
            raise InstallError("fixture probe failed")
        return command(arguments, **options)

    installer.command = failing
    with pytest.raises(InstallError, match="fixture probe"):
        installer.limits(request)
    assert ["systemctl", "stop", name] in calls and ["systemctl", "start", name] not in calls
    assert json.loads((root / "var/lib/reasoning-deploy/modal-setup/relay-limits.json").read_text()) == {
        "restart": [worker["enrollment"]["id"]]}
    installer.command = command
    calls.clear()
    result = installer.limits(request)
    assert result["updated"] == [] and result["restarted"] == [worker["enrollment"]["id"]]
    assert ["systemctl", "stop", name] not in calls and ["systemctl", "start", name] in calls


LIMIT_REFUSALS = {"server": "new limits", "claims": "drain every", "unsettled": "running or unknown", "attempt": "unfinished VM",
                  "unit": "service definition", "workers": "differ from the relay workers", "image": "maximum resources",
                  "request": "invalid Modal relay limits", "no-setup": "installed Modal setup", "binding": "runtime binding"}


@pytest.mark.parametrize("change", sorted(LIMIT_REFUSALS))
def test_relay_limits_refuse_before_stopping_or_writing_anything_when_unsafe(limits, change):
    root, request, worker, installer, calls, identity, server = limits
    identifier = worker["enrollment"]["id"]
    state = root / "var/lib/reasoning-modal" / identifier
    if change == "server":
        server["resources"] = worker["enrollment"]["resources"]
    elif change == "claims":
        server["draining"] = 0
    elif change == "unsettled":
        server["unsettled_jobs"] = [{"id": "e" * 32, "status": "running"}]
    elif change == "attempt":
        (state / "modal-sandbox.json").unlink()
        write_private(state / "modal-sandbox.json", b'{"phase":"creating"}')
    elif change == "unit":
        unit_path(root, worker).write_text(unit_path(root, worker).read_text() + "Environment=EXTRA=1\n")
    elif change == "workers":
        request = {**request, "workers": sorted([identifier, "e" * 32])}
    elif change == "image":
        request = {**request, "resources": {**request["resources"], "cpu": 8}}
        server["resources"] = request["resources"]
    elif change == "request":
        request = {**request, "resources": {**request["resources"], "cpu": 0}}
    elif change == "binding":
        bind_runtime(root, worker)
        binding = json.loads((state / "modal-runtime.json").read_text())
        (state / "modal-runtime.json").unlink()
        write_private(state / "modal-runtime.json", json.dumps({**binding, "profile_sha256": "0" * 64}).encode())
    else:
        (root / "var/lib/reasoning-deploy/modal-setup/installation.json").unlink()
    before = relay_files(root, identifier)
    with pytest.raises(InstallError, match=LIMIT_REFUSALS[change]):
        installer.limits(request)
    assert relay_files(root, identifier) == before
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "start"]) for call in calls)


def modal_runner(state, worker):
    """The relay's own Modal runner over its files on disk, with a fake app identity and no Modal calls."""
    from reasoning_ledger.server.compute_catalog import load_catalog
    from reasoning_ledger.server.compute_modal import ModalRunner

    return ModalRunner(state, worker["enrollment"]["id"], json.loads((state / "modal-profile.json").read_text()),
                       load_catalog(state / "catalog.json"), sdk=SimpleNamespace(identity=lambda: "ap-fixture"))


def bind_runtime(root, worker):
    """Bind the relay's Modal runtime exactly as the relay does on its first start."""
    state = root / "var/lib/reasoning-modal" / worker["enrollment"]["id"]
    sandbox = state / "modal-sandbox.json"
    sandbox.unlink()
    modal_runner(state, worker).verify_runtime_identity()
    write_private(sandbox, b'{"phase":"idle"}')
    return state


def test_relay_limits_rebind_the_runtime_so_the_relay_accepts_its_changed_profile(limits):
    from reasoning_ledger.server.compute_modal import RuntimeFault

    root, request, worker, installer, calls, _, _ = limits
    state = bind_runtime(root, worker)
    original = modal_runner(state, worker)
    before = json.loads((state / "modal-runtime.json").read_text())

    result = installer.limits(request)

    assert result["updated"] == [worker["enrollment"]["id"]]
    after = json.loads((state / "modal-runtime.json").read_text())
    assert {**after, "profile_sha256": None} == {**before, "profile_sha256": None}
    assert after["profile_sha256"] != before["profile_sha256"]
    assert (state / "modal-runtime.json").stat().st_mode & 0o077 == 0
    modal_runner(state, worker).verify_runtime_identity()
    with pytest.raises(RuntimeFault, match="runtime_identity_changed"):
        original.verify_runtime_identity()


def test_relay_limits_finish_a_change_that_left_the_runtime_binding_behind(limits):
    """Files written by a host step that did not yet rebind: the relays were stopped and the probe refused them."""
    root, request, worker, installer, calls, identity, _ = limits
    identifier = worker["enrollment"]["id"]
    state = bind_runtime(root, worker)
    for name, body in (("enrollment.json", worker["enrollment"]), ("modal-profile.json", worker["profile"])):
        (state / name).unlink()
        write_private(state / name, json.dumps({**body, "resources": request["resources"]}, sort_keys=True,
                                               separators=(",", ":")).encode())
    write_document(root / "var/lib/reasoning-deploy/modal-setup/relay-limits.json", {"restart": [identifier]})
    identity["service"] = {"ActiveState": "inactive"}
    command = installer.command

    def starting(arguments, **options):
        if arguments[:2] == ["systemctl", "start"]:
            identity["service"] = {}
        return command(arguments, **options)

    installer.command = starting
    result = installer.limits(request)

    assert result["updated"] == [identifier] and result["restarted"] == [identifier]
    modal_runner(state, worker).verify_runtime_identity()
    assert ["systemctl", "start", "reasoning-modal-" + identifier] in calls
    assert not (root / "var/lib/reasoning-deploy/modal-setup/relay-limits.json").exists()


@pytest.fixture
def probe(host):
    """A running relay of the installed release, and a host command that answers the probe with a fixed receipt."""
    root, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    original, answer = installer.command, {"body": json.dumps({"format": 1, "failures": [], "writer": {}}).encode()}

    def command(arguments, **options):
        if "probe-environment-mount" in arguments:
            calls.append(arguments)
            return answer["body"]
        return original(arguments, **options)

    installer.command = command
    calls.clear()
    request = {"format": 1, "release_id": payload["release_id"], "public_url": payload["public_url"]}
    return root, request, worker, installer, calls, answer


def test_environment_probe_runs_as_the_relay_account_with_the_relays_own_state(probe):
    root, request, worker, installer, calls, _ = probe
    identifier = worker["enrollment"]["id"]
    before = relay_files(root, identifier)

    result = installer.environment_probe(request)

    assert result == {"release_id": request["release_id"], "relay": identifier,
                      "probe": {"format": 1, "failures": [], "writer": {}}}
    state = f"{root}/var/lib/reasoning-modal/{identifier}"
    assert calls[-1] == ["runuser", "-u", "reasoning-modal", "--",
                         f"{root}/opt/reasoning/modal/{request['release_id']}/venv/bin/reasoning-compute-worker",
                         "--data-dir", state, "probe-environment-mount", "--modal-profile", state + "/modal-profile.json",
                         "--catalog", state + "/catalog.json"]
    # The relay keeps running and its files are untouched.
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "start"], ["systemctl", "restart"]) for call in calls)
    assert relay_files(root, identifier) == before
    assert worker["profile"]["token_secret"] not in str(calls) and worker["enrollment"]["token"] not in str(calls)


def test_environment_probe_is_bounded_by_its_own_timeout(probe, monkeypatch):
    from reasoning_ledger.deployment import modal_host

    _, request, _, installer, _, _ = probe
    seen = []
    installer.command = None
    monkeypatch.setattr(modal_host.subprocess, "run", lambda arguments, **options: seen.append(options["timeout"]) or
                        SimpleNamespace(returncode=0, stdout=json.dumps({"format": 1, "failures": []}).encode()))
    monkeypatch.setattr(installer, "service_identity", lambda *args, **options: None)
    installer.environment_probe(request)
    assert seen == [modal_host.PROBE_SECONDS] and 900 < modal_host.PROBE_SECONDS < 1200


@pytest.mark.parametrize("change", ["older-relay", "receipt", "no-relays", "no-setup", "request"])
def test_environment_probe_refuses_what_it_cannot_trust(probe, change):
    root, request, worker, installer, calls, answer = probe
    unit = root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")
    if change == "older-relay":
        unit.write_text(relay_unit("7" * 64, worker["enrollment"]["id"]))
        expected = "make compute-relays-update"
    elif change == "receipt":
        answer["body"] = b'{"format": 1}'
        expected = "invalid Lean environment probe receipt"
    elif change == "no-relays":
        unit.unlink()
        expected = "no Modal relays"
    elif change == "no-setup":
        (root / "var/lib/reasoning-deploy/modal-setup/installation.json").unlink()
        expected = "installed Modal setup"
    else:
        request = {**request, "workers": []}
        expected = "invalid Lean environment probe request"
    with pytest.raises(InstallError, match=expected):
        installer.environment_probe(request)
    if change != "receipt":
        assert not any("probe-environment-mount" in call for call in calls)


@pytest.fixture
def preparing(host):
    """A running relay of the installed release, and a host command that answers a preparation with a fixed receipt."""
    root, payload, worker, installer, calls, identity = host
    installer.install(payload, "prepare")
    installer.install({**payload, "workers": [worker]}, "activate")
    environment = "e" * 32
    original = installer.command
    answer = {"body": json.dumps({"format": 1, "environment_id": environment, "failures": []}).encode()}

    def command(arguments, **options):
        if "prepare-environment" in arguments:
            calls.append(arguments)
            return answer["body"]
        return original(arguments, **options)

    installer.command = command
    calls.clear()
    request = {"format": 1, "release_id": payload["release_id"], "public_url": payload["public_url"],
               "environment_id": environment}
    return root, request, worker, installer, calls, answer


def test_environment_preparation_runs_as_the_relay_account_for_the_named_environment(preparing):
    root, request, worker, installer, calls, _ = preparing
    identifier = worker["enrollment"]["id"]
    before = relay_files(root, identifier)

    result = installer.environment_prepare(request)

    assert result == {"release_id": request["release_id"], "relay": identifier,
                      "preparation": {"format": 1, "environment_id": request["environment_id"], "failures": []}}
    state = f"{root}/var/lib/reasoning-modal/{identifier}"
    assert calls[-1] == ["runuser", "-u", "reasoning-modal", "--",
                         f"{root}/opt/reasoning/modal/{request['release_id']}/venv/bin/reasoning-compute-worker",
                         "--data-dir", state, "prepare-environment", "--environment", request["environment_id"],
                         "--modal-profile", state + "/modal-profile.json", "--catalog", state + "/catalog.json"]
    assert not any(call[:2] in (["systemctl", "stop"], ["systemctl", "start"], ["systemctl", "restart"]) for call in calls)
    assert relay_files(root, identifier) == before
    assert worker["profile"]["token_secret"] not in str(calls) and worker["enrollment"]["token"] not in str(calls)


def test_environment_preparation_is_bounded_by_its_own_timeout(preparing, monkeypatch):
    from reasoning_ledger.deployment import modal_host
    from reasoning_ledger.server import compute_environment_prepare

    _, request, _, installer, _, _ = preparing
    seen = []
    installer.command = None
    receipt = {"format": 1, "environment_id": request["environment_id"], "failures": []}
    monkeypatch.setattr(modal_host.subprocess, "run", lambda arguments, **options: seen.append(options["timeout"]) or
                        SimpleNamespace(returncode=0, stdout=json.dumps(receipt).encode()))
    monkeypatch.setattr(installer, "service_identity", lambda *args, **options: None)
    installer.environment_prepare(request)
    assert seen == [modal_host.PREPARE_SECONDS] and modal_host.PREPARE_SECONDS > compute_environment_prepare.PREPARE_SECONDS


@pytest.mark.parametrize("change", ["older-relay", "receipt", "other-environment", "request"])
def test_environment_preparation_refuses_what_it_cannot_trust(preparing, change):
    root, request, worker, installer, calls, answer = preparing
    unit = root / "etc/systemd/system" / ("reasoning-modal-" + worker["enrollment"]["id"] + ".service")
    if change == "older-relay":
        unit.write_text(relay_unit("7" * 64, worker["enrollment"]["id"]))
        expected = "make compute-relays-update"
    elif change == "receipt":
        answer["body"] = b'{"format": 1}'
        expected = "invalid Lean environment preparation receipt"
    elif change == "other-environment":
        answer["body"] = json.dumps({"format": 1, "environment_id": "f" * 32, "failures": []}).encode()
        expected = "invalid Lean environment preparation receipt"
    else:
        request = {**request, "environment_id": "../" + "e" * 29}
        expected = "invalid Lean environment preparation request"
    with pytest.raises(InstallError, match=expected):
        installer.environment_prepare(request)
    if change in {"older-relay", "request"}:
        assert not any("prepare-environment" in call for call in calls)

