"""Guided onboarding with real protected handoffs/keys and no cloud or remote writes."""

from __future__ import annotations

import hashlib
import io
import json
import random
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_public_deployment as deployment_fixtures
from rich.console import Console
from test_public_operator_handoff import deployment_state, key_pair

from reasoning_ledger.deployment import cli, onboard, operator_handoff, setup_ui
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import MAX_STATE, locked_directory, read_document, read_private, write_document, write_private

TOKEN = "dop_v1_" + "4" * 64
REPLACEMENT_TOKEN = "dop_v1_" + "5" * 64


@pytest.fixture
def flow(tmp_path, monkeypatch, request):
    release_payload = None
    if getattr(request, "param", None) == "large":
        release_payload = deployment_fixtures.payload.__wrapped__()
        release_payload["web/assets/large-test.js"] = random.Random(0).randbytes(512 * 1024)
    original, document, _, binding, _, _ = deployment_state(tmp_path, release_payload=release_payload)
    identity, public_file, public = key_pair(tmp_path, "new-operator")
    trust_file = tmp_path / "known-hosts"
    write_private(trust_file, b"139.59.1.2 ssh-ed25519 fixture\n")

    @contextmanager
    def trust(*args, **kwargs):
        yield trust_file

    monkeypatch.setattr(operator_handoff, "server_trust", trust)
    monkeypatch.setattr(operator_handoff, "approve", lambda *args: None)

    def command(arguments):
        print(json.dumps({"phase": "closed" if "--close" in arguments else "open"}))
        return 0

    handoff = tmp_path / "received-handoff.tar.gz"
    receipt = operator_handoff.grant_operator(original, public_file, handoff, command,
        detect_public_ipv4=lambda: "203.0.113.30/32", authorize_remote=lambda *args: {"phase": "authorized"})
    stage, target = tmp_path / "onboard prep", tmp_path / "deployment state"
    calls, approvals, opened = [], [], []
    team = {"identity": document["plan"]["account_identity"]}
    valid_tokens = {TOKEN, REPLACEMENT_TOKEN}
    rejected_tokens = set()

    class Provider:
        def __init__(self, token):
            assert token in valid_tokens
            self.token = token
            calls.append("team-read")

        def identity(self):
            if self.token in rejected_tokens:
                raise InstallError("DigitalOcean account read failed (HTTP 401); check account:read permissions, "
                                   "token validity, Team access, and selected resources")
            return team["identity"]

        def close(self):
            pass

    monkeypatch.setattr(operator_handoff, "DigitalOcean", Provider)
    monkeypatch.setattr("reasoning_ledger.deployment.setup.DigitalOcean", Provider)
    monkeypatch.setattr(onboard, "prepare_identity", lambda state: (identity, public))
    monkeypatch.setattr(onboard, "approve", lambda prompt, phrase: approvals.append(phrase))
    monkeypatch.setattr(onboard, "open_page", opened.append)

    def credential(directory, name, prompt, **kwargs):
        path = directory / name
        if not path.exists():
            write_private(path, TOKEN.encode())
        return path

    monkeypatch.setattr(onboard, "private_credential", credential)
    real_join = operator_handoff.join_operator

    def join(*args, **kwargs):
        calls.append("join")
        return real_join(*args, **kwargs)

    monkeypatch.setattr(onboard, "join_operator", join)

    def sync(state, command):
        assert read_document(state / "setup.json")["identity"] == str(identity)
        calls.append("sync")
        return {"phase": "operator_synchronized", "ssh_closed": True}

    def verify(state, command, *, display=True):
        assert display is False
        calls.append("doctor")
        return {"phase": "operator_verified", "ssh_closed": True, "public_url": binding["public_url"],
                "state_dir": str(state), "release_id": binding["release_id"], "diagnosis": {"all_checks_passed": True},
                "checks": {name: True for name, _ in setup_ui.ACCESS_CHECKS}}

    monkeypatch.setattr(onboard, "synchronize_operator", sync)
    monkeypatch.setattr(onboard, "verify_operator", verify)
    return {"stage": stage, "target": target, "handoff": handoff, "digest": receipt["handoff_sha256"],
            "identity": identity, "public": public, "calls": calls, "approvals": approvals, "opened": opened,
            "command": command, "team": team, "binding": binding, "join": join, "verify": verify,
            "rejected": rejected_tokens, "credential": credential}


