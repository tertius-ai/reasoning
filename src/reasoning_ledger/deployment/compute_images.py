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


def run_refresh_relays(directory: Path, command, *, detect_public_ipv4=None, remote=owned_modal_remote):
    """Move the installed Modal relays onto the installed release's relay runtime. The caller synchronizes first."""
    with locked_directory(directory.absolute()) as state:
        current = current_binding(state)
        bundle = archived_release(state, current["release_id"])
        manifest, release, checksum = inspect_bundle(bundle, current["bundle_sha256"])
        if release != current["release_id"] or checksum != current["bundle_sha256"]:
            raise InstallError("synchronize the installed release before updating Modal relays")
        if "modal-relay-refresh-v1" not in manifest.get("installation_features", []):
            raise InstallError("upgrade the server to a release that can update Modal relays (make upgrade), then retry")
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config, document = read_document(config_path), read_document(plan_path)
        origin = public_origin(operator_origin(config, document, current))
        print(f"Site: {origin}\nRelease: {release}")
        print("Every Modal relay on the chat host moves to this release's relay code. Enrollments, Modal credentials,")
        print("catalogs and limits are unchanged. Drain every relay worker first (Administration → Compute → Drain worker);")
        print("the update refuses a relay that can still take jobs or has an unsettled one. Resume claims afterwards.")
        approve("Update the Modal relays?", "update relays " + origin)
        plan_id = document["plan_id"] if "plan_id" in document else read_document(state / "setup-completion.json")["plan_id"]
        cidr = temporary_ssh_cidr(detect_public_ipv4)
        try:
            config, opened = _ssh_action(
                command, state, config_path, config, plan_path, plan_id, ownership, ["--open-cidr", cidr, "--wait-seconds", 60]
            )
            if opened.get("phase") != "open":
                raise InstallError("temporary SSH access is unverified")
            result = remote(config, document, ownership, bundle, {"format": 1, "release_id": release, "public_url": origin},
                            "refresh")
            if (not isinstance(result, dict) or result.get("release_id") != release
                    or result.get("live_compute_verified") is not False):
                raise InstallError("Modal relay update receipt is invalid")
        finally:
            config, closed = close_ssh_with_token_recovery(
                lambda arguments: _invoke(command, arguments), state, config_path, config, plan_path, ownership, plan_id,
                document["plan"],
            )
            if closed.get("phase") != "closed":
                raise InstallError("SSH closure is unverified; run make doctor with the same deployment state")
        print("Relays updated. Resume claims on each relay worker, then run a small job; live execution is not claimed "
              "until one succeeds.")
        return {**result, "ssh_closed": True}


def describe_probe(receipt):
    """Plain lines for the operator: the verdict, then what each VM showed."""
    probe = receipt["probe"]
    lines = [f"Relay used: {receipt['relay']}"]
    if probe.get("error"):
        lines.append(f"The probe stopped during {probe['error']['stage']} ({probe['error']['exception_type']}).")
    writer, reader, container = probe.get("writer") or {}, probe.get("reader") or {}, probe.get("container") or {}
    if writer.get("mount"):
        lines.append(f"Writer VM: the Volume is a {writer['mount']['filesystem']} mount; writing and committing took "
                     f"{writer.get('seconds')} s.")
    if reader.get("environment_mount"):
        mount = reader["environment_mount"]
        lines.append(f"Job VM: the environment is a {mount['filesystem']} mount with options {mount['mount_options']} "
                     f"({'read-only' if mount['read_only'] else 'WRITABLE'}); the job {reader.get('status')} in "
                     f"{reader.get('seconds')} s.")
    if container:
        lines.append(f"Container: mount options {(container.get('mount') or {}).get('options')}; read "
                     f"{probe.get('data_bytes', 0) // 1024**2} MiB at {container.get('read_mib_per_second')} MiB/s; "
                     f"execute: {container.get('exec')}; writes: "
                     + ", ".join(f"{name} {value}" for name, value in sorted((container.get("writes") or {}).items())) + ".")
    if probe["failures"]:
        lines.append("Not proved:")
        lines.extend("  - " + item for item in probe["failures"])
    else:
        lines.append("Proved: a Modal Volume mounts read-only into the job VM and into the job's container, and the job "
                     "cannot write to it.")
    return lines


