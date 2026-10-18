"""Real transactional initial-owner setup with fixture SSH/systemd/HTTPS transport."""

import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from public_schema_fixture import remove_collaboration_schema
from test_public_deployment import bootstrap as bootstrap
from test_public_deployment import bundle as bundle
from test_public_deployment import payload as payload
from test_public_server_installation import installation as installation

from reasoning_ledger.deployment import cli, host, server_install
from reasoning_ledger.deployment.digitalocean import DigitalOcean
from reasoning_ledger.deployment.private import read_document, write_document
from reasoning_ledger.deployment.server_install import JOURNAL, OWNER_JOURNAL
from reasoning_ledger.server import cli as server_cli
from reasoning_ledger.server.backup import backup, create_key, restore
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.onboarding import canonical_owner, owner_binding, owner_input
from reasoning_ledger.server.security import password_matches, token
from reasoning_ledger.server.store import Store, StoreError


@pytest.fixture
def owner():
    return {
        "format": 1,
        "setup_id": "a" * 32,
        "request_token": token(),
        "email": "owner@example.org",
        "name": "Owner",
        "password": "private original owner password",
        "organization_name": "Mathematics",
    }


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state")


def test_host_bootstrap_is_atomic_sessionless_restart_safe_and_keyed(store, owner):
    first = store.bootstrap_host(store._setup_token, owner)
    assert first == {
        "format": 1,
        **owner_binding(owner),
        "organization_id": first["organization_id"],
        "user_id": first["user_id"],
        "initialized": True,
    }
    restarted = Store(store.data_dir)
    assert restarted.bootstrap_host(restarted._setup_token, owner) == first
    with restarted.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='organization.create'").fetchone()[0] == 1
        receipt = dict(db.execute("SELECT * FROM host_setup_receipt").fetchone())
        assert receipt["request_hash"] != hashlib.sha256(canonical_owner(owner)).hexdigest()
        assert owner["password"] not in json.dumps(receipt) and owner["request_token"] not in json.dumps(receipt)
    assert password_matches(owner["password"], restarted._dummy_password) is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("setup_id", "b" * 32),
        ("request_token", "x" * 43),
        ("email", "other@example.org"),
        ("name", "Other"),
        ("password", "another valid private password"),
        ("organization_name", "Other"),
    ],
)
def test_different_setup_input_never_creates_or_resets_an_initialized_owner(store, owner, key, value):
    first = store.bootstrap_host(store._setup_token, owner)
    with pytest.raises(StoreError) as error:
        store.bootstrap_host(store._setup_token, {**owner, key: value})
    assert error.value.status == 409
    assert store.bootstrap_host(store._setup_token, owner) == first
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert password_matches(owner["password"], db.execute("SELECT password_hash FROM users").fetchone()[0])


def test_public_bootstrap_is_still_one_time_and_cannot_adopt_host_receipt(store, owner):
    store.bootstrap_host(store._setup_token, owner)
    with pytest.raises(StoreError) as error:
        store.bootstrap(store._setup_token, owner["email"], owner["name"], owner["password"], owner["organization_name"])
    assert error.value.status == 409


def test_host_setup_cannot_adopt_an_existing_web_or_prompt_setup(store, owner):
    store.bootstrap(store._setup_token, owner["email"], owner["name"], owner["password"], owner["organization_name"])
    with pytest.raises(StoreError) as error:
        store.bootstrap_host(store._setup_token, owner)
    assert error.value.status == 409
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM host_setup_receipt").fetchone()[0] == 0


def test_wrong_host_local_authority_fails_before_any_changes(store, owner):
    with pytest.raises(StoreError) as error:
        store.bootstrap_host("wrong local setup token", owner)
    assert error.value.status == 403 and store.initialized() is False


