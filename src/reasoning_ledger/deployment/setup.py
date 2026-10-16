"""Interactive operator-owned setup using the existing verified launch commands."""

from __future__ import annotations

import getpass
import hashlib
import io
import ipaddress
import json
import re
import time
from contextlib import redirect_stdout
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import setup_ui
from .digitalocean import DigitalOcean, ProviderUpdateRejected, selection
from .dns import zone_name
from .host import InstallError, inspect_bundle, public_origin
from .private import locked_directory, read_document, read_private, write_document, write_private
from .provision import plan_identity

DIGITALOCEAN_PERMISSIONS = (
    ("Account", ("account:read",)),
    ("Domain", ("domain:read", "domain:create")),
    ("Droplet", ("droplet:read", "droplet:create")),
    ("Firewall", ("firewall:read", "firewall:create", "firewall:update")),
    ("Sizes", ("sizes:read",)),
    ("SSH key", ("ssh_key:read", "ssh_key:create*")),
    ("Tag", ("tag:read", "tag:create")),
)
OPERATOR_DIGITALOCEAN_PERMISSIONS = (
    ("Account", ("account:read",)),
    ("Domain", ("domain:read",)),
    ("Droplet", ("droplet:read",)),
    ("Firewall", ("firewall:read", "firewall:update")),
    ("Tag", ("tag:read",)),
)

CONFIG_KEYS = {
    "format", "release_id", "public_url", "zone", "name", "region", "size", "ssh_key_id", "identity", "admin_cidr",
    "maximum_hourly_usd", "token_file", "models", "monthly_budget_microusd", "openrouter_file",
}
COMPLETION_KEYS = {
    "format", "release_id", "plan_id", "public_url", "organization_id", "compute_backend", "compute_setup",
}
DNS_DELEGATION_KEYS = {"format", "release_id", "provider", "parent_zone", "zone", "parent_nameservers"}
DNS_WAIT_ATTEMPTS = 61
DNS_WAIT_INTERVAL_SECONDS = 10
DIGITALOCEAN_TOKEN_URL = "https://cloud.digitalocean.com/account/api/tokens"
# Personal access and OAuth tokens carry a typed prefix; tokens created before
# prefixes were introduced are 64 lowercase hexadecimal characters.
DIGITALOCEAN_TOKEN = re.compile(r"(?:dop|doo)_v1_[0-9a-f]{64}|[0-9a-f]{64}")
DIGITALOCEAN_TOKEN_LENGTH = len("dop_v1_") + 64
# Provider-typed secret prefixes that never belong in an echoed answer.
_SECRET_PREFIX = re.compile(r"do[por]_v1_[0-9A-Fa-f]{8}|sk-or-v1-[0-9A-Fa-f]{8}")
# Digests of secrets this process saw in echoed answers; they are refused later in the same run.
_EXPOSED_SECRETS: set[str] = set()


def secret_shaped(text: str, *, strict: bool = False) -> bool:
    """Recognize a provider secret; strict also treats one long opaque word as a secret."""
    value = text.strip()
    if _SECRET_PREFIX.search(value):
        return True
    return strict and re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value) is not None


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def read_answer(prompt, *, strict=False):
    """Read one echoed answer and refuse a pasted secret without repeating it."""
    while True:
        value = input(prompt).strip()
        if not secret_shaped(value, strict=strict):
            return value
        _EXPOSED_SECRETS.add(_secret_digest(value))
        for candidate in re.findall(r"do[po]_v1_[0-9a-f]{64}|[A-Za-z0-9_\-]{32,}", value):
            _EXPOSED_SECRETS.add(_secret_digest(candidate))
        setup_ui.exposed_secret()


def ask(prompt, default=None):
    value = read_answer(prompt + (f" [{default}]" if default is not None else "") + ": ")
    if not value and default is not None:
        return str(default)
    if not value:
        raise InstallError("setup requires an explicit value; no resource was approved by an empty answer")
    return value


def confirm(prompt, *, default=True):
    """Ask a yes/no question; repeat until the answer is yes, no, or empty for the default."""
    while True:
        value = read_answer(prompt + (" [Y/n]: " if default else " [y/N]: "), strict=True).casefold()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter yes or no.")


