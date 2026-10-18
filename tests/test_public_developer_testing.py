"""Actual isolated development-box tests and owner-approved app tool receipts.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reasoning_ledger.agents.tools import ToolRegistry
from reasoning_ledger.deployment.private import read_document, write_document
from reasoning_ledger.server import bot_testing
from reasoning_ledger.server.bot_client import BotClientError
from reasoning_ledger.server.bot_diagnostics import deployment_identity, release_identity


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "src/reasoning_ledger/server").mkdir(parents=True)
    (root / "src/reasoning_ledger/server/api.py").write_text("# fixture\n")
    (root / "tests").mkdir()
    (root / "tests/test_public_server.py").write_text(
        "import os, socket\nfrom pathlib import Path\n"
        "def test_offline():\n"
        " assert 'REASONING_TEST_PRIVATE' not in os.environ\n"
        " assert not Path('/root/.ssh').exists()\n"
        " assert not Path('.git').exists()\n"
        " assert not Path('tmp4.json').exists()\n"
        " assert not Path('.env').exists()\n"
        " assert not Path('.venv/../.git').exists()\n"
        " assert socket.socket().connect_ex(('127.0.0.1', 443)) != 0\n"
    )
    (root / ".env").write_text("private env fixture")
    (root / "tmp4.json").write_text("private export fixture")
    (root / "src/.env").write_text("tracked sensitive fixture must not enter snapshot")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
                    "commit", "-qm", "fixture"], check=True)
    # A real already-installed test toolchain, not a fake process.
    (root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    state = tmp_path / "private"
    state.mkdir(mode=0o700)
    policy = state / "developer.json"
    write_document(policy, {"format": 1, "repository": "research/bench", "source_root": str(root), "profiles": ["smoke"]}, new=True)
    return root, state, policy


def test_snapshot_excludes_private_paths_and_identifies_local_source(checkout, tmp_path):
    root, _, path = checkout
    document = bot_testing.policy(path)
    source = tmp_path / "source"
    source.mkdir()
    identity = bot_testing.snapshot(root, source)
    assert not (source / ".env").exists() and not (source / "src/.env").exists()
    assert not (source / "tmp4.json").exists() and not (source / ".git").exists()
    assert not identity["dirty"]
    (root / "src/reasoning_ledger/server/api.py").write_text("# changed\n")
    another = tmp_path / "another"
    another.mkdir()
    changed = bot_testing.snapshot(root, another)
    assert changed["source_revision"] == identity["source_revision"]
    assert changed["snapshot_sha256"] != identity["snapshot_sha256"] and changed["dirty"]
    with pytest.raises(BotClientError, match="not enabled"):
        bot_testing.prepare(document, "frontend", tmp_path)


def test_real_offline_sandbox_retains_receipt_and_does_not_expose_environment(checkout, tmp_path, monkeypatch):
    if sys.platform != "linux" or not shutil.which("bwrap"):
        pytest.skip("Linux bubblewrap acceptance")
    root, state, path = checkout
    monkeypatch.setenv("REASONING_TEST_PRIVATE", "private fixture")
    directory = tmp_path / "invocation"
    directory.mkdir()
    identity, command = bot_testing.prepare(bot_testing.policy(path), "smoke", directory)
    receipt = state / "result.json"
    result = bot_testing.execute(identity, command, receipt)
    output = read_document(receipt)
    assert result["status"] == "passed", output["output"]
    assert output["exit_code"] == 0 and "1 passed" in output["output"]
    assert "output" not in result and result["output_retained_locally"]
    assert not (root / ".pytest_cache").exists()
    assert receipt.stat().st_mode & 0o077 == 0


def test_local_policy_refuses_invalid_profiles_and_symlink_source(checkout):
    root, _, path = checkout
    document = read_document(path)
    write_document(path, {**document, "profiles": ["$(shell)"]})
    with pytest.raises(BotClientError, match="policy"):
        bot_testing.policy(path)
    write_document(path, document)
    (root / "src/reasoning_ledger/server/api.py").unlink()
    (root / "src/reasoning_ledger/server/api.py").symlink_to(path)
    destination = root.parent / "snapshot"
    destination.mkdir()
    with pytest.raises(BotClientError, match="symlink"):
        bot_testing.snapshot(root, destination)


@pytest.mark.parametrize("approved", [False, True])
def test_app_test_tool_freezes_snapshot_and_consumes_exact_owner_approval(checkout, monkeypatch, approved):
    _, state, path = checkout
    executed, requests = [], []
    class Gateway:
        local_tools = True
        receipt_directory = state

        def request(self, suffix, **values):
            requests.append((suffix, values))
            if suffix == "/developer":
                return {"enabled": True, "repositories": ["research/bench"]}
            if suffix == "/executions":
                self.command = values["command"]
                assert self.command[:3] == ["reasoning-dev-test", "research/bench", "smoke"]
                assert len(self.command[4]) == 64
                return {"id": "a" * 32, "status": "approved" if approved else "denied"}
            if suffix.endswith("/consume"):
                return {"command_json": json.dumps(self.command)}
            return {}
    def execute(identity, arguments, receipt):
        executed.append(identity)
        assert any(suffix.endswith("/consume") for suffix, _ in requests)
        assert "--unshare-all" in arguments
        return {**identity, "status": "passed"}
    monkeypatch.setattr(bot_testing, "execute", execute)
    registry = ToolRegistry()
    bot_testing.register(Gateway(), registry, path, {"developer": {"enabled": True, "repositories": ["research/bench"]}})
    result = registry.invoke("development_test", {"profile": "smoke"})
    assert bool(executed) == approved
    assert ("refused" in result) != approved


def test_test_tool_absent_without_local_and_server_grants(checkout):
    _, state, path = checkout
    class Gateway:
        local_tools = False
        receipt_directory = state
    registry = ToolRegistry()
    bot_testing.register(Gateway(), registry, path, {"developer": {"enabled": True, "repositories": ["research/bench"]}})
    assert registry.list() == []
    Gateway.local_tools = True
    bot_testing.register(Gateway(), registry, path, {"developer": {"enabled": True, "repositories": ["elsewhere/repo"]}})
    assert registry.list() == []


def test_regression_inclusion_is_frozen_and_changes_snapshot_identity(checkout, tmp_path):
    _, _, path = checkout
    plain, extra = tmp_path / "plain", tmp_path / "extra"
    plain.mkdir()
    extra.mkdir()
    document = bot_testing.policy(path)
    first, _ = bot_testing.prepare(document, "smoke", plain)
    regression = b"def test_regression():\n assert 2 + 2 == 4\n"
    identity, command = bot_testing.prepare(document, "smoke", extra, regression=regression)
    assert identity["snapshot_sha256"] != first["snapshot_sha256"]
    assert identity["regression_sha256"] == hashlib.sha256(regression).hexdigest()
    assert (extra / "source/tests/test_developer_regression.py").read_bytes() == regression
    assert command[-1] == "tests/test_developer_regression.py"


def test_source_read_requires_current_grant_and_excludes_private_files(checkout):
    _, state, path = checkout
    class Gateway:
        local_tools = True
        receipt_directory = state
        enabled = True

        def request(self, suffix, **values):
            assert suffix == "/developer"
            return {"enabled": self.enabled, "repositories": ["research/bench"]}
    gateway = Gateway()
    registry = ToolRegistry()
    bot_testing.register(gateway, registry, path, {"developer": {"enabled": True, "repositories": ["research/bench"]}})
    assert registry.invoke("development_source_read", {"path": "src/reasoning_ledger/server/api.py"})["text"] == "# fixture\n"
    for name in [".env", "tmp4.json", "src/.env", "../private/developer.json"]:
        with pytest.raises(BotClientError):
            registry.invoke("development_source_read", {"path": name})
    gateway.enabled = False
    with pytest.raises(BotClientError, match="permission changed"):
        registry.invoke("development_source_read", {"path": "src/reasoning_ledger/server/api.py"})


def test_deployment_identity_requires_exact_release_hash_and_valid_revision(tmp_path):
    frontend = tmp_path / "web"
    frontend.mkdir()
    document = json.dumps({"source_revision": "b" * 40, "private": "never returned"}).encode()
    (tmp_path / "manifest.json").write_bytes(document)
    release = hashlib.sha256(document).hexdigest()
    assert deployment_identity(frontend, release) == {"release_id": release, "source_revision": "b" * 40}
    assert deployment_identity(frontend, "c" * 64) == {"release_id": "c" * 64, "source_revision": None}
    assert deployment_identity(None, None) == {"release_id": None, "source_revision": None}


@pytest.mark.parametrize("dirty", [True, False, "yes", None])
def test_release_identity_reports_modified_source_only_for_a_verified_revision(tmp_path, dirty):
    frontend = tmp_path / "web"
    frontend.mkdir()
    document = json.dumps({"source_revision": "b" * 40, **({} if dirty is None else {"dirty": dirty})}).encode()
    (tmp_path / "manifest.json").write_bytes(document)
    release = hashlib.sha256(document).hexdigest()
    expected = dirty if isinstance(dirty, bool) else None
    assert release_identity(frontend, release) == {"release_id": release, "source_revision": "b" * 40, "source_modified": expected}
    assert release_identity(frontend, "c" * 64) == {"release_id": "c" * 64, "source_revision": None, "source_modified": None}
    unverified = json.dumps({"source_revision": "unverified", "dirty": True}).encode()
    (tmp_path / "manifest.json").write_bytes(unverified)
    assert release_identity(frontend, hashlib.sha256(unverified).hexdigest())["source_modified"] is None


@pytest.mark.parametrize("failure", ["missing", "linked", "directory", "fifo", "oversized", "invalid_json", "invalid_revision", "array"])
def test_deployment_identity_rejects_unverified_manifest_without_crashing_or_blocking(tmp_path, failure):
    frontend = tmp_path / "web"
    frontend.mkdir()
    manifest = tmp_path / "manifest.json"
    body = json.dumps({"source_revision": "b" * 40}).encode()
    if failure == "linked":
        original = tmp_path / "original.json"
        original.write_bytes(body)
        manifest.symlink_to(original)
    elif failure == "directory":
        manifest.mkdir()
    elif failure == "fifo":
        import os

        os.mkfifo(manifest)
    elif failure != "missing":
        body = {"oversized": b"x" * (1024 * 1024 + 1), "invalid_json": b"not json",
                "invalid_revision": b'{"source_revision":"unverified"}', "array": b"[]"}.get(failure, body)
        manifest.write_bytes(body)
    release = hashlib.sha256(body).hexdigest()
    assert deployment_identity(frontend, release) == {"release_id": release, "source_revision": None}


def test_control_character_output_fits_private_receipt_and_is_never_exposed(checkout, monkeypatch):
    _, state, _ = checkout
    def noisy(arguments, *, timeout, maximum):
        assert maximum == 32 * 1024
        return {"exit_code": 0, "output": "\0" * maximum, "truncated": True, "timed_out": False}
    monkeypatch.setattr(bot_testing, "bounded_execute", noisy)
    receipt = state / "noisy.json"
    result = bot_testing.execute({"profile": "smoke"}, [], receipt)
    assert result["status"] == "failed" and "output" not in result
    assert len(read_document(receipt)["output"]) == 32 * 1024


def test_failure_identifiers_do_not_expose_parameter_values(checkout, monkeypatch):
    _, state, _ = checkout
    def failed(arguments, **limits):
        return {"exit_code": 1, "output": "FAILED tests/test_private.py::test_case[private_param] - private detail\n",
                "truncated": False, "timed_out": False}
    monkeypatch.setattr(bot_testing, "bounded_execute", failed)
    result = bot_testing.execute({"profile": "smoke"}, [], state / "failure.json")
    assert result["failed_tests"] == ["tests/test_private.py::test_case"]
    assert "private_param" not in json.dumps(result) and "private detail" not in json.dumps(result)
