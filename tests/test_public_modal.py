"""Modal SDK/worker boundaries use non-executing fixtures, never paid resources."""

import asyncio
import copy
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from test_public_compute_worker import IMAGE, StoreTransport, fixture  # noqa: F401

from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_docker import CleanupUncertain, Outcome, RuntimeFault
from reasoning_ledger.server.compute_modal import SUPERVISOR_IMAGE, ModalRunner, ModalSDK, RemoteDockerRunner, modal_profile, sandbox_name
from reasoning_ledger.server.compute_worker import ComputeWorker, WorkerClient, main, write_state
from reasoning_ledger.server.security import private_file


def profile(**changes):
    return {"format": 1, "app": "reasoning-compute", "environment": "main", "token_id": "ak_fixture_token",
            "token_secret": "as_fixture_secret", "supervisor_image": SUPERVISOR_IMAGE, "resources": dict(DEFAULT_RESOURCES), **changes}


class SandboxFixture:
    object_id = "sb-fixture"


class SDKFixture:
    def __init__(self):
        self.creates, self.terminations, self.commands = [], [], []
        self.active, self.lose, self.uncertain, self.app = None, False, False, "ap-fixture"

    def identity(self):
        return self.app

    def create(self, name, limits):
        self.creates.append((name, limits))
        self.active = SandboxFixture()
        if self.lose:
            raise OSError("private provider body")
        return self.active

    def lookup(self, name, identifier=None):
        if self.active is None:
            raise CleanupUncertain("uncertain")
        return self.active

    def terminate(self, sandbox):
        if self.uncertain:
            raise OSError("private provider body")
        self.terminations.append(sandbox.object_id)
        self.active = None


class DockerFixture:
    def __init__(self, directory, worker, sdk, sandbox):
        self.sdk = sdk

    def verify_runtime_identity(self):
        pass

    def _command(self, command, **options):
        self.sdk.commands.append(command)

    def run(self, job, spool, log, stopped):
        assert not stopped()
        log(b"nonexecuting Modal transport fixture\n")
        return Outcome("succeeded", 42, 0, None)


def runner(tmp_path, worker="a" * 32, sdk=None):
    sdk = sdk or SDKFixture()
    return ModalRunner(tmp_path / "relay", worker, profile(), {"fixture": ComputeImage.parse(IMAGE)},
                       sdk=sdk, docker_factory=DockerFixture), sdk


def job(worker="a" * 32):
    program = "print('nonexecuting fixture')"
    return {"id": "c" * 32, "worker_id": worker, "attempt_id": "b" * 32,
            "image": IMAGE, "parameters": {"image_id": "fixture", "language": "python", "program": program,
                                          "resources": dict(DEFAULT_RESOURCES)},
            "program_digest": "sha256:" + hashlib.sha256(program.encode()).hexdigest()}


def test_run_uses_one_ephemeral_vm_and_existing_digest_pinned_docker_boundary(tmp_path):
    instance, sdk = runner(tmp_path)
    logs = []
    outcome = instance.run(job(), tmp_path, logs.append, lambda: False)
    assert outcome.status == "succeeded" and outcome.elapsed_ms == 42
    assert len(sdk.creates) == len(sdk.terminations) == 1
    assert sdk.commands == [["image", "pull", "--quiet", "--platform", "linux/amd64", IMAGE["reference"]]]
    assert instance.state() == {"phase": "idle"} and logs
    receipt = json.loads((instance.data_dir / "modal-last-attempt.json").read_bytes())
    assert receipt["execution_status"] == "succeeded" and receipt["cleanup_verified"] is True
    assert receipt["stage"] == "execution" and receipt["sandbox_id"] == "sb-fixture"
    assert logs == [b"nonexecuting Modal transport fixture\n"]


@pytest.mark.parametrize("stage,code", [("docker_setup", "docker_unavailable"), ("docker_ready", "invalid_runtime_identity"),
                                        ("image_pull", "output_limit"), ("image_pull", "wall_time_limit"),
                                        ("execution", "image_verification_failed")])
