"""Pinned source staging, never a mathematical or public distribution audit.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from reasoning_ledger.image_candidate import validate_run
from reasoning_ledger.image_context import NOETHER_PUBLIC_COMMIT, download_archive, materialize, stage_noether, validate_noether

ROOT = Path(__file__).resolve().parents[1]


def fixture_archive(tmp_path, extra=None):
    path = tmp_path / "source.tar.gz"
    prefix = "noether-" + "a" * 40
    entries = {name: b"fixture" for name in ("LICENSE", "Cargo.toml", "Cargo.lock", "pyproject.toml", "rust-toolchain.toml", "Makefile")}
    with tarfile.open(path, "w:gz") as archive:
        for name, body in entries.items():
            item = tarfile.TarInfo(prefix + "/" + name)
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
        if extra:
            if not extra.name.startswith("/"):
                extra.name = prefix + "/" + extra.name
            archive.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isreg() else None)
    lock = {"commit": "a" * 40, "version": "0.3.2", "build_context": "noether-source", "source_archive": {
        "url": "https://codeload.github.com/tertius-ai/noether/tar.gz/" + "a" * 40, "transport": "https", "strip_prefix": prefix,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}}
    return path, lock


def test_supplied_archive_is_private_hash_locked_snapshot_without_credentials(tmp_path):
    source, lock = fixture_archive(tmp_path)
    staged = stage_noether(lock, tmp_path / "context", source_archive=source)
    assert json.loads((staged / "reasoning-source-pin.json").read_bytes()) == {"commit": lock["commit"], "version": lock["version"]}
    assert not (staged / ".git").exists()
    assert not (staged / lock["source_archive"]["strip_prefix"]).exists()
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="exact-size"):
        stage_noether(lock, tmp_path / "other", source_archive=source)


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", ".git/config", ".env", "reasoning-source-pin.json", "LICENSE"])
def test_archive_escapes_duplicates_and_forged_pin_are_refused(tmp_path, name):
    item = tarfile.TarInfo(name)
    item.size = 1
    source, lock = fixture_archive(tmp_path, item)
    with pytest.raises(ValueError, match="unsafe or duplicate"):
        materialize(source, tmp_path / "context", lock)


def test_archive_links_and_mutable_archive_symlink_are_refused(tmp_path):
    item = tarfile.TarInfo("link")
    item.type, item.linkname = tarfile.SYMTYPE, "/etc/passwd"
    source, lock = fixture_archive(tmp_path, item)
    with pytest.raises(ValueError, match="unsafe"):
        stage_noether(lock, tmp_path / "context", source_archive=source)
    link = tmp_path / "alias"
    link.symlink_to(source)
    with pytest.raises(OSError):
        stage_noether(lock, tmp_path / "other", source_archive=link)


def test_real_recipe_has_bounded_source_pin_without_embedded_credentials():
    lock = json.loads((ROOT / "images/research-cpu/inputs.json").read_bytes())
    assert validate_noether(lock)["commit"] == NOETHER_PUBLIC_COMMIT
    lock["noether"]["source_archive"]["url"] += "?token=private"
    with pytest.raises(ValueError, match="bounded hash-locked"):
        validate_noether(lock)


@pytest.mark.parametrize("repository, url", [
    ("https://github.com/other-owner/noether", None),  # archive URL still names the recipe's owner
    ("http://github.com/tertius-ai/noether", None),
    ("https://github.com/tertius-ai/noether-fork", None),
    ("https://example.org/tertius-ai/noether", None),
    (None, "https://codeload.github.com/other-owner/noether/tar.gz/{commit}"),
])
def test_archive_url_must_come_from_the_locked_source_repository(repository, url):
    lock = json.loads((ROOT / "images/research-cpu/inputs.json").read_bytes())
    noether = lock["noether"]
    if repository is not None:
        noether["source_repository"] = repository
    if url is not None:
        noether["source_archive"]["url"] = url.format(commit=noether["commit"])
    with pytest.raises(ValueError, match="bounded hash-locked"):
        validate_noether(lock)


def test_consistent_repository_and_archive_from_another_owner_are_accepted():
    lock = json.loads((ROOT / "images/research-cpu/inputs.json").read_bytes())
    noether = lock["noether"]
    noether["source_repository"] = "https://github.com/other-owner/noether"
    noether["source_archive"]["url"] = "https://codeload.github.com/other-owner/noether/tar.gz/" + noether["commit"]
    assert validate_noether(lock)["commit"] == NOETHER_PUBLIC_COMMIT


def test_git_override_exports_only_the_locked_object_not_private_working_tree(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    def git(*arguments):
        return subprocess.check_output(["git", *arguments], cwd=repository).decode().strip()
    git("init", "--quiet")
    git("config", "user.name", "Fixture author")
    git("config", "user.email", "fixture@example.invalid")
    for name in ("LICENSE", "Cargo.toml", "Cargo.lock", "pyproject.toml", "rust-toolchain.toml", "Makefile"):
        (repository / name).write_text("committed fixture")
    git("add", ".")
    git("commit", "--quiet", "-m", "Synthetic source fixture")
    revision = git("rev-parse", "HEAD")
    (repository / "LICENSE").write_text("private working-tree edit")
    (repository / "private-untracked.txt").write_text("must not enter build context")
    lock = {"commit": revision, "version": "fixture", "source_archive": {"strip_prefix": "unused-public-archive-prefix"}}
    staged = stage_noether(lock, tmp_path / "context", source_root=repository)
    assert (staged / "LICENSE").read_text() == "committed fixture"
    assert not (staged / "private-untracked.txt").exists() and not (staged / ".git").exists()


@pytest.mark.parametrize("failure", [None, "oversized", "truncated", "changed", "redirect"])
def test_public_download_is_anonymous_and_archive_bytes_are_locked(tmp_path, monkeypatch, failure):
    source, lock = fixture_archive(tmp_path)
    body = source.read_bytes()
    if failure == "oversized":
        body += b"extra"
    elif failure == "truncated":
        body = body[:-1]
    elif failure == "changed":
        body = b"x" * len(body)
    original = httpx.Client
    def response(request):
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(302 if failure == "redirect" else 200, stream=httpx.ByteStream(body))
    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(response))
    monkeypatch.setattr("reasoning_ledger.image_context.httpx.Client", client)
    if failure:
        with pytest.raises(ValueError):
            stage_noether(lock, tmp_path / "context")
        assert not (tmp_path / "context").exists()
    else:
        assert (stage_noether(lock, tmp_path / "context") / "LICENSE").read_bytes() == b"fixture"


def test_download_deadline_stops_before_writing_expired_response(tmp_path, monkeypatch):
    source, lock = fixture_archive(tmp_path)
    original = httpx.Client
    monkeypatch.setattr("reasoning_ledger.image_context.httpx.Client", lambda **options: original(
        **options, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(source.read_bytes())))))
    times = iter((0, 181))
    monkeypatch.setattr("reasoning_ledger.image_context.time", SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(ValueError, match="timed out"):
        download_archive(lock["source_archive"], tmp_path / "download")


@pytest.mark.parametrize("field,value", [("head_sha", "b" * 40), ("status", "in_progress"),
    ("conclusion", "failure"), ("event", "pull_request"), ("path", ".github/workflows/other.yml"),
    ("id", True), ("head_repository", {"id": 12})])
def test_publication_refuses_wrong_or_unfinished_candidate_run(field, value):
    run = {"head_sha": "a" * 40, "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
           "path": ".github/workflows/compute-images.yml", "id": 11, "repository": {"id": 10}, "head_repository": {"id": 10}}
    validate_run(run, "a" * 40)
    run[field] = value
    with pytest.raises(ValueError, match="exact successful"):
        validate_run(run, "a" * 40)
