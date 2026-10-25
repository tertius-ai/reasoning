"""Fast local development boundaries, fixtures, caching, and command selection."""

import asyncio
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console

from reasoning_ledger import dev_app, development
from reasoning_ledger.deployment import setup_ui
from reasoning_ledger.deployment.host import InstallError


def fixture_node(directory, version):
    directory.mkdir(parents=True, exist_ok=True)
    node = directory / "node"
    node.write_text(f"#!{sys.executable}\nimport sys\n"
                    f"if sys.argv[1:] == ['--version']: print({version!r})\n"
                    f"elif sys.argv[1:] == ['web/check-node.cjs']: sys.exit(0 if {version!r}.startswith('v24.') else 2)\n"
                    "else: raise AssertionError(sys.argv)\n")
    node.chmod(0o700)


@pytest.mark.parametrize("discovery", ["custom", "xdg"])
def test_build_wrapper_selects_installed_node_for_children_without_global_changes(tmp_path, discovery):
    root = Path(__file__).resolve().parents[1]
    fixture_node(tmp_path / "old", "v18.19.1")
    nvm = tmp_path / "version manager with spaces"
    fixture_node(nvm / "nvm" / "bin", "v24.15.0")
    script = nvm / "nvm" / "nvm.sh"
    script.write_text('test "$#" = 1 && test "$1" = --no-use || return 41\n'
                      'nvm() {\n'
                      '  test "$#" = 3 && test "$1" = use && test "$2" = --silent && test "$3" = 24 || return 42\n'
                      '  PATH="$NVM_DIR/bin:$PATH"; export PATH\n'
                      '}\n')
    default = nvm / "nvm" / "default"
    default.write_text("18\n")
    environment = {**os.environ, "PATH": str(tmp_path / "old") + os.pathsep + os.environ["PATH"]}
    if discovery == "custom":
        environment["NVM_DIR"] = str(nvm / "nvm")
    else:
        environment.pop("NVM_DIR", None)
        environment["XDG_CONFIG_HOME"] = str(nvm)
    response = subprocess.run(["/bin/sh", "web/with-node.sh", "/bin/sh", "-c",
                               'node --version; printf "%s\\n" "$1"; exit 7', "build", "argument with spaces"],
                              cwd=root, env=environment, capture_output=True, text=True)
    assert response.returncode == 7 and response.stdout == "v24.15.0\nargument with spaces\n", response.stderr
    assert "this command only" in response.stderr and default.read_text() == "18\n"
    assert subprocess.check_output(["node", "--version"], env=environment, text=True).strip() == "v18.19.1"


