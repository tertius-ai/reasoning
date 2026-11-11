"""Real encrypted SQLite maintenance with explicit package/systemd fixtures."""

import copy
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from test_public_deployment import bootstrap as bootstrap
from test_public_deployment import pack
from test_public_deployment import payload as payload
from test_public_server_operations import installed as installed

from reasoning_ledger.deployment import host, server_operations
from reasoning_ledger.deployment import server_upgrade as upgrade
from reasoning_ledger.server import backup as snapshots
from reasoning_ledger.server import cli, maintenance
from reasoning_ledger.server.mail import MailConfig
from reasoning_ledger.server.store import Store

OPERATION = "c" * 64
ORIGIN = "https://chat.example.org"


@pytest.fixture
def prepared_inputs(installed):
    inspector, source_manifest, source_release, source_checksum, original, _, _, payload = installed
    installer = host.HostInstaller(root=inspector.root)
    staging = installer._directory("var/lib/reasoning-deploy/staging", 0o700)
    source = staging / "source.tar.gz"
    shutil.copyfile(original, source)
    source.chmod(0o600)
    changed = copy.deepcopy(payload)
    changed["web/assets/shared-x.js"] += b"\n// Distinct target release fixture\n"
    target = pack(
        staging / "target.tar.gz",
        changed,
        manifest_change=lambda manifest: manifest.update(
            database_schema=source_manifest["database_schema"],
            installation_features=list(host.INSTALLATION_FEATURES),
            source_revision="d" * 40,
        ),
    )
    target.chmod(0o600)
    target_manifest, target_release, target_checksum = host.inspect_bundle(target)
    report = inspector.inspect(source_manifest, source_release, source_checksum, ORIGIN)
    plan = server_operations.upgrade_plan(report, source_manifest, target_manifest, target_release, target_checksum)
    assert plan["preflight_checks_passed"], plan
    observation = {"calls": [], "active": True, "loaded": {}, "interrupt": None, "helpers": [], "probes": []}

    def command(args, *, check=True):
        observation["calls"].append(args)
        if observation["interrupt"] is not None:
            observation["interrupt"](args)
        if args[:2] == ["systemctl", "stop"]:
            observation["active"] = False
        elif args[:2] == ["systemctl", "show"]:
            properties = args[3].removeprefix("--property=").split(",")
            account = "reasoning" if args[-1] == "reasoning-server" else "caddy"
            values = {
                "FragmentPath": f"/etc/systemd/system/{args[-1]}.service",
                "DropInPaths": "",
                "User": account,
                "Group": account,
                "LoadState": "loaded",
                "NeedDaemonReload": "no",
                "ActiveState": "active",
                "MainPID": "123",
                "SubState": "running",
            }
            if args[-1] == "reasoning-server":
                if (inspector.root / upgrade.DROPIN).exists():
                    values["DropInPaths"] = "/" + upgrade.DROPIN
                if not observation["active"]:
                    values.update(ActiveState="inactive", MainPID="0", SubState="dead")
                values.update(observation["loaded"])
            return subprocess.CompletedProcess(args, 0, stdout="".join(key + "=" + values[key] + "\n" for key in properties).encode())
        elif args[0].endswith("/bin/pip") and "--no-deps" in args:
            wheel = Path(args[-1])
            site = wheel.parent.parent / "venv/lib/python3.12/site-packages"
            with zipfile.ZipFile(wheel) as archive:
                for entry in archive.infolist():
                    path = site / entry.filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    for parent in path.parents:
                        if parent == inspector.root:
                            break
                        parent.chmod(0o755)
                    path.write_bytes(archive.read(entry))
                    path.chmod(0o644)
        elif args[:4] == ["runuser", "-u", "reasoning", "--"] and args[8:9] == [upgrade.BUSY_PROBE]:
            # The check for running work runs for real, read-only, on the live fixture database.
            assert args[5:8] == ["-I", "-B", "-c"] and args[4].endswith("/releases/" + source_release + "/venv/bin/python")
            observation["probes"].append(observation["active"])
            with redirect_stdout(io.StringIO()) as output, pytest.MonkeyPatch.context() as context:
                context.setattr(sys, "argv", ["-c", *args[9:]])
                exec(compile(args[8], "<busy-probe>", "exec"), {})
            return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())
        elif args[:4] == ["runuser", "-u", "reasoning", "--"]:
            python_runner = args[4].endswith("/bin/python")
            arguments = args[9:] if python_runner else args[5:]
            observation["helpers"].append(arguments[2])
            # The installed-wheel comparison fixture is not executable. Invoke
            # the real application CLI here; no backup/migration logic is mocked.
            with redirect_stdout(io.StringIO()) as output:
                if python_runner:
                    assert args[5:8] == ["-I", "-B", "-c"] and args[8] == upgrade.MIGRATION_RUNNER
                    with pytest.MonkeyPatch.context() as context:
                        context.setattr(sys, "argv", ["-c", *arguments])
                        with pytest.raises(SystemExit) as exited:
                            exec(compile(args[8], "<verified-migration-runner>", "exec"), {})
                        assert exited.value.code == 0
                else:
                    assert cli.main(arguments) == 0
            return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    engine = upgrade.HostUpgrade(root=inspector.root, command=command, health=inspector.health, droplet_id=inspector.droplet_id)
    return engine, source, target, source_checksum, target_checksum, plan, observation