def test_initialization_and_receipt_roll_back_together(store, owner, monkeypatch):
    original = store._initial_owner

    def interrupted(*args):
        original(*args)
        raise RuntimeError("synthetic before receipt commit")

    monkeypatch.setattr(store, "_initial_owner", interrupted)
    with pytest.raises(RuntimeError):
        store.bootstrap_host(store._setup_token, owner)
    with store.transaction() as db:
        for table in ("users", "organizations", "memberships", "host_setup_receipt", "audit_events", "sessions"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    assert not store.initialized()
    monkeypatch.setattr(store, "_initial_owner", original)
    assert store.bootstrap_host(store._setup_token, owner)["initialized"] is True


def test_concurrent_identical_delivery_creates_exactly_one_owner_receipt(store, owner):
    stores = [Store(store.data_dir), Store(store.data_dir)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: value.bootstrap_host(value._setup_token, owner), stores))
    assert results[0] == results[1]
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_replay_does_not_undo_a_later_password_reset_or_issue_sessions(store, owner):
    first = store.bootstrap_host(store._setup_token, owner)
    new_password = "later reset password long enough"
    store.reset_password(owner["email"], new_password)
    assert store.bootstrap_host(store._setup_token, owner) == first
    with store.transaction() as db:
        actual = db.execute("SELECT password_hash FROM users").fetchone()[0]
        assert password_matches(new_password, actual) and not password_matches(owner["password"], actual)
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_schema_eight_migration_supports_fresh_host_initialization(tmp_path, owner):
    path = tmp_path / "old"
    path.mkdir(mode=0o700)
    database = path / "server.sqlite3"
    with sqlite3.connect(database) as db:
        for migration in MIGRATIONS[:8]:
            for statement in migration:
                db.execute(statement)
        db.execute("PRAGMA user_version=8")
    database.chmod(0o600)
    # Existing databases always require their original key, even when empty.
    (path / "encryption.key").write_bytes(os.urandom(32))
    (path / "encryption.key").chmod(0o600)
    store = Store(path)
    assert not store.initialized()
    assert store.bootstrap_host(store._setup_token, owner)["initialized"] is True
    with store.transaction() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_schema_eight_existing_owner_is_preserved_and_never_adopted_for_host_replay(store, owner):
    original = store.bootstrap(store._setup_token, owner["email"], owner["name"], owner["password"], owner["organization_name"])
    with store.transaction(write=True) as db:
        remove_collaboration_schema(db)
        for table in (
            "knowledge_writes",
            "knowledge_versions",
            "knowledge_entries",
            "user_presence",
            "bot_webhook_requests",
            "bot_webhook_deliveries",
            "bot_webhooks",
            "workflow_run_actions",
            "workflow_run_events",
            "workflow_node_attempts",
            "workflow_runs",
            "workflow_writes",
            "workflow_versions",
            "workflows",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DROP TABLE invitation_mail")
        db.execute("DROP INDEX invitation_org_identity")
        db.execute("DROP TABLE host_setup_receipt")
        db.execute("PRAGMA user_version=8")
        db.execute("DROP TABLE model_impact_profiles")
        db.execute("ALTER TABLE llm_runs DROP COLUMN impact_profile_json")
        db.execute("ALTER TABLE topics DROP COLUMN default_model")
        db.execute("ALTER TABLE topics DROP COLUMN knowledge_policy_json")
        db.execute("DROP TABLE project_memberships")
        db.execute("ALTER TABLE projects DROP COLUMN visibility")
        db.execute("DROP INDEX message_org_identity")
    upgraded = Store(store.data_dir)
    assert upgraded.session(original["access_token"])["user_id"] == original["user_id"]
    with pytest.raises(StoreError) as error:
        upgraded.bootstrap_host(upgraded._setup_token, owner)
    assert error.value.status == 409
    with upgraded.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM host_setup_receipt").fetchone()[0] == 0
        assert db.execute("SELECT id FROM users").fetchone()[0] == original["user_id"]


def test_encrypted_restore_preserves_exact_replay_and_rotates_local_setup_authority(store, owner, tmp_path):
    first = store.bootstrap_host(store._setup_token, owner)
    key = tmp_path / "backup-key"
    create_key(key)
    archive = tmp_path / "backup.rnbak"
    result = backup(store.data_dir, archive, key)
    destination = tmp_path / "restored"
    restore(archive, key, result["sha256"], destination, source_stopped=True)
    recovered = Store(destination)
    assert recovered._setup_token != store._setup_token
    assert recovered.bootstrap_host(recovered._setup_token, owner) == first


@pytest.mark.parametrize(
    "change", ["short-password", "extra", "bool-format", "bad-token", "bad-id", "uppercase-email", "bad-email", "empty-name"]
)
def test_owner_input_validation_never_echoes_credentials(owner, change):
    value = copy.deepcopy(owner)
    if change == "short-password":
        value["password"] = "secret"
    elif change == "extra":
        value["provider_token"] = "sensitive-extra"
    elif change == "bool-format":
        value["format"] = True
    elif change == "bad-token":
        value["request_token"] = "bad secret token"
    elif change == "bad-id":
        value["setup_id"] = "not-an-id"
    elif change == "uppercase-email":
        value["email"] = "Owner@example.org"
    elif change == "bad-email":
        value["email"] = "owner at example"
    else:
        value["name"] = ""
    with pytest.raises(StoreError) as error:
        owner_input(value)
    assert owner["password"] not in str(error.value) and owner["request_token"] not in str(error.value)
    assert "sensitive-extra" not in str(error.value)


def test_local_setup_file_cli_validates_private_input_and_never_prints_secrets(tmp_path, owner, capsys):
    path = tmp_path / "owner.json"
    write_document(path, owner, new=True)
    directory = tmp_path / "state"
    arguments = [
        "--data-dir",
        str(directory),
        "setup-file",
        "--input",
        str(path),
        "--expected-sha256",
        owner_binding(owner)["input_sha256"],
    ]
    assert server_cli.main(arguments) == 0
    first = capsys.readouterr().out
    assert owner["password"] not in first and owner["request_token"] not in first
    assert server_cli.main(arguments) == 0 and capsys.readouterr().out == first
    assert (directory / "setup-token").read_text() not in first


@pytest.mark.parametrize("change", ["symlink", "public", "hardlink", "wrong-hash", "noncanonical", "duplicate"])
def test_local_setup_file_refuses_untrusted_inputs_before_creating_state(tmp_path, owner, capsys, change):
    path = tmp_path / "owner.json"
    write_document(path, owner, new=True)
    digest = owner_binding(owner)["input_sha256"]
    if change == "symlink":
        original = tmp_path / "original.json"
        path.rename(original)
        path.symlink_to(original)
    elif change == "public":
        path.chmod(0o644)
    elif change == "hardlink":
        os.link(path, tmp_path / "alias.json")
    elif change == "wrong-hash":
        digest = "0" * 64
    else:
        raw = json.dumps(owner).encode() if change == "noncanonical" else b'{"password":"sensitive","password":"duplicate"}'
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
    state = tmp_path / "state"
    with pytest.raises(SystemExit) as error:
        server_cli.main(["--data-dir", str(state), "setup-file", "--input", str(path), "--expected-sha256", digest])
    assert error.value.code == 2 and not state.exists()
    assert owner["password"] not in capsys.readouterr().err


def test_owner_input_command_prompts_generates_private_immutable_nonce_and_normalizes_email(tmp_path, monkeypatch, capsys):
    password = "a private password for the owner"
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: password)
    path = tmp_path / "owner.json"
    arguments = ["owner-input", "--email", "Owner@Example.org", "--name", "Owner", "--organization", "Mathematics", "--output", str(path)]
    assert cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    value = read_document(path)
    assert value["email"] == "owner@example.org" and owner_input(value) == value
    assert result["creates_organization"] is False and result["creates_cloud_resources"] is False
    assert password not in json.dumps(result) and value["request_token"] not in json.dumps(result)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        cli.main(arguments)
    assert read_document(path) == value