def test_build_wrapper_preserves_compatible_node_without_loading_nvm(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture_node(tmp_path / "current", "v24.15.0")
    script = tmp_path / "nvm.sh"
    script.write_text("exit 99\n")
    response = subprocess.run(["/bin/sh", "web/with-node.sh", "node", "--version"], cwd=root, text=True,
                              env={**os.environ, "NVM_DIR": str(tmp_path),
                                   "PATH": str(tmp_path / "current") + os.pathsep + os.environ["PATH"]}, capture_output=True)
    assert response.returncode == 0 and response.stdout == "v24.15.0\n" and response.stderr == ""


@pytest.mark.parametrize("nvm_available", [False, True])
def test_make_upgrade_refuses_unusable_node_before_operator_or_build_work(tmp_path, nvm_available):
    root = Path(__file__).resolve().parents[1]
    fixture_node(tmp_path / "old", "v18.19.1")
    nvm = tmp_path / "nvm"
    nvm.mkdir()
    if nvm_available:
        (nvm / "nvm.sh").write_text('nvm() { return 3; }\n')
    marker = tmp_path / "uv-called"
    uv = tmp_path / "uv-fixture"
    uv.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
    uv.chmod(0o700)
    response = subprocess.run(["make", "upgrade", "UV=" + str(uv), "ALLOW_DIRTY=1"], cwd=root,
                              env={**os.environ, "NVM_DIR": str(nvm),
                                   "PATH": str(tmp_path / "old") + os.pathsep + os.environ["PATH"]}, capture_output=True, text=True)
    assert response.returncode != 0 and not marker.exists()
    assert "No Node installation, default-version change, or cloud access was attempted" in response.stderr
    assert ("nvm install 24" if nvm_available else "set NVM_DIR") in response.stderr


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    monkeypatch.setenv("REASONING_DEV_STATE", str(tmp_path / "dev"))
    monkeypatch.setenv("REASONING_DEV_ORIGIN", "http://127.0.0.1:5173")
    return dev_app.create_dev_app()


def test_seeded_organization_contains_roles_topics_and_only_simulated_models(local_app):
    store = local_app.state.store
    session = store.login(dev_app.EMAILS[0], dev_app.PASSWORD, "127.0.0.1")
    with store.transaction() as db:
        org = db.execute("SELECT id FROM organizations").fetchone()[0]
        assert [row[0] for row in db.execute("SELECT role FROM memberships ORDER BY role")] == ["member", "owner", "viewer"]
    hierarchy = store.hierarchy(session["user_id"], org)
    assert {topic["kind"] for topic in hierarchy["topics"]} == {"alpha", "beta", "gamma"}
    assert store.policy(session["user_id"], org)["allowed_models"] == list(dev_app.MODELS)
    with TestClient(local_app, base_url="http://127.0.0.1:5173") as client:
        response = client.get("/api/health")
        assert response.headers["X-Reasoning-Development"] == "simulated-no-charges"


def test_development_refuses_unmarked_existing_state_and_symlinks(tmp_path):
    production = tmp_path / "production"
    production.mkdir(mode=0o700)
    (production / "server.sqlite3").write_bytes(b"untouched")
    with pytest.raises(ValueError, match="unmarked"):
        dev_app.development_state(production)
    assert (production / "server.sqlite3").read_bytes() == b"untouched"
    alias = tmp_path / "alias"
    alias.symlink_to(production, target_is_directory=True)
    with pytest.raises((ValueError, InstallError)):
        dev_app.development_state(alias)


def test_development_requires_explicit_loopback_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("REASONING_DEV_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("REASONING_DEV_ORIGIN", "https://public.example.org")
    with pytest.raises(ValueError, match="127.0.0.1"):
        dev_app.create_dev_app()
    assert not (tmp_path / "state").exists()


def test_persistent_fixture_restart_does_not_duplicate_or_replace_user_edits(local_app, monkeypatch):
    store = local_app.state.store
    before = store.database.read_bytes()
    another = dev_app.create_dev_app()
    with another.state.store.transaction() as db:
        # Three people and the owner's development bot.
        assert db.execute("SELECT count(*) FROM users WHERE account_kind='human'").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM users WHERE account_kind='bot'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM topics").fetchone()[0] == 3
    assert before == store.database.read_bytes()


def test_simulated_provider_never_accepts_real_keys_and_streams_zero_cost():
    async def run():
        provider = dev_app.simulated_provider()
        quote = (await provider.quotes([dev_app.MODELS[0]]))[0]
        chunks = [chunk async for chunk in provider.stream(dev_app.KEY, quote, [{"role": "user", "content": "Research"}], None)]
        assert "Simulated development" in "".join(chunk.text for chunk in chunks)
        assert [chunk.usage.cost_microusd for chunk in chunks if chunk.usage] == [0]
        async with provider.client() as client:
            result = await client.post(provider.base_url + "/chat/completions", headers={"Authorization": "Bearer real-key"}, json={})
            assert result.status_code == 401
    asyncio.run(run())


def test_simulated_provider_completes_a_real_beta_worker_run(local_app):
    store = local_app.state.store
    user = store.login(dev_app.EMAILS[0], dev_app.PASSWORD, "127.0.0.1")["user_id"]
    with store.transaction() as db:
        org = db.execute("SELECT id FROM organizations").fetchone()[0]
        topic = db.execute("SELECT id FROM topics WHERE kind='beta'").fetchone()[0]
    store.post_message(user, org, topic, "Exercise a development response", "development-worker-test")
    provider = local_app.state.inference_worker.provider
    quote = asyncio.run(provider.quotes([dev_app.MODELS[0]]))[0]
    state = local_app.state.inference
    with TestClient(local_app, base_url="http://127.0.0.1:5173"):
        run = state.enqueue(user, org, topic, "development-inference-test", state.parameters([quote.model], None, 50, None), [quote])[0]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = state.run(user, org, run["id"])
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)
        assert result["status"] == "succeeded", result
        assert "Simulated development response" in result["output_text"]


def test_mock_goal_records_blocker_instead_of_claiming_research_success():
    async def run():
        stream = dev_app.SimulatedStream({"model": dev_app.MODELS[0], "messages": [],
                                        "tools": [{"function": {"name": "goal_checkpoint"}}]})
        result = b"".join([part async for part in stream])
        assert b"goal_checkpoint" in result and b"blocked" in result and b"complete" not in result
    asyncio.run(run())


def test_check_plan_is_scoped_and_whole_files_supersede_smoke_selectors():
    plan = development.check_plan(["web/src/BetaConversation.tsx"])
    assert plan == {"tests": sorted(development.SMOKE), "web": True, "desktop": False}
    plan = development.check_plan(["src/reasoning_ledger/deployment/cli.py", "tests/test_public_server.py"])
    assert "tests/test_public_operator_session.py" in plan["tests"]
    assert "tests/test_public_server.py" in plan["tests"]
    assert not any("test_public_server.py::" in test for test in plan["tests"])
    assert not development.check_plan(["README.md"])["web"]


def test_changed_files_includes_committed_staged_unstaged_and_untracked(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "fixture@example.test")
    git("config", "user.name", "Fixture")
    (tmp_path / "tracked").write_text("one")
    git("add", "tracked")
    git("commit", "-qm", "Fixture")
    base = git("rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "tracked").write_text("two")
    git("commit", "-qam", "Fixture update")
    (tmp_path / "staged").write_text("three")
    git("add", "staged")
    (tmp_path / "untracked").write_text("four")
    assert development.changed_files(tmp_path, base) == ["staged", "tracked", "untracked"]


def test_dependency_cache_invalidates_lockfiles_and_missing_tools(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    for name in ("package.json", "package-lock.json"):
        (web / name).write_text("{}")
    monkeypatch.setattr(development.subprocess, "check_output", lambda *a, **k: b"v24.15.0")
    original = development.subprocess.run
    installs = []

    def run(args, **kwargs):
        if args[:1] == ["node"]:
            return subprocess.CompletedProcess(args, 0)
        installs.append(args)
        (web / "node_modules/typescript/bin").mkdir(parents=True, exist_ok=True)
        (web / "node_modules/typescript/bin/tsc").write_text("fixture")
        (web / "node_modules/.package-lock.json").write_text("{}")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(development.subprocess, "run", run)
    development.dependencies(tmp_path)
    development.dependencies(tmp_path)
    assert len(installs) == 1
    (web / "package-lock.json").write_text('{"changed": true}')
    development.dependencies(tmp_path)
    assert len(installs) == 2
    (web / "node_modules/typescript/bin/tsc").unlink()
    development.dependencies(tmp_path)
    assert len(installs) == 3
    monkeypatch.setattr(development.subprocess, "run", original)


def test_phase_timing_preserves_command_exit_status(capsys):
    assert development.timed([sys.executable, "-c", "raise SystemExit(7)"], "Fixture phase") == 7
    output = capsys.readouterr()
    assert "Fixture phase:" in output.err and "exit 7" in output.err


def test_upgrade_progress_reports_elapsed_time_without_changing_failure_semantics(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(setup_ui, "console", Console(file=output, force_terminal=False))
    ticks = iter([10, 12, 20, 23])
    monkeypatch.setattr(setup_ui, "monotonic", lambda: next(ticks))
    with setup_ui.upgrade_progress("Local fixture"):
        pass
    with pytest.raises(ValueError), setup_ui.upgrade_progress("Failing fixture"):
        raise ValueError("fixture")
    assert "(2.0s)" in output.getvalue() and "after 3.0s" in output.getvalue()


def test_development_ports_are_loopback_and_distinct(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        development.serve(tmp_path, None, 5173, 5173, "npm")


def test_development_does_not_create_persistent_state_in_checkout(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text("{}")
    (tmp_path / "src/reasoning_ledger/server").mkdir(parents=True)
    (tmp_path / "src/reasoning_ledger/server/api.py").write_text("# fixture")
    with pytest.raises(ValueError, match="outside the source checkout"):
        development.serve(tmp_path, tmp_path / "private", 5173, 9119, "npm")
    assert not (tmp_path / "private").exists()
