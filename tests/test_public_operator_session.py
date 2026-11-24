"""Local SSH agents and disposable fixture keys; no cloud or deployment access."""

import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from reasoning_ledger.deployment import operator_session as session
from reasoning_ledger.deployment.cli import main
from reasoning_ledger.deployment.host import InstallError
from reasoning_ledger.deployment.private import write_document


@pytest.fixture
def key(tmp_path, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    identity = directory / "operator-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "fixture-passphrase", "-f", str(identity)], check=True)
    write_document(directory / "setup.json", {"identity": str(identity)}, new=True)
    return directory, identity


def test_locked_key_noninteractive_stops_before_command_and_keeps_state(key, monkeypatch):
    directory, identity = key
    monkeypatch.setattr(session.sys.stdin, "isatty", lambda: False)
    before = identity.read_bytes()
    with pytest.raises(InstallError, match="No cloud access was attempted"):
        session.run_session(directory, ["--", sys.executable, "-c", "raise AssertionError('must not execute')"])
    assert identity.read_bytes() == before


def test_cli_session_rejects_missing_command_without_reading_operator_state(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["operator-session", "--state-dir", str(tmp_path), "--"])
    assert error.value.code == 2
    assert "requires a local command" in capsys.readouterr().err


def test_unencrypted_key_needs_no_agent_or_terminal(key, monkeypatch):
    directory, identity = key
    subprocess.run(["ssh-keygen", "-q", "-p", "-P", "fixture-passphrase", "-N", "", "-f", str(identity)],
                   stdout=subprocess.DEVNULL, check=True)
    monkeypatch.setattr(session.sys.stdin, "isatty", lambda: False)
    assert session.run_session(directory, [sys.executable, "-c", "raise SystemExit(7)"]) == 7
    assert "SSH_AUTH_SOCK" not in os.environ


def test_invalid_private_permissions_stop_before_ssh_tools(key, monkeypatch):
    directory, identity = key
    identity.chmod(0o644)
    monkeypatch.setattr(session, "_capture", lambda *a, **k: pytest.fail("must validate private input first"))
    with pytest.raises(InstallError, match="private regular file"):
        session.run_session(directory, ["must-not-run"])


def test_temporary_agent_is_owned_cleaned_and_does_not_change_parent_environment():
    before = dict(os.environ)
    with session._temporary_agent(before) as environment:
        socket, pid = Path(environment["SSH_AUTH_SOCK"]), int(environment["SSH_AGENT_PID"])
        assert socket.exists() and os.environ == before
        assert subprocess.run(["ssh-add", "-l"], env=environment, capture_output=True).returncode == 1
    assert not socket.exists() and os.environ == before
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_unlock_cancellation_cleans_only_owned_agent_without_running_command(key, monkeypatch):
    directory, identity = key
    monkeypatch.setattr(session.sys.stdin, "isatty", lambda: True)
    original = subprocess.run
    observed = []

    def run(arguments, **options):
        if arguments == ["ssh-add", str(identity)]:
            observed.append(options["env"])
            assert options["env"]["SSH_ASKPASS_REQUIRE"] == "never"
            raise KeyboardInterrupt
        return original(arguments, **options)

    monkeypatch.setattr(session.subprocess, "run", run)
    with pytest.raises(InstallError, match="cancelled.*no cloud access"):
        session.run_session(directory, ["must-not-run"])
    assert len(observed) == 1 and not Path(observed[0]["SSH_AUTH_SOCK"]).exists()
    assert "SSH_AUTH_SOCK" not in os.environ


def test_existing_agent_matching_key_is_reused_and_never_stopped(key, monkeypatch):
    directory, identity = key
    subprocess.run(["ssh-keygen", "-q", "-p", "-P", "fixture-passphrase", "-N", "", "-f", str(identity)],
                   stdout=subprocess.DEVNULL, check=True)
    with session._temporary_agent(dict(os.environ)) as environment:
        subprocess.run(["ssh-add", str(identity)], env=environment, capture_output=True, check=True)
        # Re-encrypt the file: only the already-unlocked agent can serve the key.
        subprocess.run(["ssh-keygen", "-q", "-p", "-P", "", "-N", "fixture-passphrase", "-f", str(identity)],
                       stdout=subprocess.DEVNULL, check=True)
        monkeypatch.setenv("SSH_AUTH_SOCK", environment["SSH_AUTH_SOCK"])
        monkeypatch.setattr(session.sys.stdin, "isatty", lambda: False)
        assert session.run_session(directory, [sys.executable, "-c", "raise SystemExit(0)"]) == 0
        assert Path(environment["SSH_AUTH_SOCK"]).exists()
        assert subprocess.run(["ssh-add", "-l"], env=environment, capture_output=True).returncode == 0


def test_real_terminal_unlocks_encrypted_key_child_inherits_agent_and_agent_is_reaped(key):
    import errno
    import pty

    directory, identity = key
    original_key = identity.read_bytes()
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(sys.executable, [sys.executable, "-c",
                 "import sys; from reasoning_ledger.deployment.operator_session import run_session; "
                 "from pathlib import Path; "
                 "sys.exit(run_session(Path(sys.argv[1]), [sys.executable, '-c', "
                 "\"import os,subprocess; print('SOCKET='+os.environ['SSH_AUTH_SOCK'], flush=True); "
                 "print('AGENT='+os.environ['SSH_AGENT_PID'], flush=True); "
                 "assert subprocess.run(['ssh-add','-l'],capture_output=True).returncode==0\"]))", str(directory)])
    output, sent, finished, terminal_closed = bytearray(), False, False, False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not terminal_closed and select.select([terminal], [], [], 0.1)[0]:
                try:
                    block = os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    block = b""
                terminal_closed = not block
                output.extend(block)
                if b"Enter passphrase" in output and not sent:
                    os.write(terminal, b"fixture-passphrase\n")
                    sent = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                finished = True
                assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors="replace")
                break
            if terminal_closed:
                # PTY EOF can precede the child's waitable exit. Preserve the
                # deadline and exit-status check rather than racing waitpid.
                time.sleep(0.01)
        if not finished:
            waited, status = os.waitpid(pid, os.WNOHANG)
            finished = bool(waited)
            assert finished and os.waitstatus_to_exitcode(status) == 0, output.decode(errors="replace")
        assert sent and b"fixture-passphrase" not in output
        assert identity.read_bytes() == original_key
        lines = output.decode().splitlines()
        socket = Path(next(line.removeprefix("SOCKET=") for line in lines if line.startswith("SOCKET=")))
        agent = int(next(line.removeprefix("AGENT=") for line in lines if line.startswith("AGENT=")))
        assert not socket.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(agent, 0)
    finally:
        os.close(terminal)
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_adjacent_mismatched_public_key_cannot_claim_agent_readiness(key, monkeypatch):
    directory, identity = key
    other = directory / "other-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    identity.with_suffix(".pub").write_bytes(other.with_suffix(".pub").read_bytes())
    with session._temporary_agent(dict(os.environ)) as environment:
        subprocess.run(["ssh-add", str(other)], env=environment, capture_output=True, check=True)
        monkeypatch.setenv("SSH_AUTH_SOCK", environment["SSH_AUTH_SOCK"])
        monkeypatch.setattr(session.sys.stdin, "isatty", lambda: False)
        with pytest.raises(InstallError, match="No cloud access was attempted"):
            session.run_session(directory, ["must-not-run"])