def approve(prompt, phrase):
    phrase = phrase.replace("https://", "").replace("http://", "")
    value = read_answer(prompt + f" Type '{phrase}' to confirm: ").replace("https://", "").replace("http://", "")
    if value != phrase:
        raise InstallError("setup was not confirmed; preserve its directory and resume when ready")


def digitalocean_token(body):
    """Return a DigitalOcean API token after checking its shape locally, before any API call."""
    try:
        value = (body.decode("ascii") if isinstance(body, bytes) else body).strip()
    except UnicodeError:
        raise InstallError("the DigitalOcean token contains non-ASCII characters; "
                           "copy it again from the DigitalOcean token page. No API call was made") from None
    expected = f"expected dop_v1_ followed by 64 lowercase hexadecimal characters ({DIGITALOCEAN_TOKEN_LENGTH} characters)"
    half = len(value) // 2
    if len(re.findall(r"do[por]_v1_", value)) > 1 or (
        len(value) % 2 == 0 and value[:half] == value[half:] and DIGITALOCEAN_TOKEN.fullmatch(value[:half])
    ):
        raise InstallError(f"the DigitalOcean token appears to be pasted more than once ({len(value)} characters; "
                           f"{expected}). Enter it exactly once. No API call was made")
    if any(character.isspace() or not character.isprintable() for character in value):
        raise InstallError("the DigitalOcean token contains spaces, line breaks, or control characters; "
                           f"{expected}. No API call was made")
    if value.startswith("dor_v1_"):
        raise InstallError("this is a DigitalOcean OAuth refresh token, not an API token; create a personal "
                           "access token on the DigitalOcean token page. No API call was made")
    if not DIGITALOCEAN_TOKEN.fullmatch(value):
        raise InstallError(f"this does not look like a DigitalOcean API token ({len(value)} characters; {expected}). "
                           "No API call was made")
    if _secret_digest(value) in _EXPOSED_SECRETS:
        raise InstallError("this DigitalOcean token was displayed in the terminal earlier in this run; revoke it on the "
                           "DigitalOcean token page and create a new one. No API call was made")
    return value


def validate_digitalocean_token(body):
    digitalocean_token(body)


def dollar_budget(value):
    try:
        number = Decimal(value) * 1000000
        if not number.is_finite() or not 0 < number <= 10**15 or number != number.to_integral_value():
            raise ValueError
        return int(number)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise InstallError("enter a positive USD budget with at most six decimal places") from exc


def same_hourly_budget(first, second):
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    try:
        return Decimal(first) == Decimal(second)
    except InvalidOperation:
        return False


PRIVATE_ENTRY_ATTEMPTS = 3


def private_credential(directory, name, prompt, *, minimum, maximum, validate=None):
    """Return a private credential file; `validate` checks the secret's shape before it is saved or used."""
    path = directory / name
    if path.exists() or path.is_symlink():
        body = read_private(path, maximum)
        if validate is not None:
            try:
                validate(body)
            except InstallError as exc:
                raise InstallError(f"saved credential {path} is unusable: {exc}; move it aside and rerun") from None
        return path
    filename = read_answer(prompt + " file path (blank to enter the secret privately): ", strict=True)
    if filename:
        path = Path(filename).expanduser().absolute()
        body = read_private(path, maximum)
        if validate is not None:
            validate(body)
    else:
        for attempt in range(1, PRIVATE_ENTRY_ATTEMPTS + 1):
            body = getpass.getpass(prompt + " (input is hidden): ").strip().encode("utf-8")
            try:
                if not minimum <= len(body) <= maximum:
                    raise InstallError("credential length is invalid")
                if validate is not None:
                    validate(body)
                break
            except InstallError as exc:
                if attempt == PRIVATE_ENTRY_ATTEMPTS:
                    raise
                print(f"{exc}. Nothing was saved; paste it again.")
        write_private(path, body)
    if not minimum <= len(body.strip()) <= maximum:
        raise InstallError("credential length is invalid")
    return path


