"""Verify a deployment operator using retained state and temporarily opened SSH."""

from __future__ import annotations

from pathlib import Path

from . import setup_ui
from .host import InstallError, inspect_bundle
from .operator_handoff import _handoff_installation
from .private import locked_directory, read_document
from .release_state import archived_release, current_binding
from .server_diagnostics import diagnose_remote
from .setup import temporary_ssh_cidr
from .setup_upgrade import _ssh_action
from .ssh_host import server_trust


def verify_operator(directory: Path, command, *, detect_public_ipv4=None, diagnose=diagnose_remote, display=True) -> dict:
    """Use the operator's own credentials, inspect the host, and close SSH again.

    Each reported check is True only from evidence gathered in this call: the operator's own token
    opened SSH, the pinned host answered the diagnosis, every server check passed, and SSH closed.
    """
    with locked_directory(directory.absolute()) as state:
        source = current_binding(state)
        deployment, request, address = _handoff_installation(state, source)
        source_archive = archived_release(state, source["release_id"])
        inspect_bundle(source_archive, source["bundle_sha256"])
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config = read_document(config_path)
        plan_id = request["plan_id"]
        cidr = temporary_ssh_cidr(detect_public_ipv4)
        report = None
        checks = {"digitalocean_access": False, "ssh_server_identity": False, "release_and_https": False, "ssh_closed": False}
        try:
            config, opened = _ssh_action(
                command, state, config_path, config, plan_path, plan_id, ownership, ["--open-cidr", cidr, "--wait-seconds", 60]
            )
            if opened.get("phase") != "open":
                raise InstallError("temporary SSH access is not verified; rerun make doctor with the same state directory")
            checks["digitalocean_access"] = True
            with (
                server_trust(deployment, ownership, address, request) as known_hosts,
                setup_ui.upgrade_progress(
                    "Verifying deployment access and the installed server", ("pin SSH trust", "inspect release", "check HTTPS")
                ),
            ):
                report = diagnose(
                    source_archive,
                    source["public_url"],
                    address,
                    Path(config["identity"]),
                    known_hosts,
                    confirmed_release=source["release_id"],
                )
            checks["ssh_server_identity"] = isinstance(report, dict)
        finally:
            config, closed = _ssh_action(
                command, state, config_path, config, plan_path, plan_id, ownership, ["--close", "--wait-seconds", 60]
            )
            if closed.get("phase") != "closed":
                raise InstallError("SSH closure is unverified; rerun make doctor with the same state directory")
            checks["ssh_closed"] = True
        if report is None or report.get("all_checks_passed") is not True:
            failed = ", ".join(
                name for name, status in (report or {}).get("checks", {}).items() if status not in {"pass", "not_applicable"}
            )
            raise InstallError("deployment verification failed" + (": " + failed if failed else "") + "; inbound SSH is closed")
        checks["release_and_https"] = True
        result = {
            "phase": "operator_verified",
            "public_url": source["public_url"],
            "release_id": source["release_id"],
            "state_dir": str(state),
            "ssh_closed": True,
            "checks": checks,
            "diagnosis": report,
        }
    if display:
        setup_ui.access_verified(result)
    return result
