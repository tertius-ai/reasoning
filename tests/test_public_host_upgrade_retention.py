"""Upgrade retention and the free-space check, with real preparation/activation/rollback and explicit host fixtures."""

import errno
import hashlib
import json
import subprocess
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from test_public_host_rollback import candidate, rollback_prepare
from test_public_host_rollback_activation import activate, rollback_activate
from test_public_host_rollback_activation import activation_inputs as activation_inputs
from test_public_host_rollback_activation import bootstrap as bootstrap
from test_public_host_rollback_activation import installed as installed
from test_public_host_rollback_activation import payload as payload
from test_public_host_rollback_activation import prepared_inputs as prepared_inputs
from test_public_host_rollback_activation import rollback_inputs as rollback_inputs
from test_public_host_upgrade import OPERATION, ORIGIN, operation, pack, prepare, refresh_plan, state

from reasoning_ledger.deployment import host, operator_sync_host, server_changes, server_operations
from reasoning_ledger.deployment import server_upgrade as upgrade
from reasoning_ledger.server import maintenance
from reasoning_ledger.server.store import Store

R = {number: hashlib.sha256(b"release-%d" % number).hexdigest() for number in range(1, 13)}
SUCCESSOR = "2" * 64


def binding(release):
    return {"release_id": release, "bundle_sha256": "b" * 64, "public_url": ORIGIN, "source_revision": "c" * 40, "dirty": False}


def live_binding(engine):
    return {key: value for key, value in engine._read(engine.directory / "installation.json").items() if key != "phase"}


def tree(engine, relative, mode=0o700):
    path = engine.installer._directory(relative, mode)
    (path / "payload").write_bytes(b"x" * 8192)
    return path


def release(engine, name):
    return tree(engine, "opt/reasoning/releases/" + name, 0o755)


def history(engine, name, source, target, created, *, completed=True, rollback=None):
    """A retained operation with exactly the root records that retention reads."""
    directory = tree(engine, "var/lib/reasoning-deploy/server-upgrades/" + name)
    value = {
        "format": 1,
        "operation_id": name,
        "plan_id": "a" * 64,
        "identity": {"source": source, "target": target},
        "phase": "completed" if completed and rollback is None else "prepared",
        "backup": {"created_at": created},
    }
    engine._write(directory / "operation.json", value)
    if completed and rollback is None:
        engine._write(directory / "completion.json", engine._upgrade_receipt(value, directory))
    if rollback is not None:
        journal = {"phase": "completed" if completed else "restoring", "rollback_id": rollback, "restoration": {"phase": "prepared"},
                   "displaced": {"files": 1}}
        engine._write(directory / "rollback.json", journal)
        engine._write(directory / "rollback-completion.json", engine._rollback_receipt(value, journal, directory))
        tree(engine, "var/lib/reasoning-rollback-" + rollback)
        tree(engine, "run/reasoning-rollback-" + rollback)
    tree(engine, "var/lib/reasoning-upgrade-scratch/" + name)
    tree(engine, "run/reasoning-upgrade-" + name)
    return directory


def names(path):
    return sorted(item.name for item in path.iterdir()) if path.exists() else []


