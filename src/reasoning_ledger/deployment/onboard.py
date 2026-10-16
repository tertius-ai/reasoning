"""Guided new-operator preparation, authenticated handoff, join, and live verification."""

from __future__ import annotations

import hashlib
import re
import secrets
import shlex
from pathlib import Path

from . import setup_ui
from .host import InstallError, inspect_bundle
from .operator_doctor import verify_operator
from .operator_handoff import DOCUMENT_NAMES, MAX_HANDOFF, _read_handoff, _read_handoff_body, _validate_documents, join_operator
from .operator_sync import synchronize_operator
from .private import locked_directory, protected_parent, read_document, read_private, write_document, write_private
from .release_state import current_binding
from .setup import (
    OPERATOR_DIGITALOCEAN_PERMISSIONS,
    approve,
    ask,
    confirm,
    digitalocean_token,
    private_credential,
    replacement_digitalocean_token,
    validate_digitalocean_token,
)
from .setup_auth import open_page
from .setup_ssh import prepare_identity, public_identity, ssh_fingerprint
from .ssh_host import PUBLIC_JOURNAL

KEYS = {"format", "state_dir", "identity", "public_key", "handoff_sha256", "token_file", "phase"}
PHASES = {"awaiting_handoff", "joining", "joined", "verified"}


def _outside_checkout(path: Path) -> None:
    protected_parent(path)
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file() for parent in (path, *path.parents)):
        raise InstallError("keep onboarding and deployment state outside Git checkouts; they contain credentials")


def _empty_target(target: Path) -> None:
    if target.exists() or target.is_symlink():
        with locked_directory(target) as directory:
            if {entry.name for entry in directory.iterdir()} != {"provision.lock"}:
                raise InstallError("onboard requires new empty deployment state; do not adopt or replace another operator's directory")


def _untouched_target(target: Path) -> bool:
    """True when the deployment state is absent or holds only its lock: a join wrote nothing there."""
    if not target.exists() and not target.is_symlink():
        return True
    with locked_directory(target) as directory:
        return {entry.name for entry in directory.iterdir()} == {"provision.lock"}


def _replace_command(target: Path, state: Path) -> str:
    return shlex.join(["make", "onboard", "ONBOARD_REPLACE_TOKEN=1", "DEPLOY_STATE=" + str(target), "ONBOARD_STATE=" + str(state)])


def _validate(value: dict, target: Path) -> None:
    try:
        if (set(value) != KEYS or type(value["format"]) is not int or value["format"] != 1
                or value["state_dir"] != str(target) or value["phase"] not in PHASES):
            raise ValueError
        for field in ("identity", "token_file"):
            if value[field] is not None and (not isinstance(value[field], str) or not Path(value[field]).is_absolute()):
                raise ValueError
        if (value["identity"] is None) != (value["public_key"] is None):
            raise ValueError
        if value["public_key"] is not None and public_identity(value["public_key"]) != value["public_key"]:
            raise ValueError
        if value["handoff_sha256"] is not None and not re.fullmatch(r"[a-f0-9]{64}", value["handoff_sha256"]):
            raise ValueError
        if value["phase"] != "awaiting_handoff" and any(value[field] is None for field in ("identity", "handoff_sha256", "token_file")):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise InstallError("saved onboarding inputs differ or are invalid; preserve both private directories") from exc


def _snapshot_handoff(state: Path, config: dict, journal: Path) -> Path | None:
    snapshot = state / "handoff.tar.gz"
    if not snapshot.exists() and not snapshot.is_symlink():
        setup_ui.section("Received handoff", "Enter the archive path, or leave it blank to pause until the existing operator sends it.")
        print()
        filename = input("Handoff archive path: ").strip()
        if not filename:
            return None
        body = read_private(Path(filename).expanduser().absolute(), MAX_HANDOFF)
        if config["handoff_sha256"] is None:
            expected = ask("Handoff archive checksum (SHA-256) from the existing operator's message").lower()
            if not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise InstallError("enter the full SHA-256 received from the existing operator, not a locally inferred approval")
            config["handoff_sha256"] = expected
        if not secrets.compare_digest(hashlib.sha256(body).hexdigest(), config["handoff_sha256"]):
            raise InstallError("handoff checksum differs from the authenticated existing-operator digest")
        documents, release_name, release_body = _read_handoff_body(body)
        _validate_documents(documents, release_name, release_body)
        if documents["delegation.json"]["operator_public_key"] != config["public_key"]:
            raise InstallError("handoff authorizes a different SSH key; request a handoff for this box's displayed key")
        # Retain intent before copying; a lost copy resumes with the same approved bytes.
        write_document(journal, config)
        write_private(snapshot, body, maximum=MAX_HANDOFF)
    if config["handoff_sha256"] is None or not secrets.compare_digest(
        hashlib.sha256(read_private(snapshot, MAX_HANDOFF)).hexdigest(), config["handoff_sha256"]
    ):
        raise InstallError("retained handoff differs from the authenticated digest; preserve onboarding state")
    return snapshot


