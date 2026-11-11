"""Verified transport with real maintenance/SQL; systemd, packages and TLS fixtures."""

import copy
import hashlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path

import pytest
from test_public_host_rollback import ROLLBACK
from test_public_host_rollback_activation import activation_inputs as activation_inputs
from test_public_host_rollback_activation import bootstrap as bootstrap
from test_public_host_rollback_activation import installed as installed
from test_public_host_rollback_activation import payload as payload
from test_public_host_rollback_activation import prepared_inputs as prepared_inputs
from test_public_host_rollback_activation import rollback_inputs as rollback_inputs
from test_public_host_upgrade import OPERATION
from test_public_server_operations import ssh_inputs

from reasoning_ledger.deployment import cli, host, private, server_changes, server_diagnostics
from reasoning_ledger.deployment.private import read_document, write_document


def test_ssh_snapshot_uses_normalized_protected_temporary_directory(tmp_path, monkeypatch):
    key, known = ssh_inputs(tmp_path)
    calls = []

    @contextmanager
    def protected(prefix):
        calls.append(prefix)
        trust = tmp_path / "normalized-trust"
        trust.mkdir(mode=0o700)
        yield trust

    monkeypatch.setattr(private, "protected_temporary_directory", protected)
    with server_changes.snapshot_ssh("server.example.org", key, known) as (arguments, transport):
        assert calls == ["reasoning-change-trust-"]
        assert str((tmp_path / "normalized-trust" / "identity").absolute()) in arguments
        assert transport["hostname"] == "server.example.org"


@pytest.fixture
def operator(rollback_inputs, tmp_path, monkeypatch):
    engine, source, target, _, _, plan, observation = rollback_inputs
    key, known = ssh_inputs(tmp_path)
    for name in ("prepare", "activate", "rollback_prepare", "rollback_activate"):
        original = getattr(engine, name)

        def wrapper(*args, _method=original, **kwargs):
            return _method(*args, **kwargs, skip_platform=True)

        monkeypatch.setattr(engine, name, wrapper)
    requests, packets = [], []

    def command(args, chunks):
        assert args[:2] == ["ssh", "-T"]
        assert shlex.split(args[-1]) == ["python3", "-I", "-S", "-B", "-c", server_changes.LAUNCHER]
        assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
        assert "-F" in args and args[args.index("-F") + 1] == "/dev/null"
        packet = b"".join(chunks)
        packets.append(packet)
        stream = io.BytesIO(packet)
        script = stream.read(int(stream.readline()))
        compile(script, "<transport-fixture>", "exec")

        def endpoint(request, _stdin):
            requests.append(request)
            receipt = server_changes.execute_remote(request, stream, engine=engine)
            # Disposable fixture root is not the production filesystem layout.
            receipt["recovery_directory"] = receipt["recovery_directory"].removeprefix(str(engine.root))
            return receipt

        script = script.replace(b"if os.geteuid() != 0:", b"if False:", 1)
        script = script.replace(b"package.server_changes.execute_remote(request, sys.stdin.buffer)", b"endpoint(request, None)", 1)
        # Failure inspection uses the same disposable host root as execution.
        script = script.replace(b"package.server_changes.failure_report(request, error=exc)", b"report_failure(request, exc)", 1)

        def report_failure(request, error):
            return server_changes.failure_report(request, engine=engine, error=error)

        with redirect_stdout(io.StringIO()) as output:
            exec(compile(script, "<transport-fixture>", "exec"), {"endpoint": endpoint, "report_failure": report_failure})
        return subprocess.CompletedProcess(args, 0, stdout=output.getvalue().encode())

    def change(mode, **changes):
        options = {
            "operation_id": OPERATION,
            "confirmed_plan": plan["plan_id"],
            "command": command,
            "workers_fenced": mode.endswith("prepare"),
            "activation_confirmed": mode.endswith("activate"),
            "data_loss_confirmed": mode == "rollback-prepare",
        }
        if mode == "upgrade-prepare":
            options.update(source_bundle=source, target_bundle=target)
        if mode.startswith("rollback"):
            options["rollback_id"] = ROLLBACK
        options.update(changes)
        return server_changes.change_remote(mode, plan, "server.example.org", key, known, tmp_path / "operator", **options)

    change.remote_command = command
    return change, engine, observation, plan, requests, packets, key, known