def run_environment_probe(directory: Path, command, *, detect_public_ipv4=None, remote=owned_modal_remote):
    """Check on a live Modal run that a sealed Lean environment mounts read-only into a job.

    One relay on the chat host starts two small VMs with its own Modal credentials and a temporary Volume that Modal
    deletes afterwards; see reasoning_ledger.server.compute_environment_probe. The relays keep running. The receipt is
    printed and saved in DEPLOY_STATE/compute-environment-probe.
    """
    intro = [
        "One Modal relay on the chat host starts two small VMs on the relays' Modal account (1 CPU and 1 GiB each,",
        "a few minutes in all) and a temporary Modal Volume of 256 MiB that Modal deletes afterwards. No job, topic",
        "or organization data is involved, and the relays keep running. The relays must run this release's relay",
        "code (make compute-relays-update).",
    ]

    def valid(result):
        return isinstance(result.get("probe"), dict) and isinstance(result["probe"].get("failures"), list)

    return _relay_operation(
        directory, command, operation="environment-probe", feature="modal-environment-probe-v1",
        purpose="probe Lean environments", gerund="probing Lean environments", receipt="Lean environment probe",
        intro=intro, question="Run the Lean environment probe?",
        consent="probe environments", value={}, valid=valid, receipts="compute-environment-probe",
        describe=describe_probe, detect_public_ipv4=detect_public_ipv4, remote=remote)


def describe_preparation(receipt):
    """Plain lines for the operator: what was prepared where, or why it was not."""
    preparation = receipt["preparation"]
    vm = preparation.get("vm") or {}
    lines = [f"Relay used: {receipt['relay']}"]
    if preparation.get("attempt"):
        toolchain = preparation["toolchain"]
        lines += [f"Environment {preparation['environment_id']} (key {preparation['key']}), attempt {preparation['attempt']}.",
                  f"Lean {toolchain['version']}: {toolchain['bytes']} bytes, SHA-256 {toolchain['sha256']} from GitHub."]
    else:
        lines.append(f"Environment {preparation['environment_id']}: nothing was started on Modal.")
    for package in vm.get("packages") or []:
        lines.append(f"  {package['name']}: on its {package['on']} ({package['seconds']} s)")
    if vm.get("cache"):
        cache = vm["cache"]
        lines.append(f"mathlib's files: {cache['files']} files, {cache['bytes_in']} bytes, {cache['upstream_errors']} "
                     f"upstream errors; {cache['refused']} requests refused"
                     + (f" ({'; '.join(cache['refused_requests'])})" if cache["refused_requests"] else "") + ".")
    if vm.get("built"):
        lines.append("Built offline: " + ", ".join(vm["built"]) + ".")
    if vm.get("seconds"):
        lines.append("Seconds: " + ", ".join(f"{name} {value}" for name, value in vm["seconds"].items()) + ".")
    if preparation["failures"]:
        lines.append("Not prepared:")
        lines.extend("  - " + item for item in preparation["failures"])
        if preparation.get("removed"):
            lines.append("The attempt's directory was removed from the Volume.")
        elif preparation.get("attempt"):
            lines.append(f"The attempt's directory is kept: Volume {preparation['volume']}, {preparation['sub_path']}.")
    else:
        seal = vm["seal"]
        lines.append(f"Sealed: {seal['files']} files, {seal['bytes']} bytes, seal SHA-256 {seal['sha256']}, read back from "
                     f"the Volume and matched.")
        lines.append(f"Volume {preparation['volume']}, directory {preparation['sub_path']}.")
    if preparation.get("log"):
        lines.append(f"The relay keeps the receipt and the VM's log ({preparation['log']}) in its environment-preparations "
                     "directory.")
    return lines


