"""Source-collection boundaries; synthetic fixtures never assert a legal source audit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import bz2
import gzip
import hashlib
import io
import json
import lzma
import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from reasoning_ledger import image_sources as sources


def archive_bytes(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return stream.getvalue()


def component(name="libc6", version="2.35", kind="deb", source="glibc", source_version="2.35-1"):
    return {
        "SPDXID": "SPDXRef-" + name,
        "name": name,
        "versionInfo": version,
        "comment": json.dumps({"kind": kind, "name": name, "version": version, "source": source, "source_version": source_version}),
    }


def test_inventory_preserves_exact_os_source_versions_and_does_not_assert_licenses():
    result = sources.inventory({"packages": [component()]})
    assert result == [
        {"id": "SPDXRef-libc6", "name": "libc6", "version": "2.35", "kind": "deb", "source": "glibc", "source_version": "2.35-1"}
    ]
    with pytest.raises(ValueError, match="duplicated"):
        sources.inventory({"packages": [component(), component()]})


def test_local_noether_crate_uses_repository_source_not_a_fabricated_registry_archive(tmp_path):
    item = sources.inventory({"packages": [component("noether", "0.3.1", "noether-rust")]})[0]
    artifacts = [{"id": "noether", "path": "noether-source.tar"}]
    (tmp_path / "noether-source.tar").write_bytes(
        archive_bytes(
            {
                "Cargo.lock": b'version=3\n[[package]]\nname="noether"\nversion="0.3.1"\n',
                "Cargo.toml": b'[workspace.package]\nversion="0.3.1"\n[package]\nname="noether"\nversion.workspace=true\n',
            }
        )
    )
    assert sources.candidates(item, artifacts, tmp_path, {}) == ["noether-source.tar"]
    assert sources.candidates(item, [], tmp_path, {}) == []
    dependency = {**item, "name": "rayon", "version": "1.11.0"}
    assert sources.candidates(dependency, artifacts, tmp_path, {}) == []
    (tmp_path / "cargo").mkdir()
    (tmp_path / "cargo/rayon-1.11.0.crate").write_bytes(b"synthetic registry source")
    assert sources.candidates(dependency, artifacts, tmp_path, {}) == ["cargo/rayon-1.11.0.crate"]


@pytest.mark.parametrize("manifest", [None, b'[package]\nname="module-core"\nversion="0.3.2"\n'])
def test_local_workspace_mapping_requires_exact_version_and_retained_manifest(tmp_path, manifest):
    entries = {"Cargo.lock": b'version=3\n[[package]]\nname="module-core"\nversion="0.3.1"\n'}
    if manifest is not None:
        entries["crates/module-core/Cargo.toml"] = manifest
    (tmp_path / "noether-source.tar").write_bytes(archive_bytes(entries))
    item = sources.inventory({"packages": [component("module-core", "0.3.1", "noether-rust")]})[0]
    assert sources.candidates(item, [{"id": "noether", "path": "noether-source.tar"}], tmp_path, {}) == []


def test_local_workspace_subcrate_mapping_does_not_claim_registry_or_other_version(tmp_path):
    (tmp_path / "noether-source.tar").write_bytes(
        archive_bytes(
            {
                "Cargo.lock": b'version=3\n[[package]]\nname="module-core"\nversion="0.3.1"\n'
                b'[[package]]\nname="registry-core"\nversion="0.3.1"\nsource="registry+https://github.com/rust-lang/crates.io-index"\n',
                "Cargo.toml": b'[workspace.package]\nversion="0.3.1"\n',
                "crates/module-core/Cargo.toml": b'[package]\nname="module-core"\nversion.workspace=true\n',
                "crates/registry-core/Cargo.toml": b'[package]\nname="registry-core"\nversion="0.3.1"\n',
            }
        )
    )
    artifacts = [{"id": "noether", "path": "noether-source.tar"}]
    item = sources.inventory({"packages": [component("module-core", "0.3.1", "noether-rust")]})[0]
    assert sources.candidates(item, artifacts, tmp_path, {}) == ["noether-source.tar"]
    assert sources.candidates({**item, "version": "0.3.2"}, artifacts, tmp_path, {}) == []
    assert sources.candidates({**item, "name": "registry-core"}, artifacts, tmp_path, {}) == []


def test_locked_download_redirects_without_credentials_and_verifies_bytes(tmp_path):
    content, seen = b"exact upstream source fixture", []

    def handle(request):
        seen.append(request)
        assert not request.headers.get("authorization") and not request.headers.get("cookie")
        if request.url.host == "source.example.org":
            return httpx.Response(302, headers={"location": "https://assets.example.org/source.tar?public-signature=yes"})
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = sources.download(
            "https://source.example.org/source.tar", tmp_path / "source.tar", hashlib.sha256(content).hexdigest(), client=client
        )
    assert len(seen) == 2 and result["bytes"] == len(content)
    assert (tmp_path / "source.tar").read_bytes() == content


@pytest.mark.parametrize(
    "location", ["http://assets.example.org/source", "https://127.0.0.1/source", "https://user:secret@assets.example.org/source"]
)
def test_unsafe_redirect_has_no_followup_request_or_source_file(tmp_path, location):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(ValueError):
        sources.download("https://source.example.org/source", tmp_path / "source", "a" * 64, client=client)
    assert len(seen) == 1 and not (tmp_path / "source").exists()


def test_hash_mismatch_retains_unverified_artifact_but_has_no_completion(tmp_path):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong"))) as client,
        pytest.raises(ValueError, match="differs"),
    ):
        sources.download("https://source.example.org/source", tmp_path / "source", "a" * 64, client=client)
    assert (tmp_path / "source").read_bytes() == b"wrong"


def test_notices_are_copied_by_safe_identity_without_extracting_upstream_paths(tmp_path):
    (tmp_path / "upstream").mkdir()
    (tmp_path / "upstream" / "sources.tar.gz").write_bytes(archive_bytes({"../../LICENSE": b"upstream license", "a/code.py": b"code"}))
    result = sources.notices(tmp_path)
    assert len(result) == 1 and result[0]["member"] == "../../LICENSE"
    assert (tmp_path / result[0]["path"]).read_bytes() == b"upstream license"
    assert len(list((tmp_path / "notices").iterdir())) == 2


def test_os_source_archive_notice_is_materialized_without_source_extraction(tmp_path):
    (tmp_path / "upstream").mkdir()
    directory = tmp_path / "apt" / "20250215T000000Z" / "source"
    directory.mkdir(parents=True)
    (directory / "package.debian.tar.xz").write_bytes(archive_bytes({"debian/copyright": b"synthetic OS copyright notice"}))
    records = sources.notices(tmp_path)
    assert len(records) == 1 and records[0]["archive"] == "apt/20250215T000000Z/source/package.debian.tar.xz"
    assert (tmp_path / records[0]["path"]).read_bytes() == b"synthetic OS copyright notice"
    assert not (tmp_path / "debian").exists()


@pytest.mark.parametrize("suffix,compress", [("gz", gzip.compress), ("bz2", bz2.compress), ("xz", lzma.compress)])
def test_os_source_signature_sidecars_are_retained_but_not_scanned_as_tar(tmp_path, suffix, compress):
    (tmp_path / "upstream").mkdir()
    directory = tmp_path / "apt/20250405T000000Z/fixture"
    directory.mkdir(parents=True)
    source = directory / ("package.orig.tar." + suffix)
    source.write_bytes(compress(gzip.decompress(archive_bytes({"COPYING": b"synthetic source notice"}))))
    signature = directory / (source.name + ".asc")
    content = b"-----BEGIN PGP SIGNATURE-----\nsynthetic detached signature\n-----END PGP SIGNATURE-----\n"
    signature.write_bytes(content)
    metadata = directory / "package.dsc"
    metadata.write_bytes(b"synthetic signed-index source metadata")
    records = sources.notices(tmp_path)
    assert len(records) == 1 and records[0]["archive"] == str(source.relative_to(tmp_path))
    assert signature.read_bytes() == content and metadata.exists(), "retain authentication evidence without treating it as a tar"
    assert (tmp_path / records[0]["path"]).read_bytes() == b"synthetic source notice"


def test_cargo_superset_uses_exact_lock_checksums_without_running_builds(tmp_path, monkeypatch):
    cargo = """version = 3
