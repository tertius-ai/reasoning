"""Independent deployment-operator enrollment and secret-free handoff."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_public_deployment as deployment_fixtures
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rich.console import Console
from test_public_provisioning import ProviderFixture

from reasoning_ledger.deployment import operator_handoff, setup_ssh, setup_ui
from reasoning_ledger.deployment.host import InstallError, inspect_bundle
from reasoning_ledger.deployment.private import read_document, write_document, write_private
from reasoning_ledger.deployment.provision import save_plan
from reasoning_ledger.deployment.release_state import record_current, release_binding, retain_release
from reasoning_ledger.deployment.ssh_host import host_identity, server_trust


def key_pair(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    key = Ed25519PrivateKey.generate()
    private = tmp_path / name
    public = tmp_path / (name + ".pub")
    private_body = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    public_body = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    write_private(private, private_body)
    public.write_bytes(public_body + b" collaborator\n")
    public.chmod(0o644)
    return private, public, public_body.decode("ascii")


def deployment_state(tmp_path: Path, *, release_payload: dict[str, bytes] | None = None):
    state = tmp_path / "original"
    state.mkdir(mode=0o700)
    ownership = state / "ownership"
    ownership.mkdir(mode=0o700)
    if release_payload is None:
        release_payload = deployment_fixtures.payload.__wrapped__()
    source = deployment_fixtures.pack(tmp_path / "source.tar.gz", release_payload)
    manifest, release_id, checksum = inspect_bundle(source)
    provider = ProviderFixture()
    try:
        plan = provider.client.plan(
            name="reasoning-test",
            region="fra1",
            size="s-2vcpu-4gb",
            origin="https://chat.example.org",
            dns_zone="example.org",
            ssh_key_id=123,
            admin_cidr="203.0.113.20/32",
            maximum_hourly_usd="0.10",
            pin_ssh_host_key=True,
        )
    finally:
        provider.client.close()
    saved = save_plan(state / "plan.json", plan)
    document, plan_id = read_document(state / "plan.json"), saved["plan_id"]
    binding = release_binding(manifest, release_id, checksum, "https://chat.example.org")
    server_binding = {name: binding[name] for name in ("release_id", "bundle_sha256", "source_revision", "public_url")}
    request = {
        "format": 1,
        "plan_id": plan_id,
        "deployment_id": document["deployment_id"],
        "droplet_id": 456,
    }
    write_document(
        state / "setup-completion.json",
        {
            "format": 1,
            "release_id": release_id,
            "plan_id": plan_id,
            "public_url": binding["public_url"],
            "organization_id": "d" * 32,
            "compute_backend": "none",
            "compute_setup": None,
        },
        new=True,
    )
    write_document(
        ownership / "digitalocean-installation.json",
        {
            "intent": {"binding": server_binding, "request": request, "ssh_host": "139.59.1.2"},
        },
        new=True,
    )
    write_document(
        ownership / "digitalocean.json",
        {
            "plan_id": plan_id,
            "resources": {"droplet": {"status": "created", "id": 456}},
        },
        new=True,
    )
    host_identity(ownership, plan_id, document["deployment_id"], allow_new=True)
    token = tmp_path / "original-token"
    original_identity = tmp_path / "original-identity"
    write_private(token, b"original-private-token-" + b"x" * 32)
    write_private(original_identity, b"original private SSH key fixture")
    write_document(state / "setup.json", {"token_file": str(token), "identity": str(original_identity)}, new=True)
    retain_release(source, state, binding)
    record_current(state, binding)
    return state, document, request, binding, token, original_identity


def test_operator_handoff_uses_own_credentials_and_public_host_trust(tmp_path, monkeypatch, capsys):
    state, document, request, binding, original_token, original_identity = deployment_state(tmp_path)
    collaborator_identity, collaborator_public_file, collaborator_public = key_pair(tmp_path, "collaborator")
    known = tmp_path / "known"
    write_private(known, b"139.59.1.2 ssh-ed25519 fixture\n")
    calls = []

    @contextmanager
    def trust(*_args, **_kwargs):
        yield known

    monkeypatch.setattr(operator_handoff, "server_trust", trust)
    monkeypatch.setattr(operator_handoff, "approve", lambda *_args: None)

    def authorize(address, identity, trusted, public):
        calls.append((address, identity, trusted, public))
        return {"phase": "authorized"}

    def command(arguments):
        assert arguments[0] == "do-ssh-access"
        print(json.dumps({"phase": "closed" if "--close" in arguments else "open"}))
        return 0

    handoff = tmp_path / "operator-handoff.tar.gz"
    result = operator_handoff.grant_operator(
        state,
        collaborator_public_file,
        handoff,
        command,
        detect_public_ipv4=lambda: "203.0.113.20/32",
        authorize_remote=authorize,
    )
    assert result["phase"] == "operator_handoff_ready"
    assert calls == [("139.59.1.2", original_identity, known, collaborator_public)]
    archive = handoff.read_bytes()
    assert original_token.read_bytes() not in archive
    assert original_identity.read_bytes() not in archive

    collaborator_token = tmp_path / "collaborator-token"
    write_private(collaborator_token, b"dop_v1_" + b"2" * 64)

    class SameTeam:
        def __init__(self, token):
            assert token == collaborator_token.read_text()

        def identity(self):
            return document["plan"]["account_identity"]

        def close(self):
            pass

    monkeypatch.setattr(operator_handoff, "DigitalOcean", SameTeam)
    joined = tmp_path / "joined"
    joined_result = operator_handoff.join_operator(handoff, joined, collaborator_token, collaborator_identity, result["handoff_sha256"])
    assert joined_result["phase"] == "operator_ready" and joined_result["release_id"] == binding["release_id"]
    assert read_document(joined / "setup.json") == {
        "identity": str(collaborator_identity),
        "token_file": str(collaborator_token),
    }
    assert not (joined / "ownership" / "digitalocean-ssh.json").exists()
    with server_trust(document, joined / "ownership", "139.59.1.2", request) as delegated_known:
        assert delegated_known.read_text().startswith("139.59.1.2 ssh-ed25519 ")
    _third_identity, third_public_file, third_public = key_pair(tmp_path, "third")
    second_handoff = tmp_path / "second-handoff.tar.gz"
    second_result = operator_handoff.grant_operator(
        joined,
        third_public_file,
        second_handoff,
        command,
        detect_public_ipv4=lambda: "203.0.113.21/32",
        authorize_remote=authorize,
    )
    assert second_result["phase"] == "operator_handoff_ready"
    assert calls[-1] == ("139.59.1.2", collaborator_identity, known, third_public)
    assert "original-private-token" not in capsys.readouterr().out


def test_operator_join_rejects_a_different_private_key_before_provider_access(tmp_path, monkeypatch):
    state, *_ = deployment_state(tmp_path)
    _identity, public_file, _public = key_pair(tmp_path, "authorized")
    wrong_identity, _wrong_public_file, _wrong_public = key_pair(tmp_path, "wrong")
    known = tmp_path / "known"
    write_private(known, b"fixture")

    @contextmanager
    def trust(*_args, **_kwargs):
        yield known

    monkeypatch.setattr(operator_handoff, "server_trust", trust)
    monkeypatch.setattr(operator_handoff, "approve", lambda *_args: None)
    handoff = tmp_path / "operator-handoff.tar.gz"

    def command(arguments):
        print(json.dumps({"phase": "closed" if "--close" in arguments else "open"}))
        return 0

    operator_handoff.grant_operator(
        state,
        public_file,
        handoff,
        command,
        detect_public_ipv4=lambda: "203.0.113.20/32",
        authorize_remote=lambda *_args: {"phase": "authorized"},
    )
    token = tmp_path / "token"
    write_private(token, b"x" * 40)
    monkeypatch.setattr(operator_handoff, "DigitalOcean", lambda *_args: pytest.fail("provider must not be contacted"))
    with pytest.raises(InstallError, match="does not match"):
        operator_handoff.join_operator(
            handoff,
            tmp_path / "joined",
            token,
            wrong_identity,
            hashlib.sha256(handoff.read_bytes()).hexdigest(),
        )


@pytest.fixture
def authorizing(tmp_path, monkeypatch):
    """An original operator state with recorded SSH actions, approvals, enrollments, and rendered output."""
    state, document, *_ = deployment_state(tmp_path)
    known = tmp_path / "known"
    write_private(known, b"139.59.1.2 ssh-ed25519 fixture\n")
    commands, approvals, enrolled = [], [], []
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False, color_system=None, width=200))

    @contextmanager
    def trust(*_args, **_kwargs):
        yield known

    monkeypatch.setattr(operator_handoff, "server_trust", trust)
    monkeypatch.setattr(operator_handoff, "approve", lambda prompt, phrase: approvals.append(phrase))

    def command(arguments):
        commands.append(arguments)
        print(json.dumps({"phase": "closed" if "--close" in arguments else "open"}))
        return 0

    def grant(public_file, handoff=None):
        return operator_handoff.grant_operator(
            state, public_file, handoff, command, detect_public_ipv4=lambda: "203.0.113.20/32",
            authorize_remote=lambda *args: enrolled.append(args[-1]) or {"phase": "authorized"},
        )

    return SimpleNamespace(state=state, document=document, commands=commands, approvals=approvals, enrolled=enrolled,
                           output=output, grant=grant, tmp_path=tmp_path)


def ssh_keygen_fingerprint(public_file: Path) -> str:
    return subprocess.run(["ssh-keygen", "-lf", str(public_file)], check=True, capture_output=True, text=True).stdout.split()[1]


def test_authorization_confirms_with_site_hostname_and_shows_the_standard_fingerprint(authorizing):
    _identity, public_file, public = key_pair(authorizing.tmp_path, "collaborator")
    result = authorizing.grant(public_file)
    fingerprint = ssh_keygen_fingerprint(public_file)
    assert authorizing.approvals == ["authorize operator chat.example.org"]
    assert result["operator_public_key_fingerprint"] == fingerprint == setup_ssh.ssh_fingerprint(public)
    rendered = authorizing.output.getvalue()
    review = rendered[rendered.index("Review collaborator access"):]
    assert "Site" in review and "https://chat.example.org" in review
    assert "Collaborator SSH key fingerprint  " + fingerprint in review
    assert "Handoff archive checksum" in rendered and "SHA-256 " + result["handoff_sha256"] in rendered
    assert result["operator_public_key_sha256"] not in rendered


def test_default_handoff_is_private_and_distinct_per_key_and_release_and_reused(authorizing):
    _identity, first_file, first = key_pair(authorizing.tmp_path, "first")
    _identity, second_file, second = key_pair(authorizing.tmp_path, "second")
    release = read_document(authorizing.state / "current-release.json")["release_id"]
    first_result = authorizing.grant(first_file)
    handoffs = authorizing.state / "handoffs"
    assert handoffs.stat().st_mode & 0o777 == 0o700
    expected = handoffs / f"operator-{operator_handoff._fingerprint(first)[:16]}-release-{release[:12]}.tar.gz"
    assert first_result["handoff"] == str(expected) and expected.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == first_result["handoff_sha256"]
    second_result = authorizing.grant(second_file)
    assert second_result["handoff"] != first_result["handoff"] and Path(first_result["handoff"]).read_bytes() == expected.read_bytes()
    assert authorizing.enrolled == [first, second]
    commands = len(authorizing.commands)
    assert authorizing.grant(first_file) == first_result
    assert len(authorizing.commands) == commands and authorizing.enrolled == [first, second]


def test_ready_to_send_message_names_archive_checksum_and_recipient_steps(authorizing):
    _identity, public_file, _public = key_pair(authorizing.tmp_path, "collaborator")
    result = authorizing.grant(public_file)
    rendered = authorizing.output.getvalue()
    message = rendered[rendered.index("Message to send"):]
    name = Path(result["handoff"]).name
    assert "Send the archive file privately" in rendered
    assert "https://chat.example.org" in message and ssh_keygen_fingerprint(public_file) in message
    assert "chmod 600 /path/to/" + name in message and "make onboard" in message
    assert "handoff archive checksum (SHA-256):\n   " + result["handoff_sha256"] in message
    assert "confirm the checksum with me directly" in message
    assert setup_ui.handoff_message("https://x.example", "SHA256:f", "/a/b.tar.gz", "c" * 64).count("c" * 64) == 1


@pytest.mark.parametrize("problem", ["group-writable", "world-writable", "missing", "symlink"])
def test_unsafe_handoff_destination_is_refused_before_ssh_or_enrollment(authorizing, problem):
    _identity, public_file, _public = key_pair(authorizing.tmp_path, "collaborator")
    directory = authorizing.tmp_path / "shared"
    if problem == "missing":
        offending = directory
    elif problem == "symlink":
        real = authorizing.tmp_path / "real"
        real.mkdir(mode=0o700)
        directory.symlink_to(real)
        offending = directory
    else:
        directory.mkdir(mode=0o700)
        directory.chmod(0o770 if problem == "group-writable" else 0o707)
        offending = directory
    with pytest.raises(InstallError) as error:
        authorizing.grant(public_file, directory / "handoff.tar.gz")
    message = str(error.value)
    assert str(offending) in message
    expected = {"group-writable": "writable by group (mode 0770)", "world-writable": "writable by others (mode 0707)",
                "missing": "does not exist", "symlink": "is a symlink"}[problem]
    assert expected in message
    assert authorizing.commands == [] and authorizing.enrolled == [] and authorizing.approvals == []


def test_handoff_write_failure_after_enrollment_says_what_succeeded_and_closes_ssh(authorizing, monkeypatch):
    _identity, public_file, _public = key_pair(authorizing.tmp_path, "collaborator")

    def fail(*args):
        raise InstallError("disk full fixture")

    monkeypatch.setattr(operator_handoff, "_write_handoff", fail)
    with pytest.raises(InstallError, match="key is authorized on the server, but the handoff archive .* was not written"):
        authorizing.grant(public_file)
    assert len(authorizing.enrolled) == 1 and "--close" in authorizing.commands[-1]


@pytest.mark.parametrize("form", ["path", "line"])
def test_operator_authorize_asks_for_the_public_key_when_none_is_given(authorizing, monkeypatch, form):
    _identity, public_file, public = key_pair(authorizing.tmp_path, "collaborator")
    prompts = []
    answer = str(public_file) if form == "path" else public_file.read_text().strip()
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    result = authorizing.grant(None)
    assert result["phase"] == "operator_handoff_ready" and authorizing.enrolled == [public]
    assert prompts == ["Public-key file path or key line: "]
    rendered = authorizing.output.getvalue()
    assert "read once" in rendered and rendered.count("DEPLOYMENT OPERATOR") == 1


def test_join_rejects_a_malformed_token_file_before_provider_access(authorizing, monkeypatch):
    identity, public_file, _public = key_pair(authorizing.tmp_path, "collaborator")
    result = authorizing.grant(public_file)
    token = authorizing.tmp_path / "doubled-token"
    write_private(token, b"dop_v1_" + b"9" * 64 + b"dop_v1_" + b"9" * 64)
    monkeypatch.setattr(operator_handoff, "DigitalOcean", lambda *_args: pytest.fail("provider must not be contacted"))
    with pytest.raises(InstallError, match="token file .* pasted more than once") as error:
        operator_handoff.join_operator(Path(result["handoff"]), authorizing.tmp_path / "joined", token, identity,
                                       result["handoff_sha256"])
    assert "9" * 64 not in str(error.value)
    assert not (authorizing.tmp_path / "joined" / "setup.json").exists()


def test_make_authorize_prompts_by_default_and_keeps_explicit_paths(tmp_path):
    fake = tmp_path / "uv"
    fake.write_text("#!/bin/sh\nprintf 'ARGS %s\\n' \"$*\"\n")
    fake.chmod(0o700)
    root = Path(__file__).resolve().parents[1]
    default = subprocess.run(["make", "--no-print-directory", "-C", str(root), "_operator-authorize", "UV=" + str(fake),
                              "DEPLOY_STATE=/private/state"], check=True, capture_output=True, text=True).stdout
    line = [item for item in default.splitlines() if "operator-authorize" in item][0]
    assert "--state-dir /private/state" in line and "--public-key" not in line and "--output" not in line
    explicit = subprocess.run(["make", "--no-print-directory", "-C", str(root), "_operator-authorize", "UV=" + str(fake),
                               "OPERATOR_PUBLIC_KEY=/secure/key.pub", "OPERATOR_HANDOFF=/secure/out.tar.gz"],
                              check=True, capture_output=True, text=True).stdout
    line = [item for item in explicit.splitlines() if "operator-authorize" in item][0]
    assert "--public-key /secure/key.pub" in line and "--output /secure/out.tar.gz" in line
    join = subprocess.run(["make", "-n", "-C", str(root), "operator-join"], check=True, capture_output=True, text=True).stdout
    assert "Set OPERATOR_HANDOFF to the received handoff archive." in join