def test_failed_stage_survives_cleanup_with_safe_code_and_one_startup_message(tmp_path, monkeypatch, stage, code):
    instance, sdk = runner(tmp_path)

    class FailedDocker(DockerFixture):
        def __init__(self, *args):
            super().__init__(*args)
            if stage == "docker_setup":
                raise RuntimeFault(code)

        def verify_runtime_identity(self):
            if stage == "docker_ready":
                raise RuntimeFault(code)

        def _command(self, command, **options):
            assert "--quiet" in command and options["maximum"] == 65536 and options["timeout"] == 180
            if stage == "image_pull":
                raise RuntimeFault(code)

        def run(self, *args):
            raise RuntimeFault(code)

    if stage == "docker_ready":
        clock = iter((0, 181))
        monkeypatch.setattr("reasoning_ledger.server.compute_modal.time", SimpleNamespace(
            monotonic=lambda: next(clock), time=time.time, sleep=lambda _: None))
    instance.docker_factory = FailedDocker
    logs = []
    outcome = instance.run(job(), tmp_path, logs.append, lambda: False)
    receipt = json.loads((instance.data_dir / "modal-last-attempt.json").read_bytes())
    assert receipt["stage"] == stage and receipt["error_code"] == code
    assert receipt["execution_status"] == "failed" and receipt["cleanup_verified"] is True
    assert receipt["job_id"] == job()["id"] and receipt["attempt_id"] == job()["attempt_id"]
    assert outcome.status == "failed" and outcome.elapsed_ms == DEFAULT_RESOURCES["wall_time_seconds"] * 1000
    assert sdk.terminations == ["sb-fixture"] and instance.state() == {"phase": "idle"}
    if stage != "execution":
        assert len(logs) == 1 and code.encode() in logs[0]


@pytest.mark.parametrize("error", [OSError("private provider body as_secret sk-secret"), RuntimeFault("as_secret sk-secret")])
def test_creation_failure_does_not_publish_exception_messages_or_unknown_fault_codes(tmp_path, error):
    instance, sdk = runner(tmp_path)
    original = sdk.create

    def fail(*args):
        original(*args)
        raise error

    sdk.create = fail
    logs = []
    outcome = instance.run(job(), tmp_path, logs.append, lambda: False)
    data = (instance.data_dir / "modal-last-attempt.json").read_bytes()
    receipt = json.loads(data)
    assert receipt["stage"] == "sandbox_create" and receipt["error_code"] == "unexpected_error"
    assert receipt["exception_type"] == type(error).__name__
    assert outcome.status == "failed" and receipt["cleanup_verified"] is True
    assert b"private provider body" not in data + b"".join(logs)
    assert b"as_secret" not in data + b"".join(logs) and b"sk-secret" not in data + b"".join(logs)


def test_uncertain_vm_cleanup_never_records_cleanup_as_verified(tmp_path):
    instance, sdk = runner(tmp_path)
    sdk.uncertain = True
    with pytest.raises(CleanupUncertain):
        instance.run(job(), tmp_path, lambda _: None, lambda: False)
    receipt = json.loads((instance.data_dir / "modal-last-attempt.json").read_bytes())
    assert receipt["cleanup_verified"] is False and instance.state()["phase"] == "creating"


def test_diagnostic_symlink_refuses_before_paid_vm_creation(tmp_path):
    instance, sdk = runner(tmp_path)
    target = private_file(tmp_path / "private-original", b"unchanged")
    (instance.data_dir / "modal-last-attempt.json").symlink_to(tmp_path / "private-original")
    with pytest.raises(OSError):
        instance.run(job(), tmp_path, lambda _: None, lambda: False)
    assert sdk.creates == [] and target == (tmp_path / "private-original").read_bytes()


def test_sdk_exception_context_retains_only_safe_exception_type(tmp_path):
    instance, _ = runner(tmp_path)

    def fail(*args):
        try:
            raise AttributeError("private provider response and credentials")
        except AttributeError:
            raise RuntimeFault("docker_unavailable") from None

    instance.docker_factory = fail
    logs = []
    instance.run(job(), tmp_path, logs.append, lambda: False)
    data = (instance.data_dir / "modal-last-attempt.json").read_bytes()
    receipt = json.loads(data)
    assert receipt["exception_type"] == "AttributeError" and receipt["error_code"] == "docker_unavailable"
    assert b"private provider response" not in data + b"".join(logs)


