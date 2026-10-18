"""Observed provenance and protected signing fixtures, not public/legal acceptance.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import copy
import hashlib
import io
import os
import tarfile

import pytest
from cryptography.hazmat.primitives import serialization
from test_public_compute_publication import reviewed as reviewed_fixture
from test_public_compute_review import approve

from reasoning_ledger import image_provenance as provenance
from reasoning_ledger import image_publication as publication
from reasoning_ledger.image_sources import inventory
from reasoning_ledger.server.compute_catalog import canonical


def tar_bytes(entries, *, revision=None):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": revision} if revision else {}) as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return result.getvalue()


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    args, audit, _, _, sbom = reviewed_fixture.__wrapped__(tmp_path, monkeypatch)
    components = [{**item, "source_candidates": ["sources/fixture.tar"], "notice_candidates": ["notices/fixture.txt"]}
                  for item in inventory(sbom)]
    collection = {"format": 1, "profile": "research-cpu", "source_revision": audit["source_revision"], "components": components,
                  "notices": [{"path": "notices/fixture.txt", "sha256": hashlib.sha256(b"notice bytes").hexdigest(),
                               "bytes": len(b"notice bytes")}],
                  "unresolved": [{"reason": value} for value in provenance.DISCLAIMERS],
                  "dependency_closure_reviewed": False, "source_delivery_approved": False,
                  "public_binary_distribution_approved": False}
    recipe = tar_bytes({"LICENSE": b"fixture-license", "contracts/executor/v1alpha1/image-capabilities.schema.json": b"{}"},
                       revision=audit["source_revision"])
    entries = {"sources/fixture.tar": b"source bytes", "notices/fixture.txt": b"notice bytes", "recipe.tar": recipe,
               "collection.json": canonical(collection).encode()}
    bundle = args[4]
    bundle.write_bytes(tar_bytes(entries))
    recipe_sha = hashlib.sha256(recipe).hexdigest()
    monkeypatch.setattr(publication, "recipe_identity", lambda *_: (audit["source_revision"], recipe_sha))
    draft = {"format": 1, "profile": "research-cpu", "source_revision": audit["source_revision"], "recipe_sha256": recipe_sha,
             "source_bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(), "source_bundle_bytes": bundle.stat().st_size,
             "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                       for name, content in entries.items()],
             "dependency_closure_reviewed": False, "source_delivery_approved": False, "public_binary_distribution_approved": False}
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(canonical(draft))
    state = tmp_path / "provenance"
    return args, draft_path, state, collection, sbom


def prepare(candidate):
    args, draft, state, *_ = candidate
    return provenance.prepare(args[0], args[1], args[2], args[3], args[4], "https://sources.example.org/source.tar", draft, state)


def test_machine_provenance_preserves_scope_without_human_approval_records(candidate):
    audit = prepare(candidate)
    assert audit["format"] == 2 and audit["purpose"] == provenance.PURPOSE
    assert set(audit) == provenance.FIELDS and "reviewer" not in audit
    assert all(row["disposition"] == "observed-source-candidates" for row in audit["coverage"])
    assert audit["human_review_verified"] is audit["legal_audit_verified"] is audit["complete_dependency_closure_verified"] is False
    assert audit["scope_caveats"] == sorted(provenance.DISCLAIMERS)
    assert not (candidate[2] / "source-review.ed25519").exists()
    assert prepare(candidate) == audit
    assert set(provenance.SCOPE_SUMMARIES) == set(provenance.DISCLAIMERS)
    assert all("missing" not in value for value in provenance.SCOPE_SUMMARIES.values())


def test_observe_validates_before_upload_without_signable_state(candidate):
    args, draft, state, *_ = candidate
    observed = provenance.observe(args[0], args[1], args[2], args[3], args[4], draft)
    assert observed["source_bundle_sha256"] == publication.hash_file(args[4])[0]
    assert observed["source_bundle_bytes"] == args[4].stat().st_size
    assert "source_bundle_url" not in observed and "format" not in observed and "purpose" not in observed
    assert not state.exists()
    args[4].write_bytes(b"replaced before upload")
    with pytest.raises(ValueError):
        provenance.observe(args[0], args[1], args[2], args[3], args[4], draft)
    assert not state.exists()


@pytest.mark.parametrize("drift", ["version", "source_version", "name", "kind", "missing_source", "missing_notice", "missing_component",
                                   "concrete_unresolved", "fake_human_approval"])
def test_concrete_missing_or_mismatched_evidence_blocks_machine_provenance(candidate, drift):
    _, _, _, original, sbom = candidate
    collection = copy.deepcopy(original)
    if drift in {"version", "source_version", "name", "kind"}:
        collection["components"][0][drift] = "mismatched"
    elif drift in {"missing_source", "missing_notice"}:
        collection["components"][0]["source_candidates" if drift == "missing_source" else "notice_candidates"] = ["missing/path"]
    elif drift == "missing_component":
        collection["components"].pop()
    elif drift == "concrete_unresolved":
        collection["unresolved"].append({"source": "actual-package", "source_version": "1.0", "reason": "missing exact source"})
    else:
        collection["dependency_closure_reviewed"] = True
    from reasoning_ledger.image_review import read_json

    files = read_json(candidate[1])["files"]
    with pytest.raises(ValueError):
        provenance.coverage(collection, files, sbom, "research-cpu", original["source_revision"])


def test_observed_notice_candidates_may_include_empty_supplements_but_require_real_notice(candidate):
    from reasoning_ledger.image_review import read_json

    files = read_json(candidate[1])["files"] + [{"path": "notices/empty", "sha256": "a" * 64, "bytes": 0}]
    collection = copy.deepcopy(candidate[3])
    collection["notices"].append({"path": "notices/empty", "sha256": "a" * 64, "bytes": 0})
    collection["components"][0]["notice_candidates"].append("notices/empty")
    provenance.coverage(collection, files, candidate[4], "research-cpu", collection["source_revision"])
    collection["components"][0]["notice_candidates"] = ["notices/empty"]
    with pytest.raises(ValueError):
        provenance.coverage(collection, files, candidate[4], "research-cpu", collection["source_revision"])


def test_signing_is_explicit_encrypted_idempotent_and_domain_separated(candidate):
    audit = prepare(candidate)
    state = candidate[2]
    key, public = state / "private.pem", state / "public.pem"
    signed = provenance.sign(state, key, public, ask=approve, password=lambda _: "fixture-passphrase")
    assert signed == audit and b"ENCRYPTED PRIVATE KEY" in key.read_bytes()
    assert provenance.sign(state, key, public, ask=approve, password=lambda _: "fixture-passphrase") == audit
    signature = state / "source-review.ed25519"
    publication.load_audit(state / "source-review.json", signature, public)
    private = serialization.load_pem_private_key(key.read_bytes(), password=b"fixture-passphrase")
    signature.write_bytes(private.sign(publication.DOMAIN + canonical(audit).encode()))
    with pytest.raises(ValueError, match="signature"):
        publication.load_audit(state / "source-review.json", signature, public)


@pytest.mark.parametrize("umask", [0o000, 0o002, 0o022])
def test_prepared_and_signed_state_is_readable_by_private_staging_under_any_umask(candidate, umask):
    from reasoning_ledger.deployment.private import read_private

    state = candidate[2]
    old = os.umask(umask)
    try:
        audit = prepare(candidate)
        signed = provenance.sign(state, state / "private.pem", state / "public.pem",
                                 ask=approve, password=lambda _: "fixture-passphrase")
        assert signed == audit and prepare(candidate) == audit
        for name in ("source-review.json", "source-review.ed25519", "provenance-inputs.json"):
            path = state / name
            assert path.stat().st_mode & 0o077 == 0
            assert read_private(path, provenance.LIMIT) == path.read_bytes()
    finally:
        os.umask(old)


def test_sign_rechecks_exact_sources_before_key_creation(candidate):
    prepare(candidate)
    candidate[0][4].write_bytes(b"changed source archive")
    state = candidate[2]
    with pytest.raises((ValueError, tarfile.TarError)):
        provenance.sign(state, state / "private.pem", state / "public.pem", ask=approve, password=lambda _: "fixture-passphrase")
    assert not (state / "private.pem").exists()


@pytest.mark.parametrize("drift", ["purpose", "human", "closure", "legal", "unknown_caveat"])
def test_machine_assurance_cannot_be_relabelled_as_human_or_complete_audit(candidate, drift):
    audit = prepare(candidate)
    if drift == "purpose":
        audit["purpose"] = "human-review"
    elif drift == "unknown_caveat":
        audit["scope_caveats"] = ["missing exact source package"]
    else:
        fields = {"human": "human_review_verified", "closure": "complete_dependency_closure_verified", "legal": "legal_audit_verified"}
        field = fields[drift]
        audit[field] = True
    with pytest.raises(ValueError):
        provenance.validate(audit)


def test_recipe_is_recoverable_without_git_history_and_rejects_changed_files(candidate, tmp_path):
    audit = prepare(candidate)
    root = tmp_path / "recipe"
    provenance.extract_recipe(candidate[0][4], audit, root)
    assert provenance.recipe_identity(root, "research-cpu") == (audit["source_revision"], audit["recipe_sha256"])
    (root / "LICENSE").write_bytes(b"changed")
    with pytest.raises(ValueError, match="archived bytes"):
        provenance.recipe_identity(root, "research-cpu")


def test_unsigned_candidate_recipe_restores_exact_bytes_but_does_not_create_attestation(candidate, tmp_path):
    args, draft, state, *_ = candidate
    root = tmp_path / "unsigned-recipe"
    provenance.extract_candidate_recipe(args[4], draft, root)
    from reasoning_ledger.image_review import read_json

    hint = read_json(draft)
    assert provenance.recipe_identity(root, "research-cpu") == (hint["source_revision"], hint["recipe_sha256"])
    assert not state.exists()
    hint["public_binary_distribution_approved"] = True
    draft.write_text(canonical(hint))
    with pytest.raises(ValueError, match="falsely claims approval"):
        provenance.extract_candidate_recipe(args[4], draft, tmp_path / "false-approval")


def test_machine_bundle_verifier_checks_sbom_coverage_and_embedded_collection(candidate):
    audit = prepare(candidate)
    sbom = copy.deepcopy(candidate[4])
    sbom["packages"][0]["versionInfo"] = "mismatched"
    with pytest.raises(ValueError, match="SBOM"):
        publication.verify_source_bundle(candidate[0][4], audit, sbom)
    audit["coverage"][0]["sources"] = ["notices/fixture.txt"]
    with pytest.raises(ValueError, match="archived collection"):
        publication.verify_source_bundle(candidate[0][4], audit, candidate[4])


def test_signing_refusal_does_not_create_key_or_signature(candidate):
    prepare(candidate)
    state = candidate[2]
    with pytest.raises(ValueError):
        provenance.sign(state, state / "private.pem", state / "public.pem", ask=lambda _: "no", password=lambda _: "not called")
    assert not (state / "private.pem").exists() and not (state / "source-review.ed25519").exists()


def test_machine_publication_and_catalog_combination_never_claim_human_review(candidate, monkeypatch, tmp_path):
    from test_public_compute_images import SCHEMA
    from test_public_compute_review import complete

    from reasoning_ledger.image_release import inspect_image
    from reasoning_ledger.image_review import combine, read_json

    audit = prepare(candidate)
    state = candidate[2]
    key, public = state / "private.pem", state / "public.pem"
    provenance.sign(state, key, public, ask=approve, password=lambda _: "fixture-passphrase")
    args = candidate[0][:5] + (state / "source-review.json", state / "source-review.ed25519", public, tmp_path / "published")
    report, _ = inspect_image(args[1], SCHEMA, args[3])
    complete((args, audit, None, report, candidate[4]), monkeypatch)
    receipt = read_json(args[-1] / "publication.json")
    assert receipt["attestation_purpose"] == provenance.PURPOSE and receipt["maintainer_review"] is None
    assert receipt["human_review_verified"] is receipt["complete_dependency_closure_verified"] is False
    monkeypatch.setattr(publication, "verify_public_image", lambda *_: None)
    combine([args[-1]], public, tmp_path / "approved.json")
    assert (tmp_path / "approved.json").exists()