def prepare(inputs, **changes):
    engine, source, target, source_checksum, target_checksum, plan, _ = inputs
    options = {"operation_id": OPERATION, "confirmed_plan": plan["plan_id"], "workers_fenced": True, "skip_platform": True}
    options.update(changes)
    return engine.prepare(source, target, source_checksum, target_checksum, ORIGIN, **options)


def operation(inputs):
    return inputs[0].directory / "server-upgrades" / OPERATION


def state(inputs):
    return inputs[0]._read(operation(inputs) / "operation.json")


def test_prepare_executes_real_backup_migration_and_seals_recovery_without_activation(prepared_inputs):
    engine, *_ = prepared_inputs
    result = prepare(prepared_inputs)
    assert result["phase"] == "prepared" and result["upgrade_completed"] is result["activation_performed"] is False
    assert result["server_restart_fenced"] and result["root_private_backup_retained"]
    assert prepared_inputs[-1]["active"] is False
    # Running work was checked before any journal and again just before the fence, both while the server was serving.
    assert prepared_inputs[-1]["probes"] == [True, True]
    original = state(prepared_inputs)
    assert original["phase"] == "prepared" and original["migration"]["phase"] == "migrated"
    assert (operation(prepared_inputs) / "backup.key").stat().st_mode & 0o777 == 0o600
    assert (operation(prepared_inputs) / "source.rnbk").stat().st_mode & 0o777 == 0o600
    scratch = engine.root / "var/lib/reasoning-upgrade-scratch" / OPERATION
    assert scratch.stat().st_uid == os.getuid()
    assert not (engine.root / "run" / ("reasoning-upgrade-" + OPERATION)).exists()
    assert (engine.root / upgrade.DROPIN).read_text() == upgrade.FENCE_UNIT
    assert prepare(prepared_inputs) == result and state(prepared_inputs) == original
    assert prepared_inputs[-1]["helpers"].count("backup") == 1
    assert prepared_inputs[-1]["helpers"].count("migrate-offline") == 1
    with pytest.raises(host.InstallError, match="managed upgrade owns"):
        host.HostInstaller(root=engine.root).install(prepared_inputs[1], prepared_inputs[3], ORIGIN, skip_platform=True)
    with pytest.raises(maintenance.MaintenanceError, match="unfinished"):
        Store(engine.root / "var/lib/reasoning")
    assert all(
        args[:2] not in (["systemctl", "start"], ["systemctl", "restart"], ["systemctl", "enable"]) for args in prepared_inputs[-1]["calls"]
    )


@pytest.mark.parametrize(
    "change", [{"workers_fenced": False}, {"operation_id": "invalid"}, {"confirmed_plan": "0" * 64}, {"maximum": True}, {"maximum": 1}]
)
def test_invalid_confirmation_never_stops_or_fences_the_source(prepared_inputs, change):
    with pytest.raises(host.InstallError):
        prepare(prepared_inputs, **change)
    assert prepared_inputs[-1]["active"] is True
    assert not (prepared_inputs[0].root / upgrade.FENCE).exists()
    assert not prepared_inputs[0].active.exists()


