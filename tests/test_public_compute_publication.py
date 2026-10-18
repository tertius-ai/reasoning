"""Synthetic signed source-review gates and publication contract; no real push or cloud execution.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import base64
import hashlib
import io
import json
import tarfile

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_public_compute_images import REVISION, ROOT, SCHEMA, write_oci

from reasoning_ledger import image_publication as publication
from reasoning_ledger.image_release import inspect_image
from reasoning_ledger.server.compute_catalog import canonical, load_catalog


def execution_receipt(image):
    """Schema-faithful synthetic receipt; this does not execute a real container."""
    return {"format": 1, "verification": "actual-loopback-http-public-api-and-docker-worker", "image": image,
            "execution_verified": True, "cases": [{"case": "synthetic_boundary", "job_id": "a" * 32,
                "status": "succeeded", "error_code": None, "program_digest": "sha256:" + "b" * 64,
                "logs_digest": "sha256:" + "c" * 64, "cleanup_verified": True}],
            "public_https_verified": False, "cloud_verified": False, "native_clients_verified": False,
            "signature_verified": False, "license_source_audit_verified": False}


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    archive, descriptor, _ = write_oci(tmp_path)
    repo = "ghcr.io/research/bench/research-cpu"
    report, evidence = inspect_image(archive, SCHEMA, repo)
    entries = {"sources/fixture.tar": b"synthetic source bytes; not a real dependency closure",
               "notices/fixture.txt": b"synthetic test notice; not a real upstream license"}
    bundle = tmp_path / "sources.tar"
    with tarfile.open(bundle, "w") as stream:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            stream.addfile(info, io.BytesIO(content))
    sbom = json.loads(evidence["usr/share/reasoning/sbom.spdx.json"])
    audit = {"format": 1, "profile": "research-cpu", "reviewer": "Synthetic fixture reviewer", "reviewed_at": "2026-10-12T10:00:00Z",
             "source_revision": REVISION, "recipe_sha256": "a" * 64, "image_digest": descriptor["digest"],
             "source_bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(), "source_bundle_bytes": bundle.stat().st_size,
             "source_bundle_url": "https://sources.example.org/immutable-fixture.tar",
             "dependency_closure_reviewed": True, "source_delivery_approved": True, "public_binary_distribution_approved": True,
             "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                       for name, content in entries.items()],
             "coverage": [{"id": item["SPDXID"], "disposition": "source-included", "sources": ["sources/fixture.tar"],
                           "notices": ["notices/fixture.txt"], "reason": "Synthetic fixture only, not a legal release audit."}
                          for item in sbom["packages"]]}
    key = Ed25519PrivateKey.generate()
    audit_path, signature, public_key = (tmp_path / name for name in ("audit.json", "review.ed25519", "review-key.pem"))
    public_key.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    def sign():
        audit_path.write_text(canonical(audit))
        signature.write_bytes(key.sign(publication.DOMAIN + canonical(audit).encode()))
    sign()
    monkeypatch.setattr(publication, "recipe_identity", lambda root, profile: (REVISION, "a" * 64))
    original = httpx.Client
    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        def response(request):
            assert "authorization" not in request.headers and "cookie" not in request.headers
            return httpx.Response(200, content=bundle.read_bytes())
        return original(**options, transport=httpx.MockTransport(response))
    monkeypatch.setattr(publication.httpx, "Client", client)
    args = (ROOT, archive, "research-cpu", repo, bundle, audit_path, signature, public_key, tmp_path / "published")
    return args, audit, sign, report, sbom


def test_publication_catalog_only_after_all_review_public_pull_and_actual_execution_gates(reviewed, monkeypatch):
    args, _, _, report, _ = reviewed
    effects = []
    def run(argv, env):
        assert "fixture-private-token" not in " ".join(argv) and "REASONING_IMAGE_REGISTRY_TOKEN" not in env
        assert env["HOME"] != str(ROOT) and env["REGISTRY_AUTH_FILE"].endswith("anonymous.json")
        if argv[1] == "copy":
            auth_path = argv[argv.index("--dest-authfile") + 1]
            from pathlib import Path
            auth = json.loads(Path(auth_path).read_bytes())["auths"]["ghcr.io"]["auth"]
            assert base64.b64decode(auth).decode() == "fixture-user:fixture-private-token"
            assert "--preserve-digests" in argv
        effects.append(argv)
        if argv[1] == "inspect":
            # Exact OCI runtime/index bytes are taken from the inspected synthetic archive, not invented here.
            from reasoning_ledger.image_release import OCIArchive
            archive = OCIArchive(args[1])
            try:
                return archive.read("blobs/sha256/" + report["image"]["reference"].split("@sha256:")[1], 1024**2)
            finally:
                archive.close()
        return b""
    def execute(catalog, output, profile):
        assert not (args[-1] / "compute-images.json").exists()
        assert load_catalog(catalog)[profile].record() == report["image"]
        return execution_receipt(report["image"])
    monkeypatch.setattr(publication, "run", run)
    monkeypatch.setattr(publication, "verify_execution", execute)
    result = publication.publish(*args, token="fixture-private-token", username="fixture-user")
    assert len(effects) == 3 and effects[-1][0] == "docker"
    assert result["audit_signature_verified"] and result["anonymous_source_delivery_verified"]
    assert result["image_signature_verified"] is result["independent_license_audit_verified"] is False
    public = args[-1] / "public-acceptance.json"
    assert result["acceptance_receipt"] == public.name
    assert result["acceptance_sha256"] == publication.digest(public.read_bytes())
    assert json.loads(public.read_bytes()) == execution_receipt(report["image"])
    assert load_catalog(args[-1] / "compute-images.json")["research-cpu"].record() == report["image"]
    assert "fixture-private-token" not in (args[-1] / "publication.json").read_text()
    assert not list(args[-1].glob("reasoning-registry-*")), "credential files are private and removed after the scoped operation"


def test_signed_prior_noether_image_is_refused_before_public_fetch_or_registry_effect(reviewed, monkeypatch, tmp_path):
    args, audit, sign, _, _ = reviewed
    def prior(sbom):
        source = next(package for package in sbom["packages"] if package["name"] == "noether-source")
        source["versionInfo"] = "ca8fc50f3d9a902a827fbd558dba138ad7bf0f8f"
    directory = tmp_path / "prior"
    directory.mkdir()
    archive, _, _ = write_oci(directory, change_inventory=prior)
    report, _ = inspect_image(archive, SCHEMA, args[3])
    audit["image_digest"] = report["image"]["reference"].split("@")[1]
    sign()
    monkeypatch.setattr(publication, "fetch_source", lambda *arguments: pytest.fail("must refuse before public source fetch"))
    monkeypatch.setattr(publication, "run", lambda *arguments: pytest.fail("must refuse before any registry effect"))
    with pytest.raises(ValueError, match="approved public Noether release; rebuild"):
        publication.publish(args[0], archive, *args[2:], token="fixture-private-token", username="fixture-user")


@pytest.mark.parametrize("change", ["signature", "untrusted-key", "review", "digest", "revision", "recipe",
                                    "bundle", "coverage", "notices"])
def test_failed_review_has_no_registry_effect_or_completion_catalog(reviewed, monkeypatch, change):
    args, audit, sign, _, _ = reviewed
    if change == "signature":
        args[6].write_bytes(b"x" * 64)
    elif change == "untrusted-key":
        args[7].write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    elif change == "bundle":
        args[4].write_bytes(b"changed source bundle")
    else:
        if change == "review":
            audit["source_delivery_approved"] = False
        elif change == "digest":
            audit["image_digest"] = "sha256:" + "e" * 64
        elif change == "revision":
            audit["source_revision"] = "c" * 40
        elif change == "recipe":
            audit["recipe_sha256"] = "d" * 64
        elif change == "coverage":
            audit["coverage"].pop()
        elif change == "notices":
            audit["coverage"][0]["notices"] = []
        sign()
    effects = []
    monkeypatch.setattr(publication, "run", lambda *args: effects.append(args))
    with pytest.raises(ValueError):
        publication.publish(*args, token="fixture-private-token", username="fixture-user")
    assert effects == [] and not (args[-1] / "compute-images.json").exists()


@pytest.mark.parametrize("stage", ["source-not-public", "source-mismatch", "registry-push", "anonymous-digest", "execution"])
def test_external_gate_failure_never_emits_approved_catalog(reviewed, monkeypatch, stage):
    args, _, _, report, _ = reviewed
    if stage in {"source-not-public", "source-mismatch"}:
        original = httpx.Client
        # Use the original underlying httpx constructor rather than the fixture's content transport.
        from httpx import _client
        monkeypatch.setattr(publication.httpx, "Client", lambda **options: _client.Client(**options,
            transport=httpx.MockTransport(lambda request: httpx.Response(403 if stage == "source-not-public" else 200,
                                                                         content=b"not reviewed sources"))))
        assert original
    def run(argv, env):
        if stage == "registry-push":
            raise ValueError("fixture registry denial")
        if argv[1] == "inspect":
            if stage == "anonymous-digest":
                return b"changed manifest"
            from reasoning_ledger.image_release import OCIArchive
            archive = OCIArchive(args[1])
            try:
                return archive.read("blobs/sha256/" + report["image"]["reference"].split("@sha256:")[1], 1024**2)
            finally:
                archive.close()
        return b""
    monkeypatch.setattr(publication, "run", run)
    monkeypatch.setattr(publication, "verify_execution", lambda *args: {"execution_verified": False})
    with pytest.raises(ValueError):
        publication.publish(*args, token="fixture-private-token", username="fixture-user")
    assert not (args[-1] / "compute-images.json").exists() and not (args[-1] / "publication.json").exists()


@pytest.mark.parametrize("stage", ["registry-upload", "anonymous-manifest", "anonymous-pull", "worker-execution"])
def test_publication_failure_identifies_the_stage_without_disclosing_command_output(reviewed, monkeypatch, capsys, stage):
    from reasoning_ledger.image_release import OCIArchive

    args, _, _, report, _ = reviewed

    def run(argv, environment):
        current = "registry-upload" if argv[1] == "copy" else "anonymous-manifest" if argv[1] == "inspect" else "anonymous-pull"
        if stage == current:
            raise ValueError("private-registry-token-and-command-output")
        if argv[1] == "inspect":
            archive = OCIArchive(args[1])
            try:
                return archive.read("blobs/sha256/" + report["image"]["reference"].split("@sha256:")[1], 1024**2)
            finally:
                archive.close()
        return b""

    def execute(*args):
        raise ValueError("private-execution-output")

    monkeypatch.setattr(publication, "run", run)
    monkeypatch.setattr(publication, "verify_execution", execute)
    with pytest.raises(publication.PublicationStepFailure) as raised:
        publication.publish(*args, token="fixture-private-token", username="fixture-user")
    assert raised.value.stage == stage
    output = capsys.readouterr().out + str(raised.value)
    assert "private-registry-token-and-command-output" not in output and "private-execution-output" not in output
    assert "fixture-private-token" not in output
    assert not (args[-1] / "compute-images.json").exists()


@pytest.mark.parametrize("stage", ["registry-upload", "anonymous-manifest"])
def test_cli_distinguishes_failed_upload_from_failed_anonymous_access(reviewed, monkeypatch, capsys, stage):
    args = reviewed[0]

    def fail(*args, **kwargs):
        raise publication.PublicationStepFailure(stage)

    monkeypatch.setattr(publication, "publish", fail)
    argv = ["--source-root", str(args[0]), "--archive", str(args[1]), "--profile", args[2], "--repository", args[3],
            "--source-bundle", str(args[4]), "--audit", str(args[5]), "--signature", str(args[6]),
            "--trusted-key", str(args[7]), "--output", str(args[8])]
    with pytest.raises(SystemExit) as raised:
        publication.main(argv)
    assert raised.value.code == 1
    output = capsys.readouterr().err
    assert publication.PUBLICATION_FAILURES[stage] in output
    assert ("Change visibility" in output) == (stage == "anonymous-manifest")
    assert "retry the original workflow run" in output


def test_source_bundle_rejects_signed_symlinks_and_unreviewed_members(reviewed):
    args, audit, sign, _, sbom = reviewed
    with tarfile.open(args[4], "w") as archive:
        item = tarfile.TarInfo("sources/fixture.tar")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        archive.addfile(item)
    audit["source_bundle_sha256"], audit["source_bundle_bytes"] = publication.hash_file(args[4])
    sign()
    with pytest.raises(ValueError, match="linked"):
        publication.verify_source_bundle(args[4], audit, sbom)


@pytest.mark.parametrize("url", ["http://example.org/source", "https://user:password@example.org/source",
                                  "https://example.org/source?token=x", "https://127.0.0.1/source", "https://localhost/source"])
def test_public_source_url_rejects_credentials_private_hosts_and_expiring_tokens(url):
    with pytest.raises(ValueError):
        publication.public_url(url)


def test_manual_workflow_requires_signed_review_before_build_and_publication():
    workflow = (ROOT / ".github/workflows/compute-publish.yml").read_text()
    assert "workflow_dispatch:" in workflow and "pull_request:" not in workflow and "push:" not in workflow
    assert "environment: compute-publication" in workflow
    assert workflow.index("--fetch-source-only") < workflow.index("image_candidate") < workflow.index("REASONING_IMAGE_REGISTRY_TOKEN:")
    assert "--retain-archive" not in workflow and "image_ci" not in workflow, "never rebuild a reviewed digest"
    assert '--name "candidate-$SOURCE_REVISION-$PROFILE"' in workflow
    assert "--trusted-key images/publication/review-key.pem" in workflow
    assert '--repository "ghcr.io/${GITHUB_REPOSITORY,,}/$PROFILE"' in workflow
    assert "if: failure()" in workflow and "publication-intent.json" in workflow
    assert "published/public-acceptance.json" in workflow
    assert "published/acceptance/acceptance.json" not in workflow
    assert "path: ${{ runner.temp }}/reasoning-publication\n" not in workflow, "never upload private acceptance/registry credentials"


def test_local_publication_requires_explicit_destination_before_registry_effects(reviewed, monkeypatch, capsys):
    args, _, _, _, _ = reviewed
    def forbidden(*args, **kwargs):
        pytest.fail("missing destination must not reach publication")
    monkeypatch.setattr(publication, "publish", forbidden)
    argv = ["--source-root", str(args[0]), "--archive", str(args[1]), "--profile", args[2],
            "--source-bundle", str(args[4]), "--audit", str(args[5]), "--signature", str(args[6]),
            "--trusted-key", str(args[7]), "--output", str(args[8])]
    with pytest.raises(SystemExit) as raised:
        publication.main(argv)
    assert raised.value.code == 1
    assert "no registry request was started" in capsys.readouterr().err
    assert not args[8].exists()
