"""Docker command/policy and lifecycle fixtures; not real algebra execution."""

import copy
import hashlib
import json
import os
import sys
import time

import pytest

from reasoning_ledger.server.compute_archive import ArchiveError
from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_docker import (
    COMMANDS,
    ENVIRONMENT,
    LABEL,
    PROGRAM_USER,
    SUPERVISOR_USER,
    CleanupUncertain,
    DockerRunner,
    RuntimeFault,
    container_name,
    create_arguments,
    execution_arguments,
    input_arguments,
    mounts,
    verify_container,
    verify_daemon,
    verify_evidence,
    verify_image,
)

WORKER, ATTEMPT = "a" * 32, "b" * 32
IMAGE_ID = "sha256:" + "e" * 64
LIMITS = DEFAULT_RESOURCES.copy()
CAPABILITIES = {
    "manifest_scope": "installed",
    "schema_version": "1.0.0-alpha.1",
    "source_revision": "c" * 40,
    "platform": {"os": "linux", "architecture": "amd64"},
    "resource_limits": {"maximums": LIMITS},
    "network": {"supported_modes": ["denied"]},
    "devices": [],
    "sbom": {"path": "/usr/share/research/sbom.json"},
    "tools": [{"entry_points": [command[1][0]]} for command in COMMANDS.values()],
}
CAP_BYTES = json.dumps(CAPABILITIES).encode()
SBOM_BYTES = b'{"spdxVersion":"SPDX-2.3"}'


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.fixture
def image():
    return ComputeImage.parse(
        {
            "id": "fixture",
            "name": "Nonexecuting protocol fixture",
            "reference": "registry.example/research@sha256:" + "d" * 64,
            "languages": list(COMMANDS),
            "source_revision": "c" * 40,
            "capabilities_digest": digest(CAP_BYTES),
            "sbom_digest": digest(SBOM_BYTES),
            "capabilities_path": "/etc/research/capabilities.json",
            "sbom_path": "/usr/share/research/sbom.json",
            "maximum_resources": LIMITS,
        }
    )


@pytest.fixture
def daemon():
    return {
        "OSType": "linux",
        "Architecture": "amd64",
        "CgroupVersion": "2",
        "MemoryLimit": True,
        "SwapLimit": True,
        "CpuCfsQuota": True,
        "PidsLimit": True,
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "NCPU": 2,
        "MemTotal": 2 * 1024**3,
    }


@pytest.fixture
def inspected_image(image):
    return {"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64", "RepoDigests": [image.reference], "Config": {"Volumes": None}}


@pytest.fixture
def container():
    return {
        "Image": IMAGE_ID,
        "Name": "/" + container_name(WORKER, ATTEMPT),
        "Config": {
            "User": SUPERVISOR_USER,
            "WorkingDir": "/",
            "Entrypoint": ["/bin/sleep"],
            "Cmd": ["210"],
            "Env": [f"{key}={value}" for key, value in ENVIRONMENT.items()],
            "Labels": {LABEL: WORKER, "org.reasoning.attempt": ATTEMPT},
            "Healthcheck": {"Test": ["NONE"]},
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "NetworkMode": "none",
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "NanoCpus": 10**9,
            "Memory": 1024**3,
            "MemorySwap": 1024**3,
            "PidsLimit": 128,
            "ShmSize": 1024**2,
            "Init": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges=true"],
            "RestartPolicy": {"Name": "no"},
            "LogConfig": {"Type": "none"},
            "Tmpfs": mounts(LIMITS),
        },
        "Mounts": [],
        "State": {"Running": True, "OOMKilled": False},
    }


@pytest.fixture
def job(image):
    program = "print('protocol fixture, not a computation')"
    return {
        "worker_id": WORKER,
        "attempt_id": ATTEMPT,
        "program_digest": digest(program.encode()),
        "image": image.record(),
        "parameters": {"image_id": image.id, "language": "python", "program": program, "resources": LIMITS},
    }


