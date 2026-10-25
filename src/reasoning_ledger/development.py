"""Fast source-tree workflows; independent of deployment credentials and state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, suppress
from pathlib import Path

import httpx

from .deployment.host import InstallError
from .deployment.private import protected_temporary_directory

SMOKE = [
    "tests/test_public_server.py::test_durable_hierarchy_messages_and_session_survive_restart",
    "tests/test_public_server.py::test_tenant_scope_for_all_objects_and_foreign_keys",
    "tests/test_public_server.py::test_viewer_can_read_but_not_write_or_manage_keys",
]


def timed(arguments, label):
    if not label or len(label) > 120 or any(ord(c) < 32 for c in label):
        raise ValueError("phase labels must be short printable text")
    started = time.monotonic()
    code = 1
    try:
        code = subprocess.run(arguments, check=False).returncode
        return code if code >= 0 else 128 - code
    except KeyboardInterrupt:
        code = 130
        return code
    finally:
        print(f"{label}: {time.monotonic() - started:.2f}s (exit {code})", file=sys.stderr, flush=True)


def dependencies(root: Path, package="web", npm="npm"):
    if package not in {"web", "desktop"}:
        raise ValueError("choose web or desktop dependencies")
    directory = root / package
    version = subprocess.check_output(["node", "--version"]).strip()
    if package == "web":
        subprocess.run(["node", str(root / "web/check-node.cjs")], check=True)
    inputs = hashlib.sha256(b"\0".join([
        (directory / "package.json").read_bytes(), (directory / "package-lock.json").read_bytes(),
        version, platform.system().encode(), platform.machine().encode(),
    ])).hexdigest()
    stamp = directory / "node_modules/.reasoning-dependencies.json"
    installed = directory / "node_modules/.package-lock.json"
    required = directory / ("node_modules/typescript/bin/tsc" if package == "web" else "node_modules/electron/package.json")
    try:
        previous = json.loads(stamp.read_text())
        if required.is_file() and previous == {"inputs": inputs, "installed": hashlib.sha256(installed.read_bytes()).hexdigest()}:
            print(f"{package} dependencies: unchanged; using installed packages", flush=True)
            return
    except (OSError, ValueError):
        pass
    code = timed([npm, "--prefix", str(directory), "ci", "--engine-strict", "--no-audit", "--no-fund"], package + " dependency install")
    if code:
        raise ValueError(f"{package} dependency installation failed (exit {code})")
    stamp.write_text(json.dumps({"inputs": inputs, "installed": hashlib.sha256(installed.read_bytes()).hexdigest()}))


def changed_files(root: Path, base: str):
    if base.startswith("-") or any(ord(c) < 32 for c in base):
        raise ValueError("check base must be a Git revision")
    args = ["git", "-C", str(root), "diff", "--name-only", *( [base] if base else ["HEAD"]), "--"]
    changed = subprocess.check_output(args, text=True).splitlines()
    untracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"], text=True).splitlines()
    return sorted(set(changed + untracked))


def check_plan(files):
    tests, web, desktop = set(SMOKE), False, False
    for filename in files:
        if filename.startswith("web/") or filename in {"LICENSE", "NOTICE", ".nvmrc"}:
            web = True
        if filename.startswith("desktop/"):
            desktop = True
        if filename.startswith("tests/test_") and filename.endswith(".py") and not Path(filename).name.startswith("test_science_"):
            tests.add(filename)
        if filename.startswith("src/reasoning_ledger/deployment/"):
            tests.update(["tests/test_public_server_diagnostics.py", "tests/test_public_operator_session.py",
                          "tests/test_public_operator_sync.py"])
        if filename.startswith("src/reasoning_ledger/server/research_") or filename.endswith("/agents/native.py"):
            tests.update(["tests/test_public_provider_stream.py", "tests/test_public_research_export.py"])
        if filename.endswith("/server/openrouter.py"):
            tests.add("tests/test_public_provider_stream.py")
        if filename in {"Makefile", ".python-version", "pyproject.toml", "uv.lock"} or filename.startswith("src/reasoning_ledger/dev"):
            tests.add("tests/test_development.py")
        if filename.endswith(".md"):
            tests.add("tests/test_documentation.py")
    # A whole test file supersedes any smoke selections in that same file.
    tests = {test for test in tests if "::" not in test or test.split("::")[0] not in tests}
    return {"tests": sorted(tests), "web": web, "desktop": desktop}


def check(root: Path, base: str, explicit: list[str], npm: str):
    files = changed_files(root, base)
    plan = check_plan(files)
    tests = explicit or plan["tests"]
    print("Fast checks (not a release gate):\n  " + "\n  ".join(tests), flush=True)
    print(f"Frontend: {plan['web']} · desktop: {plan['desktop']} · base: {base or 'HEAD'}", flush=True)
    for args, label in (([sys.executable, "-m", "ruff", "check", "src", "tests"], "Python lint"),
                        ([sys.executable, "-m", "pytest", "-q", "--durations=5", *tests], "Selected Python tests")):
        code = timed(args, label)
        if code:
            return code
    for package in ("web", "desktop"):
        if plan[package]:
            dependencies(root, package, npm)
            code = timed([npm, "--prefix", package, "test"], package + " tests")
            if code:
                return code
            if package == "web":
                code = timed([npm, "--prefix", package, "run", "build"], "Frontend typecheck/build")
                if code:
                    return code
    return 0


def serve(root: Path, directory: Path | None, frontend_port: int, backend_port: int, npm: str):
    if not 1024 <= frontend_port <= 65535 or not 1024 <= backend_port <= 65535 or frontend_port == backend_port:
        raise ValueError("choose distinct unprivileged development ports")
    from .dev_app import EMAILS, PASSWORD, development_state

    if not (root / "web/package.json").is_file() or not (root / "src/reasoning_ledger/server/api.py").is_file():
        raise ValueError("start development from the Reasoning source checkout")
    ephemeral = directory is None
    if directory is not None:
        directory = directory.expanduser().absolute()
        if directory.is_relative_to(root):
            raise ValueError("keep persistent development state outside the source checkout")
    with ExitStack() as cleanup:
        if directory is None:
            directory = cleanup.enter_context(protected_temporary_directory("reasoning-dev-")) / "state"
        directory = development_state(directory)
        origin = f"http://127.0.0.1:{frontend_port}"
        environment = {**os.environ, "REASONING_DEV_STATE": str(directory), "REASONING_DEV_ORIGIN": origin,
                       "REASONING_DEV_BOT": os.environ.get("REASONING_DEV_BOT", "1"),
                       "REASONING_DEV_BACKEND_PORT": str(backend_port), "REASONING_DEV_FRONTEND_PORT": str(frontend_port)}
        print(f"\nLocal development: {origin}/organization.html\n"
              f"Simulated inference only — no paid model calls. State: {directory}\n"
              f"State policy: {'ephemeral — removed on exit' if ephemeral else 'persistent — retained on exit'}\n"
              f"Accounts: {', '.join(EMAILS)}\nPassword (local fixtures only): {PASSWORD}\n"
              "Ctrl-C stops both servers. Python, prompt, and frontend edits reload automatically.\n", flush=True)
        children = []

        def stop():
            for child in children:
                if child.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGTERM)
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    child.wait()

        cleanup.callback(stop)

        def interrupted(_signal, _frame):
            raise KeyboardInterrupt

        for number in (signal.SIGTERM, signal.SIGHUP):
            previous = signal.signal(number, interrupted)
            cleanup.callback(signal.signal, number, previous)
        backend = [sys.executable, "-m", "uvicorn", "reasoning_ledger.dev_app:create_dev_app", "--factory", "--host", "127.0.0.1",
                   "--port", str(backend_port), "--reload", "--reload-dir", str(root / "src"),
                   "--reload-include", "*.md", "--reload-include", "*.json"]
        frontend = [npm, "--prefix", str(root / "web"), "run", "dev", "--", "--host", "127.0.0.1",
                    "--port", str(frontend_port), "--strictPort"]
        try:
            for args in (backend, frontend):
                children.append(subprocess.Popen(args, env=environment, cwd=root, start_new_session=True))
            deadline = time.monotonic() + 20
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while time.monotonic() < deadline and all(child.poll() is None for child in children):
                    try:
                        response = client.get(f"http://127.0.0.1:{backend_port}/api/health")
                        if response.is_success and response.headers.get("X-Reasoning-Development") == "simulated-no-charges":
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise ValueError("development API did not become ready; both owned servers are being stopped")
            while all(child.poll() is None for child in children):
                time.sleep(0.1)
            failed = next(child for child in children if child.poll() is not None)
            return failed.returncode or 1
        except KeyboardInterrupt:
            return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("dependencies", "serve", "check"):
        command = commands.add_parser(name)
        command.add_argument("--source-root", type=Path, default=Path.cwd())
        command.add_argument("--npm", default="npm")
        if name == "dependencies":
            command.add_argument("--package", choices=["web", "desktop"], default="web")
        elif name == "serve":
            command.add_argument("--state-dir", type=Path)
            command.add_argument("--frontend-port", type=int, default=5173)
            command.add_argument("--backend-port", type=int, default=9119)
        else:
            command.add_argument("--base", default="origin/main")
            command.add_argument("--tests", nargs="*", default=[])
    command = commands.add_parser("time")
    command.add_argument("--label", required=True)
    command.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == "time":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            if not arguments:
                raise ValueError("time requires a command after --")
            return timed(arguments, args.label)
        root = args.source_root.resolve()
        if args.command == "dependencies":
            dependencies(root, args.package, args.npm)
            return 0
        os.chdir(root)
        if args.command == "check":
            began = time.monotonic()
            try:
                return check(root, args.base, args.tests, args.npm)
            finally:
                print(f"Fast checks total: {time.monotonic() - began:.2f}s", file=sys.stderr, flush=True)
        return serve(root, args.state_dir, args.frontend_port, args.backend_port, args.npm)
    except (OSError, ValueError, InstallError, subprocess.SubprocessError) as error:
        parser.exit(2, f"reasoning-dev: {error}\n")


if __name__ == "__main__":
    sys.exit(main())
