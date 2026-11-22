"""The Lean environment mount probe, with a nonexecuting Modal SDK; no paid resources."""

import contextlib
import copy
import json
import subprocess
import sys

import pytest
from test_public_compute_worker import IMAGE

from reasoning_ledger.server.compute_catalog import DEFAULT_RESOURCES, ComputeImage
from reasoning_ledger.server.compute_docker import Outcome, RuntimeFault
from reasoning_ledger.server.compute_environment_probe import (
    DATA_BYTES,
    PROGRAM,
    SUB_PATH,
    VOLUME_MOUNT,
    WRITER,
    WRITES,
    failures,
    probe_image,
    run_probe,
)
from reasoning_ledger.server.compute_modal import ENVIRONMENT_MOUNT, SUPERVISOR_IMAGE
from reasoning_ledger.server.compute_worker import main
from reasoning_ledger.server.security import private_file

WORKER = "a" * 32
DIGEST = "5" * 64
PROFILE = {"format": 1, "app": "reasoning-compute", "environment": "main", "token_id": "ak_fixture_token",
           "token_secret": "as_fixture_secret", "supervisor_image": SUPERVISOR_IMAGE, "resources": dict(DEFAULT_RESOURCES)}
WRITER_MOUNTS = b"36 22 0:32 / /mnt/lean-env-volume rw,relatime - fuse modal rw\n"
PROVEN = {"uid": 10001, "mount": {"options": "ro,nosuid,nodev,relatime", "filesystem": "fuse", "super_options": "rw"},
          "entries": ["bin", "data.bin", "writable"], "sibling_visible": False, "sha256": DIGEST, "read_seconds": 2.0,
          "exec": "environment-exec-ok", "writes": dict.fromkeys(WRITES, "EROFS"), "modes": {}}


class Sandbox:
    def __init__(self, identifier):
        self.object_id = identifier


class SDK:
    def __init__(self, result=PROVEN, writer_code=0, vm_read_only=True, fail=None):
        self.result, self.writer_code, self.vm_read_only, self.fail = result, writer_code, vm_read_only, fail
        self.creates, self.terminated, self.volume_open = [], [], None

    def identity(self):
        return "ap-fixture"

    @contextlib.contextmanager
    def ephemeral_volume(self):
        if self.fail == "volume":
            raise OSError("private provider body")
        self.volume_open = True
        yield "ephemeral-volume"
        self.volume_open = False

    @staticmethod
    def mounted(volume, *, read_only, sub_path=None):
        return (volume, read_only, sub_path)

    def create(self, name, limits, volumes=None):
        assert self.volume_open
        if self.fail == "create":
            raise OSError("private provider body")
        self.creates.append((name, limits, volumes))
        return Sandbox(f"sb-{len(self.creates)}")

    def pump(self, sandbox, args, **options):
        if args == ["/bin/cat", "/proc/self/mountinfo"]:
            return 0, WRITER_MOUNTS
        assert args == ["/bin/sh", "-c", WRITER] and options["timeout"] == 180
        return self.writer_code, (DIGEST + "\n").encode()

    def lookup(self, name, identifier=None):
        return Sandbox(identifier)

    def terminate(self, sandbox):
        self.terminated.append(sandbox.object_id)


class Docker:
    """The job VM's Docker: reports the VM mount, then runs the probe program and prints the SDK's chosen result."""

    def __init__(self, directory, worker, sdk, sandbox):
        self.sdk = sdk

    def verify_runtime_identity(self):
        pass

    def mount(self, path):
        assert path == ENVIRONMENT_MOUNT
        options = "ro,relatime" if self.sdk.vm_read_only else "rw,relatime"
        return {"filesystem": "fuse", "mount_options": options, "super_options": "rw", "read_only": self.sdk.vm_read_only}

    def _command(self, args, **options):
        assert args[:2] == ["image", "pull"]

    def capacity(self):
        return {"memory_bytes": 1024**3, "cpus": 1}

    def preflight(self, image, limits):
        return "sha256:" + "f" * 64

    def run(self, job, spool, log, stopped, *, lean_environment=None):
        assert lean_environment == ENVIRONMENT_MOUNT and job["parameters"]["program"] == PROGRAM
        log(b"other output\n")
        if self.sdk.result is not None:
            log(b"PROBE-RESULT " + json.dumps(self.sdk.result).encode() + b"\n")
        return Outcome("succeeded", 1000, 0, None)


