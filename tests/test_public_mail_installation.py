"""Private mail launch/install through real local account/outbox state and fixture hosts."""

import asyncio
import copy
import io
import json
import os
import shlex
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest
from test_public_deployment import bootstrap as bootstrap
from test_public_deployment import pack
from test_public_deployment import payload as payload
from test_public_owner_setup import owner as owner
from test_public_owner_setup import owner_installation as owner_installation
from test_public_server_installation import installation as installation

from reasoning_ledger.deployment import cli, host, server_install
from reasoning_ledger.deployment.digitalocean import DigitalOcean
from reasoning_ledger.deployment.host import InstallError, canonical, mail_binding
from reasoning_ledger.deployment.private import read_document, write_document, write_private
from reasoning_ledger.deployment.server_install import MAIL_JOURNAL
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.mail import MailConfig, Resend
from reasoning_ledger.server.store import Store

CONFIG = MailConfig("invites@example.org", "re_operator_private_mail_key")
BODY = canonical(CONFIG.document())
BINDING = mail_binding(BODY)
PROVIDER_ID = "a1111111-2222-3333-4444-555555555555"


@pytest.fixture
def bundle(tmp_path, payload):
    return pack(
        tmp_path / "release.tar.gz",
        payload,
        manifest_change=lambda value: value.update(installation_features=list(host.INSTALLATION_FEATURES)),
    )


@pytest.fixture
def mail_installation(owner_installation, monkeypatch):
    base, arguments, owner = owner_installation
    controller, _, provider, observations, installer, commands, directory, document, bundle, tmp_path = base
    original = installer.command
    runtime = {"app": None, "sent": [], "health": [], "fail_health": False}

    def send(request):
        runtime["sent"].append(json.loads(request.content))
        assert str(request.url) == "https://api.resend.com/emails"
        assert request.headers["Authorization"] == "Bearer " + CONFIG.api_key
        return httpx.Response(200, json={"id": PROVIDER_ID})

    def start_service():
        previous = runtime["app"]
        if previous is not None:
            previous.state.mail_worker.stop()
        data = installer.root / "var/lib/reasoning"
        config = MailConfig.read(data / "mail.json")
        app = create_app(
            Config(data, controller.binding["public_url"], inference_enabled=False, mail=config),
            mail_provider=Resend(config, transport=httpx.MockTransport(send)),
        )
        app.state.mail_worker.start()
        runtime["app"] = app

    def command(args, **kwargs):
        assert CONFIG.api_key not in " ".join(args)
        result = original(args, **kwargs)
        if (
            (args[:3] == ["systemctl", "enable", "--now"] and "reasoning-server" in args)
            or args == ["systemctl", "restart", "reasoning-server"]
        ) and (installer.root / "var/lib/reasoning/mail.json").exists():
            start_service()
        return result

    def health(origin, *, require_mail=False, expected_release=None):
        assert origin == controller.binding["public_url"]
        assert expected_release in {None, controller.binding["release_id"]}
        runtime["health"].append(require_mail)
        app = runtime["app"]
        if require_mail and (runtime["fail_health"] or app is None or not app.state.mail_worker.running):
            raise InstallError("mail worker is not ready")
        initialized = Store(installer.root / "var/lib/reasoning").initialized()
        return {"public_url": origin, "https_ready": True, "initialized": initialized}

    installer.command = command
    monkeypatch.setattr(host, "ready", health)
    monkeypatch.setattr(server_install, "ready", health)

    def install_host(address, request):
        observations["host"] += 1
        assert address == "139.59.1.2"
        state = installer.install(
            bundle,
            controller.binding["bundle_sha256"],
            controller.binding["public_url"],
            provisioning=request,
            mail=BODY,
            skip_platform=True,
        )
        return installer.receipt(state, request, BINDING)

    values = {**arguments, "owner": None, "mail": BINDING, "install_host": install_host, "check_https": None}
    try:
        yield base, values, owner, runtime
    finally:
        if runtime["app"] is not None:
            runtime["app"].state.mail_worker.stop()


