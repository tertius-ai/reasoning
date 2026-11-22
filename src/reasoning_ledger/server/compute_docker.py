"""Fail-closed Docker execution, bounded pipe capture, and private artifact spooling."""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .compute_archive import ArchiveError, CollectedArtifact, collect_artifacts
from .compute_catalog import DIGEST, ComputeImage, fits, resources
from .compute_store import ATTACHMENT_PATH, LOG_BYTES, ComputeStore
from .security import private_file
from .workflow_inputs import input_bytes

HEX_ID = re.compile(r"[a-f0-9]{32}\Z")
CONTAINER_ID = re.compile(r"[a-f0-9]{64}\Z")
LABEL = "org.reasoning.compute-worker"
SUPERVISOR_USER = "10002:10002"
PROGRAM_USER = "10001:10001"
COMMANDS = {
    "sage": ("program.sage", ["sage"]),
    "python": ("program.py", ["python3"]),
    "julia": ("program.jl", ["julia", "--startup-file=no", "--history-file=no"]),
    "gap": ("program.g", ["gap", "-q", "-b", "-T"]),
    "pari": ("program.gp", ["gp", "-q", "-f"]),
    "lean": ("program.lean", ["lean", "-DwarningAsError=true"]),
}
ENVIRONMENT = {
    "DOT_SAGE": "/tmp/sage",
    "JULIA_DEPOT_PATH": "/tmp/julia",
    "XDG_CACHE_HOME": "/tmp/xdg",
    "PYTHONDONTWRITEBYTECODE": "1",
    "REASONING_OUTPUT_DIR": "/work/out",
}
# A sealed Lean environment appears here, read-only, in a job that names one.
LEAN_ENVIRONMENT_PATH = "/opt/lean-env"
HOST_PATH = re.compile(r"(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}){1,8}\Z")