@pytest.mark.parametrize("boundary", ["target_ready", "fencing", "source_stopped", "backup_retained", "migrating", "sealing", "prepared"])
def test_original_operation_recovers_each_durable_boundary_without_resnapshot_or_activation(prepared_inputs, monkeypatch, boundary):
    original = prepared_inputs[0]._write
    raised = False

    def interrupt(path, value):
        nonlocal raised
        original(path, value)
        if path.name == "operation.json" and value["phase"] == boundary and not raised:
            raised = True
            raise RuntimeError("interruption fixture")

    monkeypatch.setattr(prepared_inputs[0], "_write", interrupt)
    with pytest.raises(RuntimeError, match="interruption fixture"):
        prepare(prepared_inputs)
    monkeypatch.setattr(prepared_inputs[0], "_write", original)
    assert prepare(prepared_inputs)["phase"] == "prepared"
    assert prepared_inputs[-1]["helpers"].count("backup") == 1
    assert prepared_inputs[-1]["active"] is False


def test_resume_failed_ram_scratch_backup_on_disk_preserves_escrow_and_seals_both_paths(prepared_inputs, monkeypatch):
    engine, *_, observation = prepared_inputs
    legacy = engine.installer._directory("run") / ("reasoning-upgrade-" + OPERATION)
    legacy.mkdir(mode=0o700)
    original_command = engine.command

    def no_space(args, **options):
        if "backup" in args:
            raise OSError(28, "No space left on device")
        return original_command(args, **options)

    with monkeypatch.context() as context:
        context.setattr(engine, "_scratch", lambda state, uid: legacy)
        context.setattr(engine, "command", no_space)
        with pytest.raises(host.InstallError, match="release change failed"):
            prepare(prepared_inputs)

    original = state(prepared_inputs)
    assert original["phase"] == "source_stopped" and original["backup"] is None
    escrow = (operation(prepared_inputs) / "backup.key").read_bytes()
    assert (legacy / "backup.key").read_bytes() == escrow
    assert prepare(prepared_inputs)["phase"] == "prepared"
    resumed = state(prepared_inputs)
    assert resumed["operation_id"] == original["operation_id"]
    assert resumed["plan_id"] == original["plan_id"]
    assert resumed["backup_key_sha256"] == original["backup_key_sha256"]
    assert (operation(prepared_inputs) / "backup.key").read_bytes() == escrow
    assert (legacy / "backup.key").read_bytes() == escrow
    disk = engine.root / "var/lib/reasoning-upgrade-scratch" / OPERATION
    assert (disk / "source.rnbk").is_file()
    for path in (disk, legacy):
        assert path.stat().st_uid == os.getuid()
        assert path.stat().st_mode & 0o777 == 0o700
    assert observation["helpers"].count("backup") == 1


def test_prepared_operation_with_sealed_ram_scratch_remains_resumable(prepared_inputs):
    engine, *_ = prepared_inputs
    receipt = prepare(prepared_inputs)
    original = state(prepared_inputs)
    disk = engine.root / "var/lib/reasoning-upgrade-scratch" / OPERATION
    legacy = engine.installer._directory("run") / ("reasoning-upgrade-" + OPERATION)
    disk.rename(legacy)
    assert prepare(prepared_inputs) == receipt
    assert state(prepared_inputs) == original
    assert not disk.exists()


@pytest.mark.parametrize("command", ["stop", "backup", "migrate-offline"])
def test_lost_command_response_reuses_original_operation_and_recognizes_completed_sql(prepared_inputs, monkeypatch, command):
    engine, *_, observation = prepared_inputs
    original = engine.command
    raised = False

    def lost(args, **options):
        nonlocal raised
        result = original(args, **options)
        if command in args and not raised:
            raised = True
            raise RuntimeError("lost successful response fixture")
        return result

    monkeypatch.setattr(engine, "command", lost)
    engine.inspector.command = lost
    engine.installer.command = lost
    with pytest.raises(RuntimeError, match="lost successful"):
        prepare(prepared_inputs)
    monkeypatch.setattr(engine, "command", original)
    engine.inspector.command = original
    engine.installer.command = original
    assert prepare(prepared_inputs)["phase"] == "prepared"
    assert observation["helpers"].count("backup") == 1
    assert observation["helpers"].count("migrate-offline") == (2 if command == "migrate-offline" else 1)