def test_all_four_commands_execute_original_backends_and_retain_operator_receipts(operator):
    change, engine, observation, plan, requests, packets, *_ = operator
    for mode in ("upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"):
        receipt = change(mode)
        assert receipt["operation"] == mode and receipt["plan_id"] == plan["plan_id"]
        assert receipt["phase"] == ("prepared" if mode.endswith("prepare") else "completed")
        assert change(mode) == receipt
    assert len(requests) == len(packets) == 8
    assert observation["helpers"].count("backup") == 1
    assert observation["helpers"].count("migrate-offline") == 1
    assert observation["helpers"].count("rollback-restore") == 1
    assert observation["starts"] == observation["source_starts"] == 1
    assert not engine.active.exists()
    assert requests[0]["upload_sizes"] and all(not request["upload_sizes"] for request in requests[2:])


@pytest.mark.parametrize("lost_mode", ["upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"])
def test_lost_remote_reply_reuses_original_operation_without_snapshot_or_activation_replay(operator, lost_mode):
    change, _, observation, _, requests, *_ = operator
    for mode in ("upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"):
        if mode == lost_mode:
            # Keep the real callback: it executes before the synthetic lost reply.
            callback = change.remote_command

            def lost(args, chunks, _callback=callback):
                _callback(args, chunks)
                return subprocess.CompletedProcess(args, 255, stdout=b"private incomplete response")

            with pytest.raises(host.InstallError, match="changes may have occurred"):
                change(mode, command=lost)
        change(mode)
    assert observation["helpers"].count("backup") == observation["helpers"].count("migrate-offline") == 1
    assert observation["helpers"].count("rollback-restore") == 1
    assert observation["starts"] == observation["source_starts"] == 1
    assert {request["operation_id"] for request in requests} == {OPERATION}
    assert {request["rollback_id"] for request in requests if request["rollback_id"]} == {ROLLBACK}


@pytest.mark.parametrize(
    "changes",
    [
        {"workers_fenced": False},
        {"workers_fenced": 1},
        {"activation_confirmed": True},
        {"operation_id": "bad"},
        {"confirmed_plan": "0" * 64},
        {"maximum": True},
        {"maximum": 1},
        {"source_bundle": None},
        {"target_bundle": None},
        {"rollback_id": ROLLBACK},
        {"data_loss_confirmed": True},
    ],
)
def test_bad_preparation_inputs_request_no_remote_effects(operator, changes):
    change, _, observation, _, requests, *_ = operator
    with pytest.raises(host.InstallError):
        change("upgrade-prepare", **changes)
    assert not requests and observation["active"] is True


@pytest.mark.parametrize("mutation", ["extra", "identity", "refusal", "preflight", "schema", "dirty", "configuration", "checks", "format"])
def test_malformed_saved_plan_is_not_mutation_authority(operator, mutation):
    _, _, _, plan, requests, *_ = operator
    modified = copy.deepcopy(plan)
    if mutation == "extra":
        modified["secret"] = "never returned"
    elif mutation == "identity":
        modified["source"]["release_id"] = "a" * 64
    elif mutation == "refusal":
        modified["refusals"] = ["uncommitted_release"]
    elif mutation == "preflight":
        modified["preflight_checks_passed"] = 1
    elif mutation == "schema":
        modified["source_database_schema"] = True
    elif mutation == "dirty":
        modified["target"]["dirty"] = True
    elif mutation == "configuration":
        modified["configuration"]["compute_catalog_sha256"] = "b" * 64
    elif mutation == "checks":
        modified["diagnosis"]["checks"]["bootstrap_lock"] = "fail"
    else:
        modified["format"] = True
    with pytest.raises(host.InstallError):
        server_changes.validate_plan(modified, modified["plan_id"])
    assert not requests