def answers(monkeypatch, values):
    values = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt: next(values))


def run(flow, **kwargs):
    return onboard.run_onboard(flow["target"], flow["stage"], flow["command"], **kwargs)


def ready(flow, monkeypatch, *, browser="no"):
    answers(monkeypatch, [str(flow["handoff"]), flow["digest"], browser])


def test_handoff_instructions_have_spaced_steps_and_copyable_literal_commands(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False, color_system=None, width=120))
    resume = "make onboard 'DEPLOY_STATE=/private/deployment [test]' 'ONBOARD_STATE=/private/onboard prep'"
    setup_ui.onboard_handoff("/private/onboard [test]/operator.pub", "SHA256:fixture", "ssh-ed25519 fixture", resume)
    rendered = output.getvalue()
    assert "Public key file: /private/onboard [test]/operator.pub" in rendered
    assert "SSH key fingerprint: SHA256:fixture" in rendered
    assert "\n\nssh-ed25519 fixture\n\n1. On the existing operator's computer\n" in rendered
    assert "reads it once.\n\n  make operator-authorize\n\n" in rendered
    assert '"Collaborator SSH key fingerprint"' in rendered
    assert "handoff archive checksum" in rendered
    assert "Protect the received archive:\n\n  chmod 600 /path/to/received-handoff.tar.gz\n\n3. Continue" in rendered
    assert "To resume, run:\n\n  " + resume + "\n\n" in rendered


def test_archive_prompt_is_short_and_separated_from_instructions(flow, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert run(flow)["phase"] == "operator_handoff_required"
    assert prompts == ["Handoff archive path: "]
    rendered = capsys.readouterr().out
    assert "RECEIVED HANDOFF" in rendered
    assert "leave it blank to pause" in rendered
    assert rendered.index("3. Continue here") < rendered.index("RECEIVED HANDOFF")
    assert flow["calls"] == []


def test_pause_exports_public_key_without_cloud_access_or_secrets_and_reuses_it(flow, monkeypatch, capsys):
    answers(monkeypatch, ["", ""])
    first = run(flow)
    assert first["phase"] == "operator_handoff_required"
    assert flow["calls"] == flow["approvals"] == []
    public_file = Path(first["public_key_file"])
    assert public_file.read_text() == flow["public"] + "\n"
    assert public_file.stat().st_mode & 0o777 == 0o600
    fingerprint = subprocess.run(["ssh-keygen", "-lf", str(public_file)], check=True, capture_output=True, text=True).stdout.split()[1]
    assert first["public_key_fingerprint"] == fingerprint
    original = public_file.read_bytes()
    assert run(flow) == first and public_file.read_bytes() == original
    assert shlex.split(first["resume_command"]) == ["make", "onboard", "DEPLOY_STATE=" + str(flow["target"]),
                                                  "ONBOARD_STATE=" + str(flow["stage"])]
    output = capsys.readouterr().out
    assert "make operator-authorize" in output and TOKEN not in output
    assert "PRIVATE KEY" not in output


def test_complete_onboarding_joins_once_and_always_rechecks_live_access(flow, monkeypatch, capsys):
    ready(flow, monkeypatch, browser="yes")
    result = run(flow)
    assert result["phase"] == "operator_onboarded" and result["ssh_closed"] is True and result["application_modified"] is False
    assert flow["calls"] == ["join", "team-read", "sync", "doctor"]
    assert flow["opened"] == ["https://cloud.digitalocean.com/account/api/tokens"]
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "verified"
    snapshot = flow["stage"] / "handoff.tar.gz"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == flow["digest"] and snapshot.stat().st_mode & 0o777 == 0o600
    assert read_document(flow["target"] / "setup.json") == {
        "identity": str(flow["identity"]), "token_file": str(flow["stage"] / "digitalocean-token"),
    }
    # The original input archive can move; retries use the retained exact snapshot.
    flow["handoff"].rename(flow["handoff"].with_name("moved.tar.gz"))
    answers(monkeypatch, [])
    assert run(flow)["phase"] == "operator_onboarded"
    assert flow["calls"].count("join") == 1 and flow["calls"].count("doctor") == 2
    assert flow["approvals"] == ["onboard " + flow["binding"]["public_url"]] * 2
    assert TOKEN not in capsys.readouterr().out


def test_hidden_token_prompt_stays_private_and_is_not_repeated(flow, monkeypatch, capsys):
    from reasoning_ledger.deployment.setup import private_credential

    prompts = []
    monkeypatch.setattr(onboard, "private_credential", private_credential)
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or TOKEN)
    answers(monkeypatch, [str(flow["handoff"]), flow["digest"], "no", ""])
    assert run(flow)["phase"] == "operator_onboarded"
    token = flow["stage"] / "digitalocean-token"
    assert token.read_text() == TOKEN and token.stat().st_mode & 0o777 == 0o600
    answers(monkeypatch, [])
    run(flow)
    assert len(prompts) == 1 and TOKEN not in capsys.readouterr().out


