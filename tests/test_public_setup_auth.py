"""Local browser/HTTP/key fixtures; no provider accounts or billable resources."""

import base64
import hashlib
import io
import json
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from test_public_compute import IMAGE
from test_public_deployment import builder_inputs, pack, payload  # noqa: F401

from reasoning_ledger.deployment import host, modal_setup, release, setup, setup_auth, setup_ssh
from reasoning_ledger.deployment.digitalocean import DigitalOcean
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import read_document, read_private, write_document, write_private


@pytest.fixture
def ssh_key(tmp_path):
    path = tmp_path / "private-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path, setup_ssh.derive_public(path)


@pytest.fixture
def account():
    state = {"keys": [], "loss": False, "visible": True, "visibility_delay": 0, "reject": False, "account": "a" * 32}
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/v2/account":
            return httpx.Response(200, json={"account": {"uuid": state["account"], "status": "active", "email_verified": True}})
        assert request.url.path == "/v2/account/keys"
        if request.method == "GET":
            if state["keys"] and state["visibility_delay"]:
                state["visibility_delay"] -= 1
                return httpx.Response(200, json={"ssh_keys": []})
            return httpx.Response(200, json={"ssh_keys": state["keys"] if state["visible"] else []})
        assert request.method == "POST"
        if state["reject"]:
            return httpx.Response(403, json={"id": "forbidden"})
        key = {**json.loads(request.content), "id": 123}
        state["keys"].append(key)
        if state["loss"]:
            raise httpx.ReadError("private transport diagnostic")
        return httpx.Response(201, json={"ssh_key": key})

    with_provider = DigitalOcean("x" * 40, transport=httpx.MockTransport(handle))
    yield with_provider, state, calls
    with_provider.close()


def test_existing_ssh_key_is_derived_and_matching_remote_key_reused_without_upload(tmp_path, ssh_key, account, monkeypatch):
    identity, public = ssh_key
    provider, state, calls = account
    monkeypatch.setattr(setup, "ask", lambda *args: str(identity))
    assert setup_ssh.prepare_identity(tmp_path) == (identity, public)
    state["keys"] = [{"id": 44, "public_key": public + " existing-comment"}]
    monkeypatch.setattr(setup, "approve", lambda *args: pytest.fail("existing key does not need an upload"))
    assert setup_ssh.enroll_public_key(tmp_path, provider, identity, public) == 44
    assert setup_ssh.enroll_public_key(tmp_path, provider, identity, public) == 44
    assert not any(request.method == "POST" for request in calls)


def test_generate_private_key_once_and_recover_completed_generation(tmp_path, monkeypatch):
    original = subprocess.run
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        # Test generation does not use an interactive passphrase; production
        # generation delegates passphrase prompts to ssh-keygen's terminal.
        return original([*args, "-N", ""] if "-t" in args else args, **kwargs)

    monkeypatch.setattr(setup_ssh.subprocess, "run", command)
    monkeypatch.setattr(setup, "ask", lambda *args: "generate")
    first = setup_ssh.prepare_identity(tmp_path)
    assert first[0].stat().st_mode & 0o777 == 0o600
    assert first == setup_ssh.prepare_identity(tmp_path)
    assert sum("-t" in args for args in calls) == 1
    write_document(tmp_path / "ssh/identity.json", {"identity": str(first[0]), "generated": True, "phase": "preparing"})
    assert setup_ssh.prepare_identity(tmp_path) == first
    assert sum("-t" in args for args in calls) == 1


def test_no_generated_key_overwrite_or_missing_generation_retry(tmp_path, ssh_key, monkeypatch):
    state = tmp_path / "ssh"
    state.mkdir(mode=0o700)
    write_private(state / "id_ed25519", ssh_key[0].read_bytes())
    monkeypatch.setattr(setup, "ask", lambda *args: "generate")
    with pytest.raises(InstallError, match="do not overwrite"):
        setup_ssh.prepare_identity(tmp_path)
    assert not (state / "identity.json").exists()


def test_public_only_upload_is_confirmed_and_lost_reply_reconciles_once(tmp_path, ssh_key, account, monkeypatch):
    provider, state, calls = account
    identity, public = ssh_key
    approvals = []
    monkeypatch.setattr(setup, "approve", lambda prompt, phrase: approvals.append(phrase))
    state["loss"] = True
    with pytest.raises(InstallError, match="uncertain"):
        setup_ssh.enroll_public_key(tmp_path, provider, identity, public)
    assert setup_ssh.enroll_public_key(tmp_path, provider, identity, public) == 123
    posts = [request for request in calls if request.method == "POST"]
    assert len(posts) == 1 and approvals == ["upload ssh key"]
    assert json.loads(posts[0].content)["public_key"] == public
    assert identity.read_bytes() not in posts[0].content
    assert "PRIVATE KEY" not in (tmp_path / "ssh-enrollment.json").read_text()


