"""Real local files/SQLite and bounded transport fixtures; no live cloud install."""

import copy
import email.message
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_public_deployment import bootstrap as bootstrap
from test_public_deployment import pack
from test_public_deployment import payload as payload

from reasoning_ledger.deployment import cli, host
from reasoning_ledger.deployment import server_diagnostics as transport
from reasoning_ledger.deployment import server_operations as operations
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.mail import MailConfig
from reasoning_ledger.server.migrations import MIGRATIONS
from reasoning_ledger.server.store import Store

ORIGIN = "https://chat.example.org"


def wheel_fixture():
    files = {
        "reasoning_ledger/__init__.py": b"",
        "reasoning_ledger/server/__init__.py": b"",
        "reasoning_ledger/server/api.py": b"# Non-executed wheel-source comparison fixture\n",
        "reasoning_ledger/server/cli.py": b"# Non-executed wheel-source comparison fixture\n",
        "reasoning_ledger-0.1.0.dist-info/METADATA": b"Metadata-Version: 2.3\nName: reasoning-ledger\nVersion: 0.1.0\n",
    }
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)
    return raw.getvalue(), files


@pytest.fixture
def installed(tmp_path, payload, bootstrap):
    installer, commands = bootstrap
    wheel, files = wheel_fixture()
    payload["wheel/reasoning_ledger-0.1.0-py3-none-any.whl"] = wheel
    archive = pack(
        tmp_path / "source.tar.gz",
        payload,
        manifest_change=lambda manifest: manifest.update(
            database_schema=len(MIGRATIONS), installation_features=list(host.INSTALLATION_FEATURES)
        ),
    )
    manifest, release, checksum = host.inspect_bundle(archive)
    state = installer.install(archive, checksum, ORIGIN, skip_platform=True)
    directory = installer.root / "opt/reasoning/releases" / release
    for name, body in files.items():
        path = directory / "venv/lib/python3.12/site-packages" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in path.parents:
            if parent == directory:
                break
            parent.chmod(0o755)
        path.write_bytes(body)
        path.chmod(0o644)
    Store(installer.root / "var/lib/reasoning")
    observations = {"unit": {}, "health": [], "commands": []}

    def command(args):
        observations["commands"].append(args)
        assert args[:3] == ["systemctl", "show", "--no-pager"]
        account = "reasoning" if args[-1] == "reasoning-server" else "caddy"
        properties = {
            "FragmentPath": f"/etc/systemd/system/{args[-1]}.service",
            "DropInPaths": "",
            "User": account,
            "Group": account,
            "LoadState": "loaded",
            "NeedDaemonReload": "no",
            "ActiveState": "active",
            **observations["unit"],
        }
        return subprocess.CompletedProcess(args, 0, stdout="".join(f"{key}={item}\n" for key, item in properties.items()).encode())

    def health(origin, **options):
        assert origin == ORIGIN and options["timeout"] == 0
        assert options.get("expected_release") in {None, release}
        observations["health"].append(options)
        return {"public_url": ORIGIN, "https_ready": True, "initialized": False}

    inspector = operations.ServerInspector(root=installer.root, command=command, health=health, droplet_id=lambda: 456)
    return inspector, manifest, release, checksum, archive, state, observations, payload


def diagnose(installed):
    inspector, manifest, release, checksum, *_ = installed
    return inspector.inspect(manifest, release, checksum, ORIGIN)


def fingerprint(root):
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            path.lstat().st_uid,
            path.lstat().st_ino,
            path.lstat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


def test_doctor_reads_real_state_without_opening_database_or_mutating_host(installed, monkeypatch):
    inspector, manifest, release, checksum, *_ = installed
    before = fingerprint(inspector.root)
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: pytest.fail("diagnosis must not open SQLite"))
    report = diagnose(installed)
    assert report["all_checks_passed"] is True, report
    assert report["checks"]["mail_configuration"] == report["checks"]["provisioning_identity"] == "not_applicable"
    assert report["expected_database_schema"] == len(MIGRATIONS)
    assert report["database_opened"] is report["host_changed"] is False
    assert report["pending_upgrade_checks"] == operations.PENDING
    assert fingerprint(inspector.root) == before
    assert operations.validate_report(report, operations.release_binding(manifest, release, checksum, ORIGIN), len(MIGRATIONS)) == report