def test_exact_owned_install_starts_real_worker_and_keeps_secret_out_of_receipts(mail_installation):
    base, arguments, _, runtime = mail_installation
    controller, _, provider, _, installer, commands, directory, *_ = base
    writes = len(provider.writes)
    result = controller.install(directory, **arguments)
    assert result["mail_setup"] == {"format": 1, **BINDING, "configuration_installed": True, "worker_running": True}
    assert read_document(directory / MAIL_JOURNAL)["phase"] == "configured"
    assert runtime["health"].count(True) >= 2 and not runtime["sent"]
    assert (installer.root / "var/lib/reasoning/mail.json").read_bytes() == BODY
    assert (installer.root / "var/lib/reasoning/mail.json").stat().st_mode & 0o777 == 0o600
    assert ["systemctl", "restart", "reasoning-server"] in commands.calls
    assert controller.install(directory, **arguments) == result
    assert len(provider.writes) == writes and not runtime["sent"]
    assert CONFIG.api_key not in json.dumps(result) and CONFIG.api_key not in (directory / MAIL_JOURNAL).read_text()


@pytest.mark.parametrize("change", ["key", "sender", "limit", "omit"])
def test_operator_mail_claim_rejects_changed_or_omitted_input_before_provider_or_host(mail_installation, change):
    base, arguments, _, _ = mail_installation
    controller, _, provider, observations, _, _, directory, *_ = base
    controller.prepare_launch(directory, mail=BINDING)
    value = CONFIG.document()
    if change == "key":
        value["api_key"] = "re_other_private_sending_key"
    elif change == "sender":
        value["sender"] = "changed@example.org"
    else:
        value["daily_limit"] = 50
    changed = {**arguments, "mail": None if change == "omit" else mail_binding(canonical(value))}
    reads, writes = len(provider.requests), len(provider.writes)
    with pytest.raises(InstallError):
        controller.install(directory, **changed)
    assert observations["host"] == 0 and len(provider.requests) == reads and len(provider.writes) == writes


def test_lost_host_success_preserves_pending_claim_and_recovers_exact_original_config(mail_installation):
    base, arguments, _, runtime = mail_installation
    controller, _, provider, _, installer, _, directory, *_ = base
    original = arguments["install_host"]

    def lose(address, request):
        original(address, request)
        raise InstallError("lost SSH response")

    with pytest.raises(InstallError):
        controller.install(directory, **{**arguments, "install_host": lose})
    assert read_document(directory / MAIL_JOURNAL)["phase"] == "pending"
    assert (installer.root / "var/lib/reasoning/mail.json").read_bytes() == BODY
    writes = len(provider.writes)
    assert controller.install(directory, **arguments)["mail_setup"]["worker_running"] is True
    assert len(provider.writes) == writes and not runtime["sent"]


def test_changed_host_mail_is_refused_before_package_commands(mail_installation):
    base, arguments, _, _ = mail_installation
    controller, _, _, _, installer, commands, directory, *_ = base
    controller.install(directory, **arguments)
    path = installer.root / "var/lib/reasoning/mail.json"
    changed = {**CONFIG.document(), "api_key": "re_operator_rotated_private_key"}
    path.write_bytes(canonical(changed))
    count = len(commands.calls)
    with pytest.raises(InstallError, match="managed mail configuration"):
        controller.install(directory, **arguments)
    assert len(commands.calls) == count and path.read_bytes() == canonical(changed)


def test_existing_unmanaged_mail_is_not_adopted(mail_installation):
    base, arguments, _, _ = mail_installation
    controller, _, _, _, installer, commands, directory, *_ = base
    # Install normally with the existing owner fixture, then add operator-managed mail.
    state = installer.install(base[-2], controller.binding["bundle_sha256"], controller.binding["public_url"], skip_platform=True)
    assert state["phase"] == "services_started"
    path = installer.root / "var/lib/reasoning/mail.json"
    write_private(path, BODY)
    count = len(commands.calls)
    with pytest.raises(InstallError, match="unmanaged mail"):
        controller.install(directory, **arguments)
    assert len(commands.calls) == count and path.read_bytes() == BODY


def test_mail_health_failure_never_publishes_configured_receipt(mail_installation):
    base, arguments, _, runtime = mail_installation
    controller, _, _, _, _, _, directory, *_ = base
    runtime["fail_health"] = True
    with pytest.raises(InstallError, match="mail worker is not ready"):
        controller.install(directory, **arguments)
    assert read_document(directory / MAIL_JOURNAL)["phase"] == "pending"
    runtime["fail_health"] = False
    assert controller.install(directory, **arguments)["mail_setup"]["configuration_installed"]