def test_successful_upload_waits_for_key_list_visibility(tmp_path, ssh_key, account, monkeypatch):
    provider, state, calls = account
    waits = []
    state["visibility_delay"] = 2
    monkeypatch.setattr(setup, "approve", lambda *args: None)
    assert setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key, wait=waits.append) == 123
    assert waits == [0.25, 0.5]
    assert sum(request.method == "POST" for request in calls) == 1


def test_definitive_upload_rejection_can_retry_after_scope_correction(tmp_path, ssh_key, account, monkeypatch):
    provider, state, calls = account
    state["reject"] = True
    monkeypatch.setattr(setup, "approve", lambda *args: None)
    with pytest.raises(InstallError, match="enable ssh_key:create"):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key, wait=lambda _: None)
    assert read_document(tmp_path / "ssh-enrollment.json")["phase"] == "prepared"
    state["reject"] = False
    assert setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key, wait=lambda _: None) == 123
    assert sum(request.method == "POST" for request in calls) == 2


def test_uncertain_invisible_upload_does_not_post_again(tmp_path, ssh_key, account, monkeypatch):
    provider, state, calls = account
    monkeypatch.setattr(setup, "approve", lambda *args: None)
    state.update(loss=True, visible=False)
    with pytest.raises(InstallError):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key)
    with pytest.raises(InstallError, match="uncertain"):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key)
    assert sum(request.method == "POST" for request in calls) == 1


def test_denied_upload_and_changed_account_key_are_refused(tmp_path, ssh_key, account, monkeypatch):
    provider, state, calls = account
    monkeypatch.setattr(setup, "approve", lambda *args: (_ for _ in ()).throw(InstallError("denied")))
    with pytest.raises(InstallError, match="denied"):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key)
    assert not any(request.method == "POST" for request in calls)
    state["keys"] = [{"id": 123, "public_key": ssh_key[1]}]
    assert setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key) == 123
    state["keys"] = []
    with pytest.raises(InstallError, match="removed or changed"):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key)
    state["account"] = "b" * 32
    with pytest.raises(InstallError, match="original account"):
        setup_ssh.enroll_public_key(tmp_path, provider, *ssh_key)


def test_saved_setup_refuses_a_different_private_key_before_provisioning(tmp_path, ssh_key):
    identity, public = ssh_key
    config = {"identity": str(identity), "ssh_key_id": 123}
    write_document(tmp_path / "ssh-enrollment.json", {"phase": "ready", "key_id": 123,
                   "binding": {"account": "fixture-account", "identity": str(identity), "public_key": public}})
    setup_ssh.verify_saved_identity(tmp_path, config)
    replacement = tmp_path / "other-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(replacement)], check=True)
    host.atomic_write(identity, replacement.read_bytes())
    with pytest.raises(InstallError, match="SSH identity changed"):
        setup_ssh.verify_saved_identity(tmp_path, config)


def test_configuration_runs_real_key_matching_and_pkce_boundaries(tmp_path, ssh_key, account, monkeypatch, capsys):
    provider, _, calls = account
    answers = iter(["n", "", "example.org", "", str(ssh_key[0]), "upload ssh key", "", "", "",
                    "0.10", "2.50", "browser"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "dop_v1_" + "0" * 64)
    monkeypatch.setattr(setup, "DigitalOcean", lambda token: provider)
    login = setup_auth.openrouter_login
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"key": "sk-private-wizard-key"}))
    monkeypatch.setattr(setup_auth, "openrouter_login", lambda directory: login(
        directory, transport=transport, callback=lambda challenge: "private-wizard-code"))
    value = setup.collect_configuration(tmp_path, "a" * 64, detect_public_ipv4=lambda: "203.0.113.10/32")
    setup.validate_configuration(value, "a" * 64)
    assert value["ssh_key_id"] == 123 and value["identity"] == str(ssh_key[0])
    assert value["models"] == []
    assert read_private(Path(value["openrouter_file"])) == b"sk-private-wizard-key"
    assert sum(request.method == "POST" for request in calls) == 1
    output = capsys.readouterr().out
    assert "one custom DigitalOcean token" in output and "not separate tokens" in output
    ordered = ["account:read", "domain:read", "domain:create", "droplet:read", "droplet:create", "firewall:read",
               "firewall:create", "firewall:update", "sizes:read", "ssh_key:read", "ssh_key:create", "tag:read", "tag:create"]
    assert [output.index(scope) for scope in ordered] == sorted(output.index(scope) for scope in ordered)
    assert "regions:read" not in output and "image:read" not in output and "block_storage:read" not in output
    assert "sk-private-wizard-key" not in output


