"""Real successive SQL/rollback state; explicit root/systemd/package/HTTPS fixtures."""

import subprocess
from contextlib import ExitStack

import pytest
from test_public_host_rollback import candidate
from test_public_host_rollback_activation import activate, ready, rollback_activate, rollback_prepare
from test_public_host_rollback_activation import activation_inputs as activation_inputs
from test_public_host_rollback_activation import bootstrap as bootstrap
from test_public_host_rollback_activation import installed as installed
from test_public_host_rollback_activation import payload as payload
from test_public_host_rollback_activation import prepared_inputs as prepared_inputs
from test_public_host_rollback_activation import rollback_inputs as rollback_inputs
from test_public_host_upgrade import OPERATION, ORIGIN, operation, pack, prepare, refresh_plan

from reasoning_ledger.deployment import host, server_operations
from reasoning_ledger.server import backup as snapshots
from reasoning_ledger.server import maintenance, rollback
from reasoning_ledger.server import store as store_module
from reasoning_ledger.server.store import Store

NEXT_OPERATION = "1" * 64


def next_inputs(inputs, *, target_schema=None, supports_handoff=True):
    engine, source, _target, source_checksum, *_, observation = inputs
    directory = engine.installer._directory("var/lib/reasoning-deploy/next-upgrade-fixture", 0o700)
    host.extract_bundle(source, directory)
    files = {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    files["web/assets/shared-x.js"] += b"\n// Next target fixture\n"
    features = [feature for feature in host.INSTALLATION_FEATURES if supports_handoff or feature != "server-rollback-upgrade-v1"]
    target = pack(
        source.parent / "next-upgrade.tar.gz",
        files,
        manifest_change=lambda manifest: manifest.update(
            database_schema=target_schema or len(maintenance.MIGRATIONS),
            installation_features=features,
            source_revision="9" * 40,
        ),
    )
    target.chmod(0o600)
    source_manifest, source_release, _ = host.inspect_bundle(source)
    manifest, release, checksum = host.inspect_bundle(target)
    report = engine.inspector.inspect(source_manifest, source_release, source_checksum, ORIGIN)
    plan = server_operations.upgrade_plan(report, source_manifest, manifest, release, checksum)
    assert plan["preflight_checks_passed"] is True
    return engine, source, target, source_checksum, checksum, plan, observation


@pytest.mark.parametrize("increase_schema", [False, True])
def test_managed_upgrade_after_completed_rollback_retains_original_history_and_activates_successor(
    rollback_inputs, monkeypatch, increase_schema
):
    original = rollback_inputs
    ready(original)
    root_rollback = rollback_activate(original)
    engine = original[0]
    data = engine.root / "var/lib/reasoning"
    store = Store(data)
    store.bootstrap(store._setup_token, "live@example.org", "Owner", "managed-successor-password", "Live source organization")
    original_rollback_history = (operation(original) / "rollback-completion.json").read_bytes()
    original_source = (data / rollback.JOURNAL).read_bytes()
    source_schema = maintenance.MIGRATIONS
    target_schema = (*source_schema, ("CREATE TABLE managed_successor_probe(value TEXT)",)) if increase_schema else source_schema
    inputs = next_inputs(original, target_schema=len(target_schema))
    target = inputs[5]["target"]
    command, health = engine.command, engine.inspector.health
    operation_path = engine.directory / "server-upgrades" / NEXT_OPERATION / "operation.json"
    with ExitStack() as serving:

        def commands(args, **options):
            target_helper = args[:4] == ["runuser", "-u", "reasoning", "--"] and target["release_id"] in args[4]
            target_start = args[:3] == ["systemctl", "start", "reasoning-server"]
            if target_helper or target_start:
                with monkeypatch.context() as patch:
                    for module in (maintenance, snapshots, rollback, store_module):
                        patch.setattr(module, "MIGRATIONS", target_schema)
                    if target_start:
                        assert engine._read(operation_path)["phase"] == "activating"
                        assert not (engine.root / "var/lib/reasoning-deploy/server-upgrade-fence").exists()
                        expected = host.server_unit(
                            target["release_id"], release_identity=True, maintenance_operation=NEXT_OPERATION
                        ).replace("PUBLIC_ORIGIN", ORIGIN)
                        assert (engine.root / "etc/systemd/system/reasoning-server.service").read_text() == expected
                        if not inputs[-1]["active"]:
                            serving.enter_context(maintenance.serving(data, release=target["release_id"], operation_id=NEXT_OPERATION))
                            assert Store(data).initialized()
                            inputs[-1]["active"] = True
                        inputs[-1]["calls"].append(args)
                        return subprocess.CompletedProcess(args, 0, stdout=b"")
                    return command(args, **options)
            if args[:2] == ["systemctl", "stop"]:
                serving.close()
            return command(args, **options)

        def readiness(origin, **options):
            if options.get("expected_release") == target["release_id"]:
                assert inputs[-1]["active"] is True
                assert maintenance.journal(data)["operation_id"] == NEXT_OPERATION
                assert rollback._read(data, rollback.JOURNAL)["phase"] == "superseded"
                return {"public_url": origin, "https_ready": True, "initialized": True}
            return health(origin, **options)

        engine.command = engine.inspector.command = engine.installer.command = commands
        engine.inspector.health = readiness
        prepare(inputs, operation_id=NEXT_OPERATION)
        # Retention keeps the rolled-back operation, bound through its source and named by provenance, with its stage.
        assert (operation(original) / "rollback-completion.json").exists() and candidate(original).is_dir()
        assert not (engine.root / "var/lib/reasoning-upgrade-scratch" / OPERATION).exists()
        result = engine.activate(
            operation_id=NEXT_OPERATION,
            confirmed_plan=inputs[5]["plan_id"],
            target_release=target["release_id"],
            activation_confirmed=True,
            skip_platform=True,
        )
        assert result["upgrade_completed"] is True
        assert (operation(original) / "rollback-completion.json").read_bytes() == original_rollback_history
        assert (data / rollback.HISTORY / (root_rollback["rollback_id"] + ".json")).read_bytes() == original_source
        with monkeypatch.context() as patch:
            for module in (maintenance, snapshots, rollback, store_module):
                patch.setattr(module, "MIGRATIONS", target_schema)
            assert Store(data).initialized()
            assert (
                engine.activate(
                    operation_id=NEXT_OPERATION,
                    confirmed_plan=inputs[5]["plan_id"],
                    target_release=target["release_id"],
                    activation_confirmed=True,
                    skip_platform=True,
                )
                == result
            )
            with Store(data).transaction() as database:
                assert database.execute("SELECT name FROM organizations").fetchone()[0] == "Live source organization"
                assert database.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_old_target_without_handoff_capability_is_refused_before_stopping_restored_source(rollback_inputs):
    ready(rollback_inputs)
    rollback_activate(rollback_inputs)
    inputs = next_inputs(rollback_inputs, supports_handoff=False)
    calls = len(inputs[-1]["calls"])
    with pytest.raises(host.InstallError, match="handoff support"):
        prepare(inputs, operation_id=NEXT_OPERATION)
    assert len(inputs[-1]["calls"]) == calls
    assert inputs[-1]["active"] is True
    assert not (inputs[0].directory / "server-upgrades" / NEXT_OPERATION).exists()


@pytest.mark.parametrize("evidence", ["root-completion", "root-journal", "source-provenance"])
def test_restored_source_must_match_original_protected_rollback_completion_before_upgrade(rollback_inputs, evidence):
    ready(rollback_inputs)
    rollback_activate(rollback_inputs)
    inputs = next_inputs(rollback_inputs)
    engine = inputs[0]
    if evidence == "root-completion":
        (operation(rollback_inputs) / "rollback-completion.json").unlink()
    elif evidence == "root-journal":
        (operation(rollback_inputs) / "rollback.json").unlink()
    else:
        path = engine.root / "var/lib/reasoning" / rollback.JOURNAL
        value = rollback._read(path.parent, rollback.JOURNAL)
        value["state_sha256"] = "0" * 64
        host.atomic_write(path, host.canonical(value))
    calls = len(inputs[-1]["calls"])
    with pytest.raises(host.InstallError):
        prepare(inputs, operation_id=NEXT_OPERATION)
    assert len(inputs[-1]["calls"]) == calls
    assert inputs[-1]["active"] is True
    assert not (engine.directory / "server-upgrades" / NEXT_OPERATION).exists()


def test_ordinary_old_target_can_still_be_rolled_back_without_upgrade_handoff_support(rollback_inputs):
    inputs = next_inputs(rollback_inputs, supports_handoff=False)
    inputs = refresh_plan(inputs)
    inputs[0].inspector.health = lambda origin, **options: {"public_url": origin, "https_ready": True, "initialized": False}
    prepare(inputs)
    activate(inputs)
    rollback_prepare(inputs)
    assert rollback_activate(inputs)["rollback_completed"] is True
