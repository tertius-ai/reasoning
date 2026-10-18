"""Guided source review and completed-publication catalog boundaries.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from test_public_compute_publication import execution_receipt
from test_public_compute_publication import reviewed as reviewed_fixture

from reasoning_ledger import image_publication as publication
from reasoning_ledger import image_review as review
from reasoning_ledger.server.compute_catalog import canonical, load_catalog


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    return reviewed_fixture.__wrapped__(tmp_path, monkeypatch)


def prepared(reviewed, tmp_path):
    args, audit, _, _, _ = reviewed
    state = tmp_path / "review-state"
    draft = review.prepare(args[0], args[1], args[2], args[3], args[4], audit["source_bundle_url"], state)
    return args, audit, state, draft


def test_prepare_truthful_inventory_never_approves_or_publishes(reviewed, tmp_path):
    args, audit, state, draft = prepared(reviewed, tmp_path)
    assert draft["files"] == sorted(audit["files"], key=lambda row: row["path"])
    assert all(draft[key] is False for key in review.APPROVALS)
    assert all(item["disposition"] == "unreviewed" for item in draft["coverage"])
    assert not (state / "source-review.ed25519").exists()
    assert not (state / "compute-images.json").exists()
    assert (state / "review-draft.json").stat().st_mode & 0o077 == 0
    review.prepare(args[0], args[1], args[2], args[3], args[4], audit["source_bundle_url"], state)
    args[4].write_bytes(b"changed")
    with pytest.raises((ValueError, review.tarfile.TarError)):
        review.prepare(args[0], args[1], args[2], args[3], args[4], audit["source_bundle_url"], state)


def edit_draft(state, audit):
    draft = json.loads((state / "review-draft.json").read_bytes())
    draft["reviewer"] = "Synthetic fixture human reviewer"
    draft["coverage"] = audit["coverage"]
    (state / "review-draft.json").write_text(canonical(draft))


def approve(prompt):
    return prompt.split("Type '")[1].split("' to confirm:")[0]


def test_review_requires_explicit_human_approvals_and_encrypted_key(reviewed, tmp_path):
    args, audit, state, _ = prepared(reviewed, tmp_path)
    edit_draft(state, audit)
    key, public = state / "review-private.pem", state / "review-public.pem"
    result = review.sign(args[0], args[1], args[4], state, key, public,
                         ask=approve, password=lambda _: "fixture-encrypted-passphrase")
    assert all(result[key] is True for key in review.APPROVALS)
    assert b"ENCRYPTED PRIVATE KEY" in key.read_bytes()
    assert b"fixture-encrypted-passphrase" not in key.read_bytes()
    assert key.stat().st_mode & 0o077 == 0
    assert not (state / "compute-images.json").exists()
    assert publication.load_audit(state / "source-review.json", state / "source-review.ed25519", public)[0] == result
    assert review.sign(args[0], args[1], args[4], state, key, public, ask=approve,
                       password=lambda _: "fixture-encrypted-passphrase") == result
    # An interrupted signature write can be resumed without changing the retained reviewed timestamp/fields.
    (state / "source-review.ed25519").unlink()
    assert review.sign(args[0], args[1], args[4], state, key, public, ask=approve,
                       password=lambda _: "fixture-encrypted-passphrase") == result


@pytest.mark.parametrize("change", ["unreviewed", "automated-approval", "cancel", "changed-archive", "changed-notice"])
def test_review_does_not_silently_grant_approval(reviewed, tmp_path, change):
    args, audit, state, _ = prepared(reviewed, tmp_path)
    if change != "unreviewed":
        edit_draft(state, audit)
    if change == "automated-approval":
        draft = json.loads((state / "review-draft.json").read_bytes())
        draft["dependency_closure_reviewed"] = True
        (state / "review-draft.json").write_text(canonical(draft))
    elif change == "changed-archive":
        args[1].write_bytes(b"different archive")
    elif change == "changed-notice":
        draft = json.loads((state / "review-draft.json").read_bytes())
        draft["files"][0]["sha256"] = "f" * 64
        (state / "review-draft.json").write_text(canonical(draft))
    with pytest.raises((ValueError, review.tarfile.TarError)):
        review.sign(args[0], args[1], args[4], state, state / "private.pem", state / "public.pem",
                    ask=(lambda _: "") if change == "cancel" else approve,
                    password=lambda _: "fixture-passphrase")
    assert not (state / "source-review.ed25519").exists()


def test_key_creation_retries_empty_and_mismatched_passphrases_without_leaking_them(tmp_path, capsys):
    from cryptography.hazmat.primitives import serialization

    path = tmp_path / "signing-key.pem"
    answers = iter(["", "first-private-attempt", "wrong-confirmation", "saved-private-passphrase", "saved-private-passphrase"])

    def password(message):
        assert not path.exists()
        return next(answers)

    review.create_key(path, ask=approve, password=password)
    key = serialization.load_pem_private_key(path.read_bytes(), password=b"saved-private-passphrase")
    assert key is not None and path.stat().st_mode & 0o077 == 0
    output = capsys.readouterr().out
    assert "cannot be empty" in output and "did not match" in output and "password manager" in output
    assert all(value not in output for value in ("first-private-attempt", "wrong-confirmation", "saved-private-passphrase"))


def test_key_creation_can_be_cancelled_after_an_empty_passphrase(tmp_path):
    path = tmp_path / "signing-key.pem"
    answers = iter([""])

    def password(message):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    with pytest.raises(KeyboardInterrupt):
        review.create_key(path, ask=approve, password=password)
    assert not path.exists()


def complete(reviewed, monkeypatch, execute=None):
    args, _, _, report, _ = reviewed
    def run(argv, env):
        if argv[1] == "inspect":
            from reasoning_ledger.image_release import OCIArchive
            archive = OCIArchive(args[1])
            try:
                return archive.read("blobs/sha256/" + report["image"]["reference"].split("@sha256:")[1], 1024**2)
            finally:
                archive.close()
        return b""
    monkeypatch.setattr(publication, "run", run)
    monkeypatch.setattr(publication, "verify_execution", execute or (lambda *a: execution_receipt(report["image"])))
    publication.publish(*args, token="fixture-secret-token", username="fixture-user")
    return args, report


def test_combine_requires_completed_signed_receipts_and_fresh_execution(reviewed, monkeypatch, tmp_path):
    args, report = complete(reviewed, monkeypatch)
    seen = []
    monkeypatch.setattr(publication, "verify_public_image", lambda image, parent: seen.append(image))
    output = tmp_path / "approved.json"
    review.combine([args[-1]], args[7], output)
    assert seen == [report["image"]]
    assert load_catalog(output)["research-cpu"].record() == report["image"]
    with pytest.raises(FileExistsError):
        review.combine([args[-1]], args[7], output)


@pytest.mark.parametrize("change", ["receipt", "catalog", "signature", "public-source", "execution", "duplicate",
                                    "acceptance-path", "acceptance-hash", "acceptance-fields"])
def test_combination_never_emits_catalog_from_incomplete_or_failed_evidence(reviewed, monkeypatch, tmp_path, change):
    args, _ = complete(reviewed, monkeypatch)
    directory = args[-1]
    if change == "receipt":
        receipt = json.loads((directory / "publication.json").read_bytes())
        receipt["execution_verified"] = False
        (directory / "publication.json").write_text(canonical(receipt))
    elif change == "catalog":
        (directory / "compute-images.json").write_text("[]")
    elif change == "signature":
        (directory / "source-review.ed25519").write_bytes(b"x" * 64)
    elif change == "acceptance-path":
        receipt = json.loads((directory / "publication.json").read_bytes())
        receipt["acceptance_receipt"] = "acceptance/private-worker-token"
        (directory / "publication.json").write_text(canonical(receipt))
    elif change in {"acceptance-hash", "acceptance-fields"}:
        accepted = json.loads((directory / "public-acceptance.json").read_bytes())
        accepted["worker_token"] = "fixture-must-not-be-public"
        (directory / "public-acceptance.json").write_text(canonical(accepted))
        if change == "acceptance-fields":
            receipt = json.loads((directory / "publication.json").read_bytes())
            receipt["acceptance_sha256"] = publication.digest((directory / "public-acceptance.json").read_bytes())
            (directory / "publication.json").write_text(canonical(receipt))
    elif change == "public-source":
        monkeypatch.setattr(publication, "fetch_source", lambda _: (_ for _ in ()).throw(ValueError("no public source")))
    elif change == "execution":
        monkeypatch.setattr(publication, "verify_public_image", lambda *_: (_ for _ in ()).throw(ValueError("no execution")))
    if change != "execution":
        monkeypatch.setattr(publication, "verify_public_image", lambda *_: None)
    output = tmp_path / "must-not-exist.json"
    with pytest.raises(ValueError):
        review.combine([directory, directory] if change == "duplicate" else [directory], args[7], output)
    assert not output.exists()


def test_publication_resume_is_exact_archive_and_reverifies_public_gates(reviewed, monkeypatch):
    args, report = complete(reviewed, monkeypatch)
    publication.publish(*args, token="fixture-secret-token", username="fixture-user")
    assert load_catalog(args[-1] / "compute-images.json")["research-cpu"].record() == report["image"]
    intent = json.loads((args[-1] / "publication-intent.json").read_bytes())
    intent["archive_sha256"] = "f" * 64
    (args[-1] / "publication-intent.json").write_text(canonical(intent))
    with pytest.raises(ValueError, match="retained publication evidence"):
        publication.publish(*args, token="fixture-secret-token", username="fixture-user")


def test_retry_exports_sanitized_successful_receipt_without_private_attempt_credentials(reviewed, monkeypatch):
    args, _, _, report, _ = reviewed
    attempts = []
    def execute(catalog, output, profile):
        output.mkdir(mode=0o700)
        (output / "private-worker-token").write_text("fixture-private-enrollment-credential")
        attempts.append(output)
        if len(attempts) == 1:
            raise ValueError("synthetic interrupted actual-worker boundary")
        accepted = execution_receipt(report["image"])
        accepted["worker_token"] = "fixture-private-enrollment-credential"
        accepted["cases"][0]["private_header"] = "fixture-private-provider-header"
        (output / "acceptance.json").write_text(canonical(accepted))
        return accepted
    with pytest.raises(ValueError, match="interrupted"):
        complete(reviewed, monkeypatch, execute=execute)
    assert not (args[-1] / "public-acceptance.json").exists()
    assert not (args[-1] / "compute-images.json").exists()
    result = publication.publish(*args, token="fixture-secret-token", username="fixture-user")
    assert [attempt.name for attempt in attempts] == ["acceptance", "acceptance-retry-1"]
    assert not (attempts[0] / "acceptance.json").exists()
    public = args[-1] / result["acceptance_receipt"]
    assert result["acceptance_sha256"] == publication.digest(public.read_bytes())
    assert json.loads(public.read_bytes()) == execution_receipt(report["image"])
    assert b"fixture-private" not in public.read_bytes()
    assert (attempts[1] / "private-worker-token").read_text() == "fixture-private-enrollment-credential"
    # Completed repeats keep the original bound public receipt and append a private execution attempt.
    publication.publish(*args, token="fixture-secret-token", username="fixture-user")
    assert attempts[-1].name == "acceptance-retry-2"
    assert result["acceptance_sha256"] == publication.digest(public.read_bytes())


@pytest.mark.parametrize("change", ["format", "verification", "case", "cleanup", "digest", "status"])
def test_incomplete_returned_execution_receipt_cannot_create_public_completion(reviewed, monkeypatch, change):
    args, _, _, report, _ = reviewed
    accepted = execution_receipt(report["image"])
    if change in {"format", "verification"}:
        accepted[change] = "wrong"
    elif change == "case":
        accepted["cases"] = [{"name": "incomplete"}]
    elif change == "cleanup":
        accepted["cases"][0]["cleanup_verified"] = False
    elif change == "digest":
        accepted["cases"][0]["logs_digest"] = "not-a-digest"
    else:
        accepted["cases"][0]["status"] = "running"
    with pytest.raises(ValueError, match="receipt|case"):
        complete(reviewed, monkeypatch, execute=lambda *_: accepted)
    assert not (args[-1] / "public-acceptance.json").exists()
    assert not (args[-1] / "publication.json").exists()
    assert not (args[-1] / "compute-images.json").exists()


@pytest.mark.parametrize("target", ["https://evil.example.org/asset", "https://127.0.0.1/asset",
                                    "https://user:secret@release-assets.githubusercontent.com/github-production-release-asset/a",
                                    "http://release-assets.githubusercontent.com/github-production-release-asset/a",
                                    "https://release-assets.githubusercontent.com/other-path/a", "repeat"])
def test_public_source_redirects_are_narrowly_scoped_and_bounded(reviewed, monkeypatch, target):
    args, audit, _, _, _ = reviewed
    audit["source_bundle_url"] = "https://github.com/public/sources/releases/download/v1/source.tar"
    from httpx import _client
    def response(request):
        if request.url.host == "github.com":
            location = ("https://release-assets.githubusercontent.com/github-production-release-asset/a?transient=x"
                        if target == "repeat" else target)
            return httpx.Response(302, headers={"location": location, "set-cookie": "private=must-not-forward"})
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(302, headers={"location": str(request.url)})
    monkeypatch.setattr(publication.httpx, "Client", lambda **kwargs: _client.Client(**kwargs,
                      transport=httpx.MockTransport(response)))
    with pytest.raises(ValueError, match="redirect"):
        publication.fetch_source(audit)


def test_public_github_source_delivery_accepts_only_anonymous_release_cdn_redirect(reviewed, monkeypatch):
    args, audit, _, _, _ = reviewed
    audit["source_bundle_url"] = "https://github.com/public/sources/releases/download/v1/source.tar"
    from httpx import _client
    requests = []
    def response(request):
        assert "cookie" not in request.headers and "authorization" not in request.headers
        requests.append(request)
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location":
                "https://release-assets.githubusercontent.com/github-production-release-asset/a?transient=x",
                "set-cookie": "private=must-not-forward"})
        return httpx.Response(200, content=args[4].read_bytes())
    monkeypatch.setattr(publication.httpx, "Client", lambda **kwargs: _client.Client(**kwargs,
                      transport=httpx.MockTransport(response)))
    publication.fetch_source(audit)
    assert len(requests) == 2
    audit["source_bundle_url"] = "https://other.example.org/source.tar"
    monkeypatch.setattr(publication.httpx, "Client", lambda **kwargs: _client.Client(**kwargs,
                      transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location":
                        "https://release-assets.githubusercontent.com/github-production-release-asset/a?transient=x"}))))
    with pytest.raises(ValueError, match="redirect"):
        publication.fetch_source(audit)


def test_source_delivery_has_a_total_transfer_deadline(reviewed, monkeypatch):
    _, audit, _, _, _ = reviewed
    times = iter([0, 1801])
    monkeypatch.setattr(publication, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(ValueError, match="30 minute"):
        publication.fetch_source(audit)


@pytest.mark.parametrize("case", ["private", "draft", "prerelease", "cancel", "upload-fail", "existing", "success"])
def test_source_publication_is_exact_explicit_and_never_changes_repository_visibility(reviewed, monkeypatch, tmp_path, case):
    args, _, _, _, _ = reviewed
    sha, _ = publication.hash_file(args[4])
    calls = []
    def gh(arguments):
        calls.append(arguments)
        if arguments[0] == "api":
            return {"visibility": "private" if case == "private" else "public", "private": case == "private"}
        return {"isDraft": case == "draft", "isPrerelease": case == "prerelease",
                "assets": [{"name": f"compute-sources-{sha}.tar"}] if case == "existing" else []}
    def upload(arguments, **kwargs):
        calls.append(arguments)
        assert arguments[:3] == ["gh", "release", "upload"]
        assert "--clobber" not in arguments
        return SimpleNamespace(returncode=1 if case == "upload-fail" else 0)
    monkeypatch.setattr(review, "gh_json", gh)
    monkeypatch.setattr(review.subprocess, "run", upload)
    state = tmp_path / "source-publish"
    if case in {"existing", "success"}:
        result = review.publish_source(args[4], "public/sources", "v1", state, ask=approve)
        assert result["source_bundle_url"].endswith(f"/compute-sources-{sha}.tar")
        assert (state / "source-upload-complete.json").exists()
    else:
        with pytest.raises(ValueError):
            review.publish_source(args[4], "public/sources", "v1", state,
                                  ask=(lambda _: "") if case == "cancel" else approve)
        assert not (state / "source-upload-complete.json").exists()
    assert not any("create" in argument or "edit" in argument for argument in calls)
    if case in {"private", "draft", "prerelease", "cancel", "existing"}:
        assert not any(argument[:3] == ["gh", "release", "upload"] for argument in calls)