def run_environment_prepare(directory: Path, command, environment_id: str, *, detect_public_ipv4=None,
                            remote=owned_modal_remote):
    """Prepare one approved Lean environment on the relays' Modal account.

    One relay on the chat host reads the environment from the server, which returns it only if an administrator of the
    organization approved it, then fetches, builds and seals it in one VM on the organization's Modal Volume; see
    reasoning_ledger.server.compute_environment_prepare. The relays keep running. The receipt is printed and saved in
    DEPLOY_STATE/compute-environment-preparations.
    """
    if not isinstance(environment_id, str) or not re.fullmatch(r"[a-f0-9]{32}", environment_id):
        raise InstallError("name the approved environment by its 32-character ID (Administration, Compute, Lean environments)")
    intro = [
        f"One Modal relay on the chat host prepares Lean environment {environment_id}, if an administrator approved it.",
        "It starts one VM on the relays' Modal account with the relay's CPU and memory limits, for up to an hour (about",
        "ten minutes for a mathlib project whose compiled files are in mathlib's cache), and writes about 10 GB to the",
        "organization's Modal Volume, which is created on first use and kept. The VM fetches only the approved sources,",
        "from GitHub and mathlib's cache. The relays keep running and must run this release's relay code",
        "(make compute-relays-update).",
    ]

    def valid(result):
        preparation = result.get("preparation")
        return (isinstance(preparation, dict) and isinstance(preparation.get("failures"), list)
                and preparation.get("environment_id") == environment_id)

    return _relay_operation(
        directory, command, operation="environment-prepare", feature="modal-environment-prepare-v1",
        purpose="prepare Lean environments", gerund="preparing Lean environments", receipt="Lean environment preparation",
        intro=intro, question="Prepare this Lean environment?",
        consent="prepare " + environment_id, value={"environment_id": environment_id}, valid=valid,
        receipts="compute-environment-preparations", describe=describe_preparation,
        detect_public_ipv4=detect_public_ipv4, remote=remote)


def _relay_operation(directory: Path, command, *, operation, feature, purpose, gerund, receipt, intro, question, consent,
                     value, valid, receipts, describe, detect_public_ipv4, remote):
    """Open SSH briefly, run one relay operation of the installed release on the chat host, close SSH, and keep the
    receipt in DEPLOY_STATE/<receipts>."""
    with locked_directory(directory.absolute()) as state:
        current = current_binding(state)
        bundle = archived_release(state, current["release_id"])
        manifest, release, checksum = inspect_bundle(bundle, current["bundle_sha256"])
        if release != current["release_id"] or checksum != current["bundle_sha256"]:
            raise InstallError(f"synchronize the installed release before {gerund}")
        if feature not in manifest.get("installation_features", []):
            raise InstallError(f"upgrade the server to a release that can {purpose} (make upgrade), then retry")
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config, document = read_document(config_path), read_document(plan_path)
        origin = public_origin(operator_origin(config, document, current))
        print(f"Site: {origin}\nRelease: {release}")
        for line in intro:
            print(line)
        approve(question, f"{consent} {origin}")
        plan_id = document["plan_id"] if "plan_id" in document else read_document(state / "setup-completion.json")["plan_id"]
        cidr = temporary_ssh_cidr(detect_public_ipv4)
        try:
            config, opened = _ssh_action(
                command, state, config_path, config, plan_path, plan_id, ownership, ["--open-cidr", cidr, "--wait-seconds", 60]
            )
            if opened.get("phase") != "open":
                raise InstallError("temporary SSH access is unverified")
            result = remote(config, document, ownership, bundle,
                            {"format": 1, "release_id": release, "public_url": origin, **value}, operation)
            if not isinstance(result, dict) or result.get("release_id") != release or not valid(result):
                raise InstallError(f"{receipt} receipt is invalid")
        finally:
            config, closed = close_ssh_with_token_recovery(
                lambda arguments: _invoke(command, arguments), state, config_path, config, plan_path, ownership, plan_id,
                document["plan"],
            )
            if closed.get("phase") != "closed":
                raise InstallError("SSH closure is unverified; run make doctor with the same deployment state")
        work = state / receipts
        with locked_directory(work):
            write_document(work / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}.json", result,
                           new=True)
        for line in describe(result):
            print(line)
        return {**result, "ssh_closed": True}


