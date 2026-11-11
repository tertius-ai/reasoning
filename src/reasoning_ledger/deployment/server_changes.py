"""Explicit, resumable server changes over verified SSH, using retained plans."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from . import host, server_operations, server_upgrade

MODES = {"upgrade-prepare", "upgrade-activate", "rollback-prepare", "rollback-activate"}
MAX_SCRIPT = 2 * 1024**2
MAX_RECEIPT = 32768
UNCERTAIN = "remote release change is unverified; host changes may have occurred; resume the original operation and inputs"
LAUNCHER = (
    "import sys; n=int(sys.stdin.buffer.readline(16)); assert 0<n<=2097152; "
    "b=sys.stdin.buffer.read(n); assert len(b)==n; exec(compile(b,'<reasoning-release-change>','exec'))"
)


def busy_report(request, engine, error):
    """A refusal before the source was fenced or stopped, with its allowlisted diagnosis; None otherwise."""
    try:
        if (not isinstance(error, server_upgrade.ServerBusy) or error.reason not in server_upgrade.BUSY_REASONS
                or request["mode"] != "upgrade-prepare"
                or not all(server_upgrade._hex(request[key]) for key in ("operation_id", "plan_id"))):
            return None
        path = engine.directory / "server-upgrades" / request["operation_id"] / "operation.json"
        if path.exists() or path.is_symlink():
            state = engine._read(path)
            if (state["operation_id"] != request["operation_id"] or state["plan_id"] != request["plan_id"]
                    or host.canonical(state["identity"]) != host.canonical(request["identity"])):
                return None
            phase = state["phase"]
        else:
            phase = "none"
        if phase not in server_upgrade.BUSY_PHASES:
            return None
        return {"format": 1, "kind": "server_busy", "mode": request["mode"], "operation_id": request["operation_id"],
                "plan_id": request["plan_id"], "phase": phase, "reason": error.reason}
    except Exception:
        return None


def failure_report(request, *, engine=None, error=None):
    """Only identity-bound phases and allowlisted diagnoses; never exception/log text."""
    phase = "unknown"
    reason = None
    try:
        busy = busy_report(request, engine or server_upgrade.HostUpgrade(), error)
        if busy is not None:
            return busy
    except Exception:
        pass
    try:
        if request["mode"] not in MODES or not all(server_upgrade._hex(request[key]) for key in ("operation_id", "plan_id")):
            raise ValueError
        engine = engine or server_upgrade.HostUpgrade()
        state = engine._read(engine.directory / "server-upgrades" / request["operation_id"] / "operation.json")
        if (state["operation_id"] == request["operation_id"] and state["plan_id"] == request["plan_id"]
                and host.canonical(state["identity"]) == host.canonical(request["identity"])
                and state["phase"] in server_upgrade.PHASES | server_upgrade.ACTIVATION_PHASES):
            phase = state["phase"]
            if phase == "migrating":
                reason = server_upgrade.migration_failure_reason(error)
    except Exception:
        pass
    result = {"format": 1, "kind": "release_change_failure", "mode": request.get("mode"),
              "operation_id": request.get("operation_id"), "plan_id": request.get("plan_id"), "phase": phase}
    if reason is not None:
        result["reason"] = reason
    return result


def reject_busy(value, request):
    """Raise the operator's explanation of a refusal made before the server was stopped."""
    expected = {"format": 1, "kind": "server_busy", "mode": "upgrade-prepare", "operation_id": request["operation_id"],
                "plan_id": request["plan_id"], "phase": value.get("phase"), "reason": value.get("reason")}
    if (request["mode"] != "upgrade-prepare" or set(value) != set(expected)
            or any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items())
            or value["phase"] not in server_upgrade.BUSY_PHASES or value["reason"] not in server_upgrade.BUSY_REASONS):
        raise ValueError("invalid bound busy report")
    nothing_pending = value["phase"] == "none"
    raise host.InstallError(
        "Upgrade refused before stopping the server: " + server_upgrade.BUSY_REASONS[value["reason"]] + ". "
        "The server was not stopped and keeps serving"
        + (". No upgrade is pending; " if nothing_pending else "; this upgrade is pending but has not fenced or stopped anything. ")
        + "Let that work finish or stop it, then run "
        + ("make upgrade again." if nothing_pending else "make upgrade-resume.")
    )