def test_operator_scope_table_has_all_six_scopes_and_no_key_upload_or_full_access(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False, color_system=None, width=120))
    setup_ui.digitalocean_access(onboard.OPERATOR_DIGITALOCEAN_PERMISSIONS)
    rendered = output.getvalue()
    assert {scope for _, scopes in onboard.OPERATOR_DIGITALOCEAN_PERMISSIONS for scope in scopes} == {
        "account:read", "domain:read", "droplet:read", "tag:read", "firewall:read", "firewall:update",
    }
    assert all(scope in rendered for _, scopes in onboard.OPERATOR_DIGITALOCEAN_PERMISSIONS for scope in scopes)
    assert "ssh_key:create" not in rendered and "Full Access" not in rendered
    for name in ("operations.md", "deployment.md", "cli.md"):
        text = (Path(__file__).resolve().parents[1] / "docs" / name).read_text()
        assert all("`" + scope + "`" in text for _, scopes in onboard.OPERATOR_DIGITALOCEAN_PERMISSIONS for scope in scopes), name


def test_replace_token_resumes_completed_join_without_touching_key_archive_or_old_token(flow, monkeypatch, capsys):
    ready(flow, monkeypatch)
    run(flow)
    identity_before = flow["identity"].read_bytes()
    archive_before = (flow["stage"] / "handoff.tar.gz").read_bytes()
    old_setup = read_document(flow["target"] / "setup.json")
    monkeypatch.setattr("getpass.getpass", lambda prompt: REPLACEMENT_TOKEN)
    answers(monkeypatch, ["no", ""])
    assert run(flow, replace_token=True)["phase"] == "operator_onboarded"
    local = read_document(flow["target"] / "setup.json")
    replacement = Path(local["token_file"])
    assert replacement == flow["stage"] / "digitalocean-token-2"
    assert read_private(replacement, 4096) == REPLACEMENT_TOKEN.encode()
    assert replacement.stat().st_mode & 0o777 == 0o600
    assert read_private(Path(old_setup["token_file"]), 4096) == TOKEN.encode()
    assert read_document(flow["stage"] / "onboarding.json")["token_file"] == str(replacement)
    assert flow["identity"].read_bytes() == identity_before
    assert (flow["stage"] / "handoff.tar.gz").read_bytes() == archive_before
    assert flow["calls"].count("join") == 1
    answers(monkeypatch, [])
    run(flow)
    assert flow["calls"].count("join") == 1
    rendered = capsys.readouterr().out
    assert REPLACEMENT_TOKEN not in rendered and "droplet:read" in rendered and "tag:read" in rendered
    assert "Full Access" not in rendered


@pytest.mark.parametrize("failure", ["wrong-team", "denied", "changed-origin"])
def test_token_replacement_cannot_change_joined_state_without_team_site_and_approval(flow, monkeypatch, failure):
    ready(flow, monkeypatch)
    run(flow)
    old_setup = (flow["target"] / "setup.json").read_bytes()
    old_journal = (flow["stage"] / "onboarding.json").read_bytes()
    flow["calls"].clear()
    if failure == "wrong-team":
        flow["team"]["identity"] = "other-team"
    elif failure == "denied":
        def deny(*args):
            raise InstallError("not confirmed")

        monkeypatch.setattr(onboard, "approve", deny)
    else:
        value = read_document(flow["target"] / "current-release.json")
        write_document(flow["target"] / "current-release.json", {**value, "public_url": "https://different.example.org"})
    monkeypatch.setattr("getpass.getpass", lambda prompt: REPLACEMENT_TOKEN)
    answers(monkeypatch, ["no", ""])
    with pytest.raises(InstallError):
        run(flow, replace_token=True)
    assert (flow["target"] / "setup.json").read_bytes() == old_setup
    assert (flow["stage"] / "onboarding.json").read_bytes() == old_journal
    assert "join" not in flow["calls"] and "sync" not in flow["calls"] and "doctor" not in flow["calls"]


