"""Public asynchronous operator requests and bounded local handoff imports."""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

from .host import InstallError, canonical
from .private import MAX_STATE, protected_parent, read_private, write_private
from .setup_ssh import public_identity, ssh_fingerprint


def read_transfer(path: Path, maximum: int = MAX_STATE, *, make_private: bool = False) -> bytes:
    """Read an owner-owned download once, refusing links and other writers."""
    path = path.expanduser().absolute()
    protected_parent(path, writable=False)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022
                    or info.st_nlink != 1 or info.st_size > maximum):
                raise InstallError("operator transfer must be a bounded owner-owned regular file without other writers or links")
            if make_private:
                os.fchmod(stream.fileno(), 0o600)
            body = stream.read(maximum + 1)
            if len(body) > maximum:
                raise InstallError("operator transfer exceeds its byte allowance")
            return body
    except OSError as exc:
        raise InstallError("operator transfer must be a readable local regular file, not a symlink") from exc


def operator_request(public: str, target: Path, staging: Path) -> dict:
    return {"format": 1, "public_key": public, "public_key_fingerprint": ssh_fingerprint(public),
            "state_dir": str(target), "onboard_dir": str(staging)}


def read_request(path: Path) -> dict:
    body = read_transfer(path)
    try:
        value = json.loads(body)
        if (not isinstance(value, dict) or set(value) != {
                "format", "public_key", "public_key_fingerprint", "state_dir", "onboard_dir"}
                or type(value["format"]) is not int or value["format"] != 1
                or canonical(value) != body or public_identity(value["public_key"]) != value["public_key"]
                or not value["public_key"].startswith("ssh-ed25519 ")
                or ssh_fingerprint(value["public_key"]) != value["public_key_fingerprint"]):
            raise ValueError
        for field in ("state_dir", "onboard_dir"):
            name = value[field]
            if (not isinstance(name, str) or len(name) > 4096 or not Path(name).is_absolute()
                    or ".." in Path(name).parts or any(ord(c) < 32 or ord(c) == 127 for c in name)):
                raise ValueError
        target, staging = Path(value["state_dir"]), Path(value["onboard_dir"])
        if target == staging or target in staging.parents or staging in target.parents:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise InstallError("invalid operator request; ask the collaborator to resend its original request file") from exc


def retain_message(path: Path, body: bytes) -> None:
    if path.exists() or path.is_symlink():
        if read_private(path) != body:
            raise InstallError("saved operator request differs; preserve the onboarding directory")
    else:
        write_private(path, body)


def save_request(state: Path, public: str, target: Path) -> Path:
    request = state / "operator-request.json"
    retain_message(request, canonical(operator_request(public, target, state)))
    message = (
        "Please authorize this computer as a Reasoning deployment operator.\n"
        "I have attached operator-request.json. It contains my public SSH key and resume paths, not credentials.\n"
        f"SSH key fingerprint: {ssh_fingerprint(public)}\n\n"
        "Run make operator-authorize OPERATOR_REQUEST=/path/to/operator-request.json,\n"
        "compare the displayed fingerprint, and send me the handoff archive and generated reply.\n"
        "I can finish later; neither terminal needs to stay open.\n"
    )
    retain_message(state / "request-message.txt", message.encode("utf8"))
    return request


def resume_command(request: dict | None, checksum: str) -> str:
    if request is None:
        return shlex.join(["make", "onboard", "ONBOARD_HANDOFF_SHA256=" + checksum])
    # Shell-quote data as CLI arguments; never evaluate recipient paths as Make expressions or commands.
    return shlex.join(["uv", "run", "--locked", "reasoning-deploy", "onboard", "--state-dir", request["state_dir"],
                       "--onboard-dir", request["onboard_dir"], "--confirm-handoff-sha256", checksum])