def test_host_commands_cannot_inherit_public_writable_package_permissions(tmp_path):
    previous = os.umask(0o002)
    try:
        host.HostInstaller._command(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "from pathlib import Path; import sys; path=Path(sys.argv[1]); path.mkdir(); (path / 'file').write_bytes(b'fixture')",
                str(tmp_path / "command-output"),
            ]
        )
    finally:
        os.umask(previous)
    assert (tmp_path / "command-output").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "command-output/file").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize(
    "change", ["missing", "changed", "symlink", "hardlink", "public-write", "linked-parent", "private-key-public", "state-public"]
)
def test_doctor_detects_protected_file_and_parent_drift(installed, tmp_path, change):
    inspector, _, release, *_ = installed
    file = inspector.root / "opt/reasoning/releases" / release / "web/assets/shared-x.js"
    if change == "missing":
        file.unlink()
    elif change == "changed":
        file.write_bytes(b"wrong release bytes")
    elif change == "symlink":
        file.unlink()
        file.symlink_to(tmp_path / "outside")
    elif change == "hardlink":
        os.link(file, tmp_path / "outside")
    elif change == "public-write":
        file.chmod(0o666)
    elif change == "linked-parent":
        file.parent.rename(tmp_path / "outside-directory")
        file.parent.symlink_to(tmp_path / "outside-directory", target_is_directory=True)
    elif change == "private-key-public":
        (inspector.root / "var/lib/reasoning/encryption.key").chmod(0o644)
    elif change == "state-public":
        (inspector.root / "var/lib/reasoning").chmod(0o755)
    report = diagnose(installed)
    name = "private_server_state" if change in {"private-key-public", "state-public"} else "release_payload"
    assert report["checks"][name] == "fail" and report["all_checks_passed"] is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("release_id", "0" * 64),
        ("bundle_sha256", "0" * 64),
        ("source_revision", "0" * 40),
        ("dirty", True),
        ("phase", "packages"),
        ("public_url", "https://other.example.org"),
        ("extra", "secret-do-not-print"),
    ],
)
def test_journal_must_match_exact_installation_not_just_release(installed, key, value):
    inspector, *_ = installed
    path = inspector.root / "var/lib/reasoning-deploy/installation.json"
    state = json.loads(path.read_bytes())
    state[key] = value
    host.atomic_write(path, host.canonical(state))
    report = diagnose(installed)
    assert report["checks"]["installation_journal"] == "fail"
    assert "secret-do-not-print" not in json.dumps(report)


@pytest.mark.parametrize(
    "key,value",
    [
        ("DropInPaths", "/secret/override.conf"),
        ("NeedDaemonReload", "yes"),
        ("User", "root"),
        ("Group", "root"),
        ("FragmentPath", "/secret/other.service"),
        ("LoadState", "not-found"),
        ("extra", "secret-do-not-print"),
    ],
)
def test_loaded_service_overrides_reload_and_identity_drift_are_not_success(installed, key, value):
    installed[6]["unit"][key] = value
    report = diagnose(installed)
    assert report["checks"]["loaded_server_unit"] == report["checks"]["loaded_proxy_unit"] == "fail"
    assert report["checks"]["server_active"] == "unverified" and report["all_checks_passed"] is False
    assert "secret" not in json.dumps(report)


def test_inactive_server_still_returns_read_only_configuration_diagnostics(installed):
    installed[6]["unit"]["ActiveState"] = "inactive"
    report = diagnose(installed)
    assert report["checks"]["loaded_server_unit"] == "pass"
    assert report["checks"]["server_active"] == "fail"
    assert report["all_checks_passed"] is False