@pytest.fixture
def owner_installation(installation, owner, monkeypatch):
    controller, arguments, provider, observations, installer, commands, directory, document, bundle, tmp_path = installation
    original_command = installer.command

    def command(args, **kwargs):
        if args[:3] == ["runuser", "-u", "reasoning"] and args[-1] == "init":
            path = installer.root / "var/lib/reasoning"
            with redirect_stdout(io.StringIO()) as output:
                assert server_cli.main(["--data-dir", str(path), "init"]) == 0
            commands.calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())
        if "setup-file" in args:
            assert owner["password"] not in " ".join(args) and owner["request_token"] not in " ".join(args)
            commands.calls.append(args)
            with redirect_stdout(io.StringIO()) as output:
                assert server_cli.main(args[args.index("--data-dir") :]) == 0
            value = output.getvalue().encode()
            assert owner["password"].encode() not in value and owner["request_token"].encode() not in value
            return subprocess.CompletedProcess(args, 0, stdout=value)
        return original_command(args, **kwargs)

    installer.command = command

    def health(origin):
        assert origin == controller.binding["public_url"]
        observations["https"] += 1
        with sqlite3.connect(installer.root / "var/lib/reasoning/server.sqlite3") as db:
            initialized = bool(db.execute("SELECT initialized FROM installation").fetchone()[0])
        return {"public_url": origin, "https_ready": True, "initialized": initialized}

    monkeypatch.setattr(host, "ready", health)
    monkeypatch.setattr(server_install, "ready", health)

    def install_host(address, request):
        arguments["install_host"](address, request)
        proof = installer.setup_owner(read_document(installer.state_path), canonical_owner(owner), request)
        receipt = installer.receipt(read_document(installer.state_path), request)
        return {**receipt, "owner_setup": proof}

    return installation, {**arguments, "owner": owner_binding(owner), "install_host": install_host, "check_https": health}, owner


