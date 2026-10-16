"""Encrypted offline operator recovery without copying original setup state."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from test_public_operator_handoff import deployment_state

from reasoning_ledger.deployment import operator_handoff, operator_recovery, setup
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import locked_directory, read_document, write_private

RECOVERY_CODE = "rnr_" + "A" * 43


def test_recovery_kit_authorizes_distinct_key_and_rebuilds_operator_state(tmp_path, monkeypatch):
    state, document, _request, binding, original_token, original_identity = deployment_state(tmp_path)
    known = tmp_path / "known"
    write_private(known, b"139.59.1.2 ssh-ed25519 fixture\n")
    authorized = []

    @contextmanager
    def trust(*_args, **_kwargs):
        yield known

    monkeypatch.setattr(operator_handoff, "server_trust", trust)
    monkeypatch.setattr(operator_handoff, "approve", lambda *_args: None)

    def command(arguments):
        assert arguments[0] == "do-ssh-access"
        print(json.dumps({"phase": "closed" if "--close" in arguments else "open"}))
        return 0

    kit = tmp_path / "operator-recovery.rnrk"
    with locked_directory(state) as locked:
        result = operator_recovery.create_recovery_kit_locked(
            locked,
            kit,
            command,
            detect_public_ipv4=lambda: "203.0.113.22/32",
            authorize_remote=lambda address, identity, trusted, public: authorized.append(
                (address, identity, trusted, public)
            ),
            code_factory=lambda: RECOVERY_CODE,
            confirm_code=lambda code: code == RECOVERY_CODE or pytest.fail("unexpected recovery code"),
        )

    assert result["mode"] == "recovery_kit" and len(authorized) == 1
    assert authorized[0][:3] == ("139.59.1.2", original_identity, known)
    encrypted = kit.read_bytes()
    assert encrypted.startswith(operator_recovery.MAGIC)
    assert original_token.read_bytes() not in encrypted and original_identity.read_bytes() not in encrypted
    with pytest.raises(InstallError, match="kit or code"):
        operator_recovery.open_recovery_kit(kit, "rnr_" + "B" * 43)

    material = operator_recovery.open_recovery_kit(kit, RECOVERY_CODE)
    assert material["binding"]["release_id"] == binding["release_id"]
    assert material["public"] == authorized[0][3]

    fresh_token = tmp_path / "fresh-token"
    write_private(fresh_token, b"dop_v1_" + b"3" * 64)

    class SameTeam:
        def __init__(self, token):
            assert token == fresh_token.read_text()

        def identity(self):
            return document["plan"]["account_identity"]

        def close(self):
            pass

    monkeypatch.setattr(operator_handoff, "DigitalOcean", SameTeam)
    recovered = tmp_path / "recovered"
    receipt = operator_recovery.recover_operator(kit, recovered, fresh_token, RECOVERY_CODE)
    assert receipt["phase"] == "operator_ready" and receipt["release_id"] == binding["release_id"]
    recovered_setup = read_document(recovered / "setup.json")
    assert recovered_setup["identity"] == str(recovered / "recovery-identity")
    assert (recovered / "recovery-identity").is_file()
    assert recovered_setup["token_file"] == str(fresh_token)


def test_setup_protection_requires_explicit_test_only_deferral(tmp_path, monkeypatch, capsys):
    state, *_ = deployment_state(tmp_path)
    answers = iter(["3"])
    approvals = []
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup, "approve", lambda prompt, phrase: approvals.append(phrase))

    with locked_directory(state) as locked:
        receipt = operator_recovery.protect_deployment_locked(locked, lambda arguments: 0)

    assert receipt == {"format": 1, "mode": "deferred"}
    assert approvals == ["accept single-device risk"]
    assert read_document(state / operator_recovery.PROTECTION_FILE) == receipt
    assert "single-device" in capsys.readouterr().out.lower()


def test_setup_protection_can_authorize_second_operator(tmp_path, monkeypatch):
    state, *_ = deployment_state(tmp_path)
    _private, public = operator_recovery._identity()
    public_file = tmp_path / "other.pub"
    public_file.write_text(public + " other\n")
    public_file.chmod(0o600)
    output = tmp_path / "handoff.tar.gz"
    answers = iter(["2", str(public_file), str(output)])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []

    def grant(locked, authorized, destination, command, **kwargs):
        calls.append((locked, authorized, destination, command, kwargs))
        return {
            "phase": "operator_handoff_ready",
            "handoff_sha256": "a" * 64,
            "operator_public_key_sha256": "b" * 64,
        }

    monkeypatch.setattr(operator_recovery, "grant_operator_locked", grant)
    with locked_directory(state) as locked:
        receipt = operator_recovery.protect_deployment_locked(locked, object())

    assert receipt == {
        "format": 1,
        "mode": "second_operator",
        "handoff_sha256": "a" * 64,
        "operator_public_key_sha256": "b" * 64,
    }
    assert calls[0][0] == state and calls[0][1] == public and calls[0][2] == output