def replacement_digitalocean_token(directory, expected_account, *, permissions=None):
    from .setup_auth import open_page

    if permissions is None:
        setup_ui.digitalocean_token_recovery()
    else:
        setup_ui.section("Replace your DigitalOcean Team token",
                         "Create a new token in the existing Team. The old token and completed join are retained.")
        setup_ui.digitalocean_access(permissions)
    if confirm("Open the DigitalOcean token page in your browser?"):
        open_page(DIGITALOCEAN_TOKEN_URL)
    index = 2
    while (directory / f"digitalocean-token-{index}").exists() or (directory / f"digitalocean-token-{index}").is_symlink():
        index += 1
        if index > 100:
            raise InstallError("too many replacement token files; preserve the setup directory")
    path = private_credential(
        directory,
        f"digitalocean-token-{index}",
        "Replacement DigitalOcean API token",
        minimum=32,
        maximum=4096,
        validate=validate_digitalocean_token,
    )
    provider = DigitalOcean(digitalocean_token(read_private(path, 4096)))
    try:
        if provider.identity() != expected_account:
            raise InstallError("replacement DigitalOcean token belongs to a different account")
    finally:
        provider.close()
    return path


def close_ssh_with_token_recovery(invoke, state, config_path, config, plan_path, ownership, plan_id, plan):
    def close(token_file):
        return invoke([
            "do-ssh-access", "--token-file", token_file, "--plan", plan_path,
            "--state-dir", ownership, "--confirm-plan", plan_id, "--close", "--wait-seconds", 60,
        ])

    try:
        return config, close(config["token_file"])
    except ProviderUpdateRejected:
        replacement = replacement_digitalocean_token(state, plan["account_identity"])
        config = {**config, "token_file": str(replacement)}
        write_document(config_path, config)
        return config, close(config["token_file"])