@pytest.fixture
def retained_history(prepared_inputs):
    """Eight operations around the live installation, plus releases, temporary and interrupted leftovers."""
    engine = prepared_inputs[0]
    live = live_binding(engine)
    for number in R:
        release(engine, R[number])
    history(engine, "1" * 64, binding(R[1]), live, 300)  # bound to the live installation
    history(engine, "2" * 64, binding(R[2]), binding(R[1]), 200)  # next most recent completed
    history(engine, "3" * 64, binding(R[3]), binding(R[2]), 100)  # older completed: removed
    history(engine, "4" * 64, binding(R[4]), binding(R[5]), 50, rollback="a" * 64)  # older completed rollback: removed
    history(engine, "5" * 64, binding(R[6]), binding(R[7]), 400, completed=False)  # unfinished: kept
    history(engine, "6" * 64, binding(R[8]), binding(R[9]), 5, rollback="b" * 64)  # live rollback provenance: kept
    tampered = history(engine, "7" * 64, binding(R[10]), binding(R[11]), 1)  # unrecognized completion: kept
    host.atomic_write(tampered / "completion.json", host.canonical({"changed": True}))
    engine.installer._directory("var/lib/reasoning-deploy/server-upgrades/" + "8" * 64, 0o700)  # never journaled: kept
    engine.installer._directory("var/lib/reasoning-deploy/server-upgrades/notes", 0o700)
    provenance = {"format": 1, "operation_id": "6" * 64, "rollback_id": "b" * 64, "phase": "released"}
    host.atomic_write(engine.root / "var/lib/reasoning/server-rollback.json", host.canonical(provenance))
    tree(engine, "opt/reasoning/releases/.release-abc12345", 0o755)
    tree(engine, "opt/reasoning/releases/.upgrade-release-x_9y8z7w6", 0o755)
    (engine.root / "opt/reasoning/releases" / ("9" * 64)).symlink_to(engine.root / "opt/reasoning/releases" / R[12])
    tree(engine, "opt/reasoning/modal/" + R[3], 0o755)
    tree(engine, "var/lib/reasoning-deploy/server-upgrades/.pruned-" + "e" * 64)
    tree(engine, "opt/reasoning/releases/.pruned-" + "e" * 64, 0o755)
    tree(engine, "run/.pruned-reasoning-upgrade-" + "e" * 64)
    return prepared_inputs


def assert_retained(engine, source_release):
    root = engine.root
    assert names(engine.directory / "server-upgrades") == sorted(
        ["1" * 64, "2" * 64, "5" * 64, "6" * 64, "7" * 64, "8" * 64, "notes"]
    )
    assert names(root / "opt/reasoning/releases") == sorted(
        [source_release, "9" * 64, *(R[number] for number in (1, 2, 6, 7, 8, 9, 10, 11))]
    )
    assert names(root / "opt/reasoning/modal") == [R[3]]
    # Scratch goes for every completed operation; an unfinished one keeps everything.
    assert names(root / "var/lib/reasoning-upgrade-scratch") == ["5" * 64, "7" * 64]
    assert names(root / "run") == ["reasoning-upgrade-" + "5" * 64, "reasoning-upgrade-" + "7" * 64]
    # A kept completed rollback keeps its published stage; a removed one does not.
    assert (root / "var/lib" / ("reasoning-rollback-" + "b" * 64)).is_dir()
    assert not (root / "var/lib" / ("reasoning-rollback-" + "a" * 64)).exists()
    assert not [name for name in names(root / "var/lib") if name.startswith(".pruned-")]


def test_retention_keeps_bound_previous_provenance_and_unfinished_history_and_removes_the_rest(retained_history):
    engine = retained_history[0]
    source_release = retained_history[5]["source"]["release_id"]
    removed = engine._prune("d" * 64)
    assert_retained(engine, source_release)
    assert removed["upgrades_removed"] == 3  # two old operations and one interrupted removal
    assert removed["releases_removed"] == 5  # R3, R4, R5, the unused R12 and one interrupted removal
    assert removed["scratch_removed"] == 16  # ten upgrade and three rollback scratch trees, two temporary releases, one leftover
    assert removed["bytes_freed"] >= (3 + 5 + 16) * 8192
    record = engine._read(engine.directory / "server-upgrade-retention.json")
    assert record == {"format": 1, "operation_id": "d" * 64, **removed}
    assert (engine.directory / "server-upgrade-retention.json").stat().st_mode & 0o777 == 0o600
    assert engine._prune("d" * 64) == {"upgrades_removed": 0, "releases_removed": 0, "scratch_removed": 0, "bytes_freed": 0}
    assert engine._read(engine.directory / "server-upgrade-retention.json") == record
    assert_retained(engine, source_release)


def test_an_operation_with_unreadable_releases_keeps_every_release(retained_history):
    engine = retained_history[0]
    unreadable = engine.directory / "server-upgrades" / ("f" * 64)
    unreadable.mkdir(mode=0o700)
    host.atomic_write(unreadable / "operation.json", b"not a journal")
    removed = engine._prune("d" * 64)
    assert removed["upgrades_removed"] == 3 and removed["releases_removed"] == 1  # only the interrupted removal
    assert (engine.root / "opt/reasoning/releases" / R[3]).is_dir() and (engine.root / "opt/reasoning/releases" / R[12]).is_dir()
    assert (unreadable / "operation.json").read_bytes() == b"not a journal"