@pytest.mark.parametrize("change", ["backup", "key", "fence", "dropin", "catalog", "active-pointer", "unit"])
def test_prepared_replay_refuses_drift_and_never_overwrites_original_recovery_evidence(prepared_inputs, change):
    prepare(prepared_inputs)
    engine = prepared_inputs[0]
    paths = {
        "backup": operation(prepared_inputs) / "source.rnbk",
        "key": operation(prepared_inputs) / "backup.key",
        "fence": engine.root / upgrade.FENCE,
        "dropin": engine.root / upgrade.DROPIN,
        "catalog": engine.root / "etc/reasoning/compute-images.json",
        "active-pointer": engine.active,
        "unit": engine.root / "etc/systemd/system/reasoning-server.service",
    }
    changed = paths[change]
    changed.write_bytes(b"deliberately changed recovery fixture")
    with pytest.raises(host.InstallError):
        prepare(prepared_inputs)
    assert changed.read_bytes() == b"deliberately changed recovery fixture"
    assert prepared_inputs[-1]["active"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("MainPID", "123"),
        ("ActiveState", "active"),
        ("SubState", "running"),
        ("DropInPaths", "/etc/systemd/system/other.conf"),
        ("NeedDaemonReload", "yes"),
    ],
)
def test_stop_response_is_not_proof_of_exact_loaded_fence_or_process_termination(prepared_inputs, field, value):
    def changed_after_stop(args):
        if args[:2] == ["systemctl", "stop"]:
            prepared_inputs[-1]["loaded"][field] = value

    prepared_inputs[-1]["interrupt"] = changed_after_stop
    with pytest.raises(host.InstallError, match="verified stopped"):
        prepare(prepared_inputs)
    assert state(prepared_inputs)["phase"] == "fencing"
    assert "backup" not in prepared_inputs[-1]["helpers"]


def test_helper_refusal_retains_backup_and_source_fence(prepared_inputs):
    # The maintenance guard is exercised independently in the offline suite;
    # here use a real helper refusal to verify the root transaction keeps all
    # recovery material and never automatically restarts the source.
    original = prepared_inputs[0].command

    def refuse(args, **kwargs):
        if "migrate-offline" in args:
            raise host.InstallError("settled execution required")
        return original(args, **kwargs)

    prepared_inputs[0].command = refuse
    with pytest.raises(host.InstallError, match="settled execution"):
        prepare(prepared_inputs)
    assert state(prepared_inputs)["phase"] == "migrating"
    assert (operation(prepared_inputs) / "source.rnbk").exists()
    assert (operation(prepared_inputs) / "backup.key").exists()
    assert prepared_inputs[-1]["active"] is False


def test_prepare_recovery_uses_retained_releases_after_operator_input_paths_disappear(prepared_inputs):
    prepare(prepared_inputs)
    prepared_inputs[1].unlink()
    prepared_inputs[2].unlink()
    assert prepare(prepared_inputs)["phase"] == "prepared"


def test_bounded_child_failure_never_exposes_stderr_or_leaves_its_process_alive():
    args = [sys.executable, "-I", "-S", "-c", "import sys; sys.stderr.write('secret-not-a-receipt'); sys.exit(7)"]
    with pytest.raises(host.InstallError) as error:
        upgrade.bounded_command(args)
    assert "secret-not-a-receipt" not in str(error.value)
    assert upgrade.migration_failure_reason(error.value) == "helper_failed"
    assert upgrade.bounded_command(args, check=False).returncode == 7


@pytest.mark.parametrize("code,reason", [(-9, "helper_killed"), (137, "helper_killed"), (2, "helper_failed"), (None, "helper_timeout")])
def test_only_bounded_helper_outcomes_are_migration_diagnoses(code, reason):
    assert upgrade.migration_failure_reason(upgrade.CommandFailure(code)) == reason
    assert upgrade.migration_failure_reason(host.InstallError("SECRET child stderr")) is None


