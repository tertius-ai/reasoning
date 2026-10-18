"""Guided maintainer source review; drafts never grant public image approval.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import image_publication as publication
from .deployment.host import InstallError
from .deployment.private import locked_directory, read_private, write_private
from .image_release import PROFILES, BoundedTarInfo, digest, inspect_image, json_body, member_name
from .server.compute_catalog import canonical, load_catalog
from .server.compute_docker import RuntimeFault

MAX_REVIEW = 8 * 1024**2
APPROVALS = {
    "dependency_closure_reviewed": "reviewed dependency closure",
    "source_delivery_approved": "approved source delivery",
    "public_binary_distribution_approved": "approved public binary distribution",
}


def read_json(path):
    publication.hash_file(path, MAX_REVIEW)
    return json_body(path.read_bytes())


def inventory(bundle):
    """Inventory actual files without extracting arbitrary archive paths."""
    publication.hash_file(bundle)
    files, names, total = [], set(), 0
    with tarfile.open(bundle, "r|*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            archive.members.clear()
            name = member_name(entry.name)
            if entry.isdir():
                if entry.size:
                    raise ValueError("source directory carries a payload")
                continue
            if (name != entry.name or name in names or not entry.isreg() or entry.sparse is not None
                    or any(key.startswith("GNU.sparse") for key in entry.pax_headers) or len(files) >= 65536):
                raise ValueError("source inventory rejects links, duplicate, sparse or unsafe files")
            names.add(name)
            total += entry.size
            if total > 64 * 1024**3:
                raise ValueError("source inventory exceeds its uncompressed allowance")
            hasher, count = hashlib.sha256(), 0
            with archive.extractfile(entry) as stream:
                while chunk := stream.read(1024**2):
                    count += len(chunk)
                    hasher.update(chunk)
            if count != entry.size:
                raise ValueError("source member was truncated")
            files.append({"path": name, "sha256": hasher.hexdigest(), "bytes": count})
    if not files:
        raise ValueError("source bundle is empty")
    return sorted(files, key=lambda item: item["path"])


def prepare(root, archive, profile, repository, bundle, source_url, state):
    revision, recipe_sha = publication.recipe_identity(root, profile)
    report, evidence = inspect_image(archive, json_body(
        (root / "contracts/executor/v1alpha1/image-capabilities.schema.json").read_bytes()), repository)
    if report["image"]["id"] != profile or report["image"]["source_revision"] != revision:
        raise ValueError("candidate does not match this committed recipe and profile")
    bundle_sha, bundle_bytes = publication.hash_file(bundle)
    archive_sha, archive_bytes = publication.hash_file(archive)
    files = inventory(bundle)
    if publication.hash_file(bundle) != (bundle_sha, bundle_bytes):
        raise ValueError("source bundle changed during inventory")
    audit = {"format": 1, "reviewer": "", "reviewed_at": "", "profile": profile,
             "source_revision": revision, "recipe_sha256": recipe_sha,
             "image_digest": report["image"]["reference"].split("@")[1],
             "source_bundle_sha256": bundle_sha, "source_bundle_bytes": bundle_bytes,
             "source_bundle_url": publication.public_url(source_url), "files": files,
             "coverage": [{"id": item["SPDXID"], "disposition": "unreviewed", "sources": [], "notices": [], "reason": ""}
                          for item in json_body(evidence["usr/share/reasoning/sbom.spdx.json"])["packages"]],
             **dict.fromkeys(APPROVALS, False)}
    binding = {"format": 1, "image": report["image"], "archive_sha256": archive_sha, "archive_bytes": archive_bytes,
               "recipe_sha256": recipe_sha, "source_bundle_sha256": bundle_sha, "source_bundle_bytes": bundle_bytes,
               "source_bundle_url": audit["source_bundle_url"]}
    with locked_directory(state) as directory:
        intent = directory / "review-inputs.json"
        if intent.exists() or intent.is_symlink():
            if read_json(intent) != binding:
                raise ValueError("review inputs differ from the retained candidate; use the original bytes")
        else:
            write_private(intent, canonical(binding).encode(), maximum=MAX_REVIEW)
        draft = directory / "review-draft.json"
        if not draft.exists() and not draft.is_symlink():
            write_private(draft, canonical(audit).encode(), maximum=MAX_REVIEW)
    return audit


def confirm(message, phrase, *, ask=input):
    if ask(f"{message}\nType '{phrase}' to confirm: ").strip() != phrase:
        raise ValueError("review paused without approval")


def create_key(path, *, ask=input, password=getpass.getpass):
    """Never generate a maintainer identity merely by preparing candidate evidence."""
    confirm("Create an encrypted maintainer signing key at this exact private path?", "create review key", ask=ask)
    print("Choose a passphrase for the compute-image signing key and save it in your password manager. "
          "Input stays hidden. Press Ctrl-C to pause.")
    while True:
        phrase = password("New signing-key passphrase (hidden): ")
        if not phrase:
            print("The passphrase cannot be empty. Try again.")
        elif phrase != password("Confirm signing-key passphrase (hidden): "):
            print("The passphrases did not match. Try again.")
        else:
            break
    key = Ed25519PrivateKey.generate()
    write_private(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.BestAvailableEncryption(phrase.encode())), maximum=16384)
    return key


def gh_json(arguments):
    """The normal protected gh login is used without copying its token into output or argv."""
    result = subprocess.run(["gh", *arguments], capture_output=True, timeout=180, check=False)
    if result.returncode or len(result.stdout) > 2 * 1024**2:
        raise ValueError("GitHub source publication request failed; credential-bearing output suppressed")
    return json_body(result.stdout)


def publish_source(bundle, repository, release, state, *, ask=input):
    """Upload only an explicitly approved source bundle to an already-public repository/release."""
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", release)):
        raise ValueError("select an explicit GitHub repository and bounded release tag")
    sha, size = publication.hash_file(bundle)
    if size >= 2 * 1024**3:
        raise ValueError("GitHub release source assets must be smaller than 2 GiB; select another stable public host")
    files = inventory(bundle)
    repo = gh_json(["api", f"repos/{repository}"])
    if repo.get("visibility") != "public" or repo.get("private") is not False:
        raise ValueError("source hosting repository must already be public; this command never changes privacy or creates repositories")
    hosted = gh_json(["release", "view", release, "--repo", repository, "--json", "url,assets,isDraft,isPrerelease"])
    if hosted.get("isDraft") is not False or hosted.get("isPrerelease") is not False:
        raise ValueError("select an existing published non-prerelease source-hosting release")
    name = f"compute-sources-{sha}.tar"
    url = f"https://github.com/{repository}/releases/download/{release}/{name}"
    audit = {"source_bundle_url": url, "source_bundle_sha256": sha, "source_bundle_bytes": size}
    intent = {"format": 1, **audit, "repository": repository, "release": release, "files": files}
    with locked_directory(state) as directory:
        saved = directory / "source-upload-intent.json"
        if saved.exists() or saved.is_symlink():
            if read_json(saved) != intent:
                raise ValueError("source upload differs from its retained exact bundle/repository/release")
        else:
            write_private(saved, canonical(intent).encode(), maximum=MAX_REVIEW)
        from rich.text import Text

        from .deployment.setup_ui import console
        console.print(Text(f"Public destination: {url}\nBundle SHA-256: {sha}\n"
                           f"Bytes: {size} · inventoried files: {len(files)}\n"
                           f"Full inventory: {saved}\n"
                           "Review that the ENTIRE bundle is intended public and contains no credentials, private code or datasets.",
                           style="yellow"))
        confirm("Publish these exact source bytes to the selected already-public release?",
                f"publish sources {sha[:12]}", ask=ask)
        if any(asset.get("name") == name for asset in hosted.get("assets", [])):
            # Never clobber a public digest-named asset, including after an uncertain upload result.
            publication.fetch_source(audit)
            publication.write_matching(directory / "source-upload-complete.json", canonical(audit).encode())
            return audit
        with tempfile.TemporaryDirectory(prefix="reasoning-source-upload-", dir=directory) as temporary:
            retained = Path(temporary) / name
            shutil.copyfile(bundle, retained, follow_symlinks=False)
            if publication.hash_file(retained) != (sha, size) or publication.hash_file(bundle) != (sha, size):
                raise ValueError("source bytes changed before upload; nothing was published")
            result = subprocess.run(["gh", "release", "upload", release, str(retained), "--repo", repository],
                                    capture_output=True, timeout=1800, check=False)
            if result.returncode:
                raise ValueError("source upload is unverified; preserve the exact inputs and retry (credential-bearing output suppressed)")
        publication.fetch_source(audit)
        write_matching = publication.write_matching
        write_matching(directory / "source-upload-complete.json", canonical(audit).encode())
    return audit


def sign(root, archive, bundle, state, key_path, trusted_key, *, ask=input, password=getpass.getpass):
    with locked_directory(state) as directory:
        binding, draft = read_json(directory / "review-inputs.json"), read_json(directory / "review-draft.json")
        if set(draft) != publication.AUDIT_FIELDS or any(draft[key] is not False for key in APPROVALS):
            raise ValueError("draft approvals must remain false; approvals are collected by this wizard")
        profile = binding["image"]["id"]
        revision, recipe_sha = publication.recipe_identity(root, profile)
        report, evidence = inspect_image(archive, json_body(
            (root / "contracts/executor/v1alpha1/image-capabilities.schema.json").read_bytes()),
            binding["image"]["reference"].split("@")[0])
        if (report["image"] != binding["image"] or revision != draft["source_revision"]
                or recipe_sha != binding["recipe_sha256"] or draft["recipe_sha256"] != recipe_sha
                or draft["profile"] != profile or draft["image_digest"] != report["image"]["reference"].split("@")[1]
                or publication.hash_file(archive) != (binding["archive_sha256"], binding["archive_bytes"])
                or any(draft[key] != binding[key] for key in ("source_bundle_sha256", "source_bundle_bytes", "source_bundle_url"))):
            raise ValueError("review differs from the exact retained candidate and sources")
        # Validate actual coverage/source inventory before collecting human approval or touching a signing key.
        publication.verify_source_bundle(bundle, draft, json_body(evidence["usr/share/reasoning/sbom.spdx.json"]))
        if not isinstance(draft["reviewer"], str) or not 1 <= len(draft["reviewer"].strip()) <= 200:
            raise ValueError("edit the draft with the human reviewer's name and complete component coverage first")
        for _, phrase in APPROVALS.items():
            confirm("Confirm your actual source/notice/distribution review, not an automated audit.", phrase, ask=ask)
        if not key_path.exists() and not key_path.is_symlink():
            key = create_key(key_path, ask=ask, password=password)
        else:
            key = serialization.load_pem_private_key(read_private(key_path, 16384),
                password=password("Signing-key passphrase (hidden): ").encode())
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("review signing requires an Ed25519 key")
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        if trusted_key.exists() or trusted_key.is_symlink():
            publication.hash_file(trusted_key, 16384)
            if trusted_key.read_bytes() != public:
                raise ValueError("signing key differs from the independently selected trusted public key")
        else:
            confirm("Enroll this new maintainer public key at the selected trust path?", "enroll review public key", ask=ask)
            write_private(trusted_key, public, maximum=16384)
        audit_path, signature = directory / "source-review.json", directory / "source-review.ed25519"
        if audit_path.exists() or audit_path.is_symlink():
            existing = read_json(audit_path)
            if {**existing, "reviewed_at": "", **dict.fromkeys(APPROVALS, False)} != {**draft, "reviewed_at": ""}:
                raise ValueError("completed review differs from the draft; preserve original signed inputs")
            if any(existing[key] is not True for key in APPROVALS) or not existing["reviewed_at"].endswith("Z"):
                raise ValueError("retained signed review has invalid approval or timestamp")
            datetime.fromisoformat(existing["reviewed_at"].replace("Z", "+00:00"))
            if not signature.exists() and not signature.is_symlink():
                write_private(signature, key.sign(publication.DOMAIN + canonical(existing).encode()), maximum=64)
            return publication.load_audit(audit_path, signature, trusted_key)[0]
        if signature.exists() or signature.is_symlink():
            raise ValueError("signature without retained reviewed inputs cannot be adopted")
        audit = {**draft, **dict.fromkeys(APPROVALS, True), "reviewed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
        write_private(audit_path, canonical(audit).encode(), maximum=MAX_REVIEW)
        write_private(signature, key.sign(publication.DOMAIN + canonical(audit).encode()), maximum=64)
        return publication.load_audit(audit_path, signature, trusted_key)[0]


def combine(directories, trusted_key, output):
    """Reverify public delivery/execution rather than trusting editable completion booleans."""
    if not 1 <= len(directories) <= 32:
        raise ValueError("select 1–32 completed publications")
    images = {}
    for directory in directories:
        receipt = read_json(directory / "publication.json")
        catalog = directory / "compute-images.json"
        publication.hash_file(catalog, 65536)
        entries = load_catalog(catalog)
        audit, fingerprint = publication.load_audit(directory / "source-review.json", directory / "source-review.ed25519", trusted_key)
        if (len(entries) != 1 or receipt.get("image") not in [image.record() for image in entries.values()]
                or receipt.get("catalog_sha256") != digest(catalog.read_bytes())
                or receipt.get("audit_sha256") != digest(canonical(audit).encode())
                or receipt.get("maintainer_key_sha256") != fingerprint
                or any(receipt.get(key) is not True for key in ("audit_signature_verified", "anonymous_source_delivery_verified",
                                                               "anonymous_registry_digest_verified", "execution_verified"))):
            raise ValueError("select complete matching public publication evidence, not candidate catalogs")
        image = next(iter(entries.values())).record()
        if (image["id"] != audit["profile"] or image["source_revision"] != audit["source_revision"]
                or image["reference"].split("@")[1] != audit["image_digest"] or image["id"] in images):
            raise ValueError("publication does not match the signed image or duplicates a profile")
        if receipt.get("acceptance_receipt") != "public-acceptance.json":
            raise ValueError("publication does not bind its canonical public execution receipt")
        public_path = directory / "public-acceptance.json"
        publication.hash_file(public_path, 1024**2)
        if receipt.get("acceptance_sha256") != digest(public_path.read_bytes()):
            raise ValueError("public execution receipt differs from its completed publication")
        accepted = read_json(public_path)
        if publication.public_acceptance(accepted, image) != accepted:
            raise ValueError("public execution receipt is not sanitized matching real-worker evidence")
        publication.fetch_source(audit)
        publication.verify_public_image(image, directory)
        images[image["id"]] = image
    body = (canonical([images[key] for key in sorted(images)]) + "\n").encode()
    if len(body) > 65536:
        raise ValueError("combined catalog exceeds its allowance")
    # No output until every image has passed a fresh anonymous delivery/execution check.
    with output.open("xb") as stream:
        stream.write(body)
    return images


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "sign"):
        sub = commands.add_parser(action)
        sub.add_argument("--source-root", type=Path, required=True)
        sub.add_argument("--archive", type=Path, required=True)
        sub.add_argument("--source-bundle", type=Path, required=True)
        sub.add_argument("--state", type=Path, required=True)
        if action == "prepare":
            sub.add_argument("--profile", choices=tuple(PROFILES), required=True)
            sub.add_argument("--repository", required=True)
            sub.add_argument("--source-url", required=True)
        else:
            sub.add_argument("--private-key", type=Path, required=True)
            sub.add_argument("--trusted-key", type=Path, required=True)
    sub = commands.add_parser("combine")
    sub.add_argument("--publication", type=Path, action="append", required=True)
    sub.add_argument("--trusted-key", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True)
    sub = commands.add_parser("publish-source")
    sub.add_argument("--source-bundle", type=Path, required=True)
    sub.add_argument("--repository", required=True, help="already-public source-hosting repository")
    sub.add_argument("--release", required=True, help="existing published non-prerelease tag")
    sub.add_argument("--state", type=Path, required=True)
    sub = commands.add_parser("publish-source-spaces", help="guided large-source delivery to DigitalOcean Spaces")
    sub.add_argument("--source-bundle", type=Path, help="prompted when omitted")
    sub.add_argument("--bucket", help="prompted, with retained defaults, when omitted")
    sub.add_argument("--region", help="prompted when omitted")
    sub.add_argument("--state", type=Path, help="private journal base; default ~/.reasoning-compute-publication")
    sub.add_argument("--credentials-file", type=Path, help="optional owner-only JSON file; otherwise hidden CLI entry")
    args = parser.parse_args(argv)
    from rich.panel import Panel
    from rich.text import Text

    from .deployment.setup_ui import console
    heading = "COMPUTE SOURCE PUBLICATION" if args.action == "publish-source-spaces" else "COMPUTE IMAGE REVIEW"
    console.print(Panel.fit(Text(heading + "\nExact sources and retained candidate bytes; no automated human approval."),
                            title=" REASONING ", border_style="bright_cyan", padding=(1, 3)))
    try:
        if args.action == "prepare":
            prepare(args.source_root, args.archive, args.profile, args.repository, args.source_bundle, args.source_url, args.state)
            console.print(Text(f"Draft: {args.state / 'review-draft.json'}\n"
                               "Edit reviewer and every component's source/notice coverage; approvals stay false.\n"
                               "No image or source bytes were published.", style="cyan"))
        elif args.action == "sign":
            sign(args.source_root, args.archive, args.source_bundle, args.state, args.private_key, args.trusted_key)
            console.print(Text("Exact review signed. Publish the retained archive, not a rebuilt image. "
                               "No public catalog exists yet.", style="green"))
        elif args.action == "combine":
            combine(args.publication, args.trusted_key, args.output)
            console.print(Text(f"Reverified approved catalog: {args.output}", style="green"))
        elif args.action == "publish-source-spaces":
            from .image_spaces import publish_source as publish_spaces
            result = publish_spaces(args.source_bundle, args.bucket, args.region, args.state, args.credentials_file)
            console.print(Text(f"Anonymous source delivery verified: {result['source_bundle_url']}\n"
                               "Use this URL for maintainer review. No image or approved catalog was published.", style="green"))
        else:
            result = publish_source(args.source_bundle, args.repository, args.release, args.state)
            console.print(Text(f"Anonymous source delivery verified: {result['source_bundle_url']}", style="green"))
        return 0
    except KeyboardInterrupt:
        parser.exit(130, "compute review: paused; preserve private state and retry the original inputs\n")
    except (ValueError, KeyError, TypeError, OSError, InstallError, tarfile.TarError,
            subprocess.SubprocessError, httpx.HTTPError, RuntimeFault) as exc:
        parser.exit(1, f"compute review: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