class RuntimeFault(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CleanupUncertain(RuntimeFault):
    """The host could not prove that its container was removed."""


@dataclass(frozen=True)
class Outcome:
    status: str
    elapsed_ms: int
    exit_code: int | None
    error_code: str | None
    artifacts: tuple[CollectedArtifact, ...] = ()


def mounts(limits: dict) -> dict[str, str]:
    # All writable scratch, including /dev/shm, fits the declared disk ceiling.
    available = limits["disk_bytes"] - 1024**2
    # Leave room for the submitted source and filesystem metadata even when
    # the requested artifact allowance is exactly half the total scratch size.
    work = available // 2 + 1024**2
    temporary = available - work
    return {
        "/work": f"rw,noexec,nosuid,nodev,size={work},mode=0700,uid=10001,gid=10001",
        "/tmp": f"rw,noexec,nosuid,nodev,size={temporary},mode=0700,uid=10001,gid=10001",
    }


def container_name(worker: str, attempt: str) -> str:
    if not HEX_ID.fullmatch(worker) or not HEX_ID.fullmatch(attempt):
        raise RuntimeFault("invalid_job")
    return f"reasoning-{worker}-{attempt}"


def environment_mount(source: str) -> dict:
    """The one bind mount a job may have: a sealed environment from the Docker host, read-only and unpropagated."""
    if not isinstance(source, str) or not HOST_PATH.fullmatch(source) or "/../" in source + "/" or "/./" in source + "/":
        raise RuntimeFault("invalid_job")
    return {"Type": "bind", "Source": source, "Target": LEAN_ENVIRONMENT_PATH, "ReadOnly": True,
            "BindOptions": {"Propagation": "rprivate"}}


def create_arguments(worker: str, attempt: str, image: ComputeImage, limits: dict, *,
                     lean_environment: str | None = None) -> list[str]:
    """`lean_environment` is a sealed environment's directory on the Docker host, bound read-only at LEAN_ENVIRONMENT_PATH."""
    limits = resources(limits)
    if not fits(limits, image.maximum_resources):
        raise RuntimeFault("image_verification_failed")
    name = container_name(worker, attempt)
    args = [
        "container",
        "create",
        "--name",
        name,
        "--label",
        f"{LABEL}={worker}",
        "--label",
        f"org.reasoning.attempt={attempt}",
        "--platform",
        "linux/amd64",
        "--pull",
        "never",
        "--read-only",
        "--network",
        "none",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        SUPERVISOR_USER,
        "--workdir",
        "/",
        "--init",
        "--no-healthcheck",
        "--restart",
        "no",
        "--log-driver",
        "none",
        "--cpus",
        str(limits["cpu"]),
        "--memory",
        str(limits["memory_bytes"]),
        "--memory-swap",
        str(limits["memory_bytes"]),
        "--pids-limit",
        str(limits["pids"]),
        "--shm-size",
        str(1024**2),
        "--ulimit",
        "nofile=1024:1024",
        "--entrypoint",
        "/bin/sleep",
    ]
    for path, options in mounts(limits).items():
        args.extend(["--tmpfs", f"{path}:{options}"])
    if lean_environment is not None:
        bind = environment_mount(lean_environment)
        args.extend(["--mount", f"type=bind,source={bind['Source']},target={bind['Target']},readonly,bind-propagation=rprivate"])
    for key, value in ENVIRONMENT.items():
        args.extend(["--env", f"{key}={value}"])
    return [*args, image.reference, str(limits["wall_time_seconds"] + 90)]


def execution_arguments(name: str, language: str) -> list[str]:
    if language not in COMMANDS:
        raise RuntimeFault("invalid_job")
    filename, command = COMMANDS[language]
    # No source text or user-controlled argument is interpolated into the shell.
    # Check inherited kernel restrictions in the job's execution process.
    launcher = (
        'set -eu; test "$(/usr/bin/id -u)" = 10001; '
        "test \"$(/usr/bin/awk '/NoNewPrivs:/ {print $2}' /proc/self/status)\" = 1; "
        "test \"$(/usr/bin/awk '/CapEff:/ {print $2}' /proc/self/status)\" = 0000000000000000; "
        "test \"$(/usr/bin/awk '/Seccomp:/ {print $2}' /proc/self/status)\" = 2; "
        "umask 077; mkdir -p /work/out /tmp/sage /tmp/julia /tmp/xdg; "
        f"cat > /work/{filename}; exec {' '.join(command)} /work/{filename}"
    )
    return ["container", "exec", "--interactive", "--user", PROGRAM_USER, "--workdir", "/work", name, "/bin/sh", "-c", launcher]


def input_arguments(name: str) -> list[str]:
    # Input bytes are stdin data, never part of an argv or shell expression.
    return [
        "container",
        "exec",
        "--interactive",
        "--user",
        PROGRAM_USER,
        "--workdir",
        "/work",
        name,
        "/bin/sh",
        "-c",
        "set -eu; umask 077; mkdir -p /work; cat > /work/inputs.json",
    ]


def attachment_arguments(name: str, path: str) -> list[str]:
    # File bytes are stdin data; the sanitized sandbox name is a positional argument, never shell source.
    if not ATTACHMENT_PATH.fullmatch(path):
        raise RuntimeFault("invalid_job")
    return [
        "container",
        "exec",
        "--interactive",
        "--user",
        PROGRAM_USER,
        "--workdir",
        "/work",
        name,
        "/bin/sh",
        "-c",
        'set -eu; umask 077; mkdir -p /work/in; cat > "/work/in/$1"',
        "sh",
        path,
    ]


def seal_arguments(name: str, paths: list[str]) -> list[str]:
    # The program user owns the files it was given; they and their directory become read-only.
    if not paths or any(not ATTACHMENT_PATH.fullmatch(path) for path in paths):
        raise RuntimeFault("invalid_job")
    return [
        "container",
        "exec",
        "--user",
        PROGRAM_USER,
        "--workdir",
        "/work/in",
        name,
        "/bin/sh",
        "-c",
        'set -eu; chmod 0444 -- "$@"; chmod 0555 /work/in',
        "sh",
        *paths,
    ]


def attachment_inputs(job: dict) -> list[tuple[str, bytes]]:
    """Each pinned input file's sandbox path and the worker's verified local bytes, in pinned order."""
    import hashlib

    pinned, files = job["parameters"].get("attachments", []), job.get("attachment_files", [])
    if not isinstance(pinned, list) or not isinstance(files, list) or len(pinned) != len(files):
        raise RuntimeFault("invalid_job")
    inputs = []
    for item, file in zip(pinned, files, strict=True):
        try:
            if not isinstance(item, dict) or not isinstance(file, str) or not ATTACHMENT_PATH.fullmatch(item["path"]):
                raise ValueError
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size != item["size_bytes"]:
                    raise ValueError
                body = stream.read(item["size_bytes"] + 1)
            if len(body) != item["size_bytes"] or hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise RuntimeFault("invalid_job") from exc
        inputs.append((item["path"], body))
    return inputs


def verify_daemon(info: dict, limits: dict, *, swapless: bool = False) -> None:
    """A daemon that enforces every job limit. `swapless` is only for a host proven to have no swap at all: there a
    container cannot swap, so the memory limit alone bounds it and the daemon's swap-limit support is not needed."""
    required = ("MemoryLimit", "CpuCfsQuota", "PidsLimit") if swapless else ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
    if (
        info.get("OSType") != "linux"
        or info.get("Architecture") not in {"amd64", "x86_64"}
        or info.get("CgroupVersion") != "2"
        or any(info.get(key) is not True for key in required)
        or not any(item.startswith("name=seccomp,") and "profile=builtin" in item for item in info.get("SecurityOptions", []))
        or info.get("NCPU", 0) < limits["cpu"]
        or info.get("MemTotal", 0) < limits["memory_bytes"] + 256 * 1024**2
    ):
        raise RuntimeFault("sandbox_unavailable")


def _reference(value: str) -> str:
    if value.startswith("docker.io/"):
        value = value[len("docker.io/") :]
    if "/" not in value.split("@", 1)[0]:
        value = "library/" + value
    return value


def verify_image(info: dict, image: ComputeImage) -> str:
    identifier = info.get("Id", "")
    if (
        not DIGEST.fullmatch(identifier)
        or info.get("Os") != "linux"
        or info.get("Architecture") != "amd64"
        or info.get("Config", {}).get("Volumes")
        or _reference(image.reference) not in {_reference(item) for item in info.get("RepoDigests", [])}
    ):
        raise RuntimeFault("image_verification_failed")
    return identifier


def verify_mounts(info: dict, lean_environment: str | None) -> bool:
    """No host path reaches the container, except the job's own sealed environment as one read-only, unpropagated bind."""
    host = info.get("HostConfig", {})
    expected = [] if lean_environment is None else [environment_mount(lean_environment)]
    if (host.get("Mounts") or []) != expected:
        return False
    actual = [mount for mount in info.get("Mounts") or [] if mount.get("Type") != "tmpfs"]
    return len(actual) == len(expected) and all(
        mount.get("Type") == "bind" and mount.get("Source") == bind["Source"] and mount.get("Destination") == bind["Target"]
        and mount.get("RW") is False and mount.get("Propagation") == "rprivate" and mount.get("Mode", "") in {"", "ro"}
        for mount, bind in zip(actual, expected, strict=True))


def verify_container(info: dict, identifier: str, worker: str, attempt: str, limits: dict, *, swapless: bool = False,
                     lean_environment: str | None = None) -> None:
    config, host = info.get("Config", {}), info.get("HostConfig", {})
    expected = {
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": "none",
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "NanoCpus": limits["cpu"] * 10**9,
        "Memory": limits["memory_bytes"],
        # Without swap-limit support Docker records -1 ("memory limited without swap"); valid only on a swapless host.
        "MemorySwap": -1 if swapless else limits["memory_bytes"],
        "PidsLimit": limits["pids"],
        "ShmSize": 1024**2,
        "Init": True,
    }
    security = set(host.get("SecurityOpt") or [])
    labels = config.get("Labels") or {}
    if (
        info.get("Image") != identifier
        or info.get("Name") != "/" + container_name(worker, attempt)
        or config.get("User") != SUPERVISOR_USER
        or config.get("WorkingDir") != "/"
        or config.get("Entrypoint") != ["/bin/sleep"]
        or config.get("Cmd") != [str(limits["wall_time_seconds"] + 90)]
        or config.get("Volumes")
        or not {f"{key}={value}" for key, value in ENVIRONMENT.items()} <= set(config.get("Env") or [])
        or labels.get(LABEL) != worker
        or labels.get("org.reasoning.attempt") != attempt
        or config.get("Healthcheck", {}).get("Test") != ["NONE"]
        or any(host.get(key) != value for key, value in expected.items())
        or set(host.get("CapDrop") or []) != {"ALL"}
        or not security.intersection({"no-new-privileges", "no-new-privileges=true", "no-new-privileges:true"})
        or not security <= {"no-new-privileges", "no-new-privileges=true", "no-new-privileges:true"}
        or any(
            host.get(key)
            for key in (
                "Binds",
                "VolumesFrom",
                "Devices",
                "DeviceRequests",
                "DeviceCgroupRules",
                "CapAdd",
                "PortBindings",
                "PidMode",
                "UTSMode",
            )
        )
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("LogConfig", {}).get("Type") != "none"
        or host.get("Tmpfs") != mounts(limits)
        or not verify_mounts(info, lean_environment)
    ):
        raise RuntimeFault("image_verification_failed")


def verify_evidence(capabilities: bytes, sbom: bytes, image: ComputeImage, limits: dict, language: str) -> None:
    import hashlib

    if (
        "sha256:" + hashlib.sha256(capabilities).hexdigest() != image.capabilities_digest
        or "sha256:" + hashlib.sha256(sbom).hexdigest() != image.sbom_digest
    ):
        raise RuntimeFault("image_verification_failed")
    try:
        body = json.loads(capabilities)
        component = json.loads(sbom)
        maximum = resources(body["resource_limits"]["maximums"])
        entry_points = {entry for tool in body["tools"] for entry in tool["entry_points"]}
        if (
            body["manifest_scope"] != "installed"
            or body["schema_version"] != "1.0.0-alpha.1"
            or body["source_revision"] != image.source_revision
            or body["platform"] != {"os": "linux", "architecture": "amd64"}
            or "denied" not in body["network"]["supported_modes"]
            or body["devices"]
            or body["sbom"]["path"] != image.sbom_path
            or not isinstance(component, dict)
            or not fits(image.maximum_resources, maximum)
            or not fits(limits, maximum)
            or COMMANDS[language][1][0] not in entry_points
        ):
            raise ValueError("unapproved installed capabilities")
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeFault("image_verification_failed") from exc


class DockerRunner:
    # Only a runner that has proven its Docker host has no swap may waive swap-limit support (see verify_daemon).
    swapless = False

    def __init__(self, data_dir: Path, worker_id: str, socket_path: Path = Path("/var/run/docker.sock")):
        if not HEX_ID.fullmatch(worker_id) or not socket_path.is_absolute() or any(c in str(socket_path) for c in "\r\n\x00"):
            raise ValueError("worker ID and local Docker socket are required")
        self.worker_id, self.data_dir = worker_id, data_dir.absolute()
        self.socket_path = socket_path.resolve()
        ComputeStore._directory(self.data_dir)
        config = self.data_dir / "docker-client"
        ComputeStore._directory(config)
        if private_file(config / "config.json", b'{"auths":{}}') != b'{"auths":{}}':
            raise ValueError("worker Docker client configuration must not contain credentials or proxies")
        executable = shutil.which("docker")
        if executable is None:
            raise RuntimeFault("sandbox_unavailable")
        self.prefix = [executable, "--config", str(config), "--host", "unix://" + str(self.socket_path)]
        self.environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "DOCKER_CLI_HINTS": "false"}

    def verify_runtime_identity(self) -> None:
        """Bind cleanup/execution to one local endpoint and reported daemon ID."""
        try:
            info = json.loads(self._command(["info", "--format", "{{json .}}"], timeout=10))
            identifier = info.get("ID") if isinstance(info, dict) else None
            if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256 or any(ord(char) < 32 for char in identifier):
                raise ValueError
            path = self.data_dir / "docker-runtime.json"
            if (
                not path.exists()
                and not path.is_symlink()
                and ((self.data_dir / "job.json").exists() or (self.data_dir / "job.json").is_symlink())
            ):
                raise RuntimeFault("unbound_runtime_state")
            body = json.dumps(
                {"format": 1, "worker_id": self.worker_id, "socket": str(self.socket_path), "daemon_id": identifier},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if path.exists() or path.is_symlink():
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    saved = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(saved.st_mode)
                        or saved.st_uid != os.getuid()
                        or saved.st_mode & 0o077
                        or saved.st_nlink != 1
                        or saved.st_size > 4096
                    ):
                        raise ValueError
                    existing = stream.read(4097)
            else:
                existing = private_file(path, body)
            if existing != body:
                raise RuntimeFault("runtime_identity_changed")
        except (ValueError, TypeError, OSError) as exc:
            raise RuntimeFault("invalid_runtime_identity") from exc

    def _pump(
        self,
        args: list[str],
        *,
        timeout: float = 30,
        input_bytes: bytes | None = None,
        maximum: int = 1024**2,
        sink: Callable[[bytes], None] | None = None,
        stderr_sink: Callable[[bytes], None] | None = None,
        stopped: Callable[[], bool] = lambda: False,
    ) -> tuple[int, bytes]:
        output, count, errors = bytearray(), 0, 0
        process = subprocess.Popen(
            [*self.prefix, *args],
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=self.environment,
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        pending = memoryview(input_bytes or b"")
        if process.stdin is not None:
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map() or process.poll() is None:
                if stopped():
                    raise RuntimeFault("cancelled")
                if time.monotonic() >= deadline:
                    raise RuntimeFault("wall_time_limit")
                for key, _events in selector.select(0.1):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            sent = os.write(stream.fileno(), pending[:8192])
                            pending = pending[sent:]
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        body = os.read(stream.fileno(), 32768)
                    except BlockingIOError:
                        continue
                    if not body:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if stream is process.stdout:
                        count += len(body)
                        if count > maximum:
                            raise RuntimeFault("output_limit")
                        if sink is None:
                            output.extend(body)
                        else:
                            sink(body)
                    else:
                        errors += len(body)
                        if errors > (maximum if stderr_sink is not None else 65536):
                            raise RuntimeFault("output_limit")
                        if stderr_sink is not None:
                            stderr_sink(body)
            return process.wait(), bytes(output)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def _command(self, args: list[str], **options) -> bytes:
        status, body = self._pump(args, **options)
        if status:
            raise RuntimeFault("docker_unavailable")
        return body

    def _inspect(self, kind: str, target: str) -> dict:
        try:
            body = json.loads(self._command([kind, "inspect", target]))
            if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
                raise ValueError("invalid Docker inspection")
            return body[0]
        except (ValueError, TypeError) as exc:
            raise RuntimeFault("docker_unavailable") from exc

    def preflight(self, image: ComputeImage, limits: dict) -> str:
        try:
            info = json.loads(self._command(["info", "--format", "{{json .}}"], timeout=10))
            verify_daemon(info, limits, swapless=self.swapless)
            return verify_image(self._inspect("image", image.reference), image)
        except (ValueError, TypeError) as exc:
            raise RuntimeFault("sandbox_unavailable") from exc

    def _archive(self, name: str, path: str, destination: Path, maximum: int, stopped: Callable[[], bool]) -> list[CollectedArtifact]:
        with tempfile.TemporaryFile(dir=self.data_dir) as wire:
            try:
                # Docker cp cannot reliably see tmpfs scratch. The fixed helper
                # reads it in the container mount namespace as the program UID.
                # Root-image evidence is copied before the container is started.
                arguments = (
                    [
                        "container",
                        "exec",
                        "--user",
                        PROGRAM_USER,
                        "--env",
                        "TAR_OPTIONS=",
                        name,
                        "/bin/tar",
                        "--format=posix",
                        "-C",
                        "/work",
                        "-cf",
                        "-",
                        "out",
                    ]
                    if path == "/work/out/."
                    else ["container", "cp", f"{name}:{path}", "-"]
                )
                self._command(arguments, timeout=60, maximum=maximum + 1024**2, sink=wire.write, stopped=stopped)
            except RuntimeFault as exc:
                if exc.code in {"output_limit", "wall_time_limit"}:
                    raise ArchiveError("container archive exceeded its delivery bounds") from exc
                raise
            wire.seek(0)
            return collect_artifacts(wire, destination, maximum)

    def _evidence(self, name: str, image: ComputeImage, limits: dict, language: str, stopped: Callable[[], bool]) -> None:
        bodies = []
        for path, maximum in ((image.capabilities_path, 1024**2), (image.sbom_path, 8 * 1024**2)):
            with tempfile.TemporaryDirectory(prefix="evidence-", dir=self.data_dir) as temporary:
                try:
                    copied = self._archive(name, path, Path(temporary), maximum, stopped)
                except ArchiveError as exc:
                    raise RuntimeFault("image_verification_failed") from exc
                if len(copied) != 1 or copied[0].name != Path(path).name:
                    raise RuntimeFault("image_verification_failed")
                bodies.append(copied[0].path.read_bytes())
        verify_evidence(bodies[0], bodies[1], image, limits, language)

    def remove(self, name: str) -> None:
        if not re.fullmatch(r"reasoning-[a-f0-9]{32}-[a-f0-9]{32}", name):
            raise CleanupUncertain("cleanup_uncertain")
        try:
            self.verify_runtime_identity()
            candidates = (
                self._command(["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name={name}"], timeout=10)
                .decode()
                .split()
            )
            for identifier in candidates:
                if not CONTAINER_ID.fullmatch(identifier):
                    raise RuntimeFault("cleanup_uncertain")
                info = self._inspect("container", identifier)
                if info.get("Name") != "/" + name:
                    continue
                if info.get("Config", {}).get("Labels", {}).get(LABEL) != self.worker_id:
                    raise RuntimeFault("cleanup_uncertain")
                self._command(["container", "rm", "--force", identifier], timeout=15)
            remaining = self._command(["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"name={name}"], timeout=10)
            if remaining.strip():
                raise RuntimeFault("cleanup_uncertain")
        except (RuntimeFault, ValueError, TypeError) as exc:
            raise CleanupUncertain("cleanup_uncertain") from exc

    def cleanup(self) -> None:
        self.verify_runtime_identity()
        identifiers = (
            self._command(["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}={self.worker_id}"], timeout=10)
            .decode()
            .split()
        )
        for identifier in identifiers:
            if not CONTAINER_ID.fullmatch(identifier):
                raise CleanupUncertain("cleanup_uncertain")
            info = self._inspect("container", identifier)
            name = info.get("Name", "").removeprefix("/")
            self.remove(name)

    def verify_cleanup(self) -> None:
        self.verify_runtime_identity()
        remaining = self._command(
            ["container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}={self.worker_id}"], timeout=10
        )
        if remaining.strip():
            raise CleanupUncertain("cleanup_uncertain")

    def run(self, job: dict, spool: Path, log: Callable[[bytes], None], stopped: Callable[[], bool], *,
            lean_environment: str | None = None) -> Outcome:
        """Run one claimed attempt. `lean_environment` is a sealed environment's directory on this Docker host, which the
        job sees read-only at LEAN_ENVIRONMENT_PATH."""
        self.verify_runtime_identity()
        image = ComputeImage.parse(job["image"])
        parameters = job["parameters"]
        limits, language = resources(parameters["resources"]), parameters["language"]
        if language not in image.languages or parameters["image_id"] != image.id:
            raise RuntimeFault("invalid_job")
        program = parameters["program"]
        if not isinstance(program, str) or not 1 <= len(program) <= 16000 or "\x00" in program:
            raise RuntimeFault("invalid_job")
        import hashlib

        encoded_inputs = None
        if "inputs" in parameters:
            try:
                encoded_inputs = input_bytes(parameters["inputs"])
                if parameters.get("inputs_digest") != "sha256:" + hashlib.sha256(encoded_inputs).hexdigest():
                    raise ValueError
            except (ValueError, TypeError, RecursionError) as exc:
                raise RuntimeFault("invalid_job") from exc
        files = attachment_inputs(job)

        if (
            job.get("worker_id") != self.worker_id
            or job.get("program_digest") != "sha256:" + hashlib.sha256(program.encode("utf8")).hexdigest()
        ):
            raise RuntimeFault("invalid_job")
        name = container_name(self.worker_id, job["attempt_id"])
        identifier = self.preflight(image, limits)
        created, began, elapsed, exit_code, artifacts = False, None, 0, None, []
        status, error = "failed", "execution_failed"
        try:
            # Creation may succeed even if delivery of its response fails. The
            # deterministic name is checked and cleaned in every exit path.
            created = True
            self._command(create_arguments(self.worker_id, job["attempt_id"], image, limits,
                                           lean_environment=lean_environment),
                          stopped=stopped)
            info = self._inspect("container", name)
            verify_container(info, identifier, self.worker_id, job["attempt_id"], limits, swapless=self.swapless,
                             lean_environment=lean_environment)
            self._evidence(name, image, limits, language, stopped)
            self._command(["container", "start", name], stopped=stopped)
            began = time.monotonic()
            timeout = limits["wall_time_seconds"]

            def remaining():
                left = limits["wall_time_seconds"] - (time.monotonic() - began)
                if left <= 0:
                    raise RuntimeFault("wall_time_limit")
                return left

            if encoded_inputs is not None:
                self._command(input_arguments(name), input_bytes=encoded_inputs, maximum=4096, timeout=min(10, timeout), stopped=stopped)
                timeout = remaining()
            # Pinned files are written before the program starts, within its wall time.
            for path, body in files:
                self._command(attachment_arguments(name, path), input_bytes=body, maximum=4096, timeout=remaining(), stopped=stopped)
            if files:
                self._command(seal_arguments(name, [path for path, _ in files]), maximum=4096, timeout=min(10, remaining()),
                              stopped=stopped)
                timeout = remaining()
            count = 0

            def captured(body):
                nonlocal count
                count += len(body)
                if count > LOG_BYTES:
                    raise RuntimeFault("log_limit")
                log(body)

            exit_code, _ = self._pump(
                execution_arguments(name, language),
                input_bytes=program.encode("utf8"),
                timeout=timeout,
                maximum=LOG_BYTES,
                sink=captured,
                stderr_sink=captured,
                stopped=stopped,
            )
            elapsed = int((time.monotonic() - began) * 1000)
            status, error = ("succeeded", None) if exit_code == 0 else ("failed", "execution_failed")
            state = self._inspect("container", name)["State"]
            if state.get("OOMKilled"):
                status, error = "failed", "resource_limit"
            if state.get("Running"):
                artifacts = self._archive(name, "/work/out/.", spool, limits["artifact_bytes"], stopped)
            elif status == "succeeded":
                # Losing the supervisor also loses its tmpfs. A successful exec
                # exit alone cannot establish successful artifact collection.
                status, error = "failed", "execution_failed"
        except ArchiveError:
            status, error = "failed", "artifact_limit"
        except RuntimeFault as exc:
            status, error = ("cancelled", None) if exc.code == "cancelled" else ("failed", exc.code)
            if error in {"docker_unavailable", "sandbox_unavailable", "invalid_job"}:
                error = "execution_failed"
            if error == "output_limit":
                error = "log_limit"
        finally:
            if began is not None and not elapsed:
                elapsed = min(limits["wall_time_seconds"] * 1000, int((time.monotonic() - began) * 1000))
            if created:
                self.remove(name)
        return Outcome(status, elapsed, exit_code, error, tuple(artifacts))