def reject_failure(value, request):
    if isinstance(value, dict) and value.get("kind") == "server_busy":
        reject_busy(value, request)
    if not isinstance(value, dict) or value.get("kind") != "release_change_failure":
        return
    expected = {"format": 1, "kind": "release_change_failure", "mode": request["mode"],
                "operation_id": request["operation_id"], "plan_id": request["plan_id"], "phase": value.get("phase")}
    detail = ""
    if "reason" in value:
        reason = value["reason"]
        if not isinstance(reason, str) or reason not in server_upgrade.MIGRATION_FAILURE_REASONS or value["phase"] != "migrating":
            raise ValueError("invalid bound migration diagnosis")
        expected["reason"] = reason
        detail = ". Migration diagnosis: " + reason + " — " + server_upgrade.MIGRATION_FAILURE_REASONS[reason]
    phases = server_upgrade.PHASES | server_upgrade.ACTIVATION_PHASES | {"unknown"}
    if (set(value) != set(expected) or any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items())
            or not isinstance(value["phase"], str) or value["phase"] not in phases):
        raise ValueError("invalid bound failure report")
    raise host.InstallError(UNCERTAIN + ". Reported operation phase: " + value["phase"]
                            + detail + ". Preserve the original backup and journals; no completion receipt was accepted. "
                            "Resume with make upgrade-resume; do not rebuild the pending target.")


def validate_plan(plan, confirmation):
    """A saved passing plan is an identity, never a substitute for host preflight."""
    try:
        fields = set(server_upgrade.IDENTITY_FIELDS) | {
            "plan_id",
            "refusals",
            "preflight_checks_passed",
            "upgrade_performed",
            "host_changed",
            "pending_upgrade_checks",
            "diagnosis",
        }
        if not isinstance(plan, dict) or set(plan) != fields:
            raise ValueError
        identity = {name: plan[name] for name in server_upgrade.IDENTITY_FIELDS}
        if (
            type(identity["format"]) is not int
            or identity["format"] != 1
            or identity["operation"] != "upgrade-plan"
            or not server_upgrade._hex(confirmation)
            or plan["plan_id"] != confirmation
            or hashlib.sha256(host.canonical(identity)).hexdigest() != confirmation
            or plan["refusals"] != []
            or plan["preflight_checks_passed"] is not True
            or plan["upgrade_performed"] is not False
            or plan["host_changed"] is not False
            or plan["pending_upgrade_checks"] != server_operations.PENDING
        ):
            raise ValueError
        for binding in (identity["source"], identity["target"]):
            if (
                not isinstance(binding, dict)
                or set(binding) != {"release_id", "bundle_sha256", "public_url", "source_revision", "dirty"}
                or not server_upgrade._hex(binding["release_id"])
                or not server_upgrade._hex(binding["bundle_sha256"])
                or not isinstance(binding["source_revision"], str)
                or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", binding["source_revision"]) is None
                or binding["dirty"] is not False
                or host.public_origin(binding["public_url"]) != binding["public_url"]
            ):
                raise ValueError
        if (
            identity["source"]["public_url"] != identity["target"]["public_url"]
            or identity["source"]["release_id"] == identity["target"]["release_id"]
            or any(type(identity[name]) is not int for name in ("source_database_schema", "target_database_schema"))
            or not 13 <= identity["source_database_schema"] <= identity["target_database_schema"] <= 1000
        ):
            raise ValueError
        report = server_operations.validate_report(plan["diagnosis"], identity["source"], identity["source_database_schema"])
        if host.canonical(report["configuration"]) != host.canonical(identity["configuration"]) or any(
            value not in {"pass", "not_applicable"} and not (name == "https_release_identity" and value == "unverified")
            for name, value in report["checks"].items()
        ):
            raise ValueError
        return identity
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise host.InstallError("release change requires the exact saved passing upgrade plan") from exc