def test_token_replacement_refuses_uncertain_join_before_collecting_credentials(flow, monkeypatch):
    ready(flow, monkeypatch)
    run(flow)
    value = read_document(flow["stage"] / "onboarding.json")
    write_document(flow["stage"] / "onboarding.json", {**value, "phase": "joining"})
    answers(monkeypatch, [])
    with pytest.raises(InstallError, match="without --replace-token"):
        run(flow, replace_token=True)
    assert not (flow["stage"] / "digitalocean-token-2").exists()


def test_rejected_token_in_a_join_that_wrote_nothing_can_be_replaced(flow, monkeypatch, capsys):
    flow["rejected"].add(TOKEN)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="HTTP 401") as error:
        run(flow)
    assert "The join wrote no deployment state" in str(error.value)
    assert shlex.split(str(error.value).rsplit("run: ", 1)[1]) == [
        "make", "onboard", "ONBOARD_REPLACE_TOKEN=1", "DEPLOY_STATE=" + str(flow["target"]), "ONBOARD_STATE=" + str(flow["stage"])]
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "joining"
    assert not flow["target"].exists() or {entry.name for entry in flow["target"].iterdir()} == {"provision.lock"}
    old_token = flow["stage"] / "digitalocean-token"
    monkeypatch.setattr("getpass.getpass", lambda prompt: REPLACEMENT_TOKEN)
    answers(monkeypatch, ["no", ""])
    assert run(flow, replace_token=True)["phase"] == "operator_onboarded"
    replacement = flow["stage"] / "digitalocean-token-2"
    assert read_document(flow["target"] / "setup.json")["token_file"] == str(replacement)
    assert read_document(flow["stage"] / "onboarding.json")["token_file"] == str(replacement)
    assert read_private(old_token, 4096) == TOKEN.encode()
    assert flow["calls"].count("join") == 2 and flow["approvals"] == ["onboard " + flow["binding"]["public_url"]] * 2
    assert TOKEN not in capsys.readouterr().out


def test_malformed_saved_token_stops_before_any_api_call_and_names_the_replacement(flow, monkeypatch):
    def doubled(directory, name, prompt, **kwargs):
        path = directory / name
        write_private(path, (TOKEN + TOKEN).encode())
        return path

    monkeypatch.setattr(onboard, "private_credential", doubled)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="pasted more than once") as error:
        run(flow)
    assert "ONBOARD_REPLACE_TOKEN=1" in str(error.value) and TOKEN not in str(error.value)
    assert flow["calls"] == [] and not (flow["target"] / "setup.json").exists()
    monkeypatch.setattr("getpass.getpass", lambda prompt: REPLACEMENT_TOKEN)
    answers(monkeypatch, ["no", ""])
    assert run(flow, replace_token=True)["phase"] == "operator_onboarded"
    assert read_document(flow["target"] / "setup.json")["token_file"] == str(flow["stage"] / "digitalocean-token-2")


def test_token_pasted_at_the_browser_question_is_refused_and_cannot_be_used(flow, monkeypatch, capsys):
    from reasoning_ledger.deployment import setup
    from reasoning_ledger.deployment.setup import private_credential

    monkeypatch.setattr(setup, "_EXPOSED_SECRETS", set())
    monkeypatch.setattr(onboard, "private_credential", private_credential)
    secrets = iter([TOKEN, REPLACEMENT_TOKEN])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(secrets))
    answers(monkeypatch, [str(flow["handoff"]), flow["digest"], TOKEN, "no", ""])
    assert run(flow)["phase"] == "operator_onboarded"
    assert (flow["stage"] / "digitalocean-token").read_text() == REPLACEMENT_TOKEN
    output = capsys.readouterr().out
    assert "Secret pasted into a visible prompt" in output and "displayed in the terminal" in output
    assert TOKEN not in output and REPLACEMENT_TOKEN not in output
    assert flow["opened"] == []