def test_creation_uses_digest_and_bounded_private_scratch_without_host_mounts(image):
    args = create_arguments(WORKER, ATTEMPT, image, LIMITS)
    assert args[-2:] == [image.reference, "210"]
    assert args[args.index("--pull") + 1] == "never"
    assert args[args.index("--user") + 1] == SUPERVISOR_USER
    assert not {"--volume", "--mount", "--privileged", "--publish"}.intersection(args)
    assert not any(value.startswith("HOME=") for value in args)
    scratch = [int(value.split("size=", 1)[1].split(",", 1)[0]) for value in mounts(LIMITS).values()]
    assert sum(scratch) + 1024**2 == LIMITS["disk_bytes"]
    assert all("noexec,nosuid,nodev" in value for value in mounts(LIMITS).values())


@pytest.mark.parametrize("language", COMMANDS)
def test_program_exec_has_separate_uid_and_kernel_guards_without_source_interpolation(language):
    args = execution_arguments(container_name(WORKER, ATTEMPT), language)
    assert args[args.index("--user") + 1] == PROGRAM_USER != SUPERVISOR_USER
    assert all(guard in args[-1] for guard in ("NoNewPrivs:", "CapEff:", "Seccomp:"))
    assert COMMANDS[language][0] in args[-1]
    assert "protocol fixture" not in args[-1]


@pytest.mark.parametrize(
    "key,value",
    [
        ("OSType", "windows"),
        ("Architecture", "arm64"),
        ("CgroupVersion", "1"),
        ("MemoryLimit", False),
        ("SwapLimit", False),
        ("CpuCfsQuota", False),
        ("PidsLimit", False),
        ("SecurityOptions", ["name=seccomp,profile=unconfined"]),
        ("NCPU", 0),
        ("MemTotal", 1024**3),
    ],
)
def test_daemon_capabilities_fail_closed(daemon, key, value):
    verify_daemon(daemon, LIMITS)
    daemon[key] = value
    with pytest.raises(RuntimeFault, match="sandbox_unavailable"):
        verify_daemon(daemon, LIMITS)


@pytest.mark.parametrize("key,value", [("MemoryLimit", False), ("CpuCfsQuota", False), ("PidsLimit", False), ("CgroupVersion", "1")])
def test_a_swapless_host_waives_only_swap_limit_support(daemon, key, value):
    daemon["SwapLimit"] = False
    with pytest.raises(RuntimeFault, match="sandbox_unavailable"):
        verify_daemon(daemon, LIMITS)
    verify_daemon(daemon, LIMITS, swapless=True)
    daemon[key] = value
    with pytest.raises(RuntimeFault, match="sandbox_unavailable"):
        verify_daemon(daemon, LIMITS, swapless=True)


@pytest.mark.parametrize(
    "key,value",
    [("Id", "mutable-tag"), ("Os", "windows"), ("Architecture", "arm64"), ("RepoDigests", []), ("Config", {"Volumes": {"/host": {}}})],
)
def test_actual_image_must_match_pinned_platform_and_no_anonymous_volumes(image, inspected_image, key, value):
    assert verify_image(inspected_image, image) == IMAGE_ID
    inspected_image[key] = value
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        verify_image(inspected_image, image)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ReadonlyRootfs", False),
        ("Privileged", True),
        ("NetworkMode", "host"),
        ("IpcMode", "host"),
        ("CgroupnsMode", "host"),
        ("NanoCpus", 0),
        ("Memory", 0),
        ("MemorySwap", -1),
        ("PidsLimit", -1),
        ("ShmSize", 100 * 1024**2),
        ("Init", False),
        ("CapDrop", []),
        ("CapAdd", ["SYS_ADMIN"]),
        ("SecurityOpt", ["no-new-privileges=false"]),
        ("SecurityOpt", ["no-new-privileges=true", "seccomp=unconfined"]),
        ("Binds", ["/:/host"]),
        ("Devices", [{}]),
        ("DeviceRequests", [{}]),
        ("DeviceCgroupRules", ["a"]),
        ("PortBindings", {"80": []}),
        ("Tmpfs", {}),
        ("RestartPolicy", {"Name": "always"}),
        ("LogConfig", {"Type": "json-file"}),
    ],
)
def test_inspected_sandbox_rejects_weakened_host_policy(container, key, value):
    verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS)
    container["HostConfig"][key] = value
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS)