@pytest.mark.parametrize("mutation", ["host", "key", "known", "plan", "allowance", "operation"])
def test_operator_binding_drift_refuses_before_remote_retry(operator, tmp_path, mutation):
    change, _, _, plan, requests, _, key, known = operator
    change("upgrade-prepare")
    before = len(requests)
    changes = {}
    if mutation == "key":
        key.write_text("different-private-identity")
    elif mutation == "known":
        known.write_text("different-verified-host-key")
    elif mutation == "allowance":
        changes["maximum"] = 1024**2
    elif mutation == "operation":
        changes["operation_id"] = "a" * 64
    else:
        path = tmp_path / "operator/operation.json"
        value = read_document(path)
        if mutation == "host":
            value["transport"]["hostname"] = "different.example.org"
        else:
            value["identity"]["configuration"]["compute_catalog_sha256"] = "a" * 64
        write_document(path, value)
    with pytest.raises(host.InstallError):
        change("upgrade-prepare", **changes)
    assert len(requests) == before and plan["plan_id"]


def test_input_path_changes_after_network_start_do_not_change_trust_or_archive_snapshot(operator):
    change, _, _, _, _, _, key, known = operator
    callback = change.remote_command
    original_key, original_known = key.read_bytes(), known.read_bytes()

    def replacing(args, chunks):
        key.write_bytes(b"replaced-private-input")
        known.write_bytes(b"replaced-trust-input")
        assert Path(args[args.index("-i") + 1]).read_bytes() == original_key
        known_option = next(item for item in args if item.startswith("UserKnownHostsFile="))
        assert Path(known_option.split('"')[1]).read_bytes() == original_known
        return callback(args, chunks)

    assert change("upgrade-prepare", command=replacing)["phase"] == "prepared"


@pytest.mark.parametrize(
    "mutation", ["extra", "format", "operation", "phase", "source", "flags", "path", "duplicate", "oversized", "nonbytes"]
)
def test_invalid_receipt_never_becomes_operator_success(operator, mutation):
    change, *_ = operator
    callback = change.remote_command

    def invalid(args, chunks):
        response = callback(args, chunks)
        value = json.loads(response.stdout)
        if mutation == "extra":
            value["private_key"] = "do not return"
        elif mutation == "format":
            value["format"] = True
        elif mutation == "operation":
            value["operation_id"] = "0" * 64
        elif mutation == "phase":
            value["phase"] = "completed"
        elif mutation == "source":
            value["source"]["release_id"] = "0" * 64
        elif mutation == "flags":
            value["activation_performed"] = 0
        elif mutation == "path":
            value["recovery_directory"] = "/etc/private-key"
        body = json.dumps(value).encode()
        if mutation == "duplicate":
            body = b'{"format":1,' + body[1:]
        elif mutation == "oversized":
            body = b"x" * (server_changes.MAX_RECEIPT + 1)
        elif mutation == "nonbytes":
            body = body.decode()
        return subprocess.CompletedProcess(args, 0, stdout=body)

    with pytest.raises(host.InstallError, match="changes may have occurred"):
        change("upgrade-prepare", command=invalid)
    assert change("upgrade-prepare")["phase"] == "prepared"


def test_isolated_remote_modules_load_without_installed_application_or_uploaded_python(operator):
    _, _, _, plan, *_ = operator
    request = {"mode": "upgrade-activate", "identity": {name: plan[name] for name in server_changes.server_upgrade.IDENTITY_FIELDS}}
    script = server_changes.remote_script(request).replace(b"if os.geteuid() != 0:", b"if False:", 1)
    script = script.replace(
        b"receipt = package.server_changes.execute_remote(request, sys.stdin.buffer)",
        b"receipt = {'modules': sorted(name for name in sys.modules if name.startswith('reasoning_change.')), "
        b"'remaining': sys.stdin.buffer.read().decode()}",
        1,
    )
    chunks = (
        [(str(len(script)) + "\n").encode()]
        + [script[offset : offset + 65536] for offset in range(0, len(script), 65536)]
        + [b"input-boundary"]
    )
    result = server_changes._run([sys.executable, "-I", "-S", "-B", "-c", server_changes.LAUNCHER], chunks, timeout=5)
    value = json.loads(result.stdout)
    assert value["remaining"] == "input-boundary"
    assert value["modules"] == [
        "reasoning_change.host",
        "reasoning_change.server_changes",
        "reasoning_change.server_operations",
        "reasoning_change.server_upgrade",
    ]