def test_onboarding_checks_the_private_key_once_per_run(flow, monkeypatch):
    monkeypatch.setattr(operator_handoff, "derive_public", lambda identity: pytest.fail("second passphrase prompt"))
    ready(flow, monkeypatch)
    assert run(flow)["phase"] == "operator_onboarded"


def test_completion_lists_verified_checks_and_next_steps(flow, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False, color_system=None, width=200))
    ready(flow, monkeypatch)
    run(flow)
    rendered = output.getvalue()
    summary = rendered[rendered.index("Deployment access verified: " + flow["binding"]["public_url"]):]
    for label in ("DigitalOcean access", "SSH authentication and server identity", "Installed release and HTTPS",
                  "Temporary SSH access closed"):
        assert "✓ " + label in summary
    assert "Ready to upgrade.\nCoordinate with the other operator, then run: make upgrade" in summary
    assert "Optional later recheck: make doctor" in summary


def test_success_requires_every_verified_check(flow, monkeypatch):
    def partial(state, command, **kwargs):
        result = flow["verify"](state, command, **kwargs)
        return {**result, "checks": {**result["checks"], "release_and_https": False}}

    monkeypatch.setattr(onboard, "verify_operator", partial)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="unverified"):
        run(flow)
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "joined"


def test_retry_recovers_active_token_pointer_after_lost_onboarding_journal_write(flow, monkeypatch):
    ready(flow, monkeypatch)
    run(flow)
    write = onboard.write_document

    def interrupted(path, value, **kwargs):
        if path == flow["stage"] / "onboarding.json" and value["token_file"].endswith("digitalocean-token-2"):
            raise InstallError("lost replacement journal write")
        return write(path, value, **kwargs)

    monkeypatch.setattr(onboard, "write_document", interrupted)
    monkeypatch.setattr("getpass.getpass", lambda prompt: REPLACEMENT_TOKEN)
    answers(monkeypatch, ["no", ""])
    with pytest.raises(InstallError, match="lost replacement"):
        run(flow, replace_token=True)
    active = read_document(flow["target"] / "setup.json")["token_file"]
    assert active.endswith("digitalocean-token-2")
    monkeypatch.setattr(onboard, "write_document", write)
    answers(monkeypatch, [])
    assert run(flow)["phase"] == "operator_onboarded"
    assert read_document(flow["stage"] / "onboarding.json")["token_file"] == active
    assert flow["calls"].count("join") == 1


@pytest.mark.parametrize("flow", ["large"], indirect=True)
def test_large_handoff_resumes_after_old_small_document_copy_limit(flow, monkeypatch):
    assert MAX_STATE < flow["handoff"].stat().st_size <= onboard.MAX_HANDOFF
    current_writer = onboard.write_private

    def small_document_writer(path, body, **kwargs):
        # Reproduce the prior failure after saving digest intent, before copying.
        return current_writer(path, body)

    monkeypatch.setattr(onboard, "write_private", small_document_writer)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="byte allowance"):
        run(flow)
    assert read_document(flow["stage"] / "onboarding.json")["handoff_sha256"] == flow["digest"]
    assert not (flow["stage"] / "handoff.tar.gz").exists()
    assert not flow["target"].exists() and flow["calls"] == []
    monkeypatch.setattr(onboard, "write_private", current_writer)
    answers(monkeypatch, [str(flow["handoff"]), "no"])
    assert run(flow)["phase"] == "operator_onboarded"
    snapshot = flow["stage"] / "handoff.tar.gz"
    assert snapshot.read_bytes() == flow["handoff"].read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert flow["calls"].count("join") == 1
    answers(monkeypatch, [])
    assert run(flow)["phase"] == "operator_onboarded"
    assert flow["calls"].count("join") == 1


def test_private_archive_writer_keeps_small_document_default_and_explicit_bound(tmp_path):
    body = b"x" * (MAX_STATE + 1)
    rejected = tmp_path / "small-document"
    with pytest.raises(InstallError, match="byte allowance"):
        write_private(rejected, body)
    with pytest.raises(InstallError, match="byte allowance"):
        write_document(rejected, {"large": "x" * MAX_STATE}, new=True)
    assert not rejected.exists()
    archive = tmp_path / "private-archive"
    write_private(archive, body, maximum=len(body))
    assert read_private(archive, len(body)) == body
    assert archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(InstallError, match="already exists"):
        write_private(archive, b"replacement", maximum=len(body))
    with pytest.raises(InstallError, match="byte allowance"):
        write_private(rejected, body, maximum=len(body) - 1)
    assert not rejected.exists() and archive.read_bytes() == body