[[package]]
name = "synthetic-core"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
[[package]]
name = "local-facade"
version = "0.3.1"
"""
    archive = tmp_path / "source.tar"
    archive.write_bytes(archive_bytes({"Cargo.lock": cargo.encode()}))
    calls = []

    def download(url, path, expected):
        calls.append((url, expected))
        path.write_bytes(b"source fixture")

    monkeypatch.setattr(sources, "download", download)
    assert sources.cargo_sources(archive, tmp_path) == ["cargo/synthetic-core-1.0.0.crate"]
    assert calls == [("https://static.crates.io/crates/synthetic-core/synthetic-core-1.0.0.crate", "a" * 64)]


def test_apt_collection_uses_isolated_signed_indexes_and_records_unavailable_versions(tmp_path, monkeypatch):
    keyring = tmp_path / "keyring"
    keyring.write_bytes(b"synthetic keyring fixture")
    payload = tmp_path / "payload"
    payload.mkdir()
    calls = []

    def run(argv, *, cwd, env, capture_output, timeout):
        calls.append(argv)
        assert env == {"PATH": sources.os.defpath, "LANG": "C.UTF-8"}
        assert "Acquire::AllowInsecureRepositories=false" in argv
        assert "APT::Get::AllowUnauthenticated=false" in argv
        assert "Dir::Etc::parts=-" in argv and "Dir::Etc::main=-" in argv
        assert "Dir::Etc::Trusted=" + str(keyring) in argv
        assert "--allow-unauthenticated" not in argv
        if argv[-1] == "update":
            (cwd / "lists" / "fixture_InRelease").write_bytes(b"synthetic signed index receipt")
        elif argv[-1].startswith("missing="):
            return subprocess.CompletedProcess(argv, 100, b"", b"version unavailable")
        else:
            (cwd / "glibc_2.35-1.dsc").write_bytes(b"synthetic source metadata")
            (cwd / "glibc_2.35.orig.tar.gz").write_bytes(b"synthetic exact source bytes")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sources.subprocess, "run", run)
    components = sources.inventory({"packages": [component(), component("missing-bin", source="missing")]})
    result, unresolved = sources.apt_sources(components, payload, snapshot="20261006T000000Z", keyring=keyring)
    assert len(calls) == 3 and len(result[("glibc", "2.35-1")]) == 2
    assert unresolved[0]["source"] == "missing"
    assert (payload / "apt-evidence/20261006T000000Z/fixture_InRelease").exists()


def test_failed_source_partials_stay_private_outside_bundle_and_snapshot_can_continue(tmp_path, monkeypatch):
    keyring = tmp_path / "keyring"
    keyring.write_bytes(b"synthetic trusted keyring")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "upstream").mkdir()
    # A real truncated xz stream is unsafe to scan as a completed source tar.
    partial = lzma.compress(gzip.decompress(archive_bytes({"LICENSE": b"synthetic license"})))[:-12]
    calls = []

    def run(argv, *, cwd, env, capture_output, timeout):
        if argv[-1] == "update":
            (cwd / "lists/fixture_InRelease").write_bytes(b"synthetic authenticated index")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        calls.append(argv[-1])
        if len(calls) == 1:
            (cwd / "source.dsc").write_bytes(b"downloaded metadata alone is not complete source")
            (cwd / "source.orig.tar.xz").write_bytes(partial)
            return subprocess.CompletedProcess(argv, 100, b"", b"interrupted archive download")
        (cwd / "source.dsc").write_bytes(b"synthetic verified source metadata")
        (cwd / "source.orig.tar.xz").write_bytes(
            lzma.compress(gzip.decompress(archive_bytes({"LICENSE": b"synthetic verified source license"})))
        )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sources.subprocess, "run", run)
    components = sources.inventory({"packages": [component()]})
    first, unresolved = sources.apt_sources(components, payload, snapshot="20261006T000000Z", keyring=keyring)
    assert not first and len(unresolved) == 1
    retained = tmp_path / unresolved[0]["retained_in"]
    assert retained.is_dir() and (retained / "source.orig.tar.xz").read_bytes() == partial
    assert (retained / "apt-error.txt").read_bytes() == b"interrupted archive download"
    assert (tmp_path / "download-failures").stat().st_mode & 0o777 == 0o700
    assert not list((payload / "apt").rglob("*.tar.xz"))
    second, missing = sources.apt_sources(components, payload, snapshot="20250215T000000Z", keyring=keyring)
    assert len(second) == 1 and not missing
    records = sources.notices(payload)
    assert len(records) == 1 and records[0]["archive"] in second[("glibc", "2.35-1")]
    assert all("download-failures" not in item["archive"] for item in records)
    assert (retained / "source.orig.tar.xz").read_bytes() == partial, "the failed bytes remain recoverable"


def test_unsigned_bundle_is_deterministic_and_never_approves_unknown_closure(tmp_path, monkeypatch):
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"packages": [component()]}))
    upstream = archive_bytes({"source/LICENSE": b"synthetic upstream notice", "source/build.sh": b"do not execute"})
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "format": 1,
                "artifacts": [
                    {
                        "id": "source",
                        "profiles": ["research-cpu"],
                        "file": "source.tar.gz",
                        "url": "https://source.example.org/source.tar.gz",
                        "sha256": hashlib.sha256(upstream).hexdigest(),
                    }
                ],
            }
        )
    )
    recipe = archive_bytes({"images/research-cpu/inputs.json": b"synthetic recipe"})
    monkeypatch.setattr(sources, "command", lambda argv, cwd: ("a" * 40 + "\n").encode() if argv[1] == "rev-parse" else recipe)

    def download(url, destination, expected):
        destination.write_bytes(upstream)
        return {"sha256": expected, "bytes": len(upstream), "url": url}

    monkeypatch.setattr(sources, "download", download)
    first = sources.assemble(tmp_path, sbom, lock, tmp_path / "first", profile="research-cpu")
    second = sources.assemble(tmp_path, sbom, lock, tmp_path / "second", profile="research-cpu")
    assert first["sha256"] == second["sha256"] and not first["approved"]
    draft = json.loads((tmp_path / "first/source-review.draft.json").read_bytes())
    assert not draft["source_delivery_approved"] and not draft["dependency_closure_reviewed"]
    assert not draft["public_binary_distribution_approved"] and draft["coverage"][0]["disposition"] == "pending-review"
    with tarfile.open(first["bundle"]) as archive:
        assert set(archive.getnames()) == {item["path"] for item in draft["files"]}
        for item in draft["files"]:
            content = archive.extractfile(item["path"]).read()
            assert hashlib.sha256(content).hexdigest() == item["sha256"] and len(content) == item["bytes"]
    with pytest.raises(FileExistsError):
        sources.assemble(tmp_path, sbom, lock, tmp_path / "first", profile="research-cpu")


def test_committed_sources_match_mathlib_and_runtime_version_locks():
    root = Path(__file__).resolve().parents[1]
    locks = json.loads((root / "images/source-inputs.json").read_bytes())
    artifacts = {item["id"]: item for item in locks["artifacts"]}
    lean = json.loads((root / "images/lean-mathlib/inputs.json").read_bytes())
    assert artifacts["mathlib-source"]["sha256"] == lean["mathlib_sha256"]
    assert lean["mathlib_revision"] in artifacts["mathlib-source"]["url"]
    for name, revision in lean["dependencies"].items():
        assert revision in artifacts[name + "-source"]["url"]
    runtimes = json.loads((root / "images/research-cpu/inputs.json").read_bytes())["versions"]
    assert runtimes["sage"] in artifacts["sage-full-source"]["file"]
    assert runtimes["julia"] in artifacts["julia-full-source"]["file"]
    assert runtimes["lean"] in artifacts["lean-source"]["file"]


def test_compiler_notice_distribution_is_not_a_standard_library_source_candidate(tmp_path):
    artifacts = [
        {"id": "rust-standard-library-source", "path": "upstream/rustc-src.tar.xz"},
        {"id": "rust-compiler-notices", "path": "upstream/rustc-binary.tar.xz"},
    ]
    item = {"kind": "compiler-runtime", "name": "rust-standard-library-runtime", "version": "1.94.0"}
    item["source_candidates"] = sources.candidates(item, artifacts, tmp_path, {})
    assert item["source_candidates"] == ["upstream/rustc-src.tar.xz"]
    assert sources.notice_origins(item, artifacts) == ["upstream/rustc-src.tar.xz", "upstream/rustc-binary.tar.xz"]
    assert sources.candidates(item, artifacts[1:], tmp_path, {}) == [], "compiler binary bytes cannot stand in for actual std source"


def rust_fixture(mutation=None):
    base = "rustc-1.94.0-x86_64-unknown-linux-gnu/rustc/share/doc/rust/"
    entries = {
        base + "COPYRIGHT-library.html": b"synthetic standard library copyright",
        base + "licenses/MIT.txt": b"synthetic MIT license",
        base + "licenses/Apache-2.0.txt": b"synthetic Apache license",
        "compiler/bin/rustc": b"do not convey this synthetic executable",
        "unrelated/LICENSE": b"do not mistake this for a runtime notice",
    }
    notice_hashes = {
        name: hashlib.sha256(entries[base + member]).hexdigest()
        for name, member in {
            "COPYRIGHT-library.html": "COPYRIGHT-library.html",
            "LICENSE-MIT": "licenses/MIT.txt",
            "LICENSE-APACHE": "licenses/Apache-2.0.txt",
        }.items()
    }
    if mutation == "missing":
        del entries[base + "licenses/MIT.txt"]
    elif mutation == "wrong":
        entries[base + "licenses/MIT.txt"] = b"different notice"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in entries.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            if mutation == "linked" and name == base + "licenses/MIT.txt":
                entry.type, entry.size, entry.linkname = tarfile.SYMTYPE, 0, "../../../elsewhere"
                archive.addfile(entry)
            else:
                archive.addfile(entry, io.BytesIO(content))
                if mutation == "duplicate" and name == base + "licenses/MIT.txt":
                    archive.addfile(entry, io.BytesIO(content))
    compiler = stream.getvalue()
    source = archive_bytes({"rust/library/core/src/lib.rs": b"actual synthetic standard-library source"})
    runtime = {
        "commit": sources.NOETHER_PIN,
        "rust_version": "1.94.0",
        "rust_runtime": {
            "notices": notice_hashes,
            "compiler_distribution": {
                "url": "https://static.rust-lang.org/dist/rustc-1.94.0-x86_64-unknown-linux-gnu.tar.xz",
                "sha256": hashlib.sha256(compiler).hexdigest(),
                "bytes": len(compiler),
            },
            "source_distribution": {
                "url": "https://static.rust-lang.org/dist/rustc-1.94.0-src.tar.xz",
                "sha256": hashlib.sha256(source).hexdigest(),
                "bytes": len(source),
            },
        },
    }
    return runtime, compiler, source


def test_rust_compiler_notices_only_enter_bundle_with_verified_source_and_private_archive(tmp_path, monkeypatch):
    runtime, compiler, source = rust_fixture()
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"packages": [component("rust-standard-library-runtime", "1.94.0", "compiler-runtime")]}))
    artifacts = []
    for identity, distribution in (
        ("rust-compiler-notices", "compiler_distribution"),
        ("rust-standard-library-source", "source_distribution"),
    ):
        pin = runtime["rust_runtime"][distribution]
        artifacts.append(
            {"id": identity, "file": identity + ".tar.xz", "profiles": ["research-cpu"], "url": pin["url"], "sha256": pin["sha256"]}
        )
    lock = tmp_path / "sources.json"
    lock.write_text(json.dumps({"format": 1, "artifacts": artifacts}))
    recipe = archive_bytes({"images/research-cpu/inputs.json": json.dumps({"noether": runtime}).encode()})
    monkeypatch.setattr(sources, "command", lambda argv, cwd: ("a" * 40 + "\n").encode() if argv[1] == "rev-parse" else recipe)

    def download(url, destination, expected):
        content = source if url.endswith("-src.tar.xz") else compiler
        destination.write_bytes(content)
        return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "url": url}

    monkeypatch.setattr(sources, "download", download)
    output = tmp_path / "collected"
    receipt = sources.assemble(tmp_path, sbom, lock, output, profile="research-cpu")
    report = json.loads((output / "payload/collection.json").read_bytes())
    assert len(report["notices"]) == 3
    assert set(item["notice_name"] for item in report["notices"]) == {"COPYRIGHT-library.html", "LICENSE-MIT", "LICENSE-APACHE"}
    compiler_receipt = report["artifacts"][0]
    assert not compiler_receipt["conveyed"] and compiler_receipt["role"] == "compiler-license-input"
    assert (output / compiler_receipt["retained_in"]).read_bytes() == compiler
    assert (output / "build-materials").stat().st_mode & 0o777 == 0o700
    assert not (output / "payload" / compiler_receipt["path"]).exists()
    assert report["components"][0]["source_candidates"] == ["upstream/rust-standard-library-source.tar.xz"]
    assert len(report["components"][0]["notice_candidates"]) == 3
    with tarfile.open(receipt["bundle"]) as archive:
        assert compiler_receipt["path"] not in archive.getnames()
        assert all(not path.startswith("build-materials/") for path in archive.getnames())
    draft = json.loads((output / "source-review.draft.json").read_bytes())
    assert not draft["source_delivery_approved"] and not draft["dependency_closure_reviewed"]
    assert all(item["path"] != compiler_receipt["path"] for item in draft["files"])


@pytest.mark.parametrize("mutation", ["missing", "wrong", "duplicate", "linked"])
def test_exact_rust_notices_reject_incomplete_mismatched_or_linked_members(tmp_path, mutation):
    runtime, compiler, _ = rust_fixture(mutation)
    payload = tmp_path / "payload"
    (payload / "upstream").mkdir(parents=True)
    path = payload / "upstream/compiler.tar.xz"
    path.write_bytes(compiler)
    artifact = {**runtime["rust_runtime"]["compiler_distribution"], "path": "upstream/compiler.tar.xz"}
    with pytest.raises(ValueError, match="notice"):
        sources.rust_notices(payload, path, artifact, runtime)
    assert (tmp_path / "build-materials/compiler.tar.xz").read_bytes() == compiler
    assert not list(payload.rglob("*.txt")), "no partial notice evidence is materialized"


@pytest.mark.parametrize("field,value", [("sha256", "a" * 64), ("bytes", 1), ("url", "https://source.example.org/wrong.tar.xz")])
def test_rust_source_and_compiler_artifacts_must_match_retained_recipe_pin(field, value):
    runtime, _, _ = rust_fixture()
    pin = dict(runtime["rust_runtime"]["compiler_distribution"])
    receipt = dict(pin)
    receipt[field] = value
    with pytest.raises(ValueError, match="retained compiler pin"):
        sources.validate_rust_artifact({**pin, "id": "rust-compiler-notices"}, receipt, runtime)
    source = dict(runtime["rust_runtime"]["source_distribution"])
    source[field] = value
    with pytest.raises(ValueError, match="retained compiler pin"):
        sources.validate_rust_artifact({**source, "id": "rust-standard-library-source"}, source, runtime)


def test_multiple_signed_snapshots_only_retry_unresolved_exact_versions(tmp_path, monkeypatch):
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps({"packages": [component(), component("other", source="other")]}))
    lock = tmp_path / "lock.json"
    archive = archive_bytes({"LICENSE": b"synthetic source fixture"})
    lock.write_text(
        json.dumps(
            {
                "format": 1,
                "artifacts": [
                    {
                        "id": "source",
                        "profiles": ["research-cpu"],
                        "file": "source.tar.gz",
                        "url": "https://source.example.org/source.tar.gz",
                        "sha256": "a" * 64,
                    }
                ],
            }
        )
    )
    keyring = tmp_path / "keyring"
    keyring.write_bytes(b"synthetic keyring")
    monkeypatch.setattr(sources, "command", lambda argv, cwd: ("a" * 40 + "\n").encode() if argv[1] == "rev-parse" else archive)

    def download(url, destination, expected):
        destination.write_bytes(archive)
        return {"sha256": expected, "bytes": len(archive), "url": url}

    monkeypatch.setattr(sources, "download", download)
    calls = []

    def apt(components, payload, *, snapshot, keyring):
        calls.append((snapshot, [item["source"] for item in components]))
        name = "glibc" if len(calls) == 1 else "other"
        (payload / (name + ".dsc")).write_bytes(b"synthetic authenticated source fixture")
        return {(name, "2.35-1"): [name + ".dsc"]}, []

    monkeypatch.setattr(sources, "apt_sources", apt)
    sources.assemble(
        tmp_path,
        sbom,
        lock,
        tmp_path / "output",
        profile="research-cpu",
        snapshot=["20261006T000000Z", "20250215T000000Z", "20241215T000000Z"],
        keyring=keyring,
    )
    assert calls == [("20261006T000000Z", ["glibc", "other"]), ("20250215T000000Z", ["other"])]
    report = json.loads((tmp_path / "output/payload/collection.json").read_bytes())
    assert len(report["apt_attempts"]) == 2 and not any("source_version" in item for item in report["unresolved"])


def test_hosted_source_collection_checks_the_candidate_and_keeps_only_the_bundle_and_draft():
    import re

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/compute-sources.yml").read_text()
    assert "workflow_dispatch:" in workflow and "pull_request:" not in workflow and "push:" not in workflow
    assert re.search(r"^permissions:\n  contents: read\n  actions: read\n\n", workflow, re.MULTILINE)
    assert "write" not in workflow
    assert set(re.findall(r"secrets\.([A-Z_]+)", workflow)) == {"GITHUB_TOKEN"}
    assert "options: [tex]" in workflow, "profiles with Noether or upstream archives are collected on a maintainer machine"
    order = ["image_candidate", "ref: ${{ steps.candidate.outputs.revision }}", "acceptance-$REVISION", "image_sources",
             "set(item) != {\"reason\"}", "upload-artifact"]
    assert [workflow.index(step) for step in order] == sorted(workflow.index(step) for step in order)
    assert "--ubuntu-keyring /usr/share/keyrings/ubuntu-archive-keyring.gpg" in workflow
    refusal = workflow.split("Refuse a bundle with unresolved sources", 1)[1].split("- name:", 1)[0]
    assert "set -o pipefail" in refusal and refusal.index("set -o pipefail") < refusal.index("| tee")
    uploaded = workflow.split("upload-artifact", 1)[1].split("if-no-files-found", 1)[0]
    assert "sources/sources.tar" in uploaded and "sources/source-review.draft.json" in uploaded
    assert "payload" not in uploaded and "download-failures" not in uploaded
    diagnostics = workflow.split("Retain failure diagnostics", 1)[1].split("retention-days", 1)[0]
    assert "if: failure()" in diagnostics and "path: ${{ runner.temp }}/diagnostics/" in diagnostics
    gathered = workflow.split("Gather failure diagnostics", 1)[1].split("Retain failure diagnostics", 1)[0]
    assert "-name apt-error.txt" in gathered and "sources.tar" not in gathered
    assert "retention-days: 7" in workflow and "persist-credentials: false" in workflow