def test_a_swapless_host_expects_dockers_memory_only_limit_and_nothing_weaker(container):
    # Without swap-limit support Docker records MemorySwap -1; that is accepted only on a host proven swapless.
    container["HostConfig"]["MemorySwap"] = -1
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS)
    verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS, swapless=True)
    container["HostConfig"]["Memory"] = 0
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS, swapless=True)


@pytest.mark.parametrize(
    "key,value",
    [
        ("User", "0"),
        ("WorkingDir", "/host"),
        ("Entrypoint", ["/bin/sh"]),
        ("Cmd", ["forever"]),
        ("Env", []),
        ("Labels", {LABEL: "other"}),
        ("Healthcheck", {"Test": ["CMD", "arbitrary"]}),
        ("Volumes", {"/data": {}}),
    ],
)
def test_inspected_identity_and_supervisor_are_verified(container, key, value):
    container["Config"][key] = value
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        verify_container(container, IMAGE_ID, WORKER, ATTEMPT, LIMITS)


def test_evidence_requires_exact_bytes_and_matching_installed_source(image):
    verify_evidence(CAP_BYTES, SBOM_BYTES, image, LIMITS, "python")
    with pytest.raises(RuntimeFault):
        verify_evidence(CAP_BYTES + b" ", SBOM_BYTES, image, LIMITS, "python")
    altered = json.dumps({**CAPABILITIES, "source_revision": "f" * 40}).encode()
    record = {**image.record(), "capabilities_digest": digest(altered)}
    with pytest.raises(RuntimeFault):
        verify_evidence(altered, SBOM_BYTES, ComputeImage.parse(record), LIMITS, "python")


@pytest.fixture
def pipe_runner():
    # Trusted subprocess diagnostics only: no submitted CAS program is run on host.
    runner = object.__new__(DockerRunner)
    runner.prefix, runner.environment = [sys.executable], {"PATH": os.environ["PATH"]}
    return runner


def test_pipe_capture_drains_both_streams_while_writing_bounded_input(pipe_runner):
    errors = []
    status, body = pipe_runner._pump(
        ["-c", "import sys; b=sys.stdin.buffer.read(); sys.stdout.buffer.write(b); sys.stderr.write('diagnostic')"],
        input_bytes=b"x" * 65536,
        maximum=65536,
        stderr_sink=errors.append,
    )
    assert status == 0 and body == b"x" * 65536 and b"".join(errors) == b"diagnostic"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_pipe_limits_terminate_owned_cli_process(pipe_runner, stream):
    with pytest.raises(RuntimeFault, match="output_limit"):
        pipe_runner._pump(["-c", f"import sys; sys.{stream}.write('x'*200000)"], maximum=10000, stderr_sink=lambda _: None)


@pytest.mark.parametrize("cancelled", [False, True])
def test_pipe_deadline_and_stop_are_bounded_without_sockets(pipe_runner, cancelled):
    began = time.monotonic()
    with pytest.raises(RuntimeFault, match="cancelled" if cancelled else "wall_time_limit"):
        pipe_runner._pump(["-c", "import time; time.sleep(5)"], timeout=0.2, stopped=lambda: cancelled)
    assert time.monotonic() - began < 2


