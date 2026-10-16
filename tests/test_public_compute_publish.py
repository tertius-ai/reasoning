"""Offline guided publication contracts; no real keys, uploads, image push or hosted dispatch.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import io
import json
import os
import stat
import sys
import types
import zipfile
from pathlib import Path

import pytest
from test_public_compute_hosted import completed as completed_fixture
from test_public_compute_images import REVISION, ROOT, SCHEMA, write_oci
from test_public_compute_provenance import candidate as provenance_candidate_fixture
from test_public_compute_publication import execution_receipt

from reasoning_ledger import compute_publish as wizard
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import locked_directory, read_document, write_document, write_private
from reasoning_ledger.image_release import inspect_image
from reasoning_ledger.server.compute_catalog import canonical


@pytest.fixture
def completed(tmp_path, monkeypatch):
    return completed_fixture.__wrapped__(tmp_path, monkeypatch)


def candidate_run(revision=REVISION):
    return {"id": 101, "head_sha": revision, "status": "completed", "conclusion": "success",
            "event": "workflow_dispatch", "path": ".github/workflows/compute-images.yml",
            "repository": {"id": 123, "full_name": "research/bench"}, "head_repository": {"id": 123}}


def hosted_run(record, *, identity=202, conclusion="success"):
    return {"id": identity, "head_sha": record["head"], "head_branch": record["branch"], "event": "workflow_dispatch",
            "path": ".github/workflows/" + record["workflow"], "status": "completed", "conclusion": conclusion,
            "repository": {"id": 123, "full_name": "research/bench"}, "head_repository": {"id": 123}}


def prompt(value):
    if "Type '" in value:
        return value.split("Type '")[1].split("'", 1)[0]
    return ""


class FakeServices:
    """Protocol fixture, never real cryptographic approval or execution evidence."""
    def __init__(self):
        self.calls, self.dispatches, self.records = [], [], {}

    def repository(self, root):
        return "research/bench"

    def git(self, root, arguments):
        return "a" * 40 if arguments == ["rev-parse", "HEAD"] else ""

    def checkout(self, root, revision, path):
        path.mkdir(mode=0o700, exist_ok=True)
        self.calls.append("checkout")
        return path

    def recipe(self, inputs, path):
        path.mkdir(mode=0o700, exist_ok=True)
        self.calls.append("recipe")
        return path

    def candidate(self, root, recipe, repository, handoff, profile, inputs, state):
        self.calls.append("candidate")

    def recipe_ci(self, repository, revision, profile, recipe):
        self.calls.append("recipe_ci")

    def observe(self, recipe, profile, inputs):
        self.calls.append("observe")
        sha, size = wizard.publication.hash_file(inputs["bundle"])
        return {"source_bundle_sha256": sha, "source_bundle_bytes": size}

    def source(self, bundle, source_state, expected, *, ask, password):
        self.calls.append("source")
        assert wizard.publication.hash_file(bundle) == (expected["source_bundle_sha256"], expected["source_bundle_bytes"])
        return {"source_bundle_url": "https://sources.example.org/fixture.tar"}

    def prepare(self, recipe, profile, inputs, source, state):
        self.calls.append("prepare")
        state.mkdir(mode=0o700, exist_ok=True)
        audit = {"format": 2, "profile": profile, "source_revision": REVISION, "human_review_verified": False,
                 "legal_audit_verified": False, "complete_dependency_closure_verified": False}
        path = state / "source-review.json"
        if not path.exists():
            write_private(path, canonical(audit).encode())
        return audit

    def sign(self, state, key_path, trusted_key, *, ask, password):
        self.calls.append("sign")
        assert password("Hidden signing-key fixture prompt: ") == "fixture-secret-not-real"
        if not trusted_key.exists():
            write_private(trusted_key, b"public fixture key; not real signing evidence")
        return json.loads((state / "source-review.json").read_bytes())

    def stage(self, root, tooling_revision, profiles, trust, state):
        self.calls.append("stage")
        return "e" * 40

    def push(self, root, repository, revision, branch):
        self.calls.append("push")

    def runs(self, repository, workflow, branch):
        values = [value for value in self.records.values() if value["head_branch"] == branch]
        return {"total_count": len(values), "workflow_runs": values}

    def dispatch(self, repository, workflow, branch, inputs):
        self.dispatches.append((workflow, inputs))
        identity = 200 + len(self.records)
        self.records[identity] = hosted_run({"head": "e" * 40, "branch": branch, "workflow": workflow}, identity=identity)

    def run(self, repository, identity):
        return self.records[identity]

    def billing_blocked(self, repository, identity):
        return False

    def catalog(self, repository, identity, revision, runs, audits, trust, state):
        self.calls.append("catalog")
        path = state / "fixture-catalog.json"
        if not path.exists():
            write_private(path, b"[]")
        return path


def handoff(tmp_path):
    directory = tmp_path / "hints"
    directory.mkdir(mode=0o700)
    for name in ("image.oci.tar", "sources.tar", "source-draft.json"):
        (directory / name).write_bytes(b"fixture input, not an actual image or source bundle")
    path = directory / "handoff.json"
    path.write_text(canonical({"source_revision": REVISION, "candidate_workflow_run": 101, "profiles": {
        "research-cpu": {"archive": "image.oci.tar", "source_bundle": "sources.tar", "source_draft": "source-draft.json"}}}))
    return path


def test_one_guided_protocol_never_invents_human_assurance_or_leaks_password(tmp_path, capsys):
    service = FakeServices()
    state = tmp_path / "private"
    result = wizard.run(tmp_path, state, handoff_path=handoff(tmp_path), ask=prompt,
                        password=lambda message: "fixture-secret-not-real", services=service)
    assert result["phase"] == "catalog_verified" and result["cloud_execution_verified"] is False
    assert (service.calls.index("candidate") < service.calls.index("recipe_ci") < service.calls.index("observe")
            < service.calls.index("source")
            < service.calls.index("prepare") < service.calls.index("sign") < service.calls.index("push"))
    assert [item[0] for item in service.dispatches] == ["compute-publish.yml", "compute-combine.yml"]
    assert service.dispatches[0][1]["candidate_run_id"] == "101"
    assert json.loads(service.dispatches[1][1]["publications"]) == {"research-cpu": 200}
    document = read_document(state / "operation.json")
    assert document["phase"] == "catalog_verified"
    assert (state / "operation.json").stat().st_mode & 0o077 == 0
    audit = json.loads((state / "research-cpu/source-review.json").read_bytes())
    assert all(audit[field] is False for field in ("human_review_verified", "legal_audit_verified", "complete_dependency_closure_verified"))
    assert "fixture-secret-not-real" not in capsys.readouterr().out
    assert all("fixture-secret-not-real" not in path.read_text() for path in state.rglob("*.json"))


def test_hosted_consent_refusal_makes_no_branch_push_or_dispatch(tmp_path):
    service = FakeServices()
    with pytest.raises(ValueError, match="paused without approval"):
        wizard.run(tmp_path, tmp_path / "private", handoff_path=handoff(tmp_path), ask=lambda message: "",
                   password=lambda message: "fixture-secret-not-real", services=service)
    assert "sign" in service.calls and "push" not in service.calls and not service.dispatches


def test_private_state_rejects_group_writable_parent_without_changing_it(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o775)
    parent.chmod(0o775)
    with pytest.raises(InstallError, match="parents must be protected"):
        wizard.run(tmp_path, parent / "private", handoff_path=handoff(tmp_path), services=FakeServices())
    assert stat.S_IMODE(parent.stat().st_mode) == 0o775 and not (parent / "private").exists()


@pytest.mark.parametrize("name", ["../outside", "/outside", "a/../../outside", "a\\outside"])
def test_handoff_cannot_select_files_outside_its_directory(tmp_path, name):
    with pytest.raises(ValueError, match="stay inside"):
        wizard.retained_path(tmp_path, name)


def test_discovery_hints_are_untrusted_bounded_and_symlink_refusing(tmp_path):
    path = handoff(tmp_path)
    path.parent.chmod(0o775)
    assert wizard.hint(path)["candidate_workflow_run"] == 101
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlinks"):
        wizard.hint(link)
    with pytest.raises(ValueError, match="bounded owned"):
        wizard.hint(path, maximum=8)


@pytest.mark.parametrize("filename, mode", [("../escape", 0), ("/escape", 0), ("safe.json", stat.S_IFLNK | 0o777),
                                           ("unexpected-key.pem", 0)])
def test_download_extraction_refuses_paths_links_and_unexpected_files(tmp_path, filename, mode):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        entry = zipfile.ZipInfo(filename)
        entry.external_attr = mode << 16
        archive.writestr(entry, b"not approved evidence")
    with pytest.raises(ValueError, match="unsafe|unexpected"):
        wizard.unpack(data.getvalue(), tmp_path, {"safe.json"})
    assert not (tmp_path / "unexpected-key.pem").exists()


def test_unknown_dispatch_receipt_resumes_by_discovery_not_duplicate_dispatch(tmp_path):
    service = FakeServices()
    record = {"workflow": "compute-publish.yml", "branch": "reasoning-publication/fixture/research-cpu", "head": "e" * 40}
    operation = {"runs": {}}
    with locked_directory(tmp_path / "private") as state:
        write_document(state / "operation.json", operation, new=True)
        original = service.dispatch

        def lost(*args):
            original(*args)
            raise ValueError("fixture dispatch receipt lost")

        service.dispatch = lost
        with pytest.raises(ValueError, match="receipt lost"):
            wizard.hosted(service, "research/bench", record["workflow"], record["branch"], record["head"], {},
                          state, operation, "research-cpu", wait_seconds=0)
        operation = read_document(state / "operation.json")
        assert operation["runs"]["research-cpu"]["dispatch_requested"]
        identity = wizard.hosted(service, "research/bench", record["workflow"], record["branch"], record["head"], {},
                                 state, operation, "research-cpu", wait_seconds=0)
        assert identity == 200 and len(service.dispatches) == 1


def test_ambiguous_existing_runs_do_not_adopt_or_dispatch(tmp_path):
    service = FakeServices()
    branch = "reasoning-publication/fixture/research-cpu"
    record = {"workflow": "compute-publish.yml", "branch": branch, "head": "e" * 40}
    service.records = {number: hosted_run(record, identity=number) for number in (200, 201)}
    with locked_directory(tmp_path / "private") as state:
        operation = {"runs": {}}
        write_document(state / "operation.json", operation, new=True)
        with pytest.raises(ValueError, match="multiple dispatches"):
            wizard.hosted(service, "research/bench", record["workflow"], branch, record["head"], {}, state,
                          operation, "research-cpu", wait_seconds=0)
    assert not service.dispatches


@pytest.mark.parametrize("field, value", [("head_sha", "f" * 40), ("event", "pull_request"),
                                          ("path", ".github/workflows/other.yml"), ("head_branch", "main")])
def test_hosted_receipts_must_match_isolated_exact_tooling_dispatch(field, value):
    record = {"workflow": "compute-publish.yml", "branch": "isolated", "head": "e" * 40}
    run = hosted_run(record)
    run[field] = value
    with pytest.raises(ValueError, match="does not match"):
        wizard.validate_hosted(run, record["workflow"], record["branch"], record["head"])


def test_failed_hosted_verification_is_pending_not_a_completed_catalog(tmp_path):
    service = FakeServices()
    record = {"workflow": "compute-publish.yml", "branch": "isolated", "head": "e" * 40}
    service.records = {200: hosted_run(record, identity=200, conclusion="failure")}
    with locked_directory(tmp_path / "private") as state:
        operation = {"runs": {}}
        write_document(state / "operation.json", operation, new=True)
        with pytest.raises(wizard.Pending, match="hosted run failed"):
            wizard.hosted(service, "research/bench", record["workflow"], record["branch"], record["head"], {}, state,
                          operation, "research-cpu", wait_seconds=0, ask=lambda message: "n")
        assert not operation["runs"]["research-cpu"].get("completed")


def test_main_service_calls_exact_machine_provenance_api_without_fabricated_approvals(tmp_path, monkeypatch):
    module = types.ModuleType("reasoning_ledger.image_provenance")
    calls = []
    module.prepare = lambda *args: calls.append(("prepare", args)) or {"format": 2}
    module.observe = lambda *args: calls.append(("observe", args)) or {"source_bundle_sha256": "a" * 64}
    module.sign = lambda *args, **kwargs: calls.append(("sign", args, kwargs)) or {"format": 2}
    monkeypatch.setitem(sys.modules, module.__name__, module)
    service = wizard.Services()
    inputs = {"archive": tmp_path / "image", "bundle": tmp_path / "bundle", "draft": tmp_path / "draft", "repository": "ghcr.io/r/p/x"}
    result = service.prepare(tmp_path, "research-cpu", inputs,
                              {"source_bundle_url": "https://sources.example.org/file"}, tmp_path / "state")
    assert result == {
        "format": 2}
    assert calls[0][1] == (tmp_path, inputs["archive"], "research-cpu", inputs["repository"], inputs["bundle"],
                           "https://sources.example.org/file", inputs["draft"], tmp_path / "state")
    def secret(message):
        return "protected fixture passphrase"
    service.sign(tmp_path / "state", tmp_path / "key", tmp_path / "public", ask=prompt, password=secret)
    assert calls[1][1] == (tmp_path / "state", tmp_path / "key", tmp_path / "public")
    assert calls[1][2] == {"ask": prompt, "password": secret}
    service.observe(tmp_path, "research-cpu", inputs)
    assert calls[2][1] == (tmp_path, inputs["archive"], "research-cpu", inputs["repository"], inputs["bundle"], inputs["draft"])


def test_sources_changing_after_read_only_preflight_fail_before_upload(tmp_path, monkeypatch):
    bundle = tmp_path / "sources.tar"
    bundle.write_bytes(b"untrusted changed bytes")
    called = []
    monkeypatch.setattr("reasoning_ledger.image_spaces.publish_source", lambda **options: called.append(options))
    with pytest.raises(ValueError, match="changed after provenance preflight"):
        wizard.Services().source(bundle, tmp_path / "private", {"source_bundle_sha256": "a" * 64, "source_bundle_bytes": 1},
                                 ask=prompt, password=lambda message: "never requested")
    assert called == []


def test_candidate_rejects_prepublic_noether_before_source_upload_or_signing(tmp_path, monkeypatch):
    def old_source(sbom):
        source = next(package for package in sbom["packages"] if package["name"] == "noether-source")
        source["versionInfo"] = "ca8fc50f3d9a902a827fbd558dba138ad7bf0f8f"
    archive, _, _ = write_oci(tmp_path, change_inventory=old_source)
    service = wizard.Services()
    monkeypatch.setattr(service, "gh", lambda arguments: candidate_run())
    monkeypatch.setattr(service, "artifact", lambda *arguments: pytest.fail("unapproved source must fail before further CI reads"))
    with locked_directory(tmp_path / "private") as state, pytest.raises(ValueError, match="approved public Noether release; rebuild"):
        service.candidate(ROOT, ROOT, "research/bench", {"source_revision": REVISION, "candidate_workflow_run": 101},
                          "research-cpu", {"archive": archive, "repository": "ghcr.io/research/bench/research-cpu"}, state)


@pytest.mark.parametrize("changed", [None, "run", "manifest", "config", "sbom", "acceptance"])
def test_candidate_binds_actual_retained_oci_to_authenticated_ci_not_local_hints(tmp_path, monkeypatch, changed):
    archive, _, _ = write_oci(tmp_path)
    repository = "ghcr.io/research/bench/research-cpu"
    report, blobs = inspect_image(archive, SCHEMA, repository)
    observed = json.loads(canonical(report))
    accepted = execution_receipt(report["image"])
    run = candidate_run()
    if changed == "run":
        run["head_sha"] = "f" * 40
    elif changed == "manifest":
        observed["image"]["reference"] = repository + "@sha256:" + "f" * 64
    elif changed == "config":
        observed["config_digest"] = "sha256:" + "f" * 64
    elif changed == "acceptance":
        accepted["execution_verified"] = False

    class Service(wizard.Services):
        def gh(self, arguments):
            return run

        def artifact(self, repo, run_id, name, directory, allowed):
            assert repo == "research/bench" and run_id == 101 and name == "research-cpu-acceptance-" + REVISION
            directory.mkdir(mode=0o700)
            write_private(directory / "image-evidence.json", canonical(observed).encode())
            write_private(directory / "acceptance.json", canonical(accepted).encode())
            write_private(directory / "capabilities.json", blobs["etc/reasoning/capabilities.json"])
            write_private(directory / "sbom.spdx.json", b"changed" if changed == "sbom" else blobs["usr/share/reasoning/sbom.spdx.json"])

    with locked_directory(tmp_path / "private") as state:
        def call():
            return Service().candidate(ROOT, ROOT, "research/bench", {"source_revision": REVISION, "candidate_workflow_run": 101},
                                       "research-cpu", {"archive": archive, "repository": repository}, state)
        if changed:
            with pytest.raises(ValueError):
                call()
        else:
            assert call() == report["image"]


def test_cli_help_and_make_target_do_not_build_or_touch_release_bundle():
    root = Path(__file__).resolve().parents[1]
    makefile = (root / "Makefile").read_text()
    target = makefile.split("compute-publish: check-uv", 1)[1].split("compute-source-publish:", 1)[0]
    assert "reasoning_ledger.compute_publish" in target
    assert all(word not in target for word in ("build-release", "docker", "--yes", "signature_verified=true"))


@pytest.mark.parametrize("drift", [None, "anonymous", "tooling", "request", "signature", "acceptance", "catalog"])
def test_actual_catalog_consumer_verifies_real_signed_fixture_and_hosted_receipt(completed, tmp_path, drift):
    """Use real publication/combine validators; registry/worker effects remain synthetic."""
    from reasoning_ledger import image_hosted

    args, report, env, _, github, _ = completed
    combined = tmp_path / "combined"
    revision = report["image"]["source_revision"]
    image_hosted.hosted_combine(revision, {"research-cpu": 42}, args[7], combined,
                               github=github, environment=env)
    audit = wizard.publication.load_audit(args[5], args[6], args[7])[0]
    if drift in ("anonymous", "tooling", "catalog"):
        path = combined / "combine.json"
        value = json.loads(path.read_bytes())
        value[{"anonymous": "anonymous_reverification", "tooling": "tooling_revision", "catalog": "catalog_sha256"}[drift]] = (
            False if drift == "anonymous" else "f" * (40 if drift == "tooling" else 64))
        path.write_text(canonical(value))
    elif drift == "request":
        path = combined / "published/research-cpu/workflow-request.json"
        value = json.loads(path.read_bytes()) | {"candidate_run_id": 18}
        path.write_text(canonical(value))
    elif drift == "signature":
        (combined / "published/research-cpu/source-review.ed25519").write_bytes(b"x" * 64)
    elif drift == "acceptance":
        (combined / "published/research-cpu/public-acceptance.json").write_text("{}")

    class Service(wizard.Services):
        def artifact(self, repository, run, name, directory, allowed):
            assert (repository, run, name) == ("research/bench", 42, "approved-compute-catalog-" + revision)
            found = set()
            for path in combined.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(combined).as_posix()
                    if relative == "provision.lock":
                        continue  # Hosted workflow uploads public allowlist, not its private lock.
                    assert relative in allowed
                    target = directory / relative
                    wizard.private_parents(directory, Path(relative).parent)
                    write_private(target, path.read_bytes())
                    found.add(relative)
            return found

    with locked_directory(tmp_path / "private") as state:
        trust = state / "review-key.pem"
        write_private(trust, args[7].read_bytes())
        write_document(state / "operation.json", {"public_head": env["GITHUB_SHA"], "candidate_run_id": 17,
            "runs": {"research-cpu": {"branch": "publication-fixture"}}}, new=True)
        def consume():
            return Service().catalog("research/bench", 42, revision, {"research-cpu": 42},
                                     {"research-cpu": audit}, trust, state)
        if drift:
            with pytest.raises(ValueError):
                consume()
        else:
            assert consume().read_bytes() == (combined / "compute-images.json").read_bytes()


def test_isolated_git_stage_commits_only_public_attestations_and_reuses_exact_commit(tmp_path, monkeypatch):
    service = wizard.Services()
    root = tmp_path / "repo"
    root.mkdir(mode=0o700)
    service.git(root, ["init", "-q"])
    (root / "LICENSE").write_text("fixture Apache license marker")
    service.git(root, ["add", "LICENSE"])
    service.git(root, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"])
    revision = service.git(root, ["rev-parse", "HEAD"])
    original_stage = service.stage
    def stage(*args):
        old = os.umask(0o002)
        try:
            return original_stage(*args)
        finally:
            os.umask(old)
    monkeypatch.setattr(service, "stage", stage)
    with locked_directory(tmp_path / "private") as state:
        trust = state / "review-key.pem"
        write_private(trust, b"public synthetic key")
        write_private(state / "signing-key.pem", b"PRIVATE-FIXTURE-DO-NOT-COMMIT")
        profile = state / "research-cpu"
        profile.mkdir(mode=0o700)
        write_private(profile / "source-review.json", b"{}")
        write_private(profile / "source-review.ed25519", b"synthetic signature")
        audits = {"research-cpu": {"source_revision": "a" * 40}}
        head = service.stage(root, revision, audits, trust, state)
        assert service.stage(root, revision, audits, trust, state) == head
        changed = service.git(state / "tooling", ["diff", "--name-only", revision, head]).splitlines()
        assert len(changed) == 3 and all(path.startswith("images/publication/") for path in changed)
        assert "signing-key" not in service.git(state / "tooling", ["ls-tree", "-r", "--name-only", "HEAD"])
        (state / "tooling" / "unexpected.txt").write_text("do not include")
        with pytest.raises(ValueError, match="only committed exact"):
            service.stage(root, revision, audits, trust, state)


def test_command_bounds_output_and_never_reports_credential_bearing_stderr():
    with pytest.raises(ValueError, match="output suppressed") as failure:
        wizard.command([sys.executable, "-c", "import sys;sys.stderr.write('private-fixture-secret');sys.exit(1)"])
    assert "private-fixture-secret" not in str(failure.value)
    with pytest.raises(ValueError, match="allowance"):
        wizard.command([sys.executable, "-c", "print('x'*100)"], maximum=8)


def test_nested_artifact_evidence_is_private_even_with_shared_user_umask(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("published/research-cpu/receipt.json", b"{}")
    with locked_directory(tmp_path / "private") as state:
        old = os.umask(0o002)
        try:
            wizard.unpack(data.getvalue(), state / "evidence", {"published/research-cpu/receipt.json"})
        finally:
            os.umask(old)
        assert all(not path.stat().st_mode & 0o077 for path in (state / "evidence").rglob("*"))


def test_actual_provenance_caller_extracts_observes_prepares_and_signs_synthetic_candidate(tmp_path, monkeypatch):
    args, draft, state, *_ = provenance_candidate_fixture.__wrapped__(tmp_path, monkeypatch)
    inputs = {"archive": args[1], "bundle": args[4], "draft": draft, "repository": args[3]}
    service = wizard.Services()
    recipe = service.recipe(inputs, tmp_path / "archive-recipe")
    assert service.recipe(inputs, recipe) == recipe
    observed = service.observe(recipe, args[2], inputs)
    assert observed["source_bundle_sha256"] == wizard.publication.hash_file(inputs["bundle"])[0]
    assert not state.exists()
    audit = service.prepare(recipe, args[2], inputs, {"source_bundle_url": "https://sources.example.org/fixture.tar"}, state)
    signed = service.sign(state, state / "signing-key.pem", state / "review-key.pem", ask=prompt,
                          password=lambda message: "synthetic-test-passphrase")
    assert signed == audit and signed["format"] == 2 and signed["human_review_verified"] is False
    assert (state / "signing-key.pem").read_bytes().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY-----")


@pytest.mark.parametrize("drift", ["fork", "repository"])
def test_hosted_matching_refuses_forks_and_wrong_repository(drift):
    record = {"workflow": "compute-publish.yml", "branch": "isolated", "head": "e" * 40}
    run = hosted_run(record)
    if drift == "fork":
        run["head_repository"]["id"] = 456
    else:
        run["repository"]["full_name"] = "another/repository"
    with pytest.raises(ValueError, match="repository, not a fork"):
        wizard.validate_hosted(run, record["workflow"], record["branch"], record["head"], "research/bench")


@pytest.mark.parametrize("drift", [None, "commit", "truncated", "blob", "missing", "link"])
def test_recipe_archive_is_independently_bound_to_authenticated_candidate_git_tree(tmp_path, drift):
    recipe = tmp_path / "recipe"
    recipe.mkdir(mode=0o700)
    write_private(recipe / "LICENSE", b"exact original fixture license")
    sha = hashlib.sha1(b"blob 30\0exact original fixture license", usedforsecurity=False).hexdigest()
    commit = {"sha": REVISION, "tree": {"sha": "b" * 40}}
    tree = {"sha": "b" * 40, "truncated": False,
            "tree": [{"path": "LICENSE", "type": "blob", "mode": "100644", "sha": sha}]}
    if drift == "commit":
        commit["sha"] = "f" * 40
    elif drift == "truncated":
        tree["truncated"] = True
    elif drift == "blob":
        tree["tree"][0]["sha"] = "f" * 40
    elif drift == "missing":
        tree["tree"].append({"path": "NOTICE", "type": "blob", "mode": "100644", "sha": "c" * 40})
    elif drift == "link":
        tree["tree"][0]["mode"] = "120000"

    class Service(wizard.Services):
        def gh(self, arguments):
            if arguments[1].endswith("/commits/" + REVISION):
                return commit
            assert arguments[1].endswith("/trees/" + "b" * 40 + "?recursive=1")
            return tree

    if drift:
        with pytest.raises(ValueError):
            Service().recipe_ci("research/bench", REVISION, "research-cpu", recipe)
    else:
        Service().recipe_ci("research/bench", REVISION, "research-cpu", recipe)


def test_billing_refusal_preserves_exact_failed_run_and_does_not_retry_or_offer_wrong_diagnosis(tmp_path):
    service = FakeServices()
    record = {"workflow": "compute-publish.yml", "branch": "isolated", "head": "e" * 40}
    service.records = {200: hosted_run(record, identity=200, conclusion="failure")}
    service.billing_blocked = lambda repo, identity: repo == "research/bench" and identity == 200
    with locked_directory(tmp_path / "private") as state:
        operation = {"runs": {}}
        write_document(state / "operation.json", operation, new=True)
        with pytest.raises(wizard.Pending, match="account payments failed"):
            wizard.hosted(service, "research/bench", record["workflow"], record["branch"], record["head"], {}, state,
                          operation, "research-cpu", wait_seconds=0,
                          ask=lambda message: pytest.fail("must not offer a GHCR visibility diagnosis for a billing refusal"))
        assert operation["runs"]["research-cpu"]["billing_blocked"] is True
        assert operation["runs"]["research-cpu"]["run_id"] == 200 and not service.dispatches


@pytest.mark.parametrize("kind", ["billing", "different", "steps", "foreign"])
def test_billing_diagnosis_requires_exact_unstarted_job_annotation(kind):
    class Service(wizard.Services):
        def gh(self, arguments):
            if "/jobs?" in arguments[1]:
                return {"total_count": 1, "jobs": [{"steps": [{"name": "actual job ran"}] if kind == "steps" else [],
                    "conclusion": "failure", "check_run_url": "https://api.github.com/repos/" + (
                        "other/repo" if kind == "foreign" else "research/bench") + "/check-runs/42"}]}
            return [{"message": wizard.BILLING_REFUSAL if kind == "billing" else "network error"}]
    assert Service().billing_blocked("research/bench", 42) is (kind == "billing")