@pytest.mark.parametrize("change", ["removed", "symlink", "hardlink", "public", "directory"])
def test_insecure_or_removed_managed_secret_is_preserved_and_refused_before_packages(mail_installation, change):
    base, arguments, _, _ = mail_installation
    controller, _, _, _, installer, commands, directory, *_ = base
    controller.install(directory, **arguments)
    path = installer.root / "var/lib/reasoning/mail.json"
    if change == "removed":
        path.unlink()
    elif change == "symlink":
        original = path.with_name("original-mail.json")
        path.rename(original)
        path.symlink_to(original)
    elif change == "hardlink":
        os.link(path, path.with_name("other-mail.json"))
    elif change == "directory":
        path.parent.chmod(0o755)
    else:
        path.chmod(0o644)
    count = len(commands.calls)
    with pytest.raises(InstallError, match="managed mail configuration"):
        controller.install(directory, **arguments)
    assert len(commands.calls) == count
    if change == "removed":
        assert not path.exists()
    else:
        assert path.read_bytes() == BODY


def test_mail_publication_failure_can_resume_without_partial_or_root_owned_secret(mail_installation, monkeypatch):
    base, arguments, _, _ = mail_installation
    controller, _, _, _, installer, _, directory, *_ = base
    original = host.os.link
    path = installer.root / "var/lib/reasoning/mail.json"

    def interrupt(source, destination, **kwargs):
        if Path(destination) == path:
            raise OSError("fixture publication interruption")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(host.os, "link", interrupt)
    with pytest.raises(OSError):
        controller.install(directory, **arguments)
    assert not path.exists() and not list(path.parent.glob(".mail-*"))
    assert read_document(directory / MAIL_JOURNAL)["phase"] == "pending"
    monkeypatch.setattr(host.os, "link", original)
    assert controller.install(directory, **arguments)["mail_setup"]["worker_running"]
    assert path.stat().st_uid == os.getuid() and path.stat().st_nlink == 1 and path.read_bytes() == BODY


@pytest.mark.parametrize("flag", [None, False, True, "true", 1])
def test_https_readiness_requires_a_real_boolean_worker_flag(monkeypatch, flag):
    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, maximum):
            assert maximum == 65537
            value = {"status": "ok", "initialized": True}
            if flag is not None:
                value["mail_worker_running"] = flag
            return json.dumps(value).encode()

    class Opener:
        def open(self, url, timeout):
            assert url == "https://chat.example.org/api/health" and timeout == 3
            return Reply()

    monkeypatch.setattr(host.urllib.request, "build_opener", lambda *_args: Opener())
    if flag is True:
        assert host.ready("https://chat.example.org", timeout=0, require_mail=True)["https_ready"]
    else:
        with pytest.raises(InstallError):
            host.ready("https://chat.example.org", timeout=0, require_mail=True)


def test_old_release_is_refused_before_provider_construction_for_mail_launch(mail_installation, monkeypatch, capsys, payload):
    base, _, _, _ = mail_installation
    controller, _, _, _, _, _, _, _, _, tmp_path = base
    old_bundle = pack(tmp_path / "old-release.tar.gz", payload)
    release_id = host.inspect_bundle(old_bundle)[1]
    mail_file, identity = tmp_path / "mail.json", tmp_path / "identity"
    write_private(mail_file, BODY)
    write_private(identity, b"identity-fixture")

    def refuse(_credential):
        raise AssertionError("old mail-incompatible release must not access provider")

    monkeypatch.setattr(cli, "DigitalOcean", refuse)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "do-launch",
                "--bundle",
                str(old_bundle),
                "--identity",
                str(identity),
                "--confirm-release",
                release_id,
                "--mail-file",
                str(mail_file),
                "--token-file",
                str(tmp_path / "unused-token"),
                "--plan",
                str(tmp_path / "plan.json"),
                "--state-dir",
                str(tmp_path / "fresh"),
                "--confirm-plan",
                controller.identity,
                "--wait-seconds",
                "0",
            ]
        )
    assert "mail-enabled bundle" in capsys.readouterr().err and not (tmp_path / "fresh").exists()


@pytest.mark.parametrize(
    "features", [False, "invitation-mail-v1", [True], ["duplicate", "duplicate"], ["z", "a"], ["bad value"], ["a"] * 33]
)
def test_manifest_features_are_bounded_unique_canonical_identifiers(bundle, features):
    manifest = host.inspect_bundle(bundle)[0]
    manifest["installation_features"] = features
    with pytest.raises(InstallError, match="invalid release manifest"):
        host.validate_manifest(canonical(manifest))


