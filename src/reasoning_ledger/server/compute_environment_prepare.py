"""Prepare an approved Lean environment on a relay's Modal account.

The relay reads the environment from the server, which returns it only when an administrator of the relay's
organization approved it. The relay looks up GitHub's SHA-256 for the Lean release's Linux archive, then starts one VM
with its own Modal credentials. The VM mounts one new directory, `/environments/<key>/<attempt>`, of the organization's
Modal Volume (`reasoning-lean-<organization>`) read-write at /opt/lean-env and sees nothing else of the Volume. It runs
compute_environment_vm, which fetches, builds and seals the environment there; its outbound network is limited by Modal to
the hosts in DOMAINS as well. After the VM ends, the relay reads the seal back from the Volume and checks its digest.
A failed attempt's directory is removed once the VM's end is verified.

The receipt is printed and kept, with the VM's progress log, in the relay's `environment-preparations` directory.
Nothing is reported to the server yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path

import httpx

from . import compute_environment_vm, lean_environments
from .compute_catalog import canonical
from .compute_docker import HEX_ID, RuntimeFault
from .compute_modal import ModalSDK, modal_profile, sandbox_name
from .compute_store import ComputeStore
from .security import private_file

# The VM's lifetime. The program inside stops itself earlier, so a slow step still ends with a receipt.
PREPARE_SECONDS = 3600
PROGRAM_SECONDS = PREPARE_SECONDS - 240
DOMAINS = (*sorted(compute_environment_vm.DOWNLOAD_HOSTS), compute_environment_vm.CACHE_HOST)
VOLUME_PREFIX = "reasoning-lean-"
RELEASE_API = "https://api.github.com/repos/leanprover/lean4/releases/tags/"
RESULT = re.compile(rb"^PREPARE-RESULT (\{.*\})$", re.MULTILINE)
OUTPUT_BYTES = 256 * 1024**2
LOG_BYTES = 16 * 1024**2
SEAL_BYTES = 256 * 1024**2
PROGRAM = Path(compute_environment_vm.__file__).read_text(encoding="utf-8")


def volume_name(organization_id: str) -> str:
    """One Modal Volume per organization holds all of its environments."""
    if not isinstance(organization_id, str) or not HEX_ID.fullmatch(organization_id):
        raise RuntimeFault("invalid_job")
    return VOLUME_PREFIX + organization_id


def attempt_path(key: str, attempt: str) -> str:
    if not lean_environments.KEY.fullmatch(key) or not HEX_ID.fullmatch(attempt):
        raise RuntimeFault("invalid_job")
    return f"/environments/{key}/{attempt}"


def approved(environment: object, organization_id: str, environment_id: str) -> dict:
    """The approved specification the server returned for this relay's organization, checked against its key."""
    try:
        if (not isinstance(environment, dict) or environment.get("id") != environment_id
                or environment.get("organization_id") != organization_id or environment.get("status") != "approved"):
            raise ValueError
        specification = lean_environments.validate(environment["specification"])
        if lean_environments.key(specification) != environment["key"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise RuntimeFault("environment_not_approved") from None
    return {"id": environment_id, "key": environment["key"], "specification": specification}


def toolchain_asset(version: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """GitHub's record of the release's Linux archive: its URL, size and SHA-256. No credentials are sent."""
    if not compute_environment_vm.VERSION.fullmatch(version):
        raise RuntimeFault("toolchain_unavailable")
    name = f"lean-{version[1:]}-linux.tar.zst"
    try:
        with httpx.Client(transport=transport, timeout=30, follow_redirects=False,
                          headers={"Accept": "application/vnd.github+json"}) as client:
            response = client.get(RELEASE_API + version)
            response.raise_for_status()
            if len(response.content) > 4 * 1024**2:
                raise ValueError
            release = response.json()
        if release.get("tag_name") != version or release.get("draft"):
            raise ValueError
        asset = next(item for item in release["assets"] if isinstance(item, dict) and item.get("name") == name)
        digest = asset.get("digest")
        if (not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or type(asset.get("size")) is not int or not 0 < asset["size"] <= compute_environment_vm.TOOLCHAIN_BYTES
                or asset.get("browser_download_url") != compute_environment_vm.release_url(version)
                or asset.get("state") != "uploaded"):
            raise ValueError
    except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration, AttributeError):
        raise RuntimeFault("toolchain_unavailable") from None
    return {"version": version, "url": asset["browser_download_url"], "sha256": digest.removeprefix("sha256:"),
            "bytes": asset["size"]}


def plan(environment: dict, asset: dict, attempt: str) -> dict:
    specification = environment["specification"]
    version = lean_environments.TOOLCHAIN.fullmatch(specification["toolchain"])[1]
    if asset["version"] != version:
        raise RuntimeFault("toolchain_unavailable")
    value = {"format": 1, "key": environment["key"], "attempt": attempt, "toolchain": asset,
             "packages": specification["packages"], "seconds": PROGRAM_SECONDS}
    if compute_environment_vm.plan_problems(value):
        raise RuntimeFault("invalid_job")
    return value


class BoundedLog:
    def __init__(self, path: Path, limit: int = LOG_BYTES):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream, self.written, self.limit = os.fdopen(fd, "wb"), 0, limit

    def write(self, body: bytes) -> None:
        if self.written < self.limit:
            part = body[:self.limit - self.written]
            self.stream.write(part)
            self.stream.flush()
            self.written += len(part)
            if self.written >= self.limit:
                self.stream.write(b"\n[log truncated]\n")

    def close(self) -> None:
        self.stream.close()


def failures(receipt: dict) -> list[str]:
    """Each way the receipt falls short of a sealed, verified environment; empty when it is one."""
    problems = []
    if receipt.get("error"):
        problems.append(f"preparation stopped during {receipt['error']['stage']} ({receipt['error']['exception_type']})")
    vm = receipt.get("vm") or {}
    if vm.get("failure"):
        problems.append(f"the VM failed at {vm['failure']['step']}: {vm['failure']['message']}")
    elif not receipt.get("error") and not vm:
        problems.append("the VM reported no result")
    if receipt.get("cleanup_verified") is False:
        problems.append("the VM's end is unverified; it stops at its Modal lifetime")
    if not problems and receipt.get("seal_verified") is not True:
        problems.append("the seal read back from the Volume does not match the VM's")
    return problems


def refused(environment_id: str, reason: str) -> dict:
    """A receipt for a preparation that stopped before anything was started on Modal."""
    return {"format": 1, "environment_id": environment_id, "failures": [reason]}


def sealed(receipt: dict) -> bool:
    """Whether the VM finished and reported a seal. Such an attempt is kept even if reading the seal back failed, so a
    transient read never discards a finished environment; an unfinished one is removed."""
    vm = receipt.get("vm") or {}
    return vm.get("failure") is None and isinstance(vm.get("seal"), dict)


def prepare_environment(data_dir, worker_id: str, organization_id: str, profile: dict, environment: object,
                        environment_id: str, *, sdk=None, lookup=toolchain_asset, clock=time.monotonic) -> dict:
    """Prepare one approved environment and return the receipt; see the module docstring. Costs one VM for up to an hour."""
    profile = modal_profile(profile)
    if not HEX_ID.fullmatch(worker_id) or not HEX_ID.fullmatch(environment_id):
        raise RuntimeFault("invalid_job")
    try:
        environment = approved(environment, organization_id, environment_id)
    except RuntimeFault:
        return refused(environment_id, "the server's record is not an approved environment of this organization that "
                                        "matches its key")
    version = lean_environments.TOOLCHAIN.fullmatch(environment["specification"]["toolchain"])[1]
    try:
        asset = lookup(version)
    except RuntimeFault:
        return refused(environment_id, f"GitHub gave no SHA-256 for the Linux archive of Lean {version}")
    attempt = secrets.token_hex(16)
    value = plan(environment, asset, attempt)
    directory = Path(data_dir).absolute() / "environment-preparations"
    ComputeStore._directory(directory)
    path = attempt_path(environment["key"], attempt)
    receipt = {"format": 1, "environment_id": environment_id, "key": environment["key"], "attempt": attempt,
               "volume": volume_name(organization_id), "sub_path": path,
               "toolchain": {key: asset[key] for key in ("version", "sha256", "bytes")}, "sandbox_id": None,
               "cleanup_verified": None, "vm": None, "seal_verified": None, "removed": False, "error": None,
               "log": f"{attempt}.log"}
    sdk = sdk or ModalSDK(profile)
    log = BoundedLog(directory / f"{attempt}.log")
    began, stage, volume = clock(), "volume", None
    try:
        volume = sdk.organization_volume(receipt["volume"])
        stage = "attempt"
        sdk.put(volume, path + "/attempt.json", canonical({
            "format": 1, "environment_id": environment_id, "key": environment["key"], "attempt": attempt,
            "worker_id": worker_id, "started_at": int(time.time())}).encode())
        stage, name = "vm", sandbox_name(worker_id, attempt)
        try:
            sandbox = sdk.preparation_vm(
                name, profile["resources"], {compute_environment_vm.ROOT: sdk.mounted(volume, read_only=False, sub_path=path)},
                timeout=PREPARE_SECONDS, domains=DOMAINS)
        except Exception:
            # A lost answer may leave a VM running only `sleep`, with the program never started. Find it by name and
            # stop it rather than pay for its hour; if none is found, its end is not claimed.
            try:
                orphan = sdk.lookup(name)
                sdk.terminate(orphan)
                receipt.update(sandbox_id=orphan.object_id, cleanup_verified=True)
            except Exception:
                pass
            raise
        try:
            receipt["sandbox_id"] = sandbox.object_id
            stage = "program"
            code, output = sdk.pump(sandbox, ["python3", "-I", "-c", PROGRAM], timeout=PREPARE_SECONDS - 60,
                                    input_bytes=canonical(value).encode(), maximum=OUTPUT_BYTES, stderr_sink=log.write)
            found = RESULT.findall(output)
            receipt["vm"] = json.loads(found[-1]) if found else None
            receipt["exit_code"] = code
        finally:
            # Termination also commits the Volume. An unverified end is reported; the VM stops at its Modal lifetime.
            try:
                sdk.terminate(sandbox)
                receipt["cleanup_verified"] = True
            except Exception:
                receipt["cleanup_verified"] = False
        if sealed(receipt):
            stage = "seal"
            body = sdk.read(volume, path + "/" + compute_environment_vm.SEAL, SEAL_BYTES)
            receipt["seal_verified"] = hashlib.sha256(body).hexdigest() == receipt["vm"]["seal"].get("sha256")
    except Exception as exc:
        # As in the relay's own receipts: the stage and exception class, never provider bodies or messages.
        kind = type(exc).__name__
        receipt["error"] = {"stage": stage, "exception_type": kind if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", kind)
                            else "Exception", "code": exc.code if isinstance(exc, RuntimeFault) else None}
    finally:
        log.close()
    receipt["seconds"] = round(clock() - began, 1)
    receipt["failures"] = failures(receipt)
    if (receipt["failures"] and not sealed(receipt) and volume is not None
            and receipt["cleanup_verified"] is not False):
        # Nothing runs in the attempt's directory any more; an unfinished attempt leaves nothing behind.
        try:
            sdk.remove(volume, path)
            receipt["removed"] = True
        except Exception:
            receipt["removed"] = False
    private_file(directory / f"{attempt}.json", (json.dumps(receipt, sort_keys=True, indent=1) + "\n").encode())
    return receipt
