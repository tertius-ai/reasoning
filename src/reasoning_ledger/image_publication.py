"""Explicit maintainer publication of reviewed immutable images; never infer a source audit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .image_acceptance import verify as verify_execution
from .image_release import PROFILES, SOURCE_PATHS, BoundedTarInfo, command, digest, inspect_image, json_body, member_name, write_new
from .server.compute_catalog import canonical
from .server.compute_docker import RuntimeFault

DOMAIN = b"reasoning-compute-source-review-v1\n"
MAX_BUNDLE = 32 * 1024**3
HEX = re.compile(r"[a-f0-9]{64}")
AUDIT_FIELDS = {"format", "reviewer", "reviewed_at", "profile", "source_revision", "recipe_sha256", "image_digest",
                "source_bundle_sha256", "source_bundle_bytes", "source_bundle_url", "files", "coverage",
                "dependency_closure_reviewed", "source_delivery_approved", "public_binary_distribution_approved"}


def hash_file(path, maximum=MAX_BUNDLE):
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_size > maximum:
        raise ValueError("publication input must be a bounded regular file, not a link")
    hasher, count = hashlib.sha256(), 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("publication input changed or is not a regular file")
        while chunk := stream.read(1024**2):
            count += len(chunk)
            if count > maximum:
                raise ValueError("publication file grew beyond its allowance")
            hasher.update(chunk)
    if count != info.st_size:
        raise ValueError("publication input changed during verification")
    return hasher.hexdigest(), info.st_size


def public_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.hostname.lower() in {"localhost", "localhost.localdomain"} or parsed.port not in (None, 443)):
        raise ValueError("source delivery requires a direct public HTTPS URL without credentials or expiring query tokens")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("source delivery cannot use a private or reserved IP")
    return url


def load_audit(path, signature, trusted_key):
    hash_file(path, 8 * 1024**2)
    hash_file(signature, 64)
    hash_file(trusted_key, 16384)
    if signature.stat().st_size != 64:
        raise ValueError("source review or signature/key exceeds its allowance")
    audit = json_body(path.read_bytes())
    machine = audit.get("format") == 2
    if machine:
        from .image_provenance import DOMAIN as machine_domain
        from .image_provenance import validate

        validate(audit)
    elif (set(audit) != AUDIT_FIELDS or type(audit["format"]) is not int or audit["format"] != 1
            or audit["profile"] not in PROFILES or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", audit["source_revision"])
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", audit["image_digest"])
            or any(not isinstance(audit[key], str) or not HEX.fullmatch(audit[key])
                   for key in ("recipe_sha256", "source_bundle_sha256"))
            or type(audit["source_bundle_bytes"]) is not int or not 1 <= audit["source_bundle_bytes"] <= MAX_BUNDLE
            or not isinstance(audit["reviewer"], str) or not 1 <= len(audit["reviewer"].strip()) <= 200
            or not isinstance(audit["reviewed_at"], str) or not audit["reviewed_at"].endswith("Z")
            or any(audit[key] is not True for key in (
                "dependency_closure_reviewed", "source_delivery_approved", "public_binary_distribution_approved"))):
        raise ValueError("an explicit complete maintainer source/notice/distribution review is required")
    if not machine:
        datetime.fromisoformat(audit["reviewed_at"].replace("Z", "+00:00"))
    public_url(audit["source_bundle_url"])
    key = serialization.load_pem_public_key(trusted_key.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("select a trusted Ed25519 maintainer public key independently of the audit")
    try:
        key.verify(signature.read_bytes(), (machine_domain if machine else DOMAIN) + canonical(audit).encode())
    except InvalidSignature:
        raise ValueError("source review signature does not match the explicitly trusted maintainer key") from None
    fingerprint = hashlib.sha256(key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).hexdigest()
    return audit, fingerprint


def recipe_identity(root, profile):
    if (root / ".reasoning-recipe.tar").exists() or (root / ".reasoning-recipe.tar").is_symlink():
        from .image_provenance import recipe_identity as retained_identity

        return retained_identity(root, profile)
    if command(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise ValueError("source review requires a clean committed recipe checkout")
    revision = command(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    paths = (*SOURCE_PATHS, "images/lean-mathlib") if profile == "lean-mathlib" else SOURCE_PATHS
    return revision, hashlib.sha256(command(["git", "archive", revision, *paths], cwd=root)).hexdigest()


def require_public_noether(sbom):
    """Publication accepts the approved public Noether source revision only."""
    from .image_context import NOETHER_PUBLIC_COMMIT

    sources = [package.get("versionInfo") for package in sbom.get("packages", []) if package.get("name") == "noether-source"]
    if sources != [NOETHER_PUBLIC_COMMIT]:
        raise ValueError("compute publication requires the approved public Noether release; rebuild the image and source bundle")


def verify_source_bundle(path, audit, sbom):
    require_public_noether(sbom)
    if hash_file(path) != (audit["source_bundle_sha256"], audit["source_bundle_bytes"]):
        raise ValueError("source bundle differs from the signed review")
    files = audit["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 65536:
        raise ValueError("signed source review requires an exact bounded file inventory")
    expected = {}
    for item in files:
        if (not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"} or not isinstance(item["path"], str)
                or member_name(item["path"]) != item["path"] or item["path"] in expected
                or not isinstance(item["sha256"], str) or not HEX.fullmatch(item["sha256"])
                or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_BUNDLE):
            raise ValueError("source review file identity is invalid or duplicated")
        expected[item["path"]] = (item["sha256"], item["bytes"])
    actual, total = {}, 0
    with tarfile.open(path, mode="r|*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            archive.members.clear()
            name = member_name(entry.name)
            if entry.isdir():
                if entry.size:
                    raise ValueError("source directory cannot carry unreviewed payload")
                continue
            if (not entry.isreg() or entry.sparse is not None or any(key.startswith("GNU.sparse") for key in entry.pax_headers)
                    or name in actual or name not in expected or entry.size != expected[name][1]):
                raise ValueError("source bundle contains unreviewed, linked, duplicate or mismatched content")
            total += entry.size
            if total > 64 * 1024**3:
                raise ValueError("uncompressed corresponding sources exceed their allowance")
            hasher = hashlib.sha256()
            with archive.extractfile(entry) as stream:
                while chunk := stream.read(1024**2):
                    hasher.update(chunk)
            actual[name] = (hasher.hexdigest(), entry.size)
    if actual != expected:
        raise ValueError("source bundle is missing or differs from reviewed files")
    if audit.get("format") == 2:
        from .image_provenance import verify

        verify(path, audit, sbom)
        return
    coverage, covered = audit["coverage"], set()
    if not isinstance(coverage, list) or not 1 <= len(coverage) <= 10000:
        raise ValueError("maintainer review must cover every observed component and the complete dependency closure")
    for item in coverage:
        if (not isinstance(item, dict) or set(item) != {"id", "disposition", "sources", "notices", "reason"}
                or not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 200 or item["id"] in covered
                or item["disposition"] not in {"source-included", "source-not-required"}
                or not isinstance(item["reason"], str) or not 12 <= len(item["reason"].strip()) <= 4000):
            raise ValueError("component source/notice review is missing or duplicated")
        for field in ("sources", "notices"):
            if (not isinstance(item[field], list) or len(item[field]) > 1000
                    or any(not isinstance(name, str) or name not in expected for name in item[field])):
                raise ValueError("component review references missing source or notice files")
        if (not item["notices"] or any(expected[name][1] == 0 for name in item["notices"])
                or item["disposition"] == "source-included" and (not item["sources"] or any(
                    expected[name][1] == 0 for name in item["sources"]))):
            raise ValueError("component review must supply notices and any required corresponding sources")
        covered.add(item["id"])
    if not {package["SPDXID"] for package in sbom["packages"]} <= covered:
        raise ValueError("signed review omits installed SBOM components")


def fetch_source(audit, output=None):
    """Anonymous stable URL; only GitHub release delivery may redirect to its public asset CDN."""
    hasher, count = hashlib.sha256(), 0
    initial = public_url(audit["source_bundle_url"])
    parsed = urlsplit(initial)
    github_release = parsed.hostname == "github.com" and re.fullmatch(
        r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^/]+/[^/]+", parsed.path) is not None
    current = initial
    started = time.monotonic()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=60,
                      headers={"Accept-Encoding": "identity"}) as client:
        for attempt in range(3):
            client.cookies.clear()
            with client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    target = urlsplit(urljoin(current, location))
                    if (not github_release or attempt != 0 or len(location) > 8192 or target.scheme != "https"
                            or target.hostname != "release-assets.githubusercontent.com" or target.username or target.password
                            or target.port not in (None, 443) or target.fragment
                            or not target.path.startswith("/github-production-release-asset/")):
                        raise ValueError("source delivery redirect is not the permitted anonymous GitHub release CDN")
                    current = target.geturl()
                    continue
                if response.status_code != 200 or response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("corresponding sources are not anonymously available at the stable public URL")
                for chunk in response.iter_bytes(1024**2):
                    if time.monotonic() - started > 1800:
                        raise ValueError("public source delivery exceeds its 30 minute allowance")
                    count += len(chunk)
                    if count > audit["source_bundle_bytes"]:
                        raise ValueError("public source delivery exceeds its reviewed size")
                    hasher.update(chunk)
                    if output is not None:
                        output.write(chunk)
                break
        else:
            raise ValueError("source delivery exceeds its redirect allowance")
    if (hasher.hexdigest(), count) != (audit["source_bundle_sha256"], audit["source_bundle_bytes"]):
        raise ValueError("public source delivery differs from the signed source bundle")


def run(argv, environment):
    result = subprocess.run(argv, env=environment, capture_output=True, check=False, timeout=5400)
    if result.returncode:
        raise ValueError("publication command failed; no completion catalog was accepted (credential-bearing output suppressed)")
    return result.stdout


def verify_public_image(image, parent):
    """Fresh anonymous pull and production-worker execution; no caller registry credentials."""
    with tempfile.TemporaryDirectory(prefix="reasoning-public-recheck-", dir=parent) as temporary:
        private = Path(temporary)
        anonymous = private / "anonymous.json"
        anonymous.write_text('{"auths":{}}')
        anonymous.chmod(0o600)
        (private / "config.json").write_text('{"auths":{}}')
        environment = {"PATH": os.defpath, "HOME": str(private), "DOCKER_CONFIG": str(private),
                       "REGISTRY_AUTH_FILE": str(anonymous)}
        raw = run(["skopeo", "inspect", "--raw", "--authfile", str(anonymous), "docker://" + image["reference"]], environment)
        if len(raw) > 1024**2 or digest(raw) != image["reference"].split("@")[1]:
            raise ValueError("anonymous public registry digest differs from the reviewed image")
        run(["docker", "--host", "unix:///var/run/docker.sock", "pull", image["reference"]], environment)
        candidate = private / "candidate.json"
        write_new(candidate, canonical([image]).encode())
        accepted = verify_execution(candidate, private / "acceptance", image["id"])
        if accepted.get("execution_verified") is not True or accepted.get("image") != image or not accepted.get("cases"):
            raise ValueError("public-digest execution did not return complete matching acceptance evidence")
        return accepted


def write_matching(path, body):
    if path.exists() or path.is_symlink():
        hash_file(path, max(len(body), 1))
        if path.read_bytes() != body:
            raise ValueError("retained publication evidence differs; preserve original inputs")
    else:
        write_new(path, body)


def public_acceptance(accepted, image):
    """Project the real successful return value onto a fixed, credential-free public schema."""
    if (not isinstance(accepted, dict) or type(accepted.get("format")) is not int or accepted["format"] != 1
            or accepted.get("verification") != "actual-loopback-http-public-api-and-docker-worker"
            or accepted.get("execution_verified") is not True or accepted.get("image") != image
            or not isinstance(accepted.get("cases"), list) or not 1 <= len(accepted["cases"]) <= 100):
        raise ValueError("public execution receipt is not complete matching real-worker evidence")
    cases = []
    fields = {"case", "job_id", "status", "error_code", "program_digest", "logs_digest", "cleanup_verified"}
    for item in accepted["cases"]:
        if (not isinstance(item, dict) or not fields <= set(item) or item["cleanup_verified"] is not True
                or not isinstance(item["case"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", item["case"])
                or not isinstance(item["job_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", item["job_id"])
                or item["status"] not in {"succeeded", "failed", "cancelled"}
                or item["error_code"] is not None and (
                    not isinstance(item["error_code"], str) or not re.fullmatch(r"[a-z0-9_-]{1,128}", item["error_code"]))
                or any(not isinstance(item[key], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", item[key])
                       for key in ("program_digest", "logs_digest"))):
            raise ValueError("public execution case lacks bounded completion and cleanup evidence")
        cases.append({key: item[key] for key in fields})
    return {"format": 1, "verification": accepted["verification"], "image": image, "execution_verified": True,
            "cases": cases, "public_https_verified": False, "cloud_verified": False, "native_clients_verified": False,
            "signature_verified": False, "license_source_audit_verified": False}


def publish(root, archive, profile, repository, bundle, audit_path, signature, trusted_key, output, *, token=None, username=None):
    if not re.fullmatch(r"ghcr\.io/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)+", repository):
        raise ValueError("select an explicit lowercase GHCR repository")
    audit, fingerprint = load_audit(audit_path, signature, trusted_key)
    revision, recipe_sha = recipe_identity(root, profile)
    schema = json_body((root / "contracts/executor/v1alpha1/image-capabilities.schema.json").read_bytes())
    report, evidence = inspect_image(archive, schema, repository)
    image = report["image"]
    if (audit["profile"] != profile or image["id"] != profile or audit["source_revision"] != revision
            or image["source_revision"] != revision or audit["recipe_sha256"] != recipe_sha
            or image["reference"] != repository + "@" + audit["image_digest"]):
        raise ValueError("review does not identify this exact committed recipe, profile and image digest")
    verify_source_bundle(bundle, audit, json_body(evidence["usr/share/reasoning/sbom.spdx.json"]))
    # Every review/source gate runs before any authenticated registry effect.
    fetch_source(audit)
    if not token or not username or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", username):
        raise ValueError("supply a registry token through the private environment and its GitHub username")
    archive_sha, archive_bytes = hash_file(archive)
    if audit.get("format") == 2 and (archive_sha, archive_bytes) != (audit["archive_sha256"], audit["archive_bytes"]):
        raise ValueError("retained OCI archive bytes differ from the signed machine provenance")
    intent = canonical({"image": image, "audit_sha256": digest(canonical(audit).encode()),
        "source_bundle_sha256": audit["source_bundle_sha256"], "maintainer_key_sha256": fingerprint,
        "archive_sha256": archive_sha, "archive_bytes": archive_bytes}).encode()
    from .deployment.private import protected_parent
    if output.exists():
        protected_parent(output / "publication-intent.json", writable=False)
    else:
        protected_parent(output)
    if output.exists() or output.is_symlink():
        info = output.lstat()
        if output.is_symlink() or not output.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("publication output must be an owner-owned private directory")
        if not (output / "publication-intent.json").exists():
            raise ValueError("existing output has no matching retained publication intent")
        write_matching(output / "publication-intent.json", intent)
    else:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
        write_new(output / "publication-intent.json", intent)
    with tempfile.TemporaryDirectory(prefix="reasoning-registry-", dir=output) as temporary:
        private = Path(temporary)
        auth, anonymous = private / "auth.json", private / "anonymous.json"
        auth.write_text(json.dumps({"auths": {"ghcr.io": {"auth": base64.b64encode((username + ":" + token).encode()).decode()}}}))
        anonymous.write_text('{"auths":{}}')
        auth.chmod(0o600)
        anonymous.chmod(0o600)
        environment = {"PATH": os.defpath, "HOME": str(private), "DOCKER_CONFIG": str(private), "REGISTRY_AUTH_FILE": str(anonymous)}
        tag = repository + ":sha256-" + audit["image_digest"][7:]
        run(["skopeo", "copy", "--all", "--preserve-digests", "--src-authfile", str(anonymous), "--dest-authfile", str(auth),
             "oci-archive:" + str(archive), "docker://" + tag], environment)
        raw = run(["skopeo", "inspect", "--raw", "--authfile", str(anonymous), "docker://" + image["reference"]], environment)
        if len(raw) > 1024**2 or digest(raw) != audit["image_digest"]:
            raise ValueError("anonymous public registry digest differs from the reviewed image")
        # The Docker client gets a separate empty config: no ambient login can turn a private image into public proof.
        docker_config = private / "docker"
        docker_config.mkdir(mode=0o700)
        (docker_config / "config.json").write_text('{"auths":{}}')
        environment["DOCKER_CONFIG"] = str(docker_config)
        run(["docker", "--host", "unix:///var/run/docker.sock", "pull", image["reference"]], environment)
        internal = output / "candidate-catalog.json"
        write_matching(internal, canonical([image]).encode())
        acceptance = output / "acceptance"
        # A retry never overwrites a credential-bearing test directory; retained digest inputs remain exact.
        if acceptance.exists() or acceptance.is_symlink():
            for attempt in range(1, 100):
                acceptance = output / f"acceptance-retry-{attempt}"
                if not acceptance.exists() and not acceptance.is_symlink():
                    break
            else:
                raise ValueError("publication exceeds its bounded acceptance retry allowance")
        accepted = verify_execution(internal, acceptance, profile)
        if accepted.get("execution_verified") is not True or accepted.get("image") != image or not accepted.get("cases"):
            raise ValueError("public-digest execution did not return complete matching acceptance evidence")
        sanitized = public_acceptance(accepted, image)
    # Only now may operators receive a usable catalog. The authenticated
    # attestation purpose retains its human/machine assurance distinction.
    catalog = (canonical([image]) + "\n").encode()
    public_path = output / "public-acceptance.json"
    if public_path.exists() or public_path.is_symlink():
        hash_file(public_path, 1024**2)
        retained = json_body(public_path.read_bytes())
        if public_acceptance(retained, image) != retained:
            raise ValueError("retained public acceptance receipt is not canonical sanitized matching evidence")
        public_body = (canonical(retained) + "\n").encode()
        if public_path.read_bytes() != public_body:
            raise ValueError("retained public acceptance receipt is not canonical")
    else:
        public_body = (canonical(sanitized) + "\n").encode()
        write_new(public_path, public_body)
    receipt = {"format": 1, "image": image, "audit_sha256": digest(canonical(audit).encode()),
               "maintainer_key_sha256": fingerprint, "audit_signature_verified": True, "image_signature_verified": False,
               "independent_license_audit_verified": False, "maintainer_review": audit.get("reviewer"),
               "source_bundle_url": audit["source_bundle_url"], "anonymous_source_delivery_verified": True,
               "anonymous_registry_digest_verified": True, "execution_verified": True,
               "acceptance_receipt": "public-acceptance.json", "acceptance_sha256": digest(public_body),
               "catalog_sha256": digest(catalog)}
    if audit.get("format") == 2:
        receipt.update(attestation_purpose=audit["purpose"], human_review_verified=False,
                       complete_dependency_closure_verified=False, scope_caveats=audit["scope_caveats"])
    write_matching(output / "publication.json", (canonical(receipt) + "\n").encode())
    write_matching(output / "source-review.json", canonical(audit).encode())
    write_matching(output / "source-review.ed25519", signature.read_bytes())
    write_matching(output / "compute-images.json", catalog)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--fetch-source-only", action="store_true",
                        help="verify the signed attestation and fetch exact sources before publication")
    parser.add_argument("--recipe-from-bundle", action="store_true",
                        help="extract the signed retained recipe without requiring historical Git objects (machine provenance only)")
    parser.add_argument("--profile", choices=tuple(PROFILES), required=True)
    parser.add_argument("--repository", help="explicit destination, for example ghcr.io/OWNER/reasoning/PROFILE")
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--trusted-key", type=Path, required=True, help="independently selected maintainer Ed25519 public key")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.fetch_source_only:
            audit, _ = load_audit(args.audit, args.signature, args.trusted_key)
            if audit["profile"] != args.profile:
                raise ValueError("review does not identify the source checkout recipe")
            if args.recipe_from_bundle and audit.get("format") != 2:
                raise ValueError("archive-backed recipe extraction requires machine provenance")
            if not args.recipe_from_bundle:
                revision, recipe_sha = recipe_identity(args.source_root, args.profile)
                if audit["source_revision"] != revision or audit["recipe_sha256"] != recipe_sha:
                    raise ValueError("review does not identify the source checkout recipe")
            with args.source_bundle.open("xb") as stream:
                fetch_source(audit, stream)
            if args.recipe_from_bundle:
                from .image_provenance import extract_recipe

                extract_recipe(args.source_bundle, audit, args.source_root)
            print(json.dumps({"review_signature_verified": True, "published": False, "source_bundle": str(args.source_bundle)}))
            return 0
        if args.archive is None:
            raise ValueError("publication requires the exact reviewed candidate archive")
        if not args.repository:
            raise ValueError("publication requires an explicit destination repository")
        result = publish(args.source_root, args.archive, args.profile,
            args.repository, args.source_bundle, args.audit, args.signature,
            args.trusted_key, args.output, token=os.environ.get("REASONING_IMAGE_REGISTRY_TOKEN"),
            username=os.environ.get("REASONING_IMAGE_REGISTRY_USER"))
        print(json.dumps({"published": True, "image": result["image"]["reference"], "catalog": str(args.output / "compute-images.json")}))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, tarfile.TarError, httpx.HTTPError, RuntimeFault):
        uncertain = (args.output / "publication-intent.json").exists()
        message = ("image publication is unverified; the registry may have changed. Preserve original source/archive/review and intent; "
                   "no new completion catalog is accepted. If the exact upload exists but anonymous pull is refused, "
                   "set that GHCR package to Public as its owner, then resume the same publication inputs.\n" if uncertain else
                   "image publication preflight failed; no registry request was started. Check the source/archive/review inputs; "
                   "no completion catalog is accepted\n")
        parser.exit(1, message)


if __name__ == "__main__":
    raise SystemExit(main())
