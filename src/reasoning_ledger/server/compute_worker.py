"""Docker or Modal relay worker with durable, never-rerun delivery recovery."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .compute_catalog import DIGEST, ComputeImage, canonical, fits, load_catalog, resources
from .compute_docker import HEX_ID, CleanupUncertain, DockerRunner, Outcome, RuntimeFault
from .compute_store import (
    ARTIFACT_NAME,
    ATTACHMENT_FIELDS,
    ATTACHMENT_PATH,
    CHUNK_BYTES,
    ERROR_CODES,
    HEX64,
    LOG_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    TERMINAL,
    ComputeStore,
    attachment_allowance,
)
from .security import private_file
from .workflow_inputs import input_bytes

JOURNAL_BYTES = 256 * 1024


class WorkerFault(Exception):
    """Fixed, secret-free diagnostics; remote bodies are never printed."""


class InputsStopped(Exception):
    """The attempt stopped while its input files were downloading."""


def read_private(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise WorkerFault("invalid_private_state")
        return stream.read(maximum + 1)


def inspect_private(path: Path, maximum: int) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise WorkerFault("invalid_private_state")
    finally:
        os.close(fd)


def write_state(path: Path, value: dict) -> None:
    body = canonical(value).encode()
    if len(body) > JOURNAL_BYTES:
        raise WorkerFault("invalid_private_state")
    if path.exists() or path.is_symlink():
        read_private(path, JOURNAL_BYTES)
    fd, temporary = tempfile.mkstemp(prefix=".worker-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enrollment(server_url: str, registration: dict) -> dict:
    try:
        parsed = urlsplit(server_url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            raise ValueError
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError
        worker, org, token = registration["id"], registration["organization_id"], registration["token"]
        if not HEX_ID.fullmatch(worker) or not HEX_ID.fullmatch(org) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError
        images = registration["image_ids"]
        if not isinstance(images, list) or not 1 <= len(images) <= 32 or len(set(images)) != len(images):
            raise ValueError
        if any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item) for item in images):
            raise ValueError
        return {
            "server_url": server_url.rstrip("/"),
            "id": worker,
            "organization_id": org,
            "token": token,
            "image_ids": images,
            "resources": resources(registration["resources"]),
        }
    except (KeyError, ValueError, TypeError) as exc:
        raise WorkerFault("invalid_worker_enrollment") from exc


class WorkerClient:
    def __init__(self, config: dict, *, transport: httpx.BaseTransport | None = None):
        self.client = httpx.Client(
            base_url=config["server_url"],
            headers={"Authorization": "Bearer " + config["token"]},
            timeout=3,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/api/worker/") or len(canonical(body).encode()) > 65536:
            raise WorkerFault("invalid_worker_request")
        try:
            options = {} if body is None else {"json": body}
            with self.client.stream(method, path, **options) as response:
                if response.status_code == 401:
                    raise WorkerFault("worker_unauthorized")
                if response.status_code in {403, 404, 409, 422}:
                    raise WorkerFault("attempt_rejected")
                if not 200 <= response.status_code < 300:
                    raise WorkerFault("server_unavailable")
                if response.status_code == 204:
                    return None
                result = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    result.extend(chunk)
                    if len(result) > JOURNAL_BYTES:
                        raise WorkerFault("invalid_server_response")
                return json.loads(result)
        except httpx.HTTPError as exc:
            raise WorkerFault("server_unavailable") from exc
        except (ValueError, UnicodeError) as exc:
            raise WorkerFault("invalid_server_response") from exc


def verify_identity(config: dict, client: WorkerClient) -> dict:
    """Read and compare enrollment without claiming or updating worker activity."""
    identity = client.request("GET", "/api/worker/identity")
    try:
        if not isinstance(identity, dict):
            raise ValueError
        resources(identity.get("resources"))
    except (ValueError, TypeError) as exc:
        raise WorkerFault("enrollment_mismatch") from exc
    if (
        not isinstance(identity, dict)
        or identity.get("id") != config["id"]
        or identity.get("organization_id") != config["organization_id"]
        or identity.get("image_ids") != config["image_ids"]
        or identity.get("resources") != config["resources"]
        or identity.get("revoked_at") is not None
        or type(identity.get("draining")) is not int
        or identity["draining"] not in {0, 1}
    ):
        raise WorkerFault("enrollment_mismatch")
    return identity


def retirement_receipt(config: dict, value: dict) -> dict:
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"worker_id", "organization_id", "retired", "retirement_id", "retired_at"}
            or value["worker_id"] != config["id"]
            or value["organization_id"] != config["organization_id"]
            or type(value["retired"]) is not bool
        ):
            raise ValueError
        if value["retired"]:
            if not HEX_ID.fullmatch(value["retirement_id"]) or type(value["retired_at"]) is not int or not 0 < value["retired_at"] < 2**63:
                raise ValueError
        elif value["retirement_id"] is not None or value["retired_at"] is not None:
            raise ValueError
        return dict(value)
    except (KeyError, ValueError, TypeError) as exc:
        raise WorkerFault("invalid_retirement_receipt") from exc


def drain_status(config: dict, value: dict) -> dict:
    try:
        if (
            not isinstance(value, dict)
            or value.get("id") != config["id"]
            or value.get("organization_id") != config["organization_id"]
            or type(value.get("draining")) is not int
            or value["draining"] != 1
            or value.get("image_ids") != config["image_ids"]
            or resources(value.get("resources")) != config["resources"]
            or value.get("revoked_at") is not None
            or value.get("retired_at") is not None
        ):
            raise ValueError
        jobs = value["unsettled_jobs"]
        if not isinstance(jobs, list) or len(jobs) > 9:
            raise ValueError
        for job in jobs:
            if (
                not isinstance(job, dict)
                or set(job) != {"id", "status"}
                or not HEX_ID.fullmatch(job["id"])
                or job["status"] not in {"running", "outcome_unknown"}
            ):
                raise ValueError
        if type(value.get("quiescent")) is not bool or value["quiescent"] != (not jobs):
            raise ValueError
        return {
            "worker_id": config["id"],
            "organization_id": config["organization_id"],
            "draining": True,
            "unsettled_jobs": [{"id": job["id"], "status": job["status"]} for job in jobs],
            "quiescent": not jobs,
            "physical_cleanup_verified": False,
            "cloud_resources_deleted": False,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkerFault("invalid_worker_status") from exc


def verify_enrollment(config: dict, client: WorkerClient, runner: DockerRunner, images: dict[str, ComputeImage], *, backend: str) -> dict:
    if backend not in {"docker", "modal"}:
        raise WorkerFault("invalid_worker_backend")
    identity = verify_identity(config, client)
    if set(images) != set(config["image_ids"]):
        raise WorkerFault("enrollment_mismatch")
    for image in images.values():
        # Checks local daemon enforcement/capacity and immutable image metadata.
        # Capability/SBOM evidence and container policy are checked on each job,
        # not asserted from this metadata-only readiness probe.
        runner.preflight(image, config["resources"])
    return {
        "worker_id": config["id"],
        "organization_id": config["organization_id"],
        "enrollment_verified": True,
        "daemon_and_image_metadata_verified": backend == "docker",
        "backend": backend,
        "images": len(images),
        "draining": bool(identity["draining"]),
        "claims_job": False,
        "container_execution_verified": False,
    }


def lease_body(job: dict) -> dict:
    return {"attempt_id": job["attempt_id"], "lease": job["lease"]}


class Heartbeat:
    def __init__(self, client: WorkerClient, job: dict, stopping: threading.Event):
        self.client, self.job, self.stopping = client, job, stopping
        self.done, self.lost = threading.Event(), threading.Event()
        self.reason, self.thread = None, None
        self.valid_until = time.monotonic() + 20

    def tick(self) -> dict:
        try:
            body = self.client.request("POST", f"/api/worker/jobs/{self.job['id']}/heartbeat", lease_body(self.job))
            if (
                not isinstance(body, dict)
                or type(body.get("stop")) is not bool
                or not isinstance(body.get("status"), str)
                or body["status"] not in TERMINAL | {"running", "outcome_unknown"}
            ):
                raise WorkerFault("invalid_server_response")
            if body["stop"]:
                self.reason = "server_stop"
                self.lost.set()
            else:
                # A blocked HTTP thread cannot extend execution authority.
                self.valid_until = time.monotonic() + 20
            return body
        except WorkerFault:
            self.reason = "lease_lost"
            self.lost.set()
            raise

    def start(self):
        self.tick()
        self.thread = threading.Thread(target=self._run, name="reasoning-compute-heartbeat", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.done.wait(10):
            try:
                self.tick()
            except WorkerFault:
                return

    def stopped(self):
        expired = time.monotonic() >= self.valid_until
        if expired and self.reason is None:
            self.reason = "lease_lost"
        return self.stopping.is_set() or self.lost.is_set() or expired

    def close(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise WorkerFault("heartbeat_did_not_stop")


class ComputeWorker:
    def __init__(self, data_dir: Path, config: dict, client: WorkerClient, runner: DockerRunner):
        self.data_dir, self.config, self.client, self.runner = data_dir.absolute(), config, client, runner
        ComputeStore._directory(self.data_dir)
        self.attempts = self.data_dir / "attempts"
        ComputeStore._directory(self.attempts)
        self.journal = self.data_dir / "job.json"
        self.stopping = threading.Event()

    def retired(self) -> dict | None:
        path = self.data_dir / "retirement.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            raw = read_private(path, 65536)
            value = retirement_receipt(self.config, json.loads(raw))
            if not value["retired"] or canonical(value).encode() != raw:
                raise ValueError
            return value
        except (ValueError, TypeError) as exc:
            raise WorkerFault("invalid_private_state") from exc

    def retire(self) -> dict:
        """Seal this enrollment after cleanup/delivery; never execute a program."""
        with self.ownership():
            local = self.retired()
            self.runner.verify_runtime_identity()
            receipt = retirement_receipt(self.config, self.client.request("GET", "/api/worker/retirement"))
            if local is not None and local != receipt:
                raise WorkerFault("retirement_receipt_mismatch")
            if not receipt["retired"]:
                verify_identity(self.config, self.client)
                self.client.request("POST", "/api/worker/drain", {})
                if verify_identity(self.config, self.client)["draining"] != 1:
                    raise WorkerFault("worker_not_drained")
                # Recovery fails a never-started/crashed attempt and retains or
                # delivers collected output. It cannot execute a program here.
                self.recover()
                phase = self.state()["phase"]
                if phase in {"delivering", "finishing", "settled"}:
                    self.step()
                if self.state()["phase"] not in {"idle", "claiming"}:
                    raise WorkerFault("retirement_pending_delivery")
            elif self.state()["phase"] not in {"idle", "claiming", "settled"}:
                raise WorkerFault("retirement_pending_delivery")
            # Fresh physical verification even when acknowledging a lost seal
            # response. Cleanup touches only this worker's labeled containers.
            self.runner.cleanup()
            self.runner.verify_cleanup()
            if self.state()["phase"] == "settled":
                self._forget(self.state())
            if any(self.attempts.iterdir()):
                raise WorkerFault("retirement_retained_output")
            if not receipt["retired"]:
                receipt = retirement_receipt(
                    self.config, self.client.request("POST", "/api/worker/retirement", {"cleanup_confirmed": True})
                )
            if not receipt["retired"]:
                raise WorkerFault("invalid_retirement_receipt")
            write_state(self.data_dir / "retirement.json", receipt)
            self._save({"phase": "idle"})
            return {**receipt, "container_cleanup_verified": True, "local_state_quiescent": True, "cloud_resources_deleted": False}

    @contextmanager
    def ownership(self):
        lockfile = self.data_dir / "worker.lock"
        try:
            if lockfile.exists() or lockfile.is_symlink():
                read_private(lockfile, 0)
            private_file(lockfile, b"")
            fd = os.open(lockfile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (ValueError, OSError) as exc:
            raise WorkerFault("invalid_private_state") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size != 0
            ):
                raise WorkerFault("invalid_private_state")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerFault("another_worker_owns_state") from exc
            yield
        finally:
            os.close(fd)

    def state(self) -> dict:
        if not self.journal.exists() and not self.journal.is_symlink():
            return {"phase": "idle"}
        try:
            body = json.loads(read_private(self.journal, JOURNAL_BYTES))
            if not isinstance(body, dict) or body.get("phase") not in {
                "idle",
                "claiming",
                "claimed",
                "executing",
                "delivering",
                "finishing",
                "settled",
            }:
                raise ValueError
            if body["phase"] == "claiming" and not HEX_ID.fullmatch(body["claim_id"]):
                raise ValueError
            if body["phase"] not in {"idle", "claiming"}:
                self.validate_job(body["job"])
            if body["phase"] in {"delivering", "finishing"}:
                outcome = body["outcome"]
                if not isinstance(outcome, dict) or set(outcome) != {"status", "elapsed_ms", "exit_code", "error_code"}:
                    raise ValueError
                if (
                    outcome["status"] not in TERMINAL
                    or type(outcome["elapsed_ms"]) is not int
                    or not 0 <= outcome["elapsed_ms"] <= 86400000
                    or outcome["exit_code"] is not None
                    and (type(outcome["exit_code"]) is not int or not -255 <= outcome["exit_code"] <= 255)
                    or outcome["error_code"] is not None
                    and outcome["error_code"] not in ERROR_CODES
                    or outcome["status"] == "succeeded"
                    and (outcome["exit_code"] != 0 or outcome["error_code"] is not None)
                ):
                    raise ValueError
            if body["phase"] == "delivering":
                records, names, total = body["artifacts"], set(), 0
                if not isinstance(records, list) or len(records) > 64:
                    raise ValueError
                for index, record in enumerate(records):
                    if (
                        not isinstance(record, dict)
                        or set(record) != {"name", "size_bytes", "digest", "file"}
                        or record["file"] != str(index)
                        or not ARTIFACT_NAME.fullmatch(record["name"])
                        or record["name"] in names
                        or not DIGEST.fullmatch(record["digest"])
                        or type(record["size_bytes"]) is not int
                        or record["size_bytes"] < 0
                    ):
                        raise ValueError
                    names.add(record["name"])
                    total += record["size_bytes"]
                if total > body["job"]["parameters"]["resources"]["artifact_bytes"]:
                    raise ValueError
            return body
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkerFault("invalid_private_state") from exc

    def validate_job(self, job: dict):
        try:
            if not isinstance(job, dict) or any(
                not HEX_ID.fullmatch(job[key]) for key in ("id", "attempt_id", "worker_id", "organization_id")
            ):
                raise ValueError
            if job["worker_id"] != self.config["id"] or job["organization_id"] != self.config["organization_id"]:
                raise ValueError
            if not isinstance(job["lease"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", job["lease"]):
                raise ValueError
            image, params = ComputeImage.parse(job["image"]), job["parameters"]
            limits = resources(params["resources"])
            if (
                params["image_id"] not in self.config["image_ids"]
                or params["image_id"] != image.id
                or params["language"] not in image.languages
                or not fits(limits, self.config["resources"])
                or not fits(limits, image.maximum_resources)
            ):
                raise ValueError
            if "attachments" in params:
                pinned, total = params["attachments"], 0
                if not isinstance(pinned, list) or not 1 <= len(pinned) <= MAX_ATTACHMENTS:
                    raise ValueError
                for item in pinned:
                    if (
                        not isinstance(item, dict)
                        or set(item) != ATTACHMENT_FIELDS
                        or not HEX_ID.fullmatch(item["identity"])
                        or not HEX_ID.fullmatch(item["topic_id"])
                        or not HEX64.fullmatch(item["sha256"])
                        or not ATTACHMENT_PATH.fullmatch(item["path"])
                        or type(item["size_bytes"]) is not int
                        or not 1 <= item["size_bytes"] <= MAX_ATTACHMENT_BYTES
                    ):
                        raise ValueError
                    total += item["size_bytes"]
                if len({item["path"] for item in pinned}) != len(pinned) or len({item["identity"] for item in pinned}) != len(pinned):
                    raise ValueError
                if total > attachment_allowance(limits):
                    raise ValueError
            program = params["program"]
            if "inputs" in params and params.get("inputs_digest") != "sha256:" + hashlib.sha256(input_bytes(params["inputs"])).hexdigest():
                raise ValueError
            if (
                not isinstance(program, str)
                or not program.strip()
                or len(program) > 16000
                or "\x00" in program
                or job["program_digest"] != "sha256:" + hashlib.sha256(program.encode("utf8")).hexdigest()
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError) as exc:
            raise WorkerFault("invalid_server_job") from exc

    def _save(self, state: dict):
        write_state(self.journal, state)

    def _spool(self, job: dict) -> Path:
        # validate_job fences IDs before any path is constructed.
        directory = self.attempts / job["attempt_id"]
        ComputeStore._directory(directory)
        ComputeStore._directory(directory / "artifacts")
        return directory

    def _forget(self, state: dict):
        # Once server settlement is acknowledged, its durable artifacts are the
        # authoritative copy. Remove only our generated private attempt files.
        directory = self._spool(state["job"])
        artifacts = directory / "artifacts"
        paths = list(artifacts.iterdir())
        if any(not re.fullmatch(r"(?:[0-9]|[1-5][0-9]|6[0-3])", path.name) for path in paths):
            raise WorkerFault("invalid_private_state")
        for path in paths:
            inspect_private(path, 1024**3)
        for path in directory.iterdir():
            if path.name not in {"artifacts", "log.bin", "inputs"}:
                raise WorkerFault("invalid_private_state")
        self._clear_inputs(directory / "inputs")
        if (directory / "log.bin").exists() or (directory / "log.bin").is_symlink():
            read_private(directory / "log.bin", LOG_BYTES)
        for path in paths:
            path.unlink()
        artifacts.rmdir()
        if (directory / "log.bin").exists():
            (directory / "log.bin").unlink()
        directory.rmdir()
        self._save({"phase": "idle"})

    def recover(self):
        # Refuse further work if physical cleanup cannot be established.
        self.runner.cleanup()
        state = self.state()
        if state["phase"] in {"claimed", "executing"}:
            # A claimed job never started; an executing job might have consumed
            # its whole allocation. Neither is restarted after a worker crash.
            elapsed = 0 if state["phase"] == "claimed" else state["job"]["parameters"]["resources"]["wall_time_seconds"] * 1000
            self._save(
                {
                    "phase": "finishing",
                    "job": state["job"],
                    "outcome": {"status": "failed", "elapsed_ms": elapsed, "exit_code": None, "error_code": "worker_stopped"},
                }
            )

    def step(self) -> bool:
        state = self.state()
        if state["phase"] == "idle":
            self._save({"phase": "claiming", "claim_id": os.urandom(16).hex()})
            state = self.state()
        if state["phase"] == "claiming":
            job = self.client.request(
                "POST", "/api/worker/claim",
                {"claim_id": state["claim_id"], "supports_workflow_inputs": True, "supports_attachments": True},
            )
            if job is None:
                self._save({"phase": "idle"})
                return False
            self.validate_job(job)
            self._save({"phase": "claimed", "job": job})
            state = self.state()
        if state["phase"] == "claimed":
            self._execute(state)
            state = self.state()
        if state["phase"] == "delivering":
            self._deliver(state)
            state = self.state()
        if state["phase"] == "finishing":
            job = state["job"]
            result = self.client.request("POST", f"/api/worker/jobs/{job['id']}/finish", {**lease_body(job), **state["outcome"]})
            if not isinstance(result, dict) or result.get("id") != job["id"] or result.get("status") not in TERMINAL:
                raise WorkerFault("invalid_server_response")
            self._save({"phase": "settled", "job": job})
            state = self.state()
        if state["phase"] == "settled":
            self._forget(state)
        return True

    @staticmethod
    def _clear_inputs(directory: Path):
        # Only files this worker named by position; nothing program-controlled.
        if not directory.exists() and not directory.is_symlink():
            return
        ComputeStore._directory(directory, create=False)
        paths = list(directory.iterdir())
        if any(not re.fullmatch(r"[0-3]", path.name) for path in paths):
            raise WorkerFault("invalid_private_state")
        for path in paths:
            inspect_private(path, MAX_ATTACHMENT_BYTES)
            path.unlink()
        directory.rmdir()

    def _download(self, job: dict, spool: Path, stopped) -> list[Path] | None:
        """Fetch each pinned input file into private spool files; None when one differs from its pinned size or SHA-256."""
        pinned = job["parameters"].get("attachments", [])
        directory = spool / "inputs"
        self._clear_inputs(directory)
        if not pinned:
            return []
        ComputeStore._directory(directory)
        files = []
        for number, item in enumerate(pinned):
            target, hasher, size, index = directory / str(number), hashlib.sha256(), 0, 0
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                while index is not None:
                    if stopped():
                        raise InputsStopped
                    body = self.client.request(
                        "POST", f"/api/worker/jobs/{job['id']}/attachments/{item['identity']}/chunks/{index}", lease_body(job)
                    )
                    expected = min(CHUNK_BYTES, item["size_bytes"] - size)
                    try:
                        if (
                            not isinstance(body, dict)
                            or body.get("index") != index
                            or body.get("size_bytes") != item["size_bytes"]
                            or body.get("sha256") != item["sha256"]
                            or not isinstance(body.get("data_base64"), str)
                            or len(body["data_base64"]) > 4 * ((CHUNK_BYTES + 2) // 3)
                        ):
                            raise ValueError
                        data = base64.b64decode(body["data_base64"], validate=True)
                    except ValueError:
                        return None
                    size += len(data)
                    if len(data) != expected or body.get("next_index") != (index + 1 if size < item["size_bytes"] else None):
                        return None
                    stream.write(data)
                    hasher.update(data)
                    index = body["next_index"]
                stream.flush()
                os.fsync(stream.fileno())
            if size != item["size_bytes"] or hasher.hexdigest() != item["sha256"]:
                return None
            files.append(target)
        return files

    def _execute(self, state: dict):
        job = state["job"]
        guard = Heartbeat(self.client, job, self.stopping)
        spool = self._spool(job)
        guard.start()
        try:
            # Input files are fetched and verified before the attempt counts as executing: a crash here never ran code.
            outcome, files = None, []
            try:
                files = self._download(job, spool, guard.stopped)
            except InputsStopped:
                outcome = Outcome("cancelled", 0, None, None)
            except WorkerFault as exc:
                if str(exc) != "attempt_rejected":
                    raise
                files = None
            if files is None:
                outcome = Outcome("failed", 0, None, "input_verification_failed")
            self._save({"phase": "executing", "job": job})
            if outcome is None and guard.stopped():
                outcome = Outcome("cancelled", 0, None, None)
            if outcome is None:
                fd = os.open(spool / "log.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb", buffering=0) as log:

                    def capture(body):
                        if log.write(body) != len(body):
                            raise OSError("incomplete private log write")

                    try:
                        given = {**job, "attachment_files": [str(path) for path in files]} if files else job
                        outcome = self.runner.run(given, spool / "artifacts", capture, guard.stopped)
                    except CleanupUncertain:
                        raise
                    except RuntimeFault as exc:
                        error = "image_verification_failed" if exc.code == "image_verification_failed" else "execution_failed"
                        outcome = Outcome("failed", 0, None, error)
                    log.flush()
                    os.fsync(log.fileno())
            if outcome.status == "cancelled" and guard.reason != "server_stop":
                outcome = replace(outcome, status="failed", error_code="worker_stopped")
            records = [
                {"name": row.name, "size_bytes": row.size_bytes, "digest": row.digest, "file": row.path.name} for row in outcome.artifacts
            ]
            self._save(
                {
                    "phase": "delivering",
                    "job": job,
                    "artifacts": records,
                    "outcome": {
                        "status": outcome.status,
                        "elapsed_ms": outcome.elapsed_ms,
                        "exit_code": outcome.exit_code,
                        "error_code": outcome.error_code,
                    },
                }
            )
        finally:
            guard.close()

    def _deliver(self, state: dict):
        job, spool = state["job"], self._spool(state["job"])
        guard = Heartbeat(self.client, job, self.stopping)
        current = guard.tick()
        if current["status"] in TERMINAL:
            self._save({"phase": "settled", "job": job})
            self._forget(state)
            return
        # A lease can expire while the process is down. Delivery is allowed for
        # this same fenced attempt, but never authorizes another execution.
        guard.start()
        path, lease = f"/api/worker/jobs/{job['id']}", lease_body(job)
        try:
            if (spool / "log.bin").exists():
                raw = read_private(spool / "log.bin", LOG_BYTES)
                text = raw.decode("utf8", errors="replace").encode("utf8")[:LOG_BYTES].decode("utf8", errors="ignore")
                chunks, pending, size = [], [], 0
                for char in text:
                    amount = len(char.encode("utf8"))
                    if size + amount > 8192:
                        chunks.append("".join(pending))
                        pending, size = [], 0
                    pending.append(char)
                    size += amount
                if pending:
                    chunks.append("".join(pending))
                for index, chunk in enumerate(chunks):
                    self.client.request("POST", path + "/logs", {**lease, "chunk_index": index, "text": chunk})
            for record in state["artifacts"]:
                if not re.fullmatch(r"(?:[0-9]|[1-5][0-9]|6[0-3])", record["file"]) or not ARTIFACT_NAME.fullmatch(record["name"]):
                    raise WorkerFault("invalid_private_state")
                artifact = spool / "artifacts" / record["file"]
                # Validate the spool's bytes before registering/uploading them.
                # Chunk reads below are bounded; no program-controlled path is opened.
                fd = os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                        or info.st_size != record["size_bytes"]
                        or info.st_size > job["parameters"]["resources"]["artifact_bytes"]
                    ):
                        raise WorkerFault("invalid_private_state")
                    hasher = hashlib.sha256()
                    while body := stream.read(CHUNK_BYTES):
                        hasher.update(body)
                    if "sha256:" + hasher.hexdigest() != record["digest"]:
                        raise WorkerFault("invalid_private_state")
                    meta = self.client.request(
                        "POST", path + "/artifacts", {**lease, **{key: record[key] for key in ("name", "size_bytes", "digest")}}
                    )
                    if not isinstance(meta, dict) or not HEX_ID.fullmatch(meta.get("id", "")):
                        raise WorkerFault("invalid_server_response")
                    target = path + f"/artifacts/{meta['id']}"
                    stream.seek(0)
                    index = 0
                    while body := stream.read(CHUNK_BYTES):
                        if self.stopping.is_set():
                            raise WorkerFault("worker_stopped")
                        self.client.request(
                            "PUT", target + "/chunks", {**lease, "chunk_index": index, "content_base64": base64.b64encode(body).decode()}
                        )
                        index += 1
                    self.client.request("POST", target + "/commit", lease)
            self._save({"phase": "finishing", "job": job, "outcome": state["outcome"]})
        finally:
            guard.close()

    def run(self, *, once: bool = False):
        with self.ownership():
            if self.retired() is not None:
                raise WorkerFault("worker_retired")
            self.runner.verify_runtime_identity()
            self.recover()
            while not self.stopping.is_set():
                try:
                    worked = self.step()
                except WorkerFault as exc:
                    if str(exc) not in {"server_unavailable", "worker_stopped"} or once:
                        raise
                    print("reasoning-compute-worker: server unavailable; durable delivery retained", flush=True)
                    worked = False
                if once:
                    return
                if not worked:
                    self.stopping.wait(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reasoning-compute-worker")
    parser.add_argument("--data-dir", required=True, type=Path, help="private worker state on a separate Linux Docker host")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="import one-time worker enrollment without printing its token")
    configure.add_argument("--server-url", required=True)
    configure.add_argument("--registration", required=True, type=Path, help="private JSON worker-registration response")
    run = commands.add_parser("run", help="execute jobs and resume fenced result delivery")
    run.add_argument("--docker-socket", type=Path, default=Path("/var/run/docker.sock"))
    run.add_argument("--once", action="store_true", help="handle at most one claim or retained delivery, then exit")
    verify = commands.add_parser("verify-images", help="verify remote enrollment and local daemon/image metadata without claiming a job")
    verify.add_argument("--catalog", required=True, type=Path)
    verify.add_argument("--docker-socket", type=Path, default=Path("/var/run/docker.sock"))
    probe = commands.add_parser(
        "probe-environment-mount",
        help="start two small Modal VMs to check that a sealed Lean environment mounts read-only into a job (paid)")
    probe.add_argument("--modal-profile", required=True, type=Path)
    probe.add_argument("--catalog", required=True, type=Path)
    drain = commands.add_parser("drain", help="stop new claims without interrupting active work or stopping a service")
    retire = commands.add_parser("retire", help="irreversibly retire a stopped worker after cleanup and retained result delivery")
    for command in (drain, retire):
        command.add_argument("--confirm-worker", required=True, help="exact configured worker ID; acknowledges changing this enrollment")
    retire.add_argument("--docker-socket", type=Path, default=Path("/var/run/docker.sock"))
    for command in (run, verify, retire):
        command.add_argument("--backend", choices=("docker", "modal"), default="docker")
        command.add_argument("--modal-profile", type=Path, help="private Modal token/app/resource profile; never sent to jobs")
        if command is not verify:
            command.add_argument("--catalog", type=Path, help="approved immutable images, required with the Modal backend")
    args = parser.parse_args(argv)
    client = None
    try:
        ComputeStore._directory(args.data_dir)
        path = args.data_dir / "enrollment.json"
        if args.command == "configure":
            config = enrollment(args.server_url, json.loads(read_private(args.registration, 65536)))
            initial = canonical(config).encode()
            if private_file(path, initial) != initial:
                raise WorkerFault("worker_already_configured")
            print("Compute worker configured; enrollment token stored privately.")
            return 0
        raw = json.loads(read_private(path, 65536))
        config = enrollment(raw["server_url"], raw)
        if args.command in {"drain", "retire"} and args.confirm_worker != config["id"]:
            raise WorkerFault("confirm_worker_mismatch")
        if args.command == "probe-environment-mount":
            from .compute_environment_probe import run_probe

            # The relay's own profile and catalog; the probe never contacts the server or claims a job.
            profile = json.loads(read_private(args.modal_profile, 16384))
            read_private(args.catalog, 65536)
            images = load_catalog(args.catalog)
            if profile.get("resources") != config["resources"] or set(images) != set(config["image_ids"]):
                raise WorkerFault("enrollment_mismatch")
            print(json.dumps(run_probe(args.data_dir, config["id"], profile, images), sort_keys=True))
            return 0
        client = WorkerClient(config)
        if args.command == "drain":
            verify_identity(config, client)
            client.request("POST", "/api/worker/drain", {})
            status = client.request("GET", "/api/worker/status")
            if (
                not isinstance(status, dict)
                or status.get("id") != config["id"]
                or status.get("organization_id") != config["organization_id"]
            ):
                raise WorkerFault("enrollment_mismatch")
            print(json.dumps(drain_status(config, status), sort_keys=True))
            return 0
        if args.backend == "modal":
            from .compute_modal import ModalRunner

            if args.modal_profile is None or args.catalog is None:
                raise WorkerFault("modal_profile_and_catalog_required")
            profile = json.loads(read_private(args.modal_profile, 16384))
            read_private(args.catalog, 65536)
            images = load_catalog(args.catalog)
            if profile.get("resources") != config["resources"] or set(images) != set(config["image_ids"]):
                raise WorkerFault("enrollment_mismatch")
            runner = ModalRunner(args.data_dir, config["id"], profile, images)
        else:
            if args.modal_profile is not None:
                raise WorkerFault("modal_profile_requires_modal_backend")
            runner = DockerRunner(args.data_dir, config["id"], args.docker_socket)
        if args.command == "verify-images":
            read_private(args.catalog, 65536)
            print(json.dumps(verify_enrollment(config, client, runner, load_catalog(args.catalog), backend=args.backend), sort_keys=True))
            return 0
        worker = ComputeWorker(args.data_dir, config, client, runner)
        if args.command == "retire":
            print(json.dumps(worker.retire(), sort_keys=True))
            return 0

        def stop(_signum, _frame):
            worker.stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        worker.run(once=args.once)
        return 0
    except (WorkerFault, RuntimeFault) as exc:
        parser.exit(2, f"reasoning-compute-worker: {exc}\n")
    except (ValueError, TypeError, KeyError, OSError):
        parser.exit(2, "reasoning-compute-worker: invalid or unavailable private worker state\n")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