class FixtureRunner(DockerRunner):
    def __init__(self, container, mode=None):
        self.worker_id, self.container, self.mode, self.calls, self.removed = WORKER, copy.deepcopy(container), mode, [], False

    def verify_runtime_identity(self):
        pass

    def preflight(self, *_args):
        self.calls.append("preflight")
        return IMAGE_ID

    def _command(self, args, **_options):
        self.calls.append(args[:2])
        if args[:2] == ["container", "create"] and self.mode == "create_response_lost":
            raise RuntimeFault("docker_unavailable")
        return b""

    def _inspect(self, *_args):
        if self.mode == "unsafe":
            self.container["HostConfig"]["NetworkMode"] = "host"
        if self.mode == "supervisor_lost":
            self.container["State"]["Running"] = False
        return self.container

    def _evidence(self, *_args):
        self.calls.append("evidence")

    def _pump(self, args, **options):
        self.calls.append(args[:2])
        if self.mode in {"cancelled", "wall_time_limit", "log_limit"}:
            raise RuntimeFault(self.mode)
        options["sink"](b"protocol fixture output\n")
        return 0, b""

    def _archive(self, *_args):
        self.calls.append("harvest_while_alive")
        return []

    def remove(self, name):
        assert name == container_name(WORKER, ATTEMPT)
        self.calls.append("removed")
        if self.mode == "cleanup_uncertain":
            raise CleanupUncertain("cleanup_uncertain")
        self.removed = True


def test_lifecycle_verifies_collects_then_proves_cleanup_before_outcome(container, job, tmp_path):
    runner, output = FixtureRunner(container), []
    outcome = runner.run(job, tmp_path, output.append, lambda: False)
    assert outcome.status == "succeeded" and outcome.exit_code == 0 and outcome.error_code is None
    assert runner.calls == [
        "preflight",
        ["container", "create"],
        "evidence",
        ["container", "start"],
        ["container", "exec"],
        "harvest_while_alive",
        "removed",
    ]
    assert runner.removed and output == [b"protocol fixture output\n"]


def test_workflow_inputs_use_bounded_stdin_file_not_program_or_shell_source(container, job, tmp_path):
    from reasoning_ledger.server.workflow_inputs import input_bytes

    payload = {"answer": "α $(touch /tmp/nope)"}
    encoded = input_bytes(payload)
    job["parameters"].update(inputs=payload, inputs_digest=digest(encoded))
    runner = FixtureRunner(container)
    original = runner._command
    captured = []

    def command(args, **options):
        if args == input_arguments(container_name(WORKER, ATTEMPT)):
            captured.append((args, options))
        return original(args, **options)

    runner._command = command
    outcome = runner.run(job, tmp_path, lambda _: None, lambda: False)
    assert outcome.status == "succeeded"
    args, options = captured[0]
    assert options["input_bytes"] == encoded and options["timeout"] <= 10
    assert "/work/inputs.json" in args[-1]
    assert "touch /tmp/nope" not in " ".join(args)
    assert args[args.index("--user") + 1] == PROGRAM_USER


def test_invalid_input_digest_refuses_before_container_creation(container, job, tmp_path):
    job["parameters"].update(inputs={"answer": "four"}, inputs_digest="sha256:" + "0" * 64)
    runner = FixtureRunner(container)
    with pytest.raises(RuntimeFault, match="invalid_job"):
        runner.run(job, tmp_path, lambda _: None, lambda: False)
    assert runner.calls == []


@pytest.mark.parametrize(
    "mode,status,error",
    [
        ("cancelled", "cancelled", None),
        ("wall_time_limit", "failed", "wall_time_limit"),
        ("log_limit", "failed", "log_limit"),
        ("unsafe", "failed", "image_verification_failed"),
        ("create_response_lost", "failed", "execution_failed"),
        ("supervisor_lost", "failed", "execution_failed"),
    ],
)
def test_execution_failures_and_lost_creation_response_still_remove_container(container, job, tmp_path, mode, status, error):
    runner = FixtureRunner(container, mode)
    outcome = runner.run(job, tmp_path, lambda _: None, lambda: False)
    assert (outcome.status, outcome.error_code) == (status, error)
    assert runner.removed
    if mode == "unsafe":
        assert ["container", "start"] not in runner.calls


def test_uncertain_cleanup_never_returns_terminal_outcome(container, job, tmp_path):
    runner = FixtureRunner(container, "cleanup_uncertain")
    with pytest.raises(CleanupUncertain):
        runner.run(job, tmp_path, lambda _: None, lambda: False)
    assert not runner.removed