def test_owned_vm_setup_creates_real_owner_and_replays_without_cloud_writes(owner_installation):
    installation, arguments, owner = owner_installation
    controller, _, provider, _, installer, _, directory, *_ = installation
    count = len(provider.writes)
    result = controller.install(directory, **arguments)
    assert result["initialized"] is True and result["owner_setup"]["setup_id"] == owner["setup_id"]
    assert read_document(directory / OWNER_JOURNAL)["phase"] == "initialized"
    assert read_document(directory / JOURNAL)["receipt"]["initialized"] is True
    assert "owner_setup" not in read_document(directory / JOURNAL)["receipt"]
    assert controller.install(directory, **arguments) == result and len(provider.writes) == count
    store = Store(installer.root / "var/lib/reasoning")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert list((installer.root / "run").iterdir()) == []
    assert owner["password"] not in json.dumps(result) and owner["request_token"] not in json.dumps(result)


def test_lost_actual_owner_success_reply_resumes_same_owner_not_a_duplicate(owner_installation):
    installation, arguments, _ = owner_installation
    controller, _, _, _, installer, _, directory, *_ = installation
    identifiers = {}

    def lost(address, request):
        receipt = arguments["install_host"](address, request)
        identifiers.update(receipt["owner_setup"])
        raise InstallError("synthetic lost successful owner response")

    from reasoning_ledger.deployment.host import InstallError

    with pytest.raises(InstallError, match="lost"):
        controller.install(directory, **{**arguments, "install_host": lost})
    assert read_document(directory / OWNER_JOURNAL)["phase"] == "pending"
    assert read_document(directory / JOURNAL)["phase"] == "installing"
    result = controller.install(directory, **arguments)
    assert result["owner_setup"] == identifiers
    with Store(installer.root / "var/lib/reasoning").transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE action='organization.create'").fetchone()[0] == 1


def test_different_operator_owner_claim_fails_before_ssh(owner_installation):
    installation, arguments, _ = owner_installation
    controller, _, provider, observations, _, _, directory, *_ = installation
    controller.prepare_launch(directory, owner=arguments["owner"])
    count = len(provider.requests)
    with pytest.raises(host.InstallError, match="original exact private input"):
        controller.install(directory, **{**arguments, "owner": {**arguments["owner"], "setup_id": "b" * 32}})
    assert observations["host"] == 0 and len(provider.requests) == count


