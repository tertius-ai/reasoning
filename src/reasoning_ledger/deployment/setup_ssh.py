"""Private local SSH preparation and exact public-key account enrollment."""

from __future__ import annotations

import base64
import hashlib
import subprocess
import time
from pathlib import Path

from .digitalocean import ProviderRejected
from .host import InstallError
from .private import locked_directory, read_document, read_private, write_document


def public_identity(value):
    if not isinstance(value, str) or len(value) > 16384 or "\n" in value.strip():
        raise InstallError("invalid SSH public key metadata")
    parts = value.split()
    if len(parts) < 2 or parts[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
        raise InstallError("unsupported SSH public key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
        length = int.from_bytes(blob[:4], "big")
        if not blob or blob[4:4 + length].decode("ascii") != parts[0]:
            raise ValueError
    except (ValueError, UnicodeError):
        raise InstallError("invalid SSH public key encoding") from None
    return " ".join(parts[:2])


def ssh_fingerprint(public):
    """Return the standard OpenSSH SHA-256 fingerprint, as printed by ssh-keygen -l."""
    blob = base64.b64decode(public_identity(public).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def derive_public(identity):
    read_private(identity, 65536)
    print("Checking the SSH private key locally; enter its passphrase if requested.")
    try:
        result = subprocess.run(["ssh-keygen", "-y", "-f", str(identity)], stdout=subprocess.PIPE,
                                timeout=300, check=False)
        if result.returncode:
            raise InstallError("cannot derive the SSH public key; check the private key/passphrase")
        return public_identity(result.stdout.decode("ascii"))
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise InstallError("SSH key preparation failed; install ssh-keygen and check the private key") from None


def prepare_identity(directory):
    from .setup import ask

    with locked_directory(directory / "ssh") as state:
        journal_path = state / "identity.json"
        if journal_path.exists() or journal_path.is_symlink():
            journal = read_document(journal_path)
            if (set(journal) != {"identity", "generated", "phase"} or journal["phase"] not in {"preparing", "ready"}
                    or type(journal["generated"]) is not bool or not isinstance(journal["identity"], str)
                    or not Path(journal["identity"]).is_absolute()):
                raise InstallError("invalid saved SSH identity selection")
        else:
            value = ask("SSH private-key path, or 'generate' for a dedicated Ed25519 key", "generate")
            identity = state / "id_ed25519" if value == "generate" else Path(value).expanduser().absolute()
            journal = {"identity": str(identity), "generated": value == "generate", "phase": "preparing"}
            if identity.exists() or identity.is_symlink():
                read_private(identity, 65536)
                if journal["generated"]:
                    raise InstallError("generated SSH key already exists without its journal; do not overwrite it")
            write_document(journal_path, journal, new=True)
            if journal["generated"]:
                print("Generating a dedicated SSH key. ssh-keygen asks for an optional passphrase twice.")
                try:
                    result = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-f", str(identity), "-C", "Reasoning deployment"],
                                            timeout=300, check=False)
                    if result.returncode:
                        raise InstallError("SSH generation stopped; preserve and reconcile the original key directory")
                except (OSError, subprocess.TimeoutExpired):
                    raise InstallError("SSH generation stopped; preserve the original key directory") from None
        identity = Path(journal["identity"])
        if journal["generated"] and identity != state / "id_ed25519":
            raise InstallError("generated SSH identity differs from its private directory")
        public = derive_public(identity)  # Also safely recovers a completed generation with a lost receipt.
        journal["phase"] = "ready"
        write_document(journal_path, journal)
        return identity, public


def enroll_public_key(directory, provider, identity, public, *, wait=time.sleep):
    from .setup import approve

    public = public_identity(public)
    binding = {"account": provider.identity(), "identity": str(identity), "public_key": public}
    path = directory / "ssh-enrollment.json"
    if path.exists() or path.is_symlink():
        journal = read_document(path)
        if (set(journal) != {"binding", "phase", "key_id"} or journal["binding"] != binding
                or journal["phase"] not in {"prepared", "creating", "ready"}
                or journal["key_id"] is not None and (type(journal["key_id"]) is not int or journal["key_id"] <= 0)):
            raise InstallError("SSH enrollment differs from its original account/private key")
    else:
        journal = {"binding": binding, "phase": "prepared", "key_id": None}
        write_document(path, journal, new=True)

    def matches():
        keys = provider.pages("account/keys", "ssh_keys")
        found = []
        for key in keys:
            if type(key.get("id")) is not int or key["id"] <= 0:
                raise InstallError("invalid account SSH-key identity")
            if public_identity(key.get("public_key")) == public:
                found.append(key["id"])
        if len(found) > 1:
            raise InstallError("ambiguous duplicate SSH keys; reconcile account keys first")
        return found[0] if found else None

    identifier = matches()
    if journal["phase"] == "ready":
        if identifier != journal["key_id"]:
            raise InstallError("enrolled SSH key was removed or changed; do not replace it on retry")
        return identifier
    if identifier is None:
        if journal["phase"] == "creating":
            raise InstallError("SSH upload reply is uncertain; reconcile the original account key before retrying")
        print("SSH public fingerprint: " + ssh_fingerprint(public))
        approve("Upload only this public SSH key to DigitalOcean (requires ssh_key:create)?", "upload ssh key")
        journal["phase"] = "creating"
        write_document(path, journal)
        try:
            result = provider.post("account/keys", {"name": "reasoning-" + hashlib.sha256(public.encode()).hexdigest()[:12],
                                                    "public_key": public})
            key = result.get("ssh_key")
            if (not isinstance(key, dict) or type(key.get("id")) is not int or key["id"] <= 0
                    or public_identity(key.get("public_key")) != public):
                raise InstallError("SSH upload identity is unverified; preserve its journal")
            identifier = key["id"]
            for delay in (0, 0.25, 0.5, 1, 2, 4):
                if delay:
                    wait(delay)
                if matches() == identifier:
                    break
            else:
                raise InstallError("SSH upload is not yet visible; resume the same setup")
        except ProviderRejected:
            journal["phase"] = "prepared"
            write_document(path, journal)
            raise InstallError("DigitalOcean rejected the SSH key upload; enable ssh_key:create on this token and resume") from None
        except InstallError:
            raise InstallError("SSH enrollment reply is uncertain; resume this setup to reconcile the original public key") from None
    journal.update(phase="ready", key_id=identifier)
    write_document(path, journal)
    return identifier


def verify_saved_identity(directory, config):
    """New wizard enrollments must keep the same private/public key on retry."""
    path = directory / "ssh-enrollment.json"
    if path.exists() or path.is_symlink():
        journal = read_document(path)
        if (set(journal) != {"binding", "phase", "key_id"} or journal["phase"] != "ready"
                or journal["key_id"] != config["ssh_key_id"] or not isinstance(journal["binding"], dict)
                or journal["binding"].get("identity") != config["identity"]
                or derive_public(Path(config["identity"])) != journal["binding"].get("public_key")):
            raise InstallError("saved setup SSH identity changed; restore the original key before provisioning")
