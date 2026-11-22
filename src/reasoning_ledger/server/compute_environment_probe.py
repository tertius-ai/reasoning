"""Live check that a Modal Volume carries a sealed Lean environment into a job read-only.

The probe uses a relay's own Modal app and credentials and starts two small VMs. The first writes a test environment
into a version 2 Volume that Modal deletes when the probe ends. The second runs an ordinary attempt through ModalRunner
with that environment: the VM mounts only the environment's directory, read-only, and Docker binds it read-only into
the job's locked-down container at /opt/lean-env. The job program, running as the program user, reads, hashes and
executes files there and tries every kind of write. Nothing reaches the server, and no organization's data is used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import tempfile
import time
from pathlib import Path

from .compute_catalog import DEFAULT_RESOURCES, fits, resources
from .compute_docker import LEAN_ENVIRONMENT_PATH, PROGRAM_USER, CleanupUncertain, RuntimeFault
from .compute_modal import EnvironmentVolume, ModalRunner, ModalSDK, RemoteDockerRunner, modal_profile, mount_facts, sandbox_name
from .compute_store import ComputeStore

VOLUME_MOUNT = "/mnt/lean-env-volume"
SUB_PATH = "/probe-environment"
SIBLING = "probe-sibling"
DATA_BYTES = 256 * 1024**2
ENTRIES = ["bin", "data.bin", "writable"]
WRITES = ("create", "write", "truncate", "mkdir", "chmod", "utime", "symlink", "hardlink", "rename", "unlink")
# The writer VM runs only this fixed script. The world-writable directory and file are there so that only a read-only
# mount, not file permissions, can refuse the job's writes.
WRITER = f"""set -eu
umask 022
root={VOLUME_MOUNT}
environment="$root{SUB_PATH}"
mkdir -p "$environment/bin" "$environment/writable" "$root/{SIBLING}"
head -c {DATA_BYTES} /dev/urandom > "$environment/data.bin"
printf '#!/bin/sh\\necho environment-exec-ok\\n' > "$environment/bin/probe-exec"
chmod 0755 "$environment/bin/probe-exec"
printf 'writable\\n' > "$environment/writable/file"
chmod 0777 "$environment/writable"
chmod 0666 "$environment/writable/file"
printf 'sibling\\n' > "$root/{SIBLING}/marker"
sha256sum "$environment/data.bin" | cut -c1-64
sync "$root"
"""
# The job program: fixed text, run as the program user like any submitted program.
PROGRAM = '''import errno
import hashlib
import json
import os
import stat
import subprocess
import time

ROOT = "@ROOT@"
TARGET = ROOT + "/writable"
result = {"uid": os.getuid(), "mount": None}
with open("/proc/self/mountinfo") as stream:
    for line in stream:
        fields = line.split()
        if len(fields) > 6 and fields[4] == ROOT:
            tail = fields[fields.index("-") + 1:]
            result["mount"] = {"options": fields[5], "filesystem": tail[0], "super_options": tail[-1]}
result["entries"] = sorted(os.listdir(ROOT))
result["sibling_visible"] = os.path.exists(ROOT + "/../@SIBLING@") or os.path.exists("/@SIBLING@")
modes = {}
for name in ("bin/probe-exec", "data.bin", "writable", "writable/file"):
    info = os.stat(ROOT + "/" + name)
    modes[name] = {"mode": oct(stat.S_IMODE(info.st_mode)), "uid": info.st_uid}
result["modes"] = modes
started = time.monotonic()
digest = hashlib.sha256()
with open(ROOT + "/data.bin", "rb") as stream:
    while True:
        block = stream.read(1 << 20)
        if not block:
            break
        digest.update(block)
result["read_seconds"] = round(time.monotonic() - started, 3)
result["sha256"] = digest.hexdigest()
try:
    result["exec"] = subprocess.run([ROOT + "/bin/probe-exec"], capture_output=True, timeout=30).stdout.decode().strip()
except OSError as exc:
    result["exec"] = errno.errorcode.get(exc.errno, "error")


def attempt(action):
    try:
        action()
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    return "allowed"


result["writes"] = {
    "create": attempt(lambda: os.close(os.open(TARGET + "/created", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))),
    "write": attempt(lambda: os.close(os.open(TARGET + "/file", os.O_WRONLY))),
    "truncate": attempt(lambda: os.truncate(TARGET + "/file", 0)),
    "mkdir": attempt(lambda: os.mkdir(TARGET + "/directory")),
    "chmod": attempt(lambda: os.chmod(TARGET + "/file", 0o600)),
    "utime": attempt(lambda: os.utime(TARGET + "/file", (0, 0))),
    "symlink": attempt(lambda: os.symlink("/etc/passwd", TARGET + "/link")),
    "hardlink": attempt(lambda: os.link(TARGET + "/file", TARGET + "/linked")),
    "rename": attempt(lambda: os.rename(TARGET + "/file", TARGET + "/renamed")),
    "unlink": attempt(lambda: os.unlink(TARGET + "/renamed" if os.path.exists(TARGET + "/renamed") else TARGET + "/file")),
}
print("PROBE-RESULT " + json.dumps(result, sort_keys=True))
'''.replace("@ROOT@", LEAN_ENVIRONMENT_PATH).replace("@SIBLING@", SIBLING)
RESULT = re.compile(rb"^PROBE-RESULT (\{.*\})$", re.MULTILINE)


def probe_image(images, profile):
    """The first catalog image that runs Python programs at the default job limits."""
    for key in sorted(images):
        image = images[key]
        if "python" in image.languages and fits(DEFAULT_RESOURCES, image.maximum_resources) \
                and fits(DEFAULT_RESOURCES, profile["resources"]):
            return image
    raise RuntimeFault("invalid_job")


def probe_job(worker_id, image):
    limits = resources(dict(DEFAULT_RESOURCES))
    return {"id": secrets.token_hex(16), "worker_id": worker_id, "attempt_id": secrets.token_hex(16),
            "image": image.record(), "program_digest": "sha256:" + hashlib.sha256(PROGRAM.encode()).hexdigest(),
            "parameters": {"image_id": image.id, "language": "python", "program": PROGRAM, "resources": limits}}


def failures(receipt):
    """Each way the receipt falls short of proving the read-only mount; empty when it is proved."""
    problems = []
    writer, reader, container = receipt["writer"], receipt["reader"], receipt["container"]
    if receipt.get("error"):
        problems.append(f"the probe stopped during {receipt['error']['stage']} ({receipt['error']['exception_type']})")
    if not writer or writer.get("exit_code") != 0 or not re.fullmatch(r"[a-f0-9]{64}", writer.get("sha256") or ""):
        problems.append("the writer VM did not write and commit the test environment")
    if writer and writer.get("cleanup_verified") is False:
        problems.append("the writer VM's end is unverified; it stops at its Modal lifetime")
    if reader and reader.get("cleanup_verified") is not True:
        problems.append("the job VM's end is unverified")
    if not reader or reader.get("status") != "succeeded":
        problems.append("the job did not succeed")
    if not reader or not (reader.get("environment_mount") or {}).get("read_only"):
        problems.append("the VM mount is not read-only")
    if not container:
        problems.append("the job printed no probe result")
        return problems
    mount = container.get("mount") or {}
    if container.get("uid") != int(PROGRAM_USER.split(":")[0]):
        problems.append("the program did not run as the program user")
    if "ro" not in str(mount.get("options", "")).split(","):
        problems.append("the container's bind mount is not read-only")
    if container.get("entries") != ENTRIES or container.get("sibling_visible") is not False:
        problems.append("the container sees more than the one environment")
    if writer and container.get("sha256") != writer.get("sha256"):
        problems.append("the container read different bytes than the writer wrote")
    if container.get("exec") != "environment-exec-ok":
        problems.append("the container cannot execute files from the environment")
    writes = container.get("writes") or {}
    if set(writes) != set(WRITES) or any(value != "EROFS" for value in writes.values()):
        problems.append("a write was not refused as a write to a read-only file system")
    return problems


def run_probe(data_dir, worker_id, profile, images, *, sdk=None, docker_factory=RemoteDockerRunner, clock=time.monotonic):
    """Start the two VMs and return the receipt; see the module docstring. Costs a few VM-minutes on Modal."""
    profile = modal_profile(profile)
    image = probe_image(images, profile)
    directory = Path(data_dir).absolute() / "environment-probe"
    ComputeStore._directory(directory)
    sdk = sdk or ModalSDK(profile)
    runner = ModalRunner(directory, worker_id, profile, {image.id: image}, sdk=sdk, docker_factory=docker_factory)
    if runner.state()["phase"] == "idle":
        # Only the probe uses this directory, and nothing of it is in flight: bind it afresh to the current profile.
        for name in ("modal-sandbox.json", "modal-runtime.json", "modal-last-attempt.json"):
            (directory / name).unlink(missing_ok=True)
    else:
        runner.cleanup()  # An earlier probe's VM whose end is uncertain is terminated, or the probe stops here.
    limits = resources(dict(DEFAULT_RESOURCES))
    receipt = {"format": 1, "image_id": image.id, "volume_version": 2, "data_bytes": DATA_BYTES,
               "writer": None, "reader": None, "container": None, "error": None}
    stage = "volume"
    try:
        with sdk.ephemeral_volume() as volume:
            stage, began = "writer", clock()
            receipt["writer"] = writer = {"sandbox_id": None, "mount": None, "exit_code": None, "sha256": None}
            sandbox = sdk.create(sandbox_name(worker_id, secrets.token_hex(16)), limits,
                                 {VOLUME_MOUNT: sdk.mounted(volume, read_only=False)})
            try:
                writer["sandbox_id"] = sandbox.object_id
                code, body = sdk.pump(sandbox, ["/bin/cat", "/proc/self/mountinfo"], maximum=1024**2)
                with contextlib.suppress(RuntimeFault):
                    writer["mount"] = None if code else mount_facts(body, VOLUME_MOUNT)
                code, body = sdk.pump(sandbox, ["/bin/sh", "-c", WRITER], timeout=180, maximum=4096)
                writer.update(exit_code=code, sha256=None if code else body.decode("ascii", "replace").strip()[:64])
            finally:
                # Termination also commits the Volume. An unverified end is reported; the VM stops at its Modal lifetime.
                try:
                    sdk.terminate(sandbox)
                    writer["cleanup_verified"] = True
                except Exception:
                    writer["cleanup_verified"] = False
                writer["seconds"] = round(clock() - began, 1)
            if code:
                receipt["failures"] = failures(receipt)
                return receipt
            stage, began, output = "reader", clock(), bytearray()
            with tempfile.TemporaryDirectory(prefix="probe-", dir=directory) as spool:
                outcome = runner.run(probe_job(worker_id, image), Path(spool), output.extend, lambda: False,
                                     lean_environment=EnvironmentVolume(volume, SUB_PATH))
            attempt = json.loads((directory / "modal-last-attempt.json").read_bytes())
            receipt["reader"] = {"status": outcome.status, "error_code": attempt["error_code"], "stage": attempt["stage"],
                                 "sandbox_id": attempt["sandbox_id"], "environment_mount": attempt["environment_mount"],
                                 "cleanup_verified": attempt["cleanup_verified"], "seconds": round(clock() - began, 1)}
            found = RESULT.findall(bytes(output))
            if found:
                container = json.loads(found[-1])
                seconds = container.get("read_seconds")
                if isinstance(seconds, (int, float)) and seconds > 0:
                    container["read_mib_per_second"] = round(DATA_BYTES / 1024**2 / seconds, 1)
                receipt["container"] = container
            elif output:
                receipt["output"] = bytes(output[-2048:]).decode("utf8", "replace")
            stage = "volume_cleanup"
    except CleanupUncertain:
        raise
    except Exception as exc:
        # As in the relay's own receipts: the stage and exception class, never provider bodies or messages.
        kind = type(exc).__name__
        receipt["error"] = {"stage": stage, "exception_type": kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", kind)
                            else "Exception", "code": exc.code if isinstance(exc, RuntimeFault) else None}
    receipt["failures"] = failures(receipt)
    return receipt