class RemovalFixture(DockerRunner):
    def verify_runtime_identity(self):
        # This fixture isolates immutable-ID removal, not daemon binding.
        pass

    def __init__(self, name, owner=WORKER, *, remaining=False, unreachable=False):
        self.worker_id, self.name, self.owner = WORKER, name, owner
        self.remaining, self.unreachable, self.removals, self.listings = remaining, unreachable, [], 0

    def _command(self, args, **_options):
        if self.unreachable:
            raise RuntimeFault("docker_unavailable")
        if args[:2] == ["container", "ls"]:
            self.listings += 1
            return ("f" * 64).encode() if self.listings == 1 or self.remaining else b""
        assert args[:3] == ["container", "rm", "--force"]
        self.removals.append(args[3])
        return b""

    def _inspect(self, _kind, identifier):
        assert identifier == "f" * 64
        return {"Name": "/" + self.name, "Config": {"Labels": {LABEL: self.owner}}}


def test_removal_uses_immutable_id_after_exact_name_and_owner_verification():
    name = container_name(WORKER, ATTEMPT)
    runner = RemovalFixture(name)
    runner.remove(name)
    assert runner.removals == ["f" * 64] and runner.listings == 2


@pytest.mark.parametrize("owner,remaining,unreachable", [("other", False, False), (WORKER, True, False), (WORKER, False, True)])
def test_removal_does_not_confuse_command_success_with_proven_absence(owner, remaining, unreachable):
    name = container_name(WORKER, ATTEMPT)
    runner = RemovalFixture(name, owner, remaining=remaining, unreachable=unreachable)
    with pytest.raises(CleanupUncertain):
        runner.remove(name)
    if owner != WORKER or unreachable:
        assert runner.removals == []


def test_substring_matching_never_deletes_another_named_container():
    name = container_name(WORKER, ATTEMPT)
    runner = RemovalFixture(name + "-other", remaining=True)
    with pytest.raises(CleanupUncertain):
        runner.remove(name)
    assert runner.removals == []


@pytest.mark.parametrize("cpu,artifact", [(1, 4 * 1024**2), (2, 1024**3)])
def test_scratch_includes_full_output_allowance_and_source_without_exceeding_total(cpu, artifact):
    limits = {**LIMITS, "cpu": cpu, "artifact_bytes": artifact, "disk_bytes": 2 * artifact}
    options = mounts(limits)
    amounts = {path: int(value.split("size=", 1)[1].split(",", 1)[0]) for path, value in options.items()}
    assert amounts["/work"] >= artifact + 64000 and amounts["/tmp"] > 0
    assert sum(amounts.values()) + 1024**2 == limits["disk_bytes"]


@pytest.mark.parametrize("failure", ["output_limit", "wall_time_limit"])
def test_archive_producer_bounds_are_not_confused_with_program_log_limits(tmp_path, monkeypatch, failure):
    runner = object.__new__(DockerRunner)
    runner.data_dir = tmp_path

    def fail(*_args, **_kwargs):
        raise RuntimeFault(failure)

    monkeypatch.setattr(runner, "_command", fail)
    with pytest.raises(ArchiveError, match="delivery bounds"):
        runner._archive("fixture", "/work/out/.", tmp_path, 1024, lambda: False)


@pytest.mark.parametrize("scratch", [True, False])
def test_archive_reads_tmpfs_inside_container_but_keeps_evidence_copy_separate(tmp_path, monkeypatch, scratch):
    import tarfile

    from test_public_compute_archive import archive

    runner = object.__new__(DockerRunner)
    runner.data_dir = tmp_path
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    calls = []

    def produce(args, **options):
        calls.append(args)
        assert options["timeout"] == 60 and options["maximum"] == 1024 + 1024**2
        options["sink"](archive([("out/result.txt" if scratch else "capabilities.json", b"result", tarfile.REGTYPE)]).read())

    monkeypatch.setattr(runner, "_command", produce)
    result = runner._archive("fixture", "/work/out/." if scratch else "/etc/reasoning/capabilities.json", spool, 1024, lambda: False)
    assert result[0].path.read_bytes() == b"result"
    if scratch:
        assert calls[0][:4] == ["container", "exec", "--user", PROGRAM_USER]
        assert "/bin/tar" in calls[0] and "TAR_OPTIONS=" in calls[0] and "cp" not in calls[0]
    else:
        assert calls[0] == ["container", "cp", "fixture:/etc/reasoning/capabilities.json", "-"]


