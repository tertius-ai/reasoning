"""Existing deployment compute wizard boundaries; no paid jobs or SSH in these tests.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json

import pytest

from reasoning_ledger.deployment import compute_setup
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import read_document, write_document


@pytest.fixture
def setup(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    state = tmp_path / "operator"
    state.mkdir(mode=0o700)
    config = {"public_url": "https://chat.example.org", "token_file": "/private/token", "identity": "/private/key"}
    write_document(state / "setup.json", config)
    write_document(state / "plan.json", {"plan": {"public_url": config["public_url"]}})
    write_document(state / "setup-completion.json", {"organization_id": "c" * 32, "plan_id": "d" * 64})
    current = {"release_id": "a" * 64, "bundle_sha256": "b" * 64, "public_url": config["public_url"]}
    monkeypatch.setattr(compute_setup, "current_binding", lambda _: current)
    monkeypatch.setattr(compute_setup, "archived_release", lambda *_: state / "installed.tar.gz")
    monkeypatch.setattr(compute_setup, "inspect_bundle", lambda *_: ({"installation_features": ["modal-relay-v1"]}, "a" * 64, "b" * 64))
    monkeypatch.setattr(compute_setup, "save_release_catalog", lambda *_: None)
    monkeypatch.setattr(compute_setup, "ask", lambda prompt, default=None: default or "owner@example.org")
    monkeypatch.setattr(compute_setup.getpass, "getpass", lambda _: "password-not-persisted")
    calls = []
    monkeypatch.setattr(compute_setup, "approve", lambda *_: calls.append("consent"))
    monkeypatch.setattr(compute_setup, "temporary_ssh_cidr", lambda *_: "203.0.113.1/32")
    monkeypatch.setattr(compute_setup, "_ssh_action", lambda *args: (calls.append("open") or config, {"phase": "open"}))
    monkeypatch.setattr(
        compute_setup, "close_ssh_with_token_recovery", lambda *args: (calls.append("close") or config, {"phase": "closed"})
    )
    value = {"workers": 2, "monthly_cpu_seconds": 10000}
    return state, calls, value


@pytest.mark.parametrize("independent_operator", [False, True])
def test_compute_setup_confirms_before_mutation_keeps_password_in_memory_and_closes_ssh(setup, independent_operator):
    state, calls, value = setup
    config = {"token_file": "/private/token", "identity": "/private/key"}
    if independent_operator:
        write_document(state / "setup.json", config)

    def install(selection, config, owner, org, private, remote):
        assert calls == ["verify", "consent", "open"] and selection == value
        assert owner["password"] == "password-not-persisted" and org == "c" * 32
        assert config["public_url"] == "https://chat.example.org"
        assert "password" not in read_document(private / "target.json")
        calls.append("install")
        return {"setup_id": "e" * 64, "relays_active": 2, "live_compute_verified": False}

    result = compute_setup.run_compute_setup(
        state, None, collect=lambda *_: value, verify=lambda _: calls.append("verify"), install=install
    )
    assert calls == ["verify", "consent", "open", "install", "close"]
    assert result["ssh_closed"] and not result["live_compute_verified"]
    if independent_operator:
        assert read_document(state / "setup.json") == config


def test_failed_compute_activation_still_closes_ssh(setup):
    state, calls, value = setup

    def fail(*args):
        raise InstallError("retained failure")

    with pytest.raises(InstallError, match="retained failure"):
        compute_setup.run_compute_setup(state, None, collect=lambda *_: value, verify=lambda _: None, install=fail)
    assert calls == ["consent", "open", "close"]


def test_refused_consent_never_opens_ssh(setup, monkeypatch):
    state, calls, value = setup

    def refuse(*args):
        raise InstallError("not confirmed")

    monkeypatch.setattr(compute_setup, "approve", refuse)
    with pytest.raises(InstallError, match="not confirmed"):
        compute_setup.run_compute_setup(state, None, collect=lambda *_: value, verify=lambda _: None)
    assert calls == []


@pytest.mark.parametrize("failure", ["config", "plan", "missing_plan"])
def test_compute_setup_refuses_origin_drift_before_credentials_or_ssh(setup, failure):
    state, calls, _ = setup
    path = state / ("setup.json" if failure == "config" else "plan.json")
    document = read_document(path)
    if failure == "config":
        document["public_url"] = "https://different.example.org"
    elif failure == "plan":
        document["plan"]["public_url"] = "https://different.example.org"
    else:
        document["plan"].pop("public_url")
    write_document(path, document)
    with pytest.raises(InstallError, match="verified deployment"):
        compute_setup.run_compute_setup(state, None, collect=lambda *_: pytest.fail("must not request credentials"))
    assert calls == []


TEX_LIMITS = {"cpu": 2, "memory_bytes": 2 * 1024**3, "disk_bytes": 268435456, "pids": 128, "wall_time_seconds": 600,
              "artifact_bytes": 1048576}


def image_record(identity, digit):
    from test_public_compute import IMAGE

    return {**IMAGE, "id": identity, "reference": "registry.example.org/research/" + identity + "@sha256:" + digit * 64,
            "maximum_resources": {**TEX_LIMITS, "cpu": 64, "wall_time_seconds": 86400}}


def relay(identity="1" * 32, **changes):
    return {"id": identity, "organization_id": "c" * 32, "name": "reasoning-modal-" + "2" * 32 + "-0", "created_at": 1,
            "last_seen_at": 2, "draining": 0, "revoked_at": None, "retired_at": None, "retirement_id": None,
            "image_ids": ["research-cpu"], "resources": TEX_LIMITS, **changes}


def approved(*records):
    from reasoning_ledger.server.compute_catalog import ComputeImage

    return {record["id"]: ComputeImage.parse(record) for record in records}


def test_addition_plan_adds_only_new_images_and_borrows_an_active_relay_profile():
    from reasoning_ledger.deployment.compute_images import plan_addition

    cpu, tex = image_record("research-cpu", "a"), image_record("tex", "e")
    workers = [relay("3" * 32, draining=1), relay("4" * 32, name="laptop"), relay()]
    plan = plan_addition({"images": [cpu]}, workers, approved(cpu, tex))
    assert plan == {"previous": [cpu], "images": [cpu, tex], "additions": ["tex"], "source_worker": "1" * 32, "resources": TEX_LIMITS}
    with pytest.raises(InstallError, match="never replaces"):
        plan_addition({"images": [cpu]}, workers, approved({**cpu, "name": "Replaced"}, tex))
    with pytest.raises(InstallError, match="already installed"):
        plan_addition({"images": [cpu]}, workers, approved(cpu))
    with pytest.raises(InstallError, match="no active Modal relay"):
        plan_addition({"images": [cpu]}, workers[:2], approved(cpu, tex))
    small = {**tex, "maximum_resources": {**TEX_LIMITS, "cpu": 1}}
    with pytest.raises(InstallError, match="exceed image tex"):
        plan_addition({"images": [cpu]}, workers, approved(cpu, small))


@pytest.fixture
def addition(tmp_path, monkeypatch):
    import httpx

    from reasoning_ledger.deployment import compute_images

    tmp_path.chmod(0o700)
    work = tmp_path / "compute-images"
    work.mkdir(mode=0o700)
    cpu, tex = image_record("research-cpu", "a"), image_record("tex", "e")
    server = {"images": [cpu], "policy": {"allowed_images": ["research-cpu"], "monthly_cpu_seconds": 100000, "max_active_jobs": 2},
              "calls": [], "remote": [], "enrolled": [], "fail_relays": False}

    def handler(request):
        method, path = request.method, request.url.path
        server["calls"].append((method, path.split("/")[-1]))
        body = json.loads(request.content) if request.content else None
        if path == "/api/auth/login":
            assert body["password"] == "password-not-persisted"
            return httpx.Response(200, json={"access_token": "fixture-session-" + "s" * 16})
        if path == "/api/auth/logout" or method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/compute-configuration"):
            return httpx.Response(200, json={"images": server["images"], "policy": server["policy"]})
        if path.endswith("/compute-policy"):
            server["policy"] = body
            return httpx.Response(200, json=body)
        if path.endswith("/compute-scalers") and method == "GET":
            return httpx.Response(200, json=[])
        if path.endswith("/compute-scalers"):
            server["grant_request"] = body
            return httpx.Response(201, json={"id": "5" * 32, "token": "rns_fixture" + "g" * 36})
        raise AssertionError(path)

    def enroll(scaler, directory, origin, organization, name, image_ids, limits, registration, catalog):
        server["enrolled"].append(name)
        record = {"id": "f" * 32, "organization_id": organization, "token": "rnw_fixture" + "w" * 36, "image_ids": image_ids,
                  "resources": limits, "name": name}
        write_document(registration, record, new=True)

    def remote(payload, operation):
        server["remote"].append(operation)
        if operation == "extend":
            server["images"] = payload["images"]
            return {"addition_id": payload["addition_id"], "catalog_installed": True, "relays_active": 0}
        if server["fail_relays"]:
            raise InstallError("fixture relay installation failed")
        assert payload["workers"][0]["enrollment"]["image_ids"] == ["tex"] and "profile" not in payload["workers"][0]
        return {"addition_id": payload["addition_id"], "relays_active": len(payload["workers"]), "live_compute_verified": False}

    monkeypatch.setattr(compute_images, "enroll_worker", enroll)
    target = {"organization_id": "c" * 32, "email": "owner@example.org", "relays": 1, "addition_id": "9" * 64}
    journal = compute_images._journal(work, target, "https://chat.example.org", "a" * 64, server, [relay()], approved(cpu, tex))
    return work, journal, target, server, remote, httpx.MockTransport(handler)


def test_adding_images_extends_first_then_allows_enrolls_installs_and_revokes_the_grant(addition):
    from reasoning_ledger.deployment.compute_images import add_images

    work, journal, target, server, remote, transport = addition
    result = add_images(work, journal, target, "https://chat.example.org", "password-not-persisted", remote, transport=transport)
    assert result == {"addition_id": "9" * 64, "images_added": ["tex"], "relays_active": 1, "workers": ["f" * 32],
                      "live_compute_verified": False}
    assert server["remote"] == ["extend", "relays"]
    assert server["policy"] == {"allowed_images": ["research-cpu", "tex"], "monthly_cpu_seconds": 100000, "max_active_jobs": 2}
    assert server["grant_request"] == {"name": "reasoning-modal-" + "9" * 16, "image_ids": ["tex"], "resources": TEX_LIMITS,
                                       "maximum_workers": 1, "lifetime_seconds": 6 * 3600}
    assert ("DELETE", "5" * 32) in server["calls"] and server["calls"][-1] == ("POST", "logout")
    assert read_document(work / "addition.json")["revoke_phase"] == "ready"
    assert "password-not-persisted" not in json.dumps(read_document(work / "addition.json"))


def test_a_failed_relay_install_resumes_without_a_second_grant_or_enrollment(addition):
    from reasoning_ledger.deployment.compute_images import add_images

    work, journal, target, server, remote, transport = addition
    server["fail_relays"] = True
    with pytest.raises(InstallError, match="fixture relay"):
        add_images(work, journal, target, "https://chat.example.org", "password-not-persisted", remote, transport=transport)
    assert ("DELETE", "5" * 32) not in server["calls"] and server["calls"][-1] == ("POST", "logout")
    server["fail_relays"] = False
    journal = read_document(work / "addition.json")
    add_images(work, journal, target, "https://chat.example.org", "password-not-persisted", remote, transport=transport)
    assert server["enrolled"] == ["reasoning-modal-" + "9" * 16 + "-0"]
    assert server["calls"].count(("POST", "compute-scalers")) == 1
    assert server["remote"] == ["extend", "relays", "extend", "relays"]


def test_add_images_refuses_a_release_without_the_host_operation_before_any_prompt(setup, monkeypatch):
    from reasoning_ledger.deployment import compute_images

    state, calls, _ = setup
    current = {"release_id": "a" * 64, "bundle_sha256": "b" * 64, "public_url": "https://chat.example.org"}
    monkeypatch.setattr(compute_images, "current_binding", lambda _: current)
    monkeypatch.setattr(compute_images, "archived_release", lambda *_: state / "installed.tar.gz")
    monkeypatch.setattr(compute_images, "inspect_bundle", lambda *_: ({"installation_features": ["modal-relay-v1"]}, "a" * 64, "b" * 64))
    monkeypatch.setattr(compute_images, "ask", lambda *_: pytest.fail("must not prompt"))
    with pytest.raises(InstallError, match="make upgrade"):
        compute_images.run_add_images(state, None)
    assert calls == [] and not (state / "compute-images").exists()