def test_transport_streams_input_bounds_output_and_suppresses_secret_stderr():
    script = "import sys,hashlib; sys.stderr.write('private-key'); print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"
    args = [sys.executable, "-I", "-S", "-B", "-c", script]
    blocks = [b"x" * 65536 for _ in range(20)]
    result = server_changes._run(args, iter(blocks), timeout=5)
    assert result.stdout.strip().decode() == hashlib.sha256(b"".join(blocks)).hexdigest()
    for code in ("print('x' * 40000)", "raise SystemExit(2)"):
        with pytest.raises(host.InstallError, match="changes may have occurred"):
            server_changes._run([sys.executable, "-I", "-S", "-B", "-c", code], [b"x"], timeout=5)


def test_failure_report_reads_only_the_original_bound_phase_and_hides_exception_text(operator):
    change, engine, _, plan, *_ = operator
    original = engine.command

    def refuse(args, **options):
        if "migrate-offline" in args:
            raise host.InstallError("SECRET PROVIDER KEY AND DATABASE CONTENT")
        return original(args, **options)

    engine.command = refuse
    with pytest.raises(host.InstallError, match="Reported operation phase") as error:
        change("upgrade-prepare")
    assert "SECRET" not in str(error.value)
    request = {"mode": "upgrade-prepare", "operation_id": OPERATION, "plan_id": plan["plan_id"],
               "identity": {key: plan[key] for key in server_changes.server_upgrade.IDENTITY_FIELDS}}
    report = server_changes.failure_report(request, engine=engine)
    assert report["phase"] == "migrating"
    with pytest.raises(host.InstallError, match="Reported operation phase: migrating"):
        server_changes.reject_failure(report, request)
    wrong = request | {"plan_id": "0" * 64}
    assert server_changes.failure_report(wrong, engine=engine)["phase"] == "unknown"
    for mutation in ({"format": True}, {"phase": "SECRET"}, {"operation_id": "0" * 64}, {"extra": "SECRET"}):
        with pytest.raises(ValueError):
            server_changes.reject_failure(report | mutation, request)
    assert not any(row[0:2] == ["systemctl", "start"] for row in operator[2]["calls"])


def occupy(engine, *, occupied=True):
    """An enrolled compute worker that is not draining: migration would refuse it once the server is stopped."""
    with closing(sqlite3.connect(engine.root / "var/lib/reasoning/server.sqlite3")) as db, db:
        if occupied:
            db.execute("INSERT INTO compute_workers(id,organization_id,name,token_hash,image_ids_json,resources_json,created_at) "
                       "VALUES(?,?,?,?,?,?,?)", ("e" * 32, "f" * 32, "worker", "a" * 64, "[]", "{}", 1))
        else:
            db.execute("DELETE FROM compute_workers WHERE id=?", ("e" * 32,))


def test_busy_server_is_refused_before_any_journal_and_keeps_serving_until_its_work_is_settled(operator):
    change, engine, observation, *_ = operator
    occupy(engine)
    with pytest.raises(host.InstallError, match="Upgrade refused before stopping the server: a compute worker") as error:
        change("upgrade-prepare")
    assert "keeps serving. No upgrade is pending" in str(error.value) and "make upgrade again" in str(error.value)
    assert observation["active"] is True and observation["probes"] == [True]
    assert not (engine.root / server_changes.server_upgrade.FENCE).exists() and not engine.active.exists()
    assert not (engine.directory / "server-upgrades" / OPERATION).exists()
    assert not any(args[:2] == ["systemctl", "stop"] for args in observation["calls"])
    occupy(engine, occupied=False)
    assert change("upgrade-prepare")["phase"] == "prepared"
    assert observation["probes"] == [True, True, True] and observation["active"] is False


