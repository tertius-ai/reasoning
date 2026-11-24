"""Local, command-scoped SSH authentication before deployment network access."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .host import InstallError
from .private import protected_temporary_directory, read_document, read_private, write_private
from .setup_ssh import public_identity


def _capture(arguments, environment):
    try:
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=environment, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("local SSH preparation failed; check ssh-keygen, ssh-add, and ssh-agent installation") from None
    if len(result.stdout) > 256 * 1024:
        raise InstallError("local SSH metadata exceeded its allowance")
    return result


def _agent_ready(fingerprint, environment):
    if not environment.get("SSH_AUTH_SOCK"):
        return False
    try:
        result = _capture(["ssh-add", "-L"], environment)
    except InstallError:
        return False
    if result.returncode:
        return False
    for line in result.stdout.decode("ascii", errors="replace").splitlines():
        try:
            public = public_identity(line)
        except InstallError:
            continue
        digest = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(public.split()[1])).digest()).decode().rstrip("=")
        if digest == fingerprint:
            with protected_temporary_directory("reasoning-ssh-test-") as directory:
                path = directory / "public-key"
                write_private(path, public.encode("ascii") + b"\n")
                try:
                    return _capture(["ssh-add", "-T", str(path)], environment).returncode == 0
                except InstallError:
                    return False
    return False


@contextmanager
def _temporary_agent(environment):
    with protected_temporary_directory("reasoning-agent-") as directory:
        socket = directory / "socket"
        try:
            agent = subprocess.Popen(["ssh-agent", "-D", "-t", "3600", "-a", str(socket)], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            raise InstallError("cannot start a temporary SSH agent; install ssh-agent") from None
        try:
            deadline = time.monotonic() + 5
            while not socket.exists():
                if agent.poll() is not None or time.monotonic() >= deadline:
                    raise InstallError("temporary SSH agent did not start; no cloud access was attempted")
                time.sleep(0.05)
            info = socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise InstallError("temporary SSH agent socket is not owned by this operator")
            yield {**environment, "SSH_AUTH_SOCK": str(socket), "SSH_AGENT_PID": str(agent.pid)}
        finally:
            if agent.poll() is None:
                agent.terminate()
                try:
                    agent.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    agent.kill()
                    agent.wait()


@contextmanager
def authenticated_environment(identity: Path):
    identity = identity.absolute()
    key = read_private(identity, 65536)
    environment = dict(os.environ)
    # Read this exact protected private key, not an adjacent .pub file.
    with protected_temporary_directory("reasoning-key-check-") as directory:
        snapshot = directory / "private-key"
        write_private(snapshot, key)
        result = _capture(["ssh-keygen", "-l", "-E", "sha256", "-P", "", "-f", str(snapshot)], environment)
    match = re.search(rb"\b(SHA256:[A-Za-z0-9+/]{43})\b", result.stdout)
    if result.returncode or match is None:
        raise InstallError("cannot identify the operator SSH private key; check its format without replacing it")
    fingerprint = match[1].decode("ascii")
    if _agent_ready(fingerprint, environment):
        yield environment
        return
    plain = _capture(["ssh-keygen", "-y", "-P", "", "-f", str(identity)], environment)
    if plain.returncode == 0:
        public_identity(plain.stdout.decode("ascii"))
        yield environment
        return
    if not sys.stdin.isatty():
        raise InstallError(
            "operator SSH key is not unlocked. Run this command in your own interactive terminal, or load the key into "
            'an SSH agent first (eval "$(ssh-agent -s)"; ssh-add ' + shlex.quote(str(identity)) + "). "
            "No cloud access was attempted."
        )
    print("Unlocking the operator SSH key locally for this command.\n"
          "Enter its passphrase in this terminal; Ctrl-C cancels before cloud access.\n"
          "The temporary agent is removed when the command finishes.", file=sys.stderr, flush=True)
    with _temporary_agent(environment) as prepared:
        unlock_environment = {**prepared, "SSH_ASKPASS_REQUIRE": "never"}
        unlock_environment.pop("SSH_ASKPASS", None)
        try:
            unlocked = subprocess.run(["ssh-add", str(identity)], env=unlock_environment, stdout=subprocess.DEVNULL,
                                      timeout=300, check=False)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            raise InstallError("SSH unlocking cancelled or timed out; no cloud access was attempted; retry the same command") from None
        if unlocked.returncode or not _agent_ready(fingerprint, prepared):
            raise InstallError("operator SSH key was not unlocked; no cloud access was attempted; retry the same command")
        yield prepared


@contextmanager
def authenticated_session(identity: Path):
    """Use a command-owned agent for in-process SSH calls and restore the caller's environment."""
    names = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")
    original = {name: os.environ.get(name) for name in names}
    with authenticated_environment(identity) as environment:
        try:
            for name in names:
                if name in environment:
                    os.environ[name] = environment[name]
                else:
                    os.environ.pop(name, None)
            yield
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def run_session(directory: Path, arguments: list[str]) -> int:
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        raise InstallError("operator-session requires a local command after --")
    config = read_document(directory.absolute() / "setup.json")
    identity = config.get("identity")
    if not isinstance(identity, str) or not Path(identity).is_absolute() or any(ord(c) < 32 or ord(c) == 127 for c in identity):
        raise InstallError("saved operator SSH identity must be an absolute protected private-key path")
    with authenticated_environment(Path(identity)) as environment:
        try:
            code = subprocess.run(arguments, env=environment, check=False).returncode
            return code if code >= 0 else 128 - code
        except KeyboardInterrupt:
            print("Operator command interrupted; preserve deployment state and resume the same command.", file=sys.stderr)
            return 130