def _run(args, chunks, *, timeout=7200):
    """Stream bounded inputs/output; a killed local SSH is not a stopped remote."""
    if type(timeout) not in {int, float} or not 0 < timeout <= 7200:
        raise host.InstallError("invalid release-change transport deadline")
    deadline = time.monotonic() + timeout
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
        complete = False
        try:
            assert child.stdin is not None and child.stdout is not None
            os.set_blocking(child.stdin.fileno(), False)
            os.set_blocking(child.stdout.fileno(), False)
            inputs, remaining, body = iter(chunks), memoryview(b""), bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdin, selectors.EVENT_WRITE)
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise host.InstallError(UNCERTAIN)
                    for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                        if key.fileobj is child.stdin:
                            if not remaining:
                                block = next(inputs, None)
                                if block is None:
                                    selector.unregister(child.stdin)
                                    child.stdin.close()
                                    continue
                                if not isinstance(block, bytes) or not 0 < len(block) <= 65536:
                                    raise host.InstallError(UNCERTAIN)
                                remaining = memoryview(block)
                            written = os.write(child.stdin.fileno(), remaining)
                            remaining = remaining[written:]
                        else:
                            block = os.read(child.stdout.fileno(), 65536)
                            body.extend(block)
                            if len(body) > MAX_RECEIPT:
                                raise host.InstallError(UNCERTAIN)
                            if not block:
                                selector.unregister(child.stdout)
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
                if child.returncode:
                    raise host.InstallError(UNCERTAIN)
                complete = True
                return subprocess.CompletedProcess(args, child.returncode, stdout=bytes(body))
        except (OSError, subprocess.SubprocessError) as exc:
            raise host.InstallError(UNCERTAIN) from exc
        finally:
            if not complete:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def remote_script(request):
    return _remote_script(
        request,
        (
            ("host", host),
            ("server_operations", server_operations),
            ("server_upgrade", server_upgrade),
            ("server_changes", sys.modules[__name__]),
        ),
        endpoint="server_changes",
    )


def _remote_script(request, modules, *, endpoint):
    """Load trusted local standard-library tooling, never uploaded executables."""
    sources = []
    if not re.fullmatch(r"[a-z_]+", endpoint):
        raise host.InstallError("invalid release-change endpoint")
    for name, module in modules:
        body = Path(module.__file__).read_bytes()
        if len(body) > 256 * 1024:
            raise host.InstallError("release-change tooling exceeds its source allowance")
        sources.append((name, base64.b64encode(body).decode("ascii")))
    encoded = base64.b64encode(host.canonical(request)).decode("ascii")
    failure = ("print(json.dumps(package.server_changes.failure_report(request, error=exc), sort_keys=True))"
               if endpoint == "server_changes"
               else "raise SystemExit('release change failed; retain original operation and protected recovery evidence') from None")
    script = f"""import base64, json, os, sys, types
if os.geteuid() != 0:
    raise SystemExit('release change requires root')
package = types.ModuleType('reasoning_change')
package.__path__ = []
sys.modules[package.__name__] = package
for name, source in {sources!r}:
    module = types.ModuleType(package.__name__ + '.' + name)
    module.__package__ = package.__name__
    sys.modules[module.__name__] = module
    setattr(package, name, module)
    exec(compile(base64.b64decode(source), '<reasoning-' + name + '>', 'exec'), module.__dict__)
request = json.loads(base64.b64decode({encoded!r}))
try:
    receipt = package.{endpoint}.execute_remote(request, sys.stdin.buffer)
    print(json.dumps(receipt, sort_keys=True))
except Exception as exc:
    {failure}
""".encode("ascii")
    if len(script) > MAX_SCRIPT:
        raise host.InstallError("release-change script exceeds its source allowance")
    return script


@contextmanager
def snapshot_ssh(hostname, identity, known_hosts):
    """Freeze operator SSH trust before journaling and network access."""
    from .cli import ssh_arguments
    from .private import protected_temporary_directory, read_private

    ssh_arguments(hostname, identity, known_hosts)
    key_body = read_private(identity, 65536)
    fd = os.open(known_hosts, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {os.getuid(), Path("/").stat().st_uid}
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or info.st_size > 1024**2
        ):
            raise host.InstallError("known-hosts input changed or is not protected")
        known_body = stream.read(1024**2 + 1)
        if len(known_body) > 1024**2 or len(known_body) != info.st_size:
            raise host.InstallError("known-hosts input changed or exceeds its allowance")
    transport = {
        "hostname": hostname,
        "identity_sha256": hashlib.sha256(key_body).hexdigest(),
        "known_hosts_sha256": hashlib.sha256(known_body).hexdigest(),
    }
    with protected_temporary_directory("reasoning-change-trust-") as trust:
        for name, body in (("identity", key_body), ("known-hosts", known_body)):
            path = trust / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(body)
        yield ssh_arguments(hostname, trust / "identity", trust / "known-hosts"), transport