def test_owned_bootstrap_diagnosis_does_not_release_mutating_caller_lease(prepared_inputs):
    engine, source, *_ = prepared_inputs
    manifest, release, checksum = host.inspect_bundle(source)
    lock = engine.directory / "bootstrap.lock"
    with lock.open("rb") as owned, lock.open("rb") as other:
        fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = engine.inspector.inspect(manifest, release, checksum, ORIGIN, owned_bootstrap_fd=owned.fileno())
        assert report["all_checks_passed"]
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_standalone_preparer_loads_only_selected_neighbor_sources_under_isolated_python(prepared_inputs):
    directory = prepared_inputs[0].directory / "standalone-fixture"
    directory.mkdir(mode=0o700)
    for name, module in (("installer.py", host), ("server-operations.py", server_operations), ("server-upgrade.py", upgrade)):
        shutil.copyfile(module.__file__, directory / name)
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(directory / "server-upgrade.py"), "--help"], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr.decode()
    assert b"--confirm-workers-fenced" in result.stdout and b"--confirm-activation" in result.stdout
    assert b"--rollback-activate" in result.stdout
    assert b"--rollback-prepare" in result.stdout


def refresh_plan(inputs):
    engine, source, target, source_checksum, target_checksum, _, observation = inputs
    source_manifest, source_release, _ = host.inspect_bundle(source)
    target_manifest, target_release, _ = host.inspect_bundle(target)
    report = engine.inspector.inspect(source_manifest, source_release, source_checksum, ORIGIN)
    plan = server_operations.upgrade_plan(report, source_manifest, target_manifest, target_release, target_checksum)
    assert plan["preflight_checks_passed"], plan
    return engine, source, target, source_checksum, target_checksum, plan, observation


def test_managed_mail_and_original_vm_identity_are_retained_and_revalidated(prepared_inputs):
    engine = prepared_inputs[0]
    source = prepared_inputs[5]["source"]
    body = host.canonical(MailConfig("invites@example.org", "private-mail-fixture-not-receipt", 100).document())
    host.atomic_write(engine.root / "var/lib/reasoning/mail.json", body)
    host.atomic_write(engine.directory / "server-mail.json", host.canonical({"installation": source, "mail": host.mail_binding(body)}))
    request = {"format": 1, "plan_id": "a" * 64, "deployment_id": "b" * 32, "droplet_id": 456}
    host.atomic_write(engine.directory / "server-provisioning.json", host.canonical({"format": 1, "binding": source, "request": request}))
    host.atomic_write(engine.root / "etc/reasoning-provisioning.json", host.canonical({"plan_id": "a" * 64, "deployment_id": "b" * 32}))
    inputs = refresh_plan(prepared_inputs)
    result = prepare(inputs)
    assert (operation(inputs) / "mail.json").read_bytes() == body
    assert (operation(inputs) / "source-server-mail.json").read_bytes() == (engine.directory / "server-mail.json").read_bytes()
    assert (operation(inputs) / "source-server-provisioning.json").read_bytes() == (
        engine.directory / "server-provisioning.json"
    ).read_bytes()
    assert "private-mail-fixture-not-receipt" not in json.dumps(result)
    engine.inspector.droplet_id = lambda: 999
    with pytest.raises(host.InstallError, match="VM identity"):
        prepare(inputs)
    assert (operation(inputs) / "mail.json").read_bytes() == body


def test_root_preparation_holds_the_original_bootstrap_lease_before_target_or_service_changes(prepared_inputs):
    lock = prepared_inputs[0].directory / "bootstrap.lock"
    with lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(host.InstallError):
            prepare(prepared_inputs)
    assert not prepared_inputs[0].active.exists()
    assert prepared_inputs[-1]["active"] is True