def test_interrupted_removal_hides_its_victim_and_the_next_prune_finishes_it(retained_history, monkeypatch):
    engine = retained_history[0]
    source_release = retained_history[5]["source"]["release_id"]
    original, calls = upgrade.shutil.rmtree, []

    def interrupted(path, *args, **kwargs):
        calls.append(path)
        if len([call for call in calls if ".pruned-" in str(call)]) == 4:
            raise OSError(errno.EIO, "interrupted removal fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(upgrade.shutil, "rmtree", interrupted)
    with pytest.raises(upgrade.ServerBusy) as refused:
        engine._prune("d" * 64)
    assert refused.value.reason == "retention_failed" and "interrupted removal fixture" not in str(refused.value)
    hidden = calls[-1]
    assert hidden.name.startswith(".pruned-") and hidden.exists()
    assert not hidden.with_name(hidden.name.removeprefix(".pruned-")).exists()
    assert not (engine.directory / "server-upgrade-retention.json").exists()
    monkeypatch.setattr(upgrade.shutil, "rmtree", original)
    engine._prune("d" * 64)
    assert not hidden.exists()
    assert_retained(engine, source_release)


def test_retention_runs_only_without_pending_upgrade_or_recovery(retained_history):
    engine = retained_history[0]
    host.atomic_write(engine.directory / "active-server-recovery.json", b"{}")
    with pytest.raises(host.InstallError, match="no pending upgrade"):
        engine._prune("d" * 64)
    assert (engine.directory / "server-upgrades" / ("3" * 64)).exists()


def failure_request(inputs):
    plan = inputs[5]
    return {"mode": "upgrade-prepare", "operation_id": OPERATION, "plan_id": plan["plan_id"],
            "identity": {key: plan[key] for key in upgrade.IDENTITY_FIELDS}}


def test_retention_failure_refuses_before_any_journal_with_the_source_serving(prepared_inputs, monkeypatch):
    engine, *_, observation = prepared_inputs
    unused = release(engine, R[12])
    original = upgrade.shutil.rmtree

    def refused(path, *args, **kwargs):
        if ".pruned-" in str(path):
            raise OSError(errno.EBUSY, "removal fixture")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch, pytest.raises(upgrade.ServerBusy) as error:
        patch.setattr(upgrade.shutil, "rmtree", refused)
        prepare(prepared_inputs)
    assert error.value.reason == "retention_failed"
    assert observation["active"] is True and observation["probes"] == []
    assert not (engine.directory / "server-upgrades" / OPERATION).exists() and not engine.active.exists()
    assert not (engine.root / upgrade.FENCE).exists()
    report = server_changes.failure_report(failure_request(prepared_inputs), engine=engine, error=error.value)
    assert report["kind"] == "server_busy" and report["phase"] == "none" and report["reason"] == "retention_failed"
    with pytest.raises(host.InstallError, match="could not be removed safely") as rejected:
        server_changes.reject_failure(report, failure_request(prepared_inputs))
    assert "No upgrade is pending" in str(rejected.value) and "make upgrade again" in str(rejected.value)
    assert not unused.exists() and unused.with_name(".pruned-" + unused.name).exists()
    assert prepare(prepared_inputs)["phase"] == "prepared"
    assert not unused.with_name(".pruned-" + unused.name).exists()


def test_live_size_counts_only_backed_up_state_without_following_links(prepared_inputs):
    engine = prepared_inputs[0]
    data = engine.root / "var/lib/reasoning"
    before = engine._live_bytes()
    assert before >= (data / "server.sqlite3").stat().st_size > 0
    chunk = data / "artifacts" / ("a" * 32)
    chunk.mkdir(parents=True, exist_ok=True)
    (chunk / "0").write_bytes(b"x" * 1000)
    (data / "research-sources").mkdir()
    (data / "research-sources" / "cache").write_bytes(b"x" * 5000)
    outside = engine.root / "outside"
    outside.write_bytes(b"x" * 7000)
    (chunk / "1").symlink_to(outside)
    assert engine._live_bytes() == before + 1000


def test_insufficient_space_refuses_before_the_fence_with_the_source_serving_and_resumes(prepared_inputs):
    engine, *_, observation = prepared_inputs
    measured = []

    def usage(path):
        measured.append(path)
        return SimpleNamespace(f_bavail=1, f_frsize=4096)

    engine.disk_usage = usage
    with pytest.raises(upgrade.ServerBusy) as refused:
        prepare(prepared_inputs)
    space = {"filesystem": "/var/lib/reasoning-upgrade-scratch", "available_bytes": 4096,
             "required_bytes": upgrade.SPACE_FACTOR * engine._live_bytes() + upgrade.SPACE_MARGIN}
    assert refused.value.reason == "insufficient_disk" and refused.value.space == space
    assert len(measured) == 1  # every role shares the fixture's filesystem
    assert state(prepared_inputs)["phase"] == "target_ready"
    assert observation["active"] is True and observation["probes"] == [True, True]
    assert not (engine.root / upgrade.FENCE).exists() and not (engine.root / upgrade.DROPIN).exists()
    assert not any(args[:2] == ["systemctl", "stop"] for args in observation["calls"])
    report = server_changes.failure_report(failure_request(prepared_inputs), engine=engine, error=refused.value)
    assert report == {"format": 1, "kind": "server_busy", "mode": "upgrade-prepare", "operation_id": OPERATION,
                      "plan_id": prepared_inputs[5]["plan_id"], "phase": "target_ready", "reason": "insufficient_disk", **space}
    with pytest.raises(host.InstallError, match="lacks the free disk space") as rejected:
        server_changes.reject_failure(report, failure_request(prepared_inputs))
    text = str(rejected.value)
    assert "GB must be free on the filesystem holding /var/lib/reasoning-upgrade-scratch; 0.0 GB is free" in text
    assert "keeps serving; this upgrade is pending" in text and "Free disk space on the server, then run make upgrade-resume." in text
    engine.disk_usage = lambda path: SimpleNamespace(f_bavail=2**40, f_frsize=1)
    assert prepare(prepared_inputs)["phase"] == "prepared"
    assert observation["probes"] == [True, True, True]


def test_rollback_of_the_latest_upgrade_and_its_replay_survive_retention(rollback_inputs):
    inputs = rollback_inputs
    engine = inputs[0]
    old = [history(engine, str(number) * 64, binding(R[number]), binding(R[number + 5]), 10 - number) for number in (3, 4, 5)]
    for number in (3, 4, 5, 8, 9, 10):
        release(engine, R[number])
    prepare(inputs)
    # Nothing is bound to the live installation yet: the two most recent completed operations remain.
    assert [path.exists() for path in old] == [True, True, False]
    assert not (engine.root / "opt/reasoning/releases" / R[5]).exists() and (engine.root / "opt/reasoning/releases" / R[4]).exists()
    activate(inputs)
    scratch = engine.root / "var/lib/reasoning-upgrade-scratch" / OPERATION
    assert scratch.exists()
    # A later preparation prunes before it can be refused, for example because the server is busy.
    engine._prune("d" * 64)
    assert [path.exists() for path in old] == [True, False, False]
    assert not scratch.exists() and (operation(inputs) / "source.rnbk").exists()
    assert {inputs[5][role]["release_id"] for role in ("source", "target")} <= set(names(engine.root / "opt/reasoning/releases"))
    rollback_prepare(inputs)
    result = rollback_activate(inputs)
    assert result["rollback_completed"] is True
    # The rolled-back operation is now bound through its source and named by live provenance.
    engine._prune("d" * 64)
    assert (operation(inputs) / "rollback-completion.json").exists() and candidate(inputs).is_dir()
    assert rollback_activate(inputs) == result


def successor(inputs):
    """A further clean release built from the installed target, planned against it."""
    engine, _source, target, _source_checksum, target_checksum, _plan, observation = inputs
    directory = engine.installer._directory("var/lib/reasoning-deploy/successor-fixture", 0o700)
    host.extract_bundle(target, directory)
    files = {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    files["web/assets/shared-x.js"] += b"\n// Successor target fixture\n"
    target_manifest, target_release, _ = host.inspect_bundle(target)
    bundle = pack(
        target.parent / "successor.tar.gz",
        files,
        manifest_change=lambda manifest: manifest.update(
            database_schema=target_manifest["database_schema"],
            installation_features=list(host.INSTALLATION_FEATURES),
            source_revision="e" * 40,
        ),
    )
    bundle.chmod(0o600)
    manifest, release_id, checksum = host.inspect_bundle(bundle)
    report = engine.inspector.inspect(target_manifest, target_release, target_checksum, ORIGIN)
    plan = server_operations.upgrade_plan(report, target_manifest, manifest, release_id, checksum)
    assert plan["preflight_checks_passed"], plan
    return engine, target, bundle, target_checksum, checksum, plan, observation


def test_successive_upgrades_after_retention_activate_replay_and_keep_the_operator_sync_archive(activation_inputs):
    engine = activation_inputs[0]
    first_source = activation_inputs[5]["source"]
    request = {"format": 1, "plan_id": "a" * 64, "deployment_id": "b" * 32, "droplet_id": 456}
    provisioning = {"format": 1, "binding": first_source, "request": request}
    host.atomic_write(engine.directory / "server-provisioning.json", host.canonical(provisioning))
    host.atomic_write(engine.root / "etc/reasoning-provisioning.json", host.canonical({"plan_id": "a" * 64, "deployment_id": "b" * 32}))
    inputs = refresh_plan(activation_inputs)
    prepare(inputs)
    activate(inputs)
    assert names(engine.directory / "server-upgrades") == [OPERATION]
    assert names(engine.root / "opt/reasoning/releases") == sorted(inputs[5][role]["release_id"] for role in ("source", "target"))

    unused = release(engine, R[12])
    following = successor(inputs)
    target = following[5]["target"]
    observation, data = following[-1], engine.root / "var/lib/reasoning"
    command, health = engine.command, engine.inspector.health
    pending = engine.directory / "server-upgrades" / SUCCESSOR / "operation.json"
    with ExitStack() as serving:

        def commands(args, **options):
            if args[:4] == ["runuser", "-u", "reasoning", "--"] and args[8:9] == [upgrade.BUSY_PROBE]:
                # The probe fixture names the first source's interpreter; the probe itself reads the live database.
                interpreter = args[4].replace(following[5]["source"]["release_id"], first_source["release_id"])
                args = [*args[:4], interpreter, *args[5:]]
                return command(args, **options)
            if args[:3] == ["systemctl", "start", "reasoning-server"] and pending.exists():
                assert engine._read(pending)["phase"] == "activating"
                if not observation["active"]:
                    serving.enter_context(maintenance.serving(data, release=target["release_id"], operation_id=SUCCESSOR))
                    Store(data)
                    observation["active"] = True
                observation["calls"].append(args)
                return subprocess.CompletedProcess(args, 0, stdout=b"")
            if args[:2] == ["systemctl", "stop"]:
                serving.close()
            return command(args, **options)

        def readiness(origin, **options):
            if options.get("expected_release") == target["release_id"]:
                assert observation["active"] is True
                return {"public_url": origin, "https_ready": True, "initialized": False}
            return health(origin, **options)

        engine.command = engine.inspector.command = engine.installer.command = commands
        engine.inspector.health = readiness
        prepare(following, operation_id=SUCCESSOR)
        # The first upgrade is bound to the live installation at that point: kept, without its scratch.
        assert names(engine.directory / "server-upgrades") == sorted([OPERATION, SUCCESSOR])
        assert names(engine.root / "var/lib/reasoning-upgrade-scratch") == [SUCCESSOR]
        assert not unused.exists() and (engine.root / "opt/reasoning/releases" / target["release_id"]).is_dir()
        options = {"operation_id": SUCCESSOR, "confirmed_plan": following[5]["plan_id"], "target_release": target["release_id"],
                   "activation_confirmed": True, "skip_platform": True}
        result = engine.activate(**options)
        assert result["upgrade_completed"] is True
        calls = len(observation["calls"])
        assert engine.activate(**options) == result
        assert not any(args[:2] in (["systemctl", "start"], ["systemctl", "stop"]) for args in observation["calls"][calls:])
        source_manifest = host.inspect_bundle(inputs[1])[0]
        body = operator_sync_host.export_state(first_source, request, source_manifest, inspector=engine.inspector)
        metadata = json.loads(body[4 : 4 + int.from_bytes(body[:4], "big")])
        assert metadata["phase"] == "verified" and metadata["installation"] == target
        assert metadata["retained"] == {"operation_id": SUCCESSOR, "archive": "target"}