def probe(tmp_path, sdk):
    return run_probe(tmp_path, WORKER, PROFILE, {"fixture": ComputeImage.parse(IMAGE)}, sdk=sdk, docker_factory=Docker)


def test_a_read_only_environment_through_both_mounts_proves_the_question(tmp_path):
    sdk = SDK()
    receipt = probe(tmp_path, sdk)
    assert receipt["failures"] == [] and receipt["error"] is None
    (_, _, writer), (_, limits, reader) = sdk.creates
    # The writer sees the whole Volume, writable; the job's VM only the environment's directory, read-only.
    assert writer == {VOLUME_MOUNT: ("ephemeral-volume", False, None)}
    assert reader == {ENVIRONMENT_MOUNT: ("ephemeral-volume", True, SUB_PATH)}
    assert limits == DEFAULT_RESOURCES and sdk.terminated == ["sb-1", "sb-2"] and sdk.volume_open is False
    assert receipt["writer"]["mount"]["read_only"] is False and receipt["writer"]["sha256"] == DIGEST
    assert receipt["reader"]["environment_mount"]["read_only"] is True and receipt["reader"]["cleanup_verified"] is True
    assert receipt["container"]["read_mib_per_second"] == DATA_BYTES / 1024**2 / 2
    assert PROFILE["token_secret"] not in json.dumps(receipt)


@pytest.mark.parametrize("change,expected", [
    ({"writes": {**PROVEN["writes"], "unlink": "allowed"}}, "a write was not refused"),
    ({"writes": {**PROVEN["writes"], "write": "EACCES"}}, "a write was not refused"),
    ({"mount": {**PROVEN["mount"], "options": "rw,relatime"}}, "bind mount is not read-only"),
    ({"entries": [*PROVEN["entries"], "probe-sibling"]}, "more than the one environment"),
    ({"sibling_visible": True}, "more than the one environment"),
    ({"sha256": "6" * 64}, "different bytes"),
    ({"exec": "EACCES"}, "cannot execute"),
    ({"uid": 0}, "program user"),
])
def test_each_shortfall_of_the_container_is_reported(tmp_path, change, expected):
    receipt = probe(tmp_path, SDK(result={**copy.deepcopy(PROVEN), **change}))
    assert len(receipt["failures"]) == 1 and expected in receipt["failures"][0]


def test_a_writable_vm_mount_stops_before_the_program(tmp_path):
    receipt = probe(tmp_path, SDK(vm_read_only=False))
    assert receipt["reader"]["status"] == "failed" and receipt["reader"]["error_code"] == "environment_writable"
    assert receipt["reader"]["stage"] == "environment_mount" and receipt["container"] is None
    assert "the VM mount is not read-only" in receipt["failures"]
    assert receipt["output"].startswith("Compute failed during Lean environment mount check")


def test_a_failed_writer_skips_the_job_vm(tmp_path):
    sdk = SDK(writer_code=1)
    receipt = probe(tmp_path, sdk)
    assert len(sdk.creates) == 1 and sdk.terminated == ["sb-1"] and receipt["reader"] is None
    assert receipt["failures"][0] == "the writer VM did not write and commit the test environment"


@pytest.mark.parametrize("stage", ["volume", "create"])
def test_a_provider_failure_is_reported_by_stage_and_class_only(tmp_path, stage):
    receipt = probe(tmp_path, SDK(fail=stage))
    assert receipt["error"] == {"stage": "volume" if stage == "volume" else "writer", "exception_type": "OSError", "code": None}
    assert "private provider body" not in json.dumps(receipt)


def test_a_later_probe_rebinds_to_the_current_profile(tmp_path):
    larger = {**DEFAULT_RESOURCES, "memory_bytes": 2 * 1024**3}
    images = {"fixture": ComputeImage.parse({**IMAGE, "maximum_resources": larger})}
    assert run_probe(tmp_path, WORKER, PROFILE, images, sdk=SDK(), docker_factory=Docker)["failures"] == []
    changed = {**PROFILE, "resources": larger}
    assert run_probe(tmp_path, WORKER, changed, images, sdk=SDK(), docker_factory=Docker)["failures"] == []