def test_probe_is_not_a_paid_run_and_changed_app_or_credentials_refuse_state(tmp_path):
    instance, sdk = runner(tmp_path)
    instance.preflight(ComputeImage.parse(IMAGE), DEFAULT_RESOURCES)
    assert sdk.creates == []
    sdk.app = "ap-different"
    with pytest.raises(RuntimeFault, match="runtime_identity_changed"):
        instance.verify_runtime_identity()
    sdk.app = "ap-fixture"
    changed = ModalRunner(instance.data_dir, instance.worker_id, profile(token_secret="changed_private_secret"), instance.images, sdk=sdk)
    with pytest.raises(RuntimeFault, match="runtime_identity_changed"):
        changed.verify_runtime_identity()
    assert "fixture_secret" not in (instance.data_dir / "modal-runtime.json").read_text()


@pytest.mark.parametrize("mutation", ["worker", "digest", "image", "resources", "language", "inputs"])
def test_invalid_jobs_cannot_create_paid_sandboxes(tmp_path, mutation):
    instance, sdk = runner(tmp_path)
    value = copy.deepcopy(job())
    if mutation == "worker":
        value["worker_id"] = "e" * 32
    elif mutation == "digest":
        value["program_digest"] = "sha256:" + "f" * 64
    elif mutation == "image":
        value["image"]["reference"] = "registry.example/cpu@sha256:" + "f" * 64
    elif mutation == "resources":
        value["parameters"]["resources"]["cpu"] = 2
    elif mutation == "language":
        value["parameters"]["language"] = "julia"
    else:
        value["parameters"].update(inputs={"source": 42}, inputs_digest="sha256:" + "f" * 64)
    with pytest.raises(RuntimeFault):
        instance.run(value, tmp_path, lambda _: None, lambda: False)
    assert sdk.creates == []


def test_lost_create_response_reconciles_and_charges_failed_paid_start_without_rerun(tmp_path):
    instance, sdk = runner(tmp_path)
    sdk.lose = True
    outcome = instance.run(job(), tmp_path, lambda _: None, lambda: False)
    assert outcome.status == "failed" and outcome.elapsed_ms == DEFAULT_RESOURCES["wall_time_seconds"] * 1000
    assert len(sdk.creates) == len(sdk.terminations) == 1 and sdk.commands == []
    instance.cleanup()
    assert len(sdk.creates) == 1


def test_unproven_vm_termination_blocks_more_work_and_can_recover_without_source_execution(tmp_path):
    instance, sdk = runner(tmp_path)
    sdk.uncertain = True
    with pytest.raises(CleanupUncertain):
        instance.run(job(), tmp_path, lambda _: None, lambda: False)
    assert instance.state()["sandbox_id"] == "sb-fixture"
    with pytest.raises(RuntimeFault, match="invalid_job"):
        instance.run(job(), tmp_path, lambda _: None, lambda: False)
    assert len(sdk.creates) == 1
    sdk.uncertain = False
    instance.cleanup()
    instance.verify_cleanup()
    assert len(sdk.creates) == len(sdk.terminations) == 1


def test_cancel_before_create_and_worker_restart_never_repeat_program(tmp_path, fixture):  # noqa: F811
    instance, sdk = runner(tmp_path, fixture.config["id"])
    with pytest.raises(RuntimeFault, match="cancelled"):
        instance.run(job(fixture.config["id"]), tmp_path, lambda _: None, lambda: True)
    assert sdk.creates == []
    fixture.enqueue()
    wire = StoreTransport(fixture)
    client = WorkerClient(fixture.config, transport=httpx.MockTransport(wire.handle))
    worker = ComputeWorker(instance.data_dir, fixture.config, client, instance)
    instance.verify_runtime_identity()
    claimed = fixture.state.claim(fixture.config["token"])
    write_state(worker.journal, {"phase": "executing", "job": claimed})
    worker.run(once=True)
    assert sdk.creates == []
    assert fixture.state.job(fixture.user, fixture.org, claimed["id"])["error_code"] == "worker_stopped"
    client.close()


def test_modal_outcome_uses_real_worker_delivery_protocol_without_a_second_sandbox(tmp_path, fixture):  # noqa: F811
    instance, sdk = runner(tmp_path, fixture.config["id"])
    value = fixture.enqueue()
    wire = StoreTransport(fixture)
    wire.lose = "/finish"
    client = WorkerClient(fixture.config, transport=httpx.MockTransport(wire.handle))
    worker = ComputeWorker(instance.data_dir, fixture.config, client, instance)
    with pytest.raises(Exception, match="server_unavailable"):
        worker.run(once=True)
    worker.run(once=True)
    assert fixture.state.job(fixture.user, fixture.org, value["id"])["status"] == "succeeded"
    assert len(sdk.creates) == 1
    client.close()