@pytest.mark.parametrize("change", ["password", "public", "duplicate", "extra-secret", "oversized"])
def test_invalid_owner_file_refuses_launch_before_provider_access(installation, owner, monkeypatch, capsys, change):
    controller, _, _, _, _, _, directory, _, bundle, tmp_path = installation
    path = tmp_path / "owner.json"
    value = copy.deepcopy(owner)
    if change == "password":
        value["password"] = "secret"
    elif change == "extra-secret":
        value["openrouter_key"] = "do-not-echo-this"
    write_document(path, value, new=True)
    if change == "public":
        path.chmod(0o644)
    elif change == "duplicate":
        path.write_bytes(b'{"password":"do-not-echo-this","password":"duplicate"}')
    elif change == "oversized":
        path.write_bytes(b"x" * 32769)
    monkeypatch.setattr(cli, "DigitalOcean", lambda _: pytest.fail("invalid owner file must precede provider access"))
    with pytest.raises(SystemExit):
        cli.main(
            [
                "do-launch",
                "--token-file",
                "missing",
                "--plan",
                str(tmp_path / "plan.json"),
                "--state-dir",
                str(directory),
                "--bundle",
                str(bundle),
                "--identity",
                str(path),
                "--owner-file",
                str(path),
                "--confirm-plan",
                controller.identity,
                "--confirm-release",
                controller.binding["release_id"],
                "--wait-seconds",
                "0",
            ]
        )
    output = capsys.readouterr()
    assert not output.out and "do-not-echo-this" not in output.err and owner["password"] not in output.err


@pytest.mark.parametrize("mode", ["do-launch", "do-install", "install"])
def test_full_cli_upload_host_main_and_real_account_creation(owner_installation, monkeypatch, capsys, mode):
    import shlex

    import httpx

    installation, _, owner = owner_installation
    controller, _, provider, _, installer, _, directory, _, bundle, tmp_path = installation
    owner_file = tmp_path / "operator-owner.json"
    write_document(owner_file, owner, new=True)
    identity, known, provider_token = (tmp_path / name for name in ("ssh-identity", "known-hosts", "provider-token"))
    for path, body in ((identity, b"fixture-key"), (known, b"fixture-known-key"), (provider_token, provider.token.encode())):
        path.write_bytes(body)
        path.chmod(0o600)
    original_install = installer.install

    def fixture_install(*args, **kwargs):
        return original_install(*args, **{**kwargs, "skip_platform": True})

    monkeypatch.setattr(installer, "install", fixture_install)
    monkeypatch.setattr(host, "HostInstaller", lambda: installer)
    calls, uploads = [], []
    if mode == "do-launch":
        directory = tmp_path / "fresh-owner-ownership"
        for rows in (provider.tags, provider.firewalls, provider.droplets, provider.records):
            rows.clear()

        def before_post(kind, body):
            assert read_document(directory / OWNER_JOURNAL)["phase"] == "pending"
            assert read_document(directory / OWNER_JOURNAL)["binding"] == owner_binding(owner)

        provider.before_post = before_post

    def remote(args, **kwargs):
        calls.append(args)
        assert owner["password"] not in " ".join(args) and owner["request_token"] not in " ".join(args)
        if args[0] == "scp":
            destination = installer.root / args[-1].split(":", 1)[1].lstrip("/")
            destination.write_bytes(Path(args[-2]).read_bytes())
            destination.chmod(0o600)
            uploads.append(destination)
        else:
            parts = shlex.split(args[-1])
            if "--expected-sha256" not in parts:
                installer._directory("var/lib/reasoning-deploy", 0o700)
                installer._directory("var/lib/reasoning-deploy/staging", 0o700)
                installer._directory(parts[-1].lstrip("/"), 0o700)
            else:
                inputs = parts[2:]
                for flag in ("--bundle", "--provisioning", "--owner-input"):
                    if flag in inputs:
                        index = inputs.index(flag) + 1
                        inputs[index] = str(installer.root / inputs[index].lstrip("/"))
                with redirect_stdout(io.StringIO()) as output:
                    assert host.main(inputs) == 0
                return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    monkeypatch.setattr(cli.subprocess, "run", remote)
    monkeypatch.setattr(cli, "DigitalOcean", lambda credential: DigitalOcean(credential, transport=httpx.MockTransport(provider.handle)))
    arguments = [
        mode,
        "--bundle",
        str(bundle),
        "--identity",
        str(identity),
        "--confirm-release",
        controller.binding["release_id"],
        "--owner-file",
        str(owner_file),
    ]
    if mode == "install":
        arguments += ["--public-url", controller.binding["public_url"], "--ssh-host", "139.59.1.2", "--known-hosts", str(known)]
    else:
        arguments += [
            "--token-file",
            str(provider_token),
            "--plan",
            str(tmp_path / "plan.json"),
            "--state-dir",
            str(directory),
            "--confirm-plan",
            controller.identity,
        ]
        arguments += ["--wait-seconds", "0"] if mode == "do-launch" else ["--confirm-droplet", "456"]
    writes = len(provider.writes)
    assert cli.main(arguments) == 0
    first_raw = capsys.readouterr().out
    result = json.loads(first_raw)
    assert result["initialized"] is True and result["owner_setup"]["setup_id"] == owner["setup_id"]
    assert owner["password"] not in first_raw and owner["request_token"] not in first_raw
    assert not next(path for path in uploads if path.name == "owner.json").exists()
    assert owner_file.exists() and list((installer.root / "run").iterdir()) == []
    assert len(provider.writes) == writes + (4 if mode == "do-launch" else 0)
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out) == result
    state = Store(installer.root / "var/lib/reasoning")
    login = state.login(owner["email"], owner["password"], "127.0.0.1")
    invitation = state.invite(login["user_id"], result["owner_setup"]["organization_id"], "member@example.org", "member", 3600)
    accepted = state.accept_invitation(invitation["token"], user_id=None, name="Member", password="member private password long")
    assert accepted["organization_id"] == result["owner_setup"]["organization_id"]
    assert len(calls) == (10 if mode == "install" else 12)