@pytest.mark.parametrize("change", ["origin", "plan", "trust", "identity"])
def test_verified_state_cannot_be_rebound_to_other_deployment_on_retry(flow, monkeypatch, change):
    from reasoning_ledger.deployment.ssh_host import PUBLIC_JOURNAL

    ready(flow, monkeypatch)
    run(flow)
    relative = {"origin": "current-release.json", "plan": "plan.json", "trust": "ownership/" + PUBLIC_JOURNAL,
                "identity": "setup.json"}[change]
    path = flow["target"] / relative
    value = read_document(path)
    if change == "origin":
        value["public_url"] = "https://different.example.org"
    elif change == "plan":
        value["deployment_id"] = "e" * 64
    elif change == "trust":
        value["public_key"] = "changed"
    else:
        value["identity"] = "/private/another-key"
    write_document(path, value)
    flow["calls"].clear()
    answers(monkeypatch, [])
    with pytest.raises(InstallError):
        run(flow)
    assert flow["calls"] == []


@pytest.mark.parametrize("phase,receipt", [
    ("sync", {"phase": "operator_sync_deferred", "ssh_closed": True}),
    ("sync", {"phase": "operator_synchronized", "ssh_closed": False}),
    ("doctor", {"phase": "operator_verified", "ssh_closed": False}),
])
def test_unverified_or_deferred_receipts_cannot_report_success(flow, monkeypatch, phase, receipt):
    monkeypatch.setattr(onboard, "synchronize_operator" if phase == "sync" else "verify_operator", lambda *args, **kwargs: receipt)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="unverified"):
        run(flow)
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "joined"
    assert not (flow["stage"] / "last-verification.json").exists()


@pytest.mark.parametrize("kind", ["checksum", "untrusted-digest", "wrong-key", "wrong-team", "denied"])
def test_bad_handoff_or_authority_stops_before_live_verification(flow, monkeypatch, kind):
    digest = "0" * 64 if kind == "checksum" else "" if kind == "untrusted-digest" else flow["digest"]
    if kind == "wrong-key":
        identity, _, public = key_pair(flow["stage"].parent, "different")
        monkeypatch.setattr(onboard, "prepare_identity", lambda state: (identity, public))
    if kind == "wrong-team":
        flow["team"]["identity"] = "other-team"
    if kind == "denied":
        def deny(*args):
            raise InstallError("not confirmed")

        monkeypatch.setattr(onboard, "approve", deny)
    answers(monkeypatch, [str(flow["handoff"]), digest, "no"])
    with pytest.raises(InstallError):
        run(flow)
    assert "sync" not in flow["calls"] and "doctor" not in flow["calls"]
    assert not (flow["target"] / "setup.json").exists()


def test_lost_join_receipt_is_recovered_without_second_provider_read_or_join(flow, monkeypatch):
    def lose_reply(*args, **kwargs):
        flow["join"](*args, **kwargs)
        raise InstallError("lost join receipt")

    monkeypatch.setattr(onboard, "join_operator", lose_reply)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="lost join receipt"):
        run(flow)
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "joining"
    answers(monkeypatch, [])
    assert run(flow)["phase"] == "operator_onboarded"
    assert flow["calls"].count("join") == flow["calls"].count("team-read") == 1


def test_partial_join_is_preserved_and_never_repeated_blindly(flow, monkeypatch):
    def interrupt(handoff, target, *args, **kwargs):
        with locked_directory(target):
            write_document(target / "setup.json",
                           {"identity": str(flow["identity"]), "token_file": str(flow["stage"] / "digitalocean-token")}, new=True)
        raise InstallError("interrupted join")

    monkeypatch.setattr(onboard, "join_operator", interrupt)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="interrupted join"):
        run(flow)
    original = (flow["target"] / "setup.json").read_bytes()
    answers(monkeypatch, [])
    with pytest.raises(InstallError, match="preserve the partial directory"):
        run(flow)
    assert (flow["target"] / "setup.json").read_bytes() == original and flow["calls"] == []