def test_startup_failure_log_and_private_receipt_survive_lost_finish_without_paid_retry(tmp_path, fixture):  # noqa: F811
    instance, sdk = runner(tmp_path, fixture.config["id"])
    sdk.lose = True
    value = fixture.enqueue()
    wire = StoreTransport(fixture)
    wire.lose = "/finish"
    client = WorkerClient(fixture.config, transport=httpx.MockTransport(wire.handle))
    worker = ComputeWorker(instance.data_dir, fixture.config, client, instance)
    try:
        with pytest.raises(Exception, match="server_unavailable"):
            worker.run(once=True)
        worker.run(once=True)
        result = fixture.state.job(fixture.user, fixture.org, value["id"])
        logs = [entry["event"]["text"] for entry in fixture.state.events(fixture.user, fixture.org, value["id"])
                if entry["event"]["type"] == "log"]
        assert result["status"] == "failed" and result["used_cpu_seconds"] == result["reserved_cpu_seconds"]
        assert logs == ["Compute failed during Modal VM creation (unexpected_error).\n"]
        assert len(sdk.creates) == 1 and len(sdk.terminations) == 1
        receipt = json.loads((instance.data_dir / "modal-last-attempt.json").read_bytes())
        assert receipt["job_id"] == value["id"] and receipt["cleanup_verified"] is True
    finally:
        client.close()


@pytest.mark.parametrize("changes", [dict(resources={**DEFAULT_RESOURCES, "wall_time_seconds": 86400}),
                                     dict(supervisor_image="docker.io/library/ubuntu:latest"),
                                     dict(token_secret="private\nunsafe"), dict(environment="../main")])
def test_invalid_profiles_fail_locally(changes):
    with pytest.raises(ValueError):
        modal_profile(profile(**changes))


def test_sdk_create_pins_runtime_caps_and_does_not_inject_any_credentials(monkeypatch):
    calls = []
    sdk = object.__new__(ModalSDK)
    sdk.profile, sdk.client = profile(), object()
    image = SimpleNamespace(apt_install=lambda *args: ("docker-supervisor", args))
    sdk.modal = SimpleNamespace(App=SimpleNamespace(lookup=lambda *a, **kw: SimpleNamespace(app_id="ap-fixture")),
        Image=SimpleNamespace(from_registry=lambda *a, **kw: image),
        Sandbox=SimpleNamespace(create=lambda *args, **kwargs: calls.append((args, kwargs))))
    name = sandbox_name("a" * 32, "b" * 32)
    assert len(name) <= 63
    sdk.create(name, DEFAULT_RESOURCES)
    args, options = calls[0]
    assert args[0] == "dockerd" and options["runtime"] == "vm"
    assert options["cpu"] == (0.5, 0.5) and options["memory"] == (1536, 1536)
    assert options["timeout"] == 720 and options["secrets"] == [] and options["include_oidc_identity_token"] is False
    assert options["tags"] == {"reasoning-name": name}
    assert "fixture_secret" not in json.dumps({key: value for key, value in options.items() if key not in {"client", "app"}})


def test_sdk_cleanup_refuses_foreign_ownership_tags():
    sdk = object.__new__(ModalSDK)
    sdk.profile, sdk.client = profile(), object()
    sandbox = SimpleNamespace(object_id="sb-fixture", get_tags=lambda: {"reasoning-name": "rn-somebody-else"})
    sdk.modal = SimpleNamespace(Sandbox=SimpleNamespace(from_id=lambda *a, **kw: sandbox))
    with pytest.raises(CleanupUncertain, match="ownership_mismatch"):
        sdk.lookup(sandbox_name("a" * 32, "b" * 32), "sb-fixture")


def test_sdk_identity_uses_public_app_id_and_named_app_creation_is_not_a_vm():
    sdk = object.__new__(ModalSDK)
    sdk.profile, sdk.client = profile(), object()
    calls = []

    def lookup(*args, **options):
        calls.append(options)
        return SimpleNamespace(app_id="ap-fixture")

    sdk.modal = SimpleNamespace(App=SimpleNamespace(lookup=lookup))
    assert sdk.identity() == "ap-fixture" and calls[0]["create_if_missing"] is True