def test_mail_input_prompts_privately_and_creates_canonical_runtime_compatible_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "mail.json"
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: CONFIG.api_key)
    args = ["mail-input", "--sender", CONFIG.sender, "--output", str(path), "--daily-limit", "100"]
    assert cli.main(args) == 0
    assert path.stat().st_mode & 0o777 == 0o600 and path.read_bytes() == BODY
    assert cli.read_mail(path) == CONFIG.document() and MailConfig.read(path) == CONFIG
    result = json.loads(capsys.readouterr().out)
    assert result["creates_cloud_resources"] is False and result["sends_email"] is False
    assert CONFIG.api_key not in json.dumps(result)
    with pytest.raises(SystemExit):
        cli.main(args)
    assert path.read_bytes() == BODY


def test_inspect_exposes_release_installation_features_without_secrets(bundle, capsys):
    assert cli.main(["inspect", "--bundle", str(bundle)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["installation_features"] == list(host.INSTALLATION_FEATURES)
    assert value["database_schema"] == host.inspect_bundle(bundle)[0]["database_schema"]
    assert CONFIG.api_key not in json.dumps(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", True),
        ("input_sha256", "b" * 64),
        ("configuration_installed", False),
        ("worker_running", False),
        ("api_key", CONFIG.api_key),
    ],
)
def test_mail_receipt_cannot_claim_other_input_or_expose_secret(field, value):
    receipt = {"format": 1, **BINDING, "configuration_installed": True, "worker_running": True}
    receipt[field] = value
    with pytest.raises(InstallError) as error:
        host.mail_result(receipt, BINDING)
    assert CONFIG.api_key not in str(error.value)


@pytest.mark.parametrize("mode", ["install", "do-install", "do-launch"])
@pytest.mark.parametrize("with_owner", [False, True])
def test_full_cli_private_copy_host_bootstrap_and_real_mailed_join(mail_installation, monkeypatch, capsys, mode, with_owner):
    base, _, owner, runtime = mail_installation
    controller, _, provider, _, installer, _, directory, _, bundle, tmp_path = base
    mail_file, owner_file = tmp_path / "operator-mail.json", tmp_path / "operator-owner.json"
    write_private(mail_file, BODY)
    write_document(owner_file, owner, new=True)
    identity, known, provider_token = (tmp_path / name for name in ("identity", "known", "provider-token"))
    for path, body in ((identity, b"ssh-fixture"), (known, b"host-fixture"), (provider_token, provider.token.encode())):
        write_private(path, body)
    original = installer.install
    monkeypatch.setattr(installer, "install", lambda *args, **kwargs: original(*args, **kwargs, skip_platform=True))
    monkeypatch.setattr(host, "HostInstaller", lambda: installer)
    calls, uploads = [], []

    def remote(args, **_kwargs):
        calls.append(args)
        assert CONFIG.api_key not in " ".join(args) and owner["password"] not in " ".join(args)
        if args[0] == "scp":
            source = Path(args[-2])
            destination = installer.root / args[-1].split(":", 1)[1].lstrip("/")
            if source.name == "mail.json":
                assert source.stat().st_mode & 0o777 == 0o600 and source.read_bytes() == BODY
            destination.write_bytes(source.read_bytes())
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
                for flag in ("--bundle", "--provisioning", "--owner-input", "--mail-input"):
                    if flag in inputs:
                        index = inputs.index(flag) + 1
                        inputs[index] = str(installer.root / inputs[index].lstrip("/"))
                with redirect_stdout(io.StringIO()) as output:
                    assert host.main(inputs) == 0
                return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    monkeypatch.setattr(cli.subprocess, "run", remote)
    monkeypatch.setattr(cli, "DigitalOcean", lambda credential: DigitalOcean(credential, transport=httpx.MockTransport(provider.handle)))
    if mode == "do-launch":
        directory = tmp_path / "fresh-mail-launch"
        for rows in (provider.tags, provider.firewalls, provider.droplets, provider.records):
            rows.clear()
    args = [
        mode,
        "--bundle",
        str(bundle),
        "--identity",
        str(identity),
        "--confirm-release",
        controller.binding["release_id"],
        "--mail-file",
        str(mail_file),
    ]
    if with_owner:
        args += ["--owner-file", str(owner_file)]
    if mode == "install":
        args += ["--public-url", controller.binding["public_url"], "--ssh-host", "139.59.1.2", "--known-hosts", str(known)]
    else:
        args += [
            "--token-file",
            str(provider_token),
            "--plan",
            str(tmp_path / "plan.json"),
            "--state-dir",
            str(directory),
            "--confirm-plan",
            controller.identity,
        ]
        args += ["--wait-seconds", "0"] if mode == "do-launch" else ["--confirm-droplet", "456"]
    writes = len(provider.writes)
    assert cli.main(args) == 0
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["mail_setup"]["worker_running"] is True and CONFIG.api_key not in raw
    assert not runtime["sent"] and mail_file.exists()
    assert not next(path for path in uploads if path.name == "mail.json").exists()
    assert len(provider.writes) == writes + (4 if mode == "do-launch" else 0)
    assert all(CONFIG.api_key not in request.content.decode() for request in provider.writes)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == result
    app = runtime["app"]
    state = app.state.store
    if with_owner:
        first_owner = state.login(owner["email"], owner["password"], "127.0.0.1")
        first_owner["organization_id"] = result["owner_setup"]["organization_id"]
    else:
        first_owner = state.bootstrap(state._setup_token, owner["email"], owner["name"], owner["password"], owner["organization_name"])
    invite = app.state.mail.enqueue(
        first_owner["user_id"], first_owner["organization_id"], "member@example.org", "member", 3600, "new_invitation_1234"
    )
    deadline = time.monotonic() + 5
    while not runtime["sent"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(runtime["sent"]) == 1
    credential = runtime["sent"][0]["text"].split("#invite=")[1].splitlines()[0]
    assert credential == invite["token"]
    member = state.accept_invitation(credential, user_id=None, name="Member", password="private member password long")
    assert member["organization_id"] == first_owner["organization_id"]
    assert len(state.members(first_owner["user_id"], first_owner["organization_id"])) == 2


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "duplicate", "extra", "oversize", "bad_sender", "bool_limit"])
def test_bad_mail_file_is_rejected_before_paid_provider_access(mail_installation, monkeypatch, capsys, kind):
    base, _, _, _ = mail_installation
    controller, _, provider, _, _, _, _, _, bundle, tmp_path = base
    path = tmp_path / "mail.json"
    value = copy.deepcopy(CONFIG.document())
    if kind == "extra":
        value["endpoint"] = "https://untrusted.example.org"
    if kind == "bad_sender":
        value["sender"] = "one@example.org,two@example.org"
    if kind == "bool_limit":
        value["daily_limit"] = True
    body = canonical(value)
    if kind == "duplicate":
        body = body[:-2] + b',"api_key":"never_echo_me"}\n'
    if kind == "oversize":
        body = b"never_echo_me" * 1000
    write_private(path, body)
    if kind == "mode":
        path.chmod(0o644)
    if kind == "hardlink":
        os.link(path, tmp_path / "other.json")
    if kind == "symlink":
        path.rename(tmp_path / "original.json")
        path.symlink_to(tmp_path / "original.json")
    identity = tmp_path / "identity"
    write_private(identity, b"ssh-fixture")

    def refuse(_credential):
        raise AssertionError("provider must not be accessed")

    monkeypatch.setattr(cli, "DigitalOcean", refuse)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "do-launch",
                "--bundle",
                str(bundle),
                "--identity",
                str(identity),
                "--confirm-release",
                controller.binding["release_id"],
                "--mail-file",
                str(path),
                "--token-file",
                str(tmp_path / "unused-token"),
                "--plan",
                str(tmp_path / "plan.json"),
                "--state-dir",
                str(tmp_path / "fresh"),
                "--confirm-plan",
                controller.identity,
                "--wait-seconds",
                "0",
            ]
        )
    output = capsys.readouterr()
    assert not output.out and CONFIG.api_key not in output.err and "never_echo_me" not in output.err
    assert provider is not None and not (tmp_path / "fresh").exists()


def test_real_health_exposes_only_worker_liveness_not_mail_configuration(tmp_path, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing
    import starlette.concurrency

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    for module in (fastapi.dependencies.utils, fastapi.routing, starlette.concurrency):
        monkeypatch.setattr(module, "run_in_threadpool", inline)
    app = create_app(Config(tmp_path / "state", "https://chat.example.org", inference_enabled=False, mail=CONFIG))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://chat.example.org") as client:
            result = await client.get("/api/health")
            assert {key: result.json()[key] for key in ("status", "initialized", "mail_worker_running")} == {
                "status": "ok", "initialized": False, "mail_worker_running": False}
            async with app.router.lifespan_context(app):
                result = await client.get("/api/health")
                assert result.json()["mail_worker_running"] is True
            assert CONFIG.api_key not in result.text and CONFIG.sender not in result.text

    asyncio.run(run())
