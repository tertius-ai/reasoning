"""Guided, resumable upgrades for servers created by the setup wizard."""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import stat
from contextlib import redirect_stdout
from pathlib import Path

from . import setup_ui
from .digitalocean import ProviderUpdateRejected
from .host import InstallError, inspect_bundle
from .private import locked_directory, read_document, write_document
from .release_state import archived_release, current_binding, record_current, release_binding, retain_release
from .server_operations import CHECKS
from .setup import approve, replacement_digitalocean_token, temporary_ssh_cidr
from .ssh_host import server_trust

OPERATION_KEYS = {"format", "source_release", "target_release", "plan_id", "operation_id"}
PREPARED_KEYS = {
    "format",
    "operation",
    "operation_id",
    "plan_id",
    "source",
    "target",
    "phase",
    "server_restart_fenced",
    "root_private_backup_retained",
    "activation_performed",
    "upgrade_completed",
    "runtime_dependency_attestation",
    "recovery_directory",
}


def _private_directory(path: Path) -> Path:
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise InstallError("upgrade directories must be private directories")
    return path


def _operation(value: dict, source: str, target: str, plan_id: str) -> dict:
    try:
        if (
            not isinstance(value, dict)
            or set(value) != OPERATION_KEYS
            or type(value["format"]) is not int
            or value["format"] != 1
            or value["source_release"] != source
            or value["target_release"] != target
            or value["plan_id"] != plan_id
            or not re.fullmatch(r"[a-f0-9]{64}", value["operation_id"])
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("saved upgrade operation differs from this release change") from exc


def _server_binding(value: dict) -> dict:
    return {
        "release_id": value["release_id"],
        "bundle_sha256": value["bundle_sha256"],
        "source_revision": value["source_revision"],
        "dirty": False,
        "public_url": value["public_url"],
    }


def _preflight_refusal(value: dict) -> str:
    # Report only locally known names/statuses, never arbitrary remote output.
    diagnosis = value.get("diagnosis")
    checks = diagnosis.get("checks") if isinstance(diagnosis, dict) else None
    failed = []
    if isinstance(checks, dict):
        failed = [f"{name}={checks[name]}" for name in CHECKS if checks.get(name) in ("fail", "unverified")]
    reasons = {
        "uncommitted_release": "uncommitted release",
        "target_is_current_release": "target is already installed",
        "schema_downgrade_requires_explicit_snapshot_restore": "schema downgrade requires explicit snapshot restore",
        "target_has_no_https_release_identity": "target lacks HTTPS release identity",
        "installed_host_checks_failed_or_unverified": "installed server checks failed or were unverified",
    }
    description = "; ".join(reasons[reason] for reason in value["refusals"] if reason in reasons)
    return (
        "upgrade preflight refused; no preparation or activation was attempted by this command. "
        + ("Failed checks: " + ", ".join(failed) + ". " if failed else "")
        + ("Reason: " + description + ". " if description else "")
        + "Repair the reported checks before retrying; preserve the deployment and upgrade directories. "
        "A refused preflight does not prove the existing service is healthy."
    )


def _plan(value: dict, source: dict, target: dict, *, allow_refused: bool = False) -> dict:
    try:
        if (
            value["operation"] != "upgrade-plan"
            or not re.fullmatch(r"[a-f0-9]{64}", value["plan_id"])
            or value["source"] != _server_binding(source)
            or value["target"] != _server_binding(target)
            or type(value["preflight_checks_passed"]) is not bool
            or not isinstance(value["refusals"], list)
            or any(not isinstance(reason, str) for reason in value["refusals"])
            or value["preflight_checks_passed"] != (value["refusals"] == [])
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("upgrade preflight refused or differs from the retained releases") from exc
    if not value["preflight_checks_passed"] and not allow_refused:
        raise InstallError(_preflight_refusal(value))
    return value


def _prepared(value: dict, source: dict, target: dict, plan_id: str, operation_id: str) -> dict:
    try:
        if (
            set(value) != PREPARED_KEYS
            or value["format"] != 1
            or type(value["format"]) is not int
            or value["operation"] != "upgrade-prepare"
            or value["operation_id"] != operation_id
            or value["plan_id"] != plan_id
            or value["source"] != _server_binding(source)
            or value["target"] != _server_binding(target)
            or value["phase"] != "prepared"
            or value["server_restart_fenced"] is not True
            or value["root_private_backup_retained"] is not True
            or value["activation_performed"] is not False
            or value["upgrade_completed"] is not False
            or value["runtime_dependency_attestation"] is not False
            or value["recovery_directory"] != "/var/lib/reasoning-deploy/server-upgrades/" + operation_id
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("saved preparation receipt differs from this upgrade operation") from exc


def _installation(state: Path, source: dict) -> tuple[dict, dict, str]:
    plan = read_document(state / "plan.json")
    completion = read_document(state / "setup-completion.json")
    installation = read_document(state / "ownership" / "digitalocean-installation.json")
    try:
        intent = installation["intent"]
        if (
            completion["compute_backend"] != "none"
            or intent["binding"]["public_url"] != source["public_url"]
            or not isinstance(intent["ssh_host"], str)
            or intent["request"]["plan_id"] != completion["plan_id"]
        ):
            raise ValueError
        return plan, intent["request"], intent["ssh_host"]
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("guided upgrade requires the original completed non-compute deployment state") from exc


def _invoke(command, arguments) -> dict:
    output = io.StringIO()
    with redirect_stdout(output):
        code = command([str(value) for value in arguments])
    if code != 0:
        raise InstallError("upgrade command failed; preserve the setup and upgrade directories")
    try:
        value = json.loads(output.getvalue())
    except (json.JSONDecodeError, TypeError) as exc:
        raise InstallError("upgrade command returned an invalid receipt") from exc
    if not isinstance(value, dict):
        raise InstallError("upgrade command returned an invalid receipt")
    return value


def _ssh_action(command, state, config_path, config, plan_path, plan_id, ownership, arguments):
    def run(token_file):
        return _invoke(
            command,
            [
                "do-ssh-access",
                "--token-file",
                token_file,
                "--plan",
                plan_path,
                "--state-dir",
                ownership,
                "--confirm-plan",
                plan_id,
                *arguments,
            ],
        )

    try:
        return config, run(config["token_file"])
    except ProviderUpdateRejected:
        plan = read_document(plan_path)["plan"]
        replacement = replacement_digitalocean_token(state, plan["account_identity"])
        config = {**config, "token_file": str(replacement)}
        write_document(config_path, config)
        return config, run(config["token_file"])


def run_upgrade(target_bundle: Path, directory: Path, command, *, detect_public_ipv4=None) -> dict:
    directory = directory.absolute()
    manifest, target_release, target_checksum = inspect_bundle(target_bundle)
    if manifest["dirty"]:
        raise InstallError("guided upgrade requires a release built from committed clean source")
    with locked_directory(directory) as state:
        source = current_binding(state)
        target = release_binding(manifest, target_release, target_checksum, source["public_url"])
        target_archive = retain_release(target_bundle, state, target)
        setup_ui.upgrade_header(source["release_id"], target_release)
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config = read_document(config_path)
        deployment, request, address = _installation(state, source)
        plan_id = request["plan_id"]

        if target_release == source["release_id"]:
            access_path = ownership / "digitalocean-ssh-access.json"
            if access_path.exists() or access_path.is_symlink():
                access = read_document(access_path)
                if access.get("current") is not None or access.get("desired") is not None:
                    config, closed = _ssh_action(
                        command, state, config_path, config, plan_path, plan_id, ownership, ["--close", "--wait-seconds", 60]
                    )
                    if closed.get("phase") != "closed":
                        return {
                            "phase": "ssh_access_closing",
                            "release_id": target_release,
                            "public_url": source["public_url"],
                            "state_dir": str(state),
                        }
            return {"phase": "already_current", "release_id": target_release, "public_url": source["public_url"], "state_dir": str(state)}

        source_archive = archived_release(state, source["release_id"])
        inspect_bundle(source_archive, source["bundle_sha256"])
        root = _private_directory(state / "upgrades")
        change = _private_directory(root / target_release)
        saved_plan = change / "plan.json"
        operation_path = change / "operation.json"
        prepared_path = change / "prepared.json"
        operator = change / "operator"
        session_cidr = temporary_ssh_cidr(detect_public_ipv4)
        setup_ui.upgrade_review(source, target, str(change), session_cidr)
        config, opened = _ssh_action(
            command,
            state,
            config_path,
            config,
            plan_path,
            plan_id,
            ownership,
            ["--open-cidr", session_cidr, "--wait-seconds", 60],
        )
        if opened.get("phase") != "open":
            return {
                "phase": "ssh_access_opening",
                "release_id": source["release_id"],
                "public_url": source["public_url"],
                "state_dir": str(state),
            }
        result = None
        try:
            with server_trust(deployment, ownership, address, request) as known_hosts:
                plan = None
                if saved_plan.exists() or saved_plan.is_symlink():
                    previous = _plan(read_document(saved_plan), source, target, allow_refused=True)
                    if previous["preflight_checks_passed"]:
                        plan = previous
                    else:
                        # Never replace a plan bound to any started operation.
                        if any(path.exists() or path.is_symlink() for path in (operation_path, prepared_path, operator)):
                            raise InstallError(_preflight_refusal(previous) + " An operation already exists; its plan was not replaced.")
                        retained = change / ("refused-plan-" + secrets.token_hex(16) + ".json")
                        write_document(retained, previous, new=True)
                        saved_plan.unlink()
                        setup_ui.console.print("Retained refused preflight; checking the server again before any preparation.")
                if plan is None:
                    with setup_ui.upgrade_progress(
                        "Checking the installed server and target release",
                        ("pin SSH trust", "inspect installed release", "compare target", "run upgrade preflight"),
                    ):
                        value = _invoke(
                            command,
                            [
                                "upgrade-plan",
                                "--bundle",
                                source_archive,
                                "--confirm-release",
                                source["release_id"],
                                "--target-bundle",
                                target_archive,
                                "--confirm-target-release",
                                target_release,
                                "--public-url",
                                source["public_url"],
                                "--ssh-host",
                                address,
                                "--identity",
                                config["identity"],
                                "--known-hosts",
                                known_hosts,
                                "--output",
                                saved_plan,
                            ],
                        )
                        plan = _plan(value, source, target)
                if operation_path.exists() or operation_path.is_symlink():
                    operation = _operation(read_document(operation_path), source["release_id"], target_release, plan["plan_id"])
                else:
                    operation = {
                        "format": 1,
                        "source_release": source["release_id"],
                        "target_release": target_release,
                        "plan_id": plan["plan_id"],
                        "operation_id": secrets.token_hex(32),
                    }
                    write_document(operation_path, operation, new=True)
                setup_ui.upgrade_ready(plan)
                if prepared_path.exists() or prepared_path.is_symlink():
                    _prepared(read_document(prepared_path), source, target, plan["plan_id"], operation["operation_id"])
                else:
                    approve(
                        "Stop the server, retain a verified encrypted backup, and prepare this upgrade? "
                        "If the server has running work that would block migration, nothing is stopped.",
                        "prepare upgrade " + source["public_url"],
                    )
                    with setup_ui.upgrade_progress(
                        "Preparing the target release and verified backup",
                        (
                            "snapshot SSH trust",
                            "upload exact releases",
                            "check for running work",
                            "fence restart",
                            "stop service",
                            "verify encrypted backup",
                            "prepare migration",
                        ),
                    ):
                        prepared = _invoke(
                            command,
                            [
                                "upgrade-prepare",
                                "--plan",
                                saved_plan,
                                "--confirm-plan",
                                plan["plan_id"],
                                "--operation-id",
                                operation["operation_id"],
                                "--state-dir",
                                operator,
                                "--ssh-host",
                                address,
                                "--identity",
                                config["identity"],
                                "--known-hosts",
                                known_hosts,
                                "--source-bundle",
                                source_archive,
                                "--target-bundle",
                                target_archive,
                                "--confirm-workers-fenced",
                            ],
                        )
                    prepared = _prepared(prepared, source, target, plan["plan_id"], operation["operation_id"])
                    write_document(prepared_path, prepared, new=True)
                setup_ui.upgrade_prepared(read_document(prepared_path))
                approve("Activate the prepared release and restore HTTPS?", "activate " + source["public_url"])
                with setup_ui.upgrade_progress(
                    "Activating the target and verifying HTTPS",
                    ("publish prepared state", "start services", "verify release identity", "check public HTTPS"),
                ):
                    activated = _invoke(
                        command,
                        [
                            "upgrade-activate",
                            "--plan",
                            saved_plan,
                            "--confirm-plan",
                            plan["plan_id"],
                            "--operation-id",
                            operation["operation_id"],
                            "--state-dir",
                            operator,
                            "--ssh-host",
                            address,
                            "--identity",
                            config["identity"],
                            "--known-hosts",
                            known_hosts,
                            "--confirm-activation",
                        ],
                    )
                if activated.get("phase") != "completed" or activated.get("upgrade_completed") is not True:
                    raise InstallError("upgrade activation is incomplete; resume with the same state directory")
                record_current(state, target)
                result = {
                    "phase": "upgrade_complete",
                    "release_id": target_release,
                    "public_url": source["public_url"],
                    "state_dir": str(state),
                }
        finally:
            config, closed = _ssh_action(
                command, state, config_path, config, plan_path, plan_id, ownership, ["--close", "--wait-seconds", 60]
            )
            if result is not None and closed.get("phase") != "closed":
                result = {**result, "phase": "ssh_access_closing"}
        if result is None:
            raise InstallError("upgrade did not produce a completion receipt; resume the same operation")
        if result["phase"] == "upgrade_complete":
            setup_ui.upgrade_success(source["public_url"], target_release)
        return result
