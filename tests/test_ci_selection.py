"""PR selection stays conservative and required gates reject failed prerequisites."""

import os
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELECTOR = runpy.run_path(str(ROOT / ".github/ci/select-checks.py"))
select = SELECTOR["select"]


@pytest.mark.parametrize("paths", [[], ["docs/bots.md"], ["README.md"]])
def test_documentation_does_not_require_integration_suites(paths):
    assert not any(select(paths).values())


def test_bot_client_change_keeps_backend_and_sandbox_coverage():
    selected = select(["src/reasoning_ledger/server/bot_agent.py", "tests/test_public_bot_agents.py", "docs/bots.md"])
    assert {suite for suite, required in selected.items() if required} == {"backend", "developer"}


@pytest.mark.parametrize("path", [
    "src/reasoning_ledger/server/store.py", "src/reasoning_ledger/server/api.py",
    "src/reasoning_ledger/deployment/host.py", "uv.lock", "pyproject.toml",
    "tests/conftest.py", ".github/workflows/checks.yml", ".github/ci/select-checks.py",
    "src/reasoning_ledger/prompts/research.md", "tests/test_public_bot_integration.py",
    "unknown-new-component/config.json",
])
def test_shared_code_and_unknown_paths_require_full_coverage(path):
    assert all(select([path]).values())


def test_main_and_manual_runs_always_require_full_coverage():
    assert all(select([], full=True).values())
    assert all(select(["docs/bots.md"], full=True).values())


def test_combined_scopes_retain_every_affected_suite():
    assert select(["desktop/src/main.mjs"])["desktop"]
    selected = select(["web/src/organization-main.tsx", "desktop/src/main.mjs"])
    assert {suite for suite, required in selected.items() if required} == {"backend", "developer", "frontend", "desktop"}


def test_git_selection_includes_both_paths_of_a_renamed_file(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for key, value in [("user.email", "fixture@example.test"), ("user.name", "Fixture")]:
        subprocess.run(["git", "-C", str(tmp_path), "config", key, value], check=True)
    (tmp_path / "runtime.py").write_text("runtime = True\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    (tmp_path / "runtime.py").rename(tmp_path / "README.md")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "rename"], check=True)
    output = tmp_path / "outputs"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHECK_EVENT", "pull_request")
    monkeypatch.setenv("CHECK_BASE", base)
    monkeypatch.setenv("CHECK_HEAD", "HEAD")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    SELECTOR["main"]()
    assert "backend=true" in output.read_text() and "upgrade=true" in output.read_text()


@pytest.mark.parametrize("selection,quality,required,result,success", [
    ("success", "success", "true", "success", True),
    ("success", "success", "false", "skipped", True),
    ("failure", "success", "false", "skipped", False),
    ("success", "failure", "false", "skipped", False),
    ("success", "success", "true", "failure", False),
    ("success", "success", "true", "cancelled", False),
    ("success", "success", "true", "skipped", False),
    ("success", "success", "", "skipped", False),
])
def test_backend_gate_rejects_failed_or_unexpected_skips(selection, quality, required, result, success):
    # Execute the actual workflow gate rather than a Python imitation.
    workflow = (ROOT / ".github/workflows/checks.yml").read_text()
    gate = workflow.split("  backend:\n", 1)[1].split("  upgrade-recovery-shards:\n", 1)[0]
    command = "\n".join(line.removeprefix("          ") for line in gate.split("        run: |\n", 1)[1].splitlines())
    response = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                              env={**os.environ, "SELECTION": selection, "QUALITY": quality, "REQUIRED": required, "RESULT": result})
    assert (response.returncode == 0) is success
