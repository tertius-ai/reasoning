"""On-demand Modal VM sandboxes containing the ordinary fail-closed Docker runner.

The relay holds provider/enrollment credentials locally. Only Docker commands,
source stdin, and inert dependency inputs cross into the disposable VM.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from .compute_catalog import REFERENCE, ComputeImage, canonical, fits, resources
from .compute_docker import HEX_ID, CleanupUncertain, DockerRunner, Outcome, RuntimeFault, container_name
from .compute_store import ERROR_CODES, ComputeStore
from .security import private_file
from .workflow_inputs import input_bytes

# Official Ubuntu 24.04 linux/amd64 base manifest, not a mutable tag.
SUPERVISOR_IMAGE = "docker.io/library/ubuntu@sha256:496754492fb28b4d3049432f2ca787449331e23fb14f0dd3fffea86bf5a93eb4"
OVERHEAD_SECONDS = 600
OVERHEAD_MEMORY = 512 * 1024**2
DIAGNOSTIC_CODES = ERROR_CODES | {
    "cancelled", "docker_unavailable", "sandbox_unavailable", "modal_unavailable", "output_limit",
    "invalid_runtime_identity", "runtime_identity_changed", "unbound_runtime_state", "invalid_job",
}
STAGE_NAMES = {
    "sandbox_create": "Modal VM creation", "docker_setup": "Docker client setup",
    "docker_ready": "Docker readiness", "image_pull": "image download", "execution": "execution and result collection",
}


def sandbox_name(worker, attempt):
    container_name(worker, attempt)  # Validate full local identities first.
    return "rn-" + hashlib.sha256((worker + ":" + attempt).encode()).hexdigest()[:60]


def modal_profile(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "app", "environment", "token_id", "token_secret", "supervisor_image", "resources"}
        or type(value["format"]) is not int or value["format"] != 1
        or any(not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value[key])
               for key in ("app", "environment"))
        or any(not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,4096}", value[key])
               for key in ("token_id", "token_secret"))
        or not isinstance(value["supervisor_image"], str) or not REFERENCE.fullmatch(value["supervisor_image"])
    ):
        raise ValueError("invalid private Modal profile")
    limits = resources(value["resources"])
    if limits["wall_time_seconds"] > 86400 - OVERHEAD_SECONDS:
        raise ValueError("Modal wall time must leave room for bounded VM startup and cleanup")
    return {**value, "resources": limits}


class ModalSDK:
    """Small injectable SDK boundary; importing the default server needs no Modal."""

    def __init__(self, profile):
        try:
            import modal
        except ImportError:
            raise RuntimeFault("install_modal_extra") from None
        self.modal, self.profile, self.client = modal, profile, None

    def connect(self):
        if self.client is None:
            self.client = self.modal.Client.from_credentials(self.profile["token_id"], self.profile["token_secret"])
        return self.client

    def identity(self):
        client = self.connect()
        app = self.modal.App.lookup(self.profile["app"], environment_name=self.profile["environment"],
                                    create_if_missing=True, client=client)
        return app.app_id

    def create(self, name, limits):
        client = self.connect()
        app = self.modal.App.lookup(self.profile["app"], environment_name=self.profile["environment"], client=client)
        # Modal uses physical cores (two vCPUs/core). Docker uses logical CPUs.
        cores = limits["cpu"] / 2
        memory = math.ceil((limits["memory_bytes"] + OVERHEAD_MEMORY) / 1024**2)
        return self.modal.Sandbox.create(
            "dockerd", "--host=unix:///var/run/docker.sock", "--log-level=error", "--bridge=none", "--iptables=false",
            app=app, client=client, name=name,
            image=self.modal.Image.from_registry(self.profile["supervisor_image"], add_python="3.12").apt_install("docker.io"),
            runtime="vm", cpu=(cores, cores), memory=(memory, memory), timeout=limits["wall_time_seconds"] + OVERHEAD_SECONDS,
            secrets=[], include_oidc_identity_token=False, encrypted_ports=[], unencrypted_ports=[],
            tags={"reasoning-name": name},
        )

    def lookup(self, name, identifier=None):
        client = self.connect()
        if identifier is not None:
            sandbox = self.modal.Sandbox.from_id(identifier, client=client)
        else:
            try:
                sandbox = self.modal.Sandbox.from_name(self.profile["app"], name,
                                                       environment_name=self.profile["environment"], client=client)
            except self.modal.exception.NotFoundError:
                # An uncertain create followed by a missing *running* name does
                # not prove absence: it may be finished or still provisioning.
                raise CleanupUncertain("modal_creation_unreconciled") from None
        if sandbox.get_tags() != {"reasoning-name": name} or identifier is not None and sandbox.object_id != identifier:
            raise CleanupUncertain("modal_ownership_mismatch")
        return sandbox

    def terminate(self, sandbox):
        sandbox.terminate(wait=True)
        if sandbox.poll() is None:
            raise CleanupUncertain("cleanup_uncertain")

    def pump(self, sandbox, args, *, timeout=30, input_bytes=None, maximum=1024**2, sink=None, stderr_sink=None,
             stopped=lambda: False, environment=None):
        async def execute():
            output = bytearray()
            counts = [0, 0]

            async def read(stream, index, target, ceiling):
                async for body in stream:
                    if not isinstance(body, bytes):
                        raise RuntimeFault("docker_unavailable")
                    counts[index] += len(body)
                    if counts[index] > ceiling:
                        raise RuntimeFault("output_limit")
                    if target is not None:
                        target(body)
                    elif index == 0:
                        output.extend(body)

            async def command():
                process = await sandbox.exec.aio(*args, timeout=max(1, math.ceil(timeout)), text=False, env=environment)
                if input_bytes:
                    process.stdin.write(input_bytes)
                process.stdin.write_eof()
                await process.stdin.drain.aio()
                await asyncio.gather(read(process.stdout, 0, sink, maximum),
                                     read(process.stderr, 1, stderr_sink, maximum if stderr_sink else 65536))
                return await process.wait.aio(), bytes(output)

            task = asyncio.create_task(command())
            deadline = time.monotonic() + timeout
            try:
                while not task.done():
                    if stopped():
                        raise RuntimeFault("cancelled")
                    if time.monotonic() >= deadline:
                        raise RuntimeFault("wall_time_limit")
                    await asyncio.wait({task}, timeout=0.1)
                return task.result()
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            return asyncio.run(execute())
        except RuntimeFault:
            raise
        except Exception:
            # Never return provider error bodies or credentials to server logs.
            raise RuntimeFault("docker_unavailable") from None


class RemoteDockerRunner(DockerRunner):
    def __init__(self, directory, worker_id, sdk, sandbox):
        self.data_dir, self.worker_id, self.sdk, self.sandbox = directory, worker_id, sdk, sandbox
        self.socket_path = Path("/var/run/docker.sock")
        self.prefix = ["docker", "--config", "/tmp/reasoning-docker-client", "--host", "unix:///var/run/docker.sock"]
        self.environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        # No registry credentials, inherited provider environment, or job paths.
        code, _ = sdk.pump(sandbox, ["/bin/sh", "-c", 'umask 077; mkdir /tmp/reasoning-docker-client; '
                                 'printf \'{"auths":{}}\' > /tmp/reasoning-docker-client/config.json'], maximum=4096)
        if code:
            raise RuntimeFault("sandbox_unavailable")

    def _pump(self, args, **options):
        return self.sdk.pump(self.sandbox, [*self.prefix, *args], environment=self.environment, **options)


class ModalRunner:
    def __init__(self, data_dir, worker_id, profile, images, *, sdk=None, docker_factory=RemoteDockerRunner):
        from .compute_worker import read_private, write_state

        if not isinstance(worker_id, str) or not HEX_ID.fullmatch(worker_id):
            raise ValueError("invalid worker identity")
        self.data_dir, self.worker_id = Path(data_dir).absolute(), worker_id
        ComputeStore._directory(self.data_dir)
        self.profile, self.images = modal_profile(profile), dict(images)
        if not self.images or any(not fits(self.profile["resources"], image.maximum_resources) for image in self.images.values()):
            raise ValueError("Modal profile must fit every enrolled image")
        self.sdk, self.docker_factory = sdk or ModalSDK(self.profile), docker_factory
        self.read_private, self.write_state = read_private, write_state
        self.journal = self.data_dir / "modal-sandbox.json"

    def verify_runtime_identity(self):
        try:
            app = self.sdk.identity()
            if not isinstance(app, str) or not re.fullmatch(r"ap-[A-Za-z0-9]+", app):
                raise ValueError
            binding = canonical({"format": 1, "worker_id": self.worker_id, "app_id": app,
                                 "profile_sha256": hashlib.sha256(canonical(self.profile).encode()).hexdigest(),
                                 "images": [self.images[key].record() for key in sorted(self.images)]}).encode()
            path = self.data_dir / "modal-runtime.json"
            if path.exists() or path.is_symlink():
                original = self.read_private(path, 65536)
            else:
                if (self.data_dir / "job.json").exists() or self.journal.exists():
                    raise RuntimeFault("unbound_runtime_state")
                original = private_file(path, binding)
            if original != binding:
                raise RuntimeFault("runtime_identity_changed")
        except RuntimeFault:
            raise
        except Exception:
            raise RuntimeFault("modal_unavailable") from None

    def state(self):
        import json

        if not self.journal.exists() and not self.journal.is_symlink():
            return {"phase": "idle"}
        try:
            value = json.loads(self.read_private(self.journal, 4096))
            if value == {"phase": "idle"}:
                return value
            if (not isinstance(value, dict) or set(value) != {"phase", "name", "sandbox_id", "attempt_id"}
                    or value["phase"] != "creating"
                    or not isinstance(value["attempt_id"], str) or not HEX_ID.fullmatch(value["attempt_id"])
                    or value["name"] != sandbox_name(self.worker_id, value["attempt_id"])
                    or value["sandbox_id"] is not None and not re.fullmatch(r"sb-[A-Za-z0-9]+", value["sandbox_id"])):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError):
            raise CleanupUncertain("invalid_modal_state") from None

    def cleanup(self):
        value = self.state()
        if value["phase"] == "idle":
            return
        try:
            self.verify_runtime_identity()
            sandbox = self.sdk.lookup(value["name"], value["sandbox_id"])
            if value["sandbox_id"] is None:
                if not re.fullmatch(r"sb-[A-Za-z0-9]+", sandbox.object_id):
                    raise ValueError
                value["sandbox_id"] = sandbox.object_id
                self.write_state(self.journal, value)
            self.sdk.terminate(sandbox)
            self.write_state(self.journal, {"phase": "idle"})
        except Exception:
            raise CleanupUncertain("cleanup_uncertain") from None

    def verify_cleanup(self):
        if self.state()["phase"] != "idle":
            raise CleanupUncertain("cleanup_uncertain")

    def preflight(self, image, limits):
        if self.images.get(image.id) != image or not fits(resources(limits), self.profile["resources"]):
            raise RuntimeFault("image_verification_failed")
        # Configuration/identity check only: do not start a paid VM in a probe.
        self.verify_runtime_identity()

    def run(self, job, spool, log, stopped):
        image = ComputeImage.parse(job["image"])
        parameters = job["parameters"]
        limits = resources(parameters["resources"])
        program = parameters.get("program")
        if (parameters.get("image_id") != image.id or parameters.get("language") not in image.languages
                or not isinstance(program, str) or not program.strip() or len(program) > 16000 or "\x00" in program
                or job.get("program_digest") != "sha256:" + hashlib.sha256(program.encode()).hexdigest()
                or "inputs" in parameters and parameters.get("inputs_digest") != "sha256:" +
                hashlib.sha256(input_bytes(parameters["inputs"])).hexdigest()):
            raise RuntimeFault("invalid_job")
        self.preflight(image, limits)
        name = sandbox_name(self.worker_id, job["attempt_id"])
        if job["worker_id"] != self.worker_id or self.state()["phase"] != "idle":
            raise RuntimeFault("invalid_job")
        if stopped():
            raise RuntimeFault("cancelled")
        diagnostic = {"format": 1, "job_id": job["id"], "worker_id": self.worker_id,
                      "attempt_id": job["attempt_id"], "sandbox_id": None, "stage": "sandbox_create",
                      "execution_status": None, "error_code": None, "exception_type": None, "cleanup_verified": False}
        diagnostic_path = self.data_dir / "modal-last-attempt.json"

        def record(**changes):
            diagnostic.update(changes, updated_at=int(time.time()))
            self.write_state(diagnostic_path, diagnostic)

        def failed(code, status, exc):
            safe_code = code if isinstance(code, str) and code in DIAGNOSTIC_CODES else "unexpected_error"
            cause = exc.__cause__ or exc.__context__ or exc
            kind = type(cause).__name__
            record(error_code=safe_code, execution_status=status,
                   exception_type=kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", kind) else "Exception")
            # Startup has produced no program output. Never log provider bodies,
            # exception messages, environment, source, or credentials.
            if status == "failed" and diagnostic["stage"] != "execution":
                log(f"Compute failed during {STAGE_NAMES[diagnostic['stage']]} ({safe_code}).\n".encode())

        record()
        intent = {"phase": "creating", "name": name, "sandbox_id": None, "attempt_id": job["attempt_id"]}
        # A lost creation response is reconciled by name, never by creating again.
        self.write_state(self.journal, intent)
        try:
            sandbox = self.sdk.create(name, limits)
            if not isinstance(sandbox.object_id, str) or not re.fullmatch(r"sb-[A-Za-z0-9]+", sandbox.object_id):
                raise RuntimeFault("modal_unavailable")
            intent["sandbox_id"] = sandbox.object_id
            self.write_state(self.journal, intent)
            record(sandbox_id=sandbox.object_id, stage="docker_setup")
            with tempfile.TemporaryDirectory(prefix="modal-docker-", dir=self.data_dir) as temporary:
                runner = self.docker_factory(Path(temporary), self.worker_id, self.sdk, sandbox)
                record(stage="docker_ready")
                deadline = time.monotonic() + 180
                while True:
                    if stopped():
                        raise RuntimeFault("cancelled")
                    try:
                        runner.verify_runtime_identity()
                        break
                    except RuntimeFault:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.1)
                record(stage="image_pull")
                runner._command(["image", "pull", "--quiet", "--platform", "linux/amd64", image.reference],
                                timeout=180, maximum=65536, stopped=stopped)
                record(stage="execution")
                outcome = runner.run(job, spool, log, stopped)
                record(execution_status=outcome.status, error_code=outcome.error_code)
                return outcome if outcome.status == "succeeded" else replace(outcome, elapsed_ms=limits["wall_time_seconds"] * 1000)
        except CleanupUncertain:
            raise
        except RuntimeFault as exc:
            status = "cancelled" if exc.code == "cancelled" else "failed"
            failed(exc.code, status, exc)
            error = None if status == "cancelled" else (
                "image_verification_failed" if exc.code == "image_verification_failed" else "execution_failed")
            # Failed paid starts must not recycle their entire app reservation.
            return Outcome(status, limits["wall_time_seconds"] * 1000, None, error)
        except Exception as exc:
            failed("unexpected_error", "failed", exc)
            return Outcome("failed", limits["wall_time_seconds"] * 1000, None, "execution_failed")
        finally:
            # VM termination is the final physical boundary, even if nested
            # Docker removal or result transport fails. Do not rerun any source.
            self.cleanup()
            record(cleanup_verified=True)