@pytest.mark.parametrize("target", ["doctor", "upgrade-resume"])
def test_make_doctor_keeps_agent_across_both_cli_calls_and_preserves_state_override(key, monkeypatch, target):
    directory, identity = key
    subprocess.run(["ssh-keygen", "-q", "-p", "-P", "fixture-passphrase", "-N", "", "-f", str(identity)],
                   stdout=subprocess.DEVNULL, check=True)
    fixture = directory / "uv-fixture"
    log = directory / "calls"
    fixture.write_text(
        f"#!{sys.executable}\nimport os,sys\nfrom reasoning_ledger.deployment.cli import main\n"
        "args=sys.argv[1:]\n"
        "if args[:4]==['run','--locked','reasoning-deploy','operator-session']: sys.exit(main(args[3:]))\n"
        "assert args[:3]==['run','--locked','reasoning-deploy'], args\n"
        "assert args[3] in {'operator-sync','operator-doctor','operator-resume-upgrade'}, args\n"
        f"assert args[args.index('--state-dir')+1]=={str(directory)!r}, args\n"
        "assert os.environ['SSH_AUTH_SOCK']==os.environ['EXPECTED_SOCKET']\n"
        f"with open({str(log)!r},'a') as stream: stream.write(args[3]+'\\n')\n"
    )
    fixture.chmod(0o700)
    with session._temporary_agent(dict(os.environ)) as environment:
        subprocess.run(["ssh-add", str(identity)], env=environment, capture_output=True, check=True)
        monkeypatch.setenv("EXPECTED_SOCKET", environment["SSH_AUTH_SOCK"])
        response = subprocess.run(["make", target, "UV=" + str(fixture), "DEPLOY_STATE=" + str(directory)],
                                  env={**os.environ, **environment}, capture_output=True, text=True)
        assert response.returncode == 0, response.stderr
        assert log.read_text().splitlines() == (["operator-sync", "operator-doctor"] if target == "doctor" else ["operator-resume-upgrade"])
        assert Path(environment["SSH_AUTH_SOCK"]).exists()


