"""Offline deployment-operator recovery kits and setup protection choices."""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import setup_ui
from .host import InstallError, canonical
from .operator_handoff import (
    MAX_HANDOFF,
    _fingerprint,
    _read_handoff_body,
    _validate_documents,
    default_handoff,
    grant_operator_locked,
    join_operator,
    prompt_public_key,
)
from .private import (
    locked_directory,
    protected_parent,
    protected_temporary_directory,
    read_document,
    read_private,
    write_document,
    write_private,
)
from .release_state import current_binding
from .setup_ssh import public_identity

MAGIC = b"REASONING-OPERATOR-RECOVERY-V1\n"
SALT_BYTES = 16
NONCE_BYTES = 12
CODE = re.compile(r"rnr_[A-Za-z0-9_-]{43}\Z")
MAX_RECOVERY_KIT = 2 * MAX_HANDOFF
PROTECTION_FILE = "operator-protection.json"


def recovery_code() -> str:
    return "rnr_" + secrets.token_urlsafe(32)


def _key(code: str, salt: bytes) -> bytes:
    if not isinstance(code, str) or CODE.fullmatch(code) is None or len(salt) != SALT_BYTES:
        raise InstallError("invalid operator recovery code")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"reasoning-operator-recovery-kit-v1",
    ).derive(code.encode("ascii"))


def _identity() -> tuple[bytes, str]:
    private = Ed25519PrivateKey.generate()
    private_body = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return private_body, public.decode("ascii")


def _large_private(path: Path, body: bytes, maximum: int) -> None:
    path = path.absolute()
    protected_parent(path)
    if not isinstance(body, bytes) or not 0 < len(body) <= maximum:
        raise InstallError("operator recovery output exceeds its byte allowance")
    if path.exists() or path.is_symlink():
        raise InstallError("operator recovery output already exists; choose another explicit filename")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".operator-recovery-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
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
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _seal(identity: bytes, public: str, handoff: bytes, code: str) -> bytes:
    public = public_identity(public)
    if not 0 < len(identity) <= 65536 or not 0 < len(handoff) <= MAX_HANDOFF:
        raise InstallError("operator recovery material exceeds its byte allowance")
    payload = canonical(
        {
            "format": 1,
            "handoff": base64.b64encode(handoff).decode("ascii"),
            "handoff_sha256": hashlib.sha256(handoff).hexdigest(),
            "identity": base64.b64encode(identity).decode("ascii"),
            "operator_public_key": public,
        }
    )
    salt, nonce = secrets.token_bytes(SALT_BYTES), secrets.token_bytes(NONCE_BYTES)
    sealed = AESGCM(_key(code, salt)).encrypt(nonce, payload, MAGIC)
    result = MAGIC + salt + nonce + sealed
    if len(result) > MAX_RECOVERY_KIT:
        raise InstallError("operator recovery kit exceeds its byte allowance")
    return result


