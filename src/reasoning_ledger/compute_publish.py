"""Guided exact-candidate publication with protected, resumable hosted verification.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path, PurePosixPath

from rich.panel import Panel
from rich.text import Text

from . import image_publication as publication
from .deployment.host import InstallError
from .deployment.private import locked_directory, protected_parent, read_document, read_private, write_document, write_private
from .deployment.setup_ui import console
from .image_candidate import validate_run
from .image_release import PROFILES, SOURCE_PATHS, digest, inspect_image, json_body
from .image_review import confirm
from .server.compute_catalog import canonical, load_catalog

MAX_JSON = 8 * 1024**2
MAX_ARTIFACT = 64 * 1024**2
REVISION = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BILLING_REFUSAL = ("The job was not started because recent account payments have failed or your spending limit needs "
                   "to be increased. Please check the 'Billing & plans' section in your settings")


class Pending(ValueError):
    """Verified completion is not yet available; preserve the original operation."""


def command(arguments, *, cwd=None, maximum=MAX_JSON, timeout=180):
    """Bound both command pipes and elapsed time; never forward credential-bearing stderr."""
    process = subprocess.Popen(arguments, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, umask=0o077)
    result, counts, deadline = bytearray(), {}, time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                selector.register(stream, selectors.EVENT_READ)
                counts[stream] = 0
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("publication command timed out; output suppressed, retain state and resume")
                for key, _ in selector.select(.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    counts[key.fileobj] += len(chunk)
                    if counts[key.fileobj] > (maximum if key.fileobj is process.stdout else 65536):
                        raise ValueError("publication command output exceeded its allowance; output suppressed")
                    if key.fileobj is process.stdout:
                        result.extend(chunk)
        process.wait(timeout=max(1, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("publication command failed; credential-bearing output suppressed, retain state and inspect GitHub")
        return bytes(result)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        process.stdout.close()
        process.stderr.close()


def hint(path, maximum=MAX_JSON):
    """Read discovery hints, never authority or private secrets, from existing candidate storage."""
    path = path.absolute()
    if path.resolve() != path or ".." in path.parts:
        raise ValueError("candidate hints must not traverse symlinks or parent paths")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("candidate hint must be a bounded owned regular file")
        body = stream.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("candidate hint grew beyond its allowance")
    return json_body(body)


def retained_path(directory, name):
    if not isinstance(name, str) or not name or len(name) > 4096:
        raise ValueError("candidate lacks a retained relative file path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError("retained candidate paths must stay inside the selected candidate directory")
    path = directory / relative
    if path.resolve() != path.absolute() or not path.is_file():
        raise ValueError("retained candidate file is missing or traverses a symlink")
    return path


def discover(base):
    if not base.exists():
        return []
    if base.is_symlink() or not base.is_dir():
        raise ValueError("candidate discovery root must be an ordinary directory")
    children = list(base.iterdir())
    if len(children) > 256:
        raise ValueError("too many candidate directories; select an explicit handoff path")
    return sorted(child / "handoff.json" for child in children
                  if not child.is_symlink() and child.is_dir() and (child / "handoff.json").is_file())


def choose(candidates, *, ask=input):
    if len(candidates) == 1:
        return candidates[0]
    for number, path in enumerate(candidates, 1):
        console.print(Text(f"{number}. {path}"))
    value = ask("Candidate number or handoff.json path (blank to pause): ").strip()
    if not value:
        raise Pending("publication paused before selecting a candidate")
    if value.isdecimal() and 1 <= int(value) <= len(candidates):
        return candidates[int(value) - 1]
    return Path(value).expanduser().absolute()


def private_parents(base, relative):
    base.mkdir(mode=0o700, exist_ok=True)
    protected_parent(base / ".check")
    parent = base
    for part in relative.parts:
        parent = parent / part
        parent.mkdir(mode=0o700, exist_ok=True)
        protected_parent(parent / ".check")


def unpack(body, directory, allowed):
    """Extract only bounded allowlisted public evidence, never arbitrary artifact paths."""
    if len(body) > MAX_ARTIFACT:
        raise ValueError("publication artifact exceeds its allowance")
    seen, total = set(), 0
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if len(archive.infolist()) > 64:
            raise ValueError("publication artifact contains too many entries")
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                    or str(path) != entry.filename.rstrip("/") or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError("publication artifact has an unsafe path or link")
            if entry.is_dir():
                continue
            total += entry.file_size
            if entry.filename not in allowed or entry.filename in seen or entry.file_size > MAX_JSON or total > MAX_ARTIFACT:
                raise ValueError("publication artifact contains unexpected, duplicate or oversized evidence")
            seen.add(entry.filename)
            target = directory / path
            private_parents(directory, path.parent)
            with archive.open(entry) as stream:
                value = stream.read(MAX_JSON + 1)
                if len(value) != entry.file_size:
                    raise ValueError("publication artifact member was truncated or exceeds its allowance")
            if target.exists() or target.is_symlink():
                if read_private(target, MAX_JSON) != value:
                    raise ValueError("downloaded evidence differs from the retained artifact; original state preserved")
            else:
                write_private(target, value, maximum=MAX_JSON)
    return seen


def evidence_path(directory, first, alternate=None):
    paths = [directory / name for name in (first, alternate) if name is not None and (directory / name).is_file()]
    if len(paths) != 1:
        raise ValueError("candidate artifact lacks unique exact evidence")
    return paths[0]


def normalized_image(image):
    return {**image, "reference": image["reference"].split("@", 1)[1]}


class Services:
    def recipe(self, inputs, path):
        from .image_provenance import extract_candidate_recipe

        if not path.exists() and not path.is_symlink():
            temporary = path.with_name(path.name + ".preparing-" + uuid.uuid4().hex)
            extract_candidate_recipe(inputs["bundle"], inputs["draft"], temporary)
            temporary.rename(path)
        if path.is_symlink() or not path.is_dir():
            raise ValueError("retained recipe must be an ordinary private directory")
        # CI matching and observe() revalidate the exact extracted bytes before
        # any upload/signature. An interrupted temporary extraction is not used.
        return path

    def recipe_ci(self, repository, revision, profile, recipe):
        """Bind unsigned archive recipe bytes to the authenticated candidate Git commit."""
        commit = self.gh(["api", f"repos/{repository}/git/commits/{revision}"])
        tree_sha = commit.get("tree", {}).get("sha")
        if commit.get("sha") != revision or not isinstance(tree_sha, str) or not REVISION.fullmatch(tree_sha):
            raise ValueError("candidate source commit lacks its exact authenticated Git tree")
        tree = self.gh(["api", f"repos/{repository}/git/trees/{tree_sha}?recursive=1"])
        if tree.get("sha") != tree_sha or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise ValueError("candidate Git tree is incomplete; exact recipe proof was not accepted")
        prefixes = (*SOURCE_PATHS, "images/lean-mathlib") if profile == "lean-mathlib" else SOURCE_PATHS
        expected = {}
        for item in tree["tree"]:
            name = item.get("path", "")
            if any(name == prefix or name.startswith(prefix + "/") for prefix in prefixes) and item.get("type") != "tree":
                if (item.get("type") != "blob" or item.get("mode") not in ("100644", "100755")
                        or name in expected or len(expected) >= 512 or not isinstance(item.get("sha"), str)
                        or not REVISION.fullmatch(item["sha"])):
                    raise ValueError("candidate recipe tree has linked, duplicate or unbounded source entries")
                expected[name] = item["sha"]
        actual = {path.relative_to(recipe).as_posix(): path for path in recipe.rglob("*") if path.is_file()}
        for marker in (".reasoning-recipe.tar", ".reasoning-recipe.json"):
            actual.pop(marker, None)
        if not expected or set(actual) != set(expected):
            raise ValueError("retained recipe file set differs from the authenticated candidate source tree")
        for name, sha in expected.items():
            body = read_private(actual[name], 16 * 1024**2)
            blob = b"blob " + str(len(body)).encode() + b"\0" + body
            actual_sha = (hashlib.sha1(blob, usedforsecurity=False).hexdigest() if len(sha) == 40
                          else hashlib.sha256(blob).hexdigest())
            if actual_sha != sha:
                raise ValueError("retained recipe bytes differ from the authenticated candidate Git commit")

    def gh(self, arguments):
        return json_body(command(["gh", *arguments]))

    def git(self, root, arguments):
        return command(["git", *arguments], cwd=root).decode().strip()

    def repository(self, root):
        return json_body(command(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=root))["nameWithOwner"]

    def artifact(self, repository, run, name, directory, allowed):
        document = self.gh(["api", f"repos/{repository}/actions/runs/{run}/artifacts?per_page=100"])
        if document.get("total_count", 0) > 100:
            raise ValueError("artifact listing exceeds its bounded page")
        matches = [item for item in document.get("artifacts", []) if item.get("name") == name and item.get("expired") is False]
        if (len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0
                or type(matches[0].get("size_in_bytes")) is not int or not 0 < matches[0]["size_in_bytes"] <= MAX_ARTIFACT):
            raise ValueError("successful workflow lacks one retained bounded matching evidence artifact")
        directory.mkdir(mode=0o700, exist_ok=True)
        body = command(["gh", "api", f"repos/{repository}/actions/artifacts/{matches[0]['id']}/zip"], maximum=MAX_ARTIFACT)
        return unpack(body, directory, allowed)

    def checkout(self, root, revision, path):
        if not REVISION.fullmatch(revision):
            raise ValueError("recipe/tooling checkout requires an exact full Git revision")
        if not path.exists():
            self.git(root, ["worktree", "add", "--detach", str(path), revision])
        if self.git(path, ["rev-parse", "HEAD"]) != revision or self.git(path, ["status", "--porcelain", "--untracked-files=all"]):
            raise ValueError("retained checkout differs from the clean exact selected revision")
        return path

    def candidate(self, root, recipe, repository, handoff, profile, inputs, state):
        run = self.gh(["api", f"repos/{repository}/actions/runs/{handoff['candidate_workflow_run']}"])
        validate_run(run, handoff["source_revision"])
        validate_repository(run, repository)
        report, blobs = inspect_image(inputs["archive"], json_body(
            (recipe / "contracts/executor/v1alpha1/image-capabilities.schema.json").read_bytes()), inputs["repository"])
        publication.require_public_noether(json_body(blobs["usr/share/reasoning/sbom.spdx.json"]))
        if report["image"]["id"] != profile or report["image"]["source_revision"] != handoff["source_revision"]:
            raise ValueError("retained OCI profile/recipe revision differs from the successful candidate workflow")
        allowed = {"candidate/image-evidence.json", "candidate/capabilities.json", "candidate/sbom.spdx.json",
                   "image-evidence.json", "capabilities.json", "sbom.spdx.json", "acceptance/acceptance.json", "acceptance.json"}
        folder = state / ("candidate-ci-" + profile)
        self.artifact(repository, run["id"], f"{profile}-acceptance-{handoff['source_revision']}", folder, allowed)
        observed = hint(evidence_path(folder, "candidate/image-evidence.json", "image-evidence.json"))
        accepted = hint(evidence_path(folder, "acceptance/acceptance.json", "acceptance.json"))
        if (normalized_image(report["image"]) != normalized_image(observed["image"])
                or any(report[key] != observed[key] for key in ("config_digest", "platform_manifest_digest", "layer_digests"))
                or normalized_image(accepted["image"]) != normalized_image(report["image"])
                or any(read_private(evidence_path(folder, "candidate/" + name, name), MAX_JSON) != blobs[target]
                       for name, target in (("capabilities.json", "etc/reasoning/capabilities.json"),
                                            ("sbom.spdx.json", "usr/share/reasoning/sbom.spdx.json")))):
            raise ValueError("retained OCI contents/recipe/SBOM differ from authenticated successful candidate CI evidence")
        publication.public_acceptance(accepted, accepted["image"])
        return report["image"]

    def observe(self, recipe, profile, inputs):
        from .image_provenance import observe

        return observe(recipe, inputs["archive"], profile, inputs["repository"], inputs["bundle"], inputs["draft"])

    def source(self, bundle, source_state, expected, *, ask, password):
        sha, size = publication.hash_file(bundle)
        if (sha, size) != (expected["source_bundle_sha256"], expected["source_bundle_bytes"]):
            raise ValueError("source bundle changed after provenance preflight; nothing was uploaded")
        receipt = source_state / sha / "source-upload-complete.json"
        if receipt.exists() or receipt.is_symlink():
            value = json_body(read_private(receipt))
        else:
            from .image_spaces import publish_source

            value = publish_source(bundle=bundle, state=source_state, ask=ask, password=password)
        if value.get("source_bundle_sha256") != sha or value.get("source_bundle_bytes") != size:
            raise ValueError("source upload receipt differs from the selected source bytes")
        publication.public_url(value["source_bundle_url"])
        return value

    def prepare(self, recipe, profile, inputs, source, state):
        from .image_provenance import prepare

        return prepare(recipe, inputs["archive"], profile, inputs["repository"], inputs["bundle"],
                       source["source_bundle_url"], inputs["draft"], state)

    def sign(self, state, key_path, trusted_key, *, ask, password):
        from .image_provenance import sign

        return sign(state, key_path, trusted_key, ask=ask, password=password)

    def stage(self, root, tooling_revision, profiles, trust, state):
        checkout = state / "tooling"
        expected = state / "branch-inputs.json"
        public = {"images/publication/review-key.pem": read_private(trust, 16384)}
        for profile, audit in profiles.items():
            for name in ("source-review.json", "source-review.ed25519"):
                public[f"images/publication/{profile}/{audit['source_revision']}/{name}"] = read_private(state / profile / name, MAX_JSON)
        binding = {"tooling_revision": tooling_revision, "files": {name: digest(body) for name, body in public.items()}}
        if expected.exists():
            if read_document(expected) != binding:
                raise ValueError("public attestation branch inputs differ from the retained exact signatures/key")
        else:
            write_document(expected, binding, new=True)
        if not checkout.exists():
            self.checkout(root, tooling_revision, checkout)
        head = self.git(checkout, ["rev-parse", "HEAD"])
        changed = self.git(checkout, ["diff", "--name-only", tooling_revision]).splitlines()
        if set(changed) - set(public) or head != tooling_revision and self.git(checkout, ["rev-parse", "HEAD^"]) != tooling_revision:
            raise ValueError("retained public tooling contains changes beyond its exact attestation files")
        for name, body in public.items():
            path = checkout / name
            private_parents(checkout, Path(name).parent)
            if path.exists() or path.is_symlink():
                if path.is_symlink() or path.read_bytes() != body:
                    raise ValueError("tooling already contains different public attestation/key bytes")
            else:
                write_private(path, body, maximum=MAX_JSON)
        self.git(checkout, ["add", "--", *sorted(public)])
        if self.git(checkout, ["diff", "--cached", "--name-only"]):
            if head != tooling_revision:
                raise ValueError("retained public commit differs from newly staged evidence")
            self.git(checkout, ["-c", "user.name=Reasoning publication", "-c", "user.email=publication@users.noreply.github.com",
                                "commit", "-m", "Publish exact observed-source provenance and enrolled verification key"])
        if self.git(checkout, ["status", "--porcelain", "--untracked-files=all"]):
            raise ValueError("public tooling must contain only committed exact attestation bytes")
        return self.git(checkout, ["rev-parse", "HEAD"])

    def push(self, root, repository, revision, branch):
        url = f"https://github.com/{repository}.git"
        self.git(root, ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                        "push", url, f"{revision}:refs/heads/{branch}"])

    def runs(self, repository, workflow, branch):
        path = f"repos/{repository}/actions/workflows/{workflow}/runs?event=workflow_dispatch&branch={branch}&per_page=100"
        return self.gh(["api", path])

    def dispatch(self, repository, workflow, branch, inputs):
        arguments = ["gh", "workflow", "run", workflow, "--repo", repository, "--ref", branch]
        for name, value in sorted(inputs.items()):
            arguments += ["--field", f"{name}={value}"]
        command(arguments)

    def run(self, repository, identity):
        return self.gh(["api", f"repos/{repository}/actions/runs/{identity}"])

    def billing_blocked(self, repository, identity):
        jobs = self.gh(["api", f"repos/{repository}/actions/runs/{identity}/jobs?per_page=100"])
        if jobs.get("total_count", 0) > 100:
            return False
        for job in jobs.get("jobs", []):
            if job.get("steps") != [] or job.get("conclusion") != "failure":
                continue
            match = re.fullmatch(r"https://api.github.com/repos/" + re.escape(repository) + r"/check-runs/([1-9][0-9]*)",
                                 str(job.get("check_run_url", "")))
            if not match:
                continue
            annotations = self.gh(["api", f"repos/{repository}/check-runs/{match[1]}/annotations?per_page=100"])
            if isinstance(annotations, list) and any(item.get("message") == BILLING_REFUSAL for item in annotations):
                return True
        return False

    def catalog(self, repository, run_id, revision, runs, audits, trust, state):
        names = {"compute-images.json", "combine.json", "review-key.pem"}
        publication_names = {"publication.json", "compute-images.json", "source-review.json", "source-review.ed25519",
                             "public-acceptance.json", "workflow-request.json"}
        names.update(f"published/{profile}/{name}" for profile in audits for name in publication_names)
        folder = state / "catalog-evidence"
        seen = self.artifact(repository, run_id, f"approved-compute-catalog-{revision}", folder, names)
        if seen != names or read_private(folder / "review-key.pem", 16384) != read_private(trust, 16384):
            raise ValueError("combined artifact lacks complete evidence or uses a different enrolled verification key")
        receipt = hint(folder / "combine.json")
        operation = read_document(state / "operation.json")
        catalog = folder / "compute-images.json"
        entries = load_catalog(catalog)
        if (receipt.get("format") != 1 or receipt.get("source_revision") != revision
                or receipt.get("publication_runs") != runs or receipt.get("anonymous_reverification") is not True
                or receipt.get("catalog_sha256") != digest(read_private(catalog, 65536))[7:] or set(entries) != set(audits)
                or receipt.get("workflow_run_id") != run_id or receipt.get("tooling_revision") != operation["public_head"]
                or receipt.get("repository") != repository or receipt.get("human_review_verified") is not False
                or receipt.get("independent_license_audit_verified") is not False):
            raise ValueError("hosted combination does not bind the exact selected publications and approved catalog bytes")
        for profile, audit in audits.items():
            directory = folder / "published" / profile
            signed, fingerprint = publication.load_audit(directory / "source-review.json", directory / "source-review.ed25519", trust)
            image, published = entries[profile].record(), hint(directory / "publication.json")
            workflow = hint(directory / "workflow-request.json")
            if (signed != audit or workflow.get("profile") != profile or workflow.get("source_revision") != revision
                    or str(workflow.get("workflow_run_id")) != str(runs[profile])
                    or workflow.get("candidate_run_id") != operation["candidate_run_id"]
                    or workflow.get("format") != 1 or workflow.get("repository") != repository
                    or workflow.get("tooling_revision") != operation["public_head"]
                    or workflow.get("ref") != "refs/heads/" + operation["runs"][profile]["branch"]
                    or published.get("image") != image or published.get("maintainer_key_sha256") != fingerprint
                    or published.get("audit_sha256") != digest(canonical(audit).encode())
                    or published.get("catalog_sha256") != digest(read_private(directory / "compute-images.json", 65536))
                    or any(published.get(key) is not True for key in ("audit_signature_verified", "anonymous_source_delivery_verified",
                                                                   "anonymous_registry_digest_verified", "execution_verified"))
                    or image["source_revision"] != revision or image["reference"].split("@")[1] != audit["image_digest"]
                    or published.get("acceptance_receipt") != "public-acceptance.json"
                    or published.get("acceptance_sha256") != digest(read_private(directory / "public-acceptance.json", MAX_JSON))):
                raise ValueError("catalog publication evidence does not match the signed exact image/source or hosted dispatch")
            accepted = hint(directory / "public-acceptance.json")
            if publication.public_acceptance(accepted, image) != accepted:
                raise ValueError("catalog lacks complete matching anonymous real-worker acceptance evidence")
        return catalog


def validate_repository(run, repository):
    owned, source = run.get("repository", {}), run.get("head_repository", {})
    if (not isinstance(owned, dict) or not isinstance(source, dict)
            or type(owned.get("id")) is not int or owned["id"] <= 0 or source.get("id") != owned["id"]
            or str(owned.get("full_name", "")).lower() != repository.lower()):
        raise ValueError("hosted verification must belong to the selected repository, not a fork")


def validate_hosted(run, workflow, branch, head, repository=None):
    if (not isinstance(run, dict) or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != branch or run.get("head_sha") != head
            or run.get("path", "").split("@", 1)[0] != ".github/workflows/" + workflow
            or type(run.get("id")) is not int or run["id"] <= 0):
        raise ValueError("hosted verification run does not match this isolated exact tooling branch and workflow")
    if repository is not None:
        validate_repository(run, repository)


def hosted(services, repository, workflow, branch, head, inputs, state, operation, label, *, wait_seconds, ask=input):
    journal = state / "operation.json"
    record = operation["runs"].setdefault(label, {"workflow": workflow, "branch": branch, "head": head, "inputs": inputs})
    if any(record[key] != value for key, value in {"workflow": workflow, "branch": branch, "head": head, "inputs": inputs}.items()):
        raise ValueError("hosted verification inputs differ from the retained operation")
    write_document(journal, operation)
    deadline = time.monotonic() + wait_seconds
    while True:
        if "run_id" not in record:
            listing = services.runs(repository, workflow, branch)
            if listing.get("total_count", 0) > 100:
                raise ValueError("too many runs on the isolated publication branch")
            matches = [run for run in listing.get("workflow_runs", []) if run.get("head_sha") == head]
            if len(matches) > 1:
                raise ValueError("multiple dispatches match the isolated branch; select/reconcile the original run outside this wizard")
            if matches:
                validate_hosted(matches[0], workflow, branch, head, repository)
                record["run_id"] = matches[0]["id"]
                write_document(journal, operation)
            elif not record.get("dispatch_requested"):
                # This durable intent precedes the outbound effect. If its receipt
                # is lost, discovery/polling continues without blindly redispatching.
                record["dispatch_requested"] = True
                write_document(journal, operation)
                services.dispatch(repository, workflow, branch, inputs)
        if "run_id" in record:
            run = services.run(repository, record["run_id"])
            validate_hosted(run, workflow, branch, head, repository)
            if run.get("status") == "completed":
                if run.get("conclusion") == "success":
                    record["completed"] = True
                    write_document(journal, operation)
                    return run["id"]
                if services.billing_blocked(repository, run["id"]):
                    record["billing_blocked"] = True
                    write_document(journal, operation)
                    raise Pending(f"GitHub Actions refused to start because account payments failed or the spending limit "
                                  f"was reached. Review Billing & plans with the account owner, then rerun the retained exact "
                                  f"run https://github.com/{repository}/actions/runs/{run['id']} and resume. "
                                  "No automatic retry or spending-limit change was attempted")
                console.print(Text(f"Verification failed: https://github.com/{repository}/actions/runs/{run['id']}\n"
                                   "No approved catalog was accepted. Inspect the failure; GHCR packages may need Public visibility.\n"
                                   "Open package settings in GitHub, enable Public for the exact image if appropriate, "
                                   "then retry this run.",
                                   style="yellow"))
                value = ask("Open GitHub packages in your browser? [y/N]: ").strip().lower()
                if value == "y":
                    webbrowser.open(f"https://github.com/{repository.split('/')[0]}?tab=packages")
                raise Pending("original hosted run failed; preserve state, inspect it and rerun that exact Actions run before resuming")
        if time.monotonic() >= deadline:
            raise Pending("hosted verification is pending or its dispatch is unverified; resume the same wizard/state, do not redispatch")
        console.print(Text(f"Waiting for {label} verification…", style="dim"))
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def run(root, state, *, handoff_path=None, profiles=None, source_state=None, repository=None, wait_seconds=600,
        ask=input, password=getpass.getpass, services=None):
    services = services or Services()
    root = root.absolute()
    requested_source_state = source_state
    source_state = (source_state or Path.home() / ".reasoning-compute-publication").absolute()
    with locked_directory(state) as directory:
        journal = directory / "operation.json"
        operation = read_document(journal) if journal.exists() or journal.is_symlink() else None
        if operation:
            if requested_source_state is None:
                source_state = Path(operation["source_state"])
            if handoff_path and str(handoff_path.absolute()) != operation["handoff"]:
                raise ValueError("resume requires the original handoff; select another private state for another candidate")
            handoff_path = Path(operation["handoff"])
            if profiles and sorted(profiles) != operation["profiles"]:
                raise ValueError("resume requires the original selected profiles")
            profiles = operation["profiles"]
            if repository and repository != operation["repository"]:
                raise ValueError("resume requires the original repository")
            repository = operation["repository"]
        else:
            handoff_path = handoff_path or choose(discover(Path.home() / ".local/share/reasoning/compute-publication"), ask=ask)
        handoff_path = handoff_path.expanduser().absolute()
        handoff = hint(handoff_path)
        revision, candidate_run = handoff.get("source_revision"), handoff.get("candidate_workflow_run")
        if (not isinstance(revision, str) or not REVISION.fullmatch(revision)
                or type(candidate_run) is not int or candidate_run <= 0 or not isinstance(handoff.get("profiles"), dict)
                or not handoff["profiles"] or set(handoff["profiles"]) - set(PROFILES)):
            raise ValueError("candidate hint lacks an exact recipe revision, retained Actions run and supported profiles")
        if not profiles:
            available = sorted(handoff["profiles"])
            value = ask(f"Profiles [{','.join(available)}]: ").strip()
            profiles = value.split(",") if value else available
        profiles = sorted(set(profile.strip() for profile in profiles))
        if not profiles or set(profiles) - set(handoff["profiles"]):
            raise ValueError("select retained supported profiles from this candidate")
        repository = repository or services.repository(root)
        if not REPOSITORY.fullmatch(repository):
            raise ValueError("select an exact GitHub owner/repository")
        if operation is None:
            if services.git(root, ["status", "--porcelain", "--untracked-files=all"]):
                raise ValueError("start publication from a clean committed tooling checkout")
            tooling = services.git(root, ["rev-parse", "HEAD"])
            if not REVISION.fullmatch(tooling):
                raise ValueError("publication tooling must have a complete immutable Git revision")
            operation = {"format": 1, "operation_id": uuid.uuid4().hex, "handoff": str(handoff_path), "source_revision": revision,
                         "candidate_run_id": candidate_run, "repository": repository, "profiles": profiles,
                         "tooling_revision": tooling, "runs": {}, "signed": [], "pushed": [],
                         "source_state": str(source_state)}
            write_document(journal, operation, new=True)
        if (operation["source_revision"] != revision or operation["candidate_run_id"] != candidate_run
                or operation["source_state"] != str(source_state)):
            raise ValueError("retained publication identity differs; resume with original candidate/source state")
        if "key_path" not in operation:
            key = ask(f"Encrypted signing key path [{directory / 'signing-key.pem'}]: ").strip()
            trusted = ask(f"Enrolled public key path [{directory / 'review-key.pem'}]: ").strip()
            operation["key_path"] = str(Path(key).expanduser().absolute() if key else directory / "signing-key.pem")
            operation["trusted_key"] = str(Path(trusted).expanduser().absolute() if trusted else directory / "review-key.pem")
            protected_parent(Path(operation["key_path"]))
            protected_parent(Path(operation["trusted_key"]))
            write_document(journal, operation)
        audits = {}
        for profile in profiles:
            values = handoff["profiles"][profile]
            inputs = {"archive": retained_path(handoff_path.parent, values.get("archive")),
                      "bundle": retained_path(handoff_path.parent, values.get("source_bundle")),
                      "draft": retained_path(handoff_path.parent, values.get("source_draft")),
                      "repository": f"ghcr.io/{repository.lower()}/{profile}"}
            with console.status(f"Checking {profile} exact archive-backed build recipe…"):
                recipe = services.recipe(inputs, directory / ("recipe-" + profile))
            console.print(Text(f"Checking {profile}: exact retained image and authenticated CI evidence…"))
            with console.status(f"Checking {profile} immutable OCI contents against successful hosted acceptance…"):
                services.candidate(root, recipe, repository, handoff, profile, inputs, directory)
                services.recipe_ci(repository, revision, profile, recipe)
            with console.status(f"Preflighting {profile} corresponding-source candidates before any public upload…"):
                observed = services.observe(recipe, profile, inputs)
            source = services.source(inputs["bundle"], source_state, observed, ask=ask, password=password)
            with console.status(f"Checking {profile} source provenance; large archives can take several minutes…"):
                prepared = services.prepare(recipe, profile, inputs, source, directory / profile)
            if profile not in operation["signed"]:
                console.print(Text(f"Rechecking exact {profile} image/source bytes before the protected signing prompts…", style="dim"))
                audit = services.sign(directory / profile, Path(operation["key_path"]), Path(operation["trusted_key"]),
                                      ask=ask, password=password)
                if audit != prepared:
                    raise ValueError("signed provenance differs from the exact prepared candidate")
                operation["signed"].append(profile)
                write_document(journal, operation)
            else:
                audit, _ = publication.load_audit(directory / profile / "source-review.json",
                    directory / profile / "source-review.ed25519", Path(operation["trusted_key"]))
                if audit != prepared:
                    raise ValueError("retained signed provenance changed; no new hosted request was made")
            audits[profile] = audit
        head = services.stage(root, operation["tooling_revision"], audits, Path(operation["trusted_key"]), directory)
        if operation.get("public_head") and operation["public_head"] != head:
            raise ValueError("public attestation commit differs from this retained operation")
        operation["public_head"] = head
        if not operation.get("hosted_consent"):
            confirm("Publish these exact signed provenance/key files on isolated GitHub branches and run hosted image distribution/"
                    "anonymous execution verification? Actions charges may apply. No image rebuild or Modal activation is performed.",
                    "publish images " + revision[:12], ask=ask)
            operation["hosted_consent"] = True
            write_document(journal, operation)
        publications = {}
        for label in [*profiles, "catalog"]:
            branch = f"reasoning-publication/{operation['operation_id']}/{label}"
            if label not in operation["pushed"]:
                services.push(root, repository, head, branch)
                operation["pushed"].append(label)
                write_document(journal, operation)
            workflow = "compute-combine.yml" if label == "catalog" else "compute-publish.yml"
            inputs = {"source_revision": revision, "publications": canonical(publications)} if label == "catalog" else {
                "profile": label, "source_revision": revision, "candidate_run_id": str(candidate_run)}
            identity = hosted(services, repository, workflow, branch, head, inputs, directory, operation, label,
                              wait_seconds=wait_seconds, ask=ask)
            if label != "catalog":
                publications[label] = identity
        catalog = services.catalog(repository, identity, revision, publications, audits, Path(operation["trusted_key"]), directory)
        operation["catalog"] = str(catalog)
        operation["catalog_sha256"] = digest(read_private(catalog, 65536))
        operation["phase"] = "catalog_verified"
        write_document(journal, operation)
        return {"phase": "catalog_verified", "catalog": str(catalog), "state": str(directory), "cloud_execution_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--state", type=Path, default=Path.home() / ".reasoning-compute-publish")
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--profile", action="append", choices=tuple(PROFILES))
    parser.add_argument("--source-state", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if not 0 <= args.wait_seconds <= 3600:
        parser.error("--wait-seconds must be between 0 and 3600")
    console.print(Panel.fit(Text("COMPUTE PUBLICATION\nExact retained images, observed source provenance, "
                                "and hosted anonymous verification."),
                            title=" REASONING ", border_style="bright_cyan", padding=(1, 3)))
    try:
        result = run(args.source_root, args.state, handoff_path=args.handoff, profiles=args.profile,
                     source_state=args.source_state, repository=args.repository, wait_seconds=args.wait_seconds)
        console.print(Text(f"Approved catalog: {result['catalog']}\nNext: make compute-setup and select this catalog. "
                           "Modal execution is not yet tested.",
                           style="green"))
    except Pending as error:
        console.print(Text(f"{error}\nPrivate state: {args.state}\n"
                           f"Resume: make compute-publish COMPUTE_PUBLICATION_STATE={args.state}", style="yellow"))
        return 0
    except (EOFError, KeyboardInterrupt):
        console.print(Text(f"Publication paused. Preserve private state at {args.state}; "
                           "resume the original operation. No catalog completion is implied.", style="yellow"))
        return 130
    except (OSError, ValueError, KeyError, TypeError, InstallError, zipfile.BadZipFile) as error:
        parser.exit(2, f"reasoning-compute-publish: {error}\nRetain the original publication state; no verified catalog was accepted.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