def test_unsafe_evidence_archive_fails_image_verification_before_execution(tmp_path, monkeypatch, image):
    runner = object.__new__(DockerRunner)
    runner.data_dir = tmp_path

    def fail(*_args, **_kwargs):
        raise ArchiveError("unsafe evidence")

    monkeypatch.setattr(runner, "_archive", fail)
    with pytest.raises(RuntimeFault, match="image_verification_failed"):
        runner._evidence("fixture", image, LIMITS, "python", lambda: False)


def pinned_input(job, tmp_path, body=b"pinned \x00 input bytes", path="01-values.bin"):
    from reasoning_ledger.server.security import private_file

    local = tmp_path / "inputs" / "0"
    local.parent.mkdir(mode=0o700)
    private_file(local, body)
    job["parameters"]["attachments"] = [{
        "identity": "f" * 32, "sha256": hashlib.sha256(body).hexdigest(), "topic_id": "e" * 32, "name": "values.bin",
        "mime": "application/octet-stream", "size_bytes": len(body), "path": path,
    }]
    job["attachment_files"] = [str(local)]
    return body


def test_pinned_inputs_are_written_read_only_before_the_program_with_the_name_as_an_argument(container, job, tmp_path):
    from reasoning_ledger.server.compute_docker import attachment_arguments, seal_arguments

    body = pinned_input(job, tmp_path)
    runner = FixtureRunner(container)
    original, captured = runner._command, []

    def command(args, **options):
        captured.append((args, options))
        result = original(args, **options)
        if args[:2] == ["container", "exec"]:
            runner.calls.append(args[-1])
        return result

    runner._command = command
    outcome = runner.run(job, tmp_path / "spool", lambda _: None, lambda: False)
    assert outcome.status == "succeeded"
    name = container_name(WORKER, ATTEMPT)
    write = next(options for args, options in captured if args == attachment_arguments(name, "01-values.bin"))
    assert write["input_bytes"] == body and write["timeout"] <= LIMITS["wall_time_seconds"]
    args = attachment_arguments(name, "01-values.bin")
    assert args[-3:] == ['set -eu; umask 077; mkdir -p /work/in; cat > "/work/in/$1"', "sh", "01-values.bin"]
    assert args[args.index("--user") + 1] == PROGRAM_USER
    assert seal_arguments(name, ["01-values.bin"])[-3:] == ['set -eu; chmod 0444 -- "$@"; chmod 0555 /work/in', "sh", "01-values.bin"]
    start = runner.calls.index(["container", "start"])
    assert runner.calls[start + 1:start + 6] == [
        ["container", "exec"], "01-values.bin", ["container", "exec"], "01-values.bin", ["container", "exec"]
    ]
    assert runner.calls[start + 6] == "harvest_while_alive"
    with pytest.raises(RuntimeFault, match="invalid_job"):
        attachment_arguments(name, "../escape")


@pytest.mark.parametrize("change", ["checksum", "size", "missing"])
def test_inputs_that_differ_from_their_pins_refuse_before_container_creation(container, job, tmp_path, change):
    pinned_input(job, tmp_path)
    if change == "checksum":
        job["parameters"]["attachments"][0]["sha256"] = "0" * 64
    elif change == "size":
        job["parameters"]["attachments"][0]["size_bytes"] += 1
    else:
        job["attachment_files"] = []
    runner = FixtureRunner(container)
    with pytest.raises(RuntimeFault, match="invalid_job"):
        runner.run(job, tmp_path / "spool", lambda _: None, lambda: False)
    assert runner.calls == []