def test_public_ipv4_detection_is_bounded_and_requires_a_global_ipv4():
    def response(body, status=200):
        return httpx.MockTransport(lambda request: httpx.Response(status, text=body))

    assert setup.current_public_ipv4(transport=response("107.3.188.125\n")) == "107.3.188.125/32"
    for body, status in (("10.0.0.1", 200), ("2001:db8::1", 200), ("invalid", 200), ("107.3.188.125", 500), ("1" * 65, 200)):
        with pytest.raises(InstallError, match="detect"):
            setup.current_public_ipv4(transport=response(body, status))


@pytest.mark.parametrize("value", ["ssh-ed25519 invalid", "ssh-rsa AAAA", "ssh-dss AAAA", "", "ssh-ed25519 !!!"])
def test_invalid_ssh_metadata_rejected(value):
    with pytest.raises(InstallError):
        setup_ssh.public_identity(value)


def test_openrouter_browser_pkce_exchange_saved_privately_and_reused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ask", lambda *args: "browser")
    challenges, calls = [], []

    def callback(challenge):
        challenges.append(challenge)
        return "single-use-authorization-code"

    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/auth/keys"
        body = json.loads(request.content)
        assert body["code_challenge_method"] == "S256"
        assert challenges == [base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).decode().rstrip("=")]
        return httpx.Response(200, json={"key": "sk-or-v1-private-browser-key"})

    path = setup_auth.openrouter_login(tmp_path, callback=callback, transport=httpx.MockTransport(handle))
    assert read_private(path) == b"sk-or-v1-private-browser-key"
    assert setup_auth.openrouter_login(tmp_path) == path and len(calls) == 1
    journal = read_document(tmp_path / "openrouter-auth.json")
    assert journal == {"format": 1, "phase": "ready"}
    assert "private-browser-key" not in capsys.readouterr().out


def require_loopback():
    try:
        with socket.socket() as connection:
            connection.bind(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("execution sandbox forbids loopback sockets; callback parser is covered by socket-free fixtures")


def test_loopback_callback_rejects_unbound_duplicate_and_foreign_host_without_logging(capsys):
    require_loopback()
    responses, threads = [], []

    def opener(url):
        query = parse_qs(urlsplit(url).query)
        callback = query["callback_url"][0]
        assert urlsplit(callback).hostname == "127.0.0.1"

        def send():
            with httpx.Client(trust_env=False, timeout=5) as client:
                responses.append(client.get(callback.replace("/callback/", "/wrong/") + "?code=bad-code").status_code)
                responses.append(client.get(callback + "?code=bad-code&code=other-code").status_code)
                responses.append(client.get(callback + "?code=bad-code", headers={"Host": "attacker.invalid"}).status_code)
                responses.append(client.get(callback + "?" + urlencode({"code": "private-valid-code"})).status_code)

        thread = threading.Thread(target=send)
        threads.append(thread)
        thread.start()

    assert setup_auth.browser_code("pkce-challenge", opener=opener, timeout=10) == "private-valid-code"
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert responses == [400, 400, 400, 200]
    assert "private-valid-code" not in capsys.readouterr().out


def test_loopback_timeout_does_not_exchange_or_print_code():
    require_loopback()
    with pytest.raises(InstallError, match="timed out"):
        setup_auth.browser_code("challenge", timeout=0.01, opener=lambda url: None)


@pytest.mark.parametrize("attack", ["none", "path", "host", "origin", "duplicate", "short", "extra"])
def test_callback_parser_with_socket_free_http_fixture(monkeypatch, capsys, attack):
    request_paths, responses = [], []

    class Connection:
        def __init__(self, body):
            self.body = body

        def makefile(self, *args):
            return io.BytesIO(self.body)

        def sendall(self, body):
            responses.append(body)

        def shutdown(self, how):
            pass

    class Listener:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 0)
            self.server_port, self.handler = 43210, handler

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def handle_request(self):
            path = request_paths.pop(0)
            host_header = "attacker.invalid" if attack == "host" and not request_paths else "127.0.0.1:43210"
            origin = "Origin: https://attacker.invalid\r\n" if attack == "origin" and not request_paths else ""
            body = f"GET {path} HTTP/1.0\r\nHost: {host_header}\r\n{origin}\r\n".encode()
            self.handler(Connection(body), ("127.0.0.1", 1), self)

    monkeypatch.setattr(setup_auth, "HTTPServer", Listener)

    def opener(url):
        callback = parse_qs(urlsplit(url).query)["callback_url"][0]
        path = urlsplit(callback).path + "?code=private-parser-code"
        attacked = {"path": path.replace("/callback/", "/wrong/"), "duplicate": path + "&code=other-code",
                    "short": path.split("?")[0] + "?code=x", "extra": path + "&unknown=ignored"}.get(attack, path)
        request_paths.append(attacked)

    if attack == "none":
        assert setup_auth.browser_code("challenge", opener=opener, timeout=0.01) == "private-parser-code"
        assert b"200 OK" in b"".join(responses)
    else:
        # Stop after the rejected callback without a real blocking listener.
        clock = iter([0, 0, 1000])
        monkeypatch.setattr(setup_auth.time, "monotonic", lambda: next(clock))
        with pytest.raises(InstallError, match="timed out"):
            setup_auth.browser_code("challenge", opener=opener, timeout=10)
        assert b"400 Bad Request" in b"".join(responses)
    assert "private-parser-code" not in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["lost", "redirect", "invalid", "oversize", "rejected"])