def test_an_image_that_cannot_run_python_cannot_host_the_probe():
    lean = ComputeImage.parse({**IMAGE, "id": "lean", "languages": ["lean"]})
    assert probe_image({"fixture": ComputeImage.parse(IMAGE), "lean": lean}, PROFILE).id == "fixture"
    with pytest.raises(RuntimeFault, match="invalid_job"):
        probe_image({"lean": lean}, PROFILE)


def test_the_program_reports_a_writable_directory_as_writable(tmp_path):
    """The real job program, run here against an ordinary directory, finds every write allowed."""
    root = tmp_path / "environment"
    (root / "bin").mkdir(parents=True)
    (root / "writable").mkdir()
    (root / "data.bin").write_bytes(b"\x00" * 4096)
    (root / "bin/probe-exec").write_text("#!/bin/sh\necho environment-exec-ok\n")
    (root / "bin/probe-exec").chmod(0o755)
    (root / "writable/file").write_text("writable\n")
    (tmp_path / "mountinfo").write_text("")
    program = PROGRAM.replace("/opt/lean-env", str(root)).replace("/proc/self/mountinfo", str(tmp_path / "mountinfo"))
    output = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, check=True, timeout=60).stdout
    result = json.loads(output.decode().rsplit("PROBE-RESULT ", 1)[1])
    assert result["exec"] == "environment-exec-ok" and result["entries"] == ["bin", "data.bin", "writable"]
    assert set(result["writes"]) == set(WRITES) and set(result["writes"].values()) == {"allowed"}
    receipt = {"writer": {"exit_code": 0, "sha256": result["sha256"]}, "reader": {"status": "succeeded",
               "environment_mount": {"read_only": True}, "cleanup_verified": True}, "container": result}
    assert failures(receipt) == ["the program did not run as the program user", "the container's bind mount is not read-only",
                                 "a write was not refused as a write to a read-only file system"]


def test_the_cli_refuses_a_profile_or_catalog_that_differs_from_the_enrollment(tmp_path, monkeypatch):
    monkeypatch.setattr("reasoning_ledger.server.compute_environment_probe.run_probe",
                        lambda *args: pytest.fail("must not start a VM"))
    private_file(tmp_path / "enrollment.json", json.dumps({
        "server_url": "https://chat.example.org", "id": WORKER, "organization_id": "b" * 32, "token": "rnw_" + "x" * 43,
        "image_ids": ["fixture"], "resources": DEFAULT_RESOURCES}).encode())
    private_file(tmp_path / "profile.json", json.dumps({**PROFILE, "resources": {**DEFAULT_RESOURCES, "cpu": 2}}).encode())
    private_file(tmp_path / "catalog.json", json.dumps([IMAGE]).encode())
    tmp_path.chmod(0o700)
    with pytest.raises(SystemExit) as result:
        main(["--data-dir", str(tmp_path), "probe-environment-mount", "--modal-profile", str(tmp_path / "profile.json"),
              "--catalog", str(tmp_path / "catalog.json")])
    assert result.value.code == 2 and not (tmp_path / "environment-probe").exists()


def test_the_cli_prints_the_receipt(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("reasoning_ledger.server.compute_environment_probe.run_probe",
                        lambda *args: calls.append(args) or {"format": 1, "failures": []})
    private_file(tmp_path / "enrollment.json", json.dumps({
        "server_url": "https://chat.example.org", "id": WORKER, "organization_id": "b" * 32, "token": "rnw_" + "x" * 43,
        "image_ids": ["fixture"], "resources": DEFAULT_RESOURCES}).encode())
    private_file(tmp_path / "profile.json", json.dumps(PROFILE).encode())
    private_file(tmp_path / "catalog.json", json.dumps([IMAGE]).encode())
    tmp_path.chmod(0o700)
    assert main(["--data-dir", str(tmp_path), "probe-environment-mount", "--modal-profile", str(tmp_path / "profile.json"),
                 "--catalog", str(tmp_path / "catalog.json")]) == 0
    assert json.loads(capsys.readouterr().out) == {"format": 1, "failures": []}
    assert calls[0][1] == WORKER and set(calls[0][3]) == {"fixture"}