@pytest.mark.parametrize("fail_sync", [False, True])
@pytest.mark.parametrize("use_nvm", [False, True])
def test_make_upgrade_keeps_one_session_through_retention_build_and_upgrade(key, monkeypatch, fail_sync, use_nvm):
    directory, identity = key
    subprocess.run(["ssh-keygen", "-q", "-p", "-P", "fixture-passphrase", "-N", "", "-f", str(identity)],
                   stdout=subprocess.DEVNULL, check=True)
    log, bundle = directory / "calls", directory / "release.tar.gz"
    bundle.write_bytes(b"previous fixture release")
    uv, npm, node = (directory / name for name in ("uv-fixture", "npm-fixture", "node"))
    recording = (
        "import os,sys\nfrom pathlib import Path\n"
        "assert os.environ['SSH_AUTH_SOCK']==os.environ['EXPECTED_SOCKET']\n"
        + ("assert os.environ['REASONING_FIXTURE_NODE24']=='24'\n" if use_nvm else "") +
        f"def record(value):\n with open({str(log)!r},'a') as stream: stream.write(value+'\\n')\n"
    )
    uv.write_text(
        f"#!{sys.executable}\nimport subprocess\nfrom reasoning_ledger.deployment.cli import main\n" + recording +
        "args=sys.argv[1:]\n"
        "if args[:3]==['run','--no-sync','reasoning-dev']: sys.exit(subprocess.run(args[args.index('--')+1:]).returncode)\n"
        "if args[:4]==['run','--locked','reasoning-deploy','operator-session']: sys.exit(main(args[3:]))\n"
        "if args[0] in {'sync','build'}: record('uv-'+args[0]); sys.exit(0)\n"
        "assert args[:3]==['run','--locked','reasoning-deploy'], args\n"
        "operation=args[3]; record(operation)\n"
        "if operation=='operator-sync': assert '--require-no-pending-upgrade' in args\n"
        f"if '--state-dir' in args: assert args[args.index('--state-dir')+1]=={str(directory)!r}, args\n"
        f"if operation=='operator-sync' and {fail_sync!r}: sys.exit(2)\n"
        "if operation=='bundle': Path(args[args.index('--output')+1]).write_bytes(b'new fixture release')\n"
    )
    npm.write_text(f"#!{sys.executable}\n" + recording + "record('npm-'+sys.argv[3])\n")
    node.write_text(f"#!{sys.executable}\nimport os,sys\n"
                    "if sys.argv[1:] == ['--version']: print('v24.15.0')\n"
                    "else:\n assert sys.argv[1]=='web/check-node.cjs'\n"
                    f" sys.exit(2 if {use_nvm!r} and os.environ.get('REASONING_FIXTURE_NODE24') != '24' else 0)\n")
    if use_nvm:
        (directory / "nvm.sh").write_text('test "$1" = --no-use || return 41\n'
                                           'nvm() { test "$1" = use && test "$3" = 24 || return 42; '
                                           'export REASONING_FIXTURE_NODE24=24; }\n')
    for program in (uv, npm, node):
        program.chmod(0o700)
    with session._temporary_agent(dict(os.environ)) as environment:
        subprocess.run(["ssh-add", str(identity)], env=environment, capture_output=True, check=True)
        monkeypatch.setenv("EXPECTED_SOCKET", environment["SSH_AUTH_SOCK"])
        response = subprocess.run(
            ["make", "upgrade", "UV=" + str(uv), "NPM=" + str(npm), "DEPLOY_STATE=" + str(directory),
             "RELEASE=" + str(bundle), "ALLOW_DIRTY=1"],
            env={**os.environ, **environment, "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                 **({"NVM_DIR": str(directory)} if use_nvm else {})},
            capture_output=True, text=True,
        )
        assert Path(environment["SSH_AUTH_SOCK"]).exists()
    if fail_sync:
        assert response.returncode != 0 and log.read_text().splitlines() == ["operator-sync"]
        assert bundle.read_bytes() == b"previous fixture release"
    else:
        assert response.returncode == 0, response.stderr
        assert log.read_text().splitlines() == [
            "operator-sync", "upgrade-stage", "uv-sync", "npm-ci", "npm-run", "bundle", "inspect", "upgrade",
        ]
        assert bundle.read_bytes() == b"new fixture release"


@pytest.mark.parametrize("fail", [False, True])
def test_in_process_agent_environment_is_scoped_and_restored(monkeypatch, fail):
    monkeypatch.setenv("SSH_AUTH_SOCK", "original-socket")
    monkeypatch.delenv("SSH_AGENT_PID", raising=False)
    observed = []

    @contextmanager
    def prepared(identity):
        assert identity == Path("/private/fixture-key")
        try:
            yield {**os.environ, "SSH_AUTH_SOCK": "temporary-socket", "SSH_AGENT_PID": "123"}
        finally:
            observed.append(os.environ.get("SSH_AUTH_SOCK"))

    monkeypatch.setattr(session, "authenticated_environment", prepared)
    try:
        with session.authenticated_session(Path("/private/fixture-key")):
            assert os.environ["SSH_AUTH_SOCK"] == "temporary-socket" and os.environ["SSH_AGENT_PID"] == "123"
            if fail:
                raise RuntimeError("fixture interruption")
    except RuntimeError:
        assert fail
    assert os.environ["SSH_AUTH_SOCK"] == "original-socket" and "SSH_AGENT_PID" not in os.environ
    assert observed == ["original-socket"]
