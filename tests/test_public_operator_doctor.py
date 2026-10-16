"""Operator verification uses retained release/trust and always closes SSH."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console
from test_public_operator_handoff import deployment_state

from reasoning_ledger.deployment import operator_doctor, setup_ui
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.release_state import archived_release


@pytest.mark.parametrize("outcome", ["pass", "failed_check", "transport_error", "opening_pending", "closing_pending"])
def test_operator_verification_uses_exact_retained_inputs_and_closes_ssh(tmp_path, outcome):
    state, document, request, binding, token, identity = deployment_state(tmp_path)
    calls = []

    def command(arguments):
        calls.append(arguments)
        assert arguments[0] == "do-ssh-access"
        assert arguments[arguments.index("--token-file") + 1] == str(token)
        phase = "closed" if "--close" in arguments else "open"
        if outcome == "opening_pending" and phase == "open" or outcome == "closing_pending" and phase == "closed":
            phase = "pending"
        print(json.dumps({"phase": phase}))
        return 0

    def diagnose(archive, origin, address, private, known_hosts, *, confirmed_release):
        assert archive == archived_release(state, binding["release_id"])
        assert origin == binding["public_url"] and address == "139.59.1.2"
        assert private == identity and confirmed_release == binding["release_id"]
        assert known_hosts.read_text().startswith("139.59.1.2 ssh-ed25519 ")
        if outcome == "transport_error":
            raise InstallError("diagnostic transport fixture")
        return {"all_checks_passed": outcome != "failed_check", "checks": {"https_health": "fail" if outcome == "failed_check" else "pass"}}

    if outcome == "pass":
        result = operator_doctor.verify_operator(state, command, detect_public_ipv4=lambda: "203.0.113.20/32", diagnose=diagnose)
        assert result["phase"] == "operator_verified" and result["ssh_closed"] is True
    else:
        with pytest.raises(InstallError):
            operator_doctor.verify_operator(state, command, detect_public_ipv4=lambda: "203.0.113.20/32", diagnose=diagnose)
    assert len(calls) == 2 and "--open-cidr" in calls[0] and "--close" in calls[-1]


def test_cli_operator_doctor_uses_selected_state(tmp_path, monkeypatch, capsys):
    from reasoning_ledger.deployment.cli import main

    calls = []

    def verify(directory, command):
        calls.append(directory)
        return {"phase": "operator_verified", "ssh_closed": True}

    monkeypatch.setattr(operator_doctor, "verify_operator", verify)
    assert main(["operator-doctor", "--state-dir", str(tmp_path)]) == 0
    assert calls == [tmp_path]
    assert json.loads(capsys.readouterr().out)["ssh_closed"] is True


LABELS = ("DigitalOcean access", "SSH authentication and server identity", "Installed release and HTTPS",
          "Temporary SSH access closed")


@pytest.mark.parametrize("outcome", ["pass", "failed_check", "closing_pending"])
def test_doctor_reports_readiness_only_from_verified_checks_and_closure(tmp_path, monkeypatch, outcome):
    state, _document, _request, binding, _token, _identity = deployment_state(tmp_path)
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False, color_system=None, width=200))

    def command(arguments):
        phase = "closed" if "--close" in arguments else "open"
        print(json.dumps({"phase": "pending" if outcome == "closing_pending" and phase == "closed" else phase}))
        return 0

    def diagnose(*args, **kwargs):
        return {"all_checks_passed": outcome != "failed_check", "checks": {"https_health": "fail" if outcome == "failed_check" else "pass"}}

    if outcome == "pass":
        result = operator_doctor.verify_operator(state, command, detect_public_ipv4=lambda: "203.0.113.20/32", diagnose=diagnose)
        assert result["checks"] == {name: True for name, _ in setup_ui.ACCESS_CHECKS}
        rendered = output.getvalue()
        summary = rendered[rendered.index("Deployment access verified: " + binding["public_url"]):]
        assert all("✓ " + label in summary for label in LABELS)
        assert "Ready to upgrade.\nCoordinate with the other operator, then run: make upgrade" in summary
    else:
        with pytest.raises(InstallError, match="https_health" if outcome == "failed_check" else "SSH closure is unverified"):
            operator_doctor.verify_operator(state, command, detect_public_ipv4=lambda: "203.0.113.20/32", diagnose=diagnose)
        assert "Deployment access verified" not in output.getvalue() and "Ready to upgrade" not in output.getvalue()


def test_access_summary_marks_unverified_checks_and_withholds_readiness():
    output = io.StringIO()
    original = setup_ui.console
    setup_ui.console = Console(file=output, force_terminal=False, color_system=None, width=200)
    try:
        checks = {name: True for name, _ in setup_ui.ACCESS_CHECKS} | {"ssh_closed": False}
        assert setup_ui.access_verified({"phase": "operator_verified", "public_url": "https://x.example", "checks": checks}) is False
        assert setup_ui.access_verified({"phase": "operator_verified", "public_url": "https://x.example"}) is False
    finally:
        setup_ui.console = original
    rendered = output.getvalue()
    assert "Deployment access not verified: https://x.example" in rendered
    assert "✗ Temporary SSH access closed" in rendered and "✓ DigitalOcean access" in rendered
    assert "Ready to upgrade" not in rendered