def test_existing_public_app_observes_host_setup_and_supports_owner_login_and_invitation(tmp_path, owner, monkeypatch):
    import asyncio

    import fastapi.dependencies.utils
    import fastapi.routing
    import httpx
    import starlette.concurrency

    from reasoning_ledger.server.api import Config, create_app

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    for module in (fastapi.dependencies.utils, fastapi.routing, starlette.concurrency):
        monkeypatch.setattr(module, "run_in_threadpool", inline)
    origin = "https://chat.example.org"
    app = create_app(Config(tmp_path / "public-state", origin, inference_enabled=False))

    async def journey():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
            assert (await client.get("/api/health")).json()["initialized"] is False
            local = Store(app.state.store.data_dir)
            receipt = local.bootstrap_host(local._setup_token, owner)
            assert (await client.get("/api/health")).json()["initialized"] is True
            response = await client.post(
                "/api/auth/login", json={"email": owner["email"], "password": owner["password"], "transport": "bearer"}
            )
            assert response.status_code == 200
            assert response.json()["user_id"] == receipt["user_id"]
            assert owner["password"] not in response.text and owner["request_token"] not in response.text
            headers = {"Authorization": "Bearer " + response.json()["access_token"]}
            invitation = await client.post(
                f"/api/organizations/{receipt['organization_id']}/invitations",
                headers=headers,
                json={"email": "member@example.org", "role": "member"},
            )
            assert invitation.status_code == 201
            accepted = await client.post(
                "/api/invitations/accept",
                json={
                    "token": invitation.json()["token"],
                    "name": "Member",
                    "password": "member private password long",
                    "transport": "bearer",
                },
            )
            assert accepted.status_code == 200 and accepted.json()["organization_id"] == receipt["organization_id"]
            members = await client.get(f"/api/organizations/{receipt['organization_id']}/members", headers=headers)
            assert members.status_code == 200 and len(members.json()) == 2
            # HTTP callers get no host replay capability, even with the
            # original public setup credential after initialization.
            rejected = await client.post(
                "/api/setup",
                json={
                    "setup_token": local._setup_token,
                    "email": owner["email"],
                    "name": owner["name"],
                    "password": owner["password"],
                    "organization_name": owner["organization_name"],
                },
            )
            assert rejected.status_code == 409

    asyncio.run(journey())
