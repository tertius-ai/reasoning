"""Interactive Modal selection and recoverable organization relay enrollment."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import httpx

from reasoning_ledger.server.compute_catalog import ComputeImage, canonical, fits, load_catalog, resources
from reasoning_ledger.server.compute_modal import SUPERVISOR_IMAGE, ModalSDK, modal_profile
from reasoning_ledger.server.compute_worker import enrollment

from .enrollment import ScalerClient, enroll_worker
from .host import InstallError, public_origin
from .private import locked_directory, protected_temporary_directory, read_document, read_private, write_document


def collect_modal(directory, release):
    from .setup import ask
    from .setup_auth import modal_login

    path = directory / "modal-input.json"
    if path.exists() or path.is_symlink():
        value = read_document(path)
    else:
        bundled = directory / "release-compute-images.json"
        catalog_path = bundled if bundled.exists() or bundled.is_symlink() else Path(ask(
            "Approved published compute-image catalog file path (this release has no bundled catalog)")).expanduser().absolute()
        read_private(catalog_path, 65536)
        images = load_catalog(catalog_path)
        if not images:
            raise InstallError("Modal needs a nonempty approved, published immutable image catalog")
        print("Modal account signup/payment and provider-wide spending limits remain in your Modal workspace.")
        print("Tokens are kept in private relay state, never in a research image or submitted program.")
        identifier, secret = modal_login(directory)
        limits = {key: min(image.maximum_resources[key] for image in images.values())
                  for key in next(iter(images.values())).maximum_resources}
        limits["cpu"] = int(ask("Maximum logical CPUs per compute job", min(4, limits["cpu"])))
        limits["memory_bytes"] = int(ask("Maximum MiB memory per compute job", min(4096, limits["memory_bytes"] // 1024**2))) * 1024**2
        limits["wall_time_seconds"] = int(ask("Maximum seconds per compute job", min(3600, limits["wall_time_seconds"])))
        limits["artifact_bytes"] = min(limits["artifact_bytes"], limits["disk_bytes"] // 2)
        value = {"format": 1, "release_id": release, "images": [images[key].record() for key in sorted(images)],
                 "token_id_file": str(identifier), "token_secret_file": str(secret),
                 "app": ask("Modal application name", "reasoning-compute"), "environment": ask("Modal environment", "main"),
                 "supervisor_image": SUPERVISOR_IMAGE, "resources": limits,
                 "workers": int(ask("Maximum simultaneous Modal jobs", 2)),
                 "monthly_cpu_seconds": int(ask("Organization monthly compute quota, logical CPU-seconds", 100000))}
        validate_modal_input(value, release)
        write_document(path, value, new=True)
    validate_modal_input(value, release)
    return value


def validate_modal_input(value, release):
    try:
        if (not isinstance(value, dict) or set(value) != {"format", "release_id", "images", "token_id_file", "token_secret_file",
                "app", "environment", "supervisor_image", "resources", "workers", "monthly_cpu_seconds"}
                or type(value["format"]) is not int or value["format"] != 1 or value["release_id"] != release
                or type(value["workers"]) is not int or not 1 <= value["workers"] <= 8
                or type(value["monthly_cpu_seconds"]) is not int or not 1 <= value["monthly_cpu_seconds"] <= 10**12
                or not isinstance(value["images"], list) or not 1 <= len(value["images"]) <= 32):
            raise ValueError
        images = [ComputeImage.parse(record) for record in value["images"]]
        if len({image.id for image in images}) != len(images) or any(not fits(resources(value["resources"]), image.maximum_resources)
                                                                  for image in images):
            raise ValueError
        profile_from_input(value)
    except (KeyError, TypeError, ValueError, OSError):
        raise InstallError("invalid Modal selection, image approvals, private credentials, or compute limits") from None


def profile_from_input(value):
    credentials = {}
    for field in ("token_id", "token_secret"):
        path = Path(value[field + "_file"])
        if not path.is_absolute():
            raise InstallError("Modal credential paths must be absolute")
        credentials[field] = read_private(path, 4096).decode().strip()
    return modal_profile({"format": 1, **credentials,
                          **{key: value[key] for key in ("app", "environment", "supervisor_image", "resources")}})


def verify_modal_credentials(value):
    """Read-only authentication probe before any billable DO request."""
    try:
        sdk = ModalSDK(profile_from_input(value))
        sdk.connect().hello()
    except Exception:
        raise InstallError("Modal authentication failed or the optional SDK is absent; install the modal extra "
                           "and verify the saved private credentials before launching") from None


def configure_modal_organization(value, config, owner, organization, directory, *, transport=None):
    """Grant creation is not blindly retried; worker enrollment uses saved nonces."""
    origin = public_origin(config["public_url"])
    if not re.fullmatch(r"[a-f0-9]{32}", organization) or not re.fullmatch(r"[a-f0-9]{32}", owner["setup_id"]):
        raise InstallError("invalid Modal organization/owner identity")
    desired = {"allowed_images": sorted(record["id"] for record in value["images"]),
               "monthly_cpu_seconds": value["monthly_cpu_seconds"], "max_active_jobs": value["workers"]}
    label = "reasoning-modal-" + owner["setup_id"]
    binding = {"public_url": origin, "organization_id": organization, "selection": value, "name": label}
    path = directory / "modal-organization.json"
    if path.exists() or path.is_symlink():
        journal = read_document(path)
        if (set(journal) != {"binding", "policy_phase", "grant_phase", "grant"} or journal["binding"] != binding
                or journal["policy_phase"] not in {"not_requested", "setting", "ready"}
                or journal["grant_phase"] not in {"not_requested", "creating", "ready"}):
            raise InstallError("Modal setup differs from its original organization, approvals, or grant")
    else:
        journal = {"binding": binding, "policy_phase": "not_requested", "grant_phase": "not_requested", "grant": None}
        write_document(path, journal, new=True)
    with httpx.Client(base_url=origin, timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        def request(method, endpoint, body=None):
            try:
                with client.stream(method, endpoint, json=body) as response:
                    if response.status_code not in {200, 201, 204}:
                        raise InstallError("Modal organization setup was rejected; preserve the original state")
                    content = bytearray()
                    for part in response.iter_bytes(chunk_size=8192):
                        content.extend(part)
                        if len(content) > 256 * 1024:
                            raise InstallError("Modal organization response exceeds its allowance")
                    return None if response.status_code == 204 else json.loads(content)
            except (httpx.HTTPError, ValueError):
                raise InstallError("Modal organization reply is uncertain; preserve and reconcile the original journal") from None

        session = request("POST", "/api/auth/login", {"email": owner["email"], "password": owner["password"], "transport": "bearer"})
        if not isinstance(session, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{16,2048}", str(session.get("access_token", ""))):
            raise InstallError("invalid Modal setup login response")
        client.headers["Authorization"] = "Bearer " + session["access_token"]
        complete = False
        try:
            endpoint = "/api/organizations/" + organization
            configuration = request("GET", endpoint + "/compute-configuration")
            if not isinstance(configuration, dict) or configuration.get("images") != value["images"]:
                raise InstallError("installation compute images differ from the approved Modal selection")
            actual = configuration.get("policy")
            if actual != desired:
                if (actual != {"allowed_images": [], "monthly_cpu_seconds": 0, "max_active_jobs": 2}
                        or journal["policy_phase"] != "not_requested"):
                    raise InstallError("compute policy changed; Modal setup will not overwrite administrator edits")
                journal["policy_phase"] = "setting"
                write_document(path, journal)
                if request("PUT", endpoint + "/compute-policy", desired) != desired:
                    raise InstallError("Modal compute policy is unverified")
            journal["policy_phase"] = "ready"
            write_document(path, journal)
            if journal["grant_phase"] != "ready":
                if journal["grant_phase"] != "not_requested":
                    raise InstallError("Modal scaler grant creation is uncertain; recover its credential or revoke it "
                                       "before manual reconciliation")
                grants = request("GET", endpoint + "/compute-scalers")
                if not isinstance(grants, list) or any(not isinstance(row, dict) for row in grants):
                    raise InstallError("invalid Modal scaler grant metadata")
                if any(row.get("name") == label for row in grants):
                    raise InstallError("Modal grant already exists without its saved credential; reconcile before creating another")
                journal["grant_phase"] = "creating"
                write_document(path, journal)
                grant = request("POST", endpoint + "/compute-scalers", {"name": label, "image_ids": desired["allowed_images"],
                                    "resources": value["resources"], "maximum_workers": value["workers"], "lifetime_seconds": 31536000})
                if not isinstance(grant, dict) or not re.fullmatch(r"rns_[A-Za-z0-9_-]{43}", str(grant.get("token", ""))):
                    raise InstallError("Modal scaler grant returned an invalid credential; preserve its recorded intent")
                journal.update(grant_phase="ready", grant=grant)
                write_document(path, journal)
            grant = journal["grant"]
            if not isinstance(grant, dict) or not re.fullmatch(r"rns_[A-Za-z0-9_-]{43}", str(grant.get("token", ""))):
                raise InstallError("invalid saved Modal scaler grant")
            scaler = ScalerClient(origin, grant["token"], transport=transport)
            try:
                workers = []
                for index in range(value["workers"]):
                    worker_dir = directory / f"modal-worker-{index}"
                    with locked_directory(worker_dir):
                        pass
                    registration = worker_dir / "registration.json"
                    catalog = worker_dir / "catalog.json"
                    enroll_worker(scaler, worker_dir / "enroll", origin, organization, label + f"-{index}",
                                  desired["allowed_images"], value["resources"], registration, catalog)
                    record = read_document(registration)
                    profile = profile_from_input(value)
                    profile_path = worker_dir / "modal-profile.json"
                    if profile_path.exists() or profile_path.is_symlink():
                        if read_document(profile_path) != profile:
                            raise InstallError("Modal relay profile changed; preserve its original enrollment")
                    else:
                        write_document(profile_path, profile, new=True)
                    workers.append({"enrollment": enrollment(origin, record), "profile": profile})
            finally:
                scaler.close()
            complete = True
        finally:
            try:
                request("POST", "/api/auth/logout", {})
            except InstallError:
                if complete:
                    raise
    return workers


def setup_modal(value, config, owner, organization, directory, remote):
    """Install catalog first, then enroll, then activate exactly those relays."""
    profile = profile_from_input(value)
    identity = hashlib.sha256(canonical({"selection": value, "organization_id": organization}).encode()).hexdigest()
    base = {"format": 1, "release_id": value["release_id"], "setup_id": identity,
            "public_url": config["public_url"], "images": value["images"], "workers": []}
    receipt = remote(base, "prepare")
    if receipt != {"setup_id": identity, "catalog_installed": True, "relays_active": False}:
        raise InstallError("Modal catalog preparation receipt is invalid")
    workers = configure_modal_organization(value, config, owner, organization, directory)
    # The install transport sends only bounded private configuration, not the
    # owner's password/session, DigitalOcean token, or OpenRouter key.
    assert all(worker["profile"] == profile for worker in workers)
    result = remote({**base, "workers": workers}, "activate")
    if not isinstance(result, dict) or result != {"setup_id": identity, "relays_active": len(workers), "live_compute_verified": False}:
        raise InstallError("Modal relay activation receipt is invalid")
    return result


def owned_modal_remote(config, document, directory, bundle, value, operation, *, command=None):
    """Root changes use the fresh owned VM and its original provider-pinned key."""
    import shlex
    import subprocess

    from .cli import STAGE_SCRIPT, ssh_arguments
    from .digitalocean import DigitalOcean
    from .modal_host import validate_addition, validate_request
    from .private import write_private
    from .release_state import operator_origin
    from .server_install import ServerInstaller
    from .ssh_host import server_trust

    addition = operation in {"extend", "relays"}
    validate_addition(value, operation) if addition else validate_request(value, operation)
    if value["public_url"] != operator_origin(config, document):
        raise InstallError("Modal host request must use the originally confirmed organization URL")
    provider = DigitalOcean(read_private(Path(config["token_file"]), 4096).decode().strip())
    try:
        installer = ServerInstaller(provider, document, bundle, existing_server=True)
        features = installer.manifest.get("installation_features", [])
        if "modal-relay-v1" not in features:
            raise InstallError("build a current Modal-enabled release before enabling compute")
        if addition and "modal-add-images-v1" not in features:
            raise InstallError("upgrade the server to a release that can add Modal images before adding them")
        with locked_directory(directory):
            installer.state = read_document(directory / "digitalocean.json")
            installer._validate_state()
            installer._load_ssh_access(directory)
            previous = read_document(directory / "digitalocean-installation.json")
            address = installer._ready_host()
            request = {"format": 1, "plan_id": installer.identity, "deployment_id": document["deployment_id"],
                       "droplet_id": installer.state["resources"]["droplet"]["id"]}
            intent = {"format": 1, "binding": installer.binding, "request": request, "ssh_host": address,
                      "resources": {kind: installer.state["resources"][kind]["id"] for kind in installer.kinds}}
            installer._validate_installation(previous, intent)
            if previous["phase"] != "installed" or value["release_id"] != installer.binding["release_id"]:
                raise InstallError("Modal relays require the original verified installed release")
            with server_trust(document, directory, address, request) as trusted:
                options = ssh_arguments(address, Path(config["identity"]), trusted)

                def run(arguments, *, capture=False):
                    if command is not None:
                        return command(arguments, capture=capture)
                    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, timeout=1200, check=False)
                    if result.returncode or capture and len(result.stdout) > 65536:
                        raise InstallError("Modal relay SSH installation failed; preserve original enrollment and staging inputs")
                    return result.stdout if capture else None

                body = canonical(value).encode()
                stage = hashlib.sha256(operation.encode() + body).hexdigest()
                destination = "/var/lib/reasoning-deploy/staging/" + stage
                run(["ssh", *options, "root@" + address, shlex.join(["python3", "-c", STAGE_SCRIPT, destination])])
                with protected_temporary_directory("reasoning-modal-install-") as temporary:
                    path = temporary / "modal-input.json"
                    write_private(path, body)
                    run(["scp", *options, str(path), "root@" + address + ":" + destination + "/modal-input.json"])
                python = "/opt/reasoning/releases/" + value["release_id"] + "/venv/bin/python"
                output = run(["ssh", *options, "root@" + address, shlex.join([python, "-m", "reasoning_ledger.deployment.modal_host",
                              "--input", destination + "/modal-input.json", "--operation", operation])], capture=True)
                if installer._ready_host() != address:
                    raise InstallError("owned Modal relay host changed during installation")
                try:
                    return json.loads(output)
                except (TypeError, ValueError):
                    raise InstallError("invalid private Modal host installation receipt") from None
    finally:
        provider.close()