@pytest.mark.parametrize("phase", ["sync", "doctor"])
def test_verification_failures_keep_joined_state_and_resume_without_rejoining(flow, monkeypatch, phase):
    name = "synchronize_operator" if phase == "sync" else "verify_operator"
    original = getattr(onboard, name)

    def unavailable(*args, **kwargs):
        raise InstallError("verification unavailable; SSH closed")

    monkeypatch.setattr(onboard, name, unavailable)
    ready(flow, monkeypatch)
    with pytest.raises(InstallError, match="verification unavailable"):
        run(flow)
    assert read_document(flow["stage"] / "onboarding.json")["phase"] == "joined"
    monkeypatch.setattr(onboard, name, original)
    answers(monkeypatch, [])
    assert run(flow)["phase"] == "operator_onboarded" and flow["calls"].count("join") == 1


@pytest.mark.parametrize("change", ["target", "key", "export", "snapshot", "public-mode", "journal-symlink"])
def test_changed_or_unsafe_saved_inputs_are_refused(flow, monkeypatch, change):
    answers(monkeypatch, [""])
    run(flow)
    if change == "target":
        flow["target"] = flow["target"].with_name("different-target")
    elif change == "key":
        identity, _, public = key_pair(flow["stage"].parent, "changed-key")
        monkeypatch.setattr(onboard, "prepare_identity", lambda state: (identity, public))
    elif change == "export":
        write_private(flow["stage"] / "other.pub", b"changed")
        (flow["stage"] / "operator.pub").rename(flow["stage"] / "original.pub")
        (flow["stage"] / "operator.pub").symlink_to(flow["stage"] / "other.pub")
    elif change == "snapshot":
        write_private(flow["stage"] / "handoff.tar.gz", b"unapproved archive")
    elif change == "public-mode":
        (flow["stage"] / "onboarding.json").chmod(0o644)
    else:
        (flow["stage"] / "onboarding.json").rename(flow["stage"] / "original.json")
        (flow["stage"] / "onboarding.json").symlink_to(flow["stage"] / "original.json")
    answers(monkeypatch, [])
    with pytest.raises(InstallError):
        run(flow)
    assert flow["calls"] == []


def test_existing_deployment_and_git_or_nested_paths_are_not_adopted(flow, monkeypatch):
    flow["target"].mkdir(mode=0o700)
    write_private(flow["target"] / "existing-private-state", b"must survive")
    with pytest.raises(InstallError, match="empty deployment state"):
        run(flow)
    assert (flow["target"] / "existing-private-state").read_bytes() == b"must survive"
    nested = flow["stage"] / "nested"
    flow["stage"].mkdir(mode=0o700, exist_ok=True)
    with pytest.raises(InstallError, match="non-nested"):
        onboard.run_onboard(nested, flow["stage"], flow["command"])
    checkout = flow["stage"].parent / "checkout"
    checkout.mkdir(mode=0o700)
    (checkout / ".git").mkdir()
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    with pytest.raises(InstallError, match="outside Git"):
        onboard.run_onboard(checkout / "state", flow["stage"], flow["command"])


def test_cli_requires_own_interactive_terminal_before_preparation(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main(["onboard", "--state-dir", str(tmp_path / "target"), "--onboard-dir", str(tmp_path / "stage")])
    assert error.value.code == 2 and not (tmp_path / "stage").exists()


def test_make_target_only_invokes_guided_onboard_and_retains_path_overrides():
    result = subprocess.run(["make", "-n", "onboard", "DEPLOY_STATE=/private/deployment state", "ONBOARD_STATE=/private/onboard prep"],
                            check=True, capture_output=True, text=True)
    assert 'reasoning-deploy onboard --state-dir "/private/deployment state" --onboard-dir "/private/onboard prep"' in result.stdout
    assert "upgrade" not in result.stdout and "bundle" not in result.stdout and "npm" not in result.stdout


def test_make_token_replacement_is_explicit_and_rejects_invalid_flag():
    result = subprocess.run(["make", "-n", "onboard", "ONBOARD_REPLACE_TOKEN=1"],
                            check=True, capture_output=True, text=True)
    assert 'case "1"' in result.stdout and "--replace-token" in result.stdout
    invalid = subprocess.run(["make", "onboard", "ONBOARD_REPLACE_TOKEN=invalid", "UV=/bin/true"],
                             check=False, capture_output=True, text=True)
    assert invalid.returncode != 0 and "ONBOARD_REPLACE_TOKEN must be 0 or 1" in invalid.stderr
