"""Machine-observed source provenance, never a fabricated human or legal audit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import re
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import image_publication as publication
from .deployment.private import locked_directory, read_private, write_private
from .image_release import SOURCE_PATHS, BoundedTarInfo, inspect_image, json_body, member_name
from .image_sources import inventory as components_from_sbom
from .server.compute_catalog import canonical

DOMAIN = b"reasoning-compute-observed-source-provenance-v1\n"
PURPOSE = "observed-source-provenance-v1"
LIMIT = 8 * 1024**2
DISCLAIMERS = (
    "human review must match binary native/Python components to sources, notices, patches and build/relink materials",
    "full installed dependency closure is not proved by this package inventory or upstream source archives",
    "Julia BinaryBuilder native builds and Lean compiler/native dependencies need exact corresponding build/source review",
)
SCOPE_SUMMARIES = dict(zip(DISCLAIMERS, (
    "Source correspondence and build/relink completeness: not independently reviewed.",
    "Full installed dependency closure: not proved by the inventory/source archives.",
    "Julia BinaryBuilder and Lean native/compiler source correspondence: not independently reviewed.",
), strict=True))
FIELDS = {"format", "purpose", "profile", "source_revision", "recipe_sha256", "image_digest", "sbom_sha256",
          "source_bundle_sha256", "source_bundle_bytes", "source_bundle_url", "files", "coverage", "collection_sha256",
          "scope_caveats", "human_review_verified", "legal_audit_verified", "complete_dependency_closure_verified",
          "archive_sha256", "archive_bytes"}
COMPONENT_FIELDS = {"id", "name", "version", "kind", "source", "source_version", "source_candidates", "notice_candidates"}


def validate(audit):
    if (not isinstance(audit, dict) or set(audit) != FIELDS or type(audit["format"]) is not int or audit["format"] != 2
            or audit["purpose"] != PURPOSE or audit["profile"] not in publication.PROFILES
            or not isinstance(audit["source_revision"], str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", audit["source_revision"])
            or not isinstance(audit["image_digest"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", audit["image_digest"])
            or any(not isinstance(audit[field], str) or not publication.HEX.fullmatch(audit[field]) for field in (
                "recipe_sha256", "sbom_sha256", "source_bundle_sha256", "collection_sha256", "archive_sha256"))
            or type(audit["source_bundle_bytes"]) is not int or not 1 <= audit["source_bundle_bytes"] <= publication.MAX_BUNDLE
            or type(audit["archive_bytes"]) is not int or not 1 <= audit["archive_bytes"] <= publication.MAX_BUNDLE
            or any(audit[field] is not False for field in (
                "human_review_verified", "legal_audit_verified", "complete_dependency_closure_verified"))
            or not isinstance(audit["scope_caveats"], list)
            or any(not isinstance(item, str) or item not in DISCLAIMERS for item in audit["scope_caveats"])
            or len(set(audit["scope_caveats"])) != len(audit["scope_caveats"])):
        raise ValueError("invalid machine provenance purpose, bindings, or assurance scope")
    publication.public_url(audit["source_bundle_url"])
    return audit


def collection_from_bundle(bundle):
    with tarfile.open(bundle, "r|*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            archive.members.clear()
            if entry.name == "collection.json":
                if not entry.isreg() or not 0 < entry.size <= LIMIT:
                    raise ValueError("source collection must be a bounded regular archive member")
                body = archive.extractfile(entry).read(LIMIT + 1)
                return json_body(body), hashlib.sha256(body).hexdigest()
    raise ValueError("source archive has no retained collection.json provenance")


def coverage(collection, files, sbom, profile, revision):
    """Match observed identities and retained candidates; do not prove correspondence."""
    if (collection.get("format") != 1 or collection.get("profile") != profile or collection.get("source_revision") != revision
            or any(collection.get(field) is not False for field in (
                "dependency_closure_reviewed", "source_delivery_approved", "public_binary_distribution_approved"))):
        raise ValueError("collection bindings changed or falsely claim human review")
    caveats = []
    for item in collection.get("unresolved", []):
        if not isinstance(item, dict) or set(item) != {"reason"} or item["reason"] not in DISCLAIMERS:
            raise ValueError("source collection has concrete missing source/version/build evidence; resolve its retained unresolved record")
        caveats.append(item["reason"])
    available = {item["path"]: item for item in files}
    notice_records = collection.get("notices")
    if not isinstance(notice_records, list) or not 1 <= len(notice_records) <= 65536:
        raise ValueError("source collection lacks hashed notice provenance")
    notices = set()
    for item in notice_records:
        if (not isinstance(item, dict) or item.get("path") not in available or item["path"] in notices
                or item.get("sha256") != available[item["path"]]["sha256"]
                or item.get("bytes") != available[item["path"]]["bytes"]):
            raise ValueError("notice provenance differs from retained hashed source-bundle members")
        notices.add(item["path"])
    values, seen = [], set()
    components = collection.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 10000:
        raise ValueError("source collection lacks bounded component provenance")
    for item in components:
        if (not isinstance(item, dict) or set(item) != COMPONENT_FIELDS
                or any(not isinstance(item[field], str) or not 1 <= len(item[field]) <= 1000 for field in (
                    "id", "name", "version", "kind", "source", "source_version")) or item["id"] in seen):
            raise ValueError("source component identity/version evidence is absent or duplicated")
        for field in ("source_candidates", "notice_candidates"):
            paths = item[field]
            if (not isinstance(paths, list) or not 1 <= len(paths) <= 10000
                    or any(not isinstance(path, str) or path not in available for path in paths)
                    or len(set(paths)) != len(paths)
                    or not any(available[path]["bytes"] > 0 for path in paths)):
                raise ValueError("source component is missing a concrete retained source or notice path: " + item["id"])
        if not set(item["notice_candidates"]) <= notices:
            raise ValueError("component notice candidates lack retained notice provenance: " + item["id"])
        seen.add(item["id"])
        values.append({"id": item["id"], "disposition": "observed-source-candidates",
                       "sources": item["source_candidates"], "notices": item["notice_candidates"],
                       "name": item["name"], "version": item["version"], "kind": item["kind"],
                       "source": item["source"], "source_version": item["source_version"]})
    observed = {item["id"]: item for item in components_from_sbom(sbom)}
    classified = {item["id"]: item for item in components}
    for identity, item in observed.items():
        candidate = classified.get(identity)
        if candidate is None or any(candidate[field] != item[field] for field in ("name", "version", "kind", "source", "source_version")):
            raise ValueError("source component differs from installed SBOM identity/version: " + identity)
    return sorted(values, key=lambda item: item["id"]), sorted(set(caveats))


def verify(bundle, audit, sbom):
    validate(audit)
    if hashlib.sha256(canonical(sbom).encode()).hexdigest() != audit["sbom_sha256"]:
        raise ValueError("machine provenance differs from actual installed image SBOM")
    collection, sha = collection_from_bundle(bundle)
    mapped, caveats = coverage(collection, audit["files"], sbom, audit["profile"], audit["source_revision"])
    if sha != audit["collection_sha256"] or mapped != audit["coverage"] or caveats != audit["scope_caveats"]:
        raise ValueError("signed component/source provenance differs from the archived collection")
    recipe = [item for item in audit["files"] if item["path"] == "recipe.tar"]
    if len(recipe) != 1 or recipe[0]["sha256"] != audit["recipe_sha256"]:
        raise ValueError("source bundle omits the exact original recipe archive")


def _prepare_audit(root, archive, profile, repository, bundle, source_url, draft_path):
    from .image_review import inventory, read_json

    revision, recipe_sha = publication.recipe_identity(root, profile)
    report, evidence = inspect_image(archive, json_body(
        (root / "contracts/executor/v1alpha1/image-capabilities.schema.json").read_bytes()), repository)
    publication.require_public_noether(json_body(evidence["usr/share/reasoning/sbom.spdx.json"]))
    image = report["image"]
    draft = read_json(draft_path)
    sha, size = publication.hash_file(bundle)
    files = inventory(bundle)
    if (image["id"] != profile or image["source_revision"] != revision
            or draft.get("profile") != profile or draft.get("source_revision") != revision
            or draft.get("recipe_sha256") != recipe_sha or draft.get("source_bundle_sha256") != sha
            or draft.get("source_bundle_bytes") != size or sorted(draft.get("files", []), key=lambda item: item["path"]) != files
            or any(draft.get(field) is not False for field in (
                "dependency_closure_reviewed", "source_delivery_approved", "public_binary_distribution_approved"))):
        raise ValueError("retained source draft/image/recipe/bundle identities disagree")
    collection, collection_sha = collection_from_bundle(bundle)
    sbom = json_body(evidence["usr/share/reasoning/sbom.spdx.json"])
    classification = collection.get("classification_provenance")
    if classification is not None and classification.get("actual_installed_spdx_sha256") != hashlib.sha256(
            evidence["usr/share/reasoning/sbom.spdx.json"]).hexdigest():
        raise ValueError("source candidate classification differs from actual immutable image SBOM bytes")
    mapped, caveats = coverage(collection, files, sbom, profile, revision)
    archive_sha, archive_bytes = publication.hash_file(archive)
    audit = {"format": 2, "purpose": PURPOSE, "profile": profile, "source_revision": revision,
             "recipe_sha256": recipe_sha, "image_digest": image["reference"].split("@")[1],
             "sbom_sha256": hashlib.sha256(canonical(sbom).encode()).hexdigest(), "collection_sha256": collection_sha,
             "source_bundle_sha256": sha, "source_bundle_bytes": size, "source_bundle_url": publication.public_url(source_url),
             "files": files, "coverage": mapped, "scope_caveats": caveats,
             "archive_sha256": archive_sha, "archive_bytes": archive_bytes,
             "human_review_verified": False, "legal_audit_verified": False, "complete_dependency_closure_verified": False}
    publication.verify_source_bundle(bundle, audit, sbom)
    return audit


def observe(root, archive, profile, repository, bundle, draft_path):
    """Read-only exact-byte preflight before source upload; not a signable attestation.

    The reserved non-distribution URL is internal to validation and is never
    returned, persisted, signed or fetched. Call prepare with the real public
    delivery URL after the uploader independently verifies these exact bytes.
    """
    try:
        audit = _prepare_audit(root, archive, profile, repository, bundle,
                               "https://invalid.example/observed-source-only", draft_path)
    except tarfile.TarError as exc:
        raise ValueError("retained source/recipe archive is malformed; no upload or signing was attempted") from exc
    return {field: audit[field] for field in (
        "profile", "source_revision", "recipe_sha256", "image_digest", "sbom_sha256", "collection_sha256",
        "source_bundle_sha256", "source_bundle_bytes", "archive_sha256", "archive_bytes", "scope_caveats")}


def prepare(root, archive, profile, repository, bundle, source_url, draft_path, state):
    audit = _prepare_audit(root, archive, profile, repository, bundle, source_url, draft_path)
    inputs = {"source_root": str(root.absolute()), "archive": str(archive.absolute()), "profile": profile,
              "repository": repository, "source_bundle": str(bundle.absolute()), "source_url": source_url,
              "draft": str(draft_path.absolute())}
    with locked_directory(state) as directory:
        publication.write_matching(directory / "provenance-inputs.json", canonical(inputs).encode())
        publication.write_matching(directory / "source-review.json", canonical(audit).encode())
    return audit


def sign(state, key_path, trusted_key, *, ask=input, password=getpass.getpass):
    from .image_review import confirm, create_key, read_json

    with locked_directory(state) as directory:
        audit_path, signature = directory / "source-review.json", directory / "source-review.ed25519"
        audit = validate(read_json(audit_path))
        inputs = read_json(directory / "provenance-inputs.json")
        rechecked = _prepare_audit(Path(inputs["source_root"]), Path(inputs["archive"]), inputs["profile"], inputs["repository"],
                                   Path(inputs["source_bundle"]), inputs["source_url"], Path(inputs["draft"]))
        if rechecked != audit:
            raise ValueError("provenance changed before signing; retain exact original inputs")
        print(f"Observed-source provenance: {len(audit['coverage'])} components; no human or complete legal audit is claimed.")
        for caveat in audit["scope_caveats"]:
            print("Scope limitation: " + SCOPE_SUMMARIES[caveat])
        confirm("Sign and approve PUBLIC DISTRIBUTION of these exact image/source provenance bytes with the stated limitations?",
                "publish provenance " + audit["image_digest"][7:19], ask=ask)
        if not key_path.exists() and not key_path.is_symlink():
            key = create_key(key_path, ask=ask, password=password)
        else:
            key = serialization.load_pem_private_key(read_private(key_path, 16384),
                                                    password=password("Signing-key passphrase (hidden): ").encode())
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("provenance signing requires an encrypted Ed25519 key")
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        if trusted_key.exists() or trusted_key.is_symlink():
            if read_private(trusted_key, 16384) != public:
                raise ValueError("signing key differs from independently enrolled trust key")
        else:
            confirm("Enroll this exact provenance public key at the selected trust path?", "enroll provenance public key", ask=ask)
            write_private(trusted_key, public, maximum=16384)
        publication.write_matching(signature, key.sign(DOMAIN + canonical(audit).encode()))
        return publication.load_audit(audit_path, signature, trusted_key)[0]


def _recipe_members(body, profile, revision):
    result = {}
    with tarfile.open(fileobj=io.BytesIO(body), tarinfo=BoundedTarInfo) as archive:
        if archive.pax_headers.get("comment") != revision:
            raise ValueError("retained Git recipe archive does not bind its original source revision")
        for entry in archive:
            name = member_name(entry.name)
            if entry.isdir():
                continue
            paths = (*SOURCE_PATHS, "images/lean-mathlib") if profile == "lean-mathlib" else SOURCE_PATHS
            if (name != entry.name or not entry.isreg() or name in result or len(result) >= 512
                    or entry.size > 16 * 1024**2 or not any(name == path or name.startswith(path + "/") for path in paths)):
                raise ValueError("recipe archive contains linked, duplicated or unapproved paths")
            result[name] = archive.extractfile(entry).read(16 * 1024**2 + 1)
    return result


def extract_recipe(bundle, audit, output):
    validate(audit)
    _extract_recipe(bundle, audit, output)


def extract_candidate_recipe(bundle, draft_path, output):
    """Extract unsigned retained recipe bytes for CI-bound observation, not trust.

    The draft is an untrusted discovery hint. Its full bundle/recipe hashes and
    original Git archive identity are checked, but only subsequent authenticated
    candidate CI matching and actual OCI/SBOM observation justify signing.
    """
    from .image_review import read_json

    draft = read_json(draft_path)
    if (type(draft.get("format")) is not int or draft["format"] != 1 or draft.get("profile") not in publication.PROFILES
            or not isinstance(draft.get("source_revision"), str)
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", draft["source_revision"])
            or any(not isinstance(draft.get(field), str) or not publication.HEX.fullmatch(draft[field])
                   for field in ("source_bundle_sha256", "recipe_sha256"))
            or type(draft.get("source_bundle_bytes")) is not int or not 1 <= draft["source_bundle_bytes"] <= publication.MAX_BUNDLE
            or any(draft.get(field) is not False for field in (
                "dependency_closure_reviewed", "source_delivery_approved", "public_binary_distribution_approved"))):
        raise ValueError("unsigned candidate recipe draft lacks bounded exact provenance or falsely claims approval")
    _extract_recipe(bundle, draft, output)


def _extract_recipe(bundle, audit, output):
    if publication.hash_file(bundle) != (audit["source_bundle_sha256"], audit["source_bundle_bytes"]):
        raise ValueError("source bundle differs from retained recipe provenance")
    with tarfile.open(bundle, "r|*", tarinfo=BoundedTarInfo) as archive:
        for entry in archive:
            archive.members.clear()
            if entry.name == "recipe.tar":
                if not entry.isreg() or not 0 < entry.size <= 64 * 1024**2:
                    raise ValueError("recipe archive is absent or exceeds its allowance")
                body = archive.extractfile(entry).read(64 * 1024**2 + 1)
                break
        else:
            raise ValueError("source bundle has no exact recipe archive")
    if hashlib.sha256(body).hexdigest() != audit["recipe_sha256"]:
        raise ValueError("retained recipe archive differs from signed provenance")
    entries = _recipe_members(body, audit["profile"], audit["source_revision"])
    if output.exists() or output.is_symlink():
        raise ValueError("recipe extraction requires a new exact directory")
    output.mkdir(mode=0o700)
    for name, content in entries.items():
        path = output / name
        parent = output
        for part in Path(name).parts[:-1]:
            parent = parent / part
            parent.mkdir(mode=0o700, exist_ok=True)
        write_private(path, content, maximum=16 * 1024**2)
    write_private(output / ".reasoning-recipe.tar", body, maximum=64 * 1024**2)
    write_private(output / ".reasoning-recipe.json", canonical({"profile": audit["profile"],
                  "source_revision": audit["source_revision"]}).encode())


def recipe_identity(root, profile):
    metadata = json_body(read_private(root / ".reasoning-recipe.json"))
    if metadata.get("profile") != profile:
        raise ValueError("retained recipe directory belongs to a different profile")
    body = read_private(root / ".reasoning-recipe.tar", 64 * 1024**2)
    entries = _recipe_members(body, profile, metadata["source_revision"])
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != set(entries) | {".reasoning-recipe.tar", ".reasoning-recipe.json"}:
        raise ValueError("retained recipe directory has unexpected files")
    for name, content in entries.items():
        if read_private(root / name, 16 * 1024**2) != content:
            raise ValueError("retained recipe checkout differs from its exact archived bytes")
    return metadata["source_revision"], hashlib.sha256(body).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--trusted-key", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    audit, _ = publication.load_audit(args.audit, args.signature, args.trusted_key)
    extract_recipe(args.source_bundle, audit, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