def test_installed_optional_sdk_accepts_adapter_signatures_and_identity_lengths():
    import inspect

    modal = pytest.importorskip("modal")
    name = sandbox_name("a" * 32, "b" * 32)
    assert len(name) <= 64 and hasattr(modal.App, "app_id")
    inspect.signature(modal.Sandbox.create).bind("dockerd", name=name, runtime="vm", tags={"reasoning-name": name},
                                               cpu=(0.5, 0.5), memory=(1536, 1536), secrets=[], include_oidc_identity_token=False)
    inspect.signature(modal.Image.from_registry).bind(SUPERVISOR_IMAGE, add_python="3.12")
    # The SDK's synchronized classmethod retains cls in __signature__.
    inspect.signature(modal.Client.from_credentials).bind(modal.Client, "ak_fixture_identifier", "as_fixture_secret")


class AsyncMethod:
    def __init__(self, fn):
        self.aio = fn


class Stream:
    def __init__(self, parts):
        self.parts = parts

    def __aiter__(self):
        async def chunks():
            for body in self.parts:
                yield body
        return chunks()


def process_fixture(stdout, stderr, *, hanging=False):
    inputs, calls = [], []

    async def drain():
        pass

    async def wait():
        return 0

    async def execute(*args, **options):
        calls.append((args, options))
        if hanging:
            await asyncio.sleep(100)
        return SimpleNamespace(stdout=Stream(stdout), stderr=Stream(stderr), wait=AsyncMethod(wait),
                stdin=SimpleNamespace(write=inputs.append, write_eof=lambda: inputs.append(None), drain=AsyncMethod(drain)))
    return SimpleNamespace(exec=AsyncMethod(execute)), inputs, calls


def test_sdk_streams_binary_stdin_and_separate_bounded_outputs_without_shell_interpolation():
    sandbox, inputs, calls = process_fixture([b"safe", b" output"], [b"safe error"])
    sdk = object.__new__(ModalSDK)
    errors = []
    code, output = sdk.pump(sandbox, ["docker", "exec", "--interactive", "fixture"],
                            input_bytes=b"untrusted $(source)", stderr_sink=errors.append)
    assert code == 0 and output == b"safe output" and errors == [b"safe error"]
    assert inputs == [b"untrusted $(source)", None]
    assert "source" not in str(calls[0][0]) and calls[0][1]["text"] is False


@pytest.mark.parametrize("kind", ["stdout", "stderr", "cancel", "timeout"])
def test_sdk_bounds_output_and_reacts_while_transport_is_blocked(kind):
    sandbox, _, _ = process_fixture([b"x" * 100], [b"x" * 100], hanging=kind in {"cancel", "timeout"})
    sdk = object.__new__(ModalSDK)
    options = {"maximum": 8, "timeout": 0.01}
    if kind == "stderr":
        options.update(maximum=1000, stderr_sink=lambda _: None)
        sandbox, _, _ = process_fixture([], [b"x" * 1001])
    if kind == "cancel":
        options["stopped"] = lambda: True
    with pytest.raises(RuntimeFault, match="cancelled|wall_time_limit|output_limit"):
        sdk.pump(sandbox, ["docker", "fixture"], **options)


def test_remote_docker_has_no_local_docker_or_inherited_provider_environment(tmp_path):
    commands = []
    sdk = SimpleNamespace(pump=lambda sandbox, args, **options: (commands.append((args, options)) or (0, b"")))
    instance = RemoteDockerRunner(tmp_path, "a" * 32, sdk, SandboxFixture())
    instance._command(["info"])
    assert commands[1][0][:3] == ["docker", "--config", "/tmp/reasoning-docker-client"]
    assert set(commands[1][1]["environment"]) == {"PATH", "LANG"}


def test_cli_requires_profile_and_catalog_before_sdk_or_claim(tmp_path):
    private_file(tmp_path / "enrollment.json", json.dumps({"server_url": "https://chat.example.org", "id": "a" * 32,
                 "organization_id": "b" * 32, "token": "rnw_" + "x" * 43, "image_ids": ["fixture"],
                 "resources": DEFAULT_RESOURCES}).encode())
    tmp_path.chmod(0o700)
    with pytest.raises(SystemExit) as result:
        main(["--data-dir", str(tmp_path), "run", "--backend", "modal"])
    assert result.value.code == 2 and not (tmp_path / "job.json").exists()
