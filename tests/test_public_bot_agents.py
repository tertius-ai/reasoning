"""Personal CLI pairing, invocation isolation and crash-safe bot replies.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import asyncio
import json
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import httpx
import pytest
from test_public_bots import fixture as bot_fixture

from reasoning_ledger.deployment.private import locked_directory, read_document, write_document
from reasoning_ledger.server.api import Config, create_app
from reasoning_ledger.server.bot_agent import agent_arguments, agent_step, connect, invoke, parse_result
from reasoning_ledger.server.bot_client import BotClientError
from reasoning_ledger.server.bot_pairing import BotPairing
from reasoning_ledger.server.security import digest
from reasoning_ledger.server.store import StoreError

DEVICE, CODE, CREDENTIAL, SESSION = "a" * 43, "ABCDEFGH2345", "rnb_" + "b" * 43, "a" * 32


@pytest.fixture
def fixture(tmp_path):
    return bot_fixture.__wrapped__(tmp_path)


def test_pairing_hashes_credentials_and_approves_idempotently(fixture):
    store, owner, topics, _, _, bots = fixture
    pairing = BotPairing(bots)
    inputs = (DEVICE, CODE, digest(CREDENTIAL), "Codex", "codex")
    assert pairing.start(*inputs) == pairing.start(*inputs)
    assert pairing.status(DEVICE) == {"status": "pending"}
    assert pairing.info(CODE)["engine"] == "codex"
    approval = (owner["user_id"], owner["organization_id"], CODE, topics[:1], ["messages:read"], 3600)
    result = pairing.approve(*approval)
    assert "token" not in result and result["delegation_kind"] == "personal"
    assert pairing.approve(*approval) == result
    assert pairing.status(DEVICE)["bot"]["id"] == bots.identity(CREDENTIAL)["id"]
    with store.transaction() as db:
        assert CREDENTIAL not in str(tuple(db.execute("SELECT * FROM bot_pairings").fetchone()))
    with pytest.raises(StoreError) as error:
        pairing.approve(owner["user_id"], owner["organization_id"], CODE, topics[1:2], ["messages:read"], 3600)
    assert error.value.status == 409
    bots.revoke(owner["user_id"], owner["organization_id"], result["id"], personal=True)
    with pytest.raises(StoreError):
        pairing.status(DEVICE)


def test_pairing_expiry_and_rate_limits(fixture):
    store, _, _, _, _, bots = fixture
    pairing = BotPairing(bots)
    pairing.start(DEVICE, CODE, digest(CREDENTIAL), "Claude", "claude")
    with store.transaction(write=True) as db:
        db.execute("UPDATE bot_pairings SET expires_at=0")
    with pytest.raises(StoreError) as error:
        pairing.info(CODE)
    assert error.value.status == 404
    for _ in range(30):
        pairing.meter("peer")
    with pytest.raises(StoreError) as error:
        pairing.meter("peer")
    assert error.value.status == 429


def test_pairing_quota_cleanup_does_not_clear_longer_login_limits(fixture):
    store, _, _, _, _, bots = fixture
    with store.transaction(write=True) as db:
        db.execute("INSERT INTO auth_attempts VALUES(?,?)", ("email:protected-signin", int(time.time()) - 120))
    BotPairing(bots).meter("peer")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope='email:protected-signin'").fetchone()[0] == 1


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_cli_permissions_and_explicit_sessions(engine):
    minimal = agent_arguments(engine, Path("/workspace"))
    enabled = agent_arguments(engine, Path("/workspace"), True, SESSION)
    assert not any("dangerously" in arg for arg in minimal + enabled)
    assert "--last" not in enabled and "--continue" not in enabled and SESSION in enabled
    if engine == "codex":
        assert "read-only" in minimal and "features.shell_tool=false" in minimal
        assert "workspace-write" in enabled and "never" in enabled
    else:
        assert "--restricted" in minimal and "--safe-mode" not in minimal and "dontAsk" in enabled
        assert minimal[minimal.index("--tools") + 1] == ""
    with pytest.raises(BotClientError):
        agent_arguments(engine, Path("/workspace"), session="--last")


def test_codex_waits_for_required_gateway_instead_of_starting_without_tools():
    gateway = Path("/private/invocation/gateway.json")
    args = agent_arguments("codex", Path("/workspace"), gateway=gateway)
    configuration = next(tomllib.loads(arg) for arg in args if arg.startswith("mcp_servers="))

    assert set(configuration["mcp_servers"]) == {"reasoning"}
    reasoning = configuration["mcp_servers"]["reasoning"]
    assert reasoning["required"] is True
    assert reasoning["startup_timeout_sec"] == 30
    assert reasoning["args"] == ["-I", "-m", "reasoning_ledger.server.bot_gateway", str(gateway)]
    assert "--ignore-user-config" in args and "--ephemeral" in args


def test_completed_cli_protocols_and_failures():
    events = [
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
        {"type": "turn.completed"},
    ]
    assert parse_result("codex", "\n".join(json.dumps(e) for e in events)) == {"text": "Answer", "session": SESSION}
    assert parse_result("claude", json.dumps({"result": "Answer", "session_id": SESSION}))["text"] == "Answer"
    for engine, output in (("codex", '{"type":"turn.failed"}'), ("claude", '{"is_error":true,"result":"failed"}')):
        with pytest.raises(BotClientError):
            parse_result(engine, output)


def test_owner_mentions_journal_before_invocation_and_replay_only_posts(tmp_path):
    bot_id, owner_id, topic = "a" * 32, "b" * 32, "c" * 32
    binding = {
        "server": "https://example.org",
        "bot_id": bot_id,
        "owner_id": owner_id,
        "engine": "codex",
        "workspace": str(tmp_path / "workspace"),
        "local_tools": False,
        "allow_other_users": False,
    }
    tmp_path.chmod(0o700)
    journal = tmp_path / "runner.json"
    calls, posts = [], []

    class Client:
        fail = True

        def messages(self, after, limit):
            return [
                message
                for message in (
                    {
                        "id": "d" * 32,
                        "sequence": 1,
                        "topic_id": topic,
                        "author_kind": "bot",
                        "author_id": bot_id,
                        "content": f"@[{bot_id}]",
                    },
                    {
                        "id": "e" * 32,
                        "sequence": 2,
                        "topic_id": topic,
                        "author_kind": "human",
                        "author_id": "f" * 32,
                        "content": f"@[{bot_id}]",
                    },
                    {
                        "id": "f" * 32,
                        "sequence": 3,
                        "topic_id": topic,
                        "author_kind": "human",
                        "author_id": owner_id,
                        "content": f"@[{bot_id}] Question",
                    },
                )
                if message["sequence"] > after
            ]

        def post(self, topic_id, content, client_id):
            posts.append((topic_id, content, client_id))
            if self.fail:
                self.fail = False
                raise BotClientError("lost receipt")

    def runner(*args):
        assert read_document(journal)["pending"]["phase"] == "executing" and args[3] is False
        calls.append(args)
        return {"text": "Answer", "session": SESSION}

    client = Client()
    with pytest.raises(BotClientError):
        agent_step(client, journal, binding, runner=runner)
    assert len(calls) == 1 and read_document(journal)["pending"]["phase"] == "reply_ready"
    assert agent_step(client, journal, binding, runner=runner)["cursor"] == 3
    assert posts[0] == posts[1] and len(calls) == 1
    assert read_document(journal)["sessions"][topic] == SESSION


def test_unknown_outcome_requires_explicit_recovery(tmp_path):
    tmp_path.chmod(0o700)
    journal = tmp_path / "runner.json"
    binding = {"bot_id": "a" * 32}
    write_document(journal, {"format": 1, "binding": binding, "cursor": 0, "sessions": {}, "pending": {"phase": "executing"}})
    with pytest.raises(BotClientError, match="unknown outcome"):
        agent_step(None, journal, binding)


def test_recovery_inspection_is_read_only_and_skip_archives_evidence(tmp_path, monkeypatch, capsys):
    from reasoning_ledger.server.bot_client import main

    tmp_path.chmod(0o700)
    journal = tmp_path / "runner.json"
    secret = "retained-private-session-and-credential"
    state = {"format": 1, "binding": {"credential": secret}, "cursor": 0, "sessions": {},
             "pending": {"phase": "executing", "sequence": 3, "message_id": "a" * 32, "text": secret}}
    write_document(journal, state)
    original = journal.read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("inspection must not prompt"))
    assert main(["recover", "--state-dir", str(tmp_path), "--action", "inspect"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["status"] == "owner_review_required" and "--action inspect" in value["inspect_command"]
    assert secret not in json.dumps(value) and journal.read_bytes() == original
    monkeypatch.setattr("builtins.input", lambda _: "skip")
    assert main(["recover", "--state-dir", str(tmp_path), "--action", "skip"]) == 0
    archive, = tmp_path.glob("recovered-" + "a" * 32 + "-*.json")
    assert read_document(archive) == state
    assert read_document(journal)["pending"] is None
    assert "make bot-run" in capsys.readouterr().out


def test_active_topic_probe_refuses_removed_grant_or_respond_scope():
    from reasoning_ledger.server.bot_agent import probe_access

    class Client:
        def identity(self):
            return self.value

    client = Client()
    client.value = {"scopes": ["messages:read", "messages:write"], "topic_ids": ["topic"]}
    probe_access(client, "topic")
    client.value["topic_ids"] = ["other-topic"]
    with pytest.raises(BotClientError, match="active topic"):
        probe_access(client, "topic")
    client.value = {"scopes": ["messages:read"], "topic_ids": ["topic"]}
    with pytest.raises(BotClientError, match="active topic"):
        probe_access(client, "topic")


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_real_subprocess_uses_stdin_not_shell_interpolation(tmp_path, engine):
    executable = tmp_path / "fixture-cli"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "bot_cli.py", executable)
    executable.chmod(0o700)
    activities = []
    result = invoke(engine, tmp_path, "Human message $(touch /tmp/never-execute)", executable=executable, deadline=5,
                    progress=activities.append)
    assert result["session"] == SESSION and result["text"].startswith("Fixture")
    assert activities == ["reading_topic"]


def test_activity_parser_ignores_malformed_payloads_and_never_forwards_free_text():
    from reasoning_ledger.server.bot_activity import cli_activity

    for event in [None, [], {"type": "assistant", "message": None}, {"type": "assistant", "message": {"content": None}}]:
        assert cli_activity(event) is None
    assert cli_activity({"type": "item.started", "item": {"type": "mcp_tool_call", "tool": None}}) == "using_tools"
    assert cli_activity({"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE REASONING"}}) == "thinking"


def test_pairing_api_requires_human_csrf_approval_and_scoped_mentions(fixture, monkeypatch):
    import fastapi.dependencies.utils
    import fastapi.routing

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.dependencies.utils, "run_in_threadpool", inline)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline)
    store, owner, topics, _, _, _ = fixture
    app = create_app(Config(store.data_dir, "https://server.example", inference_enabled=False))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://server.example") as client:
            body = {"device_secret": DEVICE, "user_code": CODE, "credential_hash": digest(CREDENTIAL), "name": "Codex", "engine": "codex"}
            assert (await client.post("/api/bot/connect", json=body)).status_code == 200
            assert (await client.get("/api/bot/connect/" + CODE)).status_code == 401
            status = await client.get("/api/bot/connect/status", headers={"authorization": "Bearer " + DEVICE})
            assert status.json() == {"status": "pending"}
            assert status.headers["cache-control"] == "no-store"
            client.cookies.set("__Host-reasoning_session", owner["access_token"])
            root = "/api/organizations/" + owner["organization_id"]
            approval = {"topic_ids": topics[:1], "scopes": ["messages:read", "messages:write"]}
            path = root + "/my-bots/connect/" + CODE
            assert (await client.post(path, json=approval)).status_code == 403
            headers = {"origin": "https://server.example", "x-csrf-token": owner["csrf_token"]}
            result = await client.post(path, json=approval, headers=headers)
            assert result.status_code == 200 and "token" not in result.json()
            assert (await client.get(root + "/topics/" + topics[0] + "/bots")).json()[0]["id"] == result.json()["id"]
            assert (await client.get(root + "/topics/" + topics[1] + "/bots")).json() == []

    asyncio.run(check())


@pytest.mark.parametrize("noninteractive", [False, True])
def test_cli_connect_saves_locally_generated_credentials_and_uses_browser_only_for_approval(fixture, tmp_path, monkeypatch,
                                                                                        capsys, noninteractive):
    _, owner, topics, _, _, bots = fixture
    pairing = BotPairing(bots)
    directory = tmp_path / "personal-bot"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    answers = iter(["codex", "n", "default", "My Codex"])
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("noninteractive connection prompted")
                       if noninteractive else next(answers))
    monkeypatch.setattr(
        "reasoning_ledger.server.bot_agent.doctor",
        lambda engine, workspace, local_tools: {
            "engine": engine,
            "workspace": workspace,
            "local_tools": local_tools,
            "installed_cas": [],
        },
    )
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/bot/connect":
            body = json.loads(request.content)
            assert "token" not in body and "credential_hash" in body
            result = pairing.start(body["device_secret"], body["user_code"], body["credential_hash"], body["name"], body["engine"])
        else:
            result = pairing.status(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(200, json=result)

    def browser(url):
        code = url.split("bot_connect=", 1)[1]
        assert "rnb_" not in url
        pairing.approve(owner["user_id"], owner["organization_id"], code, topics[:1], ["messages:read", "messages:write"], 3600)

    settings = {"engine": "codex", "name": "My Codex", "models": "default", "local_tools": False} if noninteractive else {}
    connect("https://server.example", directory, transport=httpx.MockTransport(handle), open_browser=browser,
            wait=lambda _: None, **settings)
    config = read_document(directory / "config.json")
    agent = read_document(directory / "agent.json")
    assert agent["local_tools"] is False and config["token"].startswith("rnb_")
    assert bots.identity(config["token"])["name"] == "My Codex"
    output = capsys.readouterr().out
    assert config["token"] not in output and "uv run --locked reasoning-bot run" in output
    assert len(calls) == 2
    assert (directory / "config.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("pair_only", [False, True])
@pytest.mark.parametrize("engine_flag", [False, True])
def test_connect_cli_prompts_for_server_pairs_then_runs_without_relocking_pairing(fixture, tmp_path, monkeypatch, capsys,
                                                                                pair_only, engine_flag):
    from reasoning_ledger.server import bot_agent, bot_client, bot_runner

    _, owner, topics, _, _, bots = fixture
    pairing = BotPairing(bots)
    directory = tmp_path / "paired"
    answers = iter(["", "server.example"] + ([] if engine_flag else ["codex", "n", "default", "My Codex"]))
    prompts, calls, steps, clients = [], [], [], []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(bot_agent, "doctor", lambda engine, workspace, local_tools: {
        "engine": engine, "workspace": workspace, "local_tools": local_tools, "installed_cas": [],
    })

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/bot/connect":
            body = json.loads(request.content)
            result = pairing.start(body["device_secret"], body["user_code"], body["credential_hash"], body["name"], body["engine"])
        elif request.url.path == "/api/bot/connect/status":
            result = pairing.status(request.headers["authorization"].removeprefix("Bearer "))
        else:
            assert request.url.path == "/api/bot/identity"
            result = bots.identity(request.headers["authorization"].removeprefix("Bearer "))
        return httpx.Response(200, json=result)

    def browser(url):
        pairing.approve(owner["user_id"], owner["organization_id"], url.split("bot_connect=", 1)[1], topics[:1],
                        ["messages:read", "messages:write"], 3600)

    original_connect, original_client = bot_agent.connect, bot_client.BotClient

    def pair(server, state, **options):
        assert server == "https://server.example"
        options["transport"] = httpx.MockTransport(handle)
        options["open_browser"] = browser
        return original_connect(server, state, **options)

    def client(config):
        connected = original_client(config, transport=httpx.MockTransport(handle))
        clients.append(connected)
        return connected

    def step(client, state, config, agent, binding, **options):
        assert state == directory.absolute()
        assert binding["allow_other_users"] is False and binding["owner_id"] == owner["user_id"]
        steps.append(binding)
        return {"replied": False}

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(bot_agent, "connect", pair)
    monkeypatch.setattr(bot_client, "BotClient", client)
    monkeypatch.setattr(bot_runner, "runtime_step", step)
    monkeypatch.setattr(bot_client.time, "sleep", stop)
    arguments = ["connect", "--state-dir", str(directory)]
    if engine_flag:
        arguments += ["--engine", "codex"]
    if pair_only:
        arguments += ["--pair-only"]
    assert bot_client.main(arguments) == (0 if pair_only else 130)
    assert len(steps) == (0 if pair_only else 1)
    assert len(clients) == (0 if pair_only else 1)
    assert all(client.client.is_closed for client in clients)
    assert calls == ["/api/bot/connect", "/api/bot/connect/status"] + ([] if pair_only else ["/api/bot/identity"])
    assert "hostname or HTTPS URL" in prompts[0]
    output = capsys.readouterr().out
    assert ("Starting the bot" in output) is not pair_only
    assert ("REASONING · BOT" in output) is not pair_only
    assert read_document(directory / "config.json")["token"] not in output


def test_connection_server_accepts_saved_default_and_reports_missing_stdin_without_network(tmp_path, monkeypatch):
    from reasoning_ledger.server import bot_client

    write_document(tmp_path / "config.json", bot_client.configuration("https://server.example", CREDENTIAL), new=True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert bot_client.connection_server(None, tmp_path) == "https://server.example"
    assert "[https://server.example]" in prompts[0] and CREDENTIAL not in prompts[0]
    assert bot_client.connection_server("server.example/", tmp_path) == "https://server.example/"
    assert bot_client.connection_server("http://localhost:8766", tmp_path) == "http://localhost:8766"

    def eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(BotClientError, match="supply connect --server"):
        bot_client.connection_server(None, tmp_path / "fresh")


def test_existing_pairing_can_start_without_browser_but_cannot_switch_servers(tmp_path, monkeypatch, capsys):
    from reasoning_ledger.server.bot_client import configuration

    directory = tmp_path / "existing"
    directory.mkdir(mode=0o700)
    write_document(directory / "config.json", configuration("https://server.example", CREDENTIAL), new=True)
    before = (directory / "config.json").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("existing pairing must not prompt for CLI settings"))
    connect("https://server.example", directory, run_after=True,
            open_browser=lambda _: pytest.fail("existing pairing must not open a browser"))
    assert "Already connected. Starting" in capsys.readouterr().out
    with pytest.raises(BotClientError, match="another server"):
        connect("https://another.example", directory, run_after=True)
    assert (directory / "config.json").read_bytes() == before


def test_failed_pairing_never_starts_the_runner(tmp_path, monkeypatch, capsys):
    from reasoning_ledger.server import bot_agent, bot_client

    def refuse(*args, **options):
        raise BotClientError("connection approval unavailable")

    monkeypatch.setattr(bot_agent, "connect", refuse)
    monkeypatch.setattr(bot_client, "BotClient", lambda _: pytest.fail("failed pairing must not start a client"))
    with pytest.raises(SystemExit) as error:
        bot_client.main(["connect", "--server", "server.example", "--state-dir", str(tmp_path)])
    assert error.value.code == 2
    assert "connection approval unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "start", "doctor", "connect", "recover"])
def test_busy_bot_state_reports_existing_runner_without_invoking_or_changing_receipts(tmp_path, monkeypatch, capsys, command):
    from reasoning_ledger.server import bot_agent, bot_client

    directory = tmp_path / "paired"
    directory.mkdir(mode=0o700)
    write_document(directory / "config.json", bot_client.configuration("https://server.example", CREDENTIAL), new=True)
    write_document(directory / "runtime-runner.json", {"pending": {"phase": "executing", "session": SESSION}}, new=True)
    retained = {path: path.read_bytes() for path in directory.iterdir()}
    original_client = bot_client.BotClient
    monkeypatch.setattr(bot_client, "BotClient", lambda config: original_client(config, transport=httpx.MockTransport(
        lambda _: pytest.fail("a busy state directory must not contact the server"))))
    monkeypatch.setattr(bot_agent, "doctor", lambda *args: pytest.fail("a busy state directory must not invoke the CLI"))
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("a busy state directory must not prompt"))
    arguments = [command, "--state-dir", str(directory)]
    if command == "connect":
        arguments += ["--server", "https://server.example"]
    if command == "recover":
        arguments += ["--action", "skip"]
    with locked_directory(directory), pytest.raises(SystemExit) as error:
        bot_client.main(arguments)
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "already using this bot state directory" in output.err
    assert "Ctrl-C" in output.err and "make bot-status" in output.err
    assert "inputs or private state are invalid" not in output.err
    assert CREDENTIAL not in output.out + output.err and SESSION not in output.out + output.err
    assert all(path.read_bytes() == body for path, body in retained.items())
    with locked_directory(directory):
        pass  # The rejected command must not retain ownership after the existing runner stops.


@pytest.mark.parametrize("server,pair_only", [("", "0"), ("server.example", "0"), ("", "1"), ("server.example", "1")])
def test_make_bot_connect_passes_optional_server_and_pair_only_without_another_command(tmp_path, server, pair_only):
    executable = tmp_path / "fixture-uv"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o700)
    result = subprocess.run(["make", "--no-print-directory", "bot-connect", "UV=" + str(executable),
        "BOT_SERVER=" + server, "BOT_STATE=" + str(tmp_path / "private state"), "BOT_PAIR_ONLY=" + pair_only],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["run", "--locked", "reasoning-bot", "connect"] + (
        ["--server", server] if server else []) + ["--state-dir", str(tmp_path / "private state")] + (
        ["--pair-only"] if pair_only == "1" else [])


@pytest.mark.parametrize("others", ["0", "1"])
def test_make_bot_run_passes_explicit_other_user_policy(tmp_path, others):
    executable = tmp_path / "fixture-uv"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o700)
    state = tmp_path / "private state"
    result = subprocess.run(["make", "--no-print-directory", "bot-run", "UV=" + str(executable), "BOT_STATE=" + str(state),
        "BOT_ALLOW_OTHER_USERS=" + others], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["run", "--locked", "reasoning-bot", "run", "--state-dir", str(state),
        "--config", str(state / "config.json")] + (["--allow-other-users"] if others == "1" else [])
    rejected = subprocess.run(["make", "--no-print-directory", "bot-run", "UV=true", "BOT_ALLOW_OTHER_USERS=yes"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert rejected.returncode != 0 and "BOT_ALLOW_OTHER_USERS must be 0 or 1" in rejected.stderr


def test_runner_flag_is_documented_as_deprecated_in_favour_of_my_bots(capsys):
    from reasoning_ledger.server.bot_client import main

    with pytest.raises(SystemExit):
        main(["run", "--help"])
    assert "deprecated: choose who can use this bot in My bots" in " ".join(capsys.readouterr().out.split())
    result = subprocess.run(["make", "--no-print-directory", "help"], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert "BOT_ALLOW_OTHER_USERS=1  Deprecated; choose who can use a bot in My bots" in result.stdout


def test_make_bot_connect_rejects_invalid_pair_only_before_starting(tmp_path):
    result = subprocess.run(["make", "--no-print-directory", "bot-connect", "UV=true", "BOT_PAIR_ONLY=invalid"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode != 0
    assert "BOT_PAIR_ONLY must be 0 or 1" in result.stderr
