"""Rich, resumable DigitalOcean bot setup without local provider CLI installs.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import subprocess
import webbrowser
from pathlib import Path

from reasoning_ledger.server.bot_agent import connect
from reasoning_ledger.server.bot_client import BotClient, BotClientError, bot_panel, connection_server, load_configuration

from . import bot_cloud, setup_ui
from .digitalocean import DigitalOcean
from .host import InstallError
from .private import locked_directory, protected_parent, read_document, read_private, write_document, write_private
from .setup import ask, current_public_ipv4


def checked_agent(engine, workspace, local_tools):
    # This pairing runs on dedicated hardware; the installer verifies the
    # remote CLI and tools. No local Codex/Claude installation is required.
    return {"engine": engine, "workspace": workspace, "local_tools": local_tools,
            "local_execution_available": False, "installed_cas": []}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a personal Codex/Claude Code bot on your own DigitalOcean account")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
                        / "reasoning/cloud-bots/default")
    parser.add_argument("--server")
    parser.add_argument("--enable-web", action="store_true", help="operator opt-in for owned chat-server outbound SSH")
    parser.add_argument("--deploy-state", type=Path, default=Path.home() / ".reasoning-deploy")
    parser.add_argument("--action", choices=("setup", "status", "login", "start"), default="setup")
    args = parser.parse_args(argv)
    provider = None
    try:
        if args.enable_web:
            from .bot_cloud_access import wizard

            wizard(args.deploy_state.absolute())
            return 0
        directory = args.state_dir.absolute()
        missing, parent = [], directory.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            protected_parent(parent)
            parent.mkdir(mode=0o700)
        bot_panel("REASONING · CLOUD BOT", [("Hardware", "Your DigitalOcean account · dedicated CPU VM"),
                  ("Provider", "Your Codex or Claude Code account · sign in on the VM"),
                  ("State", str(directory)), ("Billing", "Continues while powered off; destroy the Droplet to stop VM charges")])
        with locked_directory(directory):
            path = directory / "setup.json"
            if path.exists():
                setup = read_document(path)
            else:
                if args.action != "setup":
                    raise InstallError("no retained setup; run make bot-cloud first")
                server, name = connection_server(args.server, directory), ask("Bot name", "cloud-bot")
                engine = ask("CLI engine (codex or claude)", "codex")
                cas = ask("Install SymPy, GAP, PARI/GP and Singular? y/n", "n").lower() == "y"
                lean = ask("Install pinned Lean 4 and mathlib? y/n", "n").lower() == "y"
                models = [item.strip() for item in ask("Provider CLI models, comma-separated", "default").split(",")]
                # Build only the trusted Python bot artifact; no frontend/node
                # installation and no private repository credentials on the VM.
                artifact = directory / "artifact"
                artifact.mkdir(mode=0o700, exist_ok=True)
                from reasoning_ledger.source_release import build_source

                source = build_source(Path.cwd(), artifact / "reasoning-source.zip")
                spec = {"server": server.rstrip("/"), "engine": engine, "models": models, "cas": cas, "lean": lean,
                        "wheel_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
                bot_cloud.validate_spec(spec)
                webbrowser.open("https://cloud.digitalocean.com/account/api/tokens")
                print("Create a token in the same Team that should own the bot. Required permissions: " + bot_cloud.TOKEN_SCOPES)
                print("Payment/account setup is in DigitalOcean. The token stays in protected local state, not on the bot VM.")
                token = getpass.getpass("DigitalOcean token (hidden): ").strip()
                provider = DigitalOcean(token)
                quote = provider.compute_quote(name=name, region=ask("DigitalOcean region", "fra1"),
                    size=ask("CPU Droplet size", "s-4vcpu-8gb" if lean else "s-2vcpu-4gb" if cas else "s-1vcpu-2gb"),
                    admin_cidr=current_public_ipv4(), maximum_hourly_usd=ask("Maximum Droplet hourly USD"))
                bot_cloud.check_size(quote, spec)
                setup = {"quote": quote, "spec": spec, "wheel": str(source), "phase": "quoted"}
                write_private(directory / "digitalocean-token", token.encode())
                write_document(path, setup, new=True)
        bot_panel("Review cloud bot", [("Bot", setup["quote"]["name"]), ("Engine", setup["spec"]["engine"]),
                  ("Region / size", setup["quote"]["region"] + " / " + setup["quote"]["size"]),
                  ("USD / hour", setup["quote"]["quote"]["compute_hourly_usd"]),
                  ("USD / 730 hours", setup["quote"]["quote"]["estimated_730_hours_usd"]),
                  ("Excludes", "Provider inference, tax, transfer, storage, backups, other services"),
                  ("Tools", "CAS " + str(setup["spec"]["cas"]) + " · Lean/mathlib " + str(setup["spec"]["lean"]))])
        if args.action == "status":
            print("Retained setup: " + setup["phase"])
            print("Cloud resources may be billable even when the bot is offline. Inspect your DigitalOcean account.")
            return 0
        if setup["phase"] == "quoted":
            expected = "create bot " + setup["quote"]["name"]
            if input(f"Create billable hardware? Type '{expected}': ").strip() != expected:
                raise InstallError("cloud bot creation not confirmed")
            setup["phase"] = "confirmed"
            write_document(path, setup)
        pairing = directory / "pairing"
        if not (pairing / "config.json").exists():
            connect(setup["spec"]["server"], pairing, engine=setup["spec"]["engine"], name=setup["quote"]["name"],
                    models=",".join(setup["spec"]["models"]), local_tools=setup["spec"]["cas"] or setup["spec"]["lean"],
                    agent_check=checked_agent)
        config = load_configuration(pairing / "config.json")
        provider = provider or DigitalOcean(read_private(directory / "digitalocean-token").decode())
        host = directory / "host"
        with locked_directory(host):
            document = bot_cloud.prepare(provider, host, setup["quote"], setup["spec"])

        def authorize():
            client = BotClient(config)
            try:
                client.identity()
            finally:
                client.close()

        controller = bot_cloud.BotProvisioner(provider, document, authorize=authorize)
        if setup["phase"] != "installed":
            with setup_ui.upgrade_progress("Provisioning the dedicated bot VM", ("retain ownership", "pin SSH", "no public chat ports")):
                result = bot_cloud.provision_ready(controller, host)
                if not result["ssh_host"]:
                    raise InstallError("VM still preparing; rerun make bot-cloud with this state")
            with setup_ui.upgrade_progress("Installing the bot and selected tools",
                                           ("verified artifact", "isolated user", "CAS/Lean probes")):
                bot_cloud.install(controller, host, Path(setup["wheel"]), config["token"])
            setup["phase"] = "installed"
            write_document(path, setup)
        if args.action != "start":
            print("Sign in below; browser account/payment steps remain with the CLI provider.")
            if setup["spec"]["engine"] == "codex":
                print("Device authentication must be enabled in your ChatGPT account/workspace.")
            with controller.connection(host) as (ssh, _):
                ssh.command(["sudo", "-u", "reasoning-bot", "-H", *bot_cloud.LOGIN[setup["spec"]["engine"]].split()],
                            interactive=True, timeout=600)
        bot_cloud.start(controller, host)
        bot_panel("CLOUD BOT READY", [("Bot", setup["quote"]["name"]), ("Server", setup["spec"]["server"]),
                  ("Runner", "systemd · reconnects automatically"), ("Approvals", "My bots in Reasoning"),
                  ("Who can use it", "Only you · change it in My bots"),
                  ("Hardware", "Manage/destroy this dedicated Droplet in DigitalOcean to end VM billing")])
        return 0
    except (InstallError, BotClientError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("reasoning-bot-cloud: " + str(exc))
        return 2
    except (KeyboardInterrupt, EOFError):
        print("Cloud bot setup paused; retain state and rerun the same command. Created hardware remains billable.")
        return 130
    finally:
        if provider:
            provider.close()