def test_uncertain_openrouter_exchange_cannot_remint_key(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(setup, "ask", lambda *args: "browser")
    calls = []

    def handle(request):
        calls.append(request)
        if outcome == "lost":
            raise httpx.ReadError("secret-provider-diagnostic")
        if outcome == "redirect":
            return httpx.Response(302, headers={"Location": "https://attacker.invalid"})
        if outcome == "oversize":
            return httpx.Response(200, content=b"x" * 16385)
        if outcome == "rejected":
            return httpx.Response(403, json={"error": "secret-provider-diagnostic"})
        return httpx.Response(200, json={"key": "bad\nkey"})

    with pytest.raises(InstallError, match="uncertain") as error:
        setup_auth.openrouter_login(tmp_path, callback=lambda challenge: "private-code", transport=httpx.MockTransport(handle))
    assert "secret-provider-diagnostic" not in str(error.value)
    with pytest.raises(InstallError, match="uncertain"):
        setup_auth.openrouter_login(tmp_path)
    assert len(calls) == 1 and not (tmp_path / "openrouter-key").exists()


def test_openrouter_headless_code_is_hidden_and_uses_pkce(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ask", lambda *args: "code")
    monkeypatch.setattr(setup_auth.getpass, "getpass", lambda prompt: "private-headless-code")
    urls = []
    monkeypatch.setattr(setup_auth, "open_page", urls.append)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"key": "sk-private-headless-key"}))
    setup_auth.openrouter_login(tmp_path, transport=transport)
    query = parse_qs(urlsplit(urls[0]).query)
    assert "callback_url" not in query and query["code_challenge_method"] == ["S256"]


def test_modal_browser_credentials_use_isolated_config_and_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ask", lambda *args: "browser")
    monkeypatch.setenv("MODAL_TOKEN_ID", "unrelated-account")
    monkeypatch.setenv("MODAL_SERVER_URL", "https://attacker.invalid")
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        assert args[-3:] == ["token", "new", "--verify"]
        assert kwargs["env"]["MODAL_PROFILE"] == "reasoning"
        assert "MODAL_TOKEN_ID" not in kwargs["env"] and "MODAL_SERVER_URL" not in kwargs["env"]
        assert kwargs["stderr"] == subprocess.DEVNULL
        config = Path(kwargs["env"]["MODAL_CONFIG_PATH"])
        assert config == tmp_path / "modal-browser.toml" and config.stat().st_mode & 0o777 == 0o600
        host.atomic_write(config, b'[reasoning]\nactive=true\ntoken_id="ak_private_identifier"\ntoken_secret="as_private_secret"\n')
        return SimpleNamespace(returncode=0)

    first = setup_auth.modal_login(tmp_path, runner=runner)
    assert setup_auth.modal_login(tmp_path, runner=runner) == first and len(calls) == 1
    assert read_private(first[1]) == b"as_private_secret"
    assert "as_private_secret" not in capsys.readouterr().out