def test_root_preparation_runs_real_sql_for_a_synthetic_next_release_schema(prepared_inputs, monkeypatch):
    engine, source, target, source_checksum, *_ = prepared_inputs
    original_schema = maintenance.MIGRATIONS
    future_schema = (
        *original_schema,
        (
            "CREATE TABLE root_upgrade_probe(id INTEGER PRIMARY KEY, value TEXT)",
            "INSERT INTO root_upgrade_probe VALUES(1,'verified root preparation')",
        ),
    )
    payload = {}
    host.extract_bundle(target, engine.installer._directory("var/lib/reasoning-deploy/target-fixture", 0o700))
    directory = engine.directory / "target-fixture"
    for path in directory.rglob("*"):
        if path.is_file() and path.name != "manifest.json":
            payload[path.relative_to(directory).as_posix()] = path.read_bytes()
    next_target = pack(
        target.parent / "next-schema.tar.gz",
        payload,
        manifest_change=lambda value: value.update(
            database_schema=len(future_schema), installation_features=list(host.INSTALLATION_FEATURES), source_revision="e" * 40
        ),
    )
    next_target.chmod(0o600)
    _, _, checksum = host.inspect_bundle(next_target)
    inputs = refresh_plan((engine, source, next_target, source_checksum, checksum, {}, prepared_inputs[-1]))
    original_command = engine.command

    def command(args, **options):
        if "migrate-offline" in args or "maintenance-verify" in args:
            with monkeypatch.context() as patch:
                patch.setattr(maintenance, "MIGRATIONS", future_schema)
                patch.setattr(snapshots, "MIGRATIONS", future_schema)
                return original_command(args, **options)
        return original_command(args, **options)

    engine.command = command
    assert prepare(inputs)["phase"] == "prepared"
    with snapshots._connect(engine.root / "var/lib/reasoning/server.sqlite3") as database:
        assert database.execute("PRAGMA user_version").fetchone()[0] == len(future_schema)
        assert database.execute("SELECT value FROM root_upgrade_probe").fetchone()[0] == "verified root preparation"
    assert state(inputs)["migration"]["target_schema"] == len(future_schema)
    assert prepared_inputs[-1]["active"] is False


@pytest.mark.parametrize("change", ["activation", "operation", "secret-field"])
def test_false_or_secret_migration_receipt_never_completes_host_preparation(prepared_inputs, change):
    engine = prepared_inputs[0]
    original = engine.command

    def changed(args, **options):
        result = original(args, **options)
        if "migrate-offline" in args:
            value = json.loads(result.stdout)
            if change == "activation":
                value["activation_performed"] = True
            elif change == "operation":
                value["operation_id"] = "e" * 64
            else:
                value["unexpected"] = "secret-never-returned"
            result.stdout = json.dumps(value).encode()
        return result

    engine.command = changed
    with pytest.raises(host.InstallError, match="differently bound") as failure:
        prepare(prepared_inputs)
    assert "secret-never-returned" not in str(failure.value)
    assert state(prepared_inputs)["phase"] == "migrating"
    assert prepared_inputs[-1]["active"] is False
    engine.command = original
    assert prepare(prepared_inputs)["phase"] == "prepared"


@pytest.mark.parametrize("mode", ["timeout", "output"])
def test_bounded_command_kills_and_reaps_only_its_owned_process_group(monkeypatch, mode):
    calls = []
    original = os.killpg

    def kill(group, signal):
        calls.append((group, signal))
        return original(group, signal)

    monkeypatch.setattr(os, "killpg", kill)
    program = "import time; time.sleep(60)" if mode == "timeout" else "import sys; sys.stdout.buffer.write(b'x'*3000000)"
    with pytest.raises(host.InstallError):
        upgrade.bounded_command([sys.executable, "-I", "-S", "-c", program], timeout=0.1 if mode == "timeout" else 10)
    assert len(calls) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(calls[0][0], os.WNOHANG)


@pytest.mark.parametrize("changed", ["pointer", "fence"])
def test_boolean_format_is_not_an_exact_original_ownership_identity(prepared_inputs, changed):
    prepare(prepared_inputs)
    engine = prepared_inputs[0]
    path = engine.active if changed == "pointer" else engine.root / upgrade.FENCE
    value = engine._read(path)
    value["format"] = True
    body = host.canonical(value)
    host.atomic_write(path, body)
    with pytest.raises(host.InstallError):
        prepare(prepared_inputs)
    assert path.read_bytes() == body


def test_deeply_nested_ownership_input_is_refused_without_source_or_service_mutation(prepared_inputs):
    engine = prepared_inputs[0]
    body = b'{"nested":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}\n"
    host.atomic_write(engine.active, body)
    with pytest.raises(host.InstallError):
        prepare(prepared_inputs)
    assert engine.active.read_bytes() == body
    assert not (engine.root / upgrade.FENCE).exists()
    assert prepared_inputs[-1]["active"] is True
