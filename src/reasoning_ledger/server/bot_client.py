"""Private-config bot client, notifications, and a checkpointed arithmetic research example."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import sys
import time
import webbrowser
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..deployment.host import InstallError
from ..deployment.private import PrivateDirectoryBusy, locked_directory, read_document, read_private, write_document
from .bot_store import IDENTITY, TOKEN

MAX_RESPONSE = 32 * 1024**2


class BotClientError(ValueError):
    pass


class BotRateLimit(BotClientError):
    pass


class BotUnavailable(BotClientError):
    """Temporary connection/server failure; safe receipts may be resumed."""


def bot_panel(title, rows):
    table = Table.grid(padding=(0, 2))
    for label, value in rows:
        table.add_row(Text(label, style="dim"), Text(str(value)))
    Console(highlight=False).print(Panel(table, title=Text(title), border_style="cyan", padding=(1, 2)))


def reconnect(operation, *, once=False, attempts=None, wait=None, report=None, on_status=None):
    """Retry transport/availability only; authorization and unknown work stop."""
    wait = wait or time.sleep
    report = report or (lambda message: print(message, flush=True))
    delay, disconnected, failures = 1, False, 0
    offline_since = None
    while True:
        try:
            result = operation()
            if on_status is not None:
                on_status({"connected": True})
            elif disconnected:
                report("✓ Reconnected; continuing from retained receipts.")
            return result
        except (BotUnavailable, BotRateLimit) as exc:
            failures += 1
            if once or attempts is not None and (failures >= attempts or isinstance(exc, BotRateLimit)):
                raise
            pause = 60 if isinstance(exc, BotRateLimit) else delay
            reason = "Rate limited" if isinstance(exc, BotRateLimit) else "Server unavailable"
            if on_status is not None:
                if offline_since is None:
                    offline_since = time.time()
                on_status({"connected": False, "kind": "rate_limit" if isinstance(exc, BotRateLimit) else "unavailable",
                           "retry_seconds": pause, "offline_since": offline_since})
            else:
                report(f"! {reason}; reconnecting in {pause}s. Ctrl-C to stop.")
            disconnected = True
            wait(pause)
            delay = min(60, delay * 2)


def configuration(server: str, credential: str) -> dict:
    try:
        if not isinstance(server, str) or not server or any(not 33 <= ord(char) <= 126 for char in server):
            raise ValueError
        parsed = urlsplit(server)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
            or parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            or not isinstance(credential, str)
            or not TOKEN.fullmatch(credential)
        ):
            raise ValueError
        return {"format": 1, "server": server.rstrip("/"), "token": credential}
    except (ValueError, TypeError) as exc:
        raise BotClientError("bot configuration requires a HTTPS origin (or HTTP loopback) and a bot token") from exc


def load_configuration(path: Path) -> dict:
    document = read_document(path)
    if set(document) != {"format", "server", "token"} or type(document["format"]) is not int or document["format"] != 1:
        raise BotClientError("unsupported bot configuration")
    return configuration(document["server"], document["token"])


def connection_server(server: str | None, directory: Path) -> str:
    if not server:
        saved = directory / "config.json"
        default = load_configuration(saved)["server"] if saved.exists() or saved.is_symlink() else ""
        while not server:
            try:
                server = input("Reasoning server (hostname or HTTPS URL)" + (f" [{default}]" if default else "") + ": ").strip() or default
            except EOFError:
                raise BotClientError("enter your Reasoning server at the prompt, or supply connect --server HOSTNAME") from None
            if not server:
                print("Enter the hostname of your Reasoning organization, for example chat.example.org.")
    server = server.strip()
    return server if "://" in server else "https://" + server


def server_reason(response, *, maximum=8 * 1024):
    """The server's short refusal explanation as one bounded printable line, or nothing.

    Reasoning error bodies are {"detail": message} or, for request validation,
    {"detail": [{"loc": [...], "type": ...}]} without submitted values.
    Nothing else from the body is used.
    """
    try:
        if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json" or \
                response.headers.get("content-encoding", "identity").lower() != "identity":
            return ""
        data = bytearray()
        for chunk in response.iter_bytes(4096):
            data.extend(chunk)
            if len(data) > maximum:
                return ""
        detail = json.loads(data).get("detail")
    except (httpx.HTTPError, ValueError, AttributeError):
        return ""
    if isinstance(detail, list):
        detail = "; ".join(
            "invalid " + ".".join(str(part) for part in item.get("loc", []))[:120] + " (" + str(item.get("type", ""))[:60] + ")"
            for item in detail[:5] if isinstance(item, dict))
    if not isinstance(detail, str):
        return ""
    detail = " ".join("".join(char if char.isprintable() else " " for char in detail).split())[:1000]
    return ": " + detail if detail else ""


class BotClient:
    def __init__(self, config: dict, *, transport=None):
        config = configuration(config["server"], config["token"])
        self.server = config["server"]
        self.client = httpx.Client(
            headers={"Authorization": "Bearer " + config["token"], "Accept-Encoding": "identity"},
            timeout=15,
            verify=True,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def _request(self, method: str, path: str, body=None):
        route = urlsplit(path)
        knowledge_route = not (route.scheme or route.netloc or route.fragment) and bool(
            re.fullmatch(r"/api/bot/knowledge(?:/[a-f0-9]{32})?", route.path)
        )
        knowledge_query = not set(parse_qs(route.query)) - {"kind", "q", "limit", "before", "revision"}
        if (
            not (knowledge_route and knowledge_query)
            and path not in {"/api/bot/identity", "/api/bot/topics"}
            and not re.fullmatch(r"/api/bot/runtime|/api/bot/runs/(?:claim|from-message)|/api/bot/runs/[a-f0-9]{32}/"
                                 r"(?:progress|finish|context|gateway|tools|knowledge|developer|diagnostics|receipt|"
                                 r"issues(?:/[a-f0-9]{32}/(?:status|consume|receipt))?|"
                                 r"executions(?:/[a-f0-9]{32}/(?:status|consume))?)", path)
            and not re.fullmatch(r"/api/bot/messages\?after=[0-9]+&limit=[0-9]+|/api/bot/topics/[a-f0-9]{32}/messages", path)
        ):
            raise BotClientError("unsupported bot API path")
        try:
            request_body = {} if method in {"POST", "PUT"} and body is None else body
            with self.client.stream(method, self.server + path, json=request_body) as response:
                if response.status_code == 429:
                    raise BotRateLimit("bot API rate limit reached; wait at least 60 seconds before retrying")
                if response.status_code in {408, 425, 500, 502, 503, 504}:
                    raise BotUnavailable("bot server temporarily unavailable; retained receipts will be resumed")
                if response.status_code in {401, 403}:
                    # An authentication refusal has no useful explanation; a policy refusal (403) does.
                    reason = server_reason(response) if response.status_code == 403 else ""
                    raise BotClientError(
                        f"bot access rejected (HTTP {response.status_code}){reason}. If this is a credential "
                        "problem, check My bots for expired/revoked credentials or changed owner permissions. Rotate the "
                        "credential and use configure --output with a new protected config path, or pair a new bot with "
                        "connect --state-dir pointing to a new private directory. Do not delete retained invocation receipts."
                    )
                if response.status_code not in {200, 201}:
                    reason = server_reason(response) if 400 <= response.status_code < 500 else ""
                    raise BotClientError(f"bot API rejected request (HTTP {response.status_code}){reason}")
                if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                    raise BotClientError("bot API did not return JSON")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise BotClientError("compressed bot API responses are not accepted")
                data = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    if len(data) + len(chunk) > MAX_RESPONSE:
                        raise BotClientError("bot API response exceeds its byte allowance")
                    data.extend(chunk)
                return json.loads(data)
        except httpx.HTTPError as exc:
            raise BotUnavailable("bot server connection interrupted; retry safe receipts, not unknown invocations") from exc
        except ValueError as exc:
            if isinstance(exc, BotClientError):
                raise
            raise BotClientError("bot API request or JSON response failed; retry posts with the same client_id") from exc

    def identity(self):
        result = self._request("GET", "/api/bot/identity")
        if (
            not isinstance(result, dict)
            or set(result) - {"delegation_kind", "explicit_topic_ids", "public_access"}
            != {
                "id",
                "organization_id",
                "created_by",
                "authorized_by",
                "created_at",
                "expires_at",
                "revoked_at",
                "last_used_at",
                "name",
                "scopes",
                "topic_ids",
                "enabled",
            }
            or any(
                not isinstance(result.get(field), str) or not IDENTITY.fullmatch(result[field])
                for field in ("id", "organization_id", "created_by", "authorized_by")
            )
        ):
            raise BotClientError("invalid bot identity response")
        return result

    def topics(self):
        result = self._request("GET", "/api/bot/topics")
        if (
            not isinstance(result, list)
            or len(result) > 10000
            or any(
                not isinstance(topic, dict)
                or set(topic) - {"project_id"} != {"id", "name", "kind", "channel_id"}
                or any(not isinstance(topic[field], str) or not IDENTITY.fullmatch(topic[field]) for field in ("id", "channel_id"))
                or not isinstance(topic["name"], str)
                or not topic["name"].strip()
                or len(topic["name"]) > 200
                or topic["kind"] not in {"alpha", "beta", "gamma"}
                for topic in result
            )
        ):
            raise BotClientError("invalid bot topic list")
        if len({topic["id"] for topic in result}) != len(result):
            raise BotClientError("invalid bot topic list")
        return result

    def post(self, topic: str, content: str, client_id: str):
        if not isinstance(topic, str) or not IDENTITY.fullmatch(topic):
            raise BotClientError("topic must be a 32-character identity")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > 16_000
            or not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 128
        ):
            raise BotClientError("invalid bounded message or stable client_id")
        result = self._request("POST", f"/api/bot/topics/{topic}/messages", {"content": content, "client_id": client_id})
        if (
            not isinstance(result, dict)
            or result.get("topic_id") != topic
            or result.get("content") != content
            or result.get("client_id") != client_id
            or result.get("author_kind") != "bot"
            or type(result.get("sequence")) is not int
            or not 1 <= result["sequence"] <= 2**63 - 1
            or not isinstance(result.get("id"), str)
            or not IDENTITY.fullmatch(result["id"])
        ):
            raise BotClientError("invalid bot message acknowledgment; retry with the same client_id")
        return result

    def messages(self, after=0, limit=100):
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 100:
            raise BotClientError("invalid bot message cursor")
        rows = self._request("GET", f"/api/bot/messages?after={after}&limit={limit}")
        if not isinstance(rows, list) or len(rows) > limit:
            raise BotClientError("invalid bot message page")
        sequence = after
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not IDENTITY.fullmatch(row["id"])
                or not isinstance(row.get("topic_id"), str)
                or not IDENTITY.fullmatch(row["topic_id"])
                or type(row.get("sequence")) is not int
                or not sequence < row["sequence"] <= 2**63 - 1
                or row.get("author_kind") not in {"human", "assistant", "bot"}
                or not isinstance(row.get("content"), str)
                or len(row["content"].encode()) > 1024**2
            ):
                raise BotClientError("invalid bot message page")
            sequence = row["sequence"]
        return rows


def gcd_reply(message: dict) -> str | None:
    if message["author_kind"] != "human":
        return None
    match = re.fullmatch(r"!gcd\s+(-?[0-9]{1,100})\s+(-?[0-9]{1,100})\s*", message["content"])
    if not match:
        return None
    a, b = int(match[1]), int(match[2])
    return f"gcd({a}, {b}) = {math.gcd(a, b)}. Computed by the arithmetic research bot (integer Euclidean algorithm)."


def research_step(client: BotClient, journal: Path, binding: dict) -> dict:
    state = (
        read_document(journal)
        if journal.exists() or journal.is_symlink()
        else {"format": 1, "binding": binding, "cursor": 0, "pending": None}
    )
    if (
        set(state) != {"format", "binding", "cursor", "pending"}
        or type(state["format"]) is not int
        or state["format"] != 1
        or state["binding"] != binding
        or type(state["cursor"]) is not int
        or not 0 <= state["cursor"] <= 2**63 - 1
    ):
        raise BotClientError("research journal is invalid or belongs to another server/bot; choose a new state directory")
    processed, replies = 0, 0
    pending = state["pending"]
    if pending is not None:
        if (
            not isinstance(pending, dict)
            or set(pending) != {"sequence", "topic_id", "content", "client_id"}
            or type(pending["sequence"]) is not int
            or not state["cursor"] < pending["sequence"] <= 2**63 - 1
            or not isinstance(pending["topic_id"], str)
            or not IDENTITY.fullmatch(pending["topic_id"])
            or not isinstance(pending["client_id"], str)
            or not re.fullmatch(r"gcd-[a-f0-9]{32}", pending["client_id"])
            or not isinstance(pending["content"], str)
            or not pending["content"].strip()
            or len(pending["content"]) > 16_000
        ):
            raise BotClientError("invalid pending research reply")
        client.post(pending["topic_id"], pending["content"], pending["client_id"])
        state["cursor"], state["pending"] = pending["sequence"], None
        write_document(journal, state)
        processed, replies = 1, 1
    # One batch stays within the bot's write quota. Post identity makes lost receipts/restarts idempotent.
    for message in client.messages(state["cursor"], 20):
        reply = gcd_reply(message)
        if reply:
            state["pending"] = {
                "sequence": message["sequence"],
                "topic_id": message["topic_id"],
                "content": reply,
                "client_id": "gcd-" + message["id"],
            }
            write_document(journal, state, new=not journal.exists())
            client.post(message["topic_id"], reply, state["pending"]["client_id"])
            replies += 1
            state["pending"] = None
        state["cursor"] = message["sequence"]
        write_document(journal, state, new=not journal.exists())
        processed += 1
    return {"cursor": state["cursor"], "processed": processed, "replies": replies}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reasoning-bot", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="create private configuration without printing or overwriting credentials")
    configure.add_argument("--server", required=True)
    configure.add_argument("--token-file", type=Path, help="read the credential from a protected file instead of prompting privately")
    configure.add_argument("--output", type=Path, required=True)
    connect_command = commands.add_parser("connect", help="pair a personal Codex/Claude Code bot, then start its runner")
    connect_command.add_argument("--server")
    legacy = Path.home() / ".reasoning-bot"
    default_state = legacy if (legacy / "config.json").exists() else Path(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "reasoning/bots"
    connect_command.add_argument("--state-dir", type=Path, default=default_state)
    connect_command.add_argument("--engine", choices=("codex", "claude"),
                                 help="configure without terminal prompts; still requires browser approval")
    connect_command.add_argument("--name")
    connect_command.add_argument("--models")
    tools = connect_command.add_mutually_exclusive_group()
    tools.add_argument("--local-tools", action="store_true", default=None)
    tools.add_argument("--no-local-tools", dest="local_tools", action="store_false")
    connect_command.add_argument("--no-browser", action="store_true", help="print the approval URL without launching a browser")
    connect_command.add_argument("--pair-only", action="store_true", help="save the approved pairing without starting the runner")
    for name in ("run", "start", "doctor", "status"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=None)
        command.add_argument("--state-dir", type=Path, default=default_state)
        if name == "status":
            command.add_argument("--json", action="store_true", help="emit bounded status JSON instead of the terminal panel")
        if name in {"run", "start"}:
            command.add_argument("--once", action="store_true")
            command.add_argument(
                "--allow-other-users", action="store_true",
                help="deprecated: choose who can use this bot in My bots; only servers without that setting read this flag",
            )
    recovery = commands.add_parser("recover", help="explicitly retry or skip a retained CLI invocation with an unknown outcome")
    recovery.add_argument("--state-dir", type=Path, required=True)
    recovery.add_argument("--action", choices=("retry", "skip"), required=True)
    for name in ("identity", "topics", "post", "research"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "post":
            command.add_argument("--topic", required=True)
            command.add_argument("--message-file", type=Path, required=True)
            command.add_argument("--client-id", required=True, help="reuse exactly this identity after a lost response")
        if name == "research":
            command.add_argument("--state-dir", type=Path, required=True)
            command.add_argument("--once", action="store_true", help="process at most one 20-message batch")
    args = parser.parse_args(argv)
    if args.command == "start":
        args.command = "run"
    if args.command in {"run", "doctor", "status"} and args.config is None:
        args.config = args.state_dir / "config.json"
    client = None
    try:
        if args.command == "connect":
            from .bot_agent import connect

            connect(connection_server(args.server, args.state_dir), args.state_dir,
                    engine=args.engine, name=args.name, models=args.models or ("default" if args.engine else None),
                    local_tools=args.local_tools, open_browser=(lambda _: None) if args.no_browser else webbrowser.open,
                    run_after=not args.pair_only)
            if args.pair_only:
                return 0
            args.command = "run"
            args.config = args.state_dir / "config.json"
            args.once = False
            args.allow_other_users = False
        if args.command == "recover":
            from .bot_runner import read_journal, write_journal

            with locked_directory(args.state_dir) as directory:
                runtime_journal = directory / "runtime-runner.json"
                journal = runtime_journal if runtime_journal.exists() else directory / "runner.json"
                state = read_journal(journal) if journal == runtime_journal else read_document(journal)
                pending = state.get("pending")
                if not isinstance(pending, dict) or pending.get("phase") not in (
                        {"executing", "reply_ready"} if journal == runtime_journal else {"executing"}):
                    raise BotClientError("there is no unknown CLI invocation to recover")
                if args.action == "retry":
                    if journal == runtime_journal:
                        raise BotClientError("use --action skip after inspecting the retained invocation, then submit a new "
                                             "request in Reasoning; unknown local work is not silently replayed")
                    print("Retry can spend again and repeat local side effects. Inspect the local CLI session first.")
                else:
                    state["cursor"] = pending.get("sequence", state["cursor"])
                if input(f"Type {args.action} to confirm: ").strip() != args.action:
                    raise BotClientError("recovery not confirmed")
                if journal == runtime_journal:
                    identity = pending["run"]["id"]
                    if not IDENTITY.fullmatch(identity):
                        raise BotClientError("invalid retained invocation identity")
                    archive = directory / f"recovered-{identity}.json"
                    if not archive.exists():
                        write_journal(archive, state, new=True)
                    print("Retained invocation receipt: " + str(archive))
                state["pending"] = None
                if journal == runtime_journal:
                    write_journal(journal, state)
                else:
                    write_document(journal, state)
            return 0
        if args.command == "configure":
            credential = (
                read_private(args.token_file, 128).decode().strip() if args.token_file else getpass.getpass("Bot credential: ").strip()
            )
            config = configuration(args.server, credential)
            write_document(args.output, config, new=True)
            print(json.dumps({"config_file": str(args.output.absolute())}))
            return 0
        if args.command in {"run", "doctor", "status"} and not (args.config.exists() or args.config.is_symlink()):
            raise BotClientError("no paired bot configuration found; run make bot-connect BOT_SERVER=https://your-server "
                                 "or select the existing private directory with BOT_STATE/--state-dir")
        client = BotClient(load_configuration(args.config))
        if args.command == "status":
            from .bot_runner import read_journal

            identity = client.identity()
            pending = None
            journal = args.state_dir / "runtime-runner.json"
            if journal.exists() or journal.is_symlink():
                pending = read_journal(journal).get("pending")
            result = {"name": identity["name"], "enabled": identity["enabled"], "server": client.server,
                      "topics": len(identity["topic_ids"]), "expires_at": identity["expires_at"],
                      "pending": pending.get("phase") if pending else None, "state_dir": str(args.state_dir.absolute())}
            if args.json:
                print(json.dumps(result))
            else:
                bot_panel("REASONING · BOT STATUS", [(key.replace("_", " ").title(), value) for key, value in result.items()])
            return 0
        if args.command in {"doctor", "run"}:
            from .bot_agent import doctor
            from .bot_runner import runtime_step

            if getattr(args, "allow_other_users", False):
                print("reasoning-bot: --allow-other-users is deprecated; choose who can use this bot in My bots.",
                      file=sys.stderr)

            with locked_directory(args.state_dir) as directory, ExitStack() as display:
                agent = read_document(directory / "agent.json")
                managed = directory / "workspaces"
                if managed.is_symlink():
                    raise BotClientError("managed workspace root cannot be a symlink")
                managed.mkdir(mode=0o700, exist_ok=True)
                agent = {**agent, "workspace": str(managed)}
                checked = doctor(agent["engine"], agent["workspace"], agent["local_tools"])
                if agent["local_tools"] and not checked["local_execution_available"]:
                    print("Local execution is unavailable: Linux bubblewrap (bwrap) is required. "
                          "Inference and knowledge tools remain enabled.")
                    agent = {**agent, "local_tools": False}
                if args.config.absolute().is_relative_to(Path(checked["workspace"])) or directory.is_relative_to(
                    Path(checked["workspace"])
                ):
                    raise BotClientError("keep bot credentials and state outside the agent workspace")
                terminal = None
                if args.command == "run" and not args.once:
                    from .bot_terminal import BotTerminal

                    terminal = display.enter_context(BotTerminal([
                        ("Bot", "Checking identity"), ("Server", client.server), ("CLI", agent["engine"]), ("Topics", "Checking access"),
                        ("Private state", directory), ("Reconnect", "Automatic · 1–60s backoff"),
                        ("Stop", "Ctrl-C · receipts retained; no paid invocation replay"),
                    ]))
                status = terminal.connection if terminal else None
                identity = reconnect(client.identity, once=args.command == "doctor" or args.once, on_status=status)
                if args.command == "doctor":
                    print(json.dumps({**checked, "bot_id": identity["id"], "topics": client.topics()}))
                    return 0
                if not {"messages:read", "messages:write"}.issubset(identity["scopes"]):
                    raise BotClientError("the CLI runner requires read and respond access to selected topics")
                binding = {
                    "server": client.server,
                    "bot_id": identity["id"],
                    "owner_id": identity["created_by"],
                    "engine": agent["engine"],
                    "workspace": checked["workspace"],
                    "local_tools": agent["local_tools"],
                    "allow_other_users": args.allow_other_users,
                }
                if terminal:
                    terminal.identify(identity["name"], len(identity["topic_ids"]))
                while True:
                    def step():
                        old_journal = directory / "runner.json"
                        if old_journal.exists() and read_document(old_journal).get("pending"):
                            raise BotClientError("reconcile the retained invocation with reasoning-bot recover before starting new work")
                        options = {"on_activity": terminal.activity, "on_status": status} if terminal else {}
                        return runtime_step(client, directory, load_configuration(args.config), agent, binding, **options)
                    result = reconnect(step, once=args.once, on_status=status)
                    if args.once:
                        print(json.dumps(result))
                        return 0
                    if terminal:
                        terminal.activity("Reply delivered · listening" if result.get("replied") else "Listening")
                    time.sleep(5)
        if args.command == "identity":
            print(json.dumps(client.identity()))
        elif args.command == "topics":
            print(json.dumps(client.topics()))
        elif args.command == "post":
            result = client.post(args.topic, read_private(args.message_file, 64_000).decode(), args.client_id)
            print(json.dumps({"message_id": result["id"], "sequence": result["sequence"]}))
        else:
            identity = client.identity()
            binding = {"server": client.server, "bot_id": identity["id"]}
            with locked_directory(args.state_dir) as directory:
                while True:
                    result = reconnect(lambda: research_step(client, directory / "research.json", binding), once=args.once)
                    if args.once:
                        print(json.dumps(result))
                        break
                    time.sleep(5)
        return 0
    except KeyboardInterrupt:
        return 130
    except PrivateDirectoryBusy:
        parser.exit(2, "reasoning-bot: another process is already using this bot state directory; "
                      "stop the existing runner with Ctrl-C in its terminal before restarting. "
                      "Use make bot-status with the same BOT_STATE to inspect the pairing.\n")
    except (BotClientError, InstallError, OSError, UnicodeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, BotClientError) else "bot inputs or private state are invalid"
        parser.exit(2, f"reasoning-bot: {message}\n")
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