def _open_body(body: bytes, code: str) -> dict:
    minimum = len(MAGIC) + SALT_BYTES + NONCE_BYTES + 16
    if not isinstance(body, bytes) or not minimum <= len(body) <= MAX_RECOVERY_KIT or not body.startswith(MAGIC):
        raise InstallError("invalid operator recovery kit")
    offset = len(MAGIC)
    salt = body[offset : offset + SALT_BYTES]
    nonce = body[offset + SALT_BYTES : offset + SALT_BYTES + NONCE_BYTES]
    ciphertext = body[offset + SALT_BYTES + NONCE_BYTES :]
    try:
        plaintext = AESGCM(_key(code, salt)).decrypt(nonce, ciphertext, MAGIC)
        value = json.loads(plaintext)
        if not isinstance(value, dict) or canonical(value) != plaintext or set(value) != {
            "format",
            "handoff",
            "handoff_sha256",
            "identity",
            "operator_public_key",
        }:
            raise ValueError
        if not isinstance(value["format"], int) or isinstance(value["format"], bool) or value["format"] != 1:
            raise ValueError
        identity = base64.b64decode(value["identity"], validate=True)
        handoff = base64.b64decode(value["handoff"], validate=True)
        if not 0 < len(identity) <= 65536 or not 0 < len(handoff) <= MAX_HANDOFF:
            raise ValueError
        if not re.fullmatch(r"[a-f0-9]{64}", value["handoff_sha256"]) or not hmac.compare_digest(
            hashlib.sha256(handoff).hexdigest(), value["handoff_sha256"]
        ):
            raise ValueError
        public = public_identity(value["operator_public_key"])
        key = serialization.load_ssh_private_key(identity, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError
        derived = key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode("ascii")
        if derived != public:
            raise ValueError
        documents, release_name, release_body = _read_handoff_body(handoff)
        binding, _plan_id = _validate_documents(documents, release_name, release_body)
        if documents["delegation.json"]["operator_public_key"] != public:
            raise ValueError
        return {
            "identity": identity,
            "public": public,
            "handoff": handoff,
            "handoff_sha256": value["handoff_sha256"],
            "binding": binding,
        }
    except (InvalidTag, KeyError, TypeError, UnsupportedAlgorithm, ValueError, UnicodeError) as exc:
        raise InstallError("operator recovery kit or code is invalid") from exc


def open_recovery_kit(path: Path, code: str) -> dict:
    return _open_body(read_private(path.absolute(), MAX_RECOVERY_KIT), code)


def _confirm_code(code: str) -> None:
    for _attempt in range(3):
        repeated = getpass.getpass("Re-enter the recovery code from your saved copy: ").strip()
        if hmac.compare_digest(repeated, code):
            return
        print("Recovery code did not match.")
    raise InstallError("recovery code confirmation failed; preserve the written kit and resume with its saved code")


def create_recovery_kit_locked(
    state: Path,
    output: Path,
    command,
    *,
    detect_public_ipv4=None,
    authorize_remote=None,
    code_factory=recovery_code,
    confirm_code=_confirm_code,
) -> dict:
    output = output.expanduser().absolute()
    identity, public = _identity()
    code = code_factory()
    with protected_temporary_directory("reasoning-recovery-kit-") as temporary:
        handoff = temporary / "operator-handoff.tar.gz"
        arguments = {}
        if authorize_remote is not None:
            arguments["authorize_remote"] = authorize_remote
        granted = grant_operator_locked(
            state,
            public,
            handoff,
            command,
            detect_public_ipv4=detect_public_ipv4,
            display=False,
            **arguments,
        )
        if granted["phase"] not in {"operator_handoff_ready", "ssh_access_closing"}:
            return granted
        handoff_body = read_private(handoff, MAX_HANDOFF)
        sealed = _seal(identity, public, handoff_body, code)
        setup_ui.recovery_code(code, str(output), hashlib.sha256(sealed).hexdigest())
        _large_private(output, sealed, MAX_RECOVERY_KIT)
        # Keep the finished kit if confirmation is mistyped or the terminal is
        # interrupted. Setup can validate and adopt that exact file on resume.
        confirm_code(code)
    material = open_recovery_kit(output, code)
    if material["public"] != public or material["handoff_sha256"] != granted["handoff_sha256"]:
        raise InstallError("operator recovery kit verification disagrees with its authorized handoff")
    result = {
        "phase": granted["phase"],
        "mode": "recovery_kit",
        "kit_sha256": hashlib.sha256(read_private(output, MAX_RECOVERY_KIT)).hexdigest(),
        "operator_public_key_sha256": _fingerprint(public),
        "public_url": material["binding"]["public_url"],
    }
    setup_ui.recovery_ready(str(output), result["kit_sha256"])
    return result


def validate_protection(value: dict) -> dict:
    try:
        if (
            not isinstance(value, dict)
            or value.get("format") != 1
            or not isinstance(value.get("format"), int)
            or isinstance(value.get("format"), bool)
        ):
            raise ValueError
        mode = value.get("mode")
        if mode == "deferred":
            if set(value) != {"format", "mode"}:
                raise ValueError
        elif mode in {"recovery_kit", "second_operator"}:
            digest_name = "kit_sha256" if mode == "recovery_kit" else "handoff_sha256"
            if set(value) != {"format", "mode", digest_name, "operator_public_key_sha256"} or any(
                not isinstance(value[name], str) or re.fullmatch(r"[a-f0-9]{64}", value[name]) is None
                for name in (digest_name, "operator_public_key_sha256")
            ):
                raise ValueError
        else:
            raise ValueError
        return value
    except (TypeError, ValueError) as exc:
        raise InstallError("invalid saved operator-protection receipt; preserve the setup directory") from exc


def protect_deployment_locked(state: Path, command, *, detect_public_ipv4=None) -> dict:
    path = state / PROTECTION_FILE
    if path.exists() or path.is_symlink():
        return validate_protection(read_document(path))
    setup_ui.protection_choices()
    choices = {
        "": "recovery_kit",
        "1": "recovery_kit",
        "recovery": "recovery_kit",
        "kit": "recovery_kit",
        "2": "second_operator",
        "operator": "second_operator",
        "admin": "second_operator",
        "3": "deferred",
        "defer": "deferred",
    }
    while True:
        selected = choices.get(input("Protection method [1]: ").strip().casefold())
        if selected is not None:
            break
        print("Choose 1, 2, or 3.")
    if selected == "deferred":
        from .setup import approve

        approve("Continue with no independent deployment recovery?", "accept single-device risk")
        receipt = {"format": 1, "mode": "deferred"}
        setup_ui.protection_deferred()
    elif selected == "second_operator":
        from .setup import ask

        public = prompt_public_key()
        default = default_handoff(state, public, current_binding(state)["release_id"])
        output = Path(ask("Handoff archive output", str(default))).expanduser().absolute()
        result = grant_operator_locked(
            state,
            public,
            output,
            command,
            detect_public_ipv4=detect_public_ipv4,
        )
        if result["phase"] not in {"operator_handoff_ready", "ssh_access_closing"}:
            return {"format": 1, "mode": "pending", "phase": result["phase"]}
        receipt = {
            "format": 1,
            "mode": "second_operator",
            "handoff_sha256": result["handoff_sha256"],
            "operator_public_key_sha256": result["operator_public_key_sha256"],
        }
    else:
        from .setup import ask

        output = Path(
            ask("Encrypted recovery-kit output", str(Path.home() / "reasoning-operator-recovery.rnrk"))
        ).expanduser().absolute()
        if output.exists() or output.is_symlink():
            code = getpass.getpass("Recovery code for the existing kit: ").strip()
            material = open_recovery_kit(output, code)
            if material["binding"] != current_binding(state):
                raise InstallError("existing recovery kit belongs to a different deployment release")
            receipt = {
                "format": 1,
                "mode": "recovery_kit",
                "kit_sha256": hashlib.sha256(read_private(output, MAX_RECOVERY_KIT)).hexdigest(),
                "operator_public_key_sha256": _fingerprint(material["public"]),
            }
            setup_ui.recovery_ready(str(output), receipt["kit_sha256"])
        else:
            result = create_recovery_kit_locked(state, output, command, detect_public_ipv4=detect_public_ipv4)
            if result["phase"] not in {"operator_handoff_ready", "ssh_access_closing"}:
                return {"format": 1, "mode": "pending", "phase": result["phase"]}
            receipt = {
                "format": 1,
                "mode": "recovery_kit",
                "kit_sha256": result["kit_sha256"],
                "operator_public_key_sha256": result["operator_public_key_sha256"],
            }
    validate_protection(receipt)
    write_document(path, receipt, new=True)
    return receipt


def create_operator_recovery(directory: Path, output: Path, command, *, detect_public_ipv4=None) -> dict:
    with locked_directory(directory.absolute()) as state:
        result = create_recovery_kit_locked(state, output, command, detect_public_ipv4=detect_public_ipv4)
        if result["phase"] in {"operator_handoff_ready", "ssh_access_closing"}:
            receipt = {
                "format": 1,
                "mode": "recovery_kit",
                "kit_sha256": result["kit_sha256"],
                "operator_public_key_sha256": result["operator_public_key_sha256"],
            }
            write_document(state / PROTECTION_FILE, receipt)
        return result


def recover_operator(kit: Path, directory: Path, token_file: Path, code: str) -> dict:
    kit, directory, token_file = kit.absolute(), directory.absolute(), token_file.absolute()
    material = open_recovery_kit(kit, code)
    with protected_temporary_directory("reasoning-operator-recover-") as temporary:
        handoff = temporary / "operator-handoff.tar.gz"
        identity = temporary / "recovery-identity"
        _large_private(handoff, material["handoff"], MAX_HANDOFF)
        write_private(identity, material["identity"])
        return join_operator(
            handoff,
            directory,
            token_file,
            identity,
            material["handoff_sha256"],
            retain_identity=True,
        )