def test_work_that_starts_while_the_target_installs_is_refused_before_the_fence_and_resumes(operator):
    change, engine, observation, *_ = operator

    def start_work(args):
        if args[8:9] == [server_changes.server_upgrade.BUSY_PROBE] and len(observation["probes"]) == 1:
            occupy(engine)

    observation["interrupt"] = start_work
    with pytest.raises(host.InstallError, match="this upgrade is pending but has not fenced or stopped anything") as error:
        change("upgrade-prepare")
    assert "make upgrade-resume" in str(error.value)
    observation["interrupt"] = None
    assert engine._read(engine.directory / "server-upgrades" / OPERATION / "operation.json")["phase"] == "target_ready"
    assert observation["active"] is True and not (engine.root / server_changes.server_upgrade.FENCE).exists()
    occupy(engine, occupied=False)
    assert change("upgrade-prepare")["phase"] == "prepared"


def test_an_unreadable_database_refuses_as_unverified_without_stopping(operator):
    change, engine, observation, *_ = operator
    original = engine.command

    def broken(args, **options):
        if args[8:9] == [server_changes.server_upgrade.BUSY_PROBE]:
            return subprocess.CompletedProcess(args, 1, stdout=b"SECRET database error")
        return original(args, **options)

    engine.command = broken
    with pytest.raises(host.InstallError, match="could not read the server database") as error:
        change("upgrade-prepare")
    assert "SECRET" not in str(error.value) and observation["active"] is True


@pytest.mark.parametrize("mutation", [
    {"reason": "SECRET"}, {"reason": "disk_full"}, {"phase": "source_stopped"}, {"phase": "migrating"}, {"format": True},
    {"operation_id": "0" * 64}, {"mode": "upgrade-activate"}, {"extra": "SECRET"},
])
def test_busy_reports_refuse_unrecognized_unbound_or_post_stop_diagnoses(mutation):
    request = {"mode": "upgrade-prepare", "operation_id": OPERATION, "plan_id": "d" * 64}
    report = {"format": 1, "kind": "server_busy", **request, "phase": "none", "reason": "compute_jobs"}
    with pytest.raises(host.InstallError, match="computation is running"):
        server_changes.reject_failure(report, request)
    with pytest.raises(ValueError):
        server_changes.reject_failure(report | mutation, request)


@pytest.mark.parametrize("reason", ["disk_full", "inflight_execution"])
def test_migration_diagnosis_survives_transport_without_accepting_completion_and_can_resume(operator, reason):
    change, engine, observation, plan, *_ = operator
    original = engine.command

    def refuse(args, **options):
        if "migrate-offline" in args:
            return subprocess.CompletedProcess(args, 0, stdout=host.canonical(
                {"format": 1, "kind": "upgrade_migration_failure", "reason": reason}))
        return original(args, **options)

    engine.command = refuse
    with pytest.raises(host.InstallError, match="Migration diagnosis: " + reason) as error:
        change("upgrade-prepare")
    assert "make upgrade-resume" in str(error.value)
    directory = engine.directory / "server-upgrades" / OPERATION
    retained = {name: (directory / name).read_bytes() for name in ("source.rnbk", "backup.key")}
    assert engine._read(directory / "operation.json")["phase"] == "migrating"
    assert observation["active"] is False
    engine.command = original
    assert change("upgrade-prepare")["phase"] == "prepared"
    assert all((directory / name).read_bytes() == body for name, body in retained.items())
    assert observation["helpers"].count("backup") == 1


@pytest.mark.parametrize("mutation", [
    {"reason": "SECRET"}, {"reason": []}, {"reason": True}, {"format": True},
    {"reason": "disk_full", "phase": "completed"}, {"reason": "disk_full", "operation_id": "0" * 64},
    {"reason": "disk_full", "extra": "SECRET"},
])
def test_migration_failure_reports_refuse_unrecognized_or_unbound_diagnoses(mutation):
    request = {"mode": "upgrade-prepare", "operation_id": OPERATION, "plan_id": "d" * 64}
    report = {"format": 1, "kind": "release_change_failure", **request, "phase": "migrating", "reason": "disk_full"}
    with pytest.raises(ValueError):
        server_changes.reject_failure(report | mutation, request)