def relay_limits(configuration, workers):
    """The organization's live Modal relay workers, and every installed image with its maximum resources."""
    try:
        images = {record["id"]: ComputeImage.parse(record) for record in configuration["images"]}
        if not isinstance(workers, list) or any(not isinstance(worker, dict) for worker in workers):
            raise ValueError
        relays = [worker for worker in workers if str(worker.get("name", "")).startswith(RELAY_PREFIX)
                  and worker.get("revoked_at") is None and worker.get("retired_at") is None]
        for worker in relays:
            if (not re.fullmatch(r"[a-f0-9]{32}", str(worker["id"])) or not isinstance(worker["image_ids"], list)
                    or worker["resources"] != resources(worker["resources"])):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise InstallError("invalid compute images, Modal relay identity or limits") from None
    if not relays:
        raise InstallError("no Modal relay workers are enrolled; run make compute-setup first")
    return images, relays


def describe(limits):
    return f"{limits['cpu']} CPU, {limits['memory_bytes'] // 1024**2} MiB memory, {limits['wall_time_seconds']} s wall time"


def choose_limits(current):
    """Only CPU, memory and wall time are asked, as in Modal setup; disk, PIDs and artifact limits are kept."""
    try:
        chosen = {**current,
                  "cpu": int(ask("Maximum logical CPUs per compute job", current["cpu"])),
                  "memory_bytes": int(ask("Maximum MiB memory per compute job", current["memory_bytes"] // 1024**2)) * 1024**2,
                  "wall_time_seconds": int(ask("Maximum seconds per compute job", current["wall_time_seconds"]))}
        return resources(chosen)
    except (TypeError, ValueError):
        raise InstallError("choose whole numbers within the supported bounds: 1 to 64 CPUs, 64 MiB to 256 GiB memory, "
                           "1 to 86400 seconds") from None


def run_relay_limits(directory: Path, command, *, detect_public_ipv4=None, remote=owned_modal_remote, transport=None):
    """Change the per-job limits of every Modal relay: the server's record first, then each relay on the chat host.

    The relays must stay drained throughout, so a relay never claims a job while it and the server disagree. An
    interrupted change is saved in DEPLOY_STATE/compute-limits and resumed by running the same command again.
    """
    with locked_directory(directory.absolute()) as state:
        current = current_binding(state)
        bundle = archived_release(state, current["release_id"])
        manifest, release, checksum = inspect_bundle(bundle, current["bundle_sha256"])
        if release != current["release_id"] or checksum != current["bundle_sha256"]:
            raise InstallError("synchronize the installed release before changing Modal relay limits")
        if "modal-relay-limits-v1" not in manifest.get("installation_features", []):
            raise InstallError("upgrade the server to a release that can change Modal relay limits (make upgrade), then retry")
        config_path, plan_path, ownership = state / "setup.json", state / "plan.json", state / "ownership"
        config, document = read_document(config_path), read_document(plan_path)
        origin = public_origin(operator_origin(config, document, current))
        work = state / "compute-limits"
        with locked_directory(work):
            pending_path = work / "pending.json"
            pending = read_document(pending_path) if pending_path.exists() or pending_path.is_symlink() else None
            if pending is not None:
                organization, email = pending["organization_id"], pending["email"]
                print(f"Resuming the interrupted change to {describe(pending['resources'])}.")
            else:
                completion = state / "setup-completion.json"
                default = read_document(completion)["organization_id"] if completion.exists() else None
                organization = ask("Existing organization ID", default)
                email = ask("Organization administrator email")
                if not re.fullmatch(r"[a-f0-9]{32}", organization):
                    raise InstallError("organization ID must be a 32-character identity")
            secret = getpass.getpass("Organization administrator password (not saved): ")
            endpoint = "/api/organizations/" + organization
            admin = AdminSession(origin, email, secret, transport=transport)
            try:
                images, relays = relay_limits(admin.request("GET", endpoint + "/compute-configuration"),
                                              admin.request("GET", endpoint + "/compute-workers"))
                identities = sorted(worker["id"] for worker in relays)
                undrained = [worker["id"] for worker in relays if not worker.get("draining")]
                if undrained:
                    raise InstallError("drain every Modal relay worker first (Administration → Compute → Drain worker); "
                                       "still taking jobs: " + ", ".join(undrained))
                if pending is not None:
                    if pending["workers"] != identities:
                        raise InstallError("the Modal relay workers changed since the interrupted change; "
                                           f"reconcile them, then remove {pending_path}")
                    chosen = pending["resources"]
                else:
                    if any(worker["resources"] != relays[0]["resources"] for worker in relays):
                        raise InstallError("the Modal relays have different limits, and this command gives them all the same "
                                           "limits; reconcile them first")
                    print(f"Site: {origin}\nOrganization: {organization}")
                    print(f"Current limits of every Modal relay: {describe(relays[0]['resources'])}.")
                    chosen = choose_limits(relays[0]["resources"])
                for worker in relays:
                    for image in worker["image_ids"]:
                        if image not in images or not fits(chosen, images[image].maximum_resources):
                            raise InstallError(f"these limits exceed the maximum resources of image {image}")
                print(f"Site: {origin}")
                for worker in relays:
                    print(f"Relay {worker['id']}: {describe(worker['resources'])} → {describe(chosen)}")
                print("Each job's Modal VM is sized to the resources the job requests, so larger jobs cost more on the "
                      "relays' Modal account.")
                print("Enrollments, Modal credentials, catalogs and relay code are unchanged. The relays stay drained; "
                      "resume claims on each afterwards.")
                approve("Change the Modal relay limits?", "change relay limits " + origin)
                if pending is None:
                    pending = {"organization_id": organization, "email": email, "resources": chosen, "workers": identities}
                    write_document(pending_path, pending, new=True)
                for worker in relays:
                    if worker["resources"] != chosen:
                        changed = admin.request("PUT", endpoint + f"/compute-workers/{worker['id']}/resources", chosen)
                        if not isinstance(changed, dict) or changed.get("id") != worker["id"] or changed.get("resources") != chosen:
                            raise InstallError("the server's relay limits change is unverified; run the same command again")
            finally:
                admin.close()
            plan_id = document["plan_id"] if "plan_id" in document else read_document(state / "setup-completion.json")["plan_id"]
            cidr = temporary_ssh_cidr(detect_public_ipv4)
            try:
                config, opened = _ssh_action(
                    command, state, config_path, config, plan_path, plan_id, ownership, ["--open-cidr", cidr, "--wait-seconds", 60]
                )
                if opened.get("phase") != "open":
                    raise InstallError("temporary SSH access is unverified")
                request = {"format": 1, "release_id": release, "public_url": origin, "resources": chosen, "workers": identities}
                result = remote(config, document, ownership, bundle, request, "limits")
                if (not isinstance(result, dict) or result.get("resources") != chosen or result.get("relays") != identities
                        or result.get("live_compute_verified") is not False):
                    raise InstallError("Modal relay limits receipt is invalid")
            finally:
                config, closed = close_ssh_with_token_recovery(
                    lambda arguments: _invoke(command, arguments), state, config_path, config, plan_path, ownership, plan_id,
                    document["plan"],
                )
                if closed.get("phase") != "closed":
                    raise InstallError("SSH closure is unverified; run make doctor with the same deployment state")
            pending_path.unlink()
        print("Relay limits changed. Resume claims on each relay worker, then run a small job; live execution is not "
              "claimed until one succeeds.")
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