def execute_remote(request, stream, *, engine=None):
    """Root endpoint: unique private uploads, then the original guarded backend."""
    engine = engine or server_upgrade.HostUpgrade()
    mode, identity = request["mode"], request["identity"]
    operation, plan = request["operation_id"], request["plan_id"]
    if mode not in MODES or not server_upgrade._hex(operation) or not server_upgrade._hex(plan):
        raise host.InstallError("invalid original release-change identity")
    if hashlib.sha256(host.canonical(identity)).hexdigest() != plan:
        raise host.InstallError("release-change plan identity changed")
    if mode == "upgrade-prepare":
        engine.inspector._directory(engine.directory, private=True)
        parent = engine.installer._directory("var/lib/reasoning-deploy/staging", 0o700)
        with tempfile.TemporaryDirectory(prefix="server-change-", dir=parent) as temporary:
            archives = []
            sizes = request["upload_sizes"]
            if (
                not isinstance(sizes, list)
                or len(sizes) != 2
                or any(type(size) is not int or not 0 < size <= host.MAX_BUNDLE for size in sizes)
            ):
                raise host.InstallError("invalid verified archive lengths")
            for name, size in zip(("source", "target"), sizes, strict=True):
                archive = Path(temporary) / (name + ".tar.gz")
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    remaining = size
                    while remaining:
                        body = stream.read(min(remaining, 65536))
                        if not body:
                            raise host.InstallError("verified archive upload was interrupted")
                        output.write(body)
                        remaining -= len(body)
                manifest, release, checksum = host.inspect_bundle(archive, identity[name]["bundle_sha256"])
                if (
                    server_operations.release_binding(manifest, release, checksum, identity[name]["public_url"]) != identity[name]
                    or manifest["database_schema"] != identity[name + "_database_schema"]
                ):
                    raise host.InstallError("uploaded archive differs from the saved plan")
                archives.append(archive)
            if stream.read(1):
                raise host.InstallError("unexpected bytes after verified archives")
            return engine.prepare(
                *archives,
                identity["source"]["bundle_sha256"],
                identity["target"]["bundle_sha256"],
                identity["source"]["public_url"],
                operation_id=operation,
                confirmed_plan=plan,
                workers_fenced=request["workers_fenced"],
                maximum=request["maximum_bytes"],
            )
    if stream.read(1):
        raise host.InstallError("activation and rollback cannot accept replacement archives")
    retained = engine._read(engine.directory / "server-upgrades" / operation / "operation.json")
    if host.canonical(retained["identity"]) != host.canonical(identity) or retained["plan_id"] != plan:
        raise host.InstallError("retained host operation differs from the saved plan")
    options = {"operation_id": operation, "confirmed_plan": plan, "target_release": identity["target"]["release_id"]}
    if mode == "upgrade-activate":
        return engine.activate(**options, activation_confirmed=request["activation_confirmed"])
    options.update(rollback_id=request["rollback_id"], source_release=identity["source"]["release_id"])
    if mode == "rollback-prepare":
        return engine.rollback_prepare(
            **options, workers_fenced=request["workers_fenced"], data_loss_confirmed=request["data_loss_confirmed"]
        )
    return engine.rollback_activate(**options, activation_confirmed=request["activation_confirmed"])


