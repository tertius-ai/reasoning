"""Terminal presentation for the interactive first-organization setup."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import PurePath
from time import monotonic

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)


def header(release: str) -> None:
    body = Text()
    body.append("FIRST ORGANIZATION\n", style="bold bright_white")
    body.append("A guided, resumable launch to your own cloud account.", style="white")
    console.print()
    console.print(
        Panel.fit(
            body,
            title=Text(" REASONING ", style="bold bright_cyan"),
            subtitle=Text("release " + release[:12], style="dim"),
            border_style="bright_cyan",
            padding=(1, 3),
        )
    )


def upgrade_header(source: str, target: str) -> None:
    body = Text()
    body.append("SERVER UPGRADE\n", style="bold bright_white")
    body.append("A verified, resumable release change with rollback evidence.", style="white")
    console.print()
    console.print(
        Panel.fit(
            body,
            title=Text(" REASONING ", style="bold bright_cyan"),
            subtitle=Text(source[:8] + " → " + target[:8], style="dim"),
            border_style="bright_cyan",
            padding=(1, 3),
        )
    )


def upgrade_review(source: dict, target: dict, state_path: str, ssh_cidr: str) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column(overflow="fold")
    rows.add_row("Site", escape(source["public_url"]))
    rows.add_row("Installed", source["release_id"][:12])
    rows.add_row("Target", target["release_id"][:12])
    rows.add_row("SSH", escape(ssh_cidr) + " · temporary")
    rows.add_row("Recovery state", escape(state_path))
    caution = Text()
    caution.append("Preparation briefly stops the server. ", style="bold yellow")
    caution.append("The operation retains its source archive, encrypted backup, exact plan, and retry identity.")
    console.print(Panel(Group(rows, Text(), caution), title="Review release change", border_style="yellow", padding=(1, 2)))


def upgrade_ready(plan: dict) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column()
    rows.add_row("Database", f"schema {plan['source_database_schema']} → {plan['target_database_schema']}")
    rows.add_row("Plan", plan["plan_id"][:12])
    rows.add_row("Preflight", "passed")
    console.print(Panel(rows, title="Ready to prepare", border_style="green", padding=(0, 2)))


@contextmanager
def upgrade_progress(message: str, stages: tuple[str, ...] = ()):
    began = monotonic()
    started = Text()
    started.append("→ ", style="bold cyan")
    started.append(message, style="bold bright_white")
    console.print(started)
    if stages:
        console.print(Text("  " + " · ".join(stages), style="dim"), soft_wrap=True)
    console.print(Text("  Working… this can take several minutes; keep this terminal open.", style="dim"))
    try:
        with console.status(Text("Working… keep this terminal open", style="bold cyan"), spinner="dots"):
            yield
    except BaseException:
        console.print(Text(f"! No verified completion receipt after {monotonic() - began:.1f}s; "
                           "the same operation can be resumed.", style="bold yellow"))
        raise
    else:
        console.print(Text(f"✓ {message} ({monotonic() - began:.1f}s)", style="bold green"))


def upgrade_prepared(receipt: dict) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold green", no_wrap=True)
    rows.add_column()
    rows.add_row("✓", "Server restart fenced")
    rows.add_row("✓", "Encrypted root backup retained")
    rows.add_row("✓", "Target release prepared; activation has not run")
    rows.add_row("Operation", receipt["operation_id"][:12])
    console.print(Panel(rows, title="Preparation verified", border_style="green", padding=(0, 2)))


def upgrade_success(url: str, release: str) -> None:
    body = Text()
    body.append("Upgrade complete\n", style="bold bright_green")
    body.append(url + "\n", style="bold bright_white")
    body.append("Release " + release[:12] + " is active; inbound SSH is closed.", style="white")
    console.print(Panel.fit(body, border_style="green", padding=(1, 3)))


def operator_header(mode: str) -> None:
    body = Text()
    body.append("DEPLOYMENT OPERATOR\n", style="bold bright_white")
    body.append(
        "Authorize a collaborator without sharing provider or SSH secrets."
        if mode == "authorize"
        else "Create independent deployment state using your own credentials.",
        style="white",
    )
    console.print()
    console.print(Panel.fit(body, title=Text(" REASONING ", style="bold bright_cyan"), border_style="bright_cyan", padding=(1, 3)))


def onboard_header() -> None:
    body = Text("ONBOARD THIS COMPUTER\n", style="bold bright_white")
    body.append("Independent deployment access to an existing organization server.\n", style="white")
    body.append("Prepare your own key, receive an authorized handoff, and verify access without upgrading.", style="dim")
    console.print(Panel.fit(body, title=Text(" REASONING ", style="bold bright_cyan"), border_style="bright_cyan", padding=(1, 3)))


def onboard_handoff(public_file: str, fingerprint: str, public_key: str, resume: str) -> None:
    section("Existing operator authorization", "Send only this public key, never its private key or your token.")
    console.print()
    console.print(Text("Public key file: " + public_file))
    console.print(Text("SSH key fingerprint: " + fingerprint))
    console.print()
    console.print(Text(public_key), soft_wrap=True)
    console.print()
    console.print(Text("1. On the existing operator's computer", style="bold bright_white"))
    console.print(Text("Save the public key in a file, then run the command below. It asks for that file and reads it once."))
    console.print()
    console.print(Text("  make operator-authorize", style="bright_cyan"), soft_wrap=True)
    console.print()
    console.print(Text("Its review shows a \"Collaborator SSH key fingerprint\"; it must equal the fingerprint above."))
    console.print()
    console.print(Text("2. Receive the handoff on this computer", style="bold bright_white"))
    console.print(Text("The existing operator sends the archive privately and its handoff archive checksum "
                       "through an authenticated connection."))
    console.print()
    console.print(Text("Protect the received archive:"))
    console.print()
    console.print(Text("  chmod 600 /path/to/received-handoff.tar.gz", style="bright_cyan"), soft_wrap=True)
    console.print()
    console.print(Text("3. Continue here, or pause and resume later", style="bold bright_white"))
    console.print(Text("Keep both private directories on this box for retries. To resume, run:"))
    console.print()
    console.print(Text("  " + resume, style="bright_cyan"), soft_wrap=True)
    console.print()


ACCESS_CHECKS = (
    ("digitalocean_access", "DigitalOcean access"),
    ("ssh_server_identity", "SSH authentication and server identity"),
    ("release_and_https", "Installed release and HTTPS"),
    ("ssh_closed", "Temporary SSH access closed"),
)


def access_verified(result: dict, *, onboarded: bool = False, state: str | None = None, staging: str | None = None) -> bool:
    """Print each access check; report readiness only when every check passed and SSH closed."""
    checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
    ready = result.get("phase") == "operator_verified" and all(checks.get(name) is True for name, _ in ACCESS_CHECKS)
    console.print()
    heading = Text()
    heading.append("Deployment access verified: " if ready else "Deployment access not verified: ",
                   style="bold bright_green" if ready else "bold yellow")
    heading.append(str(result.get("public_url", "")), style="bold bright_white")
    console.print(heading)
    console.print()
    for name, label in ACCESS_CHECKS:
        line = Text()
        line.append("✓ " if checks.get(name) is True else "✗ ", style="bold green" if checks.get(name) is True else "bold red")
        line.append(label)
        console.print(line)
    console.print()
    if not ready:
        return False
    if onboarded:
        console.print(Text("This computer is onboarded. No application upgrade was performed.", style="white"))
        if state is not None and staging is not None:
            console.print(Text("Keep deployment state: " + state + "\nKeep key/token preparation: " + staging, style="dim"))
        console.print()
    console.print(Text("Ready to upgrade.", style="bold bright_white"))
    console.print(Text("Coordinate with the other operator, then run: make upgrade"))
    if onboarded:
        console.print(Text("Optional later recheck: make doctor", style="dim"))
    return True


def operator_review(url: str, fingerprint: str, output: str, ssh_cidr: str) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column(overflow="fold")
    rows.add_row("Site", escape(url))
    rows.add_row("Collaborator SSH key fingerprint", escape(fingerprint))
    rows.add_row("SSH", escape(ssh_cidr) + " · temporary")
    rows.add_row("Handoff archive", escape(output))
    note = Text()
    note.append("Compare the fingerprint with the one the collaborator's make onboard displays, "
                "through a channel where you recognize them.\n", style="white")
    note.append("The handoff contains public trust and release state—not tokens, passwords, or private keys.", style="dim")
    console.print(Panel(Group(rows, Text(), note), title="Review collaborator access", border_style="yellow", padding=(1, 2)))


def handoff_message(url: str, fingerprint: str, archive: str, checksum: str) -> str:
    name = PurePath(archive).name
    return (
        f"You are authorized as a Reasoning deployment operator for {url}.\n"
        f"Your SSH key fingerprint: {fingerprint}\n"
        "\n"
        f"1. Save the handoff archive {name} that I sent separately, and protect it:\n"
        f"   chmod 600 /path/to/{name}\n"
        "2. In your Reasoning checkout, rerun make onboard (or the resume command it printed).\n"
        "3. When asked, enter the archive path and this handoff archive checksum (SHA-256):\n"
        f"   {checksum}\n"
        "\n"
        "Onboarding refuses an archive whose checksum differs. If this message did not reach you through a channel "
        "where you recognize me, confirm the checksum with me directly before continuing."
    )


def operator_handoff_ready(url: str, fingerprint: str, archive: str, checksum: str, *, closing: bool = False) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column(overflow="fold")
    rows.add_row("Site", escape(url))
    rows.add_row("Collaborator SSH key fingerprint", escape(fingerprint))
    rows.add_row("Handoff archive", escape(archive))
    rows.add_row("Handoff archive checksum", "SHA-256 " + checksum)
    console.print(Panel(rows, title="Collaborator authorized", border_style="yellow" if closing else "green", padding=(1, 2)))
    if closing:
        console.print(Text("DigitalOcean is still applying the SSH firewall closure. Rerun the same "
                           "make operator-authorize command to confirm it.", style="bold yellow"))
    console.print(Text("Send the archive file privately. Send the message below through a channel where the collaborator "
                       "recognizes you, such as a call or an authenticated chat."))
    console.print()
    console.print(Rule(Text("Message to send", style="bold cyan"), style="cyan"))
    console.print(Text(handoff_message(url, fingerprint, archive, checksum)), soft_wrap=True)
    console.print(Rule(style="cyan"))


def operator_joined(url: str, detail: str) -> None:
    body = Text()
    body.append("Deployment operator ready\n", style="bold bright_green")
    body.append(url + "\n", style="bold bright_white")
    body.append(detail, style="white")
    body.append("\nNext: make doctor to verify SSH and HTTPS; then make upgrade.", style="dim")
    console.print(Panel.fit(body, border_style="green", padding=(1, 3)))


def exposed_secret() -> None:
    warning(
        "That answer looks like an API token or key, and the terminal displayed it. It was not saved, logged, or sent. "
        "Treat it as exposed: revoke it at the provider (DigitalOcean: API > Tokens) and create a new one. "
        "Paste secrets only at a prompt marked hidden. Answer the question again.",
        title="Secret pasted into a visible prompt",
    )


def protection_choices() -> None:
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=False)
    table.add_column("#", justify="right", style="bold cyan", no_wrap=True)
    table.add_column("Method", style="bold bright_white")
    table.add_column("Use when", overflow="fold")
    table.add_row("1", "Encrypted recovery kit", "No second machine or operator is readily available")
    table.add_row("2", "Another deployment operator", "A trusted administrator can hold independent infrastructure access")
    table.add_row("3", "Defer for testing only", "The installation may become unupgradeable if this computer is lost")
    note = Text()
    note.append("Organization administration and deployment access are separate. ", style="bold bright_white")
    note.append("A second operator may also be an organization admin, but must receive an independent SSH handoff.\n")
    note.append("A recovery kit restores operator access only; it is not an application-data backup.", style="dim")
    console.print(Panel(Group(note, Text(), table), title="Protect this deployment", border_style="yellow", padding=(1, 2)))


def recovery_code(code: str, output: str, checksum: str) -> None:
    body = Text()
    body.append("Save this recovery code now\n", style="bold yellow")
    body.append(code + "\n", style="bold bright_white")
    body.append("Kit: ", style="dim")
    body.append(escape(output) + "\n")
    body.append("SHA-256: " + checksum + "\n", style="dim")
    body.append(
        "Keep the code and encrypted kit somewhere available without this computer—such as a password manager, "
        "encrypted cloud drive, or removable storage.",
        style="white",
    )
    console.print(Panel(body, title="Offline operator recovery", border_style="yellow", padding=(1, 2)))


def recovery_ready(output: str, checksum: str) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold green", no_wrap=True)
    rows.add_column(overflow="fold")
    rows.add_row("✓", "Independent recovery SSH key authorized")
    rows.add_row("✓", "Encrypted recovery kit decrypted and verified")
    rows.add_row("Kit", escape(output))
    rows.add_row("SHA-256", checksum)
    console.print(Panel(rows, title="Deployment recovery ready", border_style="green", padding=(1, 2)))


def protection_deferred() -> None:
    warning(
        "This test installation still depends on one computer's operator state and SSH key. "
        "Create a recovery kit or add another operator before relying on it.",
        title="Single-device recovery risk accepted",
    )


def section(title: str, description: str | None = None) -> None:
    console.print()
    console.print(Rule(Text(title.upper(), style="bold cyan"), style="cyan"))
    if description:
        console.print(Text(description, style="dim"))


def digitalocean_access(permissions: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=False)
    table.add_column("Resource", style="bold", no_wrap=True)
    table.add_column("Enable", overflow="fold")
    for resource, scopes in permissions:
        table.add_row(resource, "  ".join(scopes))
    message = Text()
    message.append("Create one custom DigitalOcean token", style="bold bright_white")
    message.append(" and enable the permissions below.\n", style="white")
    message.append(
        "Resources are alphabetical; select each resource's permissions together on that one token—not separate tokens.",
        style="bright_cyan",
    )
    contents = [message, table]
    if any(scope.endswith("*") for _, scopes in permissions for scope in scopes):
        contents.append(Text("* ssh_key:create is needed for the default generated-key path unless the key already exists.", style="dim"))
    console.print(Panel(Group(*contents), title="DigitalOcean access", border_style="cyan", padding=(1, 2)))


def digitalocean_token_recovery() -> None:
    body = Text()
    body.append("The server is running and HTTPS is ready. ", style="bold green")
    body.append("SSH remains restricted to the saved administrator /32 until its firewall rule can be removed.\n")
    body.append("DigitalOcean does not let you add scopes to an existing token. Create a replacement token in the same team; ")
    body.append("Full Access", style="bold bright_white")
    body.append(" is the simplest recovery choice. Setup verifies the account before retrying.\n")
    body.append("After setup succeeds, revoke the old token in DigitalOcean.", style="yellow")
    console.print(Panel(body, title="DigitalOcean token replacement required", border_style="yellow", padding=(1, 2)))


def state(path: str, *, resumed: bool) -> None:
    label = "Resuming protected setup state" if resumed else "Protected setup state"
    console.print(Panel(escape(path), title=label, border_style="blue", padding=(0, 1)))


def deployment_review(config: dict, plan: dict, state_path: str, modal: dict | None, *, ssh_cidr: str | None = None) -> None:
    quote = plan["quote"]
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column(overflow="fold")
    rows.add_row("Address", escape(config["public_url"]))
    rows.add_row("Server", f"{escape(plan['size'])} · {escape(plan['region'])}")
    rows.add_row("Compute", f"${escape(quote['compute_hourly_usd'])}/hour · ${escape(quote['estimated_730_hours_usd'])}/730 hours")
    rows.add_row("Models", "Any available OpenRouter text model or router")
    rows.add_row("LLM spending", "Daily key limit configured and enforced in OpenRouter")
    rows.add_row("State", escape(state_path))
    if ssh_cidr is not None:
        rows.add_row("SSH", f"{escape(ssh_cidr)} · temporary")
    if modal is not None:
        rows.add_row("Modal", f"{modal['workers']} relays · {modal['resources']['cpu']} CPUs/job")
        rows.add_row("Compute quota", f"{modal['monthly_cpu_seconds']} CPU-seconds/month")
    caution = Text()
    caution.append("This is not an invoice cap. ", style="bold yellow")
    caution.append("Taxes, transfer, storage, backups, and provider-side usage are excluded.")
    console.print(Panel(Group(rows, Text(), caution), title="Review before launch", border_style="yellow", padding=(1, 2)))


def warning(message: str, *, title: str = "Attention") -> None:
    console.print(Panel(Text(message), title=title, border_style="yellow", padding=(0, 1)))


def ssh_access(cidr: str) -> None:
    body = Text()
    body.append(cidr, style="bold bright_white")
    body.append(" can reach SSH during this setup session.\n")
    body.append("The rule is refreshed on resume and removed after successful setup.", style="dim")
    console.print(Panel(body, title="Temporary SSH access", border_style="blue", padding=(0, 1)))


def ssh_closed() -> None:
    console.print(
        Panel(
            Text("Inbound SSH is closed. HTTPS remains public.", style="bold green"),
            title="Server locked",
            border_style="green",
            padding=(0, 1),
        )
    )


def cloudflare_child_zone(parent: str, child: str) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="bold cyan", no_wrap=True)
    rows.add_column()
    rows.add_row("Keep on Cloudflare", parent)
    rows.add_row("Delegate to DigitalOcean", child)
    rows.add_row("Chat address", "https://" + child)
    console.print(Panel(rows, title="Simpler Cloudflare setup", border_style="cyan", padding=(1, 2)))


def registrar_choices(guides, matches) -> None:
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=False)
    table.add_column("#", justify="right", style="bold cyan", no_wrap=True)
    table.add_column("Registrar", no_wrap=True)
    visible = set(matches)
    for number, guide in enumerate(guides, 1):
        if guide in visible:
            table.add_row(str(number), guide.name)
    table.add_row("0", "Other / delegated subdomain")
    console.print(Panel(table, title="Choose your registrar", subtitle="number, name, or search text", border_style="cyan", padding=(0, 2)))


def delegation_guide(
    zone: str,
    provider: str,
    steps: tuple[str, ...],
    nameservers: tuple[str, ...],
    *,
    dashboard: str,
    help_url: str,
    dnssec_url: str,
    parent_zone: str | None = None,
) -> None:
    summary = Text()
    summary.append(zone, style="bold bright_white")
    summary.append("  →  DigitalOcean DNS\n")
    summary.append("DNS provider / registrar: ", style="dim")
    summary.append(provider, style="bold")
    console.print(Panel(summary, title="DNS delegation required", border_style="yellow", padding=(1, 2)))

    if parent_zone is not None and provider == "Cloudflare DNS":
        note = Text()
        note.append(parent_zone, style="bold bright_white")
        note.append(" stays on Cloudflare. Its nameservers and unrelated records do not change.")
        console.print(note, soft_wrap=True)
    else:
        caution = Text()
        caution.append("Before switching, copy all existing DNS records", style="bold yellow")
        caution.append(" (web, MX, TXT, SPF, DKIM, and DMARC) into DigitalOcean.\n")
        caution.append("This wizard does not copy records or inspect DNSSEC. DigitalOcean DNS does not support DNSSEC; ")
        caution.append("remove the parent DS records first if DNSSEC is enabled.\n")
        caution.append("For a registered domain, change its nameservers. For a delegated subdomain, add NS records in its parent zone. ")
        caution.append("If the provider does not support external nameservers, stop and contact its support.")
        console.print(Rule(Text("Protect existing services", style="bold yellow"), style="yellow"))
        console.print(caution, soft_wrap=True)

    console.print(Rule(Text("Steps", style="bold cyan"), style="cyan"))
    for number, step in enumerate(steps, 1):
        line = Text()
        line.append(f"{number}. ", style="bold cyan")
        line.append(step)
        console.print(line, soft_wrap=True)

    console.print(Rule(Text("Publish all three nameservers · no IP addresses", style="bold cyan"), style="cyan"))
    if parent_zone is not None and provider == "Cloudflare DNS":
        label = zone[: -(len(parent_zone) + 1)]
        records = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
        records.add_column("Name", style="bold bright_white")
        records.add_column("Type", style="bold cyan")
        records.add_column("Target", style="bold bright_white")
        for nameserver in nameservers:
            records.add_row(label, "NS", nameserver)
        console.print(records)
    else:
        console.print(Text("\n".join(nameservers), style="bold bright_white"), soft_wrap=True)

    resources = [("DigitalOcean", "https://cloud.digitalocean.com/networking/domains")]
    if dashboard:
        resources.append(("Provider dashboard", dashboard))
    resources.extend((("Official guide", help_url), ("DNSSEC help", dnssec_url)))
    console.print(Rule(Text("Links", style="bold cyan"), style="cyan"))
    for label, url in resources:
        line = Text()
        line.append(label + ": ", style="bold cyan")
        line.append(url)
        console.print(line, soft_wrap=True)


def resume(command: str) -> None:
    body = Text()
    body.append("No paid server is created while delegation is pending.\n", style="bold green")
    body.append("Propagation can take up to 48 hours. Resume with the same checkout and setup state:", style="white")
    console.print(Panel(body, title="Resume after DNS propagates", border_style="blue", padding=(1, 2)))
    console.print(Text(command, style="bold bright_cyan"), soft_wrap=True)


@contextmanager
def dns_wait(zone: str, seconds: int):
    message = Text()
    message.append("Watching public DNS for ", style="dim")
    message.append(zone, style="bold bright_white")
    message.append(f" · up to {seconds // 60} minutes · Ctrl-C is safe", style="dim")
    with console.status(message, spinner="dots") as status:

        def update(observed: list[str], remaining: int) -> None:
            detail = ", ".join(observed) if observed else "no delegated nameservers visible yet"
            line = Text()
            line.append("Waiting for DigitalOcean nameservers", style="bold cyan")
            line.append(f" · {detail} · next check in {remaining}s", style="dim")
            status.update(line)

        yield update


def dns_ready(zone: str) -> None:
    console.print(Text(f"✓ Public DNS now delegates {zone} to DigitalOcean.", style="bold green"))


def success(url: str) -> None:
    body = Text()
    body.append("Organization ready\n", style="bold bright_green")
    body.append(url + "\n", style="bold bright_white")
    body.append("Sign in, then share invitation links from organization administration.", style="white")
    console.print(Panel.fit(body, border_style="green", padding=(1, 3)))