def test_modal_interrupted_login_with_complete_config_recovers_without_new_token(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ask", lambda *args: "browser")

    def interrupted(args, **kwargs):
        host.atomic_write(Path(kwargs["env"]["MODAL_CONFIG_PATH"]),
                          b'[reasoning]\ntoken_id="ak_recovered_identifier"\ntoken_secret="as_recovered_secret"\n')
        return SimpleNamespace(returncode=1)

    with pytest.raises(InstallError, match="stopped"):
        setup_auth.modal_login(tmp_path, runner=interrupted)
    paths = setup_auth.modal_login(tmp_path, runner=lambda *args, **kwargs: pytest.fail("do not mint again"))
    assert read_private(paths[1]) == b"as_recovered_secret"


def test_modal_incomplete_interruption_stops_without_reminting(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ask", lambda *args: "browser")
    with pytest.raises(InstallError, match="stopped"):
        setup_auth.modal_login(tmp_path, runner=lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(InstallError, match="incomplete"):
        setup_auth.modal_login(tmp_path, runner=lambda *args, **kwargs: pytest.fail("do not mint again"))


def test_browser_login_does_not_follow_unprotected_credential_symlinks(tmp_path, monkeypatch):
    target = tmp_path / "other-key"
    write_private(target, b"sk-private-existing-key")
    (tmp_path / "openrouter-key").symlink_to(target)
    monkeypatch.setattr(setup, "ask", lambda *args: pytest.fail("symlink must fail before prompting"))
    with pytest.raises(InstallError, match="symlink"):
        setup_auth.openrouter_login(tmp_path)


def test_modal_browser_rejects_endpoint_or_unrelated_profile_in_saved_config(tmp_path):
    write_document(tmp_path / "modal-auth.json", {"format": 1, "phase": "authorizing"})
    write_private(tmp_path / "modal-browser.toml",
                  b'[reasoning]\ntoken_id="ak_identifier"\ntoken_secret="as_secret"\nserver_url="https://attacker.invalid"\n')
    with pytest.raises(InstallError, match="incomplete or changed"):
        setup_auth.modal_login(tmp_path, runner=lambda *args, **kwargs: pytest.fail("do not mint again"))


def test_release_bundle_distributes_catalog_and_private_copy_checks_identity(builder_inputs, tmp_path):  # noqa: F811
    source, wheel, frontend = builder_inputs
    catalog = source / "approved.json"
    catalog.write_text(json.dumps([IMAGE]))
    output = source / "catalog-release.tar.gz"
    release.build_bundle(source, wheel, frontend, output, allow_dirty=True, compute_catalog=catalog)
    manifest, _, _ = host.inspect_bundle(output)
    state = tmp_path / "operator"
    state.mkdir(mode=0o700)
    release.save_release_catalog(output, manifest, state)
    saved = state / "release-compute-images.json"
    assert read_private(saved) == release.approved_catalog(catalog.read_bytes())
    release.save_release_catalog(output, manifest, state)
    host.atomic_write(saved, b"changed")
    with pytest.raises(InstallError, match="changed"):
        release.save_release_catalog(output, manifest, state)


@pytest.mark.parametrize("value", [[], [IMAGE, IMAGE], [{**IMAGE, "reference": "mutable:latest"}], "bad", [None]])
def test_release_catalog_refuses_empty_mutable_or_invalid_records(value):
    with pytest.raises(InstallError):
        release.approved_catalog(json.dumps(value).encode())


def test_release_catalog_rejects_tampered_payload_and_unadvertised_copy(tmp_path, payload):  # noqa: F811
    payload["compute-images.json"] = release.approved_catalog(json.dumps([IMAGE]).encode())
    bundle = pack(tmp_path / "catalog.tar.gz", payload)
    manifest, _, _ = host.inspect_bundle(bundle)
    payload["compute-images.json"] = release.approved_catalog(json.dumps([{**IMAGE, "name": "changed"}]).encode())
    pack(bundle, payload)
    with pytest.raises(InstallError, match="release changed"):
        release.save_release_catalog(bundle, manifest, tmp_path)
    write_private(tmp_path / "release-compute-images.json", b"[]")
    with pytest.raises(InstallError, match="absent"):
        release.save_release_catalog(bundle, {"files": {}}, tmp_path)


def test_modal_selection_uses_bundled_catalog_without_image_path_prompt(tmp_path, monkeypatch):
    write_private(tmp_path / "release-compute-images.json", release.approved_catalog(json.dumps([IMAGE]).encode()))
    write_private(tmp_path / "modal-token-id", b"ak_fixture_identifier")
    write_private(tmp_path / "modal-token-secret", b"as_fixture_secret")
    monkeypatch.setattr(setup, "ask", lambda prompt, default=None: str(default) if default is not None else pytest.fail(prompt))
    value = modal_setup.collect_modal(tmp_path, "a" * 64)
    assert value["images"] == [IMAGE]
