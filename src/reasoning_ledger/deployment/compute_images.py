"""Add approved images to a running Modal compute installation.

Only new catalog entries and new relays are added. Installed images, existing relays
and their jobs are unchanged. The new relays reuse an existing relay's Modal profile
on the chat host; Modal credentials never reach the operator computer.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import getpass
import json
import re
import secrets
import time
from pathlib import Path

import httpx

from reasoning_ledger.server.compute_catalog import ComputeImage, fits, load_catalog, resources
from reasoning_ledger.server.compute_worker import enrollment

from .enrollment import ScalerClient, enroll_worker
from .host import InstallError, inspect_bundle, public_origin
from .modal_setup import owned_modal_remote
from .private import locked_directory, read_document, read_private, write_document
from .release import save_release_catalog
from .release_state import archived_release, current_binding, operator_origin
from .setup import approve, ask, close_ssh_with_token_recovery, temporary_ssh_cidr
from .setup_upgrade import _ssh_action

RELAY_PREFIX = "reasoning-modal-"
# Long enough to resume an interrupted enrollment; revoked as soon as the relays run.
GRANT_SECONDS = 6 * 3600
POLICY_FIELDS = ("allowed_images", "monthly_cpu_seconds", "max_active_jobs", "max_pending_jobs", "requests_per_user_15_minutes")


class AdminSession:
    """A bounded organization-administrator session; the password is never saved."""

    def __init__(self, origin, email, password, *, transport=None):
        self.client = httpx.Client(base_url=origin, timeout=10, trust_env=False, follow_redirects=False, transport=transport)
        try:
            session = self.request("POST", "/api/auth/login", {"email": email, "password": password, "transport": "bearer"})
            if not isinstance(session, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{16,2048}", str(session.get("access_token", ""))):
                raise InstallError("invalid administrator login response")
            self.client.headers["Authorization"] = "Bearer " + session["access_token"]
        except BaseException:
            self.client.close()
            raise

    def request(self, method, endpoint, body=None):
        try:
            with self.client.stream(method, endpoint, **({"json": body} if body is not None else {})) as response:
                if response.status_code not in {200, 201, 204}:
                    raise InstallError(f"the server refused {method} {endpoint} (HTTP {response.status_code}); preserve the original state")
                content = bytearray()
                for part in response.iter_bytes(chunk_size=8192):
                    content.extend(part)
                    if len(content) > 1024**2:
                        raise InstallError("server response exceeds its allowance")
                return None if response.status_code == 204 else json.loads(content)
        except (httpx.HTTPError, ValueError):
            raise InstallError("server reply is uncertain; preserve and resume the original addition") from None

    def close(self):
        try:
            self.request("POST", "/api/auth/logout", {})
        finally:
            self.client.close()


def plan_addition(configuration, workers, approved):
    """Choose the new images and the relay whose Modal profile the new relays reuse."""
    try:
        previous = configuration["images"]
        installed = {record["id"]: ComputeImage.parse(record) for record in previous}
        if not installed or len(installed) != len(previous):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise InstallError("the server reports no installed compute images; run make compute-setup first") from None
    for key, image in approved.items():
        if key in installed and installed[key] != image:
            raise InstallError(f"the approved catalog changes installed image {key}; adding images never replaces one")
    additions = sorted(key for key in approved if key not in installed)
    if not additions:
        raise InstallError("every image in the approved catalog is already installed")
    if not isinstance(workers, list) or any(not isinstance(worker, dict) for worker in workers):
        raise InstallError("invalid compute worker list")
    relays = [worker for worker in workers if str(worker.get("name", "")).startswith(RELAY_PREFIX)
              and worker.get("revoked_at") is None and worker.get("retired_at") is None and not worker.get("draining")]
    if not relays:
        raise InstallError("no active Modal relay can lend its profile; run make compute-setup first")
    source = relays[0]
    try:
        if not re.fullmatch(r"[a-f0-9]{32}", source["id"]):
            raise ValueError
        limits = resources(source["resources"])
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid Modal relay identity or limits") from None
    for key in additions:
        if not fits(limits, approved[key].maximum_resources):
            raise InstallError(f"the Modal relay limits exceed image {key}'s maximum resources")
    images = {**installed, **{key: approved[key] for key in additions}}
    return {"previous": previous, "images": [images[key].record() for key in sorted(images)], "additions": additions,
            "source_worker": source["id"], "resources": limits}


def run_add_images(directory: Path, command, *, detect_public_ipv4=None, remote=owned_modal_remote, transport=None, wait=time.sleep):
    """The caller synchronizes the operator first. Changes need typed consent and a fresh SSH window."""
    with locked_directory(directory.absolute()) as state:
        current = current_binding(state)
        bundle = archived_release(state, current["release_id"])
        manifest, release, checksum = inspect_bundle(bundle, current["bundle_sha256"])
        features = manifest.get("installation_features", [])
        if release != current["release_id"] or checksum != current["bundle_sha256"] or "modal-relay-v1" not in features:
            raise InstallError("synchronize an installed Modal-enabled release before adding images")
        if "modal-add-images-v1" not in features:
            raise InstallError("upgrade the server to a release that can add Modal images (make upgrade), then retry")
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config, document = read_document(config_path), read_document(plan_path)
        origin = public_origin(operator_origin(config, document, current))
        work = state / "compute-images"
        with locked_directory(work):
            save_release_catalog(bundle, manifest, work)
            target_path = work / "target.json"
            if target_path.exists() or target_path.is_symlink():
                target = read_document(target_path)
            else:
                bundled = work / "release-compute-images.json"
                completion = state / "setup-completion.json"
                organization = read_document(completion)["organization_id"] if completion.exists() else None
                target = {
                    "organization_id": ask("Existing organization ID", organization),
                    "email": ask("Organization administrator email"),
                    "catalog": str(Path(ask("Approved published compute-image catalog file",
                                            bundled if bundled.exists() else None)).expanduser().absolute()),
                    "relays": int(ask("New Modal relays for the added images (simultaneous jobs)", 1)),
                    "addition_id": secrets.token_hex(32),
                }
                if not re.fullmatch(r"[a-f0-9]{32}", target["organization_id"]) or not 1 <= target["relays"] <= 8:
                    raise InstallError("choose a 32-character organization ID and one through eight relays")
                write_document(target_path, target, new=True)
            read_private(Path(target["catalog"]), 65536)
            approved = load_catalog(Path(target["catalog"]))
            password = getpass.getpass("Organization administrator password (not saved): ")
            endpoint = "/api/organizations/" + target["organization_id"]
            admin = AdminSession(origin, target["email"], password, transport=transport)
            try:
                configuration = admin.request("GET", endpoint + "/compute-configuration")
                workers = admin.request("GET", endpoint + "/compute-workers")
            finally:
                admin.close()
            journal = _journal(work, target, origin, release, configuration, workers, approved)
            plan = journal["binding"]
            print(f"Site: {origin}\nOrganization: {target['organization_id']}")
            for key in plan["additions"]:
                image = approved[key]
                print(f"Add image {key}: {image.name}, {image.reference}")
            print(f"New relays: {target['relays']}, each running one job at a time within {plan['resources']}.")
            print(f"They reuse the Modal profile of relay {plan['source_worker']} on the chat host; jobs bill to that Modal account.")
            print("The compute policy will allow the added images. Installed images and existing relays are unchanged.")
            print("The chat server restarts once to load the catalog; replies in progress can be interrupted. Pick a quiet time.")
            approve("Add these images and relays?", "add images " + origin)
            plan_id = document["plan_id"] if "plan_id" in document else read_document(state / "setup-completion.json")["plan_id"]
            cidr = temporary_ssh_cidr(detect_public_ipv4)
            try:
                config, opened = _ssh_action(
                    command, state, config_path, config, plan_path, plan_id, ownership, ["--open-cidr", cidr, "--wait-seconds", 60]
                )
                if opened.get("phase") != "open":
                    raise InstallError("temporary SSH access is unverified")
                result = add_images(work, journal, target, origin, password,
                                    lambda payload, operation: remote(config, document, ownership, bundle, payload, operation),
                                    transport=transport, wait=wait)
            finally:
                config, closed = close_ssh_with_token_recovery(
                    lambda arguments: _invoke(command, arguments), state, config_path, config, plan_path, ownership, plan_id,
                    document["plan"],
                )
                if closed.get("phase") != "closed":
                    raise InstallError("SSH closure is unverified; run make doctor with the same deployment state")
            print("Images added. Run a small job on each added image; live execution is not claimed until one succeeds.")
            return {**result, "ssh_closed": True}


def _journal(work, target, origin, release, configuration, workers, approved):
    path = work / "addition.json"
    if path.exists() or path.is_symlink():
        journal = read_document(path)
        binding = journal.get("binding", {})
        if (set(journal) != {"binding", "policy_phase", "grant_phase", "grant", "revoke_phase"}
                or binding.get("release_id") != release or binding.get("public_url") != origin):
            raise InstallError("the saved image addition differs from this release or site; finish it with its original release")
        return journal
    plan = plan_addition(configuration, workers, approved)
    binding = {"public_url": origin, "organization_id": target["organization_id"], "release_id": release,
               "addition_id": target["addition_id"], "relays": target["relays"], **plan}
    journal = {"binding": binding, "policy_phase": "not_requested", "grant_phase": "not_requested", "grant": None,
               "revoke_phase": "not_requested"}
    write_document(path, journal, new=True)
    return journal


def add_images(work, journal, target, origin, password, remote, *, transport=None, wait=time.sleep):
    """Catalog first, then policy and enrollment, then relays, then revoke the short-lived grant."""
    plan = journal["binding"]
    path = work / "addition.json"
    base = {"format": 1, "release_id": plan["release_id"], "addition_id": plan["addition_id"], "public_url": origin,
            "previous": plan["previous"], "images": plan["images"], "source_worker": plan["source_worker"], "workers": []}
    receipt = remote(base, "extend")
    if receipt != {"addition_id": plan["addition_id"], "catalog_installed": True, "relays_active": 0}:
        raise InstallError("image catalog extension receipt is invalid")
    endpoint = "/api/organizations/" + plan["organization_id"]
    admin = None
    for _ in range(30):
        try:
            admin = AdminSession(origin, target["email"], password, transport=transport)
            break
        except InstallError:
            wait(2)
    if admin is None:
        raise InstallError("the chat server did not come back after loading the catalog; check it, then resume")
    complete = False
    try:
        configuration = admin.request("GET", endpoint + "/compute-configuration")
        if not isinstance(configuration, dict) or configuration.get("images") != plan["images"]:
            raise InstallError("the running server does not report the extended catalog")
        policy = configuration.get("policy")
        if not isinstance(policy, dict) or not isinstance(policy.get("allowed_images"), list):
            raise InstallError("invalid compute policy")
        if not set(plan["additions"]) <= set(policy["allowed_images"]):
            journal["policy_phase"] = "setting"
            write_document(path, journal)
            # Every other policy field is kept exactly as the administrator set it.
            desired = {key: policy[key] for key in POLICY_FIELDS if key in policy}
            desired["allowed_images"] = sorted(set(policy["allowed_images"]) | set(plan["additions"]))
            changed = admin.request("PUT", endpoint + "/compute-policy", desired)
            if not isinstance(changed, dict) or not set(plan["additions"]) <= set(changed.get("allowed_images", [])):
                raise InstallError("the compute policy change is unverified")
        journal["policy_phase"] = "ready"
        write_document(path, journal)
        label = RELAY_PREFIX + plan["addition_id"][:16]
        if journal["grant_phase"] != "ready":
            if journal["grant_phase"] != "not_requested":
                raise InstallError("the enrollment grant was requested but not saved; revoke it in Settings, then reconcile")
            grants = admin.request("GET", endpoint + "/compute-scalers")
            if not isinstance(grants, list) or any(isinstance(row, dict) and row.get("name") == label for row in grants):
                raise InstallError("an enrollment grant for this addition exists without its saved credential; revoke it first")
            journal["grant_phase"] = "creating"
            write_document(path, journal)
            grant = admin.request("POST", endpoint + "/compute-scalers", {
                "name": label, "image_ids": plan["additions"], "resources": plan["resources"],
                "maximum_workers": plan["relays"], "lifetime_seconds": GRANT_SECONDS})
            if (not isinstance(grant, dict) or not re.fullmatch(r"rns_[A-Za-z0-9_-]{43}", str(grant.get("token", "")))
                    or not re.fullmatch(r"[a-f0-9]{32}", str(grant.get("id", "")))):
                raise InstallError("the enrollment grant returned an invalid credential; preserve its recorded intent")
            journal.update(grant_phase="ready", grant=grant)
            write_document(path, journal)
        grant = journal["grant"]
        workers = []
        scaler = None
        try:
            for index in range(plan["relays"]):
                relay = work / f"relay-{index}"
                with locked_directory(relay):
                    pass
                registration = relay / "registration.json"
                # A saved registration is a verified enrollment; resuming does not need the grant again.
                if not registration.exists() and not registration.is_symlink():
                    scaler = scaler or ScalerClient(origin, grant["token"], transport=transport)
                    enroll_worker(scaler, relay / "enroll", origin, plan["organization_id"], label + f"-{index}",
                                  plan["additions"], plan["resources"], registration, relay / "catalog.json")
                workers.append({"enrollment": enrollment(origin, read_document(registration))})
        finally:
            if scaler is not None:
                scaler.close()
        result = remote({**base, "workers": workers}, "relays")
        if result != {"addition_id": plan["addition_id"], "relays_active": len(workers), "live_compute_verified": False}:
            raise InstallError("Modal relay activation receipt is invalid")
        if journal["revoke_phase"] != "ready":
            journal["revoke_phase"] = "revoking"
            write_document(path, journal)
            admin.request("DELETE", endpoint + "/compute-scalers/" + grant["id"])
            journal["revoke_phase"] = "ready"
            write_document(path, journal)
        complete = True
        completion = {"addition_id": plan["addition_id"], "images_added": plan["additions"], "relays_active": len(workers),
                      "workers": [worker["enrollment"]["id"] for worker in workers], "live_compute_verified": False}
        write_document(work / "completion.json", completion, new=not (work / "completion.json").exists())
        return completion
    finally:
        try:
            admin.close()
        except InstallError:
            if complete:
                raise


def _invoke(command, arguments):
    from .setup_upgrade import _invoke as invoke

    return invoke(command, arguments)