def validate_receipt(value, request):
    """Only bounded typed facts with exact original bindings may reach stdout."""
    try:
        mode, identity = request["mode"], request["identity"]
        expected = {
            "format": 1,
            "operation": mode,
            "operation_id": request["operation_id"],
            "plan_id": request["plan_id"],
            "source": identity["source"],
            "target": identity["target"],
            "phase": "prepared" if mode.endswith("prepare") else "completed",
            "server_restart_fenced": mode.endswith("prepare"),
            "root_private_backup_retained": True,
            "runtime_dependency_attestation": False,
            "recovery_directory": "/var/lib/reasoning-deploy/server-upgrades/" + request["operation_id"],
        }
        if mode.startswith("upgrade"):
            expected.update(activation_performed=mode.endswith("activate"), upgrade_completed=mode.endswith("activate"))
        else:
            expected.update(
                rollback_id=request["rollback_id"],
                newer_data_loss_confirmed=True,
                source_activation_performed=mode.endswith("activate"),
                rollback_completed=mode.endswith("activate"),
                displaced_state_retained=True,
                displaced_database_validated=False,
            )
            restoration = value["restoration"]
            fields = {
                "format",
                "operation_id",
                "rollback_id",
                "plan_id",
                "source_release",
                "source_schema",
                "backup_sha256",
                "backup_key_sha256",
                "mail_sha256",
                "maximum_bytes",
                "phase",
                "state_sha256",
                "recovery",
            }
            if not isinstance(restoration, dict) or set(restoration) != fields:
                raise ValueError
            fixed = {
                "format": 1,
                "operation_id": request["operation_id"],
                "rollback_id": request["rollback_id"],
                "plan_id": request["plan_id"],
                "source_release": identity["source"]["release_id"],
                "source_schema": identity["source_database_schema"],
                "mail_sha256": identity["configuration"]["mail_input_sha256"],
                "maximum_bytes": request["maximum_bytes"],
                "phase": "prepared",
            }
            if any(type(restoration[key]) is not type(item) or restoration[key] != item for key, item in fixed.items()) or any(
                not server_upgrade._hex(restoration[key]) for key in ("state_sha256", "backup_sha256", "backup_key_sha256")
            ):
                raise ValueError
            counts = {
                "sessions_revoked",
                "invitations_revoked",
                "workers_revoked",
                "bots_revoked",
                "webhooks_revoked",
                "scalers_revoked",
                "inference_runs_held",
                "compute_jobs_held",
            }
            recovery = restoration["recovery"]
            displaced = value["displaced"]
            if (
                not isinstance(recovery, dict)
                or not counts <= set(recovery) <= counts | {"workflow_runs_held", "work_workers_revoked"}
                or any(type(number) is not int or not 0 <= number <= 2**63 - 1 for number in recovery.values())
                or not isinstance(displaced, dict)
                or set(displaced) != {"sha256", "files", "bytes"}
                or not server_upgrade._hex(displaced["sha256"])
                or type(displaced["files"]) is not int
                or not 0 <= displaced["files"] <= 262247
                or type(displaced["bytes"]) is not int
                or not 0 <= displaced["bytes"] <= 2 * server_upgrade.MAX_BACKUP
            ):
                raise ValueError
            expected.update(restoration=restoration, displaced=displaced)
        if mode.endswith("activate"):
            expected.update(https_release_verified=True, database_schema_verified=True)
        if (
            not isinstance(value, dict)
            or set(value) != set(expected)
            or any(
                type(value[key]) is not type(item) or host.canonical(value[key]) != host.canonical(item) for key, item in expected.items()
            )
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise host.InstallError(UNCERTAIN) from exc


def change_remote(
    mode,
    plan,
    hostname,
    identity,
    known_hosts,
    state_directory,
    *,
    operation_id,
    confirmed_plan,
    source_bundle=None,
    target_bundle=None,
    workers_fenced=False,
    activation_confirmed=False,
    rollback_id=None,
    data_loss_confirmed=False,
    maximum=None,
    command=None,
):
    from .cli import bundle_snapshot
    from .private import locked_directory, read_document, write_document

    binding = validate_plan(plan, confirmed_plan)
    if (
        mode not in MODES
        or not server_upgrade._hex(operation_id)
        or (mode.startswith("rollback") != (rollback_id is not None))
        or rollback_id is not None
        and not server_upgrade._hex(rollback_id)
        or workers_fenced is not mode.endswith("prepare")
        or activation_confirmed is not mode.endswith("activate")
        or data_loss_confirmed is not (mode == "rollback-prepare")
        or (mode == "upgrade-prepare") != (source_bundle is not None and target_bundle is not None)
        or mode != "upgrade-prepare"
        and (source_bundle is not None or target_bundle is not None or maximum is not None)
        or maximum is not None
        and (type(maximum) is not int or not 1024**2 <= maximum <= 64 * 1024**3)
    ):
        raise host.InstallError("release change requires exact original identities and mode-specific acknowledgments/inputs")
    with ExitStack() as stack:
        options, transport = stack.enter_context(snapshot_ssh(hostname, identity, known_hosts))
        archives = []
        if mode == "upgrade-prepare":
            for name, original in (("source", source_bundle), ("target", target_bundle)):
                archive = stack.enter_context(bundle_snapshot(original, binding[name]["release_id"]))
                manifest, release, checksum = host.inspect_bundle(archive, binding[name]["bundle_sha256"])
                if (
                    server_operations.release_binding(manifest, release, checksum, binding[name]["public_url"]) != binding[name]
                    or manifest["database_schema"] != binding[name + "_database_schema"]
                ):
                    raise host.InstallError("release archive differs from the saved plan")
                archives.append(archive)
        directory = stack.enter_context(locked_directory(state_directory))
        local = directory / "operation.json"
        if local.exists() or local.is_symlink():
            state = read_document(local)
            allowance = state.get("maximum_bytes")
        else:
            if mode != "upgrade-prepare":
                raise host.InstallError("retain the original operator operation directory for activation and rollback")
            allowance = 8 * 1024**3 if maximum is None else maximum
            state = None
        expected = {
            "format": 1,
            "operation_id": operation_id,
            "plan_id": confirmed_plan,
            "identity": binding,
            "transport": transport,
            "maximum_bytes": allowance,
        }
        if (
            type(allowance) is not int
            or not 1024**2 <= allowance <= 64 * 1024**3
            or state is not None
            and host.canonical(state) != host.canonical(expected)
        ):
            raise host.InstallError("original operator operation, host trust or plan binding changed")
        if maximum is not None and maximum != allowance:
            raise host.InstallError("retain the original operation backup allowance")
        if state is None:
            write_document(local, expected, new=True)
        if rollback_id is not None:
            rollback = directory / "rollback.json"
            record = {"format": 1, "operation_id": operation_id, "rollback_id": rollback_id, "plan_id": confirmed_plan}
            if rollback.exists() or rollback.is_symlink():
                if host.canonical(read_document(rollback)) != host.canonical(record):
                    raise host.InstallError("retain the original operator rollback identity")
            elif mode == "rollback-prepare":
                write_document(rollback, record, new=True)
            else:
                raise host.InstallError("retain the original operator rollback preparation record")
        request = {
            "mode": mode,
            "identity": binding,
            "operation_id": operation_id,
            "plan_id": confirmed_plan,
            "maximum_bytes": allowance,
            "rollback_id": rollback_id,
            "workers_fenced": workers_fenced,
            "activation_confirmed": activation_confirmed,
            "data_loss_confirmed": data_loss_confirmed,
            "upload_sizes": [path.stat().st_size for path in archives],
        }
        script = remote_script(request)

        def chunks():
            yield (str(len(script)) + "\n").encode("ascii")
            for offset in range(0, len(script), 65536):
                yield script[offset : offset + 65536]
            for archive in archives:
                with archive.open("rb") as stream:
                    while body := stream.read(65536):
                        yield body

        args = ["ssh", "-T", *options, "root@" + hostname, shlex.join(["python3", "-I", "-S", "-B", "-c", LAUNCHER])]
        try:
            response = (command or _run)(args, chunks())
            if response.returncode != 0 or not isinstance(response.stdout, bytes) or len(response.stdout) > MAX_RECEIPT:
                raise ValueError
            value = json.loads(response.stdout, object_pairs_hook=server_upgrade._pairs)
            reject_failure(value, request)
            receipt = validate_receipt(value, request)
        except (AttributeError, OSError, TypeError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
            raise host.InstallError(UNCERTAIN) from exc
        receipt_path = directory / (mode + ".json")
        if receipt_path.exists() or receipt_path.is_symlink():
            if host.canonical(read_document(receipt_path)) != host.canonical(receipt):
                raise host.InstallError(UNCERTAIN)
        else:
            write_document(receipt_path, receipt, new=True)
        return receipt