def _recover_join(target: Path, config: dict, documents: dict, binding: dict, release_name: str) -> bool:
    """Observe an exactly completed join after a lost receipt; never fill or overwrite partial state."""
    if not target.exists() and not target.is_symlink():
        return False
    with locked_directory(target) as directory:
        if {entry.name for entry in directory.iterdir()} == {"provision.lock"}:
            return False
        try:
            if read_document(directory / "setup.json") != {"identity": config["identity"], "token_file": config["token_file"]}:
                raise InstallError("joined credentials differ")
            for name in DOCUMENT_NAMES:
                if name != "delegation.json" and read_document(directory / name) != documents[name]:
                    raise InstallError("joined deployment metadata differs")
            if current_binding(directory) != binding:
                raise InstallError("joined release differs")
            inspect_bundle(directory / release_name, binding["bundle_sha256"])
        except (InstallError, OSError) as exc:
            raise InstallError("join was interrupted or its state differs; preserve the partial directory "
                               "for reconciliation, do not rejoin blindly") from exc
    return True


def run_onboard(directory: Path, onboard_directory: Path, command, *, replace_token: bool = False) -> dict:
    target, staging = directory.absolute(), onboard_directory.absolute()
    _outside_checkout(target)
    _outside_checkout(staging)
    if target == staging or target in staging.parents or staging in target.parents:
        raise InstallError("onboarding preparation and deployment state must be separate, non-nested directories")
    setup_ui.onboard_header()
    with locked_directory(staging) as state:
        journal = state / "onboarding.json"
        if journal.exists() or journal.is_symlink():
            config = read_document(journal)
            _validate(config, target)
        else:
            if {entry.name for entry in state.iterdir()} != {"provision.lock"}:
                raise InstallError("choose an empty onboarding preparation directory; preserve existing files")
            _empty_target(target)
            config = {"format": 1, "state_dir": str(target), "identity": None, "public_key": None,
                      "handoff_sha256": None, "token_file": None, "phase": "awaiting_handoff"}
            write_document(journal, config, new=True)
        # A join that wrote no deployment state made no change anywhere; its token may be replaced.
        # A partial join keeps its credentials until it is resumed and reconciled.
        if replace_token and config["phase"] == "joining" and not _untouched_target(target):
            raise InstallError("resume the existing join without --replace-token before replacing credentials; preserve both directories")
        if config["phase"] == "awaiting_handoff":
            _empty_target(target)
        setup_ui.section("This computer's SSH identity",
                         "Generate a dedicated key or select an existing private key; passphrases stay in this terminal.")
        identity, public = prepare_identity(state)
        if config["identity"] is not None and (str(identity) != config["identity"] or public != config["public_key"]):
            raise InstallError("onboarding SSH identity changed; do not authorize a replacement silently")
        config.update(identity=str(identity), public_key=public)
        write_document(journal, config)
        public_file = state / "operator.pub"
        encoded = (public + "\n").encode("ascii")
        if public_file.exists() or public_file.is_symlink():
            if read_private(public_file, 16384) != encoded:
                raise InstallError("exported operator public key changed; preserve it")
        else:
            write_private(public_file, encoded)
        fingerprint = ssh_fingerprint(public)
        resume = shlex.join(["make", "onboard", "DEPLOY_STATE=" + str(target), "ONBOARD_STATE=" + str(state)])
        setup_ui.onboard_handoff(str(public_file), fingerprint, public, resume)
        handoff = _snapshot_handoff(state, config, journal)
        if handoff is None:
            setup_ui.warning("Public key preparation is complete. Waiting for existing-operator authorization; "
                             "no cloud API, SSH, or application changes.",
                             title="Onboarding paused")
            return {"phase": "operator_handoff_required", "state_dir": str(target), "onboard_dir": str(state),
                    "public_key_file": str(public_file), "public_key_fingerprint": fingerprint, "resume_command": resume}
        documents, release_name, release_body = _read_handoff(handoff)
        binding, _ = _validate_documents(documents, release_name, release_body)
        if documents["delegation.json"]["operator_public_key"] != public:
            raise InstallError("handoff authorizes a different SSH key; request a handoff for this box's displayed key")
        if config["token_file"] is None:
            setup_ui.section("Your DigitalOcean Team token",
                             "Switch to the existing deployment's Team; organization roles alone do not grant infrastructure access.")
            setup_ui.digitalocean_access(OPERATOR_DIGITALOCEAN_PERMISSIONS)
            if confirm("Open the DigitalOcean token page in your browser?"):
                open_page("https://cloud.digitalocean.com/account/api/tokens")
            token = private_credential(state, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                       validate=validate_digitalocean_token)
            config["token_file"] = str(token)
            write_document(journal, config)
            replacement = None
        elif replace_token:
            replacement = replacement_digitalocean_token(
                state, documents["delegation.json"]["account_identity"], permissions=OPERATOR_DIGITALOCEAN_PERMISSIONS
            )
        else:
            replacement = None
        if config["phase"] in {"awaiting_handoff", "joining"} and replacement is None:
            try:
                digitalocean_token(read_private(Path(config["token_file"]), 4096))
            except InstallError as exc:
                raise InstallError(f"saved DigitalOcean token {config['token_file']} is unusable: {exc}. "
                                   "Replace it privately with: " + _replace_command(target, state)) from None
        setup_ui.section("Join and verify", "Verify Team/key/release identity, retain private state, synchronize the installed release, "
                         "check SSH/HTTPS, and close temporary SSH.")
        print("Deployment: " + binding["public_url"])
        print("No new organization, VM, deployment, service restart, database access, or upgrade is authorized by this routine.")
        approve("Join/resume this deployment and temporarily open SSH to this box's current /32 for pinned, read-only verification?",
                "onboard " + binding["public_url"])
        if replacement is not None and config["phase"] in {"awaiting_handoff", "joining"}:
            config["token_file"] = str(replacement)
            write_document(journal, config)
        if config["phase"] in {"awaiting_handoff", "joining"}:
            recovered = config["phase"] == "joining" and _recover_join(target, config, documents, binding, release_name)
            if not recovered:
                _empty_target(target)
                config["phase"] = "joining"
                write_document(journal, config)
                try:
                    # The key was derived from this identity above; do not prompt for its passphrase again.
                    join_operator(handoff, target, Path(config["token_file"]), identity, config["handoff_sha256"],
                                  public_key=public)
                except InstallError as exc:
                    try:
                        untouched = _untouched_target(target)
                    except (InstallError, OSError):
                        untouched = False
                    if not untouched:
                        raise
                    raise InstallError(f"{exc}. The join wrote no deployment state. If the DigitalOcean token is wrong, "
                                       "expired, from another Team, or missing a permission, create a new one and run: "
                                       + _replace_command(target, state)) from exc
            config["phase"] = "joined"
            write_document(journal, config)
        # Recheck even a previously verified onboarding: a receipt is not current access evidence.
        with locked_directory(target) as deployment:
            local = read_document(deployment / "setup.json")
            if local.get("identity") != str(identity):
                raise InstallError("joined deployment no longer uses this operator's SSH identity")
            if (current_binding(deployment)["public_url"] != binding["public_url"]
                    or read_document(deployment / "plan.json") != documents["plan.json"]
                    or read_document(deployment / "ownership" / PUBLIC_JOURNAL) != documents["ownership/" + PUBLIC_JOURNAL]):
                raise InstallError("joined origin, VM plan, or pinned SSH trust differs from the authorized handoff")
            if replacement is not None:
                local = {**local, "token_file": str(replacement)}
                write_document(deployment / "setup.json", local)
            read_private(Path(local["token_file"]), 4096)
            config["token_file"] = local["token_file"]
        config["phase"] = "joined"
        write_document(journal, config)
        synchronized = synchronize_operator(target, command)
        if synchronized.get("phase") != "operator_synchronized" or synchronized.get("ssh_closed") is not True:
            raise InstallError("onboarding synchronization is unverified; preserve state and resume the same command")
        result = verify_operator(target, command, display=False)
        checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
        if (result.get("phase") != "operator_verified" or result.get("ssh_closed") is not True
                or any(checks.get(name) is not True for name, _ in setup_ui.ACCESS_CHECKS)):
            raise InstallError("onboarding verification or SSH closure is unverified; preserve state and resume")
        write_document(state / "last-verification.json", result, new=not (state / "last-verification.json").exists())
        config["phase"] = "verified"
        write_document(journal, config)
        setup_ui.access_verified(result, onboarded=True, state=str(target), staging=str(state))
        return {**result, "phase": "operator_onboarded", "onboard_dir": str(state), "application_modified": False}
