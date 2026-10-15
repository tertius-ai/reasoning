"""Conservatively select PR suites; main and manual runs exercise every suite."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SUITES = ("backend", "upgrade", "postgres", "frontend", "desktop", "developer")
BOT_CLIENTS = {
    "src/reasoning_ledger/server/bot_agent.py",
    "src/reasoning_ledger/server/bot_gateway_probe.py",
    "src/reasoning_ledger/server/bot_runner.py",
    "tests/test_public_bot_agents.py",
    "tests/test_public_bot_gateway_probe.py",
    "tests/test_public_bot_runner.py",
}


def select(paths: list[str], *, full: bool = False) -> dict[str, bool]:
    selected = set(SUITES) if full else set()
    for path in paths:
        if path.startswith("docs/") or (path.endswith(".md") and "/" not in path):
            continue
        if path in BOT_CLIENTS:
            # The deployment shards retain all bot tests. The developer job
            # additionally exercises its actual sandbox and offline profiles.
            selected.update(("backend", "developer"))
        elif path.startswith("desktop/"):
            selected.add("desktop")
        elif path.startswith("web/"):
            selected.update(("backend", "frontend", "developer"))
        else:
            # Shared server/library code, dependencies, release tooling, test
            # infrastructure, and unknown paths can affect every integration.
            selected.update(SUITES)
    # The developer job owns tests omitted from the deployment shards.
    if "backend" in selected:
        selected.add("developer")
    return {suite: suite in selected for suite in SUITES}


def main() -> None:
    full = os.environ.get("CHECK_EVENT") != "pull_request"
    paths = []
    if not full:
        base, head = os.environ["CHECK_BASE"], os.environ["CHECK_HEAD"]
        changed = subprocess.check_output([
            "git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--",
        ])
        paths = changed.decode("utf-8").rstrip("\0").split("\0") if changed else []
    selected = select(paths, full=full)
    print("Selected suites: " + ", ".join(suite for suite, enabled in selected.items() if enabled))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for suite, enabled in selected.items():
            output.write(f"{suite}={str(enabled).lower()}\n")


if __name__ == "__main__":
    main()