@pytest.mark.parametrize("mutation", [{"format": True}, {"reason": "SECRET"}, {"reason": []}, {"extra": "SECRET"}])
def test_helper_failure_envelope_is_not_arbitrary_diagnostic_text_or_preparation_receipt(operator, mutation):
    change, engine, observation, *_ = operator
    original = engine.command

    def refuse(args, **options):
        if "migrate-offline" in args:
            return subprocess.CompletedProcess(args, 0, stdout=host.canonical(
                {"format": 1, "kind": "upgrade_migration_failure", "reason": "disk_full", **mutation}))
        return original(args, **options)

    engine.command = refuse
    with pytest.raises(host.InstallError, match="Reported operation phase: migrating") as error:
        change("upgrade-prepare")
    assert "SECRET" not in str(error.value) and "Migration diagnosis" not in str(error.value)
    assert observation["active"] is False
    assert engine._read(engine.directory / "server-upgrades" / OPERATION / "operation.json")["phase"] == "migrating"


def test_transport_timeout_reaps_only_owned_local_process_and_claims_no_remote_stop(monkeypatch):
    children = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(server_changes.subprocess, "Popen", launch)
    with pytest.raises(host.InstallError, match="changes may have occurred"):
        server_changes._run([sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(60)"], [b"x"], timeout=0.2)
    assert children[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


@pytest.mark.parametrize("mutation", ["truncated", "checksum", "extra_bytes", "size", "schema"])
def test_remote_bad_archive_upload_never_invokes_mutating_backend(operator, monkeypatch, mutation):
    change, engine, _, plan, requests, *_ = operator
    captured = []

    def capture(args, chunks):
        captured.append(b"".join(chunks))
        return subprocess.CompletedProcess(args, 255, stdout=b"")

    with pytest.raises(host.InstallError):
        change("upgrade-prepare", command=capture)
    packet = io.BytesIO(captured[0])
    script = packet.read(int(packet.readline()))
    # Capture the request without performing a release change.
    script = script.replace(b"if os.geteuid() != 0:", b"if False:", 1)
    script = script.replace(b"package.server_changes.execute_remote(request, sys.stdin.buffer)", b"capture_request(request)", 1)
    with redirect_stdout(io.StringIO()):
        exec(compile(script, "<upload-fixture>", "exec"), {"capture_request": lambda value: requests.append(value) or {}})
    request = copy.deepcopy(requests[-1])
    body = packet.read()
    if mutation == "truncated":
        body = body[:-50]
    elif mutation == "checksum":
        body = b"not a release" + body[12:]
    elif mutation == "extra_bytes":
        body += b"unapproved replacement"
    elif mutation == "size":
        request["upload_sizes"][0] = True
    else:
        request["identity"]["target_database_schema"] += 1
        request["plan_id"] = hashlib.sha256(host.canonical(request["identity"])).hexdigest()
    calls = []
    monkeypatch.setattr(engine, "prepare", lambda *args, **kwargs: calls.append(args))
    with pytest.raises((host.InstallError, ValueError)):
        server_changes.execute_remote(request, io.BytesIO(body), engine=engine)
    assert not calls
    assert not list((engine.directory / "staging").glob("server-change-*"))


@pytest.mark.parametrize(
    "mutation", ["extra", "identity", "schema", "phase", "hash", "count", "work_count_bool", "work_count_negative",
                 "counts_extra", "displaced", "displaced_bytes"]
)
def test_rollback_receipt_rejects_unbound_nested_evidence(operator, mutation):
    change, _, _, _, requests, *_ = operator
    change("upgrade-prepare")
    change("upgrade-activate")
    receipt = change("rollback-prepare")
    request = requests[-1]
    modified = copy.deepcopy(receipt)
    restoration = modified["restoration"]
    if mutation == "extra":
        restoration["credential"] = "do not expose"
    elif mutation == "identity":
        restoration["rollback_id"] = "a" * 64
    elif mutation == "schema":
        restoration["source_schema"] = True
    elif mutation == "phase":
        restoration["phase"] = "released"
    elif mutation == "hash":
        restoration["backup_sha256"] = "private unknown output"
    elif mutation == "count":
        restoration["recovery"]["sessions_revoked"] = True
    elif mutation == "work_count_bool":
        restoration["recovery"]["work_workers_revoked"] = True
    elif mutation == "work_count_negative":
        restoration["recovery"]["work_workers_revoked"] = -1
    elif mutation == "counts_extra":
        restoration["recovery"]["private_key"] = "do not expose"
    elif mutation == "displaced":
        modified["displaced"]["sha256"] = "not a digest"
    else:
        modified["displaced"]["bytes"] = 2 * server_changes.server_upgrade.MAX_BACKUP + 1
    with pytest.raises(host.InstallError, match="changes may have occurred"):
        server_changes.validate_receipt(modified, request)


def test_changed_rollback_id_cannot_replace_original_operator_or_root_ownership(operator):
    change, _, _, _, requests, *_ = operator
    change("upgrade-prepare")
    change("rollback-prepare")
    before = len(requests)
    with pytest.raises(host.InstallError, match="rollback identity"):
        change("rollback-prepare", rollback_id="a" * 64)
    with pytest.raises(host.InstallError, match="rollback identity"):
        change("rollback-activate", rollback_id="a" * 64)
    assert len(requests) == before


@pytest.mark.parametrize("mode", ["upgrade-activate", "rollback-prepare", "rollback-activate"])
def test_activation_and_rollback_cannot_create_replacement_operator_identity(operator, mode):
    change, _, _, _, requests, *_ = operator
    with pytest.raises(host.InstallError, match="original operator operation directory"):
        change(mode)
    assert not requests


@pytest.mark.parametrize("mode", ["upgrade-activate", "rollback-prepare", "rollback-activate"])
def test_activation_and_rollback_reject_replacement_archives_and_allowance(operator, mode):
    change, _, _, _, requests, *_ = operator
    for changes in ({"source_bundle": Path("replacement")}, {"target_bundle": Path("replacement")}, {"maximum": 1024**2}):
        with pytest.raises(host.InstallError):
            change(mode, **changes)
    assert not requests


@pytest.mark.parametrize("mode", sorted(server_changes.MODES))
def test_cli_routes_exact_mode_specific_inputs(operator, tmp_path, monkeypatch, capsys, mode):
    _, _, _, plan, _, _, key, known = operator
    path = tmp_path / "plan.json"
    write_document(path, plan, new=True)
    calls = []

    def change(*args, **kwargs):
        calls.append((args, kwargs))
        return {"operation": mode}

    monkeypatch.setattr(server_changes, "change_remote", change)
    args = [
        mode,
        "--plan",
        str(path),
        "--confirm-plan",
        plan["plan_id"],
        "--operation-id",
        OPERATION,
        "--state-dir",
        str(tmp_path / "operator"),
        "--ssh-host",
        "server.example.org",
        "--identity",
        str(key),
        "--known-hosts",
        str(known),
    ]
    args += ["--confirm-workers-fenced"] if mode.endswith("prepare") else ["--confirm-activation"]
    if mode == "upgrade-prepare":
        args += ["--source-bundle", "/secure/source.tar.gz", "--target-bundle", "/secure/target.tar.gz"]
    if mode.startswith("rollback"):
        args += ["--rollback-id", ROLLBACK]
    if mode == "rollback-prepare":
        args += ["--confirm-newer-data-loss"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"operation": mode}
    assert calls[0][0][:2] == (mode, plan)
    assert calls[0][1]["workers_fenced"] is mode.endswith("prepare")
    assert calls[0][1]["activation_confirmed"] is mode.endswith("activate")


def test_upgrade_plan_output_is_private_immutable_and_contains_original_identity(operator, tmp_path, monkeypatch, capsys):
    _, _, _, plan, *_ = operator
    monkeypatch.setattr(server_diagnostics, "diagnose_remote", lambda *args, **kwargs: plan)
    path = tmp_path / "saved-plan.json"
    args = [
        "upgrade-plan",
        "--bundle",
        "source",
        "--confirm-release",
        plan["source"]["release_id"],
        "--target-bundle",
        "target",
        "--confirm-target-release",
        plan["target"]["release_id"],
        "--public-url",
        plan["source"]["public_url"],
        "--ssh-host",
        "server.example.org",
        "--identity",
        "identity",
        "--known-hosts",
        "known",
        "--output",
        str(path),
    ]
    assert cli.main(args) == 0
    assert read_document(path) == plan and path.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out) == plan
    with pytest.raises(SystemExit):
        cli.main(args)
    assert read_document(path) == plan