def current_public_ipv4(*, transport=None) -> str:
    try:
        with (
            httpx.Client(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client,
            client.stream("GET", "https://api.ipify.org") as response,
        ):
            if response.status_code != 200:
                raise ValueError
            body = bytearray()
            for block in response.iter_bytes(chunk_size=64):
                body.extend(block)
                if len(body) > 64:
                    raise ValueError
        address = ipaddress.IPv4Address(body.decode("ascii").strip())
        if not address.is_global:
            raise ValueError
        return str(address) + "/32"
    except (httpx.HTTPError, UnicodeError, ValueError) as exc:
        raise InstallError("could not detect this computer's public IPv4 address") from exc


def temporary_ssh_cidr(detector=None) -> str:
    from .provision import ssh_access_cidr

    try:
        return ssh_access_cidr((detector or current_public_ipv4)())
    except InstallError:
        return ssh_access_cidr(ask("Current public IPv4 address with /32 for this setup session"))


def collect_configuration(directory, release, *, detect_public_ipv4=None):
    from .setup_auth import open_page, openrouter_login
    from .setup_ssh import enroll_public_key, prepare_identity

    setup_ui.section("DigitalOcean", "Reasoning provisions one server in an account you control.")
    setup_ui.digitalocean_access(DIGITALOCEAN_PERMISSIONS)
    if not (directory / "digitalocean-token").exists() and confirm("Open the DigitalOcean token page in your browser?"):
        open_page(DIGITALOCEAN_TOKEN_URL)
    token = private_credential(directory, "digitalocean-token", "DigitalOcean API token", minimum=32, maximum=4096,
                               validate=validate_digitalocean_token)
    setup_ui.section("Organization address", "Use a dedicated domain, or a subdomain reserved for Reasoning; existing DNS is not copied.")
    zone = zone_name(ask("Domain or subdomain to manage in DigitalOcean (for example reasoning.example.com)"))
    origin = public_origin("https://" + ask("Chat hostname", "chat." + zone))
    setup_ui.section("Secure Shell", "Generate a dedicated deployment key or reuse an existing private key.")
    identity, public = prepare_identity(directory)
    provider = DigitalOcean(digitalocean_token(read_private(token, 4096)))
    try:
        key_id = enroll_public_key(directory, provider, identity, public)
    finally:
        provider.close()
    name = ask("Deployment name", "reasoning-test")
    region = ask("DigitalOcean region", "fra1")
    size = ask("CPU Droplet size", "s-2vcpu-4gb")
    setup_ui.section("Temporary SSH access", "Setup opens SSH only to this computer's current public address, then closes it.")
    admin = temporary_ssh_cidr(detect_public_ipv4)
    setup_ui.ssh_access(admin)
    ceiling = ask("Maximum Droplet hourly USD (excludes tax, transfer, storage, and other services)")
    selection(name=name, region=region, size=size, origin=origin, dns_zone=zone, ssh_key_id=key_id,
              admin_cidr=admin, maximum_hourly_usd=ceiling)
    setup_ui.section("Models and budget", "Connect OpenRouter and set the organization's initial spending policy.")
    models = []
    print("The organization key can use any available OpenRouter text model or router.")
    monthly = dollar_budget(ask("Organization monthly LLM budget in USD (does not cap other uses of this provider key)"))
    router = openrouter_login(directory)
    return {
        "format": 1, "release_id": release, "public_url": origin, "zone": zone, "name": name, "region": region, "size": size,
        "ssh_key_id": key_id, "identity": str(identity), "admin_cidr": admin, "maximum_hourly_usd": ceiling,
        "token_file": str(token), "models": models, "monthly_budget_microusd": monthly, "openrouter_file": str(router),
    }


def validate_configuration(config, release):
    if (
        set(config) != CONFIG_KEYS
        or not isinstance(config["format"], int)
        or isinstance(config["format"], bool)
        or config["format"] != 1
        or config["release_id"] != release
    ):
        raise InstallError("setup configuration does not match the original release; preserve it")
    selection(name=config["name"], region=config["region"], size=config["size"], origin=config["public_url"], dns_zone=config["zone"],
              ssh_key_id=config["ssh_key_id"], admin_cidr=config["admin_cidr"], maximum_hourly_usd=config["maximum_hourly_usd"])
    if (
        not isinstance(config["models"], list) or len(config["models"]) > 100
        or any(not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", model) for model in config["models"])
        or not isinstance(config["monthly_budget_microusd"], int)
        or isinstance(config["monthly_budget_microusd"], bool)
        or not 0 < config["monthly_budget_microusd"] <= 10**15
    ):
        raise InstallError("invalid saved organization LLM policy")
    for field, maximum in (("token_file", 4096), ("identity", 65536), ("openrouter_file", 4096)):
        if not isinstance(config[field], str) or not Path(config[field]).is_absolute():
            raise InstallError("saved credential paths must be absolute")
        read_private(Path(config[field]), maximum)


def validate_completion(value, release, plan_id, config, compute_backend):
    try:
        if (
            not isinstance(value, dict)
            or set(value) != COMPLETION_KEYS
            or not isinstance(value["format"], int)
            or isinstance(value["format"], bool)
            or value["format"] != 1
            or value["release_id"] != release
            or value["plan_id"] != plan_id
            or value["public_url"] != config["public_url"]
            or not isinstance(value["organization_id"], str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["organization_id"])
            or value["compute_backend"] != compute_backend
            or compute_backend == "none" and value["compute_setup"] is not None
            or compute_backend == "modal" and not isinstance(value["compute_setup"], dict)
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise InstallError("invalid completed setup receipt; preserve the setup directory") from exc


def completed_result(value, state, *, protection=None, phase="organization_ready"):
    return {
        "phase": phase,
        "https_ready": True,
        "public_url": value["public_url"],
        "organization_id": value["organization_id"],
        "llm_policy_configured": True,
        "compute_backend": value["compute_backend"],
        "compute_setup": value["compute_setup"],
        "live_user_journey_verified": False,
        "operator_protection": protection,
        "state_dir": str(state),
    }


def validate_dns_delegation(value, release, config):
    try:
        hostname = urlsplit(config["public_url"]).hostname
        if (
            not isinstance(value, dict)
            or set(value) != DNS_DELEGATION_KEYS
            or not isinstance(value["format"], int)
            or isinstance(value["format"], bool)
            or value["format"] != 1
            or value["release_id"] != release
            or value["provider"] != "cloudflare"
            or zone_name(value["parent_zone"]) != value["parent_zone"]
            or zone_name(value["zone"]) != value["zone"]
            or value["zone"] != hostname
            or not value["zone"].endswith("." + value["parent_zone"])
            or not isinstance(value["parent_nameservers"], list)
            or not 1 <= len(value["parent_nameservers"]) <= 10
            or any(not isinstance(item, str) or not item.endswith(".ns.cloudflare.com")
                   for item in value["parent_nameservers"])
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, InstallError) as exc:
        raise InstallError("invalid saved DNS delegation selection; preserve the setup directory") from exc


def dns_state_directory(state: Path, zone: str) -> Path:
    primary = state / "dns"
    journal = primary / "digitalocean-dns.json"
    if not journal.exists() and not journal.is_symlink():
        return primary
    existing = read_document(journal)
    if existing.get("zone") == zone:
        return primary
    suffix = hashlib.sha256(zone.encode("ascii")).hexdigest()[:12]
    return state / ("dns-" + suffix)


def cloudflare_child_candidate(config, delegation):
    hostname = urlsplit(config["public_url"]).hostname
    nameservers = delegation.get("observed_nameservers") if isinstance(delegation, dict) else None
    if (
        isinstance(hostname, str)
        and hostname != config["zone"]
        and hostname.endswith("." + config["zone"])
        and isinstance(nameservers, list)
        and nameservers
        and all(isinstance(item, str) and item.endswith(".ns.cloudflare.com") for item in nameservers)
    ):
        return hostname
    return None


def wait_for_dns_delegation(
    zone,
    initial,
    check,
    *,
    attempts=DNS_WAIT_ATTEMPTS,
    interval=DNS_WAIT_INTERVAL_SECONDS,
    sleep=time.sleep,
):
    """Poll public NS answers for about ten minutes after an operator DNS handoff."""
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 1
        or not isinstance(interval, int)
        or isinstance(interval, bool)
        or interval < 1
    ):
        raise ValueError("DNS polling bounds must be positive integers")
    latest = initial
    with setup_ui.dns_wait(zone, (attempts - 1) * interval) as update:
        for attempt in range(attempts):
            latest = check()
            if latest.get("delegation_verified") is True:
                break
            if attempt + 1 < attempts:
                update(latest.get("observed_nameservers", []), interval)
                sleep(interval)
    if latest.get("delegation_verified") is True:
        setup_ui.dns_ready(zone)
    return latest


def select_compute_backend(state, requested):
    """Retain the initial choice; auto offers Modal with an explicit safe skip."""
    if requested not in {"auto", "none", "modal"}:
        raise InstallError("unsupported setup compute backend")
    choice = state / "compute-choice.json"
    if requested != "auto":
        if choice.exists() or choice.is_symlink():
            value = read_document(choice)
            if set(value) != {"backend"} or value["backend"] not in {"none", "modal"}:
                raise InstallError("saved compute choice is invalid")
            if value["backend"] != requested:
                if any(path.exists() or path.is_symlink() for path in (state / "modal-input.json", state / "setup-completion.json")):
                    raise InstallError("resume the original compute selection; use make compute-setup to enable compute later")
                write_document(choice, {"backend": requested})
        return requested
    if choice.exists() or choice.is_symlink():
        value = read_document(choice)
        if set(value) != {"backend"} or value["backend"] not in {"none", "modal"}:
            raise InstallError("saved compute choice is invalid")
        return value["backend"]
    completion = state / "setup-completion.json"
    if completion.exists() or completion.is_symlink():
        backend = read_document(completion).get("compute_backend")
        if backend not in {"none", "modal"}:
            raise InstallError("completed setup has an invalid compute selection")
    elif (state / "modal-input.json").exists() or (state / "modal-input.json").is_symlink():
        backend = "modal"
    else:
        print("Set up Modal cloud computation alongside chat? You can skip and run make compute-setup later.")
        while True:
            answer = ask("Enable Modal computation now? (yes/no)", "yes").lower()
            if answer in {"yes", "y", "no", "n", "skip"}:
                backend = "modal" if answer in {"yes", "y"} else "none"
                break
            print("Enter yes to configure compute or no to skip it.")
    write_document(choice, {"backend": backend}, new=True)
    return backend


def run_setup(
    bundle: Path,
    directory: Path,
    command,
    *,
    configure_provider=None,
    compute_backend="none",
    configure_compute=None,
    protect_operator=None,
    detect_public_ipv4=None,
):
    """One operator/one journal; every cloud/root approval remains interactive."""
    directory = directory.absolute()
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file() for parent in (directory, *directory.parents)):
        raise InstallError("keep the private setup directory outside Git checkouts; it contains credentials")
    manifest, release, checksum = inspect_bundle(bundle)
    if manifest["dirty"]:
        raise InstallError("interactive setup requires a release built from committed clean source")
    setup_ui.header(release)

    def invoke(arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = command([str(value) for value in arguments])
        if code not in {0, 1}:
            raise InstallError("setup command failed; preserve its journal")
        value = json.loads(output.getvalue())
        if not isinstance(value, dict):
            raise InstallError("setup command returned an invalid receipt")
        return value

    with locked_directory(directory) as state:
        compute_backend = select_compute_backend(state, compute_backend)
        config_path = state / "setup.json"
        resumed = config_path.exists() or config_path.is_symlink()
        if resumed:
            config = read_document(config_path)
        else:
            config = collect_configuration(state, release, detect_public_ipv4=detect_public_ipv4)
            write_document(config_path, config, new=True)
        validate_configuration(config, release)

        def finish(completion, plan_path, ownership, plan_id, plan):
            nonlocal config
            config, closed = close_ssh_with_token_recovery(
                invoke, state, config_path, config, plan_path, ownership, plan_id, plan,
            )
            if closed.get("phase") != "closed":
                setup_ui.warning(
                    "DigitalOcean is still applying the SSH firewall closure. Run make deploy again with this state directory.",
                    title="Server lock in progress",
                )
                return completed_result(completion, state, phase="ssh_access_closing")
            if protect_operator is None:
                from .operator_recovery import protect_deployment_locked

                protection = protect_deployment_locked(state, command, detect_public_ipv4=detect_public_ipv4)
            else:
                protection = protect_operator(state, command)
            config, closed = close_ssh_with_token_recovery(
                invoke, state, config_path, config, plan_path, ownership, plan_id, plan,
            )
            if closed.get("phase") != "closed":
                setup_ui.warning(
                    "DigitalOcean is still applying the SSH firewall closure. Run make deploy again with this state directory.",
                    title="Server lock in progress",
                )
                return completed_result(completion, state, protection=protection, phase="ssh_access_closing")
            setup_ui.ssh_closed()
            if protection.get("mode") == "pending":
                setup_ui.warning(
                    "Operator protection is incomplete. Run make deploy again with this same state directory.",
                    title="Recovery protection pending",
                )
                return completed_result(completion, state, protection=protection, phase="operator_protection_required")
            setup_ui.success(config["public_url"])
            setup_ui.warning(
                "Server and provider setup passed. The two-user chat, beta/gamma, bot, and restart journey still need acceptance.",
                title="Next verification",
            )
            phase = "organization_ready_unprotected" if protection.get("mode") == "deferred" else "organization_ready"
            return completed_result(completion, state, protection=protection, phase=phase)

        retained_binding = None
        if bundle.is_file():
            from .release_state import release_binding, retain_release

            retained_binding = release_binding(manifest, release, checksum, config["public_url"])
            retain_release(bundle, state, retained_binding)
        dns_delegation_path = state / "dns-delegation.json"
        dns_delegation = None
        if dns_delegation_path.exists() or dns_delegation_path.is_symlink():
            dns_delegation = validate_dns_delegation(read_document(dns_delegation_path), release, config)
            if config["zone"] == dns_delegation["parent_zone"]:
                config = {**config, "zone": dns_delegation["zone"]}
                validate_configuration(config, release)
                write_document(config_path, config)
            elif config["zone"] != dns_delegation["zone"]:
                raise InstallError("saved setup DNS zone differs from its confirmed delegation selection")
        from .setup_ssh import verify_saved_identity

        verify_saved_identity(state, config)
        if compute_backend not in {"none", "modal"}:
            raise InstallError("unsupported setup compute backend")
        plan_path = state / "plan.json"
        completion_path = state / "setup-completion.json"
        if completion_path.exists() or completion_path.is_symlink():
            if not plan_path.exists() and not plan_path.is_symlink():
                raise InstallError("completed setup receipt lacks its original deployment plan")
            document = read_document(plan_path)
            plan_id = plan_identity(document)
            plan = document["plan"]
            if any(plan[field] != config[field] for field in ("name", "region", "size", "ssh_key_id", "public_url")):
                raise InstallError("saved deployment plan differs from setup configuration")
            if plan["dns_zone"] != config["zone"]:
                raise InstallError("saved DNS selection differs from setup configuration")
            if not same_hourly_budget(plan["quote"]["maximum_hourly_usd"], config["maximum_hourly_usd"]):
                raise InstallError("saved hourly budget differs from setup configuration")
            completion = read_document(completion_path)
            if completion.get("compute_backend") == "modal" and compute_backend != "modal":
                raise InstallError("resume this setup with --compute-backend modal; do not silently omit its compute selection")
            completion = validate_completion(completion, release, plan_id, config, compute_backend)
            if retained_binding is not None:
                from .release_state import record_current

                record_current(state, retained_binding)
            ownership = state / "ownership"
            return finish(completion, plan_path, ownership, plan_id, plan)
        modal = None
        if compute_backend == "modal":
            from .modal_setup import collect_modal, verify_modal_credentials
            from .release import save_release_catalog

            if "modal-relay-v1" not in manifest.get("installation_features", []):
                raise InstallError("setup needs a current Modal-enabled release bundle")
            save_release_catalog(bundle, manifest, state)
            modal = collect_modal(state, release)
            verify_modal_credentials(modal)
        elif (state / "modal-input.json").exists() or (state / "modal-input.json").is_symlink():
            raise InstallError("resume this setup with --compute-backend modal; do not silently omit its compute selection")
        setup_ui.state(str(state), resumed=resumed)
        setup_ui.section("DNS delegation", "No server is created until the selected zone resolves through DigitalOcean.")
        zone = config["zone"]
        dns_state = dns_state_directory(state, zone)
        if not (dns_state / "digitalocean-dns.json").exists():
            approve("Create or reuse only this DNS zone; existing records and registrar settings will not be changed.", zone)
        invoke(["do-dns-setup", "--token-file", config["token_file"], "--zone", zone, "--state-dir", dns_state, "--confirm-zone", zone])
        delegation = invoke(["do-dns-check", "--zone", zone])
        candidate = cloudflare_child_candidate(config, delegation) if dns_delegation is None else None
        if candidate is not None:
            if any((state / name).exists() or (state / name).is_symlink()
                   for name in ("plan.json", "owner.json", "ownership", "setup-completion.json")):
                raise InstallError("deployment state already exists; the DNS zone cannot be changed safely")
            setup_ui.cloudflare_child_zone(zone, candidate)
            while True:
                answer = input(f"Use {candidate} for both the app address and DigitalOcean DNS zone? [Y/n]: ").strip().casefold()
                if answer in {"", "y", "yes", "n", "no"}:
                    break
                print("Enter yes or no.")
            if answer not in {"n", "no"}:
                dns_delegation = {
                    "format": 1,
                    "release_id": release,
                    "provider": "cloudflare",
                    "parent_zone": zone,
                    "zone": candidate,
                    "parent_nameservers": delegation["observed_nameservers"],
                }
                validate_dns_delegation(dns_delegation, release, config)
                write_document(dns_delegation_path, dns_delegation, new=True)
                config = {**config, "zone": candidate}
                validate_configuration(config, release)
                write_document(config_path, config)
                zone = candidate
                dns_state = dns_state_directory(state, zone)
                if not (dns_state / "digitalocean-dns.json").exists():
                    approve("Create or reuse only this delegated DNS zone; existing Cloudflare records will not be changed.", zone)
                invoke(["do-dns-setup", "--token-file", config["token_file"], "--zone", zone,
                        "--state-dir", dns_state, "--confirm-zone", zone])
                delegation = invoke(["do-dns-check", "--zone", zone])
        if not delegation["delegation_verified"]:
            from .registrar_guides import guide_delegation

            observed = (dns_delegation or {}).get("parent_nameservers", delegation.get("observed_nameservers"))
            guide_delegation(
                state,
                config,
                observed,
                parent_zone=(dns_delegation or {}).get("parent_zone"),
                confirmed_dns_provider=(dns_delegation or {}).get("provider"),
            )
            delegation = wait_for_dns_delegation(
                zone,
                delegation,
                lambda: invoke(["do-dns-check", "--zone", zone]),
            )
        if not delegation["delegation_verified"]:
            from .registrar_guides import resume_command

            setup_ui.resume(resume_command(state, bundle, compute_backend))
            setup_ui.warning("DNS delegation is still propagating. Run make deploy again with this same state directory.",
                             title="Paused safely · no billable server created")
            return {"phase": "dns_delegation_required", "https_ready": False, "creates_billable_resources": False,
                    "state_dir": str(state), "public_url": config["public_url"]}
        session_cidr = temporary_ssh_cidr(detect_public_ipv4)
        if not plan_path.exists() and not plan_path.is_symlink():
            invoke(["do-plan", "--token-file", config["token_file"], "--name", config["name"], "--region", config["region"],
                    "--size", config["size"], "--public-url", config["public_url"], "--dns-zone", zone,
                    "--ssh-key-id", config["ssh_key_id"],
                    "--admin-cidr", session_cidr, "--maximum-hourly-usd", config["maximum_hourly_usd"],
                    "--pin-ssh-host-key", "--output", plan_path])
        document = read_document(plan_path)
        plan_id = plan_identity(document)
        plan = document["plan"]
        if any(plan[field] != config[field] for field in ("name", "region", "size", "ssh_key_id", "public_url")):
            raise InstallError("saved deployment plan differs from setup configuration")
        if plan["dns_zone"] != zone:
            raise InstallError("saved DNS selection differs from setup configuration")
        if not same_hourly_budget(plan["quote"]["maximum_hourly_usd"], config["maximum_hourly_usd"]):
            raise InstallError("saved hourly budget differs from setup configuration")
        owner = state / "owner.json"
        if not owner.exists() and not owner.is_symlink():
            # Password prompts remain in owner-input, outside captured output.
            command(["owner-input", "--email", ask("Owner email"), "--name", ask("Owner name"),
                     "--organization", ask("Organization name"), "--output", str(owner)])
        setup_ui.section("Launch", "Review the exact infrastructure and application policy before approving paid resources.")
        setup_ui.ssh_access(session_cidr)
        ownership = state / "ownership"
        if (ownership / "digitalocean.json").exists() or (ownership / "digitalocean.json").is_symlink():
            invoke(["do-ssh-access", "--token-file", config["token_file"], "--plan", plan_path,
                    "--state-dir", ownership, "--confirm-plan", plan_id, "--open-cidr", session_cidr])
        setup_ui.deployment_review(config, plan, str(state), modal, ssh_cidr=session_cidr)
        print("Launch rechecks current price. Failed launches can leave billable resources; retries must use this same directory.")
        print("Approval also authorizes root installation and provider-delivered SSH host-key pinning on this VM.")
        if modal is not None:
            setup_ui.warning(
                "This is not a Modal dollar/invoice cap. Approval authorizes persistent relays and on-demand paid Modal VMs. "
                "Provisioning, image pulls, cleanup, supervisor builds, and egress can incur usage outside the app CPU-time quota.",
                title="Modal billing boundary",
            )
        approve("Create/resume this exact deployment and configure its organization LLM policy?", "launch " + config["public_url"])
        result = invoke(["do-launch", "--token-file", config["token_file"], "--plan", plan_path, "--state-dir", ownership,
                         "--bundle", bundle.absolute(), "--identity", config["identity"], "--owner-file", owner,
                         "--confirm-plan", plan_id, "--confirm-release", release, "--ssh-cidr", session_cidr, "--wait-seconds", 60])
        if result.get("https_ready") is not True:
            setup_ui.warning("Provider or application readiness is pending. Resume this exact setup; do not start a new deployment.",
                             title="Launch in progress")
            return result
        setup_ui.section("Final configuration", "Applying the budget and encrypted provider credential "
                         "for all available OpenRouter models.")
        if configure_provider is None:
            from .setup_provider import configure_openrouter

            configure_provider = configure_openrouter
        configure_provider(config, read_document(owner), result["owner_setup"]["organization_id"], state)
        compute_result = None
        if modal is not None:
            if configure_compute is None:
                from .modal_setup import owned_modal_remote, setup_modal

                def configure_compute(value, config, owner, organization, directory):
                    return setup_modal(value, config, owner, organization, directory,
                                       lambda payload, operation: owned_modal_remote(config, document, state / "ownership", bundle,
                                                                                     payload, operation))

            compute_result = configure_compute(modal, config, read_document(owner), result["owner_setup"]["organization_id"], state)
        completion = {
            "format": 1,
            "release_id": release,
            "plan_id": plan_id,
            "public_url": config["public_url"],
            "organization_id": result["owner_setup"]["organization_id"],
            "compute_backend": compute_backend,
            "compute_setup": compute_result,
        }
        validate_completion(completion, release, plan_id, config, compute_backend)
        write_document(completion_path, completion, new=True)
        if retained_binding is not None:
            from .release_state import record_current

            record_current(state, retained_binding)
        return finish(completion, plan_path, ownership, plan_id, plan)