def test_busy_bootstrap_lock_is_not_created_modified_or_mistaken_for_upgrade_ownership(installed):
    path = installed[0].root / "var/lib/reasoning-deploy/bootstrap.lock"
    before = path.stat()
    with path.open("rb") as source:
        fcntl.flock(source, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = diagnose(installed)
    assert report["checks"]["bootstrap_lock"] == "fail"
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    path.unlink()
    assert diagnose(installed)["checks"]["bootstrap_lock"] == "fail" and not path.exists()


@pytest.mark.parametrize("change", ["changed", "added", "linked", "malformed-wheel", "stale-schema"])
def test_installed_python_source_verification_is_distinct_from_archive_and_schema_claims(installed, change):
    inspector, _, release, *_ = installed
    site = inspector.root / "opt/reasoning/releases" / release / "venv/lib/python3.12/site-packages"
    api = site / "reasoning_ledger/server/api.py"
    if change == "changed":
        api.write_bytes(b"changed executable source")
    elif change == "added":
        (api.parent / "unapproved.py").write_bytes(b"unexpected executable source")
    elif change == "linked":
        api.unlink()
        api.symlink_to(site / "reasoning_ledger/server/cli.py")
    elif change == "malformed-wheel":
        next((inspector.root / "opt/reasoning/releases" / release / "wheel").iterdir()).write_bytes(b"not a wheel")
    elif change == "stale-schema":
        with sqlite3.connect(inspector.root / "var/lib/reasoning/server.sqlite3") as database:
            database.execute("PRAGMA user_version=1")
    report = diagnose(installed)
    if change == "stale-schema":
        # Doctor does not falsely claim to inspect/migrate the actual DB schema.
        assert report["all_checks_passed"] and report["database_opened"] is False
    else:
        assert report["checks"]["installed_python_sources"] == "fail"


@pytest.mark.parametrize("change", ["none", "unmanaged", "missing", "changed", "linked"])
def test_private_mail_digest_and_managed_installation_binding(installed, change):
    inspector, manifest, release, checksum, *_ = installed
    path = inspector.root / "var/lib/reasoning/mail.json"
    body = host.canonical(MailConfig("invites@example.org", "private-key-not-in-report", 100).document())
    if change != "none":
        host.atomic_write(path, body)
        if change != "unmanaged":
            value = {"installation": operations.release_binding(manifest, release, checksum, ORIGIN), "mail": host.mail_binding(body)}
            host.atomic_write(inspector.root / "var/lib/reasoning-deploy/server-mail.json", host.canonical(value))
        if change == "missing":
            path.unlink()
        elif change == "changed":
            host.atomic_write(path, body.replace(b"100", b"101"))
        elif change == "linked":
            path.unlink()
            path.symlink_to(inspector.root / "var/lib/reasoning/setup-token")
    report = diagnose(installed)
    assert report["checks"]["mail_configuration"] == ("not_applicable" if change == "none" else "fail")
    assert "private-key" not in json.dumps(report) and "invites@" not in json.dumps(report)


def test_managed_mail_and_original_cloud_identity_pass_without_provider_api_or_secret_output(installed):
    inspector, manifest, release, checksum, *_ = installed
    binding = operations.release_binding(manifest, release, checksum, ORIGIN)
    body = host.canonical(MailConfig("invites@example.org", "secret-key-fixture", 100).document())
    host.atomic_write(inspector.root / "var/lib/reasoning/mail.json", body)
    host.atomic_write(
        inspector.root / "var/lib/reasoning-deploy/server-mail.json",
        host.canonical({"installation": binding, "mail": host.mail_binding(body)}),
    )
    request = {"format": 1, "plan_id": "a" * 64, "deployment_id": "b" * 32, "droplet_id": 456}
    host.atomic_write(
        inspector.root / "var/lib/reasoning-deploy/server-provisioning.json",
        host.canonical({"format": 1, "binding": binding, "request": request}),
    )
    host.atomic_write(inspector.root / "etc/reasoning-provisioning.json", host.canonical({"plan_id": "a" * 64, "deployment_id": "b" * 32}))
    report = diagnose(installed)
    assert report["all_checks_passed"] and report["checks"]["provisioning_identity"] == "pass"
    assert all(options["require_mail"] for options in installed[6]["health"])
    assert report["configuration"]["mail_input_sha256"] == hashlib.sha256(body).hexdigest()
    inspector.droplet_id = lambda: 789
    assert diagnose(installed)["checks"]["provisioning_identity"] == "fail"


def target(installed, tmp_path, **changes):
    payload = dict(installed[-1])
    payload["NOTICE"] += b" target release"
    path = pack(
        tmp_path / "target.tar.gz",
        payload,
        manifest_change=lambda manifest: manifest.update(
            {"database_schema": len(MIGRATIONS), "installation_features": list(host.INSTALLATION_FEATURES), **changes}
        ),
    )
    return (path, *host.inspect_bundle(path))


def test_upgrade_plan_binds_all_inputs_and_never_claims_execution(installed, tmp_path):
    _, manifest, release, checksum = target(installed, tmp_path)
    result = operations.upgrade_plan(diagnose(installed), installed[1], manifest, release, checksum)
    assert result["preflight_checks_passed"] and not result["refusals"]
    assert result["upgrade_performed"] is result["host_changed"] is False
    assert result["pending_upgrade_checks"] == operations.PENDING
    installed[6]["unit"]["ActiveState"] = "inactive"
    failed = operations.upgrade_plan(diagnose(installed), installed[1], manifest, release, checksum)
    assert not failed["preflight_checks_passed"]


def test_legacy_source_health_does_not_falsely_gain_release_identity(installed, tmp_path):
    inspector, manifest, _, _, *_ = installed
    legacy = copy.deepcopy(manifest)
    legacy["installation_features"] = []
    release = hashlib.sha256(host.canonical(legacy)).hexdigest()
    original = inspector.root / "opt/reasoning/releases" / installed[2]
    original.rename(original.parent / release)
    binding = operations.release_binding(legacy, release, installed[3], ORIGIN)
    host.atomic_write(
        inspector.root / "var/lib/reasoning-deploy/installation.json", host.canonical({**binding, "phase": "services_started"})
    )
    host.atomic_write(
        inspector.root / "etc/systemd/system/reasoning-server.service",
        host.server_unit(release).replace("PUBLIC_ORIGIN", ORIGIN).encode(),
        0o644,
    )
    inspector.health = lambda origin, **_options: {"public_url": origin, "https_ready": True, "initialized": False}
    report = inspector.inspect(legacy, release, installed[3], ORIGIN)
    assert report["checks"]["https_health"] == "pass" and report["checks"]["https_release_identity"] == "unverified"
    assert report["all_checks_passed"] is False
    _, target_manifest, target_release, target_checksum = target(installed, tmp_path)
    result = operations.upgrade_plan(report, legacy, target_manifest, target_release, target_checksum)
    assert result["preflight_checks_passed"] and result["upgrade_performed"] is False


@pytest.mark.parametrize(
    "change,refusal",
    [
        ("dirty", "uncommitted_release"),
        ("downgrade", "schema_downgrade_requires_explicit_snapshot_restore"),
        ("legacy", "target_has_no_https_release_identity"),
        ("same", "target_is_current_release"),
        ("source-header", "installed_host_checks_failed_or_unverified"),
    ],
)
def test_upgrade_plan_refuses_unsafe_inputs_without_downgrade_guesses(installed, tmp_path, change, refusal):
    path, manifest, release, checksum = target(installed, tmp_path)
    if change == "same":
        manifest, release, checksum = installed[1:4]
    elif change in {"dirty", "downgrade", "legacy"}:
        manifest = copy.deepcopy(manifest)
        if change == "dirty":
            manifest["dirty"] = True
        elif change == "downgrade":
            manifest["database_schema"] = len(MIGRATIONS) - 1
        else:
            manifest["installation_features"] = []
        release = hashlib.sha256(host.canonical(manifest)).hexdigest()
    report = diagnose(installed)
    if change == "source-header":
        report["checks"]["https_release_identity"] = "fail"
        report["all_checks_passed"] = False
    result = operations.upgrade_plan(report, installed[1], manifest, release, checksum)
    assert refusal in result["refusals"] and result["upgrade_performed"] is False


@pytest.mark.parametrize(
    "change", ["extra", "stale", "true-int", "checks", "secret", "claimed-mutation", "claimed-success", "inapplicable"]
)
def test_remote_reports_have_exact_secret_free_shape_and_binding(installed, change):
    report = diagnose(installed)
    if change == "extra":
        report["secret"] = "never print"
    elif change == "stale":
        report["installation"]["release_id"] = "0" * 64
    elif change == "true-int":
        report["format"] = True
    elif change == "checks":
        report["checks"]["extra"] = "pass"
    elif change == "secret":
        report["configuration"]["mail_input_sha256"] = "raw sending key"
    elif change == "claimed-mutation":
        report["host_changed"] = True
    elif change == "claimed-success":
        report["checks"]["server_unit"] = "fail"
    elif change == "inapplicable":
        report["checks"]["server_unit"] = "not_applicable"
    binding = operations.release_binding(installed[1], installed[2], installed[3], ORIGIN)
    with pytest.raises(host.InstallError, match="receipt"):
        operations.validate_report(report, binding, len(MIGRATIONS))


def ssh_inputs(tmp_path):
    identity, known = tmp_path / "identity", tmp_path / "known"
    identity.write_bytes(b"nonfunctional private key fixture")
    identity.chmod(0o600)
    known.write_bytes(b"nonfunctional verified host-key fixture")
    known.chmod(0o644)
    return identity, known


def test_remote_doctor_uses_isolated_stdin_tooling_no_upload_or_host_staging(installed, tmp_path):
    identity, known = ssh_inputs(tmp_path)
    observations = []

    def command(args, script):
        observations.append((args, script))
        assert args[0:2] == ["ssh", "-T"] and args[-1] == "python3 -I -S -B -"
        assert "StrictHostKeyChecking=yes" in args and "-F" in args
        assert b"installer = types.ModuleType" in script and b"ServerInspector().inspect" in script
        compile(script, "<remote-doctor>", "exec")
        return subprocess.CompletedProcess(args, 0, stdout=host.canonical(diagnose(installed)))

    before = fingerprint(installed[0].root)
    result = transport.diagnose_remote(
        installed[4], ORIGIN, "server.example.org", identity, known, confirmed_release=installed[2], command=command
    )
    assert result["all_checks_passed"] and len(observations) == 1
    assert fingerprint(installed[0].root) == before
    assert not (installed[0].root / "var/lib/reasoning-deploy/staging").exists()


def test_isolated_standalone_remote_script_executes_without_application_imports(installed):
    inspector, manifest, release, checksum, *_ = installed
    script = transport.remote_script(manifest, release, checksum, ORIGIN)
    # Explicit local fixture substitutions, not root/SSH/cloud proof. Actual
    # bundled standard-library sources execute in a -I -S -B Python process.
    script = script.replace(b"if os.geteuid() != 0:", b"if False:", 1)
    account = f"installer.pwd.getpwnam = lambda name: types.SimpleNamespace(pw_uid={os.getuid()})\n".encode()
    script = script.replace(b"request = json.loads", account + b"request = json.loads", 1)
    replacement = (
        f"operations.ServerInspector(root=operations.Path({str(inspector.root)!r}), "
        "command=lambda args: types.SimpleNamespace(returncode=1, stdout=b''), "
        "health=lambda origin, **options: {'public_url': origin, 'https_ready': True, 'initialized': False})"
    ).encode()
    script = script.replace(b"operations.ServerInspector()", replacement, 1)
    before = fingerprint(inspector.root)
    response = transport._run([sys.executable, "-I", "-S", "-B", "-"], script)
    report = json.loads(response.stdout)
    assert report["checks"]["release_payload"] == report["checks"]["installed_python_sources"] == "pass"
    assert report["checks"]["loaded_server_unit"] == "fail"
    assert report["checks"]["private_server_state"] == "pass"
    assert report["host_changed"] is False and fingerprint(inspector.root) == before


@pytest.mark.parametrize("change", ["oversized", "duplicate-json", "extra-field", "failed-process"])
def test_remote_transport_refuses_unbounded_ambiguous_or_secret_receipts(installed, tmp_path, change):
    identity, known = ssh_inputs(tmp_path)
    report = diagnose(installed)
    body = host.canonical(report)
    code = 0
    if change == "oversized":
        body = b"x" * (operations.MAX_REPORT + 1)
    elif change == "duplicate-json":
        body = body.replace(b'"format":1', b'"format":1,"format":1', 1)
    elif change == "extra-field":
        report["secret"] = "private-provider-key"
        body = host.canonical(report)
    else:
        code = 2
    with pytest.raises(host.InstallError) as failure:
        transport.diagnose_remote(
            installed[4],
            ORIGIN,
            "server.example.org",
            identity,
            known,
            confirmed_release=installed[2],
            command=lambda args, _script: subprocess.CompletedProcess(args, code, stdout=body),
        )
    assert "private-provider-key" not in str(failure.value)


def test_upgrade_plan_snapshots_both_inputs_before_ssh_and_never_uploads_target(installed, tmp_path):
    identity, known = ssh_inputs(tmp_path)
    path, _, release, checksum = target(installed, tmp_path)

    def command(args, _script):
        assert args[0] == "ssh"
        installed[4].write_bytes(b"source pathname replaced after verified snapshot")
        path.write_bytes(b"target pathname replaced after verified snapshot")
        return subprocess.CompletedProcess(args, 0, stdout=host.canonical(diagnose(installed)))

    result = transport.diagnose_remote(
        installed[4],
        ORIGIN,
        "server.example.org",
        identity,
        known,
        confirmed_release=installed[2],
        target_bundle=path,
        confirmed_target=release,
        command=command,
    )
    assert result["source"]["release_id"] == installed[2]
    assert result["target"]["release_id"] == release and result["target"]["bundle_sha256"] == checksum
    assert result["upgrade_performed"] is False


@pytest.mark.parametrize(
    "change", ["source-confirmation", "target-confirmation", "source-corruption", "target-corruption", "missing-trust", "invalid-origin"]
)
def test_invalid_local_inputs_refuse_all_ssh_access(installed, tmp_path, change):
    identity, known = ssh_inputs(tmp_path)
    path, _, next_release, _ = target(installed, tmp_path)
    arguments = {
        "confirmed_release": installed[2],
        "target_bundle": path,
        "confirmed_target": next_release,
        "command": lambda *_args: pytest.fail("invalid local inputs must not reach SSH"),
    }
    if change == "source-confirmation":
        arguments["confirmed_release"] = "0" * 64
    elif change == "target-confirmation":
        arguments["confirmed_target"] = "0" * 64
    elif change == "source-corruption":
        installed[4].write_bytes(b"corrupt")
    elif change == "target-corruption":
        path.write_bytes(b"corrupt")
    elif change == "missing-trust":
        known.unlink()
    origin = "http://example.org" if change == "invalid-origin" else ORIGIN
    with pytest.raises((host.InstallError, OSError)):
        transport.diagnose_remote(installed[4], origin, "server.example.org", identity, known, **arguments)


def test_transport_bounds_real_child_output_and_ignores_secret_stderr():
    result = transport._run([sys.executable, "-I", "-S", "-B", "-"], b"import sys\nsys.stderr.write('private-key')\nprint('{}')\n")
    assert result.stdout == b"{}\n"
    with pytest.raises(host.InstallError, match="allowance"):
        transport._run([sys.executable, "-I", "-S", "-B", "-"], b"print('x' * 100000)\n")
    with pytest.raises(host.InstallError, match="failed"):
        transport._run([sys.executable, "-I", "-S", "-B", "-"], b"raise SystemExit(2)\n")


def test_transport_timeout_kills_and_reaps_its_owned_child(monkeypatch):
    processes = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", launch)
    with pytest.raises(host.InstallError, match="timed out"):
        transport._run([sys.executable, "-I", "-S", "-B", "-"], b"import time\ntime.sleep(60)\n", timeout=0.2)
    assert len(processes) == 1 and processes[0].returncode is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


UNKNOWN_SOURCE = {"source_revision": None, "source_modified": None, "schema_version": len(MIGRATIONS)}


def health_without_uptime(response) -> dict:
    value = response.json()
    assert type(value.pop("uptime_seconds")) is int
    return value


@pytest.mark.parametrize("release", [None, "a" * 64])
def test_public_health_exposes_only_explicit_operator_release_identity(tmp_path, release):
    with TestClient(
        create_app(Config(tmp_path / "state", ORIGIN, inference_enabled=False, deployment_release=release)), base_url=ORIGIN
    ) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert health_without_uptime(response) == {"status": "ok", "initialized": False, "release_id": release, **UNKNOWN_SOURCE}
        assert response.headers.get("X-Reasoning-Release") == release
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("release", [None, "a" * 64])
def test_every_response_and_the_served_page_name_the_running_release(tmp_path, release):
    frontend = tmp_path / "web"
    (frontend / "assets").mkdir(parents=True)
    page = '<!doctype html><html><head><title>Reasoning</title></head><body></body></html>'
    (frontend / "organization.html").write_text(page)
    with TestClient(create_app(Config(tmp_path / "state", ORIGIN, frontend_dir=frontend, inference_enabled=False,
                                      deployment_release=release)), base_url=ORIGIN) as client:
        for response in (client.get("/api/auth/me"), client.get("/api/missing"), client.get("/assets/missing-AbCd1234.js"),
                         client.post("/api/auth/login", content="x", headers={"content-type": "text/plain"}), client.get("/")):
            assert response.headers.get_list("X-Reasoning-Release") == ([release] if release else []), response.request.url
        html = client.get("/").text
        if release is None:
            assert html == page
        else:
            assert html == page.replace("</head>", f'<meta name="reasoning-release" content="{release}" /></head>')
        assert client.get("/organization.html?view=ledger").text == html


@pytest.mark.parametrize("dirty", [False, True])
def test_public_health_reports_source_revision_only_from_the_verified_release_manifest(tmp_path, dirty):
    frontend = tmp_path / "release" / "web"
    frontend.mkdir(parents=True)
    (frontend / "organization.html").write_text("<!doctype html>")
    (frontend / "assets").mkdir()
    document = json.dumps({"source_revision": "b" * 40, "dirty": dirty, "files": {"private/path": "c" * 64}}).encode()
    (frontend.parent / "manifest.json").write_bytes(document)
    release = hashlib.sha256(document).hexdigest()
    with TestClient(create_app(Config(tmp_path / "state", ORIGIN, frontend_dir=frontend, inference_enabled=False,
                                      deployment_release=release)), base_url=ORIGIN) as client:
        response = client.get("/api/health")
        assert health_without_uptime(response) == {
            "status": "ok", "initialized": False, "release_id": release, "source_revision": "b" * 40,
            "source_modified": dirty, "schema_version": len(MIGRATIONS),
        }
        assert "private" not in response.text and str(tmp_path) not in response.text
        # The manifest is read once at startup; later changes to the file do not alter the reported identity.
        (frontend.parent / "manifest.json").write_bytes(b"{}")
        assert client.get("/api/health").json()["source_revision"] == "b" * 40


def test_public_health_does_not_trust_a_manifest_that_differs_from_the_running_release(tmp_path):
    frontend = tmp_path / "release" / "web"
    frontend.mkdir(parents=True)
    (frontend / "organization.html").write_text("<!doctype html>")
    (frontend / "assets").mkdir()
    (frontend.parent / "manifest.json").write_bytes(json.dumps({"source_revision": "b" * 40, "dirty": False}).encode())
    with TestClient(create_app(Config(tmp_path / "state", ORIGIN, frontend_dir=frontend, inference_enabled=False,
                                      deployment_release="d" * 64)), base_url=ORIGIN) as client:
        assert health_without_uptime(client.get("/api/health")) == {
            "status": "ok", "initialized": False, "release_id": "d" * 64, **UNKNOWN_SOURCE}


@pytest.mark.parametrize("value", [True, 123, "a" * 40, "A" * 64, "a" * 64 + "\n"])
def test_invalid_health_release_identity_is_refused_before_state_creation(tmp_path, value):
    with pytest.raises(ValueError, match="release"):
        Config(tmp_path / "state", ORIGIN, deployment_release=value)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("headers", [[], ["b" * 64], ["a" * 64, "a" * 64], ["a" * 64]])
def test_https_readiness_checks_exact_single_release_header(monkeypatch, headers):
    class Response:
        status = 200

        def __init__(self):
            self.headers = email.message.Message()
            for value in headers:
                self.headers["X-Reasoning-Release"] = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return b'{"status":"ok","initialized":false}'

    class Opener:
        def open(self, url, timeout):
            assert url == ORIGIN + "/api/health" and timeout == 3
            return Response()

    monkeypatch.setattr(host.urllib.request, "build_opener", lambda *_args: Opener())
    if headers == ["a" * 64]:
        assert host.ready(ORIGIN, timeout=0, expected_release="a" * 64)["https_ready"] is True
    else:
        with pytest.raises(host.InstallError, match="unverified"):
            host.ready(ORIGIN, timeout=0, expected_release="a" * 64)


def test_cli_documents_diagnosis_and_plan_without_claiming_upgrade_or_rollback(capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(["upgrade-plan", "--help"])
    assert failure.value.code == 0
    output = capsys.readouterr().out
    assert "--target-bundle" in output and "--confirm-target-release" in output
