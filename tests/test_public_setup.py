"""Interactive approvals and actual local API state; no paid cloud/TLS/LLM acceptance."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_public_provisioning import ProviderFixture
from test_public_server import PASSWORD, installation  # noqa: F401

from reasoning_ledger.deployment import cli, operator_recovery, setup
from reasoning_ledger.deployment.digitalocean import ProviderUpdateRejected
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import read_document, write_document, write_private
from reasoning_ledger.deployment.provision import save_plan
from reasoning_ledger.deployment.setup_provider import configure_openrouter
from reasoning_ledger.server.store import StoreError


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    state = tmp_path / "operator"
    token, identity, router = (tmp_path / name for name in ("token", "identity", "router"))
    for path, body in ((token, b"x" * 40), (identity, b"nonfunctional-key-fixture"), (router, b"private-openrouter-fixture")):
        write_private(path, body)
    config = {
        "format": 1, "release_id": "a" * 64, "public_url": "https://chat.example.org", "zone": "example.org", "name": "reasoning-test",
        "region": "fra1", "size": "s-2vcpu-4gb", "ssh_key_id": 123, "identity": str(identity), "admin_cidr": "203.0.113.10/32",
        "maximum_hourly_usd": "0.10", "token_file": str(token), "models": ["test/model"], "monthly_budget_microusd": 2000000,
        "openrouter_file": str(router),
    }
    monkeypatch.setattr(setup, "inspect_bundle", lambda bundle: ({"dirty": False}, config["release_id"], "b" * 64))
    monkeypatch.setattr(setup, "collect_configuration", lambda directory, release, **kwargs: config)
    calls, approvals, providers = [], [], []
    active = {
        "delegated": True,
        "ready": True,
        "ssh_closed": True,
        "cloudflare_parent": False,
        "ssh_update_rejected": False,
    }
    monkeypatch.setattr(setup, "wait_for_dns_delegation", lambda zone, initial, check: initial)
    monkeypatch.setattr(
        operator_recovery,
        "protect_deployment_locked",
        lambda state, command, **kwargs: {
            "format": 1,
            "mode": "second_operator",
            "handoff_sha256": "d" * 64,
            "operator_public_key_sha256": "e" * 64,
        },
    )

    def approve(prompt, phrase):
        approvals.append(phrase)

    monkeypatch.setattr(setup, "approve", approve)
    monkeypatch.setattr(setup, "ask", lambda prompt: "owner@example.org" if prompt == "Owner email" else "Owner")
    # Pending DNS defaults to a manual, headless handoff in this shared fixture.
    answers = iter(["manual", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    def command(arguments):
        name = arguments[0]
        calls.append(name)

        def arg(flag):
            return arguments[arguments.index(flag) + 1]

        if name == "do-dns-setup":
            Path(arg("--state-dir")).mkdir(mode=0o700, exist_ok=True)
            write_document(Path(arg("--state-dir")) / "digitalocean-dns.json", {"zone": arg("--zone")})
            value = {"zone_ready": True}
        elif name == "do-dns-check":
            parent = arg("--zone") == config["zone"]
            value = {
                "delegation_verified": active["delegated"] and not active["cloudflare_parent"],
                "nameservers": ["ns1.digitalocean.com"],
                "observed_nameservers": ["sue.ns.cloudflare.com", "terin.ns.cloudflare.com"]
                if active["cloudflare_parent"] and parent else [],
            }
        elif name == "do-plan":
            fixture = ProviderFixture()
            try:
                plan = fixture.client.plan(name=config["name"], region=config["region"], size=config["size"], origin=config["public_url"],
                                           dns_zone=arg("--dns-zone"), ssh_key_id=123, admin_cidr=config["admin_cidr"],
                                           maximum_hourly_usd=config["maximum_hourly_usd"], pin_ssh_host_key=True)
                value = save_plan(Path(arg("--output")), plan)
            finally:
                fixture.client.close()
        elif name == "owner-input":
            write_document(Path(arg("--output")), {"fixture": True}, new=True)
            value = {"owner_file": arg("--output")}
        elif name == "do-launch":
            assert arg("--confirm-release") == config["release_id"]
            assert arg("--ssh-cidr") == config["admin_cidr"]
            value = {"https_ready": active["ready"], "owner_setup": {"organization_id": "c" * 32}}
        elif name == "do-ssh-access":
            if active["ssh_update_rejected"]:
                active["ssh_update_rejected"] = False
                raise ProviderUpdateRejected("DigitalOcean rejected the firewall update; check firewall:update access")
            value = {"phase": "closed" if active["ssh_closed"] else "closing", "ssh_cidr": None}
        else:
            raise AssertionError(name)
        print(json.dumps(value))
        return 0

    def provider(*args):
        providers.append(args)

    return state, config, calls, approvals, active, providers, command, provider


def run(wizard):
    state, _, _, _, _, _, command, provider = wizard
    return setup.run_setup(Path("fixture-bundle"), state, command, configure_provider=provider,
                           detect_public_ipv4=lambda: "203.0.113.10/32")


def test_wizard_quotes_and_confirms_before_launch_then_reuses_original_state(wizard, capsys):
    state, _, calls, approvals, _, providers, _, _ = wizard
    result = run(wizard)
    assert result["phase"] == "organization_ready" and result["live_user_journey_verified"] is False
    assert result["operator_protection"]["mode"] == "second_operator"
    assert approvals == ["example.org", "launch https://chat.example.org"]
    original_plan = (state / "plan.json").read_bytes()
    run(wizard)
    assert (state / "plan.json").read_bytes() == original_plan
    assert calls.count("owner-input") == calls.count("do-plan") == 1
    assert calls.count("do-launch") == 1 and len(providers) == 1
    assert (state / "setup-completion.json").exists()
    output = capsys.readouterr().out
    assert "$0.035/hour" in output and "not an invoice cap" in output
    assert "private-openrouter-fixture" not in output and "x" * 40 not in output


def test_explicit_operator_protection_deferral_marks_setup_unprotected(wizard):
    state, _, _, _, _, _, command, provider = wizard
    result = setup.run_setup(
        Path("fixture-bundle"),
        state,
        command,
        configure_provider=provider,
        protect_operator=lambda *_args: {"format": 1, "mode": "deferred"},
        detect_public_ipv4=lambda: "203.0.113.10/32",
    )

    assert result["phase"] == "organization_ready_unprotected"
    assert result["operator_protection"] == {"format": 1, "mode": "deferred"}


def test_equivalent_hourly_budget_spelling_matches_saved_plan(wizard):
    state, config, _, _, _, _, _, _ = wizard
    config["maximum_hourly_usd"] = ".05"

    assert run(wizard)["phase"] == "organization_ready"
    assert read_document(state / "setup.json")["maximum_hourly_usd"] == ".05"
    assert read_document(state / "plan.json")["plan"]["quote"]["maximum_hourly_usd"] == "0.05"


def test_missing_delegation_stops_before_owner_or_paid_resources(wizard):
    _, _, calls, approvals, active, providers, _, _ = wizard
    active["delegated"] = False
    assert run(wizard)["phase"] == "dns_delegation_required"
    assert calls == ["do-dns-setup", "do-dns-check"] and providers == []
    assert approvals == ["example.org"]


def test_dns_wait_continues_the_same_run_when_delegation_appears(wizard, monkeypatch, capsys):
    _, _, calls, _, active, _, _, _ = wizard
    active["delegated"] = False

    def wait(_zone, _initial, check):
        active["delegated"] = True
        return check()

    monkeypatch.setattr(setup, "wait_for_dns_delegation", wait)

    assert run(wizard)["phase"] == "organization_ready"
    assert calls.count("do-dns-check") == 2 and calls.count("do-launch") == 1
    assert "Resume after DNS propagates" not in capsys.readouterr().out


def test_dns_wait_polls_with_a_spinner_until_all_nameservers_appear(capsys):
    receipts = iter([
        {"delegation_verified": False, "observed_nameservers": []},
        {"delegation_verified": False, "observed_nameservers": ["ns1.digitalocean.com"]},
        {"delegation_verified": True, "observed_nameservers": [
            "ns1.digitalocean.com", "ns2.digitalocean.com", "ns3.digitalocean.com",
        ]},
    ])
    sleeps = []

    result = setup.wait_for_dns_delegation(
        "chat.example.org",
        {"delegation_verified": False},
        lambda: next(receipts),
        attempts=3,
        interval=7,
        sleep=sleeps.append,
    )

    assert result["delegation_verified"] is True and sleeps == [7, 7]
    assert "Public DNS now delegates chat.example.org" in capsys.readouterr().out


def test_cloudflare_parent_switches_to_chat_hostname_without_recollecting_credentials(wizard, monkeypatch, capsys):
    state, config, calls, approvals, active, providers, _, _ = wizard
    state.mkdir(mode=0o700)
    write_document(state / "setup.json", config, new=True)
    dns_state = state / "dns"
    dns_state.mkdir(mode=0o700)
    write_document(dns_state / "digitalocean-dns.json", {"zone": config["zone"]}, new=True)
    write_document(state / "registrar-guide.json", {"format": 1, "release_id": config["release_id"],
                                                     "zone": config["zone"], "registrar": "cloudflare"}, new=True)
    active.update(delegated=False, cloudflare_parent=True)
    answers = iter(["yes", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    result = run(wizard)

    assert result["phase"] == "dns_delegation_required" and providers == []
    assert read_document(state / "setup.json")["zone"] == "chat.example.org"
    selection = read_document(state / "dns-delegation.json")
    assert selection["parent_zone"] == "example.org" and selection["zone"] == "chat.example.org"
    assert read_document(dns_state / "digitalocean-dns.json")["zone"] == "example.org"
    child_states = [path for path in state.glob("dns-*") if path.is_dir()]
    assert len(child_states) == 1 and read_document(child_states[0] / "digitalocean-dns.json")["zone"] == "chat.example.org"
    assert approvals == ["chat.example.org"] and "do-plan" not in calls
    output = capsys.readouterr().out
    assert "Keep on Cloudflare" in output and "chat" in output and "NS" in output


def test_registrar_handoff_retries_then_continues_only_after_dns_matches(wizard, monkeypatch, capsys):
    from reasoning_ledger.deployment import registrar_guides

    state, _, calls, _, active, providers, _, _ = wizard
    active["delegated"] = False
    answers = iter(["porkbun", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(registrar_guides, "open_page", lambda url: pytest.fail("browser declined"))
    assert run(wizard)["phase"] == "dns_delegation_required"
    guide = (state / "registrar-guide.json").read_bytes()
    assert run(wizard)["phase"] == "dns_delegation_required"
    assert (state / "registrar-guide.json").read_bytes() == guide
    assert calls == ["do-dns-setup", "do-dns-check"] * 2 and providers == []
    assert not (state / "owner.json").exists() and not (state / "plan.json").exists()
    output = capsys.readouterr().out
    assert "Domain Management > Details" in output and "ns3.digitalocean.com" in output
    active["delegated"] = True
    assert run(wizard)["phase"] == "organization_ready"
    assert calls.count("do-plan") == calls.count("do-launch") == 1


def test_launch_denial_preserves_inputs_and_cannot_create_resources(wizard, monkeypatch):
    state, _, calls, _, _, providers, _, _ = wizard

    def deny_launch(prompt, phrase):
        if phrase.startswith("launch "):
            raise InstallError("not confirmed")

    monkeypatch.setattr(setup, "approve", deny_launch)
    with pytest.raises(InstallError, match="not confirmed"):
        run(wizard)
    assert "do-launch" not in calls and providers == []
    assert (state / "plan.json").exists() and (state / "owner.json").exists()


def test_pending_launch_does_not_configure_provider_and_can_resume(wizard):
    _, _, calls, _, active, providers, _, _ = wizard
    active["ready"] = False
    assert run(wizard)["https_ready"] is False and providers == []
    active["ready"] = True
    assert run(wizard)["phase"] == "organization_ready"
    assert calls.count("do-plan") == 1 and calls.count("owner-input") == 1


def test_pending_ssh_closure_resumes_without_relaunch_or_reconfiguration(wizard):
    state, _, calls, _, active, providers, _, _ = wizard
    active["ssh_closed"] = False
    assert run(wizard)["phase"] == "ssh_access_closing"
    assert (state / "setup-completion.json").exists()
    active["ssh_closed"] = True
    assert run(wizard)["phase"] == "organization_ready"
    assert calls.count("do-launch") == 1 and len(providers) == 1


def test_missing_firewall_update_scope_replaces_token_without_relaunch(wizard, monkeypatch):
    state, config, calls, _, active, providers, _, _ = wizard
    replacement = state.parent / "replacement-token"
    write_private(replacement, b"r" * 40)
    active["ssh_update_rejected"] = True
    expected_accounts = []

    def replace(directory, account):
        assert directory == state
        expected_accounts.append(account)
        return replacement

    monkeypatch.setattr(setup, "replacement_digitalocean_token", replace)

    assert run(wizard)["phase"] == "organization_ready"
    assert expected_accounts == ["user:" + "a" * 40]
    assert read_document(state / "setup.json")["token_file"] == str(replacement)
    assert calls.count("do-launch") == 1 and calls.count("do-ssh-access") == 3
    assert len(providers) == 1 and config["token_file"] != str(replacement)


def test_replacement_token_is_private_and_bound_to_the_original_account(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    token = "dop_v1_" + "1" * 64
    opened = []
    closed = []
    answers = iter(["no", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: token)
    monkeypatch.setattr("reasoning_ledger.deployment.setup_auth.open_page", opened.append)
    monkeypatch.setattr(setup, "DigitalOcean", lambda value: type("Account", (), {
        "identity": lambda self: "team:original-account",
        "close": lambda self: closed.append(value),
    })())

    path = setup.replacement_digitalocean_token(state, "team:original-account")

    assert path == state / "digitalocean-token-2"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text(encoding="ascii") == token
    assert opened == [] and closed == [token]
    assert token not in capsys.readouterr().out


def test_wrong_release_cannot_rebind_setup(wizard, monkeypatch):
    _, _, calls, _, _, _, _, _ = wizard
    run(wizard)
    calls.clear()
    monkeypatch.setattr(setup, "inspect_bundle", lambda bundle: ({"dirty": False}, "f" * 64, "b" * 64))
    with pytest.raises(InstallError, match="original release"):
        run(wizard)
    assert calls == []


def test_dirty_release_and_noninteractive_cli_do_not_prompt_or_launch(wizard, monkeypatch):
    monkeypatch.setattr(setup, "inspect_bundle", lambda bundle: ({"dirty": True}, "a" * 64, "b" * 64))
    with pytest.raises(InstallError, match="committed clean"):
        run(wizard)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as error:
        cli.main(["setup", "--bundle", "fixture"])
    assert error.value.code == 2


def test_private_setup_inputs_cannot_be_created_inside_a_checkout(wizard):
    state, _, calls, _, _, _, _, _ = wizard
    (state.parent / ".git").mkdir()
    (state.parent / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    with pytest.raises(InstallError, match="outside Git"):
        run(wizard)
    assert calls == [] and not state.exists()


def test_empty_confirmation_and_invalid_budgets_are_not_consent(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(InstallError, match="not confirmed"):
        setup.approve("Approve", "launch")
    for value in ("0", "-1", "NaN", "Infinity", "0.0000001"):
        with pytest.raises(InstallError):
            setup.dollar_budget(value)
    assert setup.dollar_budget("2.50") == 2500000


def test_confirmation_asks_for_hostname_without_scheme_and_keeps_exact_target(monkeypatch):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "prepare upgrade chat.example.org")
    setup.approve("Review https://chat.example.org", "prepare upgrade https://chat.example.org")
    assert "Type 'prepare upgrade chat.example.org'" in prompts[0]
    monkeypatch.setattr("builtins.input", lambda _: "prepare upgrade other.example.org")
    with pytest.raises(InstallError, match="not confirmed"):
        setup.approve("Review", "prepare upgrade https://chat.example.org")


def test_credentials_are_prompted_privately_and_budget_inputs_are_saved_without_values(tmp_path, monkeypatch, capsys):
    identity = tmp_path / "identity"
    write_private(identity, b"key-fixture")
    answers = iter(["n", "", "example.org", "", "", "", "", "0.10", "2.50", "manual", ""])
    token = "dop_v1_" + "0" * 64
    secrets = iter([token, "secret-router-key"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: next(secrets))
    monkeypatch.setattr("reasoning_ledger.deployment.setup_ssh.prepare_identity", lambda directory: (identity, "fixture-public"))
    monkeypatch.setattr("reasoning_ledger.deployment.setup_ssh.enroll_public_key", lambda *args: 123)
    monkeypatch.setattr(setup, "DigitalOcean", lambda token: type("Account", (), {
        "identity": lambda self: "account", "pages": lambda self, *args: [{"id": 123, "name": "Operator"}], "close": lambda self: None,
    })())
    config = setup.collect_configuration(tmp_path, "a" * 64, detect_public_ipv4=lambda: "203.0.113.10/32")
    setup.validate_configuration(config, "a" * 64)
    assert config["monthly_budget_microusd"] == 2500000
    assert config["ssh_key_id"] == 123 and config["public_url"] == "https://chat.example.org"
    assert "secret-router-key" not in json.dumps(config)
    assert token not in capsys.readouterr().out


@pytest.fixture
def organization_provider(installation, tmp_path):  # noqa: F811
    _, store, client, initial = installation
    key = tmp_path / "router-key"
    write_private(key, b"secret-router-key")
    config = {"public_url": "https://chat.example.org", "openrouter_file": str(key), "models": ["test/model"],
              "monthly_budget_microusd": 2000000}
    owner = {"setup_id": "b" * 32, "email": "owner@example.org", "password": PASSWORD}
    tokens, posts = [], []
    mode = {"failure": None}

    def handle(request):
        if request.method == "POST" and request.url.path.endswith("provider-keys"):
            posts.append(request)
            if mode["failure"] == "before":
                raise httpx.ReadError("private transport text")
        response = client.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
        if request.url.path == "/api/auth/login":
            tokens.append(response.json()["access_token"])
        if request.method == "POST" and request.url.path.endswith("provider-keys") and mode["failure"] == "after":
            raise httpx.ReadError("private transport text")
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    def configure():
        return configure_openrouter(config, owner, initial["organization_id"], tmp_path, transport=httpx.MockTransport(handle))

    return store, initial, config, configure, tokens, posts, mode


def test_actual_policy_encrypted_key_and_session_revocation_are_replay_safe(organization_provider):
    store, initial, config, configure, tokens, posts, _ = organization_provider
    configure()
    configure()
    assert store.policy(initial["user_id"], initial["organization_id"]) == {
        "allowed_models": config["models"], "monthly_budget_microusd": config["monthly_budget_microusd"],
    }
    keys = store.keys(initial["user_id"], initial["organization_id"])
    assert len(keys) == len(posts) == 1
    assert keys[0]["fingerprint"] == hashlib.sha256(b"secret-router-key").hexdigest()[:16]
    assert "secret-router-key" not in json.dumps(keys)
    for token in tokens:
        with pytest.raises(StoreError):
            store.session(token)


@pytest.mark.parametrize("failure", ["before", "after"])
def test_lost_key_creation_reply_reconciles_without_duplicate_posts(organization_provider, failure):
    _, _, _, configure, _, posts, mode = organization_provider
    mode["failure"] = failure
    with pytest.raises(InstallError, match="reconcile"):
        configure()
    mode["failure"] = None
    if failure == "after":
        configure()
    else:
        with pytest.raises(InstallError, match="reconcile"):
            configure()
    assert len(posts) == 1


def test_later_administrator_policy_and_key_revocation_are_not_overwritten(organization_provider):
    store, initial, _, configure, _, posts, _ = organization_provider
    configure()
    store.set_policy(initial["user_id"], initial["organization_id"], [], 0)
    with pytest.raises(InstallError, match="administrator edits"):
        configure()
    assert len(posts) == 1 and store.policy(initial["user_id"], initial["organization_id"])["monthly_budget_microusd"] == 0


DO_TOKEN = "dop_v1_" + "6" * 64


@pytest.mark.parametrize("value,message", [
    (DO_TOKEN + DO_TOKEN, "pasted more than once"),
    (DO_TOKEN + "\n" + DO_TOKEN, "pasted more than once"),
    ("dop_v1_" + "6" * 63, "71 characters"),
    ("dop_v1_" + "G" * 64, "does not look like"),
    ("dop_v1_" + "6" * 30 + " " + "6" * 34, "spaces"),
    ("dor_v1_" + "6" * 64, "refresh token"),
    ("x" * 40, "does not look like"),
    ("tökén" * 10, "non-ASCII"),
])
def test_digitalocean_token_shape_is_checked_locally_without_repeating_it(value, message):
    with pytest.raises(InstallError, match=message) as error:
        setup.digitalocean_token(value.encode("utf-8"))
    assert "No API call was made" in str(error.value)
    assert value not in str(error.value) and DO_TOKEN not in str(error.value)


def test_digitalocean_token_accepts_current_and_legacy_shapes():
    assert setup.digitalocean_token((" " + DO_TOKEN + "\n").encode()) == DO_TOKEN
    assert setup.digitalocean_token("doo_v1_" + "a" * 64) == "doo_v1_" + "a" * 64
    assert setup.digitalocean_token("f" * 64) == "f" * 64


def test_hidden_token_entry_rejects_a_doubled_paste_before_saving_or_calling_the_api(tmp_path, monkeypatch, capsys):
    secrets = iter([DO_TOKEN + DO_TOKEN, DO_TOKEN])
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: next(secrets))
    path = setup.private_credential(tmp_path, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                    validate=setup.validate_digitalocean_token)
    assert path.read_text() == DO_TOKEN and path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "pasted more than once" in output and "Nothing was saved" in output and DO_TOKEN not in output

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: DO_TOKEN + DO_TOKEN)
    with pytest.raises(InstallError, match="pasted more than once"):
        setup.private_credential(other, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                 validate=setup.validate_digitalocean_token)
    assert not (other / "digitalocean-token").exists()


def test_token_file_and_saved_token_shapes_are_checked(tmp_path, monkeypatch):
    doubled = tmp_path / "doubled-token"
    write_private(doubled, (DO_TOKEN + DO_TOKEN).encode())
    monkeypatch.setattr("builtins.input", lambda prompt: str(doubled))
    with pytest.raises(InstallError, match="pasted more than once"):
        setup.private_credential(tmp_path, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                 validate=setup.validate_digitalocean_token)
    write_private(tmp_path / "digitalocean-token", b"x" * 40)
    with pytest.raises(InstallError, match="saved credential .*digitalocean-token is unusable.*move it aside"):
        setup.private_credential(tmp_path, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                 validate=setup.validate_digitalocean_token)


@pytest.mark.parametrize("pasted", [DO_TOKEN, DO_TOKEN + DO_TOKEN, "f" * 64, "sk-or-v1-" + "a" * 64])
def test_secret_pasted_at_a_visible_prompt_is_refused_and_not_repeated(tmp_path, monkeypatch, capsys, pasted):
    monkeypatch.setattr(setup, "_EXPOSED_SECRETS", set())
    answers = iter([pasted, "no", ""])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or next(answers))
    monkeypatch.setattr("reasoning_ledger.deployment.setup_auth.open_page", lambda url: pytest.fail("browser opened"))
    fresh = "dop_v1_" + "7" * 64
    secrets = iter([DO_TOKEN, fresh])
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: next(secrets))
    accounts = []
    monkeypatch.setattr(setup, "DigitalOcean", lambda value: accounts.append(value) or type("Account", (), {
        "identity": lambda self: "team:original-account", "close": lambda self: None,
    })())
    path = setup.replacement_digitalocean_token(tmp_path, "team:original-account")
    output = capsys.readouterr().out
    assert "Secret pasted into a visible prompt" in output and "revoke it" in output
    assert pasted not in output and DO_TOKEN not in output
    assert prompts[0] == prompts[1] and "[Y/n]" in prompts[0]
    if pasted.startswith("dop_v1_"):
        # The exposed token is refused at the hidden prompt too; a new one is required.
        assert "displayed in the terminal" in output
        assert path.read_text() == fresh
    else:
        assert path.read_text() == DO_TOKEN
    assert accounts == [path.read_text()]


def test_secret_pasted_as_a_credential_file_path_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "_EXPOSED_SECRETS", set())
    answers = iter([DO_TOKEN, ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "dop_v1_" + "8" * 64)
    path = setup.private_credential(tmp_path, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                                    validate=setup.validate_digitalocean_token)
    assert path.read_text() == "dop_v1_" + "8" * 64
    output = capsys.readouterr().out
    assert "Secret pasted into a visible prompt" in output and DO_TOKEN not in output


def test_yes_no_prompt_repeats_until_answered_and_checksum_answers_are_not_secrets(monkeypatch):
    answers = iter(["maybe", "", "N"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert setup.confirm("Open?") is True
    assert setup.confirm("Open?") is False
    monkeypatch.setattr("builtins.input", lambda prompt: "a" * 64)
    assert setup.ask("Handoff archive checksum") == "a" * 64
